"""The real helper process, playing into the file sink (INVADERS_AUDIO=file:PATH): what reaches the "device", the test
tone, what update() costs the game, and that no process outlives the game however it ends."""

import errno
import os
import resource
import select
import signal
import statistics
import subprocess
import sys
import time
from array import array
from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest

from terminal_invaders.audio import CLOSE_WAIT, ROOT, Audio
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


def switches() -> tuple[int, int]:
    """How often the scheduler has switched this thread out: as it waited, and as it was preempted ((0, 0) where that
    is not known, as on macOS)."""
    if THREAD is None:
        return 0, 0
    usage = resource.getrusage(THREAD)
    return usage.ru_nvcsw, usage.ru_nivcsw


def machine(before: tuple[int, int], after: tuple[int, int], cpu: int) -> bool:
    """Whether the kernel puts a delay down to the machine: it preempted the thread, which never gave up the CPU
    itself (to wait) and used at most 1 ms of it. Never where switches are not known."""
    return after[1] != before[1] and after[0] == before[0] and cpu <= 1_000_000


def recording(fd: int, writes: list):
    """os.write as update() sees it: a write on fd (the helper's pipe) is timed and kept in writes as (could block,
    wall ns), and one on the pipe left blocking is not made, as on a stopped helper it would never return (fail, not
    hang). Writes elsewhere pass straight through."""
    real = os.write

    def write(to, data):
        if to != fd:
            return real(to, data)
        if os.get_blocking(fd):
            writes.append((True, 0))
            raise BlockingIOError(errno.EAGAIN, "not made: it could block")
        began = time.perf_counter_ns()
        try:
            return real(fd, data)
        finally:
            writes.append((False, time.perf_counter_ns() - began))

    return write


def late(cpu: int) -> bool:
    """A control for a call that used cpu ns of CPU time: a spin on the wall clock for twice as long (at most 0.5 ms,
    so a call slow by its own work cannot excuse itself), timed as the call was. Whether it took over 1 ms that the
    kernel does not put down to the machine (machine()): the spin cannot wait, so only the machine can hold it up. A
    spin the kernel says did wait was held up by the game itself (another of its threads had the GIL), so not late."""
    switched = switches()
    ran, began = time.thread_time_ns(), time.perf_counter_ns()
    end = began + min(2 * cpu, 500_000)
    while time.perf_counter_ns() < end:
        pass
    wall, ran = time.perf_counter_ns() - began, time.thread_time_ns() - ran
    after = switches()
    return wall > 1_000_000 and not machine(switched, after, ran) and after[0] == switched[0]


def timed_updates(audio, step: float, now: list) -> tuple[list[tuple[int, bool, int, int]], int]:
    """10 000 update() calls, the clock moving step s per call, cycling through the cues: for each, its cost, whether
    the kernel puts a delay in it down to the machine (machine()), how many writes it made and how many of those could
    have blocked; and how many of their controls (late()) were late.

    The cost is the call's wall time less the time it was off the CPU inside its write (the write's wall time, or the
    call's whole time off the CPU where that is less, so its own CPU time always counts). The pipe does not block, so
    the call cannot wait for the helper there; but the write wakes the helper, which on a busy machine may take the CPU
    (and something else after it) before the write returns. A call the kernel says waited keeps all of its time:
    os.write lets go of the GIL, so a wait for another thread of the game lands there too. Elsewhere in the call, only
    the machine can hold it up without the call (or the game) doing something slow, and the controls measure that as
    it happens: after each 100 calls, made back to back, come their 100 controls, each exposed to the machine twice as
    long, with signals held so that a handler of the game's runs after them, not in them."""
    names, calls, writes, seen = [name for name, *_ in CUES], [], [], 0
    with patch("os.write", recording(audio.proc.stdin.fileno(), writes)):
        for block in range(100):
            timed = []
            for n in range(block * 100, block * 100 + 100):
                now[0] += step
                writes.clear()
                switched = switches()
                cpu, began = time.thread_time_ns(), time.perf_counter_ns()
                audio.update(names[n % len(names) : n % len(names) + 3])
                wall, cpu = time.perf_counter_ns() - began, time.thread_time_ns() - cpu
                after = switches()
                timed.append((wall, cpu, machine(switched, after, cpu), after[0] != switched[0], tuple(writes)))
            held = signal.pthread_sigmask(signal.SIG_BLOCK, signal.valid_signals())
            try:
                for wall, cpu, by_machine, waited, made in timed:
                    descheduled = 0 if waited else max(min(sum(spent for _, spent in made), wall - cpu), 0)
                    calls.append((wall - descheduled, by_machine, len(made), sum(could for could, _ in made)))
                    seen += late(cpu)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, held)
    return calls, seen


