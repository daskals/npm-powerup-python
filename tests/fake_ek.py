"""A stand-in for the serial port of an nPM1300 EK.

It behaves as the firmware shell is understood to behave: commands are
echoed, the reply follows, then the prompt. Log lines erase the prompt,
print, and redraw it. This reflects assumptions, not a recording of a
real kit.
"""

from __future__ import annotations

import queue
import threading
import time

PROMPT = "shell:~$ "
ERASE_LINE = "\x1b[2K\r"
COLUMNS = 80

# What the settings read as before anything is set.
DEFAULTS = {
    "npmx charger termination_voltage normal": "4200",
    "npmx charger termination_voltage warm": "3600",
    "npmx charger charging_current": "400000",
    "npmx charger termination_current": "10",
    "npmx charger trickle_voltage": "2900",
    "npmx charger module recharge": "1",
    "npm_adc fullscale": "1000",
    "npmx adc ntc type": "10000",
    "npmx adc ntc beta": "3380",
    "npmx charger ntc_temperature cold": "0",
    "npmx charger ntc_temperature cool": "10",
    "npmx charger ntc_temperature warm": "45",
    "npmx charger ntc_temperature hot": "60",
    "npmx charger die_temp stop": "110",
    "npmx charger die_temp resume": "100",
    "npmx buck voltage normal 0": "1800",
    "npmx buck voltage normal 1": "3000",
    "npmx buck voltage retention 0": "1200",
    "npmx buck voltage retention 1": "1800",
    "npmx buck status 0": "1",
    "npmx buck status 1": "1",
    "npmx buck vout_select 0": "1",
    "npmx buck vout_select 1": "1",
    "powerup_buck mode 0": "Auto",
    "powerup_buck mode 1": "Auto",
    "npmx buck gpio on_off index 0": "-1",
    "npmx buck gpio on_off index 1": "-1",
    "npmx buck gpio retention index 0": "-1",
    "npmx buck gpio retention index 1": "-1",
    "npmx ldsw ldo_voltage 0": "1000",
    "npmx ldsw ldo_voltage 1": "1000",
    "npmx ldsw soft_start current 0": "25",
    "npmx ldsw soft_start current 1": "25",
    "npmx ldsw gpio index 0": "-1",
    "npmx ldsw gpio index 1": "-1",
    "npmx gpio config drive 0": "1",
    "npmx gpio config drive 1": "1",
    "npmx gpio config drive 2": "1",
    "npmx gpio config drive 3": "1",
    "npmx gpio config drive 4": "1",
    "npmx led mode 0": "0",
    "npmx led mode 1": "1",
    "npmx led mode 2": "2",
    "npmx ship config time": "96",
    "powerup_ship longpress": "one_button",
    "npmx pof status": "1",
    "npmx pof threshold": "2800",
    "npmx pof polarity": "1",
    "npmx vbusin current_limit": "500",
}


def stamp(ms: int) -> str:
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"[{hours:02}:{minutes:02}:{seconds:02}.{millis:03},000]"


