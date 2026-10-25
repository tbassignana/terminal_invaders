"""Sprites: the glyph strings of the k = 1 arena, the pixel art of the pixel arena, and the checks that tie them.

Pixel art is authored at 2 px per model unit (px2, for k = 2) and 3 px per unit (px3, for k = 3). A unit is k px wide
and 2k px tall, so a sprite whose glyph is three characters wide gets at most 6 x 4 px in px2 and 9 x 6 px in px3;
k = 4 and k = 6 draw px2 and px3 doubled. The art is original.

Everything the arena draws has an art key (Phase 4 spec 3.7): an enemy kind with its `_tell` and `_cracked` frames
and its dive pose, a shot kind, a boss part `<boss>.<part>` with `_charge`, `_cracked` and `_shielded` frames, a hazard
tile, the ship, the UFO and the pickup ring (whose letter is text). Each key has a glyph (px1) and px2 and px3 art.
Shape tells things apart first and colour second: 16 colours and mono have only the shape.
"""

import math

from .model import HEIGHT, WIDTH
from .model import PICKUPS as PICKUP_KINDS
from .term.pixels import Art
from .theme import SLOT

# ---- k = 1 glyphs ----
# Enemy kinds, two idle frames each, in role alien.
ALIENS = {
    "scout": ("/W\\", "\\W/"),
    "diver": ("<V>", "<v>"),
    "gunner": ("(o)", "(O)"),
    "weaver": ("~w~", "-w-"),
    "shell": ("[X]", "{X}"),
    "lancer": ("-Y-", "=Y="),
    "bomber": ("{#}", "[#]"),
    "splitter": ("<o>", ">o<"),
    "carrier": ("[=]", "[-]"),
    "mite": ("w", "m"),
    "guard": ("{=}", "}={"),
}
# The pose a shooter holds while its shot is announced. Every kind that fires has one, since a burst may come from any
# shooter; the pellet kinds show the pellet's own mark.
TELLS = {
    "gunner": "(@)",
    "lancer": "*Y*",
    "bomber": "{@}",
    "splitter": "<@>",
    "carrier": "[*]",
    "scout": "/!\\",
    "weaver": "~!~",
    "shell": "[!]",
    "guard": "{!}",
}
CRACKED = {"shell": "[x]", "bomber": "{%}", "splitter": "<.>", "carrier": "[_]", "guard": "{-}"}  # 2+ HP, damaged
DIVES = {"diver": "\\V/", "mite": "w"}  # the dive pose (and the wobble before it), in role diver
SHOTS = {"player": "|", "pellet": "!", "dart": "v", "hunter": "V", "needle": ":", "slug": "%", "cluster": "o"}
PICKUPS = {"rapid": "R", "spread": "S", "shield": "+", "slow": "T", "repair": "L", "bomb": "B"}
SHIP, SHIELDED, UFO = "/A\\", "{A}", "=<=>="
SOLID, CRACKED_CELL, STAR, CARGO = "#", ":", ".", "$"
# Boss parts, one model row tall and part.w cells wide, in PART_FRAMES order: open (it can be hurt), charging an
# attack, cracked (below half its HP) and shielded. Shielded and open differ in shape: the window shuts.
PARTS = {
    "warden.core": ("<=[ O ]=>", "<=[@@@]=>", "<=[.o.]=>", "<=[===]=>"),
    "warden.turret": ("/T\\", "/@\\", "/t\\", "{T}"),
    "hive_queen.queen": ("}-=<(Q)>=-{", "}-=<(@)>=-{", "}-=<(q)>=-{", "}-=<[#]>=-{"),
    "dreadnought.core": ("[#[ O ]#]", "[#[@@@]#]", "[#[.o.]#]", "[#[===]#]"),
    "dreadnought.battery": ("{o=o}", "{@=@}", "{.=o}", "{===}"),
    "mothership.core": ("<=<=[  O  ]=>=>", "<=<=[@@@@@]=>=>", "<=<=[ .o. ]=>=>", "<=<=[=====]=>=>"),
    "mothership.pod": ("(-o-)", "(-@-)", "(-.-)", "(===)"),
}
PART_FRAMES = ("", "_charge", "_cracked", "_shielded")
# Hazard tiles, one cell each: the dotted column of a beam's warning, the live beam, the chevrons under a locked beam,
# and the curtain gap's dashes and brackets (a safe lane is marked by the brackets alone).
HAZARDS = {"beam_warn": "'", "beam_live": "I", "lane_mark": "^", "gap_mark": "-", "gap_open": "[", "gap_close": "]"}
# The bomb's Effect has this text and is drawn as a ring of telegraph dots growing from the ship to the shockwave's
# reach, never as text; its ttl starts at RING_TICKS.
RING, RING_TICKS, RING_RADIUS = "()", 30, 5.0

