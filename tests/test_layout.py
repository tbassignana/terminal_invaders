"""The scale ladder picks the largest block that fits and centres it; small windows never get negative origins.

Half blocks are no condition: where they are unsafe the pixel arena pools 2:1 (tests/test_arena.py checks that)."""

import pytest

from terminal_invaders.layout import Layout, compute_layout


@pytest.mark.parametrize(
    "cols, rows, k, left, top",
    [
        (60, 24, 1, 0, 0),
        (80, 24, 1, 10, 0),
        (119, 44, 1, 29, 10),
        (120, 43, 1, 30, 9),
        (120, 44, 2, 0, 0),
        (179, 64, 2, 29, 10),
        (180, 64, 3, 0, 0),
        (240, 70, 3, 30, 3),  # letterboxed
        (240, 84, 4, 0, 0),
        (359, 124, 4, 59, 20),
        (360, 124, 6, 0, 0),
        (500, 200, 6, 70, 38),
    ],
)
def test_ladder(cols, rows, k, left, top):
    layout = compute_layout(cols, rows, max_k=None)
    assert (layout.k, layout.left, layout.top) == (k, left, top)
    assert (layout.width, layout.height) == (60 * k, 20 * k + 4)
    assert layout.fits and (layout.cols, layout.rows) == (cols, rows)
    assert layout.left + layout.width <= cols and layout.top + layout.height <= rows


@pytest.mark.parametrize("max_k, k", [(1, 1), (2, 2), (3, 3), (4, 4), (5, 4), (6, 6), (None, 6)])
def test_scale_flag_caps_k(max_k, k):
    assert compute_layout(400, 200, max_k=max_k).k == k


def test_capped_layout_is_centred_in_a_big_window():
    layout = compute_layout(240, 70, max_k=1)
    assert (layout.k, layout.left, layout.top, layout.width, layout.height) == (1, 90, 23, 60, 24)


@pytest.mark.parametrize("cols, rows, fits", [(59, 24, False), (60, 23, False), (1, 1, False), (0, 0, False)])
def test_too_small_windows(cols, rows, fits):
    layout = compute_layout(cols, rows, max_k=None)
    assert layout == Layout(cols, rows, 1, 0, 0, 60, 24, fits)
