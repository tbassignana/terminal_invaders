#!/usr/bin/env python3
"""Cue latency and tone dropouts on the null sink (gates G2.2, G2.3): python3 tools/audio_latency.py --backend NAME
[NAME ...] [--runs 3] [--tone].

Each run plays through the game's real Audio client and helper with INVADERS_AUDIO=NAME on the default sink, which
must be the null sink "game", and records game.monitor through a pa_simple record stream in 96-frame reads. A
recorded sample's time is its read's end, less the record stream's latency, less its offset within the read.
Without --tone a run sends 20 `hit` cues 0.35 s apart plus 0-50 ms at random; a cue's latency runs from its update()
to the first |x| > 2000 at or after the read in progress when it was sent. With --tone a run plays a 440 Hz test tone
(INVADERS_AUDIO_TONE=440) for 11 s and counts the 2 ms windows whose RMS is under 30% of the tone's. A run whose
recorder saw a gap over 100 ms between reads (a VM stall) is run again once (spec 14 A13). It refuses to start while
other streams play, and prints one JSON line per backend: latency median, p90 and max in ms, cues heard and stale,
dropped windows, and the helper's stats (CPU ms/s, lead p5 in ms, underruns).
"""

import argparse
import bisect
import json
import math
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from array import array
from ctypes import CDLL, POINTER, byref, c_int, c_size_t, c_void_p, create_string_buffer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from terminal_invaders.audio import Audio  # noqa: E402
from terminal_invaders.audio.linux import NONE, PULSE, Attr, Spec, bind  # noqa: E402
from terminal_invaders.audio.protocol import RATE  # noqa: E402

MONITOR = b"game.monitor"
FRAG = 96  # frames per record read: 2 ms, which is also the dropout window
CUES, SPACING, JITTER = 20, 0.35, 0.05  # s
ONSET = 2000  # |x| over this is a cue's first sound (hit peaks at 5000)
TONE_SECONDS, DROP = 11.0, 0.3  # a window under DROP x the tone's RMS is a dropout
GAP = 0.100  # s between two recorder reads that makes a run count as stalled
READY = 2.0  # s to wait for the helper's backend line


