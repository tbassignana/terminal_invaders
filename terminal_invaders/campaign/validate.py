"""Content checks for the Campaign's tables (spec §20.1), and B's fluid model of wave and run length.

`check()` returns every failure as one line of plain text; `python -m terminal_invaders.campaign.validate` prints
them (or the estimates when there are none) and exits 1 on any failure. tests/test_campaign_tables.py runs it.

`estimate()` runs each wave as a queue at a kill rate in effective HP a second: flights arrive by their schedule,
become targetable 1 s later, and are killed oldest first. Each flight holds its band (its rows plus a 2-cell gap)
while any of it lives. An arriving flight pushes the older flights down by the overlap, as §7.3 says, and waits
while the push would take an enemy below y 12 or the 72-enemy cap won't allow it. Later flights wait behind it.
Flights don't march down by themselves in the model.

The model's numbers (what a kill costs the bot by kind, its rates by act, boss DPS and duty) describe the bots, not
the game, so they live here rather than in tables.py. Boss DPS is fitted to the strong bot's measured boss lengths.
"""

import sys
from dataclasses import fields, replace
from math import floor
from typing import NamedTuple

from .. import model
from .fleet import height, layout
from .tables import (
    ACTS,
    ARRIVAL,
    BOSSES,
    DIVE,
    ENDLESS,
    ENEMIES,
    FAMILIES,
    FORMATIONS,
    LIMITS,
    PATTERNS,
    PICKUPS,
    PROFILES,
    SALTS,
    SHAPES,
    SHOTS,
    TELEGRAPHS,
    TIMING,
    UPGRADES,
    WAVES,
    Attack,
    Flight,
)

# --- the bots' model (B's fluid model, scratch/spec-author/waves_base.py) -------------------------------------------

COST = {"scout": 1.0, "diver": 1.3, "gunner": 1.0, "weaver": 1.35, "shell": 1.0, "lancer": 1.1, "bomber": 1.0}
COST |= {"splitter": 1.0, "carrier": 1.0, "mite": 1.3, "guard": 1.0}  # shots per HP, by kind
CARRIER_MITES = 2  # mites a carrier launches over its life
RATE = {1: 2.6, 2: 2.4, 3: 2.3, 4: 2.2}  # the strong bot's effective HP a second, by act
GOOD = 0.88  # the good bot's share of the strong bot's rate
NOVICE = 0.55  # the novice's effective HP a second
FAST = 4.0  # B's measured fast rate (effective HP a second; boss hits a second)
FAST_DUTY = 0.7  # the fast bot's boss duty
ENTRY = 60  # ticks from a flight's arrival until it can be targeted
# Boss hits a second at full duty, measured on the strong bot as §19.3 step 1 asks (§9's first estimates were 4.0 /
# 4.6 / 5.0 / 5.3). T round 1 staged `strong` immortal at each boss wave (Normal, seeds 1000-1019, a typical
# loadout), with each part's HP by phase at 95, 360 / 270, 220, 130 / 110, 290, 165 / 42, 155, 60. Each value makes
# boss_estimate() give that run's median: 122.3 / 122.3 / 122.8 / 141.2 s. DUTY is unchanged, so these rates also
# absorb its error. tests/test_campaign_tables.py holds the measurements.
BOSS_DPS = {"warden": 8.7, "hive_queen": 12.1, "dreadnought": 12.7, "mothership": 8.3}
DUTY = {"warden": (0.55, 0.55), "hive_queen": (0.5, 0.5, 0.45), "dreadnought": (0.5, 0.45, 0.42)}
DUTY |= {"mothership": (0.0, 0.45, 0.42, 0.38)}  # the share of a phase the bot can shoot at the boss
CARD = 90  # ticks a pilot spends on an upgrade card
SECOND = 60
LIMIT = 30 * 60 * SECOND  # the queue gives up after 30 minutes

# --- the bands and pins of §20.1 -------------------------------------------------------------------------------------

REGULAR_S = (47.0, 88.0)
BOSS_S = (95.0, 178.0)
RUN_MIN = 33.0  # minutes at the default rates
FAST_RUN_MIN = 30.5  # minutes at B's fast rate
NOVICE_ALIVE = 20  # the novice's wave 1: never more alive than this
# Waves whose waits are reported rather than failed. None now: wave 23's flights have 2 rows (E-M2), so the three
# alive from its 5th flight on share the band above y 12 (E-M1's D17).
REPORTED = ()
BOSS_ATTACKS = (3, 6)  # attacks per boss, spawns not counted (D14: the MOTHERSHIP has six)
STATS = ("reload", "veteran", "slots", "divers", "speed", "flights", "fuse", "reaction")  # what a modifier can change
STACKED = 64 * 8  # modifiers in a stack deep enough that every one is at its cap
LOOP_PRESSURE = (0.8, 1.1)
SALT_PINS = (0x243F6A8885A308D3, 0x13198A2E03707344, 0xA4093822299F31D0)
SHAPE_LIMITS = {"line": (1, 14), "rect": (3, 14), "checker": (3, 13), "vee": (7, 13), "diamond": (5, 13)}


class Queue(NamedTuple):
    """One wave run as a queue: when it ended, the most enemies alive, and every flight that had to wait."""

    end: int  # ticks
    peak: int
    waits: tuple  # (flight index, ticks it waited)


class WaveEstimate(NamedTuple):
    wave: int
    name: str
    boss: bool
    strong: float  # play seconds at the strong bot's default rate
    good: float
    fast: float
    peak: int  # most enemies alive in the good bot's queue
    waits: tuple  # the good bot's waits: (flight index, ticks)


