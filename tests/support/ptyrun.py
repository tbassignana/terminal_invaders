"""The game on a real pty: the slave is its stdin, stdout and stderr, and a terminal stub thread reads the master.

The child starts a new session, so it has no controlling terminal and opens the slave by name (spec 4.1), or it
runs under minishell, which makes the slave its controlling terminal for the job-control tests. The stub feeds
MiniVT and answers queries through the fixture's Responder; it can read slowly, like a slow link. IsolatedPty runs
the stub in a process of its own instead (stubproc.py), so the test process cannot delay an ack. finish() checks
what a shell would find afterwards.
"""

import fcntl
import json
import os
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path

from terminal_invaders.app import text_hash
from terminal_invaders.term.keys import PRESS, RELEASE, Key
from terminal_invaders.term.simkeys import encode
from tests.support.faketerm import Responder
from tests.support.terminals import load
from tests.support.vt import MiniVT

ROOT = Path(__file__).resolve().parents[2]
SHELL = Path(__file__).with_name("minishell.py")
LOGS = ("Library", "Logs") if sys.platform == "darwin" else ("state",)  # storage.state_dir() under HOME
VT_SIZE = (300, 100)  # never smaller than the pty: frames drawn for an old size may arrive after a resize
LEAVE = b"\x1b[?1049l"
# The host's terminal must not leak into the child: the fixture's env replaces all of it.
HOST = {"TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "LC_TERMINAL", "VTE_VERSION", "KONSOLE_VERSION"}
HOST |= {"WT_SESSION", "TMUX", "STY", "NO_COLOR", "COLORFGBG", "LANG", "LC_ALL", "LC_CTYPE", "COLUMNS", "LINES"}
HOST |= {"INVADERS_FAULT", "TERMINFO", "TERMINFO_DIRS", "INVADERS_ATTRACT_IDLE"}
# Phase 5 spec 12.3: no pty test meets the title demo unless it asks for it (env={"INVADERS_ATTRACT_IDLE": "2"}).
DEFAULTS = {"INVADERS_ATTRACT_IDLE": "off"}
FRAME = re.compile(r"Frame \d+ (\d+)x(\d+) ticks (\d+) bytes (\d+) full (\d) text (\w+)")
LOOP = re.compile(r"Loop ([\d.]+)s: (\d+) ticks (\d+) frames (\d+) bytes, longest write ([\d.]+)ms")


class Stub(threading.Thread):
    """The terminal side of the pty: keeps every byte, applies them to MiniVT and answers queries.

    throttle (bytes, seconds) reads at most that much per interval, as a slow link would; None reads at once.
    stalled, while set, reads nothing at all, as a terminal that stopped reading."""

    def __init__(self, master: int, doc: dict | None):
        super().__init__(daemon=True)
        self.master, self.lock, self.stopping = master, threading.Lock(), threading.Event()
        self.output = bytearray()
        self.arrivals: list[tuple[float, int]] = []  # (when, total bytes read by then), one per read
        self.vt = MiniVT(*VT_SIZE, on_esu=self._esu)
        self.responder = Responder(doc)
        self.screens: list[list[str]] = []  # MiniVT's text at every ESU
        self.throttle, self.error, self.stalled = None, None, False

    def run(self) -> None:
        while not self.stopping.is_set():
            if self.stalled or not select.select([self.master], [], [], 0.01)[0]:
                time.sleep(0.01 if self.stalled else 0)
                continue
            throttle = self.throttle
            try:
                data = os.read(self.master, throttle[0] if throttle else 65536)
            except OSError:
                return  # the master was closed
            if not data:
                return  # macOS: the slave side is gone (Linux raises EIO instead)
            with self.lock:
                self.output += data
                self.arrivals.append((time.monotonic(), len(self.output)))
                if not self.error:
                    try:
                        self.vt.feed(data)
                        reply = self.responder.answer(self.vt.queries, self.vt.cprs)
                    except AssertionError as exc:  # VTError: a sequence MiniVT does not accept
                        self.error, reply = exc, b""
                    if reply:
                        os.write(self.master, reply)
            if throttle:
                time.sleep(throttle[1])

    def _esu(self, vt: MiniVT) -> None:
        self.screens.append(vt.lines())

    def stop(self) -> None:
        self.stopping.set()
        self.join(1)


class ProcessStub:
    """The Stub in a process of its own (stubproc.py): it reads the master, answers as the fixture would and keeps
    every byte, and when it read them, in files. A garbage collection or a busy thread in the test process then
    cannot hold up an ack, and with it the game's next frame.

    This side has the Stub's attributes, read from those files when asked (the bytes replayed into a MiniVT of its
    own): output, arrivals, vt, screens and error. It cannot throttle or stall."""

    def __init__(self, master: int, doc: dict | None):
        self.master, self.lock, self._sync = master, threading.Lock(), threading.RLock()
        self.directory = Path(tempfile.mkdtemp(prefix="stub-"))
        (self.directory / "doc.json").write_text(json.dumps(doc), encoding="utf-8")
        self._output, self._arrivals, self._read, self._partial = bytearray(), [], 0, b""
        self._screens: list[list[str]] = []
        self._vt = MiniVT(*VT_SIZE, on_esu=lambda vt: self._screens.append(vt.lines()))
        self._error = None
        self.process = None

    def start(self) -> None:
        command = [sys.executable, "-m", "tests.support.stubproc", str(self.master), *map(str, VT_SIZE)]
        self.process = subprocess.Popen(
            [*command, str(self.directory)], cwd=ROOT, stdin=subprocess.PIPE, pass_fds=(self.master,)
        )
        deadline = time.monotonic() + 10.0
        while not (self.directory / "arrivals").exists():  # it is ready once its files are open
            assert self.process.poll() is None and time.monotonic() < deadline, "the stub process did not start"
            time.sleep(0.005)

    def sync(self) -> None:
        """Take in what the stub process has read since the last call: its arrivals first, so every one of them
        has its bytes in the output that follows."""
        with self._sync:
            arrivals, output = self.directory / "arrivals", self.directory / "output"
            if not arrivals.exists():
                return
            with arrivals.open("rb") as file:
                file.seek(self._read)
                chunk = file.read()
            self._read += len(chunk)
            *lines, self._partial = (self._partial + chunk).split(b"\n")
            self._arrivals += [(float(when), int(total)) for when, total in map(bytes.split, lines)]
            with output.open("rb") as file:
                file.seek(len(self._output))
                data = file.read()
            self._output += data
            if data and not self._error:
                try:
                    self._vt.feed(data)
                except AssertionError as exc:
                    self._error = exc
            if (self.directory / "error").exists() and not self._error:
                self._error = AssertionError((self.directory / "error").read_text(encoding="utf-8"))

    @property
    def output(self) -> bytearray:
        self.sync()
        return self._output

    @property
    def arrivals(self) -> list[tuple[float, int]]:
        self.sync()
        return self._arrivals

    @property
    def vt(self) -> MiniVT:
        self.sync()
        return self._vt

    @property
    def screens(self) -> list[list[str]]:
        self.sync()
        return self._screens

    @property
    def error(self):
        self.sync()
        return self._error

    def settle(self, seconds: float = 1.0) -> None:
        """Until the stub process has read everything the game wrote (FIONREAD on the master is 0)."""
        deadline = time.monotonic() + seconds
        while unread(self.master) and time.monotonic() < deadline:
            time.sleep(0.005)
        time.sleep(0.02)  # it writes its files right after the read

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.sync()
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()


class Pty:
    """One game process on a pty, started with the fixture's environment and the given arguments."""

    stub_type = Stub

    def __init__(self, directory: Path, *arguments, fixture="xterm", size=(80, 30), env=None):
        self.directory = directory
        self.master, self.slave = os.openpty()
        self.set_size(*size)
        self.original = termios.tcgetattr(self.slave)
        self.doc = load(fixture) if fixture else None
        self.tape = directory / "recording.json"
        self.log = directory.joinpath(*LOGS, "terminal-invaders", "invaders.log")
        environment = {key: value for key, value in os.environ.items() if key not in HOST and key[:4] != "SSH_"}
        environment |= self.doc["env"] if self.doc else {}
        environment |= DEFAULTS | {"HOME": str(directory), "XDG_STATE_HOME": str(directory / "state")} | (env or {})
        self.environment = {key: value for key, value in environment.items() if value is not None}
        self.sound = "INVADERS_AUDIO" in (env or {})  # a test that names a backend gets sound; the rest run muted
        self.terminal = self.connect()
        self.stub = self.stub_type(self.terminal, self.doc)
        self.stub.start()
        self.arguments = list(arguments)
        self.events = None
        self.process = self.launch()
        self.started = time.monotonic()

    def game(self) -> list[str]:
        mute = [] if self.sound else ["--no-sound"]
        return [sys.executable, "-m", "terminal_invaders", *mute, "--profile", str(self.directory / "p.json")]

    def connect(self) -> int:
        """The fd the terminal stub and send() use as the terminal's side of the pty."""
        return self.master

    def launch(self) -> subprocess.Popen:
        return subprocess.Popen(
            self.game() + self.arguments,
            cwd=ROOT,
            env=self.environment,
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            start_new_session=True,
        )

    # -- driving ------------------------------------------------------------------------------------------------
    def set_size(self, cols: int, rows: int) -> None:
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, cols: int, rows: int) -> None:
        self.set_size(cols, rows)
        self.process.send_signal(signal.SIGWINCH)

    def send(self, keys: bytes | str) -> None:
        os.write(self.terminal, keys.encode() if isinstance(keys, str) else keys)

    def tap(self, *names: str, mods: int = 0) -> int:
        """A press and release of each key, as the terminal sends it now: kitty CSI u while the game has the
        protocol pushed, legacy bytes (and no release) otherwise. Returns how many key events that was."""
        with self.stub.lock:
            vt = self.stub.vt
            stack = vt.kitty["alt" if vt.alt_screen else "main"]
            kitty = bool(stack and stack[-1])
        sent = [encode(Key(name, mods=mods, kind=kind), kitty=kitty) for name in names for kind in (PRESS, RELEASE)]
        self.send(b"".join(sent))
        return sum(map(bool, sent))

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    def output(self) -> bytes:
        with self.stub.lock:
            return bytes(self.stub.output)

    def screen(self) -> str:
        with self.stub.lock:
            return "\n".join(self.stub.vt.lines())

    def wait_for(self, text: str | bytes, seconds: float = 3.0) -> bool:
        """Until text is on MiniVT's screen or anywhere in the output; a loaded machine can be slow."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.shows(text):
                return True
            time.sleep(0.02)
        return self.shows(text)

    def shows(self, text: str | bytes) -> bool:
        raw = text.encode() if isinstance(text, str) else text
        return raw in self.output() or raw.decode() in self.screen()

    def arrival(self, marker: bytes, start: int = 0) -> float | None:
        """When the first marker at or after offset start had been read in full; None if it never arrived."""
        with self.stub.lock:
            found = self.stub.output.find(marker, start)
            end = found + len(marker)
            return None if found < 0 else next(when for when, total in self.stub.arrivals if total >= end)

    def frames(self) -> list[tuple]:
        """The --debug log's frames: (cols, rows, ticks, bytes, full, text hash)."""
        found = FRAME.findall(self.log.read_text()) if self.log.exists() else []
        return [(int(c), int(r), int(t), int(b), f == "1", h) for c, r, t, b, f, h in found]

    def loops(self) -> list[tuple]:
        """The --debug log's once-a-second loop lines: (seconds, ticks, frames, bytes, longest write in ms)."""
        found = LOOP.findall(self.log.read_text()) if self.log.exists() else []
        return [(float(s), int(t), int(f), int(b), float(w)) for s, t, f, b, w in found]

    def screen_hashes(self) -> list[str]:
        """At every ESU, the text hash of the region the matching frame drew (the cells of its own size)."""
        sizes = [(cols, rows) for cols, rows, *_ in self.frames()]
        return [text_hash([line[:c] for line in screen[:r]]) for (c, r), screen in zip(sizes, self.stub.screens)]

    # -- ending -------------------------------------------------------------------------------------------------
    def exit(self, seconds: float = 5.0) -> int:
        try:
            code = self.process.wait(seconds)
        finally:
            time.sleep(0.05)  # the last bytes reach the stub
        return code

    def finish(self, *, traceback: bool = False, tape: bool = True) -> int:
        """Wait for the exit, then check the terminal is as the shell left it; returns the exit code."""
        code = self.exit()
        restored = termios.tcgetattr(self.slave)
        pendin = getattr(termios, "PENDIN", 0)  # macOS marks queued input for retyping: a transient kernel flag
        restored[3] &= ~pendin
        self.original[3] &= ~pendin
        assert restored == self.original
        output = self.output()
        assert (b"Traceback" in output) == traceback, output[-2000:]
        assert pending(self.slave) == 0, "input left for the shell"
        assert echoed(output) == [], "replies echoed into the shell"
        vt = self.stub.vt
        assert self.stub.error is None, self.stub.error
        assert not vt.alt_screen and vt.cursor_visible and vt.autowrap and vt.kitty_depth == 0
        assert (vt.titles, vt.title) == ([], ""), "the title stack is not as it was"
        assert not vt.focus and not vt.paste and not vt.sync
        if tape:
            assert self.tape.exists()
            verified = subprocess.run(
                [sys.executable, "-m", "terminal_invaders", "--verify-replay", str(self.tape)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert verified.returncode == 0, verified.stderr + verified.stdout
        return code

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        self.stub.stop()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


class IsolatedPty(Pty):
    """A Pty whose terminal stub runs in a process of its own (ProcessStub)."""

    stub_type = ProcessStub

    def exit(self, seconds: float = 5.0) -> int:
        code = super().exit(seconds)
        self.stub.settle()
        return code


class ShellPty(Pty):
    """The game as a job of minishell, on the pty as its controlling terminal: in the foreground, or started in the
    background (background=True, as with &). minishell stays after the game's exit until close(): on macOS a session
    leader's exit revokes its controlling terminal, and finish() could no longer read the slave."""

    def __init__(self, *arguments, background: bool = False, **kwargs):
        self.background = background
        self.code = None  # the game's exit code, once minishell reported it
        super().__init__(*arguments, **kwargs)

    def launch(self) -> subprocess.Popen:
        self.events, events = os.pipe()
        commands, self.commands = os.pipe()
        start = ["--background"] if self.background else []
        process = subprocess.Popen(
            [sys.executable, str(SHELL), str(events), str(commands), *start, "--", *self.game(), *self.arguments],
            cwd=ROOT,
            env=self.environment,
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            start_new_session=True,
            pass_fds=(events, commands),
        )
        os.close(events)
        os.close(commands)
        self.reader = os.fdopen(self.events)
        self.game_pid = self.event()["started"]
        return process

    def event(self, seconds: float = 5.0) -> dict:
        """The shell's next report: started, stopped (with the terminal as the shell found it) or exit."""
        assert select.select([self.reader], [], [], seconds)[0], "minishell reported nothing"
        report = json.loads(self.reader.readline())
        self.code = report.get("exit", self.code)
        return report

    def exit(self, seconds: float = 5.0) -> int:
        """The game's exit code from minishell's report, which the test reads first; minishell is still there."""
        assert self.code is not None, "minishell has not reported the game's exit"
        time.sleep(0.05)  # the last bytes reach the stub
        return self.code

    def fg(self) -> None:
        os.write(self.commands, b"fg\n")

    def bg(self) -> None:
        os.write(self.commands, b"bg\n")

    def kill_job(self) -> None:
        """bash's kill %1 on the stopped game: SIGTERM, then SIGCONT, in the background."""
        os.write(self.commands, b"kill\n")

    def close(self) -> None:
        os.close(self.commands)  # minishell leaves once the game has exited and the commands end
        if self.code is not None:
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                pass  # killed below
        super().close()
        self.reader.close()


def pending(fd: int) -> int:
    """Bytes waiting to be read on a tty (FIONREAD), counted with ICANON off: in canonical mode Linux counts only
    complete lines, and replies echoed after the exit sit in an unfinished one."""
    attrs = termios.tcgetattr(fd)
    counting = [*attrs[:3], attrs[3] & ~termios.ICANON, *attrs[4:6], list(attrs[6])]
    termios.tcsetattr(fd, termios.TCSANOW, counting)
    try:
        return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, attrs)


def unread(fd: int) -> int:
    """Bytes waiting to be read on a pty master (FIONREAD)."""
    return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]


def echoed(output: bytes) -> list[bytes]:
    """Escape sequences the tty echoed (as ^[...) after the game left the alt screen: replies a shell would see."""
    return re.findall(rb"\^\[\[[0-9;?]*[ -/]*[@-~]", output[output.rfind(LEAVE) :])