class FakeEk:
    def __init__(self, wrap_echo: bool = True, echo: bool = True):
        self.is_open = True
        self.written: list[str] = []
        self.uptime_ms = 1000
        self.wrap_echo = wrap_echo
        self.echo = echo
        self._out: "queue.Queue[bytes]" = queue.Queue()
        self._pending = b""
        self._input = ""
        self._lock = threading.Lock()

        self.charger_enabled = False
        self.charger_status = 0x09  # battery detected, constant current
        self.usb_status = 1
        self.fuel_gauge = False
        self.profiling = False
        self.cc_sink = True
        self.app_version = "1.5.2+0"
        self.hw_version = "npm1300ek_nrf5340"
        self.replies: dict[str, str] = {}
        self.downloading = False
        self.downloaded = ""
        self.applied_slot = None
        self.values: dict[str, str] = dict(DEFAULTS)

    # -- serial port interface ----------------------------------------------

    @property
    def in_waiting(self) -> int:
        return len(self._pending) + sum(len(b) for b in list(self._out.queue))

    def read(self, size: int = 1) -> bytes:
        if not self.is_open:
            raise OSError("port is closed")
        if not self._pending:
            try:
                self._pending = self._out.get(timeout=0.05)
            except queue.Empty:
                return b""
        data, self._pending = self._pending[:size], self._pending[size:]
        return data

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise OSError("port is closed")
        self._input += data.decode("utf-8")
        while "\r\n" in self._input:
            line, self._input = self._input.split("\r\n", 1)
            self._run(line)
        return len(data)

    def close(self) -> None:
        self.is_open = False

    # -- firmware behaviour --------------------------------------------------

    def _send(self, text: str) -> None:
        self._out.put(text.encode("utf-8"))

    def _echo(self, line: str) -> str:
        if not self.echo or self.downloading:
            return ""
        if not self.wrap_echo:
            return line
        width = COLUMNS - len(PROMPT)
        first, rest = line[:width], line[width:]
        chunks = [first] + [rest[i:i + COLUMNS] for i in range(0, len(rest), COLUMNS)]
        return "\r\n".join(chunks)

    def _run(self, line: str) -> None:
        with self._lock:
            if not line.strip():
                self._send("\r\n" + PROMPT)
                return
            self.written.append(line)
            echo = self._echo(line)
            reply = self.reply_to(line)
            self._send(echo + "\r\n" + reply + "\r\n" + PROMPT)

    def log(self, module: str, message: str, level: str = "inf") -> None:
        with self._lock:
            self.uptime_ms += 1000
            self._send(
                f"{ERASE_LINE}{stamp(self.uptime_ms)} <{level}> {module}: "
                f"{message}\r\n{PROMPT}"
            )

    def adc(self, vbat=3.9, ibat=0.01, tbat=24.5, soc="NaN") -> None:
        self.log(
            "module_pmic_adc",
            f"ibat={ibat},vbat={vbat},tbat={tbat},soc={soc},tte=NaN,ttf=NaN,"
            "soh=NaN,cycle_count=0",
        )

    def profiling_sample(self, seq, vload=4.1, iload=0.0, tbat=25.0) -> None:
        self.log(
            "module_cc_profiling",
            f"iload={iload},vload={vload},tbat={tbat},cycle=0,seq={seq},rep=0,tload=500",
        )

    def reply_to(self, line: str) -> str:
        if line in self.replies:
            return self.replies[line]
        words = line.split()

        if line == "hw_version":
            return f"hw_version={self.hw_version},version=1.2.0,pca=PCA10152"
        if line == "app_version":
            return f"app_version={self.app_version}"
        if line == "pmic_revision":
            return "pmic_revision=2.1"
        if line == "kernel uptime":
            return f"Uptime: {self.uptime_ms} ms"
        if line == "npm_pmic_ping check":
            return "Pinging PMIC succeeded"
        if line == "cc_sink available":
            return f"Value: {int(self.cc_sink)}."
        if line == "cc_profile active":
            return f"Value: {int(self.profiling)}."
        if line == "cc_profile start":
            self.profiling = True
            return "Success: Profiling started"
        if line == "cc_profile stop":
            if not self.profiling:
                return "Error: No profiling ongoing"
            self.profiling = False
            return "Success: Profiling stopped"
        if line.startswith("cc_profile profile set"):
            return "Success: Profile set"
        if line == "npmx charger status all get":
            return f"Value: {self.charger_status}"
        if line == "powerup_vbusin status get":
            return f"Value: {self.usb_status}."
        if line.startswith("npmx charger module charger"):
            if words[-2] == "set":
                self.charger_enabled = words[-1] == "1"
            return f"Value: {int(self.charger_enabled)}."
        if line.startswith("fuel_gauge set"):
            self.fuel_gauge = words[-1] == "1"
            return f"Value: {int(self.fuel_gauge)}."
        if line == "fuel_gauge get":
            return f"Value: {int(self.fuel_gauge)}."
        if line == "fuel_gauge model download begin":
            self.downloading, self.downloaded = True, ""
            return "Success: Download started"
        if line.startswith("fuel_gauge model download apply"):
            self.downloading, self.applied_slot = False, int(words[-1])
            return "Success: Model applied"
        if line == "fuel_gauge model download abort":
            self.downloading = False
            return "Success: Download aborted"
        if line.startswith('fuel_gauge model download "'):
            chunk = line[len('fuel_gauge model download "'):-1]
            self.downloaded += chunk.replace('\\"', '"')
            return "Success: Chunk received"
        if line == "fuel_gauge model get":
            return 'Value: name="LP803448",Q={1491.63 mAh}'
        if line == "npmx errlog get":
            return "RSTCAUSE:\r\nSWRESET\r\nCHARGER_ERROR:\r\nSENSOR_ERROR:"
        if line.startswith("npm_adc sample"):
            return "Value: sample interval=1000, report interval=2000"
        if line.startswith("npmx ship mode"):
            return "Success"
        if line == "bad command":
            return "Error: unknown"
        if "set" in words:
            at = words.index("set")
            key = " ".join(words[:at] + words[at + 1:-1])
            self.values[key] = words[-1]
            return f"Value: {words[-1]}."
        if "get" in words:
            at = words.index("get")
            key = " ".join(words[:at] + words[at + 1:])
            return f"Value: {self.values.get(key, '0')}."
        return "Value: 0."


def after(delay: float, action) -> threading.Thread:
    def run() -> None:
        time.sleep(delay)
        action()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
