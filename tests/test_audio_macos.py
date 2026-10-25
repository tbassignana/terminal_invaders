"""The macOS backends (spec 3, 9, 14 A2/A3/A8/A17/A18), run on any platform: AudioQueueSink against a fake
AudioToolbox that converts every argument with the code's own argtypes, as ctypes does, and refuses arrays, bytearrays
and memoryviews; the fake queue calls the real CALLBACK through its C address, from a thread every 10 ms; AfplaySink
against a fake afplay script that records its argv."""

import ctypes
import os
import queue
import signal
import struct
import subprocess
import sys
import threading
import time
import wave
from array import array
from collections import deque
from ctypes import c_void_p, sizeof
from pathlib import Path

import pytest

from terminal_invaders.arcade import machine
from terminal_invaders.audio import macos
from terminal_invaders.audio.protocol import BLOCK_BYTES, BLOCK_SAMPLES, CUES, RANK
from terminal_invaders.audio.sink import SinkError

ROOT = Path(__file__).resolve().parents[1]
MISSING = object()
# AudioStreamBasicDescription for 48 kHz stereo s16 little endian, packed field by field from CoreAudioBaseTypes.h
FORMAT_BYTES = struct.pack("<d8I", 48000.0, 0x6C70636D, 0xC, 4, 1, 4, 2, 16, 0)
ZEROS = bytes(BLOCK_BYTES)
not_mac = pytest.mark.skipif(sys.platform == "darwin", reason="the frameworks exist on macOS")


class FakeFunction:
    """One library function: callable only once declared, with each argument converted by its declared argtype's
    from_param (what ctypes does) after refusing array, bytearray and memoryview outright (A3)."""

    def __init__(self, name, run) -> None:
        self.name, self.run = name, run
        self.restype = self.argtypes = MISSING

    def __call__(self, *args):
        assert self.restype is not MISSING and self.argtypes is not MISSING, f"{self.name} was not declared"
        assert len(args) == len(self.argtypes), f"{self.name} takes {len(self.argtypes)} arguments"
        for arg, kind in zip(args, self.argtypes, strict=True):
            if isinstance(arg, (array, bytearray, memoryview)):
                raise TypeError(f"{self.name}: a {type(arg).__name__} handed to ctypes")
            kind.from_param(arg)
        return self.run(self.name, *args)


def target(ref):
    """What a byref() or pointer() argument points at."""
    return ref._obj if type(ref).__name__ == "CArgObject" else ref.contents


