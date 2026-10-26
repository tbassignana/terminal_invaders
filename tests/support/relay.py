"""An SSH link between the game's pty and the terminal stub (spec 13, G4).

Each direction is a FIFO bottleneck of 250,000 B/s followed by a one-way delay of half the base RTT. Toward the game
everything also waits for OpenSSH's 20 ms send slots (ObscureKeystrokeTiming, 9.5 and later), the terminal's
replies included, as they reach ssh the same way keystrokes do. The relay owns the pty master; the stub and the
test use the other end of a socket pair, so the Stub, Pty.send and Pty.tap work unchanged.

The relay times what the link sees: when each ack query left the game and when each reply reached it. PhotonStub
notes when every frame ended (ESU) and the routed-key count its stats line showed, which is how a key's photon is
found (the game runs with --show-fps).

VirtualLink is the same link on a FakeTerminal's virtual clock, for the game run in this process: what it times there
is the game's own pacing, exactly, however late a loaded machine wakes the relay, the stub or the game.
"""

import math
import os
import re
import select
import socket
import threading
import time
from collections import deque

from terminal_invaders.term.keys import PRESS, RELEASE, Key
from terminal_invaders.term.simkeys import encode
from tests.support.faketerm import FakeTerminal
from tests.support.ptyrun import Pty, Stub

RATE = 250_000  # bytes per second each way: a 2 Mbit/s link
SLOT = 0.02  # OpenSSH sends interactive input at fixed 20 ms intervals
PACKET = 1448  # the bottleneck serialises this much at a time, like a TCP segment
QUERY, REPLY = b"\x1b[6n", b"\x1b[5;7R"  # CPR, the ack of every fixture that answers, and a reply to one
REPLIES = re.compile(rb"\x1b\[\d+;\d+R")
SPIN = 1000  # VirtualLink: select() calls in a row at one instant, from a game that never waits (a run makes up to 7)
STATS = re.compile(r"FPS \S+  RTT \S+  .+?  B/f \d+  K(\d+)  ")
WINDOW = re.compile(r"FPS \S+  RTT \S+  (W [\d.]+|W fix|ACK OFF)  ")  # the stats line's pacing window


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
        found = stats_line(vt, self.row)
        if found:
            self.row, line = found
            self.photons.append((time.monotonic(), int(STATS.search(line)[1])))


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
        """(when sent, key to photon) for every tap (key_to_photon)."""
        with self.stub.lock:
            photons = list(self.stub.photons)
        return key_to_photon(self.taps, photons)


