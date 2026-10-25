"""Phase 4's px2 rows of the two entries Phase 5 edits (spec 3.7), for the golden identity proof (spec 12.2).

Phase 5 made the player's shot 1 x 4 (it was 1 x 3) and the pellet a 1-pixel zig-zag (it was a 2-pixel one), in
every tier, so that no two things read alike in mono. tools/golden_identity.py patches these back, with
`sprites.SPRITES2.update(phase4_art.SPRITES2)` before it builds any Painter, to show that level `off` draws Phase 4's
frames apart from the named edits.
"""

from terminal_invaders.sprites import frames

PX2 = {
    "player": ("player_shot", """
        #
        #
        #
    """),
    "pellet": ("enemy_shot", """
        #.  .#
        #.  .#
        .#  #.
        .#  #.
    """),
}  # fmt: skip
SPRITES2 = {name: (role, frames(picture)) for name, (role, picture) in PX2.items()}  # as sprites.tables() builds them
