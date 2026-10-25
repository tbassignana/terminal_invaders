"""Pixel layer, pooled downsampling and the half-block packer, checked against the table in spec 7.7."""

import itertools
import random
from dataclasses import dataclass

import pytest

from terminal_invaders.term.cells import A_BOLD, A_REVERSE, CellBuffer, style
from terminal_invaders.term.encode import Encoder
from terminal_invaders.term.pixels import (
    LOWER,
    PIXEL_SLOTS,
    POOL_ORDER,
    PRIORITY,
    UPPER,
    Art,
    HalfBlockPacker,
    PixelLayer,
    downsample,
    half_block,
    ranks,
    row_maps,
)
from terminal_invaders.theme import SLOT, resolve_theme
from tests.support.vt import MiniVT

ALIEN, SHOT, BOMB, PLAYER, SHELTER, STAR = (
    SLOT[role] for role in ("alien", "player_shot", "enemy_shot", "player", "shelter", "star")
)


@dataclass(frozen=True)
class Caps:
    color: str = "truecolor"
    theme: str = "dark"
    paint_background: bool = True
    utf8: bool = True


THEMES = [resolve_theme(Caps(tier, name)) for tier in ("truecolor", "256", "16", "mono") for name in ("dark", "light")]
TRUE = THEMES[0]


def layer_of(rows):
    layer = PixelLayer(len(rows[0]), len(rows))
    layer.px[:] = bytes(itertools.chain.from_iterable(rows))
    return layer


def test_dot_and_clear():
    layer = PixelLayer(3, 2)
    layer.dot(2, 1, ALIEN)
    for x, y in ((-1, 0), (3, 0), (0, -1), (0, 2)):
        layer.dot(x, y, SHOT)
    assert layer.px == bytearray([0, 0, 0, 0, 0, ALIEN])
    layer.clear()
    assert layer.px == bytearray(6)


def test_art_from_rows_and_size_check():
    art = Art.from_rows([".#.", "##", "#.#"], ALIEN)
    assert (art.w, art.h) == (3, 3)
    assert art.px == bytes([0, ALIEN, 0, ALIEN, ALIEN, 0, ALIEN, 0, ALIEN])
    assert art.runs[2] == ((0, bytes([ALIEN])), (2, bytes([ALIEN])))
    with pytest.raises(ValueError):
        Art(2, 2, b"\x01")


@pytest.mark.parametrize("x, y", list(itertools.product(range(-4, 6), range(-4, 5))))
def test_blit_clips_and_keeps_transparent_pixels(x, y):
    art = Art.from_rows(["#.##", "####", ".#.."], SHOT)
    layer = PixelLayer(5, 4)
    layer.px[:] = bytes([STAR]) * 20
    layer.blit(x, y, art)
    for py, px in itertools.product(range(4), range(5)):
        ay, ax = py - y, px - x
        inside = 0 <= ay < art.h and 0 <= ax < art.w and art.px[ay * art.w + ax]
        assert layer.px[py * 5 + px] == (SHOT if inside else STAR)


def test_ranks_is_a_permutation_with_the_order_on_top():
    rank = ranks([7, 3])
    assert sorted(rank) == list(range(256))
    assert rank[7] == 255 and rank[3] == 254 and rank[0] == 0
    assert rank[1] < rank[2] < rank[3]


def test_priority_follows_the_pooling_order():
    order = [SLOT[role] for role in POOL_ORDER] + [0]
    assert [PRIORITY[a] > PRIORITY[b] for a, b in itertools.pairwise(order)] == [True] * (len(order) - 1)
    assert POOL_ORDER[:2] == ("player_shot", "enemy_shot") and PIXEL_SLOTS[0] == 0 and len(PIXEL_SLOTS) <= 16


