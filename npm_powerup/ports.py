"""Finding nPM evaluation kits among the serial ports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from serial.tools import list_ports

NORDIC_VID = 0x1915
NPM1300_APPLICATION_PID = 0x53AB
NPM1300_RECOVERY_PID = 0x53AC  # MCUboot, firmware needs programming


@dataclass(frozen=True)
class EkPort:
    device: str
    serial_number: Optional[str]
    interface: int
    recovery_mode: bool
    description: str


def _interface_number(info) -> int:
    for text in (info.hwid or "", info.location or ""):
        match = re.search(r"MI_(\d+)", text) or re.search(r":\d+\.(\d+)$", text)
        if match:
            return int(match.group(1))
    match = re.search(r"(\d+)$", info.device or "")
    return int(match.group(1)) if match else 0


def list_npm1300_ports() -> list[EkPort]:
    """Serial ports that belong to an nPM1300 EK, lowest interface first."""
    ports = []
    for info in list_ports.comports():
        if info.vid != NORDIC_VID:
            continue
        if info.pid not in (NPM1300_APPLICATION_PID, NPM1300_RECOVERY_PID):
            continue
        ports.append(
            EkPort(
                device=info.device,
                serial_number=info.serial_number,
                interface=_interface_number(info),
                recovery_mode=info.pid == NPM1300_RECOVERY_PID,
                description=info.description or "",
            )
        )
    return sorted(ports, key=lambda p: (p.serial_number or "", p.interface, p.device))


def find_shell_port(serial_number: Optional[str] = None) -> Optional[str]:
    """The port of the EK shell: the first interface of the first EK."""
    for port in list_npm1300_ports():
        if port.recovery_mode:
            continue
        if serial_number is None or port.serial_number == serial_number:
            return port.device
    return None
