"""Phase 5's effect scenes (spec 2.12): ten moments of the golden replay tapes, each a golden in seven renders.

Each scene steps a golden tape (tests/fixtures) to a fixed tick and checks that the effect it is about is there at the
age the spec measured (an effect is first seen at age 1), so a tape that changed fails loudly here rather than
drawing another moment. A tape is flown once per process (flight()) and the game copied at each scene's tick.

frames.py lists them with `SCENES += fx_scenes.entries(session, playing, TIERS, DARK)`: each scene in the five tiers
at 120 x 44 (k = 2) and in truecolor dark at 80 x 24 (k = 1) and 180 x 64 (k = 3), 70 files. The session is built
with the scene's effect level (frames.options() reads `off` for every other scene).
"""

import copy
from functools import cache
from pathlib import Path
from typing import NamedTuple

from terminal_invaders import fx
from terminal_invaders.engine import Game
from terminal_invaders.replay import Playback

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CLASSIC = "golden-classic-normal.json"
ENDLESS = "golden-endless-hard.json"  # the goldens' only HIT (golden-classic-normal has none)


class Scene(NamedTuple):
    """A tape's game at a tick, with the effect it shows: a source of `kind` at `age` (None: no effect needed)."""

    name: str
    tape: str
    tick: int
    kind: str | None = None
    age: int = 0
    effects: str = "full"


SCENES = (
    Scene("fx-kill-burst", CLASSIC, 130, "kill", 2),  # the first kill (129), burst frame 1
    Scene("fx-kill-debris", CLASSIC, 142, "kill", 14),
    Scene("fx-ufo-kill", CLASSIC, 13046, "ufo", 6),  # the first UFO kill (13041)
    Scene("fx-boss-kick", CLASSIC, 27010, "boss", 4),  # the first boss kill (27007), the kick on
    Scene("fx-boss-debris", CLASSIC, 27020, "boss", 14),  # the kick over, the debris fading
    Scene("fx-phase-break", CLASSIC, 22649, "phase", 3),  # the first break (22647)
    Scene("fx-ship-lost", ENDLESS, 21240, "hit", 3),  # the HIT at 21238
    Scene("fx-stars", CLASSIC, 600),
    Scene("fx-stars-later", CLASSIC, 630),  # 30 ticks on: the far layer a quarter row, the near half a row
    Scene("fx-calm", CLASSIC, 142, "kill", 14, "calm"),  # kill debris at level calm
)
SIZES = ((120, 44, None), (80, 24, "dark"), (180, 64, "dark"))  # None: every tier


@cache
def flight(tape: str) -> dict[int, Game]:
    """The tape flown once to its last scene, with a copy of the game at each scene's tick."""
    ticks = sorted({scene.tick for scene in SCENES if scene.tape == tape})
    playback = Playback(FIXTURES / tape).tapes[0]
    game, taken = Game(playback.config), {}
    for tick in ticks:
        while game.tick < tick:
            game.step(playback.actions_at(game.tick))
        taken[tick] = copy.deepcopy(game)
    return taken


def check(scene: Scene, game: Game) -> None:
    """The scene's effect is on screen at its age, or an error naming what the tape shows instead."""
    assert game.tick == scene.tick and game.phase == "playing", (scene.name, game.tick, game.phase)
    if scene.kind is None:
        return
    found = [(source.kind, source.text, source.age) for source in fx.sources(game)]
    if not any(kind == scene.kind and age == scene.age for kind, _, age in found):
        raise LookupError(f"{scene.tape} at tick {scene.tick} shows {found}, not a {scene.kind} at age {scene.age}")


def game(name: str) -> Game:
    """A fresh copy of a scene's game, checked."""
    scene = next(scene for scene in SCENES if scene.name == name)
    current = copy.deepcopy(flight(scene.tape)[scene.tick])
    check(scene, current)
    return current


def entries(session, playing, tiers, dark) -> list:
    """frames.py's SCENES rows (name, columns, rows, build, tiers) for the ten scenes, given frames.py's session() and
    playing() and its tier lists."""

    def build(scene):
        return lambda: playing(session(effects=scene.effects), game(scene.name))

    rows = []
    for scene in SCENES:
        made = build(scene)  # one build per scene, shared by its sizes (frames() builds each once)
        rows += [(scene.name, cols, lines, made, tiers if which is None else dark) for cols, lines, which in SIZES]
    return rows
