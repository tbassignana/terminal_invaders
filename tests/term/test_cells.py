"""CellBuffer: the curses-compatible surface the Painter draws on; it clips everything and never raises."""

from array import array
from functools import partial

import pytest

from terminal_invaders.term.cells import A_BOLD, A_DIM, A_REVERSE, CellBuffer, style
from terminal_invaders.term.sanitize import sanitize


def test_style_bit_layout():
    assert (A_BOLD, A_DIM, A_REVERSE) == (1 << 16, 1 << 17, 1 << 18)
    assert style() == 0
    assert style(5, 7, A_BOLD | A_REVERSE) == 5 | 7 << 8 | 1 << 16 | 1 << 18
    assert style(fg=255, bg=255, attrs=A_DIM) & 0xFFFF == 0xFFFF


def test_new_buffer_is_blank_and_curses_shaped():
    buf = CellBuffer(6, 3)
    assert buf.getmaxyx() == (3, 6)
    assert (buf.cols, buf.rows) == (6, 3)
    assert buf.snapshot() == ((" " * 6,) * 3, (0,) * 18)
    assert isinstance(buf.styles, array) and buf.styles.typecode == "I"
    assert buf.noutrefresh() is None


def test_addstr_writes_text_and_style():
    buf = CellBuffer(10, 2)
    buf.addstr(1, 2, "ABC", style(3) | A_BOLD)
    assert buf.row_text(1) == "  ABC     "
    assert list(buf.styles[12:15]) == [3 | A_BOLD] * 3
    assert buf.styles[11] == buf.styles[15] == 0


@pytest.mark.parametrize(
    "y, x, text, row",
    [
        (0, 7, "ABCDEF", "       ABC"),  # right edge
        (0, -2, "ABCDEF", "CDEF      "),  # left edge, as Painter.text clips
        (0, -6, "ABCDEF", " " * 10),  # entirely left
        (0, -100, "ABCDEF", " " * 10),
        (0, 10, "ABC", " " * 10),  # at the right edge
        (0, 50, "ABC", " " * 10),
        (0, 0, "", " " * 10),
        (0, 0, "0123456789", "0123456789"),  # exactly the width
        (0, 9, "XY", "         X"),  # the last cell
    ],
)
def test_addstr_clips_horizontally(y, x, text, row):
    buf = CellBuffer(10, 2)
    buf.addstr(y, x, text)
    assert buf.row_text(0) == row
    assert len(buf.chars) == len(buf.styles) == 20


@pytest.mark.parametrize("y", [-1, 2, 99])
def test_addstr_outside_the_rows_is_ignored(y):
    buf = CellBuffer(10, 2)
    buf.addstr(y, 0, "ABC")
    assert buf.snapshot() == CellBuffer(10, 2).snapshot()


def test_bottom_right_cell_is_written_without_error():
    buf = CellBuffer(4, 2)
    buf.addstr(1, 3, "Z", A_DIM)
    assert buf.row_text(1) == "   Z" and buf.styles[7] == A_DIM


@pytest.mark.parametrize("rows, cols", [(1, 1), (10, 30), (24, 59), (40, 100)])
def test_tiny_and_resized_buffers_never_raise(rows, cols):
    """The FakeScreen clipping cases: any position, any length, never out of bounds."""
    buf = CellBuffer(cols, rows)
    for y in range(-2, rows + 2):
        for x in range(-70, cols + 2, 7):
            buf.addstr(y, x, "EARTH NEEDS ANOTHER QUARTER" * 3, style(6))
            buf.put(y, x, "#", 0)
            buf.fill(y, x, 9, 2, ".", style(2))
    assert len(buf.chars) == len(buf.styles) == rows * cols
    assert all(len(buf.row_text(y)) == cols for y in range(rows))


def test_addstr_sanitizes_all_text():
    buf = CellBuffer(12, 1)
    buf.addstr(0, 0, "a\x1b[2Jb\u6f22\tc\u0301")
    assert buf.row_text(0) == "a[2Jb? c    "


def test_sanitizer_is_injectable():
    buf = CellBuffer(6, 1, sanitizer=partial(sanitize, ambiguous_narrow=False))
    buf.addstr(0, 0, "\u2192 ok")
    assert buf.row_text(0) == "? ok  "


def test_sanitizing_happens_before_clipping():
    buf = CellBuffer(4, 1)
    buf.addstr(0, 1, "\u202eABCD")
    assert buf.row_text(0) == " ABC"


def test_put_writes_one_trusted_cell_and_clips():
    buf = CellBuffer(3, 2)
    buf.put(1, 2, "\u2580", style(10, 7))
    for y, x in ((-1, 0), (0, -1), (2, 0), (0, 3)):
        buf.put(y, x, "X", 1)
    assert buf.snapshot() == (("   ", "  \u2580"), (0, 0, 0, 0, 0, style(10, 7)))


def test_fill_clips_to_the_grid():
    buf = CellBuffer(5, 3)
    buf.fill(1, 3, 10, 10, "#", style(9))
    buf.fill(0, -2, 3, 1, "<")
    buf.fill(0, 4, 0, 3, "!")  # zero width
    buf.fill(-5, 0, 2, 2, "?")  # entirely above
    assert [buf.row_text(y) for y in range(3)] == ["<    ", "   ##", "   ##"]
    assert buf.styles[8] == style(9) and buf.styles[0] == 0


def test_erase_blanks_every_cell_in_place():
    buf = CellBuffer(4, 2)
    chars, styles = buf.chars, buf.styles
    buf.fill(0, 0, 4, 2, "x", A_REVERSE)
    buf.erase()
    assert buf.chars is chars and buf.styles is styles
    assert buf.snapshot() == CellBuffer(4, 2).snapshot()


def test_resize_reallocates_blank():
    buf = CellBuffer(4, 2)
    buf.addstr(0, 0, "abcd")
    buf.resize(7, 3)
    assert buf.getmaxyx() == (3, 7)
    assert buf.snapshot() == CellBuffer(7, 3).snapshot()
    buf.resize(0, -1)
    assert buf.getmaxyx() == (0, 0) and buf.snapshot() == ((), ())
    buf.addstr(0, 0, "x")
    buf.fill(0, 0, 1, 1)


def test_row_text_and_snapshot():
    buf = CellBuffer(3, 2)
    buf.addstr(0, 0, "ab", 4)
    buf.addstr(1, 1, "cd", 5)
    assert buf.row_text(0) == "ab " and buf.row_text(1) == " cd"
    assert buf.snapshot() == (("ab ", " cd"), (4, 4, 0, 0, 5, 5))


def test_copy_is_independent():
    buf = CellBuffer(3, 2, sanitizer=str.upper)
    buf.addstr(0, 0, "ab", 4)
    other = buf.copy()
    assert other.snapshot() == buf.snapshot() and other.sanitizer is buf.sanitizer
    buf.addstr(1, 0, "xyz", 5)
    assert other.row_text(1) == "   " and other.styles[3] == 0
    other.erase()  # the copy blanks to its own size, without touching the original
    assert other.snapshot() == CellBuffer(3, 2).snapshot() and buf.row_text(1) == "XYZ"
    buf.erase()
    assert buf.snapshot() == other.snapshot()
