"""Decoder table tests (spec 6.1 and 6.2); every case also runs split at every byte boundary, plus a fuzz."""

import random
import unittest

from terminal_invaders.term import decode
from terminal_invaders.term.decode import GROUND, MAX_PASTE, MAX_PENDING, MAX_STRING, Decoder
from terminal_invaders.term.keys import ALT, CTRL, SHIFT, SUPER, Key, Reply, is_key_name, kitty_name

NOW = 1.0
SPLIT_LIMIT = 600  # longer inputs are split at sampled boundaries


def decoder(**options):
    settings = dict(esc_timeout=0.05, string_timeout=0.5)
    settings.update(options)
    return Decoder(**settings)


def run(parts, expire=None, **options):
    dec = decoder(**options)
    for part in parts:
        dec.feed(part, NOW)
    if expire is not None:
        dec.expire(expire)
    return dec.events(10**7), dec


def key(name, mods=0, kind="press", base=None, certain=False):
    return Key(name, mods, kind, base, certain, NOW)


def reply(kind, value=None):
    return Reply(kind, value, NOW)


def unknown(raw):
    return Reply("unknown", raw, NOW)


class DecoderCase(unittest.TestCase):
    def check(self, data, expected, expire=None, **options):
        """Whole, split at every byte boundary, and one byte at a time all give the same events and state."""
        whole, dec = run([data], expire, **options)
        self.assertEqual(whole, expected)
        cuts = (
            range(1, len(data)) if len(data) <= SPLIT_LIMIT else random.Random(len(data)).sample(range(len(data)), 40)
        )
        for cut in cuts:
            events, split = run([data[:cut], data[cut:]], expire, **options)
            self.assertEqual(events, expected, f"split at {cut}")
            self.assertEqual(split.state, dec.state)
        if len(data) <= SPLIT_LIMIT:
            events, _ = run([bytes([b]) for b in data], expire, **options)
            self.assertEqual(events, expected, "byte at a time")
        return dec


class GroundTests(DecoderCase):
    def test_control_bytes(self):
        self.check(
            b"\r\n\t\x7f\x08 ",
            [key("enter"), key("enter"), key("tab"), key("backspace"), key("backspace")] + [key("space")],
        )
        self.check(b"\x01\x0c\x1a", [key("a", CTRL), key("l", CTRL), key("z", CTRL)])

    def test_printable_bytes_are_lower_case_with_shift(self):
        self.check(b"aQz1;~", [key("a"), key("q", SHIFT), key("z"), key("1"), key(";"), key("~")])

    def test_utf8_characters(self):
        self.check("é€😀É".encode(), [key("é"), key("€"), key("😀"), key("é", SHIFT)])

    def test_invalid_utf8_and_stray_bytes_are_dropped(self):
        self.check(b"\x80\xbf\xc0\xaf\xc1\xf5\xff\x9b\x85a", [key("a")])
        self.check(b"\xc3a\xe2\x82b", [key("a"), key("b")])  # truncated characters
        self.check(b"\xc0\x80\xe0\x80\x80\xed\xa0\x80c", [key("c")])  # overlong forms and a surrogate
        self.check("\u0085‎ d".encode(), [key("d")])  # decoded but not printable

    def test_unmapped_c0_bytes_are_dropped(self):
        self.check(b"\x00\x1c\x1d\x1e\x1fx", [key("x")])


