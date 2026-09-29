"""Battery model generation with Nordic's ``nrfutil npm`` tool."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


class ModelToolError(Exception):
    pass


@dataclass(frozen=True)
class BatteryModel:
    json: str
    inc: str
    params_json: Optional[str] = None


def find_tool() -> list[str]:
    """The command line that runs ``nrfutil npm``.

    Looks for an ``npm`` command installed in nrfutil, then for the copy
    that nRF Connect for Desktop keeps for the nPM PowerUP app.
    """
    override = os.environ.get("NRFUTIL_NPM")
    if override:
        return [override]

    nrfutil = shutil.which("nrfutil")
    if nrfutil:
        probe = subprocess.run(
            [nrfutil, "npm", "--version"], capture_output=True, text=True
        )
        if probe.returncode == 0:
            return [nrfutil, "npm"]

    appdata = os.environ.get("APPDATA")
    if appdata:
        pattern = os.path.join(
            appdata, "nrfconnect", "nrfutil-sandboxes", "*", "npm", "*", "bin",
            "nrfutil-npm*",
        )
        found = sorted(glob.glob(pattern))
        if found:
            return [found[-1]]

    raise ModelToolError(
        "nrfutil npm was not found. Install it with 'nrfutil install npm', "
        "or set NRFUTIL_NPM to the path of nrfutil-npm."
    )


def _run(arguments: Sequence[str]) -> None:
    result = subprocess.run(
        [*find_tool(), *arguments], capture_output=True, text=True
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ModelToolError(f"nrfutil npm {arguments[0]} failed: {detail}")


def _read_results(results: Path, params_name: Optional[str]) -> BatteryModel:
    model_json = results / "battery_model.json"
    model_inc = results / "battery_model.inc"
    if not (model_json.exists() and model_inc.exists()):
        raise ModelToolError(f"no battery model was written to {results}")
    params = None
    if params_name:
        params_file = results / params_name
        if params_file.exists():
            params = params_file.read_text(encoding="utf-8")
    return BatteryModel(
        json=model_json.read_text(encoding="utf-8"),
        inc=model_inc.read_text(encoding="utf-8"),
        params_json=params,
    )


def generate(csv_path: os.PathLike, v_cutoff_high: float, v_cutoff_low: float) -> BatteryModel:
    """Turn the CSV of one profiling run into a battery model.

    This takes a long time. The name of the CSV file is carried into the
    model, so keep the name the profiler gave it.
    """
    csv_path = Path(csv_path)
    with tempfile.TemporaryDirectory(prefix="npm-model-") as temp:
        work = Path(temp)
        source = work / csv_path.name
        shutil.copyfile(csv_path, source)
        results = work / "Results"
        results.mkdir()
        _run([
            "generate",
            "--input-file", str(source),
            "--output-directory", str(results),
            "--v-cutoff-high", str(v_cutoff_high),
            "--v-cutoff-low", str(v_cutoff_low),
        ])
        return _read_results(results, f"{csv_path.stem}_params.json")


def merge(
    params_files: Sequence[os.PathLike], v_cutoff_high: float, v_cutoff_low: float
) -> BatteryModel:
    """Combine the models of several temperatures into one."""
    if not params_files:
        raise ValueError("nothing to merge")
    with tempfile.TemporaryDirectory(prefix="npm-model-") as temp:
        results = Path(temp) / "Results"
        results.mkdir()
        arguments = [
            "merge",
            "--output-directory", str(results),
            "--v-cutoff-high", str(v_cutoff_high),
            "--v-cutoff-low", str(v_cutoff_low),
        ]
        for params in params_files:
            arguments += ["--input-file", str(params)]
        _run(arguments)
        return _read_results(results, None)
