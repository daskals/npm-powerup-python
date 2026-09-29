"""Battery profiling on an nPM1300 EK with the nPM Fuel Gauge Board.

The run follows the stages of the nPM PowerUP profiling wizard: charge the
battery to full, rest it, then discharge it in steps while recording current,
voltage and temperature. The recording is what ``nrfutil npm generate``
turns into a battery model.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import logparse
from .logparse import LogEvent, ProfilingSample
from .npm1300 import (
    BATTERY_CONNECTED_THRESHOLD_V,
    ITERM_PERCENT,
    NTC_OHMS,
    Npm1300,
    ProfileStep,
    check_ichg,
    check_vterm,
    js_number,
)
from .shell import ShellError

log = logging.getLogger(__name__)

REPORT_INTERVAL_MS = 1000
NTC_REPORT_INTERVAL_MS = REPORT_INTERVAL_MS * 8
POF_THRESHOLD_V = 2.6
CAPACITY_RANGE_MAH = (32, 3000)
V_CUTOFF_RANGE = (2.7, 3.6)
CSV_HEADER = "Seconds,Current(A),Voltage(V),Temperature(C)\r\n"

# Version of the nPM PowerUP app whose project file layout is written, so
# that the app can open the project in its Profiles tab.
PROJECT_APP_VERSION = "2.2.7"

STAGE_CHARGING = "charging"
STAGE_WAITING_FOR_USB_REMOVAL = "waiting-for-usb-removal"
STAGE_RESTING = "resting"
STAGE_PROFILING = "profiling"
STAGE_COMPLETE = "complete"
STAGE_FAILED = "failed"


class ProfilingError(Exception):
    pass


@dataclass
class BatteryProfile:
    """What is known about the battery from its datasheet."""

    name: str
    capacity_mah: float
    v_term: float = 4.2
    v_cutoff: float = 3.0
    i_chg_ma: Optional[float] = None  # defaults to half the capacity
    i_term_percent: int = 10
    ntc: str = "10k"
    temperatures: Sequence[float] = (25,)

    def __post_init__(self):
        if self.i_chg_ma is None:
            half = min(max(self.capacity_mah / 2, 32), 800)
            self.i_chg_ma = int(half // 2 * 2)
        self.validate()

    def validate(self) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9]{1,20}", self.name) or self.name == "default":
            raise ValueError(
                "battery name must be 1 to 20 letters or digits, and not 'default'"
            )
        low, high = CAPACITY_RANGE_MAH
        if not low <= self.capacity_mah <= high:
            raise ValueError(f"capacity must be {low} to {high} mAh")
        low, high = V_CUTOFF_RANGE
        if not low <= self.v_cutoff <= high:
            raise ValueError(f"discharge cut-off must be {low} to {high} V")
        check_vterm(self.v_term)
        check_ichg(self.i_chg_ma)
        if self.i_term_percent not in ITERM_PERCENT:
            raise ValueError(f"termination current must be one of {ITERM_PERCENT} %")
        if self.ntc not in NTC_OHMS:
            raise ValueError(f"thermistor must be one of {sorted(NTC_OHMS)}")
        if not self.temperatures:
            raise ValueError("at least one temperature is needed")


def resting_steps() -> list[ProfileStep]:
    """No load for 15 minutes, so the battery settles after charging."""
    return [ProfileStep(500, 500, 0, 0, cycles=900)]


def discharge_steps(profile: BatteryProfile) -> list[ProfileStep]:
    """The stepped discharge used for nPM1300, as in nPM PowerUP 2.2.7.

    Each step pulses the load and then rests for 45 minutes. The load is
    reduced as the battery empties.
    """
    capacity = profile.capacity_mah
    v_range = profile.v_term - profile.v_cutoff
    rest = 2_700_000
    return [
        ProfileStep(500, 500, 0, 0, cycles=300),
        ProfileStep(
            420_000, rest, capacity / 6 / 1000,
            v_cutoff=max(profile.v_cutoff + 0.65 * v_range, 3.9),
        ),
        ProfileStep(
            300_000, rest, capacity / 6 / 1000,
            v_cutoff=max(profile.v_cutoff + 0.4 * v_range, 3.55),
        ),
        ProfileStep(
            600_000, rest, capacity / 12 / 1000,
            v_cutoff=profile.v_cutoff + 0.2,
        ),
        ProfileStep(300_000, rest, capacity / 12 / 1000),
    ]


def profile_name(profile: BatteryProfile, temperature: float) -> str:
    sign = "n" if temperature < 0 else "p"
    return f"{profile.name}_{js_number(profile.capacity_mah)}mAh_T{sign}{js_number(temperature)}"


@dataclass
class ProjectPaths:
    root: Path
    settings: Path

    @classmethod
    def of(cls, output_dir: os.PathLike, profile: BatteryProfile) -> "ProjectPaths":
        root = Path(output_dir) / profile.name
        return cls(root=root, settings=root / "profileSettings.json")

    def run_dir(self, index: int) -> Path:
        return self.root / f"profile_{index + 1}"

    def csv(self, profile: BatteryProfile, index: int) -> Path:
        name = profile_name(profile, profile.temperatures[index])
        return self.run_dir(index) / f"{name}.csv"


def load_project(paths: ProjectPaths) -> Optional[dict]:
    if not paths.settings.exists():
        return None
    return json.loads(paths.settings.read_text(encoding="utf-8"))


def save_project(paths: ProjectPaths, project: dict) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.settings.write_text(json.dumps(project, indent=2), encoding="utf-8")


def new_project(profile: BatteryProfile) -> dict:
    return {
        "name": profile.name,
        "deviceType": "npm1300",
        "capacity": profile.capacity_mah,
        "vLowerCutOff": profile.v_cutoff,
        "vUpperCutOff": profile.v_term,
        "profiles": [
            {"temperature": temperature, "csvReady": False}
            for temperature in profile.temperatures
        ],
        "appVersion": PROJECT_APP_VERSION,
        "iTerm": str(profile.i_term_percent),
        "iChg": js_number(profile.i_chg_ma),
    }


@dataclass
class ProfilingResult:
    outcome: str  # "complete", "vcutoff", "power-fail" or "failed"
    message: str
    csv_path: Optional[Path]
    capacity_consumed_mah: float
    samples: int
    usable: bool  # whether the recording can be turned into a model


class ProfilingRun:
    """One profiling run, at one temperature.

    ``notify`` receives progress messages, including the requests to
    connect or disconnect the USB PMIC cable. ``stage`` holds the current
    stage of the run.
    """

    def __init__(
        self,
        device: Npm1300,
        profile: BatteryProfile,
        output_dir: os.PathLike,
        temperature_index: int = 0,
        notify: Callable[[str], None] = print,
        assume_charged: bool = False,
        poll_interval: float = 5.0,
    ):
        if not 0 <= temperature_index < len(profile.temperatures):
            raise ValueError("temperature index out of range")
        self.device = device
        self.profile = profile
        self.index = temperature_index
        self.temperature = profile.temperatures[temperature_index]
        self.paths = ProjectPaths.of(output_dir, profile)
        self.csv_path = self.paths.csv(profile, temperature_index)
        self._notify = notify
        self._assume_charged = assume_charged
        self._poll = poll_interval
        self._events: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.stage = ""

    # -- progress ------------------------------------------------------------

    def _say(self, message: str, action_required: str = "") -> None:
        prefix = "ACTION REQUIRED: " if action_required else ""
        self._notify(f"[{datetime.now():%H:%M:%S}] {prefix}{action_required or message}")

    def _set_stage(self, stage: str) -> None:
        self.stage = stage

    # -- project file --------------------------------------------------------

    def _update_project(self, **entry) -> None:
        project = load_project(self.paths) or new_project(self.profile)
        while len(project["profiles"]) <= self.index:
            project["profiles"].append(
                {"temperature": self.temperature, "csvReady": False}
            )
        project["profiles"][self.index].update(entry)
        save_project(self.paths, project)

    # -- stages --------------------------------------------------------------

    def run(self) -> ProfilingResult:
        device = self.device
        if not device.profiling_available():
            raise ProfilingError(
                "the nPM Fuel Gauge Board was not found; attach it to the "
                "EXT BOARD connectors P20 and P21"
            )
        if device.profiling_active():
            raise ProfilingError("a profiling run is already in progress on the EK")

        self._update_project(csvReady=False)
        unsubscribe = [
            device.on_profiling_sample(lambda s: self._events.put(("sample", s))),
            device.on_log(self._watch_log),
            device.on_usb_status(lambda s: self._events.put(("usb", s))),
        ]
        try:
            device.start_adc_sampling(1000, 2000)
            device.set_charger_status_reports(True)
            if not self._assume_charged:
                self._charge()
            self._remove_usb_power()
            self._start_discharge()
            result = self._record()
        except KeyboardInterrupt:
            self._say("Interrupted, stopping the profiling run.")
            self._stop_quietly()
            self._set_stage(STAGE_FAILED)
            raise
        except Exception as error:
            self._stop_quietly()
            self._set_stage(STAGE_FAILED)
            self._say(f"Profiling failed: {error}")
            raise
        finally:
            for remove in unsubscribe:
                remove()
            device.auto_reboot = True

        self._update_project(
            csvReady=result.usable,
            csvPath=os.path.relpath(self.csv_path, self.paths.settings),
        )
        self._set_stage(STAGE_COMPLETE if result.usable else STAGE_FAILED)
        self._say(result.message)
        return result

    def _watch_log(self, event: LogEvent) -> None:
        if event.module == logparse.MODULE_PROFILING:
            if "Success: Profiling sequence completed" in event.message:
                self._events.put(("end", "complete"))
            elif "vcutoff reached" in event.message:
                self._events.put(("end", "vcutoff"))
            elif "Profiling stopped due to a thermal event" in event.message:
                self._events.put(("end", "thermal"))
        elif event.module == logparse.MODULE_PMIC:
            if event.message == "Power Failure Warning":
                self._events.put(("end", "power-fail"))

    def _wait_until(self, condition: Callable[[], bool], describe=None) -> None:
        while True:
            try:
                if condition():
                    return
            except ShellError as error:
                log.warning("waiting: %s", error)
                self.device.shell.wait_connected(60)
            if describe:
                describe()
            time.sleep(self._poll)

    def _charge(self) -> None:
        device, profile = self.device, self.profile
        self._set_stage(STAGE_CHARGING)

        if not device.usb_connected():
            self._say(
                "Waiting for power on USB PMIC.",
                "Connect the USB PMIC cable (J3) to charge the battery.",
            )
            self._wait_until(device.usb_connected)

        self._say(
            f"Configuring the charger: {profile.v_term} V, "
            f"{js_number(profile.i_chg_ma)} mA, termination {profile.i_term_percent} %."
        )
        device.set_buck_enabled(0, False)
        for ldo in range(2):
            device.set_ldo_enabled(ldo, False)
        device.set_fuel_gauge_enabled(False)
        device.set_ntc(profile.ntc)
        device.set_charger_enabled(False)
        device.set_vterm(profile.v_term)
        device.set_ichg(profile.i_chg_ma)
        device.set_iterm(profile.i_term_percent)
        device.set_charger_enabled(True)

        self._say("Charging the battery to full.")
        last_report = [0.0]

        def report() -> None:
            if time.monotonic() - last_report[0] < 300:
                return
            last_report[0] = time.monotonic()
            adc, state = device.latest_adc, device.charging_state
            voltage = f"{adc.vbat_v:.2f} V" if adc else "unknown voltage"
            self._say(f"Charging: {state.describe() if state else 'unknown'}, {voltage}.")

        self._wait_until(lambda: device.read_charging_state().battery_full, report)
        self._say("The battery is full.")

    def _remove_usb_power(self) -> None:
        device = self.device
        self._set_stage(STAGE_WAITING_FOR_USB_REMOVAL)
        if device.usb_connected():
            self._say(
                "Waiting for USB PMIC power to be removed.",
                "Disconnect the USB PMIC cable (J3). Leave the nPM Controller "
                "cable (J4) connected.",
            )
            self._wait_until(lambda: not device.usb_connected())

        adc = device.latest_adc
        if adc is not None and adc.vbat_v <= BATTERY_CONNECTED_THRESHOLD_V:
            raise ProfilingError("no battery detected on the EK")

    def _start_discharge(self) -> None:
        device, profile = self.device, self.profile
        self._set_stage(STAGE_RESTING)
        device.set_pof_threshold(POF_THRESHOLD_V)
        # A reboot during the run would end it.
        device.auto_reboot = False
        device.set_charger_enabled(False)
        device.set_profile(
            REPORT_INTERVAL_MS,
            NTC_REPORT_INTERVAL_MS,
            profile.v_cutoff,
            [*resting_steps(), *discharge_steps(profile)],
        )
        self._discard_events()  # what happened while charging is not part of the run
        device.start_profiling()
        self._say(
            f"Resting the battery for 15 minutes before discharge at "
            f"{js_number(self.temperature)} °C."
        )

    def _discard_events(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                return

    def _record(self) -> ProfilingResult:
        profile = self.profile
        stage = STAGE_RESTING
        start_ms: Optional[int] = None
        samples = 0
        consumed = 0.0
        last_sample = time.monotonic()
        last_report = time.monotonic()
        csv = None
        outcome = message = None

        try:
            while outcome is None:
                try:
                    kind, value = self._events.get(timeout=1.0)
                except queue.Empty:
                    silent = time.monotonic() - last_sample
                    if silent > 300 and time.monotonic() - last_report > 300:
                        last_report = time.monotonic()
                        self._say(
                            f"No data from the EK for {silent / 60:.0f} minutes; "
                            "still waiting."
                        )
                    continue

                if kind == "end":
                    outcome = value
                elif kind == "usb" and value != logparse.USB_STATUS[0]:
                    outcome, message = "failed", (
                        f"USB PMIC was connected while {stage}; the run is void."
                    )
                elif kind == "sample":
                    sample: ProfilingSample = value
                    last_sample = time.monotonic()
                    if sample.vload_v <= BATTERY_CONNECTED_THRESHOLD_V:
                        outcome, message = "failed", (
                            f"The battery was disconnected while {stage}."
                        )
                    elif stage == STAGE_RESTING:
                        if sample.seq == 1:
                            stage = STAGE_PROFILING
                            start_ms = sample.timestamp_ms
                            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
                            csv = open(self.csv_path, "w", newline="", encoding="utf-8")
                            csv.write(CSV_HEADER)
                            csv.flush()
                            self._set_stage(STAGE_PROFILING)
                            self._update_project(
                                csvReady=False,
                                csvPath=os.path.relpath(
                                    self.csv_path, self.paths.settings
                                ),
                            )
                            self._say(f"Discharge started, recording to {self.csv_path}.")
                    else:
                        consumed += abs(sample.iload_a) * REPORT_INTERVAL_MS / 3600
                        samples += 1
                        temperature = (
                            self.temperature if profile.ntc == "ignore"
                            else sample.tbat_c
                        )
                        seconds = (sample.timestamp_ms - start_ms) / 1000
                        csv.write(
                            f"{js_number(seconds)},{js_number(sample.iload_a)},"
                            f"{js_number(sample.vload_v)},{js_number(temperature)}\r\n"
                        )
                        csv.flush()

                    if time.monotonic() - last_report > 600:
                        last_report = time.monotonic()
                        self._notify(
                            f"[{datetime.now():%H:%M:%S}] {stage}: "
                            f"{sample.vload_v:.3f} V, {consumed:.1f} of "
                            f"{js_number(profile.capacity_mah)} mAh, step {sample.seq}."
                        )
        finally:
            if csv is not None:
                csv.close()
            self._stop_quietly()

        return self._result(outcome, message, stage, samples, consumed)

    def _result(self, outcome, message, stage, samples, consumed) -> ProfilingResult:
        recorded = stage == STAGE_PROFILING and samples > 0
        if outcome == "complete":
            text, usable = "Profiling complete: all steps finished.", recorded
        elif outcome == "vcutoff":
            text, usable = "Profiling complete: the cut-off voltage was reached.", recorded
        elif outcome == "power-fail":
            text = "A power-fail warning ended the run before the cut-off voltage."
            usable = recorded  # usable with caution, as the app allows
        elif outcome == "thermal":
            outcome = "failed"
            text, usable = "Profiling was stopped by a thermal event.", False
        else:
            outcome = "failed"
            text, usable = message or "Profiling failed.", False
        return ProfilingResult(
            outcome=outcome,
            message=text,
            csv_path=self.csv_path if recorded else None,
            capacity_consumed_mah=consumed,
            samples=samples,
            usable=usable,
        )

    def _stop_quietly(self) -> None:
        try:
            if self.device.profiling_active():
                self.device.stop_profiling()
        except ShellError as error:
            log.warning("could not stop profiling: %s", error)
