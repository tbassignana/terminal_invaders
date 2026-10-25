"""The campaign-2 rules (spec §5-§14), independent of a terminal or a wall clock.

The Phase 3 (campaign-1) tests that lived here run against legacy.campaign1 in tests/test_legacy.py.
"""

import unittest
from dataclasses import replace
from unittest import mock

from terminal_invaders import campaign
from terminal_invaders.campaign import director, fleet
from terminal_invaders.campaign import game as game_module
from terminal_invaders.campaign import tables as T
from terminal_invaders.campaign.boss import AttackState
from terminal_invaders.campaign.collide import segment_hit
from terminal_invaders.campaign.game import combo_for
from terminal_invaders.campaign.observe import observe
from terminal_invaders.campaign.testing import staged
from terminal_invaders.engine import DIGEST_FIELDS, Game
from terminal_invaders.model import (
    ACTIONS,
    PHASES,
    UFO,
    Alien,
    Boss,
    Config,
    Effect,
    Hazard,
    Pickup,
    Shot,
)
from terminal_invaders.rng import Rng

CAMPAIGN_CHANGES = {  # one deliberate change per non-scalar gameplay field; scalars are bumped or toggled
    "config": lambda game: setattr(game, "config", replace(game.config, seed=game.config.seed + 1)),
    "rng": lambda game: game.rng.random(),
    "rng_drop": lambda game: game.rng_drop.random(),
    "rng_offer": lambda game: game.rng_offer.random(),
    "phase": lambda game: setattr(game, "phase", "victory"),
    "player": lambda game: setattr(game.player, "guard", game.player.guard + 1),
    "aliens": lambda game: game.aliens.append(Alien(1, 1)),
    "shots": lambda game: game.shots.append(Shot(1, 1)),
    "pickups": lambda game: game.pickups.append(Pickup(1, 1, "slow")),
    "ufo": lambda game: setattr(game, "ufo", UFO(5)),
    "bunkers": lambda game: game.bunkers.popitem(),
    "boss": lambda game: setattr(game, "boss", Boss(30, 3, 9, 9)),
    "hazards": lambda game: game.hazards.append(Hazard(1, "gap", 1, 8, 19, 19)),
    "powerups": lambda game: game.powerups.update(slow=1),
    "upgrades": lambda game: game.upgrades.update(MAGNET=1),
    "offers": lambda game: setattr(game, "offers", ("MAGNET",)),
    "modifiers": lambda game: game.modifiers.append("QUICK RELOAD"),
    "picks": lambda game: setattr(game, "picks", ("MAGNET",)),
    "_prev": lambda game: setattr(game, "_prev", ("fire",)),
    "_director": lambda game: game._director.update(slots=4),
    "_fire": lambda game: setattr(game, "_fire", replace(game._fire, reload=game._fire.reload + 1)),
    "_flights": lambda game: setattr(game._flights[0], "push", game._flights[0].push + 1),
    "_pending": lambda game: game._pending.pop(),
    "_pvol": lambda game: game._pvol.update({999: [1, False, False]}),
    "_pierce": lambda game: game._pierce.update({999: 1}),
    "_volleys": lambda game: game._volleys.append(director.Volley(999, "single", "safe", 1, [], [], 18, 18.0)),
    "_brood": lambda game: game._brood.update({999: [0.0, 0]}),
    "_parent": lambda game: game._parent.update({999: 1}),
    "_attacks": lambda game: game._attacks.append(AttackState("A")),
    "_motion": lambda game: setattr(game._motion, "leg", game._motion.leg + 1),
    "_hits": lambda game: game._hits.append("pellet"),
    "_landed": lambda game: game._landed.append(0),
}
GAME_MEMBERS = {  # §3.2's members and their types at a game's start
    "phase": str, "tick": int, "wave": int, "act": int, "loop": int, "score": int, "kills": int, "shots_fired": int,
    "hits": int, "aliens": list, "shots": list, "pickups": list, "bunkers": dict, "hazards": list, "combo": int,
    "best_combo": int, "chain": int, "combo_timer": int, "bomb_charge": int, "bombs_this_act": int, "powerups": dict,
    "upgrades": dict, "offers": tuple, "cursor": int, "armed": int, "prompt": int, "continues": int, "modifiers": list,
    "difficulty": float, "events": list, "staged": bool, "wave_name": str, "intro": tuple, "notice": str,
    "notes": list, "effects": list, "hit_cause": str, "fired": list,
}  # fmt: skip
RESULT_KEYS = ("score", "wave", "kills", "shots", "hits", "best_combo", "ticks", "victory", "act", "loop", "bosses")
RESULT_KEYS += ("perfect_waves", "continues_used", "bomb_kills", "bombs_used", "picks")
FAR = 2.0  # a column far from the ship's start (x 30)


def cfg(**values):
    return Config(**values)


def until(game, phase, actions=(), limit=10_000):
    for _ in range(limit):
        if game.phase == phase:
            return game
        game.step(actions)
    raise AssertionError(f"never reached {phase}; at {game.phase}")


def field(wave=2, difficulty="normal", seed=7, upgrades=(), shelters=False, bombs=None, lives=None):
    """A staged game in `playing` with an empty field: no flights to come, no shelters (unless asked), no UFO, an
    idle director and dive clock, and one scout far away at the top so the wave doesn't clear."""
    game = staged(cfg(difficulty=difficulty, seed=seed), wave, phase="playing", upgrades=upgrades, bombs=bombs,
                  lives=lives)  # fmt: skip
    game.aliens.clear()
    game._pending, game._flights, game._next_flight = [], [], 0
    if not shelters:
        game.bunkers.clear()
    game._ufo_at = 0
    game._dclock = game._dive_clock = -1e9
    put(game, FAR, 1.0)
    return game


def put(game, x, y, kind="scout", **values) -> Alien:
    hp = T.ENEMIES[kind].hp
    alien = Alien(x, y, kind, hp, id=game._new_id(), home_x=x, home_y=y, max_hp=hp, **values)
    game.aliens.append(alien)
    return alien


def enemy_shot(game, x, y, vy=0.2, kind="pellet", volley=1) -> Shot:
    shot = Shot(x, y, False, 0.0, vy, 1, game._new_id(), kind, volley)
    game.shots.append(shot)
    return shot


def hit_ship(game):
    """An enemy pellet that reaches the ship this tick."""
    return enemy_shot(game, game.player.x, 17.5)


def settle(game, ticks):
    for _ in range(ticks):
        game.step(())


def flight(game, size=2, movement="MARCH", mix=(), shape="line", dims=None, top=2.0):
    """A flight placed in formation at its slots (it skips the entry)."""
    row = T.Flight(0, size, shape, dims or (size,), movement, mix)
    state = fleet.place(game, row, top)
    for alien in game.aliens:
        if alien.flight == state.index:
            alien.state, alien.x, alien.y = "formation", alien.home_x, alien.home_y
    return state


def members(game, state):
    return [alien for alien in game.aliens if alien.flight == state.index and alien.hp > 0]


def curtain_game(seed, difficulty="normal"):
    """Wave 22 with curtains only, and only its COLUMNS flight (shells and weavers) to come."""
    curtains = {"single": 0, "burst": 0, "crossfire": 0, "curtain": 100}
    game = staged(cfg(difficulty=difficulty, seed=seed), 22, immortal=True, director=curtains)
    game._pending = [(0, ("wave", 5))]
    return game


def kill_above(game):
    """A scout just above the muzzle: the next fire kills it at once (its drop, if any, is taken away)."""
    put(game, 30.0, 16.3)
    game.step(("fire",))
    game.pickups.clear()


# --- the Game duck type ------------------------------------------------------------------------------------------------


class InterfaceTests(unittest.TestCase):
    def test_members_of_the_game_duck_type(self):
        game = campaign.Game(cfg())
        for name, kind in GAME_MEMBERS.items():
            self.assertIsInstance(getattr(game, name), kind, name)
        self.assertIsNone(game.ufo)
        self.assertIsNone(game.boss)
        self.assertEqual((game.phase, game.wave, game.act, game.loop, game.combo), ("wave", 1, 1, 1, 1))
        self.assertEqual((game.player.lives, game.player.bombs, game.player.weapon), (3, 1, 2))
        self.assertEqual(campaign.RULESET, "campaign-2")
        self.assertIn(game.phase, PHASES)
        self.assertEqual(tuple(game.result()), RESULT_KEYS)
        self.assertEqual(campaign.held_interval(), 16.5)
        self.assertEqual(campaign.held_interval(3), 11.0)
        self.assertIs(Game, campaign.Game)  # engine.py is a shim
        self.assertEqual(DIGEST_FIELDS, campaign.DIGEST_FIELDS)

    def test_every_attribute_is_hashed_or_presentation(self):
        game = staged(cfg(seed=5), 8, phase="playing", ticks=300)
        hashed, shown = set(campaign.DIGEST_FIELDS), set(campaign.PRESENTATION)
        self.assertEqual(hashed & shown, set())
        self.assertEqual(set(vars(game)) ^ (hashed | shown), set())

    def test_digest_covers_every_gameplay_field_and_ignores_presentation(self):
        def advanced():
            game = staged(cfg(seed=5), 8, phase="playing")
            for _ in range(400):
                game.step(("right", "fire"))
            return game

        original = advanced().digest()
        game = advanced()
        game.wave_name, game.notice, game.intro, game.hit_cause = "X", "Y", ("Z",), "pellet"
        game.notes.append(("NOTE", 9))
        game.effects.append(Effect(1, 1, "BOOM"))
        game.events.append("kill")
        game.fired.append(campaign.Fired(1, "pellet", "safe", 60, 40, 8.0, "formation", "scout"))
        game.shelter_rev += 1
        game._seen = object()
        self.assertEqual(game.digest(), original)
        for name in campaign.DIGEST_FIELDS:
            with self.subTest(field=name):
                game = advanced()
                if name in CAMPAIGN_CHANGES:
                    CAMPAIGN_CHANGES[name](game)
                else:
                    value = getattr(game, name)
                    self.assertIn(type(value), (int, float, bool), name)
                    setattr(game, name, not value if type(value) is bool else value + 1)
                self.assertNotEqual(game.digest(), original)

    def test_actions_are_validated_and_autofire_is_ignored(self):
        game = campaign.Game(cfg())
        with self.assertRaises(ValueError):
            game.step(("teleport",))
        self.assertEqual(game.tick, 0)
        plain, autofire = campaign.Game(cfg()), campaign.Game(cfg())
        for tick in range(400):
            plain.step(("right", "fire") if tick % 3 else ())
            autofire.step(("right", "fire", "autofire") if tick % 3 else ("autofire",))
        self.assertEqual(plain.digest(), autofire.digest())
        self.assertEqual(ACTIONS, frozenset({"left", "right", "fire", "bomb", "autofire"}))
        for phase in ("game_over", "victory"):
            game.phase = phase
            before = game.digest()
            game.step(("fire",))
            self.assertEqual(game.digest(), before)

    def test_same_seed_same_game_and_streams_differ(self):
        left, right = campaign.Game(cfg(seed=9)), campaign.Game(cfg(seed=9))
        for tick in range(3000):
            actions = ("fire", "left" if tick % 80 < 40 else "right")
            left.step(actions)
            right.step(actions)
        self.assertEqual(left.digest(), right.digest())
        self.assertNotEqual(campaign.Game(cfg(seed=1)).digest(), campaign.Game(cfg()).digest())
        states = {left.rng.getstate(), left.rng_drop.getstate(), left.rng_offer.getstate()}
        self.assertEqual(len(states), 3)

    def test_the_start_pins_test_app_relies_on(self):
        game = campaign.Game(cfg())
        game.step(("right",))
        self.assertAlmostEqual(game.player.x, 30.4)  # one tap in the intro
        self.assertEqual(game._clock, 89)
        settle(game, 89)
        self.assertEqual(game.phase, "playing")  # a 90-tick intro: play by 2.4 s
        self.assertEqual(campaign.Game(cfg()).player.bombs, 1)
        self.assertEqual(campaign.Game(cfg(difficulty="easy")).player.bombs, 2)