class EscapeTests(DecoderCase):
    def test_lone_escape_waits_for_the_timeout(self):
        dec = decoder()
        dec.feed(b"\x1b", NOW)
        self.assertEqual(dec.deadline(), NOW + 0.05)
        dec.expire(NOW + 0.049)
        self.assertEqual(dec.events(), [])
        dec.expire(NOW + 0.05)
        self.assertEqual(dec.events(), [key("escape")])  # stamped with the ESC's arrival
        self.assertIsNone(dec.deadline())
        self.check(b"\x1b", [key("escape")], expire=NOW + 0.05)

    def test_remote_timeout_is_longer(self):
        dec = decoder(esc_timeout=0.15)
        dec.feed(b"\x1b", NOW)
        dec.expire(NOW + 0.1)
        self.assertEqual(dec.events(), [])
        dec.expire(NOW + 0.15)
        self.assertEqual(dec.events(), [key("escape")])

    def test_kitty_drops_a_lone_escape(self):
        dec = self.check(b"\x1b", [], expire=NOW + 1, kitty=True)
        self.assertEqual(dec.state, GROUND)
        self.check(b"\x1b[27u", [key("escape", certain=True)], kitty=True)

    def test_escape_escape(self):
        self.check(b"\x1b\x1b", [key("escape"), key("escape")], expire=NOW + 1)
        self.check(b"\x1b\x1b[A", [key("escape"), key("up")])
        self.check(b"\x1b\x1b[A", [key("up", certain=True)], kitty=True)

    def test_alt_keys(self):
        self.check(
            b"\x1ba\x1bA\x1b \x1b\r\x1b\x7f",
            [key("a", ALT), key("a", ALT | SHIFT), key("space", ALT)] + [key("enter", ALT), key("backspace", ALT)],
        )

    def test_escape_before_a_byte_that_is_no_key(self):
        self.check(b"\x1b\xc3\xa9", [key("escape"), key("é")])
        self.check(b"\x1b\x00x", [key("escape"), key("x")])

    def test_a_sequence_arriving_in_time_is_no_escape(self):
        dec = decoder()
        dec.feed(b"\x1b", NOW)
        dec.feed(b"[D", NOW + 0.02)
        dec.expire(NOW + 1)
        self.assertEqual(dec.events(), [Key("left", time=NOW + 0.02)])


