"""AckPacer fixed, adaptive (deviation D1) and off modes (spec 8.3, 8.4), which frame a reply answers (CPR tags, or in
order), the game's own waits, and the pace_sim regression table."""

import importlib.util
import io
import math
import sys
import unittest
from collections import deque
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from terminal_invaders.term.encode import ack_tag
from terminal_invaders.term.pacing import (
    BACKOFF_MAX,
    CAP,
    FLOOR,
    HOLD_MAX,
    HOLD_MIN,
    JUMP_MIN,
    TICK,
    AckPacer,
)

TOOL = Path(__file__).resolve().parents[2] / "tools" / "pace_sim.py"
TAGS = [ack_tag(seq, 80, 24) for seq in range(8)]  # the renderer's first tags at 80x24
OFF = ack_tag(None, 80, 24)  # the tag of a frame sent while clock pacing is on


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class Link:
    """Offers the pacer a frame every tick, acks each sent frame rtt later, in order, and expires frames as the loop
    does. A tagged pacer's frames carry the renderer's tags at 80x24, and each ack names its frame.

    In an outage (down, up) the line carries nothing: every reply due meanwhile comes at `up`, `spread` s apart (a
    backlog draining), ahead of the later ones. The replies to the frames sent at a time `lost(t)` never come."""

    def __init__(self, pacer, *, outages=(), spread=0.0, lost=lambda t: False):
        self.pacer, self.ticks, self.acks, self.seq = pacer, 0, deque(), 0
        self.outages, self.spread, self.lost, self.held = outages, spread, lost, {}

    @property
    def now(self):
        return self.ticks * TICK

    def run(self, rtt, secs, size=100):
        """Returns (ack time, window) after every ack; size is in bytes, or a function of the send time."""
        seen = []
        for _ in range(round(secs / TICK)):
            t = self.now
            while self.acks and self.acks[0][0] <= t:
                at, tag = self.acks.popleft()
                self.pacer.acked(at, tag)
                seen.append((at, self.pacer.window))
            self.pacer.expire(t)
            if self.pacer.ready(t):
                self.send(t, rtt, size(t) if callable(size) else size)
            self.ticks += 1
        return seen

    def send(self, t, rtt, nbytes):
        tag = None
        if self.pacer.tagged:  # as Renderer.frame does
            tag = ack_tag(None if self.pacer.disabled else self.seq, 80, 24)
            self.seq += not self.pacer.disabled
        self.pacer.sent(t, nbytes, full=False, tag=tag)
        if self.lost(t):
            return
        due = t + rtt
        up = next((up for down, up in self.outages if down < due < up), None)
        if up is not None:
            due = up + self.spread * self.held.get(up, 0)
            self.held[up] = self.held.get(up, 0) + 1
        self.acks.append((max(due, self.acks[-1][0] if self.acks else 0.0), tag))


def per_second(seen, start, end):
    """Acks per second from start to end: the frame rate the window allowed."""
    return sum(start <= t < end for t, _ in seen) / (end - start)


def assert_in_step(test, pacer, link):
    """The pacer waits on exactly the replies still to come: each frame in flight has its own, in order, and no reply
    is owed that will not come."""
    test.assertEqual([frame.tag for frame in pacer.inflight], [tag for _, tag in link.acks])
    test.assertEqual((pacer.owed, len(pacer.late), pacer.disabled), (len(link.acks), 0, False))


def lose_three(pacer, tags=(None, None, None)):
    """Three frames sent together, each lost a second later: clock pacing, with the timeout backed off once."""
    for n, tag in enumerate(tags):
        pacer.sent(n * 0.01, 100, full=False, tag=tag)
    pacer.expire(1.05)


def spanned_probe():
    """A tagged pacer on a 100 ms link whose probe was in flight when clock pacing took over: (pacer, t)."""
    pacer, t = AckPacer("adaptive", tagged=True), 0.0
    for n in range(20):
        t = exchange(pacer, t, 0.1, tag=TAGS[n % 8])
    pacer.sent(t, 100, full=False, tag=TAGS[1])
    pacer.expire(t + 1.0)
    for n, tag in enumerate(TAGS[2:5]):
        pacer.sent(t + 1.0 + n * 0.01, 100, full=False, tag=tag)  # the probe (2 s), then two frames
    pacer.expire(t + 2.02)
    return pacer, t


def bottleneck(pacer, rtt, rate, size, secs):
    """A frame offered every tick, tagged as Renderer.frame does, through one FIFO bottleneck of `rate` B/s; its reply
    comes rtt after its last byte, in order, and none is lost. Returns (spells of clock pacing, the longest a frame
    queued at the bottleneck)."""
    acks, free, seq, spells, was, worst = deque(), 0.0, 0, 0, False, 0.0
    for n in range(round(secs / TICK)):
        t = n * TICK
        while acks and acks[0][0] <= t:
            pacer.acked(*acks.popleft())
        pacer.expire(t)
        spells += pacer.disabled and not was
        was = pacer.disabled
        if pacer.ready(t):
            tag = ack_tag(None if pacer.disabled else seq, 80, 24)
            seq += not pacer.disabled
            pacer.sent(t, size, full=False, tag=tag)
            worst = max(worst, free - t)
            free = max(free, t) + size / rate
            acks.append((free + rtt, tag))
    return spells, worst


def drive(pacer, rtt, *, secs=5.0, size=100):
    return Link(pacer).run(rtt, secs, size)


def exchange(pacer, t, rtt, size=100, tag=None):
    """One frame sent at t and acked rtt later, with nothing else in flight."""
    pacer.sent(t, size, full=False, tag=tag)
    pacer.acked(t + rtt, tag)
    return t + rtt


