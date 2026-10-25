"""RepeatModel (spec 6.6) on synthetic OS auto-repeat streams, locally and over a jittered, slotted SSH link."""

import random
import unittest

from terminal_invaders.term.repeat import NEW_RUN, RepeatModel
from terminal_invaders.term.simkeys import PROFILES, Link, key_events

SSH = Link(slot=0.020, jitter=0.030)
QUIET = 2.5  # seconds between holds; longer than any guard


def stream(holds, profile, link=None, seed=0):
    events = [(key.time, key.name) for key in key_events(holds, profile, kitty=False)]
    return link.deliver(events, random.Random(seed)) if link else events


def classify(model, events):
    return [model.observe(name, at) for at, name in events]


def holds(name, count, start=0.0, length=1.5):
    return [(name, start + n * (length + QUIET), start + n * (length + QUIET) + length) for n in range(count)]


def scenarios():
    for profile in PROFILES.values():
        for link in (None, SSH):
            yield profile, link, link is not None


def trained(profile, link, remote):
    model = RepeatModel(remote=remote)
    classify(model, stream(holds("b", 2), profile, link, seed=1))
    return model


class StreamTests(unittest.TestCase):
    def test_a_hold_gives_exactly_one_safe_edge(self):
        for profile, link, remote in scenarios():
            for seed in range(5):
                with self.subTest(profile=profile.name, remote=remote, seed=seed):
                    results = classify(RepeatModel(remote=remote), stream(holds("b", 1), profile, link, seed))
                    self.assertGreater(len(results), 5)
                    self.assertEqual(results[0], (True, True))
                    self.assertEqual(sum(safe for _, safe in results), 1)

    def test_learning_converges_within_two_holds(self):
        for profile, link, remote in scenarios():
            for seed in range(5):
                with self.subTest(profile=profile.name, remote=remote, seed=seed):
                    model = RepeatModel(remote=remote)
                    events = stream(holds("b", 3), profile, link, seed)
                    third = 2 * (1.5 + QUIET) - 0.01
                    classify(model, [event for event in events if event[0] < third])
                    self.assertLessEqual(abs(model.delay - profile.delay), model.margin / 2)
                    results = classify(model, [event for event in events if event[0] >= third])
                    self.assertEqual(results[0], (True, True))
                    self.assertEqual(sum(edge for edge, _ in results), 1)
                    self.assertEqual(sum(safe for _, safe in results), 1)

    def test_taps_250ms_apart_give_edges(self):
        taps = [("d", 0.25 * n, 0.25 * n + 0.06) for n in range(12)]
        for profile, link, remote in scenarios():
            for seed in range(5):
                with self.subTest(profile=profile.name, remote=remote, seed=seed):
                    results = classify(RepeatModel(remote=remote), stream(taps, profile, link, seed))
                    self.assertEqual(results, [(True, True)] + [(True, False)] * 11)

    def test_trained_models_still_see_taps(self):
        taps = [("b", 5 * (1.5 + QUIET) + 0.25 * n, 5 * (1.5 + QUIET) + 0.25 * n + 0.06) for n in range(12)]
        for profile, link, remote in scenarios():
            model = trained(profile, link, remote)
            results = classify(model, stream(taps, profile, link, seed=2))
            edges = sum(edge for edge, _ in results)
            with self.subTest(profile=profile.name, remote=remote):
                if abs(0.25 - model.delay) > model.margin:
                    self.assertEqual(edges, 12)
                else:  # a re-tap at the learned delay is indistinguishable from the first repeat
                    self.assertGreaterEqual(edges, 6)

    def test_guard_and_burst_gap_bounds(self):
        for profile, link, remote in scenarios():
            model = trained(profile, link, remote)
            with self.subTest(profile=profile.name, remote=remote):
                self.assertTrue(0.06 <= model.burst_gap <= 0.35)
                self.assertEqual(model.guard, max(model.delay + model.margin, 2 * model.burst_gap))
                self.assertLess(model.guard, NEW_RUN)


