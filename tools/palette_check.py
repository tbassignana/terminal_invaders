#!/usr/bin/env python3
"""The palette gate (Phase 5 spec 3.5-3.8): python3 tools/palette_check.py [--shapes] [--flash] [--quick | --all].

With no option it runs all three checks; --shapes and --flash run only theirs. It prints one line per table with its
worst check and that check's margin (the figure over its floor: 1 or more passes) and exits 1 on any failure.

Colour (spec 3.5): the 12 colour tables (default, cvd and contrast, each on a dark and a light background, in
truecolor and as the pinned 256 indices show them). Machado et al. 2009 deficiency simulation at full severity in
linear sRGB (protan P, deutan D, tritan T) besides normal vision N, and A, greyscale (the WCAG luminance Y shown as
grey); CIEDE2000 between the gameplay pairs that must stay apart, and WCAG contrast against the table's own
background. The pair sets and the floors are each palette's (FLOORS): hazards against your ship and your shot (HF)
and the pickup (HR); hazards against scenery (HS); the pickup against scenery (RS); the debris and burst colour
`spark` against the enemy shot and the telegraph marks (D). Each 256 pin is the index nearest its truecolor colour,
unless listed in DELIBERATE_PINS. Colours 0-15 belong to the user's terminal, so the 16-colour tables get structural
rules (no code shared between a hazard and something of yours, the telegraph's code its own) and figures under two
reference palettes that are reported, never gated.

Shapes (spec 3.7): the pairs a player must tell apart by shape alone, packed alone through the mono half-block packer
at k = 2, 3, 4 and 6 (they must differ in footprint or in at least 2 cells at every frame and pixel phase), and their
glyph strings at k = 1 (they must differ, but for an allowlist with reasons). No glyph of a burst is drawn by
anything else.

Flash (spec 3.8, WCAG 2.3.1): frames drawn tick by tick with effects `full`: Phase 4's runs (a bot plays wave 1),
three staged scenes (a ship lost, a phase break, a boss kill) and a golden tape's game as recorded (TAPE_RUNS: wave
3's formation flying in). A cell's transition is a change of 0.1 or more in relative luminance from its last extreme,
so a change spread over 2-3 frames counts once. Every run is drawn twice in lockstep: the same game by a second
Painter with effects `off`, Phase 4's frame. The rule (RULE, printed with the figures):

1. Play's own motion is judged by Phase 4's rule: no frame has transitions over 25% of the screen's cells.
2. The effects are judged by the 43 x 16 box rule on their own contribution: no frame has transitions over 25% of any
   43 x 16-cell box among the effects-on frame's own transitions: those in a cell where the two frames differ, and
   those the effects-off frame does not make (in the same cell, the same way), which counts an effect's going as
   well as its coming. A formation flying in at k = 3 changes up to 32% of a box with effects off:
   that is Phase 4's own motion, which its whole-screen rule judges, not a flash the effects add.
3. Rules 1 and 2 skip the frames of a kick the flash limiter admitted: each admitted kick counts as one flash
   instead, and the admitted kicks plus any frame over rule 1 or 2 are at most 3 in any 60 ticks.
4. No frame has such an area (25% of the screen, or of a box of the effects' own) of transitions that involve a
   saturated red, a kick's frames included.

--flash (or no option) runs G5.7's set: the staged scenes at k = 1, 2, 3, 4 and 6 in every palette (mono on dark);
--quick runs the plain test run's set, the staged scenes at k = 1-3 in default and cvd only; --all also flies the
three Campaign golden tapes whole at k = 3 on both backgrounds (some 20 minutes, as the slow tests do).

tests/test_palette.py and tests/test_shapes.py run these same checks.
"""

import argparse
import copy
import math
import sys
from array import array
from functools import cache, lru_cache
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from terminal_invaders.term.color import xterm_rgb  # noqa: E402
from terminal_invaders.theme import ANSI16, COLOUR_PALETTES, SHADES, TRUECOLOR, XTERM256  # noqa: E402

# ---- the maths: simulation, CIELAB, CIEDE2000 and WCAG contrast ----

MACHADO = {
    "protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
    "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
    "tritan": ((1.255528, -0.076749, -0.178779), (-0.078411, 0.930809, 0.147602), (0.004733, 0.691367, 0.303900)),
}
VISIONS = ("normal", "protan", "deutan", "tritan")  # N, P, D and T: the visions a colour pair is gated in
GREY = "achroma"  # A: greyscale, the photopic luminance alone (not a model of rod monochromacy)
ALL_VISIONS = (*VISIONS, GREY)
RED_GREEN = ("protan", "deutan")
LETTER = {"normal": "N", "protan": "P", "deutan": "D", "tritan": "T", GREY: "A"}


def linear(channel: int) -> float:
    channel /= 255
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def luminance(lin) -> float:
    """WCAG relative luminance of a linear sRGB colour."""
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def simulate(rgb: int, vision: str) -> list[float]:
    """Linear sRGB as a viewer with this vision sees the colour (full severity; A: its luminance as grey)."""
    lin = [linear(rgb >> shift & 255) for shift in (16, 8, 0)]
    if vision == "normal":
        return lin
    if vision == GREY:
        y = luminance(lin)
        return [y, y, y]
    return [max(0.0, min(1.0, sum(m * c for m, c in zip(row, lin)))) for row in MACHADO[vision]]


def lab(lin) -> tuple[float, float, float]:
    x = 0.4124 * lin[0] + 0.3576 * lin[1] + 0.1805 * lin[2]
    y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    z = 0.0193 * lin[0] + 0.1192 * lin[1] + 0.9505 * lin[2]

    def f(t):
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    fx, fy, fz = f(x / 0.95047), f(y), f(z / 1.08883)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def ciede2000(first, second) -> float:
    (l1, a1, b1), (l2, a2, b2) = first, second
    c_mean = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(c_mean**7 / (c_mean**7 + 25**7)))
    a1, a2 = a1 * (1 + g), a2 * (1 + g)
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    h1, h2 = math.degrees(math.atan2(b1, a1)) % 360, math.degrees(math.atan2(b2, a2)) % 360
    dh = 0 if c1 * c2 == 0 else h2 - h1 if abs(h2 - h1) <= 180 else h2 - h1 - 360 if h2 > h1 else h2 - h1 + 360
    dl, dc, dhh = l2 - l1, c2 - c1, 2 * math.sqrt(c1 * c2) * math.sin(math.radians(dh / 2))
    l_mean, c_mean = (l1 + l2) / 2, (c1 + c2) / 2
    if c1 * c2 == 0:
        h_mean = h1 + h2
    elif abs(h1 - h2) <= 180:
        h_mean = (h1 + h2) / 2
    else:
        h_mean = (h1 + h2 + 360) / 2 if h1 + h2 < 360 else (h1 + h2 - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(h_mean - 30))
        + 0.24 * math.cos(math.radians(2 * h_mean))
        + 0.32 * math.cos(math.radians(3 * h_mean + 6))
        - 0.20 * math.cos(math.radians(4 * h_mean - 63))
    )
    sl = 1 + 0.015 * (l_mean - 50) ** 2 / math.sqrt(20 + (l_mean - 50) ** 2)
    sc, sh = 1 + 0.045 * c_mean, 1 + 0.015 * c_mean * t
    rt = (
        -2
        * math.sqrt(c_mean**7 / (c_mean**7 + 25**7))
        * math.sin(math.radians(60 * math.exp(-(((h_mean - 275) / 25) ** 2))))
    )
    return math.sqrt((dl / sl) ** 2 + (dc / sc) ** 2 + (dhh / sh) ** 2 + rt * (dc / sc) * (dhh / sh))


