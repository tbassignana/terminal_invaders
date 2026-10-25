"""The ruleset registry: which game class plays each mode, so sessions, replays and score boards keep them apart.

RULESETS is what this version plays; LEGACY is what it can still verify (spec §17): tapes and score rows recorded
under campaign-1 load, verify and rank on their own, but a new game is always campaign-2 or Arcade.
"""

from . import legacy
from .arcade import RULESET as ARCADE
from .arcade import ArcadeGame
from .campaign import RULESET as CAMPAIGN
from .campaign import Game

RULESETS = {CAMPAIGN: Game, ARCADE: ArcadeGame}
LEGACY = {legacy.RULESET: legacy.Game}


def ruleset_for(mode: str) -> str:
    """The ruleset a mode plays: arcade is the 1978 program, every other mode the campaign."""
    return ARCADE if mode == "arcade" else CAMPAIGN


def game_class(ruleset: str):
    """The game class of a ruleset this version plays or verifies."""
    return RULESETS.get(ruleset) or LEGACY[ruleset]


def make_game(config):
    """A new game of the ruleset that config's mode plays."""
    return RULESETS[ruleset_for(config.mode)](config)
