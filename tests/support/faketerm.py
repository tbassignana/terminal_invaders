"""FakeTerminal: an in-memory tty on a virtual clock, and the Responder that answers like a fixture's terminal.

Everything the game writes goes through MiniVT, so a sequence it does not know fails the test. The Responder
reads the queries MiniVT recorded and answers as the fixture's terminal would: the whole probe batch at its DA1,
the kitty push verification, acks and teardown sentinels. ptyrun's terminal stub uses the same Responder.
"""

import errno
import math
import os
import re
import select
import signal
import termios

from terminal_invaders.term.encode import TAG_FIRST
from terminal_invaders.term.tty import FdOps
from tests.support.vt import MiniVT

DA1 = re.compile(rb"\x1b\[\?[\d;]*c")
CPR = re.compile(rb"\x1b\[\d+;\d+R")
FD = 1000  # never a real descriptor in a test process
FLUSHED_ROOM = 4096  # the room a stalled terminal has again once its unread output is flushed (TCOFLUSH)


class Responder:
    """Replies to the queries MiniVT saw, in order. A DA1 closes a batch: after CPRs it was the probe, after a
    kitty flags query the push verification, and otherwise an ack or the teardown sentinel. A CPR from row TAG_FIRST
    down outside a batch is a tagged ack, answered with the cursor's position (cprs: MiniVT's, at each CPR)."""

    def __init__(self, doc: dict | None):
        self.replies = doc["replies"].encode() if doc else b""
        self.push = (doc.get("push_reply") or "").encode() if doc else b""
        match = DA1.search(self.replies)
        self.da1 = match.group() if match else b""
        self.dsr = b"\x1b[0n" if b"\x1b[0n" in self.replies else b""
        self.cpr = bool(CPR.search(self.replies))
        self.seen, self.batch, self.cprs_seen = 0, [], 0

    def answer(self, queries: list[str], cprs: list[tuple[int, int]] = ()) -> bytes:
        out = b""
        for query in queries[self.seen :]:
            if query == "cpr":
                at = cprs[self.cprs_seen] if self.cprs_seen < len(cprs) else None
                self.cprs_seen += 1
                if at and at[0] >= TAG_FIRST and not self.batch:
                    out += b"\x1b[%d;%dR" % at if self.cpr else b""  # an ack
                    continue
            if query == "da1":
                batch, self.batch = self.batch, []
                out += self.replies if "cpr" in batch else self.push if "kitty" in batch else self.da1
            elif query == "dsr" and not self.batch:
                out += self.dsr  # an ack
            else:
                self.batch.append(query)
        self.seen = len(queries)
        return out


class SizedResponder(Responder):
    """A Responder whose terminal is MiniVT's size: every CPR, the probe's too, is answered where the cursor was, so
    on a screen of fewer rows than the probe's CUPs reach its replies come back clamped (from the S14 review)."""

    def __init__(self, doc: dict | None):
        super().__init__(doc)
        self.probe_at: list[tuple[int, int] | None] = []

    def answer(self, queries: list[str], cprs: list[tuple[int, int]] = ()) -> bytes:
        out = b""
        for query in queries[self.seen :]:
            if query == "cpr":
                at = cprs[self.cprs_seen] if self.cprs_seen < len(cprs) else None
                self.cprs_seen += 1
                if at and not self.batch:
                    out += b"\x1b[%d;%dR" % at  # an ack, on whatever row
                    continue
                self.probe_at.append(at)
            if query == "da1":
                batch, self.batch = self.batch, []
                if "cpr" in batch:
                    where = iter(self.probe_at)
                    out += CPR.sub(lambda match: b"\x1b[%d;%dR" % next(where), self.replies)
                    self.probe_at = []
                else:
                    out += self.push if "kitty" in batch else self.da1
            elif query == "dsr" and not self.batch:
                out += self.dsr
            else:
                self.batch.append(query)
        self.seen = len(queries)
        return out


