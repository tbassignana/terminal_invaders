"""MiniVT itself: the sequences the game sends are modelled, and anything else fails loudly."""

import pytest

from tests.support.vt import MiniVT, Pen, VTError, sgr

ENTER = b"\x1b[?1049h\x1b[22;0;0t\x1b(B\x1b[0m\x1b[4l\x1b[?6l\x1b[r\x1b[?25l\x1b[?7l\x1b[?1004h\x1b[?2004h\x1b[H\x1b[2J"
LEAVE = b"\x1b[0m\x1b[?2004l\x1b[?1004l\x1b[?7h\x1b[?25h\x1b[?1049l\x1b[23;0;0t"


def test_enter_and_leave_restore_the_main_screen():
    vt = MiniVT(10, 3)
    vt.feed(b"shell$ ")
    vt.feed(ENTER + b"\x1b]2;Terminal Invaders\x1b\\\x1b[>11u")
    assert vt.alt_screen and not vt.cursor_visible and not vt.autowrap and vt.focus and vt.paste
    assert vt.lines() == [" " * 10] * 3 and vt.title == "Terminal Invaders" and vt.kitty_depth == 1
    vt.feed(b"\x1b[2;3H\x1b[1;38;5;74mgame")
    assert vt.cell(1, 2) == ("g", Pen(bold=True, fg=("256", 74)))
    vt.feed(b"\x18\x1b[?2026l\x1b[<u" + LEAVE)
    assert not vt.alt_screen and vt.cursor_visible and vt.autowrap and not vt.focus and not vt.paste
    assert vt.lines()[0] == "shell$    " and (vt.y, vt.x) == (0, 7)
    assert vt.kitty_depth == 0 and vt.titles == [] and vt.title == ""


def test_kitty_stacks_are_per_screen_and_pop_counts():
    vt = MiniVT(4, 2)
    vt.feed(b"\x1b[>1u\x1b[?1049h\x1b[>11u\x1b[>15u")
    assert vt.kitty == {"main": [1], "alt": [11, 15]} and vt.kitty_depth == 3
    vt.feed(b"\x1b[<5u\x1b[?1049l\x1b[<u\x1b[<u")
    assert vt.kitty_depth == 0


def test_queries_are_recorded_in_order():
    vt = MiniVT(4, 4)
    vt.feed(
        b"\x1b[2;1H\x1b[?u\x1b[?2026$p\x1b[>0q\x1b]11;?\x1b\\\x1b[6n\x1b[3;1H\xe2\x96\x80\x1b[6n\x1b[5n\x1b[c\x1b[0c"
    )
    assert vt.queries == ["kitty", "decrqm:2026", "xtversion", "osc11", "cpr", "cpr", "dsr", "da1", "da1"]
    assert vt.lines()[2] == "\u2580   "


def test_sync_frames_call_the_hook_and_cannot_nest():
    seen = []
    vt = MiniVT(3, 1, on_esu=lambda term: seen.append(term.lines()[0]))
    vt.feed(b"\x1b[?2026h\x1b[1;1Hab\x1b[?2026l\x1b[?2026l")  # a stray ESU (LEAVE sends one) is harmless
    assert seen == ["ab "] and vt.frames == 1 and not vt.sync
    with pytest.raises(VTError, match="BSU"):
        vt.feed(b"\x1b[?2026h\x1b[?2026h")


def test_sgr_model():
    pen = sgr(Pen(), [1, 2, 7, 38, 2, 1, 2, 3, 48, 5, 200])
    assert pen == Pen(True, True, True, ("rgb", 1, 2, 3), ("256", 200))
    assert sgr(pen, [22, 27, 39, 49]) == Pen()
    assert sgr(Pen(), [31, 97, 44, 106]) == Pen(fg=("16", 15), bg=("16", 14))
    assert sgr(pen, [0]) == Pen()
    for bad in ([4], [38, 5], [38, 2, 1, 2], [48, 9, 1], [38, 5, 256]):
        with pytest.raises(VTError):
            sgr(Pen(), bad)


