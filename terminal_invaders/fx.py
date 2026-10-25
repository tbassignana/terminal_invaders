"""Phase 5's effects: bursts, debris, the field kick, the starfield and drifting popups (spec 2).

Every effect is a pure function of what the arena already draws: the game's presentation Effects (their text, place
and ttl), game.tick, the scale k, the tier and the effect level. An effect therefore looks the same live, in a
replay, at any frame rate, after a resize and in a golden. Randomness comes from mix(), a fold of the values into
rng.splitmix64 with a salt per effect, never from the game's streams, the random module or a clock. Nothing that
changes what is drawn is kept between frames but the FlashLimiter's admitted kicks (spec 2.9); the caches below only
remember the answers of pure functions.

The functions here read the game and never write it (spec 1.2): they look at game.effects, game.tick and
game.config, and build new lists.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import NamedTuple

from .campaign.tables import COMBO, UFO
from .model import HEIGHT, WIDTH
from .rng import MASK64, splitmix64

# The salts mix() starts from, one per effect (spec 1.2).
STARS = 0x5354415253
DEBRIS = 0x44454252
KICK = 0x4B49434B

FLASH_LIMIT = 3  # flashes (each admitted kick counts as one) in any FLASH_WINDOW ticks: WCAG 2.3.1's 3 a second
FLASH_WINDOW = 60  # ticks: one second of game time

# The k = 1 burst (spec 2.3): three frames of 4 ticks each, centred on the kill's row. The glyphs & ; , are drawn
# nowhere else in the arena (they are kept out of sprites.py's art keys), so a burst never reads as a shot or pickup.
BURST = {
    "small": ("&", "; ;", ",   ,"),
    "big": ("&&&", ";   ;", ",     ,"),
}
BURST_TICKS = 4  # each of the burst's three frames, ages 1-4, 5-8 and 9-12
RADII = {"small": (0.5, 1.0, 1.5), "big": (1.0, 2.0, 3.0)}  # k >= 2: the ring's radius in columns, by frame
DEBRIS_FROM = 5  # debris flies from age 5, after the burst's first frame
DEBRIS_CAP = 64  # debris points a frame, youngest sources first
SKY = HEIGHT - 1  # star rows 0-18: they never reach the ground row (19), so telegraph marks stay clean
STAR_COUNT, FAR = 32, 20  # stars 0-19 are the far layer, 20-31 the near one
STAR_SPEED = (0.5, 1.0)  # rows a second, downward, by layer
RISE_TICKS, RISE_MAX = 10, 2  # k >= 2: a kill popup rises a cell row every 10 ticks, at most 2
# What a UFO kill can pay (game.py _ufo_kill: UFO.high or UFO.low times the combo). An enemy killed while it enters
# also dies on the UFO's row (golden-classic-normal's tick 16126: a scout's +10 at y 1.0), so the row alone is not
# enough to tell them apart.
UFO_POINTS = frozenset(points * combo for points in (UFO.high, UFO.low) for combo in range(1, COMBO.most + 1))


class Link(NamedTuple):
    """What the link allows this frame (spec 2.8): Renderer.frame sets painter.link = Link(remote=caps.remote,
    congested=pacer.congested, clock=pacer.disabled); no pacer gives Link(). remote (SSH, or a slow probe RTT) and
    clock (clock pacing: no acks to measure the link) turn the kick off; congested turns `full` into `calm`."""

    remote: bool = False
    congested: bool = False
    clock: bool = False


class Level(NamedTuple):
    """What the effects draw this frame (spec 2.8), from level().

    stars: "move" (full), "still" (calm: their tick-0 places) or "off" (Phase 4's: the fixed STARS at k = 1, none at
    k >= 2); bursts and debris: drawn or not (debris also never at k = 1); rise: "drift" (full: k >= 2 popups rise
    min(2, age // 10) rows, k = 1 popups sit one row up), "offset" (calm: the k = 1 row only) or "none"; kick:
    whether a kick may start (the limiter still admits each one)."""

    stars: str
    bursts: bool
    debris: bool
    rise: str
    kick: bool


OFF = Level(stars="off", bursts=False, debris=False, rise="none", kick=False)  # Phase 4's frame, byte for byte
FULL = Level(stars="move", bursts=True, debris=True, rise="drift", kick=True)
CALM = Level(stars="still", bursts=True, debris=True, rise="offset", kick=False)
LEVELS = {"full": FULL, "calm": CALM, "off": OFF}


class Source(NamedTuple):
    """An effect that sets effects off (spec 2.2): what it is, where it is centred, and when it was born."""

    kind: str  # "kill", "ufo", "boss", "phase" or "hit"
    text: str  # the Effect's text: with the birth tick, the key the flash limiter counts kicks by
    x: float  # the centre, in model units
    y: float
    birth: int  # game tick the Effect was made at
    age: int  # ticks since then: an Effect is first seen at age 1
    size: str  # "small" or "big"
    dots: int  # the ring's dots at k >= 2
    debris: int  # debris points it throws (k >= 2)
    kick: int  # ticks the field kicks for (0: none)


class Burst(NamedTuple):
    """One burst frame to draw (spec 2.3): centred on (x, y), frame 0-2, `dots` dots at k >= 2."""

    x: float
    y: float
    size: str
    frame: int
    dots: int


def mix(*values: int) -> int:
    """A 64-bit hash of the values, each folded into rng.splitmix64 in turn (spec 1.2): pure, and the same on every
    Python. Callers start with a salt: mix(DEBRIS, birth, ix, iy, i)."""
    state = 0
    for value in values:
        output, _ = splitmix64((state ^ value) & MASK64)
        state = output
    return state


def level(options, link: Link | None, game, mono: bool = False) -> Level:
    """The effect level for this frame (spec 2.8), in order: reduced motion forces `off`; then options.effects
    (`full`, `calm` or `off`; options without the attribute, as test fakes, read `off`); then the link (congested:
    `full` becomes `calm`; remote or clock: no kick); then mono (the tier the arena draws in) drops debris and moving
    stars. Effects draw only in the Campaign arena: an Arcade game is always `off`. The arena calls it once a frame."""
    if getattr(options, "reduced_motion", False):
        return OFF
    name = getattr(options, "effects", "off")
    config = getattr(game, "config", None)
    if name not in ("full", "calm") or getattr(config, "mode", None) == "arcade":
        return OFF
    link = link or Link()
    if name == "full" and link.congested:
        name = "calm"
    found = LEVELS[name]
    if found.kick and (link.remote or link.clock):
        found = found._replace(kick=False)
    if mono:
        found = found._replace(stars="off", debris=False)
    return found


def recognise(effect) -> tuple[str, int] | None:
    """(kind, its ttl at birth) of an Effect that sets effects off (spec 2.2), or None: a `+N` popup is an enemy's
    kill, or the UFO's when it is on the UFO's row and N is a UFO's points; `+N,NNN` (a comma: every boss is worth
    1,000 or more) a boss's; `PHASE BREAK` and `HIT` (a ship lost) theirs. The ring, BOMB READY and the pickup names
    set nothing off."""
    text = effect.text
    if text[:1] == "+" and text[1:].replace(",", "").isdigit():
        if "," in text:
            return "boss", 90
        ufo = effect.y == UFO.y and int(text[1:]) in UFO_POINTS
        return ("ufo" if ufo else "kill"), 30
    if text == "HIT":
        return "hit", 45
    if text == "PHASE BREAK":
        return "phase", 60
    return None


# kind: (burst size, ring dots at k >= 2, debris points, kick ticks); an enemy kill's debris is 6-10, from its hash
KINDS = {"kill": ("small", 8, 0, 0), "ufo": ("big", 12, 14, 0), "boss": ("big", 16, 24, 10)}
KINDS |= {"phase": ("big", 12, 12, 6), "hit": ("big", 12, 12, 6)}


def sources(game) -> list[Source]:
    """The game's Effects that set effects off, in the game's order (oldest first)."""
    found, tick = [], game.tick
    for effect in game.effects:
        known = recognise(effect)
        if known is None:
            continue
        kind, ttl0 = known
        age = ttl0 - effect.ttl
        x, y = effect.x, effect.y + (1 if kind == "hit" else 0)  # HIT is drawn over the ship: the burst is the ship's
        size, dots, debris, kick = KINDS[kind]
        if kind == "kill":
            debris = 6 + mix(DEBRIS, tick - age, math.floor(4 * x), math.floor(4 * y)) % 5
        found.append(Source(kind, effect.text, x, y, tick - age, age, size, dots, debris, kick))
    return found


def bursts(game, k: int, level: Level, found: list[Source] | None = None) -> list[Burst]:
    """The bursts showing this frame (spec 2.3): each source's frame 0, 1 or 2 by its age (1-4, 5-8, 9-12; an age
    below 1 counts as 1), none after age 12 or when the level draws no bursts. `found` is sources(game), when the
    caller has it. At k = 1 a phase break's burst goes two rows down: the PHASE BREAK text sits on its source's row
    and the pickup the break drops on the row below, at the same column (campaign/game.py `_drops(boss.x, boss.y +
    2)`), both drawn after the bursts; the pickup falls into the burst's row only at about age 12, when its last
    frame leaves the middle empty. At k >= 2 the text covers one cell row of the ring, which still shows."""
    if not level.bursts:
        return []
    shown = []
    for source in sources(game) if found is None else found:
        frame = (max(1, source.age) - 1) // BURST_TICKS
        if frame < 3:
            y = source.y + (2 if k == 1 and source.kind == "phase" else 0)
            shown.append(Burst(source.x, y, source.size, frame, source.dots))
    return shown


@lru_cache(maxsize=64)
def ring_offsets(size: str, frame: int, dots: int) -> tuple[tuple[float, float], ...]:
    """A burst ring's dots as offsets in model units from its centre: `dots` points on a circle of the frame's radius
    in columns, its rows halved (a cell is twice as tall as wide), so it looks round."""
    radius = RADII[size][frame]
    turns = (2 * math.pi * index / dots for index in range(dots))
    return tuple((radius * math.cos(turn), 0.5 * radius * math.sin(turn)) for turn in turns)


@lru_cache(maxsize=512)
def flights(birth: int, ix: int, iy: int, count: int) -> tuple[tuple[float, float, float, int], ...]:
    """A source's debris points as (cos, sin, speed, life), from mix(DEBRIS, birth, ix, iy, i) (spec 2.4): an angle
    from the low 16 bits, 0.05-0.13 columns a tick (3 to 8 columns a second) from the next 8, 16-24 ticks of life
    from the rest. A pure function of its arguments: the cache only saves the hashing."""
    points = []
    for index in range(count):
        h = mix(DEBRIS, birth, ix, iy, index)
        angle = (h & 0xFFFF) / 65536 * 2 * math.pi
        speed = 0.05 + ((h >> 16) & 0xFF) / 255 * 0.08
        points.append((math.cos(angle), math.sin(angle), speed, 16 + (h >> 24) % 9))
    return tuple(points)


def debris(game, k: int, level: Level, found: list[Source] | None = None) -> list[tuple[float, float, str]]:
    """The debris points of spec 2.4 as (x, y, role) in model units: none at k = 1 or when the level draws none (mono
    draws none: level() drops it). From age 5 each point flies out and slows to a stop, reach = speed * e * (1 - e /
    (2 * life)) with e = age - 4, drawn at (x + cos * reach, y + sin * reach / 2) while e < life, in role "spark" for
    the first half of its life and "star" after (it fades); points off the field are left out, and at most 64 are
    drawn, the youngest sources' first."""
    if k < 2 or not level.debris:
        return []
    out = []
    for source in sorted(sources(game) if found is None else found, key=lambda source: -source.birth):
        elapsed = source.age - (DEBRIS_FROM - 1)
        if elapsed < 1:
            continue
        ix, iy = math.floor(4 * source.x), math.floor(4 * source.y)
        for cos, sin, speed, life in flights(source.birth, ix, iy, source.debris):
            if elapsed >= life:
                continue
            reach = speed * elapsed * (1 - elapsed / (2 * life))
            x, y = source.x + cos * reach, source.y + 0.5 * sin * reach
            if -0.5 <= x < WIDTH - 0.5 and -0.5 <= y < HEIGHT - 0.5:  # cell x + 0.5 lies on the field
                out.append((x, y, "spark" if 2 * elapsed < life else "star"))
                if len(out) == DEBRIS_CAP:
                    return out
    return out