# ---- pixel art: key -> (role, frames side by side; "." is transparent) ----
PX3 = {
    "scout": ("alien", """
        ..#...#..  ..#...#..
        ...#.#...  ...#.#...
        ..#####..  ..#####..
        .##.#.##.  .##.#.##.
        ..#####..  ..#####..
        .#.#.#.#.  #.#...#.#
    """),
    "scout_tell": ("alien", """
        .#.....#.
        ..#...#..
        ..#####..
        .#######.
        ..##.##..
        ....#....
    """),
    "diver": ("alien", """
        #.......#  .........
        ##.###.##  #..###..#
        .#######.  #########
        ..#.#.#..  .#.#.#.#.
        ...###...  ...###...
        ....#....  ....#....
    """),
    "diving": ("diver", """
        #.......#  .#.....#.
        .#.....#.  .#.....#.
        ..#####..  ..#####..
        ..##.##..  ..##.##..
        ...###...  ...###...
        ....#....  ....#....
    """),
    "gunner": ("alien", """
        ..#####..  ..#####..
        .#.....#.  .#.....#.
        #...#...#  #..###..#
        #..###..#  #..#.#..#
        .#.....#.  .#.....#.
        ..##.##..  ..##.##..
    """),
    "gunner_tell": ("alien", """
        ..#####..
        .#######.
        ##.###.##
        ##.###.##
        .#######.
        ..##.##..
    """),
    "weaver": ("alien", """
        #.......#  .#.....#.
        .#.....#.  #.#...#.#
        ..#.#.#..  ...#.#...
        ...###...  ...###...
        ..#...#..  ...#.#...
        .#.....#.  ..#...#..
    """),
    "weaver_tell": ("alien", """
        #.......#
        .#..#..#.
        ..#.#.#..
        ...###...
        ...###...
        ....#....
    """),
    "shell": ("alien", """
        .#######.  .#######.
        .##.#.##.  .##.#.##.
        .#######.  .#######.
        .#######.  .#######.
        .#.....#.  .##...##.
        .##...##.  .#.....#.
    """),
    "shell_tell": ("alien", """
        .#######.
        .##.#.##.
        .###.###.
        .##...##.
        .###.###.
        ....#....
    """),
    "shell_cracked": ("alien", """
        .###.###.
        .##..#.#.
        .#.#####.
        .####.##.
        .#.....#.
        .##...##.
    """),
    "lancer": ("alien", """
        #.......#  ##.....##
        .##...##.  ..##.##..
        ...###...  ...###...
        ...###...  ...###...
        ....#....  ....#....
        ....#....  ....#....
    """),
    "lancer_tell": ("alien", """
        #.#...#.#
        .##.#.##.
        ..#####..
        ...###...
        ...###...
        ....#....
    """),
    "bomber": ("alien", """
        #########  #########
        #.#####.#  #.#####.#
        #########  #########
        .##...##.  .#.....#.
        ..#...#..  ..##.##..
        ...#.#...  ...#.#...
    """),
    "bomber_tell": ("alien", """
        #########
        #.#####.#
        ###...###
        .#.###.#.
        ...###...
        ....#....
    """),
    "bomber_cracked": ("alien", """
        ####.####
        #.##..#.#
        ###.#####
        .##...##.
        ..#...#..
        ...#.#...
    """),
    "splitter": ("alien", """
        .##...##.  .##...##.
        ####.####  ###.#.###
        ####.####  ###.#.###
        .##...##.  .##...##.
        ..#...#..  .#.....#.
        .#.....#.  ..#...#..
    """),
    "splitter_tell": ("alien", """
        .##...##.
        #########
        #########
        .#######.
        ...###...
        ....#....
    """),
    "splitter_cracked": ("alien", """
        .##...#..
        ####.#.##
        ####.##.#
        .##...##.
        ..#...#..
        .#.....#.
    """),
    "carrier": ("alien", """
        .........  .........
        #########  #########
        #.#.#.#.#  ##.#.#.##
        #########  #########
        ##.....##  ##.....##
        #.......#  .#.....#.
    """),
    "carrier_tell": ("alien", """
        .........
        #########
        #.#####.#
        #########
        ##.###.##
        #...#...#
    """),
    "carrier_cracked": ("alien", """
        .........
        ####.####
        #.#..##.#
        ###.#####
        ##.....##
        #.......#
    """),
    "mite": ("alien", """
        ...  ...
        .#.  #.#
        ###  ###
        #.#  .#.
        ...  ...
        ...  ...
    """),
    "mite_diving": ("diver", """
        ...  ...
        #.#  #.#
        .#.  .#.
        #.#  ...
        ...  ...
        ...  ...
    """),
    "guard": ("alien", """
        #..###..#  ...###...
        .#######.  #.#####.#
        ##.#.#.##  ##.#.#.##
        #########  #########
        .##.#.##.  .##.#.##.
        ...#.#...  ..#...#..
    """),
    "guard_tell": ("alien", """
        #..###..#
        .#######.
        ##.###.##
        ###...###
        .###.###.
        ....#....
    """),
    "guard_cracked": ("alien", """
        #..#.#..#
        .###.###.
        ##.#..###
        ####.####
        .##.#.##.
        ...#.#...
    """),
    "ship": ("player", """
        ....#....
        ...###...
        ...###...
        .#######.
        #########
        #.#...#.#
    """),
    "shielded": ("player", """
        .#######.
        #...#...#
        ...###...
        .#######.
        #########
        #.#...#.#
    """),
    "ufo": ("ufo", """
        ......###......
        ....#######....
        .#############.
        ###.#.#.#.#.###
        .#############.
        ...#..#.#..#...
    """),
    "pickup": ("pickup", """
        ..#####..
        .##...##.
        ##.....##
        ##.....##
        .##...##.
        ..#####..
    """),
    "player": ("player_shot", """
        #
        #
        #
        #
    """),
    "pellet": ("enemy_shot", """
        #.  .#
        #.  .#
        .#  #.
        .#  #.
        #.  .#
        #.  .#
    """),
    "dart": ("enemy_shot", """
        #.#
        #.#
        .#.
    """),
    "hunter": ("enemy_shot", """
        .#.
        ...
        #.#
        #.#
        .#.
    """),
    "needle": ("enemy_shot", """
        #
        #
        .
        #
        #
    """),
    "slug": ("enemy_shot", """
        ###
        ###
        ###
        ###
    """),
    "cluster": ("enemy_shot", """
        ###
        #.#
        ###
    """),
    "beam_warn": ("telegraph", """
        #
        #
        .
        .
        .
        .
    """),
    "beam_live": ("enemy_shot", """
        ###
        ###
        ###
        ###
        ###
        ###
    """),
    "lane_mark": ("telegraph", """
        ...
        ...
        ...
        ...
        .#.
        #.#
    """),
    "gap_mark": ("telegraph", """
        ...
        ...
        ...
        ...
        ...
        ###
    """),
    "gap_open": ("telegraph", """
        ###
        #..
        #..
        #..
        #..
        ###
    """),
    "gap_close": ("telegraph", """
        ###
        ..#
        ..#
        ..#
        ..#
        ###
    """),
}  # fmt: skip
PX2 = {
    "scout": ("alien", """
        .#..#.  .#..#.
        .####.  .####.
        ##..##  ##..##
        .#..#.  #....#
    """),
    "scout_tell": ("alien", """
        #....#
        .####.
        ######
        ..##..
    """),
    "diver": ("alien", """
        #....#  ......
        ##..##  #.##.#
        .####.  ######
        ..##..  ..##..
    """),
    "diving": ("diver", """
        #....#  .#..#.
        .#..#.  .#..#.
        .####.  .####.
        ..##..  ..##..
    """),
    "gunner": ("alien", """
        .####.  .####.
        #.##.#  #....#
        #....#  #.##.#
        .####.  .####.
    """),
    "gunner_tell": ("alien", """
        .####.
        ######
        ######
        .####.
    """),
    "weaver": ("alien", """
        #....#  .#..#.
        .#..#.  #.##.#
        ..##..  ..##..
        .#..#.  #....#
    """),
    "weaver_tell": ("alien", """
        #.##.#
        .#..#.
        ..##..
        ..##..
    """),
    "shell": ("alien", """
        .####.  .####.
        .#..#.  .#..#.
        .####.  .####.
        #....#  .#..#.
    """),
    "shell_tell": ("alien", """
        .####.
        .####.
        .#..#.
        ..##..
    """),
    "shell_cracked": ("alien", """
        .##.#.
        .#..#.
        .#.##.
        .###..
    """),
    "lancer": ("alien", """
        #....#  ##..##
        .####.  ..##..
        ..##..  ..##..
        ..##..  ..##..
    """),
    "lancer_tell": ("alien", """
        #.##.#
        .####.
        .####.
        ..##..
    """),
    "bomber": ("alien", """
        ######  ######
        ######  ######
        .#..#.  #....#
        ..##..  .#..#.
    """),
    "bomber_tell": ("alien", """
        ######
        ##..##
        .####.
        ..##..
    """),
    "bomber_cracked": ("alien", """
        ##.###
        ###.##
        .#..#.
        ..##..
    """),
    "splitter": ("alien", """
        ##..##  ##..##
        ##..##  ##..##
        .#..#.  #....#
        #....#  .#..#.
    """),
    "splitter_tell": ("alien", """
        ##..##
        ######
        .####.
        ..##..
    """),
    "splitter_cracked": ("alien", """
        ##..#.
        ##..##
        .#..#.
        #....#
    """),
    "carrier": ("alien", """
        ######  ######
        #.##.#  ##..##
        ######  ######
        #....#  .#..#.
    """),
    "carrier_tell": ("alien", """
        ######
        #.##.#
        ######
        ..##..
    """),
    "carrier_cracked": ("alien", """
        ##.###
        #..#.#
        ######
        #....#
    """),
    "mite": ("alien", """
        ..  ..
        .#  #.
        ##  ##
        #.  .#
    """),
    "mite_diving": ("diver", """
        ..  ..
        #.  .#
        .#  #.
        #.  .#
    """),
    "guard": ("alien", """
        #.##.#  ..##..
        .####.  ######
        #.##.#  #.##.#
        .#..#.  #....#
    """),
    "guard_tell": ("alien", """
        #.##.#
        .####.
        ##..##
        ..##..
    """),
    "guard_cracked": ("alien", """
        #..#.#
        .##.#.
        #.##.#
        .#..#.
    """),
    "ship": ("player", """
        ..##..
        ..##..
        ######
        #.##.#
    """),
    "shielded": ("player", """
        .####.
        #.##.#
        ######
        #.##.#
    """),
    "ufo": ("ufo", """
        ...####...
        .########.
        ##.#..#.##
        .##.##.##.
    """),
    "pickup": ("pickup", """
        .###.
        #...#
        #...#
        .###.
    """),
    "player": ("player_shot", """
        #
        #
        #
        #
    """),
    "pellet": ("enemy_shot", """
        #.  .#
        .#  #.
        #.  .#
        .#  #.
    """),
    "dart": ("enemy_shot", """
        ##
        .#
    """),
    "hunter": ("enemy_shot", """
        #.
        ..
        ##
        .#
    """),
    "needle": ("enemy_shot", """
        #
        .
        #
    """),
    "slug": ("enemy_shot", """
        ##
        ##
        ##
    """),
    "cluster": ("enemy_shot", """
        ##
        ..
        ##
    """),
    "beam_warn": ("telegraph", """
        #
        #
        .
        .
    """),
    "beam_live": ("enemy_shot", """
        ##
        ##
        ##
        ##
    """),
    "lane_mark": ("telegraph", """
        ..
        ..
        .#
        #.
    """),
    "gap_mark": ("telegraph", """
        ..
        ..
        ..
        ##
    """),
    "gap_open": ("telegraph", """
        ##
        #.
        #.
        ##
    """),
    "gap_close": ("telegraph", """
        ##
        .#
        .#
        ##
    """),
}  # fmt: skip
# Boss part templates, one per scale, in role boss: "#" hull, "x" hull that breaks off when cracked, "w" the window
# (shut when shielded, a hole when open, striped while charging). part_frames() makes the four frames.
PART_PX3 = {
    "warden.core": """
        ...#####################...
        ..##...##wwwwwwwww##...##..
        .##.###.#wwwwwwwww#.###.##.
        ###.###.#wwwwwwwww#.###.###
        .##.....##wwwwwww##.....##.
        ..####x####x###x####x####..
    """,
    "warden.turret": """
        ...###...
        ..#www#..
        .##www##.
        ####x####
        #.##x##.#
        #.......#
    """,
    "hive_queen.queen": """
        #..........#######..........#
        .##......##wwwwwww##......##.
        ...##..###wwwwwwwww###..##...
        .....####.wwwwwwwww.####.....
        ...##..###.#wwwww#.###..##...
        .##.....x##.#####.##x.....##.
    """,
    "dreadnought.core": """
        ###########################
        #.#.#.##wwwwwwwwwww##.#.#.#
        ########wwwwwwwwwww########
        ###x####wwwwwwwwwww####x###
        #########wwwwwwwww#########
        .#..#..##x#######x##..#..#.
    """,
    "dreadnought.battery": """
        ###############
        ##.###...###.##
        ##.#wwwwwww#.##
        ####wwwwwww####
        .###x#####x###.
        ..#.#.#.#.#.#..
    """,
    "mothership.core": """
        ......#################################......
        ...####.#.#.#.#.###wwwwwww###.#.#.#.#.####...
        .#####.........###wwwwwwwww###.........#####.
        ###############x##wwwwwwwww##x###############
        .#####.#.#.#.#.###wwwwwwwww###.#.#.#.#.#####.
        ......#####x#####x#########x#####x#####......
    """,
    "mothership.pod": """
        ....#######....
        .###wwwwwww###.
        ##.wwwwwwwww.##
        ##.wwwwwwwww.##
        .###.x###x.###.
        ....#######....
    """,
}
PART_PX2 = {
    "warden.core": """
        ..##############..
        .##.##wwwwww##.##.
        ##.###wwwwww###.##
        .####x##xx##x####.
    """,
    "warden.turret": """
        ..##..
        .#ww#.
        ##xx##
        #....#
    """,
    "hive_queen.queen": """
        #.......####.......#
        .##...##wwww##...##.
        ...####.wwww.####...
        .##...x##..##x...##.
    """,
    "dreadnought.core": """
        ##################
        #.#.##wwwwww##.#.#
        ##x###wwwwww###x##
        .#..##x####x##..#.
    """,
    "dreadnought.battery": """
        ##########
        #.#wwww#.#
        ###wwww###
        .#x.##.x#.
    """,
    "mothership.core": """
        ....######################....
        .###.#.#.#.##wwww##.#.#.#.###.
        #########x###wwww###x#########
        ....####x############x####....
    """,
    "mothership.pod": """
        ..######..
        .##wwww##.
        ##.wwww.##
        ..##xx##..
    """,
}


