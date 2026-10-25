"""The title demo (Phase 5 spec 5): the cycle on a fake clock, which tape plays, what a key does while it plays, and
that it never writes the profile, never sounds and never changes the game played after it."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from terminal_invaders import attract
from terminal_invaders.attract import BANNER, CYCLES, DEMO_LENGTH, IDLE, SCORES, Attract, ReadOnlyProfile, SilentAudio
from terminal_invaders.cli import defaults
from terminal_invaders.model import Config
from terminal_invaders.replay import RESULT_KEYS, Tape
from terminal_invaders.rules import make_game
from terminal_invaders.storage import Profile
from terminal_invaders.term.cells import CellBuffer
from terminal_invaders.term.keys import CTRL, PRESS, RELEASE, REPEAT, Key
from terminal_invaders.theme import resolve_theme
from terminal_invaders.ui import QUIT, Fits, Painter, Session
from tests.support import frames

MONO = resolve_theme(SimpleNamespace(color="mono", theme="dark", paint_background=False))
STEP = 1 / 60
ROOT = Path(__file__).resolve().parents[1]


def cut(source, ticks: int) -> Tape:
    """The first ticks of a golden tape as a tape of its own (its digest and result at the cut)."""
    game = make_game(source.config)
    inputs = {}
    for tick in range(ticks):
        actions = source.actions_at(tick)
        if actions:
            inputs[tick] = actions
        game.step(actions)
    result = game.result()
    return Tape(1, source.config, ticks, inputs, game.digest(), {key: result[key] for key in RESULT_KEYS})


@cache
def campaign_tape() -> Tape:
    return cut(frames.tape(), 240)


@cache
def arcade_tape() -> Tape:
    return cut(frames.arcade_tape(), 120)


def playback(tape):
    return SimpleNamespace(tapes=[tape])


def menu(profile=None, audio=None, tapes=None, room=Fits(True, True), **overrides):
    """A title menu Session whose demo plays the given tapes (short cuts of the goldens unless given)."""
    options = dict(seed=42, fixed_seed=True, quickstart=False, name="TEST", color="mono", attract="on") | overrides
    current = Session(defaults(**options), profile or frames.Profile(), audio or Mock(enabled=False))
    current.on_resize(room)
    if tapes is None:
        tapes = [playback(campaign_tape()), playback(arcade_tape())]
    current.attractor = Attract(current.options, tapes=tapes, idle=IDLE)
    return current


def loop(current, start: float, until: float) -> float:
    """The app loop on a fake clock: the demo's hook, then a tick of whatever is shown, every 1/60 s."""
    now = start
    while now < until:
        current.update_attract(now)
        (current.attract or current).step(now)
        now = round(now + STEP, 9)
    return now


def demo_running(current, start=0.0) -> float:
    """Sit on the title (or the scores) until the next demo starts, a tape playing when there is one; its time."""
    now = start
    while True:
        current.update_attract(now)
        if current.scene == "attract" and (current.attract is not None or not current.attractor.tapes):
            return now
        now = round(now + 0.5, 9)
    return now


