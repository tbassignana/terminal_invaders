"""The Campaign's content tables (spec §3.4, §20.1).

Every table's name, shape and key set, the numbers the spec gives (§5-§12), and `campaign.validate`: the arrival
rule, the warning floors, pacing and the run-length estimates, plus checks that validate catches broken content.
"""

import dataclasses
import unittest
from dataclasses import replace
from types import MappingProxyType
from unittest import mock

from terminal_invaders import model
from terminal_invaders.campaign import boss as bosses
from terminal_invaders.campaign import tables as T
from terminal_invaders.campaign import validate

MAPPINGS = ("SHOTS", "ENEMIES", "SHAPES", "FORMATIONS", "PATTERNS", "TELEGRAPHS", "PICKUPS", "UPGRADES", "PROFILES")
MAPPINGS += ("BOSSES",)
SEQUENCES = ("ACTS", "WAVES")
SINGLE_ROWS = ("PLAYER", "FIRE", "DIVE", "ARRIVAL", "PICKUP", "BOMB", "COMBO", "SCORING", "UFO", "SHELTERS", "TIMING")
SINGLE_ROWS += ("ENDLESS", "SALTS", "LIMITS")
SPEC_TABLES = ("PLAYER", "FIRE", "SHOTS", "ENEMIES", "FORMATIONS", "PATTERNS", "TELEGRAPHS", "ACTS", "WAVES", "BOSSES")
SPEC_TABLES += ("PICKUPS", "UPGRADES", "BOMB", "COMBO", "SCORING", "UFO", "SHELTERS", "TIMING", "PROFILES", "ENDLESS")
SPEC_TABLES += ("SALTS", "LIMITS")  # §3.4's list

ENEMY_KINDS = (
    "scout",
    "diver",
    "gunner",
    "weaver",
    "shell",
    "lancer",
    "bomber",
    "splitter",
    "carrier",
    "mite",
    "guard",
)
SHOT_KINDS = ("pellet", "dart", "hunter", "needle", "slug", "cluster")
# §8.2: enemies per regular wave and HP with mites (and Normal's veteran plating on waves 21-23)
ENEMIES_PER_WAVE = {1: 40, 2: 68, 3: 66, 4: 82, 5: 84, 7: 90, 8: 96, 9: 105, 10: 105, 11: 108, 13: 108, 14: 115}
ENEMIES_PER_WAVE |= {15: 108, 16: 115, 17: 115, 19: 103, 20: 115, 21: 106, 22: 113}
ENEMIES_PER_WAVE[23] = 118  # §8.2 has 121: its COLUMNS flights hold 14 in 2 rows (7 columns at most), so none waits
LAST_ARRIVAL_S = {1: 47, 2: 52.5, 3: 54, 4: 56, 5: 56.5, 7: 55.5, 8: 57, 9: 58, 10: 58, 11: 59.5, 13: 58.5, 14: 59.5}
LAST_ARRIVAL_S |= {15: 60, 16: 58.5, 17: 59, 19: 60, 20: 58, 21: 59.5, 22: 62, 23: 59}
BOSS_HP = {"warden": 582, "hive_queen": 657, "dreadnought": 716, "mothership": 408}
CARD_WAVES = (3, 6, 9, 12, 15, 18, 21)
IMMUTABLE = (int, float, str, bool, type(None))


