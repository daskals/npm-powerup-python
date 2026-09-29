"""Recording of everything the EK reports, as the app's Record Events does.

All events go to ``all_events.csv``. Events that carry measurements also
go to one CSV file per source, with one column per value.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

from . import logparse
from .logparse import LogEvent
from .npm1300 import Npm1300

ALL_EVENTS = "all_events.csv"
SHELL_MODULE = "shell_commands"

_VALUE_MODULES = (
    logparse.MODULE_ADC,
    logparse.MODULE_IRQ,
    logparse.MODULE_PROFILING,
)


def _one_line(text: str) -> str:
    return " ".join(text.replace("\r", "\n").split("\n")).replace('"', "'")


class EventRecorder:
    def __init__(self, device: Npm1300, folder: os.PathLike):
        self.device = device
        self.folder = Path(folder)
        self.events = 0
        self._lock = threading.Lock()
        self._unsubscribe: list[Callable[[], None]] = []
        self._last_timestamp = 0

    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._unsubscribe = [
            self.device.on_log(self._record),
            self.device.shell.on_command(self._record_command),
        ]

    def stop(self) -> None:
        for remove in self._unsubscribe:
            remove()
        self._unsubscribe = []

    def __enter__(self) -> "EventRecorder":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _append(self, name: str, header: str, row: str) -> None:
        path = self.folder / name
        new_file = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as file:
            if new_file:
                file.write(header + "\r\n")
            file.write(row + "\r\n")

    def _record(self, event: LogEvent) -> None:
        with self._lock:
            self._last_timestamp = event.timestamp_ms
            if event.module in _VALUE_MODULES:
                pairs = logparse.parse_key_values(event.message)
                if pairs:
                    self._append(
                        f"{event.module}.csv",
                        "timestamp," + ",".join(pairs),
                        f"{event.timestamp_ms}," + ",".join(pairs.values()),
                    )
            self._write_event(event.timestamp_ms, event.level, event.module, event.message)

    def _record_command(self, command: str, response: str, failed: bool) -> None:
        with self._lock:
            self._write_event(
                self._last_timestamp,
                "err" if failed else "inf",
                SHELL_MODULE,
                f"command: '{command}' response: '{response}'",
            )

    def _write_event(self, timestamp: int, level: str, module: str, message: str) -> None:
        self._append(
            ALL_EVENTS,
            "timestamp,logLevel,module,message",
            f'{timestamp},{level},{module},"{_one_line(message)}"',
        )
        self.events += 1
