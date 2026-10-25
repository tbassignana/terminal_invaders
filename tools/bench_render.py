#!/usr/bin/env python3
"""Draw + encode cost per frame (gates G1.3, G3.5 and G5.8; Phase 4 §23.7, Phase 5 §2.11): python3
tools/bench_render.py [--check] [--ticks N] [--sizes 80x24 ...] [--staged busy kick | none] [--effects full|calm|off]
[--runs N] [--load-below LOAD] [--arcade [224x128 ...]].

The Skilled bot (tools/bots, through tools/balance.py's Skilled and PublicView: it reads the game's Frame) plays a
Campaign (seed 42, normal; a new run starts if one ends) while the game's Renderer builds a frame after every tick at
each terminal size, on a Ghostty-like terminal (UTF-8, synchronized output, CPR acks, truecolor with the game's own
dark background unless --color or --theme say otherwise), with the effects at --effects (default full, the option's
own default). --staged busy adds a row at 240x70 where the same bot plays the busiest staged scenes instead, half
the ticks each: wave 23 with every flight on screen (the director at its caps, SPREAD and TRIPLE SHOT held), then
boss 4 (MOTHERSHIP) in phase 4; a scene that ends starts again. It is held to 240x70's thresholds. --staged kick adds
the kick scene at 240x70 and 80x24 (Phase 5 §2.11): wave 23 of seed 42, not immortal, its lives held at 99, played by
the naive bot for KICK_TICKS ticks, so the ship is hit and the field kicks (the skilled bot is not hit there). Both
staged rows run by default with the default sizes. --arcade times an Arcade game instead, played by the Arcade policy
of tools/make_golden.py, at 224x128 (p = 1) and 112x64 (p = 2) unless given sizes; with --arcade the Campaign rows run
only for sizes given with --sizes.

A frame's time is Renderer.frame(): the Painter's scene, the diff and encode, the previous-frame copy and the
packet. With the effects on (full or calm), every Campaign row is played again with them off in the same run: the
columns `cost` (the difference of the means) and `+B` (of the mean bytes a frame) are the effects' own. --runs N
plays the whole set N times, and the busy row's cost is judged on the median of the runs. --load-below LOAD waits
before each run (up to LOAD_WAIT seconds) for the 1-minute load average to fall under LOAD and prints it before and
after the run (G5.8's load rule: `--check --runs 6 --load-below 0.5`); a run that cannot start under LOAD fails, and
one during which the load rose more than OUTSIDE_LOAD past what the bench's own busy process explains (another process
ran beside it) is named, so it can be rerun once.

--check exits 1 when a threshold is missed: p95 <= 2.0 ms at 80x24, p95 <= 4.0 ms and mean <= 1.5 ms at 240x70, the
busy and kick rows included, which hold for Python 3.11 on ubuntu-latest (spec 13); p95 <= 2.0 ms for the Arcade
block at p = 2 and p95 <= 4.0 ms at p = 1 (Phase 3); and Phase 5's rows (§2.11): the effects' own cost on the busy
row at most COST_LIMIT ms, their extra mean bytes a frame at most BYTES_LIMIT on every size and on the busy row, and
at most HEAVY_PER_KICK heavy frames caused by each kick, and with --effects full at least one kick admitted (a
kick scene without one holds nothing to that rule). A heavy frame has more than twice the kick scene's median
bytes or changes more than half the block's rows; a kick causes the frames within its ticks that are heavy with the
effects and not without them (the same game, drawn with effects off); the kicks are the ones the Renderer's flash
limiter admitted.
"""

import argparse
import gc
import math
import os
import platform
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import bots  # noqa: E402
from balance import Offline, PublicView, Skilled  # noqa: E402
from make_golden import arcade_policy  # noqa: E402

