"""The fairness contract (spec §4) and the determinism rules it rests on (§2.2).

* The AST check of §2.2 over every campaign module.
* F1-F8 as property tests (§13 rows f and g): by default 3 seeds x 24 waves x 3 profiles x 3,000 ticks, each wave
  staged (immortal) and played by a scripted mover that sweeps left and right and holds fire, so it fires whenever a
  slot frees, then each boss's later phases staged the same way. Every entry of `fired` (shots, dives and mites)
  and every tick is checked: the logged W and telegraph, how long the tell really lasted (from the tick it began),
  and that no curtain pellet starts in its own gap. A `slow`-marked tier runs 20 seeds (the boss phases too),
  Endless waves 25-60 (loops 2 and 3: 1 to 36 modifiers, the whole ladder four times over) and Endless waves 199-216
  (a whole loop's 18 waves with every modifier stacked 21 times or more: most sit at their caps); the gate runs it
  with `-m slow`.
* F2 for a curtain from a COLUMNS flight whose front row never fires (divers), which the sweep meets too rarely.
"""

import ast
import unittest
from pathlib import Path

import pytest

import terminal_invaders.campaign as campaign
from terminal_invaders.campaign import boss as bosses
from terminal_invaders.campaign import director, fleet
from terminal_invaders.campaign.tables import ENEMIES, LIMITS, TIMING, Flight
from terminal_invaders.campaign.testing import staged
from terminal_invaders.model import Config

CAMPAIGN_DIR = Path(campaign.__file__).parent
BANNED_NAMES = ("random", "hash", "id", "round", "set", "frozenset")
MATH_ALLOWED = ("floor", "ceil")


