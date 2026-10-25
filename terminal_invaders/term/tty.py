"""Owning the terminal: explicit termios, mode sequences, signals, suspend and resume, and a quiet teardown.

The game reads and writes one fd of its own on the controlling terminal. It is never made O_NONBLOCK and never a
dup of fd 0 or 1, so nothing the game does to it reaches the shell. Teardown waits for a sentinel reply while
still raw, so late probe or ack replies are read and discarded instead of echoed into the shell.
"""

from __future__ import annotations

import atexit
import errno
import fcntl
import logging
import os
import select
import signal
import struct
import sys
import termios
import time
from dataclasses import dataclass

from .caps import TITLE_STACK_FAMILIES, Capabilities, EnvHints, Overrides, resolve
from .decode import Decoder
from .keys import Key, Reply
from .output import DEAD, OutputQueue, on_alarm, timed_write, write_all, write_bounded
from .pacing import AckPacer
from .probe import CLEAR, echoed, push_kitty, read_until, run_probe, timeout

IFLAG, OFLAG, CFLAG, LFLAG, ISPEED, OSPEED, CC = range(7)
ENTER = (
    b"\x1b[?1049h"  # alt screen (saves the cursor)
    b"\x1b[22;0;0t"  # push the window and icon titles
    b"\x1b(B"  # G0 = ASCII
    b"\x1b[0m"  # SGR reset
    b"\x1b[4l"  # IRM off (replace mode)
    b"\x1b[?6l"  # DECOM off
    b"\x1b[r"  # full scroll region
    b"\x1b[?25l"  # hide the cursor
    b"\x1b[?7l"  # autowrap off
    b"\x1b[?1004h"  # focus reports
    b"\x1b[?2004h"  # bracketed paste
    b"\x1b[H\x1b[2J"
)
TITLE = b"\x1b]2;Terminal Invaders\x1b\\"  # only for TITLE_STACK_FAMILIES, whose title the LEAVE pop restores
TITLE_POP = b"\x1b[23;0;0t"  # what ENTER's title push needs before ENTER is sent again
ESU = b"\x1b[?2026l"
FATAL = {signal.SIGINT: 130, signal.SIGQUIT: 131, signal.SIGTERM: 143, signal.SIGHUP: 129}  # signal -> exit code
WATCHED = (signal.SIGWINCH, signal.SIGTSTP, signal.SIGCONT, *FATAL)
NO_TTY = frozenset((errno.ENXIO, errno.ENOENT, errno.EIO, errno.ENODEV))  # no controlling terminal
SETTLE_MAX = 1.0  # the longest wait for replies in flight (spec 4.6)


class TerminalGone(Exception):
    """The terminal hung up: SIGHUP, or EIO/ENXIO on the tty. The game exits 129."""


def winsize(fd: int) -> tuple[int, int] | None:
    """(columns, rows) from TIOCGWINSZ; None when the ioctl fails or reports no size."""
    try:
        rows, cols = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8)))[:2]
    except OSError:
        return None
    return (cols, rows) if cols and rows else None


@dataclass(frozen=True)
class FdOps:
    """The calls a TerminalSession makes on its fd; tests substitute an in-memory terminal."""

    read: object = os.read
    write: object = timed_write
    select: object = select.select
    clock: object = time.monotonic
    tcgetattr: object = termios.tcgetattr
    tcsetattr: object = termios.tcsetattr
    tcflush: object = termios.tcflush
    winsize: object = winsize

    def writes(self) -> dict:
        """Keyword arguments for write_all and write_bounded."""
        return dict(clock=self.clock, write=self.write, select=self.select)

    def io(self) -> dict:
        """Keyword arguments for run_probe and push_kitty."""
        return dict(self.writes(), read=self.read)


def open_tty(stdin_fd: int = 0) -> int:
    """A new open file description on the terminal: /dev/tty, else ttyname(stdin_fd) afresh; never a dup()."""
    flags = os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC
    try:
        return os.open("/dev/tty", flags)
    except OSError as exc:
        if exc.errno not in NO_TTY:
            raise ValueError(f"no terminal: {exc}") from exc
    try:
        return os.open(os.ttyname(stdin_fd), flags)
    except OSError as exc:
        raise ValueError("no terminal") from exc


