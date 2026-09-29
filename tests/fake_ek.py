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
        if not self.echo:
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
            reply = self.reply_to(line)
            self._send(self._echo(line) + "\r\n" + reply + "\r\n" + PROMPT)

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
        if line == "npmx charger termination_voltage normal get":
            return "Value: 4200 mv"
        if "set" in words:
            return f"Value: {words[-1]}."
        if line.startswith("npm_adc sample"):
            return "Value: sample interval=1000, report interval=2000"
        if line == "bad command":
            return "Error: unknown"
        return "Value: 0."


def after(delay: float, action) -> threading.Thread:
    def run() -> None:
        time.sleep(delay)
        action()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
