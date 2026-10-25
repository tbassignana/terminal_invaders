"""An SSH link between the game's pty and the terminal stub (spec 13, G4).

Each direction is a FIFO bottleneck of 250,000 B/s followed by a one-way delay of half the base RTT. Toward the game
everything also waits for OpenSSH's 20 ms send slots (ObscureKeystrokeTiming, 9.5 and later), the terminal's
replies included, as they reach ssh the same way keystrokes do. The relay owns the pty master; the stub and the
test use the other end of a socket pair, so the Stub, Pty.send and Pty.tap work unchanged.

The relay times what the link sees: when each ack query left the game and when each reply reached it. PhotonStub
notes when every frame ended (ESU) and the routed-key count its stats line showed, which is how a key's photon is
found (the game runs with --show-fps).
"""

import math
import os
import re
import select
import socket
import threading
import time
from collections import deque

from tests.support.ptyrun import Pty, Stub

RATE = 250_000  # bytes per second each way: a 2 Mbit/s link
SLOT = 0.02  # OpenSSH sends interactive input at fixed 20 ms intervals
PACKET = 1448  # the bottleneck serialises this much at a time, like a TCP segment
QUERY, REPLY = b"\x1b[6n", b"\x1b[5;7R"  # CPR, the ack of every fixture that answers, and a reply to one
REPLIES = re.compile(rb"\x1b\[\d+;\d+R")
STATS = re.compile(r"FPS \S+  RTT \S+  .+?  B/f \d+  K(\d+)  ")


class Pipe:
    """One direction of the link: bytes leave in order once serialised at the bottleneck and delayed."""

    def __init__(self, rate: float, delay: float, slot: float = 0.0):
        self.rate, self.delay, self.slot = rate, delay, slot
        self.free = 0.0  # when the bottleneck has sent everything offered so far
        self.queue: deque[tuple[float, bytes]] = deque()  # (arrival, packet)

    def offer(self, data: bytes, now: float) -> None:
        start = math.ceil(now / self.slot) * self.slot if self.slot else now
        for at in range(0, len(data), PACKET):
            chunk = data[at : at + PACKET]
            self.free = max(start, self.free) + len(chunk) / self.rate
            self.queue.append((self.free + self.delay, chunk))

    def due(self) -> float | None:
        return self.queue[0][0] if self.queue else None

    def take(self, now: float) -> bytes:
        """Everything that has arrived by now."""
        out = []
        while self.queue and self.queue[0][0] <= now:
            out.append(self.queue.popleft()[1])
        return b"".join(out)


class Counter:
    """Finds a marker (bytes, or a pattern no longer than `longest`) in a byte stream that arrives in pieces;
    returns how many ended in each piece."""

    def __init__(self, marker: bytes | re.Pattern, longest: int | None = None):
        self.pattern = marker if isinstance(marker, re.Pattern) else re.compile(re.escape(marker))
        self.keep, self.tail = (longest or len(marker)) - 1, b""

    def count(self, data: bytes) -> int:
        joined = self.tail + data
        found = sum(match.end() > len(self.tail) for match in self.pattern.finditer(joined))
        self.tail = joined[-self.keep :] if self.keep else b""
        return found