class VirtualLink(FakeTerminal):
    """The same link on a FakeTerminal's virtual clock: what the game writes reaches the terminal (MiniVT and the
    Responder) only when the down Pipe delivers it, and the terminal's replies and the keys tap() sends reach the game
    when the up Pipe does, in OpenSSH's send slots. The link takes every write at once, as the relay reads the pty as
    soon as there is output. It keeps what Relay, PhotonStub and RelayPty keep, the same way: sent, answered and
    ack_rtts(); photons, taps and lags(); and the pacing windows the stats line showed."""

    def __init__(self, doc: dict | None, rtt: float, *, rate: float = RATE, slot: float = SLOT, **kwargs):
        super().__init__(doc, **kwargs)
        self.rtt = rtt
        self.down, self.up = Pipe(rate, rtt / 2), Pipe(rate, rtt / 2, slot)
        self.queries, self.replies = Counter(QUERY), Counter(REPLIES, longest=16)
        self.sent: list[float] = []  # when each ack query left the game
        self.answered: list[float] = []  # when each ack reply reached the game
        self.photons: list[tuple[float, int]] = []  # (when the frame arrived, routed-key count), at every ESU
        self.taps: list[tuple[float, int]] = []  # (when sent, key events in it)
        self.windows: set[str] = set()  # W 5.8, W fix or ACK OFF
        self.row = 0  # where the stats line was last seen
        self.waking = False  # a delivery is scheduled, at the down Pipe's next arrival
        self.still = 0  # select() calls in a row that found the clock where it was

    def select(self, r, w, x, timeout):
        """FakeTerminal.select(), but a game that never waits fails rather than holding the virtual clock still for
        ever, as a busy loop costs a real machine only its time."""
        before = self.now
        found = super().select(r, w, x, timeout)
        self.still = self.still + 1 if self.now == before else 0
        if self.still >= SPIN:
            self.still = 0  # the teardown that follows waits as it should
            raise AssertionError(f"{SPIN} selects at {self.now:.3f} s without the clock moving: the game never waits")
        return found

    def write(self, fd: int, data) -> int:
        self._check(fd)
        data = bytes(data)
        self.sent += [self.now] * self.queries.count(data)
        self.down.offer(data, self.now)
        self._wake()
        return len(data)

    def tap(self, name: str) -> None:
        """A press and release of the key, sent now as the terminal sends it (Pty.tap): kitty CSI u while the game has
        the protocol pushed, legacy bytes (and no release) otherwise."""
        stack = self.vt.kitty["alt" if self.vt.alt_screen else "main"]
        sent = [encode(Key(name, kind=kind), kitty=bool(stack and stack[-1])) for kind in (PRESS, RELEASE)]
        self.taps.append((self.now, sum(map(bool, sent))))
        self._send(b"".join(sent))

    def ack_rtts(self) -> list[tuple[float, float]]:
        """(when the query left, round trip) for every CPR, the probe's two first (Relay.ack_rtts)."""
        return [(sent, answered - sent) for sent, answered in zip(self.sent, self.answered)]

    def lags(self) -> list[tuple[float, float]]:
        """(when sent, key to photon) for every tap (key_to_photon)."""
        return key_to_photon(self.taps, self.photons)

    def _wake(self) -> None:
        """One delivery at a time, at the next arrival, as the relay wakes for it: a Pipe is in order, so a packet
        offered later never arrives sooner, and a game that floods the link schedules no action per packet."""
        if not self.waking and self.down.due() is not None:
            self.waking = True
            self.at(self.down.due(), self._deliver)

    def _deliver(self) -> None:
        self.waking = False
        chunk = self.down.take(self.now)
        self.output += chunk
        self.vt.feed(chunk)
        reply = self.responder.answer(self.vt.queries, self.vt.cprs)
        if reply:
            self._send(reply)
        self._wake()

    def _send(self, data: bytes) -> None:
        self.up.offer(data, self.now)
        while self.up.queue:
            when, chunk = self.up.queue.popleft()
            self.answered += [when] * self.replies.count(chunk)
            self.type(chunk, at=when)

    def _esu(self, vt) -> None:
        super()._esu(vt)
        found = stats_line(vt, self.row)
        if found:
            self.row, line = found
            self.photons.append((self.now, int(STATS.search(line)[1])))
            window = WINDOW.search(line)
            if window:
                self.windows.add(window[1])


def stats_line(vt, row: int) -> tuple[int, str] | None:
    """The stats line on MiniVT's screen and its row, looked for first on the row where it was last seen."""
    chars = vt.screen.chars
    for y in (row, *range(len(chars))):
        line = "".join(chars[y])
        if STATS.search(line):
            return y, line
    return None


def key_to_photon(taps: list[tuple[float, int]], photons: list[tuple[float, int]]) -> list[tuple[float, float]]:
    """(when sent, key to photon) for every tap (when sent, key events): the first ESU whose stats line counted its
    first key event."""
    shown, last, counts = 0, 0, []
    for when, count in photons:
        shown, last = shown + (count - last) % 100, count  # the line shows the count mod 100
        counts.append((when, shown))
    out, routed, at = [], 0, 0
    for sent, events in taps:
        while at < len(counts) and (counts[at][0] < sent or counts[at][1] <= routed):
            at += 1
        if at == len(counts):
            break  # never shown: the run ended first
        out.append((sent, counts[at][0] - sent))
        routed += events
    return out
