"""Python control of Nordic nPM1300 evaluation kits.

An independent implementation of the serial shell protocol that the
nPM PowerUP desktop app uses. Not affiliated with Nordic Semiconductor.
"""

from .logparse import AdcSample, ChargingState, LogEvent, ProfilingSample
from .npm1300 import SUPPORTED_FIRMWARE, Npm1300, ProfileStep, UnsupportedDevice
from .ports import find_shell_port, list_npm1300_ports
from .profiling import (
    BatteryProfile,
    ProfilingError,
    ProfilingResult,
    ProfilingRun,
)
from .recorder import MeasurementRecorder, record
from .shell import (
    ShellCommandError,
    ShellDisconnected,
    ShellError,
    ShellSession,
    ShellTimeout,
)

__version__ = "0.1.0"

__all__ = [
    "AdcSample",
    "BatteryProfile",
    "ChargingState",
    "LogEvent",
    "MeasurementRecorder",
    "Npm1300",
    "ProfileStep",
    "ProfilingError",
    "ProfilingResult",
    "ProfilingRun",
    "ProfilingSample",
    "SUPPORTED_FIRMWARE",
    "ShellCommandError",
    "ShellDisconnected",
    "ShellError",
    "ShellSession",
    "ShellTimeout",
    "UnsupportedDevice",
    "find_shell_port",
    "list_npm1300_ports",
    "record",
]
