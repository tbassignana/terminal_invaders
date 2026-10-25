"""Colour slots, the palettes of every tier, and the resolved Theme the renderer encodes with.

Four palettes a player can choose (Phase 5 spec 3.1): `default` (the colours of Phases 1-4), `cvd` (red-green
colour blindness: hazards warm, the ship and its shot cool), `contrast` (low vision and greyscale screens: white on
dark or black on light is yours, colour is theirs) and `mono` (the mono tier, the same path as --mono and NO_COLOR).
The truecolor and 256 tables pass the colour-vision gate (tools/palette_check.py, run by tests/test_palette.py); 16
colours depend on the user's own palette, so there (and in mono) the sprites' shape cues carry the distinctions.
"""

from dataclasses import dataclass

from .term.caps import TIERS
from .term.cells import A_BOLD, A_DIM, A_REVERSE, style
from .term.color import fragment, xterm_rgb

# Slot numbers are stored in cell styles and pixel layers; 1-6 keep the numbers of Phase 0's colour pairs.
ROLES = (
    "default",
    "accent",
    "good",
    "warn",
    "danger",
    "enemy",
    "text",
    "bg",
    "dim",
    "shelter",
    "player",
    "player_shot",
    "enemy_shot",
    "alien",
    "diver",
    "pickup",
    "ufo",
    "boss",
    "letterbox",
    "star",
    "arcade_white",
    "arcade_red",
    "arcade_green",
    "telegraph",  # the marks that show where an announced attack will land
    "spark",  # Phase 5's bursts and debris (spec 2.3-2.4): the 16th and last pixel slot
)
SLOT = {role: slot for slot, role in enumerate(ROLES)}
BACKGROUNDS = ("bg", "letterbox")  # default colours unless the game paints its own background
SHADES = ("dark", "light")  # the backgrounds every colour palette has a table for (caps.theme)


def cube(dark: int, light: int) -> tuple[int, int, int, int]:
    """A palette row of xterm cube or grey-ramp indices: the truecolor tables hold the same colours, so every 256 pin
    is its own nearest index (spec 3.3)."""
    return xterm_rgb(dark), xterm_rgb(light), dark, light


