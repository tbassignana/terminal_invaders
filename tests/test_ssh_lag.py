"""G4 (spec 13): under 100 ms of lag on a 2 Mbit SSH link, with the real binary behind tests/support/relay.py.

The link model and a short run behind it are part of every test run: the run's numbers on a FakeTerminal's virtual
clock behind the same link model (VirtualLink), and the binary behind the relay. The gate rows take a minute each
and run with pytest -m gate (Linux only). Key to photon is the time from a key leaving the terminal to the first
frame whose stats line counts it; ack RTT is a frame's ack query (a tagged CPR) leaving the game to its reply
arriving there. Both are reported in excess of the base RTT, from 10 s into the run.
"""

import random
import re
import socket
import sys
import time

import pytest

from tests.support.relay import PACKET, QUERY, REPLIES, REPLY, Counter, Pipe, Relay, RelayPty, VirtualLink
from tests.support.terminals import load
from tests.test_terminal import GUARD, leave, main_on_a_virtual_clock, watching

FIXTURE = "ssh-iterm2"  # SSH_* set, so the game knows it is remote; kitty keys, CPR (tagged) acks, synchronized output
KEYS = ("space", "a", "d", "space")  # fire and step: a Campaign on easy outlives the run
PLAY = ("--quickstart", "--seed", "42", "--difficulty", "easy", "--show-fps")
RUN, WARMUP = 60.0, 10.0
SIZES = [(80, 24), (120, 44), (240, 70)]
STATS = re.compile(r"Stats: FPS \S+  RTT \S+  (W [\d.]+|W fix|ACK OFF)  ")


def gate(test):
    """A gate row: selected by pytest -m gate, left out of a plain run (slow), and only on Linux CI."""
    for mark in (pytest.mark.gate, pytest.mark.slow, pytest.mark.skipif(sys.platform != "linux", reason="Linux")):
        test = mark(test)
    return test


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def taps(start: float, seconds: float):
    """(when, key) for taps at random 150-350 ms intervals from start, for that many seconds."""
    rng, at, end = random.Random(1978), start, start + seconds
    while (at := at + rng.uniform(0.15, 0.35)) < end:
        yield at, rng.choice(KEYS)


def slope(samples: list[tuple[float, float]]) -> float:
    """Least-squares slope of value over time."""
    n = len(samples)
    mean_t, mean_v = sum(t for t, _ in samples) / n, sum(v for _, v in samples) / n
    spread = sum((t - mean_t) ** 2 for t, _ in samples)
    return sum((t - mean_t) * (v - mean_v) for t, v in samples) / spread


class Run:
    """One game behind the link: taps at random 150-350 ms intervals, then the measurements."""

    def __init__(self, directory, rtt, *, size=(80, 24), seconds=RUN, warmup=WARMUP, env=None, events=()):
        self.rtt, self.seconds, self.warmup = rtt, seconds, warmup
        arguments = (*PLAY, "--debug", "--record", str(directory / "recording.json"))
        self.terminal = RelayPty(directory, *arguments, fixture=FIXTURE, size=size, rtt=rtt, env=env)
        try:
            assert self.terminal.wait_for("RTT ", 5.0), self.terminal.screen()
            self.start = time.monotonic()
            self.drive(sorted(events, key=lambda event: event[0]))
            leave(self.terminal)
            self.code = self.terminal.finish()
        finally:
            self.terminal.close()

    def drive(self, events) -> None:
        for at, key in taps(self.start, self.seconds):
            while events and self.start + events[0][0] <= at:
                when, action = events.pop(0)
                time.sleep(max(0.0, self.start + when - time.monotonic()))
                action(self.terminal)
            time.sleep(max(0.0, at - time.monotonic()))
            self.terminal.tap(key)
        time.sleep(max(0.0, self.start + self.seconds - time.monotonic()))

    def measured(self, samples, since=None, until=None) -> list[tuple[float, float]]:
        """Samples from `since` s into the run on, as (seconds into the run, excess over the base RTT)."""
        since, until = self.warmup if since is None else since, self.seconds if until is None else until
        return [(t - self.start, v - self.rtt) for t, v in samples if since <= t - self.start < until]

    @property
    def link(self):
        """What timed the link's acks: the relay."""
        return self.terminal.relay

    @property
    def photons(self) -> list[tuple[float, int]]:
        return self.terminal.stub.photons

    @property
    def lags(self):
        return self.measured(self.terminal.lags())

    @property
    def acks(self):
        return self.measured(self.link.ack_rtts())

    @property
    def fps(self) -> float:
        frames = [when for when, _ in self.photons if self.warmup <= when - self.start < self.seconds]
        return len(frames) / (self.seconds - self.warmup)

    def windows(self) -> set[str]:
        return set(STATS.findall(self.terminal.log.read_text()))

    def report(self) -> str:
        lags, acks = [v for _, v in self.lags], [v for _, v in self.acks]
        return (
            f"rtt {self.rtt * 1000:.0f}ms: key to photon excess p95 {p95(lags) * 1000:.0f}ms ({len(lags)} keys),"
            f" ack excess p95 {p95(acks) * 1000:.0f}ms ({len(acks)} acks), {self.fps:.1f} fps"
        )

    def check(self, *, lag=0.100, ack=0.100) -> None:
        assert self.code == 0
        taps = [sent for sent, _ in self.terminal.taps if self.warmup <= sent - self.start < self.seconds]
        assert len(self.lags) == len(taps), "every key reached the screen"
        report = self.report()
        print(report)
        assert p95([v for _, v in self.lags]) < lag, report
        assert p95([v for _, v in self.acks]) < ack, report