class CycleTests(unittest.TestCase):
    """Spec 5.2: 30 s idle, the demo, the high scores for 10 s, the menu again; at most 10 cycles until a key."""

    def test_the_whole_cycle(self):
        current = menu()
        current.selection = 3
        self.assertFalse(current.update_attract(0.0))
        self.assertFalse(current.update_attract(IDLE - 0.1))
        self.assertTrue(current.update_attract(IDLE))
        self.assertEqual(current.scene, "attract")
        demo = current.attract
        self.assertTrue(demo.demo and demo.replay)
        self.assertEqual(demo.context, "attract")
        self.assertEqual(demo.pilot.game.config.mode, "classic")  # the Campaign tape first
        now = loop(current, IDLE, IDLE + 5)  # its 240 ticks are 4 s
        self.assertEqual((current.scene, current.attract), ("attract", None))  # the high scores
        self.assertIs(demo.pilot.verified, True)
        scores = current.attractor.since  # when the scores began
        while current.scene == "attract":
            current.update_attract(now)
            now = round(now + STEP, 9)
        self.assertAlmostEqual(now - STEP - scores, SCORES, delta=STEP)
        self.assertEqual((current.scene, current.selection, current.attractor.cycles), ("menu", 3, 1))
        self.assertFalse(current.update_attract(now + IDLE - 1))
        self.assertTrue(current.update_attract(now + IDLE))  # the idle wait starts again
        self.assertEqual(current.attract.pilot.game.config.mode, "arcade")  # they alternate

    def test_a_long_tape_gives_way_to_the_scores_after_90_s(self):
        current = menu(tapes=[playback(frames.tape())])
        start = demo_running(current)
        current.update_attract(start + DEMO_LENGTH - 0.1)
        self.assertIsNotNone(current.attract)
        current.update_attract(start + DEMO_LENGTH)
        self.assertEqual((current.scene, current.attract), ("attract", None))

    def test_the_arcade_tape_only_when_the_arcade_block_fits(self):
        for room, modes in ((Fits(True, True), ["classic", "arcade", "classic"]), (Fits(True, False), ["classic"] * 3)):
            current = menu(room=room)
            played, now = [], 0.0
            for _ in range(3):
                now = demo_running(current, now)
                played.append(current.attract.pilot.game.config.mode)
                current.handle(Key("x"), now)  # ends it
            self.assertEqual(played, modes, room)

    def test_ten_cycles_then_none_until_a_key(self):
        current = menu(tapes=[])  # no usable tape: the scores alone
        now = 0.0
        for _ in range(CYCLES):
            now = demo_running(current, now)
            now = round(now + SCORES, 9)
            current.update_attract(now)
            self.assertEqual(current.scene, "menu")
        self.assertEqual(current.attractor.cycles, CYCLES)
        self.assertFalse(current.update_attract(now + 10 * IDLE))
        current.handle(Key("down"), now + 10 * IDLE)
        self.assertTrue(current.update_attract(now + 11 * IDLE + 1))

    def test_it_starts_only_on_the_title_menu_when_allowed(self):
        cases = (
            dict(attract="off"),
            dict(attract="auto", reduced_motion=True),
        )
        for overrides in cases:
            current = menu(**overrides)
            current.update_attract(0.0)
            self.assertFalse(current.update_attract(10 * IDLE), overrides)
        current = menu(attract="auto")
        current.update_attract(0.0)
        self.assertTrue(current.update_attract(IDLE))
        for scene in ("help", "records", "settings"):
            current = menu()
            current.change_scene(scene)
            current.update_attract(0.0)
            self.assertFalse(current.update_attract(10 * IDLE), scene)
        current = menu(room=Fits(False, False))
        current.update_attract(0.0)
        self.assertFalse(current.update_attract(10 * IDLE))  # too small
        replaying = menu()
        replaying.replay = playback(campaign_tape())
        self.assertFalse(replaying.update_attract(10 * IDLE))
        self.assertEqual(defaults().attract, "off")  # every loop test that sits on the menu

    def test_the_idle_wait_comes_from_the_environment_once(self):
        self.assertEqual(attract.idle_wait({}), IDLE)
        self.assertEqual(attract.idle_wait({attract.IDLE_ENV: "2"}), 2.0)
        self.assertIsNone(attract.idle_wait({attract.IDLE_ENV: "off"}))
        self.assertEqual(attract.idle_wait({attract.IDLE_ENV: "soon"}), IDLE)
        self.assertEqual(attract.idle_wait({attract.IDLE_ENV: "-1"}), IDLE)
        with patch.dict(os.environ, {attract.IDLE_ENV: "off"}):
            current = Session(defaults(quickstart=False, attract="on"), Mock(alert=None), Mock(enabled=False))
            self.assertFalse(current.update_attract(0.0))
            self.assertFalse(current.update_attract(1000.0))
            self.assertIsNone(current.attractor.idle)

    def test_a_tape_that_fails_its_check_is_dropped_silently(self):
        bad = playback(replace(campaign_tape(), digest="0" * 64))
        current = menu(tapes=[bad])
        now = demo_running(current)
        now = loop(current, now, now + 5)
        self.assertEqual((current.scene, current.attract), ("attract", None))  # the scores, never MISMATCH
        self.assertEqual(current.attractor.tapes, [])
        buf = CellBuffer(80, 24)
        Painter(buf, current.options, MONO).draw(current)
        self.assertNotIn("MISMATCH", "\n".join(buf.row_text(y) for y in range(24)))