def test_telegraph_marks_rank_above_the_backdrop_and_below_every_entity():
    """Phase 4 spec 2.1: telegraph sits between shelter and star; the relative order of every other role is
    unchanged, and its slot sorts after arcade_green's, so the Arcade bands keep their nibbles (15 of 16 used)."""
    assert POOL_ORDER.index("telegraph") == POOL_ORDER.index("shelter") + 1 == POOL_ORDER.index("star") - 1
    before = ("player_shot", "enemy_shot", "player", "pickup", "diver", "alien", "ufo", "boss", "shelter", "star")
    before += ("arcade_white", "arcade_red", "arcade_green")
    assert tuple(role for role in POOL_ORDER if role != "telegraph") == before
    assert len(PIXEL_SLOTS) == 15 and PIXEL_SLOTS[-1] == SLOT["telegraph"]
    arcade = [PIXEL_SLOTS.index(SLOT[role]) for role in ("arcade_white", "arcade_red", "arcade_green")]
    assert arcade == [11, 12, 13]  # the nibbles they had in Phase 3
    for role in POOL_ORDER:  # a telegraph mark never hides an entity when pooled, and always hides the backdrop
        if role != "telegraph":
            above = POOL_ORDER.index(role) < POOL_ORDER.index("telegraph")
            assert (PRIORITY[SLOT[role]] > PRIORITY[SLOT["telegraph"]]) is above, role


@pytest.mark.parametrize("fx, fy", [(1, 2), (2, 2), (2, 1)])
def test_a_shot_survives_any_pool(fx, fy):
    others = [0, ALIEN, PLAYER, SHELTER, STAR, SLOT["boss"], SLOT["diver"], SLOT["pickup"], SLOT["ufo"]]
    for shot in (SHOT, BOMB):
        for position in range(fx * fy):
            for filler in itertools.product(others, repeat=fx * fy - 1):
                block = list(filler)
                block.insert(position, shot)
                layer = layer_of([block[row * fx : (row + 1) * fx] for row in range(fy)])
                assert downsample(layer, fx, fy, PRIORITY).px == bytearray([shot])


def test_downsample_keeps_the_highest_rank_and_rounds_sizes_up():
    layer = layer_of(
        [
            [ALIEN, 0, STAR, SHELTER, PLAYER],
            [0, SHOT, 0, BOMB, 0],
            [STAR, 0, 0, 0, ALIEN],
        ]
    )
    pooled = downsample(layer, 2, 2, PRIORITY)
    assert (pooled.w, pooled.h) == (3, 2)
    assert pooled.px == bytearray([SHOT, BOMB, PLAYER, STAR, 0, ALIEN])
    assert downsample(layer, 1, 1, PRIORITY).px == layer.px


def test_packer_table_cases():
    t, b, rows = ALIEN, SHOT, []
    for top, bottom in ((0, 0), (t, t), (t, 0), (0, b), (t, b)):
        rows.append(half_block(TRUE, top, bottom, mono=False))
    assert rows == [
        (" ", 0),  # t = b = 0: space on the background
        (" ", style(bg=t)),  # t = b: space in the role's colour
        (UPPER, style(t)),  # top only
        (LOWER, style(b)),  # bottom only
        (UPPER, style(t, b)),  # both: top as foreground, bottom as background
    ]
    dark16 = THEMES[4]
    assert half_block(dark16, PLAYER, 0, mono=False) == (UPPER, style(PLAYER, 0, A_BOLD))


def test_packer_table_in_mono():
    mono = THEMES[6]
    cells = [half_block(mono, top, bottom, mono=True) for top, bottom in ((0, 0), (ALIEN, 0), (0, SHOT), (SHOT, ALIEN))]
    assert cells == [(" ", 0), (UPPER, 0), (LOWER, 0), (" ", A_REVERSE)]
    assert half_block(mono, STAR, STAR, mono=True) == (" ", A_REVERSE)


