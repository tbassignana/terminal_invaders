"""The campaign-1 rules, frozen: Phase 3's engine.py and model.py copied verbatim, with only their imports repointed.

Tapes recorded under campaign-1 still verify through legacy.campaign1.Game; they can't be watched (spec §17).
"""

from .campaign1 import Game
from .model1 import CAMPAIGN_WAVES, RULESET

__all__ = ["CAMPAIGN_WAVES", "RULESET", "Game"]
