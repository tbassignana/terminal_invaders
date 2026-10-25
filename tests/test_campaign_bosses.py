"""The bosses (spec §9): phases in order, the break rules, attacks per phase per profile, beams' safe lanes, escorts.

The WARDEN plays end to end; the other three are staged through every phase, with each one's movement, attacks and
escorts checked here and their fights' fairness floors here and in test_campaign_fairness.py. E-M1's review findings
R1, R4 and R6 are pinned at the end.
"""

import unittest
from dataclasses import replace
from unittest import mock

from terminal_invaders.campaign import boss as bosses
from terminal_invaders.campaign import director, fleet
from terminal_invaders.campaign import tables as T
from terminal_invaders.campaign.boss import attacks_of, floor_hp
from terminal_invaders.campaign.testing import staged
from terminal_invaders.model import Config, Shot

MOVES = (("right",), ("left",))


def cfg(difficulty="normal", seed=11):
    return Config(difficulty, "classic", seed)


def settle(game, ticks):
    for _ in range(ticks):
        game.step(())


def wear(game, name, keep=1):
    """Take a part down to `keep` hp above its floor in the current phase, as hits would."""
    row = T.BOSSES[game.boss.kind]
    for part in game.boss.parts:
        if part.name == name and part.state in ("open", "charge"):
            low = floor_hp(row, part.name, game.boss.phase) + keep
            game.boss.hp -= part.hp - low
            part.hp = low


def attack(game, key):
    """(the attack, its state) of the current phase's attack `key`."""
    found = attacks_of(game.boss.kind, game.boss.phase, game.config.difficulty)
    return next((a, s) for a, s in zip(found, game._attacks, strict=True) if a.key == key)


def load(game) -> int:
    """Enemy shots on the field, a cluster counting the 3 pellets it splits into."""
    return sum(3 if shot.kind == "cluster" else 1 for shot in game.shots if not shot.friendly)


def untouchable(game):
    """Hits would freeze the boss for the mercy clear: timing tests keep the ship out of reach instead."""
    game.player.invulnerable = 10**6
    game.pickups.clear()  # a phase break's pickup would land on the ship: SLOW would stretch the timings
    game.powerups.clear()
    return game


def ring(game) -> list:
    return [alien for alien in game.aliens if alien.kind == "weaver" and alien.hp > 0]


def shoot_down(game, name, limit=4000):
    """Hold fire under each open part named `name` (immortal), until the phase moves on."""
    phase = game.boss.phase
    for _ in range(limit):
        targets = [p for p in game.boss.parts if p.name == name and p.state in ("open", "charge")]
        if game.boss.phase != phase or game.boss.state != "fighting" or not targets:
            return
        x = targets[0].x
        move = "left" if game.player.x > x + 0.3 else "right" if game.player.x < x - 0.3 else ""
        game.step(("fire", move) if move else ("fire",))
    raise AssertionError("the part never broke")


