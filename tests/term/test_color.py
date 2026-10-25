"""SGR colour fragments per tier and the xterm 256-colour helpers."""

import random

import pytest

from terminal_invaders.term.color import fragment, nearest256, xterm_rgb


@pytest.mark.parametrize(
    "tier, value, fg, bg",
    [
        ("truecolor", 0x56B4E9, "38;2;86;180;233", "48;2;86;180;233"),
        ("truecolor", 0x000000, "38;2;0;0;0", "48;2;0;0;0"),
        ("256", 74, "38;5;74", "48;5;74"),
        ("16", 31, "31", "41"),
        ("16", 96, "96", "106"),
        ("truecolor", None, "39", "49"),
        ("256", None, "39", "49"),
        ("16", None, "39", "49"),
        ("mono", 0xFFFFFF, "", ""),
        ("mono", None, "", ""),
    ],
)
def test_fragments(tier, value, fg, bg):
    assert fragment(tier, value) == fg
    assert fragment(tier, value, bg=True) == bg


def test_xterm_rgb_known_values():
    assert [xterm_rgb(i) for i in (16, 21, 59, 196, 231, 232, 244, 255)] == [
        0x000000,
        0x0000FF,
        0x5F5F5F,
        0xFF0000,
        0xFFFFFF,
        0x080808,
        0x808080,
        0xEEEEEE,
    ]


def test_nearest256_round_trips_every_index():
    assert [nearest256(xterm_rgb(i)) for i in range(16, 256)] == list(range(16, 256))


def test_nearest256_is_the_true_nearest_by_rgb_distance():
    rng = random.Random(256)

    def distance(a, b):
        return sum(((a >> s & 255) - (b >> s & 255)) ** 2 for s in (16, 8, 0))

    for _ in range(3000):
        rgb = rng.getrandbits(24)
        best = min(distance(xterm_rgb(i), rgb) for i in range(16, 256))
        index = nearest256(rgb)
        assert 16 <= index <= 255 and distance(xterm_rgb(index), rgb) == best
