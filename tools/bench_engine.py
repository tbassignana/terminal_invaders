#!/usr/bin/env python3
"""Campaign engine cost per tick (spec §2.3, §23.7): python3 tools/bench_engine.py [--check] [--tape FILE]
[--ticks N] [--scene-ticks N] [--seed N].

Two parts, each timing Game.step() alone (no observe, no bot, no rendering):

* the whole game: a Campaign tape replayed engine only. It uses --tape, or the Campaign golden
  (tests/fixtures/golden-classic-normal.json) once that holds a campaign-2 tape; until then it records a scripted
  mover's actions on an immortal Normal game for --ticks ticks and replays them;
* busy scenes of --scene-ticks ticks each, with the garbage collector frozen: wave 23 with every flight due and a
  maxed director (8 slots and the reload floor, LIMITS.reload) against a TRIPLE SHOT ship holding SPREAD; the
  MOTHERSHIP's phase 4 on Hard (the double spiral); Endless wave 60 (36 modifiers with the tuned ladder); and
  Endless wave 205 (181: every modifier stacked 22 times or more). The labels count them from the ladder. The
  scripted mover plays them; its time is not counted.

It prints mean, 99th percentile and max in microseconds, and each scene's enemy shots and enemies on the field
(mean and most), so a scene that isn't busy shows. --check exits 1 when a bound is missed: whole game mean
<= 40 us; busy scenes mean <= 150 us and p99 <= 400 us (CPython 3.12 on a quiet machine; it is judged at
integration). It also reports observe() over the same whole game, a tick at a time (its 20 us budget is checked by
tools/balance.py --profile, inside the bot's 60).
"""

import argparse
import gc
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from terminal_invaders.campaign import Game, observe  # noqa: E402
from terminal_invaders.campaign.endless import due, stacks  # noqa: E402
from terminal_invaders.campaign.tables import LIMITS, PLAYER, WAVES  # noqa: E402
from terminal_invaders.campaign.testing import staged  # noqa: E402
from terminal_invaders.model import RULESET, Config  # noqa: E402

GOLDEN = ROOT / "tests" / "fixtures" / "golden-classic-normal.json"
WHOLE_MEAN = 40.0  # microseconds
BUSY_MEAN, BUSY_P99 = 150.0, 400.0


def mover(game) -> tuple:
    """A scripted mover: fire, step away from shots and dives about to reach the ship, else drift under a target."""
    if game.phase in ("upgrade", "continue"):
        return ("fire",) if game.tick % 2 else ()
    x = game.player.x

    def danger(at):
        risk = 0.0
        for shot in game.shots:
            if shot.friendly or shot.vy <= 0 or shot.y > PLAYER.y + 0.7:
                continue
            ticks = max(0.0, (PLAYER.y - 0.4 - shot.y) / shot.vy)
            if ticks < 70 and abs(shot.x + shot.vx * ticks - at) < 2.4:
                risk += 1.0 / (1 + ticks / 10)
        for alien in game.aliens:
            if alien.state == "diving" and alien.y > 9 and abs(alien.x - at) < 3.5:
                risk += 2.0
        for hazard in game.hazards:
            if hazard.kind == "beam" and hazard.x0 - 2 < at < hazard.x1 + 2:
                risk += 3.0
        return risk

    target = None
    for alien in game.aliens:
        if alien.y < 16 and (target is None or abs(alien.x - x) < abs(target - x)):
            target = alien.x
    if target is None and game.boss is not None:
        target = next((part.x for part in game.boss.parts if part.state in ("open", "charge")), None)

    def cost(at):
        at = min(PLAYER.x_max, max(PLAYER.x_min, at))
        value = 10 * danger(at) + 0.5 * min(danger(at - 1.6), danger(at + 1.6))
        return value + (0.05 * abs(at - target) if target is not None else 0.0)

    best = min((x - PLAYER.speed, x, x + PLAYER.speed), key=cost)
    if best < x:
        return ("fire", "left")
    if best > x:
        return ("fire", "right")
    return ("fire",)