class FakeTerminal:
    """A tty in memory. Tests type bytes, schedule actions, resize it, stall its output (room) or hang it up; the
    clock moves only inside select(), to the next input, scheduled action or timeout."""

    def __init__(self, doc: dict | None = None, *, cols=80, rows=24, rtt=0.001, vt=None, limit=120.0):
        self.fd, self.now, self.rtt, self.limit = FD, 0.0, rtt, limit
        self.cols, self.rows = cols, rows
        self.vt = MiniVT(*(vt or (cols, rows)), on_esu=self._esu)
        self.screen: list[str] = []  # MiniVT's text at the last ESU: what the last whole frame showed
        self.responder = Responder(doc)
        self.inbox: list[tuple[float, bytes]] = []  # input for the game, in time order
        self.actions: list[tuple[float, object]] = []
        self.output = bytearray()
        self.attrs = [
            termios.ICRNL | termios.IXON,
            termios.OPOST | termios.ONLCR,
            termios.CS8 | termios.CREAD,
            termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN,
            termios.B38400,
            termios.B38400,
            [b"\x00"] * termios.NCCS,
        ]
        self.calls: list[tuple] = []  # termios calls in order: (name, argument, output length, time)
        self.selects: list[tuple[list, list, float | None]] = []  # every select(): (read fds, write fds, timeout)
        self.room, self.write_limit, self.dead = None, None, False  # room: bytes it takes before it stalls
        # gate: a callable(target), called before select() moves the clock to target: the latest time the clock may
        # move to now, or None once it has held the clock still (blocking until it allows more), when select() looks
        # again at what came meanwhile. tools/playtest.py freezes the game between commands this way.
        self.gate = None
        self.write_error: int | None = None  # the errno every write fails with, while reads still work

    def ops(self) -> FdOps:
        return FdOps(
            read=self.read,
            write=self.write,
            select=self.select,
            clock=self.clock,
            tcgetattr=self.tcgetattr,
            tcsetattr=self.tcsetattr,
            tcflush=self.tcflush,
            winsize=self.winsize,
        )

    # -- what tests do ----------------------------------------------------------------------------------------
    def type(self, data: bytes, at: float | None = None) -> None:
        self.inbox.append((self.now if at is None else at, bytes(data)))
        self.inbox.sort(key=lambda item: item[0])

    def at(self, when: float, action) -> None:
        self.actions.append((when, action))
        self.actions.sort(key=lambda item: item[0])

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        os.kill(os.getpid(), signal.SIGWINCH)

    def text(self) -> str:
        return "\n".join(self.vt.lines())

    # -- the fd operations ------------------------------------------------------------------------------------
    def clock(self) -> float:
        return self.now

    def read(self, fd: int, n: int) -> bytes:
        self._check(fd)
        ready = b"".join(data for when, data in self.inbox if when <= self.now)
        assert ready, "read() with no input would block"
        self.inbox = [(when, data) for when, data in self.inbox if when > self.now]
        if len(ready) > n:
            self.inbox.insert(0, (self.now, ready[n:]))
        return ready[:n]

    @property
    def writable(self) -> bool:
        return self.room is None or self.room > 0

    def write(self, fd: int, data) -> int:
        self._check(fd)
        if self.write_error:
            raise OSError(self.write_error, os.strerror(self.write_error))
        assert self.writable, "write() on a stalled terminal would block"
        data = bytes(data[: min(self.write_limit or len(data), len(data) if self.room is None else self.room)])
        if self.room is not None:
            self.room -= len(data)
        self.output += data
        self.vt.feed(data)
        reply = self.responder.answer(self.vt.queries, self.vt.cprs)
        if reply:
            self.type(reply, self.now + self.rtt)
        return len(data)

    def select(self, r, w, x, timeout):
        self.selects.append((list(r), list(w), timeout))
        end = math.inf if timeout is None else self.now + timeout
        real = [fd for fd in r if fd != self.fd]
        while True:
            while self.actions and self.actions[0][0] <= self.now:
                self.actions.pop(0)[1]()
            readable = [self.fd] if self.fd in r and (self.dead or any(t <= self.now for t, _ in self.inbox)) else []
            readable += select.select(real, [], [], 0)[0] if real else []
            writable = [self.fd] if self.fd in w and (self.writable or self.dead or self.write_error) else []
            if readable or writable or self.now >= end:
                return readable, writable, []
            target = min([end] + [t for t, _ in self.inbox if t > self.now] + [t for t, _ in self.actions])
            if self.gate is not None:
                allowed = self.gate(target)
                if allowed is None:  # it held the clock: input may have come meanwhile
                    continue
                if allowed < target:  # up to the gate, then look again
                    self.now = max(self.now, allowed)
                    continue
            self.now = target
            assert self.now <= self.limit, "the virtual clock ran past its limit: the game is stuck"

    def tcgetattr(self, fd: int) -> list:
        self._check(fd)
        return [*self.attrs[:6], list(self.attrs[6])]

    def tcsetattr(self, fd: int, when: int, attrs: list) -> None:
        self._check(fd)
        self.calls.append(("tcsetattr", when, len(self.output), self.now))
        self.attrs = [*attrs[:6], list(attrs[6])]

    def tcflush(self, fd: int, queue: int) -> None:
        self._check(fd)
        self.calls.append(("tcflush", queue, len(self.output), self.now))
        if queue in (termios.TCIFLUSH, termios.TCIOFLUSH):
            self.inbox = [(when, data) for when, data in self.inbox if when > self.now]
        if queue in (termios.TCOFLUSH, termios.TCIOFLUSH) and self.room is not None:
            self.room = max(self.room, FLUSHED_ROOM)  # what it had not read is gone; it still reads no faster

    def winsize(self, fd: int) -> tuple[int, int]:
        return self.cols, self.rows

    def _esu(self, vt: MiniVT) -> None:
        self.screen = vt.lines()

    def _check(self, fd: int) -> None:
        assert fd == self.fd, fd
        if self.dead:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
