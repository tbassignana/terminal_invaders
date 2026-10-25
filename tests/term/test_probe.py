"""The probe on a FakeTerminal (spec 5.2 to 5.4): the batch per env, reply routing, the deadline, early and late
input, echo detection, and the kitty push with its verification."""

import errno
import unittest
from unittest.mock import Mock

from terminal_invaders.keymap import Keymap
from terminal_invaders.term.caps import EnvHints, Overrides
from terminal_invaders.term.decode import Decoder
from terminal_invaders.term.keys import Key, Reply
from terminal_invaders.term.probe import QUERIES, echoed, probe_bytes, push_kitty, run_probe, timeout
from tests.support.faketerm import FakeTerminal
from tests.support.terminals import capabilities, load

ALL = b"".join(data for _, data in QUERIES)
LOCAL, SSH = EnvHints(term="xterm-256color"), EnvHints(term="xterm-256color", ssh=True)
PUSH = b"\x1b[>11u\x1b[?u\x1b[c"


def probe(doc, env=None, **kwargs):
    fake = FakeTerminal(doc, **kwargs)
    env = env or EnvHints.from_env(doc["env"])
    return fake, run_probe(fake.fd, env, **fake.ops().io())


def push(doc, keymap=None, overrides=Overrides()):
    fake, replies = probe(doc)
    sent = len(fake.output)
    result = push_kitty(fake.fd, replies, keymap, overrides, 0.25, **fake.ops().io())
    return result, bytes(fake.output[sent:])


class BatchTests(unittest.TestCase):
    def test_every_query_goes_in_one_batch_ending_in_da1(self):
        self.assertEqual(probe_bytes(LOCAL), ALL)
        self.assertTrue(ALL.startswith(b"\x1b[2;1H\x1b[?u"))
        self.assertTrue(ALL.endswith(b"\x1b[6n\x1b[3;1H\xe2\x96\x80\x1b[6n\x1b[5n\x1b[c"))

    def test_apple_terminal_skips_kitty_decrqm_and_xtversion(self):
        batch = probe_bytes(EnvHints(term="xterm-256color", term_program="Apple_Terminal"))
        for query in (b"\x1b[?u", b"\x1b[?2026$p", b"\x1b[>0q"):
            self.assertNotIn(query, batch)
        self.assertIn(b"\x1b]11;?\x1b\\", batch)

    def test_the_linux_console_skips_decrqm_xtversion_and_osc11(self):
        batch = probe_bytes(EnvHints(term="linux"))
        for query in (b"\x1b[?2026$p", b"\x1b[>0q", b"\x1b]11;?\x1b\\"):
            self.assertNotIn(query, batch)
        self.assertIn(b"\x1b[?u", batch)
        self.assertTrue(batch.endswith(b"\x1b[5n\x1b[c"))

    def test_the_deadline_is_250ms_locally_and_a_second_over_ssh(self):
        self.assertEqual(timeout(LOCAL), 0.25)
        self.assertEqual(timeout(SSH), 1.0)


