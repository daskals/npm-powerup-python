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
    Npm1300,
    ProfileStep,
    ProfilingError,
    ProfilingRun,
    ShellCommandError,
    ShellTimeout,
    logparse,
)
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


if __name__ == "__main__":
    unittest.main()
