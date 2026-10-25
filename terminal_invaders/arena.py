"""Arena views: how the play field between the HUD rows and the footer is drawn.

Both views draw the same things, in the same order, from the game's model objects (Phase 4 spec 15.4): the backdrop
and shelters, the telegraph marks, the UFO, boss parts, enemies in their poses with the veteran and cargo overlays,
pickups, live beams, shots, the ship, then the text: pickup letters, score popups and the boss bar. A kind, pose or
state without art is an error, never a stand-in sprite.

Phase 5's effects (fx.py, spec 2.10) hook in at fixed points: the starfield first (k >= 2: on background pixels
only), bursts (and debris, k >= 2) after the marks and before every entity, never over a mark or a shelter, the
field's kick around the packing, and drifting popups. At level `off` both views draw Phase 4's frame, apart from the
2-pixel cargo mark (CARGO_PIXELS) and the effect texts shown still under reduced motion (STILL_TEXTS)."""

import math
from collections import Counter
from typing import Protocol

from . import fx
from .campaign.tables import BOSSES
from .model import HEIGHT, POSES, WIDTH
from .sprites import (
    ALIENS,
    CARGO,
    CRACKED,
    CRACKED_CELL,
    GLYPHS,
    HAZARDS,
    PARTS,
    PICKUPS,
    RING,
    RING_RADIUS,
    RING_TICKS,
    SHIELDED,
    SHIP,
    SHOTS,
    SOLID,
    STAR,
    TELLS,
    UFO,
    artwork,
    backdrop,
    bounds,
    cell,
    placed,
)
from .term.pixels import Art, HalfBlockPacker, PixelLayer
from .theme import SLOT

GROUND = HEIGHT - 1  # the ground row: telegraph marks only, never stars
STARS = tuple(
    star
    for star in (((index * 37 + 11) % WIDTH, (index * 13 + 3) % HEIGHT) for index in range(30))
    if star[1] != GROUND
)
IDLE_TICKS = 20  # an idle 2-frame swap; frozen under reduced motion
WOBBLE_TICKS, WOBBLE = 6, 0.25  # a diver's or mite's wobble before its dive: frames swapped, and +-0.25 cell in pixels
PELLET_TICKS = 4  # the pellet's zig-zag
FLICKER = 12  # an invulnerable ship is hidden for the last 5 ticks of every 12
BREAK_TICKS = 8  # a phase break shakes the boss's parts between their frame and the cracked one
BURST_TICKS = 15  # a dying boss shows one burst at a time: 4 a second
FILL_TICKS = 15  # a curtain gap's dashes fill in from "- - -" to "-----" for the mark's last 15 ticks
GAP_REACH = 5  # dashes on each side of a curtain gap's brackets
RING_POINTS = 16, 32  # the bomb ring's dots at k = 1 and in pixels
BAR = 12  # cells in the boss bar
# Phase 5 (spec 3.7): the cargo mark at k >= 2 is a horizontal pair of pixels at the sprite's centre (1 draws Phase
# 4's single dot). Under reduced motion the effect texts (+N, HIT, PHASE BREAK, BOMB READY, pickup names) are drawn
# still where they would be (spec 2.8); False hides them, as Phase 4 did. tools/golden_identity.py sets both back.
CARGO_PIXELS = 2
STILL_TEXTS = True

NUMERALS = ("I", "II", "III", "IV", "V", "VI")
DIVE_KEYS = {"diver": "diving", "mite": "mite_diving"}  # the kinds that dive, and their dive pose
PART_STATES = {"shielded": "_shielded", "open": "", "charge": "_charge", "broken": None}  # Part.state -> frame
BOSS_STATES = ("intro", "fighting", "break", "dying")
MARK_STATES = ("warn", "lock")


class ArenaView(Protocol):
    def draw(self, painter, session) -> None: ...


def known(table, name, what):
    """table[name], or an error naming what has no art."""
    if name not in table:
        raise ValueError(f"no art for {what} {name!r}")
    return table[name]