class ClassificationTests(unittest.TestCase):
    def test_defaults(self):
        local, remote = RepeatModel(remote=False), RepeatModel(remote=True)
        self.assertEqual((local.guard, local.burst_gap, local.delay, local.remote), (0.70, 0.15, None, False))
        self.assertEqual((remote.guard, remote.burst_gap, remote.delay, remote.remote), (1.00, 0.20, None, True))

    def test_new_runs_are_safe_edges(self):
        model = RepeatModel(remote=False)
        self.assertEqual(model.observe("a", 0.0), (True, True))  # no previous event
        self.assertEqual(model.observe("b", 0.01), (True, True))  # another key
        self.assertEqual(model.observe("b", 0.71), (True, True))  # quiet for the guard
        self.assertEqual(model.observe("b", 0.72), (False, False))

    def test_second_event_of_a_run(self):
        local, remote = RepeatModel(remote=False), RepeatModel(remote=True)
        for model in (local, remote):
            model.observe("a", 0.0)
        self.assertEqual(local.observe("a", 0.05), (False, False))  # bounce under MIN_RETAP 75 ms
        self.assertEqual(remote.observe("a", 0.05), (True, False))  # MIN_RETAP is 40 ms over SSH
        model = RepeatModel(remote=False)
        model.observe("a", 0.0)
        self.assertEqual(model.observe("a", 0.3), (True, False))  # re-tap, or a first repeat not yet learned
        self.assertEqual(model.candidate, 0.3)

    def test_burst_and_restart(self):
        model = RepeatModel(remote=False)
        model.observe("a", 0.0)
        model.observe("a", 0.5)
        self.assertEqual(model.observe("a", 0.6), (False, False))  # within burst_gap
        self.assertEqual(model.observe("a", 0.95), (True, False))  # slower than a burst: the run restarts
        self.assertEqual(model.observe("a", 1.0), (False, False))  # the restarted run's second event: a bounce

    def test_learning_needs_three_fast_gaps_after_a_plausible_delay(self):
        model = RepeatModel(remote=False)
        for at in (0.0, 0.5, 0.6, 0.7):
            model.observe("a", at)
        self.assertIsNone(model.delay)
        model.observe("a", 0.8)
        self.assertAlmostEqual(model.delay, 0.5)
        model = RepeatModel(remote=False)
        for at in (0.0, 0.15, 0.25, 0.35, 0.45):  # 150 ms is too short for an OS delay
            model.observe("a", at)
        self.assertIsNone(model.delay)
        model = RepeatModel(remote=False)
        for at in (0.0, 0.5, 0.62, 0.74, 0.86, 0.98):  # 120 ms gaps continue the burst but do not confirm a delay
            model.observe("a", at)
        self.assertIsNone(model.delay)

    def test_learned_delay_is_an_average_and_recognised(self):
        model = RepeatModel(remote=False)
        for start, delay in ((0.0, 0.5), (5.0, 0.55)):
            for at in (start, start + delay, start + delay + 0.05, start + delay + 0.1, start + delay + 0.15):
                model.observe("a", at)
        self.assertAlmostEqual(model.delay, 0.7 * 0.5 + 0.3 * 0.55)
        self.assertAlmostEqual(model.guard, model.delay + 0.08)
        model.observe("a", 10.0)
        self.assertEqual(model.observe("a", 10.55), (False, False))  # within the margin of the learned delay
        model.observe("a", 15.0)
        self.assertEqual(model.observe("a", 15.6), (True, True))  # beyond the learned guard: a new press
        model.observe("a", 20.0)
        self.assertEqual(model.observe("a", 20.3), (True, False))

    def test_burst_gap_is_learned_and_clamped(self):
        model = RepeatModel(remote=False)
        at = 0.0
        for gap in (0.0, 0.5) + (0.01,) * 6:
            at += gap
            model.observe("a", at)
        self.assertEqual(model.burst_gap, 0.06)
        model = RepeatModel(remote=False)
        model.observe("a", 0.0)
        model.observe("a", 0.5)
        at, gap = 0.5, 0.14
        for _ in range(60):
            at += gap
            model.observe("a", at)
            gap = min(0.34, model.burst_gap - 0.001)
        self.assertEqual(model.burst_gap, 0.35)
        self.assertEqual(len(model.intervals), 48)


