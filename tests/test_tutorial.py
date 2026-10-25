"""The first-run tips (Phase 5 spec 7): when a run gets the Coach, each tip's trigger and done rule, the scheduler,
the bottom row's order (notices > timers > tip > codes), glyph claims, and every text's width."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from terminal_invaders import tutorial
from terminal_invaders.campaign.tables import ENEMIES
from terminal_invaders.campaign.testing import staged
from terminal_invaders.cli import defaults
from terminal_invaders.keymap import PRESETS, Keymap
from terminal_invaders.model import TICK_RATE, Config
from terminal_invaders.replay import Playback
from terminal_invaders.sprites import TELLS
from terminal_invaders.storage import Profile
from terminal_invaders.term.cells import CellBuffer
from terminal_invaders.theme import SLOT, resolve_theme
from terminal_invaders.tutorial import GAP_TICKS, MIN_TICKS, TIPS, WIDTH, Coach, View, wanted
from terminal_invaders.ui import Painter, Session
from tests.support import frames

MONO = resolve_theme(SimpleNamespace(color="mono", theme="dark", paint_background=False))
SCHEMES = ("hold", "latched", "timed")
GLYPHS = set("{}'^[]#$")  # every arena glyph a k = 1 text may name


class FakeGame:
    """What the Coach reads of a game, with nothing on the field."""

    def __init__(self, **values):
        self.player = SimpleNamespace(guard=0, invulnerable=0, lives=3, bombs=0, weapon=2)
        self.shots, self.pickups, self.aliens, self.hazards = [], [], [], []
        self.bunkers, self.upgrades, self.powerups = {}, {}, {}
        self.notes = []
        self.shelter_rev = self.bomb_charge = self.tick = 0
        self.wave, self.phase = 1, "playing"
        self.config = Config()
        vars(self).update(values)


def coach(*taught, keymap=None, **options):
    """A Coach with the named tips left to teach (all of them when none are named)."""
    made = Coach(keymap or Keymap.preset("default"), SimpleNamespace(**({"reduced_motion": False} | options)))
    if taught:
        made.done |= set(TIPS) - set(taught)
    return made


def run(teacher, game, ticks, actions=(), events=()):
    for _ in range(ticks):
        game.tick += 1
        teacher.observe(game, actions, tuple(events))


def shot(friendly=False, volley=0):
    return SimpleNamespace(friendly=friendly, volley=volley, kind="pellet")


def tell(kind):
    """An enemy of that kind in its telegraph state, on the field."""
    return SimpleNamespace(kind=kind, state="telegraph", cargo=False, y=5.0)


class WhoGetsTheCoachTests(unittest.TestCase):
    def test_wanted(self):
        fresh, learned = SimpleNamespace(settings={}), SimpleNamespace(settings={"tips_done": True})
        classic = Config()
        for tips, profile, pilots, config, expected in (
            ("auto", fresh, 1, classic, True),
            ("auto", learned, 1, classic, False),
            ("always", learned, 1, classic, True),
            ("off", fresh, 1, classic, False),
            ("auto", SimpleNamespace(), 1, classic, False),  # a stub without settings, as frames.py's
            ("auto", Mock(), 1, classic, False),  # a Mock's settings is no dict
            ("auto", fresh, 2, classic, False),  # a duel
            ("auto", fresh, 1, Config(mode="arcade"), False),
            ("auto", fresh, 1, Config(mode="endless"), True),
            ("auto", fresh, 1, Config(mode="daily"), True),
            ("always", fresh, 1, Config(mode="arcade"), False),
        ):
            with self.subTest(tips=tips, pilots=pilots, mode=config.mode):
                self.assertIs(wanted(SimpleNamespace(tips=tips), profile, pilots, config), expected)
        self.assertFalse(wanted(SimpleNamespace(), fresh, 1, classic))  # options without the attribute

    def session(self, folder=None, enable=True, **overrides):
        values = dict(seed=42, fixed_seed=True, quickstart=False, name="TEST", color="mono") | overrides
        profile = Profile(Path(folder) / "profile.json") if folder else Mock(alert=None)
        current = Session(defaults(**values), profile, Mock(enabled=False))
        if enable:
            current.enable_tips()
        return current

    def test_only_the_real_terminal_path_with_a_fresh_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            current = self.session(folder, enable=False)
            current.start()
            self.assertIsNone(current.coach)  # cli.defaults() Sessions, frames, bench_render, balance.py
            current = self.session(folder)
            current.start()
            self.assertIsInstance(current.coach, Coach)
            for overrides in (dict(two_player=True), dict(mode="arcade"), dict(tips="off")):
                current = self.session(folder, **overrides)
                current.start()
                self.assertIsNone(current.coach, overrides)
        current = self.session(tips="always")  # a Mock profile: only TIPS always teaches it
        current.start()
        self.assertIsNotNone(current.coach)
        self.assertIsNone(frames.session().coach)

    def test_never_in_a_replay_or_the_demo(self):
        tape = Playback(frames.TAPE)
        current = self.session(tips="always")
        current.replay = tape
        current.start()
        self.assertIsNone(current.coach)
        current = self.session(tips="always")
        current.demo = True
        current.start()
        self.assertIsNone(current.coach)

    def test_a_quickstart_run_gets_the_coach_once_the_terminal_path_enables_it(self):
        with tempfile.TemporaryDirectory() as folder:
            current = self.session(folder, enable=False, quickstart=True)  # started in __init__, before app.play
            self.assertIsNone(current.coach)
            current.enable_tips()
            self.assertIsInstance(current.coach, Coach)
            taught = current.coach
            current.enable_tips()  # once attached, it stays the run's
            self.assertIs(current.coach, taught)

    def test_tips_done_is_written_once_after_the_first_recorded_run(self):
        with tempfile.TemporaryDirectory() as folder:
            current = self.session(folder)
            current.start()
            game = current.pilot.game
            game.phase = "game_over"
            current.step(0)
            saved = json.loads((Path(folder) / "profile.json").read_text())
            self.assertIs(saved["settings"]["tips_done"], True)
            current.command("menu", 1)
            current.start()
            self.assertIsNone(current.coach)  # learned: the next run has none
            again = self.session(folder)
            again.start()
            self.assertIsNone(again.coach)  # nor a second launch

    def test_a_run_quit_to_the_menu_keeps_the_coach_and_what_it_taught(self):
        with tempfile.TemporaryDirectory() as folder:
            current = self.session(folder)
            current.start()
            first = current.coach
            first.done.add("move")
            current.command("menu", 1)
            current.start()
            self.assertIs(current.coach, first)
            self.assertIn("move", current.coach.done)
            self.assertNotIn("tips_done", Profile(Path(folder) / "profile.json").settings)

    def test_tips_always_teaches_every_run_from_the_start(self):
        current = self.session(tips="always")
        current.start()
        current.coach.done.add("move")
        current.start()
        self.assertEqual(current.coach.done, set())

    def test_the_coach_never_writes_the_game(self):
        game = staged(Config(), 3, phase="playing")
        teacher = coach()
        before = copy.deepcopy(game)
        for _ in range(300):
            game.step(("fire", "left"))
            teacher.observe(game, ("fire", "left"), tuple(game.events))
        twin = before
        for _ in range(300):
            twin.step(("fire", "left"))
        self.assertEqual(game.digest(), twin.digest())


class TriggerTests(unittest.TestCase):
    """Spec 7.3: each tip's trigger, and its done rule."""

    def test_move_first_then_fire_then_hud(self):
        teacher, game = coach(), FakeGame()
        run(teacher, game, 1)
        self.assertEqual((teacher.active, teacher.visible), ("move", True))
        run(teacher, game, 30, actions=("left",))
        self.assertEqual(teacher.active, "move")
        run(teacher, game, 1, actions=("right",))  # both ways: done once it had its 2 s (spec 7.2)
        self.assertEqual((teacher.active, "move" in teacher.done), ("move", False))
        run(teacher, game, MIN_TICKS - teacher.shown)
        self.assertEqual(teacher.active, "move")
        run(teacher, game, 1)
        self.assertIn("move", teacher.done)
        run(teacher, game, GAP_TICKS - 1)
        self.assertEqual(teacher.active, None)  # the row stays empty 1 s, the tick Move ended included
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "fire")
        run(teacher, game, 3, events=("fire",))
        run(teacher, game, MIN_TICKS - teacher.shown + 1)
        self.assertIn("fire", teacher.done)
        run(teacher, game, GAP_TICKS)
        self.assertEqual(teacher.active, "hud")  # wave 1, after Fire

    def test_a_tip_learned_before_it_shows_never_shows(self):
        teacher, game = coach("move", "fire", "pause"), FakeGame()
        run(teacher, game, 1, actions=("left", "right"))
        self.assertIn("move", teacher.done)
        teacher.on_pause()
        self.assertIn("pause", teacher.done)

    def test_each_tip_runs_out_after_its_seconds(self):
        for name, setup in (
            ("telegraph", lambda game: game.aliens.append(tell("gunner"))),
            ("hud", lambda game: None),
            ("pause", lambda game: setattr(game, "phase", "playing")),
        ):
            with self.subTest(tip=name):
                teacher, game = coach(name), FakeGame()
                run(teacher, game, 1)  # the run's first tick starts its counts
                teacher.cleared = name == "pause"
                setup(game)
                if name == "hud":
                    teacher.done.add("fire")
                run(teacher, game, 1)
                self.assertEqual(teacher.active, name)
                run(teacher, game, tutorial.SECONDS[name] * TICK_RATE)
                self.assertIn(name, teacher.done)

    def test_a_tip_done_in_its_first_2_s_stays_until_they_are_up(self):
        """SC-3, spec 7.2 ("A tip shows at least 2 s"): three shots in the Fire tip's first ticks leave it on the row
        until its 2 s are up, then it is done; done before it was ever drawn, it is done unseen."""
        teacher, game = coach("fire"), FakeGame()
        run(teacher, game, 1)
        self.assertEqual((teacher.active, teacher.visible), ("fire", True))
        run(teacher, game, 10, events=("fire",))  # a held Space: three shots by the tip's 11th tick
        self.assertEqual(teacher.tip("latched", 1, WIDTH), "TIP  Space fires. Hold it to keep firing")
        run(teacher, game, MIN_TICKS - teacher.shown)
        self.assertEqual((teacher.active, teacher.shown), ("fire", MIN_TICKS))
        run(teacher, game, 1)
        self.assertEqual((teacher.active, "fire" in teacher.done), (None, True))
        teacher, game = coach("fire"), FakeGame(powerups={"rapid": 600})  # a pickup timer has the row
        run(teacher, game, 1)
        self.assertEqual((teacher.active, teacher.visible), ("fire", False))
        run(teacher, game, 3, events=("fire",))
        self.assertEqual((teacher.active, "fire" in teacher.done), (None, True))  # learned unseen: never shown

    def test_a_tell_over_before_its_tip_could_show_is_not_shown_late(self):
        """SC-2, spec 7.2 ("not shown late: it re-arms"): a Telegraph tip picked while a pickup timer has the row
        waits; the tell ends first, so when the row frees no tip names an invader that is not changing shape, and
        the next tell gets the tip."""
        teacher, alien = coach("telegraph"), SimpleNamespace(kind="gunner", state="telegraph", cargo=False, y=5.0)
        game = FakeGame(aliens=[alien], powerups={"rapid": 300})
        run(teacher, game, 20)  # a tell lasts about a third of a second
        self.assertEqual((teacher.active, teacher.visible), ("telegraph", False))
        alien.state = "formation"
        run(teacher, game, 60)
        game.powerups = {}
        run(teacher, game, 1)
        self.assertIsNone(teacher.tip("latched", 1, WIDTH))
        self.assertEqual((teacher.active, "telegraph" in teacher.done), (None, False))
        alien.state = "telegraph"
        run(teacher, game, 1)
        self.assertEqual((teacher.active, teacher.visible), ("telegraph", True))
        self.assertEqual(teacher.tip("latched", 1, WIDTH), "TIP  An invader that changes shape is about to fire")

    def test_only_a_shooter_about_to_fire_is_a_tell(self):
        """G4: the Telegraph tip says an invader that changes shape is about to fire. Every kind that fires has a tell
        pose; a diver or a mite in the same telegraph state is wobbling before a dive, and it fires nothing."""
        for kind, enemy in ENEMIES.items():
            with self.subTest(kind=kind):
                teacher, game = coach("telegraph"), FakeGame(aliens=[tell(kind)])
                run(teacher, game, 1)
                self.assertEqual(teacher.active, "telegraph" if enemy.shot else None)
                self.assertEqual(kind in TELLS, bool(enemy.shot))

    def test_no_telegraph_tip_for_a_diving_wobble_then_one_at_the_first_shot_tell(self):
        """G4 in a real game: wave 3's scouts fire pellets, which come with no tell, and its divers wobble before they
        dive; wave 5's gunners are the first shooters with a tell. The tip takes the row the tick the first one shows
        its tell pose."""
        for wave, kind in ((3, "diver"), (5, "gunner")):
            with self.subTest(wave=wave):
                game, teacher = staged(Config(seed=1978), wave, phase="playing", immortal=True), coach("telegraph")
                wobbled = False
                while not any(alien.kind == kind and alien.state == "telegraph" for alien in game.aliens):
                    self.assertLess(game.tick, 3000)
                    self.assertIsNone(teacher.active)
                    actions = frames.sweep(game.tick)
                    game.step(actions)
                    teacher.observe(game, actions, tuple(game.events))
                    wobbled |= any(alien.kind == "diver" and alien.state == "telegraph" for alien in game.aliens)
                self.assertTrue(wobbled)  # wave 5 has divers too: they wobble before the first gunner's tell
                self.assertEqual(teacher.active, None if kind == "diver" else "telegraph")
                if kind == "gunner":
                    self.assertTrue(teacher.visible)

    def test_shield_while_the_training_shield_is_up(self):
        teacher, game = coach("shield"), FakeGame()
        run(teacher, game, 1)
        self.assertIsNone(teacher.active)
        game.player.guard = 1
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "shield")
        run(teacher, game, MIN_TICKS)
        game.player.guard = 0  # the shield broke: it had its 2 s, so it is done
        run(teacher, game, 1)
        self.assertIn("shield", teacher.done)

    def test_a_subject_that_goes_before_2_s_re_arms(self):
        teacher, game = coach("pickup"), FakeGame()
        game.pickups = [SimpleNamespace(kind="rapid")]
        run(teacher, game, 30)
        self.assertEqual(teacher.active, "pickup")
        game.pickups = []
        run(teacher, game, 1)
        self.assertEqual((teacher.active, "pickup" in teacher.done), (None, False))
        run(teacher, game, GAP_TICKS)
        game.pickups = [SimpleNamespace(kind="rapid")]
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "pickup")
        run(teacher, game, 1, events=("pickup",))  # caught, another still falling: done once it had its 2 s
        self.assertNotIn("pickup", teacher.done)
        run(teacher, game, MIN_TICKS)
        self.assertIn("pickup", teacher.done)

    def test_ships_after_the_first_hit_and_bomb_when_the_air_is_busy(self):
        teacher, game = coach("ships", "bomb"), FakeGame()
        run(teacher, game, 1, events=("damage",))
        self.assertEqual(teacher.active, "ships")
        teacher, game = coach("bomb"), FakeGame()
        game.player.bombs = 1
        game.shots = [shot() for _ in range(3)]
        run(teacher, game, 1)
        self.assertIsNone(teacher.active)
        game.shots.append(shot())
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "bomb")
        run(teacher, game, 1, events=("bomb",))
        run(teacher, game, MIN_TICKS)  # a tip done in its first 2 s stays until they are up (spec 7.2)
        self.assertIn("bomb", teacher.done)

    def test_shelters_when_a_shot_strikes_one_not_on_a_repair(self):
        teacher, game = coach("shelters"), FakeGame(bunkers={(10, 16): 2})
        run(teacher, game, 1)
        game.shelter_rev += 1
        run(teacher, game, 1, events=("pickup",))  # a REPAIR rebuilds: no strike
        self.assertIsNone(teacher.active)
        game.shelter_rev += 1
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "shelters")

    def test_hud_when_the_gun_reads_empty_and_meter_once_the_charge_fills(self):
        teacher, game = coach("hud", "meter"), FakeGame(wave=3)
        run(teacher, game, 1)
        self.assertIsNone(teacher.active)
        game.shots = [shot(True, 0), shot(True, 1)]
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "hud")
        game.shots = []
        run(teacher, game, tutorial.SECONDS["hud"] * TICK_RATE + GAP_TICKS)
        game.bomb_charge = 1
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "meter")

    def test_pause_after_the_first_clear_or_45_s_and_done_when_p_is_pressed(self):
        teacher, game = coach("pause"), FakeGame()
        run(teacher, game, tutorial.PAUSE_AFTER - 1)
        self.assertIsNone(teacher.active)
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "pause")
        teacher.on_pause()
        self.assertIn("pause", teacher.done)
        teacher, game = coach("pause"), FakeGame()
        run(teacher, game, 1, events=("clear",))
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "pause")

    def test_cargo_while_a_cargo_ship_is_drawn_marked(self):
        teacher, game = coach("cargo"), FakeGame()
        game.aliens = [SimpleNamespace(state="telegraph", cargo=True, y=5.0)]  # its tell pose hides the $
        run(teacher, game, 1)
        self.assertIsNone(teacher.active)
        game.aliens[0].state = "formation"
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "cargo")

    def test_no_cargo_tip_while_the_cargo_ship_is_still_above_the_field(self):
        teacher, game = coach("cargo"), FakeGame()
        game.aliens = [SimpleNamespace(state="entering", cargo=True, y=-5.75)]  # in the game, not yet on screen
        run(teacher, game, 1)
        self.assertIsNone(teacher.active)
        game.aliens[0].y = -0.25  # its row is the field's first
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "cargo")


