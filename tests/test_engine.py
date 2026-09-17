"""Gameplay invariants and regressions, independent of a terminal or wall clock."""

import random
import unittest

from terminal_invaders.engine import Game, segment_hit
from terminal_invaders.model import (
    HEIGHT,
    MAX_EFFECTS,
    MAX_PICKUPS,
    MAX_SHOTS,
    UFO,
    WIDTH,
    Alien,
    Boss,
    Config,
    Pickup,
    Shot,
)


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


if __name__ == "__main__":
    unittest.main()