class CsiKeyTests(DecoderCase):
    def test_cursor_and_function_keys(self):
        finals = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "P": "f1", "Q": "f2"}
        for final, name in (finals | {"S": "f4"}).items():
            with self.subTest(final=final):
                self.check(f"\x1b[{final}".encode(), [key(name)])
                self.check(f"\x1b[1;5{final}".encode(), [key(name, CTRL)])
                self.check(f"\x1b[1{final}".encode(), [key(name)])
        self.check(b"\x1b[2A", [unknown(b"\x1b[2A")])

    def test_tilde_keys(self):
        names = {1: "home", 2: "insert", 3: "delete", 4: "end", 5: "page_up", 6: "page_down", 7: "home", 8: "end"}
        names |= {11: "f1", 12: "f2", 13: "f3", 14: "f4", 15: "f5", 17: "f6", 18: "f7", 19: "f8", 20: "f9"}
        names |= {21: "f10", 23: "f11", 24: "f12"}
        for code, name in names.items():
            with self.subTest(code=code):
                self.check(f"\x1b[{code}~".encode(), [key(name)])
                self.check(f"\x1b[{code};3~".encode(), [key(name, ALT)])
        for raw in (b"\x1b[16~", b"\x1b[99~", b"\x1b[~", b"\x1b[;5~", b"\x1b[3;5;7~"):
            self.check(raw, [unknown(raw)])

    def test_modify_other_keys(self):
        self.check(b"\x1b[27;5;99~", [key("c", CTRL)])
        self.check(b"\x1b[27;2;65~", [key("a", SHIFT)])
        self.check(b"\x1b[27;5;13~\x1b[27;3;32~", [key("enter", CTRL), key("space", ALT)])
        self.check(b"\x1b[27;5;1~x", [key("x")])  # no key for a C0 code point

    def test_shift_tab(self):
        self.check(b"\x1b[Z", [key("tab", SHIFT)])
        self.check(b"\x1b[1Z", [unknown(b"\x1b[1Z")])

    def test_f3_versus_cpr(self):
        self.check(
            b"\x1b[R\x1b[1;2R\x1b[1;5:3R", [key("f3"), key("f3", SHIFT), key("f3", CTRL, "release", certain=True)]
        )
        self.check(b"\x1b[13~", [key("f3")])
        self.check(b"\x1b[24;80R", [reply("cpr", (24, 80))])  # from row 2 on a CPR, never a key
        self.check(b"\x1b[1;2;3R\x1b[;1R", [unknown(b"\x1b[1;2;3R"), unknown(b"\x1b[;1R")])

    def test_cprs_from_row_two_on(self):
        self.check(
            b"\x1b[2;1R\x1b[3;3R\x1b[24;60R",
            [reply("cpr", (2, 1)), reply("cpr", (3, 3)), reply("cpr", (24, 60))],
        )
        self.check(b"\x1b[1;2R\x1b[2;2R", [key("f3", SHIFT), reply("cpr", (2, 2))])

    def test_on_row_one_a_cpr_in_kitty_mode_or_while_clamped_replies_are_owed(self):
        """A terminal shrunk to one row clamps the CUP of the ack queries it reads after onto row 1, where a reply
        reads like F3 with modifiers (CPRT-1 of the S14 review). In kitty mode F3 is CSI 13 ~, so there it is always a
        CPR; otherwise as many are as `clamped` says are owed, and a CPR on any row is one of those."""
        self.check(b"\x1b[1;25R\x1b[1;5R", [reply("cpr", (1, 25)), reply("cpr", (1, 5))], kitty=True)
        dec = decoder()
        dec.clamped = 2
        dec.feed(b"\x1b[5;9R\x1b[1;25R\x1b[1;2R", NOW)
        self.assertEqual(dec.events(10), [reply("cpr", (5, 9)), reply("cpr", (1, 25)), key("f3", SHIFT)])
        self.assertEqual(dec.clamped, 0)
        dec.feed(b"\x1b[5;9R\x1b[1;1R", NOW)  # none owed: a CPR leaves the count at 0 (DM4 of the S15 review)
        self.assertEqual((dec.events(10), dec.clamped), ([reply("cpr", (5, 9)), key("f3")], 0))

    def test_a_modified_f3_key_is_a_key_while_clamped_replies_are_owed(self):
        """No tag takes columns 2 to 16 (encode.TAG_SKIP), so there CSI 1;m R is F3 with modifiers even while replies
        are owed: S16 read every one as a reply then, which ate a key a custom keymap bound over any link with an RTT
        of a frame or more (LD1 of the S16 review). Columns 1 and 17 on are the replies they are."""
        dec = decoder()
        dec.clamped = 3
        dec.feed(b"\x1b[1;5R\x1b[1;17R\x1b[1;3R\x1b[1;16R\x1b[1;1R\x1b[1;2:1R", NOW)
        self.assertEqual(
            dec.events(10),
            [key("f3", CTRL), reply("cpr", (1, 17)), key("f3", ALT), key("f3", SHIFT | ALT | CTRL | SUPER)]
            + [reply("cpr", (1, 1)), key("f3", SHIFT, "press", certain=True)],
        )
        self.assertEqual(dec.clamped, 1)
        dec.feed(b"\x1b[5;9R\x1b[1;1R", NOW)  # the last reply owed, and then none: column 1 is F3 again
        self.assertEqual((dec.events(10), dec.clamped), ([reply("cpr", (5, 9)), key("f3")], 0))


