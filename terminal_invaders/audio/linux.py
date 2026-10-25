"""The Linux backends: libpulse-simple through ctypes, and the pw-cat, pacat and aplay players on a pipe.

Only the helper imports this module. Native libraries load in open(), never at import time, so the module also
imports on macOS and under python -I -S. ctypes.util is never imported (it costs 23 ms). Every block handed to a
library is bytes or a ctypes array (spec 14 A3).
"""

import fcntl
import os
import shutil
import subprocess
import termios
import time
from array import array
from ctypes import CDLL, POINTER, Structure, byref, c_char_p, c_int, c_size_t, c_uint8, c_uint32, c_uint64, c_void_p

from .protocol import BLOCK_BYTES, CHANNELS, RATE
from .sink import Sink, SinkError

BYTES_PER_MS = RATE * CHANNELS * 2 // 1000
NONE = 0xFFFFFFFF  # pa_buffer_attr: the server's default
TLENGTH = 55 * BYTES_PER_MS  # 10560 B: a blocking write returns once less than this is queued (A2)
HEALTH_BYTES = 30 * BYTES_PER_MS  # 5760 B of silence the player must start reading at open
TICK, STALL, POLL = 0.010, 0.5, 0.002  # s: the pipe clock, no level move for this long, health check poll

PULSE = {
    "pa_simple_new": (c_void_p, [c_char_p, c_char_p, c_int, c_char_p, c_char_p, c_void_p, c_void_p, c_void_p,
                                 POINTER(c_int)]),
    "pa_simple_write": (c_int, [c_void_p, c_void_p, c_size_t, POINTER(c_int)]),
    "pa_simple_get_latency": (c_uint64, [c_void_p, POINTER(c_int)]),
    "pa_simple_free": (None, [c_void_p]),
}  # fmt: skip

# name: (argv, bytes kept in the pipe). Each runs without a shell; argv[0] is replaced by its shutil.which path.
PIPES = {
    "pw-cat": (("pw-cat", "--playback", "--format=s16", "--rate=48000", "--channels=2", "--latency=10ms",
                "--media-role=Game", "-P", '{ application.name = "Terminal Invaders" }', "-"), 20 * BYTES_PER_MS),
    "pacat": (("pacat", "--playback", "--raw", "--format=s16le", "--rate=48000", "--channels=2", "--latency-msec=20",
               "--client-name=Terminal Invaders", "--stream-name=Game"), 12 * BYTES_PER_MS),
    "aplay": (("aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", "48000", "-c", "2", "--buffer-time=40000",
               "--period-time=10000", "-"), 10 * BYTES_PER_MS),
}  # fmt: skip


class Spec(Structure):
    _fields_ = [("format", c_int), ("rate", c_uint32), ("channels", c_uint8)]


class Attr(Structure):
    _fields_ = [(name, c_uint32) for name in ("maxlength", "tlength", "prebuf", "minreq", "fragsize")]


def bind(lib, table: dict):
    """Set restype and argtypes on each function of `table`; returns lib."""
    for name, (restype, argtypes) in table.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = restype, argtypes
    return lib


class PulseSink(Sink):
    """libpulse-simple: one blocking write per block, so the server's tlength paces the helper (spec 3, 14 A2)."""

    name = "pulse-simple"

    def __init__(self, *, lib=None, clock=time.monotonic) -> None:
        self.lib, self.clock, self.handle, self.err = lib, clock, None, c_int()
        self.dry = False  # the last write found the server's queue empty

    def open(self) -> None:
        try:
            self.lib = bind(CDLL("libpulse-simple.so.0") if self.lib is None else self.lib, PULSE)
        except OSError as exc:
            raise SinkError("no libpulse-simple") from exc
        spec, attr = Spec(3, RATE, CHANNELS), Attr(NONE, TLENGTH, NONE, BLOCK_BYTES, NONE)  # S16LE; minreq 10 ms
        self.handle = self.lib.pa_simple_new(
            None, b"Terminal Invaders", 1, None, b"Game", byref(spec), None, byref(attr), byref(self.err)
        )  # 1: PA_STREAM_PLAYBACK on the default sink
        if not self.handle:
            raise SinkError(f"no server (error {self.err.value})")

    def feed(self, mixer, feeding: bool) -> None:
        """Write one block; it blocks, with the GIL released, while tlength is queued."""
        if not feeding:
            return
        data = mixer.block().tobytes()
        if self.lib.pa_simple_write(self.handle, data, len(data), byref(self.err)) < 0:
            raise SinkError(f"write failed (error {self.err.value})")
        lead = self.lead_ms()
        dry = lead is not None and lead < 5  # the server had played everything before this block (A17)
        if dry and not self.dry:  # once per run-dry: after a stall pipewire-pulse can keep reporting about 0
            self.underruns += 1
        self.dry = dry

    def wait(self, feeding: bool) -> float:
        return 0.0 if feeding else 0.5

    def lead_ms(self) -> float | None:
        if not self.handle:
            return None
        usec = self.lib.pa_simple_get_latency(self.handle, byref(self.err))
        return None if usec >= 2**64 - 1 else usec / 1000  # (pa_usec_t) -1: no timing data

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle:
            try:
                self.lib.pa_simple_free(handle)  # no drain
            except Exception:
                pass


