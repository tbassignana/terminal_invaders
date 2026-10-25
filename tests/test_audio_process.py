"""The real helper process, playing into the file sink (INVADERS_AUDIO=file:PATH): what reaches the "device", the test
tone, what update() costs the game, and that no process outlives the game however it ends."""

import os
import resource
import signal
import statistics
import subprocess
import sys
import time
from array import array
from unittest.mock import Mock

import pytest

from terminal_invaders.audio import ROOT, Audio
from terminal_invaders.audio.helper import Tone
from terminal_invaders.audio.mixer import synth
from terminal_invaders.audio.protocol import BLOCK_BYTES, CUES, RANK
from terminal_invaders.engine import Game
from terminal_invaders.model import Config
from tests.support.audioprocs import alive


def wait_until(condition, timeout: float, step: float = 0.01):
    deadline = time.monotonic() + timeout
    while not (result := condition()) and time.monotonic() < deadline:
        time.sleep(step)
    return result


@pytest.fixture
def sink(tmp_path, monkeypatch):
    path = tmp_path / "sink.raw"
    monkeypatch.setenv("INVADERS_AUDIO", f"file:{path}")
    return path


@pytest.fixture
def started(sink):
    """A real Audio whose helper has opened the file sink; closed (and checked gone) afterwards."""
    lines = []
    audio = Audio()
    audio.on_status = lines.append
    audio.lines = lines
    assert wait_until(lambda: audio.poll_status() or audio.backend, 2.0) == "file", lines
    yield audio
    pgid = audio.pid
    audio.close()
    if pgid is not None:
        assert wait_until(lambda: not alive(pgid), 0.5)


def test_a_cue_reaches_the_device_exactly_as_synthesized_at_the_frame_the_helper_names(started, sink):
    started.update(["fire"])
    voice = wait_until(lambda: started.poll_status() or [line for line in started.lines if line.startswith("voice")], 2)
    assert voice, started.lines
    name, frame = voice[0].split()[1:]
    assert name == "fire"
    expected = synth(*CUES[RANK["fire"]][1:4])
    start, end = int(frame) * 4, int(frame) * 4 + len(expected) * 2
    assert wait_until(lambda: sink.stat().st_size >= end, 2.0)
    played = array("h", sink.read_bytes()[start:end])
    if sys.byteorder == "big":
        played.byteswap()
    assert played == expected


def test_invaders_audio_tone_plays_the_test_tone_from_the_first_block(sink, monkeypatch):
    monkeypatch.setenv("INVADERS_AUDIO_TONE", "440")
    audio = Audio()
    try:
        assert wait_until(lambda: sink.exists() and sink.stat().st_size >= 10 * BLOCK_BYTES, 2.0)
        played = array("h", sink.read_bytes()[: 10 * BLOCK_BYTES])
        if sys.byteorder == "big":
            played.byteswap()
        tone = Tone(440)
        assert played == sum((tone.read() for _ in range(10)), array("h"))
    finally:
        audio.close()


THREAD = getattr(resource, "RUSAGE_THREAD", None)  # Linux: this thread's own counts


def preemptions() -> int:
    """How often the scheduler has taken the CPU from this thread (0 where that is not known)."""
    return resource.getrusage(THREAD).ru_nivcsw if THREAD is not None else 0


def timed_updates(audio, step: float, now: list) -> list[tuple[int, bool]]:
    """10 000 update() calls, the clock moving step s per call, cycling through the cues: each call's ns, and whether
    the scheduler preempted it."""
    names, calls = [name for name, *_ in CUES], []
    for n in range(10_000):
        now[0] += step
        before = preemptions()
        began = time.perf_counter_ns()
        audio.update(names[n % len(names) : n % len(names) + 3])
        calls.append((time.perf_counter_ns() - began, preemptions() != before))
    return calls


