"""Arcade art: our own sprites and 8 x 8 font in the ROM's format, at the ROM's addresses.

The port reads every bitmap from an 8 KB image exactly where the 1978 program reads it (image.build_image places
SHIP_ART there). A bitmap is one byte per screen column, bit 0 the lowest pixel; the shield has two bytes per column,
the low byte first.

The rule: the 1978 pixels only where they decide play, our own everywhere else, always inside the 1978 outlines.
* Outlines: every sprite is the size of the ROM's and lights the same lowest and highest pixel in every column
  (EXTENTS, measured from the ROM and kept as numbers). A player shot meets each column of an invader, the saucer or
  the text in two 4-pixel windows, so it hits ours exactly where it hit the ROM's.
* Ours: the invaders, the invader explosion, the saucer, its explosion and the font (FONT's comment gives its one
  constraint). The cannon is the ROM's outline, which is solid between its extents, with a 4-pixel hatch of ours
  (columns 7-9, the second and third rows from the bottom) that no alien-shot picture at any position overlaps alone.
* The ROM's, marked in PICTURES. "the ROM's: solid": the shield, the player shot, the plunger shot and the rolling
  shot's pictures 0 and 2 are solid between their extents, which leave nothing to choose. "the ROM's: for play": the
  pictures whose every lit pixel decides which shots hit, since an alien shot strikes any lit pixel it overlaps and
  an explosion erases exactly its own lit pixels from a shield: the squiggly shot, the rolling shot's pictures 1 and
  3, both shot explosions, and the cannon's two blow-ups (alien shots still falling when the cannon is hit strike the
  debris). With our designs for them the 1978 program played differently in 1 (the rolling shot) to 12 (the alien
  shot's explosion) of 12 scripts, and with our blow-ups in 7 of 32 high-score games.

Evidence, flat timing: the 1978 program run twice in lockstep, with the ROM's art and with this, comparing RAM
2000-22FF after every frame, DrawSprCollision's 20 cycles for each screen byte where pixels meet counted as free on
both: 104 scripts (random inputs, sweeps past the extra ship into a second round, saucer hunts, and 32 games at
scores of 1000-3000; 494,523 frames, 48 saucers hit, 652 deaths, 468 of them with another alien shot in flight) kept
equal RAM throughout: in all of them no pixel of ours changed a decision the program made.

Real timing: the rules are exact, but with our art the program's draw timing shifts by microseconds, which now and
then sends a game down a different path from the 1978 machine given the same inputs; only the 1978 bitmaps
everywhere remove that. The port runs on the program's cycle time, and DrawSprCollision spends those 20 cycles (10
microseconds) for each byte where pixels meet, so where a collision draw lands on a picture of ours the program's
phase against the interrupts moves. The port on this image against the port on the ROM, same inputs, 207 scripts
(the 175 acceptance scenarios and the 32 high-score games; 817,664 frames): 100 byte-equal throughout, 94 the same
game with only timing bytes differing (extraHold, the sound ports), 12 with a kill or two scored a frame later
and otherwise the same, and 1 a different game (an alien-shot explosion drawn over one of our invaders took 60 cycles
more; 270 frames later an invader's explosion ended a frame later, and the fleet marched a frame behind from then on).

The splash-only sprites (the invaders carrying a 'Y', 1BA0, 1BD0, 1F80, 1FB0 and glyph 29) are left out: the game
part never draws them.
"""


