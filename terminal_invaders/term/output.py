"""Terminal output: a select-driven queue that never stalls the loop for long, and bounded writes for teardown.

The tty fd stays blocking (never O_NONBLOCK, which would leak to the shell through a shared open file
description), so a write is only attempted after select() says the fd is writable, and its size adapts to how
quickly the terminal takes bytes. select() says writable while any room is left, and a blocking write then waits
until all of it fits, so behind a frame the terminal has not acknowledged, writes stay within the room a pty surely
has (PTY_ROOM) or take MIN_CHUNK at a time. Without acks nothing says how much room is left, so the game's writes are
timed: one that blocks for WRITE_LIMIT is cut short by a timer signal and returns what it wrote.
"""

import errno
import os
import select
import signal
import time

DEAD = frozenset((errno.EIO, errno.ENXIO, errno.EPIPE, errno.EBADF))  # the terminal is gone
# A Linux pty takes about 10 to 17 KB (spec 4.4; Linux 6 says writable up to 19.5 KB). A macOS pty holds about 1 KB
# (Phase 1's Mac check), so there a write of more blocks until the terminal reads, and timed_write cuts it short.
PTY_ROOM = 8192
WRITE_LIMIT = 0.008  # a write blocked this long returns short, so no write stalls the loop past a tick (11.3: 20 ms)


def on_alarm(signum, frame) -> None:
    """SIGALRM only cuts a blocked write short (TerminalSession installs this handler)."""


def timed_write(fd: int, data) -> int:
    """os.write, cut short once it has blocked for about WRITE_LIMIT: an interval timer's SIGALRM interrupts the
    wait, and the write returns the bytes it took (select() said writable, so it takes some). The fd stays blocking.
    A plain write when on_alarm is not installed, as the default SIGALRM would end the process."""
    if signal.getsignal(signal.SIGALRM) is not on_alarm:
        return os.write(fd, data)
    signal.setitimer(signal.ITIMER_REAL, WRITE_LIMIT, WRITE_LIMIT)
    try:
        return os.write(fd, data)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


class OutputQueue:
    """Bytes on their way to the tty; flush_some() writes adaptive chunks while the fd is writable."""

    MIN_CHUNK, START_CHUNK, MAX_CHUNK = 256, 4096, 65536

    def __init__(self, fd: int, clock=time.monotonic, *, write=os.write, select=select.select):
        self.fd, self.clock, self.write, self.select = fd, clock, write, select
        self.buf = bytearray()
        self.chunk = self.START_CHUNK
        self.dead, self.error = False, None  # error: the errno that killed the terminal
        self.frame_end = self.frame_len = 0  # where the newest frame packet ends in buf, and its length
        self.longest = 0.0  # the longest single write so far, for the debug log
        self.writing = 0.0  # how long the last flush_some() spent inside write(), waiting on the terminal

    def push(self, data: bytes, *, frame: bool = False) -> None:
        """Queue bytes; frame=True marks them as one frame packet (see mid_frame)."""
        if self.dead:
            return
        self.buf += data
        if frame:
            self.frame_end, self.frame_len = len(self.buf), len(data)

    def empty(self) -> bool:
        return not self.buf

    def pending(self) -> int:
        return len(self.buf)

    def wants_write(self) -> bool:
        return bool(self.buf) and not self.dead

    @property
    def mid_frame(self) -> bool:
        """Some but not all bytes of the newest frame packet have been written."""
        left = min(self.frame_len, max(0, self.frame_end))
        return 0 < left < self.frame_len

    def flush_some(self, budget: float = 0.004, unread: int | None = None) -> None:
        """Write while the fd is writable, for about budget seconds; short writes and EINTR are normal. unread: the
        bytes written behind a frame the terminal has not acknowledged, when acks say so (AckPacer.unread)."""
        start, self.writing = self.clock(), 0.0
        while self.buf and not self.dead:
            if not self.select([], [self.fd], [], 0)[1]:
                self.chunk = self.MIN_CHUNK  # backed up: room returns a little at a time, so ask for little
                return
            size = self.chunk if unread is None else min(self.chunk, max(self.MIN_CHUNK, PTY_ROOM - unread))
            began = self.clock()
            try:
                n = self.write(self.fd, self.buf[:size])
            except InterruptedError:
                continue
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno not in DEAD:
                    raise
                self.dead, self.error = True, exc.errno
                self.discard()
                return
            finally:
                self.writing += self.clock() - began
            took = self.clock() - began
            self.longest = max(self.longest, took)
            del self.buf[:n]
            unread = None if unread is None else unread + n
            self.frame_end -= n
            if took > 0.002:
                self.chunk = max(self.MIN_CHUNK, self.chunk // 2)
            elif n == self.chunk and took < 0.0002:
                self.chunk = min(self.MAX_CHUNK, self.chunk * 2)
            if self.clock() - start >= budget:
                return

    def drain(self, timeout: float) -> bool:
        """Write everything within timeout seconds; True when the queue emptied in time."""
        end = self.clock() + timeout
        while self.buf and not self.dead:
            left = end - self.clock()
            if left <= 0:
                return False
            if self.select([], [self.fd], [], left)[1]:
                self.flush_some(left)
        return not self.dead

    def discard(self) -> None:
        self.buf.clear()
        self.frame_end = self.frame_len = 0


def write_all(fd: int, data: bytes, deadline: float, *, clock=time.monotonic, write=os.write, select=select.select):
    """Write data by the clock() deadline; False when it could not. Raises OSError when the terminal is gone."""
    queue = OutputQueue(fd, clock, write=write, select=select)
    queue.push(data)
    done = queue.drain(deadline - clock())
    if queue.dead:
        raise OSError(queue.error, os.strerror(queue.error))
    return done


def write_bounded(fd: int, data: bytes, timeout: float, **ops) -> bool:
    """write_all for teardown: within timeout seconds, and a terminal that is gone is not an error."""
    clock = ops.get("clock", time.monotonic)
    try:
        return write_all(fd, data, clock() + timeout, **ops)
    except OSError as exc:
        if exc.errno not in DEAD:
            raise
        return False
