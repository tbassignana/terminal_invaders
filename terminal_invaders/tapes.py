"""The title demo's recorded runs, shipped as package data in terminal_invaders/demos/ (spec 5.1).

Two format-2 replay files, cut from the golden tapes by tools/make_attract.py: campaign.json (the first 5,400 ticks
of golden-classic-normal) and arcade.json (the first 5,400 ticks of golden-arcade). They are read with
importlib.resources (under as_file, because Playback wants a real path) and checked for structure only: the file
parses, format 2, one pilot, a ruleset this build plays, ticks and inputs in range. Nothing is simulated at load
time (verifying costs 0.07 s to 1 s, which would stall keys and frames); the demo's nested replay Session verifies
its tape at the tape's end, and a mismatch ends the demo silently.

The folder is `demos`, not `attract`: beside the module attract.py, a folder of that name would be a namespace
package of the same name.
"""

from __future__ import annotations

import logging
from importlib import resources

from .replay import Playback
from .rules import RULESETS

TAPES = ("campaign.json", "arcade.json")  # in the order the demo plays them (spec 5.2): Campaign first
FOLDER = "demos"


def load_tape(name: str) -> Playback:
    """One packaged tape, checked for structure only; ValueError or OSError when it is missing or not a tape the
    demo can play."""
    with resources.as_file(resources.files("terminal_invaders") / FOLDER / name) as path:
        playback = Playback(path)  # parses and checks every field (format 2, ruleset, ticks, inputs); no simulation
    if len(playback.tapes) != 1:
        raise ValueError("a demo tape holds one pilot")
    if playback.ruleset not in RULESETS:
        raise ValueError(f"a demo plays a ruleset this build plays, not {playback.ruleset}")
    if not playback.tapes[0].ticks:
        raise ValueError("a demo tape has ticks to show")
    return playback


def attract_tapes() -> list[Playback]:
    """The packaged tapes that load and pass the structural checks, in TAPES order; a bad one is skipped (and logged
    under --debug), never raised."""
    tapes = []
    for name in TAPES:
        try:
            tapes.append(load_tape(name))
        except (OSError, ValueError) as exc:
            logging.debug("Demo tape %s skipped: %s", name, exc)
    return tapes