# Every star's fixed sky place (spec 2.6): x in columns 0-60, y0 in rows 0-19, the same in every run.
FIELD = tuple(
    ((mix(STARS, index, 0) % 3840) / 64, (mix(STARS, index, 1) % 1216) / 64, 0 if index < FAR else 1)
    for index in range(STAR_COUNT)
)


def stars(tick: int, level: Level) -> list[tuple[float, float, int]]:
    """The 32 stars of spec 2.6 as (x, y, layer) at game tick `tick`, in field units: x in [0, 60) columns and y in
    [0, 19) rows from the field's top-left corner, so a star lies in cell (floor(x), floor(y)) and never on the
    ground row. They fall (y0 + speed * tick / 60) mod 19, the far layer (0) at 0.5 rows a second and the near one (1)
    at 1.0; "still" holds them at their tick-0 places; "off" draws none (the arena keeps Phase 4's)."""
    if level.stars == "off":
        return []
    seconds = tick / 60 if level.stars == "move" else 0.0
    return [(x, (y0 + STAR_SPEED[layer] * seconds) % SKY, layer) for x, y0, layer in FIELD]


def kick(game, level: Level, limiter: FlashLimiter | None, found: list[Source] | None = None) -> int:
    """The field's sideways step this frame (spec 2.5): +1 or -1 (from mix(KICK, birth) & 1) during ages 1-6 of a HIT
    or phase break and ages 1-10 of a boss kill, when the level allows a kick and the limiter admits the source (the
    youngest first; no limiter admits every one); else 0. The caller checks that the scene and the game are playing.
    """
    if not level.kick:
        return 0
    kicking = [source for source in (sources(game) if found is None else found) if 1 <= source.age <= source.kick]
    for source in sorted(kicking, key=lambda source: -source.birth):
        if limiter is None or limiter.admit((source.text, source.birth), game.tick):
            return 1 if mix(KICK, source.birth) & 1 else -1
    return 0