# PALETTES[name][role] = (truecolor dark, truecolor light, 256 dark, 256 light). A named palette lists only the rows
# it changes and inherits the rest from `default`. Indices start at 16 (0-15 are user-themable). Gated by
# tests/test_palette.py; tools/palette_check.py prints each table's worst pair and margin.
PALETTES = {
    # Phases 1-4's colours, unchanged; light bg, light good, dark star and both telegraph pins are deliberate.
    "default": {
        "bg": (0x0B0E1A, 0xF4F1E8, 233, 231),
        "alien": (0xD8D8E8, 0x34344A, 253, 237),
        "player": (0x56B4E9, 0x0B5FA5, 74, 25),
        "player_shot": (0xFFFFFF, 0x101010, 231, 233),
        "enemy_shot": (0x989900, 0x994C00, 100, 130),
        "shelter": (0xCC0098, 0xB299FF, 163, 141),
        "pickup": (0xADEAEA, 0x6500CC, 159, 56),
        "diver": (0xFF9932, 0xCC6600, 208, 166),
        "ufo": (0xFF5FD7, 0x9C2A7C, 206, 126),
        "boss": (0xFF9932, 0xA84C00, 208, 130),
        "text": (0xE1E1E8, 0x1A1A24, 254, 234),
        "dim": (0x9097AB, 0x55596A, 103, 240),
        "accent": (0x56B4E9, 0x0B5FA5, 74, 25),
        "good": (0x5FD38D, 0x0A6E45, 78, 22),
        "warn": (0xF0E442, 0x7A5A00, 184, 94),
        "danger": (0xFF7A6B, 0xB3261E, 203, 124),
        "enemy": (0xD8A6F0, 0x6B3A8C, 183, 90),
        "letterbox": (0x05060C, 0xE6E2D6, 232, 254),
        "star": (0x3A4060, 0xCFC9B8, 237, 251),
        # The cabinet overlay (MAME's CC0 invaders.lay): white, a red band at the top, green near the bottom. The
        # program prints text in the bands too (GAME OVER, the saucer's score), so each is a text role: the red is
        # lifted from its (1, 0.125, 0.125) and the light green darkened to stay 4.5:1 against bg in protan vision.
        "arcade_white": (0xFFFFFF, 0x101010, 231, 233),
        "arcade_red": (0xFF6060, 0xB3261E, 203, 124),
        "arcade_green": (0x20FF20, 0x0C700C, 46, 22),
        # Gated against the hazards' targets (Phase 4 spec 15.5, Phase 5 spec 3.5): no colour stays 20 from every
        # sprite, so a mark is told apart by where it is and its shape first. Both 256 indices are deliberate pins.
        "telegraph": (0x556677, 0x888888, 60, 243),
        # Debris and bursts take the alien's colours: gated against the enemy shot and the telegraph marks only.
        "spark": (0xD8D8E8, 0x34344A, 253, 237),
    },
    # Red-green colour blindness (protan, deutan): yellow shots and orange divers, the ship and its shot cool blue.
    "cvd": {
        "bg": cube(233, 231),
        "player": cube(74, 20),
        # On dark, the shot is light blue and the enemy shot pure yellow: a thin shot is mostly lightness to a protan
        # or deutan eye, so these two are also 2:1 apart in luminance (spec 3.5's A rule).
        "player_shot": cube(111, 233),
        "enemy_shot": cube(226, 130),
        "diver": cube(214, 166),
        "boss": cube(214, 166),
        "telegraph": cube(95, 102),
        "pickup": cube(99, 53),
        "alien": cube(253, 241),
        "shelter": cube(36, 30),
        "star": cube(237, 251),
        "spark": cube(108, 239),
        "ufo": cube(166, 126),
        "text": cube(254, 234),
        "dim": cube(103, 240),
        "accent": cube(74, 25),
        "good": cube(78, 22),
        "warn": cube(184, 94),
        "danger": cube(203, 124),
        "enemy": cube(183, 90),
    },
    # Low vision and greyscale screens: white (on dark) or black (on light) is yours, colour is theirs. Always
    # painted, even under --theme terminal: its contrast figures need its own background (spec 3.2).
    "contrast": {
        "bg": cube(16, 231),
        "player": cube(231, 16),
        "player_shot": cube(231, 16),
        "enemy_shot": cube(136, 94),
        "diver": cube(172, 94),
        "boss": cube(172, 94),
        "telegraph": cube(105, 61),
        "pickup": cube(231, 233),
        "alien": cube(247, 240),
        "shelter": cube(60, 102),
        "star": cube(235, 254),
        "spark": cube(242, 246),
        "ufo": cube(207, 90),
        "text": cube(231, 16),
        "dim": cube(250, 239),
        "accent": cube(81, 18),
        "good": cube(84, 22),
        "warn": cube(227, 52),  # on light: no amber in the xterm cube is 7:1 against white, so a dark brown-red
        "danger": cube(217, 88),
        "enemy": cube(183, 54),
    },
}
PALETTE = PALETTES["default"]  # Phase 4's name for the default palette's rows
COLOUR_PALETTES = tuple(PALETTES)  # the palettes with colour tables: default, cvd, contrast
# The palettes a player can choose (spec 3.1-3.2) and the names the settings card shows. `mono` has no colour table:
# it is the mono tier (MONO below), which the tier rule picks when it is the palette in force.
PALETTE_NAMES = ("default", "cvd", "contrast", "mono")
PALETTE_LABELS = {"default": "DEFAULT", "cvd": "COLOUR-BLIND SAFE", "contrast": "HIGH CONTRAST", "mono": "MONO"}
PAINTED = frozenset({"contrast"})  # palettes that always paint their background (owner question 3)


def rows(name: str) -> dict[str, tuple[int, int, int, int]]:
    """Every role's row of a colour palette: its own rows over the default palette's."""
    return PALETTES["default"] | PALETTES[name]