def violations(source: str, filename: str) -> list[str]:
    """§2.2's rules: from math only floor and ceil; no random, hash or id; no round(); no set literal, set
    comprehension, set() or frozenset() call. Membership tests in a given frozenset are fine."""
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        where = f"{filename}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "random" or alias.name.startswith("random."):
                    found.append(f"{where}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "random":
                found.append(f"{where}: from random import ...")
            if node.module == "math":
                found += [f"{where}: from math import {a.name}" for a in node.names if a.name not in MATH_ALLOWED]
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "math":
            if node.attr not in MATH_ALLOWED:
                found.append(f"{where}: math.{node.attr}")
        elif isinstance(node, (ast.Set, ast.SetComp)):
            found.append(f"{where}: a set literal or comprehension")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_NAMES:
            found.append(f"{where}: {node.func.id}()")
        elif isinstance(node, ast.Name) and node.id == "random" and isinstance(node.ctx, ast.Load):
            found.append(f"{where}: random")
    return found


class DeterminismRulesTests(unittest.TestCase):
    def test_every_campaign_module_follows_the_determinism_rules(self):
        modules = sorted(CAMPAIGN_DIR.glob("*.py"))
        self.assertIn("game.py", [path.name for path in modules])
        for path in modules:
            with self.subTest(module=path.name):
                self.assertEqual(violations(path.read_text(encoding="utf-8"), path.name), [])

    def test_the_check_catches_what_a_grep_would_miss(self):
        bad = (
            "from math import sin\nimport math\nx = math.sqrt(2)\ny = round(1.5)\ns = {1, 2}\nt = {v for v in ()}\n"
            "u = set()\nv = frozenset(())\nw = hash(1)\nz = id(x)\nimport random\n"
        )
        found = violations(bad, "bad.py")
        self.assertEqual(len(found), 10, found)
        ok = (
            "import math\nfrom math import floor\nx = math.floor(1.5) + math.ceil(0.5)\nreset = 1\ny = 'a' in ACTIONS\n"
        )
        self.assertEqual(violations(ok, "ok.py"), [])


# --- F1-F8 as property tests -----------------------------------------------------------------------------------------

PROFILES = ("easy", "normal", "hard")
SEEDS = (7, 1234, 90210)
SLOW_SEEDS = tuple(range(101, 121))
STACKED = range(199, 217)  # Endless loops 11-12, 18 waves: 175-192 modifiers, every one stacked at least 21 times
BOSS_PHASES = ((6, 1), (12, 1), (12, 2), (18, 1), (18, 2), (24, 1), (24, 2), (24, 3))  # each boss's later phases
TICKS = 3_000
EPS = 1e-9
MOVES = (("fire", "right"), ("fire", "left"))  # 150 ticks each way
SHOT_POSES = ("formation", "telegraph", "")  # a boss part logs ""
NO_FIRE = ("diving", "entering", "returning")


def fired_breaks(entry) -> list[str]:
    """F1, F2 and F3 for one entry of `fired`."""
    found = []
    if entry.w < LIMITS.warning or entry.flight < LIMITS.flight:
        found.append(f"W {entry.w} / flight {entry.flight} under the floor (F1)")
    if entry.y > LIMITS.spawn_y + EPS:
        found.append(f"spawned at y {entry.y} (F2)")
    if entry.kind == "dive":
        if entry.w < LIMITS.dive_warning:
            found.append(f"dive W {entry.w} < {LIMITS.dive_warning} (F1)")
    elif entry.kind == "mite":
        if entry.w < LIMITS.mite_warning:
            found.append(f"mite W {entry.w} < {LIMITS.mite_warning} (F1)")
    else:
        if entry.pose in NO_FIRE or entry.pose not in SHOT_POSES or entry.source in ("diver", "mite"):
            found.append(f"fired by a {entry.pose or 'boss'} {entry.source} (F2)")
        telegraph = entry.w - entry.flight
        if 0 < telegraph < LIMITS.telegraph:
            found.append(f"telegraph {telegraph} < {LIMITS.telegraph} (F3)")
        if entry.cls not in ("safe", "reaction", "boss"):
            found.append(f"class {entry.cls!r}")
    return found


def widest_lane(beams) -> float:
    """The widest stretch of the field no beam covers."""
    low, high = LIMITS.field
    widest, edge = 0.0, low
    for beam in sorted(beams, key=lambda h: h.x0):
        widest = max(widest, beam.x0 - edge)
        edge = max(edge, beam.x1)
    return max(widest, high - edge)


def timers(game) -> tuple:
    """The gameplay timers F7 freezes outside `playing`."""
    return (
        tuple(sorted(game.powerups.items())),
        game.player.invulnerable,
        game._dclock,
        game._dive_clock,
        game._lockout,
        game._freeze,
    )


def tell_began(game, entry, began, shots) -> int | None:
    """The tick the tell behind a `fired` entry began, or None when it can't be told: a dive or mite from its enemy's
    move into the telegraph pose, a director shot from its volley's start, a boss shot from the earliest start among
    the phase's attacks that fire that kind from that part (lenient where two could have fired it)."""
    if entry.kind in ("dive", "mite"):
        return began.get(("enemy", entry.id))
    if entry.cls == "boss":
        boss = game.boss
        if boss is None:
            return None
        attacks = bosses.attacks_of(boss.kind, boss.phase, game.config.difficulty)
        starts = [
            began[("attack", boss.phase, index)]
            for index, attack in enumerate(attacks)
            if attack.source == entry.source and attack.shot == entry.kind and ("attack", boss.phase, index) in began
        ]
        return min(starts) if starts else None
    shot = shots.get(entry.id)
    return began.get(("volley", shot.volley)) if shot is not None else None


def sweep(config, wave, ticks=TICKS, boss_phase=0) -> tuple[list[str], dict]:
    """Play one staged wave (a boss wave from `boss_phase`) with the scripted mover and return (rule breaks, counts)."""
    game = staged(config, wave, boss_phase=boss_phase, immortal=True)
    found, counts = [], {"fired": 0, "hits": 0, "beats": 0, "timed": 0, "curtain": 0, "boss curtain": 0}
    beats, gaps, locked, mercy_due, quiet_until = {}, [], {}, False, -1
    began, poses, stages, curtains = {}, {}, {}, {}  # when each tell began; last tick's poses and attack stages

    def broke(text):
        if len(found) < 10:
            where = f"wave {wave} phase {boss_phase}" if boss_phase else f"wave {wave}"
            found.append(f"{config.difficulty} {config.mode} seed {config.seed} {where} tick {game.tick}: {text}")

    for tick in range(ticks):
        before, held = game.phase, timers(game)
        game.step(MOVES[(tick // 150) % 2])
        playing = before == "playing"
        for volley in game._volleys:  # a director volley's tell begins when it is made
            began.setdefault(("volley", volley.id), game.tick)
            if volley.pattern == "curtain":
                curtains.setdefault(volley.id, volley.gap)
        for alien in game.aliens:  # a dive's or a mite's when its enemy starts to wobble
            if alien.state == "telegraph" and poses.get(alien.id) != "telegraph":
                began[("enemy", alien.id)] = game.tick
        poses = {alien.id: alien.state for alien in game.aliens}
        left_tell = []  # boss curtains whose telegraph ended this tick: their pellets fire now
        if game.boss is not None and game.boss.state != "intro":  # its attacks are set up when the intro ends
            phase = game.boss.phase
            attacks = bosses.attacks_of(game.boss.kind, phase, config.difficulty)
            for index, (attack, state) in enumerate(zip(attacks, game._attacks, strict=True)):
                key = ("attack", phase, index)
                if state.stage == 1 and stages.get(key) != 1:  # a boss attack's when it leaves stage 0
                    began[key] = game.tick
                elif state.stage != 1 and stages.get(key) == 1 and attack.aim == "curtain":
                    left_tell.append((attack, state))
            stages = {("attack", phase, index): state.stage for index, state in enumerate(game._attacks)}
        shots = {shot.id: shot for shot in game.shots if not shot.friendly} if game.fired else {}
        if not playing and game.fired:
            broke(f"fired outside playing ({before}) (F7)")
        if not playing and game.phase == before and timers(game) != held:
            broke(f"a timer ran in {before} (F7)")
        for entry in game.fired:
            counts["fired"] += 1
            for text in fired_breaks(entry):
                broke(f"{entry.cls} {entry.kind} from a {entry.source}: {text}")
            if game.tick <= quiet_until:
                broke(f"{entry.kind} fired {quiet_until - game.tick} ticks before the lockout ended (F6)")
            start = tell_began(game, entry, began, shots)
            if start is not None:  # the real tell, not the logged one (second review: TQ2)
                counts["timed"] += 1
                if game.tick - start < entry.w - entry.flight:
                    broke(f"{entry.kind} from a {entry.source}: a {game.tick - start}-tick tell, logged as "
                          f"{entry.w - entry.flight} (F1, F3, F9)")  # fmt: skip
            shot = shots.get(entry.id)
            if shot is None:
                continue
            if shot.volley in curtains:  # a curtain's pellets keep out of its own gap (second review: TQ1)
                counts["curtain"] += 1
                low, high = curtains[shot.volley]
                if low < shot.x < high:
                    broke(f"a curtain pellet at x {shot.x:.2f} inside its gap {low:.2f}..{high:.2f} (F4)")
            for attack, state in left_tell:
                if entry.cls == "boss" and entry.source == attack.source and entry.kind == attack.shot:
                    counts["boss curtain"] += 1
                    if abs(shot.x - state.gap) < attack.gap / 2:
                        broke(f"a boss curtain pellet at x {shot.x:.2f} inside its gap at {state.gap:.2f} (F4)")
        damage = game.events.count("damage")
        if damage > 1:
            broke(f"{damage} lives lost in one tick (F6)")
        if damage:
            counts["hits"] += 1
            if game._freeze != TIMING.freeze:
                broke(f"freeze {game._freeze} after a hit (F6)")
            mercy_due, quiet_until = True, game.tick + TIMING.freeze + TIMING.lockout
        elif mercy_due and game.phase == "playing" and not game._freeze:
            mercy_due = False
            if any(not shot.friendly for shot in game.shots) or game.hazards:
                broke("enemy shots or hazards survived the mercy clear (F6)")
            if game.player.invulnerable != TIMING.safe or game._lockout != TIMING.lockout:
                broke(f"invulnerable {game.player.invulnerable}, lockout {game._lockout} after the mercy clear (F6)")
            if any(alien.state == "telegraph" for alien in game.aliens):
                broke("a telegraph survived the mercy clear (F6)")
        for shot in game.shots:
            if not shot.friendly and shot.vy > LIMITS.vy + EPS:
                broke(f"enemy {shot.kind} at vy {shot.vy} (F8)")
        if game.phase == "playing" and game.wave_name and director.reload_eff(game) < LIMITS.reload:
            broke(f"reload {director.reload_eff(game)} (F8)")
        if playing:  # a MARCH flight's clock drops back to 0 on its beat
            for state in game._flights:
                if state.movement != "MARCH":
                    continue
                last, clock = beats.get(state.index, (None, state.clock))
                if state.clock == 0.0 and clock > 0.0:
                    if last is not None:
                        counts["beats"] += 1
                        if game.tick - last < LIMITS.beat:
                            broke(f"a march beat {game.tick - last} ticks after the last (F8)")
                    last = game.tick
                beats[state.index] = (last, state.clock)
        beams = [hazard for hazard in game.hazards if hazard.kind == "beam"]
        for beam in beams:
            if beam.state == "lock":
                locked.setdefault(beam.id, game.tick)
            elif beam.state == "live" and beam.id in locked:
                if game.tick - locked.pop(beam.id) < LIMITS.beam_lock:
                    broke("a beam went live under 36 ticks after its x locked (F1)")
        if beams and widest_lane(beams) < LIMITS.lane - EPS:
            broke(f"beams leave a lane of {widest_lane(beams):.1f} (F4)")
        for hazard in game.hazards:
            if hazard.kind == "gap" and hazard.id not in gaps:
                gaps.append(hazard.id)
                low, high = LIMITS.gap_x
                centre = (hazard.x0 + hazard.x1) / 2
                if hazard.x1 - hazard.x0 < LIMITS.gap - EPS or hazard.x0 < low - EPS or hazard.x1 > high + EPS:
                    broke(f"a curtain gap {hazard.x0}..{hazard.x1} (F4)")
                if abs(centre - game.player.x) > LIMITS.gap_reach + EPS:
                    broke(f"a curtain gap centred {abs(centre - game.player.x):.1f} cells from the ship (F4)")
        if game.phase in ("clear", "victory", "game_over"):
            break
    return found, counts


def run_sweep(case, seeds, waves, mode="classic"):
    """Every (profile, seed, wave) of the sweep, a wave given as (wave, boss phase) staged at that phase; fails with
    the first breaks found."""
    found, totals = [], {"fired": 0, "hits": 0, "beats": 0, "timed": 0, "curtain": 0, "boss curtain": 0}
    for difficulty in PROFILES:
        for seed in seeds:
            for wave in waves:
                wave, phase = wave if isinstance(wave, tuple) else (wave, 0)
                breaks, counts = sweep(Config(difficulty, mode, seed), wave, boss_phase=phase)
                found += breaks
                for key, value in counts.items():
                    totals[key] += value
    case.assertEqual(found[:20], [])
    return totals


class FairnessSweepTests(unittest.TestCase):
    def test_every_threat_meets_f1_to_f8_over_the_default_sweep(self):
        totals = run_sweep(self, SEEDS, (*range(1, 25), *BOSS_PHASES))  # every boss phase too (the queen's divers)
        # the sweep exercised what it checks: shots, dives, mites, hits (F6), march beats (F8), real tells and the
        # pellets of director and boss curtains
        self.assertGreater(totals["fired"], 10_000)
        self.assertGreater(totals["hits"], 100)
        self.assertGreater(totals["beats"], 1_000)
        self.assertGreater(totals["timed"], 0.95 * totals["fired"])
        self.assertGreater(totals["curtain"], 100)
        self.assertGreater(totals["boss curtain"], 100)

    @pytest.mark.slow
    def test_twenty_seeds_and_endless(self):
        run_sweep(self, SLOW_SEEDS, (*range(1, 25), *BOSS_PHASES))
        run_sweep(self, SEEDS, range(25, 61), mode="endless")
        run_sweep(self, SEEDS, STACKED, mode="endless")


class CurtainShooterTests(unittest.TestCase):
    def test_no_curtain_is_fired_by_an_enemy_that_never_fires(self):  # F2, F1
        """A COLUMNS flight whose front row is divers and whose back row is lancers, with only curtains drawn and no
        dives: every curtain comes from the lancers, and no diver fires or dives. Endless seed 6003, wave 93 (loop
        5's wave 21) fired a curtain pellet from a diver in formation, the only shot outside the rules in the 2.8
        million of tuning round 3's run: waves 21 and 23 have COLUMNS flights with divers, so once the enemies in
        front of a diver die, it is its column's front. The sweep above meets that about once in 700 whole games.
        Low (front row at y 9.5) such a diver fired the curtain's pellet; high (y 3.0) the curtain's tell started
        its dive, after a 1-tick wobble logged as 30."""
        curtains_only = {"single": 0, "burst": 0, "crossfire": 0, "curtain": 100}
        for difficulty in PROFILES:
            for wave in (21, 23):
                for top in (2.0, 8.5):
                    where = (difficulty, wave, top)
                    game = staged(Config(difficulty, "classic", 7), wave, phase="playing", immortal=True,
                                  director=curtains_only)  # fmt: skip
                    game.aliens.clear()
                    game._pending, game._flights, game._next_flight = [], [], 0  # this flight alone
                    game.bunkers.clear()
                    game._dive_clock = -1e9  # no dive of its own: the divers hold their places
                    state = fleet.place(game, Flight(0, 14, "rect", (2, 7), "COLUMNS", (("lancer", 1),)), top)
                    members = [alien for alien in game.aliens if alien.flight == state.index]
                    front = max(alien.home_y for alien in members)
                    for alien in members:
                        kind = "diver" if alien.home_y == front else "lancer"
                        alien.kind, alien.hp, alien.max_hp = kind, ENEMIES[kind].hp, ENEMIES[kind].hp
                        alien.state, alien.x, alien.y = "formation", alien.home_x, alien.home_y
                    found, sources, curtains, pellets = [], [], {}, 0
                    for tick in range(900):
                        game.step((("right",), ("left",))[(tick // 150) % 2])  # sweeps, holding fire
                        for volley in game._volleys:
                            if volley.pattern == "curtain" and volley.id not in curtains:
                                curtains[volley.id] = True
                                kinds = [alien.kind for alien in director.volley_aliens(game, volley)]
                                self.assertTrue(all(ENEMIES[kind].shot for kind in kinds), (*where, kinds))
                        shots = {shot.id: shot for shot in game.shots if not shot.friendly}
                        for entry in game.fired:
                            found += [f"{where} tick {game.tick}: {text}" for text in fired_breaks(entry)]
                            sources.append(entry.source)
                            shot = shots.get(entry.id)
                            pellets += shot is not None and shot.volley in curtains
                    self.assertEqual(found[:10], [])
                    self.assertEqual(sorted(set(sources)), ["lancer"], where)
                    self.assertGreater(len(curtains), 5, where)  # the row behind the divers fires them
                    self.assertGreater(pellets, 25, where)


class SweepCheckTests(unittest.TestCase):
    """The checks catch what they are for."""

    def entry(self, **changes):
        base = campaign.game.Fired(1, "pellet", "safe", 60, 40, 13.2, "formation", "gunner")
        return base._replace(**changes)

    def test_a_fair_entry_passes(self):
        self.assertEqual(fired_breaks(self.entry()), [])
        self.assertEqual(fired_breaks(self.entry(kind="dive", cls="dive", w=70, pose="telegraph", source="diver")), [])
        self.assertEqual(fired_breaks(self.entry(kind="mite", cls="mite", w=60, pose="telegraph", source="mite")), [])
        self.assertEqual(fired_breaks(self.entry(w=58, flight=40)), [])  # an 18-tick telegraph

    def test_unfair_entries_are_caught(self):
        for changes in (
            {"w": 29},
            {"flight": 17},
            {"y": 13.3},
            {"pose": "diving"},
            {"pose": "entering"},
            {"source": "mite"},
            {"w": 50, "flight": 40},  # a 10-tick telegraph
            {"kind": "dive", "cls": "dive", "w": 69},
            {"kind": "mite", "cls": "mite", "w": 59},
        ):
            with self.subTest(changes=changes):
                self.assertTrue(fired_breaks(self.entry(**changes)))

    def test_the_lane_measure(self):
        class Beam:
            def __init__(self, x0, x1):
                self.x0, self.x1 = x0, x1

        self.assertEqual(widest_lane([Beam(2.0, 30.0), Beam(36.0, 57.0)]), 6.0)
        self.assertEqual(widest_lane([Beam(10.0, 13.0)]), 44.0)


if __name__ == "__main__":
    unittest.main()
