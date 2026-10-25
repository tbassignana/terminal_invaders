"""The Arcade view (spec 6): the scale ladder, pooling that keeps every lit pixel, the overlay bands, the text layer
shown only while intact, the HI-SCORE field, column-wise updates, the 2:1 pooled fallback and the frame budget; on
FakeArcade, so the program's own art is not needed, except the last test, which checks the real game's dirty
columns."""

import random
import statistics
import time
from types import SimpleNamespace

import pytest

from terminal_invaders.arcade import ArcadeGame
from terminal_invaders.arcade.art import CHARSET
from terminal_invaders.arcade.view import GREEN, RED, WHITE, ArcadeView, arcade_layout, band
from terminal_invaders.cli import defaults
from terminal_invaders.model import Config
from terminal_invaders.term.cells import CellBuffer
from terminal_invaders.term.pixels import LOWER, UPPER
from terminal_invaders.theme import SLOT, resolve_theme
from terminal_invaders.ui import Painter
from tests.support.arcade_fake import HEADER, HI_SCORE, P1_SCORE, FakeArcade, glyph

THEME = resolve_theme(SimpleNamespace(color="truecolor", theme="dark", paint_background=True))
BLOCKS = {1: (224, 128), 2: (112, 64), 3: (75, 43), 4: (56, 32)}