class VirtualRun(Run):
    """The same run on a FakeTerminal's virtual clock behind the same link model (VirtualLink): cli.main() as the pty
    runs the binary, but without --debug, the same taps from 1 s on (the game is up by then: its probe crosses the
    link in about 0.2 s), then leave()'s keys, and the same measurements, exact however loaded the machine is. That
    clock counts neither a wait around the terminal, which is kept in `unclocked` (watching), nor the game's work,
    which is kept as `cpu`: this thread's CPU time over the run, the game's and the fake terminal's, which a loaded
    machine does not add to as it adds to the time on a real clock."""

    def __init__(self, directory, rtt, *, size=(80, 24), seconds=RUN, warmup=WARMUP):
        self.rtt, self.seconds, self.warmup, self.start = rtt, seconds, warmup, 1.0
        doc = load(FIXTURE)
        self.terminal = link = VirtualLink(doc, rtt, cols=size[0], rows=size[1])
        end = self.start + seconds
        leaving = [(end, "p"), (end + 0.5, "q"), (end + 1.0, "escape"), (end + 1.0 + GUARD, "escape")]  # leave()'s
        for at, key in [*taps(self.start, seconds), *leaving]:
            link.at(at, lambda key=key: link.tap(key))
        tape = str(directory / "recording.json")
        self.unclocked: list[tuple[str, float | None]] = []
        began = time.thread_time()
        with watching(self.unclocked):
            self.code, _ = main_on_a_virtual_clock(directory, doc, link, *PLAY, "--record", tape)
        self.cpu = time.thread_time() - began
        assert link.photons[0][0] < self.start, "the stats line was not up when the taps began"

    @property
    def link(self) -> VirtualLink:
        return self.terminal

    @property
    def photons(self) -> list[tuple[float, int]]:
        return self.terminal.photons

    def windows(self) -> set[str]:
        return self.terminal.windows


def opened(run: Run) -> set[float]:
    """The pacing windows the stats line showed, as numbers (not W fix or ACK OFF)."""
    return {float(window[2:]) for window in run.windows() if window[2:3].isdigit()}


def test_a_pipe_serialises_in_order_then_delays_and_upstream_waits_for_its_slot():
    down = Pipe(1000, 0.05)
    down.offer(b"x" * 500, 1.0)
    down.offer(b"y" * 100, 1.1)  # queued behind the first
    assert down.due() == pytest.approx(1.55)
    assert down.take(1.6) == b"x" * 500
    assert down.take(1.66) == b"y" * 100 and down.due() is None
    big = Pipe(10**6, 0.0)
    big.offer(b"z" * 3000, 0.0)
    assert [len(chunk) for _, chunk in big.queue] == [PACKET, PACKET, 3000 - 2 * PACKET]
    up = Pipe(10**6, 0.01, slot=0.02)
    up.offer(b"k", 1.005)
    assert up.due() == pytest.approx(1.02 + 1e-6 + 0.01)


def test_markers_are_counted_across_pieces():
    counter = Counter(QUERY)
    assert [counter.count(piece) for piece in (b"ab\x1b[", b"6nc\x1b[6n\x1b", b"[6", b"n")] == [0, 2, 0, 1]
    replies = Counter(REPLIES, longest=16)
    assert [replies.count(piece) for piece in (b"\x1b[12;", b"34Rx\x1b[5;7R\x1b[", b"4;2", b"R")] == [0, 2, 0, 1]


