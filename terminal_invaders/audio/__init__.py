"""Sound for the game: a helper process mixes and plays it, and the game only sends that helper one-byte messages.

The game loads no audio library and runs no audio thread. Audio() spawns the helper in its own session (so a ^Z of the
game never stops it and nothing it or its players print can reach the terminal), update() turns the simulation's
events into cue bytes, and close() leaves no process behind. Audio never feeds anything back into the simulation: it
reads the events after Game.step() and nothing reads its state.
"""

import logging
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from .protocol import CUE_BASE, CUES, CUT, QUIT, RANK

# The helper runs with -I -S (no PYTHON* variables, no site-packages), so it finds this package by the root it is given.
BOOT = "import sys; sys.path.append(sys.argv[1]); from terminal_invaders.audio.helper import main; main()"
ROOT = Path(__file__).resolve().parents[2]
GAPS = {name: gap / 1000 for name, _f0, _f1, _ms, gap in CUES}  # s: a cue sooner than this after the last is dropped
STATUS_EVERY = 0.25  # s between reads of the helper's status lines
CLOSE_WAIT = 0.3  # s close() waits for the helper to exit by itself before killing its group


class Audio:
    """The game's side of the sound: call update() once per tick with the events, toggle() for M, close() at exit."""

    def __init__(self, enabled: bool = True, *, clock=time.monotonic, popen=subprocess.Popen) -> None:
        self.clock, self.popen = clock, popen
        self.available = os.environ.get("INVADERS_AUDIO", "").strip().lower() != "none"
        self.enabled = bool(enabled and self.available)  # the footer's SOUND ON/OFF
        self.backend: str | None = None  # from the helper's last "backend" line
        self.on_status = None  # optional callable(line): every status line, for the check and the latency harness
        self.proc: subprocess.Popen | None = None
        self._closed = False
        self._last: dict[str, float] = {}  # cue name -> when it was last sent
        self._read_at = 0.0
        self._partial = b""
        if self.enabled:
            self._spawn()

    @property
    def pid(self) -> int | None:
        """The helper's pid, which is also its process group and session id; None when no helper runs."""
        return self.proc.pid if self.proc is not None else None

    def update(self, events=()) -> None:
        """Send the cues among events that are past their repeat gap: at most one write, which drops on a full pipe (a
        late cue is worse than none). Every STATUS_EVERY s it also reads the helper's status lines."""
        if self.proc is None:
            return
        try:
            now = self.clock()
            data = bytearray()
            if self.enabled:
                for name in dict.fromkeys(events):  # an event twice in one tick is one cue
                    rank = RANK.get(name)
                    if rank is not None and now - self._last.get(name, -1e9) >= GAPS[name]:
                        self._last[name] = now
                        data.append(CUE_BASE + rank)
            if data:
                self._write(bytes(data))
            if self.proc is not None and now - self._read_at >= STATUS_EVERY:
                self._read_at = now
                self.poll_status()
        except Exception:
            logging.debug("audio: update failed", exc_info=True)
            self._dead()

    def toggle(self) -> bool:
        """M: off silences everything at once; on starts the helper if --no-sound kept it from starting. Returns the
        new state, which stays False once sound is unavailable."""
        try:
            if self._closed or not self.available:
                self.enabled = False
                return False
            self.enabled = not self.enabled
            if not self.enabled:
                if self.proc is not None:
                    self._write(CUT)
            elif self.proc is None:
                self._spawn()
        except Exception:
            logging.debug("audio: toggle failed", exc_info=True)
            self._dead()
        return self.enabled

    def poll_status(self) -> None:
        """Read the helper's status lines now, logging each one; EOF means the helper is gone."""
        proc = self.proc
        if proc is None:
            return
        try:
            gone = self._drain(proc.stdout.fileno())
        except Exception:
            logging.debug("audio: status read failed", exc_info=True)
            gone = True
        if gone and self.proc is proc:
            self._dead()

    def close(self) -> None:
        """Ask the helper to quit, give it CLOSE_WAIT to do so, then kill its whole group (players included) and reap
        it; idempotent, never raises, at most about 0.35 s."""
        if self._closed:
            return
        self._closed = True
        self.enabled = False
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            self._quit(proc)
        except Exception:
            logging.debug("audio: close failed", exc_info=True)
        finally:
            self._sweep(proc)

    def _spawn(self) -> None:
        try:
            proc = self.popen(
                [sys.executable, "-I", "-S", "-c", BOOT, str(ROOT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                cwd="/",
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            logging.debug("audio: cannot start the helper", exc_info=True)
            self.available = self.enabled = False
            return
        self.proc = proc
        os.set_blocking(proc.stdin.fileno(), False)
        os.set_blocking(proc.stdout.fileno(), False)
        self._read_at = self.clock()

    def _write(self, data: bytes) -> bool:
        """One non-blocking write of a few bytes (under PIPE_BUF, so all or nothing): True if it went."""
        try:
            os.write(self.proc.stdin.fileno(), data)
            return True
        except BlockingIOError:
            return False  # the helper is behind (or stopped by someone): drop it
        except OSError:  # EPIPE: the helper is gone
            logging.debug("audio: the helper stopped reading", exc_info=True)
            self._dead()
            return False

    def _drain(self, fd: int) -> bool:
        """Read status until the pipe is empty, handling each whole line; True at EOF."""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                return False
            if not chunk:
                return True
            *lines, self._partial = (self._partial + chunk).split(b"\n")
            for raw in lines:
                self._status(raw.decode("ascii", "replace"))

    def _status(self, line: str) -> None:
        logging.debug("audio: %s", line)
        word, _, rest = line.partition(" ")
        if word == "backend":
            self.backend = rest
        elif word == "silent:":
            self.available = self.enabled = False
        if self.on_status is not None:
            self.on_status(line)

    def _quit(self, proc) -> None:
        """Send q, close our end, and wait for EOF on the status pipe: only the helper holds its write end, so EOF
        means the helper exited (unreaped, so its group id stays reserved for the sweep)."""
        try:
            os.write(proc.stdin.fileno(), QUIT)
        except OSError:
            pass
        proc.stdin.close()
        fd = proc.stdout.fileno()
        deadline = time.monotonic() + CLOSE_WAIT
        while (left := deadline - time.monotonic()) > 0:
            if select.select([fd], [], [], left)[0] and self._drain(fd):
                return

    def _sweep(self, proc) -> None:
        """Kill the helper's group (it and every player it started), reap the helper and log what it last said."""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # gone already; macOS says EPERM for a group whose members are all zombies
        except OSError:
            logging.debug("audio: killpg failed", exc_info=True)
        try:
            proc.wait(1.0)
        except Exception:
            logging.debug("audio: the helper was not reaped", exc_info=True)
        for pipe in (proc.stdout, proc.stdin):
            try:
                if pipe is proc.stdout and not pipe.closed:
                    self._drain(pipe.fileno())
                pipe.close()
            except Exception:
                pass

    def _dead(self) -> None:
        """The helper crashed, exited or stopped reading: sweep its group at once, without waiting for it to quit,
        and play on without sound."""
        self.available = self.enabled = False
        proc, self.proc = self.proc, None
        if proc is not None:
            self._sweep(proc)
