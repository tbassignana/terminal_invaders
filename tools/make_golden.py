#!/usr/bin/env python3
"""Regenerate the golden replays in tests/fixtures from a fixed, deterministic input policy.

The policy reads only the simulation state (no clock, no randomness), so the same engine always writes the same
tapes. Regenerate only when RULESET changes on purpose, then update the expected scores in tests/test_replay.py.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from terminal_invaders.engine import Game  # noqa: E402
from terminal_invaders.model import WIDTH, Config  # noqa: E402
from terminal_invaders.replay import Recorder  # noqa: E402

GOLDEN = {
    "golden-classic-normal.json": (Config("normal", "classic", 1978), 60 * 60 * 30),
    "golden-endless-hard.json": (Config("hard", "endless", 2026), 60 * 60 * 10),
    "golden-daily.json": (Config("normal", "daily", 20261006), 60 * 60 * 30),
}


def policy(game: Game) -> tuple[str, ...]:
    """Autofire once, then chase the lowest target, sidestep close enemy shots and bomb a landing fleet."""
    if game.tick == 0:
        return ("autofire",)
    if game.phase != "playing":
        return ()
    player = game.player
    threats = [s for s in game.shots if not s.friendly and 0 < player.y - s.y < 4 and abs(s.x - player.x) < 2.2]
    if threats and player.bombs and not player.invulnerable and player.lives == 1:
        return ("bomb",)
    if player.bombs and any(alien.y >= 14 and not alien.diving for alien in game.aliens):
        return ("bomb",)
    if threats:
        shot = min(threats, key=lambda s: player.y - s.y)
        away = -1 if shot.x >= player.x else 1
        if not 4 < player.x + 3 * away < WIDTH - 5:
            away = -away
        return ("left",) if away < 0 else ("right",)
    targets = [(alien.y, -abs(alien.x - player.x), alien.x) for alien in game.aliens]
    if game.boss:
        targets.append((0, 0, game.boss.x))
    if not targets:
        return ()
    target = max(targets)[2]
    if abs(target - player.x) < 0.5:
        return ()
    return ("right",) if target > player.x else ("left",)


def record(config: Config, cap: int) -> tuple[Recorder, Game]:
    game = Game(config)
    recorder = Recorder(config)
    while game.phase not in {"game_over", "victory"} and game.tick < cap:
        actions = policy(game)
        recorder.append(actions)
        game.step(actions)
    return recorder, game


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "tests" / "fixtures")
    options = parser.parse_args()
    for name, (config, cap) in GOLDEN.items():
        recorder, game = record(config, cap)
        recorder.save(options.out / name, game)
        result = game.result()
        print(
            f"{name}: {game.tick} ticks, {len(recorder.inputs)} inputs, score {result['score']}, wave {result['wave']}"
        )


if __name__ == "__main__":
    main()