class FakeLibrary:
    """Each name in FUNCTIONS is a FakeFunction over the method it maps to; every call is recorded in .calls, and a
    name in .status returns that OSStatus without running."""

    FUNCTIONS: dict[str, str] = {}

    def __init__(self) -> None:
        self.calls = []
        self.status: dict[str, int] = {}
        for name in self.FUNCTIONS:
            setattr(self, name, FakeFunction(name, self._run))

    def _run(self, name, *args):
        self.calls.append((name, args))
        if self.status.get(name):
            return self.status[name]
        return getattr(self, self.FUNCTIONS[name])(*args)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeToolbox(FakeLibrary):
    """An AudioQueue that plays one buffer every 10 ms on its own thread (or on complete() with thread=False) and
    calls back through the callback's C address only."""

    FUNCTIONS = {
        "AudioQueueNewOutput": "new_output",
        "AudioQueueAllocateBuffer": "allocate",
        "AudioQueueEnqueueBuffer": "enqueue",
        "AudioQueueStart": "start",
        "AudioQueueStop": "stop",
        "AudioQueueDispose": "dispose",
    }
    QUEUE = 0x5150

    def __init__(self, *, thread=True) -> None:
        super().__init__()
        self.lock = threading.Lock()
        self.queued: deque[int] = deque()  # buffers the queue holds, oldest first
        self.enqueued: list[bytes] = []  # each enqueued buffer's bytes, in order
        self.memory: list = []  # the buffers' storage
        self.callback = None
        self.running = False
        self.stopped_with: list[int] = []  # buffers held at each Stop
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._play, daemon=True) if thread else None

    def new_output(self, fmt, callback, user, run_loop, mode, flags, out):
        assert bytes(target(fmt)) == FORMAT_BYTES
        assert user is None and run_loop is None and mode is None and flags == 0  # NULL run loop: the queue's thread
        self.callback = macos.CALLBACK(ctypes.cast(callback, c_void_p).value)  # as C holds it: no Python reference
        target(out).value = self.QUEUE
        if self.thread is not None:
            self.thread.start()
        return 0

    def allocate(self, aq, size, out):
        assert aq == self.QUEUE and size == BLOCK_BYTES
        data = ctypes.create_string_buffer(size)
        buffer = macos.AQBuffer(mAudioDataBytesCapacity=size, mAudioData=ctypes.addressof(data))
        self.memory += [data, buffer]
        target(out).value = ctypes.addressof(buffer)
        return 0

    def enqueue(self, aq, ref, descriptions, packets):
        buffer = macos.AQBuffer.from_address(ref)
        assert aq == self.QUEUE and descriptions == 0 and packets is None
        assert buffer.mAudioDataByteSize == BLOCK_BYTES
        with self.lock:
            assert ref not in self.queued, "a buffer enqueued twice"
            self.queued.append(ref)
            self.enqueued.append(ctypes.string_at(buffer.mAudioData, buffer.mAudioDataByteSize))
        return 0

    def start(self, aq, when):
        assert aq == self.QUEUE and when is None
        self.running = True
        return 0

    def stop(self, aq, immediate):
        assert immediate == 1
        with self.lock:
            self.running = False
            self.stopped_with.append(len(self.queued))
            self.queued.clear()
        return 0

    def dispose(self, aq, immediate):
        assert immediate == 1 and "AudioQueueStop" in self.names()
        self.done.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(1.0)
        return 0

    def complete(self, n: int) -> None:
        """Play n buffers now, calling back on this thread."""
        for _ in range(n):
            self.callback(None, self.QUEUE, self.queued.popleft())

    def _play(self) -> None:
        while not self.done.wait(0.010):
            with self.lock:
                ref = self.queued.popleft() if self.running and self.queued else None
            if ref is not None:
                self.callback(None, self.QUEUE, ref)


class Counter:
    """A mixer whose k-th block holds k in every sample."""

    def __init__(self) -> None:
        self.k = 0

    def block(self) -> array:
        self.k += 1
        return array("h", [self.k]) * BLOCK_SAMPLES


def counted(first: int, last: int) -> list[bytes]:
    return [(array("h", [k]) * BLOCK_SAMPLES).tobytes() for k in range(first, last + 1)]


class Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def queue_sink(*, thread=True, clock=time.monotonic):
    at = macos.declare(FakeToolbox(thread=thread))
    events = queue.SimpleQueue()
    return at, events, macos.AudioQueueSink(post=events.put, at=at, clock=clock)


def turn(sink, events, mixer, feeding: bool, timeout: float = 0.2) -> int:
    """One turn of the helper's loop: wait for an event, hand over all that are queued, feed; returns how many."""
    try:
        items = [events.get(timeout=timeout)]
    except queue.Empty:
        items = []
    while True:
        try:
            items.append(events.get_nowait())
        except queue.Empty:
            break
    for item in items:
        sink.on_event(item)
    sink.feed(mixer, feeding)
    return len(items)


def handover(sink, events, wait: float = 0.0) -> None:
    """Hand the sink every queued event, after waiting up to `wait` for the first."""
    try:
        sink.on_event(events.get(timeout=wait) if wait else events.get_nowait())
    except queue.Empty:
        return
    handover(sink, events)


def test_struct_sizes_codes_and_format():
    assert {kind.__name__: sizeof(kind) for kind in (macos.ASBD, macos.AQBuffer)} == {"ASBD": 40, "AQBuffer": 56}
    assert macos.AQBuffer.mAudioData.offset == 8 and macos.AQBuffer.mPacketDescriptionCount.offset == 48
    assert macos.fourcc("lpcm") == 0x6C70636D
    assert bytes(macos.FORMAT) == FORMAT_BYTES


