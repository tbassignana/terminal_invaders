"""The terminal stub of ptyrun.ProcessStub, in a process of its own, so nothing in the test process delays an answer.

It does what ptyrun.Stub does in a thread: reads the pty master, feeds MiniVT and answers the queries it saw through
the fixture's Responder. Every byte read is appended to DIR/output and every read to DIR/arrivals, as a line
"<monotonic time> <total bytes read by then>". A sequence MiniVT does not accept is written to DIR/error, and from
then on the stub only keeps bytes, as the thread does. It stops when its stdin closes, once the master holds nothing
more to read.

    python -m tests.support.stubproc MASTER COLS ROWS DIR      (DIR/doc.json: the fixture, or null)
"""

import json
import os
import select
import sys
import time
from pathlib import Path

from tests.support.faketerm import Responder
from tests.support.vt import MiniVT

DRAIN, QUIET = 0.5, 0.02  # once stdin closes: read until the master is QUIET s, for at most DRAIN s


def serve(master: int, vt: MiniVT, responder: Responder, directory: Path, control: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    output, arrivals = os.open(directory / "output", flags), os.open(directory / "arrivals", flags)
    total, failed, stopping = 0, False, None
    while True:
        timeout = None if stopping is None else min(QUIET, max(0.0, stopping - time.monotonic()))
        ready = select.select([master] + ([control] if stopping is None else []), [], [], timeout)[0]
        if control in ready:
            stopping = time.monotonic() + DRAIN  # the test is done: read what is left, then stop
            continue
        if master not in ready or (stopping is not None and time.monotonic() >= stopping):
            return
        try:
            data = os.read(master, 65536)
        except OSError:
            return  # every slave descriptor is closed
        if not data:
            return
        total += len(data)
        os.write(output, data)
        os.write(arrivals, f"{time.monotonic():.6f} {total}\n".encode())
        if failed:
            continue
        try:
            vt.feed(data)
            reply = responder.answer(vt.queries, vt.cprs)
        except AssertionError as exc:  # VTError: a sequence MiniVT does not accept
            (directory / "error").write_text(str(exc), encoding="utf-8")
            failed, reply = True, b""
        if reply:
            os.write(master, reply)


def main() -> int:
    master, cols, rows, directory = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
    doc = json.loads((directory / "doc.json").read_text(encoding="utf-8"))
    serve(master, MiniVT(cols, rows), Responder(doc), directory, sys.stdin.fileno())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