# --- the tick order (§14.2, §13's fatal-hit row) ---------------------------------------------------------------------


class TickOrderTests(unittest.TestCase):
    def test_a_fatal_tick_still_scores_and_collects_before_the_life_goes(self):
        game = field()
        put(game, 30.0, 16.3)  # the volley fired this tick strikes it at step 4
        game.pickups.append(Pickup(30.0, 17.9, "rapid", game._new_id()))  # caught at step 9
        hit_ship(game)  # strikes the ship at step 8
        game.step(("fire",))
        self.assertEqual((game.kills, game.score, game.hits), (1, 10, 1))
        self.assertEqual(game.player.lives, 2)  # the life goes at step 10, after the kill and the pickup
        self.assertEqual(game.powerups.get("rapid"), 479)  # caught on the fatal tick, and counted down at step 10
        self.assertEqual((game.chain, game.combo), (0, 1))  # the hit breaks the chain the kill began
        self.assertEqual(game.hit_cause, "pellet")
        self.assertEqual(game._freeze, T.TIMING.freeze)
        for name in ("fire", "kill", "pickup", "damage"):
            self.assertIn(name, game.events)

    def test_at_most_one_life_a_tick(self):
        game = field()
        hit_ship(game)
        enemy_shot(game, game.player.x + 0.5, 17.5)
        state = flight(game, top=15.6)  # it lands on the same tick
        game.step(())
        self.assertEqual(game.player.lives, 2)
        self.assertIn("landed", game.events)
        self.assertEqual(members(game, state), [])  # the landed flight is removed either way

    def test_player_shots_resolve_before_the_director_fires(self):
        game = field(wave=8)
        shooter = put(game, 30.0, 16.3)
        game._fire = replace(game._fire, reload=20)
        game._dclock = 1e9  # the director would pick a shooter this very tick
        game.step(("fire",))
        self.assertEqual(shooter.hp, 0)
        self.assertEqual([f for f in game.fired if f.source == "scout" and f.y > 16], [])


# --- the ship's fire (§5) ---------------------------------------------------------------------------------------------


class FireTests(unittest.TestCase):
    def test_muzzle_slots_and_gate(self):
        game = field()
        game.step(("fire",))
        (shot,) = [s for s in game.shots if s.friendly]
        self.assertEqual((shot.x, shot.y, shot.vy), (30.0, T.FIRE.muzzle_y - T.FIRE.speed, -T.FIRE.speed))
        self.assertEqual((game.shots_fired, game._gate, len(game._pvol)), (1, T.FIRE.gate, 1))
        fired_at = [1]
        for tick in range(2, 60):
            before = game.shots_fired
            game.step(("fire",))
            if game.shots_fired > before:
                fired_at.append(tick)
        self.assertEqual(fired_at[:2], [1, 1 + T.FIRE.gate])  # the 8-tick gate
        self.assertEqual(fired_at[2], 1 + 33)  # both slots stay busy until the first shot leaves the top (33 ticks)
        self.assertLessEqual(max(len(game._pvol), 0), 2)

    def test_triple_shot_spread_rapid_and_rail_rounds(self):
        game = field(upgrades=("TRIPLE SHOT",))
        for _ in range(17):
            game.step(("fire",))
        self.assertEqual(len(game._pvol), 3)
        spread = field()
        spread.powerups["spread"] = 100
        spread.step(("fire",))
        self.assertEqual(sorted(s.vx for s in spread.shots if s.friendly), [-0.10, 0.0, 0.10])
        self.assertEqual((len(spread._pvol), spread.shots_fired), (1, 1))  # one slot, counted once
        rapid = field(upgrades=("RAIL ROUNDS",))
        rapid.powerups["rapid"] = 100
        rapid.step(("fire",))
        (shot,) = [s for s in rapid.shots if s.friendly]
        self.assertAlmostEqual(shot.vy, -(T.FIRE.speed + 0.11) * 1.5)

    def test_thrusters_and_rail_rounds_add_up_per_stack(self):  # §5 Speed, Shot speed (second review: TQ11)
        for stacks in (0, 1, 2):
            game = field(upgrades=("THRUSTERS",) * stacks)
            speed = T.PLAYER.speed + stacks * T.UPGRADES["THRUSTERS"].value  # 0.40, 0.46, 0.52
            for move, sign in (("right", 1), ("left", -1)):
                x = game.player.x
                game.step((move,))
                self.assertAlmostEqual(game.player.x - x, sign * speed, msg=stacks)
        for stacks in (1, 2):
            game = field(upgrades=("RAIL ROUNDS",) * stacks)
            game.step(("fire",))
            (shot,) = [s for s in game.shots if s.friendly]
            speed = T.FIRE.speed + stacks * T.UPGRADES["RAIL ROUNDS"].value  # 0.66, 0.77
            self.assertAlmostEqual(shot.vy, -speed, msg=stacks)

    def test_fire_is_refused_in_the_intro_and_the_hit_freeze(self):
        game = staged(cfg(), 2)
        game.step(("fire",))
        self.assertEqual((game.shots_fired, game.shots), (0, []))
        frozen = field()
        hit_ship(frozen)
        frozen.step(())
        x = frozen.player.x
        frozen.step(("fire", "right"))
        self.assertEqual((frozen.shots_fired, frozen.player.x), (0, x))

    def test_hit_miss_and_neutral(self):
        game = field()
        game.chain, game.combo = 5, 2
        game.step(("fire",))
        settle(game, 40)  # it leaves the top having hit nothing: a miss breaks the chain
        self.assertEqual((game.chain, game.combo, game.hits), (0, 1, 0))
        linked = field(upgrades=("CHAIN LINK",))
        linked.chain = 5
        linked.step(("fire",))
        settle(linked, 40)
        self.assertEqual(linked.chain, 2)
        boss = staged(cfg(), 6, phase="playing")
        boss.chain = 4
        core = next(part for part in boss.boss.parts if part.name == "core")
        boss.player.x = core.x
        boss.boss.vx = 0.0
        boss.step(("fire",))
        settle(boss, 30)  # the shielded core absorbs it: neutral, never a miss
        self.assertEqual((boss.chain, boss.hits), (4, 0))


# --- the bomb (§5) ----------------------------------------------------------------------------------------------------