class WardenTests(unittest.TestCase):
    def test_the_intro_descends_with_no_attacks_then_phase_1(self):
        game = staged(cfg(), 6)
        row = T.BOSSES["warden"]
        self.assertEqual((game.boss.y, game.boss.state), (row.station[1] - row.descent, "intro"))
        fired = []
        while game.phase == "wave":
            game.step(())
            fired += game.fired
        self.assertEqual(fired, [])
        self.assertEqual((game.boss.y, game.boss.state, game.boss.phase), (row.station[1], "fighting", 0))
        states = {part.name: part.state for part in game.boss.parts}
        self.assertEqual(states, {"turret": "open", "core": "shielded"})
        self.assertEqual((game.boss.hp, game.boss.max_hp), (582, 582))

    def test_the_guards_arrive_below_the_boss_and_fire_through_the_director(self):
        game = staged(cfg(), 6, phase="playing", ticks=120, immortal=True)
        guards = [alien for alien in game.aliens if alien.kind == "guard"]
        self.assertEqual(len(guards), 4)
        lowest = max(part.y for part in game.boss.parts)
        self.assertTrue(all(alien.home_y >= lowest + T.ARRIVAL.gap for alien in guards))
        self.assertEqual(game._fire.slots, 1)

    def test_a_phase_break_then_the_core_then_the_kill(self):
        game = staged(cfg(), 6, phase="playing", immortal=True)
        game._immortal, game.player.lives = False, 3  # the ship is kept safe below instead
        game.player.invulnerable = 10**6
        wear(game, "turret")
        for _ in range(4000):
            score = game.score
            target = next(p for p in game.boss.parts if p.name == "turret" and p.state in ("open", "charge"))
            move = "left" if game.player.x > target.x + 0.3 else "right" if game.player.x < target.x - 0.3 else ""
            game.step(("fire", move) if move else ("fire",))
            if game.boss.state == "break":
                break
        self.assertEqual((game.boss.state, game.boss.timer), ("break", T.TIMING.phase_break))
        self.assertEqual(game.score - score, 2 * 500)  # 500 x act a part
        self.assertIn("phase", game.events)
        self.assertEqual([s for s in game.shots if not s.friendly], [])
        self.assertEqual(len(game.pickups), T.PICKUP.phase_break)
        parts = [(p.x, p.y) for p in game.boss.parts]
        for _ in range(T.TIMING.phase_break - 1):
            game.step(())
            self.assertEqual(game.fired, [])  # silent
            self.assertEqual([(p.x, p.y) for p in game.boss.parts], parts)
        game.step(())
        self.assertEqual((game.boss.state, game.boss.phase), ("fighting", 1))
        self.assertEqual({p.name: p.state for p in game.boss.parts}, {"turret": "broken", "core": "open"})
        guards = sum(1 for alien in game.aliens if alien.kind == "guard")
        core = next(p for p in game.boss.parts if p.name == "core")
        core.hp = 172 // 2 + 1
        game.boss.hp = core.hp
        shoot_down_until = game.tick + 600
        while (
            game.tick < shoot_down_until
            and not game._pending
            and guards == sum(1 for alien in game.aliens if alien.kind == "guard")
        ):
            game.step(("fire",))
        self.assertTrue(game._pending or sum(1 for a in game.aliens if a.kind == "guard") > guards)  # D at 50%
        lives, bosses = game.player.lives, game.bosses
        wear(game, "core")
        shoot_down(game, "core")
        self.assertEqual((game.boss.state, game.boss.hp, game.bosses), ("dying", 0, bosses + 1))
        self.assertEqual(game.player.lives, min(T.PROFILES["normal"].cap, lives + 1))
        self.assertIn("boss", game.events)
        self.assertEqual([alien for alien in game.aliens if alien.kind == "guard"], [])
        self.assertGreaterEqual(len(game.pickups), T.PICKUP.boss_kill)
        while game.phase == "playing":
            game.step(())
        self.assertEqual(game.phase, "clear")
        self.assertIsNone(game.boss)
        self.assertNotIn("PERFECT +", game.notice)  # boss waves pay their no-hit bonus at the kill

    def test_shots_at_shielded_parts_are_neutral(self):
        game = staged(cfg(), 6, phase="playing", immortal=True)
        core = next(p for p in game.boss.parts if p.name == "core")
        game.boss.x = core.x = 30.0
        game.player.x = 30.0
        game.chain = 3
        hp = core.hp
        for _ in range(60):
            game.step(("fire",))
            core.x = game.player.x
        self.assertEqual(core.hp, hp)
        self.assertEqual(game.hits, 0)

    def test_attacks_per_profile(self):
        keys = {p: [[a.key for a in attacks_of("warden", n, p)] for n in range(2)] for p in T.PROFILES}
        self.assertEqual(keys["normal"], [["A", "B"], ["B", "C", "D"]])
        self.assertEqual(keys["easy"], [["A", "B"], ["B", "D"]])  # Easy: no C
        hard = {a.key: a for a in attacks_of("warden", 1, "hard")}
        self.assertEqual((hard["C"].shot, hard["C"].aim, hard["B"].count), ("hunter", "lead", 9))
        self.assertEqual(attacks_of("warden", 0, "hard")[1].count, 9)

    def test_boss_shots_meet_f1_and_spawn_outside_every_part(self):
        for difficulty in T.PROFILES:
            game = staged(cfg(difficulty), 6, phase="playing", immortal=True)
            seen = set()
            for tick in range(3000):
                game.step(("left",) if tick // 120 % 2 else ("right",))
                for entry in game.fired:
                    if entry.cls != "boss":
                        continue
                    seen.add(entry.source)
                    self.assertGreaterEqual(entry.w, T.LIMITS.warning)
                    self.assertGreaterEqual(entry.flight, T.LIMITS.flight)
                    shot = next(s for s in game.shots if s.id == entry.id) if any(
                        s.id == entry.id for s in game.shots) else None  # fmt: skip
                    if shot is not None:
                        for part in game.boss.parts:
                            inside = abs(shot.x - part.x) <= part.w / 2 and abs(shot.y - part.y) <= 0.45
                            self.assertFalse(inside, (difficulty, entry, part))
            self.assertEqual(seen, {"turret", "core"}, difficulty)


class EveryBossTests(unittest.TestCase):
    def test_every_boss_stages_every_phase_in_order(self):
        for key, row in T.BOSSES.items():
            for number, phase in enumerate(row.phases):
                with self.subTest(boss=key, phase=number):
                    game = staged(cfg(), row.wave, boss_phase=number, phase="playing")
                    boss = game.boss
                    self.assertEqual((boss.kind, boss.phase, boss.state, boss.phases), (key, number, "fighting",
                        len(row.phases)))  # fmt: skip
                    for part in boss.parts:
                        expected = "open" if part.name in phase.vulnerable else ("broken" if part.hp <= 0 else
                            "shielded")  # fmt: skip
                        self.assertEqual(part.state, expected, part.name)
                        if part.name in phase.vulnerable:
                            self.assertEqual(part.hp - floor_hp(row, part.name, number), phase.hp)
                    self.assertEqual(len(game._attacks), len(attacks_of(key, number, "normal")))
                    self.assertEqual(boss.hp, sum(p.hp for p in boss.parts))

    def test_the_boss_names_and_hp_pools(self):
        totals = {key: staged(cfg(), row.wave).boss.max_hp for key, row in T.BOSSES.items()}
        self.assertEqual(totals, {"warden": 582, "hive_queen": 657, "dreadnought": 716, "mothership": 408})
        self.assertEqual(staged(Config("normal", "endless", 1), 30).wave_name, "HIVE QUEEN MK II")

    def test_attacks_follow_their_profiles(self):
        self.assertNotIn("S", [a.key for a in attacks_of("hive_queen", 1, "easy")])  # Easy: no divers
        self.assertEqual({a.key: a.aim for a in attacks_of("hive_queen", 1, "hard")}["C"], "spray_alternate")
        easy = {a.key: a for a in attacks_of("dreadnought", 1, "easy")}["C"]
        hard = {a.key: a for a in attacks_of("dreadnought", 1, "hard")}["C"]
        normal = {a.key: a for a in attacks_of("dreadnought", 1, "normal")}["C"]
        for attack, profile, ahead in ((easy, "easy", 90), (normal, "normal", 60), (hard, "hard", 45)):
            factor = T.PROFILES[profile].telegraph
            marked = max(T.LIMITS.telegraph, int(attack.telegraph * factor + 0.5))
            self.assertEqual(marked, ahead, profile)
            self.assertEqual(marked - attack.track, T.LIMITS.beam_lock, profile)  # locks 36 ahead on every profile
        self.assertEqual({a.key: a.count for a in attacks_of("dreadnought", 2, "easy")}["C"], 1)
        self.assertNotIn("G", [a.key for a in attacks_of("mothership", 1, "easy")])
        self.assertEqual({a.key: a.count for a in attacks_of("mothership", 3, "hard")}["H"], 2)

    def test_beams_lock_36_ahead_and_leave_a_lane(self):
        for difficulty in T.PROFILES:
            for phase in (1, 2):
                game = staged(cfg(difficulty), 18, boss_phase=phase, immortal=True)
                locked, live, pairs = {}, {}, []
                for tick in range(1500):
                    game.step(("left",) if tick // 200 % 2 else ("right",))
                    beams = [h for h in game.hazards if h.kind == "beam"]
                    for hazard in beams:
                        if hazard.state == "lock":
                            locked.setdefault(hazard.id, game.tick)
                        if hazard.state == "live":
                            live.setdefault(hazard.id, game.tick)
                    if len(beams) == 2:
                        a, b = sorted(beams, key=lambda h: h.x0)
                        pairs.append(b.x0 - a.x1)
                with self.subTest(difficulty=difficulty, phase=phase):
                    self.assertTrue(live)
                    for ident, when in live.items():
                        self.assertGreaterEqual(when - locked[ident], T.LIMITS.beam_lock)
                    for lane in pairs:
                        self.assertGreaterEqual(lane, T.LIMITS.lane)

    def test_the_hive_queens_divers_wobble_below_her_and_dive_under_the_rule(self):
        game = staged(cfg(), 12, boss_phase=1, immortal=True)
        dives, seen = [], set()
        for tick in range(2400):
            game.step(("left",) if tick // 150 % 2 else ("right",))
            queen = game.boss.parts[0]
            for alien in game.aliens:
                if alien.kind == "diver" and alien.id not in seen:
                    seen.add(alien.id)
                    self.assertEqual(alien.state, "telegraph")  # it wobbles first (F9)
                    self.assertGreaterEqual(alien.y, queen.y + 0.45 + 3.0 - 1e-9)  # 3 cells below her box
            dives += [entry for entry in game.fired if entry.kind == "dive"]
        self.assertTrue(dives)
        self.assertTrue(all(entry.w >= T.LIMITS.dive_warning for entry in dives))

    def test_hazards_and_shots_stay_under_their_caps(self):
        for key, row in T.BOSSES.items():
            for number in range(len(row.phases)):
                game = staged(cfg("hard"), row.wave, boss_phase=number, immortal=True)
                for tick in range(900):
                    game.step(("left",) if tick // 100 % 2 else ("right",))
                    self.assertLessEqual(len(game.hazards), T.LIMITS.hazards, (key, number))
                    enemy = sum(3 if s.kind == "cluster" else 1 for s in game.shots if not s.friendly)
                    self.assertLessEqual(enemy, T.LIMITS.enemy_shots, (key, number))
                    if game.phase != "playing":
                        break


class HiveQueenTests(unittest.TestCase):
    def test_her_ring_orbits_her_on_the_ellipse(self):
        game = staged(cfg(), 12, phase="playing", ticks=60, immortal=True)
        weavers = ring(game)
        self.assertEqual(len(weavers), 6)
        flight = game._flights[weavers[0].flight]
        self.assertEqual(flight.ring, (0, 2, 5, 8, 10, 13))  # every (16 / 6)-th entry of the ellipse table
        for _ in range(200):
            game.step(())
        turned = game._play * T.FORMATIONS["ESCORT"].orbit
        self.assertGreater(turned, 4.0)  # a quarter turn and more
        for alien in ring(game):
            ox, oy = fleet.ring_at(flight.ring[alien.id - flight.base] + turned)
            self.assertAlmostEqual(alien.home_x, flight.x + ox, places=6)
            self.assertAlmostEqual(alien.home_y, flight.y + oy, places=6)
            self.assertGreaterEqual(alien.home_y, 0.5 - 1e-6)  # below the boss bar's row
        self.assertLess(abs(flight.x - game.boss.x), 1.0)  # the ring follows her

    def test_the_ring_refills_its_missing_weavers_into_their_empty_places(self):
        for profile, size in (("normal", 6), ("hard", 8)):
            with self.subTest(profile=profile):
                game = staged(cfg(profile), 12, phase="playing", ticks=60, immortal=True)
                weavers = ring(game)
                self.assertEqual(len(weavers), size)
                refill, state = attack(game, "R")
                self.assertEqual((refill.count, refill.below), (size, 3))
                for alien in weavers[:3]:  # 3 or more left: no refill
                    game._gone(alien, True)
                game._purge()
                state.clock = refill.cooldown
                game.step(())
                self.assertEqual(len(ring(game)), size - 3)
                for alien in ring(game)[: size - 5]:  # 2 left
                    game._gone(alien, True)
                game._purge()
                self.assertEqual(len(ring(game)), 2)
                state.clock = refill.cooldown
                game.step(())
                self.assertEqual(len(ring(game)), size)  # back to the ring's size, not size + 2
                places = [game._flights[a.flight].ring[a.id - game._flights[a.flight].base] for a in ring(game)]
                self.assertEqual(sorted(places), fleet.ring_places(size))
                settle(game, 60)
                homes = [(a.home_x, a.home_y) for a in ring(game)]
                for index, (x, y) in enumerate(homes):
                    for ox, oy in homes[index + 1 :]:
                        self.assertGreater(abs(x - ox) + abs(y - oy), 1.0)

    def test_the_figure_8_takes_12_seconds_and_frenzy_runs_it_faster(self):
        game = untouchable(staged(cfg(), 12, boss_phase=1))
        spec = T.BOSSES["hive_queen"].phases[1]
        self.assertEqual(len(spec.path), 8)
        seen, start = [], game._motion.leg
        for _ in range(800):
            game.step(())
            if game._motion.leg != start and (game.boss.x, game.boss.y) in spec.path:
                seen.append((game.tick, (game.boss.x, game.boss.y)))
                start = game._motion.leg
        legs = [later - earlier for (earlier, _a), (later, _b) in zip(seen, seen[1:])]
        self.assertTrue(legs and all(abs(leg - 90) <= 1 for leg in legs), legs)
        self.assertAlmostEqual(T.BOSSES["hive_queen"].phases[2].speed / spec.speed, 1.3, places=2)
        lo, hi = bosses._bounds(T.BOSSES["hive_queen"])
        self.assertTrue(all(lo <= x <= hi and 3.0 <= y <= 4.0 for x, y in spec.path))

    def test_her_divers_wobble_dive_and_return_to_her(self):
        game = staged(cfg(), 12, boss_phase=1, immortal=True)
        states, back = {}, 0
        for tick in range(3000):
            ids = {a.id for a in game.aliens if a.kind == "diver" and a.flight < 0}
            game.step(MOVES[tick // 150 % 2])
            for alien in game.aliens:
                if alien.id in ids or (alien.kind == "diver" and alien.flight < 0):
                    states.setdefault(alien.id, []).append(alien.state)
            alive = {a.id for a in game.aliens}
            back += sum(1 for ident in ids if ident not in alive and "returning" in states.get(ident, []))
        paths = [list(dict.fromkeys(seq)) for seq in states.values()]
        self.assertIn(["telegraph", "diving", "returning"], paths)
        self.assertGreater(back, 0)  # returned to her and rejoined the brood
        self.assertFalse(any("formation" in seq for seq in states.values()))


class DreadnoughtTests(unittest.TestCase):
    def test_the_ram_descends_at_once_holds_and_climbs_back(self):
        game = untouchable(staged(cfg(), 18, boss_phase=2, immortal=True))
        rows = []
        for tick in range(1500):
            game.step(MOVES[tick // 150 % 2])
            rows.append(game.boss.y)
        self.assertGreater(rows[10], T.BOSSES["dreadnought"].station[1])  # it starts down at once
        low = rows.index(max(rows))
        self.assertAlmostEqual(max(rows), 8.0)
        self.assertTrue(330 <= low <= 380, low)  # 5 rows in about 6 s
        held = sum(1 for y in rows if y == 8.0)
        self.assertTrue(350 <= held <= 370, held)
        self.assertAlmostEqual(rows[-1], T.BOSSES["dreadnought"].station[1])

    def test_the_double_beam_marks_its_safe_lane(self):
        game = staged(cfg(), 18, boss_phase=2, immortal=True)
        lanes = 0
        for tick in range(1500):
            game.step(MOVES[tick // 150 % 2])
            beams = sorted((h for h in game.hazards if h.kind == "beam"), key=lambda h: h.x0)
            for lane in (h for h in game.hazards if h.kind == "lane"):
                lanes += 1
                self.assertEqual(len(beams), 2)
                self.assertNotEqual(lane.state, "live")
                self.assertEqual((lane.x0, lane.x1), (beams[0].x1, beams[1].x0))
                self.assertGreaterEqual(lane.x1 - lane.x0, T.LIMITS.lane)
            if len(beams) == 2:
                self.assertEqual(sum(1 for h in game.hazards if h.kind == "lane"), 1)
        self.assertGreater(lanes, 0)
        easy = staged(cfg("easy"), 18, boss_phase=2, immortal=True)
        for tick in range(1500):
            easy.step(MOVES[tick // 150 % 2])
            self.assertLessEqual(sum(1 for h in easy.hazards if h.kind == "beam"), 1)  # Easy: no double beam
            self.assertFalse([h for h in easy.hazards if h.kind == "lane"])


class MothershipTests(unittest.TestCase):
    def test_the_armada_escorts_and_mites(self):
        game = staged(cfg(), 24, phase="playing", ticks=1, immortal=True)
        self.assertEqual([s.size for s in game._flights], [16])
        self.assertEqual([at for at, _key in game._pending], [900])
        mites = 0
        for tick in range(1500):
            game.step(())
            mites += sum(1 for entry in game.fired if entry.kind == "mite")
        self.assertEqual([s.size for s in game._flights], [16, 16])
        self.assertTrue(all(part.state == "shielded" for part in game.boss.parts))
        self.assertGreater(sum(1 for a in game.aliens if a.kind == "mite") + mites, 0)

    def test_the_pods_orbit_round_the_core(self):
        game = untouchable(staged(cfg(), 24, boss_phase=1, immortal=True))
        row = T.BOSSES["mothership"]
        _legs, length, starts = bosses._orbit(row)
        self.assertEqual((length, starts), (48.0, (0.0, 0.0, 12.0, 24.0, 36.0)))
        pods = [p for p in game.boss.parts if p.name == "pod"]
        before = [(p.x - game.boss.x, p.y - game.boss.y) for p in pods]
        xs = []
        for tick in range(400):
            game.step(MOVES[tick // 150 % 2])
            xs.append(game.boss.x)
            for pod in pods:
                dx, dy = pod.x - game.boss.x, pod.y - game.boss.y
                on_edge = abs(abs(dx) - 11) < 1e-6 or abs(abs(dy) - 1) < 1e-6
                self.assertTrue(on_edge and abs(dx) <= 11 + 1e-6 and abs(dy) <= 1 + 1e-6, (dx, dy))
                self.assertTrue(pod.x - 2.5 >= 2 - 1e-6 and pod.x + 2.5 <= 57 + 1e-6)
        after = [(p.x - game.boss.x, p.y - game.boss.y) for p in pods]
        self.assertNotEqual(before, after)
        self.assertAlmostEqual(game._motion.orbit, 400 * 0.06, places=6)
        self.assertGreater(max(xs) - min(xs), 1.0)  # the core drifts

    def test_converging_darts_arrive_together(self):
        game = untouchable(staged(cfg(), 24, boss_phase=1, immortal=True))
        pairs = 0
        for tick in range(1500):
            game.step(MOVES[tick // 150 % 2])
            darts = [e for e in game.fired if e.kind == "dart" and e.source == "pod"]
            if len(darts) == 2:
                pairs += 1
                a, b = (next(s for s in game.shots if s.id == e.id) for e in darts)
                arrive_a = (T.LIMITS.hit_y - a.y) / a.vy
                arrive_b = (T.LIMITS.hit_y - b.y) / b.vy
                self.assertAlmostEqual(arrive_a, arrive_b, places=6)
                if max(abs(a.vx), abs(b.vx)) < T.SHOTS["dart"].vx_max:  # neither held by its vx bound: one point
                    self.assertAlmostEqual(a.x + a.vx * arrive_a, b.x + b.vx * arrive_b, places=6)
                self.assertGreaterEqual(min(e.w for e in darts), T.LIMITS.warning)
        self.assertGreater(pairs, 0)

    def test_a_curtain_waits_while_beams_are_up(self):
        game = staged(cfg(), 24, boss_phase=2, immortal=True)
        gaps, beams_seen = 0, 0
        seen = set()
        for tick in range(3000):
            beams_before = any(h.kind == "beam" for h in game.hazards)
            game.step(MOVES[tick // 150 % 2])
            beams_seen += any(h.kind == "beam" for h in game.hazards)
            for hazard in game.hazards:
                if hazard.kind == "gap" and hazard.id not in seen:
                    seen.add(hazard.id)
                    gaps += 1
                    self.assertFalse(beams_before)
            self.assertLessEqual(len(game.hazards), T.LIMITS.hazards)
        self.assertGreater(gaps, 0)
        self.assertGreater(beams_seen, 0)

    def test_desperation_descends_a_row_every_10_seconds_to_y_7(self):
        game = untouchable(staged(cfg(), 24, boss_phase=3, immortal=True))
        y0 = game.boss.y
        settle(game, 600)
        self.assertAlmostEqual(game.boss.y - y0, 600 * 0.0017, places=6)
        self.assertEqual(T.BOSSES["mothership"].phases[3].path[-1][1], 7.0)


class ReviewFixTests(unittest.TestCase):
    def test_a_boss_attack_holds_its_shots_against_the_cap_from_its_telegraph(self):  # review R1
        game = staged(cfg(), 12, boss_phase=2, phase="playing", immortal=True)
        game.aliens.clear()
        game._dclock = -1e9
        cap = T.LIMITS.enemy_shots
        for index in range(cap - 10):  # slow pellets high up: they stay on the field
            game.shots.append(Shot(3.0 + index, 0.5, False, 0.0, 0.0001, 1, game._new_id(), "pellet", 0))
        spray, spraying = attack(game, "C")
        eggs, laying = attack(game, "A")
        bosses._start(game, spray, spraying)
        self.assertEqual((spraying.stage, spraying.owed, director.load(game)), (1, 10, cap))
        bosses._start(game, eggs, laying)
        self.assertEqual((laying.stage, laying.owed), (0, 0))  # it waits: its 3 pellets don't fit
        game._dclock = 1e9
        self.assertFalse(director._decide(game))  # nor does a director volley
        pellets = 0
        for _ in range(200):
            game.step(())
            self.assertLessEqual(load(game), cap)
            self.assertLessEqual(director.load(game), cap)
            pellets += sum(1 for e in game.fired if e.source == "queen" and e.kind == "pellet")
        self.assertEqual(pellets, 10)  # the spray fired every pellet it held room for

    def test_the_hive_queen_never_passes_the_shot_cap(self):  # review R1's scenario, at the cap it was found at
        capped = replace(T.LIMITS, enemy_shots=24)
        with mock.patch.object(director, "LIMITS", capped), mock.patch.object(bosses, "LIMITS", capped):
            for seed in (1, 4):
                game = staged(cfg(seed=seed), 12, boss_phase=2, immortal=True,
                              director={"slots": 8, "reload": 20, "reaction": 0.0})  # fmt: skip
                for tick in range(5000):
                    game.step(MOVES[tick // 120 % 2])
                    if game.boss is None:
                        break
                    self.assertLessEqual(load(game), 24, (seed, game.tick))

    def test_after_the_mercy_clear_no_flightless_diver_is_left_in_formation(self):  # review R4
        game = untouchable(staged(Config("normal", "classic", 1), 12, boss_phase=1, lives=5))
        for _ in range(3000):
            game.step(())
            if any(a.kind == "diver" and a.flight < 0 and a.state == "telegraph" for a in game.aliens):
                break
        self.assertTrue(any(a.kind == "diver" and a.flight < 0 and a.state == "telegraph" for a in game.aliens))
        game.player.invulnerable = game.player.guard = game.player.shield_hits = 0
        game.shots.append(Shot(game.player.x, 17.5, False, 0.0, 0.2, 1, game._new_id(), "pellet", 1))
        game.step(())
        self.assertEqual((game.player.lives, game._freeze), (4, T.TIMING.freeze))
        settle(game, T.TIMING.freeze)
        for _ in range(600):
            self.assertEqual([a for a in game.aliens if a.flight < 0 and a.state == "formation"], [])
            game.step(())

    def test_hard_escort_variants_reach_the_field(self):  # review R6
        for profile, brood, armada in (("normal", 6, 16), ("hard", 8, 20)):
            with self.subTest(profile=profile):
                queen = staged(cfg(profile), 12, phase="playing", ticks=60)
                self.assertEqual(len(ring(queen)), brood)
                mothership = staged(cfg(profile), 24, phase="playing", ticks=5)
                self.assertEqual([s.size for s in mothership._flights], [armada])
                self.assertEqual(mothership._flight_row(("boss", 0, 1)).size, armada)
                self.assertEqual(sum(1 for a in mothership.aliens if a.flight == 0), armada)


class SecondReviewTests(unittest.TestCase):
    def test_a_cancelled_beam_leaves_the_boss_still_until_its_next_beam(self):  # EC3
        for cancel in ("bomb", "mercy"):
            game = untouchable(staged(cfg(), 18, boss_phase=1, immortal=True))
            _beam, state = attack(game, "C")
            for tick in range(3000):
                game.step(MOVES[tick // 150 % 2])
                if state.stage == 1 and state.track > 2:
                    break
            self.assertTrue(state.stage == 1 and state.track > 2, cancel)
            if cancel == "bomb":
                game.player.bombs = 1
                game.step(("bomb",))
            else:
                game._mercy()
            self.assertEqual([s.track for s in game._attacks], [0.0] * len(game._attacks), cancel)
            x, held = game.boss.x, 0
            for tick in range(600):
                game.step(MOVES[tick // 40 % 2])  # the ship weaves: a tracking boss would follow it
                if any(s.track > 0 for s in game._attacks):
                    break  # its next beam tracks again
                self.assertEqual(game.boss.x, x, (cancel, tick))
                held += 1
            self.assertGreater(held, T.TIMING.lockout, cancel)

    def test_only_the_spirals_first_pellets_count_its_telegraph(self):  # EC4
        for difficulty in ("normal", "hard"):
            game = untouchable(staged(cfg(difficulty), 24, boss_phase=3, immortal=True))
            game._fire = replace(game._fire, reaction=0.0)  # every pellet safe: W >= 60
            game.boss.y = 7.0  # the foot of its descent: the shortest fall
            _spiral, state = attack(game, "H")
            first, later = [], []
            for _ in range(1200):
                shot = state.shot
                game.step(())
                pellets = [e for e in game.fired if e.kind == "pellet" and e.source == "core"]
                (first if shot == 0 else later).extend(pellets)
            self.assertTrue(first and len(later) > 50, difficulty)
            self.assertTrue(all(e.w == e.flight + state.tele for e in first), difficulty)
            for entry in later:  # no telegraph left to count: the pellet's own fall gives its W
                self.assertEqual(entry.w, entry.flight, difficulty)
                self.assertGreaterEqual(entry.flight, T.LIMITS.safe_warning, difficulty)

    def test_a_continue_brings_the_boss_down_over_its_shorter_intro(self):  # EC6
        for wave in (6, 12, 18, 24):
            row = T.BOSSES[T.WAVES[wave - 1].boss]
            again = staged(cfg("easy"), wave, phase="continue", ticks=T.TIMING.arm)
            again.step(("fire",))
            for game, ticks in ((staged(cfg("easy"), wave), T.TIMING.boss_intro), (again, T.TIMING.restart)):
                self.assertEqual((game.phase, game._clock, game.boss.timer), ("wave", ticks, ticks), wave)
                ys = [game.boss.y]
                while game.phase == "wave":
                    game.step(())
                    ys.append(game.boss.y)
                self.assertEqual(len(ys) - 1, ticks, wave)
                self.assertEqual(ys[0], row.station[1] - row.descent, wave)
                steps = [b - a for a, b in zip(ys, ys[1:])]
                self.assertLessEqual(max(steps), row.descent / ticks + 1e-9, (wave, ticks))  # no jump at the end
                self.assertAlmostEqual(ys[-1], row.station[1], msg=(wave, ticks))  # at its station as the fight starts

    def test_no_boss_curtain_pellet_starts_in_its_gap(self):  # TQ1
        for wave, phase in ((18, 0), (24, 2)):
            game = staged(cfg(), wave, phase="playing", boss_phase=phase, immortal=True)
            curtain, state = attack(game, "B")
            self.assertEqual(curtain.aim, "curtain")
            half = curtain.gap / 2
            low, high = T.LIMITS.gap_x
            checked = 0
            for tenths in range(int((low + half) * 10), int((high - half) * 10) + 1, 5):
                state.gap = tenths / 10
                for x in range(2, 58):
                    game.player.x = float(x)
                    xs = bosses._curtain(game, curtain, state)
                    self.assertEqual(len(xs), curtain.count, (wave, state.gap, x))
                    for pellet in xs:
                        self.assertGreaterEqual(abs(pellet - state.gap), half + 0.5 - 1e-9, (wave, state.gap, x))
                    checked += len(xs)
            self.assertGreater(checked, 5000)


if __name__ == "__main__":
    unittest.main()
