#!/usr/bin/env python3
"""Cut the title demo's tapes from the golden replays (Phase 5 spec 5.1): python3 tools/make_attract.py [--out DIR]
(default: terminal_invaders/demos, the package data the demo plays).

Each tape is the first CUT ticks of a golden in tests/fixtures: the golden's inputs below the cut, simulated for the
cut's ticks, written as a format-2 replay file whose digest and result are the game's at the cut. The file keeps the
golden's own `engine` field (not the running version), so a version bump does not change the files. Nothing here
is random and nothing reads a clock: running it twice writes byte-identical files, equal to the shipped ones.

The cuts move only when the goldens do: a gameplay change bumps the ruleset, regenerates the goldens
(tools/make_golden.py) and then these (and tests/test_attract_tapes.py's pins).
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from terminal_invaders.replay import FORMAT_VERSION, Playback, Recorder  # noqa: E402
from terminal_invaders.rules import game_class  # noqa: E402
from terminal_invaders.storage import atomic_json  # noqa: E402

CUT = 5400  # ticks: 90 seconds at 60 Hz
FIXTURES = ROOT / "tests" / "fixtures"
DEMOS = ROOT / "terminal_invaders" / "demos"
CUTS = {"campaign.json": "golden-classic-normal.json", "arcade.json": "golden-arcade.json"}  # tapes.TAPES' order


def cut(source: Path, ticks: int = CUT) -> dict:
    """The replay file of source's first `ticks` ticks: its one tape cut there and simulated to the cut."""
    engine = json.loads(source.read_text(encoding="utf-8"))["engine"]
    playback = Playback(source)
    (tape,) = playback.tapes
    if tape.ticks < ticks:
        raise ValueError(f"{source.name} has only {tape.ticks} ticks")
    game = game_class(playback.ruleset)(tape.config)
    recorder = Recorder(tape.config, tape.pilot)
    for tick in range(ticks):
        actions = tape.actions_at(tick)
        recorder.append(actions)
        game.step(actions)
        if game.tick != tick + 1:
            raise ValueError(f"{source.name} ends before tick {ticks}")
    return {"format": FORMAT_VERSION, "ruleset": playback.ruleset, "engine": engine, "tapes": [recorder.tape(game)]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEMOS, help="folder to write campaign.json and arcade.json to")
    args = parser.parse_args(argv)
    for name, source in CUTS.items():
        data = cut(FIXTURES / source)
        atomic_json(args.out / name, data)  # the compact JSON every replay file is written in
        (args.out / name).chmod(0o644)  # package data, readable by all (atomic_json's temporary file is 0600)
        written = Playback(args.out / name)
        if not written.tapes[0].verify(written.tapes[0].simulate()):
            raise SystemExit(f"{name}: the written tape does not verify")
        result = data["tapes"][0]["result"]
        level = "round" if data["ruleset"].startswith("arcade") else "wave"
        print(f"{name}: {CUT} ticks of {source}, score {result['score']}, {level} {result['wave']}, verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
