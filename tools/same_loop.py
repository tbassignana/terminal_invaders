#!/usr/bin/env python3
"""The same game through the real loop in two trees (Phase 5 spec 11.3): python3 tools/same_loop.py TREE OUT, then
python3 tools/same_loop.py --compare OUT_A OUT_B.

A run imports the game from TREE (first on sys.path: a scratch copy of the base tree, or the integrated one) and
plays it the way a person launches it: cli.main with a fresh --profile in a fresh HOME, --seed, --no-sound and
--record, its terminal tests/support/faketerm.py's FakeTerminal answering as a fixture's terminal (a virtual clock,
so it runs as fast as the CPU allows). The keys come from input.VirtualInput (macOS auto-repeat on a legacy fixture)
and input.IntentDriver, at fixed virtual times: Enter on LAUNCH at LAUNCH_AT, then a fixed in-play stream (moves,
fire, a bomb, a pause and its resume, keyed for the fixture's scheme) until the run ends (a game over or a win), or
CAP seconds of play (--cap). The run writes OUT/<fixture>.json: the recorded tape and every tick's digest of the
run's game, with what it saw of the Phase 5 parts (how many ticks a Coach observed, whether a title demo played,
which settings Hooks.apply applied).

Before LAUNCH_AT a Phase 5 tree can be shown more without moving the game: --demo lets the title sit idle until its
demo starts (INVADERS_ATTRACT_IDLE, which a base tree ignores) and a key ends it; --settings opens SETTINGS from the
title menu, changes the palette, saves with Esc and goes back to LAUNCH (a tree without the row skips it). The game
starts at the same virtual moment either way, so the tapes and digests of a base run and a Phase 5 run must be
identical: --compare prints the first difference and exits 1 on any. A run also fails when it did not show what it
is for, in a tree that has the part (missing()): the Coach observed no tick, --demo played no demo, or --settings
saved no palette or Hooks.apply did not apply it.

Each fixture runs in a process of its own, so module caches never carry from one run into the other.
"""

from __future__ import annotations

import argparse
import atexit
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FIXTURES = {"apple-terminal-26": (80, 24), "kitty": (120, 44)}  # spec 11.3: tap controls, and hold at k = 2
SEED = 4242
# The title's timeline (virtual seconds). The title shows at about 1 s; with --demo its demo starts DEMO_IDLE seconds
# later and a key ends it at DEMO_END; --settings visits SETTINGS from DEMO_END + 0.5 to about 18 s. The idle timer
# restarts at the last key, so no second demo starts before LAUNCH_AT (DEMO_END + DEMO_IDLE > LAUNCH_AT).
LAUNCH_AT = 19.0  # Enter on LAUNCH
DEMO_IDLE = 8  # INVADERS_ATTRACT_IDLE for --demo: the title demo starts after 8 idle seconds
DEMO_END = 12.0  # the key that ends the demo
CAP = 900.0  # seconds of play at most (a run that has not ended by then stops: its `end` is "cap")
GUARD = 0.9  # seconds between two taps of one key on a legacy terminal (above the KeyGate's 0.70 s repeat guard)
LOOK = 0.25  # how often the run looks for the game's end
CHILD = "--child"
ENDED = ("game_over", "victory")


# --- the in-play key stream -----------------------------------------------------------------------------------------

# (seconds after LAUNCH, intent): "move" with a direction (-1, 0, 1), "fire" on or off, "bomb", or "pause" (P).
STREAM = (
    (1.0, "fire", True),
    (1.5, "move", 1),
    *((3.0 + 2.5 * n, "move", (1, -1, 0, -1, 1, 0)[n % 6]) for n in range(16)),
    (22.0, "pause", None),
    (24.0, "pause", None),  # resume
    (26.0, "bomb", None),
    (30.0, "fire", False),  # then the fleet comes down: a run that lasts ends by landing or a hit
    *((32.0 + 4.0 * n, "move", (0, 1, 0, -1)[n % 4]) for n in range(60)),
    (60.0, "fire", True),
    (64.0, "fire", False),
    (90.0, "bomb", None),
)


