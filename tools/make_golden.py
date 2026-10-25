#!/usr/bin/env python3
"""Regenerate the golden replays in tests/fixtures from fixed, deterministic input policies: python3
tools/make_golden.py [--out DIR] [NAME ...] (default: every one, into tests/fixtures).

The Campaign tapes (campaign-2) are played by the rebuilt skilled bot through tools/balance.py's façade
(Skilled(difficulty, seed).act(PublicView(game)), the bot seeded with the tape's seed, never sending autofire);
golden-arcade.json by the Arcade policy below. The policies read only the simulation state (no clock, no
randomness but the bot's own seeded stream), so the same engine always writes the same tapes. golden-daily.json
uses the first date seed from 20261006 on which the bot wins (up to DAILY_TRIES dates; the one used is printed).
Each Campaign tape's milestones are printed: the first tick of each card, each boss intro and phase, each act
intro, and the end. Regenerate when a ruleset changes on purpose (golden-arcade.json also when the Arcade port or its
art changes), then update the expected results in tests/test_replay.py (GOLDEN and GOLDEN_DIGESTS; GOLDEN_ARCADE for
golden-arcade.json) and run `make frames`.
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from balance import PublicView, Skilled  # noqa: E402

from terminal_invaders.arcade.memory import (  # noqa: E402
    ALIEN_SHOT_DELTA,
    FIRE_BOUNCE,
    P1_ALIENS,
    P1_RACK_CNT,
    PLAYER_ALIVE,
    PLAYER_XR,
    PLU_SHOT_STATUS,
    PLU_SHOT_XR,
    PLU_SHOT_YR,
    PLYR_SHOT_STATUS,
    RAM,
    REF_ALIEN_D_XR,
    REF_ALIEN_XR,
    REF_ALIEN_YR,
    ROL_SHOT_STATUS,
    ROL_SHOT_XR,
    ROL_SHOT_YR,
    SAUCER_ACTIVE,
    SAUCER_DELTA_Y,
    SAUCER_HIT,
    SAUCER_PRI_PIC_MSB,
    SQU_SHOT_STATUS,
    SQU_SHOT_XR,
    SQU_SHOT_YR,
)
from terminal_invaders.model import TICK_RATE, Config  # noqa: E402
from terminal_invaders.replay import Recorder  # noqa: E402
from terminal_invaders.rules import make_game  # noqa: E402

MINUTE = 60 * TICK_RATE
GOLDEN = {  # (config, cap in ticks); a run also ends by itself at game over or victory
    "golden-classic-normal.json": (Config("normal", "classic", 1978), 20 * MINUTE),
    "golden-daily.json": (Config("normal", "daily", 20261006), 60 * MINUTE),
    "golden-endless-hard.json": (Config("hard", "endless", 2026), 10 * MINUTE),
    "golden-arcade.json": (Config("normal", "arcade", 1978), 20 * MINUTE),
}
DAILY_FROM, DAILY_TRIES = date(2026, 10, 6), 20  # golden-daily.json: the first of these dates the bot wins on
TERMINAL = ("game_over", "victory")
ARCADE_ROUNDS = 4  # arcade_policy dodges through this many rounds, then stops so that the game ends by itself
ALIEN_SHOTS = (  # each alien shot's status, Yr and Xr
    (ROL_SHOT_STATUS, ROL_SHOT_YR, ROL_SHOT_XR),
    (PLU_SHOT_STATUS, PLU_SHOT_YR, PLU_SHOT_XR),
    (SQU_SHOT_STATUS, SQU_SHOT_YR, SQU_SHOT_XR),
)


class Policy:
    """The Campaign's policy: the skilled bot through its façade, seeded with the tape's seed. Autofire is never
    sent (the Campaign ignores it anyway)."""

    def __init__(self, config: Config):
        self.bot = None
        self.seed = config.seed

    def __call__(self, game) -> tuple[str, ...]:
        if self.bot is None:
            self.bot = Skilled(game.difficulty, self.seed)
        return tuple(action for action in self.bot.act(PublicView(game)) if action != "autofire")


class Milestones:
    """The ticks a Campaign tape reaches its cards, boss intros and phases, act intros and its end."""

    def __init__(self):
        self.lines, self.phase, self.boss = [], None, None

    def see(self, game) -> None:
        """Called after each step."""
        if game.phase == "upgrade" and self.phase != "upgrade":
            self.lines.append((game.tick, f"card {len(game.picks) + 1} before wave {game.wave + 1}"))
        if "act" in game.events:
            self.lines.append((game.tick, f"act {game.act} intro, wave {game.wave}"))
        boss = (game.boss.kind, game.boss.phase) if game.boss is not None else None
        if "siren" in game.events and game.boss is not None:
            self.lines.append((game.tick, f"boss {game.boss.kind} intro, wave {game.wave}"))
        if boss is not None and boss != self.boss and game.boss.state != "intro":
            self.lines.append((game.tick, f"boss {boss[0]} phase {boss[1] + 1} of {game.boss.phases}"))
            self.boss = boss
        elif boss is None:
            self.boss = None
        self.phase = game.phase

    def report(self, game) -> list[str]:
        end = f"end: {game.phase}, wave {game.wave}, act {game.act}, score {game.score}"
        return [f"  tick {tick:6d}  {what}" for tick, what in [*self.lines, (game.tick, end)]]


def daily_seeds():
    """The date seeds golden-daily.json tries, in order."""
    for n in range(DAILY_TRIES):
        yield int((DAILY_FROM + timedelta(days=n)).strftime("%Y%m%d"))


def arcade_policy(game) -> tuple[str, ...]:
    """Play the 1978 program from what its RAM shows now: dodge the falling alien shots and shoot the saucer when it
    is in reach, else the nearest invader of the lowest row, aiming where the target will be when the shot gets
    there. From round ARCADE_ROUNDS + 1 on it stops dodging, so the game ends a few thousand frames later.

    Positions are the program's Xr (across, one per screen column) and Yr (up, one per pixel row). The cannon moves
    one Xr a frame between 30 and D9 and fires from Xr + 8, its shot climbing 4 Yr a frame from Yr 28; it fires when
    the shot is free and lined up. An alien shot, 3 columns wide, falls 4 Yr (5 with under 9 invaders left) every 3
    frames and kills the cannon if it meets its pixels (columns 2-14 of 16, Yr 20-27). The invaders stand 16 apart
    from the reference invader, 11 to a row from the bottom; one moves each frame, so the fleet steps 2 sideways once
    all have moved (as fast as the cannon with two left), and it turns where an invader's pixels reach Xr 29 or F5.
    Each frame the cannon steps toward its spot (under the target or, when the fleet is as fast as the cannon, the
    middle of the target's sweep) if going straight there is safe from every falling shot, else toward the nearest
    safe place beside one."""
    ram = game.ram

    def rd(addr: int, signed: bool = False) -> int:
        value = ram[addr - RAM]
        return value - 256 if signed and value > 127 else value

    if rd(PLAYER_ALIVE) != 0xFF:  # blowing up
        return ()
    cannon = rd(PLAYER_XR)
    shots = []  # (Xr, frames until it reaches the cannon) of each falling alien shot
    if rd(P1_RACK_CNT) < ARCADE_ROUNDS:
        for status, yr, xr in ALIEN_SHOTS:
            if rd(status) & 0x81 == 0x80 and rd(yr) >= 0x1E:  # flying, not exploding, not past the cannon
                shots.append((rd(xr), max(0, (rd(yr) - 0x22) * 3 // -rd(ALIEN_SHOT_DELTA, signed=True))))

    def safe(spot: int) -> bool:
        """Going straight to spot and waiting there, the cannon keeps 3 clear columns from every shot it meets."""
        return all(not -3 <= xr - (cannon + max(-t, min(t, spot - cannon))) <= 17 for xr, t in shots)

    targets = []  # (row, the cannon's Xr whose shot fired now hits it, its spot); the saucer is row -1
    alive = [n for n in range(55) if rd(P1_ALIENS + n)]
    if alive:
        ref_x, ref_y, step = rd(REF_ALIEN_XR), rd(REF_ALIEN_YR), rd(REF_ALIEN_D_XR, signed=True)
        xs = {n: (ref_x + 16 * (n % 11)) % 256 for n in alive}
        to_right, to_left = 0xF5 - 13 - max(xs.values()), min(xs.values()) + 2 - 0x29  # Xr before the fleet turns
        room, speed = to_right if step > 0 else to_left, abs(step) / len(alive)
        for n in alive:
            travel = speed * max(0, (ref_y + 16 * (n // 11)) % 256 - 0x28) / 4
            if travel > room:
                travel = 2 * room - travel
            hit = spot = round(xs[n] + (travel if step > 0 else -travel))
            if speed >= 1:
                spot = min(max(round(xs[n] + (to_right - to_left) / 2), 0x30), 0xD9)
            targets.append((n // 11, hit, spot))
    if rd(SAUCER_ACTIVE) and not rd(SAUCER_HIT):
        # The saucer, lit in columns 4-19 of 24, flies at Yr D0 (42 frames of climb), 2 Xr every 3 frames; the RAM
        # map calls its Xr byte saucerPriPicMSB.
        hit = rd(SAUCER_PRI_PIC_MSB) + 4 + 14 * rd(SAUCER_DELTA_Y, signed=True)
        targets.append((-1, hit, hit))
    targets = [target for target in targets if 0x30 <= target[2] <= 0xD9]
    spot = min(targets, key=lambda target: (target[0], abs(target[2] - cannon)))[2] if targets else cannon
    places = [spot, cannon] + [x for xr, _ in shots for x in (xr - 18, xr + 4)]  # the spot, here, beside a shot
    places.sort(key=lambda x: (x != spot, abs(x - cannon)))
    goal = next((x for x in places if 0x30 <= x <= 0xD9 and safe(x)), cannon)
    lined_up = any(abs(hit - cannon) <= 2 for _, hit, _ in targets)
    fire = ("fire",) if lined_up and rd(PLYR_SHOT_STATUS) == 0 and not rd(FIRE_BOUNCE) else ()
    if goal == cannon:
        return fire
    return ("right" if goal > cannon else "left", *fire)


def record(config: Config, cap: int, milestones: Milestones | None = None) -> tuple[Recorder, object]:
    game = make_game(config)
    recorder = Recorder(config)
    act = arcade_policy if config.mode == "arcade" else Policy(config)
    if milestones is not None:
        milestones.see(game)  # act I's intro starts with the game
    while game.phase not in TERMINAL and game.tick < cap:
        actions = act(game)
        recorder.append(actions)
        game.step(actions)
        if milestones is not None:
            milestones.see(game)
    return recorder, game


def write(name: str, out: Path) -> None:
    config, cap = GOLDEN[name]
    arcade = config.mode == "arcade"
    seeds = daily_seeds() if config.mode == "daily" else (config.seed,)
    for seed in seeds:
        config = Config(config.difficulty, config.mode, seed)
        milestones = None if arcade else Milestones()
        recorder, game = record(config, cap, milestones)
        if config.mode != "daily" or game.phase == "victory":
            break
        print(f"{name}: seed {seed} ends in {game.phase} at wave {game.wave}; trying the next date")
    else:
        raise SystemExit(f"{name}: the policy wins on none of the {DAILY_TRIES} dates from {DAILY_FROM}")
    recorder.save(out / name, game)
    (out / name).chmod(0o644)  # as git checks fixtures out
    result = game.result()
    stage = "round" if arcade else "wave"
    score, reached = result["score"], result["wave"]
    seed = f", seed {config.seed}" if config.mode == "daily" else ""
    print(f"{name}: {game.tick} ticks, {len(recorder.inputs)} inputs, score {score}, {stage} {reached}{seed}")
    if milestones is not None:
        print("\n".join(milestones.report(game)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", metavar="NAME", help=f"the tapes to write, of {', '.join(GOLDEN)}")
    parser.add_argument("--out", type=Path, default=ROOT / "tests" / "fixtures", help="where (tests/fixtures)")
    options = parser.parse_args()
    unknown = [name for name in options.names if name not in GOLDEN]
    if unknown:
        parser.error(f"no golden replay named {', '.join(unknown)}")
    options.out.mkdir(parents=True, exist_ok=True)
    for name in options.names or GOLDEN:
        write(name, options.out)


if __name__ == "__main__":
    main()