def test_the_relay_carries_both_ways_on_time_and_times_the_ack():
    game, master = socket.socketpair()  # a socket stands in for the pty master
    relay = Relay(master.fileno(), 0.04)
    relay.start()
    try:
        sent = time.monotonic()
        game.sendall(b"frame" + QUERY)
        assert relay.outer.recv(100) == b"frame" + QUERY
        arrived = time.monotonic()
        relay.outer.sendall(REPLY)
        assert game.recv(100) == REPLY
        back = time.monotonic()
        assert arrived - sent >= 0.02 and back - sent >= 0.04
        ((left, rtt),) = relay.ack_rtts()
        assert left >= sent and 0.04 <= rtt <= back - sent
        assert relay.idle(0.1)
    finally:
        relay.stop()
        relay.close()
        game.close()
        master.close()


def test_a_short_run_behind_the_link_opens_the_window_and_keeps_lag_down(tmp_path):
    """A smoke run, 6 s behind a 50 ms link: the game's adaptive pacing opens its window, every ack comes back, and
    key to photon and the ack round trip stay within 200 ms of the base RTT (16 keys make a noisy p95, and the gate
    rows hold the 100 ms).

    Those two numbers are the game's pacing at work, but on a loaded machine they are also the late wakes of the
    relay, which steps every 10 ms or less, of the stub and of the game, all of them in the path of every key and
    every ack: on one core shared with two busy loops, with Linux's timer slack at 150 ms standing in for macOS's late
    timers, key to photon came to 600 ms and acks to 450 ms. So they are taken where they are exact: the same game,
    cli.main(), behind the same link model on a FakeTerminal's virtual clock, with the same taps. What that clock does
    not count, a wait around the terminal and the game's own work, is held there too, the work by its CPU time. The
    binary then runs behind the relay, where every key still has to reach the screen, every ack come back and the
    window open, and its lag is held only to a guard against a hang."""
    virtual = VirtualRun(tmp_path / "virtual", 0.05, seconds=6.0, warmup=2.0)
    virtual.check(lag=0.200, ack=0.200)
    assert max(opened(virtual)) > 2.0  # adaptive by default, and its window grew past the floor
    assert len(virtual.link.sent) == len(virtual.link.answered)  # every ack came back
    assert virtual.unclocked == []  # and the game waited only on the terminal: a sleep would add to every key's lag
    # Nor did it work long on a frame: frames of 60 ms of work took key to photon to 200 ms on a real clock. A frame
    # takes 1 to 4.5 ms of CPU here (3.11 under coverage on a loaded core, the fake terminal's work included): 50 ms
    # is a guard more than ten times that.
    assert virtual.cpu / len(virtual.photons) < 0.050, (virtual.cpu, len(virtual.photons))

    run = Run(tmp_path, 0.05, seconds=6.0, warmup=2.0)
    run.check(lag=2.0, ack=1.5)  # over three times the most measured there (0.6 s and 0.45 s)
    assert max(opened(run)) > 2.0
    assert len(run.link.sent) == len(run.link.answered)


@gate
@pytest.mark.parametrize("size", SIZES, ids=lambda size: "%dx%d" % size)
@pytest.mark.parametrize("rtt", [0.020, 0.050, 0.100], ids=["G4.1-20ms", "G4.2-50ms", "G4.3-100ms"])
def test_g4_campaign_lag_stays_under_100_ms(tmp_path, rtt, size):
    run = Run(tmp_path, rtt, size=size)
    run.check()
    if rtt == 0.100 and size == (80, 24):
        assert run.fps >= 55, run.report()


@gate
def test_g4_4_stress_frames_keep_the_queue_bounded(tmp_path):
    run = Run(tmp_path, 0.050, env={"INVADERS_FAULT": "stress:6500"})
    acks = run.acks
    late = [(t, v) for t, v in acks if t >= RUN - 40]
    report = run.report() + f", slope over the last 40 s {slope(late) * 1000:.2f} ms/s"
    print(report)
    assert run.code == 0
    assert p95([v for _, v in acks]) < 0.150, report
    assert slope(late) < 0.001, report  # no upward trend
    assert run.fps > 20, report


@gate
def test_g4_5_a_full_repaint_mid_run_settles_within_two_seconds(tmp_path):
    resize_at = 30.0
    events = [(resize_at, lambda terminal: terminal.resize(240, 70)), (resize_at + 0.4, lambda t: t.tap("p"))]
    run = Run(tmp_path, 0.050, events=events)
    assert run.code == 0
    settled = run.measured(run.terminal.lags(), since=resize_at + 2.0)
    spike = run.measured(run.terminal.lags(), since=resize_at, until=resize_at + 2.0)
    report = run.report() + f"; after the repaint: max {max(v for _, v in spike) * 1000:.0f}ms within 2 s,"
    report += f" p95 {p95([v for _, v in settled]) * 1000:.0f}ms after"
    print(report)
    assert p95([v for _, v in settled]) < 0.100, report
    assert any(frame[:2] == (240, 70) and frame[4] for frame in run.terminal.frames())  # it did repaint in full
