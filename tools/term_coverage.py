#!/usr/bin/env python3
"""The per-module floor of spec 11.6 and Phase 4 §23: every terminal_invaders/term and terminal_invaders/campaign
module at least 90% covered; and, with --also, the named modules of terminal_invaders held to --min (Phase 5 G5.3).

CI runs pytest with --cov-report=json and then

    python3 tools/term_coverage.py coverage.json --also fx settings tapes screens tutorial attract --min 90

which exits 1 naming each module under its floor (or when the report has no modules of one of those packages at
all, or lacks a module --also names, so a moved or renamed module cannot pass). A module of --also is a name under
terminal_invaders, dotted for a subpackage's (`term.pixels`).
"""

import argparse
import json
import sys
from pathlib import PurePosixPath

FLOOR = 90.0
PACKAGES = ("term", "campaign")  # subpackages of terminal_invaders held to the floor, module by module


def package_of(name: str) -> str | None:
    """Which of PACKAGES the file belongs to (directly, not in a subpackage of it), or None."""
    parts = PurePosixPath(name.replace("\\", "/")).parent.parts
    if len(parts) >= 2 and parts[-2] == "terminal_invaders" and parts[-1] in PACKAGES:
        return parts[-1]
    return None


def floored_modules(report: dict) -> dict[str, float]:
    """Percent covered by file, for the files of PACKAGES."""
    return {
        name: data["summary"]["percent_covered"]
        for name, data in report.get("files", {}).items()
        if package_of(name) is not None
    }


def module_path(module: str) -> tuple[str, ...]:
    """The path parts under terminal_invaders of a module named as --also names it (`fx`, `term.pixels`)."""
    parts = module.replace("/", ".").removesuffix(".py").split(".")
    return (*parts[:-1], parts[-1] + ".py")


def named_modules(report: dict, modules) -> dict[str, tuple[str, float] | None]:
    """For each module named: (its file in the report, percent covered), or None when the report lacks it."""
    found = {}
    for module in modules:
        want = ("terminal_invaders", *module_path(module))
        found[module] = None
        for name, data in report.get("files", {}).items():
            if PurePosixPath(name.replace("\\", "/")).parts[-len(want) :] == want:
                found[module] = (name, data["summary"]["percent_covered"])
                break
    return found


def short(report: dict, floor: float = FLOOR, also=(), also_min: float = FLOOR) -> list[str]:
    """One line per module under floor, and one per package with no modules in the report; then each module of also
    that the report lacks or that is under also_min."""
    found = floored_modules(report)
    present = {package_of(name) for name in found}
    missing = [
        f"no terminal_invaders/{package} modules in the report" for package in PACKAGES if package not in present
    ]
    under = [f"{name}: {percent:.1f}% < {floor:g}%" for name, percent in sorted(found.items()) if percent < floor]
    for module, seen in named_modules(report, also).items():
        if seen is None:
            missing.append(f"no terminal_invaders/{'/'.join(module_path(module))} in the report")
        elif seen[1] < also_min:
            under.append(f"{seen[0]}: {seen[1]:.1f}% < {also_min:g}%")
    return missing + under


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", nargs="?", default="coverage.json", help="coverage's JSON report")
    parser.add_argument("--also", nargs="+", default=[], metavar="MODULE", help="more modules held to --min")
    parser.add_argument("--min", type=float, default=FLOOR, help=f"the floor of --also's modules (default {FLOOR:g})")
    args = parser.parse_args(argv)
    with open(args.report, encoding="utf-8") as handle:
        failures = short(json.load(handle), also=args.also, also_min=args.min)
    for line in failures:
        print(line, file=sys.stderr)
    if not failures:
        print(f"every term and campaign module covered at least {FLOOR:g}%")
        if args.also:
            print(f"{', '.join(args.also)} covered at least {args.min:g}%")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
