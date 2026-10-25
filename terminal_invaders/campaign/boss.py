"""Bosses (spec §9): parts, phases with per-part hp floors, movement, attacks through the director's floors, hazards,
phase breaks and the kill. Step 6 of the tick (§14.2); the phase checks run in step 11.

A part's hp counts down through every phase it is open in. Its floor in a phase is the hp of the later phases it is
open in, so a phase ends when each of its open parts reaches its floor, and the last phase's floors are 0. Attacks
hold no director slot: each has its own cooldown (divided by `pressure`), never overlaps itself, and waits with its
cooldown held while its shots or marks wouldn't fit under the caps. Shots are classed safe or reaction like the
director's, and every shot meets F1 and spawns outside every part's box (F2).
"""

from dataclasses import dataclass, field, replace
from math import floor

from ..model import Alien, Boss, Hazard, Part
from . import director
from .fleet import RING, dive_warning, place, release_mite
from .tables import BOSSES, LIMITS, PLAYER, PROFILES, SHOTS, TELEGRAPHS, TIMING

X_MIN, X_MAX = LIMITS.field
GROUND = 19.0  # a beam column and a mark reach down to here
ORBITING = "pod"  # the parts that move round BossRow.orbit
_ATTACKS = {}  # (boss, phase, profile) -> the phase's attacks with the profile's variants: a pure table lookup
_ESCORTS = {}  # (boss, phase, index, profile) -> the escort Flight with the profile's variants
_ORBIT = {}  # boss -> (the orbit's legs, its length, each orbiting part's starting distance along it)


@dataclass(slots=True)
class AttackState:
    """One attack of the current phase (hashed)."""

    key: str
    clock: float = 0.0  # cooldown: ticks since it last started
    stage: int = 0  # 0 idle, 1 telegraph, 2 firing, 3 beams live
    timer: float = 0.0  # ticks left in the stage
    tele: int = 0  # its telegraph after every multiplier and the floor
    track: float = 0.0  # a beam: ticks its mark still follows the source
    left: int = 0  # shots still to fire
    wait: float = 0.0  # ticks to the next shot
    shot: int = 0  # shots fired this use
    turn: int = 0  # uses so far (turrets alternate, sprays reverse, the spiral steps)
    source: int = 0  # the firing part's id
    aim: float = 0.0  # the ship's x when the telegraph began
    gap: float = 0.0  # a curtain's gap centre
    cls: str = "safe"
    done: bool = False  # an `at` attack has fired
    marks: list = field(default_factory=list)  # its hazards' ids
    owed: int = 0  # enemy shots this use still owes the field (a cluster counts 3): director.load() counts them


@dataclass(slots=True)
class Motion:
    """The boss's movement (hashed)."""

    leg: int = 0  # the path waypoint it heads for
    direction: int = 1
    dip: float = 0.0  # ticks since the last dip
    stage: int = 0  # 0 at its row, 1 going down, 2 holding, 3 coming back
    hold: float = 0.0
    row: float = 0.0  # its row this phase
    orbit: float = 0.0  # how far the orbiting parts have moved along BossRow.orbit (kept from phase to phase)
    descend: float = 0.0  # rows a tick in its intro: the descent over the intro that runs (150 ticks, a continue's 90)


def row_of(game):
    return BOSSES[game.boss.kind]


def attacks_of(kind, phase, profile) -> tuple:
    """The phase's attacks used on this profile, with its variants applied (§9's Easy and Hard changes)."""
    key = (kind, phase, profile)
    found = _ATTACKS.get(key)
    if found is None:
        row = BOSSES[kind]
        variants = PROFILES[profile].bosses
        found = []
        for attack in row.phases[phase].attacks:
            if profile not in attack.profiles:
                continue
            for variant in row.variants:
                if variant.profile == variants and variant.attack == attack.key and variant.phase in (-1, phase):
                    attack = replace(attack, **{variant.field: variant.value})
            found.append(attack)
        found = _ATTACKS[key] = tuple(found)
    return found