def contrast(first, second) -> float:
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@lru_cache(maxsize=None)
def seen(rgb: int, vision: str) -> tuple[tuple[float, ...], tuple[float, float, float]]:
    """(linear sRGB, CIELAB) of a colour in a vision; cached, since every table repeats colours."""
    lin = simulate(rgb, vision)
    return tuple(lin), lab(lin)


def delta(first: int, second: int, visions=VISIONS) -> tuple[float, str]:
    """The smallest CIEDE2000 between two colours over the visions, and the vision it is in (the first, on a tie)."""
    return min(((ciede2000(seen(first, v)[1], seen(second, v)[1]), v) for v in visions), key=lambda found: found[0])


def ratio(colour: int, bg: int, visions=VISIONS) -> tuple[float, str]:
    """The lowest WCAG contrast of a colour against the background over the visions, and the vision (the first, on a
    tie)."""
    return min(((contrast(seen(colour, v)[0], seen(bg, v)[0]), v) for v in visions), key=lambda found: found[0])


# ---- the classes, the pairs and the floors (spec 3.4, 3.5) ----

HAZARD = ("enemy_shot", "diver", "boss", "telegraph")
FRIENDLY = ("player", "player_shot")
HF = tuple((hazard, friend) for hazard in HAZARD for friend in FRIENDLY)  # is that a bullet, or me, or my shot?
HR = tuple((hazard, "pickup") for hazard in HAZARD)  # catch it or dodge it?
HS = (("enemy_shot", "shelter"), ("enemy_shot", "star"), ("diver", "alien"), ("telegraph", "enemy_shot"))
RS = (("pickup", "shelter"), ("pickup", "alien"))  # pickup/alien: the cargo mark on a carrier at k >= 2
DECOR = (("spark", "enemy_shot"), ("spark", "telegraph"))  # D: no colour clears 20 from the friendly roles as well
REPORTED_PAIRS = (
    ("boss", "diver"),  # no colour clears both boss pairs: the bosses' rules keep them apart by place
    ("boss", "enemy_shot"),
    ("enemy_shot", "diver"),
    ("telegraph", "shelter"),
    ("telegraph", "star"),
    ("ufo", "player"),
    ("ufo", "telegraph"),
    ("player_shot", "pickup"),
    ("alien", "player_shot"),
    ("spark", "player"),
    ("spark", "player_shot"),
    ("spark", "pickup"),
)
SPRITES = ("alien", "player", "player_shot", "enemy_shot", "pickup", "diver", "ufo", "boss", "telegraph")
TEXT = ("text", "dim", "accent", "good", "warn", "danger", "enemy")
ARCADE = ("arcade_white", "arcade_red", "arcade_green")  # Arcade's bands colour the 1978 program's text too
MIN_DELTA, ARCADE_CONTRAST, SPARK_CONTRAST, STAR_CONTRAST = 20.0, 4.5, 3.0, 2.0
SHOT_CONTRAST = 2.0  # cvd's A rule: or this contrast between the enemy shot and your ship or shot


class Floors(NamedTuple):
    """One palette's floors (spec 3.5); a pair's figure is its minimum over the rule's visions."""

    red_green: float | None  # HF + HR in P and D (None: the N/P/D/T floor only)
    grey: str  # HF + HR in A: "reported"; "shots" (enemy_shot x yours: 20, or SHOT_CONTRAST); "gated" (all, 20)
    pickup_alien: bool  # pickup/alien gated in N/P/D/T, else reported (default: the 2-pixel cargo mark carries it)
    sprite: float  # every sprite against the background
    shelter: float | None  # shelters against the background (None: reported)
    text: float  # every text role against the background


FLOORS = {
    "default": Floors(None, "reported", False, 3.0, None, 4.5),
    "cvd": Floors(30.0, "shots", True, 3.0, 3.0, 4.5),
    "contrast": Floors(None, "gated", True, 4.5, 3.0, 7.0),
}
# Chosen for contrast or for room from the other sprites, not for nearness: (palette, background, role).
DELIBERATE_PINS = {
    ("default", "light", "bg"),
    ("default", "light", "good"),
    ("default", "dark", "star"),
    ("default", "dark", "telegraph"),
    ("default", "light", "telegraph"),
}


class Table(NamedTuple):
    name: str  # tier-palette-background, as printed
    tier: str
    palette: str
    background: str
    colours: dict[str, int]  # role -> 0xRRGGBB (a 256 table as the xterm cube shows it)


def tables() -> list[Table]:
    """The 12 colour tables: truecolor, then 256, each palette on dark and on light."""
    found = []
    for tier in ("truecolor", "256"):
        for palette in COLOUR_PALETTES:
            for background in SHADES:
                key = (palette, background)
                if tier == "truecolor":
                    colours = dict(TRUECOLOR[key])
                else:
                    colours = {role: xterm_rgb(index) for role, index in XTERM256[key].items()}
                found.append(Table(f"{tier}-{palette}-{background}", tier, palette, background, colours))
    return found


class Check(NamedTuple):
    rule: str  # HF, HR, HS, RS, D, RG (HF + HR in P and D), A, sprite, spark, shelter, text, arcade, star
    what: str  # the figure, as printed
    floor: float
    margin: float  # the figure over its floor: 1 or more passes (stars: the floor over the figure, above 1)
    ok: bool