def cheap(calls: list[tuple[int, bool]]) -> bool:
    """Median at most 50 us, and at most 3 calls over 1 ms, so p99.9 is at most 1 ms (A13). The limits are wall time,
    so a call the scheduler preempted (a busy machine, or the helper woken by the write, took the CPU) is not counted
    as slow; a write that blocked would be a voluntary switch, so it still is."""
    slow = sum(ns > 1_000_000 and not preempted for ns, preempted in calls)
    return statistics.median(ns for ns, _ in calls) <= 50_000 and slow <= 3


def test_update_costs_the_game_microseconds_even_while_the_helper_is_stopped(sink, monkeypatch):
    now = [0.0]
    monkeypatch.setenv("INVADERS_AUDIO_TONE", "440")  # the helper feeds all along, as it does in a busy game
    audio = Audio(clock=lambda: now[0])
    try:
        # Playing: a 60 Hz game's cues, so the helper sees a game's load. Two retries, for where preemption is not
        # known (macOS) and for stalls it misses.
        assert any(cheap(timed_updates(audio, 1 / 60, now)) for _ in range(3))
        os.kill(audio.pid, signal.SIGSTOP)
        filled = 0
        try:
            while True:  # what a stopped helper leaves: a full pipe, so every write hits EAGAIN and drops
                filled += os.write(audio.proc.stdin.fileno(), b"\x00" * 4096)
        except BlockingIOError:
            pass
        assert filled > 0
        assert any(cheap(timed_updates(audio, 1.0, now)) for _ in range(3))
        assert audio.available
        os.kill(audio.pid, signal.SIGCONT)
    finally:
        audio.close()


@pytest.mark.parametrize("how", ["stopped", "running"])
def test_close_is_bounded_and_leaves_no_process(started, how):
    pgid = started.pid
    if how == "stopped":
        os.kill(pgid, signal.SIGSTOP)  # it cannot read the q: close kills its group after 0.3 s
    began = time.monotonic()
    started.close()
    took = time.monotonic() - began
    assert took <= 0.35 if how == "stopped" else took <= 0.1
    assert alive(pgid) == []
    assert "bye" in started.lines or how == "stopped"


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_the_helper_has_no_terminal_and_nothing_it_prints_goes_anywhere(started):
    pid = started.pid
    assert os.readlink(f"/proc/{pid}/fd/1") == os.readlink(f"/proc/{pid}/fd/2") == "/dev/null"
    assert os.getsid(pid) == pid != os.getsid(0)


GAME = """
import sys, time
sys.path.insert(0, sys.argv[1])
from terminal_invaders.audio import Audio
audio = Audio()
while audio.backend is None:
    audio.poll_status(); time.sleep(0.01)
print(audio.pid, flush=True)
time.sleep(60)
"""


@pytest.mark.parametrize("hang, within", [("", 0.5), ("1", 1.2)])
def test_a_killed_game_leaves_nothing_behind(sink, monkeypatch, hang, within):
    if hang:
        monkeypatch.setenv("INVADERS_AUDIO_HANG", "1")  # the main loop hangs; the watchdog ends it
    game = subprocess.Popen([sys.executable, "-c", GAME, str(ROOT)], stdout=subprocess.PIPE, text=True)
    try:
        pgid = int(game.stdout.readline())
        assert alive(pgid)
        game.kill()
        game.wait()
        began = time.monotonic()
        assert wait_until(lambda: not alive(pgid), within + 0.3), alive(pgid)
        took = time.monotonic() - began
        assert took <= within
        if hang:
            assert took >= 0.9  # the watchdog's 1 s after EOF, not the main loop, ended it
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)  # a failed case must not leave a hung helper behind
        except (NameError, ProcessLookupError, PermissionError):
            pass
        game.kill()
        game.wait()
        game.stdout.close()


def test_sound_never_changes_the_simulation(sink):
    def run(audio) -> str:
        game = Game(Config(seed=1234))
        for tick in range(1500):
            game.step(("left",) if tick % 90 < 30 else ("right", "fire") if tick % 90 < 60 else ("fire",))
            audio.update(game.events)
        return game.digest()

    audio = Audio()
    try:
        assert audio.pid is not None
        assert run(audio) == run(Mock())
    finally:
        audio.close()