@pytest.mark.parametrize("theme", THEMES, ids=lambda theme: f"{theme.tier}-{theme.name}")
@pytest.mark.parametrize("mono", [False, True])
def test_row_maps_equal_the_slow_path(theme, mono):
    maps = row_maps(theme, mono=mono)
    for top, bottom in itertools.product(PIXEL_SLOTS, repeat=2):
        code = maps.top[top] | maps.bottom[bottom]
        assert (maps.chars[code], maps.styles[code]) == half_block(theme, top, bottom, mono=mono)
        (role,) = downsample(layer_of([[top], [bottom]]), 1, 2, PRIORITY).px  # the pool keeps what downsample keeps
        assert maps.pooled[code] == ((A_REVERSE if mono else style(bg=role)) if role else 0)
    assert maps.top[SLOT["text"]] == maps.bottom[SLOT["letterbox"]] == 0  # not pixel roles: background
    assert maps.pooled[maps.top[ALIEN]] == (A_REVERSE if mono else style(bg=ALIEN)) and maps.pooled[0] == 0


def random_layer(rng, w, h):
    layer = PixelLayer(w, h)
    layer.px[:] = bytes(rng.choice(PIXEL_SLOTS) if rng.random() < 0.4 else 0 for _ in range(w * h))
    return layer


def pooled_cell(theme, upper: int, lower: int, mono: bool) -> tuple[str, int]:
    """The slow path of the 2:1 pool: a space in the colour of the role downsample keeps."""
    (role,) = downsample(layer_of([[upper], [lower]]), 1, 2, PRIORITY).px
    return " ", (A_REVERSE if mono else style(bg=role)) if role else 0


@pytest.mark.parametrize("theme", THEMES[::2] + THEMES[6:7], ids=lambda theme: f"{theme.tier}-{theme.name}")
@pytest.mark.parametrize("pooled", [False, True], ids=["halves", "pooled"])
def test_pack_matches_the_slow_path_with_clipping(theme, pooled):
    rng = random.Random(theme.tier)
    mono = theme.tier == "mono"
    packer = HalfBlockPacker(theme, mono=mono, ambiguous_wide=pooled)
    cell = pooled_cell if pooled else half_block
    for w, h, top, left in ((7, 6, 1, 2), (7, 5, 0, 0), (9, 8, -2, -3), (9, 8, 3, 5), (4, 4, 0, 20), (4, 4, 10, 0)):
        layer, buf = random_layer(rng, w, h), CellBuffer(10, 5)
        buf.fill(0, 0, 10, 5, "~", 99)
        packer.pack(layer, buf, top, left)
        for y, x in itertools.product(range(5), range(10)):
            row, col = y - top, x - left
            if 0 <= row < (h + 1) // 2 and 0 <= col < w:
                upper = layer.px[2 * row * w + col]
                lower = layer.px[(2 * row + 1) * w + col] if 2 * row + 1 < h else 0
                expected = cell(theme, upper, lower, mono=mono)
            else:
                expected = ("~", 99)
            assert (buf.chars[y * 10 + x], buf.styles[y * 10 + x]) == expected


@pytest.mark.parametrize("mono", [False, True])
def test_ambiguous_width_fallback_pools_into_spaces(mono):
    theme = THEMES[6] if mono else TRUE
    layer = layer_of([[ALIEN, 0, 0, STAR], [SHOT, 0, PLAYER, 0], [SHELTER, 0, 0, 0]])
    buf = CellBuffer(5, 3)
    HalfBlockPacker(theme, mono=mono, ambiguous_wide=True).pack(layer, buf, 1, 1)
    assert [buf.row_text(y) for y in range(3)] == [" " * 5] * 3
    pooled = [SHOT, 0, PLAYER, STAR, SHELTER, 0, 0, 0]
    expected = [(A_REVERSE if role else 0) if mono else style(bg=role) for role in pooled]
    assert list(buf.styles[6:10]) + list(buf.styles[11:15]) == expected


def test_packed_cells_survive_the_encoder_and_a_vt():
    rng = random.Random(3)
    layer, buf = random_layer(rng, 12, 9), CellBuffer(14, 6)
    HalfBlockPacker(TRUE, mono=False, ambiguous_wide=False).pack(layer, buf, 1, 1)
    vt = MiniVT(14, 6)
    vt.feed(b"\x1b[?7l" + Encoder(TRUE, Caps()).encode(None, buf))
    assert vt.lines() == [buf.row_text(y) for y in range(6)]
    assert UPPER in "".join(vt.lines()) and LOWER in "".join(vt.lines())
