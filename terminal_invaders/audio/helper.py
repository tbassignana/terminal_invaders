"""The audio helper process: one-byte messages on fd 0 in, mixed sound out through the first backend that works.

The game starts it as `python -I -S -c BOOT ROOT` in a session of its own (spec §7). Status lines go to a private
dup of its original stdout; fds 1 and 2 stay on /dev/null, so nothing it, a player or a native library prints can
reach the terminal. Only the stdlib is imported here; each backend's module loads when it is tried.
"""

from __future__ import annotations

import math
import os
import queue
import signal
import sys
import threading
import time
from array import array

from . import protocol
from .mixer import Mixer, synth
from .protocol import BLOCK_FRAMES, BLOCK_SAMPLES, CUE_BASE, CUES, IDLE_AFTER, RATE, STALE, STATS_EVERY
from .sink import Sink, SinkError

LINUX = ("pulse-simple", "pw-cat", "pacat", "aplay")
MACOS = ("coreaudio", "afplay")
FILE = "file:"  # INVADERS_AUDIO=file:PATH
TONE_PEAK = 8000
FILE_LEAD = 3  # blocks (30 ms) the file sink stays written ahead of the clock
TICK = BLOCK_FRAMES / RATE  # s per block
STATUS_MAX = 400  # characters: a status line stays one atomic pipe write
CUT_ALL, QUIT_NOW = protocol.CUT[0], protocol.QUIT[0]


def chain(platform: str, setting: str | None) -> list[str]:
    """The backends to try, in order: INVADERS_AUDIO's when set (none: no backend; file:PATH: the file sink alone;
    otherwise a comma list of names), else the platform's own chain."""
    if not setting:
        return list(MACOS if platform == "darwin" else LINUX)
    if setting == "none":
        return []
    if setting.startswith(FILE):
        return [setting]
    return [name.strip() for name in setting.split(",") if name.strip()]


def make(name: str, engine: Engine) -> Sink:
    """The backend called name, its module imported only now, so neither platform loads the other's code."""
    if name.startswith(FILE):
        return FileSink(name[len(FILE) :], clock=engine.clock, say=engine.say)
    if name in LINUX:
        from . import linux

        if name == "pulse-simple":
            return linux.PulseSink(clock=engine.clock)
        return linux.PipeSink(name, clock=engine.clock)
    if name == "coreaudio":
        from . import macos

        return macos.AudioQueueSink(post=engine.post, clock=engine.clock)
    if name == "afplay":
        from . import macos

        return macos.AfplaySink(cues=engine.mixer.cues, clock=engine.clock)
    raise SinkError("unknown backend")


def status_writer(fd: int):
    """say(line): one ASCII line per write on the non-blocking fd; a line that finds the pipe full is dropped (A27)."""

    def say(line: str) -> None:
        data = (line[:STATUS_MAX].replace("\n", " ") + "\n").encode("ascii", "replace")
        try:
            os.write(fd, data)
        except OSError:  # EAGAIN: the game has not read for a while; EPIPE: the game is gone
            pass

    return say


