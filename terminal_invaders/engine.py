"""The Campaign's game class, under the name every caller imports (spec §2.1): campaign-2's rules.

The campaign-1 rules that Phase 3 played live on, frozen, in terminal_invaders.legacy (they verify old tapes).
"""

from .campaign import DIGEST_FIELDS, Game
from .campaign.collide import segment_hit

__all__ = ["DIGEST_FIELDS", "Game", "segment_hit"]
