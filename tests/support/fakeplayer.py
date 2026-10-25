"""A stand-in for pw-cat, pacat and aplay, for the PipeSink and job-control tests.

install(directory) writes the three names into directory as shell wrappers (put it first on PATH); each one runs
    python -I -S fakeplayer.py WRAPPER-PATH ARGS...
so the player sees the argv PipeSink spawned it with, and its pid is the one PipeSink holds.

Environment:
    FAKEPLAYER_MODE  ok (the default), exit1, noread, stall_after[:SECONDS] (0.3 s by default) or chatter; or a
                     comma list of NAME=MODE ("pw-cat=noread,pacat=ok"), where a name not listed plays ok
    FAKEPLAYER_ARGV  a file: each run appends one JSON line {"argv", "mode", "pid", "pgid", "sid"}
    FAKEPLAYER_OUT   a file: every byte the player reads from stdin is appended to it

Modes: ok reads up to 1920 B every 10 ms (192 kB/s, real time) until EOF. exit1 exits 1 at once. noread never reads,
so it fails the health check. stall_after reads like ok for SECONDS, then stops reading. chatter reads like ok and at
each read writes a line to fds 1 and 2 and to /dev/tty; none of it may reach a terminal (a player started in its own
session has no /dev/tty). Every player is killed by SIGALRM after LIFETIME s, so a test that dies leaves nothing behind.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path

NAMES = ("pw-cat", "pacat", "aplay")
LIFETIME = 30  # s
READ, TICK = 1920, 0.010
CHATTER = b"fakeplayer chatter\n"


def install(directory: Path) -> Path:
    """Write the pw-cat, pacat and aplay wrappers into directory; returns it."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        path = directory / name
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I -S "{Path(__file__).resolve()}" "$0" "$@"\n')
        path.chmod(0o755)
    return directory


def mode_for(name: str) -> str:
    spec = os.environ.get("FAKEPLAYER_MODE", "ok")
    if "=" not in spec:
        return spec
    return dict(item.split("=", 1) for item in spec.split(",")).get(name, "ok")


def chatter() -> None:
    for fd in (1, 2):
        try:
            os.write(fd, CHATTER)
        except OSError:
            pass
    try:
        tty = os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return  # ENXIO: no controlling terminal
    try:
        os.write(tty, CHATTER)
    finally:
        os.close(tty)


def main(argv: list[str]) -> int:
    signal.alarm(LIFETIME)
    mode, _, value = mode_for(os.path.basename(argv[0])).partition(":")
    if record := os.environ.get("FAKEPLAYER_ARGV"):
        line = {"argv": argv, "mode": mode, "pid": os.getpid(), "pgid": os.getpgrp(), "sid": os.getsid(0)}
        with open(record, "a", encoding="utf-8") as file:
            file.write(json.dumps(line) + "\n")
    if mode == "exit1":
        return 1
    out = os.environ.get("FAKEPLAYER_OUT")
    out = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND) if out else None
    stall = time.monotonic() + float(value or 0.3) if mode == "stall_after" else float("inf")
    tick = time.monotonic()
    while True:
        if mode != "noread" and time.monotonic() < stall:
            data = os.read(0, READ)
            if not data:
                return 0
            if out is not None:
                os.write(out, data)
            if mode == "chatter":
                chatter()
        tick += TICK
        time.sleep(max(0.0, tick - time.monotonic()))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