def lit(game, x: int, y: int) -> bool:
    """Display pixel (x, y) of the game's VRAM: Xr = x + 32, Yr = 255 - y."""
    yr = 255 - y
    return bool(game.vram[32 * x + yr // 8] >> yr % 8 & 1)


def cells(addr: int) -> set[tuple[int, int]]:
    """The display pixels of the character printed at a VRAM address."""
    index = addr - 0x2400
    x, y = index // 32, 248 - 8 * (index % 32)
    return {(x + dx, y + dy) for dx in range(8) for dy in range(8)}


def drawn(game, p: int, best: int = 0, caps=None, theme=THEME) -> tuple[CellBuffer, ArcadeView, Painter]:
    """The view's frame of the game in a buffer the size of the block at p."""
    buf = CellBuffer(*BLOCKS[p])
    painter = Painter(buf, defaults(), theme, caps)
    painter.place("playing", arcade=True)
    session = SimpleNamespace(pilot=SimpleNamespace(game=game), profile=SimpleNamespace(best=lambda config: best))
    view = ArcadeView(theme, caps, charset=CHARSET)
    view.draw(painter, session)
    return buf, view, painter


def played(ticks: int) -> FakeArcade:
    game = FakeArcade()
    for tick in range(ticks):
        game.step(("fire", "right") if tick % 3 else ())
    return game


@pytest.mark.parametrize(
    "size, p, fits",
    [
        ((224, 128), 1, True), ((223, 128), 2, True), ((224, 127), 2, True), ((112, 64), 2, True),
        ((111, 64), 3, True), ((112, 63), 3, True), ((75, 43), 3, True), ((74, 43), 4, True), ((75, 42), 4, True),
        ((56, 32), 4, True), ((55, 32), 4, False), ((56, 31), 4, False), ((80, 24), 4, False), ((1, 1), 4, False),
    ],
)  # fmt: skip
def test_the_largest_scale_that_fits_is_chosen(size, p, fits):
    layout = arcade_layout(*size)
    assert (layout.p, layout.fits, (layout.width, layout.height)) == (p, fits, BLOCKS[p])


def test_the_block_is_centred_and_never_off_screen():
    for cols in range(0, 300, 7):
        for rows in range(0, 160, 5):
            layout = arcade_layout(cols, rows)
            assert layout.left == max(0, (cols - layout.width) // 2)
            assert layout.top == max(0, (rows - layout.height) // 2)


def test_the_overlay_bands_follow_the_cabinet():
    down = [band(0, y) for y in (0, 31, 32, 61, 62, 183, 184, 239)]
    assert down == [WHITE, WHITE, RED, RED, WHITE, WHITE, GREEN, GREEN]
    assert [band(x, 240) for x in (0, 15, 16, 133, 134, 223)] == [WHITE, WHITE, GREEN, GREEN, WHITE, WHITE]
    assert band(100, 255) == GREEN and band(200, 255) == WHITE
    assert (WHITE, RED, GREEN) == (SLOT["arcade_white"], SLOT["arcade_red"], SLOT["arcade_green"])


@pytest.mark.parametrize("p", [1, 2, 3, 4])
def test_pooling_keeps_every_lit_pixel_and_invents_none(p):
    game, rng = played(200), random.Random(p)
    for index in rng.sample(range(len(game.vram)), 900):
        game.vram[index] = rng.randrange(256)
    view = ArcadeView(THEME, charset=CHARSET)
    view.update(game)
    shown = view.shown(game)
    masked = set().union(*(cells(addr) for addr, _ in shown))
    layer = view.pool(p, shown)
    assert (layer.w, layer.h) == (-(-224 // p), -(-256 // p))
    expected = bytearray(layer.w * layer.h)
    for x in range(224):
        for y in range(256):
            if lit(game, x, y) and (x, y) not in masked:
                expected[y // p * layer.w + x // p] = 1
    assert bytes(1 if role else 0 for role in layer.px) == bytes(expected)
    for oy in range(layer.h):
        for ox in range(layer.w):
            role = layer.px[oy * layer.w + ox]
            assert role in (0, band(ox * p, oy * p))  # the band of the block's top-left pixel


def test_text_shows_as_terminal_text_in_its_cells_band_and_masks_its_pixels():
    game = FakeArcade()
    buf, view, painter = drawn(game, 2)
    header = buf.row_text((8 + 4) // 4)  # the header's cells start at y 8
    assert header[:45] == "    S   C   O   R   E   <   1   >       H   I"
    score = buf.row_text((24 + 4) // 4)
    assert score[12:28] == "0   0   0   0   "  # P1 at Xr 56: column 24 / 2
    assert buf.styles[3 * 112 + 4] == painter.colors[WHITE]
    shown = dict(view.shown(game))
    assert shown[HEADER + 0x100] == "S" and shown[HEADER] == " "
    layer = view.pool(2, view.shown(game))
    for x, y in cells(P1_SCORE):
        assert layer.px[y // 2 * layer.w + x // 2] == 0  # the glyph's pixels are hidden under its text


def test_text_in_the_red_and_green_bands_takes_their_colour():
    game = FakeArcade()
    saucer, low = 0x2400 + 32 * 88 + 0x1A, 0x2400 + 32 * 60 + 7  # y 40 (the saucer's score), y 192 (the shields' row)
    game.print(saucer, "1")
    game.print(low, "2")
    for p in (1, 2, 4):
        buf, _, painter = drawn(game, p)
        for addr, char, role in ((saucer, "1", RED), (low, "2", GREEN), (HEADER + 0x100, "S", WHITE)):
            index = addr - 0x2400
            x, y = index // 32, 248 - 8 * (index % 32)
            cell = (y + 4) // (2 * p) * BLOCKS[p][0] + x // p
            assert (buf.chars[cell], buf.styles[cell]) == (char, painter.colors[role]), (p, char)


def test_a_character_shows_as_pixels_once_its_bytes_change():
    game = FakeArcade()
    addr = P1_SCORE + 0x100  # the second digit
    game.vram[addr - 0x2400 + 32 * 3] ^= 0x10  # shot through: one byte no longer matches the glyph
    buf, view, _ = drawn(game, 1)
    assert addr not in dict(view.shown(game))
    row = buf.row_text((24 + 4) // 2)
    assert row[24] == row[40] == "0" and "0" not in row[32:40] and row[32:40].strip()  # the second digit is pixels
    x = (addr - 0x2400) // 32
    layer = view.pool(1, view.shown(game))
    assert any(layer.px[y * 224 + x + dx] for y in range(24, 32) for dx in range(8))  # its pixels are drawn
    game.text[addr] = 0x3E  # a code the charset does not name: pixels too
    game.vram[addr - 0x2400 + 32 * 3] ^= 0x10
    assert addr not in dict(view.shown(game))


def test_the_hi_score_shows_the_profile_best_or_the_score_when_higher():
    game = FakeArcade()
    buf = drawn(game, 2, best=1230)[0]
    hi = (HI_SCORE - 0x2400) // 32 // 2
    assert buf.row_text(7)[hi : hi + 16] == "1   2   3   0   "
    game.score = 4560
    assert drawn(game, 2, best=1230)[0].row_text(7)[hi : hi + 16] == "4   5   6   0   "
    game.score = 1230
    assert drawn(game, 2, best=12340)[0].row_text(7)[hi : hi + 16] == "2   3   4   0   "  # in four digits, wrapped
    game.score = 4560  # past the wrapped best: HI-SCORE never shows less than the score beside it
    assert drawn(game, 2, best=12340)[0].row_text(7)[hi : hi + 16] == "4   5   6   0   "
    game.score = 14560  # a wrapped score too
    assert drawn(game, 2, best=12340)[0].row_text(7)[hi : hi + 16] == "4   5   6   0   "
    for n in range(4):
        start = HI_SCORE - 0x2400 + 0x100 * n
        game.vram[start : start + 225 : 32] = bytes(8)  # the field cleared: nothing to show
    assert drawn(game, 2, best=1230)[0].row_text(7)[hi : hi + 16].strip() == ""


def test_only_the_columns_written_since_the_last_frame_are_rebuilt():
    game = played(50)
    view = ArcadeView(THEME, charset=CHARSET)
    view.update(game)
    assert game.dirty == set()
    game.vram[32 * 100 + 10] = 0xFF  # behind the view's back: not marked dirty
    game.step(())
    assert game.dirty and 100 not in game.dirty
    view.update(game)
    assert view.pixels[(255 - 80) * 224 + 100] == 0  # still the old column
    game.dirty = None
    view.update(game)
    assert view.pixels[(255 - 80) * 224 + 100] == 1 and game.dirty == set()
    other = played(50)
    other.vram[32 * 100 + 10] = 0xFF
    other.dirty = set()
    view.update(other)  # a game new to the view: every column
    assert view.pixels[(255 - 80) * 224 + 100] == 1


def test_where_half_blocks_are_unsafe_cells_pool_2_to_1_into_spaces():
    game = played(100)
    safe = drawn(game, 2)[0]
    unsafe = drawn(game, 2, caps=SimpleNamespace(unicode_blocks=False))[0]
    blocks = [c for c in safe.chars if c in (UPPER, LOWER)]
    assert blocks and not [c for c in unsafe.chars if c in (UPPER, LOWER)]
    lit_safe = sum(1 for st in safe.styles if st)
    assert sum(1 for st in unsafe.styles if st) == lit_safe  # every cell with a lit half still colours, half or full


def test_mono_draws_shapes_without_colour():
    game = played(100)
    mono = resolve_theme(SimpleNamespace(color="mono", theme="dark", paint_background=False))
    buf = drawn(game, 2, theme=mono)[0]
    assert {c for c in buf.chars} >= {UPPER, LOWER, "S"} and not [st for st in buf.styles if st & 0xFFFF]


def test_the_glyphs_the_view_matches_come_from_the_games_rom():
    game = FakeArcade()
    assert bytes(game.rom[0x1E00 + 8 * 0x12 : 0x1E00 + 8 * 0x13]) == glyph(0x12)  # S
    game.rom = bytes(0x2000)  # a ROM image without that font: no text matches
    assert [char for _, char in ArcadeView(THEME, charset=CHARSET).shown(game) if char != " "] == []


def test_a_frame_at_p_2_takes_well_under_the_budget():
    """Spec 6: under 2 ms per frame at p = 2 on the build VM (tools/bench_render.py --arcade); 20 ms here, loosely."""
    game, buf = played(60), CellBuffer(112, 64)
    painter = Painter(buf, defaults(), THEME)
    painter.place("playing", arcade=True)
    session = SimpleNamespace(pilot=SimpleNamespace(game=game), profile=SimpleNamespace(best=lambda config: 0))
    view, took = ArcadeView(THEME, charset=CHARSET), []
    for tick in range(120):
        game.step(("fire",) if tick % 2 else ("left",))
        start = time.perf_counter()
        view.draw(painter, session)
        took.append(time.perf_counter() - start)
    assert statistics.median(took) < 0.020


def test_the_real_games_dirty_columns_keep_the_view_equal_to_a_full_rebuild():
    game, view, rng = ArcadeGame(Config(mode="arcade")), ArcadeView(THEME), random.Random(5)
    for tick in range(3000):
        game.step(rng.sample(["left", "right", "fire"], rng.randrange(3)))
        view.update(game)
        if tick % 300 == 299:
            fresh = ArcadeView(THEME)
            fresh.update(game)
            assert fresh.pixels == view.pixels, tick