# --- the queue -------------------------------------------------------------------------------------------------------


def _units(flight, profile, act, veteran) -> tuple:
    """The flight's enemies as kill costs in the order the model kills them, and how many it adds alive.

    Returns (units, joins): each unit is (cost, alive change when it dies). A splitter's mites follow it and count
    only once it dies; a carrier's mites come first ("kill them first") and count from the carrier's arrival."""
    from .fleet import kinds_for

    kinds = kinds_for(flight, layout(flight), PROFILES[profile], act)
    units = []
    joins = len(kinds)
    for kind in kinds:
        hp = ENEMIES[kind].hp + veteran
        if kind == "splitter":
            mites = PROFILES[profile].mites
            units.append((hp * COST[kind], mites - 1))
            units += [(COST["mite"], -1)] * mites
        elif kind == "carrier":
            units += [(COST["mite"], -1)] * CARRIER_MITES
            units.append((hp * COST[kind], -1))
            joins += CARRIER_MITES
        else:
            units.append((hp * COST[kind], -1))
    return units, joins


def queue(flights, rate, act, profile="normal", veteran=0.0, limit=LIMIT) -> Queue:
    """Run the flights as §7.3's queue at `rate` effective HP a second, for at most `limit` ticks. A flight still
    waiting at the limit is listed in `waits` with the ticks it had waited."""
    pending = list(enumerate(flights))
    field_ = []  # [top, height, units, killed, damage, arrival tick]
    alive = peak = 0
    waits = []
    budget = rate / SECOND
    tick = 0
    while (pending or field_) and tick < limit:
        while pending and pending[0][1].at <= tick:
            index, flight = pending[0]
            h = height(layout(flight))
            if alive + flight.size > LIMITS.enemies:
                break
            band = ARRIVAL.top + h + ARRIVAL.gap
            if field_:
                highest = min(state[0] for state in field_)
                if highest < band:
                    push = band - highest
                    if max(state[0] + state[1] for state in field_) + push > ARRIVAL.push_floor:
                        break
                    for state in field_:
                        state[0] += push
            pending.pop(0)
            if tick > flight.at:
                waits.append((index, tick - flight.at))
            units, joins = _units(flight, profile, act, veteran)
            field_.append([ARRIVAL.top, h, units, 0, 0.0, tick])
            alive += joins
        peak = max(peak, alive)
        left = budget
        for state in field_:  # oldest first
            if tick - state[5] < ENTRY:
                break  # the flights after it are younger: none is targetable yet
            units = state[2]
            while left > 0 and state[3] < len(units):
                cost, change = units[state[3]]
                need = cost - state[4]
                if left >= need:
                    left -= need
                    state[3] += 1
                    state[4] = 0.0
                    alive += change
                else:
                    state[4] += left
                    left = 0.0
            if left <= 0:
                break
        if any(state[3] >= len(state[2]) for state in field_):
            field_ = [state for state in field_ if state[3] < len(state[2])]
        tick += 1
    waits += [(index, tick - flight.at) for index, flight in pending if flight.at < tick]
    return Queue(tick, peak, tuple(waits))


def _veteran(profile, wave) -> float:
    for first, last, share in PROFILES[profile].veteran:
        if first <= wave <= last:
            return share
    return 0.0


def _act(wave) -> int:
    for act in ACTS:
        if act.first <= wave <= act.last:
            return act.number
    return ACTS[-1].number


def reinforced(row, stacks) -> tuple:
    """A regular wave's flights with REINFORCED's extra flights: its last flight repeated every `ticks` after it."""
    modifier = next(m for m in ENDLESS.ladder if m.stat == "flights")
    flights = list(row.flights)
    last = flights[-1]
    for extra in range(1, min(stacks, int(modifier.cap)) + 1):
        flights.append(replace(last, at=last.at + extra * modifier.ticks))
    return tuple(flights)


# --- the estimates ---------------------------------------------------------------------------------------------------


def _phase_hp(row, phase) -> int:
    return phase.hp * sum(1 for part in row.parts if part.name in phase.vulnerable)


def _flight_cost(flight, profile, act) -> float:
    return sum(cost for cost, _change in _units(flight, profile, act, 0.0)[0])


def _escort_cost(key, profile="normal") -> tuple:
    """Effective HP of a boss's escorts per phase: its opening flights (in phase 0), each phase's flights, and its
    spawns, which the model counts as one use per cooldown over the phase's estimated length (at least one)."""
    row = BOSSES[key]
    act = _act(row.wave)
    dps, duty = BOSS_DPS[key], DUTY[key]
    out = []
    for number, phase in enumerate(row.phases):
        cost = sum(_flight_cost(flight, profile, act) for flight in phase.escorts)
        if number == 0:
            cost += sum(_flight_cost(flight, profile, act) for flight in row.escorts)
        hp = _phase_hp(row, phase)
        length = hp / (dps * duty[number]) * SECOND if hp and duty[number] else (cost / RATE[act] + 1) * SECOND
        for attack in phase.attacks:
            if attack.shot != "spawn" or profile not in attack.profiles:
                continue
            uses = 1 if attack.at or not attack.cooldown else max(1, floor(length * phase.pressure / attack.cooldown))
            kind = attack.kind
            each = (ENEMIES[kind].hp + (CARRIER_MITES if kind == "carrier" else 0)) * COST[kind]
            cost += uses * attack.count * each
        out.append(cost)
    return tuple(out)


