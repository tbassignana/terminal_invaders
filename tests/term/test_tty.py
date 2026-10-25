"""Owning the tty: raw attributes, opening, the output queue and bounded writes, and the TerminalSession's signals,
suspend and resume and quiet teardown, on a FakeTerminal and once on a real pty."""

import errno
import fcntl
import os
import signal
import struct
import sys
import termios
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from terminal_invaders.term import output, tty
from terminal_invaders.term.caps import EnvHints, Overrides
from terminal_invaders.term.output import PTY_ROOM, OutputQueue, on_alarm, timed_write, write_all, write_bounded
from terminal_invaders.term.pacing import AckPacer
from terminal_invaders.term.probe import CLEAR, probe_bytes
from terminal_invaders.term.tty import (
    ENTER,
    TITLE,
    TITLE_POP,
    FdOps,
    TerminalGone,
    TerminalSession,
    leave_bytes,
    raw_attrs,
)
from tests.support.faketerm import FakeTerminal
from tests.support.ptyrun import Stub
from tests.support.terminals import load

LEAVE = leave_bytes(cut=False, sync=False, kitty_depth=0)
DARWIN = dict(BRKINT=0x2, ICRNL=0x100, INLCR=0x40, IGNCR=0x80, INPCK=0x10, ISTRIP=0x20, IXON=0x200, IXOFF=0x400)
DARWIN |= dict(IXANY=0x800, PARMRK=0x8, ECHO=0x8, ECHONL=0x10, ICANON=0x100, IEXTEN=0x400, ISIG=0x80)
DARWIN |= dict(NOFLSH=0x80000000, VMIN=16, VTIME=17, VDSUSP=11, VSTATUS=18, VDISCARD=15, VLNEXT=14, NCCS=20)
WAS_WRITTEN = 0x10000 if sys.platform == "darwin" else 0  # XNU's FWASWRITTEN shows in F_GETFL once an fd is written
INPUT_OFF = ("BRKINT", "ICRNL", "INLCR", "IGNCR", "INPCK", "ISTRIP", "IXON", "IXOFF", "IXANY", "PARMRK")


class Writes:
    """os.write and select.select stand-ins: scripted results per write, a clock that each write advances."""

    def __init__(self, *results, took=0.0, writable=True):
        self.results, self.took, self.writable = list(results), took, writable
        self.now, self.data = 0.0, bytearray()

    def clock(self):
        return self.now

    def select(self, r, w, x, timeout):
        if not self.writable:
            self.now += timeout or 0
        return [], list(w) if self.writable else [], []

    def write(self, fd, data):
        result = self.results.pop(0) if self.results else len(data)
        self.now += self.took
        if isinstance(result, BaseException):
            raise result
        self.data += data[:result]
        return min(result, len(data))

    def ops(self):
        return dict(clock=self.clock, write=self.write, select=self.select)


def session(doc, *, kitty=False, **kwargs):
    fake = FakeTerminal(doc, **kwargs)
    overrides = Overrides(pacing="fixed", kitty=kitty)
    return fake, TerminalSession(fake.fd, EnvHints.from_env(doc["env"]), overrides, ops=fake.ops())


class Hooks:
    def __init__(self):
        self.calls = []

    def before_suspend(self):
        self.calls.append("before_suspend")

    def after_resume(self, caps, reason):
        self.calls.append(("after_resume", caps.family, reason))


class RawAttrsTests(unittest.TestCase):
    def check(self, constants):
        everything = sum(getattr(constants, name) for name in INPUT_OFF)
        lflag = constants.ECHO | constants.ECHONL | constants.ICANON | constants.IEXTEN
        saved = [everything | 0x1, 0x3, 0x4B00, lflag, 9600, 9600, [b"x"] * constants.NCCS]
        with patch.object(tty, "termios", constants), patch.object(tty, "vdisable", return_value=0xFF):
            raw = raw_attrs(saved, 0)
        self.assertEqual(raw[tty.IFLAG], 0x1)
        self.assertEqual(raw[1:3], [0x3, 0x4B00])  # output processing and control flags untouched
        self.assertEqual(raw[tty.LFLAG], constants.ISIG | constants.NOFLSH)
        self.assertEqual((raw[tty.CC][constants.VMIN], raw[tty.CC][constants.VTIME]), (1, 0))
        disabled = [name for name in ("VDSUSP", "VSTATUS", "VDISCARD", "VLNEXT") if hasattr(constants, name)]
        for name in disabled:
            self.assertEqual(raw[tty.CC][getattr(constants, name)], b"\xff")
        self.assertEqual(saved[tty.CC], [b"x"] * constants.NCCS)  # a copy: the saved attributes stay as they were
        return disabled

    def test_linux_and_darwin_constants(self):
        self.assertEqual(self.check(SimpleNamespace(**DARWIN)), ["VDSUSP", "VSTATUS", "VDISCARD", "VLNEXT"])
        linux = {name: getattr(termios, name) for name in (*INPUT_OFF, "ECHO", "ECHONL", "ICANON", "IEXTEN")}
        linux |= {name: getattr(termios, name) for name in ("ISIG", "NOFLSH", "VMIN", "VTIME", "VDISCARD", "VLNEXT")}
        self.assertEqual(self.check(SimpleNamespace(**linux, NCCS=32)), ["VDISCARD", "VLNEXT"])
        no_ixany = {key: value for key, value in DARWIN.items() if key != "IXANY"}
        self.check(SimpleNamespace(**no_ixany, IXANY=0))  # a flag the platform lacks counts as 0

    def test_vdisable_falls_back_per_platform(self):
        self.assertEqual(tty.vdisable(-1), 0xFF if tty.sys.platform == "darwin" else 0)
        with patch.object(tty.sys, "platform", "darwin"):
            self.assertEqual(tty.vdisable(-1), 0xFF)
        master, slave = os.openpty()
        try:
            self.assertIn(tty.vdisable(slave), (0, 0xFF))
        finally:
            os.close(master)
            os.close(slave)