def escort_of(kind, phase, index, profile):
    """Escort flight `index` of a phase (phase -1: the boss's own escorts) with the profile's "escort" variants: a
    variant for phase -1 changes every escort flight, one for phase n only that phase's."""
    key = (kind, phase, index, profile)
    found = _ESCORTS.get(key)
    if found is None:
        row = BOSSES[kind]
        found = (row.escorts if phase < 0 else row.phases[phase].escorts)[index]
        variants = PROFILES[profile].bosses
        for variant in row.variants:
            if variant.profile == variants and variant.attack == "escort" and variant.phase in (-1, phase):
                found = replace(found, **{variant.field: variant.value})
        _ESCORTS[key] = found
    return found


def _orbit(row):
    """(legs, length, starts): the orbit's legs as (x0, y0, x1, y1, start, length), measured in the max norm (its
    legs run straight across or down; no libm), its whole length, and each orbiting part's starting distance."""
    found = _ORBIT.get(row.kind)
    if found is None:
        path = row.orbit
        legs, total = [], 0.0
        for index, (x0, y0) in enumerate(path):
            x1, y1 = path[(index + 1) % len(path)]
            span = max(abs(x1 - x0), abs(y1 - y0))
            legs.append((x0, y0, x1, y1, total, span))
            total += span
        starts = []
        for part in row.parts:
            if part.name != ORBITING or not path:
                starts.append(0.0)
                continue
            px, py = part.dx, part.y - row.station[1]
            best = 0.0
            for x0, y0, x1, y1, start, span in legs:
                lo_x, hi_x, lo_y, hi_y = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
                if span and lo_x - 1e-9 <= px <= hi_x + 1e-9 and lo_y - 1e-9 <= py <= hi_y + 1e-9:
                    best = start + max(abs(px - x0), abs(py - y0))
                    break
            starts.append(best)
        found = _ORBIT[row.kind] = (tuple(legs), total, tuple(starts))
    return found


def _orbit_at(row, distance) -> tuple:
    """The point `distance` along the orbit (dx, dy from the boss's centre and station row)."""
    legs, total, _starts = _orbit(row)
    distance %= total
    for x0, y0, x1, y1, start, span in legs:
        if distance < start + span:
            share = (distance - start) / span
            return x0 + (x1 - x0) * share, y0 + (y1 - y0) * share
    x0, y0 = legs[0][0], legs[0][1]
    return x0, y0


def floor_hp(row, name, phase) -> int:
    """A part's hp floor in a phase: the hp of every later phase it is open in."""
    return sum(later.hp for later in row.phases[phase + 1 :] if name in later.vulnerable)


# --- building, the intro and the phases ------------------------------------------------------------------------------


def build(game, row):
    """The boss at its intro: above its station, descending to it while the intro runs (game._clock is set: the
    boss intro's 150 ticks, or a continue's 90, so it reaches its station on the intro's last tick)."""
    x, y = row.station
    top = y - row.descent
    parts = []
    for part in row.parts:
        pool = sum(phase.hp for phase in row.phases if part.name in phase.vulnerable)
        parts.append(Part(game._new_id(), part.name, x + part.dx, top + part.y - y, part.w, pool, pool, "shielded"))
    total = sum(part.hp for part in parts)
    game.boss = Boss(x, top, total, total, kind=row.kind, vx=0.0, parts=parts, phases=len(row.phases))
    game.boss.timer = game._clock
    game._attacks = []
    game._motion = Motion(row=y, descend=row.descent / game._clock)
    _states(game, 0)
    for index, flight in enumerate(row.escorts):
        game._schedule(flight.at, ("boss", -1, index))


def intro(game):
    """A boss intro tick: the boss moves down to its station (movement only; no attacks or marks)."""
    boss, row = game.boss, row_of(game)
    if boss is None:
        return
    boss.timer -= 1
    station = row.station[1]
    boss.y = min(station, boss.y + game._motion.descend)
    _place(boss, row, game._motion.orbit)


def fight(game):
    """The intro ended: the boss is at its station and phase 0 starts."""
    boss = game.boss
    if boss is None or boss.state != "intro":
        return
    row = row_of(game)
    boss.y = row.station[1]
    _place(boss, row, game._motion.orbit)
    _enter(game, 0)