def vdisable(fd: int) -> int:
    """The character value that disables a control character (_POSIX_VDISABLE)."""
    try:
        value = os.fpathconf(fd, "PC_VDISABLE")
    except (OSError, ValueError):
        value = -1
    return value if 0 <= value <= 255 else 0xFF if sys.platform == "darwin" else 0


def raw_attrs(saved: list, fd: int) -> list:
    """Raw input with signals kept (^C ^Z ^\\), output processing untouched, ^Y ^T ^O ^V disabled."""
    attrs = list(saved)
    attrs[CC] = list(saved[CC])
    off = ("BRKINT", "ICRNL", "INLCR", "IGNCR", "INPCK", "ISTRIP", "IXON", "IXOFF", "IXANY", "PARMRK")
    for name in off:
        attrs[IFLAG] &= ~getattr(termios, name, 0)
    attrs[LFLAG] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.IEXTEN)
    attrs[LFLAG] |= termios.ISIG | termios.NOFLSH  # NOFLSH: a signal never flushes queued output
    attrs[CC][termios.VMIN], attrs[CC][termios.VTIME] = 1, 0  # reads happen only after select() says readable
    disabled = bytes([vdisable(fd)])
    for name in ("VDSUSP", "VSTATUS", "VDISCARD", "VLNEXT"):
        if hasattr(termios, name):
            attrs[CC][getattr(termios, name)] = disabled
    return attrs


def unwind_bytes(*, cut: bool, sync: bool, kitty_depth: int) -> bytes:
    """End a half-sent frame and pop the kitty flags, as both LEAVE and a restart begin."""
    return (
        (b"\x18" if cut else b"")  # CAN aborts a half-sent escape sequence
        + (ESU if sync else b"")
        + b"\x1b[<u" * kitty_depth  # popped on the alt screen's own stack (kitty keeps one per screen)
    )


def leave_bytes(*, cut: bool, sync: bool, kitty_depth: int) -> bytes:
    return (
        unwind_bytes(cut=cut, sync=sync, kitty_depth=kitty_depth)
        + b"\x1b[0m\x1b[?2004l\x1b[?1004l\x1b[?7h\x1b[?25h\x1b[?1049l"
        + TITLE_POP
    )


def retried(call, *args):
    """A termios call, made again after EINTR, which Python does not retry for termios (PEP 475): a background job
    stops in tcsetattr (SIGTTOU), and the SIGCONT of bg or fg interrupts it."""
    while True:
        try:
            return call(*args)
        except termios.error as exc:
            if exc.args[0] != errno.EINTR:
                raise


def read_and_discard_until(fd: int, kind: str, seconds: float, ops: FdOps, count: int = 1) -> bool:
    """Read and drop input until count replies of this kind (da1, dsr or cpr) arrived; False on timeout or hangup.
    A CPR on row 1 is one of them too: there a terminal shrunk to one row clamps them, and keys are dropped anyway."""

    def done(events: list) -> bool:
        return sum(isinstance(event, Reply) and event.kind == kind for event in events) >= count

    decoder = Decoder(esc_timeout=seconds, string_timeout=seconds)
    decoder.clamped = count if kind == "cpr" else 0
    return read_until(fd, seconds, decoder, done, clock=ops.clock, read=ops.read, select=ops.select)[2] is not None