class BombTests(unittest.TestCase):
    def test_the_shockwave_and_what_the_bomb_clears(self):
        game = field()
        near = put(game, 31.0, 14.0)
        high = put(game, 30.0, 10.0)  # beyond the 5-cell reach (and above y 11): untouched
        shell = put(game, 33.0, 14.0, "shell")
        for x in (5.0, 20.0, 50.0):
            enemy_shot(game, x, 5.0)
        game.hazards.append(Hazard(game._new_id(), "gap", 10, 17, 13.2, 19, "lock", 20))
        drop = game.rng_drop.getstate()
        game.step(("bomb",))
        self.assertEqual((near.hp, high.hp, shell.hp), (0, 1, 1))
        self.assertEqual(game.score, 3 * 10 + 10)  # +10 a shot cleared, and the kill at base points
        self.assertEqual((game.kills, game.bomb_kills, game.bombs_used, game.player.bombs), (1, 1, 1, 0))
        self.assertEqual((game.chain, game.bomb_charge), (0, 0))  # no chain, no charge...
        self.assertEqual(game.rng_drop.getstate(), drop)  # ...and no drop draw
        self.assertEqual(([s for s in game.shots if not s.friendly], game.hazards), ([], []))
        self.assertEqual((game._lockout, game.player.invulnerable), (T.BOMB.lock - 1, T.BOMB.invulnerable - 1))
        self.assertIn("bomb", game.events)
        self.assertIn("bomb_kill", game.events)
        self.assertEqual([(e.text, e.ttl) for e in game.effects if e.text == "()"], [("()", 29)])
        self.assertEqual(game.result()["bomb_kills"], 1)

    def test_the_bomb_fires_on_an_edge_only_and_never_hurts_a_boss(self):
        game = field(bombs=2)
        game.step(("bomb",))
        game.step(("bomb",))
        self.assertEqual(game.player.bombs, 1)
        boss = staged(cfg(), 6, phase="playing", bombs=1)
        hp = boss.boss.hp
        boss.player.x = boss.boss.x
        boss.step(("bomb",))
        self.assertEqual((boss.boss.hp, boss.player.bombs), (hp, 0))

    def test_the_charge_its_act_limit_and_the_hold_at_full(self):
        game = field(wave=8, bombs=0)
        game.bomb_charge = T.BOMB.charge - 1
        put(game, 30.0, 16.3)
        game.step(("fire",))
        self.assertEqual((game.player.bombs, game.bombs_this_act, game.bomb_charge), (1, 1, 0))
        self.assertIn(("BOMB READY", T.TIMING.notice - 1), game.notes)
        game.bomb_charge = T.BOMB.charge - 1
        put(game, 30.0, 16.3)
        settle(game, 10)
        game.step(("fire",))
        self.assertEqual((game.player.bombs, game.bomb_charge), (1, T.BOMB.charge))  # the act's limit: it holds
        game.chain = 20
        put(game, 30.0, 16.3)
        settle(game, 10)
        game.step(("fire",))
        self.assertEqual(game.bomb_charge, T.BOMB.charge)  # held at full, never past it
        game._start_wave(13)  # an act start: the held charge adds its bomb
        self.assertEqual((game.player.bombs, game.bombs_this_act, game.bomb_charge), (2, 1, 0))
        capacitor = field(wave=8, upgrades=("CAPACITOR",), bombs=0)
        capacitor.bomb_charge = 599
        put(capacitor, 30.0, 16.3)
        capacitor.step(("fire",))
        self.assertEqual(capacitor.player.bombs, 1)

    def test_a_bomb_pickup_that_cant_add_a_bomb_pays_points(self):
        game = field(bombs=2)
        game.pickups.append(Pickup(30.0, 17.9, "bomb", game._new_id()))
        game.step(())
        self.assertEqual((game.player.bombs, game.score), (2, 500 * game.act))
        room = field(wave=8, bombs=0)
        room.pickups.append(Pickup(30.0, 17.9, "bomb", room._new_id()))
        room.step(())
        self.assertEqual((room.player.bombs, room.bombs_this_act), (1, 1))

    def test_no_bomb_is_earned_in_act_1(self):
        """B-F's decision (open question 9, step 1): in act I neither the charge nor a BOMB pickup adds a bomb; the
        charge holds at full and adds its bomb when act II starts. Bombs at the start and Last Stand's still count."""
        game = field(wave=3, bombs=0)
        self.assertEqual(game.player.bombs, 0)
        game.bomb_charge = T.BOMB.charge - 1
        put(game, 30.0, 16.3)
        game.step(("fire",))
        self.assertEqual((game.player.bombs, game.bombs_this_act, game.bomb_charge), (0, 0, T.BOMB.charge))
        game.pickups.append(Pickup(30.0, 17.9, "bomb", game._new_id()))
        score = game.score
        game.step(())
        self.assertEqual((game.player.bombs, game.score - score), (0, T.BOMB.full_points))
        game._start_wave(7)
        self.assertEqual((game.player.bombs, game.bombs_this_act, game.bomb_charge), (1, 1, 0))
        fresh = Game(cfg())
        self.assertEqual(fresh.player.bombs, T.PROFILES["normal"].bombs)  # the start's bomb is not earned

    def test_a_shot_kill_charges_the_bomb_by_its_multiplier(self):  # §5 Charge (second review: TQ10)
        for chain, combo in ((0, 1), (11, 4), (20, 6)):
            game = field(wave=8, bombs=0)
            game.chain = chain
            kill_above(game)
            self.assertEqual((game.combo, game.bomb_charge), (combo, combo), chain)

    def test_the_stock_cap_holds_a_full_charge_and_a_pickup_pays_points(self):  # §5 Stock (second review: TQ9)
        game = field(wave=8, bombs=2)  # act II with its bomb still to come: only the stock's cap refuses one
        game.bomb_charge = T.BOMB.charge - 1
        kill_above(game)
        self.assertEqual((game.player.bombs, game.bomb_charge, game.bombs_this_act), (2, T.BOMB.charge, 0))
        pickup = field(wave=8, bombs=2)
        pickup.pickups.append(Pickup(30.0, 17.9, "bomb", pickup._new_id()))
        pickup.step(())
        self.assertEqual((pickup.player.bombs, pickup.bombs_this_act, pickup.score), (2, 0, T.BOMB.full_points * 2))
        bay = field(wave=8, upgrades=("BOMB BAY",))
        bay.player.bombs, bay.bomb_charge = 3, T.BOMB.charge - 1
        kill_above(bay)
        self.assertEqual((bay.player.bombs, bay.bomb_charge), (3, T.BOMB.charge))  # BOMB BAY's cap is 3

    def test_a_charge_held_at_the_cap_adds_its_bomb_when_one_is_used(self):  # §5 Per act (second review: TQ19)
        game = field(wave=8, bombs=2)
        game.bomb_charge = T.BOMB.charge  # held at full: the stock is at its cap
        game.step(("bomb",))
        self.assertEqual((game.player.bombs, game.bomb_charge, game.bombs_this_act, game.bombs_used), (2, 0, 1, 1))

    def test_bomb_bay_carries_3_adds_1_now_and_2_an_act(self):  # §10.2 (second review: TQ8)
        game = field(wave=8, bombs=1)
        game._take("BOMB BAY")
        self.assertEqual(game.player.bombs, 2)  # +1 now
        for bombs, expected in ((2, (3, 1, 0)), (1, (2, 2, 0)), (1, (1, 2, T.BOMB.charge))):  # the third holds
            game.player.bombs, game.bomb_charge = bombs, T.BOMB.charge - 1
            kill_above(game)
            self.assertEqual((game.player.bombs, game.bombs_this_act, game.bomb_charge), expected, bombs)
            settle(game, T.FIRE.gate)

    def test_last_stands_bomb_is_outside_the_acts_limit(self):  # §5 Per act (second review: TQ17)
        game = field(wave=8, bombs=0, lives=2)
        hit_ship(game)
        game.step(())
        self.assertEqual((game.player.lives, game.player.bombs, game.bombs_this_act), (1, 1, 0))
        settle(game, T.TIMING.freeze + T.TIMING.safe + 5)
        game.bomb_charge = T.BOMB.charge - 1
        kill_above(game)
        self.assertEqual((game.player.bombs, game.bombs_this_act, game.bomb_charge), (2, 1, 0))  # the act's bomb

    def test_a_bomb_ends_the_volleys_and_boss_attacks_already_telegraphing(self):  # §5 Effect (second review: TQ3)
        game = field(wave=9, bombs=1)
        gunner = put(game, 30.0, 6.0, "gunner")
        game._volleys.append(director._single_volley(game, [gunner], "safe"))
        settle(game, 3)
        self.assertEqual(gunner.state, "telegraph")
        game.step(("bomb",))
        self.assertEqual((game._volleys, gunner.state), ([], "formation"))
        for _ in range(game._lockout):
            game.step(())
            self.assertEqual(game.fired, [])
        for wave in (6, 12, 18):
            boss = staged(cfg(), wave, phase="playing", immortal=True)
            boss.player.invulnerable = 10**6  # no hit freeze: it would hold the bomb back
            for _ in range(2000):
                if any(state.stage == 1 for state in boss._attacks):
                    break
                boss.step(())
                boss.pickups.clear()
            self.assertTrue(any(state.stage == 1 for state in boss._attacks), wave)
            boss.player.bombs = 1
            boss.step(("bomb",))
            self.assertEqual([(state.stage, state.marks) for state in boss._attacks], [(0, [])] * len(boss._attacks))
            self.assertEqual(boss.hazards, [])
            for _ in range(boss._lockout):
                boss.step(())
                self.assertEqual([entry.kind for entry in boss.fired if entry.cls == "boss"], [], wave)


# --- pickups (§10.1) --------------------------------------------------------------------------------------------------


class PickupTests(unittest.TestCase):
    def test_one_drop_draw_per_shot_kill_and_the_cargo_always_drops(self):
        game = field()
        state = game.rng_drop.getstate()
        put(game, 30.0, 16.3)
        game.step(("fire",))
        twin = Rng()
        twin.setstate(state)
        twin.random()
        self.assertEqual(game.rng_drop.getstate(), twin.getstate())
        cargo = field()
        put(cargo, 30.0, 16.3, cargo=True)
        cargo.step(("fire",))
        self.assertEqual(len(cargo.pickups), 1)
        self.assertEqual(cargo.pickups[0].x, 30.0)

    def test_stacking_long_fuse_and_the_cap_of_8(self):
        game = field()
        for _ in range(2):
            game.pickups.append(Pickup(30.0, 17.9, "slow", game._new_id()))
            game.step(())
        self.assertEqual(game.powerups["slow"], 720 - 1)  # 480, then +480 up to 720, then one tick
        fuse = field(upgrades=("LONG FUSE",))
        fuse.pickups.append(Pickup(30.0, 17.9, "spread", fuse._new_id()))
        fuse.step(())
        self.assertEqual(fuse.powerups["spread"], 599)
        crowded = field()
        for index in range(10):
            crowded._spawn_pickup(5.0 + index, 3.0, 0.5)
        self.assertEqual(len(crowded.pickups), T.LIMITS.pickups)

    def test_the_shield_absorbs_one_hit_or_two_with_deflector_and_lasts_600_at_most(self):
        game = field()
        game.pickups.append(Pickup(30.0, 17.9, "shield", game._new_id()))
        game.step(())
        self.assertEqual((game.player.shield_hits, game.powerups["shield"]), (1, 599))
        game.chain = 3
        hit_ship(game)
        game.step(())
        self.assertEqual((game.player.lives, game.player.shield_hits, game.chain), (3, 0, 0))
        self.assertNotIn("shield", game.powerups)
        self.assertEqual(game.player.invulnerable, T.TIMING.absorb - 1)
        deflector = field(upgrades=("DEFLECTOR",))
        deflector.player.guard = 0
        deflector.pickups.append(Pickup(30.0, 17.9, "shield", deflector._new_id()))
        deflector.step(())
        self.assertEqual(deflector.player.shield_hits, 2)
        expiring = field()
        expiring.pickups.append(Pickup(30.0, 17.9, "shield", expiring._new_id()))
        settle(expiring, 600)
        self.assertEqual((expiring.player.shield_hits, expiring.player.shield), (0, 0))

    def test_repair_rebuilds_destroyed_cells_at_1_and_magnet_reaches_further(self):
        game = field(shelters=True)
        cells = sorted(game.bunkers)
        del game.bunkers[cells[0]]
        game.bunkers[cells[1]] = 1
        game.pickups.append(Pickup(30.0, 17.9, "repair", game._new_id()))
        game.step(())
        self.assertEqual((game.bunkers[cells[0]], game.bunkers[cells[1]]), (1, 2))
        self.assertEqual(len(game.bunkers), 64)
        magnet = field(upgrades=("MAGNET",))
        magnet.pickups.append(Pickup(33.0, 17.9, "rapid", magnet._new_id()))  # |dx| 3 < 3.5
        magnet.step(())
        self.assertIn("rapid", magnet.powerups)
        plain = field()
        plain.pickups.append(Pickup(33.0, 17.9, "rapid", plain._new_id()))
        plain.step(())
        self.assertNotIn("rapid", plain.powerups)

    def test_long_fuse_stacks_to_900_and_magnet_slows_the_fall(self):  # §10.2 (second review: TQ16)
        fuse = field(upgrades=("LONG FUSE",))
        caught = []
        for _ in range(3):
            fuse.pickups.append(Pickup(30.0, 17.9, "rapid", fuse._new_id()))
            fuse.step(())
            caught.append(fuse.powerups["rapid"])
        self.assertEqual(caught, [599, 899, 899])  # 600 a pickup, stacking up to 900
        for upgrades, fall in (((), T.PICKUP.fall), (("MAGNET",), T.PICKUP.fall * T.UPGRADES["MAGNET"].extra)):
            game = field(upgrades=upgrades)
            game.pickups.append(Pickup(5.0, 5.0, "rapid", game._new_id()))
            game.step(())
            self.assertAlmostEqual(game.pickups[0].y - 5.0, fall, msg=upgrades)  # 0.045; 0.0315 with MAGNET

    def test_deflector_gives_a_one_hit_guard_at_the_pick_and_at_each_act_start(self):  # §10.2 (second review: TQ14)
        game = field(wave=8)
        self.assertEqual(game.player.guard, 0)
        game._take("DEFLECTOR")
        self.assertEqual(game.player.guard, 1)
        hit_ship(game)
        game.step(())
        self.assertEqual((game.player.guard, game.player.lives), (0, 3))
        game._start_wave(9)  # not an act start
        self.assertEqual(game.player.guard, 0)
        game._start_wave(13)  # act III starts
        self.assertEqual(game.player.guard, 1)

    def test_no_auto_collect_at_the_clear_and_the_next_intro_removes_them(self):
        game = field()
        game.aliens.clear()
        game.pickups.append(Pickup(5.0, 6.0, "rapid", game._new_id()))
        game.step(())
        self.assertEqual(game.phase, "clear")
        y = game.pickups[0].y
        game.step(())
        self.assertGreater(game.pickups[0].y, y)  # still falling in the clear
        self.assertEqual(game.powerups, {})
        until(game, "wave")
        self.assertEqual(game.pickups, [])