def _states(game, phase):
    """Each part's state in a phase: open while above its floor there, shielded, or broken at 0 hp."""
    boss, row = game.boss, row_of(game)
    vulnerable = row.phases[phase].vulnerable
    for part in boss.parts:
        if part.hp <= 0:
            part.state = "broken"
        elif part.name in vulnerable and part.hp > floor_hp(row, part.name, phase):
            part.state = "open"
        else:
            part.state = "shielded"


def _enter(game, phase):
    boss, row = game.boss, row_of(game)
    boss.phase, boss.state, boss.timer = phase, "fighting", 0
    _states(game, phase)
    profile = game.config.difficulty
    game._attacks = [AttackState(attack.key) for attack in attacks_of(boss.kind, phase, profile)]
    game._motion = Motion(row=game._motion.row, orbit=game._motion.orbit)
    spec = row.phases[phase]
    if spec.move == "ram":
        game._motion.dip = float(spec.dip[2])  # the ram starts its first descent at once
    for index, flight in enumerate(spec.escorts):
        game._schedule(game._play + flight.at, ("boss", phase, index))


def _place(boss, row, orbit=0.0):
    station = row.station[1]
    if row.orbit:
        starts = _orbit(row)[2]
        for part, spec, start in zip(boss.parts, row.parts, starts, strict=True):
            if spec.name == ORBITING:
                dx, dy = _orbit_at(row, start + orbit)
                part.x, part.y = boss.x + dx, boss.y + dy
            else:
                part.x, part.y = boss.x + spec.dx, boss.y + spec.y - station
        return
    for part, spec in zip(boss.parts, row.parts, strict=True):
        part.x, part.y = boss.x + spec.dx, boss.y + spec.y - station


def damage(game, part) -> str:
    """A player shot strikes a part: "hit" when it takes the damage, "neutral" when it is shielded."""
    boss = game.boss
    if boss.state != "fighting" or part.state not in ("open", "charge"):
        return "neutral"
    row = row_of(game)
    if part.name not in row.phases[boss.phase].vulnerable:
        return "neutral"
    part.hp -= 1
    boss.hp -= 1
    if part.hp <= floor_hp(row, part.name, boss.phase):
        part.state = "broken" if part.hp <= 0 else "shielded"
    return "hit"


def check(game):
    """Step 11: a phase whose open parts have all reached their floors breaks, or the boss dies in its last phase."""
    boss = game.boss
    if boss is None or boss.state != "fighting":
        return
    row = row_of(game)
    spec = row.phases[boss.phase]
    if spec.vulnerable:
        if any(p.name in spec.vulnerable and p.hp > floor_hp(row, p.name, boss.phase) for p in boss.parts):
            return
    elif game._pending or any(alien.flight >= 0 for alien in game.aliens):
        return  # an escorts-only phase ends when its flights have all arrived and been destroyed
    if boss.phase + 1 >= len(row.phases):
        game._boss_killed()
        return
    broken = sum(1 for p in boss.parts if p.name in spec.vulnerable) if spec.vulnerable else 0
    boss.state, boss.timer = "break", TIMING.phase_break
    cancel(game)
    game._phase_break(broken)


def cancel(game):
    """Every attack in progress ends and its marks go (a bomb, the mercy clear, a phase break)."""
    marks = []
    for state in game._attacks:
        marks += state.marks
        state.marks = []
        state.stage, state.left, state.timer, state.owed, state.track = 0, 0, 0.0, 0, 0.0  # an idle boss holds
    if marks:
        game.hazards = [hazard for hazard in game.hazards if hazard.id not in marks]
    boss = game.boss
    if boss is not None:
        for part in boss.parts:
            if part.state == "charge":
                part.state = "open"
        if boss.state == "fighting":
            _states(game, boss.phase)


# --- the tick --------------------------------------------------------------------------------------------------------


def step(game, slow):
    """Step 6: a break or the dying count down; otherwise the boss moves and attacks."""
    boss = game.boss
    if boss is None:
        return
    if boss.state == "break":
        boss.timer -= 1
        if boss.timer <= 0:
            _enter(game, boss.phase + 1)
        return
    if boss.state == "dying":
        boss.timer -= 1
        if boss.timer <= 0:
            game.boss = None
        return
    if boss.state != "fighting":
        return
    row = row_of(game)
    _move(game, row, slow)
    _attack(game, row, slow)