class KittyTests(DecoderCase):
    def test_event_forms(self):
        self.check(
            b"\x1b[97u\x1b[97;1:2u\x1b[97;1:3u",
            [key("a", certain=True), key("a", 0, "repeat", certain=True), key("a", 0, "release", certain=True)],
            kitty=True,
        )
        self.check(
            b"\x1b[97;1:1u\x1b[97;5u\x1b[97;2u\x1b[97;0u",
            [
                key("a", certain=True),
                key("a", CTRL, certain=True),
                key("a", SHIFT, certain=True),
                key("a", certain=True),
            ],
            kitty=True,
        )
        self.check(b"\x1b[97:65;2u\x1b[97;1;97u", [key("a", SHIFT, certain=True), key("a", certain=True)], kitty=True)
        self.check(b"\x1b[65u", [key("a", SHIFT, certain=True)], kitty=True)

    def test_event_types_make_a_key_certain_outside_kitty_mode(self):
        self.check(
            b"\x1b[97u\x1b[97;1:3u\x1b[A\x1b[1;1:2A",
            [key("a"), key("a", 0, "release", certain=True), key("up")] + [key("up", 0, "repeat", certain=True)],
        )

    def test_legacy_shaped_forms(self):
        expected = [
            key("left", certain=True),
            key("left", 0, "release", certain=True),
            key("right", CTRL, "repeat", certain=True),
        ]
        self.check(b"\x1b[D\x1b[1;1:3D\x1b[1;5:2C", expected, kitty=True)
        expected = [key("f1", certain=True), key("f3", certain=True), key("f5", 0, "release", certain=True)]
        self.check(b"\x1b[P\x1b[13~\x1b[15;1:3~", expected, kitty=True)

    def test_base_layout_key(self):
        self.check(
            b"\x1b[113:81:97;1:2u\x1b[113::97u",
            [key("q", 0, "repeat", "a", True), key("q", base="a", certain=True)],
            kitty=True,
        )
        self.check(
            b"\x1b[122::119u\x1b[97::57441u", [key("z", base="w", certain=True), key("a", certain=True)], kitty=True
        )

    def test_signal_keys_with_lock_bits(self):
        expected = [key("c", CTRL, certain=True), key("z", CTRL, certain=True), key("\\", CTRL, certain=True)]
        self.check(b"\x1b[99;5u\x1b[122;5u\x1b[92;5u", expected, kitty=True)
        self.check(b"\x1b[99;69u\x1b[122;133u\x1b[92;197u", expected, kitty=True)  # caps, num, both

    def test_functional_codes(self):
        names = {9: "tab", 13: "enter", 27: "escape", 127: "backspace", 32: "space", 57362: "pause"}
        names |= {57376: "f13", 57398: "f35", 57425: "insert", 57426: "delete"}
        for code, name in names.items():
            self.check(f"\x1b[{code}u".encode(), [key(name, certain=True)], kitty=True)

    def test_keypad_keys_are_normalised(self):
        names = [str(n) for n in range(10)] + [".", "/", "*", "-", "+", "enter", "=", ","]
        names += ["left", "right", "up", "down", "page_up", "page_down", "home", "end", "insert", "delete"]
        for code, name in zip(range(57399, 57427), names):
            with self.subTest(code=code):
                self.check(f"\x1b[{code}u".encode(), [key(name, certain=True)], kitty=True)
                self.check(f"\x1b[{code};129u".encode(), [key(name, certain=True)], kitty=True)  # num lock

    def test_modifier_lock_media_and_private_codes_are_dropped(self):
        dropped = [57358, 57359, 57360, 57361, 57363, 57427, *range(57428, 57455), 57344, 57357, 57455, 63743]
        dropped += [0x10FFFF, 0x110000, 10**9, 1, 31, 0x85]
        for code in dropped:
            with self.subTest(code=code):
                self.check(f"\x1b[{code}u\x1b[{code};1:3ux".encode(), [key("x")])

    def test_malformed_kitty_events(self):
        for raw in (b"\x1b[u", b"\x1b[97;1:4u", b"\x1b[97;1;97;1u", b"\x1b[9?7u", b"\x1b[;5u", b"\x1b[97;5:9u"):
            with self.subTest(raw=raw):
                self.check(raw, [unknown(raw)], kitty=True)