# --- combo, landing, lives (§5, §10.3) --------------------------------------------------------------------------------


class ComboTests(unittest.TestCase):
    def test_the_multiplier(self):
        self.assertEqual([combo_for(n) for n in (0, 1, 3, 4, 6, 7, 15, 16, 40)], [1, 1, 1, 2, 2, 3, 5, 6, 6])
        game = field()
        game.chain = 3
        put(game, 30.0, 16.3)
        game.step(("fire",))
        self.assertEqual((game.chain, game.combo, game.best_combo, game.score), (4, 2, 2, 20))

    def test_a_spread_with_one_hit_and_a_piercer_that_flies_on_keep_the_chain(self):
        game = field()
        game.powerups["spread"] = 100
        game.chain = 3
        put(game, 30.0, 16.3)
        game.step(("fire",))
        settle(game, 40)
        self.assertEqual((game.chain, game.hits), (4, 1))
        pierce = field(upgrades=("PIERCER",))
        put(pierce, 30.0, 16.3)
        put(pierce, 30.0, 14.0)
        pierce.step(("fire",))
        settle(pierce, 40)
        self.assertEqual((pierce.kills, pierce.chain, pierce.hits), (2, 2, 1))

    def test_hits_absorbed_or_not_and_landings_break_the_chain_with_no_timer(self):
        game = field(wave=1)
        self.assertEqual(game.player.guard, 1)
        game.chain = 6
        settle(game, 300)
        self.assertEqual(game.chain, 6)  # no timer
        hit_ship(game)
        game.step(())
        self.assertEqual((game.chain, game.player.lives), (0, 3))

    def test_a_landing_costs_a_life_and_removes_the_flight(self):
        game = field()
        game.chain = 5
        state = flight(game, size=3, top=15.6)
        game.step(())
        self.assertEqual((game.player.lives, game.chain, game.hit_cause), (2, 0, "landing"))
        self.assertEqual(members(game, state), [])
        self.assertEqual(game.score, 0)
        safe = field()
        safe.player.invulnerable = 50
        state = flight(safe, top=15.6)
        safe.step(())
        self.assertEqual((safe.player.lives, members(safe, state)), (3, []))
        last = field(lives=1)
        flight(last, top=15.6)
        last.step(())
        self.assertEqual((last.phase, last.notice), ("game_over", "THE FLEET LANDED"))

    def test_last_stand_once_a_run_and_never_on_hard(self):  # bug b
        game = field()
        for _ in range(2):
            hit_ship(game)
            game.step(())
            settle(game, 200)
        self.assertEqual((game.player.lives, game.player.bombs, game.last_stand_used), (1, 2, True))
        self.assertGreater(game.powerups["rapid"], 0)
        game.player.lives, game.player.bombs = 2, 0
        game.powerups.clear()
        hit_ship(game)
        game.step(())
        self.assertEqual((game.player.lives, game.player.bombs, game.powerups), (1, 0, {}))
        hard = field(difficulty="hard")
        for _ in range(2):
            hit_ship(hard)
            hard.step(())
            settle(hard, 200)
        self.assertEqual((hard.player.lives, hard.player.bombs, hard.last_stand_used), (1, 1, False))

    def test_the_training_shield(self):
        game = staged(cfg(), 1, phase="playing")
        self.assertEqual(game.player.guard, 1)
        hit_ship(game)
        game.step(())
        self.assertEqual((game.player.guard, game.player.lives), (0, 3))
        self.assertEqual(game.player.invulnerable, T.TIMING.absorb - 1)
        self.assertIn(("SHIELD DOWN - MOVE OUT FROM UNDER THE !", T.TIMING.notice - 1), game.notes)
        self.assertNotIn("shield", game.powerups)
        self.assertEqual(staged(cfg(), 2, phase="playing").player.guard, 0)
        self.assertEqual(staged(cfg(difficulty="easy"), 3, phase="playing").player.guard, 1)
        self.assertEqual(staged(cfg(difficulty="hard"), 1, phase="playing").player.guard, 0)

    def test_the_hit_freeze_then_the_mercy_clear(self):
        game = field()
        scout = put(game, 10.0, 5.0)
        far = enemy_shot(game, 45.0, 5.0)
        game.hazards.append(Hazard(game._new_id(), "gap", 40, 47, 13.2, 19, "lock", 20))
        hit_ship(game)
        game.step(())
        play, y = game._play, far.y
        for _ in range(T.TIMING.freeze - 1):
            game.step(("right", "fire"))
            self.assertEqual((far.y, scout.y, game._play, game.player.x), (y, 5.0, play, 30.0))
        self.assertIn(far, game.shots)
        game.step(())  # the freeze's last tick: the mercy clear
        self.assertEqual(([s for s in game.shots if not s.friendly], game.hazards), ([], []))
        self.assertEqual((game.player.invulnerable, game._lockout), (T.TIMING.safe, T.TIMING.lockout))
        game.step(())
        self.assertEqual((game.player.invulnerable, game._lockout), (T.TIMING.safe - 1, T.TIMING.lockout - 1))
        hit_ship(game)
        game.step(())
        self.assertEqual(game.player.lives, 2)  # invulnerable

    def test_no_dive_or_mite_starts_in_the_lockout(self):  # F6: 60 ticks with no enemy fire, mites included
        game = field(wave=15)
        carrier = put(game, 20.0, 4.0, "carrier")
        game._brood[carrier.id] = [float(T.ENEMIES["carrier"].launch), 0]
        self.assertTrue(fleet.release_mite(game, 40.0, 5.0))
        game._lockout = 40
        start, launched, released = game.tick, [], []
        for _ in range(50):
            game.step(())
            launched += [game.tick for entry in game.fired if entry.kind == "mite"]
            if game._brood[carrier.id][1] and not released:
                released.append(game.tick)
        self.assertEqual(len(launched), 1)
        self.assertGreater(launched[0] - start, 40)  # the wobble ended at 30; the dive waited for the lockout
        self.assertGreater(released[0] - start, 40)  # the carrier waited too

    def test_a_tell_lasts_as_long_as_its_entry_logs(self):  # F3, F9 (second review: TQ2)
        game = field(wave=9)
        gunner = put(game, 30.0, 6.0, "gunner")
        volley = director._single_volley(game, [gunner], "safe")
        game._volleys.append(volley)
        start = game.tick
        while not game.fired:
            game.step(())
        (entry,) = game.fired
        self.assertEqual((game.tick - start, entry.w - entry.flight), (volley.tele, volley.tele))
        split = field(wave=15)
        splitter = put(split, 30.0, 6.0, "splitter")
        splitter.hp = 1
        split.step(("fire",))
        wobble, launched = 0, []
        for _ in range(120):
            split.step(())
            wobble += any(alien.kind == "mite" and alien.state == "telegraph" for alien in split.aliens)
            launched += [entry for entry in split.fired if entry.kind == "mite"]
            if launched:
                break
        self.assertEqual(splitter.hp, 0)  # killed by the shot, before step 5 of its tick
        self.assertEqual(wobble, T.TELEGRAPHS["mite"])  # its mites showed their tell for 30 frames...
        self.assertEqual({entry.w - entry.flight for entry in launched}, {T.TELEGRAPHS["mite"]})  # ...as logged


# --- §13's rows -------------------------------------------------------------------------------------------------------


class BugTests(unittest.TestCase):
    def test_a_march_drops_and_reverses_at_the_edge_with_no_sideways_step(self):  # bug a
        game = field()
        state = flight(game, size=2)
        aliens = members(game, state)
        shift = T.LIMITS.field[1] - max(alien.home_x for alien in aliens)
        for alien in aliens:
            alien.home_x += shift
            alien.x = alien.home_x
        state.direction = 1
        moves = []
        for _ in range(80):
            before = [(a.home_x, a.home_y) for a in aliens]
            game.step(())
            after = [(a.home_x, a.home_y) for a in aliens]
            if after != before:
                moves.append((after[0][0] - before[0][0], after[0][1] - before[0][1]))
        self.assertEqual(moves[:2], [(0.0, 0.5), (-1.0, 0.0)])

    def test_glide_drops_at_every_second_bounce(self):
        game = field()
        state = flight(game, size=2, movement="GLIDE")
        drops = []
        for _ in range(3000):
            bounces = state.bounces
            game.step(())
            if state.bounces != bounces:
                drops.append(state.dy)
        self.assertEqual(drops[:4], [0.0, 0.5, 0.0, 0.5])

    def test_timers_freeze_outside_playing(self):  # bug c
        game = field()
        game.powerups.update(rapid=300, slow=200)
        game.player.invulnerable, game._lockout = 50, 20
        snapshot = lambda: (dict(game.powerups), game.player.invulnerable, game._lockout)  # noqa: E731
        before = snapshot()
        game.aliens.clear()
        game.pickups.append(Pickup(30.0, 16.0, "spread", game._new_id()))
        game.step(())  # the wave clears at this tick's end (its own timers ran once)
        self.assertEqual(game.phase, "clear")
        before = snapshot()
        clocks = (game._play, game._dclock, game._dive_clock)
        caught = False
        while game.phase == "clear":
            game.step(())
            caught = caught or "spread" in game.powerups
            if game.phase == "clear":
                self.assertEqual((game._play, game._dclock, game._dive_clock), clocks)
        self.assertTrue(caught)
        self.assertEqual(game.phase, "wave")  # the next wave's intro (wave 2 has no card)
        self.assertEqual(snapshot(), (before[0] | {"spread": 480}, *before[1:]))  # caught in the clear: a full 480
        clocks = lambda: (game._play, game._dclock, game._dive_clock, game._ufo_at, game._next_flight,  # noqa: E731
                          list(game._pending))  # fmt: skip
        at_intro = clocks()
        self.assertEqual(at_intro[:3], (0.0, 0.0, 0.0))
        ticks = 0
        while game.phase == "wave":  # review R3: nothing runs in the intro either
            game.step(())
            ticks += 1
            self.assertEqual(clocks(), at_intro)
            self.assertEqual((game.ufo, game._flights, game.aliens), (None, [], []))
        self.assertEqual((ticks, game.phase), (T.TIMING.intro, "playing"))
        self.assertEqual(snapshot(), (before[0] | {"spread": 480}, *before[1:]))
        frozen = lambda game: (dict(game.powerups), game.player.invulnerable, game._lockout,  # noqa: E731
                               game._dclock, game._dive_clock)  # fmt: skip
        for phase, game in (("upgrade", staged(cfg(), 4, phase="upgrade")),
                            ("continue", staged(cfg(difficulty="easy"), 4, phase="continue"))):  # fmt: skip
            game.powerups.update(rapid=100, slow=100)
            game.player.invulnerable, game._lockout = 10, 20  # lockouts too (second review: TQ21)
            game._dclock, game._dive_clock = 3.0, 4.0
            before = frozen(game)
            settle(game, 50)
            self.assertEqual((game.phase, frozen(game)), (phase, before))
        boss = staged(cfg(), 6)
        clocks = [(state.clock, state.stage) for state in boss._attacks]
        settle(boss, 100)
        self.assertEqual([(state.clock, state.stage) for state in boss._attacks], clocks)
        self.assertEqual(boss.phase, "wave")

    def test_a_missing_diver_returns_to_its_slot_where_the_formation_moved_it(self):  # bug d
        game = field()
        state = flight(game, size=3, movement="GLIDE", mix=(("diver", 1),))
        diver = next(a for a in members(game, state) if a.kind == "diver")
        game.player.x = 55.0
        diver.state, diver.diving, diver.vx = "diving", True, 0.0
        start = diver.home_x
        for _ in range(600):
            game.step(())
            if diver.state == "formation":
                break
        self.assertEqual(diver.state, "formation")
        self.assertNotEqual(diver.home_x, start)
        self.assertEqual((diver.x, diver.y), (diver.home_x, diver.home_y))
        self.assertGreater(diver.hp, 0)

    def test_a_ramming_diver_dies_without_points(self):
        game = field()
        diver = put(game, 30.0, 17.5, "diver", state="diving", diving=True)
        game.step(())
        self.assertEqual((diver.hp, game.score, game.kills, game.player.lives), (0, 0, 0, 2))
        self.assertEqual(game.hit_cause, "diver")

    def test_the_ufo_under_slow_and_its_kill(self):  # bug e
        game = field()
        game.powerups["slow"] = 100
        game.ufo = UFO(10.0, 1.0, 0.12)
        play = game._play
        game.step(())
        self.assertAlmostEqual(game.ufo.x, 10.0 + 0.12 * 0.6)
        self.assertAlmostEqual(game._play - play, 0.6)
        kill = field()
        kill.ufo = UFO(30.0, 1.0, 0.0)
        kill.chain = 2
        kill._wave_shots = 7  # this volley is the wave's 8th: 300 points
        kill.step(("fire",))
        settle(kill, 40)
        self.assertIsNone(kill.ufo)
        self.assertEqual((kill.kills, kill.hits, kill.chain, kill.score), (1, 1, 3, 300))
        self.assertEqual(len(kill.pickups), 1)  # the UFO always drops

    def test_the_ufo_appears_once_after_its_time_with_8_enemies(self):
        game = field()
        for index in range(8):
            put(game, 5.0 + 4 * index, 3.0)
        game._ufo_at = int(game._play) + 3
        settle(game, 2)
        self.assertIsNone(game.ufo)
        settle(game, 2)
        self.assertIsNotNone(game.ufo)
        self.assertTrue(game._ufo_done)
        self.assertGreaterEqual(staged(cfg(), 2)._ufo_at, T.UFO.delay)
        self.assertEqual(staged(cfg(), 1)._ufo_at, 0)  # none in wave 1
        self.assertEqual(staged(cfg(), 6)._ufo_at, 0)  # none in boss waves