def frozen(game) -> bool:
    """A phase break or the dying: steps 5-8 wait (the boss invulnerable and silent, the schedule frozen)."""
    boss = game.boss
    return boss is not None and boss.state in ("break", "dying")


def _move(game, row, slow):
    boss, motion = game.boss, game._motion
    spec = row.phases[boss.phase]
    lo, hi = _bounds(row)
    move = spec.move
    speed = spec.speed * slow
    if move in ("patrol", "sway", "drift"):
        if spec.path:
            lo, hi = max(lo, spec.path[0][0]), min(hi, spec.path[-1][0])
        boss.x += speed * motion.direction
        if boss.x >= hi:
            boss.x, motion.direction = hi, -1
        elif boss.x <= lo:
            boss.x, motion.direction = lo, 1
    elif move == "figure8" and spec.path:
        tx, ty = spec.path[motion.leg % len(spec.path)]
        dx, dy = tx - boss.x, ty - boss.y
        if abs(dx) <= speed and abs(dy) <= speed:
            boss.x, boss.y = tx, ty
            motion.leg += 1
        else:
            reach = max(abs(dx), abs(dy))
            boss.x += dx / reach * speed
            boss.y += dy / reach * speed
    elif move == "track":
        if any(state.track > 0 for state in game._attacks):
            dx = game.player.x - boss.x
            boss.x += min(speed, max(-speed, dx))
    elif move == "descend" and spec.path:
        boss.y = min(spec.path[-1][1], boss.y + speed)
    boss.x = min(hi, max(lo, boss.x))
    if spec.dip[2] and move != "descend":
        _dip(boss, motion, spec, slow)
    if spec.orbit and row.orbit:
        motion.orbit = (motion.orbit + spec.orbit * slow) % _orbit(row)[1]
    _place(boss, row, motion.orbit)


def _dip(boss, motion, spec, slow):
    """Dip to a row, hold there, come back (WARDEN every 10 s; DREADNOUGHT's ram)."""
    target, hold, every = spec.dip
    speed = (spec.speed if spec.move == "ram" else spec.dip_speed) * slow
    if motion.stage == 0:
        motion.dip += slow
        if motion.dip >= every:
            motion.stage, motion.dip = 1, 0.0
    elif motion.stage == 1:
        boss.y = min(target, boss.y + speed)
        if boss.y >= target:
            motion.stage, motion.hold = 2, float(hold)
    elif motion.stage == 2:
        motion.hold -= slow
        if motion.hold <= 0:
            motion.stage = 3
    else:
        boss.y = max(motion.row, boss.y - speed)
        if boss.y <= motion.row:
            motion.stage = 0


def _bounds(row):
    """The boss centre's x range that keeps every part inside the field (orbiting parts anywhere on their orbit)."""
    left = min(part.dx - part.w / 2 for part in row.parts)
    right = max(part.dx + part.w / 2 for part in row.parts)
    if row.orbit:
        half = max(part.w for part in row.parts if part.name == ORBITING) / 2
        left = min(left, min(dx for dx, _dy in row.orbit) - half)
        right = max(right, max(dx for dx, _dy in row.orbit) + half)
    return X_MIN - left, X_MAX - right


# --- attacks ---------------------------------------------------------------------------------------------------------


def _attack(game, row, slow):
    boss = game.boss
    attacks = attacks_of(boss.kind, boss.phase, game.config.difficulty)
    pressure = game._pressure()
    for attack, state in zip(attacks, game._attacks, strict=True):
        if state.stage == 0:
            if attack.at:
                if not state.done and _share(game, row) <= attack.at:
                    state.done = True
                    _spawn(game, attack)
                continue
            if attack.cooldown <= 0 or game._lockout:
                continue
            state.clock += slow
            if state.clock >= attack.cooldown / pressure:
                _start(game, attack, state)
        elif state.stage == 1:
            state.timer -= slow
            if state.marks:
                director.mark_ttl(game, state.marks, state.timer)
            if state.track > 0:
                state.track -= slow
                _follow(game, state)
                if state.track <= 0:
                    _lock(game, state)
            if state.timer <= 0:
                _release(game, attack, state)
        elif state.stage == 2:
            state.wait -= slow
            if state.wait <= 0:
                _volley(game, attack, state)
        else:
            state.timer -= slow
            if state.marks:
                director.mark_ttl(game, state.marks, state.timer)  # a live beam: the ticks it has left
            if state.timer <= 0:
                _clear_marks(game, state)
                state.stage = 0