def _why(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


class Engine:
    """Applies the game's messages to the mixer and feeds the sink, moving down the chain when a sink fails (§1, §3).

    names come from chain(); make(name, engine) builds each sink. The voices live in the mixer, so they carry over a
    failover. Feeding stays on while the mixer is audible and IDLE_AFTER s after."""

    def __init__(self, mixer: Mixer, names, *, say, post=None, make=make, clock=time.monotonic,
                 cpu=time.process_time) -> None:  # fmt: skip
        self.mixer, self.names, self.say, self.post = mixer, list(names), say, post
        self.make, self.clock, self.cpu = make, clock, cpu
        self.sink: Sink | None = None
        self.quit = False
        self.heard = -math.inf  # when the mixer was last audible
        self.window: list | None = None  # stats: [start, cpu then, lead samples, underruns then, callbacks then]
        self._open_next("no backend")

    def _open_next(self, why: str) -> None:
        """Open the next backend in the chain; with none left say silent and quit."""
        while self.names:
            name = self.names.pop(0)
            label = name.partition(":")[0]  # file:PATH reports as file
            sink = None
            try:
                sink = self.make(name, self)
                sink.open()
            except (SinkError, OSError, ImportError) as exc:
                self.say(f"fallback {label}: {_why(exc)}")
                if sink is not None:
                    sink.close()
                why = "every backend failed"
                continue
            self.sink = sink
            self.say(f"backend {sink.name}")
            return
        self.sink, self.quit = None, True
        self.say(f"silent: {why}")

    def _guard(self, call, *args) -> bool:
        """call(*args); on SinkError close the sink, say fallback, and open the next one."""
        try:
            call(*args)
            return True
        except SinkError as exc:
            self.say(f"fallback {self.sink.name}: {_why(exc)}")
            self.sink.close()
            self.sink, self.window = None, None
            self._open_next("every backend failed")
            return False

    def _feeding(self, now: float) -> bool:
        if self.mixer.audible:
            self.heard = now
        return self.sink.mixes and now - self.heard < IDLE_AFTER

    def handle(self, t: float, data) -> None:
        """Apply one queue item: bytes the game sent (read at t), None (EOF or a signal: quit), or a sink's event."""
        if data is None:
            self.quit = True
        elif isinstance(data, tuple):
            if self.sink is not None:
                self._guard(self.sink.on_event, data)
        else:
            stale = self.clock() - t > STALE  # a late cue is worse than none; "!" and "q" always apply
            for byte in data:
                sink = self.sink
                if byte == QUIT_NOW:
                    self.quit = True
                    return
                if byte == CUT_ALL:
                    self.mixer.cut()
                    if sink is not None and not sink.mixes:
                        self._guard(sink.cut)
                elif CUE_BASE <= byte < CUE_BASE + len(CUES) and sink is not None:
                    if stale:
                        self.say(f"stale {CUES[byte - CUE_BASE][0]}")
                    elif sink.mixes:
                        self.mixer.cue(byte - CUE_BASE)
                    else:
                        self._guard(sink.cue, byte - CUE_BASE)

    def wait(self) -> float:
        """Seconds the loop may block on the queue: what the sink asks for, feeding or idle."""
        if self.sink is None:
            return 0.0
        return max(0.0, self.sink.wait(self._feeding(self.clock())))

    def feed(self) -> None:
        """Let the sink write what its pacing allows, then report the stats."""
        if self.sink is None:
            return
        feeding = self._feeding(self.clock())
        before = self.sink.lead_ms() if self.sink.low_water else None
        if self._guard(self.sink.feed, self.mixer, feeding) and feeding:
            self._stats(before)
        elif not feeding:
            self.window = None

    def _stats(self, before: float | None) -> None:
        """Sample the lead; every STATS_EVERY s of feeding say the window's CPU, lead spread, underruns, callbacks.
        A low-water sink's sample (before) is skipped on a window's first turn, which follows an idle or a failover."""
        sink, now = self.sink, self.clock()
        fresh = self.window is None
        if fresh:
            self.window = [now, self.cpu(), [], sink.underruns, sink.callbacks]
        start, cpu, leads, underruns, callbacks = self.window
        lead = before if sink.low_water else sink.lead_ms()
        if lead is not None and not (fresh and sink.low_water):
            leads.append(lead)
        if now - start < STATS_EVERY:
            return
        spread = "-"
        if leads:
            leads.sort()
            spread = "/".join(f"{leads[i]:.1f}" for i in (0, (len(leads) - 1) * 5 // 100, len(leads) // 2, -1))
        busy = 1000 * (self.cpu() - cpu) / (now - start)
        ran_dry = "-" if sink.underruns is None else sink.underruns - underruns
        self.say(f"stats cpu={busy:.1f} lead={spread} underruns={ran_dry} callbacks={sink.callbacks - callbacks}")
        self.window = [now, self.cpu(), [], sink.underruns, sink.callbacks]

    def close(self) -> None:
        """Close the sink; bounded, never raises."""
        if self.sink is not None:
            self.sink.close()
            self.sink = None


def run(engine: Engine, messages) -> None:
    """The main loop (§1, §14 A10): block for a message or the sink's next turn, apply everything queued, feed."""
    while not engine.quit:
        try:
            items = [messages.get(timeout=engine.wait())]
        except queue.Empty:
            items = []
        try:
            while True:
                items.append(messages.get_nowait())
        except queue.Empty:
            pass
        for t, data in items:
            engine.handle(t, data)
        if not engine.quit:
            engine.feed()


class FileSink(Sink):
    """INVADERS_AUDIO=file:PATH, for tests: raw s16le stereo blocks appended to PATH in real time, FILE_LEAD ahead of
    the clock on a 10 ms schedule, saying `voice NAME FRAME` at the file frame where each voice's first sample lands."""

    name = "file"

    def __init__(self, path: str, *, clock=time.monotonic, say=None) -> None:
        self.path, self.clock, self.say = path, clock, say or (lambda line: None)
        self.fd: int | None = None
        self.frames = 0  # frames in the file
        self.start, self.sent = 0.0, 0  # block k since start plays at start + k * TICK (integers: no float drift)

    def open(self) -> None:
        try:
            self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        except OSError as exc:
            raise SinkError(exc.strerror or _why(exc)) from None
        self.frames = os.fstat(self.fd).st_size // 4
        self.start, self.sent = self.clock(), 0

    def feed(self, mixer, feeding: bool) -> None:
        if not feeding:
            return
        now = self.clock()
        if self.start + self.sent * TICK < now:  # after an idle spell the file goes on from now; it never fills the gap
            self.start, self.sent = now, 0
        while self.sent < math.ceil(round((now - self.start) / TICK, 6)) + FILE_LEAD:
            block = mixer.block()
            for rank in mixer.started:
                self.say(f"voice {CUES[rank][0]} {self.frames}")
            try:
                os.write(self.fd, block.tobytes())
            except OSError as exc:
                raise SinkError(exc.strerror or _why(exc)) from None
            self.frames += BLOCK_FRAMES
            self.sent += 1

    def wait(self, feeding: bool) -> float:
        return max(0.0, self.start + (self.sent - FILE_LEAD) * TICK - self.clock()) if feeding else 0.5

    def lead_ms(self) -> float:
        return 0.0

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


class Tone:
    """INVADERS_AUDIO_TONE=HZ, for the dropout check: one exact period of a sine at peak 8000 as an integer table, read
    in blocks round and round under the cues."""

    def __init__(self, hz: int) -> None:
        if not 0 < hz < RATE // 2:
            raise ValueError(f"bad tone {hz}")
        frames = RATE // math.gcd(RATE, hz)  # the sine repeats exactly after this many frames
        mono = array("h", (round(TONE_PEAK * math.sin(2 * math.pi * hz * i / RATE)) for i in range(frames)))
        period = array("h", bytes(4 * frames))
        period[0::2] = period[1::2] = mono
        self.period = len(period)
        self.table = period * (BLOCK_SAMPLES // self.period + 2)  # any block-long slice from inside one period fits
        self.pos = 0

    def read(self) -> array:
        block = self.table[self.pos : self.pos + BLOCK_SAMPLES]
        self.pos = (self.pos + BLOCK_SAMPLES) % self.period
        return block


def _read(messages) -> None:  # pragma: no cover - runs in the helper process (tests/test_audio_process.py)
    """The reader thread: queue each read from fd 0 with its time. On EOF queue a quit, then be the watchdog: if the
    helper is still alive 1 s later, SIGKILL its process group, which holds it and its players."""
    while True:
        try:
            data = os.read(0, 512)
        except OSError:
            data = b""
        messages.put((time.monotonic(), data or None))
        if not data:
            break
    time.sleep(1.0)
    if os.getpgrp() == os.getpid():  # a group of its own (start_new_session), never a caller's
        os.killpg(0, signal.SIGKILL)


def main() -> None:  # pragma: no cover - runs in the helper process (tests/test_audio_process.py)
    """The helper. Never returns: it ends in os._exit(0) after "bye"."""
    status = os.dup(1)  # non-inheritable, so no player holds the game's status pipe open
    os.set_blocking(status, False)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.close(null)
    say = status_writer(status)
    messages = queue.SimpleQueue()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: messages.put((time.monotonic(), None)))
    threading.Thread(target=_read, args=(messages,), daemon=True).start()
    engine = None
    try:
        hz = os.environ.get("INVADERS_AUDIO_TONE")  # test hook: a steady sine under the cues (the dropout check)
        mixer = Mixer([synth(f0, f1, ms) for _name, f0, f1, ms, _gap in CUES], Tone(int(hz)) if hz else None)
        engine = Engine(mixer, chain(sys.platform, os.environ.get("INVADERS_AUDIO")), say=say,
                        post=lambda event: messages.put((time.monotonic(), event)))  # fmt: skip
        while engine.sink is not None and os.environ.get("INVADERS_AUDIO_HANG") == "1":  # test hook: a hung main loop,
            time.sleep(60)  # which only the watchdog's killpg after EOF ends (§9, G2.7)
        run(engine, messages)
    except BaseException as exc:
        say(f"error {type(exc).__name__}: {exc}")
    try:
        if engine is not None:
            engine.close()
    except BaseException:
        pass
    say("bye")
    os._exit(0)