class Recorder(threading.Thread):
    """Reads the monitor: the left channel, and for each read the time its last sample was at the sink."""

    def __init__(self, lib) -> None:
        super().__init__(daemon=True)
        self.lib, self.err, self.stopping = lib, c_int(), False
        spec, attr = Spec(3, RATE, 2), Attr(NONE, NONE, NONE, NONE, FRAG * 4)
        self.handle = lib.pa_simple_new(
            None, b"audio_latency", 2, MONITOR, b"monitor", byref(spec), None, byref(attr), byref(self.err)
        )  # 2: PA_STREAM_RECORD
        if not self.handle:
            raise SystemExit(f"cannot record {MONITOR.decode()} (error {self.err.value})")
        self.samples, self.reads, self.gap = array("h"), [], 0.0

    def run(self) -> None:
        buf, last = create_string_buffer(FRAG * 4), None
        while not self.stopping and self.lib.pa_simple_read(self.handle, buf, len(buf), byref(self.err)) >= 0:
            now = time.monotonic()
            latency = self.lib.pa_simple_get_latency(self.handle, byref(self.err)) / 1e6
            self.samples.extend(array("h", buf.raw)[0::2])
            self.reads.append(now - latency)
            if last is not None:
                self.gap = max(self.gap, now - last)
            last = now
        self.lib.pa_simple_free(self.handle)

    def at(self, j: int) -> float:
        """When sample j was at the sink."""
        return self.reads[j // FRAG] - (FRAG - 1 - j % FRAG) / RATE

    def finish(self) -> None:
        self.stopping = True
        self.join(2.0)


def latencies(rec: Recorder, sent: list[float]) -> list[float | None]:
    """Each cue's latency in ms, or None when nothing loud came before the next cue was sent."""
    out = []
    for k, cue in enumerate(sent):
        until = sent[k + 1] if k + 1 < len(sent) else math.inf
        j = max(0, bisect.bisect_left(rec.reads, cue) - 1) * FRAG  # the read in progress when the cue was sent
        while j < len(rec.samples) and abs(rec.samples[j]) <= ONSET and rec.at(j) < until:
            j += 1
        out.append(1000 * (rec.at(j) - cue) if j < len(rec.samples) and rec.at(j) < until else None)
    return out


def dropouts(rec: Recorder) -> int:
    """2 ms windows under DROP x the tone's RMS in the last TONE_SECONDS of the tone."""
    x = rec.samples
    rms = [math.sqrt(sum(v * v for v in x[i : i + FRAG]) / FRAG) for i in range(0, len(x) - FRAG + 1, FRAG)]
    loud = [k for k, value in enumerate(rms) if value > ONSET]
    if not loud:
        return -1  # no tone at all
    window = rms[max(0, loud[-1] + 1 - int(TONE_SECONDS * RATE / FRAG)) : loud[-1] + 1]
    level = statistics.median(window)
    return sum(value < DROP * level for value in window)


def once(lib, backend: str, tone: bool) -> dict:
    """One run: start the recorder and the game's Audio, play, close, and measure."""
    rec, lines = Recorder(lib), []
    rec.start()
    time.sleep(0.3)
    if tone:
        os.environ["INVADERS_AUDIO_TONE"] = "440"  # the helper plays it from its first block until it quits
    try:
        audio = Audio()
    finally:
        os.environ.pop("INVADERS_AUDIO_TONE", None)
    audio.on_status = lines.append
    sent = []
    try:
        deadline = time.monotonic() + READY
        while audio.available and audio.backend is None and time.monotonic() < deadline:
            audio.poll_status()
            time.sleep(0.005)
        if audio.backend != backend:
            raise SystemExit(f"{backend} did not start: {lines}")
        due = time.monotonic() + 0.3
        end = due + (TONE_SECONDS + 0.5 if tone else CUES * (SPACING + JITTER))
        while time.monotonic() < end and (tone or len(sent) < CUES):
            if not tone and time.monotonic() >= due:
                sent.append(time.monotonic())
                audio.update(("hit",))
                due = sent[-1] + SPACING + random.uniform(0, JITTER)
            else:
                audio.update()
            time.sleep(0.005)  # the game's frame loop, roughly; each cue's time is taken as it is sent
        time.sleep(0.4)
        audio.poll_status()
    finally:
        audio.close()
        rec.finish()
    stats = [dict(item.split("=", 1) for item in line.split()[1:]) for line in lines if line.startswith("stats ")]
    run = {
        "gap_ms": round(1000 * rec.gap, 1),
        "stale": sum(line.startswith("stale") for line in lines),
        "cpu_ms_s": [float(s["cpu"]) for s in stats],
        "lead_p5_ms": [float(s["lead"].split("/")[1]) for s in stats if s["lead"] != "-"],  # lead=MIN/P5/MED/MAX
        "underruns": sum(int(s["underruns"]) for s in stats if s["underruns"] != "-"),  # "-": a pipe player
    }
    if tone:
        run["dropped"] = dropouts(rec)
    else:
        run["latency_ms"] = [None if value is None else round(value, 1) for value in latencies(rec, sent)]
        run["heard"] = sum(value is not None for value in run["latency_ms"])
    return run


def measure(lib, backend: str, runs: int, tone: bool) -> dict:
    """runs runs of one backend (each rerun once after a recorder gap), and their summary."""
    os.environ["INVADERS_AUDIO"] = backend
    done, reruns = [], 0
    for _ in range(runs):
        run = once(lib, backend, tone)
        if run["gap_ms"] > 1000 * GAP:
            reruns += 1
            run = once(lib, backend, tone)
        done.append(run)
    leads = [lead for run in done for lead in run["lead_p5_ms"]]
    summary = {"backend": backend, "tone": tone, "reruns": reruns, "runs": done,
               "cpu_ms_s": round(statistics.median(cpu for run in done for cpu in run["cpu_ms_s"]), 1),
               "lead_p5_ms": {"min": min(leads), "median": statistics.median(leads)} if leads else None,
               "underruns": sum(run["underruns"] for run in done)}  # fmt: skip
    if tone:
        summary["dropped"] = [run["dropped"] for run in done]
        return summary
    heard = sorted(value for run in done for value in run["latency_ms"] if value is not None)
    summary |= {"cues": runs * CUES, "heard": len(heard), "stale": sum(run["stale"] for run in done)}
    if heard:
        p90 = heard[math.ceil(0.9 * len(heard)) - 1]
        summary |= {"median_ms": round(statistics.median(heard), 1), "p90_ms": p90, "max_ms": heard[-1]}
    return summary


def others() -> list[str]:
    """The server's playback streams, one line each."""
    listing = subprocess.run(["pactl", "list", "short", "sink-inputs"], capture_output=True, text=True, timeout=5)
    return listing.stdout.split("\n")[:-1] if listing.returncode == 0 else ["pactl failed: " + listing.stderr]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--backend", nargs="+", required=True, help="pulse-simple, pw-cat, pacat or aplay")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--tone", action="store_true", help="count dropouts in 11 s of a 440 Hz tone")
    options = parser.parse_args(argv)
    if busy := others():
        print("other streams are playing; stop them first:", *busy, sep="\n  ", file=sys.stderr)
        return 2
    read = {"pa_simple_read": (c_int, [c_void_p, c_void_p, c_size_t, POINTER(c_int)])}
    lib = bind(CDLL("libpulse-simple.so.0"), PULSE | read)
    for backend in options.backend:
        print(json.dumps(measure(lib, backend, options.runs, options.tone)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