# The keyed tables, (palette, background) -> role -> colour: truecolor 0xRRGGBB and 256 indices.
TRUECOLOR = {
    (name, shade): {role: row[n] for role, row in rows(name).items()}
    for name in COLOUR_PALETTES
    for n, shade in enumerate(SHADES)
}
XTERM256 = {
    (name, shade): {role: row[n + 2] for role, row in rows(name).items()}
    for name in COLOUR_PALETTES
    for n, shade in enumerate(SHADES)
}
# role: ((code, attributes) on a dark-background terminal, the same on a light one); None is the default colour.
ANSI16_ROLES = {
    "text": ((None, 0), (None, 0)),
    "dim": ((None, A_DIM), (None, A_DIM)),
    "star": ((None, A_DIM), (None, A_DIM)),
    "alien": ((37, 0), (30, 0)),
    "player": ((96, A_BOLD), (34, A_BOLD)),
    "player_shot": ((97, A_BOLD), (30, A_BOLD)),
    "enemy_shot": ((93, 0), (31, 0)),
    "shelter": ((35, 0), (36, 0)),
    "pickup": ((92, 0), (35, 0)),
    "diver": ((91, 0), (31, A_BOLD)),
    "ufo": ((95, 0), (35, 0)),
    "boss": ((91, 0), (31, 0)),
    "accent": ((36, 0), (34, 0)),
    "good": ((32, 0), (32, 0)),
    "warn": ((33, 0), (33, 0)),
    "danger": ((31, 0), (31, 0)),
    "enemy": ((35, 0), (35, 0)),
    "arcade_white": ((97, 0), (30, 0)),
    "arcade_red": ((91, 0), (31, 0)),
    "arcade_green": ((92, 0), (32, 0)),
    "telegraph": ((34, 0), (90, 0)),  # no other gameplay role uses these codes in its table; never bold
    "spark": ((37, 0), (30, 0)),  # the alien's code, by an explicit allowance (spec 3.6)
}
# cvd's dark 16-colour table moves the pickup off the enemy shot's red-green axis (92 -> 95: protan 4.1 -> 26.4 under
# xterm's colours) and lifts the telegraph above the diver's contrast on black (34 -> 90). Every other palette and
# background uses default's 16-colour table (spec 3.6).
ANSI16_CHANGES = {("cvd", "dark"): {"pickup": (95, 0), "telegraph": (90, 0)}}
ANSI16 = {
    (name, shade): {role: row[n] for role, row in ANSI16_ROLES.items()} | ANSI16_CHANGES.get((name, shade), {})
    for name in COLOUR_PALETTES
    for n, shade in enumerate(SHADES)
}
# Mono is identical on light and dark backgrounds: attributes only, no colour SGR at all.
# telegraph is bold in the glyph arena only: the mono pixel arena draws shapes, never attributes. A spark (the k = 1
# burst) draws in the lit attribute.
MONO = dict(player=A_BOLD, player_shot=A_BOLD, diver=A_BOLD, shelter=A_DIM, star=A_DIM, dim=A_DIM, pickup=A_REVERSE)
MONO |= dict(telegraph=A_BOLD)


@dataclass(frozen=True)
class Theme:
    """Per-slot SGR colour fragments and attributes for one tier; slot 0 is text on bg when the background is
    painted."""

    tier: str
    name: str
    paint_background: bool
    fg: tuple[str, ...]
    bg: tuple[str, ...]
    attrs: tuple[int, ...]
    palette: str = "default"  # the palette resolved (spec 3.2); an unknown name resolves as default

    def style(self, role: str) -> int:
        """Cell style for a role: its slot (none in mono) plus the tier's attributes for it."""
        slot = SLOT[role]
        return style(0 if self.tier == "mono" else slot, 0, self.attrs[slot])

    def styles(self) -> dict[int, int]:
        """The Painter's table: slot -> cell style, for every role (in mono, the attributes alone)."""
        return {slot: self.style(role) for slot, role in enumerate(ROLES) if slot}


def resolve_theme(caps, palette="default") -> Theme:
    """Theme for caps.color, caps.theme and caps.paint_background in a palette (spec 3.2).

    The tier is caps.color (an unknown tier is mono) and the background caps.theme (an unknown name is dark). An
    unknown palette resolves as `default` (a saved value from a newer build). `contrast` paints its background
    whenever the tier has colours, --theme terminal or not. `mono` names the mono tier: the tier rule picks it when
    `mono` is the palette in force, and a colour tier given on the command line over it draws default's colours.
    """
    tier = caps.color if caps.color in TIERS else "mono"
    name = caps.theme if caps.theme in SHADES else "dark"
    palette = palette if palette in PALETTE_NAMES else "default"
    table = palette if palette in COLOUR_PALETTES else "default"
    paint = (caps.paint_background or palette in PAINTED) and tier in ("truecolor", "256")
    values, attrs = [None] * len(ROLES), [0] * len(ROLES)
    if tier in ("truecolor", "256"):
        for role, value in (TRUECOLOR if tier == "truecolor" else XTERM256)[table, name].items():
            values[SLOT[role]] = None if role in BACKGROUNDS and not paint else value
    elif tier == "16":
        for role, (code, attr) in ANSI16[table, name].items():
            values[SLOT[role]], attrs[SLOT[role]] = code, attr
    else:
        for role, attr in MONO.items():
            attrs[SLOT[role]] = attr
    fg = [fragment(tier, value) for value in values]
    bg = [fragment(tier, value, bg=True) for value in values]
    if paint:
        fg[0], bg[0] = fg[SLOT["text"]], bg[SLOT["bg"]]
    return Theme(tier, name, paint, tuple(fg), tuple(bg), tuple(attrs), palette)
