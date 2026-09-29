"""Export and load of the EK configuration.

The JSON written here uses the field names of the nPM PowerUP app's
configuration files (file format version 2), so that files can be moved
between the app and this package. The overlay is a Zephyr devicetree
overlay for the ``nordic,npm1300`` bindings.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from . import npm1300 as device_limits
from .npm1300 import GPIO_MODES, GPIO_NAMES, GPIO_PULLS, Npm1300

FILE_FORMAT_VERSION = 2
DEVICE_TYPE = "npm1300"

# How the app's files name the thermistor and regulator choices.
_NTC_TO_FILE = {"ignore": "Ignore NTC", "10k": "10 kΩ", "47k": "47 kΩ", "100k": "100 kΩ"}
_NTC_FROM_FILE = {name: key for key, name in _NTC_TO_FILE.items()}
_BUCK_MODE_TO_FILE = {"vset": "vSet", "software": "software"}
_BUCK_MODE_FROM_FILE = {name: key for key, name in _BUCK_MODE_TO_FILE.items()}
_LDO_MODE_TO_FILE = {"ldo": "LDO", "load_switch": "Load_switch"}
_LDO_MODE_FROM_FILE = {name: key for key, name in _LDO_MODE_TO_FILE.items()}

# Values a freshly reset PMIC has; the overlay leaves these out.
_DEFAULT_JEITA = {"tCold": 0, "tCool": 10, "tWarm": 45, "tHot": 60}


class ConfigurationError(Exception):
    pass


def read_configuration(device: Npm1300) -> dict:
    """Read every setting of the EK into a configuration."""
    charger = device.read_charger()
    bucks = [device.read_buck(i) for i in range(device_limits.BUCK_COUNT)]
    ldos = [device.read_ldo(i) for i in range(device_limits.LDO_COUNT)]
    gpios = [device.read_gpio(i) for i in range(device_limits.GPIO_COUNT)]
    power_failure = device.power_failure()
    timer = device.timer()

    return {
        "boosts": [],
        "charger": {
            "vTerm": charger["v_term"],
            "vTrickleFast": charger["trickle_voltage"],
            "iChg": charger["i_chg_ma"],
            "enabled": charger["enabled"],
            "iTerm": charger["i_term_percent"],
            "iBatLim": charger["battery_current_limit_ma"],
            "enableRecharging": charger["recharging"],
            "enableVBatLow": charger["vbat_low_charging"],
            "ntcThermistor": _NTC_TO_FILE[charger["ntc"]],
            "ntcBeta": charger["ntc_beta"],
            "tChgStop": charger["t_chg_stop"],
            "tChgResume": charger["t_chg_resume"],
            "vTermR": charger["v_term_warm"],
            "tCold": charger["t_cold"],
            "tCool": charger["t_cool"],
            "tWarm": charger["t_warm"],
            "tHot": charger["t_hot"],
        },
        "bucks": [
            {
                "vOutNormal": buck["voltage"],
                "vOutRetention": buck["retention_voltage"],
                "mode": _BUCK_MODE_TO_FILE[buck["mode"]],
                "modeControl": buck["mode_control"],
                "onOffControl": buck["on_off_control"],
                "retentionControl": buck["retention_control"],
                "enabled": buck["enabled"],
                "activeDischarge": buck["active_discharge"],
            }
            for buck in bucks
        ],
        "ldos": [
            {
                "activeDischarge": ldo["active_discharge"],
                "enabled": ldo["enabled"],
                "mode": _LDO_MODE_TO_FILE[ldo["mode"]],
                "onOffControl": ldo["on_off_control"],
                "softStart": True,
                "softStartCurrent": ldo["soft_start_current_ma"],
                "voltage": ldo["voltage"],
            }
            for ldo in ldos
        ],
        "gpios": [
            {
                "mode": GPIO_MODES.index(gpio["mode"]),
                "pull": GPIO_PULLS.index(gpio["pull"]),
                "drive": gpio["drive_ma"],
                "openDrain": gpio["open_drain"],
                "debounce": gpio["debounce"],
            }
            for gpio in gpios
        ],
        "leds": [
            {"mode": device.led_mode(i)} for i in range(device_limits.LED_COUNT)
        ],
        "pof": {
            "enable": power_failure["enabled"],
            "polarity": power_failure["polarity"],
            "threshold": power_failure["threshold"],
        },
        "lowPower": {"timeToActive": str(device.ship_time_to_active_ms())},
        "reset": {"longPressReset": device.long_press_reset()},
        "timerConfig": {
            "mode": device_limits.TIMER_MODES.index(timer["mode"]),
            "prescaler": timer["prescaler"],
            "period": timer["period"],
        },
        "fuelGaugeSettings": {"enabled": device.fuel_gauge_enabled()},
        "firmwareVersion": device.app_version(),
        "deviceType": DEVICE_TYPE,
        "usbPower": {"currentLimiter": device.vbus_current_limit_a()},
        "fileFormatVersion": FILE_FORMAT_VERSION,
    }


def apply_configuration(device: Npm1300, config: dict, force: bool = False) -> list[str]:
    """Write a configuration to the EK.

    Returns the settings that could not be applied. ``force`` allows
    settings of buck 2 that can cut the link to the PMIC.
    """
    if config.get("deviceType", DEVICE_TYPE) != DEVICE_TYPE:
        raise ConfigurationError(
            f"the configuration is for {config.get('deviceType')}, not {DEVICE_TYPE}"
        )
    if config.get("firmwareVersion") is None:
        raise ConfigurationError("not a configuration file: no firmware version")

    failures: list[str] = []

    def attempt(name: str, action, *arguments, **options) -> None:
        try:
            action(*arguments, **options)
        except Exception as error:  # keep going, as the app does
            failures.append(f"{name}: {error}")

    fuel_gauge = config.get("fuelGaugeSettings") or {}
    charger = config.get("charger")
    if charger:
        attempt("charger vTerm", device.set_vterm, charger["vTerm"])
        attempt("charger iChg", device.set_ichg, charger["iChg"])
        attempt("charger iTerm", device.set_iterm, int(charger["iTerm"]))
        if charger.get("iBatLim") is not None:
            attempt("charger iBatLim", device.set_battery_current_limit, charger["iBatLim"])
        attempt("charger recharging", device.set_recharging_enabled, charger["enableRecharging"])
        attempt("charger vBatLow", device.set_vbat_low_charging_enabled, charger["enableVBatLow"])
        attempt("charger vTrickleFast", device.set_trickle_voltage, charger["vTrickleFast"])
        attempt(
            "charger die temperature", device.set_die_temperature_limits,
            stop=charger.get("tChgStop"), resume=charger.get("tChgResume"),
        )
        attempt(
            "charger JEITA", device.set_jeita,
            cold=charger.get("tCold"), cool=charger.get("tCool"),
            warm=charger.get("tWarm"), hot=charger.get("tHot"),
        )
        if charger.get("ntcThermistor") in _NTC_FROM_FILE:
            attempt("charger NTC", device.set_ntc, _NTC_FROM_FILE[charger["ntcThermistor"]])
        if charger.get("ntcBeta"):
            attempt("charger NTC beta", device.set_ntc_beta, charger["ntcBeta"])
        if charger.get("vTermR") is not None:
            attempt("charger vTermR", device.set_vterm_warm, charger["vTermR"])
        # Last, as the settings above switch the charger off.
        attempt("charger enabled", device.set_charger_enabled, charger["enabled"])

    for index, buck in enumerate(config.get("bucks") or []):
        name = f"buck {index + 1}"
        attempt(f"{name} voltage", device.set_buck_voltage, index, buck["vOutNormal"], force=force)
        attempt(f"{name} enabled", device.set_buck_enabled, index, buck["enabled"], force=force)
        attempt(f"{name} mode control", device.set_buck_mode_control, index, buck["modeControl"])
        attempt(f"{name} on/off control", device.set_buck_on_off_control, index, buck["onOffControl"])
        attempt(f"{name} mode", device.set_buck_mode, index, _BUCK_MODE_FROM_FILE[buck["mode"]])
        if buck.get("activeDischarge") is not None:
            attempt(f"{name} discharge", device.set_buck_active_discharge, index, buck["activeDischarge"])
        if buck.get("retentionControl") is not None:
            attempt(f"{name} retention control", device.set_buck_retention_control, index, buck["retentionControl"])
        if buck.get("vOutRetention") is not None:
            attempt(f"{name} retention voltage", device.set_buck_retention_voltage, index, buck["vOutRetention"])

    for index, ldo in enumerate(config.get("ldos") or []):
        name = f"LDO {index + 1}"
        attempt(f"{name} enabled", device.set_ldo_enabled, index, ldo["enabled"])
        attempt(f"{name} discharge", device.set_ldo_active_discharge, index, ldo["activeDischarge"])
        attempt(f"{name} on/off control", device.set_ldo_on_off_control, index, ldo["onOffControl"])
        mode = _LDO_MODE_FROM_FILE.get(ldo.get("mode"))
        if mode:
            attempt(f"{name} mode", device.set_ldo_mode, index, mode)
        if ldo.get("softStartCurrent") is not None:
            attempt(f"{name} soft start", device.set_ldo_soft_start_current, index, ldo["softStartCurrent"])
        if mode == "ldo" and ldo.get("voltage") is not None:
            attempt(f"{name} voltage", device.set_ldo_voltage, index, ldo["voltage"])

    for index, gpio in enumerate(config.get("gpios") or []):
        name = f"GPIO{index}"
        attempt(f"{name} mode", device.set_gpio_mode, index, GPIO_MODES[gpio["mode"]])
        attempt(f"{name} pull", device.set_gpio_pull, index, GPIO_PULLS[gpio["pull"]])
        attempt(f"{name} drive", device.set_gpio_drive, index, gpio["drive"])
        attempt(f"{name} open drain", device.set_gpio_open_drain, index, gpio["openDrain"])
        attempt(f"{name} debounce", device.set_gpio_debounce, index, gpio["debounce"])

    for index, led in enumerate(config.get("leds") or []):
        if led.get("mode") is not None:
            attempt(f"LED{index} mode", device.set_led_mode, index, led["mode"])

    pof = config.get("pof")
    if pof:
        attempt(
            "power failure", device.set_power_failure,
            enabled=pof["enable"], threshold=pof["threshold"], polarity=pof["polarity"],
        )

    timer = config.get("timerConfig")
    if timer:
        attempt(
            "timer", device.set_timer,
            mode=device_limits.TIMER_MODES[timer["mode"]],
            prescaler=timer["prescaler"], period=timer["period"],
        )

    low_power = config.get("lowPower")
    if low_power:
        attempt("time to active", device.set_ship_time_to_active, int(low_power["timeToActive"]))

    reset = config.get("reset")
    if reset:
        attempt("long press reset", device.set_long_press_reset, reset["longPressReset"])

    if "enabled" in fuel_gauge:
        attempt("fuel gauge", device.set_fuel_gauge_enabled, fuel_gauge["enabled"])

    usb = config.get("usbPower")
    if usb and usb.get("currentLimiter") is not None:
        attempt("VBUS current limit", device.set_vbus_current_limit, usb["currentLimiter"])

    return failures


def upgrade(config: dict) -> dict:
    """Bring a file written by an old version of the app up to date."""
    if "fuelGaugeSettings" in config:
        return config
    upgraded = dict(config)
    upgraded["fuelGaugeSettings"] = {
        "enabled": config.get("fuelGauge", False),
        "chargingSamplingRate": config.get("fuelGaugeChargingSamplingRate"),
    }
    charger = upgraded.get("charger")
    if charger and "iTerm" in charger:
        charger["iTerm"] = float(str(charger["iTerm"]).rstrip("%"))
    upgraded["fileFormatVersion"] = FILE_FORMAT_VERSION
    return upgraded


def load_file(path: os.PathLike) -> dict:
    return upgrade(json.loads(Path(path).read_text(encoding="utf-8")))


def save_file(path: os.PathLike, config: dict) -> None:
    """Save as JSON, or as a devicetree overlay if the name ends in ``.overlay``."""
    path = Path(path)
    if path.suffix == ".overlay":
        path.write_text(overlay(config), encoding="utf-8")
    else:
        path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


# -- devicetree overlay ------------------------------------------------------


def _micro(value: float) -> int:
    return round(value * 1_000_000)


def _milli(value: float) -> int:
    return round(value * 1_000)


def _gpio_cell(name: str) -> Optional[str]:
    if name in GPIO_NAMES:
        return f"<{GPIO_NAMES.index(name)} GPIO_ACTIVE_HIGH>"
    return None


class _Node:
    """A devicetree node, printed with tab indentation."""

    def __init__(self, heading: str):
        self.heading = heading
        self.entries: list = []

    def add(self, line: Optional[str]) -> "_Node":
        if line:
            self.entries.append(line)
        return self

    def flag(self, name: str, present: bool) -> "_Node":
        return self.add(f"{name};" if present else None)

    def cell(self, name: str, value) -> "_Node":
        return self.add(None if value is None else f"{name} = <{value}>;")

    def text(self, name: str, value: Optional[str]) -> "_Node":
        return self.add(None if value is None else f'{name} = "{value}";')

    def child(self, node: "_Node") -> "_Node":
        self.entries.append(node)
        return self

    def render(self, depth: int = 0) -> str:
        indent = "\t" * depth
        lines = [f"{indent}{self.heading} {{"]
        for entry in self.entries:
            if isinstance(entry, _Node):
                lines.append("")
                lines.append(entry.render(depth + 1))
            else:
                lines.append(f"{indent}\t{entry}")
        lines.append(f"{indent}}};")
        return "\n".join(lines)


def _buck_node(index: int, buck: dict) -> _Node:
    low, high = device_limits.BUCK_VOLTAGE_RANGE
    mode_control = buck["modeControl"]
    node = _Node(f"npm1300_buck{index + 1}: BUCK{index + 1}")
    node.flag("regulator-boot-on", buck["enabled"])
    node.cell("regulator-min-microvolt", _micro(low))
    node.cell("regulator-max-microvolt", _micro(high))
    if buck["mode"] != "vSet":
        node.cell("regulator-init-microvolt", _micro(buck["vOutNormal"]))
    if mode_control not in GPIO_NAMES:
        node.cell("regulator-initial-mode", f"NPM13XX_BUCK_MODE_{mode_control.upper()}")
    if buck.get("vOutRetention") is not None:
        node.cell("retention-microvolt", _micro(buck["vOutRetention"]))
    node.add(_property("enable-gpio-config", _gpio_cell(buck["onOffControl"])))
    node.add(_property("pwm-gpio-config", _gpio_cell(mode_control)))
    node.add(_property("retention-gpio-config", _gpio_cell(buck.get("retentionControl", "Off"))))
    node.flag("active-discharge", bool(buck.get("activeDischarge")))
    return node


def _property(name: str, cell: Optional[str]) -> Optional[str]:
    return f"{name} = {cell};" if cell else None


def _ldo_node(index: int, ldo: dict) -> _Node:
    low, high = device_limits.LDO_VOLTAGE_RANGE
    is_ldo = ldo.get("mode") == "LDO"
    node = _Node(f"npm1300_ldo{index + 1}: LDO{index + 1}")
    node.flag("regulator-boot-on", ldo["enabled"])
    node.cell("regulator-min-microvolt", _micro(low))
    node.cell("regulator-max-microvolt", _micro(high))
    if is_ldo and ldo.get("voltage") is not None:
        node.cell("regulator-init-microvolt", _micro(ldo["voltage"]))
    node.cell(
        "regulator-initial-mode",
        "NPM13XX_LDSW_MODE_LDO" if is_ldo else "NPM13XX_LDSW_MODE_LDSW",
    )
    node.add(_property("enable-gpio-config", _gpio_cell(ldo["onOffControl"])))
    if ldo.get("softStartCurrent") is not None:
        node.cell("soft-start-microamp", _micro(ldo["softStartCurrent"] / 1000))
    node.flag("active-discharge", bool(ldo.get("activeDischarge")))
    return node


def _charger_node(charger: dict, usb: dict) -> _Node:
    thermistor = _NTC_FROM_FILE.get(charger.get("ntcThermistor"), "ignore")
    node = _Node("npm1300_charger: charger")
    node.text("compatible", "nordic,npm1300-charger")
    node.cell("vbus-limit-microamp", _micro(usb["currentLimiter"]))
    node.cell("thermistor-ohms", device_limits.NTC_OHMS[thermistor])
    node.cell("thermistor-beta", 0 if thermistor == "ignore" else charger.get("ntcBeta", 0))
    if thermistor != "ignore":
        regions = (("cold", "tCold"), ("cool", "tCool"), ("warm", "tWarm"), ("hot", "tHot"))
        for region, key in regions:
            if charger.get(key) is not None and charger[key] != _DEFAULT_JEITA[key]:
                node.cell(f"thermistor-{region}-millidegrees", _milli(charger[key]))
    node.flag("charging-enable", charger["enabled"])
    node.cell("trickle-microvolt", _micro(charger["vTrickleFast"]))
    node.flag("vbatlow-charge-enable", bool(charger.get("enableVBatLow")))
    node.flag("disable-recharge", not charger.get("enableRecharging"))
    node.cell("dietemp-resume-millidegrees", _milli(charger["tChgResume"]))
    if charger.get("tChgStop") is not None:
        node.cell("dietemp-stop-millidegrees", _milli(charger["tChgStop"]))
    node.cell("term-microvolt", _micro(charger["vTerm"]))
    if charger.get("vTermR") is not None:
        node.cell("term-warm-microvolt", _micro(charger["vTermR"]))
    node.cell("current-microamp", _micro(charger["iChg"] / 1000))
    if charger.get("iBatLim") is not None:
        node.cell("dischg-limit-microamp", _micro(charger["iBatLim"] / 1000))
    node.cell("term-current-percent", int(charger["iTerm"]))
    return node


def _gpio_with_mode(config: dict, mode: str) -> Optional[int]:
    wanted = GPIO_MODES.index(mode)
    for index, gpio in enumerate(config.get("gpios") or []):
        if gpio.get("mode") == wanted:
            return index
    return None


def overlay(config: dict, i2c_bus: str = "arduino_i2c") -> str:
    """The configuration as a Zephyr devicetree overlay.

    ``i2c_bus`` is the node label of the bus the PMIC is wired to. The
    default suits a development kit with Arduino headers.
    """
    if not config.get("charger") or not config.get("usbPower"):
        raise ConfigurationError("the configuration has no charger or USB settings")

    led_modes = {"Charger error": "error", "Charging": "charging", "Host": "host", "Not used": "host"}

    pmic = _Node("npm1300_pmic: pmic@6b")
    pmic.text("compatible", "nordic,npm1300")
    pmic.cell("reg", "0x6b")

    interrupt_pin = _gpio_with_mode(config, "Output interrupt")
    if interrupt_pin is not None:
        pmic.add("/* Set host-int-gpios for the interrupt pin to work. */")
        pmic.cell("pmic-int-pin", interrupt_pin)
    low_power = config.get("lowPower") or {}
    pmic.cell("ship-to-active-time-ms", low_power.get("timeToActive", 96))
    reset = (config.get("reset") or {}).get("longPressReset")
    if reset:
        pmic.text("long-press-reset", reset.replace("_", "-"))

    gpio = _Node("npm1300_gpio: gpio-controller")
    gpio.text("compatible", "nordic,npm1300-gpio")
    gpio.add("gpio-controller;")
    gpio.cell("#gpio-cells", 2)
    gpio.cell("ngpios", device_limits.GPIO_COUNT)
    pmic.child(gpio)

    regulators = _Node("npm1300_regulators: regulators")
    regulators.text("compatible", "nordic,npm1300-regulator")
    for index, buck in enumerate(config.get("bucks") or []):
        regulators.child(_buck_node(index, buck))
    for index, ldo in enumerate(config.get("ldos") or []):
        regulators.child(_ldo_node(index, ldo))
    pmic.child(regulators)

    pmic.child(_charger_node(config["charger"], config["usbPower"]))

    leds = [led.get("mode") for led in config.get("leds") or [] if led.get("mode")]
    if leds:
        node = _Node("npm1300_leds: leds")
        node.text("compatible", "nordic,npm1300-led")
        for index, mode in enumerate(leds):
            node.text(f"nordic,led{index}-mode", led_modes[mode])
        pmic.child(node)

    watchdog = _Node("npm1300_wdt: watchdog")
    watchdog.text("compatible", "nordic,npm1300-wdt")
    reset_pin = _gpio_with_mode(config, "Output reset")
    if reset_pin is not None:
        watchdog.add(f"reset-gpios = <&npm1300_gpio {reset_pin} GPIO_ACTIVE_LOW>;")
    pmic.child(watchdog)

    bus = _Node(f"&{i2c_bus}")
    bus.child(pmic)

    header = (
        "/*\n"
        " * nPM1300 configuration, generated by npm-powerup-python.\n"
        " */\n\n"
        "#include <zephyr/dt-bindings/regulator/npm13xx.h>\n"
        "#include <zephyr/dt-bindings/gpio/nordic-npm13xx-gpio.h>\n\n"
    )
    return header + bus.render() + "\n"
