"""Recording battery measurements to a CSV file."""

from __future__ import annotations

import csv
import os
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from .logparse import AdcSample, ChargingState
from .npm1300 import Npm1300

COLUMNS = (
    "time",
    "uptime_s",
    "vbat_v",
    "ibat_ma",
    "tbat_c",
    "soc_pct",
    "tte",
    "ttf",
    "soh",
    "cycle_count",
    "charger",
    "usb_pmic",
)


class MeasurementRecorder:
    """Writes every battery measurement of the EK to a CSV file."""

    def __init__(
        self,
        device: Npm1300,
        path: os.PathLike,
        on_sample: Optional[Callable[[AdcSample], None]] = None,
    ):
        self.device = device
        self.path = path
        self.samples = 0
        self._on_sample = on_sample
        self._lock = threading.Lock()
        self._file = None
        self._writer = None
        self._unsubscribe: list[Callable[[], None]] = []
        self._charging: Optional[ChargingState] = None

    def start(self, sample_ms: int = 1000, report_ms: int = 1000) -> None:
        new_file = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self._file = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        if new_file:
            self._writer.writerow(COLUMNS)

        self._unsubscribe = [
            self.device.on_adc_sample(self._record),
            self.device.on_charging_state(self._set_charging),
        ]
        self.device.set_charger_status_reports(True)
        self.device.start_adc_sampling(sample_ms, report_ms)

    def stop(self) -> None:
        for remove in self._unsubscribe:
            remove()
        self._unsubscribe = []
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = self._writer = None

    def __enter__(self) -> "MeasurementRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _set_charging(self, state: ChargingState) -> None:
        self._charging = state

    def _record(self, sample: AdcSample) -> None:
        with self._lock:
            if self._writer is None:
                return
            self._writer.writerow([
                datetime.now().isoformat(timespec="milliseconds"),
                sample.timestamp_ms / 1000,
                sample.vbat_v,
                round(sample.ibat_ma, 3),
                sample.tbat_c,
                sample.soc_pct,
                sample.tte,
                sample.ttf,
                sample.soh,
                sample.cycle_count,
                self._charging.describe() if self._charging else "",
                self.device.usb_status or "",
            ])
            self._file.flush()
            self.samples += 1
        if self._on_sample:
            self._on_sample(sample)


def record(
    device: Npm1300,
    path: os.PathLike,
    duration_s: Optional[float] = None,
    sample_ms: int = 1000,
    report_ms: int = 1000,
    on_sample: Optional[Callable[[AdcSample], None]] = None,
) -> int:
    """Record until ``duration_s`` has passed or Ctrl+C is pressed.

    Returns the number of measurements written.
    """
    recorder = MeasurementRecorder(device, path, on_sample)
    recorder.start(sample_ms, report_ms)
    deadline = None if duration_s is None else time.monotonic() + duration_s
    try:
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        recorder.stop()
    return recorder.samples
