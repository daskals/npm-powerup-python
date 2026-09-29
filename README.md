# npm-powerup-python

Python API for the Nordic Semiconductor nPM1300 Evaluation Kit (EK). It does
from Python what the nPM PowerUP desktop app does through its interface:
read and set the PMIC, record battery measurements, and profile a battery to
produce a fuel gauge model.

This is an unofficial, independent implementation of the serial shell protocol
that the nPM PowerUP app uses. It is not affiliated with or supported by Nordic
Semiconductor. The shell commands are not a published interface and can change
between firmware versions.

## Status

| Part | State |
|---|---|
| Unit tests against a simulated EK | Pass |
| Communication with a real nPM1300 EK | **Not yet tested** |
| A full profiling run on hardware | **Not yet tested** |

The simulated EK in `tests/fake_ek.py` encodes assumptions about how the shell
echoes commands and interleaves log lines. Treat the first session with real
hardware as a test of those assumptions. Start with `info` and `log` before
attempting a profiling run.

## Requirements

- Python 3.10 or later
- `pyserial`
- nPM1300 EK with firmware 1.5.2+0, which the nPM PowerUP app programs
- For profiling: the nPM Fuel Gauge Board, attached to `P20` and `P21`
- For model generation: `nrfutil npm`. The copy that nRF Connect for Desktop
  installs for nPM PowerUP is found automatically. Otherwise run
  `nrfutil install npm`, or set `NRFUTIL_NPM` to the path of `nrfutil-npm`.

Only one program can hold the serial port. Close nPM PowerUP before using this
package, and the reverse.

## Installation

```
git clone https://github.com/daskals/npm-powerup-python.git
cd npm-powerup-python
pip install .
```

## Command line

```
python -m npm_powerup ports
python -m npm_powerup info
python -m npm_powerup shell npmx charger status all get
python -m npm_powerup log measurements.csv --duration 600
python -m npm_powerup profile MyCell profiles --capacity 800 --vterm 4.2 --vcutoff 3.0
python -m npm_powerup model profiles/MyCell/profile_1/MyCell_800mAh_Tp25.csv
```

Add `--port COM7` before the command to choose a port by hand.

## Python

```python
from npm_powerup import Npm1300, BatteryProfile, ProfilingRun, record

with Npm1300() as ek:
    print(ek.app_version(), ek.read_charging_state().describe())

    ek.set_vterm(4.2)
    ek.set_ichg(400)
    ek.set_charger_enabled(True)

    ek.on_adc_sample(lambda s: print(s.vbat_v, s.ibat_ma, s.tbat_c))
    record(ek, "measurements.csv", duration_s=60)

    profile = BatteryProfile("MyCell", capacity_mah=800, v_term=4.2, v_cutoff=3.0)
    result = ProfilingRun(ek, profile, "profiles").run()
    print(result.outcome, result.csv_path)
```

Listeners run on the reader thread. They must not send commands to the EK.

## Battery profiling

Take the battery values from its datasheet. A run takes about 48 hours per
temperature, longer for large batteries. Keep the computer awake for the whole
run.

The EK has two USB ports, and they behave differently during a run:

| Port | Role | During discharge |
|---|---|---|
| `J4` nPM Controller | Serial link to the computer, powers the Fuel Gauge Board | Stays connected |
| `J3` USB PMIC | Power for charging the battery | Must be disconnected |

Stages, as in the app's profiling wizard:

1. **Charge.** With USB PMIC connected, the charger is configured and the
   battery is charged until the PMIC reports it full.
2. **Disconnect USB PMIC.** The run asks for the cable to be removed and waits
   until the PMIC reports no USB.
3. **Rest.** 15 minutes with no load.
4. **Discharge.** A stepped, pulsed discharge down to the cut-off voltage.
   Current, voltage and temperature are recorded once per second.
5. **Model.** `nrfutil npm generate` turns the recording into
   `battery_model.json` and `battery_model.inc`. Profiles at several
   temperatures are merged into one model.

The run is abandoned if the battery is disconnected, if USB PMIC is connected
during discharge, or on a thermal event.

### Output

The folder layout follows the nPM PowerUP app, with the aim that the app's
Profiles tab can open the project. This has not been verified.

```
<output>/<name>/
    profileSettings.json
    <name>_25C.json, <name>_25C.inc        final model
    profile_1/
        <name>_<capacity>mAh_Tp25.csv       recording
        battery_model.json, battery_model.inc
```

## Limits

- nPM1300 only. nPM1304, nPM2100 and nPM1012 use different commands.
- Firmware is not programmed by this package. Use nPM PowerUP for that.
- Exporting settings as a devicetree overlay is not implemented.
- Time-to-empty and time-to-full are passed through as the firmware reports
  them.

## Reference

`reference/pc-nrfconnect-npm` is a git submodule pointing to Nordic's
[nPM PowerUP app](https://github.com/nordicsemi/pc-nrfconnect-npm), pinned to
the commit (app version 2.2.7) that this package was written against. It is
there to compare shell commands when the firmware changes. It is Nordic's code
under Nordic's licence, and nothing in this package imports it. Fetch it with:

```
git submodule update --init
```

## Tests

```
python -m unittest discover -s tests
```

## Acknowledgement

Inspired by [ppk2-api-python](https://github.com/IRNAS/ppk2-api-python) by
IRNAS, which does the same for the Power Profiler Kit II.
