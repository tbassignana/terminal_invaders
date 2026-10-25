#!/usr/bin/env python3
"""The same games in two trees (Phase 5 spec 11.3): python3 tools/same_games.py BASE_OUT NEW_OUT [--expect N].

Each OUT is a tools/balance.py --out directory, written by the same command in the base tree and in the new one:

    python tools/balance.py --bots strong --difficulty normal hard --input kitty latched timed latched-ssh \\
      --seeds 10 --start-seed 9000 --until 6 --jobs 2 --out OUT

Every game's row (one JSON line per game in the shard files, *.jsonl but audit-*.jsonl) is matched by its `job` and
compared field by field after dropping the fields that time the run or name the build (IGNORED). The tool prints the
first difference of each differing game and fails (exit 1) on a difference, on a game missing from NEW_OUT or extra
in it, on an error row (a line that is not a JSON object with a `job`, a row with an `error`, or two rows for one
job), and on a game count other than --expect in either directory.
"""

import argparse
import json
import sys
from pathlib import Path

# What a game's row holds that is not the game: its wall-clock time, the fingerprint of the code that played it
# (balance.py build(), which every changed file moves), and --profile's per-stage timings (the cost fields).
IGNORED = ("wall", "build", "profile")


def rows(directory: Path) -> tuple[dict[str, dict], list[str]]:
    """{job: row} from the directory's shard files, and the error lines (file:line and why)."""
    found, errors = {}, []
    files = sorted(path for path in Path(directory).glob("*.jsonl") if not path.name.startswith("audit-"))
    if not files:
        errors.append(f"{directory}: no shard file (*.jsonl)")
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            where = f"{path.name}:{number}"
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                errors.append(f"{where}: not JSON ({exc})")
                continue
            if not isinstance(row, dict) or not isinstance(row.get("job"), str):
                errors.append(f"{where}: not a game row (no job)")
                continue
            if "error" in row:
                errors.append(f"{where}: {row['job']} is an error row: {row['error']}")
                continue
            if row["job"] in found:
                errors.append(f"{where}: a second row for {row['job']}")
                continue
            found[row["job"]] = row
    return found, errors


def game(row: dict) -> dict:
    """The row without the fields of IGNORED."""
    return {key: value for key, value in row.items() if key not in IGNORED}


def first_difference(base, new, path: str = "") -> str | None:
    """Where two JSON values first differ, as `path: base != new`, or None when they are equal."""
    if type(base) is not type(new):
        return f"{path or '.'}: {base!r} != {new!r}"
    if isinstance(base, dict):
        for key in sorted(set(base) | set(new), key=str):
            if key not in base or key not in new:
                which = "base" if key not in base else "new"
                return f"{path}.{key}: missing in {which}"
            found = first_difference(base[key], new[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(base, list):
        for index, (one, other) in enumerate(zip(base, new)):
            found = first_difference(one, other, f"{path}[{index}]")
            if found:
                return found
        if len(base) != len(new):
            return f"{path}: {len(base)} items != {len(new)}"
        return None
    return None if base == new else f"{path or '.'}: {base!r} != {new!r}"


def compare(base_dir: Path, new_dir: Path, expect: int | None = None) -> tuple[list[str], dict]:
    """(failure lines, counts) for two balance.py output directories."""
    base, base_errors = rows(base_dir)
    new, new_errors = rows(new_dir)
    failures = [f"error row in BASE {line}" for line in base_errors] + [
        f"error row in NEW {line}" for line in new_errors
    ]
    for name, found in (("BASE", base), ("NEW", new)):
        if expect is not None and len(found) != expect:
            failures.append(f"{name} holds {len(found)} games, not {expect}")
    missing, extra = sorted(set(base) - set(new)), sorted(set(new) - set(base))
    failures += [f"missing from NEW: {job}" for job in missing] + [f"extra in NEW: {job}" for job in extra]
    differ = 0
    for job in sorted(set(base) & set(new)):
        found = first_difference(game(base[job]), game(new[job]))
        if found:
            differ += 1
            failures.append(f"differs: {job} {found}")
    counts = dict(base=len(base), new=len(new), compared=len(set(base) & set(new)), differ=differ)
    counts |= dict(missing=len(missing), extra=len(extra), errors=len(base_errors) + len(new_errors))
    return failures, counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", type=Path, help="balance.py --out directory of the base tree")
    parser.add_argument("new", type=Path, help="balance.py --out directory of the new tree")
    parser.add_argument("--expect", type=int, help="the number of games each directory must hold")
    args = parser.parse_args(argv)
    failures, counts = compare(args.base, args.new, args.expect)
    print("ignored fields: " + ", ".join(IGNORED))
    print(
        "{compared} games compared ({base} in BASE, {new} in NEW): {differ} differ, {missing} missing, {extra} extra,"
        " {errors} error rows".format(**counts)
    )
    for line in failures:
        print("FAIL " + line)
    if not failures:
        print("OK: the same games")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