def boss_estimate(key, profile="normal") -> tuple:
    """(strong, good, fast) play seconds of a boss wave: phase HP / (DPS x duty), its escorts at the act's rate, and
    1.5 s per phase break."""
    row = BOSSES[key]
    act = _act(row.wave)
    dps, duty = BOSS_DPS[key], DUTY[key]
    escorts = sum(_escort_cost(key, profile))
    breaks = (len(row.phases) - 1) * TIMING.phase_break / SECOND
    hp = [(_phase_hp(row, phase), duty[number]) for number, phase in enumerate(row.phases)]
    strong = sum(h / (dps * d) for h, d in hp if h) + escorts / RATE[act] + breaks
    good = sum(h / (dps * GOOD * d) for h, d in hp if h) + escorts / (RATE[act] * GOOD) + breaks
    fast = sum(h / (FAST * FAST_DUTY) for h, _d in hp if h) + escorts / FAST + breaks
    return strong, good, fast


def estimate(profile="normal") -> tuple:
    """Every Campaign wave's estimated play seconds at the strong, good and fast rates, with the good bot's queue."""
    out = []
    for row in WAVES:
        if row.boss:
            strong, good, fast = boss_estimate(row.boss, profile)
            out.append(WaveEstimate(row.number, row.name, True, strong, good, fast, 0, ()))
            continue
        act = _act(row.number)
        veteran = _veteran(profile, row.number)
        runs = [queue(row.flights, rate, act, profile, veteran) for rate in (RATE[act], RATE[act] * GOOD)]
        fast = queue(row.flights, FAST, act, profile, veteran)
        strong, good = runs
        out.append(
            WaveEstimate(
                row.number, row.name, False, strong.end / SECOND, good.end / SECOND, fast.end / SECOND, good.peak,
                good.waits,
            )
        )  # fmt: skip
    return tuple(out)


def overhead() -> float:
    """Seconds of intros, clears and cards in a winning run."""
    ticks = 0
    for row in WAVES:
        if row.boss:
            ticks += TIMING.boss_intro
        elif row.number > 1 and any(act.first == row.number for act in ACTS):
            ticks += TIMING.act_intro
        else:
            ticks += TIMING.intro
        ticks += TIMING.clear
        if row.card:
            ticks += CARD
    return ticks / SECOND


def run_minutes(estimates=None) -> tuple:
    """(strong, good, fast) minutes of a winning run before deaths."""
    estimates = estimates or estimate()
    extra = overhead()
    return tuple((sum(getattr(e, name) for e in estimates) + extra) / SECOND for name in ("strong", "good", "fast"))


# --- the checks ------------------------------------------------------------------------------------------------------


def _telegraph(base, profile, scanner=False) -> int:
    """director.telegraph without a game: the profile's factor (and SCANNER), then the floor; 0 stays 0."""
    if base <= 0:
        return 0
    factor = PROFILES[profile].telegraph * (UPGRADES["SCANNER"].value if scanner else 1.0)
    return max(LIMITS.telegraph, floor(base * factor + 0.5))


def _f1(y, vy, tele, cls):
    from .director import plan

    return plan(y, vy, tele, cls)


def _fastest(v_a, kind, profile, endless) -> float:
    speed = PROFILES[profile].speed
    if endless:
        modifier = next(m for m in ENDLESS.ladder if m.stat == "speed")
        speed *= modifier.cap
    return min(LIMITS.vy, SHOTS[kind].speed * v_a * speed)


def boss_attacks(key, phase, profile) -> tuple:
    from .boss import attacks_of

    return attacks_of(key, phase, profile)


def names() -> list:
    out = []
    for kind, enemy in ENEMIES.items():
        if enemy.shot and enemy.shot not in SHOTS:
            out.append(f"enemy {kind}: shot kind {enemy.shot!r} doesn't exist")
    for name, pattern in PATTERNS.items():
        if pattern.telegraph and pattern.telegraph not in TELEGRAPHS:
            out.append(f"pattern {name}: telegraph {pattern.telegraph!r} doesn't exist")
    flights = [(f"wave {row.number} flight {i + 1}", f) for row in WAVES for i, f in enumerate(row.flights)]
    for key, boss in BOSSES.items():
        flights += [(f"{key} escort {i + 1}", f) for i, f in enumerate(boss.escorts)]
        for number, phase in enumerate(boss.phases):
            flights += [(f"{key} phase {number + 1} escort {i + 1}", f) for i, f in enumerate(phase.escorts)]
    for label, flight in flights:
        if flight.shape not in SHAPES:
            out.append(f"{label}: shape {flight.shape!r} doesn't exist")
        if flight.movement not in FORMATIONS:
            out.append(f"{label}: movement {flight.movement!r} doesn't exist")
        for kind, _count in flight.mix:
            if kind not in ENEMIES:
                out.append(f"{label}: enemy kind {kind!r} doesn't exist")
    for act in ACTS:
        if act.boss not in BOSSES:
            out.append(f"act {act.number}: boss {act.boss!r} doesn't exist")
    for row in WAVES:
        if row.boss and row.boss not in BOSSES:
            out.append(f"wave {row.number}: boss {row.boss!r} doesn't exist")
    attack_fields = tuple(f.name for f in fields(Attack))
    for key, boss in BOSSES.items():
        parts = tuple(part.name for part in boss.parts)
        for number, phase in enumerate(boss.phases):
            label = f"{key} phase {number + 1}"
            for part in phase.vulnerable:
                if part not in parts:
                    out.append(f"{label}: part {part!r} doesn't exist")
            for attack in phase.attacks:
                if attack.source not in parts:
                    out.append(f"{label} attack {attack.key}: source part {attack.source!r} doesn't exist")
                if attack.shot not in ("beam", "spawn", *SHOTS):
                    out.append(f"{label} attack {attack.key}: shot {attack.shot!r} doesn't exist")
                if attack.shot == "spawn" and attack.kind not in ENEMIES:
                    out.append(f"{label} attack {attack.key}: spawned kind {attack.kind!r} doesn't exist")
                for profile in attack.profiles:
                    if profile not in PROFILES:
                        out.append(f"{label} attack {attack.key}: profile {profile!r} doesn't exist")
        flight_fields = tuple(f.name for f in fields(Flight))
        for variant in boss.variants:
            label = f"{key} variant {variant.profile} phase {variant.phase} {variant.attack or '(phase row)'}"
            if variant.profile not in PROFILES:
                out.append(f"{key} variant: profile {variant.profile!r} doesn't exist")
            if not -1 <= variant.phase < len(boss.phases):
                out.append(f"{label}: no phase {variant.phase}")
                continue
            if variant.attack == "escort":
                if variant.field not in flight_fields or variant.field == "at":
                    out.append(f"{label}: escort field {variant.field!r} doesn't exist (or can't vary)")
                flights = boss.escorts if variant.phase < 0 else boss.phases[variant.phase].escorts
                if variant.phase < 0:
                    flights += tuple(f for phase in boss.phases for f in phase.escorts)
                if not flights:
                    out.append(f"{label}: there is no escort flight to change")
                continue
            if not variant.attack:
                out.append(f"{label}: a phase row has no variants (only attacks and escorts do)")
                continue
            if variant.field not in attack_fields:
                out.append(f"{key} variant: attack field {variant.field!r} doesn't exist")
            phases = boss.phases if variant.phase < 0 else (boss.phases[variant.phase],)
            if not any(attack.key == variant.attack for phase in phases for attack in phase.attacks):
                out.append(f"{label}: attack {variant.attack!r} isn't in that phase")
    for name, profile in PROFILES.items():
        if not any(v.profile == profile.bosses for boss in BOSSES.values() for v in boss.variants) and (
            profile.bosses not in PROFILES
        ):
            out.append(f"profile {name}: boss variants {profile.bosses!r} name no profile")
    return out


