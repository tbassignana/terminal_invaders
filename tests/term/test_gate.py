"""KeyGate (spec 6.5): admission per action kind, kitty and legacy, and scene-change suppression."""

import unittest

from terminal_invaders.term.gate import KeyGate
from terminal_invaders.term.keys import Key
from terminal_invaders.term.repeat import RepeatModel
from terminal_invaders.term.simkeys import PROFILES, key_events


def gate(certain):
    return KeyGate(RepeatModel(remote=False), certain=certain)


def kitty(name, kind="press"):
    return Key(name, kind=kind, certain=True)


class KittyTests(unittest.TestCase):
    def test_admission_per_kind(self):
        expected = {
            "command": {"press": (True, True), "repeat": (False, False), "release": (False, False)},
            "pulse": {"press": (True, True), "repeat": (False, False), "release": (False, False)},
            "navigation": {"press": (True, True), "repeat": (True, False), "release": (False, False)},
            "control": {"press": (True, True), "repeat": (True, False), "release": (True, False)},
        }
        for kind, results in expected.items():
            current = gate(True)
            for event, result in results.items():
                with self.subTest(kind=kind, event=event):
                    self.assertEqual(current.admit(kitty("b", event), kind, 0.0), result)

    def test_scene_change_suppresses_held_keys_until_release(self):
        current = gate(True)
        current.admit(kitty("space"), "control", 0.0)
        current.admit(kitty("enter"), "command", 0.0)
        current.admit(kitty("enter", "release"), "command", 0.1)
        current.scene_changed(0.2)
        self.assertEqual(current.admit(kitty("space", "repeat"), "navigation", 0.3), (False, False))
        self.assertEqual(current.admit(kitty("space", "release"), "control", 0.4), (False, False))
        self.assertEqual(current.admit(kitty("space"), "command", 0.5), (True, True))
        self.assertEqual(current.admit(kitty("enter"), "command", 0.3), (True, True))  # was not down

    def test_a_fresh_press_ends_suppression_when_the_release_was_lost(self):
        current = gate(True)
        current.admit(kitty("p"), "command", 0.0)
        current.scene_changed(0.1)
        self.assertEqual(current.admit(kitty("p"), "command", 0.5), (True, True))

    def test_a_repeat_without_a_seen_press_counts_as_down(self):
        current = gate(True)
        self.assertEqual(current.admit(kitty("left", "repeat"), "control", 0.0), (True, False))
        current.scene_changed(0.1)
        self.assertEqual(current.admit(kitty("left", "repeat"), "control", 0.2), (False, False))


class LegacyTests(unittest.TestCase):
    def hold(self, current, name, kind, profile="macos-default", start=0.0, length=1.5):
        events = key_events([(name, start, start + length)], PROFILES[profile], kitty=False)
        return [current.admit(event, kind, event.time) for event in events]

    def test_one_shot_keys_are_immune_to_auto_repeat(self):
        for kind in ("command", "pulse"):
            results = self.hold(gate(False), "b", kind)
            self.assertGreater(len(results), 5)
            self.assertEqual(sum(admitted for admitted, _ in results), 1)  # holding B spends one bomb
            self.assertEqual(results[0], (True, True))

    def test_navigation_and_control_see_every_event(self):
        results = self.hold(gate(False), "down", "navigation")
        self.assertTrue(all(admitted for admitted, _ in results))  # holding Down scrolls a menu
        results = self.hold(gate(False), "left", "control")
        self.assertTrue(all(admitted for admitted, _ in results))
        self.assertEqual([edge for _, edge in results[:3]], [True, True, False])  # the unlearned first repeat

    def test_scene_change_suppresses_recent_keys_until_quiet(self):
        current = gate(False)
        current.admit(Key("enter"), "command", 0.0)
        current.admit(Key("q"), "command", 5.0)
        current.scene_changed(5.1)
        self.assertEqual(current.admit(Key("q"), "control", 5.2), (False, False))  # a re-tap, or its first repeat
        self.assertEqual(
            current.admit(Key("q"), "navigation", 5.25), (False, False)
        )  # repeating: still within the guard
        self.assertEqual(current.admit(Key("enter"), "command", 6.0), (True, True))  # quiet long before
        self.assertEqual(current.admit(Key("q"), "command", 6.6), (True, True))  # quiet for the guard

    def test_a_held_key_stays_suppressed_while_it_repeats(self):
        current = gate(False)
        events = key_events([("space", 0.0, 3.0)], PROFILES["macos-default"], kitty=False)
        before = [current.admit(event, "command", event.time) for event in events if event.time < 1.0]
        current.scene_changed(1.0)
        after = [current.admit(event, "navigation", event.time) for event in events if event.time >= 1.0]
        self.assertEqual(sum(admitted for admitted, _ in before), 1)
        self.assertTrue(after and not any(admitted for admitted, _ in after))
        self.assertEqual(current.admit(Key("space"), "command", 4.0), (True, True))

    def test_a_press_that_cannot_be_a_first_repeat_ends_suppression(self):
        # Space launched the run: whether tapped again or held, it must fire once it is pressed anew (G2.1)
        for taps, admitted in (((0.25, 0.5), [False, True]), ((0.6, 1.2), [False, True]), ((0.3,), [False])):
            with self.subTest(taps=taps):
                current = gate(False)
                current.admit(Key("space"), "command", 0.0)
                current.scene_changed(0.0)
                results = [current.admit(Key("space"), "control", t) for t in taps]
                self.assertEqual([result[0] for result in results], admitted)
        current = gate(False)
        current.admit(Key("space"), "command", 0.0)
        current.scene_changed(0.0)
        current.admit(Key("d"), "control", 0.1)  # the OS repeats only the latest key: Space was let go
        self.assertEqual(current.admit(Key("space"), "control", 0.2), (True, True))

    def test_a_learned_delay_tells_a_re_tap_from_the_first_repeat_after_a_scene_change(self):
        current = gate(False)
        for event in key_events([("x", 0.0, 1.5)], PROFILES["macos-default"], kitty=False):
            current.admit(event, "control", event.time)
        self.assertAlmostEqual(current.repeat.delay, 0.525)
        current.admit(Key("space"), "command", 3.0)
        current.scene_changed(3.0)
        self.assertEqual(current.admit(Key("space"), "control", 3.52), (False, False))  # the delay: a first repeat
        current.admit(Key("space"), "command", 6.0)
        current.scene_changed(6.0)
        self.assertEqual(current.admit(Key("space"), "control", 6.3), (True, True))  # too early for a repeat

    def test_set_certain_switches_mode_and_clears_suppression(self):
        current = gate(True)
        current.admit(kitty("space"), "control", 0.0)
        current.scene_changed(0.1)
        current.set_certain(False)
        self.assertEqual(current.admit(Key("space"), "command", 0.2), (True, True))
        self.assertEqual(current.admit(Key("space"), "command", 0.3), (False, False))

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            gate(False).admit(Key("a"), "signal", 0.0)
