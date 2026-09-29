import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ek import FakeEk, after  # noqa: E402

from npm_powerup import (  # noqa: E402
    BatteryProfile,
    ConfigurationError,
    EventRecorder,
    apply_configuration,
    overlay,
    read_configuration,
    Npm1300,
    ProfileStep,
    ProfilingError,
    ProfilingRun,
    ShellCommandError,
    ShellTimeout,
    logparse,
)
from npm_powerup import config as configuration  # noqa: E402
from npm_powerup.npm1300 import check_ichg, check_vterm, js_number  # noqa: E402
from npm_powerup.profiling import discharge_steps, profile_name  # noqa: E402
from npm_powerup.recorder import MeasurementRecorder  # noqa: E402


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def open_device(ek, **options):
    device = Npm1300("FAKE", serial_factory=lambda port, baud: ek, timeout=2, **options)
    device.open()
    return device


class LogParsing(unittest.TestCase):
    def test_log_line(self):
        event = logparse.parse_log_line(
            "[01:02:03.456,789] <inf> module_pmic_adc: ibat=0.5,vbat=4.1"
        )
        self.assertEqual(event.timestamp_ms, 3_723_456)
        self.assertEqual(event.level, "inf")
        self.assertEqual(event.module, "module_pmic_adc")
        self.assertEqual(event.message, "ibat=0.5,vbat=4.1")

    def test_not_a_log_line(self):
        self.assertIsNone(logparse.parse_log_line("Value: 4200 mv"))

    def test_adc_sample(self):
        event = logparse.parse_log_line(
            "[00:00:01.000,000] <inf> module_pmic_adc: ibat=-0.012,vbat=4.10,"
            "tbat=24.8,soc=87.5,tte=3600,ttf=NaN,soh=98,cycle_count=3"
        )
        sample = logparse.parse_adc_sample(event)
        self.assertAlmostEqual(sample.ibat_ma, -12.0)
        self.assertEqual(sample.vbat_v, 4.10)
        self.assertEqual(sample.soc_pct, 87.5)
        self.assertNotEqual(sample.ttf, sample.ttf)  # NaN

    def test_charging_state(self):
        state = logparse.ChargingState.from_bits(0x03)
        self.assertTrue(state.battery_detected)
        self.assertTrue(state.battery_full)
        self.assertFalse(state.constant_current)

    def test_value_after_colon(self):
        self.assertEqual(logparse.value_after_colon("Value: 1."), "1")
        self.assertEqual(logparse.leading_int("2300 mv"), 2300)
        self.assertEqual(logparse.leading_int("10%"), 10)


class Numbers(unittest.TestCase):
    def test_js_number(self):
        self.assertEqual(js_number(3.0), "3")
        self.assertEqual(js_number(3.9), "3.9")
        self.assertEqual(js_number(800 / 6 / 1000), "0.13333333333333333")
        self.assertEqual(js_number(float("nan")), "NaN")

    def test_vterm_limits(self):
        for good in (3.5, 3.65, 4.0, 4.2, 4.45):
            check_vterm(good)
        for bad in (3.7, 3.9, 4.5, 4.22):
            with self.assertRaises(ValueError):
                check_vterm(bad)

    def test_ichg_limits(self):
        check_ichg(32)
        check_ichg(800)
        for bad in (30, 33, 802):
            with self.assertRaises(ValueError):
                check_ichg(bad)

    def test_profile_step_encoding(self):
        self.assertEqual(ProfileStep(500, 500, 0, 0, cycles=900).encode(), "500,500,0,0,900")
        self.assertEqual(
            ProfileStep(300000, 2700000, 0.1, 0, v_cutoff=3.55).encode(),
            "300000,2700000,0.1,0,NaN,3.55",
        )