def test_imports_under_isolated_python_without_loading_a_framework():
    code = f"import sys; sys.path.append({str(ROOT)!r}); from terminal_invaders.audio import macos; "
    code += "print(macos._LIBS is None, macos.CALLBACK.__name__)"
    out = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True, timeout=10)
    assert (out.returncode, out.stdout.split()[0], out.stderr) == (0, "True", "")


@not_mac
def test_without_the_framework_the_sink_fails():
    with pytest.raises(SinkError, match="no AudioToolbox"):
        macos.AudioQueueSink(post=lambda event: None).open()


def test_the_queue_keeps_30_to_50_ms_and_refills_each_buffer_in_order():
    at, events, sink = queue_sink()
    mixer, leads, refilled = Counter(), [], set()
    assert macos.AudioQueueSink.low_water  # so the engine's stats sample the lead just before each refill
    try:
        sink.open()  # five buffers of silence, then Start
        opening = ["AudioQueueNewOutput"] + ["AudioQueueAllocateBuffer"] * 5 + ["AudioQueueEnqueueBuffer"] * 5
        assert at.names() == opening + ["AudioQueueStart"]
        assert isinstance(sink.cb, macos.CALLBACK)
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            handover(sink, events, wait=0.2)
            leads.append(sink.lead_ms())  # just before the refill
            sink.feed(mixer, True)
            refilled.add(sink.lead_ms())
    finally:
        sink.close()
    leads.sort()
    assert leads[len(leads) // 20] >= 30 and leads[len(leads) // 2] == 40 and refilled == {50}
    assert sink.callbacks >= 20 and len(at.enqueued) == 5 + mixer.k
    assert at.enqueued == [ZEROS] * 5 + counted(1, mixer.k)  # every block once, in order
    assert at.names().count("AudioQueueStart") == 1


def test_idle_stops_the_queue_once_every_buffer_is_back_and_the_next_feed_restarts_it():
    at, events, sink = queue_sink()
    mixer = Counter()
    try:
        sink.open()
        deadline = time.monotonic() + 1.0
        while at.running and time.monotonic() < deadline:
            turn(sink, events, mixer, False, timeout=0.05)
        assert at.stopped_with == [0] and not sink.running
        assert (sink.lead_ms(), sink.underruns, sink.callbacks) == (0, 0, 5)  # draining while idle is no underrun
        for _ in range(3):
            assert turn(sink, events, mixer, False, timeout=0.03) == 0  # stopped: nothing comes back
        assert at.names().count("AudioQueueStop") == 1
        calls = len(at.calls)
        sink.feed(mixer, True)
        assert at.names()[calls:] == ["AudioQueueEnqueueBuffer"] * 5 + ["AudioQueueStart"]
        assert sink.lead_ms() == 50
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            turn(sink, events, mixer, True)
        assert sink.callbacks > 10 and at.enqueued[5:10] == counted(1, 5)
    finally:
        sink.close()


def test_an_underrun_is_a_callback_that_leaves_nothing_queued_while_feeding():
    at, events, sink = queue_sink(thread=False)
    mixer = Counter()
    sink.open()
    sink.feed(mixer, True)
    at.complete(2)
    handover(sink, events)
    assert sink.lead_ms() == 30
    sink.feed(mixer, True)
    assert (sink.lead_ms(), sink.underruns) == (50, 0)
    at.complete(5)
    handover(sink, events)
    assert (sink.underruns, sink.lead_ms()) == (1, 0)
    sink.feed(mixer, True)
    sink.feed(mixer, False)
    at.complete(5)
    handover(sink, events)
    assert sink.underruns == 1  # not feeding: the queue drains on purpose
    sink.feed(mixer, False)
    assert at.stopped_with == [0] and sink.callbacks == 12
    sink.close()


def test_two_seconds_without_a_callback_while_buffers_are_queued_is_a_stall():
    clock = Clock()
    at, events, sink = queue_sink(thread=False, clock=clock)
    mixer = Counter()
    sink.open()
    clock.now += 2.0
    sink.feed(mixer, True)
    at.complete(1)
    handover(sink, events)
    clock.now += 1.9
    sink.feed(mixer, True)
    clock.now += 0.2  # 2.1 s since the last buffer came back
    with pytest.raises(SinkError, match="stalled"):
        sink.feed(mixer, True)
    sink.close()


@pytest.mark.parametrize("failing", ["NewOutput", "AllocateBuffer", "EnqueueBuffer", "Start"])
def test_a_nonzero_osstatus_fails_open_and_disposes_the_queue(failing):
    at, events, sink = queue_sink(thread=False)
    at.status["AudioQueue" + failing] = -50
    with pytest.raises(SinkError, match=f"^AudioQueue{failing} -50$"):
        sink.open()
    disposed = failing != "NewOutput"
    assert at.names()[-2:] == (["AudioQueueStop", "AudioQueueDispose"] if disposed else ["AudioQueue" + failing])
    assert sink.queue is None and sink.lead_ms() is None


def test_a_failed_enqueue_mid_run_raises():
    at, events, sink = queue_sink(thread=False)
    sink.open()
    at.complete(1)
    handover(sink, events)
    at.status["AudioQueueEnqueueBuffer"] = -66632
    with pytest.raises(SinkError, match="AudioQueueEnqueueBuffer -66632"):
        sink.feed(Counter(), True)
    sink.close()


def test_close_stops_before_disposing_then_drops_the_callback(capfd):
    at, events, sink = queue_sink(thread=False)
    sink.open()
    sink.post = lambda event: 1 / 0
    at.complete(1)  # a callback whose post raises: swallowed, nothing printed
    calls = len(at.calls)
    sink.close()
    assert at.calls[calls:] == [("AudioQueueStop", (at.QUEUE, 1)), ("AudioQueueDispose", (at.QUEUE, 1))]
    assert sink.cb is None and sink.lead_ms() is None
    sink.close()
    sink.on_event(("aq", ctypes.addressof(at.memory[1])))  # a buffer of the closed queue: ignored
    assert len(at.calls) == calls + 2 and sink.callbacks == 0
    assert capfd.readouterr() == ("", "")
    at, events, sink = queue_sink(thread=False)
    sink.open()
    at.status["AudioQueueDispose"] = -1
    callback = sink.cb
    sink.close()
    assert sink.cb is None and macos._KEEP[-1] is callback  # the queue may still call it: never freed
    macos._KEEP.remove(callback)


PLAYER = """#!/bin/sh
echo "$$ $*" >> {log}
case "$*" in *victory.wav) exit 0;; esac
exec sleep 30
"""


class Player:
    """A fake afplay: each run appends "PID ARGV" to a log, then sleeps 30 s (victory.wav ends at once). spawned
    lists the argv the sink passed to popen."""

    def __init__(self, directory: Path) -> None:
        self.log = directory / "afplay.log"
        self.path = directory / "afplay"
        self.path.write_text(PLAYER.format(log=self.log))
        self.path.chmod(0o755)
        self.spawned: list[list[str]] = []

    def popen(self, argv, **kwargs):
        assert kwargs == {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                          "close_fds": True}  # fmt: skip
        self.spawned.append(argv)
        return subprocess.Popen(argv, **kwargs)

    def runs(self, count: int) -> list[list[str]]:
        """The argv of the first `count` runs, waiting up to 3 s for them to start."""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            lines = self.log.read_text().splitlines() if self.log.exists() else []
            if len(lines) >= count:
                return [line.split()[1:] for line in lines]
            time.sleep(0.01)
        raise AssertionError(f"{count} runs expected, log: {lines}")


def afplay(tmp_path):
    player = Player(tmp_path)
    cues = [array("h", [rank, -rank] * 10) for rank in range(len(CUES))]
    sink = macos.AfplaySink(cues=cues, player=str(player.path), popen=player.popen)
    sink.open()
    return player, sink


def test_afplay_writes_mono_wavs_and_plays_three_cues_at_once_by_rank(tmp_path):
    player, sink = afplay(tmp_path)
    try:
        assert (sink.mixes, sink.wait(False), sink.lead_ms()) == (False, 0.1, None)
        with wave.open(sink.wavs[2], "rb") as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 48000)
            assert wav.readframes(100) == array("h", [2] * 10).tobytes()  # the left channel
        assert Path(sink.wavs[0]).name == "game_over.wav" and len(sink.wavs) == len(CUES)
        fire, ufo, game_over, life = RANK["fire"], RANK["ufo"], RANK["game_over"], RANK["life"]
        for count, rank in enumerate((fire, ufo, fire), 1):
            sink.cue(rank)
            player.runs(count)
        oldest_fire = sink.effects[0][1]
        sink.cue(fire)  # outranks no busy player: dropped
        assert len(player.spawned) == 3
        sink.cue(game_over)  # replaces the lowest rank, the oldest of the two fires
        assert oldest_fire.returncode == -signal.SIGKILL
        player.runs(4)
        sink.cue(life)  # replaces the other fire
        assert [rank for rank, _ in sink.effects] == [ufo, game_over, life]
        assert player.runs(5) == [[sink.wavs[rank]] for rank in (fire, ufo, fire, game_over, life)]
        players, directory = [proc for _, proc in sink.effects], sink.dir
    finally:
        sink.close()
    assert all(proc.returncode == -signal.SIGKILL for proc in players) and not os.path.exists(directory)
    sink.close()