from terminal_invaders.app import Renderer  # noqa: E402
from terminal_invaders.campaign.observe import observe  # noqa: E402
from terminal_invaders.campaign.tables import LIMITS as CAPS  # noqa: E402
from terminal_invaders.campaign.tables import WAVES  # noqa: E402
from terminal_invaders.campaign.testing import staged  # noqa: E402
from terminal_invaders.cli import defaults  # noqa: E402
from terminal_invaders.model import Config  # noqa: E402
from terminal_invaders.term.caps import TIERS, EnvHints, Overrides, ProbeReplies, resolve  # noqa: E402
from terminal_invaders.term.pacing import AckPacer  # noqa: E402
from terminal_invaders.ui import Session  # noqa: E402

SIZES = ((80, 24), (120, 44), (180, 64), (240, 70))
BUSY_SIZE = (240, 70)  # the busy row's size, held to that size's thresholds
KICK_SIZES = ((240, 70), (80, 24))  # the kick scene's sizes (Phase 5 §2.11)
KICK_TICKS = 1800
KICK_WINDOW = 12  # frames from a kick's source's birth in which its heavy frames count (a kick lasts 6 or 10 ticks)
ARCADE_SIZES = ((224, 128), (112, 64))  # the Arcade block at p = 1 and p = 2
LIMITS = {(80, 24): {"p95": 2.0}, (240, 70): {"p95": 4.0, "mean": 1.5}}  # milliseconds (G1.3)
ARCADE_LIMITS = {(224, 128): {"p95": 4.0}, (112, 64): {"p95": 2.0}}  # G3.5
COST_LIMIT = 0.25  # ms: the effects' own cost on the busy row (full - off, same run), the median of the runs
BYTES_LIMIT = 150  # the effects' extra mean bytes a frame, at every size and on the busy row
HEAVY_PER_KICK = 2  # heavy frames a kick may cause: the shift on and the shift off
LOAD_WAIT = 600.0  # seconds --load-below waits for the load to fall before a run
OUTSIDE_LOAD = 0.1  # how far a run's end load may rise past what the bench's own busy process explains
EFFECTS = ("full", "calm", "off")
GHOSTTY = {"TERM": "xterm-ghostty", "TERM_PROGRAM": "ghostty", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8"}
REPLIES = ProbeReplies(
    decrpm_2026=2, xtversion="ghostty 1.2.0", osc11=(40, 44, 52), cpr_echo=(2, 1), cpr_width=(3, 2), dsr=True,
    da1=(62, 22), rtt=0.001,
)  # fmt: skip


def options(**values) -> argparse.Namespace:
    return defaults(**dict(seed=42, fixed_seed=True, quickstart=True, name="BENCH") | values)


def frame_times(size: tuple[int, int], ticks: int, color="auto", theme="auto", **values):
    """Seconds and bytes of every frame while the bot plays `ticks` ticks, and the arena scale used."""
    caps = resolve(EnvHints.from_env(GHOSTTY), REPLIES, Overrides(color=color, theme=theme))
    session = Session(options(**values), Offline(), Offline())
    renderer, pacer = Renderer(session.options, caps, size), AckPacer("off")
    bot = Skilled(session.pilot.game.difficulty, 42)
    seconds, sizes, clock = [], [], time.perf_counter
    for tick in range(ticks):
        game = session.pilot.game
        if game.phase in ("game_over", "victory"):
            session.start()
            game = session.pilot.game
            bot = Skilled(game.difficulty, 42 + tick)
        game.step(bot.act(PublicView(game)))
        renderer.ticks_pending += 1
        start = clock()
        data = renderer.frame(session, tick / 60, True, pacer)
        seconds.append(clock() - start)
        sizes.append(len(data))
    return seconds, sizes, renderer.painter.layout.k


def wave23():
    """Wave 23 with every flight due, the director at its caps, TRIPLE SHOT taken (SPREAD is held while it plays)."""
    game = staged(Config("normal", "classic", 42), 23, upgrades=("TRIPLE SHOT",), phase="playing", immortal=True)
    game._director = {"slots": CAPS.slots, "reload": CAPS.reload}
    game._fire = game._director_row(game._wave_row())
    game._play = float(WAVES[22].flights[-1].at)  # every flight is due (the band still decides who arrives)
    return game


def boss4():
    """MOTHERSHIP (boss 4, wave 24) in its fourth phase."""
    return staged(Config("normal", "classic", 42), 24, boss_phase=3, immortal=True)


BUSY = ((wave23, True), (boss4, False))  # (build, hold SPREAD), half the ticks each


def staged_frame_times(size: tuple[int, int], ticks: int, color="auto", theme="auto", **values):
    """Seconds and bytes of every frame while the bot plays the busy scenes for `ticks` ticks, and the arena scale."""
    caps = resolve(EnvHints.from_env(GHOSTTY), REPLIES, Overrides(color=color, theme=theme))
    session = Session(options(**values), Offline(), Offline())
    renderer, pacer = Renderer(session.options, caps, size), AckPacer("off")
    seconds, sizes, clock = [], [], time.perf_counter
    for part, (build, spread) in enumerate(BUSY):
        session.pilot.game = game = build()
        bot = Skilled(game.difficulty, 42)
        for _ in range(ticks // len(BUSY) + (part < ticks % len(BUSY))):
            if game.phase in ("clear", "upgrade", "victory", "game_over"):
                session.pilot.game = game = build()
                bot = Skilled(game.difficulty, 42 + len(seconds))
            if spread and game.powerups.get("spread", 0) < 60:
                game.powerups["spread"] = 720
            game.step(bot.act(PublicView(game)))
            renderer.ticks_pending += 1
            start = clock()
            data = renderer.frame(session, len(seconds) / 60, True, pacer)
            seconds.append(clock() - start)
            sizes.append(len(data))
    return seconds, sizes, renderer.painter.layout.k


def kick_scene():
    """Wave 23 of seed 42, not immortal, its lives held at 99 (Phase 5 §2.11): the ship is hit and the field kicks."""
    return staged(Config("normal", "classic", 42), 23, phase="playing", lives=99)


def kick_frame_times(size: tuple[int, int], ticks: int = KICK_TICKS, color="auto", theme="auto", **values) -> dict:
    """The kick scene played by the naive bot: every frame's seconds, bytes and rows changed; the scale, the block's
    height, the scene's first tick and {kick: the tick its source was born} for each kick the Renderer's flash
    limiter admitted (a key is the source's text and birth tick)."""
    caps = resolve(EnvHints.from_env(GHOSTTY), REPLIES, Overrides(color=color, theme=theme))
    session = Session(options(**values), Offline(), Offline())
    session.pilot.game = game = kick_scene()
    first = game.tick
    renderer, pacer = Renderer(session.options, caps, size), AckPacer("off")
    pilot = bots.make("naive", 42)
    seconds, sizes, changed, kicks, before, clock = [], [], [], {}, None, time.perf_counter
    for _ in range(ticks):
        if game.phase in ("game_over", "victory"):
            break
        game.step(pilot.act(observe(game)))
        renderer.ticks_pending += 1
        start = clock()
        data = renderer.frame(session, game.tick / 60, True, pacer)
        seconds.append(clock() - start)
        sizes.append(len(data))
        buf = renderer.buffer
        rows = [(buf.row_text(y), bytes(buf.styles[y * buf.cols : (y + 1) * buf.cols])) for y in range(buf.rows)]
        changed.append(0 if before is None else sum(row != old for row, old in zip(rows, before, strict=True)))
        before = rows
        for key in getattr(renderer.limiter, "admitted", {}):
            kicks.setdefault(key, key[1] if isinstance(key, tuple) and len(key) == 2 else game.tick)
    return dict(seconds=seconds, sizes=sizes, changed=changed, k=renderer.painter.layout.k,
                height=renderer.painter.height, first=first, kicks=kicks)  # fmt: skip


def heavy_frames(scene: dict) -> set[int]:
    """The frames (indexes) with more than twice the scene's median bytes, or more than half the block's rows changed."""
    median, half = statistics.median(scene["sizes"]), scene["height"] / 2
    return {
        n for n, (size, rows) in enumerate(zip(scene["sizes"], scene["changed"])) if size > 2 * median or rows > half
    }


def caused_by_kicks(full: dict, off: dict) -> dict:
    """{kick: the heavy frames it caused}: the frames within its window heavy with the effects and not without."""
    caused = heavy_frames(full) - heavy_frames(off)
    per_kick = {}
    for key, birth in full["kicks"].items():
        start = birth - full["first"] - 1  # the frame drawn at tick T is frame T - first - 1
        per_kick[key] = sum(n in caused for n in range(start, start + KICK_WINDOW))
    return per_kick


def arcade_frame_times(size: tuple[int, int], ticks: int, color="auto", theme="auto"):
    """Seconds and bytes of every frame while the Arcade policy plays `ticks` ticks, and the block's p."""
    caps = resolve(EnvHints.from_env(GHOSTTY), REPLIES, Overrides(color=color, theme=theme))
    session = Session(options(mode="arcade"), Offline(), Offline())
    renderer, pacer = Renderer(session.options, caps, size), AckPacer("off")
    seconds, sizes, clock = [], [], time.perf_counter
    for tick in range(ticks):
        if session.pilot.game.phase == "game_over":
            session.start()
        game = session.pilot.game
        game.step(arcade_policy(game))
        renderer.ticks_pending += 1
        start = clock()
        data = renderer.frame(session, tick / 60, True, pacer)
        seconds.append(clock() - start)
        sizes.append(len(data))
    return seconds, sizes, f"p{renderer.painter.arcade.p}"


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def summary(size, seconds, sizes, k) -> dict:
    ms = [1000 * value for value in seconds]
    return {
        "size": "%dx%d" % size, "k": k, "frames": len(ms), "mean": statistics.fmean(ms), "p50": statistics.median(ms),
        "p95": p95(ms), "max": max(ms), "bytes_mean": statistics.fmean(sizes), "bytes_p95": p95(sizes),
    }  # fmt: skip


def failures(size, row, limits=None) -> list[str]:
    return [
        f"{row['size']} {name} {row[name]:.2f} ms > {limit} ms"
        for name, limit in (LIMITS if limits is None else limits).get(size, {}).items()
        if row[name] > limit
    ]


def size_type(value: str) -> tuple[int, int]:
    cols, _, rows = value.partition("x")
    if not (cols.isdigit() and rows.isdigit()):
        raise argparse.ArgumentTypeError("expected COLSxROWS, such as 240x70")
    return int(cols), int(rows)


def show(row: dict, off: dict | None = None) -> str:
    """One row of the table; with off, the effects' cost and extra bytes too."""
    line = (
        f"{row['size']:>13} {row['k']:>2} {row['mean']:6.2f} {row['p50']:6.2f} {row['p95']:6.2f} {row['max']:6.2f}"
        f"       {row['bytes_mean']:6.0f} {row['bytes_p95']:6.0f}"
    )
    if off is not None:
        line += f"  {row['mean'] - off['mean']:+6.3f} {row['bytes_mean'] - off['bytes_mean']:+6.0f}"
    return line


def campaign_row(kind: str, size, args, effects: str) -> dict:
    values = dict(scale=args.scale, reduced_motion=args.reduced_motion, effects=effects)
    if kind == "busy":
        row = summary(size, *staged_frame_times(size, args.ticks, args.color, args.theme, **values))
        row["size"] += " busy"
    else:
        row = summary(size, *frame_times(size, args.ticks, args.color, args.theme, **values))
    return row


def kick_row(size, args) -> tuple[dict, dict, list[str], str]:
    """The kick scene at --effects and with the effects off: both rows, the failures of its kicks and a note."""
    values = dict(scale=args.scale, reduced_motion=args.reduced_motion)
    full = kick_frame_times(size, KICK_TICKS, args.color, args.theme, effects=args.effects, **values)
    gc.collect()
    off = kick_frame_times(size, KICK_TICKS, args.color, args.theme, effects="off", **values)
    row = summary(size, full["seconds"], full["sizes"], full["k"])
    row["size"] += " kick"
    off_row = summary(size, off["seconds"], off["sizes"], off["k"])
    per_kick = caused_by_kicks(full, off)
    order = sorted(per_kick, key=lambda key: full["kicks"][key])
    bad = [f"{row['size']} {key} caused {per_kick[key]} heavy frames > {HEAVY_PER_KICK}" for key in order]
    bad = [line for line, key in zip(bad, order, strict=True) if per_kick[key] > HEAVY_PER_KICK]
    if not order and args.effects == "full" and not args.reduced_motion:  # full effects kick: a scene without one
        bad.append(f"{row['size']}: no kick admitted, so the scene proves nothing")  # holds no kick to its rule
    named = ", ".join(
        f"{key[0] if isinstance(key, tuple) else key}@{full['kicks'][key]}: {per_kick[key]}" for key in order
    )
    note = f"{row['size']:>13}: {len(order)} kicks admitted; heavy frames each caused: {named or 'none'}"
    return row, off_row, bad, note


def own_load(start: float, seconds: float) -> float:
    """The 1-minute load average after `seconds` of the bench's one busy process alone, from `start`: the kernel damps
    it as start * e^(-t/60) + 1 * (1 - e^(-t/60))."""
    decay = math.exp(-seconds / 60.0)
    return start * decay + (1.0 - decay)


def load_note(start: float, end: float, seconds: float) -> str:
    """The end-of-run load line: a run is named for a rerun when the load rose more than OUTSIDE_LOAD past what the
    bench alone explains (another process ran beside it); the bench's own process lifts the load past 0.5 in any
    run of half a minute, so the start's limit cannot judge the end."""
    alone = own_load(start, seconds)
    note = " (another process ran: rerun once)" if end - alone > OUTSIDE_LOAD else ""
    return f"load {end:.2f} at the end, {alone:.2f} from the bench alone after {seconds:.0f} s{note}"


def settle(limit: float, wait: float = LOAD_WAIT) -> tuple[float, float]:
    """Wait (polling every 5 s, at most `wait` seconds) for the 1-minute load average to fall under limit: the
    load then, and the seconds waited."""
    start = time.monotonic()
    while (load := os.getloadavg()[0]) >= limit and time.monotonic() - start < wait:
        time.sleep(5.0)
    return load, time.monotonic() - start


def main(argv: list[str] | None = None) -> int:
    command = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    command.add_argument("--ticks", type=int, default=3600, help="frames per size (default 3600: a minute of play)")
    command.add_argument("--sizes", type=size_type, nargs="+", metavar="COLSxROWS")
    command.add_argument(
        "--staged",
        choices=("busy", "kick", "none"),
        nargs="+",
        help="the staged rows: busy (240x70) and kick (240x70 and 80x24); default both with the default sizes",
    )
    command.add_argument("--arcade", type=size_type, nargs="*", metavar="COLSxROWS", help="Arcade rows")
    command.add_argument("--scale", default="auto", choices=("auto", "1", "2", "3", "4", "6"))
    command.add_argument("--reduced-motion", action="store_true")
    command.add_argument("--effects", default="full", choices=EFFECTS, help="the effects level (default full)")
    command.add_argument("--color", default="auto", choices=("auto", *TIERS), help="colour tier (auto: truecolor)")
    command.add_argument("--theme", default="auto", choices=("auto", "dark", "light", "terminal"))
    command.add_argument("--runs", type=int, default=1, help="play the whole set this many times (default 1)")
    command.add_argument("--load-below", type=float, metavar="LOAD", help="start each run under this 1-minute load")
    command.add_argument("--check", action="store_true", help="exit 1 when a G1.3, G3.5 or G5.8 threshold is missed")
    args = command.parse_args(argv)
    sizes = args.sizes or ([] if args.arcade is not None else list(SIZES))
    arcade = None if args.arcade is None else args.arcade or list(ARCADE_SIZES)
    chosen = set(args.staged or (["none"] if args.sizes or args.arcade is not None else ["busy", "kick"]))
    busy, kick = "busy" in chosen, "kick" in chosen
    baseline = args.effects != "off"
    caps = resolve(EnvHints.from_env(GHOSTTY), REPLIES, Overrides(color=args.color, theme=args.theme))
    players = ["Skilled bot, Campaign seed 42"] * bool(sizes) + ["Skilled bot, busy staged scenes"] * busy
    players += ["naive bot, kick scene"] * kick + ["Arcade policy"] * bool(arcade)
    print(
        f"Python {platform.python_version()}, {args.ticks} frames per size, {', '.join(players)},"
        f" {caps.color} {caps.theme} on {'its own' if caps.paint_background else 'the terminal'} background,"
        f" effects {args.effects}" + (" (and off, for their cost)" if baseline and (sizes or busy or kick) else "")
    )
    failed, costs = [], []
    for run in range(1, args.runs + 1):
        if args.runs > 1:
            print(f"run {run} of {args.runs}")
        if args.load_below is not None:
            start_load, waited = settle(args.load_below)
            started = time.monotonic()
            print(f"load {start_load:.2f} at the start of run {run} (waited {waited:.0f} s for < {args.load_below:g})")
            if start_load >= args.load_below:
                failed.append(f"run {run} started at load {start_load:.2f} >= {args.load_below:g}")
        heading = f"{'size':>13} {'k':>2} {'mean':>6} {'p50':>6} {'p95':>6} {'max':>6}  ms   {'B/f':>6} {'p95 B':>6}"
        print(heading + (f"  {'cost':>6} {'+B':>6}" if baseline else ""))
        for size, kind in [(size, "campaign") for size in sizes] + [(BUSY_SIZE, "busy")] * busy:
            gc.collect()
            row, off = campaign_row(kind, size, args, args.effects), None
            if baseline:
                gc.collect()
                off = campaign_row(kind, size, args, "off")
                extra = row["bytes_mean"] - off["bytes_mean"]
                if extra > BYTES_LIMIT:
                    failed.append(f"{row['size']} the effects add {extra:.0f} bytes a frame > {BYTES_LIMIT}")
                if kind == "busy":
                    costs.append(row["mean"] - off["mean"])
            print(show(row, off))
            failed += failures(size, row)
        notes = []
        for size in KICK_SIZES if kick else ():
            gc.collect()
            row, off, bad, note = kick_row(size, args)
            print(show(row, off))
            failed += failures(size, row) + bad
            notes.append(note)
        for size in arcade or ():
            gc.collect()
            row = summary(size, *arcade_frame_times(size, args.ticks, args.color, args.theme))
            print(show(row))
            failed += failures(size, row, ARCADE_LIMITS)
        for note in notes:
            print(note)
        if args.load_below is not None:
            print(f"run {run}: " + load_note(start_load, os.getloadavg()[0], time.monotonic() - started))
    if costs:
        cost = statistics.median(costs)
        print(f"the effects' own cost on the busy row: {cost:+.3f} ms (median of {len(costs)}, limit {COST_LIMIT} ms)")
        if cost > COST_LIMIT:
            failed.append(f"the effects cost {cost:.3f} ms a busy frame > {COST_LIMIT} ms")
    for line in failed:
        print("FAIL " + line)
    return 1 if args.check and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