def immutable(value):
    """True for plain values, tuples of them, and frozen dataclass rows made of them."""
    if isinstance(value, IMMUTABLE):
        return True
    if isinstance(value, tuple):
        return all(immutable(item) for item in value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        params = type(value).__dataclass_params__
        return (
            params.frozen
            and hasattr(type(value), "__slots__")
            and all(immutable(getattr(value, f.name)) for f in dataclasses.fields(value))
        )
    return False


def rows(name):
    table = getattr(T, name)
    if isinstance(table, MappingProxyType):
        return list(table.values())
    if isinstance(table, tuple):
        return list(table)
    return [table]


class ShapeTests(unittest.TestCase):
    def test_every_table_of_section_3_4_exists_with_its_container(self):
        for name in SPEC_TABLES:
            self.assertTrue(hasattr(T, name), name)
        for name in MAPPINGS:
            self.assertIsInstance(getattr(T, name), MappingProxyType, name)
        for name in SEQUENCES:
            self.assertIsInstance(getattr(T, name), tuple, name)
        for name in SINGLE_ROWS:
            table = getattr(T, name)
            self.assertTrue(dataclasses.is_dataclass(table) and not isinstance(table, type), name)

    def test_every_row_is_frozen_slotted_and_deeply_immutable(self):
        for name in MAPPINGS + SEQUENCES + SINGLE_ROWS:
            for row in rows(name):
                with self.subTest(table=name, row=getattr(row, "name", getattr(row, "kind", row))):
                    self.assertTrue(immutable(row))
                    if dataclasses.is_dataclass(row):
                        with self.assertRaises(dataclasses.FrozenInstanceError):
                            setattr(row, dataclasses.fields(row)[0].name, None)

    def test_mappings_are_keyed_by_their_rows_names(self):
        for name in MAPPINGS:
            for key, row in getattr(T, name).items():
                if name == "TELEGRAPHS":
                    self.assertIsInstance(row, int)
                    continue
                label = getattr(row, "kind", None) if name in ("SHOTS", "ENEMIES", "PICKUPS", "BOSSES") else row.name
                self.assertEqual(key, label, f"{name}[{key!r}]")


class ContentTests(unittest.TestCase):
    def test_kinds_match_the_model_and_the_art_keys(self):
        self.assertEqual(tuple(T.ENEMIES), ENEMY_KINDS)
        self.assertEqual(tuple(T.SHOTS), SHOT_KINDS)
        self.assertEqual(tuple(T.PICKUPS), model.PICKUPS)
        self.assertEqual(tuple(T.PROFILES), tuple(model.DIFFICULTIES))
        for enemy in T.ENEMIES.values():
            self.assertIn(enemy.shot, ("", *T.SHOTS), enemy.kind)
            self.assertIn(enemy.rows, ("front", "mid", "back", "any"), enemy.kind)
        self.assertEqual([kind for kind, row in T.ENEMIES.items() if not row.new], ["mite", "guard"])

    def test_waves(self):
        self.assertEqual(len(T.WAVES), model.CAMPAIGN_WAVES)
        self.assertEqual(model.CAMPAIGN_WAVES, 24)
        self.assertEqual([wave.number for wave in T.WAVES], list(range(1, 25)))
        self.assertEqual(tuple(wave.number for wave in T.WAVES if wave.card), CARD_WAVES)
        self.assertEqual({wave.number: wave.boss for wave in T.WAVES if wave.boss}, {6: "warden", 12: "hive_queen"} | {
            18: "dreadnought", 24: "mothership"})  # fmt: skip
        for wave in T.WAVES:
            with self.subTest(wave=wave.number):
                self.assertLessEqual(len(wave.line), 52)
                self.assertTrue(wave.line.isascii())
                director = wave.director
                self.assertLessEqual(director.slots, T.LIMITS.slots)
                self.assertGreaterEqual(director.reload, T.LIMITS.reload)
                self.assertLessEqual(director.v_a, T.LIMITS.vy)
                for pattern in T.PATTERNS.values():
                    if getattr(director, pattern.name):
                        self.assertGreaterEqual(wave.number, pattern.first, pattern.name)
                if wave.boss:
                    self.assertEqual((wave.flights, wave.cargo, wave.ufo), ((), -1, False))
                    self.assertEqual(T.BOSSES[wave.boss].name, wave.name)
                    continue
                self.assertEqual(sum(flight.size for flight in wave.flights), ENEMIES_PER_WAVE[wave.number])
                self.assertEqual(wave.flights[-1].at, round(LAST_ARRIVAL_S[wave.number] * 60))
                self.assertEqual(wave.flights[0].at, 0)
                self.assertEqual([f.at for f in wave.flights], sorted(f.at for f in wave.flights))
                self.assertEqual(wave.ufo, wave.number >= T.UFO.first)
                self.assertEqual(wave.cargo == -1, wave.number == 1)
                self.assertLess(wave.cargo, len(wave.flights))
                for flight in wave.flights:
                    self.assertIn(flight.movement, T.FORMATIONS)
                    shape = T.SHAPES[flight.shape]
                    self.assertLessEqual(sum(count for _kind, count in flight.mix), flight.size)
                    for kind, count in flight.mix:
                        self.assertIn(kind, T.ENEMIES)
                        self.assertGreater(count, 0)
                        self.assertLessEqual(T.ENEMIES[kind].first, wave.number, kind)
                    if flight.shape in ("rect", "checker"):
                        rows_, cols = flight.dims
                        self.assertLessEqual((rows_, cols), (shape.rows, shape.cols))
                        self.assertLessEqual(cols, shape.cols)
                        self.assertLessEqual(flight.size, rows_ * cols)
                    elif flight.shape == "line":
                        self.assertEqual(flight.dims, (flight.size,))
                    else:
                        self.assertIn(flight.dims[0], shape.sizes)
                        self.assertEqual(flight.size, flight.dims[0])
        self.assertEqual(T.WAVES[19].squadron, 3)

    def test_acts_cover_the_waves_and_end_with_their_bosses(self):
        self.assertEqual([act.number for act in T.ACTS], [1, 2, 3, 4])
        self.assertEqual([(act.first, act.last) for act in T.ACTS], [(1, 6), (7, 12), (13, 18), (19, 24)])
        for act in T.ACTS:
            self.assertEqual(T.WAVES[act.last - 1].boss, act.boss)
            self.assertEqual(T.BOSSES[act.boss].wave, act.last)
        self.assertEqual([(act.divers, act.dive_every) for act in T.ACTS], [(1, 360), (2, 300), (2, 270), (3, 240)])

    def test_bosses(self):
        self.assertEqual(tuple(T.BOSSES), ("warden", "hive_queen", "dreadnought", "mothership"))
        for key, boss in T.BOSSES.items():
            with self.subTest(boss=key):
                self.assertTrue(2 <= len(boss.phases) <= 4)
                part_names = {part.name for part in boss.parts}
                total = 0
                for part in boss.parts:
                    total += sum(phase.hp for phase in boss.phases if part.name in phase.vulnerable)
                self.assertEqual(total, BOSS_HP[key])
                for phase in boss.phases:
                    self.assertLessEqual(set(phase.vulnerable), part_names)
                    for attack in phase.attacks:
                        self.assertIn(attack.source, part_names)
                        self.assertIn(attack.shot, ("beam", "spawn", *T.SHOTS))
                        self.assertLessEqual(set(attack.profiles), set(T.PROFILES))
                        if attack.shot not in ("spawn",):
                            self.assertGreaterEqual(attack.telegraph, 24)
                        if attack.shot == "beam":
                            self.assertGreaterEqual(attack.telegraph - attack.track, T.LIMITS.beam_lock)
                        if attack.aim == "curtain":
                            self.assertGreaterEqual(attack.gap, T.LIMITS.gap)
                attack_fields = {f.name for f in dataclasses.fields(T.Attack)}
                for variant in boss.variants:
                    self.assertIn(variant.profile, T.PROFILES)
                    self.assertTrue(-1 <= variant.phase < len(boss.phases))
                    if variant.attack not in ("", "escort"):
                        self.assertIn(variant.field, attack_fields)
                        phases = boss.phases if variant.phase == -1 else (boss.phases[variant.phase],)
                        self.assertTrue(any(a.key == variant.attack for p in phases for a in p.attacks), variant)
        self.assertEqual([boss.life for boss in T.BOSSES.values()], [1, 1, 1, 0])

    def test_upgrades_pickups_and_profiles(self):
        self.assertEqual(len(T.UPGRADES), 12)
        self.assertEqual(sum(up.stacks for up in T.UPGRADES.values()), 15)
        self.assertEqual(T.FAMILIES, ("offense", "defense", "utility"))
        self.assertEqual([up.family for up in T.UPGRADES.values()], ["offense"] * 3 + ["defense"] * 4 + ["utility"] * 5)
        self.assertEqual(len({up.code for up in T.UPGRADES.values()}), 12)
        for up in T.UPGRADES.values():
            self.assertEqual(len(up.priority), up.stacks, up.name)
            self.assertGreater(up.weight, 0)
            self.assertTrue(all(len(line) <= 13 and line.isascii() for line in up.blurb), up.name)
        ranks = sorted(rank for up in T.UPGRADES.values() for rank in up.priority)
        self.assertEqual(ranks, list(range(1, 16)))
        self.assertEqual(sum(p.weight for p in T.PICKUPS.values()), 100)
        self.assertEqual(len({p.letter for p in T.PICKUPS.values()}), 6)
        for name, profile in T.PROFILES.items():
            with self.subTest(profile=name):
                self.assertEqual((profile.lives, profile.speed), model.DIFFICULTIES[name])
                self.assertLessEqual(len(profile.line), 52)
                low, high = T.LIMITS.pressure
                self.assertTrue(low <= profile.pressure <= high)
                self.assertIn(profile.shelter_wave, ("rebuild", "patch", "none"))
                self.assertIn(profile.aim, ("tell", "fire"))
        self.assertEqual([p.cap for p in T.PROFILES.values()], [5, 5, 4])
        self.assertEqual([p.bombs for p in T.PROFILES.values()], [2, 1, 1])
        self.assertEqual([p.continues for p in T.PROFILES.values()], [1, 0, 0])
        self.assertEqual([p.training for p in T.PROFILES.values()], [(1, 2, 3), (1,), ()])

    def test_numbers_the_spec_pins(self):
        self.assertEqual((T.PLAYER.y, T.PLAYER.start_x, T.PLAYER.speed, T.PLAYER.box), (18.0, 30.0, 0.40, (1.1, 0.4)))
        self.assertEqual((T.FIRE.slots, T.FIRE.gate, T.FIRE.muzzle_y, T.FIRE.speed), (2, 8, 17.0, 0.55))
        self.assertEqual((T.BOMB.cap, T.BOMB.per_act, T.BOMB.lock, T.BOMB.invulnerable), (2, 1, 60, 45))
        self.assertEqual((T.BOMB.charge, T.BOMB.first_act), (1200, 2))  # B-F: open question 9, step 1
        self.assertEqual((T.COMBO.step, T.COMBO.most), (3, 6))
        timing = T.TIMING
        self.assertEqual((timing.intro, timing.act_intro, timing.boss_intro, timing.clear), (90, 180, 150, 90))
        self.assertEqual(
            (timing.arm, timing.prompt, timing.freeze, timing.safe, timing.lockout), (30, 600, 60, 120, 60)
        )
        limits = T.LIMITS
        self.assertEqual((limits.enemies, limits.hazards, limits.shelter_cells, limits.slots), (72, 6, 64, 8))
        self.assertEqual((limits.enemy_shots, limits.reload), (32, 16))  # B-F: open question 9, step 2
        self.assertEqual((limits.pickups, limits.effects), (model.MAX_PICKUPS, model.MAX_EFFECTS))
        self.assertEqual((limits.warning, limits.flight, limits.telegraph), (30, 18, 18))
        self.assertEqual((limits.dive_warning, limits.mite_warning, limits.safe_warning), (70, 60, 60))
        self.assertEqual(len(T.SHELTERS.cells), 16)
        self.assertEqual(len(T.SHELTERS.cells) * len(T.PROFILES["normal"].shelters), limits.shelter_cells)
        self.assertEqual(len(T.SHAPES["ring"].offsets), 16)
        self.assertEqual([m.name for m in T.ENDLESS.ladder][:3], ["QUICK RELOAD", "ARMOURED", "EXTRA SLOT"])
        self.assertEqual((len(T.ENDLESS.ladder), T.ENDLESS.first, T.ENDLESS.step, T.ENDLESS.loop), (8, 25, 1, (7, 24)))

    def test_salts_are_three_distinct_64_bit_values(self):
        salts = (T.SALTS.rng, T.SALTS.drop, T.SALTS.offer)
        self.assertEqual(salts, (0x243F6A8885A308D3, 0x13198A2E03707344, 0xA4093822299F31D0))
        self.assertTrue(all(type(s) is int and 0 < s < 2**64 for s in salts))


# §8.2's "Est. s" for the strong bot (B's fluid model). §9's boss estimates (128 / 155 / 159 / 171) are gone: the
# boss model is fitted to the strong bot's measured lengths (§19.3 step 1), which MEASURED_BOSS_S holds.
SPEC_ESTIMATE = {1: 51, 2: 60, 3: 61, 4: 64, 5: 64, 7: 64, 8: 66, 9: 68, 10: 66, 11: 68, 13: 68}
SPEC_ESTIMATE |= {14: 69, 15: 75, 16: 68, 17: 73, 19: 74, 20: 73, 21: 70, 22: 76, 23: 82}

# The strong bot's boss lengths, measured by T in round 1: `strong` staged immortal at each boss wave on Normal, seeds
# 1000-1019, with a typical loadout (5 lives, 2 bombs and the baseline's median picks). Each row gives each boss's
# part HP by phase, the median seconds of play, and how near the model must come: within 5% at the HP the rates were
# fitted to, and within 10% at the untuned HP, a cross-check. With §9's first rates (4.0-5.3 hits a second) the
# model was 43-154% too long. When a boss's phases, escorts, spawns or duty change, measure again and refit
# validate.BOSS_DPS.
MEASURED_BOSS_S = (
    (
        {
            "warden": (95, 360),
            "hive_queen": (270, 220, 130),
            "dreadnought": (110, 290, 165),
            "mothership": (42, 155, 60),
        },
        {"warden": 122.3, "hive_queen": 122.3, "dreadnought": 122.8, "mothership": 141.2},
        0.05,
    ),
    (
        {"warden": (46, 172), "hive_queen": (132, 110, 66), "dreadnought": (55, 140, 90), "mothership": (35, 130, 55)},
        {"warden": 68.5, "hive_queen": 68.7, "dreadnought": 65.8, "mothership": 119.2},
        0.10,
    ),
)


def wave_with(number, **changes):
    waves = list(T.WAVES)
    waves[number - 1] = replace(waves[number - 1], **changes)
    return tuple(waves)


def boss_with_hp(row, hp):
    """The boss row with each vulnerable phase's part HP taken from hp, in order."""
    assert len(hp) == sum(1 for phase in row.phases if phase.vulnerable), (row.kind, hp)
    values = iter(hp)
    return replace(row, phases=tuple(replace(p, hp=next(values)) if p.vulnerable else p for p in row.phases))


class ValidateTests(unittest.TestCase):
    def test_the_tables_are_valid(self):
        self.assertEqual(validate.check(), [])

    def test_the_command_exits_0_and_prints_the_estimates(self):
        with mock.patch("builtins.print") as printed:
            self.assertEqual(validate.main(), 0)
        text = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("the tables are valid", text)
        self.assertIn("run: strong", text)

    def test_estimates_follow_the_spec_model(self):
        estimates = validate.estimate()
        for e in estimates:
            with self.subTest(wave=e.wave):
                if not e.boss:
                    self.assertAlmostEqual(e.strong, SPEC_ESTIMATE[e.wave], delta=6.5)
                self.assertGreaterEqual(e.good, e.strong)
                low, high = validate.BOSS_S if e.boss else validate.REGULAR_S
                self.assertTrue(low <= e.strong <= high, e)
        strong, good, fast = validate.run_minutes()
        self.assertGreaterEqual(strong, validate.RUN_MIN)
        self.assertGreaterEqual(fast, validate.FAST_RUN_MIN)
        self.assertGreater(good, strong)
        # §8.2's totals: regular waves 22.7 min, and intros, clears and cards 1.5 min. Its bosses' 10.2 min were §9's
        # first estimates; the measured boss model has its own test below.
        regular = sum(e.strong for e in estimates if not e.boss) / validate.SECOND
        self.assertAlmostEqual(regular, 22.7, delta=1.0)
        self.assertAlmostEqual(validate.overhead() / validate.SECOND, 1.5, delta=0.1)
        boss_minutes = sum(e.strong for e in estimates if e.boss) / validate.SECOND
        self.assertAlmostEqual(strong, regular + boss_minutes + validate.overhead() / validate.SECOND)

    def test_the_boss_model_gives_the_strong_bot_s_measured_lengths(self):
        for hp, measured, share in MEASURED_BOSS_S:
            rows = MappingProxyType({key: boss_with_hp(row, hp[key]) for key, row in T.BOSSES.items()})
            with mock.patch.object(validate, "BOSSES", rows):
                for key, seconds in measured.items():
                    with self.subTest(boss=key, hp=hp[key]):
                        strong, good, _fast = validate.boss_estimate(key)
                        self.assertAlmostEqual(strong, seconds, delta=share * seconds)
                        self.assertGreater(good, strong)

    def test_no_flight_waits_for_the_good_bot_and_the_novice_holds_20_at_most(self):
        for e in validate.estimate():
            if not e.boss and e.wave not in validate.REPORTED:
                self.assertEqual(e.waits, (), e.wave)
                self.assertLessEqual(e.peak, T.LIMITS.enemies)
        wave1 = T.WAVES[0]
        novice = validate.queue(wave1.flights, validate.NOVICE, 1)
        self.assertEqual(novice.waits, ())
        self.assertLessEqual(novice.peak, validate.NOVICE_ALIVE)

    def test_the_queue_waits_on_the_band_and_on_the_cap(self):
        tall = T.Flight(0, 15, "rect", (3, 5), "MARCH")
        flights = (tall, replace(tall, at=60), replace(tall, at=120))
        stuck = validate.queue(flights, 0.0, 1, limit=600)
        self.assertEqual([index for index, _ticks in stuck.waits], [2])  # two 3-row flights fill y 2..12
        line = T.Flight(0, 14, "line", (14,), "MARCH")
        lines = tuple(replace(line, at=60 * n) for n in range(6))
        capped = validate.queue(lines, 0.0, 1, limit=600)
        self.assertEqual([index for index, _ticks in capped.waits], [5])  # 5 x 14 + 14 > 72
        self.assertEqual(capped.peak, 70)
        late = validate.queue(flights, 10.0, 1)  # the first dies at tick 150 (targetable at 60, 1.5 s of fire)
        self.assertEqual(late.waits, ((2, 31),))
        spaced = tuple(replace(tall, at=300 * n) for n in range(3))
        self.assertEqual(validate.queue(spaced, 10.0, 1).waits, ())

    def test_validate_catches_broken_content(self):
        wave = T.WAVES[0]
        too_many = replace(wave.director, slots=9)
        with mock.patch.object(validate, "WAVES", wave_with(1, director=too_many)):
            self.assertTrue(any("director slots" in line for line in validate.director()))
        early = replace(T.WAVES[1].director, burst=10)
        with mock.patch.object(validate, "WAVES", wave_with(2, director=early)):
            self.assertTrue(any("burst drawn before" in line for line in validate.director()))
        wide = (replace(wave.flights[0], size=15, dims=(15,)), *wave.flights[1:])
        with mock.patch.object(validate, "WAVES", wave_with(1, flights=wide)):
            self.assertTrue(any("line(15)" in line for line in validate.shapes()))
        jumbled = (wave.flights[1], wave.flights[0], *wave.flights[2:])
        with mock.patch.object(validate, "WAVES", wave_with(1, flights=jumbled)):
            self.assertTrue(any("schedule" in line for line in validate.pacing()))
        crowded = tuple(replace(f, at=f.at // 4) for f in T.WAVES[4].flights)
        with mock.patch.object(validate, "WAVES", wave_with(5, flights=crowded)):
            self.assertTrue(any("wave 5" in line and "waits" in line for line in validate.arrival()))
        named = (replace(wave.flights[0], mix=(("ghost", 1),)), *wave.flights[1:])
        with mock.patch.object(validate, "WAVES", wave_with(1, flights=named)):
            self.assertTrue(any("'ghost'" in line for line in validate.names()))

    def test_validate_catches_broken_names(self):
        enemies = dict(T.ENEMIES, gunner=replace(T.ENEMIES["gunner"], shot="laser"))
        patterns = dict(T.PATTERNS, burst=replace(T.PATTERNS["burst"], telegraph="glow"))
        first = T.WAVES[0]
        odd = (replace(first.flights[0], shape="blob", movement="HOVER"), *first.flights[1:])
        waves = list(wave_with(1, flights=odd))
        waves[5] = replace(waves[5], boss="kraken")
        acts = (replace(T.ACTS[0], boss="kraken"), *T.ACTS[1:])
        warden = T.BOSSES["warden"]
        phase = warden.phases[0]
        attack = phase.attacks[0]
        attacks = (
            replace(attack, source="fin", shot="ray", profiles=("insane",)),
            replace(attack, shot="spawn", kind="ghost"),
            *phase.attacks[1:],
        )
        phases = (replace(phase, vulnerable=("tail",), attacks=attacks), *warden.phases[1:])
        variants = (T.Variant("insane", 1, "C", "colour", 1), *warden.variants)
        bosses = dict(T.BOSSES, warden=replace(warden, phases=phases, variants=variants))
        profiles = dict(T.PROFILES, easy=replace(T.PROFILES["easy"], bosses="nightmare"))
        broken = dict(ENEMIES=enemies, PATTERNS=patterns, WAVES=tuple(waves), ACTS=acts, BOSSES=bosses)
        with mock.patch.multiple(validate, **broken, PROFILES=profiles):
            found = validate.names()
        expected = (
            "shot kind 'laser'", "telegraph 'glow'", "shape 'blob'", "movement 'HOVER'", "act 1: boss 'kraken'",
            "wave 6: boss 'kraken'", "part 'tail'", "source part 'fin'", "shot 'ray'", "spawned kind 'ghost'",
            "attack A: profile 'insane'", "variant: profile 'insane'", "attack field 'colour'",
            "profile easy: boss variants 'nightmare'",
        )  # fmt: skip
        for text in expected:
            self.assertTrue(any(text in line for line in found), (text, found))

    def test_validate_catches_broken_keys(self):
        waves = list(T.WAVES[:-1])
        waves[0] = replace(waves[0], line="x" * 53)
        acts = (replace(T.ACTS[0], line="y" * 53), *T.ACTS[1:])
        hard = replace(T.PROFILES["hard"], pressure=2.0, line="z" * 53)
        profiles = {"hard": hard, "normal": T.PROFILES["normal"], "easy": T.PROFILES["easy"]}
        pickups = dict(reversed(T.PICKUPS.items()))
        pickups["rapid"] = replace(pickups["rapid"], weight=0)
        name, upgrade = next(iter(T.UPGRADES.items()))
        upgrades = dict(T.UPGRADES)
        upgrades[name] = replace(upgrade, weight=0, family="misc", priority=(99,) * len(upgrade.priority))
        broken = dict(WAVES=tuple(waves), ACTS=acts, PROFILES=profiles, PICKUPS=pickups, UPGRADES=upgrades)
        broken |= dict(ENDLESS=replace(T.ENDLESS, ladder=()), SALTS=replace(T.SALTS, rng=T.SALTS.rng + 1))
        with mock.patch.multiple(validate, **broken, DIVE=replace(T.DIVE, vx_max=0.0)):
            found = validate.keys()
        expected = (
            "23 waves (24)", "4 bosses", "a profile for every difficulty", "a pickup row for every pickup kind",
            "every pickup weight positive", "upgrades: positive weights", "upgrades: three families",
            "upgrades: priorities", "wave 1: its intro line", "act 1: its line", "profile hard: pressure",
            "profile hard: its line", "Endless ladder", "SALTS changed", "vx bound",
        )  # fmt: skip
        for text in expected:
            self.assertTrue(any(text in line for line in found), (text, found))

    def test_validate_catches_unfair_attacks(self):
        beam = T.Attack("C", "beam", "core", "beam", 1, 0, 0.0, 40, 220, "beam", live=40, track=10, width=3.0)
        self.assertTrue(any("locks" in line for line in validate._attack_checks("dreadnought", 1, "normal", beam)))
        pair = replace(beam, count=2, track=0, telegraph=60, gap=8.0)
        self.assertTrue(any("lane" in line for line in validate._attack_checks("dreadnought", 2, "normal", pair)))
        gap = T.Attack("B", "curtain", "core", "pellet", 9, 0, 0.0, 36, 200, "curtain", width=30.0, gap=6.0)
        self.assertTrue(any("F4" in line for line in validate._attack_checks("dreadnought", 0, "normal", gap)))
        needles = T.Attack("D", "needles", "core", "needle", 2, 0, 0.0, 24, 100, "straight")
        self.assertEqual(validate._attack_checks("dreadnought", 2, "hard", needles), [])
        with mock.patch.object(validate, "_lowest_spawn", return_value=13.2):  # a 17-tick flight at the 0.28 cap
            found = validate._attack_checks("dreadnought", 2, "hard", needles)
        self.assertTrue(any("reaction shot from y 13.2 fails F1" in line for line in found), found)

    def test_validate_checks_the_boss_variants(self):
        queen = T.BOSSES["hive_queen"]
        bad = (
            T.Variant("hard", -1, "escort", "colour", 1),  # not a Flight field
            T.Variant("hard", 0, "", "speed", 0.2),  # a phase row: nothing would apply it
            T.Variant("hard", 1, "R", "count", 9),  # R isn't in phase 2
            T.Variant("hard", 5, "A", "count", 2),  # no phase 6
        )
        broken = dict(T.BOSSES, hive_queen=replace(queen, variants=(*queen.variants, *bad)))
        with mock.patch.object(validate, "BOSSES", broken):
            found = validate.names()
        for text in ("escort field 'colour'", "a phase row has no variants", "attack 'R' isn't in that phase",
                     "no phase 5"):  # fmt: skip
            self.assertTrue(any(text in line for line in found), (text, found))
        oversized = (*queen.variants, T.Variant("hard", -1, "escort", "size", 9))  # a ring holds 8
        with mock.patch.object(validate, "BOSSES", dict(T.BOSSES, hive_queen=replace(queen, variants=oversized))):
            found = validate.shapes()
        self.assertTrue(any("hive_queen escort 1 on hard" in line and "don't fit" in line for line in found), found)
        lopsided = tuple(v for v in queen.variants if v.attack != "R")  # Hard's brood of 8, refilled to 6
        lopsided = dict(T.BOSSES, hive_queen=replace(queen, variants=lopsided))
        with mock.patch.object(validate, "BOSSES", lopsided), mock.patch.object(bosses, "BOSSES", lopsided):
            with mock.patch.dict(bosses._ATTACKS, clear=True):
                found = validate.bosses()
        self.assertTrue(any("refills to 6, its ring holds (8,)" in line for line in found), found)

    def test_validate_checks_each_phase_s_worst_case_and_the_orbit(self):
        ship = T.BOSSES["mothership"]
        core = ship.phases[2]
        beams = replace(core.attacks[0], count=2)
        crowded = (*core.attacks, replace(beams, key="X"), replace(beams, key="Y"))  # 3 double beams: 9 marks
        phases = (*ship.phases[:2], replace(core, attacks=crowded), ship.phases[3])
        moved = (*ship.parts[:1], replace(ship.parts[1], y=5.0), *ship.parts[2:])
        broken = dict(T.BOSSES, mothership=replace(ship, phases=phases, parts=moved))
        with mock.patch.object(validate, "BOSSES", broken), mock.patch.dict(bosses._ATTACKS, clear=True):
            with mock.patch.object(bosses, "BOSSES", broken), mock.patch.dict(bosses._ORBIT, clear=True):
                found = validate.bosses()
        self.assertTrue(any("mothership phase 3 on normal" in line and "hazards" in line for line in found), found)
        self.assertTrue(any("isn't on its orbit" in line for line in found), found)

    def test_validate_checks_columns_the_ladder_and_the_stacked_director(self):
        wave = T.WAVES[16]
        wide = tuple(replace(f, dims=(1, 9), size=9) if f.movement == "COLUMNS" else f for f in wave.flights)
        with mock.patch.object(validate, "WAVES", wave_with(17, flights=wide)):
            self.assertTrue(any("COLUMNS flight of 9 columns" in line for line in validate.shapes()))
        ladder = list(T.ENDLESS.ladder)
        ladder[0] = replace(ladder[0], cap=12)  # under the reload floor
        ladder[2] = replace(ladder[2], cap=9)  # 9 slots
        ladder[1] = replace(ladder[1], stat="armour")
        endless = replace(T.ENDLESS, ladder=tuple(ladder), loop_pressure=1.3)
        with mock.patch.object(validate, "ENDLESS", endless):
            found = validate.keys()
        for text in ("under the reload floor", "slots cap 9", "stat 'armour'", "loop_pressure 1.3"):
            self.assertTrue(any(text in line for line in found), (text, found))
        from terminal_invaders.campaign import endless as ladder_module

        with mock.patch.object(ladder_module, "ENDLESS", replace(T.ENDLESS, ladder=tuple(
                replace(m, cap=9) if m.stat == "slots" else m for m in T.ENDLESS.ladder))):  # fmt: skip
            ladder_module._STACKS.clear()
            found = validate.director()
        ladder_module._STACKS.clear()
        self.assertTrue(any("endless wave" in line and "director slots" in line for line in found), found)

    def test_the_salts_are_pinned_outside_the_tables(self):
        self.assertEqual(validate.SALT_PINS, (T.SALTS.rng, T.SALTS.drop, T.SALTS.offer))
        with mock.patch.object(validate, "SALTS", replace(T.SALTS, rng=1)):
            self.assertTrue(any("SALTS" in line for line in validate.keys()))

    def test_the_art_check_runs_once_the_sprites_can_tell(self):
        from terminal_invaders import sprites

        found = validate.art()
        if not hasattr(sprites, "missing_art"):
            self.assertIsNone(found)
        with mock.patch.object(sprites, "missing_art", lambda: ["scout"], create=True):
            self.assertEqual(validate.art(), ["art key 'scout' is missing at px1, px2 or px3"])
            with mock.patch.object(sprites, "missing_pickups", lambda: ["bomb"], create=True):
                self.assertEqual(validate.art()[1:], ["pickup 'bomb' has no art"])


if __name__ == "__main__":
    unittest.main()