def frames(picture: str) -> list[list[str]]:
    """The frames of a picture, each as rows of text."""
    return [list(rows) for rows in zip(*(line.split() for line in picture.strip().splitlines()))]


def doubled(rows: list[str]) -> list[str]:
    return [row.replace(".", "..").replace("#", "##") for row in rows for _ in (0, 1)]


def part_frames(template: str) -> list[list[str]]:
    """A boss part's four frames, in PART_FRAMES order, from its template."""
    (rows,) = frames(template)
    left = min(row.index("w") for row in rows if "w" in row)

    def frame(window, cracked=False) -> list[str]:
        pixel = {"#": "#", "x": "." if cracked else "#", ".": "."}
        return ["".join(window(x) if ch == "w" else pixel[ch] for x, ch in enumerate(row)) for row in rows]

    return [
        frame(lambda x: "."),  # open: the window is a hole where the weak point shows
        frame(lambda x: "#."[(x - left) % 2]),  # charging: striped, in columns, so the 2:1 pool keeps the stripes
        frame(lambda x: ".", cracked=True),  # cracked: open, and the hull breaks
        frame(lambda x: "#"),  # shielded: the window shut
    ]


def tables(px: dict, parts: dict) -> dict[str, tuple[str, list[list[str]]]]:
    """Every pixel sprite of one scale: key -> (role, frames as rows of text)."""
    table = {name: (role, frames(picture)) for name, (role, picture) in px.items()}
    for part, template in parts.items():
        for suffix, rows in zip(PART_FRAMES, part_frames(template), strict=True):
            table[part + suffix] = ("boss", [rows])
    return table