class PressOnlyTests(unittest.TestCase):
    """The press-only verdict (Arcade fire): every tap is a press, however fast, and a held key is one press; a
    possible first repeat at the learned delay waits (None) to see whether repeats follow."""

    def presses(self, model, events):
        return [(model.observe(name, at), model.press)[1] for at, name in events]

    def test_taps_faster_than_any_os_delay_are_all_presses(self):
        for gap in (0.08, 0.1, 0.125, 0.14, 0.19):
            taps = [("space", gap * n, gap * n + 0.04) for n in range(20)]
            for profile, link, remote in scenarios():
                if remote and gap > 0.1:
                    continue  # SSH jitter brings some of these gaps to a possible first repeat (FIRST_REPEAT)
                for seed in range(5):
                    with self.subTest(gap=gap, profile=profile.name, remote=remote, seed=seed):
                        events = stream(taps, profile, link, seed)
                        self.assertEqual(self.presses(RepeatModel(remote=remote), events), [True] * 20)
                        model = trained(profile, link, remote)
                        near = gap >= model.first_repeat and abs(gap - model.delay) <= model.margin
                        late = self.presses(model, events)  # near the learned delay each waits, and still counts
                        self.assertEqual(late, [True] + [None if near else True] * 19)

    def test_a_held_key_is_one_press_once_the_delay_is_learned(self):
        for profile, link, remote in scenarios():
            for seed in range(3):
                with self.subTest(profile=profile.name, remote=remote, seed=seed):
                    model = trained(profile, link, remote)
                    hold = [("space", 20.0, 22.0)]
                    verdicts = self.presses(model, stream(hold, profile, link, seed))
                    self.assertEqual(verdicts[:2], [True, None])  # the first repeat waits, then the repeats say hold
                    self.assertEqual(set(verdicts[2:]), {False})

    def test_before_the_delay_is_learned_a_hold_counts_its_first_repeat_once(self):
        for profile, link, remote in scenarios():
            with self.subTest(profile=profile.name, remote=remote):
                verdicts = self.presses(RepeatModel(remote=remote), stream(holds("space", 1), profile, link, 1))
                self.assertEqual(verdicts[:2], [True, True])  # a re-tap or the first repeat: no delay to tell
                self.assertEqual(set(verdicts[2:]), {False})

    def test_a_delay_longer_than_the_guard_still_starts_a_hold(self):
        model = RepeatModel(remote=False)
        verdicts = [(model.observe("space", at), model.press)[1] for at in (0.0, 1.02, 1.11, 1.20, 1.29, 1.38)]
        self.assertEqual(verdicts, [True, True, False, False, False, False])  # never a press for every repeat
        self.assertAlmostEqual(model.delay, 1.02)
        model.observe("space", 5.0)
        self.assertTrue(model.press)
        model.observe("space", 6.2)  # past the learned guard but not the delay: a new press, no hold to come
        model.observe("space", 6.33)
        self.assertTrue(model.press)

    def test_a_tap_at_the_learned_delay_waits_and_a_bounce_or_repeat_never_counts(self):
        model = RepeatModel(remote=False)
        for at in (0.0, 0.5, 0.55, 0.6, 0.65):
            model.observe("space", at)
        self.assertAlmostEqual(model.delay, 0.5)
        verdicts = []
        for at in (5.0, 5.5, 6.0, 6.03, 6.25, 6.27, 6.4):
            model.observe("space", at)
            verdicts.append(model.press)
        # 5.5 and 6.0 may be the first repeat; 6.03 is a repeat (so 6.0 was one); 6.25 is the next press, 6.27 a
        # bounce, and 6.4 a press: 150 ms is no repeat after a press
        self.assertEqual(verdicts, [True, None, None, False, True, False, True])
        self.assertLessEqual(model.hold_gap, model.first_repeat)
        model.observe("d", 6.45)
        model.observe("space", 6.5)
        self.assertTrue(model.press)  # another key ended the run
