"""Parsing of the log lines printed by the nPM EK firmware.

The firmware prints Zephyr log lines such as::

    [00:01:02.345,678] <inf> module_pmic_adc: ibat=0.012,vbat=4.10,tbat=24.8,...

Each parser here takes the message part of one line and returns a typed
record, or ``None`` if the message is not of that kind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_LOG_LINE = re.compile(
    r"\[(?P<h>\d{2,}):(?P<m>\d{2}):(?P<s>\d{2})\.(?P<ms>\d{3}),(?P<us>\d{3})\]"
    r"\s*<(?P<level>[^<>]+)>\s*(?P<module>[^:\s]+):\s?(?P<message>.*)$"
)

_ADC = re.compile(
    r"ibat=(?P<ibat>[^,]+),vbat=(?P<vbat>[^,]+),tbat=(?P<tbat>[^,]+),"
    r"soc=(?P<soc>[^,]+),tte=(?P<tte>[^,]+),ttf=(?P<ttf>[^,]+),"
    r"soh=(?P<soh>[^,]+),cycle_count=(?P<cycle_count>[^,]+)"
)

MODULE_PMIC = "module_pmic"
MODULE_ADC = "module_pmic_adc"
MODULE_IRQ = "module_pmic_irq"
MODULE_CHARGER = "module_pmic_charger"
MODULE_FUEL_GAUGE = "module_fg"
MODULE_PROFILING = "module_cc_profiling"

USB_STATUS = (
    "No USB connection",
    "USB 100/500 mA",
    "1.5A High Power",
    "3A High Power",
)

# Messages from module_pmic that report the USB PMIC port state.
USB_LOG_MESSAGES = {
    "No USB connection": USB_STATUS[0],
    "Default USB 100/500mA": USB_STATUS[1],
    "1.5A High Power": USB_STATUS[2],
    "3A High Power": USB_STATUS[3],
}


@dataclass(frozen=True)
class LogEvent:
    timestamp_ms: int  # device uptime
    level: str
    module: str
    message: str


@dataclass(frozen=True)
class AdcSample:
    timestamp_ms: int
    vbat_v: float
    ibat_ma: float
    tbat_c: float
    soc_pct: float  # NaN while the fuel gauge is off
    tte: float  # time to empty, as reported by the firmware
    ttf: float  # time to full, as reported by the firmware
    soh: float
    cycle_count: float


@dataclass(frozen=True)
class ChargingState:
    raw: int
    battery_detected: bool
    battery_full: bool
    trickle_charge: bool
    constant_current: bool
    constant_voltage: bool
    recharge_needed: bool
    die_temp_high: bool
    supplement_mode: bool

    @classmethod
    def from_bits(cls, value: int) -> "ChargingState":
        return cls(
            raw=value,
            battery_detected=bool(value & 0x01),
            battery_full=bool(value & 0x02),
            trickle_charge=bool(value & 0x04),
            constant_current=bool(value & 0x08),
            constant_voltage=bool(value & 0x10),
            recharge_needed=bool(value & 0x20),
            die_temp_high=bool(value & 0x40),
            supplement_mode=bool(value & 0x80),
        )

    def describe(self) -> str:
        if self.battery_full:
            return "battery full"
        if self.die_temp_high:
            return "not charging (die temperature high)"
        if self.constant_current:
            return "charging (constant current)"
        if self.constant_voltage:
            return "charging (constant voltage)"
        if self.trickle_charge:
            return "charging (trickle)"
        return "not charging"


@dataclass(frozen=True)
class ProfilingSample:
    timestamp_ms: int
    iload_a: float
    vload_v: float
    tbat_c: float
    cycle: int
    seq: int
    rep: int
    tload: float


def parse_log_line(line: str) -> Optional[LogEvent]:
    """Return the log event contained in ``line``, if there is one."""
    match = _LOG_LINE.search(line)
    if not match:
        return None
    timestamp = (
        int(match["h"]) * 3_600_000
        + int(match["m"]) * 60_000
        + int(match["s"]) * 1_000
        + int(match["ms"])
    )
    return LogEvent(
        timestamp_ms=timestamp,
        level=match["level"].strip(),
        module=match["module"],
        message=match["message"].strip(),
    )


def _number(text: str) -> float:
    try:
        return float(text.strip())
    except ValueError:
        return float("nan")


def parse_key_values(message: str) -> dict[str, str]:
    """Split ``a=1,b=2`` into a dictionary."""
    pairs = {}
    for part in message.split(","):
        key, sep, value = part.partition("=")
        if sep:
            pairs[key.strip()] = value.strip()
    return pairs


def parse_adc_sample(event: LogEvent) -> Optional[AdcSample]:
    if event.module != MODULE_ADC:
        return None
    match = _ADC.search(event.message)
    if not match:
        return None
    return AdcSample(
        timestamp_ms=event.timestamp_ms,
        vbat_v=_number(match["vbat"]),
        ibat_ma=_number(match["ibat"]) * 1000,  # firmware reports amperes
        tbat_c=_number(match["tbat"]),
        soc_pct=_number(match["soc"]),
        tte=_number(match["tte"]),
        ttf=_number(match["ttf"]),
        soh=_number(match["soh"]),
        cycle_count=_number(match["cycle_count"]),
    )


def parse_charging_state(event: LogEvent) -> Optional[ChargingState]:
    if event.module != MODULE_CHARGER:
        return None
    _, sep, value = event.message.partition("=")
    if not sep:
        return None
    try:
        return ChargingState.from_bits(int(value.strip(), 10))
    except ValueError:
        return None


def parse_profiling_sample(event: LogEvent) -> Optional[ProfilingSample]:
    if event.module != MODULE_PROFILING:
        return None
    pairs = parse_key_values(event.message)
    if "vload" not in pairs or "iload" not in pairs:
        return None

    def integer(key: str) -> int:
        try:
            return int(pairs.get(key, "0"), 10)
        except ValueError:
            return 0

    return ProfilingSample(
        timestamp_ms=event.timestamp_ms,
        iload_a=_number(pairs["iload"]),
        vload_v=_number(pairs["vload"]),
        tbat_c=_number(pairs.get("tbat", "nan")),
        cycle=integer("cycle"),
        seq=integer("seq"),
        rep=integer("rep"),
        tload=_number(pairs.get("tload", "nan")),
    )


def parse_irq(event: LogEvent) -> Optional[tuple[str, str]]:
    """Return ``(type, bit)`` of an interrupt event."""
    if event.module != MODULE_IRQ:
        return None
    pairs = parse_key_values(event.message)
    return pairs.get("type", ""), pairs.get("bit", "")


def value_after_colon(response: str) -> str:
    """Extract ``XXX`` from replies shaped like ``Value: XXX.``"""
    parts = response.split(":")
    if len(parts) < 2:
        return response.strip()
    value = parts[1].strip()
    return value[:-1] if value.endswith(".") else value


def leading_int(text: str) -> int:
    """Parse the integer a string starts with, as in ``2300 mv`` or ``10%``."""
    match = re.match(r"\s*(-?\d+)", text)
    if not match:
        raise ValueError(f"no integer in {text!r}")
    return int(match.group(1))


def leading_float(text: str) -> float:
    match = re.match(r"\s*(-?\d+(\.\d+)?)", text)
    if not match:
        raise ValueError(f"no number in {text!r}")
    return float(match.group(1))