def test_afplay_cut_silences_the_cues_now_playing(tmp_path):
    player, sink = afplay(tmp_path)
    try:
        sink.cue(0)
        sink.cue(1)
        player.runs(2)
        cues = [proc for _, proc in sink.effects]
        sink.cut()  # M off sends "!": every cue stops at once
        assert sink.effects == [] and all(proc.returncode == -signal.SIGKILL for proc in cues)
        sink.feed(None, False)
        sink.cue(2)
        assert player.runs(3)[-1] == [sink.wavs[2]]  # and the next cue plays
    finally:
        sink.close()


@pytest.mark.parametrize("name", ["saucer", "saucer_hit"])
def test_afplay_plays_a_retriggered_cue_one_player_at_a_time(tmp_path, name):
    player, sink = afplay(tmp_path)
    again, fire, march = RANK[name], RANK["fire"], RANK["march1"]
    try:
        assert macos.RETRIGGERED == set(machine._REPEATS)  # every cue the arcade re-sends while its bit stays on
        sink.cue(again)
        player.runs(1)
        sink.cue(again)  # the game's next repeat while the first still plays: dropped
        sink.cue(fire)
        sink.cue(march)
        assert [rank for rank, _ in sink.effects] == [again, fire, march] and len(player.spawned) == 3
        player.runs(3)  # both have started, so the next run logged is the repeat's
        first = sink.effects[0][1]
        first.kill()  # its clip ends
        first.wait()
        sink.cue(again)  # the next repeat plays
        assert [rank for rank, _ in sink.effects] == [fire, march, again]
        assert player.runs(4)[-1] == [sink.wavs[again]]
    finally:
        sink.close()


def test_afplay_feed_reaps_players_that_finish(tmp_path):
    player, sink = afplay(tmp_path)
    try:
        sink.cue(RANK["victory"])  # its player exits by itself
        done = sink.effects[0][1]  # before the next cue, which may already reap it
        sink.cue(RANK["game_over"])
        player.runs(2)
        deadline = time.monotonic() + 3
        while len(sink.effects) == 2 and time.monotonic() < deadline:
            time.sleep(0.01)
            sink.feed(None, False)
        assert [rank for rank, _ in sink.effects] == [RANK["game_over"]] and done.returncode == 0
    finally:
        sink.close()


def test_afplay_needs_the_player(tmp_path):
    sink = macos.AfplaySink(cues=[], player=str(tmp_path / "afplay"))
    with pytest.raises(SinkError, match="no afplay"):
        sink.open()
    sink.close()