def load_tool():
    spec = importlib.util.spec_from_file_location("pace_sim_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def settled(clock=None, rtt=0.02):
    """A pacer with base RTT rtt (20 ms) and its window opened to 6."""
    pacer = AckPacer("adaptive", clock=clock or Clock())
    t = 0.0
    for _ in range(12):
        exchange(pacer, t, rtt)
        t += TICK
    pacer.window = 6.0
    return pacer, t


def sprites(t):
    """80x24 Campaign play: a 1.3 KB sprite step every fourth frame, unchanged packets (sync and the ack) between."""
    return 1300 if round(t / TICK) % 4 == 0 else 28


class ModeTests(unittest.TestCase):
    def test_fixed_keeps_at_most_two_frames_in_flight(self):
        pacer = AckPacer("fixed")
        self.assertTrue(pacer.ready(0.0))
        pacer.sent(0.0, 500, full=True)
        self.assertTrue(pacer.ready(0.01))
        pacer.sent(0.01, 500, full=False)
        self.assertFalse(pacer.ready(0.02))
        pacer.acked(0.03)
        self.assertTrue(pacer.ready(0.03))
        self.assertAlmostEqual(pacer.srtt, 0.03)

    def test_fixed_never_widens_on_a_long_link(self):
        pacer = AckPacer("fixed")
        seen = drive(pacer, 0.105, secs=5.0)
        self.assertEqual({window for _, window in seen}, {2.0})
        self.assertAlmostEqual(len(seen) / 5.0, 2 / 0.117, delta=1.0)  # throughput is capped near 2/RTT

    def test_off_is_always_ready_and_tracks_nothing(self):
        pacer = AckPacer("off")
        for n in range(10):
            pacer.sent(n * TICK, 1000, full=False)
            self.assertTrue(pacer.ready(n * TICK))
        pacer.acked(1.0)
        pacer.expire(100.0)
        self.assertEqual((pacer.deadline(), pacer.srtt, pacer.disabled), (None, None, False))

    def test_auto_means_adaptive_and_unknown_modes_fail(self):
        self.assertEqual(AckPacer("auto").mode, "adaptive")
        with self.assertRaises(ValueError):
            AckPacer("fast")

    def test_unread_counts_what_follows_an_unacknowledged_frame(self):
        for mode in ("fixed", "adaptive"):
            pacer = AckPacer(mode)
            pacer.sent(0.0, 9000, full=False)
            self.assertIsNone(pacer.unread(0))  # only the newest frame: the queue's own chunk rule
            pacer.sent(0.01, 8000, full=False)
            self.assertEqual((pacer.inflight_bytes, pacer.unread(8000), pacer.unread(3000)), (17000, 9000, 14000))
            pacer.acked(0.2)
            self.assertIsNone(pacer.unread(0))
        off = AckPacer("off")
        off.sent(0.0, 9000, full=False)
        off.sent(0.01, 9000, full=False)
        self.assertIsNone(off.unread(0))  # without acks nothing is known

    def test_a_stray_ack_is_ignored(self):
        pacer = AckPacer("adaptive")
        pacer.acked(1.0)
        self.assertIsNone(pacer.srtt)
        self.assertEqual(pacer.window, FLOOR)

    def test_a_tagged_pacer_tracks_only_frames_that_carry_a_tag(self):
        """Without a tag (fewer than two tags fit: a screen under 5 rows, ack_tag) no ack query went out, so nothing
        waits on its reply."""
        pacer = AckPacer("adaptive", tagged=True)
        pacer.sent(0.0, 100, full=False)
        self.assertEqual((pacer.owed, len(pacer.inflight), pacer.peak), (0, 0, None))
        pacer.sent(0.1, 100, full=False, tag=TAGS[0])
        self.assertEqual((pacer.owed, [frame.tag for frame in pacer.inflight]), (1, [TAGS[0]]))


class AdaptiveWindowTests(unittest.TestCase):
    def test_window_grows_only_while_it_limits(self):
        pacer = AckPacer("adaptive")
        seen = drive(pacer, 0.005, secs=5.0)  # each frame is acked before the next tick: never window-limited
        self.assertEqual({window for _, window in seen}, {2.0})
        self.assertFalse(pacer.limited)

    def test_window_grows_by_one_over_w_until_it_stops_limiting(self):
        pacer = AckPacer("adaptive")
        seen = drive(pacer, 0.105, secs=10.0)
        windows = [window for _, window in seen]
        self.assertEqual(windows, sorted(windows))
        self.assertAlmostEqual(windows[2] - windows[1], 1 / windows[1], places=9)
        self.assertTrue(8 <= windows[-1] < 9)  # 60 fps needs 7 in flight at 105 ms; the target 9 is never needed
        self.assertFalse(pacer.limited)
        self.assertGreater(sum(t > 5 for t, _ in seen) / 5.0, 59)

    def test_growth_stops_at_the_rtt_target(self):
        pacer = AckPacer("adaptive")
        t = 0.0
        for _ in range(100):  # bursts that always fill the window, acked 20 ms later
            while pacer.ready(t):
                pacer.sent(t, 100, full=False)
            for _ in range(len(pacer.inflight)):
                pacer.acked(t + 0.02)
            t += 0.05
        self.assertEqual(pacer.window, math.ceil(0.02 / TICK) + 2)

    def test_target_is_capped(self):
        pacer = AckPacer("adaptive")
        seen = drive(pacer, 0.5, secs=20.0)
        self.assertEqual(max(window for _, window in seen), CAP)

    def test_bytes_in_flight_are_capped_at_two_bdp(self):
        pacer = AckPacer("adaptive")
        t = 0.0
        for _ in range(12):
            exchange(pacer, t, 0.02)
            t += TICK
        pacer.window = 8.0
        cap = 2 * pacer.bw * pacer.base_rtt
        sent = 0
        while pacer.ready(t):
            pacer.sent(t, 100, full=False)
            sent += 1
        self.assertLess(sent, math.floor(pacer.window))
        self.assertGreaterEqual(sent * 100, cap)
        self.assertLess((sent - 1) * 100, cap)
        self.assertTrue(pacer.limited)

    def test_a_repaint_or_a_size_jump_resets_the_window(self):
        """The spec owner's decision 6 changed this requirement: a jump is a frame at least 4x the frame size and at
        least 4 KB (it was over 3x the largest of the last 16 and over 1 KB), so a truecolor wave banner leaving no
        longer restarts the window. Decision 6 measured the 4x against a smoothed size; S12 measures it against the
        largest frame of the last 5 s instead, as most frames are unchanged packets of a few dozen bytes: a march step
        is then a jump only when no frame near its size went out in the last 5 s."""
        pacer = AckPacer("adaptive")
        pacer.window = 6.0
        for n in range(60):  # a truecolor Campaign at 80x24: unchanged frames, and a sprite step
            pacer.sent(n * TICK, 400 if n % 4 == 0 else 20, full=False)
        self.assertEqual(pacer.window, 6.0)
        self.assertEqual(pacer.peak, 400)
        pacer.sent(1.0, 1062, full=False)  # the wave banner leaving: 2.7x the peak, and 1 KB
        pacer.sent(1.1, JUMP_MIN - 1, full=False)  # just under 4 KB is no jump either
        self.assertEqual(pacer.window, 6.0)
        pacer.sent(1.2, 300, full=True)  # a repaint, whatever its size
        self.assertEqual(pacer.window, FLOOR)
        pacer = AckPacer("adaptive")
        for n in range(60):  # a quieter scene: the peak is a 200-byte step
            pacer.sent(n * TICK, 200 if n % 4 == 0 else 20, full=False)
        pacer.window = 6.0
        pacer.sent(1.0, 1062, full=False)  # the banner leaving, 5.3x the peak: only the 4 KB floor makes it no jump
        self.assertEqual(pacer.window, 6.0)
        pacer = AckPacer("adaptive")
        for n in range(20):  # a long run of 1.5 KB frames
            pacer.sent(n * TICK, 1500, full=False)
        pacer.window = 6.0
        pacer.sent(0.5, 5000, full=False)  # over 4 KB, but not 4x the peak
        self.assertEqual(pacer.window, 6.0)
        pacer = AckPacer("adaptive")
        for n in range(20):  # the pace_sim jump: 300 B frames, then 6.5 KB ones
            pacer.sent(n * TICK, 300, full=False)
        pacer.window = 6.0
        pacer.sent(0.5, 6500, full=False)
        self.assertEqual(pacer.window, FLOOR)
        pacer = AckPacer("adaptive")
        pacer.sent(0.0, JUMP_MIN // 4, full=False)  # the first frame is no jump, and starts the peak
        pacer.window = 6.0
        pacer.sent(0.1, JUMP_MIN, full=False)  # exactly 4x and exactly 4 KB: a jump
        self.assertEqual(pacer.window, FLOOR)

    def test_march_steps_reset_the_window_once_and_again_only_after_a_quiet_spell(self):
        """120x44 in truecolor: unchanged frames, and a 9 KB march step every half second (the S12 measurement)."""
        pacer = AckPacer("adaptive")
        resets, t = [], 0.0
        for n in range(600):  # 10 s
            t = n * TICK
            pacer.window = 6.0
            pacer.sent(t, 9000 if n % 30 == 15 else 20, full=False)
            if pacer.window == FLOOR:
                resets.append(n)
        self.assertEqual(resets, [15])  # the first step only; the 4x mean rule reset on all 20
        self.assertEqual(pacer.peak, 9000)
        for n in range(600, 600 + 6 * 60):  # 6 s of unchanged frames: the step leaves the window
            pacer.sent(n * TICK, 20, full=False)
        self.assertEqual(pacer.peak, 20)
        pacer.window = 6.0
        pacer.sent(16.1, 9000, full=False)
        self.assertEqual(pacer.window, FLOOR)

    def test_after_a_quiet_spell_the_last_frame_sent_is_the_peak(self):
        """Frames older than 5 s leave the peak before the next frame is weighed, all but the last one sent."""
        pacer = AckPacer("adaptive")
        pacer.sent(0.0, 9000, full=False)
        pacer.sent(0.1, 1000, full=False)
        pacer.window = 6.0
        pacer.sent(20.0, 4096, full=False)  # 4x the last frame: a jump, though under half the 9 KB of 20 s ago
        self.assertEqual((pacer.window, pacer.peak), (FLOOR, 4096))
        pacer = AckPacer("adaptive")
        pacer.sent(0.0, 9000, full=False)
        pacer.sent(0.1, 1500, full=False)
        pacer.window = 6.0
        pacer.sent(20.0, 5000, full=False)  # under 4x the last frame (1.5 KB): no jump
        self.assertEqual((pacer.window, pacer.peak), (6.0, 5000))

    def test_a_frame_weighs_in_the_peak_for_five_seconds(self):
        pacer = AckPacer("adaptive")
        pacer.sent(0.0, 9000, full=False)
        for n in range(1, 294):  # unchanged frames until 4.88 s
            pacer.sent(n * TICK, 20, full=False)
        pacer.window = 6.0
        pacer.sent(4.9, 9000, full=False)  # the step of 4.9 s ago still weighs: no jump
        self.assertEqual(pacer.window, 6.0)
        for n in range(295, 600):  # unchanged frames until 9.98 s
            pacer.sent(n * TICK, 20, full=False)
        pacer.sent(10.0, 9000, full=False)  # 5.1 s after the last step: a jump
        self.assertEqual(pacer.window, FLOOR)


class CongestionTests(unittest.TestCase):
    def test_three_queued_acks_back_off_and_further_ones_in_the_episode_keep_the_hold(self):
        """The spec owner's decision 1 changed this requirement: the hold doubles at most once per congestion episode
        (it doubled at every queued ack), and every queued ack still backs off and restarts the hold."""
        pacer, t = settled()
        holds = []
        for _ in range(8):
            t = exchange(pacer, t, 0.25) + 0.01
            holds.append(pacer.hold if pacer.window == FLOOR else None)
        # min over the last 3 samples: the first two queued acks cannot be told from jitter
        self.assertEqual(holds, [None, None, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0])
        self.assertTrue(pacer.episode)
        self.assertAlmostEqual(pacer.hold_until, pacer.last_back + 2.0)

    def test_each_episode_within_ten_seconds_of_the_last_back_off_doubles_the_hold(self):
        """An episode ends at the first ack whose queueing delay is back under 2 ticks (decision 1)."""
        pacer, t = settled()
        holds = []
        for _ in range(6):
            for _ in range(5):  # three queued acks to back off, then two more in the same episode
                t = exchange(pacer, t, 0.25) + 0.01
            holds.append(pacer.hold)
            t = exchange(pacer, t, 0.02) + 0.01  # the queue drained: the episode is over
            self.assertFalse(pacer.episode)
        self.assertEqual(holds, [2.0, 4.0, 8.0, 16.0, 32.0, HOLD_MAX])

    def test_the_hold_resets_after_ten_quiet_seconds(self):
        pacer, t = settled()
        for _ in range(2):  # two episodes
            for _ in range(4):
                t = exchange(pacer, t, 0.25) + 0.01
            t = exchange(pacer, t, 0.02) + 0.01
        self.assertEqual(pacer.hold, 4.0)
        last_back = pacer.last_back
        while t < last_back + 10.5:
            t = exchange(pacer, t, 0.02) + TICK
        for _ in range(3):
            t = exchange(pacer, t, 0.25) + 0.01
        self.assertEqual(pacer.hold, HOLD_MIN)

    def test_one_rtt_spike_no_longer_pins_the_window_at_the_floor_for_32_seconds(self):
        """The G4.3 failure (S9, decision 1) on a virtual clock: 60 fps at 103 ms, then 200 ms more for 250 ms at
        35 s. Doubling at every queued ack, as 8.4 was written, holds W at 2 for 32 s; once per episode, it holds
        for the 2 s minimum and is back to 60 fps within a few round trips of the hold."""

        class PerAckHold(AckPacer):
            def _adapt(self, now, rtt, least, nbytes):
                self.episode = False  # every queued ack starts an episode of its own: the old law
                super()._adapt(now, rtt, least, nbytes)

        for law in (PerAckHold, AckPacer):
            with self.subTest(law=law.__name__):
                pacer = law("adaptive")
                link = Link(pacer)
                link.run(0.103, 35.0)
                self.assertGreater(pacer.window, 7)  # 60 fps at 103 ms needs 7 in flight
                seen = link.run(0.303, 0.25) + link.run(0.103, 40.0)
                pinned = [t for t, window in seen if window == FLOOR]
                self.assertLess(min(pinned), 35.5)
                if law is PerAckHold:
                    self.assertEqual(pacer.hold, HOLD_MAX)
                    self.assertGreater(max(pinned) - min(pinned), 31.5)
                    continue
                self.assertEqual(pacer.hold, HOLD_MIN)
                self.assertLess(max(pinned), pacer.hold_until)  # at the floor for the hold only
                self.assertLess(pacer.hold_until, 35.25 + 0.303 + HOLD_MIN)  # from the spike's last queued ack
                back = next(t for t, window in seen if t > pacer.hold_until and window >= 7)
                self.assertLess(back - pacer.hold_until, 10 * 0.103)
                self.assertGreater(sum(40 <= t < 45 for t, _ in seen) / 5.0, 59)

    def test_no_growth_during_the_hold_then_growth_resumes(self):
        clock = Clock()
        pacer = AckPacer("adaptive", clock=clock)
        link = Link(pacer)
        link.run(0.105, 10.0)
        self.assertGreater(pacer.window, 6)
        link.run(0.305, 1.0)  # a standing queue of 200 ms
        self.assertEqual(pacer.window, FLOOR)
        clock.now = link.now
        self.assertTrue(pacer.congested)
        seen = link.run(0.105, 45.0)
        hold_until = pacer.hold_until  # the last back-off came as the queue drained
        self.assertEqual({window for t, window in seen if t < hold_until}, {2.0})
        self.assertGreater(seen[-1][1], 4.0)
        clock.now = hold_until
        self.assertFalse(pacer.congested)

    def test_queueing_between_one_and_two_ticks_neither_grows_nor_backs_off(self):
        pacer, t = settled()
        for _ in range(6):
            t = exchange(pacer, t, 0.02 + 1.5 * TICK) + 0.05
        self.assertEqual((pacer.window, pacer.hold_until), (6.0, -math.inf))

    def test_a_step_in_the_rtt_holds_the_window_for_the_minimum_only(self):
        """F5 of the S12 review: the RTT steps from 100 to 150 ms (a route change) at 80x24, where every fourth frame
        is a 1.3 KB sprite step. The game itself limits the delivery rate, so bw is far under the link's, and the
        serialisation term (bytes / bw) takes 50 ms off a sprite frame's round trip: the queue estimate then fell under
        2 ticks at every sprite frame. Ending the episode there doubled the hold at each back-off, to HOLD_MAX within
        two seconds of the step. The episode now ends once the round trips themselves are back within 2 ticks of the
        base RTT, here when the base RTT has learned the new route."""
        pacer = AckPacer("adaptive")
        link = Link(pacer)
        link.run(0.1, 12.0, sprites)
        self.assertGreater(pacer.window, 7)
        holds, seen = set(), []
        for _ in range(30):  # 30 s on the new route
            seen += link.run(0.15, 1.0, sprites)
            holds.add(pacer.hold)
        self.assertEqual(holds, {HOLD_MIN})
        self.assertAlmostEqual(pacer.base_rtt, 0.15)
        self.assertGreater(per_second(seen, link.now - 5, link.now), 59)

    def test_a_reply_read_late_does_not_end_the_episode(self):
        """The episode ends on the round trips themselves (their upper bounds): a reply the game read late, whose lower
        bound is clean, proves no queue gone, so the next back-off of the same episode keeps the hold. Ended there, the
        hold doubled."""
        pacer, t = settled()
        for _ in range(3):
            t = exchange(pacer, t, 0.25) + 0.01
        self.assertEqual((pacer.hold, pacer.episode), (HOLD_MIN, True))
        pacer.sent(t, 100, full=False)
        pacer.read(t + 0.25, 0.24)  # within [10 ms, 250 ms]
        pacer.acked(t + 0.25)
        t += 0.26
        for _ in range(3):
            t = exchange(pacer, t, 0.25) + 0.01
        self.assertEqual((pacer.window, pacer.hold, pacer.episode), (FLOOR, HOLD_MIN, True))


class GameWaitTests(unittest.TestCase):
    """A round trip is known within [rtt - unread - held, rtt]: a reply may have waited unread for as long as the game
    had not looked at the tty, and a frame's bytes may have waited on the game in the output queue. The game's own
    waits are no sign of a queue (the lower bound backs off), and no proof that none is there (the upper bound
    grows)."""

    def test_replies_the_game_read_late_back_off_no_window(self):
        """A 170 ms host stall at 80x24 on a 100 ms link (the S12 G4.3 runs): the replies to the frames in flight came
        during it and waited for the game. Taken whole, those round trips back W off to the floor; with the wait the
        loop measured, their lower bounds show no queue."""
        for told in (False, True):
            with self.subTest(told=told):
                pacer, t = settled(rtt=0.1)
                srtt, base = pacer.srtt, pacer.base_rtt
                for _ in range(6):
                    pacer.sent(t, 100, full=False)
                    t += TICK
                woke = t + 0.17  # the game looked at the tty last as the sixth frame went out
                if told:
                    pacer.read(woke, 0.17)
                for _ in range(6):
                    pacer.acked(woke)
                self.assertEqual(len(pacer.inflight), 0)
                if not told:
                    self.assertEqual(pacer.window, FLOOR)
                    continue
                self.assertEqual((pacer.window, pacer.hold_until, pacer.base_rtt), (6.0, -math.inf, base))
                self.assertGreater(pacer.srtt, srtt)  # srtt takes the upper bound, which keeps the loss timeout safe

    def test_a_lower_bound_takes_off_both_waits(self):
        """A frame held 40 ms by the game whose reply was read 50 ms after the game last looked: 90 ms of the round trip
        may be the game's."""
        pacer, bounds = AckPacer("adaptive", tagged=True), []
        with mock.patch.object(AckPacer, "_adapt", lambda self, now, rtt, least, nbytes: bounds.append((rtt, least))):
            pacer.sent(0.0, 100, full=False, tag=TAGS[0])
            pacer.held(0.04)
            pacer.read(0.3, 0.05)
            pacer.acked(0.3, TAGS[0])
        self.assertEqual(len(bounds), 1)
        self.assertAlmostEqual(bounds[0][0] - bounds[0][1], 0.09)

    def test_held_time_is_the_named_frame_s_only(self):
        """The loop names the frame whose bytes waited: one that went out untracked (no tag) leaves the newest frame in
        flight alone."""
        pacer = AckPacer("adaptive", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=TAGS[0])
        pacer.sent(0.1, 100, full=False)  # no tag: not tracked
        pacer.held(0.05, 0.1)
        self.assertEqual(pacer.inflight[-1].held, 0.0)
        pacer.held(0.05, 0.0)
        self.assertAlmostEqual(pacer.inflight[-1].held, 0.05)

    def test_a_frame_the_game_held_is_no_queue(self):
        """Still in the output queue when the game stalled, its bytes went out only after: its reply came a stall late
        (the S12 review's 0.27 s sample on a 100 ms link), though the link added nothing."""
        for told in (False, True):
            with self.subTest(told=told):
                pacer, t = settled()
                for _ in range(3):
                    pacer.sent(t, 100, full=False)
                    if told:
                        pacer.held(0.17)
                    pacer.acked(t + 0.19)
                    t += 0.25
                self.assertEqual(pacer.window, 6.0 if told else FLOOR)

    def test_a_reply_read_late_proves_no_queue_absent(self):
        """Its round trip may hold a queue as long as the game's wait: no growth on it, as none on a queued one."""
        pacer, t = settled(rtt=0.1)
        pacer.window, windows = 4.0, []
        for _ in range(6):
            while pacer.ready(t):
                pacer.sent(t, 100, full=False)
            self.assertTrue(pacer.limited)
            t += 0.22
            pacer.read(t, 0.12)  # the game had not looked at the tty for 120 ms
            for _ in range(len(pacer.inflight)):
                pacer.acked(t)
            windows.append(pacer.window)
        # in the first round the last 3 samples still held a clean one
        self.assertEqual((set(windows[1:]), pacer.hold_until), ({windows[0]}, -math.inf))

    def test_each_reply_takes_the_wait_of_the_read_it_came_in(self):
        pacer, waits = AckPacer("adaptive"), []
        for n in range(4):
            pacer.sent(n * 0.01, 100, full=False)
        pacer.read(0.1, 0.05)
        pacer.read(0.2, 0.08)
        for at in (0.05, 0.1, 0.2, 0.25):  # at 0.05 and 0.25 no read was told of
            pacer.acked(at)
            waits.append(round(pacer.recent[-1][0] - pacer.recent[-1][1], 9))
        self.assertEqual(waits, [0.0, 0.05, 0.08, 0.0])
        off = AckPacer("off")
        off.read(1.0, 0.5)
        self.assertEqual(len(off.reads), 0)

    def test_a_frame_the_game_held_has_its_loss_timeout_from_when_it_could_go_out(self):
        pacer = AckPacer("fixed")
        pacer.held(0.5)  # nothing in flight to hold
        pacer.sent(0.0, 100, full=False)
        for seconds in (0.15, 0.05, 0.0):
            pacer.held(seconds)
        self.assertAlmostEqual(pacer.deadline(), 1.2)
        self.assertEqual((pacer.overdue(1.19), pacer.overdue(1.21)), (False, True))  # what expire() would do
        pacer.expire(1.19)
        self.assertEqual((len(pacer.inflight), pacer.losses), (1, 0))
        pacer.expire(1.21)
        self.assertFalse(pacer.overdue(10.0))
        self.assertEqual((len(pacer.inflight), pacer.losses), (0, 1))
        pacer.sent(2.0, 100, full=False)
        pacer.sent(2.1, 100, full=False)
        pacer.held(0.3)  # the newest frame's bytes waited: the older frame keeps its own timeout
        self.assertEqual([frame.held for frame in pacer.inflight], [0.0, 0.3])
        self.assertEqual([pacer._lost_at(frame) for frame in pacer.inflight], [4.0, 3.4])  # the first, the probe: 2 s
        self.assertAlmostEqual(pacer.deadline(), 3.4)


class MeasurementTests(unittest.TestCase):
    def test_srtt_is_an_eighth_ewma_and_base_a_ten_second_minimum(self):
        pacer = AckPacer("adaptive")
        exchange(pacer, 0.0, 0.05)
        exchange(pacer, 1.0, 0.13)
        self.assertAlmostEqual(pacer.srtt, 0.06)
        self.assertAlmostEqual(pacer.base_rtt, 0.05)
        exchange(pacer, 10.2, 0.1)
        self.assertAlmostEqual(pacer.base_rtt, 0.1)  # the 50 ms sample is older than 10 s

    def test_bandwidth_and_budget(self):
        pacer = AckPacer("fixed")
        self.assertIsNone(pacer.budget)
        drive(pacer, 0.01, secs=2.0, size=1200)  # one 1.2 KB frame per tick is delivered: 72 KB/s
        self.assertAlmostEqual(pacer.bw, 72_000, delta=2_000)
        self.assertEqual(pacer.budget, int(0.8 * pacer.bw / 60))
        self.assertFalse(pacer.congested)

    def test_bandwidth_ignores_samples_older_than_five_seconds(self):
        pacer = AckPacer("fixed")
        link = Link(pacer)
        link.run(0.01, 1.0, size=3000)
        fast = pacer.bw
        link.run(0.01, 6.0, size=300)
        self.assertLess(pacer.bw, fast / 5)

    def test_replies_read_together_are_one_delivery_of_all_their_bytes(self):
        """Replies read in one chunk share their read time. Five read at once (after a stall) open no rate span of
        their own: the next reply's rate is its bytes over the time since them. And the merged delivery carries every
        reply's bytes: 1.2 KB frames every tick, their replies read two at a time, are 72 KB/s."""
        pacer = AckPacer("fixed")
        for n in range(5):
            pacer.sent(n * TICK, 1000, full=False)
        for _ in range(5):
            pacer.acked(0.3)
        self.assertIsNone(pacer.bw)
        exchange(pacer, 0.32, 0.04, size=1000)
        self.assertAlmostEqual(pacer.bw, 1000 / 0.06)  # 83 KB/s if the five were five deliveries
        pacer, t = AckPacer("fixed"), 0.0
        for _ in range(60):
            pacer.sent(t, 1200, full=False)
            pacer.sent(t + TICK, 1200, full=False)
            pacer.acked(t + TICK + 0.02)
            pacer.acked(t + TICK + 0.02)
            t += 2 * TICK
        self.assertAlmostEqual(pacer.bw, 72_000, delta=2_000)

    def test_a_reply_counts_the_bytes_of_the_frames_whose_replies_were_lost(self):
        pacer = AckPacer("fixed", tagged=True)
        exchange(pacer, 0.0, 0.02, size=1000, tag=TAGS[0])
        for n in range(1, 6):
            pacer.sent(n * TICK, 1000, full=False, tag=TAGS[n])
        pacer.acked(5 * TICK + 0.02, TAGS[5])  # the replies to frames 1 to 4 were lost
        self.assertAlmostEqual(pacer.bw, 5000 / (5 * TICK))

    def test_a_delivery_after_a_long_gap_starts_a_new_span(self):
        """F4 of the S12 review: after an outage of 5 s or more, the span from the last delivery before it gave bw a
        few hundred bytes a second until the next span, and the serialisation term then took seconds off round
        trips. A delivery RATE_GAP s or more after the last one starts a span of its own."""
        pacer = AckPacer("fixed")
        for n in range(120):  # 1.2 KB a tick: 72 KB/s
            exchange(pacer, n * TICK, 0.02, size=1200)
        fast, rates = pacer.bw, []
        for n in range(10):  # six seconds on
            exchange(pacer, 8.0 + n * TICK, 0.02, size=1200)
            rates.append(pacer.bw)
        self.assertGreater(min(rates), 0.9 * fast)

    def test_a_delivery_a_second_or_more_after_the_last_starts_the_span(self):
        for gap, deliveries in ((0.9, 2), (1.1, 1)):
            with self.subTest(gap=gap):
                pacer = AckPacer("fixed")
                t = exchange(pacer, 0.0, 0.02)
                exchange(pacer, t + gap - 0.02, 0.02)
                self.assertEqual(len(pacer.delivered), deliveries)


class LostAckTests(unittest.TestCase):
    def test_a_frame_expires_after_its_timeout_max_one_second_or_four_srtt(self):
        pacer = AckPacer("fixed")
        self.assertIsNone(pacer.deadline())
        pacer.sent(0.0, 100, full=False)
        self.assertEqual(pacer.deadline(), 1.0)
        pacer.expire(0.999)
        self.assertEqual((len(pacer.inflight), pacer.losses), (1, 0))
        pacer.expire(1.0)
        self.assertEqual((len(pacer.inflight), pacer.losses), (0, 1))
        exchange(pacer, 2.0, 0.5)
        pacer.sent(3.0, 100, full=False)
        self.assertEqual(pacer.deadline(), 5.0)
        self.assertEqual(pacer.losses, 0)  # the ack reset the count

    def test_three_consecutive_losses_disable_ack_pacing(self):
        pacer = AckPacer("fixed")
        pacer.sent(0.0, 100, full=False)
        pacer.sent(0.1, 100, full=False)
        self.assertFalse(pacer.ready(0.2))
        pacer.expire(1.1)
        self.assertEqual(pacer.losses, 2)
        pacer.sent(1.2, 100, full=False)  # the probe: the timeout failed once, so it waits 2 s
        pacer.sent(1.3, 100, full=False)
        pacer.expire(2.29)
        self.assertFalse(pacer.disabled)
        pacer.expire(2.3)
        self.assertTrue(pacer.disabled)
        self.assertEqual(([frame.sent for frame in pacer.inflight], pacer.deadline()), ([1.2], None))  # it waits on
        for n in range(5):
            pacer.sent(3.0 + n, 100, full=False)
            self.assertTrue(pacer.ready(3.0 + n))

    def test_while_disabled_frames_are_still_tracked_and_the_next_ack_turns_pacing_back_on(self):
        pacer = AckPacer("adaptive")
        lose_three(pacer)
        self.assertTrue(pacer.disabled)
        for n in range(30):  # clock pacing: every frame may go, and each is tracked without a deadline
            self.assertTrue(pacer.ready(3.0 + n / 60))
            pacer.sent(3.0 + n / 60, 100, full=False)
        self.assertEqual((len(pacer.inflight), pacer.deadline()), (30, None))
        pacer.expire(6.0)  # no longer counted as losses: the frames just leave
        self.assertEqual((len(pacer.inflight), pacer.losses), (0, 3))
        pacer.sent(6.0, 100, full=False)
        pacer.window = 6.0
        pacer.acked(6.2)  # the terminal answers again, but the frame went out clock pacing: its round trip is no sample
        self.assertEqual((pacer.disabled, pacer.losses, pacer.window, pacer.srtt), (False, 0, FLOOR, None))
        pacer.sent(6.3, 100, full=False)
        pacer.sent(6.31, 100, full=False)
        self.assertFalse(pacer.ready(6.32))  # ack pacing again, from the floor
        pacer.acked(6.4)
        self.assertTrue(pacer.ready(6.4))
        self.assertAlmostEqual(pacer.srtt, 0.1)

    def test_a_late_ack_with_nothing_in_flight_still_ends_clock_pacing(self):
        pacer = AckPacer("fixed")
        lose_three(pacer)
        pacer.acked(3.5)
        self.assertEqual((pacer.disabled, pacer.srtt), (False, None))

    def test_owed_counts_every_query_until_its_reply_lost_or_not(self):
        pacer = AckPacer("fixed")
        for n in range(3):
            pacer.sent(n * 0.1, 100, full=False)
        pacer.expire(1.05)  # the first frame left the window, but its reply may still come
        self.assertEqual((len(pacer.inflight), pacer.owed), (2, 3))
        pacer.acked(1.1)
        self.assertEqual(pacer.owed, 2)
        for t in (1.2, 1.3, 1.4):
            pacer.acked(t)  # a stray one never makes it negative
        self.assertEqual(pacer.owed, 0)
        off = AckPacer("off")
        off.sent(0.0, 100, full=False)
        self.assertEqual(off.owed, 0)  # off sends no queries (resolve() leaves caps.ack None)

    def test_any_ack_resets_the_loss_count(self):
        pacer = AckPacer("adaptive")
        for start in (0.0, 10.0):
            pacer.sent(start, 100, full=False)
            pacer.sent(start + 0.1, 100, full=False)
            pacer.expire(start + 1.1)
            exchange(pacer, start + 2.0, 0.02)
        self.assertEqual((pacer.losses, pacer.disabled), (0, False))


class IdentityTests(unittest.TestCase):
    """CPR acks: each reply names its frame (encode.ack_tag), and replies come in the order their queries went out."""

    def test_a_reply_answers_its_own_frame_and_proves_the_older_ones_lost(self):
        pacer = AckPacer("adaptive", tagged=True)
        for n in range(4):
            pacer.sent(n * TICK, 100, full=False, tag=TAGS[n])
        pacer.losses = 2
        pacer.acked(2 * TICK + 0.1, TAGS[2])  # the replies to frames 0 and 1 were lost
        self.assertEqual([frame.tag for frame in pacer.inflight], [TAGS[3]])
        self.assertEqual((pacer.owed, pacer.losses), (1, 0))
        self.assertAlmostEqual(pacer.rtts[-1][1], 0.1)  # its own frame's round trip; in order it was the oldest's
        pacer.acked(3 * TICK + 0.1, TAGS[3])
        self.assertEqual((len(pacer.inflight), pacer.owed), (0, 0))
        self.assertAlmostEqual(pacer.rtts[-1][1], 0.1)

    def test_a_late_reply_to_a_frame_given_up_on_is_no_sample(self):
        """A frame given up on still matches its reply: the reply frees no frame in flight, and its round trip, over
        the loss timeout, is no sample at all (Karn's rule). In order it answered the next frame, in 50 ms."""
        pacer = AckPacer("fixed", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=TAGS[0])
        pacer.expire(1.0)
        self.assertEqual(([frame.tag for frame in pacer.late], len(pacer.inflight), pacer.losses), ([TAGS[0]], 0, 1))
        pacer.sent(1.0, 100, full=False, tag=TAGS[1])
        pacer.acked(1.05, TAGS[0])
        self.assertEqual(
            (pacer.base_rtt, len(pacer.rtts), len(pacer.inflight), len(pacer.late), pacer.owed, pacer.losses),
            (None, 0, 1, 0, 1, 0),
        )
        self.assertIsNone(pacer.srtt)
        pacer.acked(1.1, TAGS[1])
        self.assertAlmostEqual(pacer.srtt, 0.1)
        self.assertAlmostEqual(pacer.base_rtt, 0.1)

    def test_a_late_round_trip_teaches_nothing_and_the_timeout_backs_off_instead(self):
        """D6, F2 and the S15 review's F2: after an outage the late replies' round trips are its length, and smoothed
        into srtt (S14) they kept the loss timeout at 4-25 s; replaced by the next round trip of a frame in flight
        (S15), they still did when none came, so a terminal that stopped answering during an outage wedged the window
        for 14-28 s. A late reply now teaches nothing: srtt, the samples and the base RTT stay, and the next frame
        sent after a timeout failed, the probe, waits twice as long, until a frame in flight is answered (the frames
        after it do not: test_only_the_probe_waits_longer_so_three_losses_come_as_soon)."""
        pacer, t = AckPacer("fixed", tagged=True), 0.0
        for n in range(20):
            t = exchange(pacer, t, 0.1, tag=TAGS[n % 8])
        before = pacer.srtt, pacer.base_rtt, list(pacer.rtts)
        pacer.sent(t, 100, full=False, tag=TAGS[0])
        pacer.held(0.1)
        pacer.expire(t + 1.1)
        self.assertEqual(pacer.backoff, 1)
        pacer.acked(t + 1.2, TAGS[0])
        self.assertEqual(((pacer.srtt, pacer.base_rtt, list(pacer.rtts)), pacer.backoff), (before, 1))
        pacer.sent(t + 1.2, 100, full=False, tag=TAGS[1])
        self.assertAlmostEqual(pacer.deadline(), t + 3.2)
        pacer.acked(t + 1.3, TAGS[1])
        self.assertEqual(pacer.backoff, 0)
        pacer.sent(t + 1.3, 100, full=False, tag=TAGS[2])
        self.assertAlmostEqual(pacer.deadline(), t + 2.3)

    def test_a_reply_to_a_frame_given_up_on_proves_the_older_ones_lost(self):
        pacer = AckPacer("fixed", tagged=True)
        for n in range(3):
            pacer.sent(n * 0.1, 100, full=False, tag=TAGS[n])
        pacer.expire(1.25)  # all three given up on
        pacer.acked(1.3, TAGS[1])  # frame 0's reply was lost
        self.assertEqual(
            ([frame.tag for frame in pacer.late], pacer.owed, pacer.base_rtt, pacer.srtt), ([TAGS[2]], 1, None, None)
        )

    def test_a_reply_to_a_frame_in_flight_settles_every_older_query(self):
        """Every query sent before its frame is answered or lost, those given up on too, and every later one is in
        flight: owed is exact again."""
        pacer = AckPacer("fixed", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=TAGS[0])
        pacer.sent(0.1, 100, full=False, tag=TAGS[1])
        pacer.expire(1.15)
        pacer.sent(1.2, 100, full=False, tag=TAGS[2])
        pacer.sent(1.21, 100, full=False, tag=TAGS[3])
        pacer.queries = deque(sorted([*pacer.queries, 1.16, 1.17, 1.18, 1.19, 1.195]))  # lost while clock pacing
        pacer.acked(1.3, TAGS[2])
        self.assertEqual(([frame.tag for frame in pacer.inflight], len(pacer.late), pacer.owed), ([TAGS[3]], 0, 1))
        self.assertAlmostEqual(pacer.srtt, 0.1)

    def test_on_clock_pacing_the_frames_in_flight_are_given_up_on_and_still_match(self):
        pacer = AckPacer("adaptive", tagged=True)
        for n in range(5):
            pacer.sent(n * 0.1, 100, full=False, tag=TAGS[n])
        pacer.expire(1.25)  # the first three are lost: clock pacing, and the other two are given up on too
        self.assertTrue(pacer.disabled)
        self.assertEqual(([frame.tag for frame in pacer.late], len(pacer.inflight)), (TAGS[:5], 0))
        pacer.acked(1.3, TAGS[3])  # over a second late: no sample, but the terminal answers again
        self.assertEqual(
            (pacer.disabled, pacer.base_rtt, pacer.srtt, [frame.tag for frame in pacer.late]),
            (False, None, None, [TAGS[4]]),
        )
        self.assertEqual((pacer.owed, pacer.losses, pacer.window, pacer.backoff), (1, 0, FLOOR, 1))

    def test_while_clock_pacing_no_frame_is_waited_on_and_any_reply_ends_it(self):
        pacer = AckPacer("adaptive", tagged=True)
        lose_three(pacer, TAGS[:3])
        self.assertTrue(pacer.disabled)
        for n in range(30):  # clock pacing: every frame may go, with the first tag, and none is waited on
            self.assertTrue(pacer.ready(3.0 + n * TICK))
            pacer.sent(3.0 + n * TICK, 100, full=False, tag=OFF)
        self.assertEqual((len(pacer.inflight), pacer.deadline(), pacer.owed, len(pacer.late)), (0, None, 33, 33))
        pacer.window = 6.0
        pacer.acked(3.1, OFF)  # the terminal answers again: the first frame sent clock pacing, so the three were lost
        self.assertEqual((pacer.disabled, pacer.losses, pacer.window, pacer.srtt), (False, 0, FLOOR, None))
        self.assertEqual((pacer.owed, len(pacer.late)), (29, 29))
        pacer.sent(3.6, 100, full=False, tag=TAGS[3])
        pacer.sent(3.61, 100, full=False, tag=TAGS[4])
        self.assertFalse(pacer.ready(3.62))  # ack pacing again, from the floor
        for n in range(29):
            pacer.acked(3.1 + (n + 1) * TICK, OFF)
        self.assertEqual((len(pacer.inflight), pacer.owed), (2, 2))  # the first tag answers no frame in flight
        pacer.acked(3.7, TAGS[3])
        self.assertAlmostEqual(pacer.srtt, 0.1)
        self.assertEqual(([frame.tag for frame in pacer.inflight], len(pacer.late), pacer.owed), ([TAGS[4]], 0, 1))

    def test_a_frame_given_up_on_matches_until_ten_seconds_after_it_went_out(self):
        pacer = AckPacer("fixed", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=TAGS[0])
        pacer.expire(10.0)
        self.assertEqual([frame.tag for frame in pacer.late], [TAGS[0]])
        pacer.expire(10.01)
        self.assertEqual(len(pacer.late), 0)
        pacer.acked(10.02, TAGS[0])  # now a reply to no known frame
        self.assertEqual((pacer.srtt, pacer.owed), (None, 0))

    def test_a_tag_sent_again_names_the_oldest_frame_first(self):
        """Tags cycle (944 at 80x24, about 16 s of frames; fewer on a screen smaller than the tag area, ack_tag), and the
        Renderer gives a new frame no tag a reply still to come may name (reserved()). One sent again all the same
        names the oldest frame first, as replies come in order: forgotten (S15), the frame given up on let its own
        reply settle the new frame, with too short a round trip."""
        pacer = AckPacer("fixed", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=TAGS[0])
        pacer.sent(0.1, 100, full=False, tag=TAGS[1])
        pacer.expire(1.15)
        self.assertEqual(pacer.reserved(), {TAGS[0], TAGS[1]})
        pacer.sent(5.0, 100, full=False, tag=TAGS[0])
        pacer.acked(5.01, TAGS[0])  # the frame given up on's reply
        self.assertEqual(([frame.tag for frame in pacer.late], len(pacer.inflight), pacer.srtt), ([TAGS[1]], 1, None))
        pacer.acked(5.1, TAGS[0])
        self.assertAlmostEqual(pacer.srtt, 0.1)
        self.assertEqual((len(pacer.late), pacer.owed), (0, 0))

    def test_replies_in_order_are_matched_in_order(self):
        """DSR and DA1 replies, the fallback where CPR is not answered, carry nothing: each answers the oldest frame
        in flight (8.3 as written)."""
        pacer = AckPacer("fixed")
        pacer.sent(0.0, 100, full=False)
        pacer.sent(0.05, 100, full=False)
        pacer.acked(0.1)
        self.assertAlmostEqual(pacer.srtt, 0.1)
        self.assertEqual([frame.sent for frame in pacer.inflight], [0.05])

    def test_a_link_slower_than_the_loss_timeout_is_learned_by_the_probe(self):
        """The round trip steps from 100 ms to 1.2 s, over the 1 s loss timeout: every frame is given up on before its
        reply comes, and a reply to a frame given up on teaches nothing (Karn's rule). Three are lost and clock pacing
        takes over until a late reply ends it; the probe out then is no sample, as its round trip spans that spell,
        and keeps no new probe back. The new probe waits twice as long and is answered in flight: its round trip sets
        srtt and the timeout (twice the last round trip), the frames are samples again and the window opens. Learning
        nothing, the pacer gave up on every frame and ran at 2 fps for as long as the link stayed slow."""
        pacer = AckPacer("adaptive", tagged=True)
        link = Link(pacer)
        link.run(0.1, 5.0)
        samples, match = [], pacer._match

        def matching(now, tag):
            frames = match(now, tag)
            if frames and not frames[-1].spanned:
                samples.append((now, frames[-1].backoff))
            return frames

        pacer._match = matching
        seen = link.run(1.2, 60.0)
        at, backoff = next((at, backoff) for at, backoff in samples if at > 5.1)
        self.assertEqual((round(at, 3), backoff), (7.4, 1))  # the slower link's first: the probe after the spell
        self.assertAlmostEqual(pacer.srtt, 1.2, delta=0.01)
        self.assertEqual((pacer.window, pacer.disabled), (CAP, False))
        self.assertGreater(per_second(seen, link.now - 20, link.now), 8)
        assert_in_step(self, pacer, link)

    def test_frames_tagged_outside_a_smaller_screen_s_tags_leave_the_window_when_it_shrinks(self):
        """A terminal that shrank clamps the CUP of every frame it reads after onto its last row, when it has fewer
        rows than the tag area, or its last column, when it has fewer columns: where no frame tagged for it is
        (encode.tag_area). So when the game learns the size, the frames tagged outside those tags leave the window, as
        no loss, and the frames to come go out at once; their replies, which still come, settle the older queries
        and frames, with no sample, each naming the oldest frame it may answer. A screen that grows keeps every frame.
        F1 of the S14 review:
        kept on the new last row, the frame tagged (20, 50) was settled by the clamped reply of the one tagged
        (23, 55), with too short a round trip; and F4: owed then forgot the replies still to come of the frames
        dropped."""
        pacer = AckPacer("fixed", tagged=True)
        for sent, tag in ((0.0, (23, 55)), (0.01, (10, 59)), (0.015, (19, 49)), (0.016, (20, 50))):
            pacer.sent(sent, 100, full=False, tag=tag)
        self.assertFalse(pacer.ready(0.02))
        pacer.resized(100, 30)
        self.assertEqual(len(pacer.inflight), 4)
        pacer.resized(80, 20)
        self.assertEqual(([frame.tag for frame in pacer.inflight], pacer.owed), ([(10, 59), (19, 49)], 4))
        pacer.resized(50, 20)
        self.assertEqual(([frame.tag for frame in pacer.inflight], pacer.owed, pacer.losses), ([(19, 49)], 4, 0))
        self.assertTrue(pacer.ready(0.02))
        tag = ack_tag(7, 50, 20)
        pacer.sent(0.02, 100, full=False, tag=tag)
        steps = []
        for at, reply in ((0.1, (20, 50)), (0.11, (10, 50)), (0.115, (19, 49)), (0.116, (20, 50)), (0.12, tag)):
            pacer.acked(at, reply)  # the replies to (23, 55), (10, 59) and (20, 50) are clamped
            steps.append(([frame.tag for frame in pacer.inflight], pacer.owed, pacer.srtt and round(pacer.srtt, 6)))
        self.assertEqual(
            steps,
            [
                ([(19, 49), tag], 4, None),
                ([(19, 49), tag], 3, None),
                ([tag], 2, 0.1),  # the reply to (20, 50) and the new frame's are still to come
                ([tag], 1, 0.1),
                ([], 0, 0.1),
            ],
        )
        fifo = AckPacer("fixed")
        fifo.sent(0.0, 100, full=False)
        fifo.resized(50, 20)  # replies in order name nothing to clamp
        self.assertEqual(len(fifo.inflight), 1)

    def test_a_screen_that_shrinks_drops_the_frames_given_up_on_tagged_outside_its_tags_with_no_sample(self):
        """Frames given up on that are tagged outside the new screen's tags are dropped too: kept on its last row, the
        frame tagged (20, 30) was matched by the clamped reply of the one tagged (22, 30), sent before it, and srtt
        learnt a round trip that was neither's. The clamped reply now names the dropped frame (22, 30) by where the
        smaller screen put it, the oldest frame it may answer, and settles the older queries, with no sample."""
        pacer = AckPacer("fixed", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=(22, 30))
        pacer.sent(0.01, 100, full=False, tag=(20, 30))
        pacer.sent(0.02, 100, full=False, tag=(19, 30))
        pacer.expire(1.05)
        self.assertEqual(len(pacer.late), 3)
        pacer.resized(80, 20)
        self.assertEqual([frame.tag for frame in pacer.late], [(19, 30)])
        pacer.acked(1.2, (20, 30))
        self.assertEqual((pacer.srtt, len(pacer.late), pacer.owed), (None, 1, 2))
        pacer.acked(1.21, (20, 30))
        pacer.acked(1.22, (19, 30))
        self.assertEqual((len(pacer.late), pacer.owed, pacer.srtt), (0, 0, None))

    def test_where_a_smaller_screen_puts_a_frame_s_reply_is_reserved_until_it_comes(self):
        """L1 of the S15 review. A frame resized() drops may still be answered at its own tag (the terminal read it
        before it shrank, or the screen grew back), or where the smaller screen clamps it. Forgotten, its tag went to
        a new frame a few frames later, at another size (ack_tag maps the sequence by size), and its reply settled
        that frame 16 ms after it went out: the base RTT that taught held W at the floor for 10 s."""
        pacer = AckPacer("fixed", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=(6, 35))
        pacer.sent(0.01, 100, full=False, tag=(5, 10))
        pacer.resized(35, 17)  # column 35 is the last: (6, 35) leaves the window
        self.assertEqual(([frame.tag for frame in pacer.voided], pacer.reserved()), ([(6, 35)], {(6, 35), (5, 10)}))
        pacer.resized(30, 12)
        self.assertEqual(pacer.reserved(), {(6, 35), (6, 30), (5, 10)})
        pacer.resized(100, 30)  # a screen that grows clamps nothing new
        self.assertEqual(pacer.reserved(), {(6, 35), (6, 30), (5, 10)})
        pacer.sent(0.02, 100, full=False, tag=(7, 1))
        pacer.acked(0.25, (6, 35))  # the dropped frame's own reply: no sample, and it settles nothing newer
        self.assertEqual(
            ([frame.tag for frame in pacer.inflight], pacer.voided, pacer.srtt), ([(5, 10), (7, 1)], deque(), None)
        )
        self.assertEqual(pacer.reserved(), {(5, 10), (7, 1)})
        pacer.acked(0.26, (5, 10))
        self.assertAlmostEqual(pacer.srtt, 0.25)

    def test_owed_is_exact_after_a_shrink_drops_frames_given_up_on_or_in_flight(self):
        """DM1 and C1 of the S15 review: frames resized() dropped, given up on or in flight, whose replies were lost,
        stayed counted after a later reply to an older frame given up on proved them lost, so a restart then waited
        its whole settle time for nothing. owed counts the queries, and a reply settles every older one."""
        for first in ((22, 30), (23, 10)):
            with self.subTest(first=first):
                pacer = AckPacer("fixed", tagged=True)
                pacer.sent(0.0, 100, full=False, tag=first)
                pacer.sent(0.01, 100, full=False, tag=(10, 30))
                if first == (23, 10):
                    pacer.resized(80, 20)  # dropped while in flight, then the other one is given up on
                pacer.expire(1.05)
                pacer.resized(80, 20)
                self.assertEqual(
                    (len(pacer.voided), [frame.tag for frame in pacer.late], pacer.owed), (1, [(10, 30)], 2)
                )
                pacer.acked(1.2, (10, 30))  # the first frame's reply was lost
                self.assertEqual((pacer.owed, len(pacer.voided), len(pacer.late)), (0, 0, 0))

    def test_every_cell_a_frame_was_clamped_to_stays_reserved(self):
        """A frame two smaller screens clamped may be answered where either put it, so neither cell goes to a new
        frame until its reply comes (the S16 review: kept, only the last could)."""
        pacer = AckPacer("fixed", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=(20, 50))
        pacer.resized(80, 15)
        pacer.resized(40, 15)
        pacer.resized(80, 24)
        self.assertEqual(pacer.reserved(), {(20, 50), (15, 50), (15, 40)})
        pacer.acked(0.2, (15, 50))  # the reply the first smaller screen clamped: dropped there, so no sample
        self.assertEqual((pacer.reserved(), pacer.owed, pacer.srtt), (set(), 0, None))

    def test_frames_dropped_by_a_shrink_are_forgotten_ten_seconds_after_they_went_out(self):
        pacer = AckPacer("fixed", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=(20, 50))
        pacer.resized(80, 15)
        pacer.expire(9.99)
        self.assertEqual((len(pacer.voided), pacer.owed), (1, 1))
        pacer.expire(10.01)
        self.assertEqual((len(pacer.voided), pacer.reserved(), pacer.owed), (0, set(), 0))

    def test_a_restart_s_pacer_keeps_the_old_frames_reserved_and_their_replies_named(self):
        """DOC-restart-tags of the S16 review: the tag sequence goes on across a restart, but at another size it maps
        to other positions, and the new pacer knew none of the old frames. A reply to one that came after the
        restart's settle wait then settled the new frame given its tag, too soon. The new pacer keeps them, as
        frames a shrink dropped: no query owed, no loss, their tags and cells reserved."""
        old = AckPacer("adaptive", tagged=True)
        old.sent(0.0, 100, full=False, tag=(16, 1))
        old.sent(0.01, 100, full=False, tag=(22, 30))
        old.expire(1.0)
        old.sent(1.0, 100, full=False, tag=(5, 20))
        new = AckPacer("adaptive", tagged=True)
        new.inherit(old)
        new.resized(50, 20)  # the game comes back at another size
        self.assertEqual((new.owed, new.inflight, new.reserved()), (0, deque(), {(16, 1), (22, 30), (20, 30), (5, 20)}))
        new.sent(2.0, 100, full=False, tag=(17, 1))
        new.acked(2.05, (16, 1))  # the old frame's reply, late: no sample, and the new frame still waits on its own
        self.assertEqual((new.srtt, new.owed, len(new.inflight)), (None, 1, 1))
        fifo = AckPacer("adaptive")
        fifo.inherit(old)
        self.assertEqual((fifo.voided, fifo.reserved()), (deque(), set()))
        new = AckPacer("adaptive", tagged=True)
        new.inherit(fifo)  # a restart from DSR acks onto CPR acks: the old frames carry no tag
        new.resized(80, 24)  # as the loop does after enter()
        self.assertEqual((new.voided, new.reserved()), (deque(), set()))

    def test_a_restart_keeps_the_frames_a_shrink_dropped_and_in_send_order(self):
        """MUT-inherit-voided-order of the S17 review: a frame resized() dropped may still be answered, at its tag or
        where the shrink put it, and the replies settle the frames kept from the left, so they are kept in send
        order."""
        old = AckPacer("fixed", tagged=True)
        old.sent(0.0, 100, full=False, tag=(20, 50))
        old.resized(80, 15)  # dropped, its CUP clamped to (15, 50)
        old.sent(0.5, 100, full=False, tag=TAGS[0])
        old.expire(1.5)
        old.sent(1.5, 100, full=False, tag=TAGS[1])  # the probe, in flight
        new = AckPacer("fixed", tagged=True)
        new.inherit(old)
        self.assertEqual([frame.sent for frame in new.voided], [0.0, 0.5, 1.5])  # before the resize that sorts them
        new.resized(80, 24)
        self.assertEqual([frame.sent for frame in new.voided], [0.0, 0.5, 1.5])
        self.assertEqual(new.reserved(), {(20, 50), (15, 50), TAGS[0], TAGS[1]})
        new.sent(2.0, 100, full=False, tag=TAGS[2])
        new.acked(2.05, (15, 50))  # the dropped frame's reply: no sample, and the new frame still waits
        self.assertEqual((new.srtt, len(new.inflight), [frame.sent for frame in new.voided]), (None, 1, [0.5, 1.5]))
        new.acked(2.06, TAGS[0])
        self.assertEqual([frame.sent for frame in new.voided], [1.5])

    def test_a_reply_that_names_no_frame_answers_the_oldest_query_and_ends_clock_pacing(self):
        """LAW17-2 of the S17 review: such a reply is one more than LATE_KEEP s late, as on a link whose backlog has
        grown past that, and the terminal still answers. Taken for no reply (S17), it left clock pacing on, which only
        grew the backlog. A modified F3 key, the reason S17 had for that (LD2 of the S16 review), no longer reads as a
        reply: tags keep off the columns of its modifiers (encode.TAG_SKIP)."""
        pacer = AckPacer("adaptive", tagged=True)
        lose_three(pacer, TAGS[1:4])  # sent at 0.0-0.02: clock pacing
        pacer.sent(5.0, 100, full=False, tag=TAGS[0])
        pacer.expire(10.5)  # the three frames and their queries are forgotten, as their replies are more than 10 s late
        self.assertEqual((pacer.disabled, pacer.owed, len(pacer.late)), (True, 1, 1))
        pacer.acked(10.6, TAGS[1])
        self.assertEqual((pacer.disabled, pacer.losses, pacer.owed, len(pacer.late)), (False, 0, 0, 1))

    def test_the_replies_after_a_long_outage_take_as_many_steps_as_there_are_replies(self):
        """F2 of the S16 review: each reply rebuilt the frames given up on, and the burst after a 10 s outage, some 600
        replies, took 20 ms in one loop iteration, more than a frame. The lists are in send order, so a reply settles
        what it names from their fronts and costs the same however many frames are still behind it or were settled
        before it. The first 500 replies of a 5000-reply burst and its last 500 each take under 4 times as long as a
        burst of 500 in the same test (about as long; S16's rebuilding made the first 500 some 15 times as long, and
        the 5000 1.6 s). And each reply, whatever the backlog, takes under 20 times as long as sending its frame did
        (2 to 4.5 times): replies grown several times dearer would stall a frame just the same. Times are this
        thread's CPU time, the best of three runs, so the machine's speed and whatever else runs on it cancel out."""
        import time

        def burst(n):
            """The CPU time the first and the last 500 replies of a burst of n took, to frames sent clock pacing after
            three were lost, and the time sending those frames took."""
            pacer = AckPacer("adaptive", tagged=True)
            lose_three(pacer, TAGS[1:4])
            sending = time.thread_time()
            for k in range(n):
                pacer.sent(1.05 + k * 0.0016, 100, full=False, tag=OFF)
            late, marks = pacer.late, []
            for k in range(n):
                if k % 500 == 0:
                    marks.append(time.thread_time())
                pacer.acked(9.1 + k * 1e-5, OFF)
            marks.append(time.thread_time())
            self.assertEqual((pacer.late, pacer.owed, pacer.disabled), (late, 0, False))
            self.assertIs(pacer.late, late)
            return marks[1] - marks[0], marks[-1] - marks[-2], marks[0] - sending

        def ratio(a, b):
            """a / b; b can be 0, as a VM takes the time its host stole off what its thread runs next."""
            return a / b if b else float("inf")

        trials = []
        for _ in range(3):
            first, last, _ = burst(5000)
            replies, _, sends = burst(500)
            trials.append((ratio(max(first, last), replies), ratio(replies, sends)))
        growth, cost = (min(ratios) for ratios in zip(*trials))
        self.assertLess(growth, 4, trials)
        self.assertLess(cost, 20, trials)

    def test_a_reply_naming_no_frame_answers_the_oldest_query(self):
        """A size the game never learned (two changes between its looks at the size) puts a reply where no frame
        recorded: it still answers a query, the oldest, as replies come in the order their queries went out."""
        pacer = AckPacer("fixed", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=TAGS[1])
        pacer.sent(0.01, 100, full=False, tag=TAGS[2])
        pacer.acked(0.1, (2, TAGS[1][1]))  # the terminal was 2 rows high when it read the first frame
        self.assertEqual((list(pacer.queries), len(pacer.inflight), pacer.srtt), ([0.01], 2, None))
        pacer.acked(0.11, TAGS[2])
        self.assertEqual((pacer.owed, len(pacer.inflight)), (0, 0))  # and proves the first frame's reply gone
        pacer.sent(1.0, 100, full=False, tag=TAGS[3])
        pacer.expire(11.01)
        self.assertEqual((list(pacer.queries), len(pacer.late)), ([], 0))  # 10 s on, a reply counts as never coming

    def test_a_reply_to_a_frame_sent_clock_pacing_names_it_not_a_newer_frame_a_screen_put_at_the_first_tag(self):
        """Fuzzing S16: a screen one column wide puts each frame tagged on the first tag row at the first tag. A
        reply still to come to a frame sent while clock pacing was on then named the newer frame in flight that such
        a screen had put there, and settled the queries between: owed, and so the count of row-1 CPRs the 1-row
        decoder expects, fell under the replies still to come."""
        pacer = AckPacer("fixed", tagged=True)
        lose_three(pacer, TAGS[:3])
        pacer.sent(1.1, 100, full=False, tag=OFF)
        pacer.sent(1.2, 100, full=False, tag=OFF)
        pacer.acked(1.3, TAGS[2])  # the terminal answers again
        self.assertEqual((pacer.disabled, pacer.owed), (False, 2))
        newer = ack_tag(3, 80, 24)
        self.assertEqual(newer[0], OFF[0])
        pacer.sent(1.4, 100, full=False, tag=newer)
        pacer.resized(1, 24)
        self.assertEqual({frame.sent: frame.cells for frame in pacer.voided}[1.4], {OFF})
        pacer.acked(1.5, OFF)
        self.assertEqual((list(pacer.queries), [frame.sent for frame in pacer.voided]), ([1.2, 1.4], [1.2, 1.4]))
        pacer.acked(1.6, OFF)
        pacer.acked(1.7, OFF)  # the newer frame's own reply, at the first tag on this screen
        self.assertEqual((pacer.owed, len(pacer.voided), len(pacer.late), pacer.srtt), (0, 0, 0, None))


class BackoffTests(unittest.TestCase):
    """The loss timeout of 8.3, max(1 s, 4 x srtt), or twice the last round trip; the probe takes it doubled once per
    timeout in a row that failed (Karn's rule and TCP's backoff)."""

    def test_the_frames_of_one_window_lost_together_double_the_timeout_once(self):
        pacer = AckPacer("adaptive")
        lose_three(pacer)
        self.assertEqual((pacer.disabled, pacer.backoff), (True, 1))

    def test_each_timeout_of_the_probe_that_fails_doubles_it_up_to_eight_times(self):
        pacer, t, timeouts = AckPacer("fixed"), 0.0, []
        for _ in range(BACKOFF_MAX + 2):
            pacer.sent(t, 100, full=False)
            timeouts.append(pacer.deadline() - t)
            t = pacer.deadline()
            pacer.expire(t)
            pacer.acked(t)  # any reply resets the loss count, so clock pacing never takes over
        self.assertEqual(timeouts, [1.0, 2.0, 4.0, 8.0, 8.0])

    def test_with_cpr_acks_the_probe_given_up_on_is_replaced_at_its_backoff_up_to_eight_times(self):
        """A tagged probe given up on waits in `late` for its reply; the next frame is the probe all the same."""
        pacer, t, timeouts = AckPacer("fixed", tagged=True), 0.0, []
        for n in range(BACKOFF_MAX + 2):
            pacer.sent(t, 100, full=False, tag=TAGS[n])
            timeouts.append(pacer.deadline() - t)
            t = pacer.deadline()
            pacer.expire(t)
            pacer.acked(t, TAGS[n])  # its reply, late: the loss count resets
        self.assertEqual((timeouts, len(pacer.late)), ([1.0, 2.0, 4.0, 8.0, 8.0], 0))
        pacer = AckPacer("fixed", tagged=True)
        for n in range(BACKOFF_MAX + 1):  # MUT-gaps of the S17 review: the last probe's reply has not come
            pacer.sent(t, 100, full=False, tag=TAGS[n])
            t = pacer.deadline()
            pacer.expire(t)
            if n < BACKOFF_MAX:
                pacer.acked(t, TAGS[n])
        self.assertEqual((pacer.backoff, pacer.late[-1].backoff), (BACKOFF_MAX, BACKOFF_MAX))
        pacer.sent(t, 100, full=False, tag=TAGS[7])  # it waits in late: this is the probe, at the cap
        self.assertEqual((pacer.inflight[-1].backoff, pacer.inflight[-1].rto), (BACKOFF_MAX, 8.0))

    def test_only_the_probe_waits_longer_so_three_losses_come_as_soon(self):
        """L2 of the S15 review, as S16 first had it: every frame backed off, a terminal that stopped answering after
        a 0.9 s outage held the window for three timeouts. One probe at a time waits it out doubled. It keeps waiting
        while clock pacing is on, as its own timeout raises the backoff and a FIFO reply to it is matched in order,
        but its round trip then spans the outage: no sample (spanned)."""
        pacer = AckPacer("fixed")
        pacer.sent(0.0, 100, full=False)
        pacer.sent(0.1, 100, full=False)
        pacer.expire(1.0)
        pacer.sent(1.0, 100, full=False)  # the first frame out after the timeout failed
        pacer.expire(1.1)
        pacer.sent(1.1, 100, full=False)
        self.assertEqual([(frame.backoff, frame.rto) for frame in pacer.inflight], [(1, 2.0), (0, 1.0)])
        self.assertAlmostEqual(pacer.deadline(), 2.1)  # the newer frame's timeout comes first
        self.assertEqual((pacer.overdue(2.09), pacer.overdue(2.1)), (False, True))
        pacer.expire(2.1)
        self.assertEqual((pacer.disabled, pacer.backoff, [frame.sent for frame in pacer.inflight]), (True, 1, [1.0]))
        self.assertTrue(pacer.inflight[0].spanned)
        pacer.expire(3.0)
        self.assertEqual((pacer.backoff, len(pacer.inflight)), (2, 0))  # its own timeout failed, clock pacing or not

    def test_a_reply_to_a_frame_given_up_on_settles_the_older_probe_as_lost_and_is_no_sample(self):
        pacer = AckPacer("fixed", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=TAGS[0])
        pacer.expire(1.0)
        pacer.sent(1.0, 100, full=False, tag=TAGS[1])  # the probe: 2 s
        pacer.sent(1.1, 100, full=False, tag=TAGS[2])
        pacer.expire(2.1)  # given up on; the probe waits on
        pacer.acked(2.5, TAGS[2])  # late: the probe's reply, which would have come first, was lost
        self.assertEqual((len(pacer.inflight), len(pacer.late), pacer.owed, pacer.srtt), (0, 0, 0, None))

    def test_a_frame_in_flight_waits_out_the_timeout_now_in_force(self):
        """A round trip that raises the timeout raises it for the frames already in flight too."""
        pacer = AckPacer("fixed")
        pacer.sent(0.0, 100, full=False)
        pacer.sent(0.1, 100, full=False)
        pacer.acked(0.9)  # the first, 0.9 s, so srtt: the timeout is now 3.6 s
        self.assertAlmostEqual(pacer.deadline(), 3.7)

    def test_a_probe_that_leaves_the_window_unanswered_is_replaced(self):
        pacer = AckPacer("fixed", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=TAGS[0])
        pacer.expire(1.0)
        pacer.sent(1.0, 100, full=False, tag=(10, 40))
        pacer.resized(80, 8)  # outside this size's tags: the probe leaves the window as no loss
        pacer.sent(1.1, 100, full=False, tag=TAGS[1])
        self.assertEqual(([frame.sent for frame in pacer.voided], pacer.inflight[0].backoff), ([1.0], 1))

    def test_a_round_trip_of_a_frame_in_flight_resets_it_and_ending_clock_pacing_keeps_it(self):
        pacer = AckPacer("fixed")
        pacer.sent(0.0, 100, full=False)
        pacer.expire(1.0)
        pacer.sent(1.0, 100, full=False)
        pacer.expire(3.0)
        self.assertEqual(pacer.backoff, 2)
        pacer.sent(3.0, 100, full=False)
        pacer.acked(3.1)
        self.assertEqual(pacer.backoff, 0)
        pacer.sent(4.0, 100, full=False)
        pacer.expire(5.0)
        self.assertEqual(pacer.backoff, 1)
        for n in range(3):  # the probe (2 s), then two frames lost in a second: clock pacing, the probe waiting on
            pacer.sent(5.0 + n * 0.01, 100, full=False)
        pacer.expire(6.02)
        self.assertEqual((pacer.disabled, pacer.backoff, [frame.sent for frame in pacer.inflight]), (True, 1, [5.0]))
        pacer.sent(6.05, 100, full=False)  # a FIFO frame sent clock pacing is tracked, as no sample
        self.assertEqual(pacer.inflight[-1][-1:], (True,))
        pacer.expire(7.05)  # the probe's timeout failed too
        self.assertEqual((pacer.disabled, pacer.backoff), (True, 2))
        pacer.acked(7.1)  # the terminal answers again, after an outage: the backoff stays for the next probe
        self.assertEqual((pacer.disabled, pacer.backoff), (False, 2))
        pacer.sent(7.2, 100, full=False)
        pacer.sent(7.3, 100, full=False)
        pacer.expire(8.2)
        pacer.sent(8.2, 100, full=False)  # a timeout failed: the probe, 4 s
        self.assertEqual([frame.rto for frame in pacer.inflight], [1.0, 4.0])
        pacer.acked(8.25)  # the frame from 7.3, in flight
        self.assertEqual(pacer.backoff, 0)

    def test_the_probe_holds_its_slot_until_it_is_given_up_on(self):
        """LAW17-1 of the S17 review: freed once a frame at no backoff would have been given up on (S17), the probe's
        slot let a third frame into the queue of a link slower than the timeout. Two frames were then lost per timeout
        between the late replies, and clock pacing took over and flooded the link."""
        pacer = AckPacer("fixed", tagged=True)
        pacer.sent(0.0, 100, full=False, tag=TAGS[0])
        pacer.expire(1.0)
        pacer.sent(1.0, 100, full=False, tag=TAGS[1])  # the probe: 2 s
        pacer.sent(1.5, 100, full=False, tag=TAGS[2])
        self.assertFalse(pacer.ready(2.4))
        pacer.expire(2.5)
        self.assertEqual((pacer.losses, pacer.ready(2.5)), (2, True))
        pacer.sent(2.5, 100, full=False, tag=TAGS[3])
        self.assertFalse(pacer.ready(2.9))
        pacer.acked(2.9, TAGS[1])  # matched in flight: a sample
        self.assertEqual((pacer.backoff, [frame.tag for frame in pacer.inflight]), (0, TAGS[3:4]))
        self.assertAlmostEqual(pacer.srtt, 1.9)

    def test_after_an_outage_no_probe_goes_out_until_a_timeout_fails_again(self):
        """F1 and F2p-b of the S16 review: the backoff an outage leaves made the first frame out after it a probe of
        4-8 s, which held one of the floor window's two slots, so a terminal that stopped answering then took three
        timeouts at a frame a second to give up on. After a timeout or more of clock pacing, the frames go out at no
        backoff until one is given up on, so it takes two timeouts at full rate, as on S13; the probe then goes out at
        the backoff kept. After a shorter spell, a terminal answering late over a link slower than the timeout, the
        probe goes first, as it would have without the spell (LAW17-1 of the S17 review)."""
        pacer = AckPacer("fixed", tagged=True)
        lose_three(pacer, TAGS[1:4])  # clock pacing from 1.05
        pacer.acked(2.1, TAGS[1])  # late: ack pacing again, the backoff kept
        self.assertEqual((pacer.disabled, pacer.backoff), (False, 1))
        pacer.sent(2.1, 100, full=False, tag=TAGS[4])
        pacer.sent(2.2, 100, full=False, tag=TAGS[5])
        self.assertEqual(([frame.rto for frame in pacer.inflight], pacer.ready(2.3)), ([1.0, 1.0], False))
        pacer.expire(3.1)
        pacer.sent(3.1, 100, full=False, tag=TAGS[6])  # the probe
        pacer.expire(3.2)
        pacer.sent(3.2, 100, full=False, tag=TAGS[7])
        self.assertEqual([frame.rto for frame in pacer.inflight], [2.0, 1.0])
        pacer.expire(4.19)
        self.assertFalse(pacer.disabled)
        pacer.expire(4.2)
        self.assertTrue(pacer.disabled)
        pacer = AckPacer("fixed", tagged=True)
        lose_three(pacer, TAGS[1:4])
        pacer.acked(1.5, TAGS[1])
        pacer.sent(1.5, 100, full=False, tag=TAGS[4])
        pacer.sent(1.6, 100, full=False, tag=TAGS[5])
        self.assertEqual([frame.rto for frame in pacer.inflight], [2.0, 1.0])

    def test_no_probe_goes_out_while_clock_pacing_is_on(self):
        """F4 of the S16 review: a FIFO pacer tracks the frames sent while clock pacing is on, and a probe among them,
        kept in flight across the outage, was taken by the first reply after it, which answered an older query: a
        sample of 3-8 s, and a 6 s timeout. None of them is a probe, even once the probe kept from before clock pacing
        has failed too."""
        pacer = AckPacer("fixed")
        pacer.sent(0.0, 100, full=False)
        pacer.expire(1.0)
        for n in range(3):  # the probe (2 s), then two frames lost in a second: clock pacing, the probe waiting on
            pacer.sent(1.0 + n * 0.01, 100, full=False)
        pacer.expire(2.02)
        pacer.expire(3.0)  # the probe's timeout failed too
        self.assertEqual((pacer.disabled, pacer.backoff, list(pacer.inflight)), (True, 2, []))
        pacer.sent(3.0, 100, full=False)
        self.assertEqual([(frame.backoff, frame.rto) for frame in pacer.inflight], [(0, 1.0)])

    def test_the_probe_in_flight_when_clock_pacing_took_over_teaches_nothing(self):
        """F2p-a, F3 and the critic's finding of the S16 review: the probe stays in flight under clock pacing, so a
        link slower than the timeout is still matched, but its round trip then spans the outage. Taken as a sample it
        made the timeout twice the outage's length, and with two frames queued behind the backlog of a bandwidth-
        limited link it backed W off to the floor for 2 s. It ends clock pacing and keeps the backoff, as a late
        reply does, and the next probe waits as long."""
        pacer, t = AckPacer("adaptive", tagged=True), 0.0
        for n in range(20):
            t = exchange(pacer, t, 0.1, tag=TAGS[n % 8])
        pacer.sent(t, 100, full=False, tag=TAGS[1])
        pacer.expire(t + 1.0)
        for n, tag in enumerate(TAGS[2:5]):
            pacer.sent(t + 1.0 + n * 0.01, 100, full=False, tag=tag)  # the probe (2 s), then two frames
        pacer.expire(t + 2.02)
        self.assertEqual((pacer.disabled, [(f.tag, f.spanned) for f in pacer.inflight]), (True, [(TAGS[2], True)]))
        before = pacer.srtt, pacer.last_rtt, list(pacer.rtts), list(pacer.recent), pacer.window
        pacer.acked(t + 2.5, TAGS[2])  # the outage ends: the probe's reply, 1.5 s after it went out
        self.assertEqual((pacer.disabled, pacer.backoff, pacer.inflight), (False, 1, deque()))
        self.assertEqual((pacer.srtt, pacer.last_rtt, list(pacer.rtts), list(pacer.recent)), before[:4])
        pacer.sent(t + 2.5, 100, full=False, tag=TAGS[5])
        self.assertEqual(pacer.inflight[0].rto, 2.0)

    def test_the_probe_stays_spanned_when_the_game_learns_a_size_meanwhile(self):
        """MUT-spanned-pins of the S17 review: resized() keeps the flag, or the probe's reply after the outage would be
        a sample again: a timeout of about twice the outage."""
        pacer, t = spanned_probe()
        pacer.resized(70, 20)
        before = pacer.srtt, pacer.last_rtt
        pacer.acked(t + 2.5, TAGS[2])
        self.assertEqual((pacer.srtt, pacer.last_rtt, pacer.backoff, pacer.disabled), (*before, 1, False))

    def test_a_frame_answered_in_flight_after_the_spanned_probe_is_a_sample(self):
        """MUT-spanned-pins of the S17 review: the probe's reply was lost in the outage, and the first frame answered
        in flight after it settles it too; the frame answered decides, not the probe."""
        pacer, t = spanned_probe()
        pacer.acked(t + 2.5, TAGS[1])  # a frame given up on before the probe: ack pacing again
        pacer.sent(t + 2.6, 100, full=False, tag=TAGS[5])
        pacer.acked(t + 2.7, TAGS[5])
        self.assertEqual((pacer.backoff, round(pacer.last_rtt, 6), len(pacer.inflight)), (0, 0.1, 0))

    def test_a_fifo_frame_sent_clock_pacing_is_no_sample(self):
        """DOC-fifo-clock-paced-samples of the S17 review: with DSR or DA1 acks the frames sent while clock pacing is
        on are tracked in flight, and their round trips may span the outage. Their replies end clock pacing but are no
        sample, as with CPR acks. (The replies to the frames given up on before still answer newer frames in flight,
        as a FIFO reply names none: a known limit of the fallback.)"""
        pacer = AckPacer("adaptive")
        lose_three(pacer)
        pacer.sent(1.1, 100, full=False)
        self.assertEqual([(frame.backoff, frame.spanned) for frame in pacer.inflight], [(0, True)])
        pacer.acked(1.95)
        self.assertEqual((pacer.disabled, pacer.srtt, pacer.last_rtt, pacer.backoff), (False, None, None, 1))

    def test_a_probe_answered_in_flight_teaches_the_timeout_but_not_the_queue(self):
        """The critic's finding of the S16 review: a probe's round trip may include a short outage, which tells nothing
        of a queue. Taken for one, with the two frames after it queued behind the drain of a bandwidth-limited link,
        it backed W off to the floor. It is a sample for srtt and the timeout only."""
        pacer, t = settled()
        pacer.sent(t, 100, full=False)
        pacer.expire(t + 1.0)
        pacer.sent(t + 1.0, 100, full=False)  # the probe
        recent, raw = list(pacer.recent), list(pacer.raw)
        pacer.acked(t + 1.9)
        self.assertEqual((round(pacer.last_rtt, 6), pacer.backoff), (0.9, 0))
        self.assertEqual((list(pacer.recent), list(pacer.raw), pacer.window), (recent, raw, 6.0))
        for n in range(2):  # queued behind the drain
            pacer.sent(t + 1.9 + n * TICK, 100, full=False)
        pacer.acked(t + 2.2)
        pacer.acked(t + 2.2 + TICK)
        self.assertEqual((pacer.window, pacer.congested), (6.0, False))

    def test_a_probe_answered_under_the_timeout_a_frame_at_no_backoff_had_may_grow_w(self):
        """S17-probe-no-growth of the S17 review: no probe's sample reached _adapt, so after a short outage W stayed a
        step lower, and a terminal that stopped answering then cost two timeouts. A probe answered under the timeout a
        frame at no backoff had (1 s here) cannot have waited out an outage: it grows W, never backs it off. Answered
        later, it may have, and W stays."""
        windows = []
        for rtt in (0.1, 1.5):
            pacer, t = AckPacer("adaptive"), 0.0
            for _ in range(5):
                t = exchange(pacer, t, 0.1)
            pacer.sent(t, 100, full=False)
            pacer.expire(t + 1.0)
            t += 1.0
            pacer.sent(t, 100, full=False)  # the probe: 2 s
            self.assertTrue(pacer.ready(t + TICK) and pacer.limited)
            pacer.sent(t + TICK, 100, full=False)
            pacer.acked(t + rtt)  # the probe's reply
            self.assertEqual((pacer.backoff, round(pacer.last_rtt, 6)), (0, rtt))
            windows.append(pacer.window)
        self.assertEqual(windows, [2.5, 2.0])

    def test_frames_whose_round_trip_spans_clock_pacing_hold_no_slot(self):
        """Frames out while clock pacing was on, a FIFO pacer's or the probe kept in flight, may be answered after it
        or never: the window counts only the others, so play goes on from the floor when ack pacing comes back."""
        for mode in ("fixed", "adaptive"):
            with self.subTest(mode=mode):
                pacer = AckPacer(mode)
                lose_three(pacer)
                for n in range(4):
                    pacer.sent(1.1 + n * TICK, 100, full=False)
                pacer.acked(1.2)  # the first of them answered: ack pacing again, three still in flight
                self.assertEqual((pacer.disabled, len(pacer.inflight)), (False, 3))
                self.assertTrue(all(frame.spanned for frame in pacer.inflight))
                self.assertTrue(pacer.ready(1.2))
                pacer.sent(1.2, 100, full=False)
                pacer.sent(1.2 + TICK, 100, full=False)
                self.assertFalse(pacer.ready(1.2 + 2 * TICK))

    def test_one_look_and_several_give_the_same_backoff(self):
        """The S16 review: expire() took overdue frames in send order. A probe sent before an answer in flight reset
        the backoff then expired ahead of a newer frame lost before it, uncounted, and the backoff depended on how
        often the loop looked. They go in the order they were lost."""
        backoffs = []
        for looks in ((2.96, 3.0), (3.0,)):
            pacer = AckPacer("fixed")
            pacer.sent(0.0, 100, full=False)
            pacer.sent(0.9, 100, full=False)
            pacer.expire(1.0)
            pacer.sent(1.0, 100, full=False)  # the probe, lost at 3.0
            pacer.acked(1.05)  # the frame sent at 0.9, in flight: the backoff is 0 again
            pacer.sent(1.95, 100, full=False)  # lost at 2.95
            for at in looks:
                pacer.expire(at)
            backoffs.append(pacer.backoff)
        self.assertEqual(backoffs, [2, 2])

    def test_a_probe_lost_before_a_newer_frame_raises_nothing_once_a_sample_reset_the_backoff(self):
        """MUT-gaps of the S17 review, the other order. The probe, lost first, is given up on first, and raises
        nothing: a sample reset the backoff, so its timeout is no longer the one in force. The newer frame's raises it
        once, however often the loop looks."""
        backoffs = []
        for looks in ((3.0, 3.6), (3.6,)):
            pacer = AckPacer("fixed")
            pacer.sent(0.0, 100, full=False)
            pacer.sent(0.9, 100, full=False)
            pacer.expire(1.0)
            pacer.sent(1.0, 100, full=False)  # the probe, lost at 3.0
            pacer.acked(1.05)  # the frame sent at 0.9, in flight: the backoff is 0 again
            pacer.sent(2.5, 100, full=False)  # lost at 3.5
            for at in looks:
                pacer.expire(at)
                backoffs.append(pacer.backoff)
        self.assertEqual(backoffs, [0, 1, 1])

    def test_a_probe_on_a_slower_link_doubles_the_timeout_in_force(self):
        """The probe's timeout is the timeout as it went out, doubled: 4 s where the timeout is 2 s."""
        pacer = AckPacer("fixed")
        exchange(pacer, 0.0, 0.5)
        pacer.sent(1.0, 100, full=False)
        pacer.expire(3.0)
        pacer.sent(3.0, 100, full=False)
        self.assertEqual((pacer.backoff, pacer.inflight[0].rto, pacer.deadline()), (1, 4.0, 7.0))

    def test_the_timeout_covers_twice_the_last_round_trip(self):
        """A round trip far above srtt (a spike, or the first one of a slower link) keeps the next frames in flight
        until twice as long, so their replies are samples and srtt follows."""
        pacer = AckPacer("fixed")
        exchange(pacer, 0.0, 0.1)
        exchange(pacer, 1.0, 0.9)
        self.assertAlmostEqual(pacer.srtt, 0.875 * 0.1 + 0.125 * 0.9)
        pacer.sent(2.0, 100, full=False)
        self.assertAlmostEqual(pacer.deadline(), 3.8)
        pacer.acked(2.1)
        pacer.sent(3.0, 100, full=False)
        self.assertAlmostEqual(pacer.deadline(), 4.0)

    def test_a_link_slower_than_the_loss_timeout_from_the_first_frame(self):
        pacer = AckPacer("adaptive", tagged=True)
        link = Link(pacer)
        seen = link.run(1.2, 40.0)
        self.assertAlmostEqual(pacer.srtt, 1.2, delta=0.01)
        self.assertEqual(pacer.disabled, False)
        self.assertGreater(per_second(seen, link.now - 20, link.now), 8)
        assert_in_step(self, pacer, link)

    def test_a_link_whose_frames_take_longer_than_the_timeout_to_cross_is_learned_not_flooded(self):
        """LAW17-1 of the S17 review: frames of 3-6 KB that take 1-1.2 s each to cross a link of 3-5 KB/s, every
        reply coming. The probe's slot freed after the timeout of a frame at no backoff (S17), a third frame joined
        the queue, two were lost per timeout between the late replies, and clock pacing took over and flooded the
        link: 6-13 spells in 60 s, frames queued 4.5-25 minutes. Holding its slot, the probe is answered in flight."""
        for rtt, rate, size in ((0.1, 5000, 6000), (0.05, 3000, 3000), (0.25, 5000, 5000)):
            with self.subTest(rtt=rtt, rate=rate, size=size):
                pacer = AckPacer("adaptive", tagged=True)
                spells, worst = bottleneck(pacer, rtt, rate, size, 60.0)
                self.assertEqual(spells, 0)
                self.assertLess(worst, 10.0)
                self.assertGreater(pacer.srtt, size / rate)


class OutageTests(unittest.TestCase):
    """Links that stop and start again. With CPR acks the pacer is back in step at the first reply to a frame in
    flight, and no reply gives a round trip shorter than the link's."""

    def back_in_step(self, pacer, link, rtt):
        assert_in_step(self, pacer, link)
        self.assertGreaterEqual(min(sample for _, sample in pacer.rtts), rtt - 1e-9)

    def test_after_an_outage_every_late_reply_answers_its_own_frame(self):
        """The line carries nothing for 1.5 s on a 100 ms round trip (the S12 outage runs): the frames sent before are
        given up on and clock pacing takes over, then their replies come all at once, or a millisecond apart (F3 of
        the S12 review), ahead of the replies to the frames sent since. Matched in order, they gave round trips near
        zero for good: base RTT fell under a millisecond and W sat at the floor."""
        for spread in (0.0, 0.001):
            with self.subTest(spread=spread):
                pacer = AckPacer("adaptive", tagged=True)
                link = Link(pacer, outages=[(10.0, 11.5)], spread=spread)
                link.run(0.1, 11.45)
                self.assertTrue(pacer.disabled)
                self.assertTrue(pacer.late)  # given up on, their replies still to come
                link.run(0.1, 2.5)
                self.back_in_step(pacer, link, 0.1)
                self.assertAlmostEqual(pacer.base_rtt, 0.1)
                seen = link.run(0.05, 10.0)  # a faster route
                self.back_in_step(pacer, link, 0.05)
                self.assertAlmostEqual(pacer.base_rtt, 0.05)
                self.assertGreater(per_second(seen, link.now - 5, link.now), 59)

    def test_after_an_outage_a_terminal_that_stops_answering_is_given_up_on_as_soon_as_before(self):
        """F2 of the S14 review: the late replies after a 5 s outage on a 100 ms link have round trips of up to 5 s.
        Smoothed into srtt for good, they made the loss timeout 8.4 s, so a terminal that stopped answering 0.3 s
        after the link came back held a full window for 5.9 s before clock pacing took over; now 1.1 s, as on S13."""
        stop = 10.3
        pacer = AckPacer("adaptive", tagged=True)
        link = Link(pacer, outages=[(5.0, 10.0)], spread=0.0005, lost=lambda t: t >= stop)
        link.run(0.1, stop)
        self.assertLess(pacer.srtt, 0.25)
        while not pacer.disabled and link.now < 20.0:
            link.run(0.1, TICK)
        self.assertLess(link.now - stop, 1.1)

    def test_a_terminal_that_stops_answering_during_an_outage_is_given_up_on_within_two_seconds(self):
        """F2 of the S15 review: the terminal stops answering during a 5 s outage on a 100 ms link, or as it ends, so no
        frame in flight is answered after the late replies. Their round trips, up to 5 s, made the loss timeout 14 s
        (S14, S15), and the game drew 4 frames in 28 s before clock pacing took over. The backoff is 2 when the
        outage ends, so the first frame out is a 4 s probe; but it holds a slot of the window only for the timeout of
        a frame at no backoff, so the frames beside it time out after 1 s and the next after 2 s, as on a link that
        stops answering (S13). Holding its slot all along (S16), it left one slot: three timeouts, at a frame a
        second (F1 and F2p-b of the S16 review)."""
        for rtt in (0.1, 0.25):
            for stop in (5.0, 6.0, 9.0, 10.0):
                with self.subTest(rtt=rtt, stop=stop):
                    pacer = AckPacer("adaptive", tagged=True)
                    link = Link(pacer, outages=[(5.0, 10.0)], spread=0.0005, lost=lambda t, stop=stop: t >= stop)
                    link.run(rtt, 10.05)
                    self.assertFalse(pacer.disabled)  # the late replies turned ack pacing back on
                    while not pacer.disabled and link.now < 40.0:
                        link.run(rtt, TICK)
                    self.assertLess(link.now - 10.0, 2.1)
                    self.assertLess(pacer.srtt, rtt + 0.01)

    def test_a_terminal_that_stops_answering_as_a_short_outage_ends_is_given_up_on_within_two_seconds(self):
        """F2p-a of the S16 review: an outage of 2 to 2.5 s, the probe out across it, and the terminal stops answering
        during it or as it ends. The probe stayed in flight under clock pacing and was answered after the late
        replies with a round trip of about the outage's length, a sample (S16): the timeout, twice it, held the window
        4-7 s at a frame every 2-3.5 s. Its round trip spans the outage, so it teaches nothing (Karn's rule), as on
        S13 there was no probe to answer."""
        for rtt in (0.1, 0.25):
            for up in (7.0, 7.5):
                for stop in (6.0, up):
                    with self.subTest(rtt=rtt, up=up, stop=stop):
                        pacer = AckPacer("adaptive", tagged=True)
                        link = Link(pacer, outages=[(5.0, up)], spread=0.0005, lost=lambda t, stop=stop: t >= stop)
                        sends, sent = [], pacer.sent
                        pacer.sent = lambda now, *args, **kwargs: (sends.append(now), sent(now, *args, **kwargs))
                        link.run(rtt, up + 0.02)
                        self.assertFalse(pacer.disabled)
                        self.assertAlmostEqual(pacer.srtt, rtt)  # the probe's round trip taught nothing
                        while not pacer.disabled and link.now < up + 30:
                            link.run(rtt, TICK)
                        after = [t for t in sends if t >= up]
                        self.assertLess(link.now - up, 2.1)
                        self.assertLess(max(b - a for a, b in zip(after, after[1:])), 1.1)

    def test_a_terminal_that_stops_answering_just_after_a_short_outage_is_given_up_on_within_two_timeouts(self):
        """L2 of the S15 review: an outage of 0.9 s ends between the first timeout and the third, and the terminal
        stops answering 15 ms later. The round trips of the frames in flight across it are about its length, and
        smoothed in they leave the timeout at 2.4-2.6 s. Clock pacing took over 4.6-6.2 s after (S15), and 6.2-7.8 s
        with every frame backed off, the game drawing a frame every 4-5 s meanwhile; now within two timeouts, and a
        frame at least every timeout."""
        for up in (5.905, 5.915, 5.925):
            with self.subTest(up=up):
                pacer = AckPacer("adaptive", tagged=True)
                link = Link(pacer, outages=[(5.0, up)], spread=0.0005, lost=lambda t, up=up: t >= up + 0.015)
                link.run(0.1, up + 0.05)
                timeout, sends, sent = pacer._timeout(), [link.now], pacer.sent
                self.assertLess(timeout, 2.7)
                pacer.sent = lambda now, *args, **kwargs: (sends.append(now), sent(now, *args, **kwargs))
                while not pacer.disabled and link.now < 20.0:
                    link.run(0.1, TICK)
                self.assertLess(link.now - up, 2 * timeout + 0.1)
                self.assertLess(max(b - a for a, b in zip(sends, sends[1:])), timeout + 0.05)

    def test_outage_after_outage_leaves_the_base_rtt_alone(self):
        """F3 of the S12 review: six 2 s outages 12 s apart, each backlog draining a millisecond apart. Each round
        trip measured near zero lowered the base RTT the next outage was judged by."""
        pacer = AckPacer("adaptive", tagged=True)
        link = Link(pacer, outages=[(10.0 + 12 * n, 12.0 + 12 * n) for n in range(6)], spread=0.001)
        seen = link.run(0.1, 10.0)
        for n in range(6):
            seen += link.run(0.1, 12.0)
            self.back_in_step(pacer, link, 0.1)
            self.assertAlmostEqual(pacer.base_rtt, 0.1)
            self.assertGreater(per_second(seen, link.now - 4, link.now), 55)

    def test_an_outage_before_the_first_reply(self):
        """F7 of the S12 review: no round trip measured yet when the first frames are given up on."""
        for rtt in (0.05, 0.25):
            for length in (1.0, 5.0):
                with self.subTest(rtt=rtt, length=length):
                    pacer = AckPacer("adaptive", tagged=True)
                    link = Link(pacer, outages=[(0.05, 0.05 + length)])
                    link.run(rtt, 0.05 + length + 3.0)
                    self.back_in_step(pacer, link, rtt)
                    self.assertAlmostEqual(pacer.base_rtt, rtt)

    def test_an_rtt_that_doubles_during_an_outage(self):
        """F8 of the S12 review: the late replies are then no sooner than the round trip of the frames in flight."""
        pacer = AckPacer("adaptive", tagged=True)
        link = Link(pacer, outages=[(10.0, 13.0)])
        link.run(0.1, 11.5)
        link.run(0.2, 4.5)
        self.back_in_step(pacer, link, 0.1)
        link.run(0.2, 10.0)
        self.back_in_step(pacer, link, 0.2)
        self.assertAlmostEqual(pacer.base_rtt, 0.2)

    def test_an_outage_longer_than_frames_given_up_on_are_kept(self):
        """F7 of the S12 review's tests lens: 12 s, over LATE_KEEP. The replies to the frames given up on then match
        nothing: only the frames sent while clock pacing was on are still known."""
        pacer = AckPacer("adaptive", tagged=True)
        link = Link(pacer, outages=[(10.0, 22.0)])
        link.run(0.1, 21.9)
        self.assertEqual((pacer.disabled, {frame.tag for frame in pacer.late}), (True, {OFF}))
        link.run(0.1, 3.0)
        self.back_in_step(pacer, link, 0.1)
        self.assertAlmostEqual(pacer.base_rtt, 0.1)

    def test_replies_lost_then_a_faster_route(self):
        """F2 of the S12 review: replies lost (for a second, or just three), then a route 50 ms faster, at once or 3 s
        later. The rule that skipped replies too soon for the oldest frame skipped the real ones, and the game ran at
        2-4 fps for good."""
        for lost in ((600, 660), (600, 603)):  # ticks
            for later in (0.0, 3.0):
                with self.subTest(lost=lost, later=later):
                    pacer = AckPacer("adaptive", tagged=True)
                    link = Link(pacer, lost=lambda t, lost=lost: lost[0] <= round(t / TICK) < lost[1])
                    link.run(0.1, 11.0 + later)
                    seen = link.run(0.05, 12.0)
                    self.back_in_step(pacer, link, 0.05)
                    self.assertAlmostEqual(pacer.base_rtt, 0.05)
                    self.assertGreater(per_second(seen, link.now - 5, link.now), 59)

    def test_replies_lost_on_a_link_under_two_ticks(self):
        """F9 of the S12 review: a second of replies lost on a 20 ms link. In order, the reply to the first frame sent
        once the window opened again answered a frame whose reply was lost, and every round trip after read a frame
        interval long."""
        pacer = AckPacer("adaptive", tagged=True)
        link = Link(pacer, lost=lambda t: 600 <= round(t / TICK) < 660)
        link.run(0.02, 15.0)
        self.back_in_step(pacer, link, 0.02)
        self.assertAlmostEqual(max(sample for t, sample in pacer.rtts if t > 12.5), 0.02)

    def test_one_reply_lost_mid_stream(self):
        """F1 of the S12 review's tests lens: one reply lost for good while newer frames are in flight. In order every
        later round trip read a frame interval long and a phantom frame counted in flight, until a pause long enough
        for it to expire, and for good once the base RTT had taken the shifted samples."""
        pacer = AckPacer("adaptive", tagged=True)
        link = Link(pacer, lost=lambda t: round(t / TICK) == 300)
        link.run(0.1, 5.5)
        self.back_in_step(pacer, link, 0.1)
        link.run(0.1, 10.0)
        self.assertAlmostEqual(max(sample for t, sample in pacer.rtts if t > 5.2), 0.1)
        self.assertAlmostEqual(pacer.base_rtt, 0.1)


class PaceSimTests(unittest.TestCase):
    """The tool reruns the scenarios behind the table in spec 8.4 through the real AckPacer."""

    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()
        cls.rows = cls.tool.table()  # computed once: the CLI test prints the same rows

    def test_the_table_reproduces_within_ten_percent(self):
        self.assertEqual(len(self.rows), 2 * len(self.tool.SCENARIOS))
        for label, mode, result, (fps, p95) in self.rows:
            with self.subTest(label, mode=mode):
                self.assertAlmostEqual(result.fps, fps, delta=0.1 * fps)
                self.assertAlmostEqual(result.lag_p95 * 1000, p95, delta=0.1 * p95)

    def test_adaptive_meets_the_lag_goal_where_fixed_fails(self):
        fixed, adaptive = (self.tool.run(mode, 0.1, self.tool.const(1150)) for mode in ("fixed", "adaptive"))
        self.assertGreater(fixed.lag_p95, 0.1)
        self.assertLess(adaptive.lag_p95, 0.05)
        self.assertGreater(adaptive.fps, 59)

    def test_cli_prints_the_table_or_one_link(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.tool.main(["--rtt", "0.05", "--size", "300", "--jump", "6500", "--secs", "12"])
        lines = out.getvalue().splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["fixed", "adaptive", "off"])
        self.assertIn("fps", lines[0])
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(self.tool, "table", lambda: self.rows):
            self.tool.main([])
        self.assertEqual(len(out.getvalue().splitlines()), 1 + 2 * len(self.tool.SCENARIOS))
        self.assertTrue(math.isnan(self.tool._pct([], 0.5)))


if __name__ == "__main__":
    unittest.main()