# --- the fleet, the director, shelters and SWEEP ----------------------------------------------------------------------


class FleetTests(unittest.TestCase):
    def test_an_arriving_flight_pushes_the_older_ones_down_or_waits(self):
        game = field()
        old = flight(game, size=6, shape="rect", dims=(2, 3))  # rows 2 and 4
        game._pending = [(game._play, ("wave", 1))]  # wave 2's vee of 13 (6 cells tall): band 2..10
        game.step(())
        self.assertEqual(game._next_flight, 1)
        self.assertAlmostEqual(old.push, 8.0 - T.ARRIVAL.entry)  # pushed by the overlap at the entry speed
        stuck = field()
        low = flight(stuck, size=9, shape="rect", dims=(3, 3), top=7.0)  # rows 7-11: a push of 3 would pass y 12
        stuck._pending = [(stuck._play, ("wave", 1))]
        settle(stuck, 5)
        self.assertEqual((stuck._next_flight, len(stuck._pending), low.push), (0, 1, 0.0))
        for alien in members(stuck, low):
            stuck._gone(alien, True)
        stuck._purge()
        stuck.step(())
        self.assertEqual(stuck._pending, [])

    def test_the_72_enemy_cap_holds_a_flight(self):
        game = field()
        for index in range(T.LIMITS.enemies - 1 - 5):
            put(game, 2.0 + (index % 14) * 4, 1.0)
        game._pending = [(game._play, ("wave", 0))]  # 12 enemies: 66 + 12 > 72
        settle(game, 3)
        self.assertEqual(len(game._pending), 1)

    def test_sweep_pays_for_a_flight_shot_down_quickly(self):
        game = field()
        state = flight(game, size=1)
        (alien,) = members(game, state)
        alien.x = alien.home_x = 30.0
        alien.y = alien.home_y = 16.3
        state.since = game._play
        game.step(("fire",))
        self.assertEqual((game._sweeps, game.score), (1, 10 + 200 * game.act))
        self.assertIn("sweep", game.events)
        late = field()
        state = flight(late, size=1)
        (alien,) = members(late, state)
        alien.x = alien.home_x = 30.0
        alien.y = alien.home_y = 16.3
        state.since = late._play - T.ARRIVAL.sweep - 1
        late.step(("fire",))
        self.assertEqual(late._sweeps, 0)

    def test_the_director_slots_count_volleys(self):
        game = staged(cfg(seed=3), 5, phase="playing", director={"reload": 20, "slots": 2}, immortal=True)
        most = 0
        for _ in range(1500):
            game.step(())
            most = max(most, sum(v.slots for v in game._volleys))
            for entry in game.fired:
                self.assertGreaterEqual(entry.w, T.LIMITS.warning)
        self.assertEqual(most, 2)
        first = staged(cfg(seed=3), 1, phase="playing")
        for _ in range(400):
            first.step(())
            if first.fired:
                break
        (entry,) = first.fired
        self.assertEqual((entry.kind, entry.cls, first.tick - 90), ("pellet", "safe", 300))
        self.assertGreaterEqual(entry.w, T.LIMITS.safe_warning)

    def test_a_volley_holds_its_slots_until_its_last_shot_ends(self):  # §6.3 step 3 (second review: EC1, TQ4)
        game = staged(cfg(seed=3), 5, phase="playing", director={"reload": 16, "slots": 1}, immortal=True)
        fired = 0
        for _ in range(1500):
            game.step(())
            held = [volley.id for volley in game._volleys]
            self.assertLessEqual(len(held), 1, game.tick)
            for shot in game.shots:
                if not shot.friendly and shot.volley:
                    self.assertIn(shot.volley, held, game.tick)  # its shot still flies: it keeps its slot
            fired += len(game.fired)
        self.assertGreater(fired, 5)

    def test_a_curtain_and_its_gap_mark_stay_until_its_last_pellet_ends(self):  # F3, F4 (second review: EC1)
        game = curtain_game(1)
        while not game.fired:
            game.step(())
        ids = [entry.id for entry in game.fired]
        (volley,) = {shot.volley for shot in game.shots if shot.id in ids}
        (curtain,) = [v for v in game._volleys if v.id == volley]  # held on the tick it fires
        self.assertEqual(curtain.pattern, "curtain")
        ticks = 0
        while any(shot.volley == volley for shot in game.shots):
            self.assertIn(curtain, game._volleys)
            self.assertIn(curtain.hazard, [hazard.id for hazard in game.hazards])  # the mark shows the whole fall
            game.step(())
            ticks += 1
        game.step(())
        self.assertGreater(ticks, 40)
        self.assertNotIn(curtain, game._volleys)
        self.assertNotIn(curtain.hazard, [hazard.id for hazard in game.hazards])

    def test_curtain_shooters_stand_clear_of_the_gap_however_they_weave(self):  # F4 (second review: TQ1, EC2)
        game = field(wave=22)
        flight(game, size=14, shape="rect", dims=(2, 7), movement="COLUMNS", mix=(("shell", 3), ("weaver", 4)))
        kinds, curtains = [], 0
        for x in (8.0, 15.0, 22.0, 27.0, 30.0, 33.0, 38.0, 45.0, 52.0):
            for _ in range(6):
                game.player.x = x
                volley = director._curtain_volley(game, "safe")
                if volley is None:
                    continue
                curtains += 1
                low, high = volley.gap
                for alien in director.volley_aliens(game, volley):
                    reach = 0.5 + T.ENEMIES[alien.kind].amplitude  # a weaver zig-zags 1.5 either way of its slot
                    self.assertFalse(low - reach <= alien.home_x <= high + reach, (x, alien.kind, alien.home_x))
                    kinds.append(alien.kind)
                game._volleys.append(volley)
                director.cancel(game)
        self.assertGreater(curtains, 20)
        self.assertEqual(sorted(set(kinds)), ["shell", "weaver"])

    def test_no_curtain_pellet_leaves_from_inside_its_gap(self):  # F4 (second review: EC2, TQ1)
        pellets = 0
        for seed in (4, 5, 6, 8):  # seeds where a weaver once zig-zagged into its own gap and fired from there
            game = curtain_game(seed)
            gaps = {}
            for _ in range(1500):
                game.step(("left",) if game.tick // 150 % 2 else ("right",))
                for volley in game._volleys:
                    if volley.pattern == "curtain":
                        gaps.setdefault(volley.id, volley.gap)
                for entry in game.fired:
                    shot = next((s for s in game.shots if s.id == entry.id), None)
                    if shot is not None and shot.volley in gaps:
                        pellets += 1
                        low, high = gaps[shot.volley]
                        self.assertFalse(low < shot.x < high, (seed, game.tick, entry.source, shot.x, low, high))
        self.assertGreater(pellets, 300)

    def test_a_flight_skips_its_drop_rather_than_close_on_a_lower_one(self):  # §7.3 (second review: TQ12)
        for movement in ("MARCH", "GLIDE"):
            game = field()
            upper = flight(game, size=10, shape="line", top=2.0, movement=movement)
            top = 2.0 + T.ARRIVAL.apart + 0.6
            lower = flight(game, size=10, shape="rect", dims=(2, 5), top=top, movement="CAROUSEL")
            flips, direction = 0, upper.direction
            for _ in range(6000):
                game.step(())
                flips += upper.direction != direction
                direction = upper.direction
                gap = min(a.y for a in members(game, lower)) - max(a.y for a in members(game, upper))
                self.assertGreaterEqual(gap, T.ARRIVAL.apart - 1e-9, (movement, game.tick))
            self.assertGreater(flips, 4, movement)  # it still turns at each edge
            self.assertEqual(max(a.home_y for a in members(game, upper)), 2.5, movement)  # the one drop that fit

    def test_masonry_adds_hp_now_and_a_cap_per_stack(self):  # §7.4, §10.2 (second review: TQ13)
        game = staged(cfg(), 2, phase="playing")
        cells = sorted(game.bunkers)
        game.bunkers[cells[0]] = 1
        game._take("MASONRY")
        self.assertEqual((game._shelter_cap(), game.bunkers[cells[0]]), (4, 2))  # +1 cap, +1 HP now
        self.assertEqual({game.bunkers[cell] for cell in cells[1:]}, {4})  # full cells rise to the new cap
        game._take("MASONRY")
        self.assertEqual(game._shelter_cap(), 5)
        game._start_wave(7)  # an act start rebuilds to the cap
        self.assertEqual(set(game.bunkers.values()), {5})
        self.assertEqual(staged(cfg(difficulty="hard"), 2, upgrades=("MASONRY",))._shelter_cap(), 3)  # Hard's 2 + 1

    def test_reload_frenzy_and_pressure(self):
        game = field(wave=8)
        game._fire = replace(game._fire, reload=60)
        self.assertEqual(director.reload_eff(game), 60 * game._fire.frenzy)  # few left, none pending: frenzy
        game._pending = [(9999, ("wave", 0))]
        self.assertEqual(director.reload_eff(game), 60)
        game._fire = replace(game._fire, reload=10)
        self.assertEqual(director.reload_eff(game), T.LIMITS.reload)

    def test_shelters_by_profile_and_wave(self):
        normal = staged(cfg(), 2, phase="playing")
        cell = next(iter(normal.bunkers))
        normal.bunkers[cell] = 1
        normal._start_wave(3)
        self.assertEqual(normal.bunkers[cell], 2)  # Normal: +1 a wave
        del normal.bunkers[cell]
        normal._start_wave(4)
        self.assertNotIn(cell, normal.bunkers)  # destroyed cells come back only at an act start
        normal._start_wave(7)
        self.assertEqual(normal.bunkers[cell], 3)
        easy = staged(cfg(difficulty="easy"), 2)
        del easy.bunkers[cell]
        easy._start_wave(3)
        self.assertEqual(easy.bunkers[cell], 3)
        hard = staged(cfg(difficulty="hard"), 2)
        self.assertEqual((len(hard.bunkers), set(hard.bunkers.values())), (48, {2}))
        some = next(iter(hard.bunkers))
        hard.bunkers[some] = 1
        hard._start_wave(3)
        self.assertEqual(hard.bunkers[some], 1)

    def test_slugs_splash_player_shots_erode_and_enemies_crush(self):
        game = field(shelters=True)
        before = dict(game.bunkers)
        enemy_shot(game, 23.0, T.SHELTERS.top - 1.0, 0.3, "slug")
        game.step(())
        (struck,) = [cell for cell in before if cell not in game.bunkers]  # 3 damage: gone
        x, y = struck
        around = [(x + dx, y + dy) for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)) if (x + dx, y + dy) in before]
        self.assertTrue(around)
        self.assertEqual([game.bunkers[cell] for cell in around], [before[cell] - 1 for cell in around])
        game.player.x = 36.0
        game.chain = 4
        cell = max(c for c in game.bunkers if c[0] == 36)
        hp = game.bunkers[cell]
        game.step(("fire",))
        settle(game, 6)
        self.assertEqual((game.bunkers.get(cell, 0), game.chain), (hp - 1, 0))  # a miss: the chain breaks
        crush = field(shelters=True)
        put(crush, 49.0, T.SHELTERS.top + 0.2)
        before = len(crush.bunkers)
        crush.step(())
        self.assertLess(len(crush.bunkers), before)


