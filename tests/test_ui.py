"""Timing, input, session boundaries, and terminal portability regressions."""

import json
import math
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from terminal_invaders import app, screens, settings
from terminal_invaders.arcade import ArcadeGame
from terminal_invaders.arena import GlyphArena, PixelArena
from terminal_invaders.campaign.observe import charge_needed
from terminal_invaders.campaign.tables import FAMILIES, PROFILES, UPGRADES
from terminal_invaders.campaign.testing import staged
from terminal_invaders.cli import defaults
from terminal_invaders.engine import Game
from terminal_invaders.input import HoldControls, LatchedControls, TimedControls
from terminal_invaders.keymap import ACTIONS, PRESETS, Keymap
from terminal_invaders.model import TICK_RATE, WIDTH, Config, Player, Shot
from terminal_invaders.replay import Playback, Recorder, save_tapes
from terminal_invaders.storage import ACHIEVEMENTS, OLD_RULES, Placing, Profile
from terminal_invaders.term.cells import A_BOLD, A_DIM, CellBuffer, style
from terminal_invaders.term.keys import ALT, CTRL, PRESS, RELEASE, REPEAT, SHIFT, Key
from terminal_invaders.theme import ROLES, SLOT, resolve_theme
from terminal_invaders.ui import (
    CONTEXTS,
    DESCRIPTIONS,
    HELP,
    HELP_WIDTH,
    HINTS,
    MENU,
    PANEL_WIDTH,
    PANELS,
    QUIT,
    Fits,
    FixedClock,
    Painter,
    Session,
    help_lines,
    hint,
    is_card,
    label,
    lives_field,
    max_scale,
    menu_line,
    mode_name,
    panel_keys,
    small_notice,
)
from tests.support.terminals import capabilities, load

MONO = resolve_theme(SimpleNamespace(color="mono", theme="dark", paint_background=False))
TICK = 1 / TICK_RATE


def session(**overrides):
    options = dict(seed=42, fixed_seed=True, quickstart=True, name="TEST", color="mono") | overrides
    profile = Mock(data={"scores": [], "stats": {}, "achievements": []}, error=None, alert=None)
    profile.record.return_value = []
    profile.best.return_value = 0
    audio = Mock(enabled=False)
    return Session(defaults(**options), profile, audio)


class TimingTests(unittest.TestCase):
    def test_fixed_steps_match_elapsed_time_at_different_render_rates(self):
        for fps in (15, 24, 30, 60, 120, 144):
            clock = FixedClock(0)
            ticks = sum(clock.advance(frame / fps, True) for frame in range(1, fps * 10 + 1))
            self.assertEqual(ticks, 600)

    def test_pause_discards_partial_tick_and_long_wait(self):
        clock = FixedClock(0)
        self.assertEqual(clock.advance(0.01, True), 0)
        self.assertEqual(clock.advance(300, False), 0)
        self.assertEqual(clock.advance(300.01, True), 0)
        self.assertEqual(clock.advance(300.02, True), 0)
        self.assertEqual(clock.advance(300.03, True), 1)

    def test_catchup_is_bounded_without_future_backlog(self):
        clock = FixedClock(0)
        self.assertEqual(clock.advance(100, True), 8)
        self.assertEqual(clock.advance(100.001, True), 0)

    def test_next_tick_is_within_a_tick_and_exact_after_advance(self):
        clock = FixedClock(0)
        for now in (0.001, 0.02, 0.0335, 0.5, 0.75):
            clock.advance(now, True)
            due = clock.next_tick_at()
            self.assertTrue(now <= due <= now + 1 / 60 + 1e-9, (now, due))
            self.assertEqual(clock.advance(due - 1e-6, True), 0)
            self.assertEqual(clock.advance(due + 1e-6, True), 1)

    def test_repeat_coalesces_actions_and_opposite_direction_wins(self):
        # Ported from Phase 0's Controls (removed in S6) to HoldControls, where the latest direction pressed wins.
        controls = HoldControls()
        for _ in range(200):
            controls.pulse("bomb")
            controls.pulse("autofire")
        for action, name, at in (("left", "a", 1), ("right", "d", 1.01), ("fire", "space", 1.01)):
            controls.feed(action, Key(name, certain=True, time=at), True, at)
        self.assertEqual(controls.sample(1.02), ("autofire", "bomb", "fire", "right"))
        self.assertEqual(controls.sample(1.03), ("fire", "right"))
        for action, name in (("left", "a"), ("right", "d"), ("fire", "space")):
            controls.feed(action, Key(name, kind="release", certain=True, time=1.5), False, 1.5)
        controls.fired()
        self.assertEqual(controls.sample(2), ())