def cheap(audio, step: float, now: list) -> bool:
    """Whether 10 000 update() calls (timed_updates) cost a median of at most 50 us, and at most 3 more of them cost
    over 1 ms than there were late controls: so p99.9 is at most 1 ms (A13) where nothing else runs. Where the machine
    is busy, the calls it held up are matched by the controls it held up, or the kernel puts both down to it alike
    (machine()); a call slow by its own doing (a wait, or its own work) is not. Whatever the timing, each call made at
    most one write (whose descheduling the cost leaves out) and none that could block, and the recording saw those
    writes, so neither check passes for want of any."""
    calls, seen = timed_updates(audio, step, now)
    assert sum(could_block for *_, could_block in calls) == 0  # a pipe left blocking: on a full one, a hang
    assert max(writes for *_, writes, _ in calls) == 1  # one write at most, and the recording sees them
    slow = sum(cost > 1_000_000 and not by_machine for cost, by_machine, *_ in calls)
    return statistics.median(cost for cost, *_ in calls) <= 50_000 and slow <= 3 + seen


def test_update_costs_the_game_microseconds_even_while_the_helper_is_stopped(sink, monkeypatch):
    now = [0.0]
    monkeypatch.setenv("INVADERS_AUDIO_TONE", "440")  # the helper feeds all along, as it does in a busy game
    audio = Audio(clock=lambda: now[0])
    try:
        # Playing: a 60 Hz game's cues, so the helper sees a game's load. Two retries, for a burst of load that hit
        # the calls and missed their controls.
        assert any(cheap(audio, 1 / 60, now) for _ in range(3))
        os.kill(audio.pid, signal.SIGSTOP)
        filled = 0
        try:
            while True:  # what a stopped helper leaves: a full pipe, so every write hits EAGAIN and drops
                filled += os.write(audio.proc.stdin.fileno(), b"\x00" * 4096)
        except BlockingIOError:
            pass
        assert filled > 0
        assert any(cheap(audio, 1.0, now) for _ in range(3))
        assert audio.available
        os.kill(audio.pid, signal.SIGCONT)
    finally:
        audio.close()


@contextmanager
def watching(proc):
    """What close() does to the helper, through the real calls, in order: ("select", timeout, ran_out, spent) for each
    wait (ran_out: nothing was ready, so it waited out its whole timeout), then ("killpg", when) and ("reap", spent),
    when being time.monotonic() as the call began and spent how long the call took."""
    steps = []
    real_select, real_killpg, real_wait = select.select, os.killpg, proc.wait

    def waits(read, write, error, timeout=None):
        assert timeout is not None, "a wait without a timeout"  # on a stopped helper it never ends: fail, not hang
        began = time.monotonic()
        ready = real_select(read, write, error, timeout)
        steps.append(("select", timeout, not any(ready), time.monotonic() - began))
        return ready

    def kills(*args):
        steps.append(("killpg", time.monotonic()))
        return real_killpg(*args)

    def reaps(*args, **options):
        began = time.monotonic()
        try:
            return real_wait(*args, **options)
        finally:
            steps.append(("reap", time.monotonic() - began))

    with patch("select.select", waits), patch("os.killpg", kills), patch.object(proc, "wait", reaps):
        yield steps


