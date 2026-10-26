"""G1 (spec 13): 60 fps at 80x24 and 240x70 with no tearing, the draw + encode cost, and non-sync terminals.

The pty rows (G1.1, G1.2) play a minute each and the benchmark row (G1.3) takes about as long: they run with
pytest -m gate on Linux. The pty rows run the terminal stub in a process of its own, and a frame over 2 ticks that
coincides with a host stall that sleeper processes (one per CPU) measured is excused and reported (the spec owner's
decision 2). The benchmark's report, G1.4 (on a FakeTerminal's virtual clock), the stall matching, the sleepers and
the stub process run every time.
"""

import argparse
import importlib.util
import math
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from terminal_invaders import app, fx
from terminal_invaders.engine import Game
from terminal_invaders.input import IntentDriver, VirtualInput
from terminal_invaders.model import TICK_RATE, Config
from terminal_invaders.term.caps import EnvHints
from terminal_invaders.term.encode import ACKS, BSU, ESU
from terminal_invaders.term.output import OutputQueue
from terminal_invaders.term.simkeys import PROFILES
from terminal_invaders.term.tty import TerminalSession
from tests.support import stalls
from tests.support.faketerm import FakeTerminal
from tests.support.frames import session
from tests.support.ptyrun import IsolatedPty
from tests.support.terminals import load
from tests.test_app import play
from tests.test_ssh_lag import gate
from tests.test_terminal import UP, leave, until

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "bench_render.py"
RUN = 60.0
# a row: size, k, mean, p50, p95, max (ms), bytes a frame and its p95, then (effects on) their cost and extra bytes
ROW = re.compile(
    r"^ *(\d+)x(\d+) +(\d) +([\d.]+) +([\d.]+) +([\d.]+) +([\d.]+) +(\d+) +(\d+)(?: +[+-][\d.]+ +[+-]\d+)?$", re.M
)
ACK = re.compile(rb"(\x1b\[\d+;\d+H\x1b\[6n)?")  # what follows a frame's ESU: its tagged ack query, if any


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("bench_render_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    path = list(sys.path)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[spec.name]
        sys.path[:] = path


def test_the_benchmark_reports_each_size_at_its_scale(bench, capsys):
    # No --check: 30 frames under CI's coverage tracer are no timing (they ran 2.2 ms mean at 240x70 against 1.5).
    # The thresholds are G1.3's gate row, which runs untraced; the last test covers --check itself.
    assert bench.main(["--ticks", "30", "--sizes", "80x24", "240x70"]) == 0
    rows = ROW.findall(capsys.readouterr().out)
    assert [(int(c), int(r), int(k)) for c, r, k, *_ in rows] == [(80, 24, 1), (240, 70, 3)]
    assert all(0 < float(mean) and int(nbytes) > 0 for *_, mean, _, _, _, nbytes, _ in rows)


def test_the_benchmark_times_truecolor_unless_told_another_tier(bench, capsys):
    assert bench.main(["--ticks", "5", "--sizes", "80x24"]) == 0
    assert "truecolor dark on its own background" in capsys.readouterr().out.splitlines()[0]
    assert bench.main(["--ticks", "5", "--sizes", "80x24", "--color", "256", "--theme", "light"]) == 0
    assert "256 light on its own background" in capsys.readouterr().out.splitlines()[0]
    assert bench.main(["--ticks", "5", "--sizes", "80x24", "--color", "16"]) == 0
    assert "16 dark on the terminal background" in capsys.readouterr().out.splitlines()[0]


def test_the_benchmark_names_each_missed_threshold(bench, capsys, monkeypatch):
    row = {"size": "240x70", "mean": 1.6, "p95": 3.0}
    assert bench.failures((240, 70), row) == ["240x70 mean 1.60 ms > 1.5 ms"]
    assert bench.failures((120, 44), row) == []  # no threshold at that size
    monkeypatch.setattr(bench, "LIMITS", {(80, 24): {"p95": math.inf}})
    assert bench.main(["--ticks", "5", "--sizes", "80x24", "--check"]) == 0  # every threshold met
    assert "FAIL" not in capsys.readouterr().out
    monkeypatch.setattr(bench, "LIMITS", {(80, 24): {"p95": 0.0}})
    assert bench.main(["--ticks", "5", "--sizes", "80x24"]) == 0  # reports only, without --check
    assert bench.main(["--ticks", "5", "--sizes", "80x24", "--check"]) == 1
    assert capsys.readouterr().out.count("FAIL 80x24 p95") == 2
    with pytest.raises(SystemExit):
        bench.main(["--sizes", "80"])
    assert "expected COLSxROWS" in capsys.readouterr().err


def scene(sizes: list[int], changed: list[int] | None = None, kicks: dict | None = None, first: int = 100) -> dict:
    """A kick scene's figures as kick_frame_times() gives them (24 rows high)."""
    changed = changed or [0] * len(sizes)
    return dict(
        seconds=[0.001] * len(sizes), sizes=sizes, changed=changed, k=1, height=24, first=first, kicks=kicks or {}
    )


def test_a_heavy_frame_has_twice_the_median_bytes_or_changes_half_the_rows(bench):
    assert bench.heavy_frames(scene([100, 100, 201, 100, 200, 100], [0, 12, 0, 13, 0, 0])) == {2, 3}


def test_a_kick_causes_the_heavy_frames_in_its_window_that_the_effects_off_game_does_not_have(bench):
    full = scene([100] * 40, kicks={("HIT", 105): 105})  # born at tick 105: frames 4-15 are its window
    for n in (4, 5, 10, 30):
        full["sizes"][n] = 500
    off = scene([100] * 40)
    off["sizes"][5] = 500  # heavy with the effects off too: the game's own
    assert bench.caused_by_kicks(full, off) == {("HIT", 105): 2}  # frames 4 and 10; 30 is past the window


@pytest.mark.parametrize(
    "effects,reduced,heavy,bad",
    [
        ("full", False, None, ["80x24 kick: no kick admitted, so the scene proves nothing"]),  # TL-6
        ("calm", False, None, []),  # calm never kicks
        ("full", True, None, []),  # nor does reduced motion
        ("full", False, (4, 5), []),
        ("full", False, (4, 5, 6), ["80x24 kick ('HIT', 105) caused 3 heavy frames > 2"]),
    ],
)
def test_the_kick_row_fails_a_kick_with_too_many_heavy_frames_and_a_scene_with_no_kick(
    bench, monkeypatch, effects, reduced, heavy, bad
):
    def played(size, ticks, color, theme, effects, **values):
        if effects == "off" or heavy is None:
            return scene([100] * 40)
        return scene([500 if n in heavy else 100 for n in range(40)], kicks={("HIT", 105): 105})

    monkeypatch.setattr(bench, "kick_frame_times", played)
    args = argparse.Namespace(scale="auto", reduced_motion=reduced, color="auto", theme="auto", effects=effects)
    row, off, failed, note = bench.kick_row((80, 24), args)
    assert failed == bad
    assert note.endswith(
        "0 kicks admitted; heavy frames each caused: none" if heavy is None else "HIT@105: " + str(len(heavy))
    )


def test_check_fails_the_kick_scene_when_nothing_kicks(bench, capsys, monkeypatch):
    """The kick scene as the bench plays it, cut to its first HIT: it kicks, and --check passes; with the kick turned
    off where the bench plays (the link read as clock-paced), --check fails it, though every frame is cheaper."""
    monkeypatch.setattr(bench, "KICK_SIZES", ((80, 24),))
    monkeypatch.setattr(bench, "KICK_TICKS", 400)  # the scene's first HIT is born at tick 347
    monkeypatch.setattr(bench, "LIMITS", {})  # the frame times are not this test's
    command = ["--check", "--ticks", "5", "--sizes", "80x24", "--staged", "kick"]
    assert bench.main(command) == 0
    assert "80x24 kick: 1 kicks admitted" in capsys.readouterr().out
    link = fx.Link
    monkeypatch.setattr(fx, "Link", lambda remote=False, congested=False, clock=False: link(remote, congested, True))
    assert bench.main(command) == 1
    assert "FAIL 80x24 kick: no kick admitted, so the scene proves nothing" in capsys.readouterr().out


def test_a_run_is_named_for_a_rerun_only_when_more_than_the_bench_raised_the_load(bench, capsys, monkeypatch):
    """The bench's own busy process lifts the 1-minute load average as start * e^(-t/60) + 1 - e^(-t/60): 0.17 at the
    start is 0.50 after 30 s with nothing else running, which is no reason to rerun (TL-7)."""
    assert bench.own_load(0.17, 30.0) == pytest.approx(0.50, abs=0.005)
    assert "rerun" not in bench.load_note(0.17, 0.50, 30.0)
    assert "rerun" not in bench.load_note(0.49, 0.69, 30.0)
    assert "rerun" not in bench.load_note(0.15, 0.56, 40.0)
    assert "another process ran: rerun once" in bench.load_note(0.17, 0.75, 30.0)
    loads = iter([0.10, 0.95, 0.10, 0.12])  # each run: the start (under 0.5) and the end
    monkeypatch.setattr(bench.os, "getloadavg", lambda: (next(loads), 0.0, 0.0))
    for named in (True, False):
        assert bench.main(["--ticks", "5", "--sizes", "80x24", "--load-below", "0.5"]) == 0
        assert ("rerun once" in capsys.readouterr().out) is named


@gate
def test_g1_3_draw_and_encode_cost(tmp_path):
    """In a fresh interpreter without coverage's tracer: the thresholds are for an uninstrumented game."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE")}
    command = [sys.executable, str(TOOL), "--check", "--sizes", "80x24", "240x70"]
    done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    print(done.stdout)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_frame_over_two_ticks_is_excused_only_by_a_stall_of_a_tick_that_overlaps_it():
    """G1.1 as the spec owner decided it (decision 2): see support/stalls.py."""
    tick = stalls.TICK
    times = [n * tick for n in range(50)] + [(49 + 5) * tick] + [(55 + n) * tick for n in range(9)]  # frame 50: 5 ticks
    assert stalls.span(times, 50, 5) == (times[49] - tick, times[50] + tick)
    assert stalls.span(times, 0, 3) == (-4 * tick, tick)
    late = (times[49] - 2 * tick, times[49] - 0.5 * tick)  # the frame before arrived late: it is stalled too
    assert stalls.span([0.0, 0.5, 0.52], 2, 5) == (0.52 - 6 * tick, 0.52 + tick)
    long = [(50, 5, 900)]
    for stall, excused in [
        ((times[49], times[49] + 4 * tick), True),  # during the frame's ticks
        ((times[49] - 3 * tick, times[49] - 0.5 * tick), True),  # it ended within a tick of their start
        ((times[50] + 0.5 * tick, times[50] + 3 * tick), True),  # it began within a tick of the arrival
        (late, True),
        ((times[49], times[49] + 0.9 * tick), False),  # shorter than a tick
        ((times[49] - 4 * tick, times[49] - 1.5 * tick), False),  # over before the frame began
        ((times[50] + 1.5 * tick, times[50] + 4 * tick), False),  # after it arrived
    ]:
        found = stalls.excuse(long, times, [(0.0, 0.5 * tick), stall])
        assert found == ([(50, 5, 900, times[50], stall)], []) if excused else ([], [(50, 5, 900, times[50])]), stall
    assert stalls.excuse([], times, [late]) == ([], [])


def test_frame_times_are_when_each_esu_was_read():
    output = b"\x1b[?1049h" + BSU + b"one" + ESU + ACKS["dsr"] + BSU + b"two" + ESU + ACKS["dsr"] + BSU + b"3" + ESU
    first, second = output.index(ESU) + len(ESU), output.rindex(BSU) + 2  # the last ESU arrived in two reads
    arrivals = [(1.0, first - 1), (1.5, first + 3), (2.0, second), (2.5, len(output))]
    assert stalls.esu_times(output, arrivals) == [1.5, 2.0, 2.5]
    assert stalls.esu_times(b"before any frame", [(1.0, 16)]) == []


def test_the_sleeper_reports_only_naps_that_overran_by_a_tick():
    sleeper = stalls.Sleeper(0.3)
    found = sleeper.stalls()
    assert [process.returncode for process in sleeper.processes] == [0] * len(sleeper.cpus)
    assert all(end - start >= stalls.STALL for start, end in found) and len(sleeper.seen_by) == len(found)
    sleeper.stop()  # once they are done, stop does nothing
    sleeper = stalls.Sleeper(30.0)
    sleeper.stop()
    assert all(process.returncode is not None for process in sleeper.processes)


class Finished:
    """A sleeper process that has already printed its stalls."""

    def __init__(self, out: str):
        self.out, self.returncode = out, 0

    def communicate(self):
        return self.out, None

    def poll(self):
        return self.returncode


class Tapped:
    """A running sleeper process whose stalls the test reads as the sleeper prints them; stalls() then gets them with
    the rest of its output, as if nothing had read them."""

    def __init__(self, process: subprocess.Popen):
        self.process, self.out = process, ""

    def __getattr__(self, name):  # pid, poll and kill are the process's own
        return getattr(self.process, name)

    def seen(self) -> list[tuple[float, float]]:
        """The stalls it has printed so far, as (start, end); this never waits for more."""
        fd = self.process.stdout.fileno()
        while select.select([fd], [], [], 0)[0] and (chunk := os.read(fd, 4096)):
            self.out += chunk.decode()
        lines = self.out[: self.out.rfind("\n") + 1].splitlines()  # whole lines only
        return [(float(start), float(end)) for start, end in map(str.split, lines)]

    def communicate(self):
        return self.out + self.process.communicate()[0], None


def pause(process, seconds: float) -> float:
    """Stop the process for `seconds`, as a host that stalls its vCPU would; when it was stopped."""
    os.kill(process.pid, signal.SIGSTOP)
    stopped = time.monotonic()
    time.sleep(seconds)
    os.kill(process.pid, signal.SIGCONT)
    return stopped


def test_there_is_a_sleeper_on_every_cpu_and_a_stall_only_one_of_them_saw_is_kept():
    """A virtual machine's host can stall one vCPU and not the others, and a sleeper sees only the vCPU it runs on
    (decision 2's detector, as verified: one unpinned sleeper missed such a stall, and G1.1 failed on the frame it
    delayed). So there is a sleeper per CPU, each pinned to its own, and their stalls are merged where they overlap.
    A sleeper stopped for 100 ms stands in for a stall of its vCPU alone."""
    merging = stalls.Sleeper.__new__(stalls.Sleeper)
    merging.cpus = [0, 1, 2]
    merging.processes = [Finished("1.000 1.050\n3.000 3.020\n"), Finished("1.010 1.060\n"), Finished("")]
    assert merging.stalls() == [(1.0, 1.06), (3.0, 3.02)] and merging.seen_by == [[0, 1], [0]]
    merging.processes = [Finished("1.000 1.050\n"), Finished("1.040 1.070\n1.065 1.090\n"), Finished("")]
    assert merging.stalls() == [(1.0, 1.09)] and merging.seen_by == [[0, 1]]  # a CPU is listed once
    sleeper = stalls.Sleeper(30.0)  # longer than all the waits below together: they are ended once the stall is in
    try:
        assert sleeper.cpus == stalls.cpus() and len(sleeper.processes) == len(sleeper.cpus)
        if hasattr(os, "sched_setaffinity"):
            assert sleeper.cpus == sorted(os.sched_getaffinity(0))
            deadline = time.monotonic() + 5.0
            while [os.sched_getaffinity(process.pid) for process in sleeper.processes] != [{n} for n in sleeper.cpus]:
                assert time.monotonic() < deadline, "each sleeper pins itself to its own CPU"
                time.sleep(0.01)
        # A sleeper measures nothing until Python has started and it is in its loop, 20-50 ms after it was started
        # and 300 ms on a loaded machine: a stop before then goes unseen. Its pinning above shows it got that far only
        # where pinning changed something (not when this process has one CPU, nor on macOS, which has none), and it
        # prints nothing but stalls, each as soon as it has measured it (flush=True). So it is stopped for 100 ms, as
        # often as it takes, until it has printed a stall (one it measured by itself counts too): then it is in its
        # loop. UP is a deadline only.
        last = sleeper.processes[-1] = Tapped(sleeper.processes[-1])
        deadline = time.monotonic() + UP
        while not last.seen():
            assert time.monotonic() < deadline, "a sleeper stopped for 100 ms prints that stall"
            pause(last, 0.1)
            until(last.seen, 0.5)
        # Then the stop the test measures, once the sleeper has had a moment to get back to its naps after printing.
        # There is one, as before: stopping it again for each stop it missed would pass a sleeper that sees only some
        # of them (one whose naps are too long, say). A sleeper that works sees it wherever it lands, as it is printing
        # too (each nap is timed from the end of the last), which matters on macOS: there the test's wake-up and the
        # sleeper's can come together. The sleepers are ended once the last one has printed the stall that covers this
        # stop, or after UP if it never does: that is all the waiting there is.
        time.sleep(0.1)
        stopped = pause(last, 0.1)
        until(lambda: any(start < stopped + 0.1 < end for start, end in last.seen()), UP)
        for process in sleeper.processes:
            process.kill()
        found = sleeper.stalls()
    finally:
        sleeper.stop()
    hits = [(end - start, cpus) for (start, end), cpus in zip(found, sleeper.seen_by) if start < stopped + 0.1 < end]
    assert any(length >= 0.08 and sleeper.cpus[-1] in cpus for length, cpus in hits), (stopped, found, sleeper.seen_by)


def test_the_stub_process_answers_as_the_fixture_and_keeps_every_byte(tmp_path):
    """IsolatedPty, as the G1 rows use it: a short kitty run, then what Pty.finish() checks."""
    tape = str(tmp_path / "recording.json")
    terminal = IsolatedPty(tmp_path, "--quickstart", "--seed", "42", "--debug", "--record", tape, fixture="ghostty")
    try:
        assert terminal.stub.process.poll() is None and terminal.stub.process.pid != os.getpid()
        assert terminal.wait_for("SCORE", 5.0), terminal.screen()
        assert terminal.stub.vt.kitty["alt"]  # the stub process answered the push verification, as the fixture does
        terminal.tap("d", "space")
        # Each frame's ack is answered at once, as the fixture does. A frame whose ack goes unanswered, or whose reply
        # names no frame in flight, holds its slot of the window for the pacer's loss timeout, a second at least, and
        # three losses in a row put the frames on the clock (pacing.py): frames that go on prove nothing by
        # themselves, and how fast they come is the machine's (one whose timers overshoot draws fewer a second). So
        # the game makes 30 ack queries, with UP as a deadline only, and then its --debug log shows the acks kept up.
        assert until(lambda: terminal.output().count(b"\x1b[6n") > 30, UP), terminal.output().count(b"\x1b[6n")
        leave(terminal)
        assert terminal.finish() == 0
        frames, arrivals = terminal.frames(), terminal.stub.arrivals
        assert len(frames) > 20 and terminal.screen_hashes() == [frame[5] for frame in frames]
        assert arrivals[-1][1] == len(terminal.output()) and arrivals == sorted(arrivals)
        assert len(stalls.esu_times(terminal.output(), arrivals)) == len(frames)
        # No frame covers half a second's ticks of play: the one a lost ack holds back covers a second's, while the game
        # draws as soon as the window lets it, and a slow machine or a stall adds 8 ticks at most each time the game
        # looks at the clock (ui.FixedClock). The pacer never gave up on the acks (app.Meter logs it), and the smoothed
        # round trip it logs once a second (srtt) was under 0.1 s at least once: a few ms, on a loaded machine too
        # (14 ms at most with the CPU shared five ways), and as late as the stub's answers if they come late.
        assert max(frame[2] for frame in frames) < TICK_RATE / 2, [frame[2] for frame in frames]
        log = terminal.log.read_text()
        srtts = [float(srtt) for srtt in re.findall(r"Loop .*, srtt (\S+)$", log, re.M) if srtt != "None"]
        assert "Acks lost: clock pacing" not in log and any(srtt < 0.1 for srtt in srtts), log[-3000:]
    finally:
        terminal.close()
    assert terminal.stub.process.returncode == 0 and not terminal.stub.directory.exists()
    assert b"\x1b[?1049l" in terminal.stub.output  # what it read is kept once its files are gone


def bot_keys(bench, seconds: float) -> list[tuple[float, bytes]]:
    """A Skilled bot's Campaign (easy, seed 42) as the kitty key events of a person holding the keys it wants
    (VirtualInput, GNOME repeat). The bot plays a copy of the run: the game on the pty takes the same keys but drifts
    from it, so it plays blind, and on easy it still outlives the minute."""
    played, vin = Game(Config("easy", seed=42)), VirtualInput("kitty", PROFILES["gnome"])
    bot, driver = bench.Skilled(played.difficulty, 42), IntentDriver("hold", vin)
    keys = []
    for tick in range(round(seconds * TICK_RATE)):
        actions = bot.act(bench.PublicView(played))
        driver.intend(tick, frozenset(actions) & {"left", "right", "fire"})
        if "bomb" in actions:
            driver.pulse("bomb", tick)
        played.step(actions)
        keys += vin.bytes_until((tick + 1) / TICK_RATE)
    for name in ("a", "d", "space"):
        vin.release(name, seconds)
    return keys + vin.bytes_until(seconds + 1.0)


def frame_count(terminal) -> int:
    return len(terminal.frames())


@gate
@pytest.mark.parametrize("size", [(80, 24), (240, 70)], ids=["80x24-k1", "240x70-k3"])
@pytest.mark.parametrize("fixture", ["ghostty", "kitty"])
def test_g1_1_and_g1_2_sixty_frames_a_second_without_tearing(tmp_path, bench, fixture, size):
    """The terminal stub answers from a process of its own, as the fixture would (IsolatedPty). Since the spec
    owner's decision 2, G1.1's "no frame over 2 ticks after the first second" excuses a frame over 2 ticks that
    coincides with a host stall of at least a tick, as sleeper processes beside the game measure it (support/
    stalls.py: one sleeper per CPU, as a host can stall one vCPU alone); such frames are reported. A frame over 2
    ticks with no stall measured still fails, and the frame count and the one-tick share are unchanged."""
    keys = bot_keys(bench, RUN)
    tape = str(tmp_path / "recording.json")
    arguments = ("--quickstart", "--seed", "42", "--difficulty", "easy", "--debug", "--record", tape)
    terminal = IsolatedPty(tmp_path, *arguments, fixture=fixture, size=size)
    sleeper = None
    try:
        assert terminal.wait_for("SCORE", 5.0), terminal.screen()
        sleeper = stalls.Sleeper(RUN + 1.0)
        start, marks = time.monotonic(), [frame_count(terminal)]
        for at, data in sorted(keys + [(1.0, b"")], key=lambda key: key[0]):  # b"": the end of the first second
            time.sleep(max(0.0, start + at - time.monotonic()))
            if data:
                terminal.send(data)
            else:
                marks.append(frame_count(terminal))
        time.sleep(max(0.0, start + RUN - time.monotonic()))
        marks.append(frame_count(terminal))
        leave(terminal)
        assert terminal.finish() == 0
        stalled = sleeper.stalls()
        frames = terminal.frames()
        # G1.2: every frame in one BSU ... ESU with only acks after it, and each ESU shows its own frame's cells
        output = terminal.output()
        packets = output[output.index(BSU) :].split(BSU)[1:]
        assert len(packets) == len(frames)
        for n, packet in enumerate(packets):
            body, end, between = packet.partition(ESU)
            last = n == len(packets) - 1  # the teardown follows the last packet
            assert end and (last or ACK.fullmatch(between)), packet[-200:]
        assert terminal.screen_hashes() == [frame[5] for frame in frames]
        # G1.1: 60 frames a second, one tick each
        played = frames[marks[0] : marks[2]]
        ticks = [frame[2] for frame in played]
        late = [(n, frame[2], frame[3]) for n, frame in enumerate(played) if frame[2] != 1]  # (frame, ticks, bytes)
        times = stalls.esu_times(output, terminal.stub.arrivals)
        long = [(n, frames[n][2], frames[n][3]) for n in range(marks[1], marks[2]) if frames[n][2] > 2]
        excused, failed = stalls.excuse(long, times, stalled)

        def into(when: float) -> float:
            return round(when - start, 2)

        # frames are counted from the start of the run, and times in s into it
        report = f"{len(played)} frames in {RUN:.0f} s, {ticks.count(1)} with 1 tick; others {late[:20]}"
        seen = [(into(a), round(1000 * (b - a)), cpus) for (a, b), cpus in zip(stalled, sleeper.seen_by)]
        report += f"; host stalls {seen[:20]} (s, ms, the CPUs whose sleepers saw it)"
        excuses = [(n - marks[0], k, b, into(w), (into(x), into(y))) for n, k, b, w, (x, y) in excused]
        report += f"; over 2 ticks, excused by a stall (frame, ticks, bytes, s, stall) {excuses}"
        report += f"; over 2 ticks, no stall {[(n - marks[0], k, b, into(w)) for n, k, b, w in failed]}"
        print(report)
        assert {frame[:2] for frame in played} == {size}
        assert len(played) >= 59.4 * RUN, report
        assert ticks.count(1) >= 0.99 * len(played), report
        assert failed == [], report
    finally:
        if sleeper:
            sleeper.stop()
        terminal.close()


@pytest.mark.gate
@pytest.mark.parametrize("size", [(80, 24), (240, 70)], ids=["80x24", "240x70"])
@pytest.mark.parametrize("fixture", ["apple-terminal-15", "vte-gnome"])
def test_g1_4_without_sync_every_frame_is_one_diffed_packet_in_at_most_two_writes(fixture, size):
    doc, (cols, rows) = load(fixture), size
    fake = FakeTerminal(doc, cols=cols, rows=rows, vt=size)
    current = session()
    for n, key in enumerate(b"fdddddfaaaaaab" * 4):
        fake.type(bytes([key]), at=0.5 + 0.1 * n)
    packets, fulls, writes = [], [], []
    push, frame, write = OutputQueue.push, app.Renderer.frame, fake.write

    def pushed(queue, data, *, frame=False):
        if frame:  # where the packet will land in the terminal's output, and its length
            packets.append((len(fake.output) + queue.pending(), len(data)))
        push(queue, data, frame=frame)

    def built(renderer, *args):
        data = frame(renderer, *args)
        fulls.append(renderer.last_full)
        return data

    def written(fd, data):
        start = len(fake.output)
        writes.append((start, write(fd, data)))
        return writes[-1][1]

    fake.write = written
    term = TerminalSession(fake.fd, EnvHints.from_env(doc["env"]), app.overrides(current.options), ops=fake.ops())
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(OutputQueue, "push", pushed)
        patched.setattr(app.Renderer, "frame", built)
        renderer, _ = play(fake, current, term, 7.0)
    assert not renderer.caps.sync and current.pilot.game.tick > 6 * TICK_RATE
    assert renderer.painter.layout.k == (3 if cols == 240 else 1)
    assert len(packets) == len(fulls) == renderer.frames > 6 * 60  # one packet per frame
    assert fulls[0] and not any(fulls[1:])  # the first frame paints the screen, every later one is a diff
    for start, length in packets:
        assert BSU not in fake.output[start : start + length]
        count = sum(1 for at, n in writes if at < start + length and start < at + n)
        assert 1 <= count <= (2 if start > packets[0][0] else -(-length // OutputQueue.START_CHUNK)), (start, count)