def art():
    """The §3.7 art keys missing at px1, px2 or px3 and the pickups with no art, or None when the sprites can't tell
    yet (before integration)."""
    from .. import sprites

    missing = getattr(sprites, "missing_art", None)
    if missing is None:
        return None
    out = [f"art key {key!r} is missing at px1, px2 or px3" for key in missing()]
    pickups = getattr(sprites, "missing_pickups", None)
    if pickups is not None:
        out += [f"pickup {key!r} has no art" for key in pickups()]
    return out


def _escort(boss, phase, index, profile):
    """boss.escort_of on these (possibly patched) tables: the escort flight with the profile's escort variants."""
    flight = (boss.escorts if phase < 0 else boss.phases[phase].escorts)[index]
    for variant in boss.variants:
        if variant.profile == PROFILES[profile].bosses and variant.attack == "escort" and variant.phase in (-1, phase):
            if variant.field in tuple(f.name for f in fields(Flight)):
                flight = replace(flight, **{variant.field: variant.value})
    return flight


def _all_flights():
    """Every flight: the waves', and each boss's escorts on every profile (with its escort variants)."""
    for row in WAVES:
        for index, flight in enumerate(row.flights):
            yield f"wave {row.number} flight {index + 1}", flight
    for key, boss in BOSSES.items():
        groups = [(-1, f"{key} escort", boss.escorts)]
        groups += [(n, f"{key} phase {n + 1} escort", phase.escorts) for n, phase in enumerate(boss.phases)]
        for number, label, flights in groups:
            for index, flight in enumerate(flights):
                yield f"{label} {index + 1}", flight
                for profile in PROFILES:
                    varied = _escort(boss, number, index, profile)
                    if varied != flight:
                        yield f"{label} {index + 1} on {profile}", varied


def shapes() -> list:
    out = []
    low, high = LIMITS.field
    for label, flight in _all_flights():
        if flight.shape not in SHAPES:
            continue
        dims = flight.dims
        if flight.shape == "ring":
            if flight.movement != "ESCORT":
                out.append(f"{label}: a ring is for ESCORT flights only")
            if not 1 <= dims[0] <= SHAPES["ring"].cols:
                out.append(f"{label}: a ring holds at most {SHAPES['ring'].cols}")
        elif flight.shape in ("rect", "checker"):
            rows, cols = dims
            most_rows, most_cols = SHAPE_LIMITS[flight.shape]
            if not (1 <= rows <= most_rows and 1 <= cols <= most_cols):
                out.append(f"{label}: {flight.shape}{dims} is outside r <= {most_rows}, c <= {most_cols}")
        elif flight.shape == "line":
            if not 1 <= dims[0] <= SHAPE_LIMITS["line"][1]:
                out.append(f"{label}: line({dims[0]}) is longer than {SHAPE_LIMITS['line'][1]}")
        elif dims[0] not in SHAPES[flight.shape].sizes:
            out.append(f"{label}: {flight.shape}({dims[0]}) isn't a legal size {SHAPES[flight.shape].sizes}")
        if flight.shape not in ("ring", "line", "rect", "checker") and dims[0] not in SHAPES[flight.shape].sizes:
            continue
        capacity = len(layout(replace(flight, size=10_000)))
        if flight.size > capacity:
            out.append(f"{label}: {flight.size} enemies don't fit {flight.shape}{dims} ({capacity} slots)")
        if sum(count for _kind, count in flight.mix) > flight.size:
            out.append(f"{label}: its mix is larger than the flight")
        slots = layout(flight)
        if not slots:
            out.append(f"{label}: no slots")
            continue
        tall = height(slots)
        wide = max(ox for ox, _oy, _c in slots) - min(ox for ox, _oy, _c in slots)
        if flight.shape != "ring" and tall > LIMITS.shape_height:
            out.append(f"{label}: {tall:g} cells tall (at most {LIMITS.shape_height})")
        if wide > LIMITS.shape_width:
            out.append(f"{label}: {wide:g} cells wide (at most {LIMITS.shape_width})")
        if flight.movement == "COLUMNS":
            fewest, most = FORMATIONS["COLUMNS"].columns
            columns = len(dict.fromkeys(col for _ox, _oy, col in slots))
            if not fewest <= columns <= most:
                out.append(f"{label}: a COLUMNS flight of {columns} columns ({fewest}-{most})")
        if flight.movement != "ESCORT":
            left = ARRIVAL.centre + min(ox for ox, _oy, _c in slots)
            right = ARRIVAL.centre + max(ox for ox, _oy, _c in slots)
            if left < low or right > high:
                out.append(f"{label}: its slots span x {left:g}..{right:g}, outside {low:g}..{high:g}")
    return out


