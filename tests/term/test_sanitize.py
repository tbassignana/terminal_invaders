"""Text reaches the screen one code point per cell: controls, invisible formats, marks and wide glyphs never do."""

import pytest

from terminal_invaders.term.sanitize import _slow, sanitize


def test_ascii_fast_path_returns_the_same_string():
    text = "PILOT ACE  |  SOUND ON [M]"
    assert sanitize(text) is text
    assert sanitize("") == ""


def test_tab_cr_and_lf_become_spaces():
    assert sanitize("a\tb\rc\nd") == "a b c d"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("a\x00b\x07c\x1b[31md\x7f", "abc[31md"),  # C0 and DEL: an injected escape sequence loses its ESC
        ("a\x85b\x9b31mc", "ab31mc"),  # C1, including the 8-bit CSI
        ("abc\u202edef", "abcdef"),  # right-to-left override
        ("a\u2066b\u2069c", "abc"),  # bidi isolates
        ("a\u200db\u200cc\ufeffd\u00ade", "abcde"),  # ZWJ, ZWNJ, BOM, soft hyphen
        ("e\u0301te\u0301", "ete"),  # combining acute (Mn)
        ("x\u20ddy", "xy"),  # enclosing circle (Me)
        ("ka\u0903", "ka"),  # Devanagari visarga (Mc)
        ("a\ue000b", "ab"),  # private use
        ("a\u0378b", "ab"),  # unassigned
        ("a\ud800b", "ab"),  # lone surrogate
        ("a\u2028b\u2029c", "abc"),  # line and paragraph separators
    ],
)
def test_dropped_categories(text, expected):
    assert sanitize(text) == expected


@pytest.mark.parametrize("text", ["\u6f22", "\uff21", "\U0001f600", "\u3000"])
def test_wide_and_fullwidth_become_a_question_mark(text):
    assert sanitize(text) == "?"
    assert sanitize(text, ambiguous_narrow=False) == "?"


def test_ambiguous_width_is_kept_only_when_narrow():
    assert sanitize("\u2192 caf\u00e9") == "\u2192 caf\u00e9"
    assert sanitize("\u2192 caf\u00e9", ambiguous_narrow=False) == "? caf?"
    assert sanitize("\u00f1\u0151", ambiguous_narrow=False) == "\u00f1\u0151"  # neutral width stays


def test_one_code_point_per_cell_for_a_hostile_name():
    name = "\u202e\x1b]2;pwn\x07\u6f22e\u0301\ttab"
    assert sanitize(name) == "]2;pwn?e tab"


def test_slow_path_is_cached():
    _slow.cache_clear()
    sanitize("caf\u00e9")
    sanitize("caf\u00e9")
    assert _slow.cache_info().hits == 1
    assert _slow.cache_info().maxsize == 4096
