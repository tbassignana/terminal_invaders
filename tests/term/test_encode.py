"""The encoder's bytes, applied by a whitelist VT, always reproduce the buffer; and they stay small and predictable."""

import random
import re
from array import array
from dataclasses import dataclass

import pytest

from terminal_invaders.term.caps import TIERS
from terminal_invaders.term.cells import A_BOLD, A_DIM, A_REVERSE, CellBuffer, style
from terminal_invaders.term.encode import (
    BSU,
    ESU,
    MAX_GAP,
    SYNC_LIMIT,
    TAG_COLS,
    TAG_FIRST,
    TAG_LAST,
    TAG_SKIP,
    Encoder,
    _spans,
    ack_tag,
    packet,
    tag_count,
)
from terminal_invaders.theme import ROLES, SLOT, resolve_theme
from tests.support.vt import MiniVT, assert_shows

CHARS = " .#AZ|!^-\u2580\u2584"
SGR = re.compile(rb"\x1b\[([0-9;]*)m")


@dataclass(frozen=True)
class Caps:
    color: str = "truecolor"
    theme: str = "dark"
    paint_background: bool = True
    sync: bool = True
    ack: str | None = "dsr"
    utf8: bool = True


def theme_for(tier, name="dark", paint=True):
    return resolve_theme(Caps(color=tier, theme=name, paint_background=paint))


def screen(cols, rows):
    vt = MiniVT(cols, rows)
    vt.feed(b"\x1b[?7l")  # ENTER turns autowrap off
    return vt


def random_style(rng):
    attrs = rng.choice(
        (0, 0, 0, A_BOLD, A_DIM, A_REVERSE, A_BOLD | A_DIM, A_BOLD | A_REVERSE, A_BOLD | A_DIM | A_REVERSE)
    )
    return style(rng.choice((0, 0, rng.randrange(len(ROLES)))), rng.choice((0, 0, 0, rng.randrange(len(ROLES)))), attrs)


def random_buffer(rng, cols, rows):
    buf = CellBuffer(cols, rows)
    palette = [random_style(rng) for _ in range(rng.randint(1, 5))]
    for i in range(cols * rows):
        buf.chars[i], buf.styles[i] = rng.choice(CHARS), rng.choice(palette)
    return buf


