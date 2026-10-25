"""resolve() rules (spec 5.5 to 5.7), environment hints (5.1) and the synthetic terminal fixtures (5.8)."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from terminal_invaders.app import fixture
from terminal_invaders.input import make_controls
from terminal_invaders.term.caps import (
    TIERS,
    TITLE_STACK_FAMILIES,
    EnvHints,
    Overrides,
    ProbeReplies,
    resolve,
    user_env,
    xtversion,
)
from tests.support import terminals
from tests.support.terminals import FIXTURES, capabilities, load, names, parse_replies

FAMILIES = {
    "kitty",
    "ghostty",
    "iterm2-3.5.6",
    "iterm2-3.4",
    "wezterm-default",
    "wezterm-kitty",
    "foot",
    "alacritty-0.13",
    "konsole-26.08",
    "konsole-old",
    "vte-gnome",
    "apple-terminal-15",
    "apple-terminal-26",
    "tmux-3.6-in-ghostty",
    "screen",
    "xterm",
    "linux-console",
    "windows-terminal-preview-1.25",
    "ssh-iterm2",
    "ssh-apple-terminal",
    "mosh",
    "silent",
    "dumb",
    "term-unset",
}
ENV_VALUES = {"TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "LC_TERMINAL", "VTE_VERSION"}
ENV_VALUES |= {"KONSOLE_VERSION", "LANG", "LC_ALL", "LC_CTYPE", "NO_COLOR", "COLORFGBG"}
ENV_PRESENCE = {"WT_SESSION", "TMUX", "STY", "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"}
ANSWERED = ProbeReplies(da1=(62,), rtt=0.001)  # a terminal that answers only the DA1 sentinel


def caps(env=None, replies=ANSWERED, overrides=Overrides(), **options):
    return resolve(EnvHints.from_env(env or {}), replies, overrides, **options)


class FixtureTests(unittest.TestCase):
    def test_every_family_of_the_spec_has_a_fixture(self):
        self.assertEqual(set(names()), FAMILIES)

    def test_fixtures_have_the_documented_shape_and_record_only_whitelisted_env(self):
        for name in sorted(FAMILIES):
            with self.subTest(name):
                self.check_shape(name, load(name))

    def test_fixtures_answer_only_what_the_probe_sends(self):
        for name in sorted(FAMILIES):
            with self.subTest(name):
                self.check_answers(load(name))

    def test_resolve_meets_every_fixture_expectation(self):
        for name in sorted(FAMILIES):
            with self.subTest(name):
                self.check_resolves(load(name))

    def test_a_capture_committed_in_place_of_a_fixture_passes_the_fixture_checks(self):
        with TemporaryDirectory() as directory, patch.object(terminals, "FIXTURES", Path(directory)):
            for name in sorted(FAMILIES):
                doc = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
                push = None if doc["push_reply"] is None else doc["push_reply"].encode()
                capture = fixture(capabilities(doc), doc["replies"].encode(), push, doc["env"])  # --probe --raw
                (Path(directory) / f"{name}.json").write_text(json.dumps(capture, indent=2), encoding="utf-8")
            self.assertEqual(set(names()), FAMILIES)
            for name in sorted(FAMILIES):
                with self.subTest(name):
                    captured = load(name)
                    self.assertTrue(captured["provenance"].startswith("captured: "))
                    self.check_shape(name, captured)
                    self.check_answers(captured)
                    self.check_resolves(captured)

    def check_shape(self, name: str, doc: dict) -> None:
        self.assertEqual(set(doc), {"family", "provenance", "env", "replies", "push_reply", "echo", "keys", "expect"})
        synthetic = doc["provenance"].startswith("synthetic: ")
        self.assertTrue(synthetic or doc["provenance"].startswith("captured: "), doc["provenance"])
        # a synthetic fixture is named by its family; a capture records the family the probe resolved
        self.assertEqual(doc["family"], name if synthetic else doc["expect"]["family"])
        self.assertLessEqual(set(doc["env"]), ENV_VALUES | ENV_PRESENCE)
        for var, value in doc["env"].items():  # presence only, or short tokens: no hosts, users or paths
            if var in ENV_PRESENCE:
                self.assertEqual(value, "1")
            else:
                self.assertRegex(value, r"^[\w.;:+-]{1,40}$")
        self.assertIn(doc["keys"]["protocol"], ("kitty", "legacy"))
        self.assertGreater(doc["keys"]["os_repeat"]["delay"], doc["keys"]["os_repeat"]["interval"])

    def check_answers(self, doc: dict) -> None:
        replies = parse_replies(doc["replies"])
        # push_kitty writes only after a flags reply; the Apple and Linux skips follow 5.2
        self.assertEqual(doc["push_reply"] is not None, replies.kitty_flags is not None)
        if doc["env"].get("TERM_PROGRAM") == "Apple_Terminal":
            self.assertEqual((replies.kitty_flags, replies.decrpm_2026, replies.xtversion), (None, None, None))
        if doc["env"].get("TERM") == "linux":
            self.assertEqual((replies.decrpm_2026, replies.xtversion, replies.osc11), (None, None, None))
        self.assertEqual(doc["echo"], replies.cpr_echo is not None and replies.cpr_echo[1] > 1)

    def check_resolves(self, doc: dict) -> None:
        result = capabilities(doc)
        scheme = make_controls(result).scheme
        got = {key: scheme if key == "scheme" else getattr(result, key) for key in doc["expect"]}
        self.assertEqual(got, doc["expect"])
        self.assertEqual(doc["keys"]["protocol"] == "kitty", result.release_events)

    def test_reply_reader_scales_colour_channels_and_orders_the_two_cprs(self):
        replies = parse_replies("\x1b]11;rgb:f/80/ffff\x07\x1b[2;3R\x1b[3;2R\x1b[?62;c")
        self.assertEqual(replies.osc11, (255, 128, 255))
        self.assertEqual((replies.cpr_echo, replies.cpr_width, replies.da1), ((2, 3), (3, 2), (62,)))
        self.assertIsNone(parse_replies("").rtt)

    def test_fixture_replies_take_cprs_only_on_the_probe_s_rows(self):
        terminals.strict(b"\x1b[2;1R\x1b[3;2R")
        with self.assertRaisesRegex(AssertionError, "does not route"):
            terminals.strict(b"\x1b[2;1R\x1b[4;2R")  # a tag's row: an ack, which the probe leaves alone


class EnvHintTests(unittest.TestCase):
    def test_presence_flags(self):
        for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
            self.assertTrue(EnvHints.from_env({var: ""}).ssh)
        self.assertFalse(EnvHints.from_env({}).ssh)
        self.assertTrue(EnvHints.from_env({"TMUX": "/tmp/tmux-1/default,1,0"}).tmux)
        self.assertTrue(EnvHints.from_env({"TERM": "tmux-256color"}).tmux)
        self.assertTrue(EnvHints.from_env({"STY": "1.pts-0"}).sty)
        self.assertTrue(EnvHints.from_env({"TERM": "screen.xterm-256color"}).sty)
        self.assertTrue(EnvHints.from_env({"WT_SESSION": "1"}).wt_session)
        self.assertEqual(EnvHints.from_env({}), EnvHints())

    def test_no_color_needs_a_non_empty_value(self):
        self.assertTrue(EnvHints.from_env({"NO_COLOR": "1"}).no_color)
        self.assertFalse(EnvHints.from_env({"NO_COLOR": ""}).no_color)

    def test_utf8_locale_takes_the_first_non_empty_variable(self):
        cases = [
            ({"LANG": "en_US.UTF-8"}, True),
            ({"LC_CTYPE": "C.utf8"}, True),
            ({"LC_ALL": "", "LC_CTYPE": "de_DE.ISO-8859-1", "LANG": "en_US.UTF-8"}, False),
            ({"LC_ALL": "en_GB.UTF-8", "LC_CTYPE": "ja_JP.eucJP"}, True),
            ({"LANG": "ja_JP.eucJP@modifier"}, False),
            ({"LANG": "C"}, None),
            ({"LANG": "POSIX"}, None),
            ({"LANG": "en_US"}, None),
            ({"LANG": "sr_RS@latin"}, None),
            ({}, None),
        ]
        for env, expected in cases:
            with self.subTest(env):
                self.assertIs(EnvHints.from_env(env).utf8_locale, expected)

    def test_the_lc_ctype_python_coerced_is_not_the_users(self):
        latin1 = {"LANG": "en_US.ISO-8859-1", "TERM": "xterm"}
        coerced, chosen = latin1 | {"LC_CTYPE": "C.UTF-8"}, latin1 | {"LC_CTYPE": "en_US.UTF-8"}
        cases = [  # (environment, UTF-8 mode, the environment as the user set it)
            (coerced, True, latin1),  # en_US.ISO-8859-1 is not installed: CPython coerced it (PEP 538 and 540)
            ({"LC_CTYPE": "C.utf8"}, True, {}),  # an unset locale
            ({"LC_CTYPE": "UTF-8"}, True, {}),  # macOS has no C.UTF-8
            (coerced, False, coerced),  # the user's own LC_CTYPE: no UTF-8 mode
            (chosen, True, chosen),  # not one CPython sets
            ({"LC_ALL": "C", "LC_CTYPE": "C.UTF-8"}, True, {"LC_ALL": "C", "LC_CTYPE": "C.UTF-8"}),  # no coercion then
        ]
        for env, utf8_mode, expected in cases:
            with self.subTest(env=env, utf8_mode=utf8_mode):
                self.assertEqual(user_env(env, utf8_mode), expected)
        self.assertIs(EnvHints.from_env(user_env(coerced, True)).utf8_locale, False)


class FamilyTests(unittest.TestCase):
    def test_xtversion_names(self):
        cases = [
            ("kitty(0.43.1)", "kitty", "0.43.1"),
            ("ghostty 1.2.0", "ghostty", "1.2.0"),
            ("iTerm2 3.5.6", "iterm2", "3.5.6"),
            ("WezTerm 20240203-110809-5046fc22", "wezterm", "20240203-110809-5046fc22"),
            ("foot(1.20.2)", "foot", "1.20.2"),
            ("XTerm(390)", "xterm", "390"),
            ("tmux 3.6a", "tmux", "3.6a"),
            ("Konsole 26.08.0", "konsole", "26.08.0"),
            ("alacritty 0.16.0", "alacritty", "0.16.0"),
            ("contour 0.6.1", "contour", "0.6.1"),
            ("VTE(7800)", "vte", "7800"),
            (" kitty", "kitty", ""),
        ]
        for text, family, version in cases:
            with self.subTest(text):
                self.assertEqual(xtversion(text), (family, version))
                result = caps({"TERM_PROGRAM": "Apple_Terminal"}, ProbeReplies(xtversion=text, da1=(1,)))
                self.assertEqual((result.family, result.version), (family, version))  # rule 1 beats TERM_PROGRAM
        for text in (None, "", "?", "mlterm 3.9.3", "(1.0)"):
            self.assertIsNone(xtversion(text))

    def test_env_rules_in_order(self):
        cases = [
            ({"TERM_PROGRAM": "Apple_Terminal", "TERM_PROGRAM_VERSION": "455"}, "apple_terminal", "455"),
            ({"TERM_PROGRAM": "iTerm.app", "LC_TERMINAL": "iTerm2", "TERM_PROGRAM_VERSION": "3.6"}, "iterm2", "3.6"),
            ({"TERM_PROGRAM": "WezTerm"}, "wezterm", ""),
            ({"TERM_PROGRAM": "ghostty", "TERM": "xterm-kitty"}, "ghostty", ""),
            ({"TERM_PROGRAM": "vscode", "TERM_PROGRAM_VERSION": "1.105.0"}, "vscode", "1.105.0"),
            ({"TERM_PROGRAM": "tmux", "TERM_PROGRAM_VERSION": "3.6a"}, "tmux", "3.6a"),
            ({"TERM_PROGRAM": "Hyper", "LC_TERMINAL": "iTerm2", "VTE_VERSION": "7800"}, "iterm2", ""),
            ({"VTE_VERSION": "7800", "KONSOLE_VERSION": "260800"}, "vte", "7800"),
            ({"KONSOLE_VERSION": "260800", "WT_SESSION": "1"}, "konsole", "260800"),
            ({"WT_SESSION": "1", "TERM": "xterm-kitty"}, "windows_terminal", ""),
            ({"TERM": "xterm-kitty"}, "kitty", ""),
            ({"TERM": "xterm-ghostty"}, "ghostty", ""),
            ({"TERM": "foot-extra"}, "foot", ""),
            ({"TERM": "alacritty"}, "alacritty", ""),
            ({"TERM": "wezterm"}, "wezterm", ""),
            ({"TERM": "linux"}, "linux", ""),
            ({"TERM": "tmux-256color"}, "tmux", ""),
            ({"TERM": "screen.xterm-256color"}, "screen", ""),
            ({"TERM": "xterm-256color"}, "unknown", ""),
            ({}, "unknown", ""),
        ]
        for env, family, version in cases:
            with self.subTest(env):
                result = caps(env)
                self.assertEqual((result.family, result.version), (family, version))

    def test_multiplexer(self):
        self.assertEqual(caps({"TMUX": "1"}).multiplexer, "tmux")
        self.assertEqual(caps({"TERM": "tmux-256color"}).multiplexer, "tmux")
        self.assertEqual(caps({}, ProbeReplies(xtversion="tmux 3.6a", da1=(1,))).multiplexer, "tmux")
        self.assertEqual(caps({"STY": "1"}).multiplexer, "screen")
        self.assertEqual(caps({"TERM": "screen"}).multiplexer, "screen")
        self.assertEqual(caps({"STY": "1", "TMUX": "1"}).multiplexer, "tmux")
        self.assertIsNone(caps({"TERM": "xterm-256color"}).multiplexer)

    def test_title_stack_families(self):
        expected = {"kitty", "ghostty", "iterm2", "wezterm", "foot", "xterm", "vte", "konsole", "alacritty"}
        self.assertEqual(TITLE_STACK_FAMILIES, expected)


class ColorTierTests(unittest.TestCase):
    """One test per rule of spec 5.6, each shadowing the rules after it."""

    def color(self, env, override="auto", replies=ANSWERED):
        return caps(env, replies, Overrides(color=override)).color

    def test_rule_1_explicit_tier_beats_everything_including_no_color(self):
        for tier in TIERS:
            self.assertEqual(self.color({"NO_COLOR": "1", "COLORTERM": "truecolor", "TERM": "dumb"}, tier), tier)

    def test_rule_2_mono_flag_or_no_color(self):
        self.assertEqual(self.color({"NO_COLOR": "1", "COLORTERM": "truecolor"}), "mono")
        self.assertEqual(self.color({"COLORTERM": "truecolor"}, "mono"), "mono")  # cli maps --mono to color=mono
        self.assertEqual(self.color({"NO_COLOR": "", "COLORTERM": "truecolor"}), "truecolor")

    def test_rule_3_colorterm(self):
        for value in ("truecolor", "24bit", "TrueColor"):
            self.assertEqual(self.color({"COLORTERM": value, "TERM": "xterm"}), "truecolor")
        self.assertEqual(self.color({"COLORTERM": "yes", "TERM": "xterm"}), "16")

    def test_rule_4_xtversion_family(self):
        for text in ("kitty(1)", "ghostty 1", "iTerm2 3", "WezTerm 1", "foot(1)", "VTE(1)", "Konsole 1", "tmux 3"):
            replies = ProbeReplies(xtversion=text, da1=(1,))
            self.assertEqual(self.color({"TERM": "xterm"}, replies=replies), "truecolor", text)
        for text in ("alacritty 1", "contour 1"):
            self.assertEqual(self.color({"TERM": "xterm"}, replies=ProbeReplies(xtversion=text, da1=(1,))), "truecolor")
        self.assertEqual(self.color({"TERM": "xterm"}, replies=ProbeReplies(xtversion="XTerm(390)", da1=(1,))), "16")

    def test_rule_5_iterm2_wezterm_ghostty_hints(self):
        self.assertEqual(self.color({"LC_TERMINAL": "iTerm2", "TERM": "xterm"}), "truecolor")
        for program in ("iTerm.app", "WezTerm", "ghostty"):
            self.assertEqual(self.color({"TERM_PROGRAM": program, "TERM": "xterm"}), "truecolor")

    def test_rule_6_apple_terminal_by_build(self):
        apple = {"TERM_PROGRAM": "Apple_Terminal", "TERM": "xterm-direct"}
        self.assertEqual(self.color({**apple, "TERM_PROGRAM_VERSION": "470"}), "truecolor")
        self.assertEqual(self.color({**apple, "TERM_PROGRAM_VERSION": "471.2"}), "truecolor")
        self.assertEqual(self.color({**apple, "TERM_PROGRAM_VERSION": "469"}), "256")
        self.assertEqual(self.color({**apple, "TERM_PROGRAM_VERSION": "455"}), "256")
        self.assertEqual(self.color(apple), "256")

    def test_rule_7_direct_terms(self):
        for term in ("xterm-direct", "st-direct", "xterm-kitty", "xterm-ghostty", "alacritty", "foot", "wezterm"):
            self.assertEqual(self.color({"TERM": term}), "truecolor", term)

    def test_rule_8_256_in_term(self):
        for term in ("xterm-256color", "screen-256color", "rxvt-unicode-256color", "tmux-256color"):
            self.assertEqual(self.color({"TERM": term}), "256", term)

    def test_rule_9_sixteen_colour_terms(self):
        for term in ("linux", "screen", "screen.xterm-new", "rxvt-unicode", "xterm", "xterm-color", "vt220", "ansi"):
            self.assertEqual(self.color({"TERM": term}), "16", term)

    def test_rule_10_vt100_and_dumb(self):
        self.assertEqual(self.color({"TERM": "vt100"}), "mono")
        self.assertEqual(self.color({"TERM": "dumb"}), "mono")

    def test_rule_11_otherwise_256_including_unset_term(self):
        self.assertEqual(self.color({}), "256")
        self.assertEqual(self.color({"TERM": "st"}), "256")
        self.assertEqual(self.color({"TERM": "dumb"}, "bogus"), "mono")  # an unknown override is auto


class ReplyRuleTests(unittest.TestCase):
    def test_remote_from_ssh_or_a_slow_probe(self):
        self.assertTrue(caps({"SSH_TTY": "1"}).remote)
        self.assertTrue(caps({}, ProbeReplies(da1=(1,), rtt=0.031)).remote)
        self.assertFalse(caps({}, ProbeReplies(da1=(1,), rtt=0.030)).remote)
        self.assertFalse(caps({}, ProbeReplies()).remote)
        local, remote = caps({}), caps({"SSH_CONNECTION": "1"})
        self.assertEqual((local.esc_timeout, local.string_timeout), (0.05, 0.5))
        self.assertEqual((remote.esc_timeout, remote.string_timeout), (0.15, 2.0))

    def test_sync_needs_decrpm_set_or_reset_and_the_flag_wins(self):
        for ps, expected in ((0, False), (1, True), (2, True), (3, False), (4, False), (None, False)):
            self.assertIs(caps({}, ProbeReplies(decrpm_2026=ps, da1=(1,))).sync, expected, ps)
        self.assertTrue(caps({}, overrides=Overrides(sync=True)).sync)
        self.assertFalse(caps({}, ProbeReplies(decrpm_2026=2, da1=(1,)), Overrides(sync=False)).sync)

    def test_kitty_flags_come_from_the_verified_push(self):
        for flags, release in ((0, False), (1, False), (3, False), (9, False), (10, True), (11, True), (15, True)):
            result = caps({}, kitty_verified=flags)
            self.assertEqual((result.kitty_flags, result.release_events), (flags, release), flags)
        self.assertEqual(caps({}, overrides=Overrides(kitty=False), kitty_verified=11).kitty_flags, 0)
        self.assertEqual(caps({}, overrides=Overrides(kitty=True), kitty_verified=11).kitty_flags, 11)

    def test_utf8_ambiguous_width_and_blocks(self):
        utf8, latin1 = {"LANG": "en_US.UTF-8"}, {"LANG": "en_US.ISO-8859-1"}
        cases = [
            (utf8, (3, 2), (True, False, True)),
            (utf8, (3, 3), (True, True, False)),
            ({}, (3, 2), (True, False, True)),
            ({"TERM": "linux"}, (3, 2), (True, False, True)),
            (latin1, (3, 2), (False, False, False)),
            (utf8, None, (True, None, True)),
            ({}, None, (True, None, True)),
            ({"TERM": "linux", **utf8}, None, (False, None, False)),
            (latin1, None, (False, None, False)),
            (utf8, (3, 4), (False, False, False)),  # 5.2: any other column means the terminal is not decoding UTF-8
        ]
        for env, width, expected in cases:
            with self.subTest(env=env, width=width):
                result = caps(env, ProbeReplies(cpr_width=width, da1=(1,)))
                self.assertEqual((result.utf8, result.ambiguous_wide, result.unicode_blocks), expected)

    def test_echoed_queries_move_the_first_cpr(self):
        self.assertFalse(caps({}, ProbeReplies(cpr_echo=(2, 1), da1=(1,))).echoes_queries)
        self.assertTrue(caps({}, ProbeReplies(cpr_echo=(2, 4), da1=(1,))).echoes_queries)
        self.assertFalse(caps({}).echoes_queries)

    def test_ack_prefers_cpr_then_dsr_then_da1(self):
        self.assertEqual(caps({}, ProbeReplies(cpr_echo=(2, 1), dsr=True, da1=(1,))).ack, "cpr")
        self.assertEqual(caps({}, ProbeReplies(cpr_echo=(2, 4), da1=(1,))).ack, "cpr")  # an echoing terminal too
        self.assertEqual(caps({}, ProbeReplies(cpr_echo=(3, 1), dsr=True, da1=(1,))).ack, "dsr")  # not on its row
        self.assertEqual(caps({}, ProbeReplies(dsr=True, da1=(1,))).ack, "dsr")
        self.assertEqual(caps({}).ack, "da1")
        self.assertIsNone(caps({}, ProbeReplies()).ack)

    def test_pacing_off_asks_for_no_acks_but_keeps_the_teardown_sentinel(self):
        answered = ProbeReplies(dsr=True, da1=(1,))
        for pacing in ("auto", "adaptive", "fixed"):
            self.assertEqual(caps({}, answered, Overrides(pacing=pacing)).ack, "dsr")
        off = caps({}, answered, Overrides(pacing="off"))
        self.assertIsNone(off.ack)
        self.assertEqual(off.answered, frozenset({"dsr", "da1"}))

    def test_answered_lists_every_reply_including_zero_values(self):
        full = ProbeReplies(0, 0, "kitty(1)", (0, 0, 0), (2, 1), (3, 2), True, (62,), 0.001)
        expected = {"kitty_flags", "decrpm_2026", "xtversion", "osc11", "cpr_echo", "cpr_width", "dsr", "da1"}
        self.assertEqual(caps({}, full).answered, expected)
        self.assertEqual(caps({}).answered, {"da1"})

    def test_a_silent_terminal_resolves_from_the_environment_only(self):
        partial = ProbeReplies(kitty_flags=0, decrpm_2026=2, xtversion="kitty(1)", osc11=(255, 255, 255), dsr=True)
        result = caps({"TERM": "xterm"}, partial)
        self.assertEqual(result.answered, frozenset())
        self.assertEqual((result.family, result.color, result.background), ("unknown", "16", "dark"))
        self.assertEqual((result.sync, result.ack, result.probe_rtt, result.ambiguous_wide), (False, None, None, None))

    def test_resolve_is_total_and_deterministic(self):
        first = resolve(EnvHints(), ProbeReplies(), Overrides())
        self.assertEqual(first, resolve(EnvHints(), ProbeReplies(), Overrides()))
        self.assertEqual(hash(first), hash(resolve(EnvHints(), ProbeReplies(), Overrides())))


class ThemeTests(unittest.TestCase):
    def background(self, env=None, osc11=None):
        return caps(env, ProbeReplies(osc11=osc11, da1=(1,))).background

    def test_osc11_luminance(self):
        self.assertEqual(self.background(osc11=(255, 255, 255)), "light")
        self.assertEqual(self.background(osc11=(0, 0, 0)), "dark")
        self.assertEqual(self.background(osc11=(128, 128, 128)), "light")
        self.assertEqual(self.background(osc11=(127, 127, 127)), "dark")
        self.assertEqual(self.background(osc11=(0, 255, 0)), "light")  # Rec. 709 weights green most
        self.assertEqual(self.background(osc11=(255, 0, 255)), "dark")

    def test_colorfgbg_when_osc11_is_unanswered(self):
        for value, expected in (("0;15", "light"), ("0;7", "light"), ("0;default;15", "light"), ("15;0", "dark")):
            self.assertEqual(self.background({"COLORFGBG": value}), expected, value)
        self.assertEqual(self.background({"COLORFGBG": ""}), "dark")
        self.assertEqual(self.background({"COLORFGBG": "0;15"}, osc11=(0, 0, 0)), "dark")  # the answer wins

    def test_theme_flag_and_painting(self):
        light = ProbeReplies(osc11=(250, 250, 250), da1=(1,))
        for color, theme, expected in [
            ("truecolor", "auto", ("light", True)),
            ("256", "auto", ("light", True)),
            ("16", "auto", ("light", False)),
            ("mono", "auto", ("light", False)),
            ("truecolor", "dark", ("dark", True)),
            ("16", "dark", ("dark", False)),
            ("256", "light", ("light", True)),
            ("truecolor", "terminal", ("light", False)),
            ("truecolor", "bogus", ("light", True)),
        ]:
            with self.subTest(color=color, theme=theme):
                result = caps({}, light, Overrides(color=color, theme=theme))
                self.assertEqual((result.theme, result.paint_background), expected)
                self.assertEqual(result.background, "light")


class RowTests(unittest.TestCase):
    def test_row_summarises_a_capable_terminal(self):
        doc = load("tmux-3.6-in-ghostty")
        row = caps(doc["env"], parse_replies(doc["replies"], rtt=0.0123), kitty_verified=11).row()
        self.assertEqual(
            row, "tmux 3.6a  local in tmux  truecolor dark  no sync  keys kitty 11 release  blocks  ack cpr  rtt 12.3ms"
        )

    def test_row_of_a_poor_terminal(self):
        replies = ProbeReplies(cpr_echo=(2, 3), cpr_width=(3, 3), da1=(1,), rtt=0.2)
        row = caps({"TERM": "xterm", "SSH_TTY": "1"}, replies, kitty_verified=1).row()
        self.assertEqual(
            row,
            "unknown  remote  16 dark on terminal bg  no sync  keys kitty 1  utf8 ambiguous-wide"
            "  ack cpr  echoes queries  rtt 200.0ms",
        )
        silent = caps({"TERM": "linux", "LANG": "C"}, ProbeReplies()).row()
        self.assertEqual(silent, "linux  local  16 dark on terminal bg  no sync  keys legacy  ascii  ack none  silent")


if __name__ == "__main__":
    unittest.main()
