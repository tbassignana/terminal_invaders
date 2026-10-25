"""The campaign-1 rules kept in legacy/ (spec §17): Phase 3's engine tests, run against legacy.campaign1 unchanged,
its three golden tapes verifying through it, its constants, and the refusal to watch one of its replays."""

import random
import unittest
from dataclasses import replace

from terminal_invaders.legacy.campaign1 import DIGEST_FIELDS, Game, segment_hit
from terminal_invaders.legacy.model1 import (
    HEIGHT,
    MAX_EFFECTS,
    MAX_PICKUPS,
    MAX_SHOTS,
    UFO,
    WIDTH,
    Alien,
    Boss,
    Config,
    Effect,
    Pickup,
    Shot,
)

# One deliberate change per non-scalar gameplay field; scalars are bumped or toggled.
GAMEPLAY_CHANGES = {
    "config": lambda game: setattr(game, "config", replace(game.config, seed=game.config.seed + 1)),
    "rng": lambda game: game.rng.random(),
    "phase": lambda game: setattr(game, "phase", "victory"),
    "player": lambda game: setattr(game.player, "bombs", game.player.bombs + 1),
    "aliens": lambda game: setattr(game.aliens[0], "hp", 9),
    "shots": lambda game: game.shots.append(Shot(1, 1)),
    "pickups": lambda game: game.pickups.append(Pickup(1, 1, "slow")),
    "bunkers": lambda game: game.bunkers.popitem(),
    "boss": lambda game: setattr(game, "boss", Boss(30, 3, 9, 9)),
    "ufo": lambda game: setattr(game, "ufo", UFO(5)),
    "powerups": lambda game: game.powerups.update(slow=1),
}


def playing(seed=7, **kwargs):
    game = Game(Config(seed=seed, **kwargs))
    game.phase = "playing"
    game.bunkers.clear()
    return game


class DeterminismTests(unittest.TestCase):
    def test_identical_inputs_reproduce_full_state_despite_global_randomness(self):
        left, right = Game(Config(seed=731)), Game(Config(seed=731))
        for tick in range(1800):
            actions = ("fire", "left" if tick % 80 < 40 else "right")
            left.step(actions)
            random.random()
            right.step(actions)
            if tick % 180 == 0:
                self.assertEqual(left.digest(), right.digest())
        self.assertEqual(left.result(), right.result())

    def test_digest_includes_hidden_timers_rng_and_configuration(self):
        game = Game(Config(seed=1))
        original = game.digest()
        game.rng.random()
        self.assertNotEqual(original, game.digest())
        original = game.digest()
        game._fire_cooldown += 1
        self.assertNotEqual(original, game.digest())
        self.assertNotEqual(Game(Config(seed=1)).digest(), Game(Config(seed=2)).digest())
        self.assertNotEqual(Game(Config()).digest(), Game(Config(mode="daily")).digest())

    def test_digest_covers_gameplay_fields_and_ignores_presentation(self):
        def advanced():
            game = Game(Config(seed=5))
            for tick in range(400):
                game.step(("fire", "left" if tick % 60 < 30 else "right"))
            return game

        game = advanced()
        original = game.digest()
        game.wave_name, game.notice = "RENAMED WAVE", "A NEW NOTICE"
        game.effects.append(Effect(10, 10, "BOOM"))
        game.events.append("kill")
        self.assertEqual(game.digest(), original)
        self.assertEqual(set(vars(game)) - set(DIGEST_FIELDS), {"wave_name", "notice", "effects", "events"})
        for name in DIGEST_FIELDS:
            with self.subTest(field=name):
                game = advanced()
                if name in GAMEPLAY_CHANGES:
                    GAMEPLAY_CHANGES[name](game)
                else:
                    value = getattr(game, name)
                    setattr(game, name, not value if type(value) is bool else value + 1)
                self.assertNotEqual(game.digest(), original)

    def test_step_validation_and_terminal_state(self):
        game = Game()
        with self.assertRaises(ValueError):
            game.step(("teleport",))
        self.assertEqual(game.tick, 0)
        for phase in ("game_over", "victory"):
            game.phase = phase
            before = game.digest()
            game.step(("fire",))
            self.assertEqual(before, game.digest())

    def test_configuration_rejects_unknown_modes_and_difficulties(self):
        for values in ({"mode": "wat"}, {"difficulty": "wat"}, {"seed": "1"}, {"seed": True}):
            with self.assertRaises(ValueError):
                Config(**values)


