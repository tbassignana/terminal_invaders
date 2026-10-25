"""What a practised player has learned about the rules, read from the public tables (spec §18: imported, never
copied). Nothing here reads a game: every function takes what the Frame shows (wave, profile, the ship's upgrades
and pickups) and answers from campaign.tables."""

from dataclasses import replace
from functools import cache
from math import floor

from terminal_invaders.campaign.tables import (
    ARRIVAL,
    BOSSES,
    DIVE,
    ENDLESS,
    ENEMIES,
    FIRE,
    LIMITS,
    PICKUP,
    PICKUPS,
    PLAYER,
    PROFILES,
    SHELTERS,
    SHOTS,
    TELEGRAPHS,
    TIMING,
    UFO,
    UPGRADES,
    WAVES,
)

X_MIN, X_MAX = PLAYER.x_min, PLAYER.x_max
SHIP_Y = PLAYER.y
BOX_X, BOX_Y = PLAYER.box
HIT_Y = LIMITS.hit_y
MUZZLE_Y = FIRE.muzzle_y
GATE = FIRE.gate
RAM_X, RAM_Y = DIVE.ram
DIVE_EXIT = DIVE.exit_y
CELL = 0.48  # a shelter cell's half-size, the collision module's (the cells are one cell apart)
SHELTER_ROWS = tuple(range(SHELTERS.top, SHELTERS.top + 3))
ENEMY_BOX = {name: kind.box for name, kind in ENEMIES.items()}
UFO_BOX = UFO.box
PART_HALF_Y = 0.45
SLOW = PICKUPS["slow"].factor
ARM = TIMING.arm
FREEZE = TIMING.freeze
SAFE = TIMING.safe
ABSORB = TIMING.absorb
LANDING = 15.5
SHOOTERS = frozenset(name for name, kind in ENEMIES.items() if kind.shot)
DIVERS = frozenset(name for name, kind in ENEMIES.items() if kind.behaviour in ("dive", "mite"))
ENEMY_SHOTS = frozenset(SHOTS)


def wave_row(wave: int):
    """The wave's script row: the Campaign's, then Endless's loop of ENDLESS.loop (acts II-IV)."""
    if wave <= len(WAVES):
        return WAVES[wave - 1]
    first, last = ENDLESS.loop
    return WAVES[first - 1 + (wave - len(WAVES) - 1) % (last - first + 1)]


def stacks(ship, name: str) -> int:
    for owned, count in ship.upgrades:
        if owned == name:
            return count
    return 0


def timed(ship, name: str) -> int:
    """Seconds left on a timed pickup as the bottom row shows it (0: not running)."""
    for owned, seconds in ship.pickups:
        if owned == name:
            return seconds
    return 0


def ship_speed(ship) -> float:
    return PLAYER.speed + stacks(ship, "THRUSTERS") * UPGRADES["THRUSTERS"].value


def bullet_speed(ship) -> float:
    speed = FIRE.speed + stacks(ship, "RAIL ROUNDS") * UPGRADES["RAIL ROUNDS"].value
    return speed * PICKUPS["rapid"].factor if timed(ship, "rapid") else speed


def catch_reach(ship) -> tuple[float, float]:
    """(|dx| a pickup is caught within, cells a tick it falls)."""
    if stacks(ship, "MAGNET"):
        return UPGRADES["MAGNET"].value, PICKUP.fall * UPGRADES["MAGNET"].extra
    return PICKUP.catch[0], PICKUP.fall


def telegraph(base: int, profile: str, scanner: bool) -> int:
    """A telegraph as the director stretches it: the profile's factor, SCANNER, then the floor; 0 stays 0."""
    if base <= 0:
        return 0
    factor = PROFILES[profile].telegraph * (UPGRADES["SCANNER"].value if scanner else 1.0)
    return max(LIMITS.telegraph, floor(base * factor + 0.5))


@cache
def nominal_vy(kind: str, wave: int, profile: str) -> float:
    """A shot kind's full speed in this wave: its factor of the act's pellet speed, the profile's, then the cap."""
    return min(LIMITS.vy, SHOTS[kind].speed * wave_row(wave).director.v_a * PROFILES[profile].speed)


@cache
def fall(kind: str, wave: int, profile: str) -> tuple[float, float]:
    """(A shot kind's full speed in this wave, the most ticks a shot of the kind takes to fall from the top to the
    ship's row): safe shots are slowed to a 60-tick warning, and a crossfire's darts to the pair's longer fall."""
    full = nominal_vy(kind, wave, profile)
    return full, max(LIMITS.safe_warning, (HIT_Y - ARRIVAL.top) / full)