def _share(game, row) -> float:
    """How much of the phase's open hp is left (1.0 at its start)."""
    boss = game.boss
    spec = row.phases[boss.phase]
    if not spec.vulnerable or not spec.hp:
        return 1.0
    names = [p for p in boss.parts if p.name in spec.vulnerable]
    left = sum(max(0, p.hp - floor_hp(row, p.name, boss.phase)) for p in names)
    return left / (spec.hp * len(names))


def _sources(game, attack) -> list:
    return [part for part in game.boss.parts if part.name == attack.source and part.state != "broken"]


def _weight(attack) -> int:
    """Shots one of the attack's shots counts for under the cap: a cluster splits into 3 pellets."""
    return 3 if attack.shot == "cluster" else 1


def _needs(attack) -> tuple[int, int]:
    """(shots, marks) one use of an attack puts on the field, for the caps."""
    if attack.shot == "beam":
        return 0, attack.count + (1 if attack.count >= 2 else 0)  # a double beam marks its safe lane too
    if attack.shot == "spawn":
        return 0, 0
    shots = 1 if attack.count == 0 else attack.count
    if attack.shot == "cluster":
        shots *= 3
    if attack.shot == "dart" and attack.aim == "converge":
        shots = 2
    return shots, 1 if attack.aim == "curtain" else 0


def _start(game, attack, state):
    if attack.shot == "spawn":
        state.clock = 0.0
        _spawn(game, attack)
        return
    sources = _sources(game, attack)
    if not sources:
        state.clock = 0.0
        return
    if attack.aim == "curtain" and any(hazard.kind == "beam" for hazard in game.hazards):
        return  # a curtain waits while beams are marked or live (§9.4's cap rule)
    shots, marks = _needs(attack)
    if director.load(game) + shots > LIMITS.enemy_shots or len(game.hazards) + marks > LIMITS.hazards:
        return  # it waits, with its cooldown held, until they fit
    source = sources[state.turn % len(sources)]
    state.turn += 1
    state.clock, state.stage, state.source, state.aim, state.shot = 0.0, 1, source.id, game.player.x, 0
    state.owed = shots  # held against the shot cap from the telegraph on (R1)
    share = game._fire.reaction * PROFILES[game.config.difficulty].reaction
    state.cls = "reaction" if share > 0 and game.rng.random() < share else "safe"
    tele = director.telegraph(game, attack.telegraph)
    if attack.shot == "beam":
        tele = max(tele, LIMITS.beam_lock)
        state.track = float(max(0, min(attack.track, tele - LIMITS.beam_lock)))
        _beam_marks(game, attack, state, source)
    elif attack.aim == "curtain":
        _gap_mark(game, attack, state, tele)
    state.tele, state.timer = tele, float(tele)
    if state.marks:
        director.mark_ttl(game, state.marks, tele)
    if source.state == "open":
        source.state = "charge"
    game.events.append("tell")


def _release(game, attack, state):
    """The telegraph ends: shots start (stage 2) or beams go live (stage 3)."""
    _restore(game, state)
    if attack.shot == "beam":
        for hazard in game.hazards:
            if hazard.id in state.marks and hazard.kind == "beam":  # its lane mark is never live
                hazard.state = "live"
        state.stage, state.timer = 3, float(attack.live)
        game.events.append("beam")
        return
    state.stage, state.wait, state.shot = 2, 0.0, 0
    state.left = attack.count if attack.count else 1
    state.owed = 0 if attack.aim == "spiral" else state.left * _weight(attack)  # a spiral checks each pellet itself
    _volley(game, attack, state)


def _restore(game, state):
    for part in game.boss.parts:
        if part.id == state.source and part.state == "charge":
            part.state = "open"


def _source(game, state):
    for part in game.boss.parts:
        if part.id == state.source:
            return part
    return None


