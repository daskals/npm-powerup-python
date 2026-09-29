"""Control of an nPM1300 EK through its firmware shell."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import logparse
from .logparse import (
    AdcSample,
    ChargingState,
    LogEvent,
    ProfilingSample,
    leading_int,
    value_after_colon,
)
from .ports import find_shell_port
from .shell import (
    PortSource,
    ShellCommandError,
    ShellDisconnected,
    ShellError,
    ShellSession,
    ShellTimeout,
)

log = logging.getLogger(__name__)

# Firmware the command set below was written against.
SUPPORTED_FIRMWARE = "1.5.2+0"

# Below this battery voltage the EK is taken to have no battery attached.
BATTERY_CONNECTED_THRESHOLD_V = 1.0

NTC_OHMS = {"ignore": 0, "10k": 10_000, "47k": 47_000, "100k": 100_000}
NTC_BETA = {"10k": 3380, "47k": 4050, "100k": 4250}
ITERM_PERCENT = (10, 20)
TRICKLE_VOLTS = (2.5, 2.9)

VTERM_RANGES = ((3.5, 3.65), (4.0, 4.45))
VTERM_STEP = 0.05
ICHG_RANGE_MA = (32, 800)
ICHG_STEP_MA = 2
JEITA_RANGE_C = (-20, 60)
DIE_TEMP_RANGE_C = (50, 110)
POF_THRESHOLD_RANGE_V = (2.6, 3.5)
VBUS_LIMIT_RANGE_A = (0.1, 1.5)

BUCK_COUNT = 2
BUCK_VOLTAGE_RANGE = (1.0, 3.3)
BUCK_RETENTION_RANGE = (1.0, 3.0)
BUCK_MODE_CONTROL = ("Auto", "PWM", "PFM")
# Buck 2 powers the link between the EK controller and the PMIC.
BUCK2_MINIMUM_SAFE_VOLTS = 1.6

LDO_COUNT = 2
LDO_VOLTAGE_RANGE = (1.0, 3.3)
LDO_SOFT_START_MA = (25, 50, 75, 100)

GPIO_COUNT = 5
GPIO_NAMES = tuple(f"GPIO{i}" for i in range(GPIO_COUNT))
GPIO_MODES = (
    "Input",
    "Input logic 1",
    "Input logic 0",
    "Input rising edge event",
    "Input falling edge event",
    "Output interrupt",
    "Output reset",
    "Output power loss warning",
    "Output logic 1",
    "Output logic 0",
)
GPIO_PULLS = ("Pull down", "Pull up", "Pull disable")
GPIO_DRIVES_MA = (1, 6)

LED_COUNT = 3
LED_MODES = ("Charger error", "Charging", "Host", "Not used")

TIMER_MODES = (
    "Boot monitor",
    "Watchdog warning",
    "Watchdog reset",
    "General purpose",
    "Wake-up",
)
TIMER_PRESCALERS = ("Slow", "Fast")
POF_POLARITIES = ("Active low", "Active high")
LONG_PRESS_RESET = ("one_button", "disabled", "two_button")
SHIP_TIME_TO_ACTIVE_MS = (16, 32, 64, 96, 304, 608, 1008, 3008)

MODEL_CHUNK_BYTES = 256

PMIC_CONNECTED = "pmic-connected"
PMIC_DISCONNECTED = "pmic-disconnected"
PMIC_PENDING_REBOOT = "pmic-pending-reboot"
PMIC_REBOOTING = "pmic-pending-rebooting"


class UnsupportedDevice(Exception):
    pass


def js_number(value: float) -> str:
    """Format a number the way the firmware shell is used to receiving it."""
    if value != value:  # NaN
        return "NaN"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _on_grid(value: float, start: float, step: float) -> bool:
    steps = (value - start) / step
    return abs(steps - round(steps)) < 1e-6


def _in_range(name: str, value: float, limits: tuple, unit: str) -> None:
    low, high = limits
    if not low <= value <= high:
        raise ValueError(f"{name} must be {low} to {high} {unit}, not {value}")


def _one_of(name: str, value, allowed: Sequence) -> int:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {list(allowed)}, not {value!r}")
    return list(allowed).index(value)


def check_vterm(volts: float) -> None:
    for low, high in VTERM_RANGES:
        if low - 1e-9 <= volts <= high + 1e-9 and _on_grid(volts, low, VTERM_STEP):
            return
    raise ValueError(
        f"termination voltage {volts} V is not allowed; use 3.50 to 3.65 V "
        "or 4.00 to 4.45 V in 0.05 V steps"
    )


def check_ichg(milliamps: float) -> None:
    low, high = ICHG_RANGE_MA
    if not (low <= milliamps <= high and _on_grid(milliamps, low, ICHG_STEP_MA)):
        raise ValueError(
            f"charge current {milliamps} mA is not allowed; use {low} to {high} mA "
            f"in {ICHG_STEP_MA} mA steps"
        )


def _gpio_index(name: str, value: str, off: str) -> int:
    """Index of a controlling GPIO, or -1 for software control."""
    if value == off:
        return -1
    return _one_of(name, value, GPIO_NAMES)


def _gpio_name(index: int, off: str) -> str:
    return off if index < 0 else GPIO_NAMES[index]


class Npm1300:
    """An nPM1300 EK.

    Measurements and status changes arrive as log lines and are delivered
    to the ``on_*`` listeners. Listeners run on the reader thread and must
    not send commands themselves.
    """

    def __init__(self, port: Optional[PortSource] = None, **session_options):
        self.shell = ShellSession(port or find_shell_port, **session_options)
        self.auto_reboot = True
        self.pmic_state = PMIC_CONNECTED
        self.latest_adc: Optional[AdcSample] = None
        self.charging_state: Optional[ChargingState] = None
        self.usb_status: Optional[str] = None

        self._adc_listeners: list[Callable[[AdcSample], None]] = []
        self._charging_listeners: list[Callable[[ChargingState], None]] = []
        self._profiling_listeners: list[Callable[[ProfilingSample], None]] = []
        self._usb_listeners: list[Callable[[str], None]] = []
        self._pmic_state_listeners: list[Callable[[str], None]] = []
        self._log_listeners: list[Callable[[LogEvent], None]] = []
        self._reboot_lock = threading.Lock()

        self.shell.on_log(self._handle_log)

    # -- lifecycle ---------------------------------------------------------

    def open(self, check_device: bool = True) -> "Npm1300":
        self.shell.open()
        if check_device:
            hardware = self.hw_version().get("hw_version", "")
            if not hardware.startswith("npm1300ek"):
                self.shell.close()
                raise UnsupportedDevice(
                    f"expected an nPM1300 EK, the device reports {hardware!r}"
                )
        return self

    def close(self) -> None:
        self.shell.close()

    def __enter__(self) -> "Npm1300":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def command(self, command: str, timeout: Optional[float] = None) -> str:
        """Send any shell command, as the app's serial terminal does."""
        return self.shell.command(command, timeout)

    # -- listeners ---------------------------------------------------------

    @staticmethod
    def _subscribe(listeners: list, listener) -> Callable[[], None]:
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    def on_adc_sample(self, listener: Callable[[AdcSample], None]):
        return self._subscribe(self._adc_listeners, listener)

    def on_charging_state(self, listener: Callable[[ChargingState], None]):
        return self._subscribe(self._charging_listeners, listener)

    def on_profiling_sample(self, listener: Callable[[ProfilingSample], None]):
        return self._subscribe(self._profiling_listeners, listener)

    def on_usb_status(self, listener: Callable[[str], None]):
        return self._subscribe(self._usb_listeners, listener)

    def on_pmic_state(self, listener: Callable[[str], None]):
        return self._subscribe(self._pmic_state_listeners, listener)

    def on_log(self, listener: Callable[[LogEvent], None]):
        return self._subscribe(self._log_listeners, listener)

    @staticmethod
    def _emit(listeners: list, value) -> None:
        for listener in list(listeners):
            try:
                listener(value)
            except Exception:
                log.exception("listener failed")

    def _handle_log(self, event: LogEvent) -> None:
        self._emit(self._log_listeners, event)

        if event.module == logparse.MODULE_ADC:
            sample = logparse.parse_adc_sample(event)
            if sample:
                self.latest_adc = sample
                self._emit(self._adc_listeners, sample)
        elif event.module == logparse.MODULE_CHARGER:
            state = logparse.parse_charging_state(event)
            if state:
                self.charging_state = state
                self._emit(self._charging_listeners, state)
        elif event.module == logparse.MODULE_PROFILING:
            sample = logparse.parse_profiling_sample(event)
            if sample:
                self._emit(self._profiling_listeners, sample)
        elif event.module == logparse.MODULE_PMIC:
            self._handle_pmic_message(event.message)
        elif event.module == logparse.MODULE_IRQ:
            irq = logparse.parse_irq(event)
            if irq == ("EVENTSVBUSIN0SET", "EVENTVBUSREMOVED"):
                self._set_usb_status(logparse.USB_STATUS[0])

    def _handle_pmic_message(self, message: str) -> None:
        if message in logparse.USB_LOG_MESSAGES:
            self._set_usb_status(logparse.USB_LOG_MESSAGES[message])
        elif message == "No response from PMIC.":
            self._set_pmic_state(PMIC_DISCONNECTED)
        elif message == "PMIC available. Application can be restarted.":
            if self.pmic_state == PMIC_REBOOTING:
                return
            if self.auto_reboot:
                self._set_pmic_state(PMIC_REBOOTING)
                threading.Thread(target=self._reboot_quietly, daemon=True).start()
            else:
                self._set_pmic_state(PMIC_PENDING_REBOOT)

    def _set_usb_status(self, status: str) -> None:
        if status != self.usb_status:
            self.usb_status = status
            self._emit(self._usb_listeners, status)

    def _set_pmic_state(self, state: str) -> None:
        if state != self.pmic_state:
            self.pmic_state = state
            self._emit(self._pmic_state_listeners, state)

    def _reboot_quietly(self) -> None:
        try:
            self.reboot()
        except ShellError as error:
            log.warning("reboot failed: %s", error)

    # -- helpers -----------------------------------------------------------

    def _get_int(self, command: str) -> int:
        return leading_int(value_after_colon(self.command(command)))

    def _get_bool(self, command: str) -> bool:
        return self._get_int(command) == 1

    def _get_word(self, command: str) -> str:
        return value_after_colon(self.command(command)).strip()

    def _set_bool(self, command: str, enabled: bool) -> None:
        self.command(f"{command} {int(bool(enabled))}")

    @staticmethod
    def _index(index: int, count: int) -> int:
        if not 0 <= index < count:
            raise ValueError(f"index {index} is out of range 0 to {count - 1}")
        return index

    # -- device information ------------------------------------------------

    def hw_version(self) -> dict[str, str]:
        return logparse.parse_key_values(self.command("hw_version"))

    def app_version(self) -> str:
        return self.command("app_version").replace("app_version=", "").strip()

    def firmware_supported(self) -> bool:
        return self.app_version() == SUPPORTED_FIRMWARE

    def pmic_revision(self) -> float:
        reply = self.command("pmic_revision").replace("pmic_revision=", "")
        return float(reply.strip())

    def pmic_powered(self) -> bool:
        try:
            return self.command("npm_pmic_ping check").strip() == "Pinging PMIC succeeded"
        except ShellCommandError:
            return False

    def uptime_ms(self) -> int:
        return leading_int(value_after_colon(self.command("kernel uptime")))

    def reboot(self, delay_ms: int = 100) -> None:
        """Restart the EK firmware. The serial port reconnects afterwards."""
        with self._reboot_lock:
            self.command(f"delayed_reboot {delay_ms}")
            self._set_pmic_state(PMIC_CONNECTED)

    # -- measurements ------------------------------------------------------

    def start_adc_sampling(self, sample_ms: int = 1000, report_ms: int = 2000) -> None:
        """Make the firmware report battery measurements.

        ``sample_ms`` is how often the battery is measured and ``report_ms``
        how often a measurement is printed, the app's reporting rate.
        """
        self.command(f"npm_adc sample {int(sample_ms)} {int(report_ms)}")

    def stop_adc_sampling(self) -> None:
        self.command("npm_adc sample 0")

    def set_charger_status_reports(self, enabled: bool) -> None:
        self._set_bool("npm_chg_status_check set", enabled)

    def battery_connected(self) -> Optional[bool]:
        if self.latest_adc is None:
            return None
        return self.latest_adc.vbat_v > BATTERY_CONNECTED_THRESHOLD_V

    # -- charger -----------------------------------------------------------

    def charger_enabled(self) -> bool:
        return self._get_bool("npmx charger module charger get")

    def set_charger_enabled(self, enabled: bool) -> None:
        self._set_bool("npmx charger module charger set", enabled)

    def read_charging_state(self) -> ChargingState:
        state = ChargingState.from_bits(self._get_int("npmx charger status all get"))
        self.charging_state = state
        return state

    def vterm(self) -> float:
        return self._get_int("npmx charger termination_voltage normal get") / 1000

    def set_vterm(self, volts: float) -> None:
        """Set the termination voltage. The charger is switched off first."""
        check_vterm(volts)
        self.set_charger_enabled(False)
        self.command(
            f"npmx charger termination_voltage normal set {round(volts * 1000)}"
        )

    def vterm_warm(self) -> float:
        return self._get_int("npmx charger termination_voltage warm get") / 1000

    def set_vterm_warm(self, volts: float) -> None:
        """Set the termination voltage used in the warm temperature region."""
        check_vterm(volts)
        self.command(f"npmx charger termination_voltage warm set {round(volts * 1000)}")

    def ichg_ma(self) -> float:
        return self._get_int("npmx charger charging_current get") / 1000

    def set_ichg(self, milliamps: float) -> None:
        """Set the charge current. The charger is switched off first."""
        check_ichg(milliamps)
        self.set_charger_enabled(False)
        self.command(f"npmx charger charging_current set {round(milliamps * 1000)}")

    def iterm_percent(self) -> int:
        return self._get_int("npmx charger termination_current get")

    def set_iterm(self, percent: int) -> None:
        _one_of("termination current", percent, ITERM_PERCENT)
        self.set_charger_enabled(False)
        self.command(f"npmx charger termination_current set {percent}")

    def trickle_voltage(self) -> float:
        return self._get_int("npmx charger trickle_voltage get") / 1000

    def set_trickle_voltage(self, volts: float) -> None:
        _one_of("trickle voltage", volts, TRICKLE_VOLTS)
        self.set_charger_enabled(False)
        self.command(f"npmx charger trickle_voltage set {round(volts * 1000)}")

    def recharging_enabled(self) -> bool:
        return self._get_bool("npmx charger module recharge get")

    def set_recharging_enabled(self, enabled: bool) -> None:
        self._set_bool("npmx charger module recharge set", enabled)

    def vbat_low_charging_enabled(self) -> bool:
        return self._get_bool("powerup_charger vbatlow get")

    def set_vbat_low_charging_enabled(self, enabled: bool) -> None:
        self._set_bool("powerup_charger vbatlow set", enabled)

    def battery_current_limit_ma(self) -> int:
        return self._get_int("npm_adc fullscale get")

    def set_battery_current_limit(self, milliamps: int) -> None:
        """Set the discharge current limit: 1000 (high) or 200 (low)."""
        self.set_charger_enabled(False)
        self.command(f"npm_adc fullscale set {int(milliamps)}")

    def ntc(self) -> str:
        ohms = self._get_int("npmx adc ntc type get")
        for name, value in NTC_OHMS.items():
            if value == ohms:
                return name
        raise ValueError(f"unknown thermistor value {ohms}")

    def set_ntc(self, ntc: str, set_beta: bool = False) -> None:
        """Select the battery thermistor: ``ignore``, ``10k``, ``47k`` or ``100k``."""
        _one_of("thermistor", ntc, tuple(NTC_OHMS))
        self.set_charger_enabled(False)
        self.command(f"npmx adc ntc type set {NTC_OHMS[ntc]}")
        if set_beta and ntc in NTC_BETA:
            self.set_ntc_beta(NTC_BETA[ntc])

    def ntc_beta(self) -> int:
        return self._get_int("npmx adc ntc beta get")

    def set_ntc_beta(self, beta: int) -> None:
        self.set_charger_enabled(False)
        self.command(f"npmx adc ntc beta set {int(beta)}")

    def jeita(self) -> dict[str, int]:
        """Battery temperature limits in °C: cold, cool, warm and hot."""
        return {
            region: self._get_int(f"npmx charger ntc_temperature {region} get")
            for region in ("cold", "cool", "warm", "hot")
        }

    def set_jeita(self, cold=None, cool=None, warm=None, hot=None) -> None:
        """Set any of the battery temperature limits, in °C."""
        limits = {"cold": cold, "cool": cool, "warm": warm, "hot": hot}
        for region, value in limits.items():
            if value is None:
                continue
            _in_range(f"{region} limit", value, JEITA_RANGE_C, "°C")
            self.command(f"npmx charger ntc_temperature {region} set {int(value)}")

    def die_temperature_limits(self) -> dict[str, int]:
        return {
            "stop": self._get_int("npmx charger die_temp stop get"),
            "resume": self._get_int("npmx charger die_temp resume get"),
        }

    def set_die_temperature_limits(self, stop=None, resume=None) -> None:
        """Set the die temperatures at which charging stops and resumes."""
        for name, value in (("stop", stop), ("resume", resume)):
            if value is None:
                continue
            _in_range(f"die temperature {name}", value, DIE_TEMP_RANGE_C, "°C")
            self.command(f"npmx charger die_temp {name} set {int(value)}")

    def read_charger(self) -> dict:
        """All charger settings."""
        jeita = self.jeita()
        die = self.die_temperature_limits()
        return {
            "enabled": self.charger_enabled(),
            "v_term": self.vterm(),
            "v_term_warm": self.vterm_warm(),
            "i_chg_ma": self.ichg_ma(),
            "i_term_percent": self.iterm_percent(),
            "trickle_voltage": self.trickle_voltage(),
            "recharging": self.recharging_enabled(),
            "vbat_low_charging": self.vbat_low_charging_enabled(),
            "battery_current_limit_ma": self.battery_current_limit_ma(),
            "ntc": self.ntc(),
            "ntc_beta": self.ntc_beta(),
            "t_cold": jeita["cold"],
            "t_cool": jeita["cool"],
            "t_warm": jeita["warm"],
            "t_hot": jeita["hot"],
            "t_chg_stop": die["stop"],
            "t_chg_resume": die["resume"],
        }

    # -- bucks -------------------------------------------------------------

    def buck_enabled(self, index: int) -> bool:
        return self._get_bool(f"npmx buck status get {self._index(index, BUCK_COUNT)}")

    def set_buck_enabled(self, index: int, enabled: bool, force: bool = False) -> None:
        """Switch a buck on or off.

        Switching off buck 2 (index 1) can cut the link to the PMIC, so it
        needs ``force=True``.
        """
        index = self._index(index, BUCK_COUNT)
        if index == 1 and not enabled and not force:
            raise ValueError(
                "buck 2 powers the link to the PMIC; pass force=True to switch it off"
            )
        self._set_bool(f"npmx buck status set {index}", enabled)

    def buck_voltage(self, index: int) -> float:
        index = self._index(index, BUCK_COUNT)
        return self._get_int(f"npmx buck voltage normal get {index}") / 1000

    def set_buck_voltage(self, index: int, volts: float, force: bool = False) -> None:
        """Set the output voltage and put the buck under software control.

        Setting buck 2 (index 1) to 1.6 V or less can cut the link to the
        PMIC, so it needs ``force=True``.
        """
        index = self._index(index, BUCK_COUNT)
        _in_range("buck voltage", volts, BUCK_VOLTAGE_RANGE, "V")
        if index == 1 and volts <= BUCK2_MINIMUM_SAFE_VOLTS and not force:
            raise ValueError(
                "buck 2 powers the link to the PMIC; pass force=True to set "
                f"{BUCK2_MINIMUM_SAFE_VOLTS} V or less"
            )
        self.command(f"npmx buck voltage normal set {index} {round(volts * 1000)}")
        self.command(f"npmx buck vout_select set {index} 1")

    def buck_retention_voltage(self, index: int) -> float:
        index = self._index(index, BUCK_COUNT)
        return self._get_int(f"npmx buck voltage retention get {index}") / 1000

    def set_buck_retention_voltage(self, index: int, volts: float) -> None:
        index = self._index(index, BUCK_COUNT)
        _in_range("retention voltage", volts, BUCK_RETENTION_RANGE, "V")
        self.command(f"npmx buck voltage retention set {index} {round(volts * 1000)}")

    def buck_mode(self, index: int) -> str:
        """``software`` or ``vset``: who sets the output voltage."""
        index = self._index(index, BUCK_COUNT)
        return "software" if self._get_int(f"npmx buck vout_select get {index}") else "vset"

    def set_buck_mode(self, index: int, mode: str) -> None:
        index = self._index(index, BUCK_COUNT)
        choice = _one_of("buck mode", mode, ("vset", "software"))
        self.command(f"npmx buck vout_select set {index} {choice}")

    def buck_mode_control(self, index: int) -> str:
        index = self._index(index, BUCK_COUNT)
        return self._get_word(f"powerup_buck mode get {index}")

    def set_buck_mode_control(self, index: int, mode: str) -> None:
        """``Auto``, ``PWM``, ``PFM``, or a GPIO that selects the mode."""
        index = self._index(index, BUCK_COUNT)
        _one_of("buck mode control", mode, BUCK_MODE_CONTROL + GPIO_NAMES)
        self.command(f"powerup_buck mode set {index} {mode}")

    def buck_on_off_control(self, index: int) -> str:
        index = self._index(index, BUCK_COUNT)
        return _gpio_name(self._get_int(f"npmx buck gpio on_off index get {index}"), "Off")

    def set_buck_on_off_control(self, index: int, control: str) -> None:
        """``Off`` for software control, or the GPIO that switches the buck."""
        index = self._index(index, BUCK_COUNT)
        gpio = _gpio_index("on/off control", control, "Off")
        self.command(f"npmx buck gpio on_off index set {index} {gpio}")

    def buck_retention_control(self, index: int) -> str:
        index = self._index(index, BUCK_COUNT)
        return _gpio_name(
            self._get_int(f"npmx buck gpio retention index get {index}"), "Off"
        )

    def set_buck_retention_control(self, index: int, control: str) -> None:
        index = self._index(index, BUCK_COUNT)
        gpio = _gpio_index("retention control", control, "Off")
        self.command(f"npmx buck gpio retention index set {index} {gpio}")

    def buck_active_discharge(self, index: int) -> bool:
        index = self._index(index, BUCK_COUNT)
        return self._get_bool(f"npmx buck active_discharge get {index}")

    def set_buck_active_discharge(self, index: int, enabled: bool) -> None:
        index = self._index(index, BUCK_COUNT)
        self._set_bool(f"npmx buck active_discharge set {index}", enabled)

    def read_buck(self, index: int) -> dict:
        return {
            "enabled": self.buck_enabled(index),
            "voltage": self.buck_voltage(index),
            "retention_voltage": self.buck_retention_voltage(index),
            "mode": self.buck_mode(index),
            "mode_control": self.buck_mode_control(index),
            "on_off_control": self.buck_on_off_control(index),
            "retention_control": self.buck_retention_control(index),
            "active_discharge": self.buck_active_discharge(index),
        }

    # -- load switches and LDOs ----------------------------------------------

    def ldo_enabled(self, index: int) -> bool:
        return self._get_bool(f"npmx ldsw status get {self._index(index, LDO_COUNT)}")

    def set_ldo_enabled(self, index: int, enabled: bool) -> None:
        index = self._index(index, LDO_COUNT)
        self._set_bool(f"npmx ldsw status set {index}", enabled)

    def ldo_mode(self, index: int) -> str:
        """``ldo`` or ``load_switch``."""
        index = self._index(index, LDO_COUNT)
        return "ldo" if self._get_int(f"npmx ldsw mode get {index}") else "load_switch"

    def set_ldo_mode(self, index: int, mode: str) -> None:
        """Choose ``ldo`` or ``load_switch``.

        LDO mode needs the EK jumpers set for it: see the EK user guide.
        """
        index = self._index(index, LDO_COUNT)
        choice = _one_of("mode", mode, ("load_switch", "ldo"))
        self.command(f"npmx ldsw mode set {index} {choice}")

    def ldo_voltage(self, index: int) -> float:
        index = self._index(index, LDO_COUNT)
        return self._get_int(f"npmx ldsw ldo_voltage get {index}") / 1000

    def set_ldo_voltage(self, index: int, volts: float) -> None:
        """Set the LDO output voltage. The output is put in LDO mode first."""
        index = self._index(index, LDO_COUNT)
        _in_range("LDO voltage", volts, LDO_VOLTAGE_RANGE, "V")
        self.set_ldo_mode(index, "ldo")
        self.command(f"npmx ldsw ldo_voltage set {index} {round(volts * 1000)}")

    def ldo_soft_start_current(self, index: int) -> int:
        index = self._index(index, LDO_COUNT)
        return self._get_int(f"npmx ldsw soft_start current get {index}")

    def set_ldo_soft_start_current(self, index: int, milliamps: int) -> None:
        index = self._index(index, LDO_COUNT)
        _one_of("soft start current", milliamps, LDO_SOFT_START_MA)
        self.command(f"npmx ldsw soft_start current set {index} {milliamps}")

    def ldo_active_discharge(self, index: int) -> bool:
        index = self._index(index, LDO_COUNT)
        return self._get_bool(f"npmx ldsw active_discharge get {index}")

    def set_ldo_active_discharge(self, index: int, enabled: bool) -> None:
        index = self._index(index, LDO_COUNT)
        self._set_bool(f"npmx ldsw active_discharge set {index}", enabled)

    def ldo_on_off_control(self, index: int) -> str:
        index = self._index(index, LDO_COUNT)
        return _gpio_name(self._get_int(f"npmx ldsw gpio index get {index}"), "SW")

    def set_ldo_on_off_control(self, index: int, control: str) -> None:
        """``SW`` for software control, or the GPIO that switches the output."""
        index = self._index(index, LDO_COUNT)
        gpio = _gpio_index("on/off control", control, "SW")
        self.command(f"npmx ldsw gpio index set {index} {gpio}")

    def read_ldo(self, index: int) -> dict:
        return {
            "enabled": self.ldo_enabled(index),
            "mode": self.ldo_mode(index),
            "voltage": self.ldo_voltage(index),
            "soft_start_current_ma": self.ldo_soft_start_current(index),
            "active_discharge": self.ldo_active_discharge(index),
            "on_off_control": self.ldo_on_off_control(index),
        }

    # -- GPIOs and LEDs ------------------------------------------------------

    def gpio_mode(self, index: int) -> str:
        index = self._index(index, GPIO_COUNT)
        return GPIO_MODES[self._get_int(f"npmx gpio config mode get {index}")]

    def set_gpio_mode(self, index: int, mode: str) -> None:
        index = self._index(index, GPIO_COUNT)
        choice = _one_of("GPIO mode", mode, GPIO_MODES)
        self.command(f"npmx gpio config mode set {index} {choice}")

    def gpio_pull(self, index: int) -> str:
        index = self._index(index, GPIO_COUNT)
        return GPIO_PULLS[self._get_int(f"npmx gpio config pull get {index}")]

    def set_gpio_pull(self, index: int, pull: str) -> None:
        index = self._index(index, GPIO_COUNT)
        choice = _one_of("GPIO pull", pull, GPIO_PULLS)
        self.command(f"npmx gpio config pull set {index} {choice}")

    def gpio_drive_ma(self, index: int) -> int:
        index = self._index(index, GPIO_COUNT)
        return self._get_int(f"npmx gpio config drive get {index}")

    def set_gpio_drive(self, index: int, milliamps: int) -> None:
        index = self._index(index, GPIO_COUNT)
        _one_of("GPIO drive", milliamps, GPIO_DRIVES_MA)
        self.command(f"npmx gpio config drive set {index} {milliamps}")

    def gpio_open_drain(self, index: int) -> bool:
        index = self._index(index, GPIO_COUNT)
        return self._get_bool(f"npmx gpio config open_drain get {index}")

    def set_gpio_open_drain(self, index: int, enabled: bool) -> None:
        index = self._index(index, GPIO_COUNT)
        self._set_bool(f"npmx gpio config open_drain set {index}", enabled)

    def gpio_debounce(self, index: int) -> bool:
        index = self._index(index, GPIO_COUNT)
        return self._get_bool(f"npmx gpio config debounce get {index}")

    def set_gpio_debounce(self, index: int, enabled: bool) -> None:
        index = self._index(index, GPIO_COUNT)
        self._set_bool(f"npmx gpio config debounce set {index}", enabled)

    def read_gpio(self, index: int) -> dict:
        return {
            "mode": self.gpio_mode(index),
            "pull": self.gpio_pull(index),
            "drive_ma": self.gpio_drive_ma(index),
            "open_drain": self.gpio_open_drain(index),
            "debounce": self.gpio_debounce(index),
        }

    def led_mode(self, index: int) -> str:
        index = self._index(index, LED_COUNT)
        return LED_MODES[self._get_int(f"npmx led mode get {index}")]

    def set_led_mode(self, index: int, mode: str) -> None:
        index = self._index(index, LED_COUNT)
        choice = _one_of("LED mode", mode, LED_MODES)
        self.command(f"npmx led mode set {index} {choice}")

    # -- system features -----------------------------------------------------

    def long_press_reset(self) -> str:
        return self._get_word("powerup_ship longpress get")

    def set_long_press_reset(self, mode: str) -> None:
        """``one_button``, ``two_button`` or ``disabled``."""
        _one_of("long press reset", mode, LONG_PRESS_RESET)
        self.command(f"powerup_ship longpress set {mode}")

    def ship_time_to_active_ms(self) -> int:
        return self._get_int("npmx ship config time get")

    def set_ship_time_to_active(self, milliseconds: int) -> None:
        """How long the button must be held to leave ship or hibernate mode."""
        _one_of("time to active", milliseconds, SHIP_TIME_TO_ACTIVE_MS)
        self.command(f"npmx ship config time set {milliseconds}")

    def enter_ship_mode(self) -> None:
        """Put the PMIC in ship mode. It stops answering until woken."""
        self._power_down("npmx ship mode ship")

    def enter_hibernate_mode(self) -> None:
        """Put the PMIC in hibernate mode. The timer can wake it."""
        self._power_down("npmx ship mode hibernate")

    def _power_down(self, command: str) -> None:
        try:
            self.shell.command(command, timeout=2)
        except (ShellTimeout, ShellDisconnected):
            pass  # the PMIC may be gone before the reply is complete

    def timer(self) -> dict:
        return {
            "mode": TIMER_MODES[self._get_int("npmx timer config mode get")],
            "prescaler": TIMER_PRESCALERS[
                self._get_int("npmx timer config prescaler get")
            ],
            "period": self._get_int("npmx timer config compare get"),
        }

    def set_timer(self, mode=None, prescaler=None, period=None) -> None:
        """Set any of the timer mode, prescaler (``Slow``, ``Fast``) and period."""
        if mode is not None:
            choice = _one_of("timer mode", mode, TIMER_MODES)
            self.command(f"npmx timer config mode set {choice}")
        if prescaler is not None:
            choice = _one_of("timer prescaler", prescaler, TIMER_PRESCALERS)
            self.command(f"npmx timer config prescaler set {choice}")
        if period is not None:
            if period < 0:
                raise ValueError("timer period cannot be negative")
            self.command(f"npmx timer config compare set {int(period)}")

    def power_failure(self) -> dict:
        return {
            "enabled": self._get_bool("npmx pof status get"),
            "threshold": self._get_int("npmx pof threshold get") / 1000,
            "polarity": POF_POLARITIES[self._get_int("npmx pof polarity get")],
        }

    def set_power_failure(self, enabled=None, threshold=None, polarity=None) -> None:
        """Set any of the power-fail comparator settings."""
        if enabled is not None:
            self._set_bool("npmx pof status set", enabled)
        if threshold is not None:
            self.set_pof_threshold(threshold)
        if polarity is not None:
            choice = _one_of("polarity", polarity, POF_POLARITIES)
            self.command(f"npmx pof polarity set {choice}")

    def set_pof_threshold(self, volts: float) -> None:
        _in_range("power-fail threshold", volts, POF_THRESHOLD_RANGE_V, "V")
        self.command(f"npmx pof threshold set {round(volts * 1000)}")

    def read_usb_status(self) -> str:
        index = self._get_int("powerup_vbusin status get")
        status = logparse.USB_STATUS[index]
        self._set_usb_status(status)
        return status

    def usb_connected(self) -> bool:
        return self.read_usb_status() != logparse.USB_STATUS[0]

    def vbus_current_limit_a(self) -> float:
        return self._get_int("npmx vbusin current_limit get") / 1000

    def set_vbus_current_limit(self, amps: float) -> None:
        _in_range("VBUS current limit", amps, VBUS_LIMIT_RANGE_A, "A")
        self.command(f"npmx vbusin current_limit set {round(amps * 1000)}")

    def error_logs(self) -> dict[str, list[str]]:
        """Reset cause, charger errors and sensor errors held by the PMIC."""
        names = {
            "RSTCAUSE:": "reset_cause",
            "CHARGER_ERROR:": "charger_errors",
            "SENSOR_ERROR:": "sensor_errors",
        }
        logs: dict[str, list[str]] = {name: [] for name in names.values()}
        current = None
        for line in self.command("npmx errlog get").splitlines():
            line = line.strip()
            if line in names:
                current = names[line]
            elif line and current:
                logs[current].append(line)
        return logs

    # -- fuel gauge ----------------------------------------------------------

    def fuel_gauge_enabled(self) -> bool:
        return self._get_bool("fuel_gauge get")

    def set_fuel_gauge_enabled(self, enabled: bool) -> None:
        self._set_bool("fuel_gauge set", enabled)

    def reset_fuel_gauge(self) -> None:
        self.command("fuel_gauge reset")

    def active_battery_model(self) -> str:
        return self.command("fuel_gauge model get")

    def set_active_battery_model(self, name: str) -> None:
        self.command(f'fuel_gauge model set "{name}"')

    def battery_models(self) -> str:
        """The battery models held by the EK, as the firmware lists them."""
        return self.command("fuel_gauge model list")

    def write_battery_model(
        self,
        model_json: "str | Path",
        slot: int = 0,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Write a battery model to the EK and make it available.

        ``model_json`` is the path of a model file, as produced by
        profiling or bundled with the nPM PowerUP app.
        """
        text = Path(model_json).read_text(encoding="utf-8")
        data = text.replace("\r", "").replace("\n", "").encode("utf-8")
        chunks = [
            data[start:start + MODEL_CHUNK_BYTES]
            for start in range(0, len(data), MODEL_CHUNK_BYTES)
        ]

        self.command("fuel_gauge model download begin")
        try:
            for number, chunk in enumerate(chunks, start=1):
                payload = chunk.decode("utf-8", errors="ignore").replace('"', '\\"')
                # The firmware does not echo while a model is being sent.
                self.shell.command(
                    f'fuel_gauge model download "{payload}"', expect_echo=False
                )
                if on_progress:
                    on_progress(number, len(chunks))
            self.shell.command(
                f"fuel_gauge model download apply {int(slot)}",
                timeout=30,
                expect_echo=False,
            )
        except Exception:
            self._abort_model_download()
            raise

    def _abort_model_download(self) -> None:
        try:
            self.shell.command("fuel_gauge model download abort", expect_echo=False)
        except ShellError as error:
            log.warning("could not abort the model download: %s", error)

    # -- battery profiling ---------------------------------------------------

    def profiling_available(self) -> bool:
        """Whether the nPM Fuel Gauge Board is attached."""
        return self._get_bool("cc_sink available")

    def profiling_active(self) -> bool:
        return self._get_bool("cc_profile active")

    def set_profile(
        self,
        report_interval_ms: int,
        ntc_interval_ms: int,
        v_cutoff: float,
        steps: Sequence["ProfileStep"],
    ) -> None:
        fields = " ".join(f'"{step.encode()}"' for step in steps)
        self.command(
            f"cc_profile profile set {int(report_interval_ms)} "
            f"{int(ntc_interval_ms)} {js_number(v_cutoff)} {fields}"
        )

    def start_profiling(self) -> None:
        self.command("cc_profile start")

    def stop_profiling(self) -> None:
        try:
            self.command("cc_profile stop")
        except ShellCommandError as error:
            if "No profiling ongoing" not in error.response:
                raise


class ProfileStep:
    """One step of a constant-current profile.

    The load alternates between ``i_load`` for ``t_load_ms`` and ``i_rest``
    for ``t_rest_ms``. It ends after ``cycles`` repetitions, or when the
    battery falls to ``v_cutoff``.
    """

    def __init__(
        self,
        t_load_ms: int,
        t_rest_ms: int,
        i_load_a: float,
        i_rest_a: float = 0,
        cycles: Optional[int] = None,
        v_cutoff: Optional[float] = None,
    ):
        self.t_load_ms = t_load_ms
        self.t_rest_ms = t_rest_ms
        self.i_load_a = i_load_a
        self.i_rest_a = i_rest_a
        self.cycles = cycles
        self.v_cutoff = v_cutoff

    def encode(self) -> str:
        fields = [
            js_number(self.t_load_ms),
            js_number(self.t_rest_ms),
            js_number(self.i_load_a),
            js_number(self.i_rest_a),
            js_number(self.cycles) if self.cycles else "NaN",
        ]
        if self.v_cutoff:
            fields.append(js_number(self.v_cutoff))
        return ",".join(fields)

    def __repr__(self) -> str:
        return f"ProfileStep({self.encode()})"