class OpenTtyTests(unittest.TestCase):
    def test_without_a_controlling_terminal_ttyname_is_opened_afresh(self):
        opened = []

        def fake_open(path, flags):
            opened.append((path, flags))
            if path == "/dev/tty":
                raise OSError(errno.ENXIO, "no such device")
            return 7

        with patch.object(tty.os, "open", fake_open), patch.object(tty.os, "ttyname", return_value="/dev/pts/9"):
            with patch.object(tty.os, "dup", side_effect=AssertionError("never dup")):
                self.assertEqual(tty.open_tty(), 7)
        self.assertEqual([path for path, _ in opened], ["/dev/tty", "/dev/pts/9"])
        for _, flags in opened:
            self.assertEqual(flags, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)  # blocking: never O_NONBLOCK

    def test_no_terminal_at_all_is_a_value_error(self):
        with patch.object(tty.os, "open", side_effect=OSError(errno.ENXIO, "no device")):
            with patch.object(tty.os, "ttyname", side_effect=OSError(errno.ENOTTY, "not a tty")):
                with self.assertRaisesRegex(ValueError, "no terminal"):
                    tty.open_tty()
        with patch.object(tty.os, "open", side_effect=PermissionError(errno.EACCES, "denied")):
            with self.assertRaisesRegex(ValueError, "no terminal"):
                tty.open_tty()

    def test_the_controlling_terminal_comes_first(self):
        with patch.object(tty.os, "open", return_value=5) as opened:
            self.assertEqual(tty.open_tty(), 5)
        opened.assert_called_once_with("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)


class SequenceTests(unittest.TestCase):
    def test_leave_cancels_a_cut_frame_ends_sync_and_pops_kitty(self):
        self.assertEqual(LEAVE, b"\x1b[0m\x1b[?2004l\x1b[?1004l\x1b[?7h\x1b[?25h\x1b[?1049l\x1b[23;0;0t")
        self.assertEqual(leave_bytes(cut=True, sync=True, kitty_depth=1), b"\x18\x1b[?2026l\x1b[<u" + LEAVE)
        self.assertEqual(leave_bytes(cut=False, sync=True, kitty_depth=0), b"\x1b[?2026l" + LEAVE)

    def test_enter_sets_every_mode_leave_restores(self):
        for part in (b"\x1b[?1049h", b"\x1b[22;0;0t", b"\x1b(B", b"\x1b[0m", b"\x1b[4l", b"\x1b[?6l", b"\x1b[r"):
            self.assertIn(part, ENTER)
        self.assertTrue(ENTER.endswith(b"\x1b[?25l\x1b[?7l\x1b[?1004h\x1b[?2004h\x1b[H\x1b[2J"))


class OutputTests(unittest.TestCase):
    def test_write_all_loops_on_short_writes_and_interrupts(self):
        data = bytes(range(256)) * 40
        writes = Writes(100, InterruptedError(), 1, 5000)
        self.assertTrue(write_all(3, data, 1.0, **writes.ops()))
        self.assertEqual(bytes(writes.data), data)

    def test_a_stalled_terminal_times_out_and_a_gone_one_raises(self):
        self.assertFalse(write_all(3, b"x", 0.5, **Writes(writable=False).ops()))
        with self.assertRaises(OSError) as raised:
            write_all(3, b"x", 0.5, **Writes(OSError(errno.EIO, "EIO")).ops())
        self.assertEqual(raised.exception.errno, errno.EIO)
        self.assertFalse(write_bounded(3, b"x", 0.5, **Writes(OSError(errno.ENXIO, "gone")).ops()))
        with self.assertRaises(OSError):
            write_bounded(3, b"x", 0.5, **Writes(OSError(errno.ENOSPC, "full")).ops())

    def test_chunk_halves_on_slow_writes_doubles_on_fast_full_ones(self):
        writes = Writes(took=0.003)
        queue = OutputQueue(3, writes.clock, write=writes.write, select=writes.select)
        queue.push(b"x" * 20000)
        queue.flush_some(budget=0.0)
        self.assertEqual(queue.chunk, 2048)
        for _ in range(10):
            queue.flush_some(budget=0.0)
        self.assertEqual(queue.chunk, OutputQueue.MIN_CHUNK)
        writes.took = 0.0
        queue.push(b"x" * 300000)
        queue.flush_some(budget=0.0)
        self.assertEqual(queue.chunk, 512)
        queue.flush_some(budget=1.0)
        self.assertTrue(queue.empty())
        self.assertEqual(queue.chunk, OutputQueue.MAX_CHUNK)
        self.assertAlmostEqual(queue.longest, 0.003)

    def test_behind_an_unacknowledged_frame_writes_stay_within_the_room_a_pty_surely_has(self):
        writes = Writes()
        queue = OutputQueue(3, writes.clock, write=writes.write, select=writes.select)
        sizes = []
        queue.write = lambda fd, data: sizes.append(len(data)) or writes.write(fd, data)
        queue.chunk = OutputQueue.MAX_CHUNK
        queue.push(b"x" * 9000, frame=True)
        queue.flush_some(budget=1.0, unread=5000)  # 5000 bytes of the frame before may still be in the pty
        self.assertEqual(sizes[:2], [PTY_ROOM - 5000, OutputQueue.MIN_CHUNK])
        self.assertEqual((set(sizes[1:-1]), sum(sizes), queue.chunk), ({256}, 9000, OutputQueue.MAX_CHUNK))
        sizes.clear()
        queue.push(b"x" * 9000, frame=True)
        queue.flush_some(budget=1.0)  # nothing unacknowledged before it: one write
        self.assertEqual(sizes, [9000])

    def test_flush_some_counts_the_time_it_spent_inside_write(self):
        """The loop takes that time off a frame's held time: blocked in write(), the bytes wait on the terminal, not
        the game. Each call counts its own writes, an interrupted one too."""
        writes = Writes(InterruptedError(), 100, 100, took=0.003)
        queue = OutputQueue(3, writes.clock, write=writes.write, select=writes.select)
        queue.push(b"x" * 200)
        queue.flush_some(budget=0.0)
        self.assertAlmostEqual(queue.writing, 0.006)  # the interrupted write, then 100 bytes
        queue.flush_some(budget=1.0)
        self.assertAlmostEqual(queue.writing, 0.003)
        self.assertTrue(queue.empty())

    def test_a_backed_up_terminal_gets_the_smallest_chunks(self):
        writes = Writes(writable=False)
        queue = OutputQueue(3, writes.clock, write=writes.write, select=writes.select)
        queue.push(b"x" * 100)
        queue.flush_some()
        self.assertEqual((queue.chunk, queue.pending(), queue.wants_write()), (OutputQueue.MIN_CHUNK, 100, True))

    def test_short_writes_leave_a_frame_half_sent(self):
        writes = Writes(40, 10, 0)
        queue = OutputQueue(3, writes.clock, write=writes.write, select=writes.select)
        queue.push(b"s" * 20)
        queue.push(b"f" * 100, frame=True)
        queue.flush_some(budget=0.0)
        self.assertTrue(queue.mid_frame)  # 20 status bytes and 20 of the frame are out
        queue.flush_some(budget=1.0)
        self.assertFalse(queue.mid_frame)
        self.assertTrue(queue.empty())
        queue.push(b"f" * 10, frame=True)
        queue.push(b"s" * 10)
        self.assertFalse(queue.mid_frame)  # not started yet
        queue.discard()
        self.assertFalse(queue.mid_frame)

    def test_eio_kills_the_queue(self):
        writes = Writes(OSError(errno.EIO, "EIO"))
        queue = OutputQueue(3, writes.clock, write=writes.write, select=writes.select)
        queue.push(b"x" * 10)
        queue.flush_some()
        self.assertEqual((queue.dead, queue.error, queue.empty(), queue.wants_write()), (True, errno.EIO, True, False))
        queue.push(b"more")
        self.assertTrue(queue.empty())
        self.assertFalse(queue.drain(1.0))

    def test_blocking_io_error_waits_for_the_next_select(self):
        writes = Writes(BlockingIOError())
        queue = OutputQueue(3, writes.clock, write=writes.write, select=writes.select)
        queue.push(b"x")
        queue.flush_some()
        self.assertEqual(queue.pending(), 1)

    def test_drain_gives_up_at_its_timeout(self):
        writes = Writes(writable=False)
        queue = OutputQueue(3, writes.clock, write=writes.write, select=writes.select)
        queue.push(b"x")
        self.assertFalse(queue.drain(0.2))
        self.assertGreaterEqual(writes.now, 0.2)
        writes.writable = True
        self.assertTrue(queue.drain(0.2))


class TimedWriteTests(unittest.TestCase):
    def test_a_write_that_blocks_returns_short_and_leaves_no_timer_behind(self):
        master, slave = os.openpty()
        old = signal.signal(signal.SIGALRM, on_alarm)
        try:
            os.set_blocking(slave, False)
            with self.assertRaises(BlockingIOError):
                while True:
                    os.write(slave, b"x" * 256)  # a full pty: the terminal reads nothing
            os.set_blocking(slave, True)
            os.read(master, 1024)  # a little room, as select() promises before a write
            started = time.monotonic()
            written = timed_write(slave, b"y" * 65536)  # os.write would wait until all of it fits: forever
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertTrue(0 < written < 65536)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        finally:
            signal.signal(signal.SIGALRM, old)
            os.close(master)
            os.close(slave)

    def test_without_its_handler_it_is_a_plain_write(self):
        with (
            patch.object(output.os, "write", return_value=3) as write,
            patch.object(output.signal, "setitimer") as timer,
        ):
            self.assertEqual(timed_write(5, b"abc"), 3)
        write.assert_called_once_with(5, b"abc")
        timer.assert_not_called()  # the default SIGALRM would end the process
        self.assertIs(FdOps().write, timed_write)


class SessionTests(unittest.TestCase):
    def test_enter_probes_inside_the_alt_screen_and_clears_after(self):
        fake, term = session(load("ghostty"))
        with term:
            batch = ENTER + probe_bytes(term.env)
            self.assertEqual(fake.output, batch + CLEAR + TITLE)
            self.assertEqual(
                fake.calls, [("tcsetattr", termios.TCSANOW, 0, 0.0), ("tcflush", termios.TCIFLUSH, 0, 0.0)]
            )
            self.assertEqual(fake.attrs, raw_attrs(term.saved, fake.fd))
            self.assertEqual((term.caps.family, term.pacer.mode, term.kitty_depth), ("ghostty", "fixed", 0))
            self.assertTrue(term.resized and fake.vt.alt_screen)
        self.assertEqual(fake.attrs, term.saved)
        self.assertFalse(fake.vt.alt_screen)
        self.assertEqual(fake.vt.title, "")  # the title stack popped

    def test_verified_kitty_flags_are_popped_on_the_alt_screen(self):
        fake, term = session(load("kitty"), kitty=None)
        with term:
            self.assertEqual((term.kitty_depth, term.caps.kitty_flags, fake.vt.kitty_depth), (1, 11, 1))
        self.assertIn(b"\x1b[<u\x1b[0m", fake.output)
        self.assertEqual(fake.vt.kitty_depth, 0)

    def test_other_families_keep_their_title_and_silence_means_clock_pacing(self):
        fake, term = session(load("apple-terminal-15"))
        with term:
            self.assertNotIn(TITLE, fake.output)
        fake, term = session(load("silent"))
        with term:
            self.assertEqual((term.pacer.mode, term.caps.ack), ("off", None))
            self.assertEqual(fake.now, 0.25)  # the probe deadline
        self.assertTrue(fake.output.endswith(LEAVE))  # no sentinel for a terminal that never answers

    def test_teardown_waits_for_the_sentinel_so_late_replies_are_never_echoed(self):
        fake, term = session(load("ghostty"))
        with term:
            fake.type(b"\x1b[2;1R\x1b[?2026;2$yq", at=fake.now + 0.0005)  # a late CPR, a late DECRPM, a key
            sent = fake.now
        restore = fake.calls[-2]
        self.assertTrue(fake.output.endswith(b"\x1b[?2026l" + LEAVE + b"\x1b[c"))
        self.assertEqual(restore[:3], ("tcsetattr", termios.TCSANOW, len(fake.output)))
        self.assertAlmostEqual(restore[3], sent + fake.rtt)  # cooked mode only once the sentinel's reply was read
        self.assertEqual(fake.calls[-1][:2], ("tcflush", termios.TCIFLUSH))
        self.assertEqual(fake.inbox, [])  # everything read and dropped while still raw

    def test_the_sentinel_is_dsr_when_only_dsr_answered(self):
        fake, term = session(load("xterm"))
        with term:
            term.caps = replace(term.caps, answered=frozenset({"dsr"}))
        self.assertTrue(fake.output.endswith(LEAVE + b"\x1b[5n"))
        self.assertEqual(fake.inbox, [])

    def test_when_acks_and_sentinel_are_both_da1_the_acks_in_flight_are_read_first(self):
        fake, term = session(load("xterm"), rtt=0.05)
        with term:
            term.caps = replace(term.caps, ack="da1", answered=term.caps.answered - {"dsr"})
            term.pacer = AckPacer(term.pacer.mode, clock=fake.clock)  # DA1 acks match in order
            for _ in range(2):
                term.pacer.sent(fake.now, 100, full=False)
            for delay, data in ((0.01, b"\x1b[?64c"), (0.02, b"q"), (0.03, b"\x1b[?64c")):
                fake.type(data, at=fake.now + delay)  # the two frames' acks, and a key between them
            sent = fake.now
        self.assertTrue(fake.output.endswith(LEAVE + b"\x1b[c"))
        self.assertAlmostEqual(fake.calls[-2][3], sent + 0.05)  # cooked mode after the sentinel's own reply
        self.assertEqual(fake.inbox, [])

    def test_acks_of_frames_that_timed_out_are_still_waited_for(self):
        fake, term = session(load("xterm"), rtt=0.05)
        with term:
            term.caps = replace(term.caps, ack="da1", answered=term.caps.answered - {"dsr"})
            term.pacer = AckPacer(term.pacer.mode, clock=fake.clock)  # DA1 acks match in order
            for _ in range(2):
                term.pacer.sent(fake.now - 2.0, 100, full=False)
            term.pacer.expire(fake.now)  # out of the window, yet their replies are on the way
            self.assertEqual((len(term.pacer.inflight), term.pacer.owed), (0, 2))
            for delay in (0.01, 0.03):
                fake.type(b"\x1b[?64c", at=fake.now + delay)
            sent = fake.now
        self.assertAlmostEqual(fake.calls[-2][3], sent + 0.05)  # cooked mode only after the sentinel's own reply
        self.assertEqual(fake.inbox, [])

    def test_a_restart_lets_the_acks_on_their_way_arrive_before_the_new_probe(self):
        fake, term = session(load("xterm"), rtt=0.05)
        with term:
            answered = term.caps.answered
            term.caps = replace(term.caps, ack="da1")  # a DA1 ack would end the new probe early
            term.pacer = AckPacer(term.pacer.mode, clock=fake.clock)
            term.pacer.sent(fake.now, 100, full=False)
            ack_at = fake.now + 0.01
            fake.type(b"\x1b[?64c", at=ack_at)
            before = len(fake.output)
            term.restart("reprobe")
            self.assertEqual(fake.output[before:].index(ENTER), len(TITLE_POP))  # ENTER pushes the title again
            self.assertEqual(fake.vt.titles, [""])
            restarted = before + len(TITLE_POP)
            enter = next(when for name, _, at, when in fake.calls if name == "tcsetattr" and at == restarted)
            self.assertAlmostEqual(enter, ack_at)  # re-entered once the ack was read
            self.assertEqual(term.caps.answered, answered)  # the whole probe was read
            self.assertEqual(fake.inbox, [])

    def test_cpr_acks_in_flight_are_read_on_the_way_to_the_sentinel_reply(self):
        """Replies come in order, so the CPR acks still to come are read before the sentinel's DA1 reply: teardown
        waits for that one reply only."""
        fake, term = session(load("ghostty"), rtt=0.05)
        with term:
            self.assertEqual(term.caps.ack, "cpr")
            for tag in ((4, 2), (4, 3)):
                term.pacer.sent(fake.now, 100, full=False, tag=tag)
            for delay, data in ((0.01, b"\x1b[4;2R"), (0.02, b"q"), (0.03, b"\x1b[4;3R")):
                fake.type(data, at=fake.now + delay)  # the two frames' acks, and a key between them
            sent = fake.now
        self.assertTrue(fake.output.endswith(LEAVE + b"\x1b[c"))
        self.assertAlmostEqual(fake.calls[-2][3], sent + 0.05)  # cooked mode after the sentinel's own reply
        self.assertEqual(fake.inbox, [])

    def test_a_restart_reads_the_tagged_acks_on_their_way_and_the_probe_leaves_a_later_one(self):
        """CPR acks: a restart reads the acks on their way for a settle time (0.15 s here). One that comes later
        still, as the terminal is probed again, is a CPR from row 4 down, which the probe takes for no reply of its
        own."""
        fake, term = session(load("xterm"), rtt=0.05)
        with term:
            self.assertEqual((term.caps.ack, term.pacer.tagged), ("cpr", True))
            answered, start = term.caps.answered, fake.now
            for tag in ((4, 2), (4, 3)):
                term.pacer.sent(fake.now, 100, full=False, tag=tag)
            fake.type(b"\x1b[4;2R", at=start + 0.01)
            fake.type(b"\x1b[4;3R", at=start + 0.17)  # after the settle time, ahead of the probe's replies
            before = len(fake.output)
            term.restart("reprobe")
            restarted = before + len(TITLE_POP)
            enter = next(when for name, _, at, when in fake.calls if name == "tcsetattr" and at == restarted)
            self.assertAlmostEqual(enter, start + 0.15)  # the settle time: one reply of the two came in it
            self.assertEqual(term.caps.answered, answered)
            self.assertEqual((term.replies.cpr_echo, term.replies.cpr_width), ((2, 1), (3, 2)))
            self.assertEqual((fake.inbox, term.pacer.tagged), ([], True))

    def test_a_restart_on_one_row_takes_the_clamped_acks_on_their_way_for_acks(self):
        """On one row the acks on their way come back clamped onto it, CSI 1;c R, which reads like F3: the settle read
        takes them for the replies they are, so the restart waits only for them, not its whole settle time (F3 of the
        S14 review's law lens)."""
        fake, term = session(load("xterm"), rtt=0.05)
        with term:
            start = fake.now
            for tag in ((4, 17), (4, 18)):
                term.pacer.sent(fake.now, 100, full=False, tag=tag)
            fake.type(b"\x1b[1;17R", at=start + 0.01)
            fake.type(b"\x1b[1;18R", at=start + 0.02)
            before = len(fake.output)
            term.restart("reprobe")
            restarted = before + len(TITLE_POP)
            enter = next(when for name, _, at, when in fake.calls if name == "tcsetattr" and at == restarted)
            self.assertAlmostEqual(enter, start + 0.02)
            self.assertEqual(term.early_keys, [])

    def test_a_restart_s_new_pacer_keeps_the_tags_of_the_old_frames(self):
        """The tag sequence goes on across a restart, but at another size it maps to other positions: the new pacer
        keeps the old frames whose replies may still come (AckPacer.inherit), so no new frame takes a tag one of them
        may name, and owes none of their queries (DOC-restart-tags of the S16 review)."""
        fake, term = session(load("xterm"), rtt=0.05)
        with term:
            old = term.pacer
            old.sent(fake.now, 100, full=False, tag=(16, 1))
            term.restart("reprobe")
            self.assertIsNot(term.pacer, old)
            self.assertEqual(
                (term.pacer.tagged, term.pacer.owed, [frame.tag for frame in term.pacer.voided]), (True, 0, [(16, 1)])
            )

    def test_a_cut_frame_is_cancelled_before_leave(self):
        fake, term = session(load("ghostty"))
        with term:
            fake.room = 12
            term.out.push(b"\x1b[?2026h\x1b[5;5Hhello\x1b[?2026l", frame=True)
            term.out.flush_some()
            self.assertTrue(term.out.mid_frame)
            fake.at(fake.now + 0.3, lambda: setattr(fake, "room", None))  # the link recovers after the drain gave up
        self.assertIn(b"\x1b[?2026h\x1b[5;\x18\x1b[?2026l" + LEAVE + b"\x1b[c", fake.output)
        self.assertFalse(fake.vt.alt_screen)

    def test_a_dead_terminal_gets_no_sentinel_and_restore_errors_are_swallowed(self):
        fake, term = session(load("ghostty"))
        with term:
            fake.dead = True
            term.out.push(b"frame", frame=True)
            term.out.flush_some()
            self.assertTrue(term.out.dead)
        self.assertTrue(term.closed)
        self.assertEqual(signal.getsignal(signal.SIGWINCH), signal.SIG_DFL)

    def test_teardown_drops_what_a_stalled_terminal_has_not_read_and_leaves_at_once(self):
        fake, term = session(load("ghostty"))
        with term:
            self.assertIs(signal.getsignal(signal.SIGALRM), on_alarm)  # what cuts a blocked write short
            fake.room = 0  # it reads nothing more: the pty is full
            term.out.push(b"\x1b[?2026h" + b"x" * 5000 + b"\x1b[?2026l\x1b[5n", frame=True)
            term.pacer.sent(fake.now, 5000, full=False)
            closing, before = fake.now, len(fake.output)
        flushed = [call for call in fake.calls if call[:2] == ("tcflush", termios.TCOFLUSH)]
        self.assertEqual(flushed, [("tcflush", termios.TCOFLUSH, before, closing)])  # first, and without waiting
        self.assertEqual(fake.output[before:], b"\x18\x1b[?2026l" + LEAVE + b"\x1b[c")  # CAN: the flush may cut
        self.assertAlmostEqual(fake.calls[-2][3], closing + fake.rtt)  # cooked mode once the sentinel's reply came
        self.assertFalse(fake.vt.alt_screen)
        self.assertEqual(signal.getsignal(signal.SIGALRM), signal.SIG_DFL)

    def test_a_terminal_that_goes_silent_after_the_probe_is_left_within_settle_max(self):
        fake, term = session(load("ghostty"))
        with term:
            term.pacer.sent(fake.now, 100, full=False)
            fake.responder.da1 = fake.responder.dsr = b""  # neither the ack nor the sentinel is answered
            closing = fake.now
        self.assertTrue(fake.output.endswith(b"\x1b[c"))
        self.assertAlmostEqual(fake.calls[-2][3], closing + tty.SETTLE_MAX)
        self.assertEqual(fake.attrs, term.saved)

    def test_a_terminal_that_takes_nothing_more_is_left_within_the_teardown_bounds(self):
        fake, term = session(load("ghostty"))
        with term:
            fake.room = 0
            term.ops = replace(term.ops, tcflush=lambda fd, queue: None)  # a flush that frees nothing
            closing = fake.now
        restored = [call for call in fake.calls if call[0] == "tcsetattr"][-1]
        self.assertAlmostEqual(restored[3], closing + 0.5)  # LEAVE gave up after 0.5 s: no sentinel to wait for
        self.assertEqual(fake.attrs, term.saved)
        self.assertLessEqual(0.5 + tty.SETTLE_MAX, 1.7)  # the worst case of 4.6, with the sentinel's wait

    def test_a_restart_after_a_reply_that_proves_one_lost_waits_for_no_reply(self):
        """DM1 of the S15 review: frames A and B given up on, A's reply lost, and a shrink that drops A (tagged
        outside the new size's tags). B's late reply then left owed one high, so a restart (a reprobe or a resume,
        paused or in the menu) waited its whole settle time, 1 s here, for a reply that would not come."""
        fake, term = session(load("xterm"), rtt=0.05)
        with term:
            pacer, t = term.pacer, fake.now
            pacer.sent(t, 100, full=False, tag=(22, 30))  # its reply is lost
            pacer.sent(t + 0.01, 100, full=False, tag=(10, 30))
            pacer.expire(t + 1.05)
            pacer.resized(80, 20)
            pacer.acked(t + 1.2, (10, 30))
            fake.now = t + 1.2
            self.assertEqual(pacer.owed, 0)
            before, start = len(fake.output), fake.now
            term.restart("reprobe")
            restarted = before + len(TITLE_POP)
            enter = next(when for name, _, at, when in fake.calls if name == "tcsetattr" and at == restarted)
            self.assertLess(enter - start, 0.1)

    def test_the_settle_wait_is_three_round_trips_within_its_bounds(self):
        fake, term = session(load("ghostty"))
        for srtt, wait in ((None, 0.15), (0.001, 0.15), (0.1, 0.3), (1.0, 1.0)):
            with self.subTest(srtt=srtt):
                term.pacer.srtt = srtt
                self.assertAlmostEqual(term.settle_time(), wait)
        self.assertEqual(tty.SETTLE_MAX, 1.0)

    def test_a_terminal_that_reads_nothing_at_startup_is_gone_after_two_seconds(self):
        fake, term = session(load("ghostty"))
        fake.room = 0  # ENTER never goes out
        with self.assertRaises(TerminalGone) as gone, term:
            pass
        self.assertEqual(str(gone.exception), "terminal stopped reading")
        self.assertAlmostEqual(fake.now, 2.0)

    def test_a_status_write_that_fails_means_the_terminal_is_gone(self):
        fake, term = session(load("ghostty"))
        fake.write_error = errno.EIO  # ENTER cannot go out
        with self.assertRaises(TerminalGone) as gone, term:
            pass
        self.assertIn("Input/output error", str(gone.exception))
        self.assertEqual(fake.attrs, term.saved)

    def test_an_output_flush_that_fails_is_logged_and_teardown_goes_on(self):
        fake, term = session(load("ghostty"))
        flush = fake.tcflush

        def tcflush(fd, queue):
            if queue == termios.TCOFLUSH:
                raise termios.error(errno.ENOTTY, "Inappropriate ioctl for device")
            flush(fd, queue)

        with self.assertLogs(level="DEBUG") as logs, term:
            term.ops = replace(term.ops, tcflush=tcflush)
        self.assertIn("Output flush failed", "\n".join(logs.output))
        self.assertFalse(fake.vt.alt_screen)
        self.assertEqual(fake.attrs, term.saved)

    def test_input_that_ends_during_the_sentinel_wait_ends_it(self):
        for result in (b"", OSError(errno.EIO, "EIO")):
            with self.subTest(result=result):
                fake, term = session(load("ghostty"))
                with term:
                    fake.type(b"x")
                    term.ops = replace(fake.ops(), read=Mock(side_effect=[result]))
                    closing = fake.now
                self.assertEqual(fake.calls[-2][:2], ("tcsetattr", termios.TCSANOW))
                self.assertEqual(fake.calls[-2][3], closing)  # no wait for a reply that cannot come
                self.assertEqual(fake.attrs, term.saved)

    def test_termios_calls_interrupted_by_a_signal_are_made_again(self):
        fake, term = session(load("xterm"))
        failures = [termios.error(errno.EINTR, "Interrupted system call")] * 2

        def tcsetattr(fd, when, attrs):
            if failures:
                raise failures.pop()  # the SIGCONT of fg ends a tcsetattr stopped by SIGTTOU
            fake.tcsetattr(fd, when, attrs)

        term.ops = replace(term.ops, tcsetattr=tcsetattr)
        with term:
            self.assertEqual(fake.attrs, raw_attrs(term.saved, fake.fd))
            failures.append(termios.error(errno.EINTR, "Interrupted system call"))
        self.assertEqual(fake.attrs, term.saved)
        fake, term = session(load("xterm"))
        term.ops = replace(term.ops, tcsetattr=Mock(side_effect=termios.error(errno.EIO, "EIO")))
        with self.assertRaises(TerminalGone), term:
            pass

    def test_a_push_interrupted_during_its_verification_is_still_popped(self):
        fake, term = session(load("kitty"), kitty=None, rtt=0.01)

        def escalate():
            if b"\x1b[>11u" in fake.output:
                raise KeyboardInterrupt  # a second ^C while the verification is on its way

        fake.at(0.015, escalate)
        with self.assertRaises(KeyboardInterrupt), term:
            pass
        self.assertIn(b"\x1b[>11u\x1b[?u\x1b[c", fake.output)
        self.assertEqual(fake.vt.kitty, {"main": [], "alt": []})
        self.assertFalse(fake.vt.alt_screen)

    def test_reads_turn_a_hangup_into_terminal_gone(self):
        fake, term = session(load("xterm"))
        with term:
            fake.type(b"ab")
            self.assertEqual(term.read(1), b"a")
            for result, error in ((b"", TerminalGone), (OSError(errno.EIO, "EIO"), TerminalGone), (OSError(), OSError)):
                term.ops = replace(fake.ops(), read=Mock(side_effect=[result]))
                self.assertRaises(error, term.read, 1)
            term.ops = fake.ops()

    def test_a_terminal_that_stops_reading_at_startup_is_gone(self):
        fake, term = session(load("xterm"))
        fake.room = 10
        with self.assertRaisesRegex(TerminalGone, "stopped reading"):
            with term:
                pass
        self.assertTrue(term.closed)
        self.assertEqual(signal.getsignal(signal.SIGWINCH), signal.SIG_DFL)
        fake, term = session(load("xterm"))
        fake.dead = True
        with self.assertRaises(TerminalGone), term:
            pass

    def test_size_falls_back_to_the_environment_then_80_by_24(self):
        fake, term = session(load("xterm"), cols=100, rows=40)
        self.assertEqual(term.size(), (100, 40))
        term.ops = replace(term.ops, winsize=lambda fd: None)
        with patch.dict(os.environ, {"COLUMNS": "120", "LINES": "50"}):
            self.assertEqual(term.size(), (120, 50))
        with patch.dict(os.environ, {"COLUMNS": "wide"}):
            self.assertEqual(term.size(), (80, 24))


class SignalTests(unittest.TestCase):
    def test_signals_are_recorded_and_acted_on_at_the_next_check(self):
        fake, term = session(load("xterm"))
        with term:
            os.kill(os.getpid(), signal.SIGWINCH)
            os.kill(os.getpid(), signal.SIGWINCH)
            self.assertEqual(term.pending, {signal.SIGWINCH})
            self.assertTrue(os.read(term.wakeup, 16))  # the wakeup pipe got the signal numbers
            term.resized = False
            term.check()
            self.assertTrue(term.resized)
            self.assertEqual(term.pending, set())
            term.check()

    def test_fatal_signals_raise_their_exit(self):
        fake, term = session(load("xterm"))
        with term:
            for signum, error in ((signal.SIGINT, KeyboardInterrupt), (signal.SIGHUP, TerminalGone)):
                term.pending = {signum, signal.SIGWINCH}
                self.assertRaises(error, term.check)
            for signum, code in ((signal.SIGQUIT, 131), (signal.SIGTERM, 143)):
                term.pending = {signum}
                with self.assertRaises(SystemExit) as raised:
                    term.check()
                self.assertEqual(raised.exception.code, code)

    def test_a_second_fatal_signal_gives_up_at_once(self):
        fake, term = session(load("xterm"))
        with term:
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertEqual(term.pending, {signal.SIGTERM})
            with self.assertRaises(SystemExit) as raised:
                os.kill(os.getpid(), signal.SIGTERM)
            self.assertEqual(raised.exception.code, 143)
            with self.assertRaises(KeyboardInterrupt):
                term._handler(signal.SIGINT, None)
            term.pending.clear()
        self.assertFalse(fake.vt.alt_screen)

    def test_close_restores_handlers_and_delivers_what_arrived_meanwhile_afterwards(self):
        received = []
        old = signal.signal(signal.SIGTERM, lambda signum, frame: received.append(fake.vt.alt_screen))
        try:
            fake, term = session(load("xterm"))
            with patch.object(tty.atexit, "register") as registered, patch.object(tty.atexit, "unregister") as dropped:
                with term:
                    fake.at(fake.now, lambda: os.kill(os.getpid(), signal.SIGTERM))  # runs during the teardown
                    self.assertEqual(signal.getsignal(signal.SIGTERM), term._handler)
                term.close()  # idempotent
            registered.assert_called_once_with(term.close)
            dropped.assert_called_once_with(term.close)
            self.assertEqual(received, [False])  # delivered once, to the old handler, on the main screen
            self.assertEqual(signal.getsignal(signal.SIGWINCH), signal.SIG_DFL)
        finally:
            signal.signal(signal.SIGTERM, old)

    def test_suspend_restores_stops_the_job_and_reenters_paused(self):
        fake, term = session(load("ghostty"))
        hooks = Hooks()
        stops = []

        def killpg(group, signum):
            stops.append((group, signum, signal.getsignal(signal.SIGTSTP), fake.vt.alt_screen, fake.attrs))
            os.kill(os.getpid(), signal.SIGCONT)  # what the shell's fg sends

        with term, patch.object(tty.os, "killpg", killpg):
            term.hooks = hooks
            os.kill(os.getpid(), signal.SIGTSTP)
            term.check()
            self.assertEqual(stops, [(os.getpgrp(), signal.SIGTSTP, signal.SIG_DFL, False, term.saved)])
            self.assertEqual(hooks.calls, ["before_suspend", ("after_resume", "ghostty", "suspended")])
            self.assertEqual(signal.getsignal(signal.SIGTSTP), term._handler)
            self.assertEqual(term.pending, set())  # our own SIGCONT is no spurious resume
            self.assertEqual(fake.output.count(ENTER), 2)
            self.assertTrue(fake.vt.alt_screen)
            self.assertEqual(fake.attrs, raw_attrs(term.saved, fake.fd))

    def test_kill_on_the_stopped_job_exits_without_entering_again(self):
        fake, term = session(load("ghostty"))

        def killpg(group, signum):  # bash's kill %1 while stopped: SIGTERM, then SIGCONT, still in the background
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGCONT)

        with self.assertRaises(SystemExit) as raised, term, patch.object(tty.os, "killpg", killpg):
            os.kill(os.getpid(), signal.SIGTSTP)
            term.check()
        self.assertEqual(raised.exception.code, 143)
        self.assertEqual(fake.output.count(ENTER), 1)  # a tcsetattr in the background would stop it again
        self.assertEqual(fake.attrs, term.saved)

    def test_a_spurious_sigcont_cancels_the_cut_frame_and_reprobes_without_echoed_queries(self):
        fake, term = session(load("ssh-apple-terminal"))
        hooks = Hooks()
        with term:
            term.hooks = hooks
            term.kitty_depth = 1
            fake.room = 5
            term.out.push(b"\x1b[1;1Hpartial", frame=True)
            term.out.flush_some()
            fake.room = None
            before = len(fake.output)
            os.kill(os.getpid(), signal.SIGCONT)
            term.check()
            again = fake.output[before:]
            self.assertTrue(again.startswith(b"\x18\x1b[<u" + TITLE_POP + ENTER))
            self.assertIn(probe_bytes(term.env, frozenset({"kitty", "decrqm", "xtversion"})), again)
            self.assertNotIn(b"\x1b[?u", again)
            self.assertEqual(hooks.calls, [("after_resume", "unknown", "resumed")])
            self.assertEqual(term.kitty_depth, 0)
            self.assertTrue(term.out.empty())  # the rest of the cut frame never follows the new probe
            after = len(fake.output)
            term.out.drain(1.0)
            self.assertNotIn(b"partial", fake.output[after:])


