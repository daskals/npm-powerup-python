"""Serial transport for the Zephyr shell that the nPM EK firmware exposes.

One background thread reads the port. Log lines are handed to listeners;
everything else is treated as the reply to the command in progress.
"""

from __future__ import annotations

import codecs
import logging
import re
import threading
import time
from typing import Callable, Optional, Union

import serial

from .logparse import LogEvent, parse_log_line

log = logging.getLogger(__name__)

DEFAULT_PROMPT = "shell:~$ "
DEFAULT_BAUDRATE = 115200
DEFAULT_TIMEOUT = 5.0

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\^_]")
_ANSI_PARTIAL = re.compile(r"\x1b(\[[0-9;?]*[ -/]*)?$")
_ERROR = re.compile(r"Error: ")

# How long to wait for the echo of a command once the prompt is back,
# before accepting the reply without one.
_NO_ECHO_GRACE = 1.0

PortSource = Union[str, Callable[[], Optional[str]]]
LogListener = Callable[[LogEvent], None]
ConnectionListener = Callable[[bool], None]


class ShellError(Exception):
    """Base class for shell transport errors."""


class ShellCommandError(ShellError):
    def __init__(self, command: str, response: str):
        super().__init__(f"{command!r} failed: {response.strip()}")
        self.command = command
        self.response = response


class ShellTimeout(ShellError):
    def __init__(self, command: str, timeout: float, received: str):
        super().__init__(
            f"no reply to {command!r} within {timeout:g} s"
            + (f" (received: {received.strip()!r})" if received.strip() else "")
        )
        self.command = command
        self.received = received


class ShellDisconnected(ShellError):
    pass