class Shell(unittest.TestCase):
    def setUp(self):
        self.ek = FakeEk()
        self.device = open_device(self.ek)

    def tearDown(self):
        self.device.close()

    def test_reply(self):
        self.assertEqual(self.device.app_version(), "1.5.2+0")
        self.assertTrue(self.device.firmware_supported())
        self.assertEqual(self.device.hw_version()["hw_version"], "npm1300ek_nrf5340")

    def test_error_reply(self):
        with self.assertRaises(ShellCommandError):
            self.device.command("bad command")

    def test_timeout(self):
        self.ek.write = lambda data: len(data)  # firmware stops answering
        with self.assertRaises(ShellTimeout):
            self.device.command("hw_version", timeout=0.3)

    def test_long_command_with_wrapped_echo(self):
        profile = BatteryProfile("cell", 800)
        self.device.set_profile(1000, 8000, 3.0, discharge_steps(profile))
        sent = self.ek.written[-1]
        self.assertGreater(len(sent), 80)
        self.assertTrue(sent.startswith("cc_profile profile set 1000 8000 3 "))
        self.assertTrue(self.device.pmic_powered())  # still in step

    def test_log_lines_between_commands(self):
        samples = []
        self.device.on_adc_sample(samples.append)
        self.ek.adc(vbat=3.95)
        self.assertEqual(self.device.vterm(), 4.2)
        self.ek.adc(vbat=3.96)
        self.assertTrue(wait_for(lambda: len(samples) == 2))
        self.assertEqual([s.vbat_v for s in samples], [3.95, 3.96])
        self.assertTrue(self.device.battery_connected())

    def test_log_does_not_leak_into_reply(self):
        after(0, lambda: [self.ek.adc() for _ in range(20)])
        for _ in range(10):
            self.assertEqual(self.device.app_version(), "1.5.2+0")

    def test_without_echo(self):
        ek = FakeEk(echo=False)
        device = open_device(ek)
        try:
            self.assertEqual(device.app_version(), "1.5.2+0")
        finally:
            device.close()

    def test_commands_sent(self):
        self.device.set_vterm(4.2)
        self.device.set_ichg(400)
        self.device.set_pof_threshold(2.6)
        self.device.set_ldo_enabled(1, False)
        self.device.start_adc_sampling(1000, 2000)
        self.assertEqual(
            self.ek.written[-7:],
            [
                "npmx charger module charger set 0",
                "npmx charger termination_voltage normal set 4200",
                "npmx charger module charger set 0",
                "npmx charger charging_current set 400000",
                "npmx pof threshold set 2600",
                "npmx ldsw status set 1 0",
                "npm_adc sample 1000 2000",
            ],
        )

    def test_usb_status_from_log(self):
        self.ek.log("module_pmic", "No USB connection")
        self.assertTrue(wait_for(lambda: self.device.usb_status == "No USB connection"))

    def test_wrong_device_rejected(self):
        ek = FakeEk()
        ek.hw_version = "npm2100ek_nrf5340"
        from npm_powerup import UnsupportedDevice

        with self.assertRaises(UnsupportedDevice):
            open_device(ek)


class Recorder(unittest.TestCase):
    def test_csv(self):
        ek = FakeEk()
        device = open_device(ek)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            recorder = MeasurementRecorder(device, path)
            recorder.start()
            ek.adc(vbat=4.0, ibat=0.1)
            ek.adc(vbat=3.99, ibat=0.1)
            self.assertTrue(wait_for(lambda: recorder.samples == 2))
            recorder.stop()
            device.close()
            rows = path.read_text().splitlines()
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0].startswith("time,uptime_s,vbat_v,ibat_ma"))
        self.assertIn("4.0,100.0", rows[1])