class PipeSink(Sink):
    """A player reading raw s16le stereo on stdin, kept PIPES[name] bytes ahead by FIONREAD on a 10 ms clock.

    Linux only: FIONREAD on a pipe's write end works only there. close() kills the player without a drain, since
    pw-cat once ignored EOF for over 20 s.
    """

    def __init__(
        self, spec_name: str, *, clock=time.monotonic, which=shutil.which, popen=subprocess.Popen, health_timeout=1.0
    ) -> None:
        self.name = spec_name
        self.argv, self.target = PIPES[spec_name]
        self.clock, self.which, self.popen, self.health_timeout = clock, which, popen, health_timeout
        self.proc, self.fd, self.level = None, -1, array("i", [0])
        self.next = self.moved = 0.0
        self.last = 0
        self.underruns = None  # the player reads ahead into its own buffer, so an empty pipe says nothing of the device

    def open(self) -> None:
        path = self.which(self.argv[0])
        if path is None:
            raise SinkError(f"no {self.name}")
        try:
            pipe, null = subprocess.PIPE, subprocess.DEVNULL
            self.proc = self.popen([path, *self.argv[1:]], stdin=pipe, stdout=null, stderr=null, close_fds=True)
        except OSError as exc:
            raise SinkError(exc.strerror or "cannot start") from exc
        self.fd = self.proc.stdin.fileno()
        os.set_blocking(self.fd, False)
        try:
            self.health()
        except SinkError:
            self.close()
            raise
        self.next = self.moved = self.clock()
        self.last = self.fill()

    def health(self) -> None:
        """Write 30 ms of silence and wait for the player to read some (players read in 10-23 ms [child E8])."""
        try:
            os.write(self.fd, bytes(HEALTH_BYTES))
        except OSError as exc:
            raise SinkError("closed its input") from exc
        deadline = self.clock() + self.health_timeout
        while self.fill() >= HEALTH_BYTES:
            self.alive()
            if self.clock() >= deadline:
                raise SinkError("health check timed out")  # e.g. a pw-cat with no session manager to link it
            time.sleep(POLL)

    def alive(self) -> None:
        if self.proc.poll() is not None:
            raise SinkError(f"exited {self.proc.returncode}")

    def fill(self) -> int:
        """Bytes in the pipe now."""
        fcntl.ioctl(self.fd, termios.FIONREAD, self.level)
        return self.level[0]

    def feed(self, mixer, feeding: bool) -> None:
        """At each tick: check the player, then write blocks while the level is under the target."""
        now = self.clock()
        if now < self.next:
            return
        self.next = self.next + TICK if now < self.next + TICK else now + TICK
        self.alive()
        level = self.fill()
        if level < self.last or level == 0:
            self.moved = now
        elif now - self.moved >= STALL:
            raise SinkError("stalled")
        while feeding and level < self.target:
            try:
                os.write(self.fd, mixer.block().tobytes())  # 1920 B < PIPE_BUF: all or nothing
            except BlockingIOError:
                break
            except OSError as exc:
                raise SinkError("broken pipe") from exc
            level += BLOCK_BYTES
        self.last = level

    def wait(self, feeding: bool) -> float:
        return max(0.0, self.next - self.clock()) if feeding else 0.5

    def lead_ms(self) -> float | None:
        return None if self.proc is None else self.fill() / BYTES_PER_MS

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            for step in (proc.stdin.close, proc.kill, proc.wait):
                try:
                    step()
                except Exception:
                    pass