class _Pending:
    def __init__(self, command: str, expect_echo: bool = True):
        self.command = command
        self.expect_echo = expect_echo
        self.lines: list[str] = []
        # The shell wraps the echo of long commands, so match it ignoring
        # whitespace. It must stand alone: a reply such as "hw_version=..."
        # starts with the command and is not its echo.
        self.echo = re.compile(
            r"(?<!\S)"
            + r"\s*".join(re.escape(c) for c in command if not c.isspace())
            + r"(?=\s|$)"
        )

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class ShellSession:
    """A command and response session with the EK shell.

    ``port`` is a port name, or a function returning one, which is called
    again whenever the port has to be reopened.

    Listeners run on the reader thread and must not call :meth:`command`.
    """

    def __init__(
        self,
        port: PortSource,
        baudrate: int = DEFAULT_BAUDRATE,
        prompt: str = DEFAULT_PROMPT,
        timeout: float = DEFAULT_TIMEOUT,
        line_ending: str = "\r\n",
        auto_reconnect: bool = True,
        serial_factory: Optional[Callable[[str, int], object]] = None,
    ):
        self._port = port
        self._baudrate = baudrate
        self._prompt = prompt
        self._timeout = timeout
        self._line_ending = line_ending
        self._auto_reconnect = auto_reconnect
        self._serial_factory = serial_factory or self._open_serial

        self._ser = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._command_lock = threading.Lock()
        self._write_lock = threading.Lock()

        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._held = ""  # incomplete escape sequence
        self._tail = ""  # text after the last newline
        self._pending: Optional[_Pending] = None
        self._at_prompt = False
        self._prompt_seen = 0.0
        self._connected = False

        self._log_listeners: list[LogListener] = []
        self._line_listeners: list[Callable[[str], None]] = []
        self._connection_listeners: list[ConnectionListener] = []
        self._command_listeners: list[Callable[[str, str, bool], None]] = []

    # -- lifecycle ---------------------------------------------------------

    @staticmethod
    def _open_serial(port: str, baudrate: int):
        return serial.Serial(port, baudrate, timeout=0.1, write_timeout=2)

    def _resolve_port(self) -> Optional[str]:
        return self._port() if callable(self._port) else self._port

    def open(self) -> "ShellSession":
        if self._thread is not None:
            return self
        port = self._resolve_port()
        if port is None:
            raise ShellDisconnected("no serial port found")
        self._ser = self._serial_factory(port, self._baudrate)
        self._set_connected(True)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="npm-shell-reader", daemon=True
        )
        self._thread.start()
        self.sync()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._close_serial()
        self._set_connected(False)

    def __enter__(self) -> "ShellSession":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._connected

    def wait_connected(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._connected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.2))
        return True

    # -- listeners ---------------------------------------------------------

    def on_log(self, listener: LogListener) -> Callable[[], None]:
        self._log_listeners.append(listener)
        return lambda: self._log_listeners.remove(listener)

    def on_line(self, listener: Callable[[str], None]) -> Callable[[], None]:
        """Listen to every line received, log lines included."""
        self._line_listeners.append(listener)
        return lambda: self._line_listeners.remove(listener)

    def on_connection_change(
        self, listener: ConnectionListener
    ) -> Callable[[], None]:
        self._connection_listeners.append(listener)
        return lambda: self._connection_listeners.remove(listener)

    def on_command(
        self, listener: Callable[[str, str, bool], None]
    ) -> Callable[[], None]:
        """Listen to every command sent: ``(command, reply, failed)``."""
        self._command_listeners.append(listener)
        return lambda: self._command_listeners.remove(listener)

    # -- commands ----------------------------------------------------------

    def sync(self, settle: float = 0.3) -> None:
        """Send an empty line and discard whatever is buffered."""
        with self._command_lock:
            self._write(self._line_ending)
            time.sleep(settle)
            with self._cond:
                self._pending = None

    def command(
        self,
        command: str,
        timeout: Optional[float] = None,
        expect_echo: bool = True,
    ) -> str:
        """Run ``command`` and return its reply.

        Raises :class:`ShellCommandError` if the firmware reports an error.
        Pass ``expect_echo=False`` where the firmware is known not to echo.
        """
        timeout = self._timeout if timeout is None else timeout
        with self._command_lock:
            pending = _Pending(command, expect_echo)
            with self._cond:
                if not self._connected:
                    raise ShellDisconnected(f"not connected, cannot send {command!r}")
                self._pending = pending
                self._at_prompt = False
            try:
                self._write(command + self._line_ending)
                response = self._wait_for_reply(pending, timeout)
            finally:
                with self._cond:
                    self._pending = None

        failed = bool(_ERROR.search(response))
        for listener in list(self._command_listeners):
            self._safely(listener, command, response, failed)
        if failed:
            raise ShellCommandError(command, response)
        return response

    def _wait_for_reply(self, pending: _Pending, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                response = self._reply_if_complete(pending)
                if response is not None:
                    return response
                if not self._connected:
                    raise ShellDisconnected(
                        f"disconnected while waiting for {pending.command!r}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ShellTimeout(pending.command, timeout, pending.text)
                self._cond.wait(min(remaining, 0.1))

    def _reply_if_complete(self, pending: _Pending) -> Optional[str]:
        if not self._at_prompt:
            return None
        text = pending.text
        echo = pending.echo.search(text)
        if echo:
            return text[echo.end():].strip()
        if not pending.expect_echo:
            return text.strip()
        if pending.lines and time.monotonic() - self._prompt_seen > _NO_ECHO_GRACE:
            return text.strip()
        return None

    def _write(self, text: str) -> None:
        ser = self._ser
        if ser is None:
            raise ShellDisconnected("serial port is closed")
        with self._write_lock:
            try:
                ser.write(text.encode("utf-8"))
            except (serial.SerialException, OSError) as error:
                raise ShellDisconnected(str(error)) from error

    # -- reader thread -----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._ser is None:
                if not self._reopen():
                    self._stop.wait(1.0)
                continue
            try:
                waiting = getattr(self._ser, "in_waiting", 0)
                data = self._ser.read(waiting or 1)
            except (serial.SerialException, OSError) as error:
                log.warning("serial port lost: %s", error)
                self._close_serial()
                self._set_connected(False)
                if not self._auto_reconnect:
                    return
                continue
            if data:
                self._feed(data)

    def _reopen(self) -> bool:
        try:
            port = self._resolve_port()
            if port is None:
                return False
            self._ser = self._serial_factory(port, self._baudrate)
        except (serial.SerialException, OSError):
            self._ser = None
            return False
        self._decoder.reset()
        self._held = self._tail = ""
        log.info("serial port reopened on %s", port)
        self._set_connected(True)
        try:
            self._write(self._line_ending)
        except ShellDisconnected:
            pass
        return True

    def _close_serial(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # the port may already be gone
                pass

    def _set_connected(self, connected: bool) -> None:
        with self._cond:
            changed = connected != self._connected
            self._connected = connected
            self._cond.notify_all()
        if changed:
            for listener in list(self._connection_listeners):
                self._safely(listener, connected)

    def _feed(self, data: bytes) -> None:
        text = self._held + self._decoder.decode(data)
        partial = _ANSI_PARTIAL.search(text)
        if partial:
            self._held, text = text[partial.start():], text[: partial.start()]
        else:
            self._held = ""
        text = _ANSI.sub("", text).replace("\r", "").replace("\x00", "")

        *lines, self._tail = (self._tail + text).split("\n")
        for line in lines:
            self._handle_line(line)

        at_prompt = self._tail.strip() == self._prompt.strip()
        with self._cond:
            if at_prompt and not self._at_prompt:
                self._prompt_seen = time.monotonic()
            self._at_prompt = at_prompt
            self._cond.notify_all()

    def _strip_prompt(self, line: str) -> str:
        bare = self._prompt.strip()
        while True:
            stripped = line.lstrip()
            if stripped.startswith(self._prompt):
                line = stripped[len(self._prompt):]
            elif stripped.startswith(bare):
                line = stripped[len(bare):]
            else:
                return line

    def _handle_line(self, line: str) -> None:
        line = self._strip_prompt(line)
        for listener in list(self._line_listeners):
            self._safely(listener, line)

        event = parse_log_line(line)
        if event is not None:
            for listener in list(self._log_listeners):
                self._safely(listener, event)
            return

        with self._cond:
            if self._pending is not None:
                self._pending.lines.append(line)

    @staticmethod
    def _safely(listener, *args) -> None:
        try:
            listener(*args)
        except Exception:
            log.exception("listener failed")