class ReplyTests(DecoderCase):
    def test_kitty_flags_da_and_dsr(self):
        self.check(b"\x1b[?15u\x1b[?0u", [reply("kitty_flags", 15), reply("kitty_flags", 0)])
        self.check(
            b"\x1b[?62;22;52c\x1b[?1;2c\x1b[>1;4000;29c",
            [reply("da1", (62, 22, 52)), reply("da1", (1, 2))] + [reply("da2", (1, 4000, 29))],
        )
        self.check(b"\x1b[0n\x1b[3n\x1b[5n", [reply("dsr", 0), reply("dsr", 3), unknown(b"\x1b[5n")])
        self.check(b"\x1b[?u", [unknown(b"\x1b[?u")])

    def test_decrpm_values(self):
        for ps in range(5):
            self.check(f"\x1b[?2026;{ps}$y".encode(), [reply("decrpm", (2026, ps))])
        self.check(b"\x1b[?1004;2$y", [reply("decrpm", (1004, 2))])
        self.check(b"\x1b[2026;2$y\x1b[?2026$y", [unknown(b"\x1b[2026;2$y"), unknown(b"\x1b[?2026$y")])

    def test_focus(self):
        self.check(b"\x1b[I\x1b[O", [reply("focus", True), reply("focus", False)])
        self.check(b"\x1b[1I", [unknown(b"\x1b[1I")])

    def test_unknown_sequences_keep_their_bytes(self):
        for raw in (b"\x1b[5x", b"\x1b[?5x", b"\x1b[>5u", b"\x1b[2 q", b"\x1b[1$2A", b"\x1b[=1;2c", b"\x1b[1?2c"):
            with self.subTest(raw=raw):
                self.check(raw, [unknown(raw)])


class CsiStructureTests(DecoderCase):
    def test_can_and_sub_abort(self):
        self.check(b"\x1b[1;5\x18A\x1b[\x1aB", [key("a", SHIFT), key("b", SHIFT)])

    def test_escape_restarts_a_sequence(self):
        self.check(b"\x1b[1;\x1b[A", [key("up")])

    def test_other_bytes_inside_csi_are_ignored(self):
        self.check(b"\x1b[1;\x00\x07\x7f\xc35A", [key("up", CTRL)])

    def test_overlong_parameters_are_swallowed(self):
        self.check(b"\x1b[" + b"1;" * 64 + b"Ax", [unknown(b"\x1b[" + b"1;" * 64 + b"A"), key("x")])
        self.check(b"\x1b[" + b"1" * 129 + b"Ax", [key("x")])
        self.check(b"\x1b[" + b"1" * 200 + b"\x1b[Bx", [key("down"), key("x")])
        self.check(b"\x1b[" + b"1" * 200 + b"\x18x", [key("x")])

    def test_pending_bytes_are_capped(self):
        self.check(b"\x1b[" + b"\x00" * 1100 + b"x", [key("x")])  # without the cap: one unknown CSI ending in x
        overflow = 1100 - MAX_PENDING - 1  # bytes after the one that overflows decode afresh
        self.check(b"\x1b[" + b"1" * 1100 + b"x", [key("1")] * overflow + [key("x")])

    def test_a_stalled_sequence_times_out(self):
        dec = decoder()
        dec.feed(b"\x1b[1;5", NOW)
        self.assertEqual(dec.deadline(), NOW + 0.5)
        dec.expire(NOW + 0.5)
        dec.feed(b"Ax", NOW + 0.6)
        self.assertEqual(dec.events(), [Key("a", SHIFT, time=NOW + 0.6), Key("x", time=NOW + 0.6)])
        self.check(b"\x1bO", [], expire=NOW + 0.5)


class Ss3Tests(DecoderCase):
    def test_ss3_keys(self):
        names = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "P": "f1", "Q": "f2"}
        for final, name in (names | {"R": "f3", "S": "f4", "M": "enter"}).items():
            self.check(f"\x1bO{final}".encode(), [key(name)])

    def test_other_finals_are_dropped(self):
        self.check(b"\x1bOx\x1bO\x18z", [key("z")])

    def test_a_modifier_parameter_modifies_the_key(self):
        """DEC-ss3-param-letter of the S17 review: some terminals send modified F1-F4 as SS3 m P..S. The parameter was
        dropped and its final read as text: ESC O 2 R a Shift+R, which the pause menu takes for restart."""
        self.check(
            b"\x1bO2R\x1bO5Q\x1bO1P\x1bO3A\x1bO10R",
            [key("f3", SHIFT), key("f2", CTRL), key("f1"), key("up", ALT), key("f3", SHIFT | SUPER)],
        )

    def test_with_a_parameter_any_other_final_drops_the_whole_sequence(self):
        self.check(b"\x1bO5y\x1bO2\x18z\x1bO1234R\x1bO2\x1b[A", [key("z"), key("up")])

    def test_escape_aborts(self):
        self.check(b"\x1bO\x1b[A", [key("up")])


