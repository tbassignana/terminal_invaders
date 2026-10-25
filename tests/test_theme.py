"""Slots keep the curses pair numbers, each tier resolves roles to SGR fragments and attributes (spec 7.3), and the
palette registry resolves default, cvd, contrast and mono (Phase 5 spec 3.1-3.2)."""

from dataclasses import dataclass, replace

import pytest

from terminal_invaders.term.cells import A_BOLD, A_DIM, A_REVERSE, style
from terminal_invaders.term.pacing import AckPacer
from terminal_invaders.term.pixels import PACKED_SLOTS, PIXEL_SLOTS, POOL_ORDER, PRIORITY, RANKED
from terminal_invaders.theme import (
    ANSI16,
    COLOUR_PALETTES,
    MONO,
    PALETTE,
    PALETTE_LABELS,
    PALETTE_NAMES,
    PALETTES,
    ROLES,
    SHADES,
    SLOT,
    TRUECOLOR,
    XTERM256,
    resolve_theme,
    rows,
)
from tests.support import frames


@dataclass(frozen=True)
class Caps:
    color: str = "truecolor"
    theme: str = "dark"
    paint_background: bool = True


def test_slot_numbers():
    assert ROLES[:7] == ("default", "accent", "good", "warn", "danger", "enemy", "text")  # the curses pairs 1-6
    expected = "bg dim shelter player player_shot enemy_shot alien diver pickup ufo boss letterbox star".split()
    expected += ["arcade_white", "arcade_red", "arcade_green"]  # Arcade mode's overlay bands, appended
    expected += ["telegraph"]  # Phase 4's marks, after the Arcade bands, which Arcade looks up by name
    expected += ["spark"]  # Phase 5's bursts and debris
    assert ROLES[7:] == tuple(expected) and len(ROLES) == 25
    assert SLOT["star"] == 19 and SLOT["letterbox"] == 18 and SLOT["shelter"] == 9 and SLOT["arcade_green"] == 22
    assert SLOT["telegraph"] == 23 and SLOT["spark"] == 24


def test_spark_pools_between_the_telegraph_and_the_stars():
    """Where a half-block cell holds debris and a shot or a mark, the shot or mark wins (spec 2.10); the Arcade
    nibbles and the 15 Phase 4 pixel slots keep their numbers (tests/term/test_pixels.py is frozen)."""
    assert RANKED.index("spark") == RANKED.index("telegraph") + 1 and RANKED.index("star") == RANKED.index("spark") + 1
    assert [role for role in RANKED if role != "spark"] == list(POOL_ORDER)
    assert PACKED_SLOTS == (*PIXEL_SLOTS, SLOT["spark"]) and len(PACKED_SLOTS) == 16  # 16 nibbles: 0-15
    assert [PIXEL_SLOTS.index(SLOT[band]) for band in ("arcade_white", "arcade_red", "arcade_green")] == [11, 12, 13]
    assert PRIORITY[SLOT["telegraph"]] > PRIORITY[SLOT["spark"]] > PRIORITY[SLOT["star"]]
    assert (
        PRIORITY[SLOT["player_shot"]] > PRIORITY[SLOT["spark"]] and PRIORITY[SLOT["shelter"]] > PRIORITY[SLOT["spark"]]
    )


def test_the_registry():
    assert tuple(PALETTES) == COLOUR_PALETTES == ("default", "cvd", "contrast")
    assert PALETTE_NAMES == ("default", "cvd", "contrast", "mono")
    assert set(PALETTE_LABELS) == set(PALETTE_NAMES) and PALETTE_LABELS["cvd"] == "COLOUR-BLIND SAFE"
    assert PALETTE is PALETTES["default"]
    assert set(TRUECOLOR) == set(XTERM256) == set(ANSI16) == {(p, bg) for p in COLOUR_PALETTES for bg in SHADES}


