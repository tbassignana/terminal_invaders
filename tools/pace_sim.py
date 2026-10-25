#!/usr/bin/env python3
"""Virtual-time link simulator for the AckPacer: python3 tools/pace_sim.py [--rtt SECONDS --size BYTES].

Frames leave at tick boundaries when the pacer is ready, cross a one-way delay into a bottleneck FIFO, and are
acked one delay after they drain. Keys are typed at random 150-350 ms intervals and slotted to 20 ms, as OpenSSH
does. The metric is key->photon lag in excess of base RTT. Without --rtt it reruns the scenarios behind the table
in spec 8.4 and prints them next to the published numbers.
"""

import argparse
import math
import random
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from terminal_invaders.term.pacing import MODES, TICK, AckPacer  # noqa: E402

RATE = 250_000  # 2 Mbit/s, in bytes per second
SECS = 40.0
WARMUP = 5.0  # frames sent earlier do not count towards fps
JUMP_AT = 10.0


@dataclass(frozen=True, slots=True)
class Result:
    fps: float
    lag_p50: float  # key->photon in excess of base RTT, seconds
    lag_p95: float
    queue_p95: float  # send to drained at the bottleneck, less the one-way delay


def const(size: int):
    return lambda t: size


def jump(small: int, large: int):
    return lambda t: small if t < JUMP_AT else large


# label, base RTT, frame sizes, {mode: (fps, key->photon excess p95 in ms)} as published in spec 8.4 (2 Mbit link)
SCENARIOS = (
    ("RTT 20 ms, 1.15 KB frames", 0.020, const(1150), {"fixed": (60, 36), "adaptive": (60, 36)}),
    ("RTT 50 ms, 1.15 KB", 0.050, const(1150), {"fixed": (30, 66), "adaptive": (60, 35)}),
    ("RTT 100 ms, 1.15 KB", 0.100, const(1150), {"fixed": (17, 114), "adaptive": (60, 37)}),
    ("RTT 250 ms, 1.3 KB", 0.250, const(1300), {"fixed": (7.5, 259), "adaptive": (45, 100)}),
    ("RTT 50 ms, 6.5 KB stress", 0.050, const(6500), {"fixed": (24, 84), "adaptive": (24, 89)}),
    ("RTT 20 ms, 6.5 KB stress (saturated)", 0.020, const(6500), {"fixed": (36, 73), "adaptive": (36, 110)}),
    ("RTT 100 ms, jump 300 B -> 6.5 KB", 0.100, jump(300, 6500), {"fixed": (15, 133), "adaptive": (22, 133)}),
)


def run(mode: str, rtt: float, sizes, *, rate: float = RATE, secs: float = SECS, slot=0.02, seed=1) -> Result:
    """One link and pacer; sizes(t) is the size of a frame built at time t."""
    rng = random.Random(seed)
    delay = rtt / 2
    typed, at = [], 0.37
    while at < secs:
        typed.append(at)
        at += rng.uniform(0.15, 0.35)
    pacer = AckPacer(mode)
    acks, waiting, frames, lags = deque(), [], [], []
    t, link_free, typed_index = 0.0, 0.0, 0
    while t < secs:
        while acks and acks[0] <= t:
            pacer.acked(acks.popleft())
        pacer.expire(t)
        while typed_index < len(typed) and math.ceil(typed[typed_index] / slot) * slot + delay <= t:
            waiting.append(typed[typed_index])
            typed_index += 1
        if pacer.ready(t):
            size = sizes(t)
            pacer.sent(t, size, full=False)
            link_free = max(t + delay, link_free) + size / rate
            frames.append((t, link_free))
            acks.append(link_free + delay)
            lags += [link_free - key - rtt for key in waiting]
            waiting.clear()
        t += TICK  # accumulated, as in the evidence simulator, so its published numbers reproduce exactly
    steady = [(sent, done) for sent, done in frames if sent > WARMUP]
    queue = sorted(done - sent - delay for sent, done in steady)
    lags = sorted(lags[5:])
    return Result(len(steady) / (secs - WARMUP), _pct(lags, 0.5), _pct(lags, 0.95), _pct(queue, 0.95))


def _pct(values: list[float], p: float) -> float:
    return values[min(len(values) - 1, int(p * len(values)))] if values else math.nan


def table(modes=("fixed", "adaptive")) -> list[tuple[str, str, Result, tuple[float, float]]]:
    """(label, mode, measured, published) for every scenario of spec 8.4."""
    return [
        (label, mode, run(mode, rtt, sizes), expect[mode]) for label, rtt, sizes, expect in SCENARIOS for mode in modes
    ]


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rtt", type=float, help="base RTT in seconds: run one link for every mode")
    parser.add_argument("--size", type=int, default=1150, help="frame bytes (default 1150)")
    parser.add_argument("--jump", type=int, metavar="BYTES", help=f"frame bytes from {JUMP_AT:.0f} s on")
    parser.add_argument("--rate", type=float, default=RATE, help="bottleneck bytes per second (default 2 Mbit)")
    parser.add_argument("--secs", type=float, default=SECS, help="simulated seconds (default 40)")
    options = parser.parse_args(argv)
    if options.rtt is None:
        print(f"{'2 Mbit link':38s} | mode     |  fps  p95 ms | spec fps  p95 ms")
        for label, mode, result, (fps, p95) in table():
            print(f"{label:38s} | {mode:8s} | {result.fps:4.1f} {result.lag_p95 * 1000:7.0f} | {fps:8.1f} {p95:7.0f}")
        return
    sizes = const(options.size) if options.jump is None else jump(options.size, options.jump)
    for mode in MODES:
        result = run(mode, options.rtt, sizes, rate=options.rate, secs=options.secs)
        print(
            f"{mode:8s} {result.fps:5.1f} fps  key->photon excess p50 {result.lag_p50 * 1000:.0f} ms"
            f"  p95 {result.lag_p95 * 1000:.0f} ms  queue p95 {result.queue_p95 * 1000:.0f} ms"
        )


if __name__ == "__main__":
    main()