class CollisionTests(unittest.TestCase):
    def test_segment_intersection_handles_parallel_and_reverse_paths(self):
        self.assertAlmostEqual(segment_hit(0, 0, 0, 10, 0, 5, 1, 1), 0.4)
        self.assertAlmostEqual(segment_hit(0, 10, 0, 0, 0, 5, 1, 1), 0.4)
        self.assertEqual(segment_hit(0, 0, 0, 0, 0, 0, 1, 1), 0)
        self.assertIsNone(segment_hit(2, 0, 2, 10, 0, 5, 1, 1))
        self.assertIsNone(segment_hit(0, 0, 0, 2, 0, 5, 1, 1))

    def test_fast_shot_hits_nearest_alien_without_tunneling(self):
        game = playing()
        far, near = Alien(20, 4), Alien(20, 9)
        game.aliens = [far, near]
        game.shots = [Shot(20, 18, vy=-20)]
        game._move_shots()
        self.assertEqual(game.aliens, [far])
        self.assertEqual(game.hits, 1)
        self.assertFalse(game.shots)

    def test_shelter_blocks_enemy_and_friendly_shots_before_other_targets(self):
        game = playing()
        alien = Alien(game.player.x, 4)
        game.aliens = [alien]
        cell = (int(game.player.x), 14)
        game.bunkers[cell] = 2
        game.shots = [Shot(game.player.x, 18, vy=-20)]
        game._move_shots()
        self.assertEqual(game.bunkers[cell], 1)
        self.assertEqual(game.aliens, [alien])
        game.shots = [Shot(game.player.x, 10, False, vy=20)]
        game._move_shots()
        self.assertNotIn(cell, game.bunkers)
        self.assertEqual(game.player.lives, 3)

    def test_armor_takes_two_hits_but_upgraded_shots_pierce_it(self):
        game = playing()
        game.aliens = [Alien(20, 4, "armor", 2)]
        game.shots = [Shot(20, 5)]
        game._move_shots()
        self.assertEqual(game.aliens[0].hp, 1)
        game.shots = [Shot(20, 5)]
        game._move_shots()
        self.assertFalse(game.aliens)
        game.aliens = [Alien(20, 4, "armor", 2)]
        game.shots = [Shot(20, 5, damage=2)]
        game._move_shots()
        self.assertFalse(game.aliens)

    def test_fast_enemy_shot_hits_player_and_is_consumed(self):
        game = playing()
        game.shots = [Shot(game.player.x, 3, False, vy=30)]
        game._move_shots()
        self.assertEqual(game.player.lives, 2)
        self.assertFalse(game.shots)

    def test_ufo_reward_and_guaranteed_drop(self):
        game = playing()
        game.aliens.clear()
        game.ufo = UFO(30)
        game.shots = [Shot(30, 5, vy=-10)]
        game._move_shots()
        self.assertIsNone(game.ufo)
        self.assertIn(game.score, (150, 250, 400))
        self.assertEqual(len(game.pickups), 1)

    def test_fatal_shot_stops_combat_before_later_rewards_or_pickups(self):
        game = playing()
        game.player.lives = 1
        game.score = 9990
        game.aliens = [Alien(20, 4)]
        game.shots = [Shot(game.player.x, 10, False, vy=20), Shot(20, 10, vy=-20)]
        game.pickups = [Pickup(game.player.x, game.player.y, "shield")]
        game.step()
        self.assertEqual(game.phase, "game_over")
        self.assertEqual(game.player.lives, 0)
        self.assertEqual(game.score, 9990)
        self.assertEqual(game.kills, 0)
        self.assertEqual(game.player.shield, 0)
        self.assertEqual(len(game.pickups), 1)


