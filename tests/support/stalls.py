"""Host stalls beside a pty run, for G1.1 (spec 13, as the spec owner decided it: decision 2).

A sleeper process that does nothing else naps NAP s at a time and measures how far each nap overran. A nap that
overran by at least STALL (one tick) is a stall: for that long the host ran nothing of it. The sleeper is not the game,
and it does not share the game's process or the test's, so what it measures is the machine.

There is one sleeper per CPU, each pinned to its own (Linux). A virtual machine's host can stall one vCPU and not the
others, and a sleeper sees only the vCPU it runs on. With one unpinned sleeper, a G1.1 run failed on a 10-tick frame
whose acks came 160 ms late while the sleeper saw nothing, and the guest's steal time recorded 180 ms on one vCPU.

A frame over 2 ticks is excused when a stall overlaps the time its ticks accumulated: from the moment the frame
before it reached the terminal (or, if earlier, its own arrival less its ticks) to its own arrival, widened by a tick
at each end for the time a frame takes to be written and read.
"""

import bisect
import os
import subprocess
import sys

from terminal_invaders.model import TICK_RATE
from terminal_invaders.term.encode import BSU, ESU

TICK = 1 / TICK_RATE
NAP, STALL = 0.002, TICK
SLEEPER = """
import os, sys, time
nap, stall = float(sys.argv[2]), float(sys.argv[3])
if sys.argv[4:]:
    os.sched_setaffinity(0, {int(sys.argv[4])})
end = time.monotonic() + float(sys.argv[1])
while (before := time.monotonic()) < end:
    time.sleep(nap)
    over = time.monotonic() - before - nap
    if over >= stall:
        print(f"{before + nap:.6f} {before + nap + over:.6f}", flush=True)
"""


def cpus() -> list[int | None]:
    """The CPUs this process may run on, one sleeper for each; [None] (one sleeper, unpinned) where a process cannot
    be pinned."""
    if not hasattr(os, "sched_setaffinity"):
        return [None]
    return sorted(os.sched_getaffinity(0))


class Sleeper:
    """The sleeper processes, one per CPU, for the given seconds. stalls() waits for them and gives each stall as
    (start, end) on the monotonic clock, those that overlap merged into one; seen_by then holds, stall by stall, the
    CPUs whose sleepers saw it."""

    def __init__(self, seconds: float):
        self.cpus = cpus()
        command = [sys.executable, "-c", SLEEPER, str(seconds), str(NAP), str(STALL)]
        self.processes = [
            subprocess.Popen(command + ([] if cpu is None else [str(cpu)]), stdout=subprocess.PIPE, text=True)
            for cpu in self.cpus
        ]
        self.seen_by: list[list[int | None]] = []

    def stalls(self) -> list[tuple[float, float]]:
        seen = []
        for cpu, process in zip(self.cpus, self.processes):
            out = process.communicate()[0]
            seen += [(float(start), float(end), cpu) for start, end in map(str.split, out.splitlines())]
        merged, seen_by = [], []
        for start, end, cpu in sorted(seen, key=lambda stall: stall[:2]):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                seen_by[-1].add(cpu)
            else:
                merged.append((start, end))
                seen_by.append({cpu})
        self.seen_by = [sorted(cpu for cpu in found if cpu is not None) or [None] for found in seen_by]
        return merged

    def stop(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


def esu_times(output: bytes, arrivals: list[tuple[float, int]]) -> list[float]:
    """When each frame's ESU had reached the terminal, frame by frame: arrivals are (when, total bytes read by
    then), one per read."""
    totals = [total for _, total in arrivals]
    times, at = [], output.find(BSU)
    while at >= 0:
        end = output.index(ESU, at) + len(ESU)
        times.append(arrivals[bisect.bisect_left(totals, end)][0])
        at = output.find(BSU, end)
    return times


def span(times: list[float], n: int, ticks: int) -> tuple[float, float]:
    """The time frame n's ticks accumulated, widened by a tick at each end."""
    start = times[n] - ticks * TICK if n == 0 else min(times[n - 1], times[n] - ticks * TICK)
    return start - TICK, times[n] + TICK


def excuse(long: list[tuple[int, int, int]], times: list[float], stalls: list[tuple[float, float]]):
    """Frames over 2 ticks, as (frame, ticks, bytes), split into those a stall excuses, each with (frame, ticks,
    bytes, arrival, the stall), and those none does, each with (frame, ticks, bytes, arrival)."""
    excused, failed = [], []
    for n, ticks, nbytes in long:
        start, end = span(times, n, ticks)
        hit = next(((a, b) for a, b in stalls if a < end and b > start and b - a >= STALL), None)
        if hit:
            excused.append((n, ticks, nbytes, times[n], hit))
        else:
            failed.append((n, ticks, nbytes, times[n]))
    return excused, failed