def mutate(rng, prev):
    cur = CellBuffer(prev.cols, prev.rows)
    cur.chars[:], cur.styles[:] = prev.chars, prev.styles
    size = len(cur.chars)
    for _ in range(rng.choice((0, 1, 2, 4, 8, size // 2 + 1))):
        start = rng.randrange(size)
        for i in range(start, min(size, start + rng.choice((1, 1, 2, 3, 6)))):
            if rng.random() < 0.6:
                cur.chars[i] = rng.choice(CHARS)
            if rng.random() < 0.5:
                cur.styles[i] = rng.choice((random_style(rng), cur.styles[max(0, i - 1)], prev.styles[i]))
    return cur


@pytest.mark.parametrize("tier", TIERS)
def test_random_pairs_round_trip_through_minivt(tier):
    rng = random.Random(f"encode-{tier}")
    for seed in range(500):
        theme = theme_for(tier, ("dark", "light")[seed % 2], paint=seed % 3 != 0)
        encoder = Encoder(theme, Caps())
        cols, rows = rng.randint(1, 24), rng.randint(1, 6)
        prev = random_buffer(rng, cols, rows)
        cur = mutate(rng, prev) if seed % 10 else random_buffer(rng, cols, rows)
        vt = screen(cols, rows)
        vt.feed(encoder.encode(None, prev))
        assert_shows(vt, prev, theme)
        body = encoder.encode(prev, cur)
        vt.feed(body)
        assert_shows(vt, cur, theme)
        assert not re.search(rb"\x1b\[[0-9;]*J", body)  # no ED, ever
        assert b":" not in body  # semicolon form only


@pytest.mark.parametrize("tier", TIERS)
def test_first_sgr_of_every_frame_is_a_full_reset_and_later_ones_are_deltas(tier):
    rng = random.Random(tier)
    encoder, prev = Encoder(theme_for(tier), Caps()), None
    for _ in range(50):
        cur = random_buffer(rng, 12, 3) if prev is None else mutate(rng, prev)
        body = encoder.encode(prev, cur)
        params = SGR.findall(body)
        if body:
            assert re.match(rb"\x1b\[\d+;\d+H\x1b\[0[;m]", body)  # CUP, then the full reset form
            assert params[0] == b"0" or params[0].startswith(b"0;")
            assert not any(p == b"0" or p.startswith(b"0;") for p in params[1:])
        prev = cur


def test_changed_spans_see_a_change_in_any_byte_of_a_cell():
    old, new = [" "] * 9, [" "] * 9
    new[0], new[8] = "\u2580", "\U0001f600"  # high code points differ in the upper bytes of the UTF-32 plane
    old_styles, new_styles = array("I", [0] * 9), array("I", [0] * 9)
    new_styles[3], new_styles[4], new_styles[5] = A_REVERSE, style(0, 9), style(1)
    assert _spans(old, new, old_styles, new_styles) == [[0, 1], [3, 6], [8, 9]]
    assert _spans(old, old, old_styles, old_styles) == []


def frame(theme, cells, prev=None):
    """Encode one row of (char, style) cells (None = unchanged) against a blank or given previous row."""
    old = prev or CellBuffer(len(cells), 1)
    cur = CellBuffer(old.cols, 1)
    cur.chars[:], cur.styles[:] = old.chars, old.styles
    for x, cell in enumerate(cells):
        if cell is not None:
            cur.chars[x], cur.styles[x] = cell
    return Encoder(theme, Caps()).encode(old, cur)


def test_attribute_deltas_are_minimal():
    mono = theme_for("mono")
    cells = [("A", A_BOLD | A_DIM), ("B", A_DIM), ("C", 0), ("D", A_REVERSE), ("E", A_BOLD), ("F", A_BOLD | A_DIM)]
    body = frame(mono, cells)
    assert body == b"\x1b[1;1H\x1b[0;1;2mA\x1b[22;2mB\x1b[22mC\x1b[7mD\x1b[27;1mE\x1b[2mF"


def test_colour_deltas_send_only_what_changed():
    dark16 = theme_for("16", paint=False)
    alien, shot, warn = SLOT["alien"], SLOT["enemy_shot"], SLOT["warn"]
    cells = [("a", style(alien)), ("b", style(alien, shot)), ("c", style(warn, shot)), ("d", 0), ("e", style(0, shot))]
    assert frame(dark16, cells) == b"\x1b[1;1H\x1b[0;37ma\x1b[103mb\x1b[33mc\x1b[39;49md\x1b[103me"
    true = theme_for("truecolor")
    body = frame(true, [("x", style(SLOT["player"])), ("y", 0)])
    assert body == b"\x1b[1;1H\x1b[0;38;2;86;180;233;48;2;11;14;26mx\x1b[38;2;225;225;232my"


def test_short_gaps_in_the_pen_style_are_resent_and_longer_ones_skipped():
    mono = theme_for("mono")
    blank = [None] * 20
    near = list(blank)
    near[0], near[5] = ("X", 0), ("Y", 0)  # gap of 4 blank cells in the pen's own style
    assert frame(mono, near) == b"\x1b[1;1H\x1b[0mX    Y"
    far = list(blank)
    far[0], far[6] = ("X", 0), ("Y", 0)  # gap of 5
    assert frame(mono, far) == b"\x1b[1;1H\x1b[0mX\x1b[5CY"
    other = list(blank)
    other[0], other[3] = ("X", A_BOLD), ("Y", A_BOLD)  # gap of 2, but in another pen
    assert frame(mono, other) == b"\x1b[1;1H\x1b[0;1mX\x1b[2CY"
    assert MAX_GAP == 4


def test_gap_cells_compare_by_rendered_pen_not_by_slot():
    mono = theme_for("mono")
    prev = CellBuffer(6, 1)
    prev.styles[1] = prev.styles[2] = style(SLOT["alien"])  # mono renders every slot alike
    body = frame(mono, [("X", 0), None, None, ("Y", 0)], prev=prev)
    assert body == b"\x1b[1;1H\x1b[0mX  Y"


def test_each_row_starts_with_cup_and_the_last_column_forces_it():
    mono = theme_for("mono")
    prev, cur = CellBuffer(8, 3), CellBuffer(8, 3)
    cur.chars[7], cur.chars[8], cur.chars[23] = "a", "b", "c"
    body = Encoder(mono, Caps()).encode(prev, cur)
    assert body == b"\x1b[1;8H\x1b[0ma\x1b[2;1Hb\x1b[3;8Hc"
    vt = screen(8, 3)
    vt.feed(body)
    assert vt.cursor_lost  # writing the last column leaves the cursor undefined


def test_full_repaint_paints_every_cell_without_ed_whatever_the_screen_held():
    theme = theme_for("256", "light")
    rng = random.Random(7)
    cur = random_buffer(rng, 10, 4)
    vt = screen(10, 4)
    vt.feed(b"\x1b[0;7;38;5;196;48;5;21m" + b"\x1b[1;1H" + b"garbage!!!" + b"\x1b[3;3Hjunk")
    encoder = Encoder(theme, Caps())
    body = encoder.encode(None, cur)
    vt.feed(body)
    assert_shows(vt, cur, theme)
    assert b"J" not in body and len(re.findall(rb"\x1b\[\d;1H", body)) == 4
    assert encoder.last_full


def test_invalidate_and_size_changes_force_a_full_repaint():
    theme = theme_for("truecolor")
    encoder = Encoder(theme, Caps())
    a, b = CellBuffer(5, 2), CellBuffer(5, 2)
    assert encoder.encode(None, a).count(b"\x1b[") == 3 and encoder.last_full
    assert encoder.encode(a, b) == b"" and not encoder.last_full
    encoder.invalidate()
    assert encoder.encode(a, b).count(b"H") == 2 and encoder.last_full
    assert encoder.encode(a, b) == b"" and not encoder.last_full
    encoder.encode(a, CellBuffer(6, 2))
    assert encoder.last_full


def test_non_utf8_terminals_get_one_byte_per_cell():
    buf = CellBuffer(3, 1)
    buf.chars[:] = ["\u2580", "a", "\u2584"]
    body = Encoder(theme_for("mono"), Caps(utf8=False)).encode(None, buf)
    assert body == b"\x1b[1;1H\x1b[0m?a?"
    assert "\u2580".encode() in Encoder(theme_for("mono"), Caps()).encode(None, buf)


def test_packet_wraps_the_body_in_the_exact_sync_bytes_then_the_ack():
    assert (BSU, ESU) == (b"\x1b[?2026h", b"\x1b[?2026l")
    assert packet(b"BODY", Caps(ack="dsr")) == b"\x1b[?2026hBODY\x1b[?2026l\x1b[5n"
    assert packet(b"BODY", Caps(ack="da1")) == b"\x1b[?2026hBODY\x1b[?2026l\x1b[c"
    assert packet(b"BODY", Caps(ack=None)) == b"\x1b[?2026hBODY\x1b[?2026l"
    assert packet(b"BODY", Caps(sync=False, ack="dsr")) == b"BODY\x1b[5n"
    assert SYNC_LIMIT == 1 << 20  # 7.8: 1 MiB, half of Alacritty's sync buffer
    huge = b"x" * (SYNC_LIMIT + 1)
    assert packet(huge, Caps()) == huge + b"\x1b[5n"
    assert packet(b"x" * SYNC_LIMIT, Caps()).startswith(BSU)


@pytest.mark.parametrize(
    "cols, rows, cycle",
    [(80, 24, 944), (240, 70, 944), (60, 24, 944), (59, 24, 902), (80, 23, 854), (40, 12, 191), (80, 5, 44)],
)
def test_ack_tags_cycle_on_the_screen_and_off_a_smaller_one_s_edges(cols, rows, cycle):
    """Each tag in a cycle is distinct, apart from the first (clock pacing's), on the screen, and under the probe's
    rows. None is on the last row of a screen with fewer rows than the tag area, nor on the last column of one with
    fewer columns, where a terminal that shrank clamps the tags of a larger one (tag_area); a screen as tall or as wide
    as the tag area clamps nothing in that dimension. None is in columns 2 to 16, where a reply clamped onto row 1
    would read like F3 with modifiers (TAG_SKIP)."""
    tags = [ack_tag(seq, cols, rows) for seq in range(cycle)]
    assert ack_tag(None, cols, rows) == (TAG_FIRST, 1)
    assert len(set(tags)) == cycle and (TAG_FIRST, 1) not in tags
    assert ack_tag(cycle, cols, rows) == tags[0]
    assert tag_count(cols, rows) == cycle + 1
    for row, col in tags:
        assert TAG_FIRST <= row <= min(rows, TAG_LAST) and 1 <= col <= min(cols, TAG_COLS)
        assert (rows >= TAG_LAST or row < rows) and (cols >= TAG_COLS or col < cols)
        assert col not in TAG_SKIP


def test_ack_tags_start_at_the_second_position_and_need_two_of_them():
    """No tag (and no ack query) where fewer than two fit: under 5 rows a reply would be a clamped one (row 4, the last
    row), read as the probe's own CPR (rows 2 and 3) or as F3 (row 1)."""
    assert (ack_tag(None, 80, 24), ack_tag(0, 80, 24), ack_tag(43, 80, 24)) == ((4, 1), (4, 17), (4, 60))
    assert (ack_tag(44, 80, 24), ack_tag(45, 80, 24)) == ((5, 1), (5, 17))
    for cols, rows in ((80, 4), (80, 3), (80, 2), (80, 1), (2, 5), (17, 5), (1, 24), (0, 0)):
        assert ack_tag(0, cols, rows) is None and ack_tag(None, cols, rows) is None
    assert ack_tag(5, 3, 6) == ack_tag(6, 3, 6) == (5, 1)  # two tags: every tracked frame shares the second
    assert ack_tag(0, 18, 5) == (4, 17)  # a screen 18 wide: column 1, then 17, off its last column


def test_a_cpr_ack_parks_the_cursor_at_the_tag_and_without_one_is_left_out():
    assert packet(b"BODY", Caps(ack="cpr"), (4, 2)) == BSU + b"BODY" + ESU + b"\x1b[4;2H\x1b[6n"
    assert packet(b"BODY", Caps(ack="cpr"), None) == BSU + b"BODY" + ESU
    assert packet(b"BODY", Caps(sync=False, ack="cpr"), (24, 60)) == b"BODY\x1b[24;60H\x1b[6n"


def test_a_synchronized_packet_is_one_minivt_frame():
    theme = theme_for("truecolor")
    buf = CellBuffer(4, 1)
    buf.addstr(0, 0, "GO", style(SLOT["good"]))
    frames = []
    vt = screen(4, 1)
    vt.on_esu = lambda term: frames.append(term.lines())
    vt.feed(packet(Encoder(theme, Caps()).encode(None, buf), Caps()))
    assert frames == [["GO  "]] and vt.queries == ["dsr"]
