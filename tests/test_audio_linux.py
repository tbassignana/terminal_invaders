"""The Linux backends (spec 3, 9, 14 A3/A6/A17): PipeSink against tests/support/fakeplayer.py on PATH and against a
fake player on a real pipe and a fake clock; PulseSink against a fake libpulse-simple that converts every argument
with the code's own argtypes, as ctypes does."""

import ctypes
import functools
import json
import os
import signal
import subprocess
import sys
import time
from array import array

import pytest

from terminal_invaders.audio import linux
from terminal_invaders.audio.protocol import BLOCK_BYTES, BLOCK_SAMPLES
from terminal_invaders.audio.sink import SinkError
from tests.support import fakeplayer

pipes = pytest.mark.skipif(sys.platform == "darwin", reason="FIONREAD on a pipe's write end works only on Linux")
ARGV = {  # spec 3, as written there
    "pw-cat": ["--playback", "--format=s16", "--rate=48000", "--channels=2", "--latency=10ms", "--media-role=Game",
               "-P", '{ application.name = "Terminal Invaders" }', "-"],
    "pacat": ["--playback", "--raw", "--format=s16le", "--rate=48000", "--channels=2", "--latency-msec=20",
              "--client-name=Terminal Invaders", "--stream-name=Game"],
    "aplay": ["-q", "-t", "raw", "-f", "S16_LE", "-r", "48000", "-c", "2", "--buffer-time=40000",
              "--period-time=10000", "-"],
}  # fmt: skip


class Counter:
    """A mixer whose k-th block holds k in every sample, so what reached a player can be checked in order."""

    def __init__(self) -> None:
        self.k = 0

    def block(self) -> array:
        self.k += 1
        return array("h", [self.k]) * BLOCK_SAMPLES