SPRITES2, SPRITES3 = tables(PX2, PART_PX2), tables(PX3, PART_PX3)

# The glyph each pixel sprite stands for: the glyph arena draws it in those cells, and the pixels must stay inside
# them (see box()).
GLYPHS = {kind: glyphs[0] for kind, glyphs in ALIENS.items()}
GLYPHS |= {f"{kind}_tell": glyph for kind, glyph in TELLS.items()}
GLYPHS |= {f"{kind}_cracked": glyph for kind, glyph in CRACKED.items()}
GLYPHS |= {"diving": DIVES["diver"], "mite_diving": DIVES["mite"]} | SHOTS | HAZARDS
GLYPHS |= {part + suffix: glyph for part, glyphs in PARTS.items() for suffix, glyph in zip(PART_FRAMES, glyphs)}
GLYPHS |= {"ship": SHIP, "shielded": SHIELDED, "ufo": UFO, "pickup": "[R]"}
GLYPHS |= {"solid": SOLID, "cracked": CRACKED_CELL, "star": STAR}

# The art keys Phase 4 spec 3.7 requires at px1, px2 and px3; validate.py checks them through missing_art().
REQUIRED = (
    *ALIENS,
    *(f"{kind}_tell" for kind in ("gunner", "lancer", "bomber", "splitter", "carrier")),
    *(f"{kind}_cracked" for kind in ("shell", "bomber", "splitter", "carrier", "guard")),
    "diving",
    *SHOTS,
    *(part + suffix for part in PARTS for suffix in ("", "_charge", "_cracked")),
    "beam_warn",
    "beam_live",
    "gap_mark",
    "lane_mark",
    "pickup",
)