def columns(picture: str) -> bytes:
    """The ROM's bytes for a picture drawn as rows of '#' and '.', top row first: one byte per column for 8 rows,
    two (low byte first) for 16."""
    rows = picture.split()
    height = len(rows)
    out = bytearray()
    for x in range(len(rows[0])):
        value = sum(1 << (height - 1 - y) for y, row in enumerate(rows) if row[x] == "#")
        out += value.to_bytes(height // 8, "little")
    return bytes(out)


# name: (address, picture); the comment gives the ROM label of the bitmap replaced.
PICTURES = {
    "alien_a0": (0x1C00, """
        ......####......
        ...##########...
        ..####....####..
        ..####.##.####..
        ..############..
        ..##.##..##.##..
        ..#.##.##.##.#..
        ..##........##..
    """),  # AlienSprA: type A (bottom two rows, 10 points), picture 0
    "alien_b0": (0x1C10, """
        .....#.....#....
        ...#.##...##.#..
        ...#.#######.#..
        ...##.#...#.##..
        ...###########..
        .....#######....
        .....#.....#....
        ....#.......#...
    """),  # AlienSprA+10: type B (middle two rows, 20 points), picture 0
    "alien_c0": (0x1C20, """
        .......##.......
        ......####......
        .....#.##.#.....
        ....########....
        ....#.####.#....
        ....##.##.##....
        .....#.##.#.....
        ....#.#..#.#....
    """),  # AlienSprA+20: type C (top row, 30 points), picture 0
    "alien_a1": (0x1C30, """
        ......####......
        ...##########...
        ..####....####..
        ..####.##.####..
        ..############..
        ...#.##..##.#...
        ...##..##..##...
        ....##....##....
    """),  # AlienSprB: type A, picture 1
    "alien_b1": (0x1C40, """
        .....#.....#....
        .....##...##....
        .....#######....
        ....#.#...#.#...
        ...###########..
        ...#.##.#.##.#..
        ...#.#.#.#.#.#..
        ......##.##.....
    """),  # AlienSprB+10: type B, picture 1
    "alien_c1": (0x1C50, """
        .......##.......
        ......####......
        .....#.##.#.....
        ....########....
        ....#.####.#....
        ....##.##.##....
        ....##....##....
        .....#....#.....
    """),  # AlienSprB+20: type C, picture 1
    "player": (0x1C60, """
        ........#.......
        .......###......
        .......###......
        ...###########..
        ..#############.
        ..######.######.
        ..#####...#####.
        ..#############.
    """),  # PlayerSprite: the cannon, also the reserve ships under the ground line (the ROM's outline, our hatch)
    "player_blowup0": (0x1C70, """
        ......#.........
        ...........#....
        ......#.#.#.....
        ...#..#.........
        .......##.##....
        .#...#.##.#.#...
        ...########..#..
        ..##########.#.#
    """),  # PlrBlowupSprites: blow-up picture 0 (the ROM's: for play)
    "player_blowup1": (0x1C80, """
        ...#.........#..
        #.....#....##..#
        ...#....##......
        ......#.......#.
        .#..#.##..##...#
        ..#....###...#..
        ...#########....
        ..##.#######..#.
    """),  # PlrBlowupSprites+10: blow-up picture 1 (the ROM's: for play)
    "player_shot": (0x1C90, """
        .
        .
        .
        .
        #
        #
        #
        #
    """),  # PlayerShotSpr (the ROM's: solid)
    "shot_explosion": (0x1C91, """
        #...#..#
        ..#...#.
        .######.
        ########
        ########
        .######.
        ..#..#..
        #..#...#
    """),  # ShotExploding: the player shot's explosion and erosion mask (the ROM's: for play)
    "alien_explosion": (0x1CC0, """
        .....#...#......
        ..#...#.#...#...
        ...#.#...#.#....
        ....#.#.#.#.....
        .##.........##..
        ....#.#.#.#.....
        ...#.##.##.#....
        ..#..#...#..#...
    """),  # AlienExplode
    "squiggly0": (0x1CD0, """
        .#.
        #..
        .#.
        ..#
        .#.
        #..
        .#.
        ...
    """),  # SquiglyShot: picture 0 (the ROM's: for play)
    "squiggly1": (0x1CD3, """
        #..
        .#.
        ..#
        .#.
        #..
        .#.
        ..#
        ...
    """),  # SquiglyShot+3: picture 1 (the ROM's: for play)
    "squiggly2": (0x1CD6, """
        .#.
        ..#
        .#.
        #..
        .#.
        ..#
        .#.
        ...
    """),  # SquiglyShot+6: picture 2 (the ROM's: for play)
    "squiggly3": (0x1CD9, """
        ..#
        .#.
        #..
        .#.
        ..#
        .#.
        #..
        ...
    """),  # SquiglyShot+9: picture 3 (the ROM's: for play)
    "alien_shot_explosion": (0x1CDC, """
        ..#...
        #...#.
        ..##.#
        .####.
        #.###.
        .#####
        #.###.
        .#.#.#
    """),  # AShotExplo: the alien shot's explosion and erosion mask (the ROM's: for play)
    "plunger0": (0x1CE2, """
        .#.
        .#.
        .#.
        .#.
        .#.
        ###
        ...
        ...
    """),  # PlungerShot: picture 0 (the ROM's: solid)
    "plunger1": (0x1CE5, """
        .#.
        .#.
        .#.
        ###
        .#.
        .#.
        ...
        ...
    """),  # PlungerShot+3: picture 1 (the ROM's: solid)
    "plunger2": (0x1CE8, """
        .#.
        .#.
        ###
        .#.
        .#.
        .#.
        ...
        ...
    """),  # PlungerShot+6: picture 2 (the ROM's: solid)
    "plunger3": (0x1CEB, """
        ###
        .#.
        .#.
        .#.
        .#.
        .#.
        ...
        ...
    """),  # PlungerShot+9: picture 3 (the ROM's: solid)
    "rolling0": (0x1CEE, """
        .#.
        .#.
        .#.
        .#.
        .#.
        .#.
        .#.
        ...
    """),  # RollShot: picture 0 (the ROM's: solid)
    "rolling1": (0x1CF1, """
        .#.
        .#.
        ##.
        .##
        .#.
        ##.
        .##
        ...
    """),  # RollShot+3: picture 1 (the ROM's: for play)
    "rolling2": (0x1CF4, """
        .#.
        .#.
        .#.
        .#.
        .#.
        .#.
        .#.
        ...
    """),  # RollShot+6: picture 2 (the ROM's: solid)
    "rolling3": (0x1CF7, """
        .##
        ##.
        .#.
        .##
        ##.
        .#.
        .#.
        ...
    """),  # RollShot+9: picture 3 (the ROM's: for play)
    "shield": (0x1D20, """
        ....##############....
        ...################...
        ..##################..
        .####################.
        ######################
        ######################
        ######################
        ######################
        ######################
        ######################
        ######################
        ######################
        #######.......########
        ######.........#######
        #####...........######
        #####...........######
    """),  # ShieldImage (the ROM's: solid)
    "saucer": (0x1D64, """
        ........................
        .........######.........
        .......##########.......
        ......#..##..##..#......
        .....##############.....
        ....##.####..####.##....
        ......###..##..###......
        .......#........#.......
    """),  # SpriteSaucer
    "saucer_explosion": (0x1D7C, """
        ...#..#.#......#.#..#...
        ....#.#......###...#....
        .#.#...#.##...#..#..#...
        .#....#.#.###.#.#..##...
        ...#.###.#.#.#.#.##..#..
        .#......###...#.#.......
        .#.#....#.#...##...#....
        ...#...#...#....#.......
    """),  # SpriteSaucerExp
}  # fmt: skip

# The characters the view prints for each character code (the ROM's font order, 1E00 + code * 8). The other codes are
# repurposed slots holding message strings, or splash glyphs, and are never drawn as text by the game part.
CHARSET = {code: char for code, char in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<> =*")}
CHARSET |= {0x38: "?", 0x3F: "-"}

# The glyphs, 5 x 6, side by side in CHARSET order (the blank one is the space): squared, with the crossbars high.
# Each sits in its 8 x 8 cell one column in from the left and one row down from the top, so letters are 3 pixels
# apart. A player shot can strike the saucer's score text (" 50", "100", "150", "300", drawn where the saucer was),
# and it meets each column in two 4-pixel steps, bits 0-3 and then 4-7; so 0, 1, 3 and 5 light pixels in the same
# halves of each column as the ROM's glyphs (test_art.SCORE_HALVES), or a shot would stop at the text in one program
# and fly through it in the other (the art equivalence experiment: with a bolder font, a shot struck the saucer's
# "100" in 3 of 20 saucer hunts where the 1978 one flew through to the top).
FONT = """
    ##### ####. ##### ####. ##### ##### ##### #...# .###. ....# #...# #.... #####
    #...# #...# #.... #...# #.... #.... #.... #...# ..#.. ....# #..#. #.... #.#.#
    ##### ####. #.... #...# ####. ####. #.... ##### ..#.. ....# ###.. #.... #.#.#
    #...# #...# #.... #...# #.... #.... #..## #...# ..#.. ....# #..#. #.... #.#.#
    #...# #...# #.... #...# #.... #.... #...# #...# ..#.. #...# #...# #.... #...#
    #...# ####. ##### ####. ##### #.... ##### #...# .###. ##### #...# ##### #...#

    #...# ##### ##### ##### ##### ##### ##### #...# #...# #...# #...# #...# #####
    ##..# #...# #...# #...# #...# #.... #.#.# #...# #...# #...# .#.#. #...# ...#.
    #.#.# #...# ##### #...# ##### ##### ..#.. #...# #...# #...# ..#.. ##### ..#..
    #..## #...# #.... #.#.# #.#.. ....# ..#.. #...# #...# #.#.# ..#.. ..#.. .#...
    #...# #...# #.... #..#. #..#. ....# ..#.. #...# .#.#. ##.## .#.#. ..#.. #....
    #...# ##### #.... ###.# #...# ##### ..#.. ##### ..#.. #...# #...# ..#.. #####

    ##### .##.. ##### ##### #...# ##### ##### ##### ##### ##### ...#. .#... .....
    #..## ..#.. ....# ....# #...# #.... #.... ....# #...# #...# ..#.. ..#.. .....
    #.#.# ..#.. ##### .#### ##### ####. ##### ...#. ##### ##### .#... ...#. .....
    ##..# ..#.. #.... ....# ....# ....# #...# ..#.. #...# ....# ..#.. ..#.. .....
    #...# ..#.. #.... ....# ....# #...# #...# ..#.. #...# ....# ...#. .#... .....
    ##### .###. ##### ##### ....# .###. ##### ..#.. ##### ##### ..... ..... .....

    ..... #.#.# ####. .....
    ##### .###. ....# .....
    ..... ##### ..##. #####
    ##### .###. ..#.. .....
    ..... #.#.# ..... .....
    ..... ..... ..#.. .....
"""  # fmt: skip


def glyphs(font: str) -> list[list[str]]:
    """The glyphs of a font picture, in order, each as its 6 rows."""
    out = []
    for block in font.strip().split("\n\n"):
        lines = [line.split() for line in block.strip().splitlines()]
        out += [list(rows) for rows in zip(*lines)]
    return out


def cell(glyph: list[str]) -> bytes:
    """A 5 x 6 glyph's 8 bytes in its 8 x 8 character cell."""
    return columns("\n".join(["." * 8] + ["." + row + ".." for row in glyph] + ["." * 8]))


SPRITES = {name: (address, columns(picture)) for name, (address, picture) in PICTURES.items()}
GLYPHS = dict(zip(sorted(CHARSET), (cell(glyph) for glyph in glyphs(FONT))))
SHIP_ART = {address: data for address, data in SPRITES.values()}
SHIP_ART |= {0x1E00 + code * 8: data for code, data in GLYPHS.items()}

# name: (address, length in bytes, per column the lowest and highest lit pixel or None): measured from the ROM's
# bitmaps by a research script (not part of this repository); the numbers are facts, not art.
EXTENTS = {
    "alien_a0": (0x1C00, 16, (None, None, (0, 5), (0, 6), (1, 6), (1, 6), (2, 7), (1, 7), (1, 7), (2, 7), (1, 6), (1, 6), (0, 6), (0, 5), None, None)),
    "alien_b0": (0x1C10, 16, (None, None, None, (3, 6), (0, 4), (1, 7), (2, 6), (2, 5), (2, 5), (2, 5), (2, 6), (1, 7), (0, 4), (3, 6), None, None)),
    "alien_c0": (0x1C20, 16, (None, None, None, None, (0, 4), (1, 5), (0, 6), (1, 7), (1, 7), (0, 6), (1, 5), (0, 4), None, None, None, None)),
    "alien_a1": (0x1C30, 16, (None, None, (3, 5), (1, 6), (0, 6), (0, 6), (2, 7), (1, 7), (1, 7), (2, 7), (0, 6), (0, 6), (1, 6), (3, 5), None, None)),
    "alien_b1": (0x1C40, 16, (None, None, None, (1, 3), (3, 4), (1, 7), (0, 6), (0, 5), (2, 5), (0, 5), (0, 6), (1, 7), (3, 4), (1, 3), None, None)),
    "alien_c1": (0x1C50, 16, (None, None, None, None, (1, 4), (0, 5), (3, 6), (2, 7), (2, 7), (3, 6), (0, 5), (1, 4), None, None, None, None)),
    "player": (0x1C60, 16, (None, None, (0, 3), (0, 4), (0, 4), (0, 4), (0, 4), (0, 6), (0, 7), (0, 6), (0, 4), (0, 4), (0, 4), (0, 4), (0, 3), None)),
    "player_blowup0": (0x1C70, 16, (None, (2, 2), (0, 0), (0, 4), (0, 1), (0, 2), (0, 7), (0, 3), (0, 5), (0, 1), (0, 5), (0, 6), (2, 2), (0, 1), None, (0, 0))),
    "player_blowup1": (0x1C80, 16, ((6, 6), (3, 3), (0, 2), (0, 7), (1, 3), (0, 1), (0, 6), (0, 3), (0, 5), (0, 5), (0, 3), (0, 6), (6, 6), (2, 7), (0, 4), (3, 6))),
    "player_shot": (0x1C90, 1, ((0, 3),)),
    "shot_explosion": (0x1C91, 8, ((0, 7), (2, 5), (1, 6), (0, 5), (2, 7), (1, 5), (2, 6), (0, 7))),
    "alien_explosion": (0x1CC0, 16, (None, (3, 3), (0, 6), (1, 5), (2, 4), (0, 7), (1, 6), None, (1, 6), (0, 7), (2, 4), (1, 5), (0, 6), (3, 3), None, None)),
    "squiggly0": (0x1CD0, 3, ((2, 6), (1, 7), (4, 4))),
    "squiggly1": (0x1CD3, 3, ((3, 7), (2, 6), (1, 5))),
    "squiggly2": (0x1CD6, 3, ((4, 4), (1, 7), (2, 6))),
    "squiggly3": (0x1CD9, 3, ((1, 5), (2, 6), (3, 7))),
    "alien_shot_explosion": (0x1CDC, 6, ((1, 6), (0, 4), (1, 7), (0, 5), (1, 6), (0, 5))),
    "plunger0": (0x1CE2, 3, ((2, 2), (2, 7), (2, 2))),
    "plunger1": (0x1CE5, 3, ((4, 4), (2, 7), (4, 4))),
    "plunger2": (0x1CE8, 3, ((5, 5), (2, 7), (5, 5))),
    "plunger3": (0x1CEB, 3, ((7, 7), (2, 7), (7, 7))),
    "rolling0": (0x1CEE, 3, (None, (1, 7), None)),
    "rolling1": (0x1CF1, 3, ((2, 5), (1, 7), (1, 4))),
    "rolling2": (0x1CF4, 3, (None, (1, 7), None)),
    "rolling3": (0x1CF7, 3, ((3, 6), (1, 7), (4, 7))),
    "shield": (0x1D20, 44, ((0, 11), (0, 12), (0, 13), (0, 14), (0, 15), (2, 15), (3, 15), (4, 15), (4, 15), (4, 15), (4, 15), (4, 15), (4, 15), (4, 15), (3, 15), (2, 15), (0, 15), (0, 15), (0, 14), (0, 13), (0, 12), (0, 11))),
    "saucer": (0x1D64, 24, (None, None, None, None, (2, 2), (2, 3), (1, 4), (0, 5), (1, 5), (2, 6), (2, 6), (1, 6), (1, 6), (2, 6), (2, 6), (1, 5), (0, 5), (1, 4), (2, 3), (2, 2), None, None, None, None)),
    "saucer_explosion": (0x1D7C, 24, (None, (1, 5), None, (0, 7), (6, 6), (3, 3), (3, 7), (0, 5), (1, 7), (2, 5), (1, 5), (0, 4), (4, 4), (3, 6), (1, 6), (1, 7), (0, 4), (3, 7), (3, 3), (1, 6), (4, 7), (3, 3), None, None)),
}  # fmt: skip


def extents(data: bytes, per_column: int = 1) -> tuple:
    """Per column of a bitmap, its lowest and highest lit pixel (bit numbers), or None for an empty column."""
    out = []
    for i in range(0, len(data), per_column):
        value = int.from_bytes(data[i : i + per_column], "little")
        out.append(((value & -value).bit_length() - 1, value.bit_length() - 1) if value else None)
    return tuple(out)