class SessionTests(unittest.TestCase):
    def test_pause_freezes_entire_state_and_clears_pending_input(self):
        current = session()
        current.handle(Key("b"), 0)
        current.handle(Key("p"), 0)
        before = current.pilot.game.digest()
        current.step(100)
        self.assertEqual(current.pilot.game.digest(), before)
        self.assertEqual(current.controls.sample(100), ())
        current.handle(Key("escape"), 100)
        current.step(100)
        self.assertEqual(current.pilot.game.tick, 1)

    def test_duel_uses_equal_independent_games_and_changes_after_hit(self):
        current = session(two_player=True, name="ABCDEFGHIJ")
        first, second = current.pilots
        self.assertEqual(first.game.digest(), second.game.digest())
        self.assertEqual(first.name, "ABCDEFGH-1")
        current.handle(Key("enter"), 0)
        game = first.game
        game.phase, game.player.guard = "playing", 0  # past the intro, without wave 1's training shield
        game.shots = [Shot(game.player.x, game.player.y - 0.1, friendly=False, vy=0.2, kind="pellet")]
        current.step(0)
        self.assertEqual(current.active, 1)
        self.assertEqual(current.scene, "handoff")
        self.assertEqual(second.game.tick, 0)
        self.assertEqual(first.game.player.lives, second.game.player.lives - 1)

    def test_completed_game_saves_once_and_moves_to_results(self):
        current = session()
        current.pilot.game.phase = "game_over"
        current.step(0)
        current.step(1)
        self.assertEqual(current.scene, "results")
        current.profile.record.assert_called_once()

    def test_menu_keyboard_and_display_shortcuts(self):
        current = session(quickstart=False)
        current.handle(Key("s", SHIFT), 0)
        current.handle(Key("right"), 0)
        self.assertEqual(current.mode, "arcade")
        current.handle(Key("w"), 0)
        current.handle(Key("space"), 0)
        self.assertEqual(current.scene, "playing")
        current.handle(Key("f1"), 0)
        self.assertTrue(current.options.show_fps)

    def test_every_key_handed_in_counts_as_routed(self):
        current = session()
        for key in (Key("d"), Key("d", kind=RELEASE), Key("x", CTRL), Key("f13")):  # bound or not, any kind
            current.handle(key, 0.1)
        self.assertEqual(current.routed, 4)

    def test_contexts_follow_the_scene_and_replay(self):
        current = session(quickstart=False)
        scenes = ("menu", "help", "records", "playing", "paused", "handoff", "results")
        contexts = ["menu", "pages", "pages", "play", "pause", "handoff", "results"]
        self.assertEqual([setattr(current, "scene", scene) or current.context for scene in scenes], contexts)
        current.replay = Mock()
        contexts = [setattr(current, "scene", scene) or current.context for scene in ("playing", "paused", "results")]
        self.assertEqual(contexts, ["replay", "replay_pause", "replay_results"])

    def test_every_command_runs_and_unknown_ones_raise(self):
        for context, actions in ACTIONS.items():
            for name, kind in actions.items():
                if kind in ("command", "navigation"):
                    with self.subTest(context=context, command=name):
                        current = session()
                        current.command(name, 0)
        with self.assertRaises(ValueError):
            session().command("launch", 0)

    def test_shift_is_ignored_but_other_modifiers_and_releases_match_nothing(self):
        current = session(quickstart=False)
        for key in (Key("escape", CTRL), Key("escape", ALT), Key("escape", kind="release")):
            current.handle(key, 0)
        self.assertEqual((current.selection, current.quit), (0, False))
        current.handle(Key("escape"), 1)
        self.assertEqual((current.selection, current.quit), (QUIT, False))
        current.handle(Key("escape", SHIFT), 2)
        self.assertTrue(current.quit)

    def test_esc_esc_quits_from_the_menu_and_a_quick_second_esc_is_not_taken_for_one(self):
        current = session(quickstart=False)
        current.handle(Key("escape"), 1.0)
        current.handle(Key("escape"), 1.3)  # within the guard: it could be the OS repeating a held Esc
        self.assertEqual((current.selection, current.quit), (QUIT, False))
        current.handle(Key("escape"), 2.2)
        self.assertTrue(current.quit)
        current = session(quickstart=False)
        current.handle(Key("down"), 0)
        current.handle(Key("escape"), 0)
        current.handle(Key("enter"), 0)  # QUIT + Enter
        self.assertTrue(current.quit)

    def test_menu_left_and_right_change_only_mode_and_difficulty(self):
        current = session(quickstart=False)
        current.handle(Key("left"), 0)
        self.assertEqual((current.scene, current.mode), ("menu", "classic"))
        current.handle(Key("down"), 0)
        current.handle(Key("left"), 0)
        self.assertEqual(current.mode, "duel")
        current.handle(Key("j"), 0)
        current.handle(Key("right"), 0)
        self.assertEqual(current.difficulty, "hard")
        current.handle(Key("k"), 0)
        current.handle(Key("enter"), 0)
        current.handle(Key("space"), 0)
        self.assertEqual((current.scene, current.mode), ("menu", "arcade"))

    def test_page_keys_flip_only_records_and_back_keys_return(self):
        keys = ("left", "right", "space", "a", "d", "q", "h", "l")
        for scene, pages in (("help", [0] * 8), ("records", [1, 0] * 4)):
            current = session(quickstart=False)
            current.change_scene(scene)
            seen = []
            for at, name in enumerate(keys):
                current.handle(Key(name), at)
                seen.append(current.page)
            self.assertEqual((current.scene, seen), (scene, pages))
        for at, name in enumerate(("enter", "escape", "backspace")):
            current.change_scene("help")
            current.handle(Key(name), 10 + at)
            self.assertEqual(current.scene, "menu")

    def test_pause_handoff_and_results_keys(self):
        current = session(two_player=True)
        for at, name in enumerate(("q", "r")):
            current.handle(Key(name), at)  # no way out of a handoff but through the pause menu
        self.assertEqual(current.scene, "handoff")
        current.handle(Key("p"), 2)
        self.assertEqual(current.scene, "paused")
        current.handle(Key("enter"), 3)
        self.assertEqual(current.scene, "handoff")  # resume returns to the handoff
        current.handle(Key("space"), 4)
        current.handle(Key("escape"), 5)
        self.assertEqual(current.scene, "paused")
        current.handle(Key("enter"), 6)
        self.assertEqual(current.scene, "playing")
        current.handle(Key("p"), 7)
        first = current.pilots[0]
        current.handle(Key("r"), 8)
        self.assertEqual(current.scene, "handoff")
        self.assertIsNot(current.pilots[0], first)
        current.change_scene("results")
        current.handle(Key("enter"), 9)
        self.assertEqual(current.scene, "handoff")
        for at, name in ((10, "q"), (12, "escape")):
            current.change_scene("results")
            current.handle(Key(name), at)
            self.assertEqual(current.scene, "menu")

    def test_replay_pauses_with_p_space_or_escape_and_exits_from_its_pause(self):
        for name in ("p", "space", "escape"):
            current = session()
            current.replay = Mock()
            current.handle(Key("q"), 0)  # Q exits only from the replay pause
            self.assertFalse(current.quit)
            current.handle(Key(name), 1)
            self.assertEqual(current.scene, "paused")
            current.handle(Key("enter"), 2)
            self.assertEqual(current.scene, "playing")
            current.handle(Key(name), 3)
            current.handle(Key("q"), 4)
            self.assertTrue(current.quit)
        current = session()
        current.replay = Mock()
        current.change_scene("results")
        current.handle(Key("enter"), 0)
        self.assertTrue(current.quit)

    def test_play_keys_feed_the_controls_and_a_resize_forgets_them(self):
        current = session()
        for name in ("h", "up", "x"):
            current.handle(Key(name), 1)
        self.assertEqual(current.controls.sample(1), ("bomb", "fire", "left"))
        current.handle(Key("l"), 1)
        current.on_resize(True)
        self.assertEqual(current.controls.sample(1), ())
        self.assertEqual((current.scene, current.pause_reason), ("paused", "resized"))

    def test_q_in_play_moves_left_and_never_leaves_play(self):
        current = session()
        for n in range(50):
            current.handle(Key("q"), n * 0.05)
        self.assertEqual(current.scene, "playing")
        self.assertEqual(current.controls.sample(3), ("left",))
        current.handle(Key("r"), 3)  # R restarts only from the pause menu
        current.handle(Key("z"), 4)  # AZERTY fire
        self.assertEqual((current.scene, current.controls.sample(4)), ("playing", ("fire", "left")))

    def test_a_resize_pauses_and_resume_counts_down_before_play(self):
        current = session()
        current.on_resize(True)
        self.assertEqual((current.scene, current.pause_reason, current.ticking), ("paused", "resized", False))
        current.handle(Key("p"), 10.0)
        self.assertEqual((current.scene, current.context, current.countdown), ("playing", "countdown", 3))
        self.assertFalse(current.ticking)
        current.handle(Key("d"), 10.1)  # play's controls pre-arm during the countdown
        digits = []
        for n in range(1, 10):
            if current.update_countdown(10.0 + n * 0.1):
                digits.append(current.countdown)
        self.assertEqual(digits, [2, 1, 0])
        self.assertTrue(current.ticking)
        self.assertEqual(current.controls.sample(11), ("right",))
        self.assertIsNone(current.countdown_deadline())

    def test_p_during_the_countdown_goes_back_to_the_pause(self):
        current = session()
        current.on_focus(False)
        current.handle(Key("enter"), 1.0)
        self.assertAlmostEqual(current.countdown_deadline(), 1.3)
        current.handle(Key("escape"), 1.2)
        self.assertEqual((current.scene, current.pause_reason), ("paused", "focus"))
        current.handle(Key("enter"), 3.0)
        self.assertEqual(current.context, "countdown")  # still an automatic pause: count down again

    def test_a_manual_pause_resumes_at_once(self):
        current = session()
        current.handle(Key("p"), 0)
        current.handle(Key("p"), 1)
        self.assertEqual((current.context, current.ticking), ("play", True))

    def test_a_too_small_window_pauses_and_refuses_resume(self):
        current = session()
        current.on_resize(False)
        self.assertEqual((current.scene, current.pause_reason), ("paused", "small"))
        current.handle(Key("p"), 1)
        self.assertEqual(current.scene, "paused")
        current.on_resize(True)
        self.assertEqual(current.pause_reason, "resized")
        current.handle(Key("p"), 2)
        self.assertEqual(current.context, "countdown")
        current = session(quickstart=False)
        current.on_resize(False)
        current.handle(Key("enter"), 0)  # Phase 5 spec 9 F2: the too-small notice takes no Enter: nothing launches
        self.assertEqual((current.scene, current.pilot), ("menu", None))
        current.on_resize(True)
        current.handle(Key("enter"), 1)
        self.assertEqual(current.scene, "playing")

    def test_focus_loss_pauses_and_clears_the_controls(self):
        current = session()
        current.handle(Key("d"), 0)
        current.on_focus(True)
        self.assertEqual(current.scene, "playing")  # FocusIn changes nothing
        current.on_focus(False)
        self.assertEqual((current.scene, current.pause_reason), ("paused", "focus"))
        self.assertEqual(current.controls.sample(0), ())
        current.on_focus(True)
        self.assertEqual(current.scene, "paused")

    def test_an_automatic_reason_replaces_a_manual_pause_and_the_menu_never_pauses(self):
        current = session()
        current.pause("manual")
        current.pause("suspended")
        self.assertEqual(current.pause_reason, "suspended")
        current.pause("manual")
        self.assertEqual(current.pause_reason, "suspended")
        current = session(quickstart=False)
        current.pause("focus")
        self.assertEqual(current.scene, "menu")

    def test_ctrl_l_asks_for_a_repaint(self):
        current = session()
        current.handle(Key("l", CTRL), 0)
        self.assertTrue(current.redraw)

    def test_daily_ignores_selected_difficulty(self):
        current = session(mode="daily", difficulty="hard")
        self.assertEqual(current.pilot.game.config.difficulty, "normal")

    def test_replay_ignores_gameplay_keys_and_never_records_score(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flight.json"
            original = Game(Config(seed=9))
            recorder = Recorder(original.config)
            for _ in range(7):
                recorder.append(("left",))
                original.step(("left",))
            recorder.save(path, original)
            current = session()
            current.replay = Playback(path)
            current.start()
            current.handle(Key("r"), 0)
            current.handle(Key("b"), 0)
            current.handle(Key("f"), 0)
            for _ in range(7):
                current.step(0)
            self.assertTrue(current.replay_verified)
            self.assertEqual(current.scene, "results")
            self.assertEqual(current.pilot.game.digest(), original.digest())
            current.profile.record.assert_not_called()

    def test_quit_to_menu_saves_a_verifiable_partial_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flight.json"
            current = session(record=path)
            current.step(0)
            current.handle(Key("p"), 0)
            current.handle(Key("q"), 0)
            (replay,) = Playback(path).tapes
            self.assertTrue(replay.verify(current.pilot.game))
            self.assertEqual(current.scene, "menu")

    def test_restart_keeps_replay_failure_visible(self):
        current = session(record=Path("unused.json"))
        with patch("terminal_invaders.ui.save_tapes", side_effect=OSError("disk unavailable")):
            current.start()
        self.assertIn("disk unavailable", current.note)

    def test_duel_records_every_pilot_into_one_verifiable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duel.json"
            current = session(two_player=True, record=path)
            for active, ticks in ((0, 5), (1, 3), (0, 2)):
                current.active = active
                current.change_scene("playing")
                for _ in range(ticks):
                    current.step(0)
                current.save_replay()
            current.handle(Key("p"), 0)
            current.handle(Key("q"), 0)
            tapes = Playback(path).tapes
            self.assertEqual([(tape.pilot, tape.ticks) for tape in tapes], [(1, 7), (2, 3)])
            self.assertTrue(all(tape.verify(tape.simulate()) for tape in tapes))

    def test_replay_watches_every_tape_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duel.json"
            tapes = []
            for pilot, ticks in ((1, 4), (2, 6)):
                game, recorder = Game(Config(seed=9)), Recorder(Config(seed=9), pilot)
                for _ in range(ticks):
                    recorder.append(("right",))
                    game.step(("right",))
                tapes.append(recorder.tape(game))
            save_tapes(path, tapes)
            current = session()
            current.replay = Playback(path)
            current.start()
            for _ in range(4):
                current.step(0)
            self.assertEqual((current.active, current.scene), (1, "playing"))
            for _ in range(6):
                current.step(0)
            self.assertEqual(current.scene, "results")
            self.assertTrue(current.replay_verified)
            self.assertEqual([(pilot.game.tick, pilot.verified) for pilot in current.pilots], [(4, True), (6, True)])
            screen = Screen()
            Painter(screen, current.options, MONO).draw(current)
            self.assertIn("REPLAY VERIFIED", screen.content())
            lines = [line.rstrip() for line in screen.content().splitlines()]  # Phase 5 spec 8.3: a column each
            self.assertIn("   TEST-1                      TEST-2", lines)
            self.assertIn("   VERIFIED                    VERIFIED", lines)


class ChoiceGame:
    """A stand-in for the campaign's Game on its upgrade card and continue prompt, with E-M0's member names (spec 3.2)
    and the rules of spec 10.2 and 5, so these tests don't ride on the engine: an edge is an action absent on the
    previous tick (_prev); on the card left and right move the cursor with wrap, and fire takes it once armed; at the
    prompt fire continues and bomb ends the run, once armed. script maps a tick to the phase the game enters on it."""

    ORDER = ("left", "right", "fire", "bomb")
    OFFERS = ("TRIPLE SHOT", "THRUSTERS", "MAGNET")

    def __init__(self, phase="playing", script=(), config=Config(difficulty="easy")):
        self.config, self.tick, self.wave, self.phase = config, 0, 3, "playing"
        self.player = Player(lives=3, weapon=2)
        self.offers, self.cursor, self.armed, self.prompt, self.continues = (), 1, 0, 0, 1
        self.events, self.picks, self._prev, self.held = [], (), (), []
        self.score = self.kills = self.shots_fired = self.hits = self.best_combo = 0
        self.script = dict(script)
        self.enter(phase)

    def enter(self, phase):
        if phase == "upgrade":
            self.offers, self.cursor, self.armed = self.OFFERS, 1, 30
        elif phase == "continue":
            self.prompt, self.armed, self.player.lives = 600, 30, 0
        elif phase == "wave" and self.phase in ("clear", "upgrade"):
            self.wave += 1
        self.phase = phase

    def step(self, actions):
        held = tuple(name for name in self.ORDER if name in actions)
        edges = [name for name in held if name not in self._prev]
        self.tick, self.events = self.tick + 1, []
        self.held.append(held)
        if self.phase == "upgrade":
            self.cursor = (self.cursor + ("right" in edges) - ("left" in edges)) % len(self.offers)
            if "fire" in edges and not self.armed:
                self.picks += (self.offers[self.cursor],)
                self.offers = ()
                self.enter("wave")
            self.armed = max(0, self.armed - 1)
        elif self.phase == "continue":
            if not self.armed and "fire" in edges:
                self.continues, self.player.lives = self.continues - 1, 2
                self.enter("wave")
            elif not self.armed and "bomb" in edges or self.prompt <= 1:
                self.phase = "game_over"
            else:
                self.prompt, self.armed = self.prompt - 1, max(0, self.armed - 1)
        if self.tick in self.script:
            self.enter(self.script[self.tick])
        self._prev = held

    def result(self):
        return {"score": self.score, "wave": self.wave, "kills": self.kills, "victory": False}


class ChoiceTests(unittest.TestCase):
    """The Session's part of the upgrade card and the continue prompt (spec 10.2, 12, 16.2): engine phases inside
    play, driven by the edges of the actions the controls sample."""

    SCHEMES = {"hold": (HoldControls, True), "latched": (LatchedControls, False), "timed": (TimedControls, False)}

    def rig(self, scheme="latched", phase="playing", script=(), game=None):
        current = session()
        controls, certain = self.SCHEMES[scheme]
        current.set_scheme(controls(), certain)
        game = game or ChoiceGame(phase, script)
        current.pilots[0].game = game
        current.match_controls()
        return current, game

    def drive(self, current, start, end, keys=()):
        """Step the session every tick from start to end, handing in each key (time, name[, kind]) before the tick
        it falls in. A kitty press without a kind is a tap: its release comes 50 ms later."""
        certain, events = current.router.gate.certain, []
        for at, name, *kind in keys:
            events.append((at, name, kind[0] if kind else PRESS))
            if certain and not kind:
                events.append((at + 0.05, name, RELEASE))
        events.sort()
        n = 0
        while (now := start + n * TICK) < end - 1e-9:
            while events and events[0][0] <= now:
                at, name, kind = events.pop(0)
                current.handle(Key(name, kind=kind, certain=certain, time=at), at)
            current.step(now)
            n += 1
        return now

    def test_each_tap_on_the_card_moves_the_cursor_once_in_every_scheme(self):
        for scheme in self.SCHEMES:
            with self.subTest(scheme):
                current, game = self.rig(scheme, "upgrade")
                cursors = []
                for n, name in enumerate(("d", "d", "d", "a", "l", "h", "right")):
                    self.drive(current, 1.0 + 0.4 * n, 1.4 + 0.4 * n, [(1.01 + 0.4 * n, name)])
                    cursors.append(game.cursor)
                self.assertEqual(cursors, [2, 0, 1, 0, 1, 0, 1])  # the same way twice moves twice; it wraps
                self.assertEqual(game.phase, "upgrade")  # and moving never takes the card

    def test_only_the_card_and_the_prompt_unlatch_a_direction(self):
        current, game = self.rig("latched", "playing")
        self.drive(current, 1.0, 1.5, [(1.0, "d")])
        self.assertEqual(sum("right" in held for held in game.held), 30)  # in play a tap moves until a stop
        for phase in ("upgrade", "continue"):
            with self.subTest(phase):
                current, game = self.rig("latched", phase)
                self.drive(current, 1.0, 1.5, [(1.0, "d")])
                self.assertEqual(sum("right" in held for held in game.held), 1)

    def test_each_edge_into_or_out_of_the_card_or_prompt_clears_the_controls_and_the_gate(self):
        edges = [("playing", "continue"), ("clear", "upgrade"), ("upgrade", "wave"), ("continue", "wave")]
        edges += [("playing", "clear"), ("clear", "wave"), ("wave", "playing")]  # not these
        for before, after in edges:
            with self.subTest(before=before, after=after):
                current, game = self.rig("latched", before, {5: after})
                gate, controls = current.router.gate, current.controls
                with (
                    patch.object(gate, "scene_changed", wraps=gate.scene_changed) as changed,
                    patch.object(controls, "clear", wraps=controls.clear) as cleared,
                ):
                    self.drive(current, 1.0, 1.0 + 10 * TICK)
                if "upgrade" in (before, after) or "continue" in (before, after):
                    changed.assert_called_once_with(1.0 + 4 * TICK)  # the tick the phase changed on
                    cleared.assert_called_once()
                else:
                    changed.assert_not_called()
                    cleared.assert_not_called()
        current, game = self.rig("latched", "clear", {3: "upgrade"})
        self.drive(current, 1.0, 1.1, [(1.0, "d"), (1.0, "space")])
        self.assertEqual(game.held[:3], [("right", "fire")] * 3)
        self.assertEqual(game.held[3:], [()] * 3)  # neither the latch nor the fire buffer crossed onto the card
        current, game = self.rig("latched", "continue")
        with patch.object(current.router.gate, "scene_changed") as changed:
            self.drive(current, 1.0, 1.6, [(1.55, "b")])  # the run ends: out of the prompt, then the results
        self.assertEqual((game.phase, current.scene), ("game_over", "results"))
        changed.assert_any_call(1.55)

    def test_a_space_held_into_the_card_never_takes_it_and_a_fresh_press_does(self):
        repeats = [(1.4 + 0.033 * n, "space") for n in range(46)]  # the OS repeats a Space held from the wave
        for scheme, held in (
            ("latched", [(0.9, "space"), *repeats]),
            ("timed", [(0.9, "space"), *repeats]),
            ("hold", [(0.9, "space", PRESS), *((at, name, REPEAT) for at, name in repeats), (2.95, "space", RELEASE)]),
        ):
            with self.subTest(scheme):
                current, game = self.rig(scheme, "clear", {6: "upgrade"})
                self.drive(current, 1.0, 3.3, held)  # armed from 1.6 s on, Space still down until 2.95 s
                self.assertEqual((game.phase, game.armed, game.picks), ("upgrade", 0, ()))
                self.drive(current, 3.3, 3.5, [(3.35, "space")])
                self.assertEqual((game.phase, game.picks), ("wave", ("THRUSTERS",)))

    def test_a_tap_before_arming_does_nothing_and_the_next_one_still_takes_the_card(self):
        for scheme in self.SCHEMES:
            with self.subTest(scheme):
                current, game = self.rig(scheme, "upgrade")
                early = 1.0 + 20 * TICK  # armed is 10 then; the next tap comes 11 ticks later, armed 0
                self.drive(current, 1.0, 1.4, [(early, "space")])
                self.assertEqual((game.phase, game.armed), ("upgrade", 6))
                self.drive(current, 1.4, 1.6, [(early + 11 * TICK, "space")])
                self.assertEqual((game.phase, game.picks), ("wave", ("THRUSTERS",)))  # not lost in a fire buffer

    def test_the_prompt_takes_space_or_b_only_once_armed(self):
        for scheme in self.SCHEMES:
            with self.subTest(scheme):
                current, game = self.rig(scheme, "continue")
                self.drive(current, 1.0, 1.5, [(1.1, "b"), (1.3, "space")])  # armed: these do nothing
                self.assertEqual((game.phase, game.prompt), ("continue", 570))
                self.drive(current, 1.5, 1.7, [(1.55, "space")])
                self.assertEqual((game.phase, game.player.lives, game.continues), ("wave", 2, 0))
                current, game = self.rig(scheme, "continue")
                self.drive(current, 1.0, 1.7, [(1.6, "x")])
                self.assertEqual((game.phase, current.scene), ("game_over", "results"))
                current.profile.record.assert_called_once()

    def test_space_presses_one_at_a_time_on_the_card_and_prompt_and_streams_in_play(self):
        current, game = self.rig("latched", "clear", {3: "upgrade"})
        self.assertFalse(current.controls.press_fire)
        self.drive(current, 1.0, 1.1)
        self.assertTrue(current.controls.press_fire)
        game.script[game.tick + 2] = "wave"
        self.drive(current, 1.1, 1.2)
        self.assertFalse(current.controls.press_fire)
        current.mode = "arcade"
        current.start()
        self.assertTrue(current.controls.press_fire)  # Arcade's own rule (spec decision 15)

    def test_p_pauses_on_the_card_and_q_there_only_chooses(self):
        current, game = self.rig("latched", "upgrade")
        current.handle(Key("d", time=1.95), 1.95)  # tapped just as the pause comes
        current.handle(Key("p", time=2.0), 2.0)
        self.assertEqual(current.scene, "paused")
        current.handle(Key("p", time=3.0), 3.0)  # a manual pause resumes at once
        self.drive(current, 3.0, 3.2)
        self.assertEqual((current.scene, game.phase, game.cursor), ("playing", "upgrade", 1))
        self.assertEqual(set(game.held), {()})  # the pause forgot the tap
        self.drive(current, 3.2, 4.0, [(3.3, "q"), (3.7, "q")])  # Q is left in play: it chooses, and never leaves
        self.assertEqual((current.scene, game.phase, game.cursor), ("playing", "upgrade", 2))

    def test_a_duel_passes_the_turn_at_each_new_wave_but_not_after_a_continue(self):
        current = session(two_player=True)
        first, second = ChoiceGame("clear", {3: "wave"}), ChoiceGame("upgrade")
        current.pilots[0].game, current.pilots[1].game = first, second
        current.command("start", 0.0)
        self.drive(current, 1.0, 1.1)
        self.assertEqual((current.active, current.scene, first.wave), (1, "handoff", 4))  # clear -> wave
        current.command("start", 2.0)
        self.drive(current, 2.0, 2.7, [(2.6, "space")])
        self.assertEqual((current.active, current.scene, second.picks), (0, "handoff", ("THRUSTERS",)))
        current.command("start", 3.0)
        first.script[first.tick + 2] = "continue"  # the last life goes: the turn passes
        self.drive(current, 3.0, 3.1)
        self.assertEqual((current.active, current.scene, first.prompt), (1, "handoff", 600))
        current.command("start", 4.0)
        second.script |= {second.tick + 2: "playing", second.tick + 4: "clear", second.tick + 6: "wave"}
        self.drive(current, 4.0, 4.2)
        self.assertEqual((current.active, current.scene), (0, "handoff"))
        self.assertEqual(first.prompt, 600)  # the prompt runs only in its own pilot's turn
        current.command("start", 5.0)
        self.drive(current, 5.0, 5.7, [(5.6, "space")])  # continue: wave from continue, the same pilot's turn
        self.assertEqual((first.phase, current.active, current.scene), ("wave", 0, "playing"))

    def test_a_staged_campaign_card_and_prompt_through_the_session(self):
        """The seam with E's engine (spec 3.6, 10.2): taps reach the real card as edges, and Space takes it."""
        for scheme in self.SCHEMES:
            with self.subTest(scheme):
                game = staged(Config("normal", "classic", 1978), 4, phase="upgrade")
                offers, cursor = game.offers, game.cursor
                current, _ = self.rig(scheme, game=game)
                self.drive(current, 1.0, 2.0, [(1.1, "d"), (1.5, "d"), (1.9, "space")])
                taken = offers[(cursor + 2) % len(offers)]
                self.assertEqual((game.phase, game.wave, game.picks), ("wave", 4, (taken,)))
                self.assertIn(f"TAKEN: {taken}", [text for text, _ in game.notes])
                game = staged(Config("easy", "classic", 1978), 2, phase="continue")
                current, _ = self.rig(scheme, game=game)
                self.drive(current, 1.0, 1.5, [(1.3, "space")])  # while it arms: nothing (second review, TQ5)
                self.assertEqual((game.phase, game.continues_used), ("continue", 0))
                self.drive(current, 1.5, 1.7, [(1.6, "space")])
                self.assertEqual((game.phase, game.player.lives, game.continues_used), ("wave", 2, 1))

    def test_each_easy_pilot_has_its_own_continue_in_a_real_duel(self):
        """Spec 12: a continue is the same pilot's turn; the other pilot's prompt waits for its own turn."""
        current = session(two_player=True, difficulty="easy")
        easy = current.pilot.game.config
        first, second = (staged(easy, 2, phase="continue") for _ in range(2))
        current.pilots[0].game, current.pilots[1].game = first, second
        current.command("start", 0.0)
        current.match_controls()
        waiting = second.prompt
        self.drive(current, 1.0, 1.7, [(1.6, "space")])  # once armed, Space continues
        self.assertEqual((first.phase, first.continues_used, current.active, current.scene), ("wave", 1, 0, "playing"))
        self.assertEqual((second.prompt, second.continues_used), (waiting, 0))  # its prompt runs only in its turn
        now = self.drive(current, 1.7, 4.0)
        self.assertEqual(first.phase, "playing")
        player = first.player
        player.invulnerable = player.guard = player.shield = 0
        first.shots.append(Shot(player.x, player.y - 0.1, friendly=False, vy=0.2, kind="pellet"))
        self.drive(current, now, now + 0.1)  # a life lost: the turn passes
        self.assertEqual((current.active, current.scene), (1, "handoff"))
        current.command("start", 5.0)
        self.drive(current, 5.0, 5.7, [(5.6, "space")])
        self.assertEqual((second.phase, current.active, current.scene), ("wave", 1, "playing"))
        self.assertEqual(
            [(game.continues_used, game.continues) for game in (first, second)],
            [(1, PROFILES["easy"].continues - 1)] * 2,
        )


class Screen(CellBuffer):
    """A CellBuffer that also checks the Painter clipped every string before handing it over."""

    def __init__(self, rows=24, columns=60):
        super().__init__(columns, rows)

    def addstr(self, y, x, text, attr=0):
        assert 0 <= y < self.rows and 0 <= x < self.cols
        assert x + len(text) <= self.cols
        super().addstr(y, x, text, attr)

    def content(self):
        return "\n".join(self.row_text(y) for y in range(self.rows))


class DrawingTests(unittest.TestCase):
    def test_all_screens_fit_minimum_size_and_render_in_monochrome(self):
        current = session()
        screen = Screen()
        painter = Painter(screen, current.options, MONO)
        for scene in ("menu", "help", "records", "playing", "paused", "handoff", "results"):
            current.scene = scene
            painter.draw(current)
            self.assertTrue(screen.content().strip())
            self.assertFalse([st for st in screen.styles if st & 0xFFFF])  # attributes only, no colour slots

    def test_tiny_and_resized_windows_are_clipped_and_centered(self):
        current = session()
        for rows, columns in ((1, 1), (10, 30), (24, 59), (40, 100)):
            screen = Screen(rows, columns)
            painter = Painter(screen, current.options, MONO)
            painter.draw(current)
            self.assertEqual(painter.resize(), rows >= 24 and columns >= 60)

    def test_layout_places_the_block_where_phase_0_did(self):
        """With --glyphs the play block is Phase 0's; at any scale the menus' 60 x 24 card lands there too."""
        glyphs, scaled = session(scale="1").options, session().options
        for rows in range(0, 90, 3):
            for columns in range(0, 260, 7):
                expected = max(0, (rows - 24) // 2), max(0, (columns - 60) // 2), rows >= 24 and columns >= 60
                painter = Painter(Screen(rows, columns), glyphs, MONO)
                placed = painter.top, painter.left, painter.resize()
                self.assertEqual((*placed, painter.layout.k), (*expected, 1), (rows, columns))
                painter = Painter(Screen(rows, columns), scaled, MONO)
                painter.place("menu")
                self.assertEqual((painter.top, painter.left, painter.layout.fits), expected, (rows, columns))

    def test_records_keep_different_difficulty_boards_separate(self):
        current = session()
        current.profile.data["scores"] = [
            dict(name="NORMAL", score=1, wave=1, mode="classic", difficulty="normal", seed=0),
            dict(name="HARD", score=99, wave=1, mode="classic", difficulty="hard", seed=0),
        ]
        current.scene = "records"
        screen = Screen()
        Painter(screen, current.options, MONO).draw(current)
        self.assertIn("NORMAL", screen.content())
        self.assertNotIn("HARD", screen.content())

    def test_profile_alerts_are_shown_whole_on_the_menu_and_results(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            for content in ('{"version": 3, "name": "FUTURE"}', "{bad json"):
                path.write_text(content)
                current = session()
                current.profile = Profile(path)
                for scene in ("menu", "results"):
                    with self.subTest(content=content, scene=scene):
                        current.scene = scene
                        screen = Screen()
                        Painter(screen, current.options, MONO).draw(current)
                        self.assertIn(current.profile.alert, screen.content())

    def test_the_hint_row_names_the_card_and_prompt_keys_while_they_show(self):
        current = session()
        screen = Screen()
        painter = Painter(screen, current.options, MONO)
        for phase, text in (("upgrade", HINTS["card"]), ("continue", HINTS["prompt"]), ("playing", HINTS["latched"])):
            current.pilot.game.phase = phase
            painter.draw(current)
            self.assertEqual(screen.row_text(22).rstrip(), text)

    def test_the_menu_calls_classic_campaign_and_shows_each_difficulty_line(self):
        current = session(quickstart=False)
        screen = Screen()
        painter = Painter(screen, current.options, MONO)
        painter.draw(current)
        self.assertIn("MODE         < CAMPAIGN  >", screen.content())
        self.assertEqual(screen.row_text(19).strip(), "24 waves in four acts, a boss at each act's end.")
        current.handle(Key("down"), 0.0)
        current.handle(Key("down"), 1.0)  # the DIFFICULTY row: its line replaces the mode's
        for at, name, line in ((2.0, "right", "hard"), (3.0, "right", "easy"), (4.0, "right", "normal")):
            current.handle(Key(name), at)
            painter.draw(current)
            self.assertEqual(screen.row_text(19).strip(), PROFILES[line].line)
        self.assertEqual(PROFILES["normal"].line, "Aimed fire and smart volleys: the intended game")
        current.mode = "daily"
        painter.draw(current)
        self.assertEqual(screen.row_text(19).strip(), PROFILES["normal"].line)  # Daily is always normal
        current.mode = "arcade"
        painter.draw(current)
        self.assertEqual(screen.row_text(19).strip(), DESCRIPTIONS["arcade"])  # the 1978 rules have no difficulty
        current.handle(Key("up"), 5.0)
        current.mode = "duel"
        painter.draw(current)
        self.assertEqual(screen.row_text(19).strip(), "Two pilots. Same fleet. Swap each wave and life.")
        self.assertEqual(
            [mode_name(mode) for mode in ("classic", "endless", "daily", "arcade", "duel")],
            ["CAMPAIGN", "ENDLESS", "DAILY", "ARCADE", "DUEL"],
        )
        self.assertTrue(all(len(line) <= 52 for line in DESCRIPTIONS.values()))

    def test_the_hint_row_follows_the_scheme_and_replays_show_their_banner(self):
        current = session()
        screen = Screen()
        painter = Painter(screen, current.options, MONO)
        for controls, text in ((current.controls, HINTS["latched"]), (HoldControls(), HINTS["hold"])):
            current.controls = controls
            painter.draw(current)
            self.assertEqual(screen.row_text(22).rstrip(), text)
        current.replay = Mock()
        painter.draw(current)
        self.assertEqual(screen.row_text(22).rstrip(), "REPLAY  P Pause, then Q Exit")

    def test_pause_reasons_and_the_countdown_show_in_their_panel(self):
        current = session()
        screen = Screen()
        painter = Painter(screen, current.options, MONO)
        for reason, line in (("manual", None), ("resized", "Window resized"), ("focus", "Focus lost")):
            current.pause(reason)
            current.pause_reason = reason
            painter.draw(current)
            content = screen.content()
            self.assertIn("P / Escape / Enter resume", content)
            self.assertIn("R restart   M sound   Q menu", content)
            self.assertTrue(line is None or line in content, (reason, content))
        for reason, line in (("suspended", "Back from suspend"), ("resumed", "Terminal restored")):
            current.pause_reason = reason
            painter.draw(current)
            self.assertIn(line, screen.content())
        current.resume(5.0)
        painter.draw(current)
        self.assertIn("GET READY", screen.content())
        self.assertEqual(screen.content().splitlines()[10].strip(), "3")
        current.on_resize(False)
        painter = Painter(Screen(20, 50), current.options, MONO)
        painter.draw(current)
        rows = rows_of(painter.screen)  # Phase 5 spec 9 F2: centred, with the way back from the pause
        at = rows.index("Enlarge the window: the game needs 60 x 24")
        self.assertEqual(rows[at + 1 : at + 3], ["Now 50 x 20 | Paused", "Q leaves the run"])

    def test_the_stats_line_replaces_the_footer_row_cut_to_the_block(self):
        stats = "FPS 59.9  RTT 42ms  W 3.4  B/f 812  K17  hold k1 truecolor  " + "x" * 30
        current = session()
        for scene in ("menu", "help", "records", "playing", "paused", "results"):
            with self.subTest(scene=scene):
                current.scene = scene
                screen = Screen(30, 100)
                painter = Painter(screen, current.options, MONO)
                painter.draw(current, stats)
                row = screen.row_text(painter.top + 23)
                left = painter.left
                self.assertEqual(row[left : left + WIDTH], stats[:WIDTH])
                self.assertEqual(row[left + WIDTH :].strip(), "")
                self.assertTrue(screen.styles[(painter.top + 23) * 100 + left] & A_DIM)
        painter.draw(current)
        self.assertNotIn("FPS", screen.content())
        small = Painter(Screen(20, 50), current.options, MONO)
        small.draw(current, stats)
        self.assertNotIn("FPS", small.screen.content())

    def test_every_role_has_its_slot_and_an_unknown_tier_is_mono(self):
        current = session()
        for tier in ("truecolor", "256", "16", "mono", "bogus"):
            with self.subTest(tier=tier):
                theme = resolve_theme(SimpleNamespace(color=tier, theme="sepia", paint_background=True))
                painter = Painter(Screen(), current.options, theme)
                mono = tier in ("mono", "bogus")
                slots = {slot: 0 if mono else slot for slot in range(1, len(ROLES))}
                self.assertEqual(painter.colors, {slot: style(fg, 0, theme.attrs[slot]) for slot, fg in slots.items()})
                self.assertEqual(theme.name, "dark")


COLOR = resolve_theme(SimpleNamespace(color="truecolor", theme="dark", paint_background=False))
NORMAL = Config("normal", "classic", 1978)


def no_field():
    """The arena draws nothing (no stars or sprites), so a panel's rows hold only the panel."""
    stack = ExitStack()
    for view in (GlyphArena, PixelArena):
        stack.enter_context(patch.object(view, "draw"))
    return stack


def shown(game, rows=24, columns=60, theme=MONO, current=None, field=False, **overrides):
    """A campaign game drawn in play (by default with an empty arena): the screen, the painter and the session."""
    current = current or session(**overrides)
    current.pilots[current.active].game = game
    current.match_controls()
    screen = Screen(rows, columns)
    painter = Painter(screen, current.options, theme)
    with nullcontext() if field else no_field():
        painter.draw(current)
    return screen, painter, current


def find(screen, text):
    """Where text first shows on the screen: (row, column)."""
    for y in range(screen.rows):
        x = screen.row_text(y).find(text)
        if x >= 0:
            return y, x
    raise AssertionError(f"{text!r} is not on the screen:\n{screen.content()}")


def style_at(screen, text):
    y, x = find(screen, text)
    return screen.styles[y * screen.cols + x]


def rows_of(screen):
    return [line.strip() for line in screen.content().splitlines()]


def upgrade_family(name) -> str:
    """The family line of an upgrade's card."""
    return UPGRADES[name].family.upper()


class CampaignScreenTests(unittest.TestCase):
    """Spec 15.2-15.3 on real staged games: the HUD rows, the bottom row, and the panels of each phase."""

    def test_the_hud_shows_wave_and_act_gun_and_bomb_at_their_columns(self):
        screen, _, _ = shown(staged(NORMAL, 9, phase="playing"), field=True)
        top, second = screen.row_text(0), screen.row_text(1)
        self.assertEqual(top[:39].rstrip(), "SCORE 0000000   HI 0000000")
        self.assertEqual(top[39:].rstrip(), "WAVE 09/24  ACT II")
        self.assertEqual(second[:36].rstrip(), "LIVES ^^^    BOMBS 1  COMBO x1")
        self.assertEqual((second[36:44].rstrip(), second[44:].rstrip()), ("GUN ||", "BOMB 0%"))
        self.assertNotIn("AUTO", screen.content())  # the Campaign has no autofire (the bottom row used to say so)
        for config, wave, text in (
            (NORMAL, 1, "WAVE 01/24  ACT I"),
            (NORMAL, 13, "WAVE 13/24  ACT III"),
            (NORMAL, 24, "WAVE 24/24  ACT IV"),
            (Config("normal", "daily", 20261018), 9, "WAVE 09/24  ACT II"),
            (Config("hard", "endless", 1978), 31, "WAVE 031  ENDLESS"),
        ):
            with self.subTest(config=config, wave=wave):
                screen, _, _ = shown(staged(config, wave))
                self.assertEqual(screen.row_text(0)[39:].rstrip(), text)
        screen, painter, _ = shown(staged(NORMAL, 9, phase="playing"), 44, 120, field=True)  # columns x k
        self.assertEqual(painter.layout.k, 2)
        top, second = (screen.row_text(painter.top + y)[painter.left :] for y in (0, 1))
        self.assertEqual((top[78:96], second[72:78], second[88:95]), ("WAVE 09/24  ACT II", "GUN ||", "BOMB 0%"))
        self.assertEqual(lives_field(5), "^^^^^")
        self.assertEqual(lives_field(99), "^ x99")  # an immortal staged game

    def test_gun_shows_a_bar_per_free_slot_and_a_dot_per_volley_in_flight(self):
        game = staged(NORMAL, 9, phase="playing", upgrades=("TRIPLE SHOT",))
        cases = (
            ((), "GUN |||"),
            ((1,), "GUN ||."),
            ((1, 1, 1), "GUN ||."),  # a SPREAD's three shots hold one slot
            ((1, 2, 2, 2), "GUN |.."),
            ((1, 2, 3), "GUN ..."),
        )
        for volleys, text in cases:
            with self.subTest(volleys=volleys):
                game.shots = [Shot(30.0, 10.0, volley=volley) for volley in volleys]
                game.shots.append(Shot(30.0, 5.0, friendly=False, vy=0.2, kind="pellet"))  # an enemy's never counts
                screen, _, _ = shown(game)
                self.assertEqual(screen.row_text(1)[36:44].rstrip(), text)

    def test_bomb_shows_the_charge_in_percent_and_full_when_it_cannot_add_one(self):
        game = staged(NORMAL, 9, phase="playing")
        need = charge_needed(game)
        for charge, text in ((0, "BOMB 0%"), (need * 0.649, "BOMB 64%"), (need - 1, "BOMB 99%"), (need, "BOMB FULL")):
            with self.subTest(charge=charge):
                game.bomb_charge = charge
                screen, painter, _ = shown(game, theme=COLOR)
                self.assertEqual(screen.row_text(1)[44:].rstrip(), text)
                role = "accent" if text == "BOMB FULL" else "dim"
                self.assertEqual(style_at(screen, "BOMB "), painter.colors[SLOT[role]])
        capacitor = staged(NORMAL, 9, phase="playing", upgrades=("CAPACITOR",))  # a bomb needs less charge
        self.assertLess(charge_needed(capacitor), need)
        capacitor.bomb_charge = game.bomb_charge = charge_needed(capacitor) / 2
        self.assertEqual(shown(capacitor)[0].row_text(1)[44:].rstrip(), "BOMB 50%")
        self.assertEqual(shown(game)[0].row_text(1)[44:].rstrip(), f"BOMB {int(50 * charge_needed(capacitor) / need)}%")

    def test_a_duel_names_the_pilot_at_column_0(self):
        current = session(two_player=True)
        current.command("start", 0.0)
        for active in (0, 1):
            current.active = active
            screen, _, _ = shown(staged(NORMAL, 4, phase="playing"), current=current)
            self.assertEqual(screen.row_text(0)[:11], f"P{active + 1} 0000000 ")

    def test_the_hud_leaves_the_boss_bar_row_to_the_arena(self):
        game = staged(NORMAL, 6, phase="playing")
        self.assertIn("WARDEN", shown(game, field=True)[0].row_text(2))
        self.assertEqual(shown(game)[0].row_text(2).strip(), "")  # with the arena drawing nothing, row 2 is empty

    def test_the_bottom_row_shows_notices_then_pickups_then_codes_and_mods(self):
        upgrades = ("TRIPLE SHOT", "RAIL ROUNDS", "RAIL ROUNDS", "DEFLECTOR")
        game = staged(NORMAL, 9, phase="playing", upgrades=upgrades)
        game.notes = [("LAST STAND", 180), ("GONE", 0)]  # a note with no ticks left is gone
        game.powerups = {"rapid": 6 * TICK_RATE, "slow": 0, "shield": 9 * TICK_RATE - 5}
        screen, painter, _ = shown(game, theme=COLOR)
        self.assertEqual(screen.row_text(23).rstrip(), "LAST STAND  RAPID 6s  SHIELD 9s | TS RR2 DF")
        for text, role in (("LAST STAND", "warn"), ("RAPID", "accent"), ("SHIELD", "accent"), ("| TS", "dim")):
            self.assertEqual(style_at(screen, text), painter.colors[SLOT[role]], text)
        game.notes, game.powerups = [], {}
        self.assertEqual(shown(game)[0].row_text(23).rstrip(), "TS RR2 DF")  # no bar before the codes alone
        game.notes = [("TAKEN: THRUSTERS", 180), ("SHIELD DOWN - MOVE OUT FROM UNDER THE !", 180), ("BOMB READY", 1)]
        row = "TAKEN: THRUSTERS  SHIELD DOWN - MOVE OUT FROM UNDER THE !  BOMB READY | TS RR2 DF"
        for columns in (60, 80):  # cut to the width (Screen checks every string was clipped first)
            screen, painter, _ = shown(game, columns=columns)
            self.assertEqual(screen.row_text(23)[painter.left :].rstrip(), row[: painter.width])
        endless = staged(Config("normal", "endless", 1978), 40, phase="playing", upgrades=("MAGNET",))
        endless.modifiers = ["QUICK RELOAD", "ARMOURED", "EXTRA SLOT"]
        endless.notes = []  # the engine's own "NEW: <modifier>" note for the wave (E-M2), tested above as a note
        self.assertEqual(shown(endless)[0].row_text(23).rstrip(), "MG  MODS 3")
        endless.modifiers = []
        self.assertEqual(shown(endless)[0].row_text(23).rstrip(), "MG")

    def test_safe_seconds_stand_in_for_the_blink_only_under_reduced_motion(self):
        game = staged(NORMAL, 9, phase="playing")
        game.player.invulnerable = 2 * TICK_RATE - 10
        for reduced, row in ((True, "SAFE 2s"), (False, "")):
            with self.subTest(reduced_motion=reduced):
                screen, _, _ = shown(game, reduced_motion=reduced)
                self.assertEqual(screen.row_text(23).rstrip(), row)
        game.player.invulnerable = 0
        self.assertEqual(shown(game, reduced_motion=True)[0].row_text(23).rstrip(), "")

    def test_the_intro_act_and_boss_panels_show_the_game_s_lines(self):
        for wave, role in ((9, "accent"), (7, "accent"), (6, "danger")):
            with self.subTest(wave=wave):
                game = staged(NORMAL, wave)
                screen, painter, _ = shown(game, theme=COLOR)
                rows = rows_of(screen)
                title, *lines = game.intro
                at = rows.index(title)  # the title, a blank row, then each line
                self.assertEqual([rows.index(line) for line in lines], list(range(at + 2, at + 2 + len(lines))))
                self.assertEqual(style_at(screen, game.intro[0]), painter.colors[SLOT[role]] | A_BOLD)
        self.assertEqual(staged(NORMAL, 9).intro[0], "WAVE 09  ACT II: SWARM")
        self.assertEqual(staged(NORMAL, 7).intro[0], "ACT II  SWARM")
        self.assertEqual(staged(NORMAL, 6).intro[:2], ("WARNING", "WARDEN"))
        self.assertNotIn("WARNING", shown(staged(NORMAL, 6, phase="playing"))[0].content())  # gone once it plays

    def test_the_clear_panel_shows_the_notice_line_by_line(self):
        game = staged(NORMAL, 3, phase="playing")
        game.aliens.clear()
        game._pending.clear()
        for _ in range(200):
            if game.phase == "clear":
                break
            game.step(())
        self.assertEqual(game.phase, "clear")
        screen, painter, _ = shown(game, theme=COLOR)
        title, *lines = game.notice.split("\n")
        self.assertTrue(title.startswith("WAVE 03 CLEAR  +"), title)
        self.assertEqual(style_at(screen, title), painter.colors[SLOT["good"]] | A_BOLD)
        rows = rows_of(screen)
        at = rows.index(title)
        self.assertEqual([rows.index(line) for line in lines], list(range(at + 2, at + 2 + len(lines))))
        self.assertTrue(lines[-1].startswith("BEST COMBO x"), lines)

    def card(self, game, theme=MONO):
        """The card drawn at 60 x 24: the screen, the painter, its title's row and its left column."""
        screen, painter, _ = shown(game, theme=theme)
        top = find(screen, "CHOOSE AN UPGRADE")[0]
        return screen, painter, top, screen.row_text(top + 1).index("+" if game.cursor else "#")

    def test_the_upgrade_card_has_three_ascii_cards_of_16_inside_and_marks_the_selected_one(self):
        game = staged(NORMAL, 10, phase="upgrade", upgrades=("TRIPLE SHOT", "RAIL ROUNDS"))
        for cursor in (1, 0, 2):
            with self.subTest(cursor=cursor):
                game.cursor = cursor
                screen, painter, top, left = self.card(game)
                self.assertEqual(screen.row_text(top).strip(), "CHOOSE AN UPGRADE   3 of 7")
                rows = [screen.row_text(y)[left : left + 57] for y in range(top + 1, top + 7)]
                self.assertTrue(all(len(row.rstrip()) == 56 for row in rows))  # 3 x 18 with 1-column gaps
                for index, name in enumerate(game.offers):
                    card = [row[19 * index : 19 * index + 19] for row in rows]
                    self.assertEqual({line[18:] for line in card}, {" "})
                    card = [line[:18] for line in card]
                    chosen = index == cursor
                    side = "#" if chosen else "|"
                    edge = "#" + "=" * 16 + "#" if chosen else "+" + "-" * 16 + "+"
                    self.assertEqual((card[0], card[-1]), (edge, edge))
                    self.assertTrue(all(line.isascii() and line[0] == line[-1] == side for line in card[1:-1]))
                    self.assertEqual(card[1][1:-1].strip(), upgrade_family(name))
                    self.assertEqual(card[2][1:-1].rstrip(), (">" if chosen else " ") + name)
                    upgrade = UPGRADES[name]
                    self.assertEqual(card[3][1:-1].strip(), upgrade.blurb[0])
                    stack = [f"{game.upgrades.get(name, 0) + 1}/{upgrade.stacks}"] if upgrade.stacks > 1 else []
                    self.assertEqual(card[4][1:-1].split(), [*upgrade.blurb[1].split(), *stack])
                    self.assertEqual((card[3][1], card[4][1], card[3][-3:-1], card[4][-3:-1]), (" ", " ", "  ", "  "))
                    name_style = screen.styles[(top + 3) * 60 + left + 19 * index + 2]
                    self.assertEqual(bool(name_style & A_BOLD), chosen)  # mono: bold and the border carry it
                self.assertEqual(screen.row_text(top + 7)[left : left + 56].rstrip(), " OWNED  TS RR")
        screen, painter, top, left = self.card(game, COLOR)
        self.assertEqual(screen.styles[(top + 3) * 60 + left + 19 * 2 + 2], painter.colors[SLOT["accent"]] | A_BOLD)
        self.assertEqual(screen.styles[(top + 3) * 60 + left + 2], painter.colors[SLOT["text"]])

    def test_the_card_counts_stacks_and_the_run_s_cards_and_endless_has_no_last_one(self):
        game = staged(NORMAL, 4, phase="upgrade")
        screen, _, top, _ = self.card(game)
        self.assertEqual(screen.row_text(top).strip(), "CHOOSE AN UPGRADE   1 of 7")
        self.assertNotIn("OWNED", screen.content())  # nothing owned yet
        game.offers, game.upgrades, game.cursor = ("RAIL ROUNDS", "THRUSTERS", "MAGNET"), {"RAIL ROUNDS": 1}, 1
        game.picks = ("RAIL ROUNDS",) * 6
        screen, _, top, left = self.card(game)
        self.assertEqual(screen.row_text(top).strip(), "CHOOSE AN UPGRADE   7 of 7")
        self.assertEqual(screen.row_text(top + 4)[left : left + 37], "| faster shots   | # ship moves     #")
        self.assertEqual(screen.row_text(top + 5)[left : left + 37], "|           2/2  | # faster    1/2  #")
        endless = staged(Config("normal", "endless", 1978), 4, phase="upgrade")
        screen, _, top, _ = self.card(endless)
        self.assertEqual(screen.row_text(top).strip(), "CHOOSE AN UPGRADE   CARD 1")
        endless.picks = ("MAGNET",) * 8
        endless.offers, endless.cursor = ("REFIT", endless.offers[1], "BOUNTY"), 1  # Endless's fillers
        screen, _, top, left = self.card(endless)
        self.assertEqual(screen.row_text(top).strip(), "CHOOSE AN UPGRADE   CARD 9")
        self.assertEqual(screen.row_text(top + 2)[left : left + 18], "| OFFENSE        |")
        self.assertEqual(screen.row_text(top + 3)[left : left + 18], "| REFIT          |")
        self.assertEqual(screen.row_text(top + 4)[left : left + 18], "| +1 bomb and    |")
        self.assertEqual(screen.row_text(top + 5)[left + 38 : left + 56], "| right now      |")

    def test_a_card_drawn_from_another_family_names_its_own_family(self):
        """Spec 10.2: a family with nothing left fills its slot from another family, so the family line names the
        offer's own family, not the slot's (second review, SI3)."""
        offense = ("TRIPLE SHOT", "RAIL ROUNDS", "RAIL ROUNDS", "PIERCER")  # Offense maxed
        game = staged(Config("normal", "classic", 4), 13, phase="upgrade", upgrades=offense)
        self.assertNotEqual([UPGRADES[name].family for name in game.offers], list(FAMILIES))
        screen, _, top, left = self.card(game)
        for index, name in enumerate(game.offers):
            family = screen.row_text(top + 2)[left + 19 * index + 1 : left + 19 * index + 17].strip()
            self.assertEqual(family, upgrade_family(name), name)

    def test_the_card_key_line_is_dim_until_the_card_arms(self):
        game = staged(NORMAL, 4, phase="upgrade")
        self.assertGreater(game.armed, 0)
        keys = "A/D choose    Space take    P pause"
        screen, painter, _ = shown(game, theme=COLOR)
        self.assertIn(keys, rows_of(screen))
        self.assertEqual(style_at(screen, keys), painter.colors[SLOT["dim"]])
        game.armed = 0
        screen, painter, _ = shown(game, theme=COLOR)
        self.assertEqual(style_at(screen, keys), painter.colors[SLOT["accent"]])
        arrows = Keymap.preset("arrows")
        self.assertIn(panel_keys("card", arrows)[0], rows_of(shown(game, keymap=arrows)[0]))

    def test_the_continue_prompt_counts_down_and_dims_its_choices_until_armed(self):
        game = staged(Config("easy", "classic", 1978), 2, phase="continue")
        self.assertGreater(game.armed, 0)
        screen, painter, _ = shown(game, theme=COLOR)
        rows = [row for row in rows_of(screen)[3:21] if row and not row.startswith("+")]
        seconds = math.ceil(game.prompt / TICK_RATE)
        self.assertEqual(
            rows,
            [
                "SHIP LOST",
                f"CONTINUE?   {seconds}",
                "Space continue - the score restarts at 0",
                f"{'B end the run':<25}1 continue left",
            ],
        )
        self.assertEqual(style_at(screen, "SHIP LOST"), painter.colors[SLOT["danger"]] | A_BOLD)
        self.assertEqual(style_at(screen, "Space continue"), painter.colors[SLOT["dim"]])
        self.assertEqual(style_at(screen, "B end"), painter.colors[SLOT["dim"]])
        game.armed, game.continues, game.notice = 0, 2, "THE FLEET LANDED"
        screen, painter, _ = shown(game, theme=COLOR)
        self.assertEqual(style_at(screen, "Space continue"), painter.colors[SLOT["accent"]])
        self.assertEqual(style_at(screen, "B end"), painter.colors[SLOT["accent"]])
        self.assertIn("2 continues left", screen.content())
        self.assertIn("THE FLEET LANDED", rows_of(screen))
        self.assertEqual(screen.row_text(22).rstrip(), HINTS["prompt"])

    def test_the_pause_countdown_and_handoff_panels_stand_in_for_the_phase_s_own(self):
        """Spec 15.3 and 16.1: P pauses on the card and the prompt. The PAUSED, countdown and handoff panels take the
        place of the card, the prompt or an intro, which would otherwise show round them (second review, SI2)."""
        card = staged(NORMAL, 4, phase="upgrade")
        endless = staged(Config("normal", "endless", 1978), 40)  # an intro with a NEW: line, wider than PAUSED
        endless.notes = []  # its NEW: note shows on the bottom row too
        cases = (
            (card, ["CHOOSE AN UPGRADE", "OFFENSE", card.offers[0], panel_keys("card", Keymap.preset("default"))[0]]),
            (staged(Config("easy", "classic", 1978), 2, phase="continue"), ["SHIP LOST", "CONTINUE?"]),
            (endless, list(endless.intro)),
        )
        for game, texts in cases:
            scenes = ("paused", "countdown", "handoff") if game is endless else ("paused", "countdown")
            for columns in (80, 60):
                for scene in scenes:
                    current = session(two_player=scene == "handoff")
                    if scene == "handoff":
                        current.scene, panel = "handoff", f"{current.pilot.name}, YOU'RE UP"
                    else:
                        current.pause("focus")
                        if scene == "countdown":
                            current.resume(5.0)
                        panel = "GET READY" if scene == "countdown" else "PAUSED"
                    content = shown(game, 24, columns, current=current)[0].content()
                    self.assertIn(panel, content)
                    for text in texts:
                        self.assertNotIn(text, content, (scene, columns))

    def results(self, game):
        current = session()
        current.pilots[0].game = game
        current.scene = "results"
        screen = Screen()
        with no_field():
            Painter(screen, current.options, MONO).draw(current)
        return rows_of(screen)

    def test_results_name_the_act_and_wave_the_reason_the_upgrades_and_a_continue(self):
        """Phase 5 spec 8: the results card says the game over in words, where the run reached, its upgrades by
        name and a continue; the keys stay on row 21 and the seed on row 19."""
        upgrades = ("TRIPLE SHOT", "RAIL ROUNDS", "RAIL ROUNDS", "DEFLECTOR")
        game = staged(Config("easy", "classic", 1978), 15, phase="playing", upgrades=upgrades)
        game.phase, game.notice, game.score, game.continues_used = "game_over", "SHIP LOST", 12345, 1
        game.hit_cause = "needle"
        rows = self.results(game)
        lines = (
            "SIGNAL LOST",
            "GAME OVER - SHOT DOWN BY A NEEDLE",
            "SCORE        12,345",
            f'REACHED      ACT III  WAVE 15  "{game.wave_name}"',
            "UPGRADES     TRIPLE SHOT, RAIL ROUNDS x2, DEFLECTOR",
            "CONTINUED    once",
            "SEED 1978",
            "Enter fly again   Q menu",
        )
        at = [rows.index(line) for line in lines]
        self.assertEqual(at, sorted(at))
        self.assertEqual((at[0], at[1], at[2], at[-2], at[-1]), (1, 2, 4, 19, 21))
        self.assertTrue(rows[at[3] + 2].startswith("KILLS") and "BEST COMBO x" in rows[at[3] + 2])
        self.assertNotIn("SHIP LOST", rows)  # the arena's notice: the card says it in words
        game = staged(Config("easy", "classic", 1978), 24, phase="playing")
        game.phase = "victory"
        rows = self.results(game)
        self.assertIn("EARTH SAVED", rows)
        self.assertIn("ALL 24 WAVES CLEARED", rows)
        self.assertIn("SCORE        0", rows)
        self.assertTrue(any(row.startswith("REACHED      ACT IV  WAVE 24") for row in rows))
        self.assertFalse({"SIGNAL LOST", "CONTINUED    once", "SHIP LOST"} & set(rows))
        self.assertFalse([row for row in rows if row.startswith(("UPGRADES", "CONTINUED", "BOSSES", "BOMBS"))])

    def test_the_pause_panel_lists_endless_modifiers(self):
        game = staged(Config("normal", "endless", 1978), 40, phase="playing")
        game.modifiers = ["QUICK RELOAD", "ARMOURED", "EXTRA SLOT", "DIVE STORM", "FAST SHOTS", "REINFORCED"]
        current = session()
        for shown_game, lines in (
            (game, ["MODS QUICK RELOAD, ARMOURED, EXTRA SLOT, DIVE STORM,", "FAST SHOTS, REINFORCED"]),
            (staged(NORMAL, 9, phase="playing"), []),
        ):
            current.pilots[0].game = shown_game
            current.scene = "playing"
            current.pause("manual")
            screen = Screen()
            with no_field():
                Painter(screen, current.options, MONO).draw(current)
            rows = rows_of(screen)
            seed = rows.index("SEED 1978")
            self.assertEqual(rows[seed + 1 : seed + 1 + len(lines)], lines)
            self.assertEqual(rows[seed + 1 + len(lines)], "P / Escape / Enter resume")

    def test_the_painter_has_no_sprite_helper(self):
        self.assertFalse(hasattr(Painter, "sprite"))  # the arena draws its own art (sprites.cell)


class RecordsTests(unittest.TestCase):
    """Spec 16.3 and 17: the records pages call classic CAMPAIGN; campaign-1's rows have their own OLD RULES page."""

    def profile(self, folder, rows):
        path = Path(folder) / "profile.json"
        rows = [row | {"date": "2025-06-01", "victory": False} for row in rows]
        path.write_text(json.dumps({"version": 1, "name": "OLD", "scores": rows}))  # version 1: all campaign-1
        return Profile(path)

    def pages(self, current):
        titles = []
        for at in range(4):
            screen = Screen()
            Painter(screen, current.options, MONO).draw(current)
            titles.append(screen.row_text(1).strip())
            current.command("page", float(at))
        return titles

    def test_the_records_title_and_rows_call_the_mode_campaign(self):
        current = session()
        current.profile.data["scores"] = [
            dict(name="ACE", score=900, wave=7, mode="classic", difficulty="normal", seed=1)
        ]
        current.scene = "records"
        screen = Screen()
        Painter(screen, current.options, MONO).draw(current)
        self.assertEqual(screen.row_text(1).strip(), "FLIGHT RECORDS - CAMPAIGN / NORMAL")
        self.assertEqual(screen.row_text(5).split(), ["ACE", "900", "7", "campaign", "normal"])
        self.assertNotIn("CLASSIC", screen.content().upper())
        self.assertEqual(self.pages(current), ["FLIGHT RECORDS - CAMPAIGN / NORMAL", "CAREER & ACHIEVEMENTS"] * 2)

    def test_the_old_rules_page_shows_campaign_1_boards_best_first_and_only_when_there_are_any(self):
        rows = [
            dict(name=f"P{n}", score=1000 * n, wave=n, mode="classic", difficulty="normal", seed=n)
            for n in range(1, 13)
        ]
        rows += [
            dict(name="HARDY", score=500, wave=4, mode="classic", difficulty="hard", seed=1),
            dict(name="ENDER", score=700, wave=9, mode="endless", difficulty="easy", seed=2),
        ]
        with tempfile.TemporaryDirectory() as folder:
            current = session()
            current.profile = self.profile(folder, rows)
            current.scene = "records"
            titles = ["FLIGHT RECORDS - CAMPAIGN / NORMAL", "CAREER & ACHIEVEMENTS", "FLIGHT RECORDS - OLD RULES"]
            self.assertEqual(self.pages(current), [*titles, titles[0]])
            current.page = 0
            screen = Screen()
            Painter(screen, current.options, MONO).draw(current)
            self.assertIn("Your first flight belongs here.", screen.content())  # campaign-1 rows rank apart
            current.page = 2
            Painter(screen, current.options, MONO).draw(current)
            self.assertEqual(screen.row_text(3).strip(), OLD_RULES)
            self.assertEqual(screen.row_text(5).split(), ["PILOT", "SCORE", "WAVE", "MODE", "DIFFICULTY"])
            listed = [screen.row_text(y).split() for y in range(7, 21) if screen.row_text(y).strip()]
            # boards in MODES x DIFFICULTIES order, best first; three boards share the 13 rows, four rows each
            self.assertEqual([row[0] for row in listed], ["P12", "P11", "P10", "P9", "HARDY", "ENDER"])
            self.assertEqual(listed[0], ["P12", "12000", "12", "campaign", "normal"])
            self.assertEqual(listed[-1], ["ENDER", "700", "9", "endless", "easy"])
            current.profile = self.profile(folder, rows[:12])  # one board: its ten best
            Painter(screen, current.options, MONO).draw(current)
            listed = [screen.row_text(y).split()[0] for y in range(7, 21) if screen.row_text(y).strip()]
            self.assertEqual(listed, [f"P{n}" for n in range(12, 2, -1)])
            self.assertEqual(screen.row_text(21).strip(), "Left/Right switch page   Enter / Escape to return")


class HintTests(unittest.TestCase):
    def test_the_default_keymap_shows_the_spec_text(self):
        keymap = Keymap.preset("default")
        for scheme, text in HINTS.items():
            self.assertEqual(hint(scheme, keymap), text)
        self.assertEqual(help_lines(keymap), HELP)  # tap controls in the glyph arena

    def test_every_preset_builds_ascii_hints_and_help_that_fit_the_block(self):
        self.assertEqual(HELP_WIDTH, 49)
        for name in PRESETS:
            keymap = Keymap.preset(name)
            for scheme in HINTS:
                with self.subTest(preset=name, scheme=scheme):
                    text = hint(scheme, keymap)
                    self.assertLessEqual(len(text), WIDTH)
                    self.assertTrue(text.isascii() and text.isprintable())
                    if scheme == "timed":  # a quick press latches: the stop key, and every part, fit
                        stop = "S" if name == "default" else label(keymap.keys_for("play", "stop")[0])
                        self.assertIn(f"Move, {stop} Stop  ", text)
                        self.assertIn(" Bomb  ", text)
            lines = help_lines(keymap)
            self.assertEqual([title for title, _ in lines], [title for title, _ in HELP])
            self.assertTrue(all(len(detail) <= HELP_WIDTH and detail.isascii() for _, detail in lines))

    def test_a_long_custom_binding_fits_the_help_card_at_a_word_boundary(self):
        """HOW TO PLAY names an action's first key only (Phase 5 spec 9 F6); a long one still fits at a word."""
        cases = (
            ({"play.fire": ["space", "up", "w", "z", "k", "enter", "tab", "f2"]}, "FIRE"),
            ({"play.pause": ["ctrl+alt+super+page_down", "p"]}, "PAUSE"),
        )
        for bind, row in cases:
            with self.subTest(bind=bind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "keys.json"
                path.write_text(json.dumps({"bind": bind}))
                text = dict(help_lines(Keymap.load(path)))[row]
                self.assertLessEqual(len(text), HELP_WIDTH)
                if row == "FIRE":
                    self.assertEqual(text, "Space fires; hold it to keep firing")
                else:
                    self.assertEqual(text, "Ctrl+Alt+Super+PgDn | M sound | F1 (fn+F1) stats")  # spaces squeezed

    def test_built_hints_name_the_first_bound_key(self):
        vim, arrows = Keymap.preset("vim"), Keymap.preset("arrows")
        self.assertEqual(hint("hold", vim), "Hold H/L  K Fire  B Bomb  P Pause")
        self.assertEqual(hint("latched", vim), "Tap H/L: move, J: stop  K Fire  B Bomb  P Pause")
        self.assertEqual(hint("card", vim), "H/L choose  K take  P Pause")
        self.assertEqual(hint("prompt", vim), "K continue  B end the run  P Pause")
        self.assertEqual(hint("menu", vim), "K/J select  Enter choose  Esc Esc quit")
        self.assertEqual(hint("timed", arrows), "Left/Right Move, Down Stop  Space Fire  B Bomb  P Pause")
        self.assertEqual(hint("timed", vim), "H/L Move, J Stop  K Fire  B Bomb  P Pause")
        self.assertEqual(hint("latched", arrows), "Tap Left/Right: move, Down: stop  Space Fire  P Pause")  # no Bomb
        self.assertEqual(hint("replay", arrows), "P Pause, then Q Exit")
        self.assertEqual(help_lines(vim)[0], ("MOVE", "Tap H/L to start; tap back or J to stop"))
        self.assertEqual(help_lines(vim, "hold")[0], ("MOVE", "Hold H/L; let go to stop"))
        self.assertEqual(help_lines(vim, "timed")[0], ("MOVE", "H/L; J stops"))
        self.assertEqual(dict(help_lines(vim))["FIRE"], "K fires; hold it to keep firing")
        self.assertEqual(dict(help_lines(arrows))["PAUSE"], "P   |   M sound   |   F1 (fn+F1) stats")

    def test_the_spec_hints_and_no_hint_or_help_line_names_autofire(self):
        self.assertEqual(HINTS["hold"], "Hold A/D or arrows  Space Fire  B Bomb  P Pause")
        self.assertEqual(HINTS["latched"], "Tap A/D: move, S: stop  Space Fire  B Bomb  P Pause")
        self.assertEqual(HINTS["timed"], "A/D or arrows Move, S Stop  Space Fire  B Bomb  P Pause")
        self.assertEqual(HINTS["card"], "A/D choose  Space take  P Pause")
        self.assertEqual(HINTS["prompt"], "Space continue  B end the run  P Pause")
        for name in PRESETS:
            keymap = Keymap.preset(name)
            texts = [hint(scheme, keymap) for scheme in HINTS] + [detail for _, detail in help_lines(keymap)]
            self.assertFalse([text for text in texts if "Auto" in text or "autofire" in text], name)

    def test_long_keys_drop_bomb_then_shorten_the_move_and_never_drop_pause(self):
        moves = {"play.left": ["ctrl+alt+page_down"], "play.right": ["ctrl+alt+page_up"], "play.stop": ["ctrl+alt+end"]}
        widest = {"play.fire": ["ctrl+alt+super+page_down"], "play.pause": ["ctrl+alt+super+insert"]}
        cases = (
            ({}, "latched", "Ctrl+Alt+PgDn/Ctrl+Alt+PgUp  Space Fire  P Pause"),  # Bomb, then the move's words
            ({}, "hold", "Hold Ctrl+Alt+PgDn/Ctrl+Alt+PgUp  Space Fire  P Pause"),  # Bomb only
            ({}, "timed", "Ctrl+Alt+PgDn/Ctrl+Alt+PgUp  Space Fire  P Pause"),  # Bomb, then Move and Stop
            ({}, "card", "Ctrl+Alt+PgDn/Ctrl+Alt+PgUp choose  Space take  P Pause"),
            (widest, "prompt", "Ctrl+Alt+Super+PgDn continue  Ctrl+Alt+Super+Ins Pause"),
            (widest, "latched", "Ctrl+Alt+PgDn/Ctrl+Alt+PgUp  Ctrl+Alt+Super+Ins Pause"),  # whole words only
        )
        for extra, scheme, expected in cases:
            with self.subTest(scheme=scheme, extra=extra), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "keys.json"
                path.write_text(json.dumps({"bind": moves | extra}))
                text = hint(scheme, Keymap.load(path))
                self.assertEqual(text, expected)
                self.assertLessEqual(len(text), WIDTH)
                self.assertTrue(text.endswith(" Pause"), text)

    def test_the_default_panels_show_the_spec_text_and_every_preset_builds_lines_that_fit(self):
        self.assertIs(panel_keys("pause", Keymap.preset("default")), PANELS["pause"])
        for name in PRESETS:
            for panel in PANELS:
                with self.subTest(preset=name, panel=panel):
                    lines = panel_keys(panel, Keymap.preset(name))
                    self.assertEqual(len(lines), len(PANELS[panel]))
                    self.assertTrue(all(len(line) <= PANEL_WIDTH and line.isascii() for line in lines))

    def test_a_replay_pause_and_a_custom_keymap_show_the_keys_that_work(self):
        current = session()
        current.replay = Mock()
        current.pause("manual")
        screen = Screen()
        Painter(screen, current.options, MONO).draw(current)
        self.assertIn("M sound   Q exit", screen.content())  # R does nothing in a replay, and Q exits it
        self.assertNotIn("restart", screen.content())
        current.handle(Key("q"), 0)
        self.assertTrue(current.quit)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.json"
            path.write_text('{"bind": {"pause.menu": ["x"], "pause.restart": ["n"], "results.menu": ["x", "escape"]}}')
            current = session(keymap=Keymap.load(path))
        current.pause("manual")
        Painter(screen, current.options, MONO).draw(current)
        self.assertIn("N restart   M sound   X menu", screen.content())
        current.handle(Key("x"), 0)
        self.assertEqual(current.scene, "menu")
        self.assertEqual(panel_keys("results", current.keymap), ("Enter fly again   X menu",))

    def test_the_help_card_has_the_campaign_rows(self):
        rows = dict(HELP)
        self.assertEqual(len(HELP), 14)
        self.assertEqual(
            [name for name, _ in HELP],
            ["MOVE", "CONTROLS", "LEGEND", "FIRE", "BOMB", "PAUSE", "SESSION", "PICKUPS", "SCORE"]
            + ["CAMPAIGN", "ARCADE", "ENDLESS", "DAILY", "DUEL"],
        )
        self.assertEqual(rows["FIRE"], "Space fires; hold it to keep firing")
        self.assertEqual(rows["BOMB"], "B clears enemy fire; combos earn more")
        self.assertEqual(rows["PAUSE"], "P or Escape   |   M sound   |   F1 (fn+F1) stats")
        self.assertEqual(rows["LEGEND"], "/A\\ you {A} shielded  ! v : theirs  [R] catch")
        self.assertEqual(rows["PICKUPS"], "R rapid S spread + shield T slow L repair B bomb")
        self.assertEqual(rows["SCORE"], "Hits in a row raise COMBO; a miss breaks it.")
        self.assertEqual(rows["CAMPAIGN"], "24 waves, 4 bosses, an upgrade every 3 waves.")
        self.assertEqual(rows["DUEL"], "Two pilots, same seed. Swap each wave and life.")
        self.assertNotIn("CLASSIC", rows)
        self.assertNotIn("UPGRADES", rows)
        current = session(quickstart=False)
        current.change_scene("help")
        screen = Screen()
        Painter(screen, current.options, MONO).draw(current)
        self.assertIn("CAMPAIGN  24 waves, 4 bosses, an upgrade every 3 waves.", screen.content())

    def test_labels(self):
        cases = {"a": "A", "@w": "W", "space": "Space", "escape": "Esc", "ctrl+x": "Ctrl+X", "alt+f1": "Alt+F1"}
        cases |= {"page_up": "PgUp", "left": "Left", ";": ";", "f12": "F12"}
        self.assertEqual({spec: label(spec) for spec in cases}, cases)


# --- Phase 5: the title menu, the new cards, the results card, the too-small notice, the footer and HOW TO PLAY ---


def real_session(folder, **overrides):
    """A Session on a real profile file in folder (the initials card needs a storage.Placing)."""
    options = dict(seed=42, fixed_seed=True, quickstart=True, name="TEST", color="mono") | overrides
    return Session(defaults(**options), Profile(Path(folder) / "profile.json"), Mock(enabled=False))


def game_over(current, score=None, now=0.0):
    """The active pilot's run ends on the next step, at now (its engine stands still, so Arcade's can end too)."""
    game = current.pilot.game
    game.step = lambda actions: None
    game.events = []
    if score is not None and not isinstance(game, ArcadeGame):
        game.score = score
    game.phase = "game_over"
    current.change_scene("playing")
    current.step(now)


def tap(current, name: str, at: float, certain: bool) -> None:
    """A tap of a key at a time: a press, and on kitty its release 50 ms later."""
    current.handle(Key(name, kind=PRESS), at)
    if certain:
        current.handle(Key(name, kind=RELEASE), at + 0.05)


def drawn(current, rows=24, columns=60, theme=MONO):
    screen = Screen(rows, columns)
    painter = Painter(screen, current.options, theme)
    painter.draw(current)
    return rows_of(screen), painter


class Phase5MenuTests(unittest.TestCase):
    """Phase 5 spec 9-10: the 7-row title menu with SETTINGS, QUIT on row 18, Esc Esc, Esc again."""

    def test_the_menu_has_seven_rows_and_quit_is_the_last(self):
        self.assertEqual(MENU, ("LAUNCH", "MODE", "DIFFICULTY", "FLIGHT RECORDS", "HOW TO PLAY", "SETTINGS", "QUIT"))
        self.assertEqual(QUIT, 6)
        current = session(quickstart=False)
        rows, _ = drawn(current)
        self.assertEqual([rows[y].lstrip("> ").split("  ")[0] for y in range(12, 19)], list(MENU))
        self.assertTrue(rows[12].startswith("> LAUNCH"))
        for _ in range(QUIT):
            current.command("down", 0)
        rows, _ = drawn(current)
        self.assertEqual(rows[18], "> QUIT")
        self.assertEqual(rows[19], "Esc again, or Enter, to quit.")  # spec 9 F3
        current.command("down", 0)
        self.assertEqual(current.selection, QUIT)  # it stops at QUIT (PN-1)
        for _ in range(QUIT):
            current.command("up", 0)
        self.assertEqual(current.selection, 0)
        current.command("up", 0)
        self.assertEqual((current.selection, current.quit), (0, False))  # and at LAUNCH: Up never reaches QUIT
        current.handle(Key("up"), 1.0)
        current.handle(Key("enter"), 2.0)
        self.assertEqual((current.scene, current.quit), ("playing", False))  # PN-1: b2's Up, Up, ..., Enter

    def test_esc_moves_to_quit_and_esc_again_quits(self):
        current = session(quickstart=False)
        current.handle(Key("escape"), 1.0)
        self.assertEqual((current.selection, current.quit), (QUIT, False))
        self.assertEqual(menu_line(current), "Esc again, or Enter, to quit.")
        current.handle(Key("escape"), 3.0)
        self.assertTrue(current.quit)
        vim = session(quickstart=False, keys="vim")
        vim.selection = QUIT
        self.assertEqual(menu_line(vim), "Esc again, or Enter, to quit.")

    def test_settings_opens_from_the_menu_and_records_open_on_their_first_page(self):
        current = session(quickstart=False)
        current.selection = MENU.index("SETTINGS")
        current.command("select", 0)
        self.assertEqual((current.scene, current.context), ("settings", "settings"))
        current.command("back", 0)
        self.assertEqual(current.scene, "menu")
        current.page = 1
        current.selection = MENU.index("FLIGHT RECORDS")
        current.command("select", 0)
        self.assertEqual((current.scene, current.page), ("records", 0))  # spec 9 F8

    def test_the_new_scenes_contexts(self):
        current = session(quickstart=False)
        for scene, context in (("settings", "settings"), ("initials", "initials"), ("attract", "attract")):
            current.scene = scene
            self.assertEqual(current.context, context)
            self.assertEqual(CONTEXTS[scene], context)
        current.scene, current.demo = "playing", True
        self.assertEqual(current.context, "attract")  # the demo's own Session binds nothing

    def test_every_new_contexts_commands_run_on_a_fresh_session(self):
        for context in ("settings", "initials"):
            for name in ACTIONS[context]:
                with self.subTest(context=context, command=name):
                    current = session(quickstart=False)
                    current.hooks = Mock()
                    if context == "settings":
                        current.open_settings()
                    else:
                        current.open_initials(pilot_mode=True)
                    current.command(name, 0)

    def test_the_footer_says_new_pilot_and_sound_n_a(self):
        current = session(quickstart=False, name="PILOT")
        rows, _ = drawn(current)
        self.assertEqual(rows[23], "NEW PILOT  |  SOUND OFF [M]")  # spec 9 F4
        current.options.name = "ACE"
        current.audio = SimpleNamespace(enabled=False, available=False)
        rows, _ = drawn(current)
        self.assertEqual(rows[23], "PILOT ACE  |  SOUND n/a")  # spec 9 F10
        current.audio = SimpleNamespace(enabled=True)  # frames.py's stub: no `available`
        rows, _ = drawn(current)
        self.assertEqual(rows[23], "PILOT ACE  |  SOUND ON [M]")


class HowToPlayTests(unittest.TestCase):
    """Phase 5 spec 9 F6 and F9: the MOVE, CONTROLS and LEGEND rows follow the scheme, the keymap and the scale."""

    def test_move_and_controls_follow_the_scheme(self):
        keymap = Keymap.preset("default")
        for scheme, move, controls in (
            ("hold", "Hold A/D or arrows; let go to stop", "Hold: this terminal reports key releases"),
            ("latched", "Tap A/D to start; tap back or S to stop", "Tap: this terminal sends no key releases"),
            ("timed", "A/D or arrows; S stops", "Timed, as chosen in SETTINGS or by --controls"),
        ):
            rows = dict(help_lines(keymap, scheme))
            self.assertEqual((rows["MOVE"], rows["CONTROLS"]), (move, controls), scheme)
        rows = dict(help_lines(keymap, "latched", controls="latched"))
        self.assertEqual(rows["CONTROLS"], "Tap, as chosen in SETTINGS or by --controls")

    def test_the_legend_matches_the_arena_this_window_draws(self):
        keymap = Keymap.preset("default")
        self.assertEqual(dict(help_lines(keymap, k=1))["LEGEND"], "/A\\ you {A} shielded  ! v : theirs  [R] catch")
        for k in (2, 3, 4, 6):
            self.assertEqual(dict(help_lines(keymap, k=k))["LEGEND"], "Shots falling are theirs. Catch falling letters")

    def test_every_preset_and_scheme_fits_the_card(self):
        for name in PRESETS:
            for scheme in ("hold", "latched", "timed"):
                for k in (1, 2):
                    lines = help_lines(Keymap.preset(name), scheme, k)
                    self.assertEqual(len(lines), 14)
                    self.assertTrue(all(len(text) <= HELP_WIDTH for _, text in lines), (name, scheme, k))

    def test_row_19_says_a_bigger_window_shows_pixel_art_only_where_it_would(self):
        current = session(quickstart=False)
        current.change_scene("help")
        rows, _ = drawn(current, 24, 80)
        self.assertEqual(rows[19], "A bigger window (120 x 44 or more) shows pixel art.")
        rows, _ = drawn(current, 44, 120)
        self.assertEqual(rows[(44 - 24) // 2 + 19], "Small windows and pause stop the entire simulation.")
        glyphs = session(quickstart=False, scale="1")
        glyphs.change_scene("help")
        rows, _ = drawn(glyphs, 24, 80)
        self.assertEqual(rows[19], "Small windows and pause stop the entire simulation.")  # --glyphs: never pixels

    def test_mono_without_half_blocks_draws_the_glyph_arena(self):
        current = session()
        no_blocks = SimpleNamespace(unicode_blocks=False)
        self.assertEqual(max_scale(current.options, MONO, no_blocks), 1)
        self.assertIsNone(max_scale(current.options, MONO, SimpleNamespace(unicode_blocks=True)))
        self.assertIsNone(max_scale(current.options, MONO, None))
        screen = Screen(64, 180)
        painter = Painter(screen, current.options, MONO, no_blocks)
        painter.draw(current)
        self.assertEqual(painter.layout.k, 1)


class SettingsCardTests(unittest.TestCase):
    """Phase 5 spec 4.2: rows cycle, a flag locks its row, a change calls the hooks, PALETTE and BACKGROUND wait for
    the repaint rule, and leaving saves once."""

    def card(self, **overrides):
        current = session(quickstart=False, **overrides)
        current.hooks = Mock()
        current.profile.update_settings.return_value = True
        current.open_settings()
        return current

    def to(self, current, key):
        current.settings_card.cursor = list(settings.ROWS).index(key)

    def test_left_and_right_cycle_a_row_and_call_the_hooks_at_once(self):
        current = self.card()
        self.to(current, "effects")
        current.handle(Key("right"), 1.0)
        self.assertEqual(current.options.effects, "calm")
        current.hooks.apply.assert_called_once_with("effects")
        current.handle(Key("left"), 2.0)
        current.handle(Key("left"), 3.0)
        self.assertEqual(current.options.effects, "off")
        current.handle(Key("enter"), 4.0)  # Enter is Right on a value row
        self.assertEqual(current.options.effects, "full")
        self.assertEqual(current.scene, "settings")

    def test_a_flag_locks_its_row(self):
        current = self.card(scale="2", given=frozenset({"scale"}))
        self.to(current, "scale")
        current.handle(Key("right"), 1.0)
        self.assertEqual(current.options.scale, "2")
        current.hooks.apply.assert_not_called()
        rows, painter = drawn(current)
        row = rows[screens.FIRST_ROW + list(settings.ROWS).index("scale")]
        self.assertIn("< 2X (--scale) >", " ".join(row.split()).replace("< ", "< ").replace(" >", " >"))
        self.assertIn("Set by --scale for this run. Saved: AUTO.", rows)

    def test_palette_and_background_wait_for_the_repaint_rule(self):
        current = self.card()
        self.to(current, "palette")
        current.handle(Key("right"), 1.0)
        self.assertEqual(current.options.palette, "cvd")
        current.hooks.apply.assert_not_called()  # the field shows it at once
        rows, _ = drawn(current)
        self.assertIn("COLOUR-BLIND SAFE", rows[screens.FIRST_ROW])
        self.assertFalse(current.update_attract(1.4))
        self.assertTrue(current.update_attract(1.5))  # 0.5 s still
        current.hooks.apply.assert_called_once_with("palette")
        current.handle(Key("right"), 1.6)
        current.handle(Key("down"), 1.7)  # leaving the row makes it due, but not within 1 s of the last
        self.assertFalse(current.update_attract(2.4))
        self.assertTrue(current.update_attract(2.5))
        self.assertEqual(current.hooks.apply.call_count, 2)

    def test_a_held_right_on_background_applies_at_most_once_a_second(self):
        current = self.card()
        self.to(current, "theme")
        applied = []
        current.hooks.apply.side_effect = lambda name: applied.append(now)
        now = 1.0
        while now < 4.0:  # an OS repeat at 30 Hz for 3 s
            current.handle(Key("right"), now)
            current.update_attract(now)
            now = round(now + 1 / 30, 6)
        for _ in range(60):
            current.update_attract(now)
            now = round(now + 1 / 30, 6)
        self.assertEqual(len(applied), 1)
        self.assertTrue(all(b - a >= 1.0 for a, b in zip(applied, applied[1:])))

    def test_every_value_fits_the_field_and_every_note_row_19(self):
        for given in (frozenset(), frozenset(settings.option_name(key) for key in settings.ROWS)):
            current = self.card(given=given, keymap_file=None)
            _, painter = drawn(current)
            for row in settings.SETTINGS:
                for value in row.values:
                    settings.put(current.options, row.key, value)
                    text, locked, note = screens.shown(painter, current, row)
                    self.assertLessEqual(len(text), screens.FIELD, (row.key, value, text))
                    self.assertLessEqual(len(note), settings.DESCRIPTION_WIDTH, (row.key, value, note))
        self.assertLessEqual(len("COLOUR-BLIND SAFE (--palette)"), screens.FIELD)

    def test_esc_saves_once_and_q_never_leaves(self):
        current = self.card()
        self.to(current, "fps")
        current.handle(Key("right"), 1.0)
        current.handle(Key("q"), 2.0)  # the menu's AZERTY Left: it changes the value, it never leaves
        self.assertEqual((current.scene, current.options.fps), ("settings", 60))
        current.handle(Key("right"), 3.0)
        current.handle(Key("escape"), 4.0)
        self.assertEqual(current.scene, "menu")
        current.profile.update_settings.assert_called_once_with({"fps": 30}, name=None)

    def test_back_leaves_and_nothing_changed_writes_nothing(self):
        current = self.card()
        current.settings_card.cursor = screens.SettingsCard.ROWS - 1
        current.handle(Key("enter"), 1.0)
        self.assertEqual(current.scene, "menu")
        current.profile.update_settings.assert_not_called()

    def test_the_pilot_row_opens_the_initials_editor(self):
        current = self.card()
        current.settings_card.cursor = len(settings.SETTINGS)
        current.handle(Key("enter"), 1.0)
        self.assertEqual(current.scene, "initials")
        rows, _ = drawn(current)
        self.assertIn("YOUR INITIALS", rows)
        self.assertEqual(rows[23], "PILOT TEST")  # M types here: the footer drops the sound key
        for at, name in enumerate("tim", 2):
            current.handle(Key(name), float(at))
        current.handle(Key("enter"), 6.0)
        self.assertEqual((current.scene, current.options.name), ("settings", "TIM"))
        current.handle(Key("escape"), 7.0)
        current.profile.update_settings.assert_called_once_with({}, name="TIM")
        locked = self.card(given=frozenset({"name"}))
        locked.settings_card.cursor = len(settings.SETTINGS)
        locked.handle(Key("enter"), 1.0)
        self.assertEqual(locked.scene, "settings")  # --name holds the row for this run

    def test_the_pilot_row_names_the_key_that_opens_its_editor(self):
        """SC-5: the PILOT row's field and row 19 name the settings context's select key, which a keymap file that
        moves the menu's moves too; that key opens the editor."""
        current = self.card()
        current.settings_card.cursor = len(settings.SETTINGS)
        rows, _ = drawn(current)
        pilot = screens.FIRST_ROW + len(settings.SETTINGS)
        self.assertTrue(rows[pilot].endswith("Enter to change"))
        self.assertIn("Your initials on the high scores. Enter to change.", rows)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "keys.json"
            path.write_text(json.dumps({"preset": "default", "unbind": ["menu.select:enter"]}))
            current = self.card(keymap=Keymap.load(path))
        current.settings_card.cursor = len(settings.SETTINGS)
        screen = Screen()
        Painter(screen, current.options, MONO).draw(current)
        self.assertEqual(screen.row_text(pilot)[44:].rstrip(), "Space to change")
        self.assertIn("Your initials on the high scores. Space to change.", rows_of(screen))
        self.assertFalse(any("Enter" in row for row in rows_of(screen)))
        current.handle(Key("enter"), 1.0)
        self.assertEqual(current.scene, "settings")
        current.handle(Key("space"), 2.0)
        self.assertEqual(current.scene, "initials")

    def test_the_hint_row_names_every_way_out(self):
        for name in PRESETS:
            current = self.card(keys=name)
            rows, _ = drawn(current)
            back = current.keymap.keys_for("settings", "back")
            self.assertIn(f"{label(back[0])} save and go back", rows[21])


class CardTellsWhatTheScreenShowsTests(unittest.TestCase):
    """The settings card's PALETTE and BACKGROUND rows say what this terminal draws (spec 4.2: "a value this
    terminal can't honour shows what it gets"): ST-3, FX-7 and ST-4's wiring, on the terminal fixtures as the
    game resolves them (app.overrides, caps.resolve, theme.resolve_theme)."""

    def shown(self, fixture, row="palette", environ=None, applied=None, **values):
        """The row's (field, locked, row 19) for a session with these options on the fixture's terminal, its theme
        resolved for the palette applied (by default the session's own: the repaint has happened)."""
        current = session(quickstart=False, **{"color": "auto"} | values)  # the terminal's own tier unless given
        current.open_settings()
        doc = load(fixture)
        doc = {**doc, "env": {**doc["env"], **(environ or {})}}
        options = current.options
        caps = capabilities(
            doc, app.overrides(SimpleNamespace(**vars(options) | {"palette": applied or options.palette}))
        )
        theme = resolve_theme(caps, applied or options.palette)
        painter = Painter(Screen(), options, theme, caps)
        return screens.shown(painter, current, settings.ROWS[row])

    def test_the_palette_row_says_when_the_screen_has_no_colour(self):
        """ST-3: NO_COLOR over a palette nobody chose, --mono (or --color mono) and a terminal without colour."""
        today = settings.ROWS["palette"].describe("default")
        no_color = {"NO_COLOR": "1"}
        self.assertEqual(self.shown("ghostty"), ("DEFAULT", False, today))
        self.assertEqual(self.shown("ghostty", environ=no_color), ("DEFAULT (MONO)", False, settings.NO_COLOR_NOTE))
        cvd = settings.ROWS["palette"].describe("cvd")
        self.assertEqual(self.shown("ghostty", environ=no_color, palette="cvd"), ("COLOUR-BLIND SAFE", False, cvd))
        self.assertEqual(
            self.shown("ghostty", palette="cvd", color="mono"),
            ("COLOUR-BLIND SAFE (MONO)", False, settings.MONO_FLAG_NOTE),
        )
        self.assertEqual(self.shown("dumb"), ("DEFAULT (MONO)", False, settings.MONO_TERMINAL_NOTE))
        mono = settings.ROWS["palette"].describe("mono")
        self.assertEqual(self.shown("ghostty", palette="mono"), ("MONO", False, mono))
        self.assertEqual(
            self.shown("ghostty", palette="mono", color="256"), ("MONO (--color)", False, settings.MONO_COLOR_NOTE)
        )
        # a change waits for its repaint (spec 2.9): the field shows the name at once, and what it gets after
        self.assertEqual(self.shown("ghostty", environ=no_color, palette="default", applied="cvd")[0], "DEFAULT")
        self.assertEqual(
            self.shown("ghostty", palette="cvd", color="mono", given=frozenset({"palette"}))[:2],
            ("COLOUR-BLIND SAFE (--palette)", True),
        )  # a flag holds the row: it says so first

    def test_the_palette_row_says_when_16_colours_draw_it_as_default(self):
        """ST-3, spec 3.6: at 16 colours HIGH CONTRAST (dark and light) and COLOUR-BLIND SAFE on light draw
        DEFAULT's colours; COLOUR-BLIND SAFE on dark has its own."""
        sixteen = ("HIGH CONTRAST (16 COLOURS)", False, settings.SIXTEEN_NOTE)
        self.assertEqual(self.shown("linux-console", palette="contrast"), sixteen)
        self.assertEqual(self.shown("linux-console", palette="contrast", theme="light"), sixteen)
        self.assertEqual(self.shown("linux-console", palette="cvd", theme="light")[0], "COLOUR-BLIND SAFE (16 COLOURS)")
        self.assertEqual(self.shown("linux-console", palette="cvd")[0], "COLOUR-BLIND SAFE")
        self.assertEqual(self.shown("linux-console")[0], "DEFAULT")
        self.assertEqual(self.shown("xterm", palette="contrast")[0], "HIGH CONTRAST")  # 256 colours have its own
        row = settings.ROWS["palette"]  # every field these rows can show fits between < and > (the longest is 30)
        for suffix in (settings.MONO_SUFFIX, settings.SIXTEEN_SUFFIX):
            for value in row.values:
                self.assertLessEqual(len(row.label(value) + suffix), screens.FIELD, (value, suffix))

    def test_the_background_row_says_painted_only_where_contrast_paints(self):
        """FX-7: HIGH CONTRAST paints its own background in truecolor and 256 colours, not in 16 colours or mono;
        ST-4: with --theme terminal given the row says the flag and that it is painted anyway."""
        terminal = settings.ROWS["theme"].describe("terminal")
        painted = (settings.PAINTED, False, settings.PAINTED_NOTE)
        self.assertEqual(self.shown("ghostty", "theme", palette="contrast", theme="terminal"), painted)
        self.assertEqual(self.shown("xterm", "theme", palette="contrast", theme="terminal"), painted)
        plain = ("TERMINAL", False, terminal)
        self.assertEqual(self.shown("linux-console", "theme", palette="contrast", theme="terminal"), plain)
        self.assertEqual(self.shown("ghostty", "theme", palette="contrast", theme="terminal", color="mono"), plain)
        self.assertEqual(self.shown("ghostty", "theme", theme="terminal"), plain)
        given = frozenset({"theme"})
        self.assertEqual(
            self.shown("ghostty", "theme", palette="contrast", theme="terminal", given=given),
            (settings.PAINTED_LOCKED, True, settings.PAINTED_LOCKED_NOTE),
        )
        locked = ("TERMINAL (--theme)", True, "Set by --theme for this run. Saved: AUTO.")
        self.assertEqual(
            self.shown("linux-console", "theme", palette="contrast", theme="terminal", given=given), locked
        )
        self.assertEqual(self.shown("ghostty", "theme", theme="terminal", given=given), locked)

    def test_a_locked_row_names_the_flag_that_was_typed(self):
        """ST-4: --glyphs holds SCALE, and the row says --glyphs, the flag the player typed."""
        glyphs = {"scale": "1", "given": frozenset({"scale"}), "given_flags": {"scale": "--glyphs"}}
        self.assertEqual(
            self.shown("ghostty", "scale", **glyphs),
            ("TEXT (--glyphs)", True, "Set by --glyphs for this run. Saved: AUTO."),
        )
        self.assertEqual(
            self.shown("ghostty", "scale", scale="1", given=frozenset({"scale"})),
            ("TEXT (--scale)", True, "Set by --scale for this run. Saved: AUTO."),
        )


class InitialsTests(unittest.TestCase):
    """Phase 5 spec 6: the initials card after a placing run, typed or cycled, saved or skipped."""

    def placed(self, folder, **overrides):
        current = real_session(folder, **overrides)
        game_over(current, 1234)
        return current

    def test_a_placing_run_asks_for_initials_before_the_results(self):
        with tempfile.TemporaryDirectory() as folder:
            current = self.placed(folder)
            self.assertEqual(current.scene, "initials")
            card = current.initials_card
            self.assertEqual(("".join(card.slots), card.rank), ("TES", 1))
            rows, _ = drawn(current)
            for line in ("NEW HIGH SCORE", "#1   CAMPAIGN / NORMAL", "SCORE 1,234", "SAVES AS  TEST"):
                self.assertIn(line, rows)
            self.assertEqual(rows[23], "PILOT TEST")
            self.assertIn("Type or Up/Down  Left/Right move  Enter save  Esc skip", rows)

    def test_typed_letters_save_on_the_row_and_the_pilot(self):
        with tempfile.TemporaryDirectory() as folder:
            current = self.placed(folder)
            for at, name in ((1.0, "a"), (1.15, "n"), (1.3, "n")):  # a quick double letter types both
                current.handle(Key(name), at)
            self.assertEqual(current.initials_card.saves_as, "ANN")
            current.handle(Key("enter"), 2.0)
            self.assertEqual(current.scene, "results")
            self.assertEqual(current.options.name, "ANN")
            saved = json.loads((Path(folder) / "profile.json").read_text())
            self.assertEqual((saved["name"], saved["scores"][0]["name"]), ("ANN", "ANN"))

    def test_m_types_and_never_toggles_the_sound(self):
        with tempfile.TemporaryDirectory() as folder:
            current = self.placed(folder)
            current.handle(Key("m"), 1.0)
            current.audio.toggle.assert_not_called()
            self.assertEqual(current.initials_card.slots[0], "M")

    def test_a_held_letter_types_once_on_a_legacy_terminal(self):
        """Once the OS repeat delay is learned (any run teaches it: a held direction), a held letter types once."""
        with tempfile.TemporaryDirectory() as folder:
            current = real_session(folder)
            for start in (0.0, 3.0):
                current.handle(Key("d"), start)
                for n in range(20):
                    current.handle(Key("d"), start + 0.5 + n * 0.033)
            self.assertIsNotNone(current.router.gate.repeat.delay)
            current.now = 6.0
            game_over(current, 1234)
            current.handle(Key("x"), 8.0)
            now = 8.5
            while now < 9.5:  # the OS repeats the held X
                current.handle(Key("x"), now)
                now += 0.033
            self.assertEqual(current.initials_card.slots, ["X", "E", "S"])

    def test_a_key_held_from_the_game_over_types_nothing(self):
        for certain in (False, True):
            with self.subTest(kitty=certain), tempfile.TemporaryDirectory() as folder:
                current = real_session(folder)
                current.router.gate.set_certain(certain)
                current.handle(Key("a"), 0.0)
                for n in range(1, 10):
                    current.handle(Key("a", kind=REPEAT if certain else PRESS), 0.5 + n * 0.033)
                current.now = 0.84
                game_over(current, 1234)
                self.assertEqual(current.scene, "initials")
                for n in range(10, 30):
                    current.handle(Key("a", kind=REPEAT if certain else PRESS), 0.5 + n * 0.033)
                self.assertEqual(current.initials_card.slots, ["T", "E", "S"])
                if certain:
                    current.handle(Key("a", kind=RELEASE), 1.5)
                current.handle(Key("a"), 4.0)  # a fresh press types
                self.assertEqual(current.initials_card.slots[0], "A")

    def test_space_types_no_blank_however_fire_is_tapped(self):
        """SC-1, spec 6.2 and 10 (a typed letter or digit; the card binds no printable key): a player tapping fire
        as the last ship goes, and on, types nothing on a legacy terminal or kitty; B O T then saves BOT. The blank
        is reached with Up/Down."""
        for certain in (False, True):
            for gap in (0.15, 0.3):
                with self.subTest(kitty=certain, gap=gap), tempfile.TemporaryDirectory() as folder:
                    current = real_session(folder, name="PILOT")
                    current.router.gate.set_certain(certain)
                    at = 1.0
                    while at < 4.0:  # firing while playing
                        tap(current, "space", at, certain)
                        at = round(at + gap, 6)
                    game_over(current, 500, now=at)
                    for _ in range(6):  # the same rhythm on, the card's first second and more
                        at = round(at + gap, 6)
                        tap(current, "space", at, certain)
                    card = current.initials_card
                    self.assertEqual((current.scene, card.slots, card.cursor), ("initials", ["A", "A", "A"], 0))
                    for name in "bot":
                        at = round(at + 0.4, 6)
                        tap(current, name, at, certain)
                    self.assertEqual(card.saves_as, "BOT")
                    for n, name in enumerate(("left", "left", "down", "down"), 1):
                        current.handle(Key(name), at + n)
                    self.assertEqual((card.slots, card.saves_as), ([" ", "O", "T"], "OT"))  # B, A, then the blank

    def test_the_card_takes_no_key_in_its_first_half_second(self):
        """SC-1 (the orchestrator's decision): the last ship goes while the player moves (A, D, Right), fires (Up)
        or pauses (Esc): in the card's first 0.5 s no key types, cycles, moves or skips, on a legacy terminal or
        kitty. A fresh press after it acts. The PILOT row's editor, opened by its own key, takes keys at once."""
        for certain in (False, True):
            with self.subTest(kitty=certain), tempfile.TemporaryDirectory() as folder:
                current = real_session(folder)
                current.router.gate.set_certain(certain)
                game_over(current, 1234, now=10.0)
                for at, name in ((10.05, "a"), (10.15, "up"), (10.25, "d"), (10.32, "right"), (10.4, "escape")):
                    tap(current, name, at, certain)
                card = current.initials_card
                self.assertEqual((current.scene, card.slots, card.cursor), ("initials", ["T", "E", "S"], 0))
                tap(current, "z", 10.55, certain)
                self.assertEqual(card.slots, ["Z", "E", "S"])
                current.handle(Key("enter"), 12.0)
                self.assertEqual((current.scene, current.options.name), ("results", "ZES"))
        current = session(quickstart=False)
        current.open_settings()
        current.settings_card.cursor = len(settings.SETTINGS)
        current.handle(Key("enter"), 1.0)
        current.handle(Key("t"), 1.05)
        self.assertEqual((current.scene, current.initials_card.slots[0]), ("initials", "T"))

    def test_cycling_backspace_and_the_cursor(self):
        with tempfile.TemporaryDirectory() as folder:
            current = self.placed(folder)
            current.handle(Key("up"), 1.0)
            self.assertEqual(current.initials_card.slots[0], "U")
            current.handle(Key("down"), 2.0)
            current.handle(Key("down"), 3.0)
            self.assertEqual(current.initials_card.slots[0], "S")
            current.handle(Key("right"), 4.0)
            current.handle(Key("right"), 5.0)
            current.handle(Key("right"), 6.0)
            self.assertEqual(current.initials_card.cursor, 2)
            current.handle(Key("backspace"), 7.0)
            self.assertEqual((current.initials_card.cursor, current.initials_card.slots), (1, ["S", " ", "S"]))
            self.assertEqual(current.initials_card.saves_as, "SS")  # clean_name drops the blank
            for at, name in enumerate("xyz", 8):
                current.handle(Key(name), float(at))
            self.assertEqual(current.initials_card.slots, ["S", "X", "Z"])  # the last slot takes the last letter
            current.handle(Key("backspace"), 12.0)  # just typed the last slot: it clears it, the cursor stays
            self.assertEqual((current.initials_card.cursor, current.initials_card.slots[2]), (2, " "))

    def test_enter_on_the_unchanged_prefill_keeps_the_full_name_and_esc_skips(self):
        with tempfile.TemporaryDirectory() as folder:
            current = self.placed(folder, name="MAVERICK")
            self.assertEqual("".join(current.initials_card.slots), "MAV")
            current.handle(Key("enter"), 1.0)
            self.assertEqual((current.scene, current.options.name), ("results", "MAVERICK"))
            saved = json.loads((Path(folder) / "profile.json").read_text())
            self.assertEqual(saved["scores"][0]["name"], "MAVERICK")
        with tempfile.TemporaryDirectory() as folder:
            current = self.placed(folder)
            current.handle(Key("z"), 1.0)
            current.handle(Key("escape"), 2.0)
            self.assertEqual((current.scene, current.options.name), ("results", "TEST"))
            saved = json.loads((Path(folder) / "profile.json").read_text())
            self.assertEqual(saved["scores"][0]["name"], "TEST")

    def test_the_default_pilot_prefills_aaa(self):
        with tempfile.TemporaryDirectory() as folder:
            current = self.placed(folder, name="PILOT")
            self.assertEqual("".join(current.initials_card.slots), "AAA")

    def test_a_duel_asks_each_placing_pilot_in_turn_with_the_rank_read_again(self):
        with tempfile.TemporaryDirectory() as folder:
            current = real_session(folder, two_player=True)
            game_over(current, 100)
            self.assertEqual((current.scene, current.active), ("handoff", 1))
            game_over(current, 200)
            self.assertEqual(current.scene, "initials")
            card = current.initials_card
            self.assertEqual((card.pilot, card.rank, current.pilots[0].placing.rank), (0, 2, 1))  # pushed down
            current.handle(Key("enter"), 1.0)
            self.assertEqual(
                (current.scene, current.initials_card.pilot, current.initials_card.rank), ("initials", 1, 1)
            )
            for at, name in enumerate("bob", 2):
                current.handle(Key(name), float(at))
            current.handle(Key("enter"), 6.0)
            self.assertEqual(current.scene, "results")
            saved = json.loads((Path(folder) / "profile.json").read_text())
            self.assertEqual([row["name"] for row in saved["scores"]], ["BOB", "TEST-1"])
            self.assertEqual(saved["name"], "PILOT")  # a duel never changes the saved pilot name
            self.assertEqual(current.options.name, "TEST")

    def test_a_row_pushed_off_the_board_gets_no_card(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Profile(Path(folder) / "profile.json")
            game = Game(Config(seed=42))
            for score in range(1000, 1009):
                profile.record(game.result() | {"score": score}, Config(seed=42), "OLD")
            current = real_session(folder, two_player=True)
            game_over(current, 100)  # tenth
            game_over(current, 200)  # ninth: the first pilot's row is gone
            self.assertEqual(
                (current.scene, current.initials_card.pilot, current.initials_card.rank), ("initials", 1, 10)
            )

    def test_the_arcade_card_comes_after_the_1978_game_over(self):
        with tempfile.TemporaryDirectory() as folder:
            current = real_session(folder, mode="arcade")
            game_over(current)
            self.assertEqual(current.scene, "initials")
            rows, _ = drawn(current, 24, 80)
            self.assertIn("#1   ARCADE / 1978", rows)
            current.handle(Key("enter"), 1.0)
            self.assertEqual(current.scene, "results")
            self.assertFalse(is_card(current))  # Arcade's results stay a panel over the 1978 block

    def test_the_arcade_card_shows_in_arcade_s_smallest_window(self):
        """SC-4: Arcade plays from 56 x 32, so its initials card shows there (as wide as the window, every line
        whole, the slots centred) and takes keys, where the too-small notice asked for 60 x 24 before. A window that
        holds neither block shows the notice; a Campaign run's card still needs its 60 x 24."""
        with tempfile.TemporaryDirectory() as folder:
            current = real_session(folder, mode="arcade")
            current.on_resize(Fits(campaign=False, arcade=True))
            game_over(current)
            self.assertEqual((current.scene, small_notice(current)), ("initials", False))
            screen = Screen(32, 56)
            Painter(screen, current.options, MONO).draw(current)
            rows = [screen.row_text(y) for y in range(32)]
            self.assertFalse(any("Enlarge" in row for row in rows))
            hint = "Type or Up/Down  Left/Right move  Enter save  Esc skip"
            for line in ("NEW HIGH SCORE", "#1   ARCADE / 1978", "SAVES AS  TEST", hint):
                row = next(row for row in rows if line in row)
                self.assertEqual(row.index(line), (56 - len(line)) // 2, line)
            slots = next(row for row in rows if row.strip() == "T   E   S")
            self.assertEqual(slots.index("T"), (56 - 9) // 2)
            current.handle(Key("b"), 1.0)
            self.assertEqual(current.initials_card.slots, ["B", "E", "S"])
            current.on_resize(Fits(campaign=False, arcade=False))
            self.assertTrue(small_notice(current))
            current.handle(Key("c"), 2.0)
            self.assertEqual(current.initials_card.slots, ["B", "E", "S"])  # the card can't be seen: no typing
            rows, _ = drawn(current, 30, 50)
            self.assertIn("Enlarge the window: the game needs 60 x 24", rows)
            current.on_resize(Fits(campaign=True, arcade=False))  # 80 x 24 holds the card, not the 1978 block
            self.assertFalse(small_notice(current))
        with tempfile.TemporaryDirectory() as folder:
            current = real_session(folder)
            current.on_resize(Fits(campaign=False, arcade=True))
            game_over(current)
            self.assertEqual((current.scene, small_notice(current)), ("initials", True))

    def test_never_for_a_stub_or_mock_profile_a_replay_or_a_read_only_profile(self):
        current = session()
        game_over(current)
        self.assertEqual(current.scene, "results")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text('{"version": 3, "name": "FUTURE"}')
            current = real_session(folder)
            self.assertTrue(current.profile.read_only)
            game_over(current)
            self.assertEqual(current.scene, "results")


class ResultsCardTests(unittest.TestCase):
    """Phase 5 spec 8: what ended the run in words, the rank and best, and the longest values at 60 x 24."""

    def card(self, game, placing=None, unlocked=(), **overrides):
        current = session(**overrides)
        current.pilots[0].game = game
        current.pilots[0].placing, current.pilots[0].unlocked = placing, tuple(unlocked)
        current.profile.rank = lambda found: found.rank
        current.scene = "results"
        screen = Screen()
        with no_field():
            Painter(screen, current.options, MONO).draw(current)
        return rows_of(screen)

    def ended(self, cause, wave=5, mode="classic"):
        game = staged(Config("normal", mode, 1978), wave, phase="playing")
        game.phase, game.hit_cause = "game_over", cause
        return game

    def test_every_cause_in_words(self):
        boss = staged(Config("normal", "classic", 1978), 6, phase="playing")
        boss.phase, boss.hit_cause = "game_over", "pellet boss"
        for game, line in (
            (self.ended("pellet"), "GAME OVER - SHOT DOWN BY A PELLET"),
            (self.ended("cluster"), "GAME OVER - SHOT DOWN BY A CLUSTER"),
            (boss, "GAME OVER - THE WARDEN'S PELLET"),
            (self.ended("beam"), "GAME OVER - CAUGHT IN A BEAM"),
            (self.ended("diver"), "GAME OVER - RAMMED BY A DIVER"),
            (self.ended("landing"), "GAME OVER - THE FLEET LANDED"),
        ):
            with self.subTest(cause=game.hit_cause):
                rows = self.card(game)
                self.assertEqual(rows[1:3], ["SIGNAL LOST", line])
        won = staged(Config("normal", "classic", 1978), 24, phase="playing")
        won.phase = "victory"
        self.assertEqual(self.card(won)[1:3], ["EARTH SAVED", "ALL 24 WAVES CLEARED"])

    def test_rank_new_best_and_best_before(self):
        game = self.ended("pellet")
        game.score = 12430
        board = ("campaign-2", "classic", "normal", None)
        rows = self.card(game, Placing(1, board, "2026-01-01", 9870, 12430))
        self.assertIn("SCORE        12,430    NEW BEST", rows)
        self.assertIn("RANK         #1 ON CAMPAIGN / NORMAL", rows)
        self.assertIn("BEST BEFORE  9,870", rows)
        rows = self.card(game, Placing(4, board, "2026-01-01", 20000, 12430))
        self.assertIn("SCORE        12,430", rows)
        self.assertIn("RANK         #4 ON CAMPAIGN / NORMAL", rows)
        rows = self.card(game)  # no placing: no rank, no best
        self.assertFalse([row for row in rows if row.startswith(("RANK", "BEST BEFORE"))])

    def test_daily_shows_its_date_and_endless_its_modifiers(self):
        game = staged(Config("normal", "daily", 20261022), 3, phase="playing")
        game.phase, game.hit_cause = "game_over", "pellet"
        rows = self.card(game, mode="daily")
        self.assertEqual(rows[2], "GAME OVER - SHOT DOWN BY A PELLET   DAILY 2026-10-22")
        game = staged(Config("normal", "endless", 1978), 40, phase="playing")
        game.phase, game.hit_cause = "game_over", "pellet"
        game.modifiers = ["QUICK RELOAD", "ARMOURED"]
        rows = self.card(game, mode="endless")
        self.assertIn("MODIFIERS    QUICK RELOAD, ARMOURED", rows)
        self.assertTrue(any(row.startswith("REACHED      WAVE 040") for row in rows))

    def test_the_longest_values_fit_60_columns(self):
        names = sorted(UPGRADES, key=len, reverse=True)
        picks = [name for name in names if UPGRADES[name].stacks == 1][:5]
        stacked = [name for name in names if UPGRADES[name].stacks > 1][:2]
        game = self.ended("cluster boss", wave=max(range(1, 25), key=lambda n: len(staged(NORMAL, n).wave_name)))
        game.score = 9_999_999
        game.upgrades = {name: 1 for name in picks} | {name: UPGRADES[name].stacks for name in stacked}
        game.continues_used = 2
        achievements = sorted((name for name, _ in ACHIEVEMENTS.values()), key=len, reverse=True)
        board = ("campaign-2", "classic", "normal", None)
        rows = self.card(game, Placing(10, board, "2026-01-01", 9_999_998, 9_999_999), achievements[:3])
        text = "\n".join(rows)
        self.assertIn("SCORE        9,999,999    NEW BEST", rows)
        self.assertIn("RANK         #10 ON CAMPAIGN / NORMAL", rows)
        self.assertIn(f'"{game.wave_name}"', text)
        for name in [*picks, *(f"{name} x{UPGRADES[name].stacks}" for name in stacked)]:
            self.assertIn(name, text)  # never split over two rows, never cut
        self.assertIn(f"UNLOCKED     {achievements[0]}, {achievements[1]}, +1 more", rows)
        self.assertIn("CONTINUED    twice", rows)
        self.assertEqual(rows[21], "Enter fly again   Q menu")
        self.assertEqual(rows[19], "SEED 1978")

    def test_a_duel_has_a_column_per_pilot(self):
        current = session(two_player=True)
        for pilot, score in zip(current.pilots, (300, 500)):
            pilot.game.score, pilot.game.phase, pilot.game.hit_cause = score, "game_over", "dart"
        current.scene = "results"
        rows, _ = drawn(current)
        self.assertEqual(rows[1], "TEST-2 WINS")
        self.assertIn("TEST-1                      TEST-2", rows)
        self.assertIn("SCORE 300                   SCORE 500", rows)
        self.assertIn("SHOT DOWN BY A DART         SHOT DOWN BY A DART", rows)
        self.assertEqual(rows[21], "Enter rematch   Q menu")

    def test_a_replay_says_verified_or_mismatch_with_no_best_and_its_own_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flight.json"
            original, recorder = Game(Config(seed=9)), Recorder(Config(seed=9))
            for _ in range(7):
                recorder.append(("left",))
                original.step(("left",))
            recorder.save(path, original)
            current = session()
            current.replay = Playback(path)
            current.start()
            for _ in range(7):
                current.step(0)
        rows, _ = drawn(current, 24, 80)
        self.assertEqual((rows[1], rows[2], rows[21]), ("REPLAY VERIFIED", "THE TAPE ENDS HERE", "Q / Escape to exit"))
        self.assertIn("SCORE        0", rows)
        self.assertFalse([row for row in rows if "NEW BEST" in row or row.startswith(("RANK", "BEST BEFORE"))])
        current.replay_verified = False
        self.assertEqual(drawn(current, 24, 80)[0][1], "REPLAY MISMATCH")

    def test_the_play_hint_row_is_not_drawn_under_the_card(self):
        current = session()
        game_over(current)
        rows, _ = drawn(current, 24, 80)
        self.assertNotIn(HINTS["latched"], rows)


class SmallNoticeTests(unittest.TestCase):
    """Phase 5 spec 9 F2: the centred Campaign too-small notice takes only the global keys and the way back."""

    def test_the_title_at_50_x_20(self):
        current = session(quickstart=False)
        current.on_resize(Fits(False, False))
        self.assertTrue(small_notice(current))
        rows, _ = drawn(current, 20, 50)
        at = rows.index("Enlarge the window: the game needs 60 x 24")
        self.assertEqual(rows[at + 1 : at + 3], ["Now 50 x 20", "Esc Esc quits"])  # no game: no Paused
        for at, name in enumerate(("enter", "down", "up", "right")):
            current.handle(Key(name), float(at))
        self.assertEqual((current.scene, current.selection, current.mode), ("menu", 0, "classic"))
        self.assertEqual(current.router.gate.last.keys() >= {"enter", "down"}, True)  # the gate saw them
        current.handle(Key("m"), 5.0)
        current.audio.toggle.assert_called_once()  # global keys still act
        current.handle(Key("escape"), 6.0)
        current.handle(Key("escape"), 8.0)
        self.assertTrue(current.quit)

    def test_after_a_resize_the_menu_is_as_it_was(self):
        current = session(quickstart=False)
        current.selection = 3
        current.on_resize(Fits(False, False))
        current.handle(Key("enter"), 1.0)
        current.on_resize(Fits(True, False))
        self.assertEqual((current.scene, current.selection, current.pilot), ("menu", 3, None))

    def test_arcade_highlighted_at_80_x_24_never_locks_the_menu(self):
        current = session(quickstart=False, mode="arcade")
        current.on_resize(Fits(True, False))
        self.assertFalse(small_notice(current))
        current.handle(Key("down"), 1.0)
        current.handle(Key("up"), 2.0)
        current.handle(Key("down"), 3.0)
        self.assertEqual(current.selection, 1)

    def test_arcade_keeps_its_own_notice_and_keys(self):
        current = session(quickstart=False, mode="arcade")
        current.room = Fits(campaign=True, arcade=False)
        current.start()
        self.assertEqual((current.scene, current.pause_reason, small_notice(current)), ("paused", "small", False))
        lines = [row.strip() for row in drawn(current, 24, 80)[0] if row.strip()]
        self.assertEqual(lines, ["ARCADE NEEDS 56 x 32", "Current: 80 x 24 | Paused", "Enlarge the window or zoom out"])
        current.handle(Key("p"), 1.0)  # Phase 4's way out of it, unchanged: P, then Q
        current.handle(Key("q"), 2.0)
        self.assertEqual(current.scene, "menu")

    def test_every_screen_names_its_way_back(self):
        cases = (
            ("help", "Enter goes back"),  # the first key of pages.back
            ("records", "Enter goes back"),
            ("settings", "Esc goes back"),
            ("initials", "Esc skips"),
        )
        for scene, way in cases:
            current = session(quickstart=False)
            current.change_scene(scene)
            current.on_resize(Fits(False, False))
            rows, _ = drawn(current, 20, 50)
            self.assertIn(way, rows, scene)
            current.handle(Key("escape"), 1.0)
            self.assertEqual(current.scene, "menu" if scene != "initials" else "settings", scene)
        current = session()
        current.on_resize(Fits(False, False))
        rows, _ = drawn(current, 20, 50)
        self.assertIn("Now 50 x 20 | Paused", rows)
        self.assertIn("Q leaves the run", rows)
        current.handle(Key("q"), 1.0)
        self.assertEqual(current.scene, "menu")
        current = session()
        game_over(current)
        current.on_resize(Fits(False, False))
        rows, _ = drawn(current, 20, 50)
        self.assertIn("Q goes to the menu", rows)
        self.assertNotIn("Now 50 x 20 | Paused", rows)

    def test_a_long_message_wraps_and_keeps_the_size_together(self):
        current = session(quickstart=False)
        current.on_resize(Fits(False, False))
        rows, _ = drawn(current, 12, 30)
        text = [row for row in rows if row]
        self.assertEqual(text[:2], ["Enlarge the window: the game", "needs 60 x 24"])


if __name__ == "__main__":
    unittest.main()
