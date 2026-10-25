#!/usr/bin/env python3
"""Regenerate the golden frames in tests/golden from the deterministic sessions in tests/support/frames.py.

Every changed frame is a visible change to a screen: review the diff before committing it. Each file written is
listed with its old and new sha-256 (Phase 5 spec 12.2), each new one with its sha-256 and each removed one with
the sha-256 it had, then a count of each; --check writes nothing (it says "would write" and "would remove") and
exits 1 when a file would change. The Arcade scenes fly tests/fixtures/golden-arcade.json (tools/make_golden.py
writes it); --campaign leaves their arcade-* files alone.
"""

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.support.frames import GOLDEN, frames  # noqa: E402


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=GOLDEN)
    parser.add_argument("--campaign", action="store_true", help="only the campaign's scenes")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if a file would change")
    options = parser.parse_args(argv)
    if not options.check:
        options.out.mkdir(parents=True, exist_ok=True)
    stale = {path.name for path in options.out.glob("*.txt")}
    if options.campaign:
        stale = {name for name in stale if not name.startswith("arcade-")}
    counts = dict(changed=0, new=0, removed=0, unchanged=0)
    wrote, removed = ("would write", "would remove") if options.check else ("wrote", "removed")
    for name, content in frames(arcade=not options.campaign):
        path, data = options.out / name, content.encode()
        old = path.read_bytes() if path.exists() else None
        stale.discard(name)
        if old == data:
            counts["unchanged"] += 1
            continue
        if not options.check:
            path.write_bytes(data)
        if old is None:
            counts["new"] += 1
            print(f"{wrote} {name}  new {sha256(data)}")
        else:
            counts["changed"] += 1
            print(f"{wrote} {name}  {sha256(old)} -> {sha256(data)}")
    for name in sorted(stale):
        path = options.out / name
        old = path.read_bytes()
        if not options.check:
            path.unlink()
        counts["removed"] += 1
        print(f"{removed} {name}  {sha256(old)}")
    print("{changed} changed, {new} new, {removed} removed, {unchanged} unchanged".format(**counts))
    if options.check and counts["changed"] + counts["new"] + counts["removed"]:
        print("FAIL: the goldens are not what frames.py draws (run make frames, then review the diff)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