class CombatTests(unittest.TestCase):
    def test_input_coalesces_repeats_and_clamps_movement(self):
        game = playing()
        game.step(("left", "left", "fire", "fire"))
        self.assertEqual(game.player.x, WIDTH / 2 - 0.4)
        self.assertEqual(game.shots_fired, 1)
        for _ in range(200):
            game._controls(("left",))
        self.assertEqual(game.player.x, 2)
        for _ in range(200):
            game._controls(("right",))
        self.assertEqual(game.player.x, WIDTH - 3)
        before = game.player.x
        game._controls(("left", "right"))
        self.assertEqual(game.player.x, before)

    def test_autofire_cooldown_and_spread(self):
        game = playing()
        game.step(("autofire",))
        self.assertEqual(game.shots_fired, 1)
        for _ in range(11):
            game.step()
        self.assertEqual(game.shots_fired, 1)
        game.step()
        self.assertEqual(game.shots_fired, 2)
        game.step(("autofire",))
        self.assertFalse(game.autofire)
        game._collect("spread")
        game._fire_cooldown = 0
        game.step(("fire",))
        self.assertEqual(game.shots_fired, 5)
        self.assertEqual(sorted(shot.vx for shot in game.shots[-3:]), [-0.13, 0.0, 0.13])

    def test_invulnerability_shield_last_stand_and_death(self):
        game = playing()
        game._collect("shield")
        game._damage_player()
        self.assertEqual((game.player.shield, game.player.lives), (1, 3))
        game._damage_player()
        self.assertEqual(game.player.shield, 1)
        for _ in range(2):
            game.player.invulnerable = 0
            game._damage_player()
        self.assertEqual((game.player.shield, game.player.lives), (0, 2))
        self.assertNotIn("shield", game.powerups)
        game.player.invulnerable = 0
        game.player.bombs = 0
        game._damage_player()
        self.assertEqual(game.player.lives, 1)
        self.assertEqual(game.player.bombs, 1)
        self.assertIn("rapid", game.powerups)
        game.player.invulnerable = 0
        game._damage_player()
        self.assertEqual(game.phase, "game_over")

    def test_combo_expiry_extra_life_and_bomb_charge(self):
        game = playing()
        game._award(9900)
        for _ in range(18):
            alien = Alien(10, 3)
            game.aliens.append(alien)
            game._kill(alien)
        self.assertEqual(game.combo, 5)
        self.assertEqual(game.best_combo, 5)
        self.assertEqual(game.player.lives, 4)
        self.assertEqual((game.player.bombs, game.bomb_charge), (2, 0))
        for _ in range(150):
            game._timers()
        self.assertEqual(game.combo, 0)

    def test_bomb_clears_bullets_but_cannot_recharge_itself(self):
        game = playing()
        game.shots = [Shot(20, 10), Shot(20, 12, False)]
        game._bomb()
        self.assertFalse(game.aliens)
        self.assertEqual(game.player.bombs, 0)
        self.assertEqual(game.bomb_charge, 0)
        self.assertEqual(game.combo, 0)
        self.assertTrue(all(shot.friendly for shot in game.shots))
        self.assertLess(game.score, 1000)

    def test_pickups_stack_expire_repair_and_collect(self):
        game = playing()
        game._collect("repair")
        self.assertTrue(game.bunkers)
        self.assertTrue(all(hp == 2 for hp in game.bunkers.values()))
        game._collect("repair")
        self.assertTrue(all(hp == 3 for hp in game.bunkers.values()))
        game.pickups = [Pickup(game.player.x, game.player.y, "shield")]
        game._move_pickups()
        self.assertEqual(game.player.shield, 2)
        self.assertFalse(game.pickups)
        for _ in range(4):
            game._collect("shield")
        self.assertEqual(game.powerups["shield"], 1200)
        self.assertEqual(game.player.shield, 3)
        game.powerups["shield"] = 1
        game._timers()
        self.assertEqual(game.player.shield, 0)
        self.assertNotIn("shield", game.powerups)