def key_bytes(protocol: str, scheme: str, launch: float, end: float, extra=()) -> list[tuple[float, bytes]]:
    """(arrival, bytes) of every key: the extra taps (name, time) of a prelude, Enter at `launch`, then STREAM."""
    from terminal_invaders.input import IntentDriver, VirtualInput
    from terminal_invaders.term.simkeys import PROFILES

    vin = VirtualInput(protocol, PROFILES["macos-default"])
    for name, at in extra:
        vin.tap(name, at)
    vin.tap("enter", launch)
    driver = IntentDriver(scheme, vin)
    held, direction = set(), 0
    for after, intent, value in STREAM:
        tick = round((launch + after) * 60)
        if intent == "fire":
            held = held | {"fire"} if value else held - {"fire"}
        elif intent == "move":
            direction = value
        elif intent == "bomb":
            driver.pulse("bomb", tick)
            continue
        elif intent == "pause":
            vin.tap("p", tick / 60)
            continue
        moves = {1: {"right"}, -1: {"left"}, 0: set()}[direction]
        driver.intend(tick, frozenset((held - {"left", "right"}) | moves))
    return vin.bytes_until(end)


# --- one run, in a child process with the tree first on sys.path ----------------------------------------------------


def child(tree: Path, fixture: str, out: Path, demo: bool, settings: bool, cap: float = CAP) -> None:
    sys.path.insert(0, str(tree))
    os.chdir(tree)
    from unittest import mock

    from terminal_invaders import app, cli, ui
    from terminal_invaders.term.keys import Key
    from terminal_invaders.term.simkeys import encode
    from tests.support.faketerm import FakeTerminal
    from tests.support.terminals import load

    doc = load(fixture)
    cols, rows = FIXTURES[fixture]
    kitty = doc["keys"]["protocol"] == "kitty"
    scheme = "hold" if doc["expect"].get("release_events") else "latched"
    fake = FakeTerminal(doc, cols=cols, rows=rows, rtt=0.001, vt=(max(cols, 200), max(rows, 80)))
    fake.limit = LAUNCH_AT + cap + 60.0
    fake.fd = os.open(os.devnull, os.O_RDWR)  # app.run closes it; FakeTerminal only compares it
    seen = {"session": None, "digests": [], "observed": 0, "demo": False, "applied": [], "settings": "not asked"}

    original_step = ui.Session.step

    def step(self, now):
        game = self.pilot.game if self.pilots else None
        before = game.tick if game is not None else None
        original_step(self, now)
        if self is seen["session"] and game is not None and game.tick != before:
            seen["digests"].append((game.tick, game.digest()))

    patches = [mock.patch.object(ui.Session, "step", step)]
    try:
        from terminal_invaders import tutorial
    except ImportError:
        tutorial = None
    if tutorial is not None:
        original_observe = tutorial.Coach.observe

        def observe(self, *args, **kwargs):
            seen["observed"] += 1
            return original_observe(self, *args, **kwargs)

        patches.append(mock.patch.object(tutorial.Coach, "observe", observe))
    if hasattr(app, "Hooks") and hasattr(app.Hooks, "apply"):
        original_apply = app.Hooks.apply

        def apply(self, name):
            seen["applied"].append(name)
            return original_apply(self, name)

        patches.append(mock.patch.object(app.Hooks, "apply", apply))
    original_play = app.play

    def play(session, term, **kwargs):
        seen["session"] = session
        return original_play(session, term, **kwargs)

    patches += [
        mock.patch.object(app, "play", play),
        mock.patch.object(app, "open_tty", lambda *args: fake.fd),
        mock.patch.object(app, "user_env", lambda *args: dict(doc["env"])),
        mock.patch.object(app, "TerminalSession", functools.partial(app.TerminalSession, ops=fake.ops())),
        mock.patch.object(cli, "require_terminal", lambda command: None),
    ]

    def tap(name: str, at: float) -> None:
        """One key, typed at `at`, as this terminal sends it (a legacy one sends no release)."""
        from terminal_invaders.term.keys import RELEASE

        fake.type(encode(Key(name), kitty=kitty), at=at)
        if kitty:
            fake.type(encode(Key(name, kind=RELEASE), kitty=kitty), at=at + 0.06)

    def screen_rows() -> list[str]:
        return fake.vt.lines()[:rows]

    def menu_rows() -> tuple[dict, list]:
        lines = screen_rows()
        return {line.strip().lstrip("> ").split("  ")[0].strip(): n for n, line in enumerate(lines)}, [
            n for n, line in enumerate(lines) if line.strip().startswith(">")
        ]

    def prelude_settings() -> None:
        """Open SETTINGS from the title menu (Down from LAUNCH: the menu stops at its ends), change the palette, save
        with Esc and come back to LAUNCH (Up). Each way takes turns among the menu's keys for it, so no key comes
        again within GUARD on a legacy terminal and the visit fits before LAUNCH_AT."""
        row_of, cursor = menu_rows()
        if "SETTINGS" not in row_of or "LAUNCH" not in row_of or "QUIT" not in row_of:
            seen["settings"] = "no SETTINGS row"
            return
        if cursor != [row_of["LAUNCH"]]:
            seen["settings"] = f"menu not as expected (cursor rows {cursor})"
            return
        steps = row_of["SETTINGS"] - row_of["LAUNCH"]  # rows down from LAUNCH to SETTINGS
        downs, ups = ("down", "s", "j"), ("up", "w", "k")  # the default menu's keys: three each
        gap = GUARD / len(downs)  # the same key again after GUARD
        at = fake.now + 0.2
        for n in range(steps):
            tap(downs[n % len(downs)], at + n * gap)
        at += steps * gap
        tap("enter", at)
        tap("right", at + 0.5)  # PALETTE, the card's first row: the next palette
        tap("escape", at + 2.0)  # saves (the palette applied after 0.5 s still) and goes back to the menu
        at += 2.5
        for n in range(steps):
            tap(ups[n % len(ups)], at + n * gap)
        seen["settings"] = f"asked: {steps} rows down and back"

    def ready() -> None:
        """Just before LAUNCH_AT: the title menu shows, no demo, its cursor on LAUNCH; else the run is not valid."""
        session, (row_of, cursor) = seen["session"], menu_rows()
        if session is None or getattr(session, "attract", None) is not None or cursor != [row_of.get("LAUNCH")]:
            seen["unready"] = f"the title is not ready to launch (demo {getattr(session, 'attract', None) is not None},"
            seen["unready"] += f" cursor rows {cursor})"

    def settings_done() -> None:
        """Before LAUNCH_AT: the cursor is back on LAUNCH and the profile holds the new palette."""
        row_of, cursor = menu_rows()
        saved = json.loads(profile.read_text(encoding="utf-8")).get("settings", {}) if profile.exists() else {}
        back = "back on LAUNCH" if cursor == [row_of.get("LAUNCH")] else f"NOT back on LAUNCH (cursor rows {cursor})"
        seen["settings"] += f"; {back}; saved palette {saved.get('palette')}"

    def look() -> None:
        session = seen["session"]
        if session is None:
            return
        if getattr(session, "attract", None) is not None:
            seen["demo"] = True
        game = session.pilot.game if session.pilots else None
        if game is not None and (game.phase in ENDED or fake.now >= LAUNCH_AT + cap):
            session.quit = True

    if demo:
        fake.at(DEMO_END, lambda: tap("x", fake.now))  # any key ends the demo; in a base tree's menu it does nothing
    if settings:
        fake.at(DEMO_END + 0.5, prelude_settings)
        fake.at(LAUNCH_AT - 0.4, lambda: seen["settings"].startswith("asked") and settings_done())
    if demo or settings:
        fake.at(LAUNCH_AT - 0.2, ready)
    for n in range(int((LAUNCH_AT + cap + 1) / LOOK)):
        fake.at(LAUNCH_AT + n * LOOK, look)
    for n in range(1, int(LAUNCH_AT / LOOK)):  # the title's demo, while it plays
        fake.at(
            n * LOOK, lambda: seen.update(demo=seen["demo"] or getattr(seen["session"], "attract", None) is not None)
        )
    for arrival, data in key_bytes(doc["keys"]["protocol"], scheme, LAUNCH_AT, LAUNCH_AT + cap):
        fake.type(data, at=arrival)
    home = Path(tempfile.mkdtemp(prefix="same-loop-home-"))
    atexit.register(shutil.rmtree, home, True)  # the run's HOME goes when the child does, even after a failure
    profile, tape = home / "profile.json", home / "run.json"
    environ = {"HOME": str(home), "XDG_STATE_HOME": str(home / "state"), "XDG_DATA_HOME": str(home / "data")}
    environ |= {"XDG_CONFIG_HOME": str(home / "config"), "INVADERS_AUDIO": "none"}
    environ |= {"INVADERS_ATTRACT_IDLE": str(DEMO_IDLE) if demo else "off"}
    argv = ["--seed", str(SEED), "--no-sound", "--profile", str(profile), "--record", str(tape)]
    with mock.patch.dict(os.environ, environ):
        for patch in patches:
            patch.start()
        try:
            code = cli.main(argv)
        finally:
            for patch in reversed(patches):
                patch.stop()
    session = seen["session"]
    game = session.pilot.game if session is not None and session.pilots else None
    result = {
        "fixture": fixture,
        "size": [cols, rows],
        "scheme": scheme,
        "tree": str(tree),
        "argv": argv,
        "exit": code,
        "end": None if game is None else game.phase if game.phase in ENDED else "cap",
        "ticks": None if game is None else game.tick,
        "score": None if game is None else game.score,
        "wave": None if game is None else game.wave,
        "coach_observed": seen["observed"] if tutorial is not None else None,
        "demo_shown": seen["demo"],
        "settings": seen["settings"] if settings else None,
        "applied": seen["applied"],
        "unready": seen.get("unready"),
        "tape": json.loads(tape.read_text(encoding="utf-8")) if tape.exists() else None,
        "digests": seen["digests"],
    }
    out.write_text(json.dumps(result) + "\n", encoding="utf-8")