class Profiling(unittest.TestCase):
    def setUp(self):
        self.ek = FakeEk()
        self.device = open_device(self.ek)
        self.folder = tempfile.TemporaryDirectory()
        self.profile = BatteryProfile("cell", 800)
        self.messages = []

    def tearDown(self):
        self.device.close()
        self.folder.cleanup()

    def run_profile(self, script, **options):
        run = ProfilingRun(
            self.device, self.profile, self.folder.name,
            notify=self.messages.append, poll_interval=0.05, **options,
        )
        after(0.2, script)
        return run, run.run()

    def test_defaults(self):
        self.assertEqual(self.profile.i_chg_ma, 400)
        self.assertEqual(profile_name(self.profile, 25), "cell_800mAh_Tp25")
        steps = [step.encode() for step in discharge_steps(self.profile)]
        self.assertEqual(steps[0], "500,500,0,0,300")
        self.assertEqual(steps[1], "420000,2700000,0.13333333333333333,0,NaN,3.9")
        self.assertEqual(steps[4], "300000,2700000,0.06666666666666667,0,NaN")

    def test_invalid_profile(self):
        with self.assertRaises(ValueError):
            BatteryProfile("bad name", 800)
        with self.assertRaises(ValueError):
            BatteryProfile("cell", 5000)

    def test_missing_fuel_gauge_board(self):
        self.ek.cc_sink = False
        run = ProfilingRun(self.device, self.profile, self.folder.name, notify=lambda m: None)
        with self.assertRaises(ProfilingError):
            run.run()

    def test_full_run(self):
        ek = self.ek

        def script():
            time.sleep(0.3)
            ek.charger_status = 0x03  # full
            time.sleep(0.3)
            ek.usb_status = 0  # cable removed
            wait_for(lambda: ek.profiling, 5)
            ek.profiling_sample(seq=0, vload=4.18)
            ek.profiling_sample(seq=1, vload=4.17)
            ek.profiling_sample(seq=1, vload=4.16)
            ek.profiling_sample(seq=2, vload=4.0, iload=0.1333)
            ek.profiling_sample(seq=2, vload=3.9, iload=0.1333)
            ek.log("module_cc_profiling", "vcutoff reached")

        run, result = self.run_profile(script)

        self.assertEqual(result.outcome, "vcutoff")
        self.assertTrue(result.usable)
        self.assertEqual(result.samples, 3)
        self.assertAlmostEqual(result.capacity_consumed_mah, 2 * 0.1333 * 1000 / 3600)

        rows = result.csv_path.read_bytes().decode().split("\r\n")
        self.assertEqual(rows[0], "Seconds,Current(A),Voltage(V),Temperature(C)")
        self.assertEqual(rows[1], "1,0,4.16,25")
        self.assertEqual(rows[2], "2,0.1333,4,25")
        self.assertEqual(result.csv_path.name, "cell_800mAh_Tp25.csv")

        project = json.loads(run.paths.settings.read_text())
        self.assertEqual(project["deviceType"], "npm1300")
        self.assertTrue(project["profiles"][0]["csvReady"])
        self.assertEqual(
            project["profiles"][0]["csvPath"],
            os.path.join("..", "profile_1", "cell_800mAh_Tp25.csv"),
        )
        self.assertEqual(run.stage, "complete")

        self.assertFalse(ek.profiling)
        self.assertTrue(self.device.auto_reboot)
        sent = ek.written
        start = sent.index("cc_profile start")
        self.assertEqual(sent[start - 1][:35], "cc_profile profile set 1000 8000 3 ")
        self.assertEqual(sent[start - 2], "npmx charger module charger set 0")
        self.assertEqual(sent[start - 3], "npmx pof threshold set 2600")
        self.assertIn("npmx buck status set 0 0", sent)
        self.assertNotIn("npmx buck status set 1 0", sent)
        self.assertTrue(any("Disconnect the USB PMIC cable" in m for m in self.messages))

    def test_waits_for_usb_to_be_connected(self):
        ek = self.ek

        def script():
            time.sleep(0.3)
            ek.usb_status = 1  # cable connected
            time.sleep(0.3)
            ek.charger_status = 0x03
            time.sleep(0.3)
            ek.usb_status = 0  # cable removed
            wait_for(lambda: ek.profiling, 5)
            ek.profiling_sample(seq=1)
            ek.profiling_sample(seq=2, iload=0.1)
            ek.log("module_cc_profiling", "Success: Profiling sequence completed")

        ek.usb_status = 0
        _, result = self.run_profile(script)
        self.assertEqual(result.outcome, "complete")
        self.assertTrue(any("Connect the USB PMIC cable" in m for m in self.messages))
        self.assertTrue(any("Disconnect the USB PMIC cable" in m for m in self.messages))

    def test_battery_removed(self):
        ek = self.ek

        def script():
            wait_for(lambda: ek.profiling, 5)
            ek.profiling_sample(seq=1)
            ek.profiling_sample(seq=2, vload=0.2)

        ek.usb_status = 0
        _, result = self.run_profile(script, assume_charged=True)
        self.assertEqual(result.outcome, "failed")
        self.assertFalse(result.usable)
        self.assertIn("disconnected", result.message)

    def test_usb_connected_during_run(self):
        ek = self.ek

        def script():
            wait_for(lambda: ek.profiling, 5)
            ek.profiling_sample(seq=1)
            ek.log("module_pmic", "Default USB 100/500mA")

        ek.usb_status = 0
        _, result = self.run_profile(script, assume_charged=True)
        self.assertEqual(result.outcome, "failed")
        self.assertIn("USB PMIC was connected", result.message)