def test_cursor_movement_is_clamped():
    vt = MiniVT(5, 3)
    vt.feed(b"\x1b[9;9Ha\x1b[H\x1b[2Cb\x1b[;4Hc\x1b[2;1H\x1b[Cd\x1b[3H\x1b[99Ce")
    assert vt.lines() == ["  bc ", " d   ", "    e"]


def test_last_column_write_without_autowrap_loses_the_cursor():
    vt = MiniVT(3, 2)
    vt.feed(b"\x1b[?7l\x1b[1;3Hz")
    assert vt.cursor_lost
    for follow in (b"y", b"\x1b[C"):
        with pytest.raises(VTError, match="undefined"):
            MiniVT(3, 2).feed(b"\x1b[?7l\x1b[1;3Hz" + follow)
    vt.feed(b"\rx\x1b[2;3Hq\x1b[1;1H")
    assert vt.lines() == ["x z", "  q"] and not vt.cursor_lost


def test_autowrap_and_scrolling_on_the_main_screen():
    vt = MiniVT(3, 2)
    vt.feed(b"abcdefg\r\nh\tz\bY\a")
    assert vt.lines() == ["g  ", "hYz"]
    vt = MiniVT(4, 2)
    vt.feed(b"one\r\ntwo\r\nsix")
    assert vt.lines() == ["two ", "six "]


def test_ed_needs_the_default_pen():
    vt = MiniVT(3, 1)
    vt.feed(b"abc\x1b[0m\x1b[2J")
    assert vt.lines() == ["   "]
    with pytest.raises(VTError, match="non-default pen"):
        vt.feed(b"\x1b[41m\x1b[2J")


def test_can_aborts_a_sequence_and_resize_keeps_content():
    vt = MiniVT(4, 2)
    vt.feed(b"ab\x1b[3\x18cd\x1b]2;x\x18e")
    assert vt.lines()[0] == "abcd" and vt.title == ""
    vt.resize(2, 3)
    assert vt.lines() == ["ab", "e ", "  "] and (vt.y, vt.x) == (1, 1)
    vt.feed(b"\x1b[?1049h")
    vt.resize(3, 1)
    assert vt.lines() == ["   "]


def test_can_right_after_escape_or_inside_a_character_aborts_it():
    # a flush or a restart cuts the stream anywhere, and the CAN that follows ends what was cut, as in xterm
    vt = MiniVT(4, 1)
    vt.feed(b"a\x1b\x18b" + "\u2580".encode()[:2])
    vt.feed(b"\x18c")
    assert vt.lines() == ["ab\ufffdc"]
    with pytest.raises(VTError, match="invalid UTF-8"):
        vt.feed(b"\xe2\x96d")  # a character cut short with no CAN after it is still the game's fault


@pytest.mark.parametrize(
    "data",
    [
        b"\x1b[4h",  # insert mode
        b"\x1b[?6h",  # origin mode
        b"\x1b[3;5r",  # partial scroll region
        b"\x1b[A",  # CUU is never sent
        b"\x1b[J",  # only ED 2
        b"\x1b[K",
        b"\x1b[4:3m",  # colon sub-parameters
        b"\x1b[1;;2m",  # empty SGR parameter
        b"\x1b[?1h",  # unknown private mode
        b"\x1b[?h",
        b"\x1b[ q",  # cursor style
        b"\x1b[=1u",  # kitty set mode
        b"\x1b[14t",
        b"\x1b]52;c;AAAA\x07",  # clipboard OSC
        b"\x1b]2;title\x1bx",  # OSC not ended by ST
        b"\x1bc",  # RIS
        b"\x1b(0",  # DEC graphics charset
        b"\x1bP>|x\x1b\\",  # DCS
        b"\x1b[\x1b[m",  # ESC inside CSI
        b"\x1b[1\x07m",  # control inside CSI
        b"\x00",
        b"\x7f",
        "\u6f22".encode(),  # wide
        "e\u0301".encode(),  # combining mark
        "\u202e".encode(),  # bidi control
        b"\xff",  # invalid UTF-8
    ],
)
def test_unknown_or_unsafe_input_fails(data):
    with pytest.raises(VTError):
        MiniVT(4, 2).feed(data)