def test_a_palette_inherits_the_rows_it_does_not_change():
    for name in ("cvd", "contrast"):
        own = PALETTES[name]
        assert {"letterbox", "arcade_white", "arcade_red", "arcade_green"}.isdisjoint(own)  # Arcade keeps 1978's
        for role, row in rows(name).items():
            assert row == own.get(role, PALETTES["default"][role]), (name, role)
        for n, bg in enumerate(SHADES):
            assert TRUECOLOR[name, bg] == {role: row[n] for role, row in rows(name).items()}
            assert XTERM256[name, bg] == {role: row[n + 2] for role, row in rows(name).items()}
            assert TRUECOLOR[name, bg]["arcade_red"] == TRUECOLOR["default", bg]["arcade_red"]


def test_the_default_palette_is_phase_4s_with_spark_as_the_alien():
    assert PALETTES["default"]["spark"] == PALETTES["default"]["alien"] == (0xD8D8E8, 0x34344A, 253, 237)
    assert PALETTES["default"]["enemy_shot"] == (0x989900, 0x994C00, 100, 130)
    assert PALETTES["default"]["bg"] == (0x0B0E1A, 0xF4F1E8, 233, 231)


def test_the_telegraph_role_has_the_measured_values():
    """Phase 4 spec 15.5: P's palette, measured with palette_check's own maths; 16 colours never bold."""
    assert (TRUECOLOR["default", "dark"]["telegraph"], TRUECOLOR["default", "light"]["telegraph"]) == (
        0x556677,
        0x888888,
    )
    assert (XTERM256["default", "dark"]["telegraph"], XTERM256["default", "light"]["telegraph"]) == (60, 243)
    assert (ANSI16["default", "dark"]["telegraph"], ANSI16["default", "light"]["telegraph"]) == ((34, 0), (90, 0))
    sprites = "alien player player_shot enemy_shot shelter pickup diver ufo boss spark".split()
    for key, table in ANSI16.items():  # no other gameplay role shares its code
        assert table["telegraph"][0] not in {table[role][0] for role in sprites}, key
    assert MONO["telegraph"] == A_BOLD and resolve_theme(Caps("16")).attrs[SLOT["telegraph"]] == 0


def test_every_palette_covers_every_role():
    for table in (*TRUECOLOR.values(), *XTERM256.values()):
        assert set(table) == set(ROLES[1:])
    for table in ANSI16.values():
        assert set(table) == set(ROLES[1:]) - {"bg", "letterbox"}  # the terminal's own background
    for key, table in ANSI16.items():  # spark draws in the alien's code, by an explicit allowance
        assert table["spark"] == table["alien"] == ((37, 0) if key[1] == "dark" else (30, 0))


@pytest.mark.parametrize("tier", ["truecolor", "256", "16"])
def test_the_painters_styles_give_every_role_its_own_slot(tier):
    theme = resolve_theme(Caps(tier, "light"))
    styles = theme.styles()
    assert set(styles) == set(range(1, len(ROLES)))  # slots 1-6 keep the numbers of the old colour pairs
    assert styles == {slot: style(slot, 0, theme.attrs[slot]) for slot in styles}
    assert styles[SLOT["accent"]] == style(1) and styles[SLOT["text"]] == style(6)


def test_mono_has_no_colour_at_all():
    theme = resolve_theme(Caps("mono", "light"))
    assert theme.styles() == {slot: theme.attrs[slot] for slot in range(1, len(ROLES))}  # attributes alone
    assert set(theme.fg) == set(theme.bg) == {""}
    assert theme.style("player") == A_BOLD and theme.style("pickup") == A_REVERSE
    assert theme.style("shelter") == theme.style("star") == theme.style("dim") == A_DIM
    assert theme.style("alien") == theme.style("text") == theme.style("spark") == 0  # a burst in the lit attribute
    assert theme.style("arcade_white") == theme.style("arcade_red") == theme.style("arcade_green") == 0  # shapes
    dark = resolve_theme(Caps("mono", "dark"))  # light and dark are identical
    assert (dark.fg, dark.bg, dark.attrs) == (theme.fg, theme.bg, theme.attrs) and len(MONO) == 8


