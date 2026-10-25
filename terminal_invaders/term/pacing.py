"""Ack pacing: replies to the frames' ack queries bound the frames in flight (fixed 2, adaptive 2 to 12, or off), but
those out while clock pacing was on, which hold no slot."""

from __future__ import annotations

import bisect
import math
import time
from collections import deque
from typing import NamedTuple

from .encode import tag_area

MODES = ("fixed", "adaptive", "off")
DEFAULT_MODE = "adaptive"  # what --pacing auto means: deviation D1 is accepted; fixed is the plan's literal rule
TICK = 1 / 60
FLOOR, CAP = 2, 12  # frames in flight
RTT_WIN, BW_WIN = 10.0, 5.0  # base RTT and bandwidth are windowed min and max over these many seconds
RATE_SPAN, RATE_KEEP = 0.05, 0.1  # delivery rate: the acks of the last RATE_KEEP s, once they span RATE_SPAN s
RATE_GAP = 1.0  # a delivery this long after the last one starts a new span: a span across an outage is no rate
# The congestion hold doubles once per congestion episode that starts within HOLD_RESET s of the last back-off. An
# episode begins with an ack queued 2+ ticks and ends once the round trips themselves are back under 2 ticks. Doubling
# per queued ack (8.4 as written) let one RTT spike, which queues five acks or more, pin W at the floor for HOLD_MAX;
# ending the episode on the queue estimate let its serialisation term (frame bytes over a delivery rate the game
# itself limits) end it at every larger frame, which doubled the hold to HOLD_MAX within two seconds of an RTT step.
HOLD_MIN, HOLD_MAX, HOLD_RESET = 2.0, 32.0, 10.0
LOSS_LIMIT = 3  # consecutive lost acks before clock pacing takes over
BACKOFF_MAX = 3  # the probe's loss timeout doubles once per timeout in a row that failed, up to 8x
LATE_KEEP = RTT_WIN  # how long after it went out a frame given up on, or its query, still waits for a reply
# Which frame a reply answers. With CPR acks every query parks the cursor at its frame's tag first (encode.ack_tag),
# so the reply names its frame. Replies come in the order their queries went out, so one also proves every older query
# still waiting lost for good: owed counts the queries themselves and so stays exact. Matched in order instead, the
# replies a link delivers late after dropping out for a second or more answered the frames in flight: round trips near
# zero, base RTT followed, and W sat at the floor behind repeated holds. A reply to a frame not in flight (given up on,
# or dropped by a shrink) is no sample, but it settles every older query, and every older frame in flight, whose
# replies were lost. A frame given up on (expired) stays known until LATE_KEEP s after it went out. A reply that names
# no frame known, one more than LATE_KEEP s late, answers the oldest query; as any reply, it resets the loss count and
# ends clock pacing. Taken for no reply (S17), it left clock pacing on over a link whose backlog had grown past that,
# the terminal answering all along, and the backlog only grew. S17 had it so for modified F3 keys, which were read as
# replies; they no longer are (encode.TAG_SKIP).
#
# A terminal that shrinks clamps the CUP of every frame it reads after onto its last row or column. When the game
# learns a size, each frame whose reply may still come records where this size would put it, and the frames tagged
# outside its tags (encode.tag_area, which keeps off the last row and column) leave the window as no loss: their
# replies, which still come, settle the older queries and frames, with no sample. A reply names the oldest frame it
# may answer, and no new frame takes a position that a frame whose reply may still come may name (reserved()): a reply
# to a frame dropped this way once settled a newer frame that took its tag 16 ms after it went out, and the base RTT
# that taught held W at the floor for 10 s. A restart's new pacer keeps the old frames the same way (inherit()), as
# the tag sequence goes on but maps to other positions at another size. While clock pacing is on, frames carry the
# first tag, which is kept for them alone, and are given up on as they go out: any reply turns ack pacing back on, and
# theirs, still known, settle the older queries. Not known (S16 as first written), a reply to one named the newer
# frame in flight that a screen one column wide had put at the first tag, and settled the queries between. DSR and DA1
# replies, the fallback where CPR is not answered, carry nothing and are matched in order (8.3 as written).
#
# The loss timeout is 8.3's max(1 s, 4 x srtt), or twice the last round trip if that is longer. One frame, the probe,
# takes it doubled once per timeout in a row that failed (Karn's rule and TCP's backoff): the first frame out after a
# timeout failed, or whenever none at that backoff is in flight. It holds its slot of the window until it is given up
# on, so that a link slower than the timeout, which otherwise gave up on every frame and held W at the floor, is
# matched in flight within a few timeouts. Freed once a frame at no backoff would have been given up on (S17), the
# slot let a third frame into the queue of such a link; two frames were then lost per timeout between its late
# replies, and clock pacing took over and flooded it. No probe goes out while clock pacing is on, nor, after a timeout
# or more of it, until a timeout fails again: the backoff an outage leaves made the first frame out after it a probe
# of 4-8 s, holding one of the floor window's two slots, so a terminal that stopped answering then took three
# timeouts at a frame a second to give up on (S16); at no backoff, two, at full rate. After a shorter spell of clock
# pacing, the terminal answering late over a slow link, the probe goes first, as it would have without the spell.
# Every frame backed off instead (S16 as first written) held the window over a terminal that stopped answering for
# three timeouts, frozen for up to 5 s after a 0.9 s outage.
#
# A round trip of a frame in flight resets the backoff, but not one of a frame in flight while clock pacing was on
# (spanned), as it may span the outage (Karn's rule): taken (S16), the probe's made the timeout twice the outage's
# length. Such a frame holds no slot and keeps no probe back. A late reply's round trip teaches nothing either: after
# an outage it is the outage's length, and learnt from (S14, S15) it kept the timeout at 4-25 s, so that a terminal
# that stopped answering then wedged the window for two such timeouts, up to 28 s. Nor does the probe's round trip
# back W off (_adapt): it may include a short outage, and with the frames queued behind the backlog of a bandwidth-
# limited link it backed W off to the floor for 2 s (S16). Under the timeout a frame at no backoff had, so no
# outage's, it may grow W: taking no part (S17), it left W a step lower after a short outage, and a terminal that
# stopped answering then took a timeout more to give up on.
#
# The game's own waits bound each round trip from below. A reply may have waited unread in the tty for as long as the
# game had not looked at the tty when it read it (read()), and a frame's bytes may have waited on the game in the output
# queue (held(): counted whenever the tty could take them when the game looked again, which may include time before the
# terminal made room, and never the time blocked in write()). So a round trip is known within [rtt - unread - held,
# rtt], less about a tick at most: the loop counts a stall inside a select() only past that select()'s timeout, as until
# then it may have been waiting on the link, and while replies are awaited its timeout is a tick at most. The upper
# bound is the sample for srtt, the base RTT and growth (a queue it rules out is surely absent); the lower bound decides
# a back-off (a queue it shows is surely there). A 170 ms host stall at 80x24 otherwise backed W off to the floor and
# held it there, 3-5 s at 14-30 fps on a 100 ms link; and taking no sample of those round trips starved the pacer of
# samples when stalls came within a round trip of each other. A frame's held time also extends its loss timeout, and a
# reply read late is no loss: the loop expires frames only up to its last look at the tty, once every reply it read is
# handled.
#
# A jump in frame size (W back to the floor) is a frame at least JUMP_RATIO x the largest frame sent in the last
# PEAK_WIN s and at least JUMP_MIN bytes (decision 6 set the ratio and the floor, against a smoothed size). Relative to
# the peak, not to a mean, as most frames are an unchanged packet of sync and the ack query only (26-28 bytes with CPR
# acks): against a mean every march step (4-9 KB at 120x44 in truecolor, 4-17 KB at 240x70) was a jump, and the resets
# cost 21-23 fps at RTT 100 ms. Absolute, so that a truecolor scene change (the wave banner leaving, about 1 KB at
# 80x24) is no jump. The cost: a march step in the last PEAK_WIN s hides a switch to frames up to 4x its size, which
# ready()'s cap of 2 x bw x base RTT in flight and the queue back-off then bound instead.
JUMP_RATIO, JUMP_MIN, PEAK_WIN = 4, 4096, 5.0


