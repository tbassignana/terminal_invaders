"""The bots' view (spec §3.5): `observe(game)` returns a frozen Frame equal to what the arena draws plus the HUD's text.

A Frame holds no velocities, exact hp, timers, rng, seed or callables. Positions are rounded to 0.25 cell. Only
`Frame` is a dataclass; its parts are NamedTuples and its containers tuples, which keeps a busy tick cheap.

observe() is incremental (§2.3): a per-game cache (`game._seen`, presentation) keeps the last Seen built for each
id and reuses it while its rounded inputs are unchanged, keeps a whole tuple when nothing in it changed, and
rebuilds the shelters only when `game.shelter_rev` moves. Entities are in id order, so the cache is walked in step.
"""

from dataclasses import dataclass, fields
from math import ceil, floor
from typing import NamedTuple

from ..model import TICK_RATE
from .tables import BOMB, ENEMIES, UPGRADES


class Ship(NamedTuple):
    x: float
    y: float
    lives: int
    bombs: int
    charge_pct: int  # the HUD's BOMB nn%: floor(100 x charge / the charge a bomb needs)
    slots: int  # shot slots (GUN)
    free_slots: int
    guarded: bool  # drawn as the shielded ship: a guard or a SHIELD pickup's hits left
    blinking: bool  # invulnerable
    upgrades: tuple[tuple[str, int], ...]  # (name, stacks) in pick order
    pickups: tuple[tuple[str, int], ...]  # (timed pickup, seconds left), as the bottom row shows them


class Seen(NamedTuple):
    """An enemy, a shot or a pickup. Shots fill `friendly`; enemies fill pose, cracked, veteran and cargo."""

    id: int
    x: float
    y: float
    kind: str
    pose: str = ""  # enemies: Alien.state (model.POSES)
    cracked: bool = False  # enemies: drawn cracked (see `cracked`)
    veteran: bool = False
    cargo: bool = False
    friendly: bool = False  # shots: the player's


class PartSeen(NamedTuple):
    id: int
    x: float
    y: float
    w: int
    boss: str  # the boss's kind (the art prefix)
    name: str
    state: str  # shielded, open, charge, broken
    bar: int  # the boss bar's filled segments, 0-12 (the same for every part of one boss)


class HazardSeen(NamedTuple):
    id: int
    kind: str  # beam, gap, lane
    x0: float
    x1: float
    y0: float
    y1: float
    locked: bool  # its x is final (a tracking beam's mark may still move until then)
    live: bool  # a beam that hits now


@dataclass(frozen=True, slots=True)
class Frame:
    tick: int
    phase: str  # model.PHASES
    mode: str
    profile: str  # easy, normal, hard
    wave: int
    act: int
    loop: int
    ship: Ship
    enemies: tuple[Seen, ...]
    shots: tuple[Seen, ...]
    parts: tuple[PartSeen, ...]
    hazards: tuple[HazardSeen, ...]
    shelters: tuple[tuple[int, int, int], ...]  # (x, y, tier 1-3 as drawn), sorted by (y, x)
    pickups: tuple[Seen, ...]
    ufo: tuple[float, float] | None
    card: tuple[tuple[str, ...], int, bool] | None  # offers, cursor, armed (True once a fire edge takes the card)
    prompt: tuple[int, int] | None  # seconds left, continues left
    modifiers: tuple[str, ...]


# Each field's slot setter, in field order. Frame's frozen __init__ goes through object.__setattr__ by name for every
# field, about 2 µs a tick; _frame() sets the slots directly, a quarter faster, and builds the same value.
_SETTERS = tuple(Frame.__dict__[field.name].__set__ for field in fields(Frame))


def _frame(*values) -> Frame:
    """Frame(*values): every field, in order."""
    frame = object.__new__(Frame)
    for put, value in zip(_SETTERS, values):
        put(frame, value)
    return frame


BAR_SEGMENTS = 12


def quarter(value: float) -> float:
    """Round to the 0.25-cell grid the Frame reports positions on: floor(4v + 0.5) / 4 (`// 1` is floor without the
    call, and as fast as the float it returns)."""
    return (4 * value + 0.5) // 1 / 4


def boss_bar(boss) -> int:
    """Filled segments of the boss bar: any hp left shows at least one."""
    if boss is None or boss.max_hp <= 0 or boss.hp <= 0:
        return 0
    return min(BAR_SEGMENTS, ceil(BAR_SEGMENTS * boss.hp / boss.max_hp))


def cracked(alien) -> bool:
    """Drawn with its _cracked frame: it has lost hp, and its kind has 2+ hp (§3.7; a plated scout has no such frame)."""
    return alien.hp < alien.max_hp and ENEMIES[alien.kind].hp >= 2


def charge_needed(game) -> int:
    """The charge that adds a bomb: BOMB.charge, or CAPACITOR's."""
    return int(UPGRADES["CAPACITOR"].value) if "CAPACITOR" in game.upgrades else BOMB.charge