class WaveFlowTests(unittest.TestCase):
    def test_intro_and_the_act_and_boss_intros(self):
        game = campaign.Game(cfg())
        game.step(("right",))
        self.assertAlmostEqual(game.player.vx, 0.4)
        self.assertEqual(game.player.guard, 1)
        self.assertEqual(game.intro[0], "ACT I  CONTACT")
        settle(game, 88)
        self.assertEqual(game.phase, "wave")
        game.step(())
        self.assertEqual((game.phase, game.events), ("playing", ["wave"]))
        self.assertEqual(staged(cfg(), 7)._clock, T.TIMING.act_intro)
        boss = staged(cfg(), 6)
        self.assertEqual((boss._clock, boss.intro[:2], boss.boss.kind, boss.boss.hp), (150, ("WARNING", "WARDEN"),
            "warden", 582))  # fmt: skip
        self.assertEqual(staged(cfg(), 8)._clock, T.TIMING.intro)
        fresh = staged(cfg(), 7, phase="upgrade", ticks=30)  # the card after the WARDEN, then act II
        fresh.step(("fire",))
        self.assertEqual((fresh.wave, fresh.act, fresh._clock, fresh.events), (7, 2, 180, ["pick", "act"]))

    def test_the_clear_panel_then_the_card_then_the_next_wave(self):
        game = staged(cfg(), 3, phase="playing", immortal=True)
        game.step(("fire",))
        self.assertTrue(game.shots)
        game.aliens.clear()
        game._pending = []
        game.player.x = 30.0
        game.step(())
        self.assertEqual(game.phase, "clear")
        self.assertEqual(game.shots, [])  # every shot gone at the clear, the player's as neutral
        self.assertEqual(game.chain, 0)
        self.assertTrue(game.notice.startswith("WAVE 03 CLEAR  +1,500"))
        self.assertIn("PERFECT +1,000", game.notice)
        game = until(game, "upgrade", ("fire",))
        self.assertEqual(game.events, ["card"])
        self.assertEqual((game.cursor, game.armed, game.offers[0]), (1, 30, "TRIPLE SHOT"))
        self.assertEqual([T.UPGRADES[name].family for name in game.offers], ["offense", "defense", "utility"])
        for _ in range(40):  # Space held from the wave never takes the card
            game.step(("fire",))
        self.assertEqual((game.phase, game.armed), ("upgrade", 0))
        game.step(())
        game.step(("right",))
        game.step(("right",))  # held: one edge
        game.step(())
        game.step(("right",))
        self.assertEqual(game.cursor, 0)  # wrapped
        game.step(())
        game.step(("left",))
        self.assertEqual(game.cursor, 2)  # wrapped to the left too (second review: TQ20)
        game.step(())
        game.step(("right",))
        self.assertEqual(game.cursor, 0)
        game.step(())
        game.step(("bomb",))  # bomb does nothing on the card
        game.step(("fire",))
        self.assertEqual((game.phase, game.wave, game.picks), ("wave", 4, ("TRIPLE SHOT",)))
        self.assertEqual((game.player.weapon, game.upgrades, game.events), (3, {"TRIPLE SHOT": 1}, ["pick"]))
        self.assertEqual(game.notes[0][0], "TAKEN: TRIPLE SHOT")

    def test_a_hit_wave_has_no_perfect(self):
        game = field()
        hit_ship(game)
        game.step(())
        settle(game, 200)
        game.aliens.clear()
        game.step(())
        self.assertEqual(game.phase, "clear")
        self.assertNotIn("PERFECT", game.notice)

    def test_card_arming_and_maxed_upgrades(self):
        game = staged(cfg(seed=3), 10, phase="upgrade", upgrades=("TRIPLE SHOT", "PIERCER"))
        self.assertNotIn("TRIPLE SHOT", game.offers)
        self.assertNotIn("PIERCER", game.offers)
        self.assertEqual(len(game.offers), len(set(game.offers)))
        settle(game, 29)
        game.step(("fire",))  # armed was 1 at this tick's start
        self.assertEqual(game.phase, "upgrade")
        game.step(())
        game.step(("fire",))
        self.assertEqual((game.phase, game.wave), ("wave", 10))
        with self.assertRaises(ValueError):
            staged(cfg(), 5, phase="upgrade")  # no card after wave 4
        with self.assertRaises(ValueError):
            staged(cfg(), 5, upgrades=("TRIPLE SHOT", "TRIPLE SHOT"))

    def test_offers_by_family_when_a_family_is_maxed(self):
        offense = ("TRIPLE SHOT", "RAIL ROUNDS", "RAIL ROUNDS", "PIERCER")
        game = staged(cfg(seed=4), 13, phase="upgrade", upgrades=offense)
        families = [T.UPGRADES[name].family for name in game.offers]
        self.assertEqual(len(game.offers), 3)
        self.assertEqual(families, ["defense", "defense", "utility"])  # defense has the most ranks left (6)

    def test_the_continue_prompt(self):
        game = staged(cfg(difficulty="easy"), 5, phase="continue")
        self.assertEqual((game.prompt, game.armed, game.continues), (600, 30, 1))
        game.step(("bomb",))  # before arming: nothing
        self.assertEqual(game.phase, "continue")
        game.step(("fire",))  # nor fire (second review: TQ5)
        self.assertEqual((game.phase, game.continues), ("continue", 1))
        settle(game, 30)
        game.score = 777
        game.chain = 9
        game.step(("fire",))
        self.assertEqual((game.phase, game.wave, game.player.lives, game.score, game.chain), ("wave", 5, 2, 0, 0))
        self.assertEqual((game._clock, game.continues, game.result()["continues_used"]), (90, 0, 1))
        self.assertEqual((game.aliens, game._next_flight), ([], 0))  # the wave restarts from its first flight
        kept = staged(cfg(difficulty="easy"), 5, phase="continue", upgrades=("TRIPLE SHOT", "MASONRY"), bombs=2)
        kept.bunkers[next(iter(kept.bunkers))] = 1
        before = (dict(kept.upgrades), kept.player.bombs, dict(kept.bunkers))
        settle(kept, 30)
        kept.step(("fire",))  # it keeps upgrades, bombs and shelters (second review: TQ6)
        self.assertEqual((kept.phase, (dict(kept.upgrades), kept.player.bombs, dict(kept.bunkers))), ("wave", before))
        act = staged(cfg(difficulty="easy"), 7, phase="continue", bombs=1)  # an act's first wave: still no act start
        act.bunkers[next(iter(act.bunkers))] = 1
        act.bombs_this_act = 1
        bunkers = dict(act.bunkers)
        settle(act, 30)
        act.step(("fire",))
        self.assertEqual((act.phase, act.wave, act.bunkers, act.bombs_this_act), ("wave", 7, bunkers, 1))
        self.assertNotIn("act", act.events)
        timeout = staged(cfg(difficulty="easy"), 5, phase="continue")
        until(timeout, "game_over")
        self.assertEqual(timeout.tick, 600)
        ended = staged(cfg(difficulty="easy"), 5, phase="continue", ticks=30)
        ended.step(("bomb",))
        self.assertEqual(ended.phase, "game_over")
        with self.assertRaises(ValueError):
            staged(cfg(), 5, phase="continue")
        last = field(difficulty="easy", lives=1)
        last.player.guard = 0  # Easy's wave 2 training shield
        hit_ship(last)
        last.step(())
        self.assertEqual((last.phase, last.notice), ("continue", "SHIP LOST"))
        normal = field(lives=1)
        hit_ship(normal)
        normal.step(())
        self.assertEqual((normal.phase, normal.notice), ("game_over", "SHIP LOST"))

    def test_staged_games(self):
        game = staged(cfg(), 8, lives=2, bombs=0, director={"slots": 5}, ticks=3)
        self.assertEqual((game.staged, game.wave, game.player.lives, game.player.bombs, game.tick), (True, 8, 2, 0, 3))
        self.assertEqual((game._director, game.player.guard, game._fire.slots), ({"slots": 5}, 0, 5))
        self.assertFalse(campaign.Game(cfg()).staged)
        immortal = staged(cfg(), 9, phase="playing", immortal=True)
        hit_ship(immortal)
        immortal.step(())
        self.assertEqual(immortal.player.lives, 99)
        with self.assertRaises(ValueError):
            staged(cfg(), 8, director={"bogus": 1})
        with self.assertRaises(ValueError):
            staged(cfg(), 25)
        endless = staged(cfg(mode="endless"), 30)
        self.assertEqual((endless.act, endless.loop, endless.wave_name), (2, 2, "HIVE QUEEN MK II"))
        self.assertEqual(staged(cfg(), 6, boss_phase=1).boss.phase, 1)

    def test_a_whole_wave_plays_to_its_clear(self):
        game = staged(cfg(seed=2), 1, phase="playing", immortal=True)
        for tick in range(20_000):
            game.step(("fire", "left" if tick // 200 % 2 else "right"))
            if game.phase == "clear":
                break
        self.assertEqual((game.phase, game.aliens, game._pending), ("clear", [], []))
        self.assertLessEqual(game.kills, 40)
        self.assertGreater(game.kills, 20)

    def test_act_one_plays_end_to_end(self):
        """A new game played by a chasing mover (lives held up): waves 1-6, both cards, the WARDEN, then act II."""

        def chase(game):
            if game.phase in ("upgrade", "continue"):
                return ("fire",) if game.tick % 2 else ()
            xs = [alien.x for alien in game.aliens if alien.y < 16]
            if game.boss is not None:
                xs += [part.x for part in game.boss.parts if part.state in ("open", "charge")]
            if not xs:
                return ("fire",)
            target = min(xs, key=lambda x: abs(x - game.player.x))
            if abs(target - game.player.x) <= 0.5:
                return ("fire",)
            return ("fire", "left" if target < game.player.x else "right")

        game = campaign.Game(cfg(seed=42))
        game._immortal = True
        waves, cards, intros = [], [], []
        for _ in range(40_000):
            wave, phase = game.wave, game.phase
            game.step(chase(game))
            if game.wave != wave:
                waves.append(game.wave)
            if game.phase == "upgrade" and phase != "upgrade":
                cards.append(game.wave)
            if game.phase == "wave" and phase != "wave":
                intros.append(game.intro[0])
            if game.act == 2 and game.phase == "playing":
                break
        self.assertEqual(waves, [2, 3, 4, 5, 6, 7])
        self.assertEqual(cards, [3, 6])  # the cards after waves 3 and 6
        self.assertEqual(len(game.picks), 2)
        self.assertEqual(game.result()["bosses"], 1)  # the WARDEN
        self.assertEqual((game.act, game.wave, game.phase), (2, 7, "playing"))
        self.assertTrue(intros[-1].startswith("ACT II"), intros)
        self.assertFalse(game.staged)


class CollisionTests(unittest.TestCase):
    def test_segment_intersection(self):
        self.assertAlmostEqual(segment_hit(0, 5, 0, -5, 0, 0, 1, 1), 0.4)
        self.assertIsNone(segment_hit(5, 5, 5, -5, 0, 0, 1, 1))
        self.assertIsNone(segment_hit(0, 5, 0, 3, 0, 0, 1, 1))

    def test_a_fast_shot_hits_the_nearest_enemy_without_tunnelling(self):
        game = field(upgrades=("RAIL ROUNDS", "RAIL ROUNDS"))
        game.powerups["rapid"] = 100
        near = put(game, 30.0, 15.9)
        far = put(game, 30.0, 15.0)
        game.step(("fire",))
        self.assertEqual((near.hp, far.hp), (0, 1))

    def test_a_shell_takes_two_hits_and_a_veteran_scores_half_again(self):
        game = field()
        shell = put(game, 30.0, 12.0, "shell")
        game.step(("fire",))
        settle(game, 12)
        self.assertEqual((shell.hp, game.kills), (1, 0))
        game.step(("fire",))
        settle(game, 12)
        self.assertEqual((shell.hp, game.kills, game.score), (0, 1, 40))
        veteran = field()
        put(veteran, 30.0, 16.3, veteran=True)
        veteran.step(("fire",))
        self.assertEqual(veteran.score, 15)

    def test_enemy_shots_hit_the_ship_box_and_shelters_first(self):
        game = field()
        miss = enemy_shot(game, 30.0 + T.PLAYER.box[0] + 0.2, 17.5)
        game.step(())
        self.assertEqual(game.player.lives, 3)
        self.assertIn(miss, game.shots)
        sheltered = field(shelters=True)
        x = 23.0
        sheltered.player.x = x
        enemy_shot(sheltered, x, T.SHELTERS.top - 0.6, 0.3)
        sheltered.step(())
        self.assertEqual(sheltered.player.lives, 3)
        self.assertEqual(sheltered.shots, [])


# --- E-M1's review: R2 and R5 (R3 is test_timers_freeze_outside_playing; R1, R4 and R6 are in test_campaign_bosses) --


def overlaps(game) -> list:
    """Pairs of enemies of different flights whose slots (both in formation) lie within 2 x 1 cells of each other."""
    live = [a for a in game.aliens if a.hp > 0 and a.flight >= 0 and a.state in ("formation", "telegraph")]
    return [
        (a.id, b.id)
        for index, a in enumerate(live)
        for b in live[index + 1 :]
        if a.flight != b.flight and abs(a.home_x - b.home_x) < 2.0 and abs(a.home_y - b.home_y) < 1.0
    ]


class ReviewFixTests(unittest.TestCase):
    def test_a_push_moves_an_entering_carousel_flight_whole(self):  # review R2
        game = field(wave=15)
        state = fleet.place(game, T.Flight(0, 8, "rect", (2, 4), "CAROUSEL"), 2.0)
        aliens = members(game, state)
        homes = sorted(a.home_y for a in aliens)
        self.assertTrue(all(a.state == "entering" for a in aliens))
        y = state.y
        state.push = 2.0
        settle(game, 8)  # 0.25 a tick
        self.assertEqual((state.push, state.y), (0.0, y + 2.0))
        self.assertTrue(all(a.state == "entering" for a in aliens))
        self.assertEqual(sorted(a.home_y for a in aliens), [home + 2.0 for home in homes])

    def test_no_two_flights_share_their_slots_in_hard_wave_15(self):  # review R2's scenario
        game = staged(cfg(difficulty="hard", seed=1), 15, immortal=True)
        moves = (("right", "fire"), ("left", "fire"))
        for tick in range(6000):
            game.step(moves[tick // 120 % 2])
            self.assertEqual(overlaps(game), [], game.tick)
            if game.phase not in ("wave", "playing"):
                break
        self.assertEqual(game._next_flight, len(game._wave_row().flights))  # every flight arrived

    def test_a_life_lost_on_the_tick_the_wave_clears_still_gets_its_mercy(self):  # review R5
        game = field()
        game.aliens.clear()
        put(game, game.player.x, T.PLAYER.y - 0.3, "diver", state="diving")  # the wave's last enemy rams the ship
        game.step(())
        self.assertEqual((game.phase, game.player.lives, game._freeze), ("clear", 2, 0))
        self.assertEqual((game.player.invulnerable, game._lockout), (T.TIMING.safe, T.TIMING.lockout))
        until(game, "playing")
        self.assertEqual((game.wave, game.player.invulnerable, game._lockout), (3, T.TIMING.safe, T.TIMING.lockout))
        landed = field()  # and the last flight landing on the tick a pellet hits
        landed.aliens.clear()
        lander = flight(landed, size=1, top=T.ARRIVAL.land)
        landed.step(())
        self.assertFalse(members(landed, lander))
        self.assertEqual((landed.phase, landed.player.lives, landed._freeze), ("clear", 2, 0))
        self.assertEqual((landed.player.invulnerable, landed._lockout), (T.TIMING.safe, T.TIMING.lockout))


# --- the profiles (§11) ----------------------------------------------------------------------------------------------


class ProfileTests(unittest.TestCase):
    def test_lives_bombs_continues_and_the_shot_speed(self):
        for name, lives, cap, bombs, continues, speed in (
            ("easy", 3, 5, 2, 1, 0.92), ("normal", 3, 5, 1, 0, 1.0), ("hard", 3, 4, 1, 0, 1.08),
        ):  # fmt: skip
            game = Game(cfg(difficulty=name))
            profile = T.PROFILES[name]
            self.assertEqual((game.player.lives, profile.cap, game.player.bombs), (lives, cap, bombs), name)
            self.assertEqual((game.continues, game.difficulty), (continues, speed), name)
            self.assertAlmostEqual(director.shot_speed(game, "pellet", 0.15), min(T.LIMITS.vy, 0.15 * speed))
        self.assertEqual(Game(cfg(difficulty="easy", mode="endless")).continues, 0)  # never in Endless
        hard = staged(cfg(difficulty="hard"), 6, phase="playing", lives=4)
        hard._boss_killed()
        self.assertEqual(hard.player.lives, 4)  # +1 at a boss, up to 4

    def test_telegraphs_reaction_and_the_patterns(self):
        for name, tele, scanned, reaction in (("easy", 25, 31, 0.5), ("normal", 20, 25, 1.0), ("hard", 18, 20, 1.25)):
            game = field(difficulty=name)
            self.assertEqual(director.telegraph(game, 20), tele, name)  # x1.25, x1, x0.8 then the 18 floor
            scanner = field(difficulty=name, upgrades=("SCANNER",))
            self.assertEqual(director.telegraph(scanner, 20), scanned, name)  # SCANNER x1.25 too (second review: TQ7)
            self.assertEqual(T.PROFILES[name].reaction, reaction)
        for upgrades, tele in (((), 18), (("SCANNER",), 23)):  # a Normal dart's tell, through the director
            game = field(wave=9, upgrades=upgrades)
            gunner = put(game, 30.0, 6.0, "gunner")
            self.assertEqual(director._single_volley(game, [gunner], "safe").tele, tele, upgrades)
        rows = {}
        for name in T.PROFILES:
            game = field(wave=11, difficulty=name)
            rows[name] = dict(director._weights(game, 8, 99))
        row = T.WAVES[10].director
        self.assertNotIn("crossfire", rows["easy"])  # Easy: no crossfire
        self.assertEqual(rows["normal"]["crossfire"], row.crossfire)
        self.assertEqual((rows["hard"]["burst"], rows["hard"]["crossfire"]), (row.burst + 10, row.crossfire + 10))

    def test_aim_and_hunter_darts(self):
        self.assertEqual([T.PROFILES[p].hunters for p in T.PROFILES], [0, 16, 5])
        for name, wave, kind in (("easy", 20, "dart"), ("normal", 15, "dart"), ("normal", 16, "hunter"),
                                 ("hard", 4, "dart"), ("hard", 5, "hunter")):  # fmt: skip
            game = field(wave=wave, difficulty=name)
            gunner = put(game, 30.0, 5.0, "gunner")
            self.assertEqual(director.shot_kind(game, gunner), kind, (name, wave))
        for name, expected in (("easy", 27.0), ("normal", 33.0)):  # Easy aims where the ship was at the tell
            game = field(wave=9, difficulty=name)
            game.player.x = 27.0
            gunner = put(game, 30.0, 5.0, "gunner")
            volley = director._single_volley(game, [gunner], "safe")
            game._volleys.append(volley)
            game.player.x = 33.0
            director._fire(game, volley, {gunner.id: gunner})
            dart = next(s for s in game.shots if not s.friendly)
            ticks = (T.LIMITS.hit_y - dart.y) / dart.vy
            self.assertAlmostEqual(dart.x + dart.vx * ticks, expected, delta=1e-6)

    def test_the_enemy_mix(self):
        row = T.Flight(0, 14, "rect", (2, 7), "MARCH", (("gunner", 4),))
        slots = fleet.layout(row)
        kinds = {p: fleet.kinds_for(row, slots, T.PROFILES[p], 3) for p in T.PROFILES}
        self.assertEqual(kinds["normal"].count("gunner"), 4)
        self.assertEqual(kinds["easy"].count("gunner"), 2)  # every second gunner is a scout
        front = [k for k, (_x, y, _c) in zip(kinds["hard"], slots, strict=True) if y == 2.0]
        back = [k for k, (_x, y, _c) in zip(kinds["hard"], slots, strict=True) if y == 0.0]
        self.assertEqual(kinds["hard"].count("gunner"), 4 + 2)  # up to 2 front-row scouts become gunners (act II on)
        self.assertEqual((front.count("gunner"), back.count("lancer")), (6, 1))  # and 1 back-row lancer (act III on)
        act2 = fleet.kinds_for(row, slots, T.PROFILES["hard"], 2)
        self.assertEqual((act2.count("gunner"), act2.count("lancer")), (6, 0))
        act1 = fleet.kinds_for(row, slots, T.PROFILES["hard"], 1)
        self.assertEqual(act1, kinds["normal"])
        for name, mites in (("easy", 1), ("normal", 2), ("hard", 2)):
            game = field(wave=15, difficulty=name)
            splitter = put(game, 30.0, 8.0, "splitter")
            game._kill(splitter)
            self.assertEqual(sum(1 for a in game.aliens if a.kind == "mite"), mites, name)

    def test_divers_veterans_and_drops(self):
        for name, most, every, vy in (("easy", 1, 432, 0.18), ("normal", 2, 360, 0.20), ("hard", 3, 306, 0.22)):
            game = field(wave=8, difficulty=name)
            profile = T.PROFILES[name]
            act = T.ACTS[1]
            self.assertEqual(max(1, act.divers + (profile.divers if 2 >= profile.divers_from else 0)), most)
            self.assertAlmostEqual(act.dive_every * profile.dive_every, every * act.dive_every / 360)
            self.assertAlmostEqual(fleet.dive_speed(game, "diver"), vy * T.ENEMIES["diver"].dive_speed)
        self.assertEqual(fleet.veteran_share(staged(cfg(difficulty="hard"), 8)), 0.1)
        self.assertEqual(fleet.veteran_share(staged(cfg(difficulty="hard"), 19)), 0.2)
        self.assertEqual(fleet.veteran_share(staged(cfg(difficulty="normal"), 19)), 0.0)
        self.assertEqual(fleet.veteran_share(staged(cfg(difficulty="easy"), 21)), 0.2)
        self.assertEqual([T.PROFILES[p].drop for p in T.PROFILES], [0.030, 0.022, 0.020])
        self.assertEqual([T.PROFILES[p].pressure for p in T.PROFILES], [1.05, 1.0, 1.0])
        self.assertEqual([T.PROFILES[p].bosses for p in T.PROFILES], list(T.PROFILES))


# --- Endless (§12) ---------------------------------------------------------------------------------------------------


def observe_modifiers(game):
    return observe(game).modifiers


def endless_game(wave, **values):
    return staged(cfg(mode="endless", seed=5), wave, **values)


class EndlessTests(unittest.TestCase):
    def test_the_ladder_stacks_from_wave_25_one_more_every_wave(self):
        ladder = [m.name for m in T.ENDLESS.ladder]
        self.assertEqual(endless_game(24).modifiers, [])
        self.assertEqual(staged(cfg(), 24).modifiers, [])  # the Campaign never has any
        for wave, count in ((25, 1), (26, 2), (28, 4), (32, 8), (33, 9), (41, 17)):
            game = endless_game(wave)
            self.assertEqual(game.modifiers, [ladder[n % 8] for n in range(count)], wave)
            new = wave >= 25  # one more every wave
            self.assertEqual(game.intro[-1] == f"NEW: {ladder[(count - 1) % 8]}", new, (wave, game.intro))
            self.assertEqual([t for t, _n in game.notes if t.startswith("NEW:")], [game.intro[-1]] if new else [])
        playing = endless_game(25, phase="playing")
        self.assertEqual(playing.modifiers, ["QUICK RELOAD"])  # it stays for the wave
        self.assertEqual(observe_modifiers(playing), ("QUICK RELOAD",))

    def test_each_modifier_and_its_cap(self):
        from terminal_invaders.campaign import endless

        def row_of(game):
            return game._wave_row().director

        quick = endless_game(25)
        self.assertAlmostEqual(quick._fire.reload, max(20, row_of(quick).reload * 0.92))
        slot = endless_game(27)
        self.assertEqual(slot._fire.slots, min(8, row_of(slot).slots + 1))
        sharp = endless_game(32)
        self.assertAlmostEqual(sharp._fire.reaction, min(0.15, row_of(sharp).reaction + 0.02))
        armoured = endless_game(26)
        base = staged(cfg(), T.WAVES.index(armoured._wave_row()) + 1)
        self.assertAlmostEqual(fleet.veteran_share(armoured), fleet.veteran_share(base) + 0.1)
        fast = endless_game(29)
        plain = endless_game(28)
        self.assertAlmostEqual(director.shot_speed(fast, "pellet", 0.15), min(0.28, 0.15 * 1.05))
        self.assertAlmostEqual(director.shot_speed(plain, "pellet", 0.15), 0.15)
        reinforced = endless_game(31)
        flights = reinforced._wave_row().flights
        self.assertEqual(len(reinforced._pending), len(flights) + 1)
        self.assertEqual(reinforced._pending[-1], (flights[-1].at + 480, ("wave", len(flights) - 1)))
        fuse = endless_game(31, phase="playing")
        fuse._collect("rapid")
        fuse._collect("rapid")
        self.assertEqual(fuse.powerups["rapid"], 360)
        storm = endless_game(28)
        most, every = endless.divers(3, 240.0, len(storm.modifiers))
        self.assertEqual((most, every), (4, 240.0 * 0.9))
        stacked = 8 * 40  # every modifier stacked 40 times: each at its cap
        row = T.WAVES[19].director
        full = endless.director_row(row, stacked)
        self.assertEqual((full.reload, full.slots, full.reaction), (20, 8, 0.15))
        self.assertEqual(endless.veteran(0.2, stacked), 0.5)
        self.assertEqual(endless.divers(3, 240.0, stacked)[0], 5)
        self.assertEqual(endless.speed(stacked), 1.25)
        self.assertEqual(endless.flights(stacked), (3, 480))
        self.assertEqual(endless.fuse(480, 720, stacked), (360, 360))
        self.assertEqual(endless.director_row(row, 0), row)

    def test_dive_storm_lets_one_more_diver_go_at_once(self):
        for wave, extra in ((27, 0), (28, 1)):
            game = endless_game(wave, phase="playing")
            game.aliens.clear()
            game._pending, game._flights = [], []
            divers = members(game, flight(game, size=6, mix=(("diver", 6),), top=4.0))
            act = T.ACTS[game.act - 1]
            busy = divers[: act.divers]
            for alien in busy:
                alien.state = "telegraph"
                alien.timer = 999
            game._dive_clock = 1e9
            fleet.dives(game, 1.0)
            started = sum(1 for alien in divers if alien.state == "telegraph") - len(busy)
            self.assertEqual(started, extra, wave)

    def test_lives_loop_marks_and_the_loop_levers(self):
        queen = endless_game(30, phase="playing", lives=2)
        self.assertEqual((queen.loop, queen.wave_name), (2, "HIVE QUEEN MK II"))
        queen._boss_killed()
        self.assertEqual(queen.player.lives, 3)
        mothership = endless_game(24, phase="playing", lives=2)
        mothership._boss_killed()
        self.assertEqual((mothership.player.lives, mothership.notes[-1][0]), (3, "EXTRA LIFE"))  # +1 at every boss
        campaign_end = staged(cfg(), 24, phase="playing", lives=2)
        campaign_end._boss_killed()
        self.assertEqual(campaign_end.player.lives, 2)  # bosses 1-3 only in the Campaign
        self.assertEqual(endless_game(25).player.lives, 3)  # loop_life is off to start
        with mock.patch.object(game_module, "ENDLESS", replace(T.ENDLESS, loop_life=1, loop_pressure=0.9)):
            looped = endless_game(25)
            self.assertEqual((looped.loop, looped.player.lives), (2, 4))
            self.assertEqual(endless_game(26).player.lives, 3)  # only at the loop's first wave
            self.assertAlmostEqual(looped._pressure(), 0.9)
            self.assertAlmostEqual(endless_game(24)._pressure(), 1.0)  # the Campaign's waves never read it
        over = endless_game(24, phase="playing")
        over._boss_killed()
        until(over, "upgrade")  # no victory at 24: a card (every third wave), then wave 25
        settle(over, T.TIMING.arm)
        over.step(("fire",))
        self.assertEqual((over.phase, over.wave, over.loop), ("wave", 25, 2))

    def test_cards_every_third_wave_with_fillers(self):
        game = endless_game(27, phase="playing")
        game.aliens.clear()
        game._pending = []
        until(game, "upgrade")
        self.assertEqual(len(game.offers), 3)


# --- Daily (§12) ------------------------------------------------------------------------------------------------------


class DailyTests(unittest.TestCase):
    def test_daily_is_the_campaign_on_the_dates_seed(self):
        seed = 20261019
        daily, classic = Game(cfg(mode="daily", seed=seed)), Game(cfg(seed=seed))
        moves = (("right", "fire"), ("left", "fire"))
        for tick in range(3000):
            for game in (daily, classic):
                game.step(moves[tick // 90 % 2])
        snapshot = lambda g: (g.score, g.wave, [(a.id, a.x, a.y, a.kind) for a in g.aliens],  # noqa: E731
                              [(s.id, s.x, s.y) for s in g.shots], g.rng.getstate(), g.rng_drop.getstate())  # fmt: skip
        self.assertEqual(snapshot(daily), snapshot(classic))
        self.assertEqual((daily.continues, daily.modifiers), (0, []))
        cards = [staged(cfg(mode=mode, seed=seed), 4, phase="upgrade").offers for mode in ("daily", "classic")]
        self.assertEqual(cards[0], cards[1])
        last = staged(cfg(mode="daily", seed=seed), 24, phase="playing")
        last._boss_killed()
        until(last, "victory")


if __name__ == "__main__":
    unittest.main()
