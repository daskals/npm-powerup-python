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

| Command | What it does | In the app |
|---|---|---|
| `ports` | Lists connected EKs | Select Device |
| `info` | Versions, charger state, Fuel Gauge Board | Connection Status |
| `show` | Every setting, and the error logs | All tabs |
| `export config.json` | Saves the configuration | Export Configuration |
| `export config.overlay` | Saves it as a Zephyr overlay | Export Configuration |
| `load config.json` | Writes a configuration to the EK | Load Configuration |
| `overlay config.json out.overlay` | Converts a saved configuration, no EK needed | |
| `shell <command>` | Sends one shell command | Open Serial Terminal |
| `reset` | Restarts the EK firmware | Reset Device |
| `events <folder>` | Records everything the EK reports | Record Events |
| `log out.csv` | Records battery measurements | Battery Status |
| `plot out.csv` | Graphs a recording, needs `matplotlib` | Graph |
| `models` | Lists bundled models and those on the EK | Profiles |
| `write-model <model>` | Writes a battery model to the EK | Write Model |
| `profile <name> <folder>` | Profiles a battery | Profile Battery |
| `model <csv>` | Generates a model from a recording | Profiles |
| `ship-mode`, `hibernate` | Powers the PMIC down | System Features |

```
python -m npm_powerup info
python -m npm_powerup export config.overlay
python -m npm_powerup log measurements.csv --duration 600 --fuel-gauge
python -m npm_powerup write-model "LP803448 (1350 mAh)"
python -m npm_powerup profile MyCell profiles --capacity 1350 --temperature 25 5 45
```

Add `--port COM7` before the command to choose a port by hand.

## Python

```python
from npm_powerup import (
    Npm1300, BatteryProfile, ProfilingRun, record, read_configuration, overlay,
)

with Npm1300() as ek:
    print(ek.app_version(), ek.read_charging_state().describe())

    # Charger
    ek.set_vterm(4.2)
    ek.set_ichg(400)
    ek.set_jeita(cold=0, cool=10, warm=45, hot=60)
    ek.set_charger_enabled(True)

    # Regulators
    ek.set_buck_voltage(0, 1.8)
    ek.set_buck_mode_control(0, "PWM")
    ek.set_ldo_voltage(0, 2.5)
    ek.set_ldo_enabled(0, True)

    # GPIOs, LEDs and system features
    ek.set_gpio_mode(0, "Output interrupt")
    ek.set_led_mode(0, "Charging")
    ek.set_timer(mode="Wake-up", prescaler="Fast", period=1000)
    ek.set_power_failure(enabled=True, threshold=2.8)
    ek.set_vbus_current_limit(1.5)

    # Measurements
    ek.on_adc_sample(lambda s: print(s.vbat_v, s.ibat_ma, s.tbat_c))
    record(ek, "measurements.csv", duration_s=60)

    # Configuration
    open("config.overlay", "w").write(overlay(read_configuration(ek)))

    # Battery profiling
    profile = BatteryProfile("MyCell", capacity_mah=800, v_term=4.2, v_cutoff=3.0)
    result = ProfilingRun(ek, profile, "profiles").run()
    print(result.outcome, result.csv_path)
```

Each setting has a matching reader, and `read_charger()`, `read_buck(i)`,
`read_ldo(i)` and `read_gpio(i)` return a whole group. Values outside the
range the app allows raise `ValueError` before anything is sent.

Listeners run on the reader thread. They must not send commands to the EK.

### Buck 2

Buck 2 powers the link between the EK controller and the PMIC. Switching it
off, or setting it to 1.6 V or less, can cut the connection. Where the app asks
for confirmation, this package needs `force=True`.

## Coverage of Nordic's tutorial videos

The package was checked against the three nPM PowerUP tutorial videos, which
show version 1.2.1 of the app.

| Video | Shown | Here |
|---|---|---|
| 1 | Connecting, battery status, fuel gauge | `info`, `log`, `set_fuel_gauge_enabled` |
| 1 | Selecting and writing a battery model | `models`, `write-model`, `set_active_battery_model` |
| 1 | Charger tab, JEITA, thermal regulation | `set_vterm`, `set_jeita`, `set_die_temperature_limits` and others |
| 1 | Regulators tab | `set_buck_*`, `set_ldo_*` |
| 1 | GPIOs and LEDs tab | `set_gpio_*`, `set_led_mode` |
| 1 | System features, ship mode, error logs | `set_timer`, `set_power_failure`, `enter_ship_mode`, `error_logs` |
| 1 | Graph tab | `plot` |
| 1 | Export, load, serial terminal, reset, record events | `export`, `load`, `shell`, `reset`, `events` |
| 2 | Exporting an overlay for a Zephyr application | `export config.overlay`, `overlay` |
| 2 | Configuring without an EK connected | `overlay` from a saved `.json` |
| 3 | Profiling at several temperatures, model generation | `profile`, `model` |

Not covered, as they happen outside the app: wiring the EK to a development
kit, and building and flashing the Zephyr application in part 2.

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
- The battery health settings added in version 2.2.6 of the app are not
  implemented.
- The generated overlay has not been built into a Zephyr application.
- Time-to-empty and time-to-full are passed through as the firmware reports
  them.

## Battery models

The models bundled with nPM PowerUP are Nordic's files and are not included in
this repository. `models` and `write-model` read them from the `reference`
submodule, or from a copy of the app named by `NPM_POWERUP_APP`.

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