class ConsoleTests(DecoderCase):
    def test_linux_console_f1_to_f5(self):
        for final, name in zip("ABCDE", ("f1", "f2", "f3", "f4", "f5")):
            self.check(f"\x1b[[{final}".encode(), [key(name)])

    def test_other_finals_are_dropped_and_never_a_letter(self):
        self.check(b"\x1b[[Zx\x1b[[1y\x1b[[\x1b[A", [key("x"), key("y"), key("up")])
        self.check(b"\x1b[[", [], expire=NOW + 0.5)


class StringTests(DecoderCase):
    def test_osc11_with_st_and_bel(self):
        self.check(b"\x1b]11;rgb:ffff/ffff/dddd\x1b\\", [reply("osc11", (255, 255, 221))])
        self.check(b"\x1b]11;rgb:0000/8080/ffff\x07", [reply("osc11", (0, 128, 255))])

    def test_osc11_channel_widths(self):
        self.check(b"\x1b]11;rgb:f/8/0\x07", [reply("osc11", (255, 136, 0))])
        self.check(b"\x1b]11;rgb:ff/80/00\x07", [reply("osc11", (255, 128, 0))])
        self.check(b"\x1b]11;rgb:fff/800/000\x07", [reply("osc11", (255, 128, 0))])
        self.check(b"\x1b]11;rgb:1c1c/1c1c/1c1c\x07", [reply("osc11", (28, 28, 28))])

    def test_other_strings_are_unknown(self):
        cases = {
            b"\x1b]11;rgb:zz/00/00\x07": b"\x1b]11;rgb:zz/00/00",
            b"\x1b]11;rgb:00000/0/0\x07": b"\x1b]11;rgb:00000/0/0",
            b"\x1b]11;rgb:0/0\x07": b"\x1b]11;rgb:0/0",
            b"\x1b]11;#ffffff\x07": b"\x1b]11;#ffffff",
            b"\x1b]10;rgb:0/0/0\x07": b"\x1b]10;rgb:0/0/0",
            b"\x1bP1$r0m\x1b\\": b"\x1bP1$r0m",
            b"\x1b_Gi=1\x1b\\": b"\x1b_Gi=1",
            b"\x1b^pm\x1b\\": b"\x1b^pm",
            b"\x1bXsos\x1b\\": b"\x1bXsos",
        }
        for data, raw in cases.items():
            with self.subTest(data=data):
                self.check(data, [unknown(raw)])

    def test_xtversion(self):
        self.check(b"\x1bP>|kitty(0.39.1)\x1b\\", [reply("xtversion", "kitty(0.39.1)")])
        self.check("\x1bP>|tërm 1\x07\x1b\\".encode(), [reply("xtversion", "tërm 1\x07")])  # BEL ends only OSC

    def test_escape_that_is_not_st_aborts_the_string(self):
        self.check(b"\x1b]11;rgb:0/0/0\x1b[A", [key("up")])
        self.check(b"\x1bP>|x\x1b\x1b[B", [key("down")])

    def test_overflow_is_swallowed(self):
        self.check(b"\x1b]11;" + b"0" * MAX_STRING + b"\x07x", [key("x")])
        self.check(b"\x1bP>|" + b"a" * (MAX_STRING - 2) + b"\x1b\\", [reply("xtversion", "a" * (MAX_STRING - 2))])
        dec = decoder()
        dec.feed(b"\x1bP" + b"a" * 10 * MAX_STRING, NOW)
        self.assertLessEqual(len(dec.buf), MAX_STRING)

    def test_timeout(self):
        dec = decoder()
        dec.feed(b"\x1b]11;rgb:0/0", NOW)
        self.assertEqual(dec.deadline(), NOW + 0.5)
        dec.expire(NOW + 0.49)
        self.assertEqual(dec.state, decode.STRING)
        dec.expire(NOW + 0.5)
        dec.feed(b"/0q", NOW + 0.6)
        self.assertEqual(dec.events(), [Key("/", time=NOW + 0.6), Key("0", time=NOW + 0.6), Key("q", time=NOW + 0.6)])
        self.check(b"\x1bPabc\x1b", [], expire=NOW + 0.5)


