"""Legacy auto-repeat classification: is this event a new press, and is it certainly one?

Without release events every OS auto-repeat arrives as another press. The OS repeats only the most recently
pressed key, so an event of any other key starts a new run. The model learns the OS repeat delay and the burst
interval from the stream itself, which also works over SSH.

A press-only action (Arcade fire) reads the same stream more strictly (the `press` verdict): a held key must never
count twice and every tap must count, however fast. For it the model also learns, locally, an OS repeat too slow for
the delay learning (0.11 to 0.2 s between repeats) from a run of gaps far more even than a person taps.
"""

from __future__ import annotations

MAX_INTERVAL = 0.11  # repeat gaps used to confirm a learned delay
NEW_RUN = 2.2  # silence after which a key's next event always starts a new run
DELAY_RANGE = (0.2, 2.0)
SAMPLES, MIN_SAMPLES = 48, 6
# local, remote
MIN_RETAP = (0.075, 0.040)
MARGIN = (0.08, 0.15)
GUARD = (0.70, 1.00)
BURST_GAP = (0.15, 0.20)
FIRST_REPEAT = (0.20, 0.15)  # the shortest gap after a press that can be its first repeat (SSH jitter shortens it)
# A slow repeat: SLOW_GAPS gaps in a row within SLOW_EVEN of each other; read late by up to SLOW_SLACK, still one.
SLOW_GAPS, SLOW_EVEN, SLOW_SLACK = 5, 0.004, 0.015