def slowest_vy(kind: str, wave: int, profile: str, y: float) -> float:
    """The slowest a shot first seen at row y can fall (fall())."""
    full, longest = fall(kind, wave, profile)
    return min(full, max(0.02, (HIT_Y - y) / longest))


def dive_vy(kind: str, profile: str) -> float:
    return PROFILES[profile].dive_vy * ENEMIES[kind].dive_speed


def dive_vx(ship_x: float, x: float, y: float, vy: float) -> float:
    """The table's dive rule: aimed at the ship's x at launch, within DIVE.vx_max."""
    ticks = (SHIP_Y - y) / vy
    return min(DIVE.vx_max, max(-DIVE.vx_max, (ship_x - x) / ticks))


@cache
def attacks(boss: str, profile: str) -> tuple:
    """Every attack of the boss used on this profile, every phase, with the profile's variants applied."""
    row = BOSSES[boss]
    out = []
    for index, phase in enumerate(row.phases):
        for attack in phase.attacks:
            if profile not in attack.profiles:
                continue
            for variant in row.variants:
                if variant.profile == profile and variant.attack == attack.key and variant.phase in (-1, index):
                    attack = replace(attack, **{variant.field: variant.value})
            out.append(attack)
    return tuple(out)


@cache
def vx_bounds(wave: int, profile: str) -> dict:
    """The |vx| bound of each enemy shot kind in this wave: the kind's own, a split's pellets, and the boss's fans,
    sprays, spirals and aimed shots."""
    bounds = {kind: spec.vx_max for kind, spec in SHOTS.items()}
    row = wave_row(wave)
    kinds = {kind for flight in row.flights for kind, _count in flight.mix}
    split = max(abs(vx) for vx in SHOTS["cluster"].split)
    if "splitter" in kinds:
        bounds["pellet"] = max(bounds["pellet"], split)
    if row.boss:
        boss = BOSSES[row.boss]
        for flight in (*boss.escorts, *(f for phase in boss.phases for f in phase.escorts)):
            if any(kind == "splitter" for kind, _count in flight.mix):
                bounds["pellet"] = max(bounds["pellet"], split)
        for attack in attacks(row.boss, profile):
            shot = attack.shot
            if shot not in SHOTS:
                continue
            if attack.aim in ("fan", "spray", "spray_alternate"):
                bound = attack.spread
            elif attack.aim == "spiral":
                bound = max((abs(vx) for vx in attack.table), default=0.0)
            elif attack.aim in ("aimed", "lead", "converge", "shelters"):
                bound = SHOTS[shot].vx_max or SHOTS["dart"].vx_max
            else:
                bound = 0.0
            if shot == "cluster":
                bounds["pellet"] = max(bounds["pellet"], split, bound)
            bounds[shot] = max(bounds[shot], bound)
    return bounds


@cache
def beam_timing(boss: str, profile: str, scanner: bool) -> tuple[int, int, int]:
    """(earliest, latest) ticks from a beam's mark to its going live, and the longest it stays live."""
    teles, lives = [], []
    for attack in attacks(boss, profile):
        if attack.shot == "beam":
            teles.append(max(telegraph(attack.telegraph, profile, scanner), LIMITS.beam_lock))
            lives.append(attack.live)
    if not teles:
        return LIMITS.beam_lock, LIMITS.beam_lock, 0
    return min(teles), max(teles), max(lives)


def dive_tell(kind: str) -> int:
    return TELEGRAPHS["mite" if kind == "mite" else "dive"]


KIND_HP = {name: kind.hp for name, kind in ENEMIES.items()}
KIND_POINTS = {name: (kind.points, kind.points * kind.dive_points) for name, kind in ENEMIES.items()}  # plain, diving


def hp(seen) -> int:
    """An enemy's hp as drawn: its kind's (plated: one more), one when it shows cracked."""
    if seen.cracked:
        return 1
    return KIND_HP[seen.kind] + (1 if seen.veteran else 0)


def points(seen) -> int:
    """Its kind's points, times the dive bonus while it dives."""
    plain, diving = KIND_POINTS[seen.kind]
    return diving if seen.pose == "diving" else plain


def priority(name: str, owned: int) -> int:
    """The bots' rank of a card offer (lower first): the table's per-stack rank, Endless fillers last."""
    upgrade = UPGRADES.get(name)
    if upgrade is None:
        return 1000
    return upgrade.priority[owned] if owned < len(upgrade.priority) else 999


def code(name: str) -> str:
    upgrade = UPGRADES.get(name)
    return upgrade.code if upgrade is not None else name