class PasteTests(DecoderCase):
    def test_paste_is_counted_and_discarded(self):
        self.check(b"\x1b[200~qqqq\x1b[201~p", [reply("paste", 4), key("p")])
        self.check(b"\x1b[200~\x1b[201~", [reply("paste", 0)])

    def test_sequences_and_broken_end_markers_are_content(self):
        content = b"\x1b[A\x1b[201x\x1b[20\x1b\x1b[2"
        self.check(b"\x1b[200~" + content + b"\x1b[201~", [reply("paste", len(content))])

    def test_paste_has_no_deadline(self):
        dec = decoder()
        dec.feed(b"\x1b[200~abc", NOW)
        self.assertIsNone(dec.deadline())
        dec.expire(NOW + 100)
        dec.feed(b"\x1b[201~", NOW + 100)
        self.assertEqual(dec.events(), [Reply("paste", 3, NOW + 100)])

    def test_one_mebibyte(self):
        self.check(b"\x1b[200~" + b"q" * MAX_PASTE + b"\x1b[201~x", [reply("paste", MAX_PASTE), key("x")])

    def test_a_paste_over_the_cap_ends_there(self):
        self.check(b"\x1b[200~" + b"q" * (MAX_PASTE + 1) + b"ab", [reply("paste", MAX_PASTE), key("a"), key("b")])
        broken = b"\x1b[200~" + b"q" * (MAX_PASTE - 2) + b"\x1b[2xc"  # the broken marker crosses the cap
        self.check(broken, [reply("paste", MAX_PASTE), key("x"), key("c")])

    def test_end_marker_outside_a_paste(self):
        self.check(b"\x1b[201~", [unknown(b"\x1b[201~")])


class DowngradeTests(DecoderCase):
    def test_bare_printable_in_kitty_mode(self):
        dec = self.check(b"\x1b[97uqa", [key("a", certain=True), reply("downgrade"), key("q"), key("a")], kitty=True)
        self.assertFalse(dec.kitty)
        self.check(b" ", [reply("downgrade"), key("space")], kitty=True)
        self.check("é".encode(), [reply("downgrade"), key("é")], kitty=True)

    def test_a_kitty_key_split_after_its_escape_is_no_downgrade(self):
        dec = decoder(kitty=True)
        dec.feed(b"\x1b", NOW)
        self.assertEqual(dec.deadline(), NOW + 0.5)  # the string timeout: esc_timeout is the legacy Escape's
        dec.expire(NOW + 0.12)  # past esc_timeout: a slow link delivered the rest late
        dec.feed(b"[100u", NOW + 0.12)
        self.assertEqual([event.name for event in dec.events()], ["d"])
        self.assertTrue(dec.kitty)

    def test_escape_decodes_as_legacy_after_a_downgrade(self):
        self.check(b"q\x1b", [reply("downgrade"), key("q"), key("escape")], expire=NOW + 1, kitty=True)

    def test_control_bytes_and_sequences_do_not_downgrade(self):
        dec = self.check(
            b"\r\x1b[A\x1b[1;5C", [key("enter"), key("up", certain=True), key("right", CTRL, certain=True)], kitty=True
        )
        self.assertTrue(dec.kitty)