class TerminalSession:
    """Raw mode, the alt screen and the probe for the duration of a with block; every exit path restores.

    hooks (set once the game exists) gets before_suspend() and after_resume(caps, reason). reprobe is set when the
    terminal stopped sending kitty key reports (the game, at a downgrade), or when the last probe ran on one row and
    the screen is taller now (enter(), check()); the loop then calls restart("reprobe") at a pause, and after a small
    probe anywhere outside play or before play draws its first frame.
    """

    def __init__(self, fd: int, env: EnvHints, overrides: Overrides, keymap=None, *, ops: FdOps = FdOps()):
        self.fd, self.env, self.overrides, self.keymap, self.ops = fd, env, overrides, keymap, ops
        self.hooks = None
        self.out = OutputQueue(self.fd, self.ops.clock, write=self.ops.write, select=self.ops.select)
        self.saved = self.caps = self.replies = self.push_reply = None  # the last probe's, for --probe --raw
        self.pacer = AckPacer("off", clock=self.ops.clock)
        self.kitty_depth, self.entered, self.closed, self.reprobe = 0, False, False, False
        self.pending, self.fatal_seen, self.own_cont, self.resized = set(), False, False, False
        self.small_probe = False  # the last probe ran on one row
        self.early_keys: list[Key] = []
        self.wakeup = self._wakeup_w = -1
        self.handlers, self.old_wakeup = {}, -1

    def __enter__(self) -> TerminalSession:
        try:
            self.open()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        try:
            self.saved = self.ops.tcgetattr(self.fd)  # captured once; resume never re-captures
        except (OSError, termios.error) as exc:
            raise TerminalGone(str(exc)) from exc
        self.wakeup, self._wakeup_w = os.pipe()
        os.set_blocking(self._wakeup_w, False)  # set_wakeup_fd needs it; the only O_NONBLOCK, and not a tty
        self.old_wakeup = signal.set_wakeup_fd(self._wakeup_w, warn_on_full_buffer=False)
        self.handlers = {signum: signal.signal(signum, self._handler) for signum in WATCHED}
        self.handlers[signal.SIGALRM] = signal.signal(signal.SIGALRM, on_alarm)  # cuts a blocked write short
        atexit.register(self.close)
        self.enter()

    def enter(self, skip: frozenset[str] = frozenset()) -> None:
        """Raw mode, ENTER, the probe and its clean-up, the kitty push, then the title and a pacer."""
        try:
            retried(self.ops.tcsetattr, self.fd, termios.TCSANOW, raw_attrs(self.saved, self.fd))
            retried(self.ops.tcflush, self.fd, termios.TCIFLUSH)
        except (OSError, termios.error) as exc:
            raise TerminalGone(str(exc)) from exc
        self.pending.discard(signal.SIGCONT)  # one that arrived until now (bg, fg) is answered by this enter
        self.entered = True
        self.write(ENTER)
        seconds, io = timeout(self.env), self.ops.io()
        probe_rows = self.size()[1]
        replies = run_probe(self.fd, self.env, skip=skip, **io)
        self.write(CLEAR)
        self.kitty_depth, flags, self.push_reply = push_kitty(
            self.fd, replies, self.keymap, self.overrides, seconds, pushed=self._pushed, **io
        )
        self.replies = replies
        # On one row the probe's CUPs to rows 2 and 3 clamp onto row 1, and its CPRs read as F3 and Shift+F3: no CPR
        # echo, so DSR acks matched in order. Its F3 keys are dropped, and a taller screen asks again (check()):
        # outside play, or before play draws a frame, so at once if it grew while the probe ran; in play at a pause.
        rows = self.size()[1]
        self.small_probe = min(probe_rows, rows) < 2
        self.caps = resolve(self.env, replies, self.overrides, kitty_verified=flags)
        if self.caps.family in TITLE_STACK_FAMILIES:
            self.write(TITLE)
        mode, tagged = self.overrides.pacing if self.caps.ack else "off", self.caps.ack == "cpr"
        old, self.pacer = self.pacer, AckPacer(mode, tagged=tagged, clock=self.ops.clock)
        self.pacer.inherit(old)  # a reply to a frame from before the restart names that frame, not a new one
        self.early_keys += [key for key in replies.early_keys if not (self.small_probe and key.name == "f3")]
        self.resized, self.reprobe = True, self.small_probe and rows >= 2
        logging.debug("Terminal: %s", self.caps.row())

    def _pushed(self) -> None:
        self.kitty_depth = 1  # counted once written: a ^C that escalates during the verification still pops it

    def write(self, data: bytes) -> None:
        """A status write (modes, title), outside the frame queue; a stalled or vanished terminal is gone."""
        try:
            done = write_all(self.fd, data, self.ops.clock() + 2.0, **self.ops.writes())
        except OSError as exc:
            raise TerminalGone(str(exc)) from exc
        if not done:
            raise TerminalGone("terminal stopped reading")

    def read(self, n: int) -> bytes:
        try:
            data = self.ops.read(self.fd, n)
        except OSError as exc:
            if exc.errno in DEAD:
                raise TerminalGone(str(exc)) from exc
            raise
        if not data:
            raise TerminalGone("end of input")
        return data

    def size(self) -> tuple[int, int]:
        """(columns, rows): TIOCGWINSZ on the tty, else COLUMNS and LINES, else 80 x 24."""
        size = self.ops.winsize(self.fd)
        if size:
            return size
        try:
            return int(os.environ["COLUMNS"]), int(os.environ["LINES"])
        except (KeyError, ValueError):
            return 80, 24

    def drain_wakeup(self) -> None:
        os.read(self.wakeup, 512)  # signal numbers; the handler already recorded them

    # -- signals ---------------------------------------------------------------------------------------------
    def _handler(self, signum, frame) -> None:
        if signum in FATAL and self.fatal_seen:  # a second fatal signal: the loop is stuck, so go now
            raise KeyboardInterrupt if signum == signal.SIGINT else SystemExit(FATAL[signum])
        self.fatal_seen |= signum in FATAL
        if signum == signal.SIGCONT and self.own_cont:
            return  # the continue after our own stop
        self.pending.add(signum)

    def check(self) -> None:
        """Act on the signals recorded since the last call (top of every loop iteration)."""
        if not self.pending:
            return
        pending, self.pending = self.pending, set()
        self.fatal(pending)
        if signal.SIGTSTP in pending:
            self.suspend()
        elif signal.SIGCONT in pending:
            self.resume_spurious()
        if signal.SIGWINCH in pending:
            self.resized = True  # however many arrived: one relayout
            self.reprobe |= self.small_probe and self.size()[1] >= 2

    def size_changed(self) -> bool:
        """A SIGWINCH arrived that check() has not handled yet."""
        return signal.SIGWINCH in self.pending

    @staticmethod
    def fatal(pending) -> None:
        """Raise the exit of a fatal signal among these (130, 131, 143, 129 as TerminalGone)."""
        for signum, code in FATAL.items():
            if signum in pending:
                if signum == signal.SIGINT:
                    raise KeyboardInterrupt
                if signum == signal.SIGHUP:
                    raise TerminalGone("hangup")
                raise SystemExit(code)

    # -- suspend, resume and teardown --------------------------------------------------------------------------
    def suspend(self) -> None:
        """^Z: pause the game, restore the terminal, stop the whole job as a shell's ^Z would, re-enter on SIGCONT."""
        if self.hooks:
            self.hooks.before_suspend()
        self.quiesce()
        old = signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTSTP})
        self.own_cont = True
        try:
            os.killpg(os.getpgrp(), signal.SIGTSTP)  # returns once continued (at once in an orphaned group)
            signal.signal(signal.SIGTSTP, old)
            self.pending.discard(signal.SIGCONT)
            self.fatal(self.pending)  # kill %1 sends SIGTERM, then SIGCONT, to a job left in the background
            self.reenter("suspended")
        finally:
            self.own_cont = False

    def resume_spurious(self) -> None:
        """SIGCONT without our own suspend (an external SIGSTOP, bg/fg): set everything up again."""
        self.restart("resumed")  # ?1049h is harmless if still on the alt screen

    def restart(self, reason: str) -> None:
        """Set everything up again while running: end a half-sent frame and pop the kitty flags, let the acks still
        on their way arrive (one could end the new probe early), then re-enter."""
        cut, sync = self.out.mid_frame, bool(self.caps and self.caps.sync)
        owed = self.owed(unsent=not self.out.empty())
        self.out.discard()
        tail = unwind_bytes(cut=cut, sync=sync, kitty_depth=self.kitty_depth) + TITLE_POP
        if write_bounded(self.fd, tail, 0.5, **self.ops.writes()) and owed:
            read_and_discard_until(self.fd, self.caps.ack, self.settle_time(), self.ops, owed)
        self.kitty_depth = 0
        self.reenter(reason)

    def reenter(self, reason: str) -> None:
        """The full probe again (tmux may be attached elsewhere now); the queries an echoing terminal printed are
        skipped."""
        skip = echoed(self.caps.answered) if self.caps and self.caps.echoes_queries else frozenset()
        self.enter(skip)
        if self.hooks:
            self.hooks.after_resume(self.caps, reason)

    def quiesce(self) -> None:
        """Drop the output not yet read (a frame waiting on a slow terminal is worth nothing now), LEAVE with CAN
        first (the flush may cut a sequence the terminal has begun), then read until the sentinel reply, then cooked
        mode. Every write is small, so a slow or stalled terminal still gets LEAVE at once."""
        unsent = not self.out.empty()  # the newest frame's ack query, at its end, never went out
        self.out.discard()
        try:
            retried(self.ops.tcflush, self.fd, termios.TCOFLUSH)
        except termios.error:
            logging.debug("Output flush failed", exc_info=True)
        caps: Capabilities | None = self.caps
        seq = leave_bytes(cut=True, sync=bool(caps and caps.sync), kitty_depth=self.kitty_depth)
        self.kitty_depth = 0
        answered = caps.answered if caps else frozenset()
        sentinel = b"\x1b[c" if "da1" in answered else b"\x1b[5n" if "dsr" in answered else b""
        if self.out.dead:
            sentinel = b""
        sent = write_bounded(self.fd, seq + sentinel, 0.5, **self.ops.writes())
        if sentinel and sent:  # still raw: late replies are read, not echoed
            kind = "da1" if sentinel == b"\x1b[c" else "dsr"
            owed = self.owed(unsent=unsent) if caps.ack == kind else 0
            # The whole SETTLE_MAX, not settle_time(): a Linux pty keeps up to 4 KB the terminal has not read past the
            # flush, and without acks nothing says how fast it reads. The wait ends at the reply anyway.
            read_and_discard_until(self.fd, kind, SETTLE_MAX, self.ops, owed + 1)  # acks in flight come first
        self.entered = False
        retried(self.ops.tcsetattr, self.fd, termios.TCSANOW, self.saved)
        retried(self.ops.tcflush, self.fd, termios.TCIFLUSH)

    def owed(self, *, unsent: bool) -> int:
        """Ack replies still to come: every query sent and not answered, less the newest frame's when that frame
        (and so its query, which ends it) never went out in full."""
        return max(0, self.pacer.owed - int(unsent)) if self.caps and self.caps.ack else 0

    def settle_time(self) -> float:
        """How long a restart waits for the acks in flight: three round trips, within 0.15 to 1 s. A pacer new since
        the last restart has measured none yet, but the probe that restart ran has (RST-settle-srtt-reset of the S17
        review: a second restart soon after the first stopped waiting after 0.15 s, and the replies owed came into
        its probe, where one read on a row-one flap was an F3 key)."""
        rtt = self.pacer.srtt or (self.replies.rtt if self.replies else None) or 0.05
        return min(SETTLE_MAX, max(0.15, 3 * rtt))

    def close(self) -> None:
        """Idempotent. Signals stay blocked while the terminal is restored, then reach their old handlers."""
        if self.closed:
            return
        self.closed = True
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, WATCHED)
        try:
            if self.entered:
                try:
                    self.quiesce()
                except (OSError, termios.error):
                    logging.debug("Terminal restore failed", exc_info=True)
        finally:
            for signum, handler in self.handlers.items():
                signal.signal(signum, handler)
            if self.wakeup >= 0:
                signal.set_wakeup_fd(self.old_wakeup)
                os.close(self.wakeup)
                os.close(self._wakeup_w)
            atexit.unregister(self.close)
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