class Settings(unittest.TestCase):
    def setUp(self):
        self.ek = FakeEk()
        self.device = open_device(self.ek)

    def tearDown(self):
        self.device.close()

    def sent(self, count):
        return self.ek.written[-count:]

    def test_charger_settings(self):
        self.device.set_trickle_voltage(2.5)
        self.device.set_recharging_enabled(False)
        self.device.set_jeita(cold=-5, hot=55)
        self.device.set_die_temperature_limits(stop=105)
        self.device.set_vterm_warm(4.0)
        self.assertEqual(
            self.sent(7),
            [
                "npmx charger module charger set 0",
                "npmx charger trickle_voltage set 2500",
                "npmx charger module recharge set 0",
                "npmx charger ntc_temperature cold set -5",
                "npmx charger ntc_temperature hot set 55",
                "npmx charger die_temp stop set 105",
                "npmx charger termination_voltage warm set 4000",
            ],
        )
        charger = self.device.read_charger()
        self.assertEqual(charger["trickle_voltage"], 2.5)
        self.assertEqual(charger["t_cold"], -5)
        self.assertEqual(charger["ntc"], "10k")
        self.assertFalse(charger["recharging"])

    def test_charger_limits(self):
        with self.assertRaises(ValueError):
            self.device.set_trickle_voltage(2.7)
        with self.assertRaises(ValueError):
            self.device.set_jeita(hot=80)
        with self.assertRaises(ValueError):
            self.device.set_iterm(15)

    def test_buck_settings(self):
        self.device.set_buck_voltage(0, 1.8)
        self.device.set_buck_mode_control(0, "PWM")
        self.device.set_buck_on_off_control(0, "GPIO2")
        self.device.set_buck_retention_control(0, "Off")
        self.device.set_buck_mode(0, "vset")
        self.assertEqual(
            self.sent(6),
            [
                "npmx buck voltage normal set 0 1800",
                "npmx buck vout_select set 0 1",
                "powerup_buck mode set 0 PWM",
                "npmx buck gpio on_off index set 0 2",
                "npmx buck gpio retention index set 0 -1",
                "npmx buck vout_select set 0 0",
            ],
        )
        buck = self.device.read_buck(0)
        self.assertEqual(buck["mode"], "vset")
        self.assertEqual(buck["mode_control"], "PWM")
        self.assertEqual(buck["on_off_control"], "GPIO2")
        self.assertEqual(buck["retention_control"], "Off")

    def test_buck_2_is_protected(self):
        before = len(self.ek.written)
        with self.assertRaises(ValueError):
            self.device.set_buck_voltage(1, 1.2)
        with self.assertRaises(ValueError):
            self.device.set_buck_enabled(1, False)
        self.assertEqual(len(self.ek.written), before)
        self.device.set_buck_voltage(1, 1.2, force=True)
        self.assertEqual(self.sent(2)[0], "npmx buck voltage normal set 1 1200")

    def test_ldo_settings(self):
        self.device.set_ldo_voltage(1, 2.5)
        self.device.set_ldo_soft_start_current(1, 50)
        self.device.set_ldo_on_off_control(1, "GPIO0")
        self.assertEqual(
            self.sent(4),
            [
                "npmx ldsw mode set 1 1",
                "npmx ldsw ldo_voltage set 1 2500",
                "npmx ldsw soft_start current set 1 50",
                "npmx ldsw gpio index set 1 0",
            ],
        )
        ldo = self.device.read_ldo(1)
        self.assertEqual(ldo["mode"], "ldo")
        self.assertEqual(ldo["voltage"], 2.5)

    def test_gpio_and_led(self):
        self.device.set_gpio_mode(3, "Output interrupt")
        self.device.set_gpio_pull(3, "Pull up")
        self.device.set_gpio_drive(3, 6)
        self.device.set_led_mode(2, "Charging")
        self.assertEqual(
            self.sent(4),
            [
                "npmx gpio config mode set 3 5",
                "npmx gpio config pull set 3 1",
                "npmx gpio config drive set 3 6",
                "npmx led mode set 2 1",
            ],
        )
        self.assertEqual(self.device.gpio_mode(3), "Output interrupt")
        self.assertEqual(self.device.led_mode(2), "Charging")
        with self.assertRaises(ValueError):
            self.device.set_gpio_mode(5, "Input")

    def test_system_features(self):
        self.device.set_long_press_reset("two_button")
        self.device.set_ship_time_to_active(304)
        self.device.set_timer(mode="Wake-up", prescaler="Fast", period=1000)
        self.device.set_power_failure(enabled=True, threshold=2.8, polarity="Active high")
        self.device.set_vbus_current_limit(1.5)
        self.device.enter_ship_mode()
        self.assertEqual(
            self.sent(10),
            [
                "powerup_ship longpress set two_button",
                "npmx ship config time set 304",
                "npmx timer config mode set 4",
                "npmx timer config prescaler set 1",
                "npmx timer config compare set 1000",
                "npmx pof status set 1",
                "npmx pof threshold set 2800",
                "npmx pof polarity set 1",
                "npmx vbusin current_limit set 1500",
                "npmx ship mode ship",
            ],
        )
        self.assertEqual(self.device.timer()["mode"], "Wake-up")
        self.assertEqual(self.device.long_press_reset(), "two_button")
        self.assertEqual(self.device.vbus_current_limit_a(), 1.5)

    def test_error_logs(self):
        logs = self.device.error_logs()
        self.assertEqual(logs["reset_cause"], ["SWRESET"])
        self.assertEqual(logs["charger_errors"], [])

    def test_write_battery_model(self):
        content = json.dumps({"name": "cell", "param_1": list(range(300))})
        progress = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.json"
            path.write_text(content + "\n")
            self.device.write_battery_model(
                path, slot=1, on_progress=lambda done, total: progress.append((done, total))
            )
        self.assertEqual(self.ek.downloaded, content)
        self.assertEqual(self.ek.applied_slot, 1)
        self.assertFalse(self.ek.downloading)
        self.assertGreater(len(progress), 1)
        self.assertEqual(progress[-1][0], progress[-1][1])
        self.assertEqual(self.device.app_version(), "1.5.2+0")  # echo is back