class KeyTests(unittest.TestCase):
    """Spec 5.3: any key ends the demo and does nothing else; held keys and their repeats never act in the menu."""

    def test_any_key_ends_it_and_is_swallowed(self):
        for name in ("enter", "escape", "m", "f1", "space", "q", "down", "a"):
            with self.subTest(key=name):
                audio = Mock(enabled=False)
                current = menu(audio=audio)
                current.selection = 2
                now = demo_running(current)
                current.handle(Key(name), now)
                self.assertEqual((current.scene, current.attract), ("menu", None))
                self.assertEqual((current.selection, current.quit, current.pilot), (2, False, None))
                audio.toggle.assert_not_called()
                self.assertFalse(current.options.show_fps)
                self.assertEqual(current.router.gate.last.get(name), now)  # the gate saw it

    def test_esc_never_counts_toward_esc_esc(self):
        current = menu()
        now = demo_running(current)
        current.handle(Key("escape"), now)
        self.assertEqual((current.selection, current.quit), (0, False))
        current.handle(Key("escape"), now + 3)
        self.assertEqual((current.selection, current.quit), (QUIT, False))  # the first Esc of the menu

    def test_a_held_enter_never_launches_on_a_legacy_terminal(self):
        current = menu()
        now = demo_running(current)
        current.handle(Key("enter"), now)
        held = now + 0.5
        while held < now + 2.0:  # the OS repeats it
            current.handle(Key("enter"), held)
            held = round(held + 0.033, 9)
        self.assertEqual((current.scene, current.pilot), ("menu", None))
        current.handle(Key("enter"), held + 3)  # a fresh press after it went quiet
        self.assertEqual(current.scene, "playing")

    def test_a_held_enter_never_launches_on_kitty(self):
        current = menu()
        current.router.gate.set_certain(True)
        now = demo_running(current)
        current.handle(Key("enter", kind=PRESS), now)
        for n in range(1, 30):
            current.handle(Key("enter", kind=REPEAT), now + 0.5 + n * 0.033)
        self.assertEqual((current.scene, current.pilot), ("menu", None))
        current.handle(Key("enter", kind=RELEASE), now + 2)
        self.assertEqual(current.scene, "menu")
        current.handle(Key("enter", kind=PRESS), now + 2.1)
        self.assertEqual(current.scene, "playing")

    def test_a_release_alone_does_not_end_it(self):
        current = menu()
        current.router.gate.set_certain(True)
        now = demo_running(current)
        current.handle(Key("enter", kind=RELEASE), now)
        self.assertEqual(current.scene, "attract")

    def test_the_signal_keys_are_still_routed(self):
        current = menu()
        now = demo_running(current)
        with patch("terminal_invaders.input.signal.raise_signal") as raised:
            current.handle(Key("c", CTRL), now)
        raised.assert_called_once()
        self.assertEqual(current.scene, "attract")

    def test_a_focus_out_or_a_window_that_no_longer_fits_ends_it(self):
        current = menu()
        current.on_focus(False)
        now = demo_running(current)
        current.on_focus(False)
        self.assertEqual((current.scene, current.attract), ("menu", None))
        now = demo_running(current, now)
        self.assertEqual(current.attract.pilot.game.config.mode, "arcade")
        current.on_resize(Fits(True, False))  # the Arcade block no longer fits
        self.assertEqual((current.scene, current.attract), ("menu", None))
        now = demo_running(current, now)
        current.on_resize(Fits(True, False))
        self.assertEqual(current.scene, "attract")  # the Campaign tape still fits
        current.on_resize(Fits(False, False))
        self.assertEqual((current.scene, current.attract), ("menu", None))

    def test_the_banner_is_drawn_in_every_demo_frame(self):
        current = menu()
        now = demo_running(current)
        loop(current, now, now + 1)
        for shown in (current.attract, current):  # the menu Session draws its demo's frame
            buf = CellBuffer(80, 24)
            Painter(buf, current.options, MONO).draw(shown)
            self.assertEqual(buf.row_text(22).strip(), BANNER)  # the hint row's place
        loop(current, now + 1, now + 6)  # the scores
        buf = CellBuffer(80, 24)
        Painter(buf, current.options, MONO).draw(current)
        self.assertIn("FLIGHT RECORDS - CAMPAIGN / NORMAL", buf.row_text(1))
        self.assertEqual(buf.row_text(21).strip(), BANNER)
        self.assertEqual(buf.row_text(23).strip(), "PILOT TEST  |  SOUND OFF")  # no key: any key ends it
        arcade = menu(tapes=[playback(arcade_tape())])
        now = demo_running(arcade)
        buf = CellBuffer(112, 64)
        Painter(buf, arcade.options, MONO).draw(arcade)
        self.assertIn(BANNER, "\n".join(buf.row_text(y) for y in range(64)))