def missing_art(keys=REQUIRED) -> list[str]:
    """The keys without a glyph (px1), px2 art or px3 art, in order: [] when each is drawn at every scale."""
    return [key for key in keys if not (key in GLYPHS and key in SPRITES2 and key in SPRITES3)]


def missing_pickups(kinds=PICKUP_KINDS) -> list[str]:
    """The pickup kinds without a letter (the ring is shared by all)."""
    return [kind for kind in kinds if kind not in PICKUPS]


def artwork(k: int) -> dict[str, tuple[Art, ...]]:
    """Every sprite's frames for scale k (2, 3, 4 or 6).

    A shelter unit is solid, or (hp 1) a checkerboard of whole cells in its two phases: PixelArena picks the phase
    that continues the pattern across a shelter, and whole cells survive the 2:1 pool where single pixels would not."""
    grow = doubled if k in (4, 6) else list
    art = {
        name: tuple(Art.from_rows(grow(rows), SLOT[role]) for rows in pictures)
        for name, (role, pictures) in (SPRITES3 if k % 3 == 0 else SPRITES2).items()
    }
    checker = [("#." * k)[phase : phase + k] for phase in (0, 1)]
    cracked = ([checker[(row // 2 + phase) % 2] for row in range(2 * k)] for phase in (0, 1))
    art["solid"] = (Art.from_rows(["#" * k] * (2 * k), SLOT["shelter"]),)
    art["cracked"] = tuple(Art.from_rows(rows, SLOT["shelter"]) for rows in cracked)
    art["star"] = (Art.from_rows(["#"], SLOT["star"]),)
    return art


def backdrop(act: int, k: int, stars) -> list[tuple[int, int]]:
    """The layer pixels of an act's backdrop at scale k (Phase 4 spec 15.4), drawn in role star: act I a dot at each
    star (as Phase 3 drew them), act II a k-pixel dash there, act III sparse dithered bands over rows 0-2 and 9-10, and
    act IV a dashed hull band with rivets along row 0. One pixel tall throughout: never a vertical line or a zig-zag,
    and nothing on the ground row."""
    if act not in (1, 2, 3, 4):
        raise ValueError(f"no backdrop for act {act!r}")
    w, h = WIDTH * k, 2 * (HEIGHT - 1) * k  # the ground row is left bare
    if act == 2:
        return [(x * k + i, (2 * y + 1) * k) for x, y in stars for i in range(k)]
    if act == 3:
        rows = [*range(0, 6 * k), *range(18 * k, 22 * k)]  # model rows 0-2 and 9-10
        return [(x, y) for y in rows if y % 2 == 0 for x in range(w) if (x + y) % 4 == 0]
    if act == 4:
        line, rivet, step = 2 * k - 1, k // 2, 4 * k
        return [(x, line) for x in range(w) if x % step < 3 * k] + [(x, rivet) for x in range(k, w, step)]
    return [(math.floor((x + 0.5) * k), (2 * y + 1) * k) for x, y in stars if (2 * y + 1) * k < h]


def bounds(art: Art) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of the opaque pixels, exclusive at the right and bottom; None when empty."""
    rows = [y for y in range(art.h) if art.runs[y]]
    if not rows:
        return None
    left = min(runs[0][0] for runs in art.runs if runs)
    right = max(runs[-1][0] + len(runs[-1][1]) for runs in art.runs if runs)
    return left, rows[0], right, rows[-1] + 1


def box(name: str, x: int, y: int, k: int) -> tuple[int, int, int, int]:
    """The pixels of the cells the glyph arena gives sprite name at model position (x, y), as bounds() gives them."""
    left = x - len(GLYPHS[name]) // 2
    return left * k, 2 * y * k, (left + len(GLYPHS[name])) * k, 2 * (y + 1) * k


def placed(x: float, y: float, art: Art, k: int) -> tuple[int, int]:
    """Top-left pixel of art centred on model position (x, y): centre ((x + 0.5)k, (y + 0.5)2k), halves rounded up,
    so motion is smooth at sub-cell steps."""
    return math.floor((x + 0.5) * k - art.w / 2 + 0.5), math.floor((y + 0.5) * 2 * k - art.h / 2 + 0.5)


def cell(v: float) -> int:
    """The cell a model coordinate falls in, with halves rounded up as placed() rounds them (never to even: bug h)."""
    return math.floor(v + 0.5)
