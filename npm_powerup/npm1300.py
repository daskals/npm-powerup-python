"""Control of an nPM1300 EK through its firmware shell."""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, Sequence

from . import logparse
from .logparse import (
    AdcSample,
    ChargingState,
    LogEvent,
    ProfilingSample,
    leading_float,
    leading_int,
    value_after_colon,
)
from .ports import find_shell_port
from .shell import PortSource, ShellCommandError, ShellError, ShellSession

log = logging.getLogger(__name__)

# Firmware the command set below was written against.
SUPPORTED_FIRMWARE = "1.5.2+0"

# Below this battery voltage the EK is taken to have no battery attached.
BATTERY_CONNECTED_THRESHOLD_V = 1.0

NTC_OHMS = {"ignore": 0, "10k": 10_000, "47k": 47_000, "100k": 100_000}
NTC_BETA = {"10k": 3380, "47k": 4050, "100k": 4250}
ITERM_PERCENT = (10, 20)

VTERM_RANGES = ((3.5, 3.65), (4.0, 4.45))
VTERM_STEP = 0.05
ICHG_RANGE_MA = (32, 800)
ICHG_STEP_MA = 2
POF_THRESHOLD_RANGE_V = (2.6, 3.5)

BUCK_COUNT = 2
LDO_COUNT = 2

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

    def _get_int(self, command: str) -> int:
        return leading_int(value_after_colon(self.command(command)))

    def _get_bool(self, command: str) -> bool:
        return self._get_int(command) == 1

    # -- measurements ------------------------------------------------------

    def start_adc_sampling(self, sample_ms: int = 1000, report_ms: int = 2000) -> None:
        """Make the firmware report battery measurements.

        ``sample_ms`` is how often the battery is measured and ``report_ms``
        how often a measurement is printed.
        """
        self.command(f"npm_adc sample {int(sample_ms)} {int(report_ms)}")

    def stop_adc_sampling(self) -> None:
        self.command("npm_adc sample 0")

    def set_charger_status_reports(self, enabled: bool) -> None:
        self.command(f"npm_chg_status_check set {int(enabled)}")

    def battery_connected(self) -> Optional[bool]:
        if self.latest_adc is None:
            return None
        return self.latest_adc.vbat_v > BATTERY_CONNECTED_THRESHOLD_V

    # -- charger -----------------------------------------------------------

    def charger_enabled(self) -> bool:
        return self._get_bool("npmx charger module charger get")

    def set_charger_enabled(self, enabled: bool) -> None:
        self.command(f"npmx charger module charger set {int(enabled)}")

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
        if percent not in ITERM_PERCENT:
            raise ValueError(f"termination current must be one of {ITERM_PERCENT} %")
        self.set_charger_enabled(False)
        self.command(f"npmx charger termination_current set {percent}")

    def set_ntc(self, ntc: str, set_beta: bool = False) -> None:
        """Select the battery thermistor: ``ignore``, ``10k``, ``47k`` or ``100k``."""
        if ntc not in NTC_OHMS:
            raise ValueError(f"thermistor must be one of {sorted(NTC_OHMS)}")
        self.set_charger_enabled(False)
        self.command(f"npmx adc ntc type set {NTC_OHMS[ntc]}")
        if set_beta and ntc in NTC_BETA:
            self.command(f"npmx adc ntc beta set {NTC_BETA[ntc]}")

    # -- regulators --------------------------------------------------------

    def buck_enabled(self, index: int) -> bool:
        return self._get_bool(f"npmx buck status get {self._index(index, BUCK_COUNT)}")

    def set_buck_enabled(self, index: int, enabled: bool) -> None:
        """Switch a buck. Buck 2 (index 1) powers the link to the PMIC."""
        index = self._index(index, BUCK_COUNT)
        self.command(f"npmx buck status set {index} {int(enabled)}")

    def buck_voltage(self, index: int) -> float:
        index = self._index(index, BUCK_COUNT)
        return self._get_int(f"npmx buck voltage normal get {index}") / 1000

    def ldo_enabled(self, index: int) -> bool:
        return self._get_bool(f"npmx ldsw status get {self._index(index, LDO_COUNT)}")

    def set_ldo_enabled(self, index: int, enabled: bool) -> None:
        index = self._index(index, LDO_COUNT)
        self.command(f"npmx ldsw status set {index} {int(enabled)}")

    @staticmethod
    def _index(index: int, count: int) -> int:
        if not 0 <= index < count:
            raise ValueError(f"index {index} is out of range 0 to {count - 1}")
        return index

    # -- power ---------------------------------------------------------------

    def read_usb_status(self) -> str:
        index = self._get_int("powerup_vbusin status get")
        status = logparse.USB_STATUS[index]
        self._set_usb_status(status)
        return status

    def usb_connected(self) -> bool:
        return self.read_usb_status() != logparse.USB_STATUS[0]

    def set_pof_threshold(self, volts: float) -> None:
        low, high = POF_THRESHOLD_RANGE_V
        if not low <= volts <= high:
            raise ValueError(f"power-fail threshold must be {low} to {high} V")
        self.command(f"npmx pof threshold set {round(volts * 1000)}")

    # -- fuel gauge ----------------------------------------------------------

    def fuel_gauge_enabled(self) -> bool:
        return self._get_bool("fuel_gauge get")

    def set_fuel_gauge_enabled(self, enabled: bool) -> None:
        self.command(f"fuel_gauge set {int(enabled)}")

    def active_battery_model(self) -> str:
        return self.command("fuel_gauge model get")

    def set_active_battery_model(self, name: str) -> None:
        self.command(f'fuel_gauge model set "{name}"')

    def battery_models(self) -> str:
        return self.command("fuel_gauge model list")

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


__all__ = [
    "Npm1300",
    "ProfileStep",
    "UnsupportedDevice",
    "SUPPORTED_FIRMWARE",
    "leading_float",
]