def director() -> list:
    """Every wave's director row, and Endless's loop (waves 7-24) with the ladder fully stacked."""
    from .endless import director_row

    first, last = ENDLESS.loop
    rows = [(f"wave {row.number}", row, row.director) for row in WAVES]
    rows += [
        (f"endless wave {row.number} (stacked)", row, director_row(row.director, STACKED))
        for row in WAVES[first - 1 : last]
    ]
    out = []
    for label, row, d in rows:
        if not 1 <= d.slots <= LIMITS.slots:
            out.append(f"{label}: {d.slots} director slots (1..{LIMITS.slots})")
        if d.reload < LIMITS.reload:
            out.append(f"{label}: reload {d.reload} below the floor {LIMITS.reload}")
        if not 0 < d.v_a <= LIMITS.vy:
            out.append(f"{label}: v_a {d.v_a} outside 0..{LIMITS.vy}")
        if not (0 <= d.focus <= 1 and 0 <= d.reaction <= 1):
            out.append(f"{label}: focus and reaction are shares (0..1)")
        if d.single <= 0:
            out.append(f"{label}: single needs a positive weight (the director falls back to it)")
        for name, pattern in PATTERNS.items():
            weight = getattr(d, name)
            if weight < 0:
                out.append(f"{label}: negative {name} weight")
            if weight and row.number < pattern.first:
                out.append(f"{label}: {name} drawn before its first wave {pattern.first}")
            if weight and pattern.slots > d.slots:
                out.append(f"{label}: {name} needs {pattern.slots} slots, the wave has {d.slots}")
            if weight and pattern.shots[1] * (3 if name == "single" else 1) > LIMITS.enemy_shots:
                out.append(f"{label}: one {name} volley can't fit under {LIMITS.enemy_shots} shots")
    return out


def bosses() -> list:
    """Each boss phase's worst case on every profile: its attacks' shots under the shot cap and their marks (beams,
    lanes, gaps) under the hazard cap, with a curtain waiting while beams are up (§9's cap rule); the ring's refill
    landing in the ring's places; every orbiting part on its orbit."""
    from .boss import ORBITING, _orbit, _orbit_at

    out = []
    for key, boss in BOSSES.items():
        for number, _phase in enumerate(boss.phases):
            for profile in PROFILES:
                label = f"{key} phase {number + 1} on {profile}"
                attacks = [a for a in boss_attacks(key, number, profile) if a.shot != "spawn"]
                needs = [(a, *_needs(a)) for a in attacks]
                shots = sum(n for _a, n, _m in needs)
                if shots > LIMITS.enemy_shots:
                    out.append(f"{label}: its attacks together need {shots} shots (cap {LIMITS.enemy_shots})")
                beams = sum(m for a, _n, m in needs if a.shot == "beam")
                others = sum(m for a, _n, m in needs if a.shot != "beam" and a.aim != "curtain")
                curtains = sum(m for a, _n, m in needs if a.aim == "curtain")
                marks = others + (max(beams, curtains) if beams else curtains)
                if marks > LIMITS.hazards:
                    out.append(f"{label}: its marks and lanes together need {marks} hazards (cap {LIMITS.hazards})")
                for attack in boss_attacks(key, number, profile):
                    if attack.shot != "spawn" or attack.kind != "weaver":
                        continue
                    rings = [i for i, f in enumerate(boss.escorts) if any(k == "weaver" for k, _n in f.mix)]
                    if not rings:
                        out.append(f"{label} attack {attack.key}: no ring of weavers to refill")
                        continue
                    ring = _escort(boss, -1, rings[0], profile)
                    if ring.shape != "ring" or attack.count != ring.dims[0]:
                        out.append(
                            f"{label} attack {attack.key}: refills to {attack.count}, its ring holds {ring.dims}"
                        )
        if boss.orbit:
            _legs, length, starts = _orbit(boss)
            if length <= 0:
                out.append(f"{key}: its orbit has no length")
            for part, start in zip(boss.parts, starts, strict=True):
                if part.name != ORBITING:
                    continue
                dx, dy = _orbit_at(boss, start)
                if abs(dx - part.dx) > 1e-6 or abs(dy - (part.y - boss.station[1])) > 1e-6:
                    out.append(f"{key}: a {part.name} at ({part.dx:g}, {part.y:g}) isn't on its orbit")
    return out