@pytest.mark.parametrize("how", ["stopped", "running"])
def test_close_is_bounded_and_leaves_no_process(started, how):
    pgid, proc = started.pid, started.proc
    if how == "stopped":
        os.kill(pgid, signal.SIGSTOP)  # it cannot read the q: close kills its group once CLOSE_WAIT is out
    with watching(proc) as steps:
        began = time.monotonic()
        started.close()
        took = time.monotonic() - began
    # It waited first, then killed the group and only then reaped the helper, and did nothing after that.
    names = [name for name, *_ in steps]
    assert len(names) > 2 and names == ["select"] * (len(names) - 2) + ["killpg", "reap"], steps
    *waits, (_, killed_at), (_, reaping) = steps
    # The waits that ran out asked for CLOSE_WAIT at most in all: each one what was left of a single deadline, so a
    # slow machine only makes them shorter, and a second CLOSE_WAIT would double it.
    assert sum(timeout for _, timeout, ran_out, _ in waits if ran_out) <= CLOSE_WAIT + 0.01, waits
    # Outside those waits and the reap, close() runs a few lines, which take far less than CLOSE_WAIT on any machine:
    # time it spent waiting some other way (a second CLOSE_WAIT, or a pause between the kill and the reap) shows here.
    assert took - sum(spent for *_, spent in waits) - reaping < CLOSE_WAIT, steps
    if how == "stopped":
        assert killed_at - began >= CLOSE_WAIT - 0.001  # it gave the helper all of CLOSE_WAIT to quit
        assert proc.returncode == -signal.SIGKILL  # then killed its group, stopped as it was, and reaped it
    else:
        # The helper's EOF ended the wait, long before CLOSE_WAIT ran out: on q it quits at once (a few ms even on a
        # loaded machine, as nothing in that path waits on a timer), so a helper slow to quit shows here too.
        assert killed_at - began < 0.1
        assert proc.returncode == 0 and "bye" in started.lines  # it quit by itself
    assert took < 2.0  # a hang guard only: how long close() waits is checked above, on its own deadline
    assert alive(pgid) == []


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
# What the helper's watchdog does, as a control on this machine: once ready, sleep 1 s after EOF (when the game dies,
# as it holds the pipe's write end), then say when it woke; time.monotonic() is one clock for every process.
WATCHDOG = """
import os, time
print("ready", flush=True)
os.read(0, 1)
time.sleep(1.0)
print(time.monotonic(), flush=True)
os._exit(0)
"""


@pytest.mark.parametrize("hang, within", [("", 0.5), ("1", 1.2)])
def test_a_killed_game_leaves_nothing_behind(sink, monkeypatch, hang, within):
    if hang:
        monkeypatch.setenv("INVADERS_AUDIO_HANG", "1")  # the main loop hangs; the watchdog ends it
    eof, held = os.pipe()
    control = subprocess.Popen([sys.executable, "-I", "-c", WATCHDOG], stdin=eof, stdout=subprocess.PIPE, text=True)
    game = subprocess.Popen([sys.executable, "-c", GAME, str(ROOT)], stdout=subprocess.PIPE, text=True, pass_fds=[held])
    os.close(eof)
    os.close(held)
    seen = []  # time.monotonic() just before each look that still found the helper alive

    def gone() -> bool:
        looked = time.monotonic()
        if alive(pgid):
            seen.append(looked)
            return False
        return True

    try:
        pgid = int(game.stdout.readline())
        assert alive(pgid) and control.stdout.readline() == "ready\n"
        began = time.monotonic()
        game.kill()
        game.wait()
        assert wait_until(gone, within + 3.0), alive(pgid)  # a hang guard only: what is checked follows
        took = time.monotonic() - began
        last = max(seen, default=began) - began  # it was alive then, however late the look that found it gone came
        if hang:
            assert took >= 0.9  # the watchdog's 1 s after EOF, not the main loop, ended it
            late = float(control.stdout.readline()) - began - 1.0  # how late a 1 s sleep after that EOF ran here
            assert last <= within + late  # and the watchdog was on time for this machine
        else:
            assert last <= within  # the main loop quit at EOF; it did not wait for the watchdog
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)  # a failed case must not leave a hung helper behind
        except (NameError, ProcessLookupError, PermissionError):
            pass
        for process in (game, control):
            process.kill()
            process.wait()
            process.stdout.close()


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