class Relay(threading.Thread):
    """The link, as a thread between the pty master and a socket pair."""

    def __init__(self, master: int, rtt: float, *, rate: float = RATE, slot: float = SLOT):
        super().__init__(daemon=True)
        self.master, self.rtt = master, rtt
        self.inner, self.outer = socket.socketpair()
        self.terminal = self.outer.fileno()  # the terminal's side, for the stub and the test
        self.down = Pipe(rate, rtt / 2)  # game to terminal
        self.up = Pipe(rate, rtt / 2, slot)  # terminal to game
        self.queries, self.replies = Counter(QUERY), Counter(REPLIES, longest=16)
        self.sent: list[float] = []  # when each ack query left the game
        self.answered: list[float] = []  # when each ack reply reached the game
        self.stopping = threading.Event()

    def run(self) -> None:
        while not self.stopping.is_set():
            now = time.monotonic()
            self._deliver(now)
            dues = [due - now for due in (self.down.due(), self.up.due()) if due is not None]
            readable = select.select([self.master, self.inner], [], [], max(0.0, min([0.01, *dues])))[0]
            for fd in readable:
                try:
                    data = os.read(fd, 65536) if fd == self.master else self.inner.recv(65536)
                except OSError:
                    return  # the pty was closed
                now = time.monotonic()
                if fd == self.master:
                    self.sent += [now] * self.queries.count(data)
                    self.down.offer(data, now)
                else:
                    self.up.offer(data, now)

    def _deliver(self, now: float) -> None:
        down, up = self.down.take(now), self.up.take(now)
        if down:
            self.inner.sendall(down)
        if up:
            self.answered += [now] * self.replies.count(up)
            while up:
                up = up[os.write(self.master, up) :]

    def idle(self, seconds: float = 2.0) -> bool:
        """Wait until nothing is left on the link; False if it is still busy after that long."""
        end = time.monotonic() + seconds
        while self.down.queue or self.up.queue:
            if time.monotonic() > end:
                return False
            time.sleep(0.005)
        return True

    def ack_rtts(self) -> list[tuple[float, float]]:
        """(when the query left, round trip) for every CPR, the probe's two first."""
        return [(sent, answered - sent) for sent, answered in zip(self.sent, self.answered)]

    def stop(self) -> None:
        self.stopping.set()
        self.join(1)

    def close(self) -> None:
        self.inner.close()
        self.outer.close()


class PhotonStub(Stub):
    """A terminal stub that notes (when, routed-key count) at every ESU instead of keeping every screen."""

    def __init__(self, master: int, doc: dict | None):
        super().__init__(master, doc)
        self.photons: list[tuple[float, int]] = []
        self.row = 0  # where the stats line was last seen

    def _esu(self, vt) -> None:
        chars = vt.screen.chars
        for y in (self.row, *range(len(chars))):
            found = STATS.search("".join(chars[y]))
            if found:
                self.row = y
                self.photons.append((time.monotonic(), int(found[1])))
                return


class RelayPty(Pty):
    """The game behind the emulated SSH link, with its stats line on (pass --show-fps)."""

    stub_type = PhotonStub

    def __init__(self, directory, *arguments, rtt: float, **kwargs):
        self.rtt = rtt
        self.taps: list[tuple[float, int]] = []  # (when sent, key events in it)
        super().__init__(directory, *arguments, **kwargs)

    def connect(self) -> int:
        self.relay = Relay(self.master, self.rtt)
        self.relay.start()
        return self.relay.terminal

    def tap(self, *names: str, mods: int = 0) -> int:
        when = time.monotonic()
        events = super().tap(*names, mods=mods)
        self.taps.append((when, events))
        return events

    def exit(self, seconds: float = 5.0) -> int:
        code = super().exit(seconds)
        self.relay.idle()  # the LEAVE is still on its way to the stub
        time.sleep(0.05)
        return code

    def close(self) -> None:
        self.relay.stop()
        super().close()
        self.relay.close()

    def lags(self) -> list[tuple[float, float]]:
        """(when sent, key to photon) for every tap: the first ESU whose stats line counted its first key event."""
        with self.stub.lock:
            photons = list(self.stub.photons)
        shown, last, counts = 0, 0, []
        for when, count in photons:
            shown, last = shown + (count - last) % 100, count  # the line shows the count mod 100
            counts.append((when, shown))
        out, routed, at = [], 0, 0
        for sent, events in self.taps:
            while at < len(counts) and (counts[at][0] < sent or counts[at][1] <= routed):
                at += 1
            if at == len(counts):
                break  # never shown: the run ended first
            out.append((sent, counts[at][0] - sent))
            routed += events
        return out
