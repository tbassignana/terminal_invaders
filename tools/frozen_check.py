#!/usr/bin/env python3
"""The files that must not change (Phase 5 spec 11.1): python3 tools/frozen_check.py BASE_SHA OWNED [--root DIR].

BASE_SHA is the base tree's sha-256 list (`sha256sum` lines, `<digest>  ./<path>`), OWNED the paths of the base tree
that a build stream owns, one a line (written at integration from spec 14.2). The frozen set is computed, not
hand-picked: every path of BASE_SHA, minus OWNED, minus the Campaign goldens (tests/golden/*.txt but arcade-*, which
the build regenerates and tools/golden_identity.py checks instead). The tool prints that list and its count, then
compares every file's sha-256 in the tree (the working directory, or --root) with the base list.

It fails (exit 1) on a changed or missing frozen file; on a count other than --expect (167); on an OWNED list that is
not spec 14.2's (OWNERS below: a path it does not list, or one of its paths left out); on an owned path the base
list lacks; and on any file under the frozen folders (FROZEN_FOLDERS) that the base list lacks. Caches
(__pycache__, *.pyc) are not files of the tree.
"""

import argparse
import hashlib
from pathlib import Path

EXPECT = 167  # spec 11.1: 748 base paths - 39 owned - 542 Campaign goldens
# Spec 14.2: the base tree's files each stream owns (its new files are not in the base list, so not here).
OWNERS = {
    "P": (
        "terminal_invaders/arena.py",
        "terminal_invaders/sprites.py",
        "terminal_invaders/theme.py",
        "terminal_invaders/term/pixels.py",
        "tools/palette_check.py",
        "tests/test_palette.py",
        "tests/test_theme.py",
        "tests/test_sprites.py",
        "tests/test_arena.py",
    ),
    "D": (
        "terminal_invaders/storage.py",
        "terminal_invaders/cli.py",
        "terminal_invaders/app.py",
        "terminal_invaders/term/caps.py",
        "terminal_invaders/term/output.py",
        "pyproject.toml",
        "tests/test_storage_safety.py",
        "tests/test_storage_robustness.py",
        "tests/test_persistence.py",
        "tests/test_app.py",
        "tests/test_packaging.py",
    ),
    "U": (
        "terminal_invaders/ui.py",
        "terminal_invaders/keymap.py",
        "README.md",
        "tests/support/frames.py",
        "tests/test_ui.py",
        "tests/test_keymap.py",
    ),
    "I": (
        "tools/bench_render.py",
        "tools/make_frames.py",
        "tools/term_coverage.py",
        "tests/test_golden.py",
        "tests/test_frame_matches_arena.py",
        "tests/test_render_gate.py",
        "tests/test_terminal.py",
        "tests/test_term_coverage.py",
        "tests/support/faketerm.py",
        "tests/support/ptyrun.py",
        ".github/workflows/ci.yml",
        ".github/workflows/gate.yml",
        "Makefile",
    ),
}
OWNED_PATHS = frozenset(path for paths in OWNERS.values() for path in paths)
# Folders no stream may add a file to (spec 11.1): what is there must be exactly the base tree's.
FROZEN_FOLDERS = (
    "terminal_invaders/campaign",
    "terminal_invaders/legacy",
    "terminal_invaders/arcade",
    "tools/bots",
    "tests/fixtures",
    "tests/arcade",
    "tests/term",
)
GOLDEN = "tests/golden/"


def read_base(path: Path) -> dict[str, str]:
    """{relative path: sha-256} from a sha256sum list (`./` prefixes dropped)."""
    base = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        base[name.strip().removeprefix("./")] = digest.lower()
    return base


def read_owned(path: Path) -> list[str]:
    """The owned paths, one a line; blank lines and # comments are skipped."""
    lines = (line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line.removeprefix("./") for line in lines if line]


def campaign_golden(path: str) -> bool:
    name = path[len(GOLDEN) :]
    return path.startswith(GOLDEN) and "/" not in name and name.endswith(".txt") and not name.startswith("arcade-")


def frozen_paths(base: dict[str, str], owned) -> list[str]:
    """Spec 11.1's set: the base paths no stream owns, but the Campaign goldens."""
    owned = set(owned)
    return sorted(path for path in base if path not in owned and not campaign_golden(path))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cached(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix == ".pyc"


def check(root: Path, base: dict[str, str], owned: list[str], expect: int = EXPECT) -> tuple[list[str], list[str]]:
    """(the frozen list, the failures): one line per failure, none when everything holds."""
    failures = []
    duplicates = sorted({path for path in owned if owned.count(path) > 1})
    failures += [f"owned twice: {path}" for path in duplicates]
    failures += [f"owned but not a spec 14.2 path: {path}" for path in sorted(set(owned) - OWNED_PATHS)]
    failures += [f"a spec 14.2 path missing from the owned list: {path}" for path in sorted(OWNED_PATHS - set(owned))]
    failures += [f"owned but not in the base list: {path}" for path in sorted(set(owned)) if path not in base]
    frozen = frozen_paths(base, owned)
    if len(frozen) != expect:
        failures.append(f"{len(frozen)} frozen files, not {expect}")
    for path in frozen:
        file = root / path
        if not file.is_file():
            failures.append(f"missing: {path}")
        elif sha256(file) != base[path]:
            failures.append(f"changed: {path}")
    for folder in FROZEN_FOLDERS:
        for file in sorted((root / folder).rglob("*")):
            if file.is_file() and not cached(file.relative_to(root)):
                name = file.relative_to(root).as_posix()
                if name not in base:
                    failures.append(f"not in the base list: {name}")
    return frozen, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_sha", type=Path, help="the base tree's sha-256 list")
    parser.add_argument("owned", type=Path, help="the owned paths, one a line")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the tree to check (default: here)")
    parser.add_argument("--expect", type=int, default=EXPECT, help=f"the frozen count (default {EXPECT})")
    parser.add_argument("--quiet", action="store_true", help="leave out the list of frozen files")
    args = parser.parse_args(argv)
    base, owned = read_base(args.base_sha), read_owned(args.owned)
    frozen, failures = check(args.root.resolve(), base, owned, args.expect)
    if not args.quiet:
        for path in frozen:
            print(path)
    goldens = sum(map(campaign_golden, base))
    print(
        f"base {len(base)} - owned {len(set(owned))} - Campaign goldens {goldens} = {len(frozen)} frozen files"
        f" (expected {args.expect}) in {args.root}"
    )
    for line in failures:
        print("FAIL " + line)
    print(f"{len(failures)} failures" if failures else f"OK: all {len(frozen)} frozen files keep their sha-256")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