class WaveTests(unittest.TestCase):
    def test_countdown_prevents_shooting_and_starts_after_ninety_ticks(self):
        game = Game()
        for _ in range(89):
            game.step(("fire",))
        self.assertEqual(game.phase, "wave")
        self.assertEqual(game.shots_fired, 0)
        game.step()
        self.assertEqual(game.phase, "playing")
        self.assertEqual(game.tick, 90)

    def test_twelve_wave_campaign_and_endless_progression(self):
        for mode in ("classic", "daily", "endless"):
            game = Game(Config(mode=mode))
            for wave in range(1, 13):
                self.assertEqual(game.wave, wave)
                self.assertEqual(game.boss is not None, wave % 4 == 0)
                self.assertEqual(game.player.weapon, min(3, 1 + (wave - 1) // 3))
                game.aliens.clear()
                game.boss = None
                game.phase = "playing"
                game.step()
            self.assertEqual(game.phase, "wave" if mode == "endless" else "victory")
            self.assertEqual(game.wave, 13 if mode == "endless" else 12)

    def test_perfect_bonus_and_remaining_drops_are_collected(self):
        game = playing()
        game.aliens.clear()
        game.pickups = [Pickup(2, 2, "rapid")]
        game.step()
        self.assertEqual(game.wave, 2)
        self.assertEqual(game.score, 600)
        self.assertIn("rapid", game.powerups)
        self.assertIn("PERFECT", game.notice)
        game._wave_damage = True
        game._complete_wave()
        self.assertEqual(game.score, 800)

    def test_boss_takes_damage_and_drops_reward(self):
        game = playing()
        game.aliens.clear()
        game.boss = Boss(30, 3, 2, 30)
        game.shots = [Shot(30, 4, damage=2)]
        game._move_shots()
        self.assertIsNone(game.boss)
        self.assertEqual(game.kills, 1)
        self.assertEqual(len(game.pickups), 1)
        self.assertIn("boss", game.events)

    def test_formation_accelerates_as_survivors_decrease_and_landing_ends_game(self):
        game = playing()
        game.aliens = [Alien(20, 4)]
        game._alien_clock = 8
        game._move_enemies()
        self.assertEqual(game.aliens[0].x, 20)
        game._move_enemies()
        self.assertEqual(game.aliens[0].x, 21)
        game.aliens = [Alien(WIDTH - 3, game.player.y - 1)]
        game._alien_clock = 100
        game._move_enemies()
        self.assertEqual(game.phase, "game_over")

    def test_diver_returns_and_crushes_shelter(self):
        game = playing()
        diver = Alien(10, HEIGHT + 1, "diver", diving=True)
        game.aliens = [diver]
        game._move_enemies()
        self.assertFalse(diver.diving)
        self.assertEqual(diver.y, 2)
        game.bunkers[(10, 14)] = 3
        game._crush_shelter(10, 14, 1.5)
        self.assertFalse(game.bunkers)

    def test_entity_counts_remain_bounded_in_a_long_session(self):
        game = playing(mode="endless")
        game.player.invulnerable = 100000
        for tick in range(12000):
            # Drive all waves while preserving ordinary combat and timers.
            if tick % 500 == 499:
                game.aliens.clear()
                game.boss = None
            if game.phase == "game_over":
                break
            game.step(("fire", "left" if tick % 90 < 45 else "right"))
            self.assertLessEqual(len(game.shots), MAX_SHOTS)
            self.assertLessEqual(len(game.effects), MAX_EFFECTS)
            self.assertLessEqual(len(game.pickups), MAX_PICKUPS)
            self.assertTrue(2 <= game.player.x <= WIDTH - 3)
            self.assertTrue(all(0 < hp <= 3 for hp in game.bunkers.values()))
        self.assertGreater(game.wave, 12)


# --- campaign-1 replays (spec §17) -----------------------------------------------------------------------------------

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from terminal_invaders import legacy, rules  # noqa: E402
from terminal_invaders.cli import main  # noqa: E402
from terminal_invaders.legacy import model1  # noqa: E402
from terminal_invaders.replay import LEGACY_REFUSAL, Playback, Recorder  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "legacy"
TAPES = {  # Phase 3's goldens: ticks, score and wave at the end
    "golden-classic-normal.json": (7596, 21050, 8),
    "golden-daily.json": (7758, 60250, 12),
    "golden-endless-hard.json": (7116, 19470, 8),
}


class LegacyReplayTests(unittest.TestCase):
    def test_the_three_phase_3_tapes_verify_through_campaign_1(self):
        for name, (ticks, score, wave) in TAPES.items():
            with self.subTest(tape=name):
                replay = Playback(FIXTURES / name)
                self.assertEqual((replay.ruleset, replay.legacy), ("campaign-1", True))
                (tape,) = replay.tapes
                game = tape.simulate()
                self.assertIsInstance(game, legacy.Game)
                self.assertTrue(tape.verify(game))
                self.assertEqual((game.tick, game.score, game.wave), (ticks, score, wave))

    def test_verify_replay_checks_a_legacy_tape(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--verify-replay", str(FIXTURES / "golden-daily.json")]), 0)
        self.assertIn("VERIFIED  pilot 1  7758 ticks  score 60250  wave 12", out.getvalue())

    def test_a_changed_legacy_tape_fails_verification(self):
        raw = json.loads((FIXTURES / "golden-endless-hard.json").read_text())
        raw["tapes"][0]["inputs"] = raw["tapes"][0]["inputs"][:-40]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "changed.json"
            path.write_text(json.dumps(raw))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["--verify-replay", str(path)]), 1)
        self.assertIn("MISMATCH", out.getvalue())

    def test_campaign_1_constants(self):
        self.assertEqual((legacy.RULESET, legacy.CAMPAIGN_WAVES), ("campaign-1", 12))
        self.assertEqual(model1.DIFFICULTIES, {"easy": (4, 0.78), "normal": (3, 1.0), "hard": (2, 1.25)})
        self.assertEqual(model1.PICKUPS, ("rapid", "spread", "shield", "slow", "repair"))
        self.assertEqual(rules.LEGACY, {"campaign-1": legacy.Game})
        self.assertNotIn("campaign-1", rules.RULESETS)
        self.assertIs(rules.game_class("campaign-1"), legacy.Game)
        self.assertEqual(Game(model1.Config()).digest(), Game(model1.Config()).digest())
        self.assertEqual(len(DIGEST_FIELDS), 33)

    def test_watching_a_legacy_replay_is_refused_with_the_message(self):
        replay = Playback(FIXTURES / "golden-classic-normal.json")
        with self.assertRaises(ValueError) as caught:
            replay.refuse_watching()
        self.assertEqual(str(caught.exception), LEGACY_REFUSAL)
        self.assertIn("old 12-wave rules (campaign-1)", LEGACY_REFUSAL)
        from terminal_invaders import campaign
        from terminal_invaders.model import Config as Config2

        game, recorder = campaign.Game(Config2(seed=4)), Recorder(Config2(seed=4))
        for tick in range(120):
            actions = ("fire",) if tick % 7 == 0 else ()
            recorder.append(actions)
            game.step(actions)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "new.json"
            recorder.save(path, game)
            current = Playback(path)
            self.assertEqual((current.ruleset, current.legacy), ("campaign-2", False))
            current.refuse_watching()  # a campaign-2 replay can be watched
            self.assertTrue(current.tapes[0].verify(current.tapes[0].simulate()))


if __name__ == "__main__":
    unittest.main()