@pytest.mark.parametrize("palette", PALETTE_NAMES)
def test_the_mono_tier_is_the_same_under_every_palette(palette):
    """The mono palette is the mono tier (the tier rule picks it when mono is in force): no palette colours it."""
    for bg in SHADES:
        theme, plain = resolve_theme(Caps("mono", bg), palette), resolve_theme(Caps("mono", bg))
        assert (theme.tier, theme.fg, theme.bg, theme.attrs, theme.palette) == (
            "mono",
            plain.fg,
            plain.bg,
            plain.attrs,
            palette,
        )


def test_a_colour_tier_under_the_mono_palette_draws_defaults_colours():
    """--color truecolor on the command line beats a saved mono palette (the tier rule's first step)."""
    theme, default = resolve_theme(Caps(), "mono"), resolve_theme(Caps())
    assert (theme.fg, theme.bg, theme.attrs, theme.palette) == (default.fg, default.bg, default.attrs, "mono")


def test_an_unknown_palette_resolves_as_default():
    theme, default = resolve_theme(Caps(), "sepia"), resolve_theme(Caps())
    assert theme.palette == "default" and (theme.fg, theme.bg) == (default.fg, default.bg)


@pytest.mark.parametrize("tier", ["truecolor", "256"])
def test_each_palette_resolves_its_own_table(tier):
    for name in COLOUR_PALETTES:
        theme = resolve_theme(Caps(tier, "light"), name)
        value = TRUECOLOR[name, "light"]["enemy_shot"] if tier == "truecolor" else XTERM256[name, "light"]["enemy_shot"]
        expected = f"38;2;{value >> 16};{value >> 8 & 255};{value & 255}" if tier == "truecolor" else f"38;5;{value}"
        assert theme.fg[SLOT["enemy_shot"]] == expected and theme.palette == name


def test_contrast_paints_its_background_under_a_terminal_theme():
    """Its contrast figures need its own black or white (owner question 3); 16 colours and mono never paint."""
    terminal = Caps("truecolor", "dark", paint_background=False)
    assert not resolve_theme(terminal).paint_background and not resolve_theme(terminal, "cvd").paint_background
    theme = resolve_theme(terminal, "contrast")
    assert theme.paint_background and theme.bg[0] == theme.bg[SLOT["bg"]] == "48;2;0;0;0"
    assert theme.fg[0] == theme.fg[SLOT["text"]] == "38;2;255;255;255"
    light = resolve_theme(Caps("256", "light", paint_background=False), "contrast")
    assert light.paint_background and light.bg[0] == "48;5;231" and light.fg[0] == "38;5;16"
    for tier in ("16", "mono"):
        assert not resolve_theme(Caps(tier, "dark", paint_background=False), "contrast").paint_background


def test_truecolor_paints_text_on_its_background():
    theme = resolve_theme(Caps())
    assert theme.paint_background
    assert theme.fg[0] == theme.fg[SLOT["text"]] == "38;2;225;225;232"
    assert theme.bg[0] == theme.bg[SLOT["bg"]] == "48;2;11;14;26"
    assert theme.bg[SLOT["letterbox"]] == "48;2;5;6;12"
    assert theme.style("player") == SLOT["player"] and set(theme.attrs) == {0}


def test_256_uses_the_pinned_indices():
    theme = resolve_theme(Caps("256", "light"))
    assert theme.fg[SLOT["good"]] == "38;5;22" and theme.bg[0] == "48;5;231" and theme.fg[0] == "38;5;234"


def test_terminal_theme_never_paints_a_background():
    theme = resolve_theme(Caps("truecolor", "dark", paint_background=False))
    assert not theme.paint_background
    assert (theme.fg[0], theme.bg[0]) == ("39", "49")
    assert theme.bg[SLOT["bg"]] == theme.bg[SLOT["letterbox"]] == "49"
    assert theme.fg[SLOT["alien"]] == "38;2;216;216;232"