class RealPtyTests(unittest.TestCase):
    def test_a_session_on_a_pty_restores_termios_and_never_touches_file_status_flags(self):
        master, slave = os.openpty()
        stub = Stub(master, load("ghostty"))  # S5: the stub always keeps a MiniVT
        stub.start()
        try:
            name = os.ttyname(slave)
            fd = os.open(name, os.O_RDWR | os.O_NOCTTY)
            flags = {n: fcntl.fcntl(n, fcntl.F_GETFL) & ~WAS_WRITTEN for n in (0, 1, fd)}
            before = termios.tcgetattr(slave)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            with TerminalSession(fd, EnvHints.from_env(load("ghostty")["env"]), Overrides(pacing="fixed")) as term:
                self.assertEqual(term.size(), (80, 24))
                self.assertEqual(term.caps.family, "ghostty")
                self.assertEqual(FdOps().winsize(fd), (80, 24))
            self.assertEqual({n: fcntl.fcntl(n, fcntl.F_GETFL) & ~WAS_WRITTEN for n in (0, 1, fd)}, flags)
            self.assertEqual(termios.tcgetattr(slave), before)
            os.close(fd)
            read, write = os.pipe()
            self.assertIsNone(tty.winsize(read))  # not a terminal
            os.close(read)
            os.close(write)
        finally:
            stub.stop()
            os.close(master)
            os.close(slave)
        self.assertIsNone(stub.error)
        self.assertFalse(stub.vt.alt_screen)


if __name__ == "__main__":
    unittest.main()