class NothingWrittenTests(unittest.TestCase):
    """Spec 5.3: the demo never writes the profile, never sounds, and leaves the game after it as it was."""

    def test_a_full_cycle_leaves_the_profile_file_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps({"version": 2, "name": "ACE", "settings": {"palette": "cvd"}}))
            before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(os.listdir(folder)))
            profile = Profile(path)
            audio = Mock(enabled=True)
            current = menu(profile=profile, audio=audio)
            now = demo_running(current)
            shown = current.attract
            loop(current, now, now + 5)
            now = demo_running(current, now + 5)
            arcade = current.attract
            loop(current, now, now + 3)
            for demo in (shown, arcade):
                self.assertIsInstance(demo.profile, ReadOnlyProfile)
                self.assertEqual(demo.profile.writes, 0)
                self.assertIsInstance(demo.audio, SilentAudio)
                self.assertIs(demo.pilot.verified, True)  # the demo's digest equals the tape's
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, sorted(os.listdir(folder))), before)
            audio.update.assert_not_called()
            audio.toggle.assert_not_called()

    def test_the_read_only_profile_and_the_silent_audio(self):
        profile = Mock(data={"scores": []}, alert="a note")
        profile.best.return_value = 7
        read_only = ReadOnlyProfile(profile)
        self.assertEqual((read_only.best(Config()), read_only.data, read_only.alert), (7, {"scores": []}, "a note"))
        self.assertEqual(read_only.record({}, Config(), "X"), [])
        self.assertFalse(read_only.save() or read_only.rename(None, "X") or read_only.update_settings({}))
        self.assertEqual(read_only.writes, 4)
        self.assertIs(read_only.read_only, True)
        profile.record.assert_not_called()
        silent = SilentAudio(SimpleNamespace(enabled=True, available=False))
        self.assertEqual((silent.enabled, silent.available), (True, False))
        self.assertFalse(silent.toggle())
        silent.update(["fire"])
        self.assertEqual((SilentAudio().enabled, SilentAudio().available), (False, True))

    def test_the_demo_copies_the_options(self):
        current = menu()
        demo_running(current)
        self.assertIsNot(current.attract.options, current.options)
        current.attract.options.show_fps = True
        self.assertFalse(current.options.show_fps)

    def flown(self, with_demo: bool) -> list[str]:
        current = menu()
        now = 0.0
        if with_demo:
            for _ in range(2):  # a full cycle of both tapes
                now = demo_running(current, now)
                now = loop(current, now, now + 5)
                while current.scene == "attract":
                    current.update_attract(now)
                    now = round(now + STEP, 9)
        current.handle(Key("enter"), now + 1)  # LAUNCH
        return digests(current, now + 1)

    def test_the_game_after_a_demo_is_the_same(self):
        self.assertEqual(self.flown(True), self.flown(False))

    def test_the_game_after_a_demo_is_the_same_as_in_a_fresh_process(self):
        code = (
            "import json\n"
            "from tests.test_attract import NothingWrittenTests\n"
            "print(json.dumps(NothingWrittenTests().flown(False)))\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        out = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True
        )
        self.assertEqual(json.loads(out.stdout), self.flown(True))


def digests(current, now: float) -> list[str]:
    """A fixed in-play key stream for 600 ticks: the digest at every 10th tick."""
    seen = []
    for tick in range(600):
        if tick % 40 == 0:
            current.handle(Key("d" if tick % 80 else "a"), now)
        if tick % 25 == 0:
            current.handle(Key("space"), now)
        current.step(now)
        if tick % 10 == 0:
            seen.append(current.pilot.game.digest())
        now = round(now + STEP, 9)
    return seen


class PackagedTapeTests(unittest.TestCase):
    """The shipped tapes (D's tapes.attract_tapes()) play through a demo to a verified end."""

    def test_each_packaged_tape_verifies_at_its_end(self):
        from terminal_invaders.tapes import attract_tapes

        tapes = attract_tapes()
        self.assertEqual(sorted(tape.tapes[0].config.mode for tape in tapes), ["arcade", "classic"])
        for tape in tapes:
            with self.subTest(mode=tape.tapes[0].config.mode):
                current = menu(tapes=[tape])
                now = demo_running(current)
                demo = current.attract
                while current.attract is not None:
                    current.update_attract(now)
                    (current.attract or current).step(now)
                    now = round(now + STEP, 9)
                self.assertIs(demo.pilot.verified, True)
                self.assertEqual(demo.pilot.game.tick, tape.tapes[0].ticks)
                self.assertEqual(current.attractor.tapes, [tape])  # kept for the next cycle


if __name__ == "__main__":
    unittest.main()