EXPECTED16 = {
    "dark": dict(alien=37, player=96, player_shot=97, enemy_shot=93, shelter=35, pickup=92, diver=91, ufo=95, boss=91),
    "light": dict(alien=30, player=34, player_shot=30, enemy_shot=31, shelter=36, pickup=35, diver=31, ufo=35, boss=31),
}
EXPECTED16["dark"]["telegraph"], EXPECTED16["light"]["telegraph"] = 34, 90
EXPECTED16["dark"]["spark"], EXPECTED16["light"]["spark"] = 37, 30
TEXT16 = {"dark": dict(accent=36, good=32, warn=33, danger=31, enemy=35), "light": dict(accent=34)}
BOLD16 = {"dark": {"player", "player_shot"}, "light": {"player", "player_shot", "diver"}}


@pytest.mark.parametrize("name", ["dark", "light"])
def test_16_colours_follow_the_table(name):
    theme = resolve_theme(Caps("16", name, paint_background=True))
    assert not theme.paint_background  # 16 colours always use the terminal's background
    expected = {**EXPECTED16[name], **TEXT16["dark"], **TEXT16[name]}
    assert {role: theme.fg[SLOT[role]] for role in expected} == {role: str(code) for role, code in expected.items()}
    assert {role: theme.bg[SLOT[role]] for role in expected} == {
        role: str(code + 10) for role, code in expected.items()
    }
    assert {role for role in ROLES if theme.attrs[SLOT[role]] & A_BOLD} == BOLD16[name]
    assert theme.fg[0] == theme.fg[SLOT["text"]] == theme.fg[SLOT["dim"]] == "39" and theme.bg[0] == "49"
    assert theme.attrs[SLOT["dim"]] == theme.attrs[SLOT["star"]] == A_DIM
    assert theme.style("player") == style(SLOT["player"], 0, A_BOLD)


def test_cvd_dark_moves_two_16_colour_codes():
    """The pickup off the enemy shot's red-green axis, the telegraph above the diver's contrast (spec 3.6)."""
    theme = resolve_theme(Caps("16", "dark"), "cvd")
    assert theme.fg[SLOT["pickup"]] == "95" and theme.fg[SLOT["telegraph"]] == "90"
    assert resolve_theme(Caps("16", "light"), "cvd").fg == resolve_theme(Caps("16", "light")).fg
    assert resolve_theme(Caps("16", "dark"), "contrast").fg == resolve_theme(Caps("16", "dark")).fg


def test_unknown_tier_and_theme_fall_back_safely():
    theme = resolve_theme(Caps("88", "sepia"))
    assert (theme.tier, theme.name, theme.paint_background) == ("mono", "dark", False)


def arcade_bytes(caps, palette: str, size=(112, 64)) -> bytes:
    from terminal_invaders.app import Renderer

    renderer = Renderer(frames.options(mode="arcade", palette=palette), caps, size)
    return renderer.frame(frames.arcade(900), 0.0, True, AckPacer("off"))


@pytest.mark.parametrize(
    "tier, background, paint",
    [("truecolor", "dark", True), ("truecolor", "light", True), ("256", "dark", True), ("16", "dark", False)]
    + [("truecolor", "dark", False), ("mono", "dark", False)],
    ids=["truecolor-dark", "truecolor-light", "256-dark", "16-dark", "truecolor-terminal", "mono"],
)
def test_arcade_frames_keep_the_1978_colours_under_every_palette(tier, background, paint):
    """An Arcade frame always resolves the default palette in the tier and background in force (spec 3.1): the
    arcade-playing scene's bytes are the default palette's under every palette (contrast's own painted black
    included); under the mono palette, the mono tier's."""
    from tests.support.terminals import capabilities, load

    caps = replace(capabilities(load("ghostty")), color=tier, theme=background, paint_background=paint)
    expected = arcade_bytes(caps, "default")
    for palette in COLOUR_PALETTES:
        assert arcade_bytes(caps, palette) == expected, palette
    mono = replace(caps, color="mono", paint_background=False)
    assert arcade_bytes(mono, "mono") == arcade_bytes(mono, "default")