def stats(samples) -> dict:
    """Mean, 99th percentile and max of per-tick seconds, in microseconds."""
    ordered = sorted(samples)
    p99 = ordered[min(len(ordered) - 1, (99 * len(ordered)) // 100)]
    return {
        "ticks": len(ordered),
        "mean": sum(ordered) / len(ordered) * 1e6,
        "p99": p99 * 1e6,
        "max": ordered[-1] * 1e6,
    }


def campaign_tape(path):
    """(config, {tick: actions}, ticks) of a campaign-2 tape file, or None when it holds another ruleset."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("ruleset") != RULESET:
        return None
    tape = raw["tapes"][0]
    inputs = {tick: tuple(actions) for tick, actions in tape["inputs"]}
    return Config(**tape["config"]), inputs, tape["ticks"]


def whole_game(args) -> tuple[dict, dict, str]:
    """Time every step of a whole game replayed from its actions, then observe() after every step of a second
    replay."""
    source = None
    for path in [args.tape] if args.tape else [GOLDEN]:
        if path and Path(path).exists():
            source = campaign_tape(path)
            if source:
                label = f"tape {Path(path).name}"
                break
    if source:
        config, inputs, ticks = source

        def build():
            return Game(config)
    else:
        config = Config("normal", "classic", args.seed)

        def build():
            return staged(config, 1, immortal=True)

        game, inputs = build(), {}
        for tick in range(args.ticks):
            actions = mover(game)
            if actions:
                inputs[tick] = actions
            game.step(actions)
            if game.phase in ("victory", "game_over"):
                break
        ticks = game.tick
        label = f"scripted mover, immortal Normal seed {args.seed}, reached wave {game.wave}"
    game = build()
    samples, clock = [], time.perf_counter
    for tick in range(ticks):
        actions = inputs.get(tick, ())
        start = clock()
        game.step(actions)
        samples.append(clock() - start)
    game, seen = build(), []
    for tick in range(ticks):
        game.step(inputs.get(tick, ()))
        start = clock()
        observe(game)
        seen.append(clock() - start)
    return stats(samples), stats(seen), label


def busy_scenes(args) -> list[tuple[str, dict]]:
    out = []
    normal = Config("normal", "classic", args.seed)

    def wave23():
        game = staged(normal, 23, upgrades=("TRIPLE SHOT",), phase="playing", immortal=True)
        game._director = {"slots": LIMITS.slots, "reload": LIMITS.reload}
        game._fire = game._director_row(game._wave_row())
        game._play = float(WAVES[22].flights[-1].at)  # every flight is due (the band still decides who arrives)
        return game

    def boss4():
        return staged(Config("hard", "classic", args.seed), 24, boss_phase=3, immortal=True)

    def endless(wave):
        return lambda: staged(Config("normal", "endless", args.seed), wave, phase="playing", immortal=True)

    maxed = f"{LIMITS.slots} slots, reload {LIMITS.reload}"
    out.append((f"wave 23, every flight due, {maxed}, SPREAD and TRIPLE SHOT", scene(wave23, args.scene_ticks, True)))
    out.append(("MOTHERSHIP phase 4, Hard", scene(boss4, args.scene_ticks)))
    out.append((f"Endless wave 60, {due(60)} modifiers", scene(endless(60), args.scene_ticks)))
    each = min(stacks(due(205)).values())
    out.append((f"Endless wave 205, every modifier x{each}", scene(endless(205), args.scene_ticks)))
    return out


def scene(build, ticks, spread=False) -> dict:
    """Time `ticks` steps of the scene, building it again whenever it ends (the build is not timed)."""
    samples, clock = [], time.perf_counter
    shots, enemies = [], []
    game = build()
    gc.collect()
    gc.freeze()
    gc.disable()
    try:
        while len(samples) < ticks:
            if game.phase in ("clear", "victory", "game_over"):
                game = build()
            if spread and game.powerups.get("spread", 0) < 60:
                game.powerups["spread"] = 720
            actions = mover(game)
            start = clock()
            game.step(actions)
            samples.append(clock() - start)
            shots.append(sum(3 if s.kind == "cluster" else 1 for s in game.shots if not s.friendly))
            enemies.append(len(game.aliens))
    finally:
        gc.enable()
        gc.unfreeze()
    row = stats(samples)
    row["shots"] = (sum(shots) / len(shots), max(shots))
    row["enemies"] = (sum(enemies) / len(enemies), max(enemies))
    return row


def line(name, row) -> str:
    text = (
        f"  {name}: {row['ticks']} ticks, mean {row['mean']:.1f} us, p99 {row['p99']:.1f} us, max {row['max']:.0f} us"
    )
    if "shots" in row:
        (shots, most), (alive, crowd) = row["shots"], row["enemies"]
        text += (
            f"\n      enemy shots {shots:.1f} (most {most} of {LIMITS.enemy_shots}), enemies {alive:.1f} (most {crowd})"
        )
    return text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when a bound of spec 2.3 is missed")
    parser.add_argument("--tape", help="a campaign-2 replay file to time instead of the golden")
    parser.add_argument("--ticks", type=int, default=72_000, help="ticks the scripted mover plays (no tape)")
    parser.add_argument("--scene-ticks", type=int, default=3_600, help="ticks per busy scene")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    print(f"Python {platform.python_version()} on {platform.machine()}")
    whole, seen, label = whole_game(args)
    print(f"whole game ({label}):")
    print(line("Game.step", whole))
    print(line("observe() (budget 20 us, judged by balance.py --profile)", seen))
    print("busy scenes (gc frozen):")
    scenes = busy_scenes(args)
    for name, row in scenes:
        print(line(name, row))
    missed = []
    if whole["mean"] > WHOLE_MEAN:
        missed.append(f"whole game mean {whole['mean']:.1f} us > {WHOLE_MEAN:g}")
    for name, row in scenes:
        if row["mean"] > BUSY_MEAN:
            missed.append(f"{name}: mean {row['mean']:.1f} us > {BUSY_MEAN:g}")
        if row["p99"] > BUSY_P99:
            missed.append(f"{name}: p99 {row['p99']:.1f} us > {BUSY_P99:g}")
    for text in missed:
        print(f"MISSED: {text}")
    return 1 if args.check and missed else 0


if __name__ == "__main__":
    sys.exit(main())