class Cache:
    """observe()'s per-game memory (game._seen): the last tuples it returned."""

    __slots__ = ("enemies", "pickups", "rev", "shelters", "shots")

    def __init__(self):
        self.enemies = self.shots = self.pickups = self.shelters = ()
        self.rev = None


ENEMY_HP = {name: kind.hp for name, kind in ENEMIES.items()}
# A NamedTuple built from all its field values in order: the same value and type as calling the class, without the
# Python-level __new__ and its keyword handling (a busy tick builds dozens).
_new = tuple.__new__


def _enemies(aliens, prev):
    out = []
    append = out.append
    changed = len(prev) != len(aliens)
    index, count = 0, len(prev)
    for alien in aliens:
        ident = alien.id
        while index < count and prev[index][0] < ident:
            index += 1
            changed = True
        x, y = (4 * alien.x + 0.5) // 1 / 4, (4 * alien.y + 0.5) // 1 / 4
        state = alien.state
        crack = alien.hp < alien.max_hp and ENEMY_HP[alien.kind] >= 2
        if index < count and prev[index][0] == ident:
            old = prev[index]
            index += 1
            if old[1] == x and old[2] == y and old[4] == state and old[5] == crack:
                append(old)
                continue
        changed = True
        append(_new(Seen, (ident, x, y, alien.kind, state, crack, alien.veteran, alien.cargo, False)))
    return tuple(out) if changed else prev


def _things(items, prev, shots):
    out = []
    append = out.append
    changed = len(prev) != len(items)
    index, count = 0, len(prev)
    for item in items:
        ident = item.id
        while index < count and prev[index][0] < ident:
            index += 1
            changed = True
        x, y = (4 * item.x + 0.5) // 1 / 4, (4 * item.y + 0.5) // 1 / 4
        if index < count and prev[index][0] == ident:
            old = prev[index]
            index += 1
            if old[1] == x and old[2] == y:
                append(old)
                continue
        changed = True
        append(_new(Seen, (ident, x, y, item.kind, "", False, False, False, item.friendly if shots else False)))
    return tuple(out) if changed else prev


def observe(game) -> Frame:
    cache = game._seen
    if cache is None:
        cache = game._seen = Cache()
    player = game.player
    busy = len(dict.fromkeys([shot.volley for shot in game.shots if shot.friendly]))  # shot slots held: one per volley
    charge = floor(100 * game.bomb_charge / charge_needed(game))
    free = player.weapon - busy
    powerups = game.powerups
    ship = _new(
        Ship,
        (
            quarter(player.x),
            quarter(player.y),
            player.lives,
            player.bombs,
            charge if charge < 100 else 100,  # min(100, charge) and max(0, free), without the calls
            player.weapon,
            free if free > 0 else 0,
            player.guard > 0 or player.shield_hits > 0,
            player.invulnerable > 0,
            tuple(game.upgrades.items()),
            tuple([(name, ceil(ticks / TICK_RATE)) for name, ticks in powerups.items() if ticks > 0])
            if powerups
            else (),
        ),
    )
    cache.enemies = enemies = _enemies(game.aliens, cache.enemies)
    cache.shots = shots = _things(game.shots, cache.shots, True)
    cache.pickups = pickups = _things(game.pickups, cache.pickups, False)
    boss = game.boss
    parts = ()
    if boss is not None:
        bar = boss_bar(boss)
        kind = boss.kind
        parts = tuple(
            [
                _new(PartSeen, (part.id, quarter(part.x), quarter(part.y), part.w, kind, part.name, part.state, bar))
                for part in boss.parts
            ]
        )
    hazards = ()
    if game.hazards:
        hazards = tuple(
            HazardSeen(
                hazard.id,
                hazard.kind,
                quarter(hazard.x0),
                quarter(hazard.x1),
                quarter(hazard.y0),
                quarter(hazard.y1),
                hazard.state != "warn",
                hazard.state == "live",
            )
            for hazard in game.hazards
        )
    rev = (game.shelter_rev, len(game.bunkers))
    if cache.rev != rev:
        cache.rev = rev
        cache.shelters = tuple(sorted((y, x, min(3, hp)) for (x, y), hp in game.bunkers.items()))
        cache.shelters = tuple((x, y, tier) for y, x, tier in cache.shelters)
    ufo = None if game.ufo is None else (quarter(game.ufo.x), quarter(game.ufo.y))
    card = (tuple(game.offers), game.cursor, game.armed == 0) if game.phase == "upgrade" else None
    prompt = (ceil(game.prompt / TICK_RATE), game.continues) if game.phase == "continue" else None
    return _frame(
        game.tick,
        game.phase,
        game.config.mode,
        game.config.difficulty,
        game.wave,
        game.act,
        game.loop,
        ship,
        enemies,
        shots,
        parts,
        hazards,
        cache.shelters,
        pickups,
        ufo,
        card,
        prompt,
        tuple(game.modifiers),
    )