def rise(effect, k: int, level: Level) -> int:
    """How many cell rows a kill popup (+N, +N,NNN) is drawn above its place (spec 2.7): at k >= 2 under "drift",
    min(2, age // 10); at k = 1 one row under "drift" or "offset", so the burst shows under it; HIT, PHASE BREAK,
    BOMB READY and pickup names never move. The caller keeps it on the field's top row."""
    if level.rise == "none" or effect.text[:1] != "+":
        return 0
    known = recognise(effect)
    if known is None or known[0] not in ("kill", "ufo", "boss"):
        return 0
    if k == 1:
        return 1
    return min(RISE_MAX, (known[1] - effect.ttl) // RISE_TICKS) if level.rise == "drift" else 0


def shift(px, w: int, dx: int):
    """The pixel layer's rows (w pixels wide) moved dx pixels sideways, the vacated columns background (spec 2.5): the
    arena shifts the layer before packing, so term/pixels.py's packer is unchanged. A new bytearray; px itself when
    dx is 0."""
    if not dx:
        return px
    out, height = bytearray(len(px)), len(px) // w
    if dx > 0:
        out[dx:] = px[:-dx]
        columns = range(dx)
    else:
        out[:dx] = px[-dx:]
        columns = range(w + dx, w)
    for column in columns:
        out[column::w] = bytes(height)
    return out


class FlashLimiter:
    """The backstop of spec 2.9, on game time: at most `limit` flashes (kicks) in any `window` ticks.

    A key (the source's text and birth tick) already admitted stays admitted; a new key is admitted while fewer than
    `limit` keys were admitted in the last `window` ticks; a tick lower than the last seen (a new game, a replay
    restart) clears it. The Renderer holds one (Renderer.limiter) and hands it to every Painter it builds, so it
    survives reconfigure; each golden gets a fresh one."""

    def __init__(self, limit: int = FLASH_LIMIT, window: int = FLASH_WINDOW):
        self.limit, self.window = limit, window
        self.admitted: dict[object, int] = {}  # key -> the tick it was admitted at
        self.last: int | None = None

    def admit(self, key, tick: int) -> bool:
        """Whether the kick named key may draw at this tick."""
        if self.last is not None and tick < self.last:
            self.admitted.clear()
        self.last = tick
        if key in self.admitted:
            return True
        recent = sum(1 for at in self.admitted.values() if tick - at < self.window)
        if recent >= self.limit:
            return False
        for old in [old for old, at in self.admitted.items() if tick - at >= self.window]:
            del self.admitted[old]  # out of every window from now on, and never asked for again
        self.admitted[key] = tick
        return True
