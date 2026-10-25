"""Simulated keyboards: OS repeat streams, terminal encodings (checked through the Decoder) and SSH delivery."""

import random
import unittest

from terminal_invaders.rng import Rng
from terminal_invaders.term.decode import Decoder
from terminal_invaders.term.keys import ALT, CTRL, SHIFT, Key
from terminal_invaders.term.simkeys import PROFILES, Link, RepeatProfile, encode, key_events, stream

NAMES = ("a", "z", "1", ";", "\\", "é", "space", "enter", "tab", "backspace", "escape", "up", "down", "left")
NAMES += ("right", "home", "end", "insert", "delete", "page_up", "page_down", "f1", "f2", "f3", "f4", "f5", "f12")


def decoded(data, kitty):
    decoder = Decoder(esc_timeout=0.05, string_timeout=0.5, kitty=kitty)
    decoder.feed(data, 0.0)
    decoder.expire(1.0)
    return [(key.name, key.mods, key.kind, key.base) for key in decoder.events()]


class ProfileTests(unittest.TestCase):
    def test_presets(self):
        table = {name: (profile.delay, profile.interval) for name, profile in PROFILES.items()}
        self.assertEqual(
            table,
            {
                "macos-default": (0.525, 0.090),
                "macos-fast": (0.225, 0.030),
                "gnome": (0.500, 0.030),
                "kde": (0.600, 0.040),
                "xorg": (0.660, 0.040),
            },
        )


class KeyEventTests(unittest.TestCase):
    def test_legacy_hold_repeats_as_presses_without_release(self):
        events = key_events([("d", 1.0, 1.75)], RepeatProfile("test", 0.5, 0.1), kitty=False)
        self.assertEqual([round(key.time, 3) for key in events], [1.0, 1.5, 1.6, 1.7])
        self.assertTrue(all(key.kind == "press" and not key.certain for key in events))

    def test_kitty_hold_reports_repeat_and_release(self):
        events = key_events([("d", 1.0, 1.75)], RepeatProfile("test", 0.5, 0.1), kitty=True)
        self.assertEqual([key.kind for key in events], ["press", "repeat", "repeat", "repeat", "release"])
        self.assertTrue(all(key.certain for key in events))
        self.assertEqual(events[-1].time, 1.75)

    def test_a_newer_press_ends_the_older_repeat_for_good(self):
        profile = RepeatProfile("test", 0.5, 0.1)
        events = key_events([("space", 0.6, 0.8), ("right", 0.0, 3.0)], profile, kitty=False)
        self.assertEqual(
            [(key.name, round(key.time, 3)) for key in events], [("right", 0.0), ("right", 0.5), ("space", 0.6)]
        )
        events = key_events([("space", 0.6, 0.8), ("right", 0.0, 3.0)], profile, kitty=True)
        self.assertEqual([(key.name, key.kind) for key in events][-2:], [("space", "release"), ("right", "release")])


class EncodeTests(unittest.TestCase):
    def test_kitty_round_trip(self):
        for name in NAMES + ("pause", "f13", "f35"):
            for mods in (0, SHIFT, CTRL, ALT | CTRL):
                for kind in ("press", "repeat", "release"):
                    with self.subTest(name=name, mods=mods, kind=kind):
                        data = encode(Key(name, mods, kind), kitty=True)
                        self.assertEqual(decoded(data, kitty=True), [(name, mods, kind, None)])

    def test_kitty_base_layout_key(self):
        data = encode(Key("q", kind="repeat", base="a"), kitty=True)
        self.assertEqual(data, b"\x1b[113::97;1:2u")
        self.assertEqual(decoded(data, kitty=True), [("q", 0, "repeat", "a")])

    def test_legacy_round_trip(self):
        for name in NAMES:
            extra = (CTRL, SHIFT) if name == "z" else (CTRL,) if name in ("up", "f3", "f5") else ()
            # legacy Alt+Escape and Alt+non-ASCII read as Escape followed by the key
            for mods in (0,) + ((ALT,) if name not in ("escape", "é") else ()) + extra:
                with self.subTest(name=name, mods=mods):
                    data = encode(Key(name, mods), kitty=False)
                    self.assertEqual(encode(Key(name, mods, "repeat"), kitty=False), data)
                    self.assertEqual(decoded(data, kitty=False), [(name, mods, "press", None)])
        self.assertEqual(encode(Key("tab", SHIFT), kitty=False), b"\x1b[Z")
        self.assertEqual(encode(Key("a", kind="release"), kitty=False), b"")

    def test_legacy_forms(self):
        cases = {Key("f1"): b"\x1bOP", Key("f3", SHIFT): b"\x1b[1;2R", Key("left"): b"\x1b[D", Key("c", CTRL): b"\x03"}
        for key, data in cases.items():
            self.assertEqual(encode(key, kitty=False), data)
        for name in ("pause", "f13"):
            with self.assertRaises(ValueError):
                encode(Key(name), kitty=False)


class LinkTests(unittest.TestCase):
    def test_delivery_is_delayed_jittered_slotted_and_in_order(self):
        sent = [(0.1 * n, n) for n in range(200)]
        link = Link(slot=0.02, jitter=0.03, rtt=0.1)
        arrived = link.deliver(sent, random.Random(3))
        self.assertEqual([item for _, item in arrived], list(range(200)))
        self.assertEqual(arrived, sorted(arrived))
        for (sent_at, _), (at, _) in zip(sent, arrived):
            self.assertAlmostEqual(at / 0.02, round(at / 0.02))
            self.assertTrue(sent_at + 0.05 <= at < sent_at + 0.05 + 0.03 + 0.02 + 1e-9)
        self.assertEqual(link.deliver(sent, random.Random(3)), arrived)

    def test_unslotted_and_with_the_game_rng(self):
        arrived = Link(slot=0, jitter=0.01).deliver([(1.0, "a"), (1.0, "b")], Rng(5))
        self.assertTrue(all(1.0 <= at < 1.01 for at, _ in arrived))
        self.assertLessEqual(arrived[0][0], arrived[1][0])
        self.assertEqual(Link(slot=0, jitter=0).deliver([(2.0, "x")], Rng(1)), [(2.0, "x")])

    def test_stream_of_terminal_bytes(self):
        profile = RepeatProfile("test", 0.5, 0.1)
        legacy = stream([("b", 0.0, 0.65)], profile, kitty=False)
        self.assertEqual(legacy, [(0.0, b"b"), (0.5, b"b"), (0.6, b"b")])
        self.assertEqual(stream([("b", 0.0, 0.2)], profile, kitty=True), [(0.0, b"\x1b[98u"), (0.2, b"\x1b[98;1:3u")])
        slotted = stream([("b", 0.0, 0.65)], profile, kitty=False, link=Link(), rng=random.Random(1))
        self.assertEqual([data for _, data in slotted], [b"b"] * 3)