def arrival() -> list:
    """The arrival rule at two kill rates (and, for Endless, with the ladder fully stacked)."""
    out = []
    for row in WAVES:
        if row.boss:
            continue
        act = _act(row.number)
        veteran = _veteran("normal", row.number)
        good = queue(row.flights, RATE[act] * GOOD, act, "normal", veteran)
        if row.number in REPORTED:
            continue
        for index, ticks in good.waits:
            out.append(f"wave {row.number}: flight {index + 1} waits {ticks} ticks at the good bot's rate")
        if row.number == 1:
            novice = queue(row.flights, NOVICE, act, "normal", veteran)
            for index, ticks in novice.waits:
                out.append(f"wave 1: flight {index + 1} waits {ticks} ticks at the novice's rate")
            if novice.peak > NOVICE_ALIVE:
                out.append(f"wave 1: {novice.peak} enemies alive at the novice's rate (at most {NOVICE_ALIVE})")
    return out


def endless_arrival() -> list:
    """Endless's loop (acts II-IV) with REINFORCED and ARMOURED fully stacked, at the good bot's rate."""
    out = []
    first, last = ENDLESS.loop
    armoured = next(m for m in ENDLESS.ladder if m.stat == "veteran")
    for row in WAVES[first - 1 : last]:
        if row.boss:
            continue
        act = _act(row.number)
        flights = reinforced(row, 99)
        good = queue(flights, RATE[act] * GOOD, act, "normal", armoured.cap)
        for index, ticks in good.waits:
            out.append(f"endless wave {row.number}: flight {index + 1} waits {ticks} ticks at the good bot's rate")
    return out


def _boss_rows(key, phase):
    """Every y a phase can hold the boss at: its station, its path's rows and its dip row."""
    row = BOSSES[key]
    spec = row.phases[phase]
    rows = [row.station[1], *(y for _x, y in spec.path)]
    if spec.dip[0]:
        rows.append(spec.dip[0])
    return rows


def _lowest_spawn(key, phase, source) -> float:
    row = BOSSES[key]
    drop = max(_boss_rows(key, phase)) - row.station[1]
    y = max(part.y for part in row.parts if part.name == source) + drop + LIMITS.boss_spawn
    for part in row.parts:  # a shot that would start inside a part's box starts just below it (F2)
        if abs(part.y + drop - y) <= 0.45:
            y = max(y, part.y + drop + 0.46)
    return y


def warnings() -> list:
    """F1 at the fastest profile x FAST SHOTS and the lowest legal spawn; the dive and mite ceilings; beams, gaps and
    telegraphs after every multiplier (F3, F4)."""
    out = []
    v_max = max(row.director.v_a for row in WAVES)
    for kind, shot in SHOTS.items():
        for profile in PROFILES:
            tele = _telegraph(shot.telegraph, profile)
            vy = _fastest(v_max, kind, profile, endless=True)
            if _f1(LIMITS.spawn_y, vy, tele, "safe") is None:
                out.append(f"{kind} on {profile}: a safe shot from y {LIMITS.spawn_y} fails F1 at vy {vy:.3f}")
            if tele and tele < LIMITS.telegraph:
                out.append(f"{kind} on {profile}: telegraph {tele} below {LIMITS.telegraph}")
    for name, base in TELEGRAPHS.items():
        for profile in PROFILES:
            if _telegraph(base, profile) < LIMITS.telegraph:
                out.append(f"telegraph {name} on {profile} below {LIMITS.telegraph}")
    from .fleet import dive_warning

    for profile in PROFILES:
        game = _Probe(profile)
        for kind, need in (("diver", LIMITS.dive_warning), ("mite", LIMITS.mite_warning)):
            ceiling = _ceiling(game, kind, need)
            if ceiling is None or ceiling < ARRIVAL.top + LIMITS.shape_height:
                out.append(f"{kind} on {profile}: the W >= {need} rule leaves no dive below a fresh flight")
                continue
            w, fall = dive_warning(game, kind, ceiling)
            if w < need or fall < LIMITS.flight:
                out.append(f"{kind} on {profile}: W {w} (flight {fall}) at its ceiling y {ceiling}")
    for key, boss in BOSSES.items():
        for number, _phase in enumerate(boss.phases):
            for profile in PROFILES:
                for attack in boss_attacks(key, number, profile):
                    out += _attack_checks(key, number, profile, attack)
    for name, pattern in PATTERNS.items():
        if pattern.gap and not _gap_ok(pattern.gap):
            out.append(f"pattern {name}: its gap {pattern.gap:g} breaks F4")
    return out


def _gap_ok(gap) -> bool:
    low, high = LIMITS.gap_x
    return LIMITS.gap <= gap <= high - low and gap / 2 <= LIMITS.gap_reach


def _attack_checks(key, number, profile, attack) -> list:
    label = f"{key} phase {number + 1} attack {attack.key} on {profile}"
    out = []
    if attack.shot == "spawn":
        return out
    v_a = WAVES[BOSSES[key].wave - 1].director.v_a
    tele = _telegraph(attack.telegraph, profile)
    if tele < LIMITS.telegraph:
        out.append(f"{label}: telegraph {tele} below {LIMITS.telegraph}")
    if attack.shot == "beam":
        if tele < LIMITS.beam_lock + attack.track:
            out.append(f"{label}: its x locks {tele - attack.track} ticks before it goes live (at least 36)")
        low, high = LIMITS.field
        span = high - low
        if attack.count >= 2 and attack.gap - attack.width < LIMITS.lane:
            out.append(f"{label}: the lane between its beams is {attack.gap - attack.width:g} cells (at least 6)")
        if attack.count * attack.width > span - LIMITS.lane:
            out.append(f"{label}: its beams leave no {LIMITS.lane:g}-cell lane")
        return out
    if attack.aim == "curtain" and not _gap_ok(attack.gap):
        out.append(f"{label}: its gap {attack.gap:g} breaks F4")
    y = _lowest_spawn(key, number, attack.source)
    vy = _fastest(v_a, attack.shot, profile, endless=True)
    for cls in ("safe", "reaction"):
        elapsed = attack.spacing * max(0, (attack.count or 1) - 1)
        if _f1(y, vy, tele, cls) is None or _f1(y, vy, tele + elapsed, cls) is None:
            out.append(f"{label}: a {cls} shot from y {y:g} fails F1 at vy {vy:.3f}")
    shots, marks = _needs(attack)
    if shots > LIMITS.enemy_shots or marks > LIMITS.hazards:
        out.append(f"{label}: one use needs {shots} shots and {marks} marks (caps {LIMITS.enemy_shots}, 6)")
    return out