class RepeatModel:
    """observe() returns (edge, safe): edge = probably a new press; safe = certainly one."""

    def __init__(self, *, remote: bool):
        self.remote = remote  # the Router reports every campaign fire press as an edge when remote (6.6)
        index = int(remote)
        self.min_retap, self.margin, self.first_repeat = MIN_RETAP[index], MARGIN[index], FIRST_REPEAT[index]
        self.default_guard, self.default_burst = GUARD[index], BURST_GAP[index]
        self.delay: float | None = None  # learned OS initial repeat delay
        self.intervals: list[float] = []
        self.key: str | None = None
        self.last = 0.0
        self.run = 0  # events in the current run
        self.candidate: float | None = None  # the run's second gap (or a long gap that began it): maybe the OS delay
        self.fast = 0  # gaps <= MAX_INTERVAL since the candidate
        self.retap = False  # the latest event was its run's second, no bounce: a re-tap, or maybe the first repeat
        self.fresh = False  # the latest event was a press that cannot be a held key's first repeat
        self.press: bool | None = True  # the latest event's press-only verdict (_press)
        self.stage = "press"  # for _press: the latest event was a press, "maybe" a first repeat, or a "repeat"
        self.slow: tuple[float, float] | None = None  # for _press: (delay, interval) of a repeat too slow for _learn
        self.slow_run: list[float] = []  # for _press: a gap as long as a delay, then the latest gaps after it

    @property
    def burst_gap(self) -> float:
        """Longest gap still read as 'the key is repeating'."""
        if len(self.intervals) < MIN_SAMPLES:
            return self.default_burst
        ordered = sorted(self.intervals)
        p95 = ordered[int(0.95 * (len(ordered) - 1))]
        return min(0.35, max(0.06, 1.5 * p95 + 0.03))

    @property
    def guard(self) -> float:
        """Quiet time after which a press is certainly new."""
        if self.delay is None:
            return self.default_guard
        return min(NEW_RUN, max(self.delay + self.margin, 2 * self.burst_gap))

    @property
    def hold_gap(self) -> float:
        """For press-only actions: the longest gap after a possible first repeat (or a repeat) that still reads as
        the key repeating. It stays under the shortest first repeat, so taps at a steady rate never read as a hold."""
        gap = self.burst_gap
        if self.delay is None and self.slow is not None:
            gap = max(gap, self.slow[1] + SLOW_SLACK)
        return min(gap, self.first_repeat)

    def observe(self, key: str, now: float) -> tuple[bool, bool]:
        gap = now - self.last
        same = key == self.key
        self.press = self._press(same, gap)
        self.key, self.last = key, now
        self.retap = self.fresh = False
        if not same or gap >= self.guard:
            result = self._restart(True)
            if same and DELAY_RANGE[0] <= gap <= DELAY_RANGE[1]:
                self.candidate = gap  # an OS delay as long as the guard, if fast repeats follow
            return result
        if self.run == 1 and self.candidate is not None and gap <= self.burst_gap:
            self.run = 2  # the long gap that began the run was the OS delay: this is a repeat
        if self.run == 1:
            self.run, self.candidate, self.fast = 2, gap, 0
            if gap < self.min_retap:
                return False, False  # bounce
            self.retap = True
            if self.delay is not None and abs(gap - self.delay) <= self.margin:
                return False, False
            self.fresh = self.delay is not None  # a learned delay rules out the first repeat
            return True, False  # a re-tap, or the first repeat before any delay is learned
        if gap <= self.burst_gap:
            self.run += 1
            self.intervals = (self.intervals + [gap])[-SAMPLES:]
            self._learn(gap)
            return False, False
        return self._restart(False)

    def _press(self, same: bool, gap: float) -> bool | None:
        """The verdict for a press-only action: True a press, False a repeat or a bounce, None maybe the first
        repeat, which is a press only if no repeat follows within hold_gap. A key repeats only after a gap as long
        as an OS delay, so a run of taps faster than that is all presses."""
        stage, self.stage = self.stage, "press"
        if self._learn_slow(same, gap):
            self.stage = "repeat"
            return False
        slow = self.delay is None and self.slow is not None
        delay = self.slow[0] if slow else self.delay
        if not same or gap >= self.guard:
            if same and gap <= DELAY_RANGE[1] and delay is None:
                self.stage = "maybe"  # an OS delay as long as the guard, if repeats follow
            elif same and slow and abs(gap - delay) <= self.margin:
                self.stage = "maybe"  # a slow repeat's delay as long as the guard
                return None
            return True
        if stage != "press" and gap < self.hold_gap:
            self.stage = "repeat"
            return False
        if stage == "repeat":
            return True  # its repeats stopped: a new press
        if gap < self.min_retap:
            return False  # bounce
        if gap < self.first_repeat:
            return True
        if delay is None:
            self.stage = "maybe"
            return True  # at once: until a delay is learned, waiting would hold back most taps
        if abs(gap - delay) <= self.margin:
            self.stage = "maybe"
            return None
        return True

    def _learn_slow(self, same: bool, gap: float) -> bool:
        """After a gap as long as a delay, SLOW_GAPS gaps in a row of 0.11 s to first_repeat that differ by at most
        SLOW_EVEN were an OS repeat too slow for _learn: True for the gap that shows it, which is a repeat. Only while
        no delay is learned (a learned delay means a fast repeat) and only locally (SSH jitter makes gaps uneven)."""
        if not same or gap >= self.first_repeat:
            self.slow_run = [gap] if same and DELAY_RANGE[0] <= gap <= DELAY_RANGE[1] else []
            return False
        if not self.slow_run:
            return False
        self.slow_run.append(gap)
        if len(self.slow_run) > SLOW_GAPS + 1:
            del self.slow_run[1]  # keep the delay and the latest SLOW_GAPS gaps
        delay, *even = self.slow_run
        if len(even) < SLOW_GAPS or self.remote or self.delay is not None:
            return False
        if min(even) <= MAX_INTERVAL or max(even) - min(even) > SLOW_EVEN:
            return False
        self.slow_run = []
        if self.slow is not None:
            delay = 0.7 * self.slow[0] + 0.3 * delay
        self.slow = (delay, sum(even) / len(even))
        return True

    def _restart(self, safe: bool) -> tuple[bool, bool]:
        self.run, self.candidate, self.fast, self.fresh = 1, None, 0, True
        return True, safe

    def _learn(self, gap: float) -> None:
        """A candidate delay followed by 3 fast repeat gaps was the OS delay."""
        if self.candidate is None or not DELAY_RANGE[0] <= self.candidate <= DELAY_RANGE[1]:
            return
        self.fast += gap <= MAX_INTERVAL
        if self.fast >= 3:
            candidate, self.candidate = self.candidate, None
            self.delay = candidate if self.delay is None else 0.7 * self.delay + 0.3 * candidate