def checks(colours: dict[str, int], palette: str = "default") -> list[Check]:
    """Every gated figure of one table, with its floor (FLOORS[palette])."""
    floors, out, bg = FLOORS[palette], [], colours["bg"]

    def pair(rule, first, second, visions, floor):
        value, vision = delta(colours[first], colours[second], visions)
        what = f"{first}/{second} {value:.1f} ({LETTER[vision]})"
        out.append(Check(rule, what, floor, value / floor, value >= floor))

    def against(rule, role, floor):
        value, vision = ratio(colours[role], bg)
        out.append(Check(rule, f"{role} {value:.2f}:1 ({LETTER[vision]})", floor, value / floor, value >= floor))

    for rule, pairs in (("HF", HF), ("HR", HR), ("HS", HS), ("RS", RS[:1]), ("D", DECOR)):
        for first, second in pairs:
            pair(rule, first, second, VISIONS, MIN_DELTA)
    if floors.pickup_alien:
        pair("RS", "pickup", "alien", VISIONS, MIN_DELTA)
    if floors.red_green:
        for first, second in HF + HR:
            pair("RG", first, second, RED_GREEN, floors.red_green)
    if floors.grey == "gated":
        for first, second in HF + HR:
            pair("A", first, second, (GREY,), MIN_DELTA)
    elif floors.grey == "shots":
        for friend in FRIENDLY:
            value, _ = delta(colours["enemy_shot"], colours[friend], (GREY,))
            between, _ = ratio(colours["enemy_shot"], colours[friend], ("normal",))
            margin = max(value / MIN_DELTA, between / SHOT_CONTRAST)
            what = f"enemy_shot/{friend} {value:.1f} (A) and {between:.2f}:1 between them"
            out.append(Check("A", what, MIN_DELTA, margin, margin >= 1))
    for role in SPRITES:
        against("sprite", role, floors.sprite)
    against("spark", "spark", SPARK_CONTRAST)
    if floors.shelter:
        against("shelter", "shelter", floors.shelter)
    for role in TEXT:
        against("text", role, floors.text)
    for role in ARCADE:
        against("arcade", role, ARCADE_CONTRAST)
    star = max(ratio(colours["star"], bg, (vision,))[0] for vision in VISIONS)  # the highest: it must stay faint
    out.append(Check("star", f"star {star:.2f}:1", STAR_CONTRAST, STAR_CONTRAST / star, star < STAR_CONTRAST))
    return out


def worst(colours: dict[str, int], palette: str = "default") -> Check:
    """The check with the smallest margin: the table's binding rule."""
    return min(checks(colours, palette), key=lambda check: check.margin)


def failures(name: str, colours: dict[str, int], palette: str = "default") -> list[str]:
    """Every gate the table misses, one line each; none means it passes."""
    lines = []
    for check in checks(colours, palette):
        if check.ok:
            continue
        if check.rule == "star":
            lines.append(f"{name}: {check.what} >= {STAR_CONTRAST:g}, reads as a shot")
        elif check.rule == "A" and "between" in check.what:
            lines.append(f"{name}: {check.what} < {MIN_DELTA:g} and {SHOT_CONTRAST:g}:1")
        else:
            lines.append(f"{name}: {check.what} < {check.floor:g}")
    return lines


def measure(colours: dict[str, int]) -> dict:
    """A table's figures for reports and pins: the worst N/P/D/T delta of every gated and reported pair, the A and
    P/D deltas of HF + HR, the contrasts against the background (the lowest over N/P/D/T) and the stars' (the
    highest)."""
    every = HF + HR + HS + RS + DECOR + REPORTED_PAIRS
    bg = colours["bg"]
    return {
        "pairs": {pair: delta(colours[pair[0]], colours[pair[1]])[0] for pair in every},
        "grey": {pair: delta(colours[pair[0]], colours[pair[1]], (GREY,))[0] for pair in HF + HR},
        "red_green": {pair: delta(colours[pair[0]], colours[pair[1]], RED_GREEN)[0] for pair in HF + HR},
        "contrast": {role: ratio(colours[role], bg)[0] for role in (*SPRITES, "spark", "shelter", *TEXT, *ARCADE)},
        "star": max(ratio(colours["star"], bg, (vision,))[0] for vision in VISIONS),
    }


@cache
def nearest256(rgb: int) -> int:
    """The index in 16-255 nearest the colour by CIEDE2000 (normal vision)."""
    target = seen(rgb, "normal")[1]
    return min(range(16, 256), key=lambda index: ciede2000(target, seen(xterm_rgb(index), "normal")[1]))


def pin_failures(palette: str, background: str) -> list[str]:
    """256 pins that are neither the nearest index nor a deliberate pin within MIN_DELTA of the truecolor colour."""
    lines, name = [], f"256-{palette}-{background}"
    for role, rgb in TRUECOLOR[palette, background].items():
        index, nearest = XTERM256[palette, background][role], nearest256(rgb)
        if (palette, background, role) in DELIBERATE_PINS:
            gap = ciede2000(seen(rgb, "normal")[1], seen(xterm_rgb(index), "normal")[1])
            if index == nearest or gap >= MIN_DELTA:
                lines.append(f"{name}: deliberate pin {role} {index} is the nearest or {gap:.1f} away")
        elif index != nearest:
            lines.append(f"{name}: {role} is {index}, the nearest is {nearest}")
    return lines


# ---- 16 colours (spec 3.6): structural rules, and figures under two reference palettes ----

YOURS16 = ("player", "player_shot", "pickup")
GAMEPLAY16 = (*SPRITES, "shelter", "spark")
SHARED16 = {("spark", "alien")}  # spark draws in the alien's code, by this explicit entry
# The terminal's own 16 colours on two common terminals: xterm's defaults and Terminal.app's Basic profile.
REFERENCES16 = {
    "xterm": (0x000000, 0xCD0000, 0x00CD00, 0xCDCD00, 0x0000EE, 0xCD00CD, 0x00CDCD, 0xE5E5E5)
    + (0x7F7F7F, 0xFF0000, 0x00FF00, 0xFFFF00, 0x5C5CFF, 0xFF00FF, 0x00FFFF, 0xFFFFFF),
    "Terminal.app": (0x000000, 0x990000, 0x00A600, 0x999900, 0x0000B2, 0xB200B2, 0x00A6B2, 0xBFBFBF)
    + (0x666666, 0xE50000, 0x00D900, 0xE5E500, 0x0000FF, 0xE500E5, 0x00E5E5, 0xE5E5E5),
}