def _needs(attack):
    from .boss import _needs as needs

    return needs(attack)


class _Probe:
    """Just enough of a Game for fleet.dive_warning."""

    def __init__(self, profile):
        self.config = model.Config(difficulty=profile)


def _ceiling(game, kind, need):
    """The lowest row (in steps of 0.1) a dive or mite release may start from under its W rule."""
    from .fleet import dive_warning

    best = None
    for tenth in range(0, 160):
        y = tenth / 10
        w, fall = dive_warning(game, kind, y)
        if w >= need and fall >= LIMITS.flight:
            best = y
    return best


def pacing() -> list:
    out = []
    firsts = {}
    for row in WAVES:
        kinds = [kind for flight in row.flights for kind, _count in flight.mix] + (["scout"] if row.flights else [])
        for kind in kinds:
            firsts.setdefault(kind, row.number)
    new = sorted((ENEMIES[kind].first, kind) for kind in ENEMIES if ENEMIES[kind].new)
    for kind, wave in firsts.items():
        if ENEMIES[kind].new and wave != ENEMIES[kind].first:
            out.append(f"{kind}: first flies in wave {wave}, its table says {ENEMIES[kind].first}")
    for (a, kind_a), (b, kind_b) in zip(new, new[1:]):
        if b - a < 2:
            out.append(f"{kind_a} (wave {a}) and {kind_b} (wave {b}): more than one new kind in 2 waves")
    elements = [kind.first for kind in ENEMIES.values() if kind.new]
    elements += [movement.first for movement in FORMATIONS.values()] + [p.first for p in PATTERNS.values()]
    for act in ACTS:
        if act.first not in elements:
            out.append(f"act {act.number}: its first wave {act.first} introduces nothing new")
    for key, boss in BOSSES.items():
        if not 2 <= len(boss.phases) <= 4:
            out.append(f"{key}: {len(boss.phases)} phases (2..4)")
        letters = dict.fromkeys(a.key for phase in boss.phases for a in phase.attacks if a.shot != "spawn")
        low, high = BOSS_ATTACKS
        if not low <= len(letters) <= high:
            out.append(f"{key}: {len(letters)} attacks ({low}..{high})")
        if boss.wave != next(act.last for act in ACTS if act.boss == key):
            out.append(f"{key}: not at its act's last wave")
    for row in WAVES:
        at = [flight.at for flight in row.flights]
        if any(later <= earlier for earlier, later in zip(at, at[1:])):
            out.append(f"wave {row.number}: flight schedule doesn't increase")
    for act in ACTS:
        regular = [row for row in WAVES[act.first - 1 : act.last] if not row.boss]
        for before, after in zip(regular, regular[1:]):
            b, a = before.director, after.director
            if a.reload > b.reload or a.v_a < b.v_a or a.slots < b.slots:
                out.append(f"wave {after.number}: its director eases from wave {before.number}'s")
    estimates = estimate()
    for e in estimates:
        low, high = BOSS_S if e.boss else REGULAR_S
        if not low <= e.strong <= high:
            out.append(f"wave {e.wave} {e.name}: estimate {e.strong:.1f} s outside {low:g}-{high:g} s")
    strong, _good, fast = run_minutes(estimates)
    if strong < RUN_MIN:
        out.append(f"the run: {strong:.1f} min at the default rates (at least {RUN_MIN:g})")
    if fast < FAST_RUN_MIN:
        out.append(f"the run: {fast:.1f} min at the fast rate (at least {FAST_RUN_MIN:g})")
    return out