class SchedulerTests(unittest.TestCase):
    """Spec 7.2: one at a time, most urgent first, 2 s at least and 1 s apart, held outside wave play."""

    def test_the_most_urgent_goes_first(self):
        teacher, game = coach(), FakeGame()
        game.aliens = [SimpleNamespace(kind="gunner", state="telegraph", cargo=False)]
        game.player.guard = 1
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "telegraph")
        self.assertEqual(TIPS[:3], ("telegraph", "marks", "shield"))
        self.assertEqual(TIPS[-2:], ("move", "fire"))

    def test_move_yields_to_a_hazard_tip_after_its_2_s_and_comes_back(self):
        teacher, game = coach(), FakeGame()
        run(teacher, game, 30)
        game.aliens = [SimpleNamespace(kind="gunner", state="telegraph", cargo=False)]
        run(teacher, game, 1)
        self.assertEqual(teacher.active, "move")  # under 2 s: it keeps the row
        run(teacher, game, MIN_TICKS)
        self.assertEqual(teacher.active, "telegraph")  # at once: a tell lasts a fraction of a second
        game.aliens = []
        run(teacher, game, tutorial.SECONDS["telegraph"] * TICK_RATE + GAP_TICKS + 1)
        self.assertEqual(teacher.active, "move")
        self.assertGreaterEqual(teacher.shown, MIN_TICKS)  # its time so far is kept

    def test_tips_hold_outside_wave_play(self):
        teacher, game = coach(), FakeGame()
        run(teacher, game, 10)
        shown = teacher.shown
        for phase in ("wave", "clear", "upgrade", "continue"):
            game.phase = phase
            run(teacher, game, 30)
            self.assertEqual((teacher.shown, teacher.visible), (shown, False), phase)
        game.phase = "playing"
        run(teacher, game, 1)
        self.assertEqual((teacher.active, teacher.shown), ("move", shown + 1))

    def test_a_tip_that_cannot_be_drawn_waits_with_its_time_stopped(self):
        teacher, game = coach(), FakeGame()
        run(teacher, game, 10)
        game.notes = [("LAST STAND", 30)]
        run(teacher, game, 30)
        self.assertEqual((teacher.shown, teacher.visible), (10, False))
        game.notes, game.powerups = [], {"rapid": 300}
        run(teacher, game, 30)
        self.assertEqual(teacher.shown, 10)
        game.powerups = {}
        teacher.tip("latched", 1, 20)  # a frame with only 20 free columns: the line doesn't fit
        run(teacher, game, 30)
        self.assertEqual(teacher.shown, 10)
        teacher.tip("latched", 1, 60)
        run(teacher, game, 1)
        self.assertEqual(teacher.shown, 11)

    def test_a_new_run_starts_the_counts_again_but_keeps_what_was_taught(self):
        teacher, first = coach(), FakeGame()
        run(teacher, first, 1, actions=("left", "right"))
        self.assertIn("move", teacher.done)
        game = FakeGame()
        run(teacher, game, 1)
        self.assertEqual(teacher.moved, set())
        self.assertNotEqual(teacher.active, "move")

    def test_each_start_is_a_new_run_for_the_coach(self):
        current = Session(defaults(seed=1, fixed_seed=True, tips="always"), Mock(alert=None), Mock(enabled=False))
        current.enable_tips()
        current.start()
        current.coach.moved.add("left")
        current.coach.run = id(current.pilot.game)
        current.start()
        self.assertIsNone(current.coach.run)  # whatever id the next game has