class Configuration(unittest.TestCase):
    def setUp(self):
        self.ek = FakeEk()
        self.device = open_device(self.ek)

    def tearDown(self):
        self.device.close()

    def test_export_matches_app_format(self):
        config = read_configuration(self.device)
        self.assertEqual(config["fileFormatVersion"], 2)
        self.assertEqual(config["deviceType"], "npm1300")
        self.assertEqual(config["firmwareVersion"], "1.5.2+0")
        self.assertEqual(config["boosts"], [])
        self.assertEqual(config["charger"]["vTerm"], 4.2)
        self.assertEqual(config["charger"]["iChg"], 400)
        self.assertEqual(config["charger"]["ntcThermistor"], "10 k\u03a9")
        self.assertEqual(config["bucks"][0]["mode"], "software")
        self.assertEqual(config["bucks"][0]["onOffControl"], "Off")
        self.assertEqual(config["ldos"][0]["mode"], "Load_switch")
        self.assertEqual(config["ldos"][0]["onOffControl"], "SW")
        self.assertEqual(len(config["gpios"]), 5)
        self.assertEqual(config["leds"][1], {"mode": "Charging"})
        self.assertEqual(config["lowPower"], {"timeToActive": "96"})
        self.assertEqual(config["usbPower"], {"currentLimiter": 0.5})

    def test_round_trip(self):
        self.device.set_buck_voltage(0, 2.1)
        self.device.set_gpio_mode(1, "Output reset")
        self.device.set_led_mode(0, "Host")
        self.device.set_timer(mode="General purpose", period=250)
        exported = read_configuration(self.device)

        other = FakeEk()
        target = open_device(other)
        try:
            failures = apply_configuration(target, exported)
            self.assertEqual(failures, [])
            self.assertEqual(read_configuration(target), exported)
        finally:
            target.close()

    def test_wrong_device_refused(self):
        config = read_configuration(self.device)
        config["deviceType"] = "npm2100"
        with self.assertRaises(ConfigurationError):
            apply_configuration(self.device, config)

    def test_file_round_trip(self):
        config = read_configuration(self.device)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            configuration.save_file(path, config)
            self.assertEqual(configuration.load_file(path), config)
            overlay_path = Path(folder) / "config.overlay"
            configuration.save_file(overlay_path, config)
            self.assertIn("npm1300_pmic: pmic@6b", overlay_path.read_text())

    def test_overlay(self):
        self.device.set_gpio_mode(2, "Output interrupt")
        self.device.set_gpio_mode(4, "Output reset")
        self.device.set_buck_on_off_control(0, "GPIO1")
        self.device.set_ldo_voltage(0, 1.8)
        text = overlay(read_configuration(self.device), i2c_bus="i2c0")

        self.assertEqual(text.count("{"), text.count("}"))
        for expected in (
            "&i2c0 {",
            'compatible = "nordic,npm1300";',
            "reg = <0x6b>;",
            "pmic-int-pin = <2>;",
            "ship-to-active-time-ms = <96>;",
            'long-press-reset = "one-button";',
            "npm1300_buck1: BUCK1 {",
            "regulator-init-microvolt = <1800000>;",
            "regulator-initial-mode = <NPM13XX_BUCK_MODE_AUTO>;",
            "enable-gpio-config = <1 GPIO_ACTIVE_HIGH>;",
            "regulator-initial-mode = <NPM13XX_LDSW_MODE_LDO>;",
            "soft-start-microamp = <25000>;",
            "term-microvolt = <4200000>;",
            "current-microamp = <400000>;",
            "vbus-limit-microamp = <500000>;",
            "thermistor-ohms = <10000>;",
            "term-current-percent = <10>;",
            'nordic,led0-mode = "error";',
            "reset-gpios = <&npm1300_gpio 4 GPIO_ACTIVE_LOW>;",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("thermistor-cold-millidegrees", text)  # default left out
        self.assertNotIn("disable-recharge", text)


class Events(unittest.TestCase):
    def test_record_events(self):
        ek = FakeEk()
        device = open_device(ek)
        with tempfile.TemporaryDirectory() as folder:
            with EventRecorder(device, folder) as recorder:
                ek.adc(vbat=4.0)
                ek.log("module_pmic", "No USB connection")
                device.vterm()
                self.assertTrue(wait_for(lambda: recorder.events == 3))
            device.close()
            all_events = (Path(folder) / "all_events.csv").read_text().splitlines()
            adc = (Path(folder) / "module_pmic_adc.csv").read_text().splitlines()
        self.assertEqual(all_events[0], "timestamp,logLevel,module,message")
        self.assertEqual(len(all_events), 4)
        self.assertIn("shell_commands", all_events[3])
        self.assertTrue(adc[0].startswith("timestamp,ibat,vbat,tbat,soc"))
        self.assertIn(",4.0,", adc[1])

if __name__ == "__main__":
    unittest.main()