def ansi16_failures() -> list[str]:
    """The structural rules of every 16-colour table: no code shared between a hazard and the ship, its shot or the
    pickup; no other gameplay role on the telegraph's code; spark on the alien's code (its explicit entry)."""
    lines = []
    for (palette, background), table in ANSI16.items():
        name, code = f"16-{palette}-{background}", {role: entry[0] for role, entry in table.items()}
        for hazard in HAZARD:
            for role in YOURS16:
                if code[hazard] == code[role]:
                    lines.append(f"{name}: {hazard} and {role} share code {code[hazard]}")
        for role in GAMEPLAY16:
            if role != "telegraph" and code[role] == code["telegraph"]:
                lines.append(f"{name}: {role} uses the telegraph's code {code['telegraph']}")
        for role, owner in SHARED16:
            if code[role] != code[owner]:
                lines.append(f"{name}: {role} is {code[role]}, not the {owner}'s {code[owner]}")
    return lines


def ansi16_reports() -> list[str]:
    """Reported, never gated: the worst HF + HR + HS + RS pair in N/P/D/T and the lowest sprite contrast of each
    16-colour table under each reference palette, on black (dark) or white (light)."""
    lines = []
    for (palette, background), table in ANSI16.items():
        bg, own = (0x000000, 0xFFFFFF) if background == "dark" else (0xFFFFFF, 0x000000)  # own: the default text
        for reference, colours in REFERENCES16.items():
            shown = {
                role: own if code is None else colours[code - 30 if code < 90 else code - 82]
                for role, (code, _) in table.items()
            }
            shown["bg"] = bg
            figures = [(delta(shown[a], shown[b]), (a, b)) for a, b in HF + HR + HS + RS]
            (value, vision), (a, b) = min(figures)
            (low, _), role = min((ratio(shown[role], bg), role) for role in SPRITES)
            lines.append(
                f"16-{palette}-{background} under {reference}: worst pair {a}/{b} {value:.1f} ({LETTER[vision]}),"
                f" lowest sprite {role} {low:.2f}:1 (reported)"
            )
    return lines


def colour_gate(out=print) -> list[str]:
    """Print each table's binding check and margin, and its reported pairs; return every failure line."""
    failed = []
    for table in tables():
        check = worst(table.colours, table.palette)
        found = measure(table.colours)
        out(f"{table.name:<23} worst {check.rule} {check.what}  margin {check.margin:.3f}  stars {found['star']:.2f}:1")
        out("    reported " + "  ".join(f"{a}/{b} {found['pairs'][a, b]:.1f}" for a, b in REPORTED_PAIRS))
        failed += failures(table.name, table.colours, table.palette)
    for palette in COLOUR_PALETTES:
        for background in SHADES:
            failed += pin_failures(palette, background)
    for line in ansi16_reports():
        out(line)
    failed += ansi16_failures()
    return failed


# ---- shapes (spec 3.7): the mono gate ----

SHAPE_SCALES = (2, 3, 4, 6)
STATIC = ("beam_warn", "beam_live", "solid", "cracked")  # drawn at whole cell rows: pixel phase 0 only
# The k = 1 pairs whose glyphs may be equal, and why a player still tells them apart.
SHAPE_ALLOW = {
    ("needle", "cracked"): "dim in mono and coloured elsewhere, and a shelter cell never moves",
    ("mite_diving", "mite"): "the wobble before a dive is motion; reduced motion shows the dive pose still",
}


def shape_pairs() -> list[tuple[str, str]]:
    """The S pairs (spec 3.7): what a player must tell apart by shape alone."""
    from terminal_invaders.sprites import PART_FRAMES, PARTS, SHOTS

    enemy = [kind for kind in SHOTS if kind != "player"]
    pairs = [(shot, "player") for shot in (*enemy, "beam_live")]  # is that their shot or mine?
    pairs += [(shot, "pickup") for shot in (*enemy, "beam_live")]
    pairs += [("beam_warn", shot) for shot in (*enemy, "player")]
    pairs += [(shot, tile) for shot in enemy for tile in ("solid", "cracked")]
    pairs += [("diving", "diver"), ("mite_diving", "mite")]
    for part in PARTS:
        pairs += list(combinations([part + suffix for suffix in PART_FRAMES], 2))
    return pairs


def reported_shape(first: str, second: str) -> bool:
    """Boss turret frames may differ by a single cell (they are also telegraphed): reported, not gated."""
    return all(key.split("_")[0].endswith(".turret") for key in (first, second))


@cache
def mono_theme():
    from terminal_invaders.theme import resolve_theme

    return resolve_theme(SimpleNamespace(color="mono", theme="dark", paint_background=False), "mono")