def _volley(game, attack, state):
    """Fire the attack's next shot (or all of them, for a volley fired at once)."""
    source = _source(game, state)
    if source is None or source.state == "broken":
        state.stage, state.left, state.owed = 0, 0, 0
        return
    elapsed = attack.spacing * state.shot
    if attack.aim == "spiral":  # continuous: a pellet (two, doubled) every `spacing` ticks for the rest of the phase
        table = attack.table or (0.0,)
        for side in range(max(1, attack.count)):
            if director.load(game) < LIMITS.enemy_shots and not game._lockout:  # a pellet that won't fit is skipped
                vx = table[(state.shot + side * len(table) // 2) % len(table)]
                tele = state.tele if state.shot == 0 else 0  # only the first volley follows the telegraph (F1)
                _shoot(game, attack, state, source.x, vx, tele)
        state.shot += 1
        state.wait = float(attack.spacing)
        return
    if attack.spacing == 0:
        state.owed = 0  # its shots are on the field now (those F1 skipped never will be)
        flight = _all_at_once(game, attack, state, source)
        state.stage, state.left = 0, 0
        if attack.aim == "curtain":  # its gap mark stays until the pellets have passed the ship's row
            state.stage, state.timer = 3, float(flight)
        return
    index = state.shot
    count = attack.count
    vx = 0.0
    x = source.x
    if attack.aim in ("spray", "spray_alternate"):
        reverse = attack.aim == "spray_alternate" and state.turn % 2 == 0
        step = -attack.spread + 2 * attack.spread * index / max(1, count - 1)
        vx = -step if reverse else step
    weight = _weight(attack)
    state.owed = max(0, state.owed - weight)
    if director.load(game) + weight <= LIMITS.enemy_shots:  # a guard: the reservation above should always hold
        _shoot(game, attack, state, x, vx, state.tele + elapsed, aim=attack.aim in ("aimed", "lead"))
    state.shot += 1
    state.left -= 1
    if state.left > 0:
        state.wait = float(attack.spacing)
    else:
        state.stage, state.owed = 0, 0


def _all_at_once(game, attack, state, source) -> int:
    """A volley fired at once; returns its longest flight."""
    count = attack.count
    flight = 0
    if attack.aim == "fan":
        for index in range(count):
            vx = -attack.spread + 2 * attack.spread * index / max(1, count - 1)
            flight = max(flight, _shoot(game, attack, state, source.x, vx, state.tele))
    elif attack.aim == "curtain":
        for x in _curtain(game, attack, state):
            flight = max(flight, _shoot(game, attack, state, x, 0.0, state.tele))
    elif attack.aim == "shelters":
        for x in _shelter_targets(game, count):
            flight = max(flight, _shoot(game, attack, state, source.x, 0.0, state.tele, target=x))
    elif attack.aim == "converge":
        pods = sorted(_sources(game, attack), key=lambda part: part.x)
        pair = [pods[0], pods[-1]] if len(pods) > 1 else pods
        longest = _together(game, attack, state, pair)
        for part in pair:
            speed = None
            if longest:
                speed = (LIMITS.hit_y - _spawn_y(game, part.x, part.y + LIMITS.boss_spawn)) / longest
            flight = max(flight, _shoot(game, attack, state, part.x, 0.0, state.tele, aim=True, source=part,
                                        speed=speed))  # fmt: skip
    else:
        for index in range(count):
            offset = 2.0 * (index - (count - 1) / 2)
            aim = attack.aim in ("aimed", "lead")
            flight = max(flight, _shoot(game, attack, state, source.x + offset, 0.0, state.tele, aim=aim))
    return flight


def _together(game, attack, state, pair) -> float:
    """A converging pair's common fall time to the ship's row (the slower dart's, and a safe pair's at least the
    safe warning), so both darts arrive together; 0.0 when either would fail F1 at it (each then fires alone)."""
    longest = 0.0
    speed = director.shot_speed(game, attack.shot, game._fire.v_a)
    for part in pair:
        y = _spawn_y(game, part.x, part.y + LIMITS.boss_spawn)
        longest = max(longest, (LIMITS.hit_y - y) / speed)
    if state.cls == "safe":
        longest = max(longest, LIMITS.safe_warning - state.tele)
    for part in pair:
        y = _spawn_y(game, part.x, part.y + LIMITS.boss_spawn)
        if director.plan(y, (LIMITS.hit_y - y) / longest, state.tele, "reaction") is None:
            return 0.0
    return longest


def _spawn_y(game, x, y) -> float:
    """Below every boss part's box (F2)."""
    for _ in range(len(game.boss.parts)):
        moved = False
        for part in game.boss.parts:
            if abs(x - part.x) <= part.w / 2 and abs(y - part.y) <= 0.45 and y < part.y + 0.46:
                y, moved = part.y + 0.46, True
        if not moved:
            break
    return y


def _shoot(game, attack, state, x, vx, tele, aim=False, target=None, source=None, speed=None) -> int:
    """One boss shot through the director's floors; returns its flight ticks (0 when F1 skips it). `speed`: a
    converging pair's common speed, already slowed as its class needs (F1 is still checked at it)."""
    part = source or _source(game, state)
    kind = attack.shot
    y = _spawn_y(game, x, part.y + LIMITS.boss_spawn)
    x = min(X_MAX, max(X_MIN, x))
    if speed:
        planned = director.plan(y, speed, tele, "reaction")
    else:
        planned = director.plan(y, director.shot_speed(game, kind, game._fire.v_a), tele, state.cls)
    if planned is None:
        return 0
    vy, w, flight = planned
    bound = SHOTS[kind].vx_max or SHOTS["dart"].vx_max
    if aim:
        if attack.aim == "lead" or kind == "hunter":
            goal = director.lead_target(game, x, y, vy)
        else:
            goal = state.aim if PROFILES[game.config.difficulty].aim == "tell" else game.player.x
        vx = director.aim_vx(x, y, vy, goal, bound)
    elif target is not None:
        vx = director.aim_vx(x, y, vy, target, bound)
    director.launch(game, x, y, kind, vx, vy, 0, "boss", w, flight, "", part.name)
    return flight


def _shelter_targets(game, count) -> list:
    """The centres of the `count` most intact shelters (the ship's x when none stand)."""
    totals = {}
    for (x, _y), hp in game.bunkers.items():
        centre = PROFILES[game.config.difficulty].shelters
        nearest = min(centre, key=lambda c: abs(c - x))
        totals[nearest] = totals.get(nearest, 0) + hp
    best = sorted(totals, key=lambda c: (-totals[c], c))[:count]
    return best or [game.player.x]


def _gap_mark(game, attack, state, tele):
    half = attack.gap / 2
    low, high = LIMITS.gap_x
    reach = floor(LIMITS.gap_reach - half)
    centre = min(high - half, max(low + half, game.player.x + game.rng.randint(-reach, reach)))
    state.gap = centre
    hazard = Hazard(game._new_id(), "gap", centre - half, centre + half, LIMITS.spawn_y, GROUND, "lock", tele)
    game.hazards.append(hazard)
    state.marks.append(hazard.id)


def _curtain(game, attack, state) -> list:
    """The pellets' x: `count` spread across `width` cells centred on the ship, none within the marked gap."""
    half = attack.width / 2
    centre = min(X_MAX - half, max(X_MIN + half, game.player.x))
    gap_lo, gap_hi = state.gap - attack.gap / 2 - 0.5, state.gap + attack.gap / 2 + 0.5
    segments = [
        (max(X_MIN, centre - half), min(gap_lo, centre + half)),
        (max(gap_hi, centre - half), min(X_MAX, centre + half)),
    ]
    segments = [(a, b) for a, b in segments if b > a]
    total = sum(b - a for a, b in segments)
    count = attack.count
    xs = []
    if total <= 0 or count <= 0:
        return xs
    for index in range(count):
        along = total * index / max(1, count - 1)
        for a, b in segments:
            if along <= b - a + 1e-9:
                xs.append(a + min(along, b - a))
                break
            along -= b - a
    return xs


def _beam_marks(game, attack, state, source):
    """A beam's mark (two, a gap apart, for a double beam, with the safe lane between them marked), fixed from its
    mark unless it tracks."""
    half = attack.width / 2
    xs = [source.x] if attack.count <= 1 else [source.x - attack.gap / 2, source.x + attack.gap / 2]
    beams = []
    for x in xs:
        x = min(X_MAX - half, max(X_MIN + half, x))
        locked = "warn" if state.track > 0 else "lock"
        hazard = Hazard(game._new_id(), "beam", x - half, x + half, source.y + 0.5, GROUND, locked, 0)
        game.hazards.append(hazard)
        state.marks.append(hazard.id)
        beams.append(hazard)
    if len(beams) == 2:
        left, right = sorted(beams, key=lambda hazard: hazard.x0)
        lane = Hazard(game._new_id(), "lane", left.x1, right.x0, LIMITS.spawn_y, GROUND, "lock", 0)
        game.hazards.append(lane)
        state.marks.append(lane.id)


def _follow(game, state):
    source = _source(game, state)
    if source is None:
        return
    for hazard in game.hazards:
        if hazard.id in state.marks and hazard.kind == "beam":
            half = (hazard.x1 - hazard.x0) / 2
            x = min(X_MAX - half, max(X_MIN + half, source.x))
            hazard.x0, hazard.x1 = x - half, x + half


def _lock(game, state):
    for hazard in game.hazards:
        if hazard.id in state.marks:
            hazard.state = "lock"


def _clear_marks(game, state):
    if state.marks:
        game.hazards = [hazard for hazard in game.hazards if hazard.id not in state.marks]
        state.marks = []


def beam_hits(game) -> bool:
    """A live beam overlaps the ship's hit box."""
    x = game.player.x
    reach = PLAYER.box[0]
    return any(h.kind == "beam" and h.state == "live" and h.x0 - reach < x < h.x1 + reach for h in game.hazards)


def _spawn(game, attack):
    """Spawn attacks: guards re-enter, the ring refills, divers and mites are released below the source."""
    boss, row = game.boss, row_of(game)
    if attack.kind == "weaver":
        _refill(game, attack, row)
        return
    if attack.kind == "guard":
        for index, flight in enumerate(row.escorts):
            if any(kind == attack.kind for kind, _count in flight.mix):
                game._schedule(game._play, ("boss", -1, index))
        return
    sources = [part for part in boss.parts if part.name == attack.source]
    if not sources:
        return
    part = sources[0]
    y = part.y + 0.45 + attack.offset
    for index in range(attack.count):
        x = part.x + 3.0 * (index - (attack.count - 1) / 2)
        if attack.kind == "mite":
            release_mite(game, x, y)
        elif attack.kind == "diver":
            _diver(game, x, y)


def _refill(game, attack, row):
    """The ring refills to `count` while fewer than `below` of its weavers live: only the missing weavers come, each
    into an empty place of the ring (the places `count` weavers take), with the ring's flight and its variants."""
    alive = [alien for alien in game.aliens if alien.kind == attack.kind and alien.hp > 0 and alien.flight >= 0]
    if attack.below and len(alive) >= attack.below:
        return
    places = [slot * RING // attack.count for slot in range(attack.count)]
    taken = {}
    for alien in alive:
        state = game._flights[alien.flight]
        if state.ring:
            taken[state.ring[alien.id - state.base]] = True
    free = [where for where in places if where not in taken][: attack.count - len(alive)]
    index = next((i for i, f in enumerate(row.escorts) if any(k == attack.kind for k, _n in f.mix)), -1)
    if not free or index < 0 or len(game.aliens) + len(free) > LIMITS.enemies:
        return
    flight = escort_of(row.kind, -1, index, game.config.difficulty)
    flight = replace(flight, size=len(free), dims=(attack.count,), mix=((attack.kind, len(free)),))
    place(game, flight, game._escort_top(), escort=True, ring=tuple(free))


def _diver(game, x, y):
    """A boss's diver: released below her box, it wobbles 30 ticks there, then dives under the F1 dive rule."""
    if len(game.aliens) >= LIMITS.enemies or dive_warning(game, "diver", y)[0] < LIMITS.dive_warning:
        return
    alien = Alien(x, y, "diver", 1, id=game._new_id(), state="telegraph", home_x=x, home_y=y)
    alien.timer = TELEGRAPHS["dive"]
    game.aliens.append(alien)
    game.events.append("tell")