def blocks(first: int, last: int) -> bytes:
    return b"".join((array("h", [k]) * BLOCK_SAMPLES).tobytes() for k in range(first, last + 1))


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def play(sink, mixer, seconds: float) -> float:
    """Run the helper's loop for up to `seconds`; returns how long it ran (until a SinkError, which propagates)."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        time.sleep(sink.wait(True))
        sink.feed(mixer, True)
    return time.monotonic() - start


@pytest.fixture
def players(tmp_path, monkeypatch):
    """fakeplayer first on PATH; returns a function that sets the mode and gives the runs' JSON lines."""
    directory = fakeplayer.install(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKEPLAYER_ARGV", str(tmp_path / "argv"))
    monkeypatch.setenv("FAKEPLAYER_OUT", str(tmp_path / "out"))

    def runs(mode=None):
        if mode is not None:
            monkeypatch.setenv("FAKEPLAYER_MODE", mode)
        log = tmp_path / "argv"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    runs.bin, runs.out = directory, tmp_path / "out"
    return runs


@pipes
@pytest.mark.parametrize("name", ["pw-cat", "pacat", "aplay"])
def test_each_player_gets_its_exact_argv_and_every_block_in_order(players, name):
    players("ok")
    sink, mixer = linux.PipeSink(name), Counter()
    sink.open()
    try:
        assert play(sink, mixer, 0.3) >= 0.3
        assert 0 <= sink.lead_ms() <= linux.PIPES[name][1] / linux.BYTES_PER_MS + 10
    finally:
        sink.close()
    sink.close()
    (run,) = players()
    assert run["argv"] == [str(players.bin / name), *ARGV[name]]
    assert gone(run["pid"])
    data = players.out.read_bytes()
    assert data[: linux.HEALTH_BYTES] == bytes(linux.HEALTH_BYTES)  # the health check's silence
    assert len(data) >= linux.HEALTH_BYTES + 10 * BLOCK_BYTES
    assert data[linux.HEALTH_BYTES :] == blocks(1, mixer.k)[: len(data) - linux.HEALTH_BYTES]


@pipes
def test_a_player_that_exits_or_is_missing_fails_open(players):
    players("exit1")
    with pytest.raises(SinkError, match="^exited 1$"):
        linux.PipeSink("pw-cat").open()
    assert gone(players()[0]["pid"])
    with pytest.raises(SinkError, match="^no pacat$"):
        linux.PipeSink("pacat", which=lambda name: None).open()

    def popen(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    with pytest.raises(SinkError, match="^No such file or directory$"):
        linux.PipeSink("aplay", popen=popen).open()


@pipes
def test_a_player_that_never_reads_fails_the_health_check_and_is_killed(players):
    players("noread")
    start = time.monotonic()
    with pytest.raises(SinkError, match="^health check timed out$"):
        linux.PipeSink("pw-cat", health_timeout=0.2).open()
    assert 0.2 <= time.monotonic() - start < 0.6
    assert gone(players()[0]["pid"])


@pipes
def test_a_player_that_stops_reading_is_stalled_after_half_a_second(players):
    players("stall_after:0.2")
    sink = linux.PipeSink("pacat")
    sink.open()
    try:
        start = time.monotonic()
        with pytest.raises(SinkError, match="^stalled$"):
            play(sink, Counter(), 3.0)
        assert 0.6 < time.monotonic() - start < 1.5  # 0.2 s of reading, then 0.5 s with no move
    finally:
        sink.close()
    assert gone(players()[0]["pid"])


@pipes
def test_a_killed_player_raises_at_the_next_tick(players):
    players("ok")
    sink = linux.PipeSink("aplay")
    sink.open()
    try:
        os.kill(players()[0]["pid"], signal.SIGKILL)
        with pytest.raises(SinkError, match="^exited -9$"):
            play(sink, Counter(), 1.0)
    finally:
        sink.close()


@pipes
def test_chatter_goes_to_dev_null_and_the_player_has_no_terminal(players, capfd):
    players("chatter")
    sink = linux.PipeSink("pw-cat", popen=functools.partial(subprocess.Popen, start_new_session=True))  # as the helper
    sink.open()
    try:
        play(sink, Counter(), 0.2)
    finally:
        sink.close()
    assert capfd.readouterr() == ("", "")
    assert len(players.out.read_bytes()) > linux.HEALTH_BYTES
    assert players()[0]["sid"] == players()[0]["pid"]


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class Proc:
    """A player on a real pipe: each poll() reads `rate` bytes, as if the player had read them since the last tick."""

    def __init__(self) -> None:
        self.reader, writer = os.pipe()
        os.set_blocking(self.reader, False)
        self.stdin, self.rate, self.returncode, self.pid = os.fdopen(writer, "wb"), BLOCK_BYTES, None, -1

    def poll(self):
        if self.reader >= 0 and self.rate:
            try:
                os.read(self.reader, self.rate)
            except BlockingIOError:
                pass
        return self.returncode

    def hangup(self) -> None:
        """The player closes its stdin."""
        if self.reader >= 0:
            os.close(self.reader)
        self.reader = -1

    def kill(self) -> None:
        self.returncode = -9

    def wait(self) -> int:
        self.hangup()
        return self.returncode


def fake_pipe(name: str, clock: Clock, procs: list, hungup=False) -> linux.PipeSink:
    def popen(argv, **options):
        procs.append(Proc())
        if hungup:
            procs[-1].hangup()
        return procs[-1]

    return linux.PipeSink(name, clock=clock, which=lambda name: f"/usr/bin/{name}", popen=popen)


@pipes
def test_level_pacing_on_the_ten_millisecond_clock():
    clock, procs, mixer, target = Clock(), [], Counter(), 20 * linux.BYTES_PER_MS
    sink = fake_pipe("pw-cat", clock, procs)
    sink.open()  # the first poll reads 1920 of the 5760 B: healthy
    proc = procs[0]
    sink.feed(mixer, True)  # tick 0: 1920 B more were read; one block brings the pipe back to 20 ms
    assert (mixer.k, sink.fill(), sink.lead_ms()) == (1, target, 20.0)
    clock.t += 0.004
    sink.feed(mixer, True)  # not yet: ticks are 10 ms apart
    assert mixer.k == 1 and sink.wait(True) == pytest.approx(0.006)
    clock.t += 0.006
    sink.feed(mixer, True)
    assert (mixer.k, sink.underruns) == (2, None)  # a player reads ahead: an empty pipe says nothing of the device
    clock.t += 0.25  # late: the schedule restarts from now
    proc.rate = 1 << 20  # the player drained the pipe
    sink.feed(mixer, True)
    assert (mixer.k, sink.underruns, sink.wait(True)) == (4, None, pytest.approx(0.01))
    clock.t += 0.01
    sink.feed(mixer, False)  # idle: nothing is written and the pipe runs empty, which is no stall
    assert (mixer.k, sink.fill(), sink.wait(False)) == (4, 0, 0.5)
    proc.rate = 0
    clock.t += 0.01
    sink.feed(mixer, True)
    assert mixer.k == 6
    start = clock.t
    with pytest.raises(SinkError, match="^stalled$"):
        for _ in range(100):  # the player reads nothing more
            clock.t += 0.01
            sink.feed(mixer, True)
    assert clock.t - start == pytest.approx(0.5, abs=0.011)
    sink.close()
    assert proc.returncode == -9 and sink.lead_ms() is None


@pipes
def test_a_player_that_closes_its_input_fails_at_open_or_at_the_next_write():
    clock, procs = Clock(), []
    sink = fake_pipe("aplay", clock, procs)
    sink.open()
    procs[0].rate = 1 << 20
    procs[0].poll()
    procs[0].hangup()
    with pytest.raises(SinkError, match="^broken pipe$"):
        sink.feed(Counter(), True)
    sink.close()
    with pytest.raises(SinkError, match="^closed its input$"):
        fake_pipe("aplay", clock, procs, hungup=True).open()
    assert procs[1].returncode == -9


class Function:
    """A fake C function: it converts each argument with the argtypes the code set, as ctypes does, so an array,
    bytearray or memoryview where a pointer goes raises TypeError (14 A3)."""

    def __init__(self, body) -> None:
        self.body, self.argtypes, self.restype = body, None, None

    def __call__(self, *args):
        assert self.argtypes is not None and len(args) == len(self.argtypes)
        for argtype, arg in zip(self.argtypes, args):
            argtype.from_param(arg)
        return self.body(*args)


class FakePulse:
    def __init__(self, handle=7, written=0, latency=40_000) -> None:
        self.handle, self.written, self.latency, self.data, self.opened, self.freed = (
            handle,
            written,
            latency,
            [],
            [],
            [],
        )
        self.pa_simple_new = Function(lambda *args: self.opened.append(args) or self.handle)
        self.pa_simple_write = Function(self.write)
        self.pa_simple_get_latency = Function(lambda handle, err: self.latency)
        self.pa_simple_free = Function(self.freed.append)

    def write(self, handle, data, size, err):
        assert isinstance(data, (bytes, ctypes.Array)) and handle == self.handle
        self.data.append(bytes(data)[:size])
        return self.written


def test_pulse_opens_a_playback_stream_with_tlength_55_ms_and_writes_bytes():
    lib = FakePulse()
    sink = linux.PulseSink(lib=lib)
    sink.open()
    (args,) = lib.opened
    assert args[:5] == (None, b"Terminal Invaders", 1, None, b"Game")
    spec, attr = args[5]._obj, args[7]._obj
    assert (spec.format, spec.rate, spec.channels) == (3, 48000, 2)
    assert (attr.maxlength, attr.tlength, attr.prebuf, attr.minreq, attr.fragsize) == (
        0xFFFFFFFF, 10560, 0xFFFFFFFF, 1920, 0xFFFFFFFF)  # fmt: skip
    mixer = Counter()
    sink.feed(mixer, True)
    sink.feed(mixer, False)  # idle: nothing written
    assert lib.data == [blocks(1, 1)] and sink.wait(True) == 0 and sink.wait(False) == 0.5
    assert sink.lead_ms() == 40.0 and sink.underruns == 0
    with pytest.raises(TypeError):  # the fake catches what real ctypes would reject
        lib.pa_simple_write(7, array("h", [0]), 2, ctypes.byref(ctypes.c_int()))
    lib.latency = 3000  # under 5 ms right after a write: the server had run dry
    sink.feed(mixer, True)
    sink.feed(mixer, True)  # still about 0 (pipewire-pulse after a stall): the same run-dry
    assert sink.underruns == 1
    lib.latency = 40000
    sink.feed(mixer, True)
    lib.latency = 0
    sink.feed(mixer, True)
    assert sink.underruns == 2
    lib.latency = 2**64 - 1
    assert sink.lead_ms() is None
    lib.written = -1
    with pytest.raises(SinkError, match="^write failed"):
        sink.feed(mixer, True)
    sink.close()
    sink.close()
    assert lib.freed == [7] and sink.lead_ms() is None


def test_pulse_fails_open_without_a_server_or_a_library(monkeypatch):
    with pytest.raises(SinkError, match=r"^no server \(error 0\)$"):
        linux.PulseSink(lib=FakePulse(handle=None)).open()

    def missing(name):
        raise OSError(f"{name}: cannot open shared object file")

    monkeypatch.setattr(linux, "CDLL", missing)
    with pytest.raises(SinkError, match="^no libpulse-simple$"):
        linux.PulseSink().open()