def footprint(art, dy: int) -> tuple[tuple[tuple[str, int], ...], ...]:
    """The cells of art packed alone through the mono half-block packer at pixel row phase dy, trimmed to lit cells."""
    from terminal_invaders.term.cells import CellBuffer
    from terminal_invaders.term.pixels import HalfBlockPacker, PixelLayer

    layer = PixelLayer(art.w + 2, art.h + 4)
    layer.blit(1, 2 + dy, art)
    buf = CellBuffer(layer.w, (layer.h + 1) // 2)
    HalfBlockPacker(mono_theme(), mono=True, ambiguous_wide=False).pack(layer, buf, 0, 0)
    width = buf.cols
    grid = [
        list(zip(buf.chars[y * width : (y + 1) * width], buf.styles[y * width : (y + 1) * width]))
        for y in range(buf.rows)
    ]
    blank = (" ", 0)
    rows = [y for y, row in enumerate(grid) if any(cell != blank for cell in row)]
    cols = [x for x in range(buf.cols) if any(grid[y][x] != blank for y in range(buf.rows))]
    if not rows:
        return ()
    return tuple(tuple(grid[y][x] for x in range(cols[0], cols[-1] + 1)) for y in range(rows[0], rows[-1] + 1))


def cells_apart(first, second) -> int | None:
    """Cells that differ between two trimmed footprints of the same size; None when the footprints differ."""
    if len(first) != len(second) or (first and len(first[0]) != len(second[0])):
        return None
    return sum(a != b for row_a, row_b in zip(first, second) for a, b in zip(row_a, row_b))


def phases(key: str) -> tuple[int, ...]:
    return (0,) if key in STATIC else (0, 1)


def shape_figures(k: int) -> dict[tuple[str, str], int | None]:
    """Each S pair's fewest differing cells at scale k over every frame and phase (None: the footprints differ at
    every one)."""
    from terminal_invaders.sprites import artwork

    art, found, prints = artwork(k), {}, {}

    def printed(key, n, dy):
        if (key, n, dy) not in prints:
            prints[key, n, dy] = footprint(art[key][n], dy)
        return prints[key, n, dy]

    for first, second in shape_pairs():
        fewest = None
        for n_a in range(len(art[first])):
            for n_b in range(len(art[second])):
                for dy_a in phases(first):
                    for dy_b in phases(second):
                        apart = cells_apart(printed(first, n_a, dy_a), printed(second, n_b, dy_b))
                        if apart is not None and (fewest is None or apart < fewest):
                            fewest = apart
        found[first, second] = fewest
    return found


def glyph_frames(key: str) -> tuple[str, ...]:
    """The glyph strings an S key is drawn with at k = 1."""
    from terminal_invaders.sprites import ALIENS, GLYPHS

    return ALIENS[key] if key in ALIENS else (GLYPHS[key],)


def glyph_failures() -> tuple[list[str], list[str]]:
    """(failures, allowed) at k = 1: the pairs with an equal glyph string, and those SHAPE_ALLOW excuses."""
    failed, allowed = [], []
    for first, second in shape_pairs():
        same = set(glyph_frames(first)) & set(glyph_frames(second))
        if same:
            line = f"k=1 {first}/{second}: both draw {sorted(same)[0]!r}"
            (allowed if (first, second) in SHAPE_ALLOW else failed).append(line)
    return failed, allowed


def drawn_glyphs() -> set[str]:
    """Every glyph string the k = 1 arena draws for a sprite, a tile, a part or a mark."""
    from terminal_invaders import sprites

    drawn = set(sprites.GLYPHS.values()) | {glyph for glyphs in sprites.ALIENS.values() for glyph in glyphs}
    drawn |= {*sprites.TELLS.values(), *sprites.CRACKED.values(), *sprites.DIVES.values(), *sprites.SHOTS.values()}
    drawn |= {*sprites.PICKUPS.values(), *sprites.HAZARDS.values(), sprites.SHIP, sprites.SHIELDED, sprites.UFO}
    drawn |= {sprites.SOLID, sprites.CRACKED_CELL, sprites.STAR, sprites.CARGO}
    return drawn | {glyph for glyphs in sprites.PARTS.values() for glyph in glyphs}


def burst_failures() -> list[str]:
    """The burst glyphs (fx.BURST) share no character with any glyph the arena draws, so a burst never reads as a
    shot or a pickup."""
    from terminal_invaders import fx

    used = set("".join(drawn_glyphs()))
    burst = {char for frames in fx.BURST.values() for frame in frames for char in frame if char != " "}
    return [f"burst glyph {char!r} is drawn elsewhere in the arena" for char in sorted(burst & used)]


def shape_gate(out=print) -> list[str]:
    """Print each scale's worst S pair; return every failure line."""
    failed = []
    for k in SHAPE_SCALES:
        figures = shape_figures(k)
        gated = [(n, pair) for pair, n in figures.items() if n is not None and not reported_shape(*pair)]
        reported = [f"{a}/{b} {n}" for (a, b), n in figures.items() if reported_shape(a, b) and n is not None and n < 2]
        least = min(gated, default=None)
        shown = f"{'/'.join(least[1])} {least[0]} cells apart" if least else "every footprint differs"
        out(f"mono k={k:<2} {len(figures)} pairs, worst {shown}; reported {', '.join(reported) or 'none'}")
        failed += [f"mono k={k} {a}/{b}: {n} cell(s) apart" for n, (a, b) in gated if n < 2]
    glyphs, allowed = glyph_failures()
    out(f"mono k=1  {len(shape_pairs())} pairs; allowed: {'; '.join(allowed)}")
    return failed + glyphs + burst_failures()


# ---- flashes (spec 3.8) ----

FLASH, FLASH_AREA, FLASH_BOX, SATURATED_RED = 0.1, 0.25, (43, 16), 0.8
FLASH_LIMIT, FLASH_WINDOW = 3, 60
FLASH_SIZES = {1: (80, 24), 2: (120, 44), 3: (180, 64), 4: (240, 84), 6: (360, 124)}  # the smallest size of each k
PLAY_RUNS = (("truecolor", "dark", (180, 64)), ("truecolor", "light", (180, 64)), ("256", "light", (80, 24)))
PLAY_TICKS = 1200
# Staged scenes (name, staged() arguments, bot, source kind), each drawn from LEAD ticks before its source's birth to
# TAIL ticks after, so the kick shows on and off and the burst and its debris run to their end. The skilled bot is
# not hit at wave 23 in 4,000 ticks, so a naive one loses the ship there.
STAGED = (
    ("ship-lost", dict(wave=23, phase="playing"), "naive", "hit"),
    ("phase-break", dict(wave=24, boss_phase=2), "skilled", "phase"),  # the break into MOTHERSHIP's last phase
    ("boss-kill", dict(wave=24, boss_phase=3), "skilled", "boss"),
)
LEAD, TAIL, SEARCH, STAGED_SEED = 8, 40, 12000, 42
# Golden tapes (tests/fixtures) replayed as recorded at 180 x 64 (k = 3), so play that a bot run from wave 1 never
# reaches is flown too: (tape, first tick, last tick). The window is wave 3's diamond flying in (13 aliens entering,
# 25-32% of a box changing every tick or two with effects off: Phase 4's own motion, judged by its whole-screen rule).
# --all flies the three Campaign tapes whole as well (about 230,000 frames, some 20 minutes).
TAPES = ROOT / "tests" / "fixtures"
TAPE_SIZE = (180, 64)
TAPE_RUNS = (("golden-classic-normal.json", 8600, 8720),)
WHOLE_TAPES = ("golden-classic-normal.json", "golden-endless-hard.json", "golden-daily.json")
BLOCKS = ("▀", "▄")  # a half block shows the mean of its two colours
RULE = (
    "flash rule (spec 3.8): play's own motion changes under 25% of the screen in any frame (Phase 4's rule); "
    "the effects' own transitions (effects full against off, drawn in lockstep) under 25% of any 43 x 16 box; "
    "each admitted kick counts once, at most 3 in any 60 ticks; no such area in saturated red"
)


class Run(NamedTuple):
    """One flash run: its frames; outside the admitted kicks, the largest share of a box in the effects' own
    transitions and the largest share of the screen in all of them; the largest share of a box in the effects' own
    transitions in the kicks' frames; the largest in saturated-red transitions; the kicks admitted, the most flashes
    in any 60 ticks, and its failures."""

    name: str
    frames: int
    share: float
    screen: float
    kick_share: float
    red: float
    kicks: int
    window: int
    failures: tuple[str, ...]


def rgb_of(fragment: str, default: int) -> int:
    """0xRRGGBB of an SGR colour fragment; the terminal's own colour (39, 49, or none in mono) is `default`."""
    parts = fragment.split(";")
    if parts[1:2] == ["2"]:
        r, g, b = map(int, parts[2:5])
        return r << 16 | g << 8 | b
    return xterm_rgb(int(parts[2])) if parts[1:2] == ["5"] else default


def saturated_red(rgb: int) -> bool:
    """WCAG's saturated red: R / (R + G + B) >= 0.8."""
    r, g, b = rgb >> 16 & 255, rgb >> 8 & 255, rgb & 255
    return r + g + b > 0 and r / (r + g + b) >= SATURATED_RED


class Meter:
    """Transitions of each cell's shown luminance, measured from its last extreme. A cell shows its background, or
    for a half block the mean of its two colours; the terminal's own colours count as white on black, the most they
    can differ. A cell involves a saturated red when its background, or its glyph or half block, is one."""

    def __init__(self, theme, cols: int, rows: int):
        self.theme, self.cols, self.rows, self.looks = theme, cols, rows, {}
        self.box = (min(FLASH_BOX[0], cols), min(FLASH_BOX[1], rows))
        self.prev = None

    def look(self, char: str, st: int) -> tuple[float, bool]:
        key = (char in BLOCKS, char == " ", st)
        if key not in self.looks:
            from terminal_invaders.term.cells import A_REVERSE

            fg, bg = rgb_of(self.theme.fg[st & 255], 0xFFFFFF), rgb_of(self.theme.bg[st >> 8 & 255], 0x000000)
            if st & A_REVERSE:
                fg, bg = bg, fg
            lum_fg, lum_bg = (luminance(seen(rgb, "normal")[0]) for rgb in (fg, bg))
            shown = (lum_fg + lum_bg) / 2 if key[0] else lum_bg
            self.looks[key] = (shown, saturated_red(bg) or (not key[1] and saturated_red(fg)))
        return self.looks[key]

    def feed(self, buf) -> tuple[int, int, int]:
        """Take a frame: (its transitions, the most in any box, the most of those involving a saturated red)."""
        self.take(buf)
        return len(self.fired), self.most(self.fired), self.most(self.fired_red)

    def take(self, buf) -> None:
        """Take a frame: self.fired maps each cell that makes a transition to its way (1 up, 2 down), and
        self.fired_red lists those that involve a saturated red. Only the cells that changed since the last frame
        are measured (an unchanged one makes no transition), in order; changed()."""
        chars, styles = list(buf.chars), array("I", buf.styles)
        if self.prev is None:
            looks = [self.look(char, st) for char, st in zip(chars, styles)]
            self.lo, self.red = [lum for lum, _ in looks], [red for _, red in looks]
            self.hi, self.last = list(self.lo), bytearray(len(chars))
            self.prev = (chars, styles)
            self.fired, self.fired_red = {}, []
            return
        lo, hi, red, last, look = self.lo, self.hi, self.red, self.last, self.look
        fired, fired_red = {}, []
        for i in self.changed(chars, styles):
            char, st = chars[i], styles[i]
            value, is_red = look(char, st)
            if last[i] != 1 and value - lo[i] >= FLASH:
                last[i] = 1
            elif last[i] != 2 and hi[i] - value >= FLASH:
                last[i] = 2
            else:  # not a transition yet: the extremes since the last one widen
                lo[i], hi[i], red[i] = min(lo[i], value), max(hi[i], value), red[i] or is_red
                continue
            fired[i] = last[i]
            if red[i] or is_red:
                fired_red.append(i)
            lo[i] = hi[i] = value
            red[i] = is_red
        self.prev = (chars, styles)
        self.fired, self.fired_red = fired, fired_red

    def changed(self, chars: list[str], styles: array) -> list[int]:
        """The cells whose character or style differs from the last frame's, in order. A frame changes a few rows (15
        cells in 5 of 64 rows a frame at 180 x 64 in play), so whole rows are compared first, at C speed, and only
        the rows that differ cell by cell: every flash run is drawn twice (fly()), and this keeps the plain run
        quick."""
        old_chars, old_styles = self.prev
        cols, n = self.cols, len(chars)
        if n != len(old_chars) or n != cols * self.rows or len(styles) != len(old_styles):
            return [i for i in range(n) if chars[i] != old_chars[i] or styles[i] != old_styles[i]]  # as before
        if chars == old_chars and styles == old_styles:
            return []
        cells = []
        for start in range(0, n, cols):
            end = start + cols
            if chars[start:end] != old_chars[start:end] or styles[start:end] != old_styles[start:end]:
                cells += [i for i in range(start, end) if chars[i] != old_chars[i] or styles[i] != old_styles[i]]
        return cells

    def most(self, cells: list[int]) -> int:
        """The most of these cells in any box (clipped to the screen); the whole screen counts as a box at its share.
        Below half a box's flash area the count itself is returned (an upper bound: no box can hold more)."""
        bw, bh = self.box
        cols, rows = self.cols, self.rows
        if len(cells) < FLASH_AREA * bw * bh / 2:
            return len(cells)
        grid = bytearray(cols * rows)
        for i in cells:
            grid[i] = 1
        sums = [[0] * (cols + 1) for _ in range(rows + 1)]
        for y in range(rows):
            above, row, run = sums[y], sums[y + 1], 0
            for x in range(cols):
                run += grid[y * cols + x]
                row[x + 1] = above[x + 1] + run
        best = 0
        for y in range(rows - bh + 1):
            top, bottom = sums[y], sums[y + bh]
            for x in range(cols - bw + 1):
                best = max(best, bottom[x + bw] - bottom[x] - top[x + bw] + top[x])
        whole = math.ceil(len(cells) / (cols * rows) * bw * bh)  # the screen's share, as cells of a box
        return max(best, whole)


class Recorder:
    """A flash limiter (fx.FlashLimiter's rules) that remembers every kick it admitted, by key and tick."""

    def __init__(self):
        from terminal_invaders import fx

        self.limiter, self.log = fx.FlashLimiter(), {}

    def admit(self, key, tick: int) -> bool:
        admitted = self.limiter.admit(key, tick)
        if admitted:
            self.log.setdefault(key, tick)
        return admitted


class StubProfile:
    """What a Session needs of a profile in these runs: no boards, no records, nothing saved."""

    alert = error = None
    data = {"name": "ACE", "scores": [], "stats": {}, "achievements": []}

    def best(self, config):
        return 0

    def old_boards(self):
        return {}

    def record(self, result, config, name):
        return []


class SilentAudio:
    enabled = False

    def update(self, events=()):
        pass

    def toggle(self):
        self.enabled = False


def flash_options(**values):
    from terminal_invaders.cli import defaults

    return defaults(**dict(seed=STAGED_SEED, fixed_seed=True, quickstart=True, name="ACE", effects="full") | values)


def theme_for(tier: str, background: str, palette: str):
    from terminal_invaders.theme import resolve_theme

    caps = SimpleNamespace(color=tier, theme=background, paint_background=tier in ("truecolor", "256"))
    return resolve_theme(caps, palette)


def bot(name: str, seed: int):
    """A bot that plays from what a player sees (tools/bots), with act(frame)."""
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    import bots

    return bots.make(name, seed)


def kick_ticks(log: dict) -> set[int]:
    """The ticks whose frames belong to an admitted kick: from age 1 to the frame after its last tick."""
    from terminal_invaders import fx

    ticks = set()
    for text, birth in log:
        known = fx.recognise(SimpleNamespace(text=text, x=0.0, y=-1.0, ttl=0))
        if known:
            ticks.update(range(birth + 1, birth + fx.KINDS[known[0]][3] + 2))
    return ticks


class Frame(NamedTuple):
    """One frame's figures: the most of the effects' own transitions in any 43 x 16 box, the most of those that
    involve a saturated red, and the transitions on the whole screen, all and saturated red."""

    tick: int
    most: int
    most_red: int
    screen: int = 0
    screen_red: int = 0


def judge(name: str, frames: list[Frame], box_cells: int, log: dict, screen_cells: int | None = None) -> Run:
    """Spec 3.8's rules (as the module's docstring states them) over one run's frames. screen_cells is the screen's
    size in cells (None: as large as a box)."""
    screen_cells = screen_cells or box_cells
    need, whole, kicking = FLASH_AREA * box_cells, FLASH_AREA * screen_cells, kick_ticks(log)
    failed, events, share, screen, kick_share, red = [], sorted(log.values()), 0.0, 0.0, 0.0, 0.0
    for tick, most, most_red, fired, fired_red in (Frame(*frame) for frame in frames):
        red = max(red, most_red / box_cells, fired_red / screen_cells)
        if most_red >= need:
            failed.append(f"{name}: tick {tick}: {most_red} saturated-red transitions of the effects in a 43 x 16 box")
        if fired_red >= whole:
            failed.append(f"{name}: tick {tick}: {fired_red / screen_cells:.0%} of the screen in saturated-red flashes")
        if tick in kicking:  # counted once under rule 3, by its admission
            kick_share = max(kick_share, most / box_cells)
            continue
        share, screen = max(share, most / box_cells), max(screen, fired / screen_cells)
        over = [f"{most / box_cells:.0%} of a 43 x 16 box flashes (the effects' own)"] if most >= need else []
        over += [f"{fired / screen_cells:.0%} of the screen flashes"] if fired >= whole else []
        if over:
            failed.append(f"{name}: tick {tick}: " + "; ".join(over))
            events.append(tick)
    events.sort()
    window = max((sum(1 for t in events if start <= t < start + FLASH_WINDOW) for start in events), default=0)
    if window > FLASH_LIMIT:
        failed.append(f"{name}: {window} flashes in 60 ticks (at most {FLASH_LIMIT})")
    return Run(name, len(frames), share, screen, kick_share, red, len(log), window, tuple(failed))


def own_flashes(fired: dict[int, int], off: dict[int, int], buf, plain) -> dict[int, int]:
    """The effects' own transitions in a frame (rule 2): those of the effects-on frame (`fired`, Meter.take's) in a
    cell where the two frames differ after it, or that the effects-off frame (`off`) does not make there the same
    way. The second part counts an effect's going too: once a spark or the kick ends, its cell shows again what the
    effects-off frame shows, so the two frames no longer differ there."""
    chars, styles, plain_chars, plain_styles = buf.chars, buf.styles, plain.chars, plain.styles
    return {
        i: way
        for i, way in fired.items()
        if off.get(i) != way or chars[i] != plain_chars[i] or styles[i] != plain_styles[i]
    }


def fly(session, theme, size: tuple[int, int], steps, caps=None) -> tuple[list[Frame], int, dict, int]:
    """Draw the session, then again after each step that steps() makes, as a terminal of `size` would show it, and
    in lockstep the same session with effects off on a second Painter (Phase 4's frame): ([Frame], a box's cells,
    the kicks admitted, the screen's cells). The effects' own transitions are own_flashes()'s."""
    from terminal_invaders import fx
    from terminal_invaders.term.cells import CellBuffer
    from terminal_invaders.ui import Painter

    buf, plain = CellBuffer(*size), CellBuffer(*size)
    painter = Painter(buf, session.options, theme, caps)
    twin = Painter(plain, copy.copy(session.options), theme, caps)
    twin.options.effects = "off"
    recorder = Recorder()
    painter.link, painter.limiter = fx.Link(), recorder
    twin.link, twin.limiter = fx.Link(), fx.FlashLimiter()
    meter, base, frames = Meter(theme, *size), Meter(theme, *size), []
    painter.draw(session)
    twin.draw(session)
    meter.take(buf)
    base.take(plain)
    for _ in steps():
        painter.draw(session)
        twin.draw(session)
        meter.take(buf)
        base.take(plain)
        own = own_flashes(meter.fired, base.fired, buf, plain)
        red = meter.fired_red
        own_red = [i for i in red if i in own] if red else []
        tick = session.pilot.game.tick
        frames.append(Frame(tick, meter.most(own), meter.most(own_red), len(meter.fired), len(red)))
    return frames, meter.box[0] * meter.box[1], recorder.log, size[0] * size[1]


def play_run(tier: str, background: str, size: tuple[int, int], palette: str, ticks: int = PLAY_TICKS) -> Run:
    """Phase 4's run: the skilled bot plays seed 42 from wave 1 (a new game after a game over) for `ticks` ticks."""
    from terminal_invaders.campaign.observe import observe
    from terminal_invaders.ui import Session

    session = Session(flash_options(), StubProfile(), SilentAudio())
    caps = SimpleNamespace(unicode_blocks=False) if tier == "mono" else None  # mono is the dumb terminal's run

    def steps():
        pilot = bot("skilled", STAGED_SEED)
        for tick in range(1, ticks):
            if session.pilot.game.phase in ("game_over", "victory"):
                session.start()
                pilot = bot("skilled", STAGED_SEED + tick)
            game = session.pilot.game
            game.step(pilot.act(observe(game)))
            yield

    frames, cells, log, screen = fly(session, theme_for(tier, background, palette), size, steps, caps)
    return judge(f"play {tier}-{palette}-{background} {size[0]}x{size[1]}", frames, cells, log, screen)


@cache
def staged_start(name: str):
    """((game, bot) LEAD ticks before the scene's source is born, its birth tick), searched once a process."""
    from terminal_invaders import fx
    from terminal_invaders.campaign.observe import observe
    from terminal_invaders.campaign.testing import staged
    from terminal_invaders.model import Config

    _, values, pilot_name, kind = next(scene for scene in STAGED if scene[0] == name)
    values = dict(values)
    wave = values.pop("wave")

    def begin():
        return staged(Config("normal", "classic", STAGED_SEED), wave, lives=99, **values), bot(pilot_name, STAGED_SEED)

    game, pilot = begin()
    birth = None
    for _ in range(SEARCH):
        game.step(pilot.act(observe(game)))
        found = [source for source in fx.sources(game) if source.kind == kind]
        if found:
            birth = found[0].birth
            break
    if birth is None:
        raise LookupError(f"the staged {name} scene shows no {kind} in {SEARCH} ticks")
    game, pilot = begin()
    while game.tick < birth - LEAD:
        game.step(pilot.act(observe(game)))
    return copy.deepcopy((game, pilot)), birth


def staged_run(name: str, k: int, background: str, palette: str) -> Run:
    """A staged scene drawn tick by tick at scale k in truecolor (palette `mono`: the mono tier)."""
    from terminal_invaders.campaign.observe import observe
    from terminal_invaders.ui import Session

    start, birth = staged_start(name)
    game, pilot = copy.deepcopy(start)
    session = Session(flash_options(), StubProfile(), SilentAudio())
    session.pilots[0].game = game
    session.match_controls()

    def steps():
        while game.tick < birth + TAIL and game.phase not in ("game_over", "victory"):
            game.step(pilot.act(observe(game)))
            yield

    tier = "mono" if palette == "mono" else "truecolor"
    frames, cells, log, screen = fly(session, theme_for(tier, background, palette), FLASH_SIZES[k], steps)
    return judge(f"staged {name} k={k} {tier}-{palette}-{background}", frames, cells, log, screen)


def tape_run(tape: str, start: int, end: int | None, background: str, palette: str = "default") -> Run:
    """A golden tape's game replayed as recorded from tick `start` to `end` (None: the tape's end), drawn at
    TAPE_SIZE (k = 3) in truecolor."""
    from terminal_invaders.replay import Playback
    from terminal_invaders.rules import game_class, make_game
    from terminal_invaders.ui import Session

    recorded = Playback(TAPES / tape).tapes[0]
    game = game_class(recorded.ruleset)(recorded.config) if recorded.ruleset else make_game(recorded.config)
    end = recorded.ticks if end is None else min(end, recorded.ticks)
    while game.tick < start:
        game.step(recorded.actions_at(game.tick))
    session = Session(flash_options(), StubProfile(), SilentAudio())
    session.pilots[0].game = game
    session.match_controls()

    def steps():
        while game.tick < end and game.phase not in ("game_over", "victory"):
            game.step(recorded.actions_at(game.tick))
            yield

    frames, cells, log, screen = fly(session, theme_for("truecolor", background, palette), TAPE_SIZE, steps)
    span = "whole" if start == 0 and end == recorded.ticks else f"{start}-{end}"
    return judge(f"tape {tape.removesuffix('.json')} {span} k=3 {palette}-{background}", frames, cells, log, screen)


def flash_runs(every: bool = True, whole: bool = False) -> list[tuple[str, tuple]]:
    """The flash check's runs, (kind, arguments): Phase 4's runs in every colour palette and in mono; the tape
    window on both backgrounds; the staged scenes in every palette (mono on dark) at every scale, or with
    every=False (--quick, the plain test run) at k = 1-3 in default and cvd only; and with whole=True (--all, the
    slow test run) the three Campaign tapes whole on both backgrounds too."""
    runs = [("play", (tier, bg, size, palette)) for palette in COLOUR_PALETTES for tier, bg, size in PLAY_RUNS]
    runs.append(("play", ("mono", "dark", (180, 64), "mono")))
    runs += [("tape", (tape, start, end, background)) for tape, start, end in TAPE_RUNS for background in SHADES]
    palettes = (*COLOUR_PALETTES, "mono") if every else ("default", "cvd")
    for name, *_ in STAGED:
        for k in tuple(FLASH_SIZES) if every else (1, 2, 3):
            for palette in palettes:
                for background in ("dark",) if palette == "mono" else SHADES:
                    runs.append(("staged", (name, k, background, palette)))
    if whole:
        runs += [("tape", (tape, 0, None, background)) for tape in WHOLE_TAPES for background in SHADES]
    return runs


def run_flash(kind: str, arguments: tuple) -> Run:
    return {"play": play_run, "staged": staged_run, "tape": tape_run}[kind](*arguments)


def flash_gate(out=print, every: bool = True, whole: bool = False) -> list[str]:
    """Print the rule, then each run's figures; return every failure line."""
    failed = []
    out(RULE)
    for kind, arguments in flash_runs(every, whole):
        run = run_flash(kind, arguments)
        out(
            f"{run.name:<56} frames {run.frames:6}  screen {run.screen:6.1%}  effects' box {run.share:6.1%}"
            f"  in kicks {run.kick_share:6.1%}  red {run.red:5.1%}  kicks {run.kicks}  most in 60 ticks {run.window}"
        )
        failed += run.failures
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shapes", action="store_true", help="only the mono shape gate (spec 3.7)")
    parser.add_argument("--flash", action="store_true", help="only the flash check (spec 3.8)")
    parser.add_argument("--quick", action="store_true", help="the plain test run's smaller flash set (see above)")
    parser.add_argument("--all", action="store_true", help="the flash check flies the whole golden tapes too")
    args = parser.parse_args(argv)
    every = not (args.shapes or args.flash)
    failed = []
    if every:
        failed += colour_gate()
    if every or args.shapes:
        failed += shape_gate()
    if every or args.flash:
        failed += flash_gate(every=not args.quick, whole=args.all)
    for line in failed:
        print("FAIL " + line)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