class ReplyTests(unittest.TestCase):
    def test_every_reply_is_routed(self):
        fake, replies = probe(load("ghostty"), rtt=0.02)
        self.assertEqual(fake.output, ALL)
        self.assertEqual(replies.kitty_flags, 0)
        self.assertEqual(replies.decrpm_2026, 2)
        self.assertTrue(replies.xtversion.startswith("ghostty"))
        self.assertEqual(replies.osc11, (0x28, 0x2C, 0x34))
        self.assertEqual((replies.cpr_echo, replies.cpr_width), ((2, 1), (3, 2)))
        self.assertTrue(replies.dsr)
        self.assertIsNotNone(replies.da1)
        self.assertAlmostEqual(replies.rtt, 0.02)
        self.assertEqual(replies.raw, load("ghostty")["replies"].encode())
        self.assertEqual(replies.early_keys, ())

    def test_reading_stops_at_the_da1_reply(self):
        fake = FakeTerminal(load("xterm"), rtt=0.01)
        fake.type(b"x", at=0.2)
        replies = run_probe(fake.fd, LOCAL, **fake.ops().io())
        self.assertIsNotNone(replies.da1)
        self.assertAlmostEqual(fake.now, 0.01)
        self.assertEqual(replies.early_keys, ())  # the x is the game's, typed after the probe

    def test_a_silent_terminal_gives_up_at_the_deadline(self):
        fake, replies = probe(load("silent"))
        self.assertIsNone(replies.da1)
        self.assertIsNone(replies.rtt)
        self.assertEqual(replies.raw, b"")
        self.assertAlmostEqual(fake.now, 0.25)

    def test_a_hangup_or_an_end_of_input_ends_the_probe_as_silent(self):
        for result in (b"", OSError(errno.EIO, "EIO")):
            with self.subTest(result=result):
                fake = FakeTerminal(load("xterm"), rtt=0.01)
                io = fake.ops().io() | {"read": Mock(side_effect=[result])}
                replies = run_probe(fake.fd, LOCAL, **io)
                self.assertEqual((replies.da1, replies.raw), (None, b""))
                self.assertAlmostEqual(fake.now, 0.01)  # no wait for the deadline

    def test_replies_the_probe_does_not_route_are_logged_and_dropped(self):
        fake = FakeTerminal(load("xterm"), rtt=0.01)
        fake.type(b"\x1b[3n")  # DSR: malfunction
        with self.assertLogs(level="DEBUG") as logs:
            replies = run_probe(fake.fd, LOCAL, **fake.ops().io())
        self.assertIn("Probe reply not routed: Reply(kind='dsr', value=3", "\n".join(logs.output))
        self.assertTrue(replies.dsr)  # the terminal's own DSR 0 still counts

    def test_ssh_waits_a_second_for_the_replies(self):
        doc = load("xterm")
        fake, local = probe(doc, LOCAL, rtt=0.6)
        self.assertIsNone(local.da1)
        self.assertAlmostEqual(fake.now, 0.25)
        fake, remote = probe(doc, SSH, rtt=0.6)
        self.assertIsNotNone(remote.da1)
        self.assertAlmostEqual(remote.rtt, 0.6)

    def test_late_replies_are_never_keys(self):
        fake, replies = probe(load("xterm"), LOCAL, rtt=0.4)
        self.assertEqual(replies.early_keys, ())
        fake.select([fake.fd], [], [], 1.0)
        decoder = Decoder(esc_timeout=0.05, string_timeout=0.5)  # the game's
        decoder.feed(fake.read(fake.fd, 4096), fake.now)
        events = decoder.events(100)
        self.assertTrue(events)
        self.assertTrue(all(isinstance(event, Reply) for event in events), events)
        self.assertEqual([e.value for e in events if e.kind == "cpr"], [(2, 1), (3, 2)])

    def test_a_late_ack_is_no_probe_reply(self):
        """A restart probes again while an ack may still come: a CPR from row TAG_FIRST down answers a frame's ack
        query, not the probe's."""
        fake = FakeTerminal(load("xterm"), rtt=0.05)
        fake.type(b"\x1b[4;2R\x1b[7;31R", at=0.0)  # two frames' acks, read ahead of the probe's replies
        with self.assertLogs(level="DEBUG") as logs:
            replies = run_probe(fake.fd, LOCAL, **fake.ops().io())
        self.assertEqual((replies.cpr_echo, replies.cpr_width), ((2, 1), (3, 2)))
        self.assertIn("Probe reply not routed: Reply(kind='cpr', value=(4, 2)", "\n".join(logs.output))

    def test_a_late_ack_a_shrunk_terminal_clamped_onto_the_probe_s_rows_comes_first(self):
        """P5 of the S13 review: on a screen of 2 or 3 rows a terminal clamps a late ack (tagged for a larger screen)
        onto the probe's own rows. It comes ahead of the probe's replies, as its query went out first, so the probe's
        are the last two CPRs there. Taken in order, the late ack became the echo and the echo the width: UTF-8 off,
        an echoing terminal and DSR acks for the rest of the session."""
        for late in (b"\x1b[3;33R", b"\x1b[2;33R", b"\x1b[3;33R\x1b[2;9R"):
            with self.subTest(late=late):
                fake = FakeTerminal(load("xterm"), rtt=0.05)
                fake.type(late, at=0.0)
                replies = run_probe(fake.fd, LOCAL, **fake.ops().io())
                self.assertEqual((replies.cpr_echo, replies.cpr_width), ((2, 1), (3, 2)))

    def test_keys_typed_during_the_probe_are_early_keys(self):
        fake = FakeTerminal(load("ghostty"), rtt=0.05)
        fake.type(b"f", at=0.0)
        fake.type(b"\x1b[D", at=0.01)
        replies = run_probe(fake.fd, LOCAL, **fake.ops().io())
        self.assertEqual([(key.name, key.mods) for key in replies.early_keys], [("f", 0), ("left", 0)])
        self.assertTrue(all(isinstance(key, Key) for key in replies.early_keys))
        self.assertIsNotNone(replies.da1)


class EchoTests(unittest.TestCase):
    def test_an_echoing_terminal_moves_the_cursor_on_row_2(self):
        doc = load("ssh-apple-terminal")
        _, replies = probe(doc)
        self.assertEqual(replies.cpr_echo, (2, 2))
        caps = capabilities(doc)
        self.assertTrue(caps.echoes_queries)
        skip = echoed(caps.answered)
        self.assertEqual(skip, {"kitty", "decrqm", "xtversion"})  # it answered OSC 11
        batch = probe_bytes(EnvHints.from_env(doc["env"]), skip)
        self.assertEqual(batch, ALL.replace(b"\x1b[?u", b"").replace(b"\x1b[?2026$p", b"").replace(b"\x1b[>0q", b""))

    def test_a_terminal_that_prints_nothing_stays_on_column_1(self):
        _, replies = probe(load("xterm"))
        self.assertEqual(replies.cpr_echo, (2, 1))
        self.assertFalse(capabilities(load("xterm")).echoes_queries)


class KittyPushTests(unittest.TestCase):
    def test_a_verified_push_returns_depth_1_the_flags_and_the_reply(self):
        result, sent = push(load("kitty"))
        self.assertEqual(result, (1, 11, load("kitty")["push_reply"].encode()))  # the reply, for --probe --raw
        self.assertEqual(sent, PUSH)

    def test_a_failed_verification_pops_the_push(self):
        result, sent = push(load("wezterm-default"))
        self.assertEqual(result, (0, 0, load("wezterm-default")["push_reply"].encode()))
        self.assertEqual(sent, PUSH + b"\x1b[<u")

    def test_physical_bindings_ask_for_flag_4_and_work_without_it(self):
        with self.assertLogs(level="DEBUG") as logs:
            result, sent = push(load("kitty"), Keymap.preset("wasd-physical"))
        self.assertEqual(sent, b"\x1b[>15u\x1b[?u\x1b[c")
        self.assertEqual(result[:2], (1, 11))  # flag 4 absent: the physical keys fall back to logical ones
        self.assertIn("flag 4", logs.output[-1])

    def test_no_push_when_refused_or_unsupported(self):
        self.assertEqual(push(load("kitty"), overrides=Overrides(kitty=False)), ((0, 0, None), b""))
        self.assertEqual(push(load("xterm")), ((0, 0, None), b""))  # no answer to the flags query
