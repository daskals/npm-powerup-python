"""Command line: ``python -m npm_powerup <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import model
from .npm1300 import SUPPORTED_FIRMWARE, Npm1300
from .ports import list_npm1300_ports
from .profiling import (
    BatteryProfile,
    ProfilingRun,
    ProjectPaths,
    load_project,
    profile_name,
    save_project,
)
from .recorder import record
from .shell import ShellError


def _open(args) -> Npm1300:
    device = Npm1300(args.port) if args.port else Npm1300()
    device.open()
    version = device.app_version()
    if version != SUPPORTED_FIRMWARE:
        print(
            f"Warning: the EK runs firmware {version}; this tool was written "
            f"for {SUPPORTED_FIRMWARE}. Commands may differ.",
            file=sys.stderr,
        )
    return device


def cmd_ports(args) -> int:
    ports = list_npm1300_ports()
    if not ports:
        print("No nPM1300 EK found.")
        return 1
    for port in ports:
        mode = "recovery mode (needs firmware)" if port.recovery_mode else "application"
        print(f"{port.device}  interface {port.interface}  {mode}  s/n {port.serial_number}")
    return 0


def cmd_info(args) -> int:
    with _open(args) as device:
        hardware = device.hw_version()
        print(f"Hardware:       {hardware.get('hw_version')}")
        print(f"Board version:  {hardware.get('version')}  ({hardware.get('pca')})")
        print(f"Firmware:       {device.app_version()}")
        powered = device.pmic_powered()
        print(f"PMIC powered:   {'yes' if powered else 'no'}")
        if powered:
            print(f"USB PMIC:       {device.read_usb_status()}")
            print(f"Charger:        {device.read_charging_state().describe()}")
            print(f"Fuel Gauge Board attached: {'yes' if device.profiling_available() else 'no'}")
    return 0


def cmd_shell(args) -> int:
    with _open(args) as device:
        print(device.command(" ".join(args.command)))
    return 0


def cmd_log(args) -> int:
    def show(sample) -> None:
        print(
            f"{sample.vbat_v:6.3f} V  {sample.ibat_ma:9.3f} mA  "
            f"{sample.tbat_c:6.2f} °C  SoC {sample.soc_pct:6.2f} %"
        )

    with _open(args) as device:
        if args.fuel_gauge:
            device.set_fuel_gauge_enabled(True)
        print(f"Recording to {args.output}. Press Ctrl+C to stop.")
        count = record(
            device,
            args.output,
            duration_s=args.duration,
            sample_ms=args.sample_ms,
            report_ms=args.report_ms,
            on_sample=None if args.quiet else show,
        )
    print(f"{count} measurements written to {args.output}.")
    return 0


def _profile(args) -> BatteryProfile:
    return BatteryProfile(
        name=args.name,
        capacity_mah=args.capacity,
        v_term=args.vterm,
        v_cutoff=args.vcutoff,
        i_chg_ma=args.ichg,
        i_term_percent=args.iterm,
        ntc=args.ntc,
        temperatures=tuple(args.temperature),
    )


def cmd_profile(args) -> int:
    profile = _profile(args)

    usable = []
    with _open(args) as device:
        for index, temperature in enumerate(profile.temperatures):
            if index > 0:
                input(f"Next profile: {temperature} °C. Press Enter to continue. ")
            run = ProfilingRun(
                device,
                profile,
                args.output,
                temperature_index=index,
                assume_charged=args.assume_charged,
            )
            result = run.run()
            if result.usable:
                usable.append(index)

    if not usable:
        print("No usable recording was made.")
        return 1
    if args.no_model:
        return 0
    return _build_models(profile, args.output, usable)


def _build_models(profile: BatteryProfile, output_dir, indexes) -> int:
    paths = ProjectPaths.of(output_dir, profile)
    params_files = []
    built = None
    for index in indexes:
        csv_path = paths.csv(profile, index)
        print(f"Generating the battery model from {csv_path.name}. This takes a while.")
        built = model.generate(csv_path, profile.v_term, profile.v_cutoff)
        run_dir = paths.run_dir(index)
        name = profile_name(profile, profile.temperatures[index])
        (run_dir / "battery_model.json").write_text(built.json, encoding="utf-8")
        (run_dir / "battery_model.inc").write_text(built.inc, encoding="utf-8")
        if built.params_json:
            params = run_dir / f"{name}_params.json"
            params.write_text(built.params_json, encoding="utf-8")
            params_files.append(params)

        project = load_project(paths)
        if project:
            project["profiles"][index].update(
                batteryJson=built.json,
                batteryInc=built.inc,
                paramsJson=built.params_json,
            )
            save_project(paths, project)

    if len(params_files) > 1:
        print("Merging the temperature models.")
        built = model.merge(params_files, profile.v_term, profile.v_cutoff)

    temperatures = "_".join(str(profile.temperatures[i]) for i in indexes)
    target = paths.root / f"{profile.name}_{temperatures}C"
    target.with_suffix(".json").write_text(built.json, encoding="utf-8")
    target.with_suffix(".inc").write_text(built.inc, encoding="utf-8")
    print(f"Battery model saved as {target}.json and {target}.inc")
    return 0


def cmd_model(args) -> int:
    built = model.generate(args.csv, args.vterm, args.vcutoff)
    out = Path(args.output or Path(args.csv).parent)
    out.mkdir(parents=True, exist_ok=True)
    (out / "battery_model.json").write_text(built.json, encoding="utf-8")
    (out / "battery_model.inc").write_text(built.inc, encoding="utf-8")
    if built.params_json:
        name = f"{Path(args.csv).stem}_params.json"
        (out / name).write_text(built.params_json, encoding="utf-8")
    print(f"Battery model written to {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="npm_powerup", description="Control an nPM1300 EK from Python."
    )
    parser.add_argument("--port", help="serial port of the EK shell; found automatically if omitted")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="action", required=True)

    commands.add_parser("ports", help="list connected nPM1300 EKs").set_defaults(run=cmd_ports)
    commands.add_parser("info", help="show EK versions and state").set_defaults(run=cmd_info)

    shell = commands.add_parser("shell", help="send one command to the EK shell")
    shell.add_argument("command", nargs="+")
    shell.set_defaults(run=cmd_shell)

    log_parser = commands.add_parser("log", help="record battery measurements to CSV")
    log_parser.add_argument("output")
    log_parser.add_argument("--duration", type=float, help="seconds; until Ctrl+C if omitted")
    log_parser.add_argument("--sample-ms", type=int, default=1000)
    log_parser.add_argument("--report-ms", type=int, default=1000)
    log_parser.add_argument("--fuel-gauge", action="store_true", help="switch the fuel gauge on first")
    log_parser.add_argument("--quiet", action="store_true")
    log_parser.set_defaults(run=cmd_log)

    profile = commands.add_parser("profile", help="profile a battery")
    profile.add_argument("name", help="battery name, letters and digits only")
    profile.add_argument("output", help="folder for the project")
    profile.add_argument("--capacity", type=float, required=True, help="mAh")
    profile.add_argument("--vterm", type=float, default=4.2, help="charge termination voltage")
    profile.add_argument("--vcutoff", type=float, default=3.0, help="discharge cut-off voltage")
    profile.add_argument("--ichg", type=float, help="charge current in mA; half the capacity if omitted")
    profile.add_argument("--iterm", type=int, default=10, choices=(10, 20))
    profile.add_argument("--ntc", default="10k", choices=("ignore", "10k", "47k", "100k"))
    profile.add_argument("--temperature", type=float, nargs="+", default=[25])
    profile.add_argument("--assume-charged", action="store_true", help="skip charging")
    profile.add_argument("--no-model", action="store_true", help="record only")
    profile.set_defaults(run=cmd_profile)

    model_parser = commands.add_parser("model", help="generate a battery model from a CSV")
    model_parser.add_argument("csv")
    model_parser.add_argument("--vterm", type=float, default=4.2)
    model_parser.add_argument("--vcutoff", type=float, default=3.0)
    model_parser.add_argument("--output")
    model_parser.set_defaults(run=cmd_model)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    try:
        return args.run(args)
    except (ShellError, model.ModelToolError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
