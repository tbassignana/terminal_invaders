"""The manual audio check (spec 10, 14 A14/A15/A28), run with the python the game is installed in:

    .venv/bin/python -m terminal_invaders.audio.check [--backend NAME] [--soak SECONDS]

It plays every cue through the real Audio client and helper, as the game does, printing each step to a saved dup of
stdout while fds 1 and 2 point at a temp file (whatever lands there is a stray byte). Then it prints one JSON object
and exits 0 when the platform's pass rules hold, else 1. Listen for as many distinct cues as step 4 counts (26: Arcade
mode's are blast, march1-4, saucer_hit and saucer), and 3 cues after silence that come at once and without a click.
"""

import argparse  # pragma: no cover
import json  # pragma: no cover
import os  # pragma: no cover
import statistics  # pragma: no cover
import sys  # pragma: no cover
import tempfile  # pragma: no cover
import time  # pragma: no cover
from ctypes import CDLL, POINTER, Structure, byref, c_double, c_int32, c_uint32, c_void_p, sizeof  # pragma: no cover

from . import Audio, macos  # pragma: no cover
from .protocol import CUES, STATS_EVERY  # pragma: no cover

CORE_AUDIO = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"  # pragma: no cover
SIZES = {"ASBD": 40, "AQBuffer": 56}  # pragma: no cover


class Address(Structure):  # pragma: no cover
    """AudioObjectPropertyAddress (AudioHardwareBase.h): selector, scope, element; 12 bytes."""

    _fields_ = [("mSelector", c_uint32), ("mScope", c_uint32), ("mElement", c_uint32)]


def device_ms() -> float | None:  # pragma: no cover
    """The default output device's latency + safety offset + IO buffer, in ms at its nominal rate (A15), through
    AudioObjectGetPropertyData; None if any of it fails. Report only."""
    try:
        get = CDLL(CORE_AUDIO).AudioObjectGetPropertyData
    except (OSError, AttributeError):
        return None
    get.restype = c_int32  # OSStatus (object, address, qualifier size, qualifier, io size, out data)
    get.argtypes = [c_uint32, POINTER(Address), c_uint32, c_void_p, POINTER(c_uint32), c_void_p]

    def query(item: int, selector: str, scope: str, value):
        size = c_uint32(sizeof(value))
        address = Address(macos.fourcc(selector), macos.fourcc(scope), 0)  # kAudioObjectPropertyElementMain
        if get(item, byref(address), 0, None, byref(size), byref(value)) or size.value != sizeof(value):
            raise OSError(selector)
        return value.value

    try:
        device = query(1, "dOut", "glob", c_uint32())  # kAudioObjectSystemObject's default output device
        rate = query(device, "nsrt", "glob", c_double())  # kAudioDevicePropertyNominalSampleRate
        frames = query(device, "ltnc", "outp", c_uint32()) + query(device, "saft", "outp", c_uint32())
        frames += query(device, "fsiz", "glob", c_uint32())  # latency, safety offset, buffer frame size
        return round(frames * 1000 / rate, 1) if rate > 0 else None
    except Exception:  # report only: any failure is null
        return None


def summarize(lines: list[str]) -> dict:  # pragma: no cover
    """The helper's stats lines (one per STATS_EVERY s of feeding, each counting its own window) as report fields."""
    stats = [dict(field.split("=", 1) for field in line.split()[1:]) for line in lines if line.startswith("stats ")]
    leads = [[float(part) for part in entry["lead"].split("/")] for entry in stats if entry["lead"] != "-"]
    lead = None
    if leads:
        columns = list(zip(*leads, strict=True))  # min, p5, median, max of each window
        lead = {"min": min(columns[0]), "p5": _median(columns[1]), "med": _median(columns[2]), "max": max(columns[3])}
    counted = [int(entry["underruns"]) for entry in stats if entry["underruns"] != "-"]  # "-": a pipe player
    return {
        "lead_ms": lead,
        "underruns": sum(counted) if counted else None,
        "callbacks_per_s": _median([int(entry["callbacks"]) / STATS_EVERY for entry in stats]),
        "cpu_ms_per_s": _median([float(entry["cpu"]) for entry in stats]),
        "stats_lines": len(stats),
    }


def _median(values) -> float | None:  # pragma: no cover
    return round(statistics.median(values), 1) if values else None


