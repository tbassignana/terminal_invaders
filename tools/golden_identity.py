#!/usr/bin/env python3
"""Phase 4's goldens, drawn again at effects off (Phase 5 spec 12.2, gate row G5.4): python3 tools/golden_identity.py
BASE_GOLDEN_DIR [--root DIR] [--list FILE].

It renders, with this tree (the working directory, or --root), every scene of tests/support/frames.py whose golden
file is in BASE_GOLDEN_DIR (Phase 4's tests/golden), with the effects off and the edits the spec names patched back
before any Painter exists: Phase 4's px2 player shot and pellet (tests/support/phase4_art.py), the 1-pixel cargo
mark (arena.CARGO_PIXELS = 1), the mono arena without half blocks drawn in pixels (ui.MONO_GLYPHS = False) and
Phase 4's reduced-motion rule, which hid the effect texts (arena.STILL_TEXTS = False). Every frame must then be
byte-identical to its base file, except the 45 card frames (CARDS), which the spec changes on purpose: the tool lists
those that differ. It fails (exit 1) on a frame outside the card list that differs, and on a base file that no scene
of this tree draws any more. The scenes this tree adds are counted, never compared (they have no base).

So an unintended change at level off cannot hide among the frames regenerated for a named reason.
"""

import argparse
import sys
from pathlib import Path

# The card scenes (spec 12.2): menu (80x24 and 240x70), help, results (80x24 and 180x64), replay-verified,
# replay-mismatch, duel-results and too-small, each in the five tiers: 45 frames.
CARDS = {
    "menu": ("80x24", "240x70"),
    "help": ("80x24",),
    "results": ("80x24", "180x64"),
    "replay-verified": ("80x24",),
    "replay-mismatch": ("80x24",),
    "duel-results": ("80x24",),
    "too-small": ("40x12",),
}


def is_card(name: str) -> bool:
    """Whether a golden file is one of the card frames the spec lets change."""
    return any(name.startswith(f"{scene}-{size}-") for scene, sizes in CARDS.items() for size in sizes)


def patch_back() -> None:
    """Undo the named edits (call before any Painter is built): the drawing is then Phase 4's at effects off."""
    from terminal_invaders import arena, sprites, ui
    from tests.support import frames, phase4_art

    sprites.SPRITES2.update(phase4_art.SPRITES2)
    arena.CARGO_PIXELS = 1
    arena.STILL_TEXTS = False
    ui.MONO_GLYPHS = False
    options = frames.options

    def off(**overrides):
        return options(**overrides | {"effects": "off"})

    frames.options = off


def rendered(names: set[str]) -> dict[str, str]:
    """{file name: golden text} for every scene whose file is in names, each session built once."""
    from tests.support import frames

    found, built = {}, {}
    for name, cols, rows, build, tiers in [*frames.SCENES, *frames.ARCADE_SCENES]:
        wanted = [tier for tier in tiers if frames.filename(name, cols, rows, tier) in names]
        if not wanted:
            continue
        if build not in built:
            built[build] = build()
        for tier in wanted:
            found[frames.filename(name, cols, rows, tier)] = frames.golden(built[build], name, cols, rows, tier)
    return found


def scene_files() -> set[str]:
    """Every golden file name this tree's frames.py lists."""
    from tests.support import frames

    return {
        frames.filename(name, cols, rows, tier)
        for name, cols, rows, _build, tiers in [*frames.SCENES, *frames.ARCADE_SCENES]
        for tier in tiers
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", type=Path, help="Phase 4's golden folder (e.g. /tmp/claude-0/p5/tests/golden)")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the tree to draw with (default: here)")
    parser.add_argument("--list", type=Path, help="write the differing file names here, one a line")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.root.resolve()))
    patch_back()
    base = {path.name: path for path in sorted(args.base.glob("*.txt"))}
    drawn = rendered(set(base))
    same, cards, failures = 0, [], []  # failures: (file name, the line printed)
    for name, path in base.items():
        if name not in drawn:
            failures.append((name, f"no scene draws {name} any more"))
        elif drawn[name] == path.read_text():
            same += 1
        elif is_card(name):
            cards.append(name)
        else:
            failures.append((name, f"differs outside the card list: {name}"))
    new = sorted(scene_files() - set(base))
    print(
        f"{len(base)} Phase 4 frames in {args.base}: {same} identical, {len(cards)} card frames differ,"
        f" {len(failures)} failures; {len(new)} frames are new in this tree (not compared)"
    )
    for name in cards:
        print("card " + name)
    for _name, line in failures:
        print("FAIL " + line)
    if args.list:
        args.list.write_text("".join(f"{name}\n" for name in [*cards, *(name for name, _line in failures)]))
    if not failures:
        print("OK: every frame outside the card list is Phase 4's, byte for byte")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
