"""The campaign-2 rules (spec §3): a 24-wave Campaign in four acts, built from the frozen tables in `tables`.

Imports only model.py, rng.py and its own modules, so other streams can copy model.py and campaign/ on their own.
"""

from ..model import RULESET
from .game import DIGEST_FIELDS, PRESENTATION, Fired, Game, held_interval
from .observe import Frame, observe

__all__ = ["DIGEST_FIELDS", "PRESENTATION", "RULESET", "Fired", "Frame", "Game", "held_interval", "observe"]