# --- the parent: one child per fixture, then the comparison ---------------------------------------------------------


def missing(tree: Path, data: dict, demo: bool, settings: bool) -> list[str]:
    """What the run should have shown of the Phase 5 parts this check exists for (spec 11.3: the Coach, the demo
    nesting and Hooks.apply) and did not; a tree without a part's module (a base tree) is not asked for it."""
    has = {name: (tree / "terminal_invaders" / f"{name}.py").exists() for name in ("tutorial", "attract", "settings")}
    gaps = []
    if has["tutorial"] and not data["coach_observed"]:
        gaps.append(f"the Coach observed {data['coach_observed']} ticks")
    if demo and has["attract"] and not data["demo_shown"]:
        gaps.append("--demo: no title demo played")
    said = data["settings"] or ""
    if settings and has["settings"] and (not said.startswith("asked") or "saved palette None" in said):
        gaps.append(f"--settings: no palette saved ({said})")
    if settings and has["settings"] and "palette" not in data["applied"]:
        gaps.append(f"--settings: Hooks.apply never applied the palette (applied {data['applied']})")
    return gaps


def run(tree: Path, out: Path, fixtures, demo: bool, settings: bool, cap: float = CAP) -> int:
    out.mkdir(parents=True, exist_ok=True)
    failed = 0
    for fixture in fixtures:
        path = out / f"{fixture}.json"
        command = [sys.executable, "-B", str(Path(__file__).resolve()), CHILD, str(tree), fixture, str(path)]
        command += ["--demo"] * demo + ["--settings"] * settings + ["--cap", repr(cap)]
        env = os.environ | {"PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run(command, env=env)
        if done.returncode or not path.exists():
            print(f"{fixture}: the run failed (exit {done.returncode})")
            failed += 1
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        print(
            f"{fixture} {data['size'][0]}x{data['size'][1]} {data['scheme']}: exit {data['exit']}, {data['end']} at"
            f" tick {data['ticks']}, score {data['score']}, wave {data['wave']}, {len(data['digests'])} digests;"
            f" coach observed {data['coach_observed']} ticks, demo shown {data['demo_shown']},"
            f" settings {data['settings']}, applied {data['applied']}"
        )
        if data.get("unready"):
            print(f"{fixture}: FAIL {data['unready']}: the prelude went wrong, so the run proves nothing")
        gaps = missing(tree, data, demo, settings)
        for gap in gaps:
            print(f"{fixture}: FAIL {gap}: the run does not cover what it is for")
        if data["exit"] != 0 or data["tape"] is None or data.get("unready") or gaps:
            failed += 1
    return 1 if failed else 0


def compare(a: Path, b: Path) -> int:
    failures = []
    names = sorted({path.name for path in a.glob("*.json")} | {path.name for path in b.glob("*.json")})
    if not names:
        failures.append("no runs to compare")
    for name in names:
        if not (a / name).exists() or not (b / name).exists():
            failures.append(f"{name}: in one folder only")
            continue
        one, other = (json.loads((folder / name).read_text(encoding="utf-8")) for folder in (a, b))
        if one["tape"] is None or other["tape"] is None:
            failures.append(f"{name}: a run without a tape")
        elif one["tape"] != other["tape"]:
            failures.append(f"{name}: the tapes differ")
        first, second = one["digests"], other["digests"]
        for index, (x, y) in enumerate(zip(first, second)):
            if x != y:
                failures.append(f"{name}: digest {index} differs: {x} != {y}")
                break
        else:
            if len(first) != len(second):
                failures.append(f"{name}: {len(first)} digests != {len(second)}")
        print(f"{name}: {len(first)} and {len(second)} digests, ends {one['end']} and {other['end']}")
    for line in failures:
        print("FAIL " + line)
    if not failures:
        print("OK: identical tapes and per-tick digests")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == [CHILD]:
        tree, fixture, out = argv[1:4]
        cap = float(argv[argv.index("--cap") + 1]) if "--cap" in argv else CAP
        child(Path(tree).resolve(), fixture, Path(out).resolve(), "--demo" in argv, "--settings" in argv, cap)
        return 0
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tree", type=Path, nargs="?", help="the tree to play (its code first on sys.path)")
    parser.add_argument("out", type=Path, nargs="?", help="where the runs go, one JSON file a fixture")
    parser.add_argument("--compare", type=Path, nargs=2, metavar=("A", "B"), help="compare two OUT folders")
    parser.add_argument("--fixtures", nargs="+", default=list(FIXTURES), choices=list(FIXTURES))
    parser.add_argument("--demo", action="store_true", help="let the title demo play before LAUNCH (Phase 5)")
    parser.add_argument("--settings", action="store_true", help="change a setting before LAUNCH (Phase 5)")
    parser.add_argument("--cap", type=float, default=CAP, help=f"seconds of play at most (default {CAP:g})")
    args = parser.parse_args(argv)
    if args.compare:
        return compare(*args.compare)
    if args.tree is None or args.out is None:
        parser.error("give TREE and OUT, or --compare A B")
    return run(args.tree.resolve(), args.out.resolve(), args.fixtures, args.demo, args.settings, args.cap)


if __name__ == "__main__":
    raise SystemExit(main())