def keys() -> list:
    out = []
    if len(WAVES) != model.CAMPAIGN_WAVES or model.CAMPAIGN_WAVES != 24:
        out.append(f"{len(WAVES)} waves (24)")
    if sum(1 for row in WAVES if row.card) != 7:
        out.append("7 upgrade cards")
    if len(BOSSES) != 4 or sum(1 for row in WAVES if row.boss) != 4:
        out.append("4 bosses")
    if tuple(PROFILES) != tuple(model.DIFFICULTIES):
        out.append("a profile for every difficulty")
    if tuple(PICKUPS) != tuple(model.PICKUPS):
        out.append("a pickup row for every pickup kind")
    if any(p.weight <= 0 for p in PICKUPS.values()):
        out.append("every pickup weight positive")
    if any(up.weight <= 0 for up in UPGRADES.values()) or sum(up.stacks for up in UPGRADES.values()) != 15:
        out.append("upgrades: positive weights and 15 ranks")
    if sorted(dict.fromkeys(up.family for up in UPGRADES.values())) != sorted(FAMILIES) or len(FAMILIES) != 3:
        out.append("upgrades: three families")
    ranks = sorted(rank for up in UPGRADES.values() for rank in up.priority)
    if ranks != list(range(1, 16)):
        out.append("upgrades: priorities 1..15, once each")
    for row in WAVES:
        if len(row.line) > 52 or not row.line.isascii():
            out.append(f"wave {row.number}: its intro line is over 52 characters")
    for act in ACTS:
        if len(act.line) > 52:
            out.append(f"act {act.number}: its line is over 52 characters")
    for name, profile in PROFILES.items():
        low, high = LIMITS.pressure
        if not low <= profile.pressure <= high:
            out.append(f"profile {name}: pressure {profile.pressure} outside {low}-{high}")
        if len(profile.line) > 52:
            out.append(f"profile {name}: its line is over 52 characters")
    if not ENDLESS.ladder or any(not m.name for m in ENDLESS.ladder):
        out.append("the Endless ladder needs named modifiers")
    out += _ladder()
    for name, profile in PROFILES.items():
        acts = range(1, len(ACTS) + 1)
        if profile.gunner_every < 0 or profile.mites < 0:
            out.append(f"profile {name}: gunner_every and mites can't be negative")
        for rule, label in ((profile.front_gunners, "front_gunners"), (profile.back_lancers, "back_lancers")):
            if rule != (0, 0) and (rule[0] not in acts or rule[1] < 1):
                out.append(f"profile {name}: {label} {rule} needs an act 1-{len(ACTS)} and at least 1 enemy")
        for first, last, share in profile.veteran:
            if not (1 <= first <= last <= model.CAMPAIGN_WAVES and 0 <= share <= 1):
                out.append(f"profile {name}: veteran plating ({first}, {last}, {share}) is outside the waves")
        if not 0 < profile.telegraph or not 0 <= profile.reaction:
            out.append(f"profile {name}: telegraph and reaction factors must be positive")
    if (SALTS.rng, SALTS.drop, SALTS.offer) != SALT_PINS:
        out.append("tables.SALTS changed: every random stream would reshuffle")
    if DIVE.vx_max <= 0:
        out.append("dives need a vx bound")
    return out


def _ladder() -> list:
    """Every modifier changes a stat the engine knows, once each, and its cap keeps the engine's own floors."""
    out = []
    stats = [m.stat for m in ENDLESS.ladder]
    for m in ENDLESS.ladder:
        if m.stat not in STATS:
            out.append(f"modifier {m.name}: stat {m.stat!r} isn't one of {', '.join(STATS)}")
    if len(dict.fromkeys(stats)) != len(stats):
        out.append("the Endless ladder changes a stat twice")
    for m in ENDLESS.ladder:
        if m.stat == "reload" and not (m.cap >= LIMITS.reload and 0 < m.mul < 1):
            out.append(f"modifier {m.name}: its floor {m.cap:g} is under the reload floor {LIMITS.reload}")
        if m.stat == "slots" and not (1 <= m.cap <= LIMITS.slots and m.add >= 1):
            out.append(f"modifier {m.name}: slots cap {m.cap:g} outside 1..{LIMITS.slots}")
        if m.stat in ("veteran", "reaction") and not 0 < m.cap <= 1:
            out.append(f"modifier {m.name}: a share's cap must be in 0..1")
        if m.stat in ("speed", "divers") and not (m.cap >= 1 and m.mul > 0):
            out.append(f"modifier {m.name}: its cap {m.cap:g} would ease the game")  # F1 is checked at the cap
        if m.stat in ("flights", "fuse") and (m.ticks <= 0 or m.cap <= 0):
            out.append(f"modifier {m.name}: needs its ticks and cap")
    low, high = LOOP_PRESSURE
    if not low <= ENDLESS.loop_pressure <= high:
        out.append(f"ENDLESS.loop_pressure {ENDLESS.loop_pressure} outside {low}-{high}")
    first, last = ENDLESS.loop
    if not (1 <= first <= last <= model.CAMPAIGN_WAVES) or ENDLESS.first <= model.CAMPAIGN_WAVES:
        out.append("ENDLESS.loop and ENDLESS.first: the loop is campaign waves, the first modifier after wave 24")
    if ENDLESS.step < 1 or ENDLESS.card_every < 1 or ENDLESS.loop_life < 0:
        out.append("ENDLESS.step and card_every at least 1, loop_life at least 0")
    return out


CHECKS = (names, shapes, director, bosses, arrival, warnings, pacing, keys)


def check() -> list:
    """Every failure, one line each; [] when the tables are valid. The art check runs when the sprites can tell."""
    out = []
    for run in CHECKS:
        out += run()
    out += art() or []
    return out


def report() -> str:
    lines = []
    estimates = estimate()
    for e in estimates:
        kind = "boss" if e.boss else "wave"
        waits = f"  waits {list(e.waits)}" if e.waits else ""
        lines.append(
            f"{kind} {e.wave:2d} {e.name:14s} strong {e.strong:5.1f} s  good {e.good:5.1f} s  fast {e.fast:5.1f} s"
            + (f"  peak {e.peak}" if not e.boss else "")
            + waits
        )
    strong, good, fast = run_minutes(estimates)
    lines.append(
        f"run: strong {strong:.1f} min, good {good:.1f} min, fast {fast:.1f} min (overhead {overhead():.0f} s)"
    )
    endless = endless_arrival()
    lines.append(f"endless (REINFORCED and ARMOURED stacked): {len(endless)} waits at the good bot's rate")
    lines += [f"  {line}" for line in endless]
    if art() is None:
        lines.append("art check skipped: the sprites have no missing_art() yet (it runs at integration)")
    return "\n".join(lines)


def main() -> int:
    failures = check()
    print(report())
    if failures:
        print(f"\n{len(failures)} problem(s):")
        for line in failures:
            print(f"  {line}")
        return 1
    print("\nthe tables are valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