def run(report: dict, say, soak: float) -> None:  # pragma: no cover
    """Steps 3-5: the game's Audio, every cue, cues after silence, the soak (with --soak), close; fills report."""
    lines: list[str] = []
    started = time.monotonic()
    audio = Audio()

    def heard(line: str) -> None:
        lines.append(line)
        say(f"    helper: {line}")
        if line.startswith("backend ") and report["startup_ms"] is None:
            report["startup_ms"] = round((time.monotonic() - started) * 1000, 1)

    def play(seconds: float, cue: str | None = None) -> None:
        end = time.monotonic() + seconds
        audio.update((cue,) if cue else ())
        while (left := end - time.monotonic()) > 0:
            time.sleep(min(left, 1 / 60))  # the game's tick
            audio.update()
            audio.poll_status()

    audio.on_status, pgid = heard, audio.pid
    say("3 Audio(): waiting up to 2 s for the backend")
    deadline = started + 2.0
    while audio.backend is None and audio.available and time.monotonic() < deadline:
        audio.poll_status()
        time.sleep(0.01)
    report["backend"] = audio.backend
    try:
        if audio.backend is not None:
            say(f"4 the {len(CUES)} cues, 0.4 s apart")
            for name, *_ in CUES:
                say(f"  {name}")
                play(0.4, cue=name)
            say("4b 3 cues, each after 3 s of silence (the queue restarts after idling): delay or click?")
            for name in ("hit", "kill", "pickup"):
                play(3)
                say(f"  {name}")
                play(0.6, cue=name)
            if soak > 0:
                say(f"4c soak: 4 cues a second for {soak:g} s")
                for index in range(int(soak * 4)):
                    play(0.25, cue=CUES[index % len(CUES)][0])
    finally:
        say("5 close")
        start = time.monotonic()
        audio.close()
        report["close_ms"] = round((time.monotonic() - start) * 1000, 1)
    # Anything going wrong after the backend opened: a failover, a crash, or no orderly exit.
    after = lines[next((i for i, line in enumerate(lines) if line.startswith("backend ")), len(lines)) + 1 :]
    report["trouble"] = [line for line in after if line.split(" ", 1)[0] in ("fallback", "silent:", "error")]
    report["bye"] = "bye" in after
    try:
        os.killpg(pgid, 0)
        report["group_gone"] = False
    except (ProcessLookupError, PermissionError):  # A1: macOS says EPERM for a group of zombies
        report["group_gone"] = True
    except (OSError, TypeError):
        report["group_gone"] = pgid is None
    report.update(summarize(lines))


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(prog="python -m terminal_invaders.audio.check", description=__doc__.split("\n")[0])
    parser.add_argument("--backend", help="force one backend (INVADERS_AUDIO=NAME), e.g. afplay or pw-cat")
    parser.add_argument(
        "--soak", type=float, default=0.0, metavar="SECONDS", help="also play 4 cues a second this long"
    )
    options = parser.parse_args(argv)
    darwin = sys.platform == "darwin"
    want = (options.backend or ("coreaudio" if darwin else "pulse-simple")).partition(":")[0]
    if options.backend:
        os.environ["INVADERS_AUDIO"] = options.backend
    else:
        os.environ.pop("INVADERS_AUDIO", None)
    out, err = os.dup(1), os.dup(2)

    def say(text: str) -> None:
        os.write(out, (text + "\n").encode("utf-8", "replace"))

    report = {"platform": sys.platform, "backend": None, "startup_ms": None}
    sizes = {kind.__name__: sizeof(kind) for kind in (macos.ASBD, macos.AQBuffer)}
    report["struct_ok"] = sizes == SIZES
    say(f"1 struct sizes {sizes}: {'ok' if report['struct_ok'] else 'WRONG'}")
    say("2 fds 1 and 2 now go to a temp file; anything there is a stray byte")
    sys.stdout.flush()
    sys.stderr.flush()
    with tempfile.TemporaryFile() as trap:
        os.dup2(trap.fileno(), 1)
        os.dup2(trap.fileno(), 2)
        try:
            run(report, say, options.soak)
            report["stray_bytes"] = os.fstat(trap.fileno()).st_size
            if darwin:  # after the count: CoreAudio may log as it loads here
                report["device_ms"] = device_ms()
                med = (report["lead_ms"] or {}).get("med")
                both = med is not None and report["device_ms"] is not None
                report["est_latency_ms"] = round(med + report["device_ms"], 1) if both else None
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(out, 1)
            os.dup2(err, 2)
    lead = report["lead_ms"] or {}
    rules = {
        "backend": report["backend"] == want,
        "helper_ok": report["bye"] and not report["trouble"],
        "stray_bytes": report["stray_bytes"] == 0,
        "group_gone": report["group_gone"],
    }
    if darwin:
        rules |= {"struct_ok": report["struct_ok"], "close_ms": report["close_ms"] <= 350}
        if report["backend"] == "coreaudio":
            cps = report["callbacks_per_s"]
            rules |= {"callbacks_per_s": cps is not None and 95 <= cps <= 105, "underruns": report["underruns"] == 0}
            rules["lead_ms"] = lead.get("min", 0) >= 30
    elif report["backend"] == "pulse-simple":
        rules["lead_ms"] = lead.get("p5", 0) >= 30  # A14
    report["failed"] = [name for name, ok in rules.items() if not ok]
    report["pass"] = not report["failed"]
    print(json.dumps(report))
    return 0 if report["pass"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
