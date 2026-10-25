"""The bots' view (spec §3.5): a frozen Frame of what the arena draws, with nothing a player can't see."""

import dataclasses
import unittest
from typing import get_type_hints

from terminal_invaders.campaign import Frame, Game, observe
from terminal_invaders.campaign.observe import HazardSeen, PartSeen, Seen, Ship, boss_bar, quarter
from terminal_invaders.campaign.testing import staged
from terminal_invaders.model import UFO, Alien, Config, Hazard, Pickup, Shot

FRAME_FIELDS = (
    "tick phase mode profile wave act loop ship enemies shots parts hazards shelters pickups ufo card prompt modifiers"
).split()
FORBIDDEN = ("vx", "vy", "timer", "ttl", "seed", "rng", "hp", "max_hp", "invulnerable", "shield", "prompt_ticks")


def walk(value):
    """Every value inside a Frame: its fields, the NamedTuples' fields, the tuples' items."""
    yield value
    if dataclasses.is_dataclass(value):
        for field in dataclasses.fields(value):
            yield from walk(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from walk(item)


def busy_game():
    game = staged(Config(seed=4), 6, phase="playing")
    game.aliens.append(Alien(10.13, 4.6, "shell", hp=1, id=901, state="telegraph", max_hp=2, cargo=True, vx=0.3))
    game.aliens.append(Alien(20.0, 5.0, "scout", id=902, veteran=True, home_x=20.0, timer=17))
    game.shots.append(Shot(11.0, 9.9, friendly=False, vx=0.05, vy=0.14, id=903, kind="dart"))
    game.shots.append(Shot(30.0, 12.0, id=904, volley=1))
    game.pickups.append(Pickup(40.37, 6.0, "spread", id=905))
    game.hazards.append(Hazard(906, "beam", 28.5, 31.5, 4.0, 19.0, state="lock", ttl=30))
    game.ufo = UFO(7.06, vx=-0.12)
    game.powerups["rapid"] = 61
    return game


class FrameTests(unittest.TestCase):
    def test_frame_is_the_one_frozen_dataclass_and_its_parts_are_named_tuples(self):
        self.assertEqual([field.name for field in dataclasses.fields(Frame)], FRAME_FIELDS)
        self.assertTrue(Frame.__dataclass_params__.frozen)
        self.assertTrue(hasattr(Frame, "__slots__"))
        for kind in (Ship, Seen, PartSeen, HazardSeen):
            self.assertTrue(issubclass(kind, tuple) and hasattr(kind, "_fields"), kind)
        frame = observe(busy_game())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            frame.tick = 3
        for value in walk(frame):
            self.assertFalse(callable(value) and not isinstance(value, (str, int, float)), value)
            self.assertNotIsInstance(value, (list, dict, set))

    def test_frame_holds_no_velocities_timers_hp_seed_or_rng(self):
        names = set(FRAME_FIELDS)
        for kind in (Ship, Seen, PartSeen, HazardSeen):
            names |= set(kind._fields)
        self.assertEqual(names & set(FORBIDDEN), set())
        self.assertNotIn("rng", " ".join(get_type_hints(Frame)))

    def test_positions_round_to_quarter_cells_and_ids_are_kept(self):
        self.assertEqual([quarter(v) for v in (1.12, 1.13, 2.625, -0.2)], [1.0, 1.25, 2.75, -0.25])
        game = busy_game()
        frame = observe(game)
        shell, scout = frame.enemies[-2:]
        self.assertEqual(shell, Seen(901, 10.25, 4.5, "shell", "telegraph", True, False, True, False))
        self.assertEqual((scout.id, scout.pose, scout.cracked, scout.veteran), (902, "formation", False, True))
        game.aliens[-1].hp, game.aliens[-1].max_hp = 1, 2  # a plated scout that was hit: no cracked frame
        self.assertFalse(observe(game).enemies[-1].cracked)
        game.aliens[-1].hp = 2
        dart, mine = frame.shots
        self.assertEqual((dart.id, dart.x, dart.y, dart.kind, dart.friendly), (903, 11.0, 10.0, "dart", False))
        self.assertTrue(mine.friendly)
        self.assertEqual(frame.pickups, (Seen(905, 40.25, 6.0, "spread"),))
        self.assertEqual(frame.hazards, (HazardSeen(906, "beam", 28.5, 31.5, 4.0, 19.0, True, False),))
        self.assertEqual(frame.ufo, (7.0, 1.0))
        self.assertEqual((frame.ship.slots, frame.ship.free_slots, frame.ship.pickups), (2, 1, (("rapid", 2),)))
        self.assertEqual(observe(game), frame)  # nothing moved: the same Frame

    def test_boss_parts_and_the_bar(self):
        game = staged(Config(), 6)
        frame = observe(game)
        self.assertEqual([(part.boss, part.name, part.state) for part in frame.parts], [
            ("warden", "turret", "open"), ("warden", "core", "shielded"), ("warden", "turret", "open")])  # fmt: skip
        self.assertEqual({part.bar for part in frame.parts}, {12})
        boss = game.boss
        boss.hp = 1
        self.assertEqual(boss_bar(boss), 1)
        boss.hp = 0
        self.assertEqual(boss_bar(boss), 0)

    def test_shelters_show_drawn_tiers(self):
        game = Game(Config())
        frame = observe(game)
        self.assertEqual(len(frame.shelters), 64)
        self.assertEqual({tier for _x, _y, tier in frame.shelters}, {3})
        self.assertEqual(list(frame.shelters), sorted(frame.shelters, key=lambda cell: (cell[1], cell[0])))
        hard = observe(Game(Config(difficulty="hard")))
        self.assertEqual((len(hard.shelters), {tier for *_xy, tier in hard.shelters}), (48, {2}))

    def test_card_and_prompt(self):
        game = staged(Config(), 4, phase="upgrade")
        frame = observe(game)
        self.assertEqual(frame.card, (game.offers, 1, False))
        self.assertEqual(frame.card[0][0], "TRIPLE SHOT")
        self.assertIsNone(frame.prompt)
        for _ in range(30):
            game.step(())
        self.assertEqual(observe(game).card, (game.offers, 1, True))
        easy = staged(Config(difficulty="easy"), 5, phase="continue")
        frame = observe(easy)
        self.assertEqual((frame.prompt, frame.card, frame.phase), ((10, 1), None, "continue"))
        self.assertEqual((frame.profile, frame.mode, frame.wave, frame.act, frame.loop), ("easy", "classic", 5, 1, 1))


class PlayedFrameTests(unittest.TestCase):
    """observe() over real play: it never changes the game, and its incremental cache returns what a fresh build
    would."""

    MOVES = (("fire", "right"), ("fire", "left"))

    def test_observing_never_changes_the_game(self):
        watched, plain = staged(Config(seed=9), 5), staged(Config(seed=9), 5)
        for tick in range(900):
            before = watched.digest()
            observe(watched)
            self.assertEqual(watched.digest(), before)
            actions = self.MOVES[tick // 120 % 2]
            watched.step(actions)
            plain.step(actions)
        self.assertEqual(watched.digest(), plain.digest())

    def test_the_cache_matches_a_fresh_build_and_ids_stay_put(self):
        for wave in (3, 6, 9):
            game = staged(Config(seed=5), wave, phase="playing", immortal=True)
            seen = {}
            for tick in range(700):
                game.step(self.MOVES[tick // 150 % 2])
                frame = observe(game)
                cache, game._seen = game._seen, None
                fresh = observe(game)
                game._seen = cache
                with self.subTest(wave=wave, tick=game.tick):
                    self.assertEqual(frame, fresh)
                    self.assertEqual([e.id for e in frame.enemies], [a.id for a in game.aliens])
                    self.assertEqual([s.id for s in frame.shots], [s.id for s in game.shots])
                    for item in (frame.ship, *frame.enemies, *frame.shots, *frame.pickups, *frame.parts):
                        self.assertEqual(len(item), len(item._fields))  # built without the class's own field check
                for enemy in frame.enemies:
                    self.assertEqual(seen.setdefault(enemy.id, enemy.kind), enemy.kind)  # an id keeps its enemy
                if game.phase != "playing":
                    break

    def test_an_unchanged_tick_reuses_the_last_values(self):
        game = busy_game()
        first = observe(game)
        again = observe(game)
        self.assertIs(again.enemies, first.enemies)
        self.assertIs(again.shots, first.shots)
        self.assertIs(again.shelters, first.shelters)
        game.aliens[-1].x += 0.3  # one enemy moves a visible step: only its entry is rebuilt
        moved = observe(game)
        self.assertIsNot(moved.enemies, first.enemies)
        self.assertEqual([a is b for a, b in zip(moved.enemies, first.enemies)][-2:], [True, False])


if __name__ == "__main__":
    unittest.main()