class Frame(NamedTuple):
    sent: float
    nbytes: int
    tag: tuple[int, int] | None = None  # the CPR position its ack query parked the cursor at
    held: float = 0.0  # how long its bytes waited on the game, not the link, before they went out
    rto: float = 1.0  # its loss timeout as it went out, backed off if it is the probe
    backoff: int = 0  # the backoff it went out with if it is the probe, else 0
    cells: frozenset = frozenset()  # where the smaller screens the game learned since would put its CUP
    spanned: bool = False  # in flight while clock pacing was on: its round trip may span an outage, so no sample


class AckPacer:
    """Acks are matched to frames in flight by tag, or FIFO; ready() says whether the next frame may be built."""

    def __init__(self, mode: str, *, tagged: bool = False, clock=time.monotonic):
        mode = DEFAULT_MODE if mode == "auto" else mode
        if mode not in MODES:
            raise ValueError(f"unknown pacing mode: {mode}")
        self.mode, self.tagged = mode, tagged  # tagged: replies name their frame (CPR acks)
        self.clock = clock
        self.window = float(FLOOR)
        self.srtt = self.base_rtt = self.bw = None
        self.disabled = False  # acks stopped coming: clock pacing until one comes back
        self.limited = False  # the window refused the last ready(), or would refuse one more frame
        self.losses = 0  # frames given up on in a row
        self.disabled_at = -math.inf  # when clock pacing last took over
        self.outage = False  # ack pacing came back on after a timeout or more of clock pacing, and no frame has been
        # answered in flight or given up on since: no probe goes out
        self.queries: deque[float] = deque()  # when the queries not answered went out (owed)
        self.inflight: deque[Frame] = deque()
        self.late: deque[Frame] = deque()  # frames given up on or sent clock pacing, in the last LATE_KEEP s (tagged)
        self.voided: deque[Frame] = deque()  # frames resized() dropped or kept from the last pacer (inherit()), while
        # their replies may still come (tagged)
        self.backoff = 0  # timeouts in a row that failed, up to BACKOFF_MAX
        self.last_rtt: float | None = None  # the last round trip of a frame in flight
        self.reads = deque(maxlen=64)  # (t, unread): how long the replies read at t may have waited unread
        self.peaks = deque()  # (sent, nbytes) of the last PEAK_WIN s, falling: peaks[0] is the largest
        self.rtts = deque()  # (t, rtt)
        self.delivered = deque()  # (t, nbytes) of recent acks
        self.rates = deque()  # (t, bytes per second)
        self.recent = deque(maxlen=3)  # (upper, lower) bounds of the RTTs less the frame's own serialisation time
        self.raw = deque(maxlen=3)  # the RTTs themselves (upper bounds)
        self.hold = HOLD_MIN
        self.hold_until = self.last_back = -math.inf
        self.episode = False  # a back-off in this congestion episode: until the round trips are back under 2 ticks

    @property
    def congested(self) -> bool:
        return self.clock() < self.hold_until

    @property
    def budget(self) -> int | None:
        """Frame bytes the measured link carries at 60 fps with 20% to spare."""
        return int(0.8 * self.bw / 60) if self.bw else None

    @property
    def peak(self) -> int | None:
        """The largest frame sent in the last PEAK_WIN s, or the last frame sent if that is older."""
        return self.peaks[0][1] if self.peaks else None

    @property
    def owed(self) -> int:
        """Queries not answered, lost ones too until a later reply proves them lost: what restart() awaits."""
        return len(self.queries)

    @property
    def inflight_bytes(self) -> int:
        return sum(frame.nbytes for frame in self.inflight)

    def unread(self, queued: int) -> int | None:
        """Bytes written that the terminal may not have read yet, while an earlier frame than the newest awaits its
        ack (queued: the newest frame's bytes still in the OutputQueue); None when only the newest is in flight,
        or without acks: then the queue's own chunk rule applies."""
        if self.mode == "off" or self.disabled or len(self.inflight) < 2:
            return None
        return max(0, self.inflight_bytes - queued)

    def ready(self, now: float) -> bool:
        if self.mode == "off" or self.disabled:
            return True
        live = [frame for frame in self.inflight if not frame.spanned]
        n = len(live)
        if self.mode == "fixed":
            return n < FLOOR
        nbytes = sum(frame.nbytes for frame in live)
        ok = n < FLOOR or (n < math.floor(self.window) and (not self.bw or nbytes < 2 * self.bw * self.base_rtt))
        self.limited = not ok or n + 1 >= math.floor(self.window)
        return ok

    def sent(self, now: float, nbytes: int, *, full: bool, tag: tuple[int, int] | None = None) -> None:
        """A frame and its ack query went out (tagged: tag None means it carried none). FIFO frames are tracked while
        disabled too, so a late answer is matched, but as no sample (spanned); tagged ones then carry the first tag and
        are given up on at once."""
        if self.mode == "off" or (self.tagged and tag is None):
            return
        self.queries.append(now)
        while len(self.peaks) > 1 and self.peaks[0][0] < now - PEAK_WIN:
            self.peaks.popleft()  # the last frame sent always stays: after a quiet spell it is the reference
        if full or (self.peaks and nbytes >= max(JUMP_MIN, JUMP_RATIO * self.peak)):
            self.window = float(FLOOR)  # a repaint, or a jump in frame size
        while self.peaks and self.peaks[-1][1] <= nbytes:
            self.peaks.pop()
        self.peaks.append((now, nbytes))
        if self.tagged and self.disabled:
            self.late.append(Frame(now, nbytes, tag))
            return
        # The probe: the first frame out at this backoff, frames whose round trip spans clock pacing aside; none while
        # clock pacing is on, nor after a timeout or more of it until a timeout fails again
        probe = not (self.disabled or self.outage)
        probe = probe and all(frame.backoff != self.backoff or frame.spanned for frame in self.inflight)
        level = self.backoff if probe else 0
        frame = Frame(now, nbytes, tag, rto=self._timeout() * 2**level, backoff=level, spanned=self.disabled)
        self.inflight.append(frame)

    def held(self, seconds: float, sent: float | None = None) -> None:
        """The newest frame's bytes (the one sent at `sent`, if given) waited this much longer in the output queue on
        the game, not on the link; at most that, as the loop cannot see when inside a stall the terminal made room."""
        if self.inflight and seconds > 0 and (sent is None or self.inflight[-1].sent == sent):
            self.inflight[-1] = self.inflight[-1]._replace(held=self.inflight[-1].held + seconds)

    def resized(self, cols: int, rows: int) -> None:
        """The game learned the screen's size. A terminal that shrank clamps the CUP of every frame it reads after
        onto its last row or column (encode.tag_area keeps this size's tags off them), so each frame whose reply may
        still come records where this size puts it, and the frames tagged outside this size's tags leave the window,
        in flight or given up on, as no loss: their replies, which still come, settle the older queries and frames,
        with no sample."""
        if self.tagged:
            last, width = tag_area(cols, rows)

            def clamped(frame: Frame) -> Frame:
                cell = (min(frame.tag[0], rows), min(frame.tag[1], cols))
                return frame if cell == frame.tag or cell in frame.cells else frame._replace(cells=frame.cells | {cell})

            def tagged_here(frame: Frame) -> bool:
                return frame.tag[0] <= last and frame.tag[1] <= width

            inflight, late = [clamped(frame) for frame in self.inflight], [clamped(frame) for frame in self.late]
            dropped = [frame for frame in inflight + late if not tagged_here(frame)]
            self.voided = deque(sorted([clamped(frame) for frame in self.voided] + dropped, key=lambda f: f.sent))
            self.inflight, self.late = deque(filter(tagged_here, inflight)), deque(filter(tagged_here, late))

    def inherit(self, old: AckPacer) -> None:
        """A new pacer after a restart (tty.enter): the frames of the old one whose replies may still come stay
        known, as dropped by resized() (no query owed, no loss), so that such a reply names its own frame and no new
        frame takes a tag it may name, at whatever size the game comes back."""
        if self.tagged and old.tagged:
            self.voided = deque(sorted([*old.inflight, *old.late, *old.voided], key=lambda f: f.sent))

    def reserved(self) -> set[tuple[int, int]]:
        """The positions a reply still to come may name: the tags of the frames in flight, given up on, dropped by
        resized() or kept from the last pacer, and where a smaller screen put their CUP. A new frame takes none of them
        (Renderer.frame)."""
        cells: set[tuple[int, int]] = set()
        for frames in (self.inflight, self.late, self.voided):
            for frame in frames:
                cells.add(frame.tag)
                cells |= frame.cells
        return cells

    def read(self, at: float, unread: float) -> None:
        """The game read the tty at `at`, unread s after it last looked: the replies read then waited at most that."""
        if self.mode != "off":
            self.reads.append((at, unread))

    def acked(self, now: float, tag: tuple[int, int] | None = None) -> None:
        """One ack reply, read at `now`. Tagged, it answers the oldest frame whose reply may name its position, in
        flight, given up on, dropped by resized() or kept from the last pacer; one that names none, more than LATE_KEEP
        s late, the oldest query. FIFO, it answers the oldest in flight. Any reply resets the loss count and ends clock
        pacing (the backoff stays, for the next probe). Only a frame in flight is a sample, which resets the backoff,
        and not one in flight while clock pacing was on (spanned)."""
        if self.tagged:
            frames = self._match(now, tag)
        else:
            if self.queries:
                self.queries.popleft()
            frames = [self.inflight.popleft()] if self.inflight else []
        self.losses = 0
        if self.disabled:
            self.disabled, self.window = False, float(FLOOR)  # the terminal answers again: start from the floor
            self.outage = now >= self.disabled_at + self._timeout()
        if not frames or frames[-1].spanned:
            return  # a late reply, or one whose round trip may be an outage's: either teaches nothing (Karn's rule)
        frame, self.outage = frames[-1], False
        rtt = now - frame.sent  # the upper bound; the lower one takes off the game's waits
        least = rtt - self._unread_at(now) - frame.held
        self._smooth(rtt)
        self.last_rtt, self.backoff = rtt, 0
        self.rtts.append((now, rtt))
        while self.rtts[0][0] < now - RTT_WIN:
            self.rtts.popleft()
        self.base_rtt = min(sample for _, sample in self.rtts)
        self._measure_rate(now, sum(f.nbytes for f in frames))  # frames whose replies were lost went through too
        if self.mode == "adaptive" and not frame.backoff:
            self._adapt(now, rtt, least, frame.nbytes)
        elif self.mode == "adaptive" and rtt < frame.rto / 2**frame.backoff:
            # The probe's round trip may include a short outage, or the backlog of a bandwidth-limited link after one,
            # so it never backs W off; under the timeout a frame at no backoff had, it may grow it
            serialise = frame.nbytes / self.bw if self.bw else 0.0
            self._grow(now, min([upper for upper, _ in self.recent] + [rtt - serialise]) - self.base_rtt)

    def deadline(self) -> float | None:
        """When the next frame in flight counts as lost (never while disabled: the loop does not wake for the probe
        kept in flight then, which expire() gives up on when it next looks)."""
        return min(map(self._lost_at, self.inflight)) if self.inflight and not self.disabled else None

    def overdue(self, now: float) -> bool:
        """Whether expire(now) gives up on a frame in flight."""
        return any(now >= self._lost_at(frame) for frame in self.inflight)

    def expire(self, now: float) -> None:
        """Frames older than their timeout leave the window, in the order they were lost (the probe, backed off, may
        outlast newer frames); three in a row without an ack in between disable it."""
        while overdue := [frame for frame in self.inflight if now >= self._lost_at(frame)]:
            frame = min(overdue, key=self._lost_at)  # so that one look and several give the same backoff
            self.inflight.remove(frame)
            if self.tagged:
                bisect.insort(self.late, frame, key=lambda f: f.sent)
            if frame.backoff == self.backoff:  # the timeout now in force failed: the probe's, or any frame's at 0
                self.backoff = min(BACKOFF_MAX, self.backoff + 1)
            if self.disabled:
                continue  # sent clock pacing: no loss
            self.losses, self.outage = self.losses + 1, False
            if self.losses >= LOSS_LIMIT:
                self.disabled, self.disabled_at = True, now  # clock pacing until an ack comes back: ACK OFF
                if self.tagged:
                    given_up = [frame for frame in self.inflight if not frame.backoff]
                    self.late = deque(sorted([*self.late, *given_up], key=lambda f: f.sent))
                # The probe still waits, as its timeout raises the backoff and a FIFO reply to it is matched in order,
                # but its round trip now spans this outage: no sample
                self.inflight = deque(frame._replace(spanned=True) for frame in self.inflight if frame.backoff)
        cutoff = now - LATE_KEEP  # a reply this late counts as never coming
        for frames in (self.late, self.voided):
            while frames and frames[0].sent < cutoff:
                frames.popleft()
        while self.queries and self.queries[0] < cutoff:
            self.queries.popleft()

    def _match(self, now: float, tag: tuple[int, int] | None) -> list[Frame]:
        """A tagged reply answers the oldest known frame that may name its position. Every query, frame given up on
        and frame dropped up to it is settled, and so is every frame in flight up to it (their replies were lost);
        those in flight are returned, oldest first, for a sample when it is the last of them, else []. Naming no
        frame known, a reply answers the oldest query, and [] is returned. Each list is in send order, so the burst
        of replies after an outage costs as many steps as it has replies."""

        def oldest(frames: deque[Frame]) -> Frame | None:
            return next((frame for frame in frames if frame.tag == tag or tag in frame.cells), None)

        named = [frame for frames in (self.late, self.voided, self.inflight) if (frame := oldest(frames))]
        if not named:
            if self.queries:
                self.queries.popleft()
            return []
        sent = min(frame.sent for frame in named)
        for times in (self.queries, self.late, self.voided):
            while times and (times[0] if times is self.queries else times[0].sent) <= sent:
                times.popleft()
        frames = []
        while self.inflight and self.inflight[0].sent <= sent:
            frames.append(self.inflight.popleft())
        return frames if frames and frames[-1].sent == sent else []

    def _smooth(self, rtt: float) -> None:
        self.srtt = rtt if self.srtt is None else 0.875 * self.srtt + 0.125 * rtt

    def _unread_at(self, now: float) -> float:
        """How long the replies read at `now` may have waited unread (0 for a time read() was not told of)."""
        while self.reads and self.reads[0][0] < now:
            self.reads.popleft()
        return self.reads[0][1] if self.reads and self.reads[0][0] == now else 0.0

    def _lost_at(self, frame: Frame) -> float:
        """When a frame counts as lost: a timeout after it went out, plus however long its bytes waited on the game."""
        return frame.sent + frame.held + max(frame.rto, self._timeout())

    def _timeout(self) -> float:
        """8.3's max(1 s, 4 x srtt), or twice the last round trip of a frame in flight if that is longer."""
        return max(1.0, 4 * (self.srtt or 0.0), 2 * (self.last_rtt or 0.0))

    def _measure_rate(self, now: float, nbytes: int) -> None:
        """Delivery rate over the acks of the last RATE_KEEP s; bw is the windowed maximum. Replies read together
        are one delivery, so a burst read after a stall opens no short span of its own."""
        if self.delivered and now - self.delivered[-1][0] >= RATE_GAP:
            self.delivered.clear()
        if self.delivered and self.delivered[-1][0] == now:
            self.delivered[-1] = (now, self.delivered[-1][1] + nbytes)
        else:
            self.delivered.append((now, nbytes))
        while len(self.delivered) > 2 and self.delivered[1][0] <= now - RATE_KEEP:
            self.delivered.popleft()
        span = now - self.delivered[0][0]
        if span >= RATE_SPAN:
            self.rates.append((now, (sum(size for _, size in self.delivered) - self.delivered[0][1]) / span))
            while self.rates[0][0] < now - BW_WIN:
                self.rates.popleft()
            self.bw = max(rate for _, rate in self.rates)

    def _adapt(self, now: float, rtt: float, least: float, nbytes: int) -> None:
        """Grow by 1/W while the queue is surely under a tick and the window binds; each ack whose queue is surely
        2+ ticks backs off and restarts the hold, which doubles only at the first back-off of an episode."""
        serialise = nbytes / self.bw if self.bw else 0.0
        self.recent.append((rtt - serialise, least - serialise))
        self.raw.append(rtt)
        if min(lower for _, lower in self.recent) - self.base_rtt > 2 * TICK:
            self.window = float(FLOOR)
            if not self.episode:
                self.hold = min(HOLD_MAX, self.hold * 2) if now - self.last_back < HOLD_RESET else HOLD_MIN
            self.episode, self.last_back, self.hold_until = True, now, now + self.hold
            return
        if min(self.raw) - self.base_rtt <= 2 * TICK:
            self.episode = False
        self._grow(now, min(upper for upper, _ in self.recent) - self.base_rtt)

    def _grow(self, now: float, queue: float) -> None:
        """By 1/W, up to the target, while the queue is surely under a tick and the window binds."""
        if queue < TICK and now >= self.hold_until and self.limited:
            target = min(CAP, math.ceil(self.base_rtt / TICK) + 2)
            self.window = min(float(target), self.window + 1 / self.window)