class RowTests(unittest.TestCase):
    """Spec 7.2: the tip takes the bottom row only while the game leaves it free; the codes follow it."""

    def frame(self, game, columns=80, teacher=None, **overrides):
        current = frames.session(**overrides)
        current.coach = teacher or coach("move")
        frames.playing(current, game)
        run(current.coach, game, 30) if teacher is None else None
        buf = CellBuffer(columns, 24)
        Painter(buf, current.options, MONO).draw(current)
        return buf.row_text(23)[(columns - 60) // 2 :].rstrip(), buf, current

    def game(self, **values):
        game = staged(Config(seed=1978), 2, phase="playing")
        for name, value in values.items():
            setattr(game, name, value)
        return game

    def test_the_tip_then_the_codes(self):
        row, buf, current = self.frame(self.game(upgrades={"TRIPLE SHOT": 1, "RAIL ROUNDS": 2}))
        self.assertEqual(row, "TIP  Tap A or D to start; tap back or S to stop | TS RR2")
        self.assertEqual(buf.row_text(22).strip(), "Tap A/D: move, S: stop  Space Fire  B Bomb  P Pause")
        coloured = CellBuffer(80, 24)
        Painter(coloured, current.options, frames.theme(("truecolor", "dark"))).draw(current)
        left = (80 - 60) // 2
        self.assertEqual([coloured.styles[23 * 80 + left + x] & 255 for x in (0, 1, 2)], [SLOT["accent"]] * 3)
        self.assertEqual(coloured.styles[23 * 80 + left + 5] & 255, SLOT["text"])

    def test_notices_and_timers_win_the_row(self):
        for values, shown in (
            (dict(notes=[("LAST STAND", 40)]), "LAST STAND"),
            (dict(powerups={"rapid": 300}), "RAPID 5s"),
        ):
            game = self.game(upgrades={"TRIPLE SHOT": 1})
            teacher = coach("move")
            run(teacher, game, 30)
            for name, value in values.items():
                setattr(game, name, value)
            row, _, _ = self.frame(game, teacher=teacher)
            self.assertTrue(row.startswith(shown), row)
            self.assertNotIn("TIP", row)

    def test_no_tip_outside_play_in_a_pause_or_a_countdown(self):
        game = self.game()
        teacher = coach("move")
        run(teacher, game, 30)
        for scene in ("paused", "countdown"):
            current = frames.session()
            current.coach = teacher
            frames.playing(current, game)
            current.pause("focus")
            if scene == "countdown":
                current.resume(1.0)
            buf = CellBuffer(80, 24)
            Painter(buf, current.options, MONO).draw(current)
            self.assertNotIn("TIP", buf.row_text(23), scene)

    def test_the_tip_never_covers_the_field_the_hud_or_the_hint_row(self):
        game = self.game()
        teacher = coach("move")
        run(teacher, game, 30)
        with_tip = frames.session()
        with_tip.coach = teacher
        frames.playing(with_tip, copy.deepcopy(game))
        without = frames.playing(frames.session(), copy.deepcopy(game))
        rows = []
        for current in (with_tip, without):
            buf = CellBuffer(80, 24)
            Painter(buf, current.options, MONO).draw(current)
            rows.append([buf.row_text(y) for y in range(24)])
        self.assertEqual(rows[0][:23], rows[1][:23])
        self.assertNotEqual(rows[0][23], rows[1][23])


class TextTests(unittest.TestCase):
    """Spec 7.3: the texts for the default keys, every text within 58 columns for every keymap and scheme, and no
    arena glyph named at k >= 2."""

    def all_texts(self, keymap):
        teacher = coach(keymap=keymap)
        for name in TIPS:
            for scheme in SCHEMES:
                for k in (1, 2):
                    for variant in ("beam", "gap") if name == "marks" else ("",):
                        teacher.variant = variant
                        yield name, scheme, k, variant, teacher.text(name, scheme, k)

    def test_the_default_texts(self):
        teacher = coach()
        expected = {
            ("move", "hold"): "TIP  Hold A or D (or the arrows) to move",
            ("move", "latched"): "TIP  Tap A or D to start; tap back or S to stop",
            ("move", "timed"): "TIP  Press A or D to move; S stops",
            ("fire", "latched"): "TIP  Space fires. Hold it to keep firing",
            ("bomb", "latched"): "TIP  B drops a bomb: it clears enemy fire",
            ("pause", "latched"): "TIP  P pauses. Q in the pause menu leaves the run",
            ("hud", "latched"): "TIP  GUN: shots ready to fire. BOMBS: bombs you hold",
            ("meter", "latched"): "TIP  BOMB %: hits in a row fill it; 100% earns a bomb",
            ("telegraph", "latched"): "TIP  An invader that changes shape is about to fire",
            ("ships", "latched"): "TIP  ^ next to LIVES are your ships. Lose them all: over",
            ("pickup", "latched"): "TIP  Catch the falling letter for a power-up",
        }
        for (name, scheme), text in expected.items():
            self.assertEqual(teacher.text(name, scheme, 1), text)
        for name, k1, k2 in (
            (
                "shield",
                "The { } around your ship is a shield: one hit",
                "The cap over your ship is a shield: it takes one hit",
            ),
            (
                "shelters",
                "Shelters # stop shots, theirs and yours",
                "The blocks above you stop shots, theirs and yours",
            ),
            ("cargo", "$ ships carry a power-up: shoot them", "Marked ships carry a power-up: shoot them"),
        ):
            self.assertEqual(
                (teacher.text(name, "hold", 1), teacher.text(name, "hold", 2)), (f"TIP  {k1}", f"TIP  {k2}")
            )
        teacher.variant = "beam"
        self.assertEqual(teacher.text("marks", "hold", 1), "TIP  ' and ^ show where a beam will strike: move away")
        self.assertEqual(teacher.text("marks", "hold", 2), "TIP  Dots and chevrons show where a beam will strike")
        teacher.variant = "gap"
        self.assertEqual(teacher.text("marks", "hold", 1), "TIP  Stand between [ and ] to stay safe")
        self.assertEqual(teacher.text("marks", "hold", 2), "TIP  Stand between the ground brackets to stay safe")

    def test_every_text_fits_for_every_preset_scheme_and_a_custom_keymap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.json"
            path.write_text(
                json.dumps(
                    {
                        "bind": {
                            "play.left": ["ctrl+alt+page_down"],
                            "play.right": ["ctrl+alt+page_up"],
                            "play.stop": ["ctrl+alt+insert"],
                            "play.fire": ["ctrl+alt+delete"],
                            "play.bomb": ["ctrl+alt+backspace"],
                            "play.pause": ["ctrl+alt+f12"],
                            "pause.menu": ["ctrl+alt+f11"],
                        }
                    }
                )
            )
            keymaps = [Keymap.preset(name) for name in PRESETS] + [Keymap.load(path)]
        for keymap in keymaps:
            for name, scheme, k, variant, text in self.all_texts(keymap):
                with self.subTest(tip=name, scheme=scheme, k=k, variant=variant):
                    self.assertLessEqual(len(text), WIDTH)
                    self.assertTrue(text.startswith("TIP  ") and len(text) > len("TIP  "))

    def test_keys_are_named_from_the_keymap(self):
        vim = coach(keymap=Keymap.preset("vim"))
        self.assertEqual(vim.text("move", "hold", 1), "TIP  Hold H or L to move")
        self.assertEqual(vim.text("fire", "hold", 1), "TIP  K fires. Hold it to keep firing")
        arrows = coach(keymap=Keymap.preset("arrows"))
        self.assertEqual(arrows.text("move", "timed", 1), "TIP  Press Left or Right to move; Down stops")

    def test_k2_texts_name_no_arena_glyph(self):
        for name, scheme, k, variant, text in self.all_texts(Keymap.preset("default")):
            if k == 2 and name != "ships":  # the Ships tip's ^ is the HUD's, text at every scale
                self.assertFalse(GLYPHS & set(text), (name, variant, text))

    def test_no_text_explains_the_bang(self):
        for _, _, _, _, text in self.all_texts(Keymap.preset("default")):
            self.assertNotIn("!", text)


class GlyphClaimTests(unittest.TestCase):
    """Spec 15.2 R7: a k = 1 text names a glyph only while it is drawn."""

    def test_shield_holds_only_while_the_ship_is_drawn_shielded(self):
        teacher = coach()
        game = FakeGame()
        game.player.guard = 1
        self.assertTrue(teacher.holds("shield", game))
        game.player.invulnerable = 30  # the ship blinks out: no { } to point at
        self.assertFalse(teacher.holds("shield", game))
        self.assertTrue(coach(reduced_motion=True).holds("shield", game))  # reduced motion holds it drawn

    def test_shelters_at_k1_need_a_solid_cell(self):
        teacher = coach()
        self.assertFalse(teacher.holds("shelters", FakeGame(bunkers={(1, 16): 1})))  # only cracked : cells
        self.assertTrue(teacher.holds("shelters", FakeGame(bunkers={(1, 16): 2})))
        teacher.view = View("latched", 2, 120)
        self.assertTrue(teacher.holds("shelters", FakeGame(bunkers={(1, 16): 1})))

    def test_the_marks_tip_needs_both_glyphs_it_names(self):
        beam = frames.coached("marks", frames.wave(18, 1), variant="beam", shown=1)()
        game = beam.pilot.game
        teacher = beam.coach
        self.assertEqual(teacher.variant, "beam")
        drawn = {key for _, _, key in tutorial.marks(game.hazards, game.bunkers, 2)}
        self.assertTrue({"beam_warn", "lane_mark"} <= drawn)
        buf = CellBuffer(80, 24)
        Painter(buf, beam.options, MONO).draw(beam)
        field = "".join(buf.row_text(y) for y in range(2, 22))
        self.assertIn("'", field)
        self.assertIn("^", field)
        self.assertIn("TIP  ' and ^ show where a beam will strike", buf.row_text(23))


if __name__ == "__main__":
    unittest.main()