def pose(alien, tick: int, still: bool, boss_wave: bool) -> tuple[str, int, str, float]:
    """How an enemy is drawn: (art key, frame, role, x offset of the pixel wobble).

    A shooter's telegraph is its tell frame and a damaged 2+ HP kind its cracked frame; both are state, so they show
    under reduced motion. A diver or mite about to dive wobbles in role diver: its two frames swap every 6 ticks (and
    it shakes +-0.25 cell in pixels); under reduced motion, or next to a boss, it shows the dive pose instead."""
    kind, state = alien.kind, alien.state
    known(ALIENS, kind, "enemy kind")
    if state not in POSES:
        raise ValueError(f"unknown pose {state!r} of a {kind}")
    if state == "diving" or (state == "telegraph" and kind in DIVE_KEYS):
        dive = known(DIVE_KEYS, kind, "the dive pose of")
        swap = (tick // WOBBLE_TICKS) % 2
        offset = 0.0 if state == "diving" or still else (WOBBLE if swap else -WOBBLE)
        if state == "diving" or still or boss_wave:
            return dive, 0, "diver", offset
        return kind, swap, "diver", offset
    if state == "telegraph":
        known(TELLS, kind, "the tell pose of")
        return f"{kind}_tell", 0, "alien", 0.0
    if kind in CRACKED and alien.hp < alien.max_hp:
        return f"{kind}_cracked", 0, "alien", 0.0
    return kind, 0 if still else (tick // IDLE_TICKS) % 2, "alien", 0.0


def glyph(key: str, frame: int) -> str:
    return ALIENS[key][frame] if key in ALIENS else GLYPHS[key]


def friendly(shot) -> bool:
    """Whether a shot is drawn as the player's (role player_shot) rather than as enemy fire: by its kind."""
    known(SHOTS, shot.kind, "shot kind")
    return shot.kind == "player"


def part_key(boss, part, tick: int, still: bool) -> str | None:
    """The art key of a boss part as drawn, or None when it is broken (gone)."""
    name = f"{boss.kind}.{part.name}"
    known(PARTS, name, "boss part")
    suffix = known(PART_STATES, part.state, f"the state of {name}:")
    if boss.state not in BOSS_STATES:
        raise ValueError(f"unknown boss state {boss.state!r}")
    if suffix is None:
        return None
    if boss.state == "dying" or (suffix == "" and 2 * part.hp < part.max_hp):
        suffix = "_cracked"  # below half its HP (spec 15.4), and every part of a dying boss
    if boss.state == "break" and not still and (tick // BREAK_TICKS) % 2:
        suffix = "" if suffix == "_cracked" else "_cracked"
    return name + suffix


def burst(boss, tick: int):
    """The part a dying boss bursts at now (one at a time, 4 a second), or None."""
    parts = [part for part in boss.parts if part.state != "broken"]
    return parts[(tick // BURST_TICKS) % len(parts)] if boss.state == "dying" and parts else None


def span(x0: float, x1: float) -> range:
    """The cells a hazard covers: those whose centre lies in [x0, x1), so x1 - x0 cells for a whole width."""
    cells = range(math.ceil(x0), math.ceil(x1))
    if not cells:
        raise ValueError(f"a hazard from x {x0} to {x1} covers no cell")
    return cells


def marks(hazards, shelters, step: int) -> list[tuple[int, int, str]]:
    """The telegraph tiles of the hazards' marks, (column, row, art key), on the field and off shelter cells.

    A beam's warning is a dotted column from its source down to the ground row, a dot every step rows, and once its
    x locks, chevrons on the ground row under its width. A curtain gap is a bracket each side of the gap with dashes
    reaching 5 cells out, every other one until the last 15 ticks of the mark. A safe lane is a bracket at each of its
    edge cells. A live beam is drawn as enemy fire instead (live())."""
    tiles = []
    for hazard in hazards:
        kind, state, cells = hazard.kind, hazard.state, span(hazard.x0, hazard.x1)
        if state not in MARK_STATES and not (kind == "beam" and state == "live"):
            raise ValueError(f"unknown state {state!r} of a {kind} hazard")
        if kind == "beam":
            if state == "live":
                continue
            column = cells[len(cells) // 2]
            tiles += [(column, row, "beam_warn") for row in range(GROUND - 1, cell(hazard.y0) - 1, -step)]
            if state == "lock":
                tiles += [(x, GROUND, "lane_mark") for x in cells]
        elif kind == "gap":
            left, right, full = cells[0] - 1, cells[-1] + 1, hazard.ttl <= FILL_TICKS
            tiles += [(left, GROUND, "gap_open"), (right, GROUND, "gap_close")]
            for reach in range(1, GAP_REACH + 1):
                if full or reach % 2:
                    tiles += [(left - reach, GROUND, "gap_mark"), (right + reach, GROUND, "gap_mark")]
        elif kind == "lane":
            tiles += [(cells[0], GROUND, "gap_open"), (cells[-1], GROUND, "gap_close")]
        else:
            raise ValueError(f"no art for hazard kind {kind!r}")
    return [(x, y, key) for x, y, key in tiles if 0 <= x < WIDTH and 0 <= y < HEIGHT and (x, y) not in shelters]


def live(hazards) -> list[tuple[int, int]]:
    """The cells of the live beams: their width, from the source to the row above the ground."""
    return [
        (x, y)
        for hazard in hazards
        if hazard.kind == "beam" and hazard.state == "live"
        for y in range(max(0, cell(hazard.y0)), min(cell(hazard.y1), GROUND - 1) + 1)
        for x in span(hazard.x0, hazard.x1)
        if 0 <= x < WIDTH
    ]


def ring(effect, points: int) -> list[tuple[float, float]]:
    """The bomb ring's dots in model units: a circle growing from the ship to the shockwave's reach as ttl runs out."""
    radius = RING_RADIUS * (1 - max(0, min(effect.ttl, RING_TICKS)) / RING_TICKS)
    turns = (2 * math.pi * index / points for index in range(points))
    return [(effect.x + radius * math.cos(turn), effect.y + radius * math.sin(turn)) for turn in turns]


def bar_cells(boss) -> int:
    """The boss bar's filled cells: 12 at full HP, never 0 while any HP is left (campaign.observe.boss_bar)."""
    return -(-BAR * max(0, boss.hp) // max(1, boss.max_hp))


def bar_marks(boss) -> list[int]:
    """The bar cells that mark where each phase after the first begins: the share of the boss's HP still left then.

    A phase's HP is its row's per-part HP (tables.BOSSES) times the boss's parts open in it, so the fill reaches a
    mark as the phase before it ends. A phase that starts at full HP (the MOTHERSHIP's escorts) has no mark."""
    phases = known(BOSSES, boss.kind, "boss").phases
    count = Counter(part.name for part in boss.parts)
    pools = [phase.hp * sum(count[name] for name in phase.vulnerable) for phase in phases]
    total, marks = sum(pools), []
    for index in range(1, len(pools)):
        mark = math.floor(BAR * sum(pools[index:]) / total + 0.5) if total else 0
        if 0 < mark < BAR and mark not in marks:
            marks.append(mark)
    return marks


def bar_text(boss) -> str:
    """`WARDEN  [########|...]  I`: the name, the 12-cell bar with a `|` at each phase threshold, the phase."""
    name = known(BOSSES, boss.kind, "boss").name
    cells = ["#" if index < bar_cells(boss) else "." for index in range(BAR)]
    for index in bar_marks(boss):
        cells[index] = "|"
    return f"{name}  [{''.join(cells)}]  {NUMERALS[boss.phase]}"


def boss_bar(painter, boss) -> None:
    painter.center(2, bar_text(boss), color=SLOT["danger"])


def effect_level(painter, game, mono: bool) -> fx.Level:
    """This frame's effect level (fx.level) for the painter's options and link (getattr: test fakes have neither)."""
    return fx.level(getattr(painter, "options", None), getattr(painter, "link", None), game, mono)


def kicking(painter, session, game, level: fx.Level, found: list) -> int:
    """The field's kick this frame (fx.kick, spec 2.5): only in the playing scene with no countdown, while the game
    plays, and as the painter's flash limiter admits it (Renderer.limiter; none admits every kick)."""
    if not level.kick or getattr(session, "scene", "playing") != "playing":
        return 0
    if getattr(session, "countdown_at", None) is not None or getattr(game, "phase", "playing") != "playing":
        return 0
    return fx.kick(game, level, getattr(painter, "limiter", None), found)


def texts(game, still: bool) -> list:
    """The Effects drawn as text: every one but the bomb ring, also under reduced motion (STILL_TEXTS)."""
    return [effect for effect in game.effects if effect.text != RING] if STILL_TEXTS or not still else []


class GlyphArena:
    """The k = 1 arena: one glyph string per sprite, drawn through the Painter on rows 2 to 21 in its role's slot.
    A sprite covers the cells from floor(x + 0.5) - len // 2 in row floor(y + 0.5), as the pixel arena centres it,
    so a fleet stepping half a cell descends evenly (bug h: round() took halves to even). While the field kicks,
    everything on it moves dx columns; the boss bar does not."""

    def draw(self, painter, session) -> None:
        game = session.pilot.game
        player, tick, still, shelters = game.player, game.tick, painter.options.reduced_motion, game.bunkers
        mono = getattr(getattr(painter, "theme", None), "tier", None) == "mono"
        level = effect_level(painter, game, mono)
        found = fx.sources(game) if level is not fx.OFF else []
        dx = kicking(painter, session, game, level, found)

        def at(start: int, row: int, text: str, role: str, bold: bool = False) -> None:
            start += dx
            text = text[max(0, -start) : max(0, WIDTH - start)]
            if text and 0 <= row < HEIGHT:
                painter.text(row + 2, max(0, start), text, SLOT[role], bold)

        def put(x: float, y: float, text: str, role: str, bold: bool = False, up: int = 0) -> None:
            row = cell(y)
            at(cell(x) - len(text) // 2, max(0, row - up) if up and row >= 0 else row, text, role, bold)

        if level.stars == "off":
            for x, y in STARS:
                put(x, y, STAR, "star")
        else:  # spec 2.6: the 32 stars in their cells, drawn first so everything covers them
            for x, y, _ in fx.stars(tick, level):
                at(math.floor(x), math.floor(y), STAR, "star")
        for (x, y), hp in shelters.items():
            put(x, y, SOLID if hp > 1 else CRACKED_CELL, "shelter")
        tiles = marks(game.hazards, shelters, 2)
        for x, y, key in tiles:
            put(x, y, HAZARDS[key], "telegraph")
        effects = [] if still else game.effects
        for effect in effects:
            if effect.text == RING:
                for x, y in {(cell(x), cell(y)) for x, y in ring(effect, RING_POINTS[0])}:
                    put(x, y, STAR, "telegraph")
        blocked = None
        for bloom in fx.bursts(game, 1, level, found):  # glyph by glyph: never over a mark or a shelter
            if blocked is None:
                blocked = {(x, y) for x, y, _ in tiles} | set(shelters)
            glyphs = fx.BURST[bloom.size][bloom.frame]
            row, start = cell(bloom.y), cell(bloom.x) - len(glyphs) // 2
            for offset, char in enumerate(glyphs):
                if char != " " and (start + offset, row) not in blocked:
                    at(start + offset, row, char, "spark")
        if game.ufo:
            put(game.ufo.x, game.ufo.y, UFO, "ufo", True)
        boss = game.boss
        if boss:
            for part in boss.parts:
                key = part_key(boss, part, tick, still)
                if key:
                    put(part.x, part.y, GLYPHS[key], "boss", True)
        for alien in game.aliens:
            key, frame, role, _ = pose(alien, tick, still, boss is not None)
            put(alien.x, alien.y, glyph(key, frame), role, alien.veteran)
            if alien.cargo and alien.state != "telegraph":  # the middle glyph; a tell keeps its whole shape
                put(alien.x, alien.y, CARGO, "pickup", alien.veteran)
        for pickup in game.pickups:
            put(pickup.x, pickup.y, "[" + known(PICKUPS, pickup.kind, "pickup kind") + "]", "pickup", True)
        for x, y in live(game.hazards):
            put(x, y, HAZARDS["beam_live"], "enemy_shot", True)
        for side in (False, True):  # the player's shots over enemy fire
            for shot in game.shots:
                if friendly(shot) is side:
                    put(shot.x, shot.y, SHOTS[shot.kind], "player_shot" if side else "enemy_shot")
        if not player.invulnerable or still or tick % FLICKER < 7:
            put(player.x, player.y, SHIELDED if player.shield or player.guard else SHIP, "player", True)
        for effect in texts(game, still):
            put(effect.x, effect.y, effect.text, "warn", up=fx.rise(effect, 1, level))
        if boss:
            part = None if still else burst(boss, tick)
            if part:
                put(part.x, part.y, "*", "warn")
            boss_bar(painter, boss)


class PixelArena:
    """The k > 1 arena: pixel art on a 60k x 40k PixelLayer, packed into the block's 20k arena rows with half blocks
    (2:1 pooled spaces where the terminal cannot show those), then the text over it: pickup letters, score popups
    and the boss bar. Sprites keep the glyph arena's rules for poses, frames, flicker and reduced motion.

    The backdrop is the act's (spec 15.4), in role star, under the shelters; the mono arena has none, as it draws
    shapes and no attributes. Veteran plating is a 1-px plate along the sprite's bottom edge, the cargo mark a pair of
    pixels in role pickup at its centre (not over a tell, as at k = 1). Phase 5's stars, debris and bursts draw one
    art pixel each (2 x 2 layer pixels at k = 4 and 6, which double px2 and px3) on background pixels only; the kick
    shifts the layer one pixel (one terminal column) before packing, and the text over the field with it."""

    def __init__(self, k: int, theme, caps=None):
        self.k, self.art = k, artwork(k)
        self.layer = PixelLayer(WIDTH * k, 2 * HEIGHT * k)
        self.mono = theme.tier == "mono"
        self.backdrops: dict[int, bytes] = {}
        self.ground, self.drawn = b"", None  # the backdrop with the shelters, and the (act, shelters) it shows
        self.looks: dict[tuple, Art] = {}  # (key, frame, role, veteran, cargo) -> the art as drawn
        self.dots = {role: Art(1, 1, bytes([SLOT[role]])) for role in ("telegraph",)}
        self.unit = 2 if k in (4, 6) else 1  # layer pixels a side of one art pixel
        blocks = caps is None or caps.unicode_blocks
        self.packer = HalfBlockPacker(theme, mono=self.mono, ambiguous_wide=not blocks)
        self.letter = theme.style("pickup")  # mono shows pickup letters reversed

    def draw(self, painter, session) -> None:
        game = session.pilot.game
        player, tick, still, shelters = game.player, game.tick, painter.options.reduced_motion, game.bunkers
        layer, put, k = self.layer, self.put, self.k
        level = effect_level(painter, game, self.mono)
        found = fx.sources(game) if level is not fx.OFF else []
        if self.drawn != (game.act, shelters):
            self.lay_ground(game.act, shelters)
        layer.px[:] = self.ground
        if level.stars != "off":
            self.sky(fx.stars(tick, level))
        for x, y, key in marks(game.hazards, shelters, 1):
            put(x, y, self.art[key][0])
        effects = [] if still else game.effects
        for effect in effects:
            if effect.text == RING:
                for x, y in ring(effect, RING_POINTS[1]):
                    layer.blit(math.floor((x + 0.5) * k), math.floor((y + 0.5) * 2 * k), self.dots["telegraph"])
        for x, y, role in fx.debris(game, k, level, found):
            self.speck(x, y, SLOT[role])
        spark = SLOT["spark"]
        for bloom in fx.bursts(game, k, level, found):
            for ox, oy in fx.ring_offsets(bloom.size, bloom.frame, bloom.dots):
                self.speck(bloom.x + ox, bloom.y + oy, spark)
        if game.ufo:
            put(game.ufo.x, game.ufo.y, self.art["ufo"][0])
        boss = game.boss
        if boss:
            for part in boss.parts:
                key = part_key(boss, part, tick, still)
                if key:
                    put(part.x, part.y, self.art[key][0])
        for alien in game.aliens:
            key, frame, role, offset = pose(alien, tick, still, boss is not None)
            art = self.look(key, frame, role, alien.veteran, alien.cargo and alien.state != "telegraph")
            layer.blit(*placed(alien.x + offset, alien.y, art, k), art)
        for pickup in game.pickups:
            known(PICKUPS, pickup.kind, "pickup kind")
            put(pickup.x, pickup.y, self.art["pickup"][0])
        beam = self.art["beam_live"][0]
        for x, y in live(game.hazards):
            put(x, y, beam)
        zigzag = 0 if still else (tick // PELLET_TICKS) % 2
        for side in (False, True):  # the player's shots over enemy fire
            for shot in game.shots:
                if friendly(shot) is side:
                    frames = self.art[shot.kind]
                    put(shot.x, shot.y, frames[zigzag % len(frames)])
        if not player.invulnerable or still or tick % FLICKER < 7:
            put(player.x, player.y, self.art["shielded" if player.shield or player.guard else "ship"][0])
        dx = kicking(painter, session, game, level, found)
        if dx:
            layer.px[:] = fx.shift(layer.px, layer.w, dx)
        self.packer.pack(layer, painter.screen, painter.top + 2, painter.left)
        for pickup in game.pickups:
            self.overlay(painter, pickup.x, pickup.y, PICKUPS[pickup.kind], self.letter, dx)
        warn = painter.colors[SLOT["warn"]]
        for effect in texts(game, still):
            self.overlay(painter, effect.x, effect.y, effect.text, warn, dx, fx.rise(effect, k, level))
        if boss:
            part = None if still else burst(boss, tick)
            if part:
                self.overlay(painter, part.x, part.y, "*", warn, dx)
            boss_bar(painter, boss)

    def sky(self, stars) -> None:
        """The starfield over the backdrop (spec 2.6): one art pixel a star, in role star, on background pixels only."""
        layer, unit, star = self.layer, self.unit, SLOT["star"]
        px, w, scale = layer.px, layer.w, self.k // unit
        for x, y, _ in stars:
            left, top = math.floor(x * scale) * unit, math.floor(y * 2 * scale) * unit
            for row in range(top, top + unit):
                for column in range(left, left + unit):
                    if not px[row * w + column]:
                        px[row * w + column] = star

    def speck(self, x: float, y: float, slot: int) -> None:
        """One art pixel of debris or a burst at model position (x, y), on background and backdrop pixels only, so it
        never covers a mark or a shelter (entities are drawn over it), clipped to the layer."""
        layer, unit = self.layer, self.unit
        px, w, h, scale, star = layer.px, layer.w, layer.h, self.k // unit, SLOT["star"]
        left, top = math.floor((x + 0.5) * scale) * unit, math.floor((y + 0.5) * 2 * scale) * unit
        for row in range(max(0, top), min(h, top + unit)):
            for column in range(max(0, left), min(w, left + unit)):
                if px[row * w + column] in (0, star):
                    px[row * w + column] = slot

    def lay_ground(self, act: int, shelters: dict) -> None:
        """The act's backdrop with the shelters over it; hp 1 takes the checkerboard phase that runs across a
        shelter."""
        layer, k, solid, cracked = self.layer, self.k, self.art["solid"][0], self.art["cracked"]
        if act not in self.backdrops:
            layer.clear()
            if not self.mono:
                star, w = SLOT["star"], layer.w
                for x, y in backdrop(act, k, STARS):
                    layer.px[y * w + x] = star
            self.backdrops[act] = bytes(layer.px)
        layer.px[:] = self.backdrops[act]
        for (x, y), hp in shelters.items():
            layer.blit(x * k, 2 * y * k, solid if hp > 1 else cracked[(x + y) * k % 2])
        self.ground, self.drawn = bytes(layer.px), (act, dict(shelters))

    def look(self, key: str, frame: int, role: str, veteran: bool, cargo: bool = False) -> Art:
        """An enemy's art as drawn: in its pose's role, with the veteran plate along its bottom edge and the cargo
        mark in its centre (role pickup): CARGO_PIXELS pixels side by side on the middle row, ending at the centre
        column. Mono shows shape only, so there the cargo flips those pixels: a mark where it is dark, a hole where
        the sprite is lit."""
        name = (key, frame, role, veteran, cargo, CARGO_PIXELS)
        if name not in self.looks:
            art = self.art[key][frame]
            px = bytearray(SLOT[role] if p else 0 for p in art.px)
            if veteran:
                left, _, right, bottom = bounds(art)
                row = (bottom - 1) * art.w
                px[row + left : row + right] = bytes([SLOT[role]]) * (right - left)
            if cargo:
                centre = art.h // 2 * art.w + art.w // 2
                for spot in range(centre - CARGO_PIXELS + 1, centre + 1):
                    px[spot] = 0 if self.mono and px[spot] else SLOT["pickup"]
            self.looks[name] = Art(art.w, art.h, bytes(px))
        return self.looks[name]

    def put(self, x: float, y: float, art: Art) -> None:
        self.layer.blit(*placed(x, y, art, self.k), art)

    def overlay(self, painter, x: float, y: float, text: str, attr: int, dx: int = 0, up: int = 0) -> None:
        """Text centred on the cell under a sprite's centre, dx columns over while the field kicks and up rows higher
        (a drifting popup, never above the field's top row), clipped to the arena."""
        k = self.k
        col, row = math.floor((x + 0.5) * k) - len(text) // 2 + dx, math.floor((y + 0.5) * k)
        if up and row >= 0:
            row = max(0, row - up)
        text = text[max(0, -col) : max(0, WIDTH * k - col)]
        if text and 0 <= row < HEIGHT * k:
            painter.screen.addstr(painter.top + 2 + row, painter.left + max(0, col), text, attr)