class QueueTests(unittest.TestCase):
    def test_events_are_bounded_per_call_and_none_is_lost(self):
        dec = decoder()
        dec.feed(b"f" * 32 + b"pq", NOW)
        first, second, third = dec.events(), dec.events(), dec.events()
        self.assertEqual(len(first), 34)
        self.assertEqual((second, third), ([], []))
        dec.feed(b"abc" * 30, NOW)
        batches = [dec.events(64), dec.events(64), dec.events(64)]
        self.assertEqual([len(batch) for batch in batches], [64, 26, 0])
        self.assertEqual("".join(k.name for batch in batches for k in batch), "abc" * 30)

    def test_keys_module_helpers(self):
        self.assertTrue(all(is_key_name(name) for name in ("a", "\\", "é", "space", "f35", "page_up", "pause")))
        self.assertFalse(any(is_key_name(name) for name in (" ", "", "f36", "ab", "\x01", "Space")))
        self.assertEqual((kitty_name(97), kitty_name(57399), kitty_name(57441)), ("a", "0", None))


FRAGMENTS = (
    b"\x1b[A",
    b"\x1b[1;5C",
    b"\x1b[97;1:3u",
    b"\x1b[113:81:97;1:2u",
    b"\x1b[99;69u",
    b"\x1bOP",
    b"\x1b[13~",
    b"\x1b[27;5;99~",
    b"\x1b[200~",
    b"\x1b[201~",
    b"\x1b]11;rgb:ffff/0000/8888\x07",
    b"\x1b]11;rgb:ff/00/88\x1b\\",
    b"\x1bP>|kitty(0.39)\x1b\\",
    b"\x1b[?2026;2$y",
    b"\x1b[3;2R",
    b"\x1b[1;2R",
    b"\x1b[0n",
    b"\x1b[?62;22c",
    b"\x1b[?15u",
    b"\x1b[I",
    b"\x1b[57441u",
    b"\x1b",
    b"\x1b[",
    b"\x1b]",
    b"\x1bP",
    b"\x1bO",
    b"\x1b_",
    b"\x18",
    b"\xc3",
    b"\xe2\x82",
    "é".encode(),
    b"q",
    b" ",
)
NOISE = b"0123456789;:?$<>[]O\x1b\x07\\~uABCRZcnyI\x18\x1a\x00\x7f\x80\x9b\xc3\xff"


def blob(rng):
    parts = []
    for _ in range(rng.randrange(1, 8)):
        roll = rng.random()
        if roll < 0.5:
            parts.append(rng.choice(FRAGMENTS))
        elif roll < 0.75:
            parts.append(rng.randbytes(rng.randrange(1, 12)))
        elif roll < 0.97:
            parts.append(bytes(rng.choice(NOISE) for _ in range(rng.randrange(1, 10))))
        else:
            parts.append(b"\x1b[" + b"1" * rng.randrange(100, 1200))
    return b"".join(parts)


class FuzzTests(unittest.TestCase):
    def assert_bounded(self, dec):
        self.assertLessEqual(len(dec.buf), MAX_STRING)
        self.assertLessEqual(len(dec.utf8), 3)
        self.assertLessEqual(dec.pending, MAX_PENDING)
        self.assertLessEqual(dec.pasted, MAX_PASTE)

    def test_random_blobs(self):
        rng = random.Random(20261014)
        now, whole, split = 0.0, None, None
        for n in range(20000):
            if n % 500 == 0:
                options = dict(kitty=rng.random() < 0.5)
                whole, split = decoder(**options), decoder(**options)
            data, now = blob(rng), now + rng.random() * 0.2
            cut = rng.randrange(len(data) + 1)
            whole.feed(data, now)
            split.feed(data[:cut], now)
            self.assert_bounded(split)
            split.feed(data[cut:], now)
            later = now + rng.random() * 0.3
            for dec in (whole, split):
                self.assert_bounded(dec)
                dec.expire(later)
            self.assertEqual(whole.events(10**6), split.events(10**6), data)
            self.assertEqual(whole.state, split.state)
            if n % 20 == 19:  # ending any paste, sequence or string returns the decoder to GROUND
                for dec in (whole, split):
                    dec.feed(b"\x1b[201~", now)
                    dec.expire(now + 10)
                    self.assertEqual(dec.state, GROUND)
                    self.assertEqual(len(dec.utf8), 0)
                    dec.events(10**6)
                    dec.feed(b"x", now + 10)
                    self.assertEqual(dec.events(10**6)[-1], Key("x", time=now + 10))
