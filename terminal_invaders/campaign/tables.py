"""Every Campaign number, in frozen tables (spec §3.4). No logic lives here.

The engine reads these tables, `campaign.validate` checks them, the bots import them (never copy them), and the
tuning stream edits only this file. A value the spec doesn't give is marked `# placeholder`.

Shapes of the tables:
* a mapping (read-only `MappingProxyType`, keyed by name, in a fixed order) of frozen rows: SHOTS, ENEMIES, SHAPES,
  FORMATIONS, PATTERNS, TELEGRAPHS (name -> ticks), PICKUPS, UPGRADES, PROFILES, BOSSES;
* a tuple of frozen rows: ACTS (4), WAVES (24, wave n at index n - 1);
* one frozen row: PLAYER, FIRE, DIVE, ARRIVAL, PICKUP, BOMB, COMBO, SCORING, UFO, SHELTERS, TIMING, ENDLESS, SALTS,
  LIMITS.

Rows are frozen, slotted dataclasses whose containers are tuples. A Game keeps names and indices into these tables,
never a table or a row it would have to copy: a MappingProxyType can't be deep-copied, and the bots' audit deep-copies
games.

Units: positions and speeds in cells and cells a tick, times in ticks (60 a second), shares as fractions.
"""

from dataclasses import dataclass
from types import MappingProxyType

# --- the ship and its fire (§5) --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlayerRow:
    y: float  # the ship's row
    x_min: float  # the ship's range
    x_max: float
    start_x: float
    speed: float  # cells a tick; THRUSTERS adds UPGRADES["THRUSTERS"].value per stack
    box: tuple[float, float]  # hit box half-sizes (F5), smaller than the drawing
    drawn: float  # half-width of the ship's drawing


PLAYER = PlayerRow(y=18.0, x_min=2.0, x_max=57.0, start_x=30.0, speed=0.40, box=(1.1, 0.4), drawn=1.5)


@dataclass(frozen=True, slots=True)
class FireRow:
    slots: int  # shots (volleys) in flight; TRIPLE SHOT makes it 3
    gate: int  # ticks between volleys
    muzzle_y: float  # player shots spawn here, a full cell above the ship
    exit_y: float  # a shot above this has left the field (a miss unless it hit)
    speed: float  # cells a tick; RAIL ROUNDS adds UPGRADES["RAIL ROUNDS"].value per stack
    damage: int
    spread: tuple[float, ...]  # vx of a SPREAD volley's three shots (one slot); RAPID's factor is in PICKUPS


FIRE = FireRow(slots=2, gate=8, muzzle_y=17.0, exit_y=-1.0, speed=0.55, damage=1, spread=(-0.10, 0.0, 0.10))

# --- enemy shots (§6.1) ----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShotKind:
    kind: str
    speed: float  # factor of the act's pellet speed v_a (director row x profile speed, capped by LIMITS.vy)
    path: str  # straight, aimed (vx = (target - x) / t), lead (aimed at the ship's lead point), split
    shelter: int  # damage to the shelter cell it strikes
    splash: int  # damage to each neighbouring cell
    box: tuple[float, float]  # hit box half-sizes; (0.0, 0.0) is a point
    telegraph: int  # the shooter's tell, Normal ticks, before every multiplier; 0 for none
    vx_max: float  # |vx| bound of an aimed shot; 0.0 for straight shots
    split_y: float = 0.0  # cluster: the row where it splits
    split: tuple[float, ...] = ()  # cluster: vx of the pellets it splits into


SHOTS = MappingProxyType(
    {
        "pellet": ShotKind("pellet", 1.0, "straight", 1, 0, (0.0, 0.0), 0, 0.0),
        "dart": ShotKind("dart", 1.15, "aimed", 1, 0, (0.0, 0.0), 18, 0.12),
        "hunter": ShotKind("hunter", 1.15, "lead", 1, 0, (0.0, 0.0), 24, 0.12),
        "needle": ShotKind("needle", 1.6, "straight", 1, 0, (0.0, 0.0), 24, 0.0),
        "slug": ShotKind("slug", 0.7, "straight", 3, 1, (0.6, 0.4), 24, 0.0),
        "cluster": ShotKind("cluster", 0.9, "split", 1, 0, (0.0, 0.0), 18, 0.0, split_y=12.0, split=(-0.08, 0.0, 0.08)),
    }
)

# Tells that aren't a shot kind's own (a volley pattern's, a wobble's), Normal ticks before every multiplier. The
# profile (PROFILES[...].telegraph), SCANNER and Endless multiply them, then LIMITS.telegraph is the floor (F3).
TELEGRAPHS = MappingProxyType(
    {
        "burst": 18,  # the shooter flashes its tell pose
        "crossfire": 24,  # both gunners
        "curtain": 30,  # the gap marks fill in
        "dive": 30,  # a diver wobbles before it dives
        "mite": 30,  # a mite wobbles where it is released
    }
)

# --- enemies, shapes and movements (§7) ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnemyKind:
    kind: str
    hp: int
    points: int
    behaviour: str  # hold, dive, weave, back (back rows only), split, carry, mite, escort
    shot: str  # the shot kind it fires; "" never fires (gunners switch to hunter by PROFILES[...].hunters)
    first: int  # first wave it appears in (Normal)
    rows: str  # where a flight places it: front, mid, back or any (scouts fill the rest)
    box: tuple[float, float]  # hit box half-sizes  # placeholder: today's 1.35 x 0.48
    new: bool  # counts toward "one new kind every 2 waves" (guards and mites don't)
    release: int = 0  # splitter: mites released on death (Easy: PROFILES[...].mites)
    launch: int = 0  # carrier: ticks between mite launches
    children: int = 0  # carrier: most of its mites alive at once
    amplitude: float = 0.0  # weaver: zig-zag half-width round its slot
    period: int = 0  # weaver: triangle period in ticks
    phase: int = 0  # weaver: ticks of phase per column
    dive_points: int = 1  # points multiplier when shot mid-dive
    dive_speed: float = 1.0  # factor of the dive's vy (mites dive at 1.2x)


_BOX = (1.35, 0.48)  # placeholder: today's enemy hit box
ENEMIES = MappingProxyType(
    {
        "scout": EnemyKind("scout", 1, 10, "hold", "pellet", 1, "any", _BOX, True),
        "diver": EnemyKind("diver", 1, 30, "dive", "", 3, "back", _BOX, True, dive_points=2),
        "gunner": EnemyKind("gunner", 1, 20, "hold", "dart", 5, "front", _BOX, True),
        "weaver": EnemyKind(
            "weaver", 1, 30, "weave", "pellet", 7, "mid", _BOX, True, amplitude=1.5, period=48, phase=12
        ),
        "shell": EnemyKind("shell", 2, 40, "hold", "pellet", 9, "front", _BOX, True),
        "lancer": EnemyKind("lancer", 1, 40, "back", "needle", 11, "back", _BOX, True),
        "bomber": EnemyKind("bomber", 2, 50, "hold", "slug", 13, "back", _BOX, True),
        "splitter": EnemyKind("splitter", 2, 40, "split", "cluster", 15, "any", _BOX, True, release=2),
        "carrier": EnemyKind("carrier", 3, 80, "carry", "pellet", 19, "any", _BOX, True, launch=360, children=2),
        "mite": EnemyKind("mite", 1, 10, "mite", "", 15, "any", _BOX, False, dive_speed=1.2),
        "guard": EnemyKind("guard", 2, 50, "escort", "pellet", 6, "any", _BOX, False),
    }
)
# weaver phase=12 and every "any" placement above are placeholders; the spec names front, mid and back only.


@dataclass(frozen=True, slots=True)
class Shape:
    """A slot generator (§7.2). Slots are `spacing` cells apart across and `pitch` rows apart down."""

    name: str
    rows: int  # most rows (vee, diamond, ring: from the size)
    cols: int  # most slots across
    pitch: int  # row pitch in cells
    spacing: int  # cells between slot centres across (vee: how much further in each row is, per arm)
    shift: int  # cells odd rows shift right (checker)
    sizes: tuple[int, ...] = ()  # the legal sizes, when not every size up to rows x cols is
    offsets: tuple[tuple[int, int], ...] = ()  # ring: 16 slot offsets in quarter cells (dx, dy), no libm


SHAPES = MappingProxyType(
    {
        "line": Shape("line", 1, 14, 0, 4, 0),
        "rect": Shape("rect", 3, 14, 2, 4, 0),
        "checker": Shape("checker", 3, 13, 2, 4, 2),
        "vee": Shape("vee", 7, 13, 1, 3, 0, sizes=(1, 3, 5, 7, 9, 11, 13)),
        "diamond": Shape("diamond", 5, 5, 1, 4, 0, sizes=(5, 13)),
        "ring": Shape(
            "ring",
            0,
            8,
            0,
            0,
            0,
            sizes=(1, 2, 3, 4, 5, 6, 7, 8),
            # an ellipse of half-width 7 and half-height 2.5, every 22.5 degrees
            offsets=(
                (28, 0), (26, 4), (20, 7), (11, 9), (0, 10), (-11, 9), (-20, 7), (-26, 4),
                (-28, 0), (-26, -4), (-20, -7), (-11, -9), (0, -10), (11, -9), (20, -7), (26, -4),
            ),
        ),
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Movement:
    """A formation movement (§7.2); `speed` is per act (I, II, III, IV) on Normal."""

    name: str
    first: int  # first wave
    speed: tuple[float, float, float, float]  # MARCH: beat B in ticks; GLIDE, SNAKE: drift vx; PINCER: inward vx;
    #                                           CAROUSEL: slot circulation; COLUMNS: ticks between drops; ESCORT: lag
    drop: float  # rows dropped at an edge (MARCH, GLIDE, SNAKE) or per COLUMNS step
    bounces: int  # drop at every n-th edge bounce; 0 never
    step: float = 0.0  # MARCH: cells a beat
    scale: tuple[float, float] = (0.0, 0.0)  # MARCH: beat = max(LIMITS.beat, B x (a + b x alive / size))
    amplitude: float = 0.0  # SNAKE: column offset amplitude
    period: int = 0  # SNAKE: triangle period in ticks
    phase: int = 0  # SNAKE: ticks of phase per column
    drift: float = 0.0  # CAROUSEL: the flight's drift vx
    entry: tuple[float, float] = (0.0, 0.0)  # PINCER: the two half-flights' entry x
    then: str = ""  # PINCER: the movement it settles into
    columns: tuple[int, int] = (0, 0)  # COLUMNS: fewest and most columns
    orbit: float = 0.0  # ESCORT ring: entries of the ring's ellipse table its slots move round a play tick


FORMATIONS = MappingProxyType(
    {
        "MARCH": Movement("MARCH", 1, (26, 22, 19, 16), 0.5, 1, step=1.0, scale=(0.35, 0.65)),
        "GLIDE": Movement("GLIDE", 4, (0.06, 0.07, 0.08, 0.09), 0.5, 2),
        "SNAKE": Movement("SNAKE", 8, (0.04, 0.04, 0.04, 0.04), 0.5, 2, amplitude=2.5, period=180, phase=12),
        "PINCER": Movement("PINCER", 10, (0.10, 0.10, 0.10, 0.10), 0.5, 1, step=1.0, entry=(-4.0, 64.0), then="MARCH"),
        "CAROUSEL": Movement("CAROUSEL", 14, (0.06, 0.06, 0.06, 0.06), 0.0, 0, drift=0.03),
        "COLUMNS": Movement("COLUMNS", 17, (40, 40, 40, 40), 0.5, 0, columns=(5, 7)),
        "ESCORT": Movement("ESCORT", 6, (12, 12, 12, 12), 0.0, 0, orbit=0.03),  # placeholder lag; a turn in 533
    }
)


@dataclass(frozen=True, slots=True)
class DiveRules:
    """Divers and mites (§7.3); per-act counts are in ACTS, per-profile vy and factors in PROFILES."""

    vx_max: float  # |vx| of a dive, aimed at the ship's x at launch (vx = dx / t)
    ram: tuple[float, float]  # ram box half-sizes
    exit_y: float  # a missing diver passes this row...
    reenter_y: float  # ...re-enters here, above its slot's current position...
    home: float  # ...and glides home at this speed, neither firing nor ramming
    mite_spread: float = 1.5  # a splitter's two mites are released this far left and right of it  # placeholder


DIVE = DiveRules(vx_max=0.15, ram=(1.9, 0.7), exit_y=20.0, reenter_y=-2.0, home=0.25)


@dataclass(frozen=True, slots=True)
class ArrivalRules:
    """Flights arriving on the schedule (§7.3) and landing (§5)."""

    top: float  # a flight arrives with its top row here
    entry: float  # cells a tick it enters (and a push moves) at
    gap: float  # the band below a flight: its rows plus this gap
    push_floor: float  # a push may not take an enemy below this row; the flight waits instead
    apart: float  # a flight's own drop is skipped while it would come within this of a lower flight
    land: float  # an enemy in formation at or below this row lands
    sweep: int  # SWEEP: a flight destroyed within this many ticks of becoming targetable
    rise: float = 8.0  # an entering flight starts this many cells above its slots
    centre: float = 29.5  # a flight's centre x when it arrives (the middle of the ship's range)


ARRIVAL = ArrivalRules(top=2.0, entry=0.25, gap=2.0, push_floor=12.0, apart=2.0, land=15.5, sweep=480)

# --- the fire director (§6.3) ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pattern:
    name: str
    shots: tuple[int, int]  # fewest and most shots in the volley
    slots: int  # director slots the volley holds from its telegraph until its last shot ends
    spacing: int  # ticks between its shots (0: all at once)
    first: int  # first wave it may be drawn in (validate checks the director rows against it)
    reach: float  # burst: the shooter is within this |dx| above the ship; crossfire: each gunner at least this far
    #               left or right of the ship; curtain: the gap's centre within this of the ship
    gap: float = 0.0  # curtain: the gap's width
    telegraph: str = ""  # its key in TELEGRAPHS; "" uses the shooter's shot kind's tell


PATTERNS = MappingProxyType(
    {
        "single": Pattern("single", (1, 1), 1, 0, 1, 0.0),
        "burst": Pattern("burst", (3, 3), 1, 8, 8, 8.0, telegraph="burst"),
        "crossfire": Pattern("crossfire", (2, 2), 2, 0, 10, 8.0, telegraph="crossfire"),
        "curtain": Pattern("curtain", (5, 7), 2, 0, 17, 10.0, gap=7.0, telegraph="curtain"),
    }
)


@dataclass(frozen=True, slots=True)
class Director:
    """One wave's (or boss wave's escort) fire director row. `staged(director=...)` overrides these fields by name."""

    slots: int  # volleys in flight at once
    reload: int  # ticks between volleys: reload_eff = max(LIMITS.reload, reload x frenzy / pressure)
    v_a: float  # pellet speed, before the profile's speed factor
    focus: float  # share of shooters drawn from the buckets within 6 cells of the ship
    reaction: float  # share of volleys fired at full speed (only F1), before the profile's factor; the rest are safe
    single: int  # pattern weights (Easy drops crossfire; Hard adds PROFILES[...].burst and .crossfire)
    burst: int
    crossfire: int
    curtain: int
    frenzy: float = 0.75  # reload factor once every flight has arrived and at most `frenzy_left` enemies remain
    frenzy_left: int = 8


# --- acts and waves (§8) ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Act:
    number: int
    roman: str
    name: str
    line: str  # the act intro's line
    first: int  # first and last wave (the boss's)
    last: int
    boss: str  # key in BOSSES
    divers: int  # most divers diving at once (Normal)
    dive_every: int  # ticks between dive picks (Normal)


ACTS = (
    Act(1, "I", "CONTACT", "Shoot first. Then learn to move.", 1, 6, "warden", 1, 360),
    Act(2, "II", "SWARM", "They weave, they dive, they aim.", 7, 12, "hive_queen", 2, 300),
    Act(3, "III", "SIEGE", "Your cover will not last.", 13, 18, "dreadnought", 2, 270),
    Act(4, "IV", "ARMADA", "Everything you have learned, at once.", 19, 24, "mothership", 3, 240),
)


@dataclass(frozen=True, slots=True)
class Flight:
    """A flight of enemies (§7.3): when it arrives, its shape, movement and mix."""

    at: int  # arrival: playing ticks from the wave's start (frozen outside play, x0.6 under SLOW)
    size: int  # enemies; a shape's last slots may stay empty
    shape: str  # key in SHAPES
    dims: tuple[int, ...]  # rect, checker: (rows, cols); line: (cols,); vee, diamond, ring: (n,)
    movement: str  # key in FORMATIONS
    mix: tuple[tuple[str, int], ...] = ()  # (kind, count) placed by ENEMIES[kind].rows; the rest are scouts


@dataclass(frozen=True, slots=True)
class Wave:
    number: int
    name: str
    line: str  # the intro's NEW: line, or the boss's line (each at most 52 characters)
    director: Director
    flights: tuple[Flight, ...]  # in arrival order; empty in boss waves (escorts are in BOSSES)
    cargo: int  # index of the flight that carries the wave's cargo enemy; -1 for none
    ufo: bool  # the UFO may appear
    boss: str  # key in BOSSES; "" for a regular wave
    card: bool  # an upgrade card follows the clear
    squadron: int = 1  # divers launched together (wave 20: 3, in mirrored pairs)


WAVES = (
    Wave(  # 1
        number=1,
        name="FIRST CONTACT",
        line="NEW: SCOUTS - SHOOT THEM, HIDE BEHIND SHELTERS",
        # tuned (T round 1, Q3): reload 240 -> 300, and the last scouts fire slower, not faster (frenzy 0.75 ->
        # 1.5): 56% of the novice's hits came with at most 3 scouts left. The novice cleared 65% (reload 300
        # alone: 66%), now 87% (300 tuning seeds); strong's wave 1 stays at 52 s (shortest 50)
        director=Director(
            slots=1,
            reload=300,
            v_a=0.130,
            focus=0.0,
            reaction=0.0,
            single=100,
            burst=0,
            crossfire=0,
            curtain=0,
            frenzy=1.5,
        ),
        flights=(
            Flight(0, 6, "line", (6,), "MARCH", ()),
            Flight(570, 6, "line", (6,), "MARCH", ()),
            Flight(1140, 7, "line", (7,), "MARCH", ()),
            Flight(1680, 7, "line", (7,), "MARCH", ()),
            Flight(2250, 7, "line", (7,), "MARCH", ()),
            Flight(2820, 7, "line", (7,), "MARCH", ()),
        ),
        cargo=-1,
        ufo=False,
        boss="",
        card=False,
    ),
    Wave(  # 2
        number=2,
        name="PICKET LINE",
        line="NEW: THE MYSTERY SHIP AND $ CARGO - BOTH DROP GIFTS",
        director=Director(
            slots=2, reload=120, v_a=0.130, focus=0.0, reaction=0.0, single=100, burst=0, crossfire=0, curtain=0
        ),
        flights=(
            Flight(0, 12, "rect", (2, 6), "MARCH", ()),
            Flight(750, 13, "vee", (13,), "MARCH", ()),
            Flight(1500, 12, "rect", (2, 6), "MARCH", ()),
            Flight(2340, 13, "vee", (13,), "MARCH", ()),
            Flight(3150, 18, "rect", (3, 6), "MARCH", ()),
        ),
        cargo=2,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 3
        number=3,
        name="DIVE ALARM",
        line="NEW: DIVERS - THEY WOBBLE, THEN DIVE AT YOU",
        director=Director(
            slots=2, reload=105, v_a=0.135, focus=0.0, reaction=0.0, single=100, burst=0, crossfire=0, curtain=0
        ),
        # placeholder shapes, flights 1, 2: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 12, "rect", (2, 6), "MARCH", ()),
            Flight(750, 12, "rect", (2, 6), "MARCH", (("diver", 2),)),
            Flight(1500, 13, "diamond", (13,), "MARCH", (("diver", 2),)),
            Flight(2340, 14, "rect", (2, 7), "MARCH", (("diver", 3),)),
            Flight(3240, 15, "rect", (3, 5), "MARCH", (("diver", 3),)),
        ),
        cargo=2,
        ufo=True,
        boss="",
        card=True,
    ),
    Wave(  # 4
        number=4,
        name="DRIFT",
        line="NEW: GLIDING FLEETS - LEAD YOUR SHOTS",
        director=Director(
            slots=3, reload=90, v_a=0.135, focus=0.3, reaction=0.0, single=100, burst=0, crossfire=0, curtain=0
        ),
        # placeholder shapes, flights 1, 3, 5, 6: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 12, "rect", (2, 6), "GLIDE", ()),
            Flight(660, 13, "vee", (13,), "MARCH", (("diver", 2),)),
            Flight(1320, 14, "rect", (2, 7), "GLIDE", ()),
            Flight(1980, 13, "vee", (13,), "MARCH", (("diver", 2),)),
            Flight(2640, 14, "rect", (2, 7), "GLIDE", ()),
            Flight(3360, 16, "checker", (2, 8), "MARCH", (("diver", 3),)),
        ),
        cargo=2,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 5
        number=5,
        name="MARKSMEN",
        line="NEW: GUNNERS - THE EYE OPENS, THEN THEY AIM",
        director=Director(
            slots=3, reload=80, v_a=0.140, focus=0.3, reaction=0.0, single=100, burst=0, crossfire=0, curtain=0
        ),
        # placeholder shapes, flights 1, 2, 3, 5, 6: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 12, "rect", (2, 6), "MARCH", (("gunner", 3),)),
            Flight(660, 13, "vee", (13,), "MARCH", (("diver", 2),)),
            Flight(1290, 14, "rect", (2, 7), "GLIDE", (("gunner", 3),)),
            Flight(1950, 14, "checker", (2, 7), "MARCH", (("gunner", 2), ("diver", 2))),
            Flight(2670, 15, "rect", (3, 5), "MARCH", (("gunner", 3),)),
            Flight(3390, 16, "rect", (2, 8), "MARCH", (("gunner", 3), ("diver", 2))),
        ),
        cargo=2,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 6
        number=6,
        name="WARDEN",
        line="Break the turrets to open the core.",
        director=Director(
            slots=1, reload=80, v_a=0.140, focus=0.3, reaction=0.05, single=100, burst=0, crossfire=0, curtain=0
        ),  # placeholder: the boss wave's escort director
        flights=(),
        cargo=-1,
        ufo=False,
        boss="warden",
        card=True,
    ),
    Wave(  # 7
        number=7,
        name="WEAVERS",
        line="NEW: WEAVERS ZIG-ZAG - SHOOT WHERE THEY WILL BE",
        director=Director(
            slots=3, reload=72, v_a=0.145, focus=0.3, reaction=0.04, single=100, burst=0, crossfire=0, curtain=0
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 14, "rect", (2, 7), "MARCH", (("weaver", 6),)),
            Flight(630, 14, "rect", (2, 7), "GLIDE", (("gunner", 3),)),
            Flight(1260, 15, "rect", (3, 5), "MARCH", (("weaver", 6), ("diver", 2))),
            Flight(1920, 15, "rect", (3, 5), "GLIDE", (("gunner", 3),)),
            Flight(2610, 16, "rect", (2, 8), "MARCH", (("weaver", 6), ("gunner", 2))),
            Flight(3330, 16, "rect", (2, 8), "GLIDE", (("weaver", 4), ("diver", 3))),
        ),
        cargo=2,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 8
        number=8,
        name="SERPENT",
        line="NEW: BURSTS - THREE QUICK SHOTS FROM ONE SHIP",
        director=Director(
            slots=3, reload=68, v_a=0.145, focus=0.3, reaction=0.04, single=70, burst=20, crossfire=0, curtain=0
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 15, "rect", (3, 5), "SNAKE", (("weaver", 4),)),
            Flight(630, 15, "rect", (3, 5), "SNAKE", (("gunner", 3),)),
            Flight(1320, 16, "rect", (2, 8), "SNAKE", (("weaver", 4), ("diver", 2))),
            Flight(2010, 16, "rect", (2, 8), "SNAKE", (("gunner", 3),)),
            Flight(2700, 16, "rect", (2, 8), "SNAKE", (("weaver", 4), ("gunner", 2))),
            Flight(3420, 18, "rect", (3, 6), "SNAKE", (("weaver", 4), ("diver", 3))),
        ),
        cargo=2,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 9
        number=9,
        name="IRONCLAD",
        line="NEW: SHELLS TAKE TWO HITS - WATCH THEM CRACK",
        director=Director(
            slots=4, reload=64, v_a=0.150, focus=0.3, reaction=0.04, single=70, burst=20, crossfire=0, curtain=0
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 14, "rect", (2, 7), "MARCH", (("shell", 5),)),
            Flight(540, 14, "rect", (2, 7), "SNAKE", (("weaver", 4), ("diver", 2))),
            Flight(1080, 15, "rect", (3, 5), "MARCH", (("shell", 5), ("gunner", 2))),
            Flight(1680, 15, "rect", (3, 5), "SNAKE", (("weaver", 5),)),
            Flight(2280, 15, "rect", (3, 5), "MARCH", (("shell", 5), ("gunner", 2))),
            Flight(2880, 16, "rect", (2, 8), "SNAKE", (("weaver", 4), ("diver", 3))),
            Flight(3480, 16, "rect", (2, 8), "MARCH", (("shell", 6), ("gunner", 3))),
        ),
        cargo=3,
        ufo=True,
        boss="",
        card=True,
    ),
    Wave(  # 10
        number=10,
        name="PINCER",
        line="NEW: CROSSFIRE - TWO GUNNERS AIM AT ONCE",
        director=Director(
            slots=4, reload=60, v_a=0.150, focus=0.3, reaction=0.04, single=70, burst=20, crossfire=10, curtain=0
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 14, "rect", (2, 7), "PINCER", (("gunner", 4),)),
            Flight(540, 14, "rect", (2, 7), "MARCH", (("shell", 4), ("diver", 2))),
            Flight(1080, 16, "rect", (2, 8), "PINCER", (("gunner", 4), ("weaver", 4))),
            Flight(1680, 14, "rect", (2, 7), "MARCH", (("shell", 4),)),
            Flight(2280, 16, "rect", (2, 8), "PINCER", (("gunner", 4), ("weaver", 4))),
            Flight(2880, 15, "rect", (3, 5), "MARCH", (("shell", 4), ("diver", 3))),
            Flight(3480, 16, "rect", (2, 8), "PINCER", (("gunner", 4), ("weaver", 4))),
        ),
        cargo=3,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 11
        number=11,
        name="LANCERS",
        line="NEW: LANCERS GLOW, THEN FIRE FAST NEEDLES",
        director=Director(
            slots=4, reload=56, v_a=0.155, focus=0.3, reaction=0.04, single=70, burst=20, crossfire=10, curtain=0
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 14, "rect", (2, 7), "MARCH", (("lancer", 3),)),
            Flight(540, 15, "rect", (3, 5), "PINCER", (("shell", 4), ("gunner", 3))),
            Flight(1140, 15, "rect", (3, 5), "SNAKE", (("weaver", 5), ("lancer", 2))),
            Flight(1740, 15, "rect", (3, 5), "MARCH", (("shell", 4), ("diver", 3))),
            Flight(2340, 16, "rect", (2, 8), "PINCER", (("lancer", 3), ("gunner", 3))),
            Flight(2970, 16, "rect", (2, 8), "SNAKE", (("weaver", 5), ("shell", 3))),
            Flight(3570, 17, "rect", (3, 6), "MARCH", (("lancer", 3), ("diver", 3))),
        ),
        cargo=3,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 12
        number=12,
        name="HIVE QUEEN",
        line="Shoot through her ring. Mind the divers.",
        director=Director(
            slots=3, reload=56, v_a=0.155, focus=0.3, reaction=0.05, single=70, burst=20, crossfire=10, curtain=0
        ),  # placeholder: the boss wave's escort director
        flights=(),
        cargo=-1,
        ufo=False,
        boss="hive_queen",
        card=True,
    ),
    Wave(  # 13
        number=13,
        name="BOMBARDMENT",
        line="NEW: BOMBERS - THEIR SLUGS SMASH SHELTERS",
        director=Director(
            slots=4, reload=52, v_a=0.155, focus=0.5, reaction=0.03, single=60, burst=20, crossfire=10, curtain=0
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 14, "rect", (2, 7), "MARCH", (("bomber", 3),)),
            Flight(540, 15, "rect", (3, 5), "GLIDE", (("shell", 4), ("gunner", 3))),
            Flight(1110, 15, "rect", (3, 5), "MARCH", (("bomber", 3), ("weaver", 4))),
            Flight(1710, 16, "rect", (2, 8), "GLIDE", (("lancer", 3), ("diver", 3))),
            Flight(2310, 15, "rect", (3, 5), "MARCH", (("bomber", 3), ("shell", 3))),
            Flight(2910, 16, "rect", (2, 8), "GLIDE", (("gunner", 4), ("weaver", 4))),
            Flight(3510, 17, "rect", (3, 6), "MARCH", (("bomber", 3), ("lancer", 3))),
        ),
        cargo=3,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 14
        number=14,
        name="CAROUSEL",
        line="NEW: CIRCLING FLEETS - WAIT FOR YOUR TARGET",
        director=Director(
            slots=5, reload=50, v_a=0.160, focus=0.5, reaction=0.03, single=60, burst=20, crossfire=10, curtain=0
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 16, "rect", (2, 8), "CAROUSEL", (("gunner", 4),)),
            Flight(600, 16, "rect", (2, 8), "SNAKE", (("shell", 4), ("weaver", 4))),
            Flight(1200, 16, "rect", (2, 8), "CAROUSEL", (("bomber", 3), ("diver", 3))),
            Flight(1770, 16, "rect", (2, 8), "SNAKE", (("lancer", 3), ("gunner", 3))),
            Flight(2370, 16, "rect", (2, 8), "CAROUSEL", (("shell", 4), ("weaver", 4))),
            Flight(2970, 17, "rect", (3, 6), "SNAKE", (("bomber", 3), ("gunner", 3))),
            Flight(3570, 18, "rect", (3, 6), "CAROUSEL", (("lancer", 3), ("diver", 3))),
        ),
        cargo=3,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 15
        number=15,
        name="HYDRA",
        line="NEW: SPLITTERS BURST INTO MITES",
        director=Director(
            slots=5, reload=48, v_a=0.160, focus=0.5, reaction=0.03, single=60, burst=20, crossfire=10, curtain=0
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 14, "rect", (2, 7), "MARCH", (("splitter", 3),)),
            Flight(540, 15, "rect", (3, 5), "CAROUSEL", (("gunner", 4), ("shell", 3))),
            Flight(1140, 15, "rect", (3, 5), "PINCER", (("splitter", 3), ("weaver", 4))),
            Flight(1770, 16, "rect", (2, 8), "MARCH", (("bomber", 3), ("diver", 3))),
            Flight(2370, 15, "rect", (3, 5), "CAROUSEL", (("splitter", 3), ("lancer", 3))),
            Flight(2970, 16, "rect", (2, 8), "PINCER", (("shell", 4), ("gunner", 4))),
            Flight(3600, 17, "rect", (3, 6), "MARCH", (("splitter", 4), ("weaver", 4))),
        ),
        cargo=3,
        ufo=True,
        boss="",
        card=True,
    ),
    Wave(  # 16
        number=16,
        name="HUNTERS",
        line="NEW: HUNTERS AIM WHERE YOU ARE GOING",
        director=Director(
            slots=5, reload=46, v_a=0.160, focus=0.5, reaction=0.03, single=60, burst=20, crossfire=10, curtain=0
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 16, "rect", (2, 8), "GLIDE", (("gunner", 5),)),
            Flight(570, 16, "rect", (2, 8), "SNAKE", (("shell", 4), ("weaver", 4))),
            Flight(1170, 16, "rect", (2, 8), "PINCER", (("gunner", 5), ("diver", 3))),
            Flight(1740, 16, "rect", (2, 8), "GLIDE", (("bomber", 3), ("lancer", 3))),
            Flight(2340, 16, "rect", (2, 8), "SNAKE", (("gunner", 5), ("splitter", 2))),
            Flight(2910, 17, "rect", (3, 6), "PINCER", (("shell", 4), ("weaver", 4))),
            Flight(3510, 18, "rect", (3, 6), "GLIDE", (("gunner", 5), ("lancer", 3))),
        ),
        cargo=3,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 17
        number=17,
        name="CURTAIN",
        line="NEW: CURTAINS - FIND THE MARKED GAP",
        director=Director(
            slots=5, reload=44, v_a=0.165, focus=0.5, reaction=0.03, single=60, burst=20, crossfire=10, curtain=10
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 16, "rect", (3, 6), "COLUMNS", (("shell", 4),)),
            Flight(570, 16, "rect", (2, 8), "MARCH", (("gunner", 4), ("weaver", 4))),
            Flight(1170, 16, "rect", (3, 6), "COLUMNS", (("bomber", 3), ("lancer", 3))),
            Flight(1740, 16, "rect", (2, 8), "MARCH", (("splitter", 3), ("diver", 3))),
            Flight(2310, 16, "rect", (3, 6), "COLUMNS", (("shell", 4), ("gunner", 4))),
            Flight(2880, 17, "rect", (3, 6), "MARCH", (("weaver", 5), ("bomber", 3))),
            Flight(3540, 18, "rect", (3, 6), "COLUMNS", (("lancer", 3), ("splitter", 3))),
        ),
        cargo=3,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 18
        number=18,
        name="DREADNOUGHT",
        line="Watch the dotted line: a beam follows.",
        director=Director(
            slots=4, reload=44, v_a=0.165, focus=0.5, reaction=0.05, single=60, burst=20, crossfire=10, curtain=0
        ),  # placeholder: the boss wave's escort director
        flights=(),
        cargo=-1,
        ufo=False,
        boss="dreadnought",
        card=True,
    ),
    Wave(  # 19
        number=19,
        name="CARRIERS",
        line="NEW: CARRIERS LAUNCH MITES - KILL THEM FIRST",
        director=Director(
            slots=5, reload=42, v_a=0.165, focus=0.5, reaction=0.03, single=50, burst=20, crossfire=15, curtain=15
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 14, "rect", (2, 7), "MARCH", (("carrier", 2),)),
            Flight(540, 15, "rect", (3, 5), "CAROUSEL", (("gunner", 4), ("shell", 3))),
            Flight(1140, 14, "rect", (2, 7), "MARCH", (("carrier", 2), ("weaver", 4))),
            Flight(1740, 15, "rect", (3, 5), "CAROUSEL", (("bomber", 2), ("lancer", 3))),
            Flight(2340, 14, "rect", (2, 7), "MARCH", (("carrier", 2), ("splitter", 2))),
            Flight(2940, 15, "rect", (3, 5), "CAROUSEL", (("gunner", 5), ("diver", 3))),
            Flight(3600, 16, "rect", (2, 8), "MARCH", (("carrier", 2), ("shell", 3))),
        ),
        cargo=3,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 20
        number=20,
        name="STORM",
        line="NEW: SQUADRON DIVES - THREE AT ONCE",
        director=Director(
            slots=6, reload=40, v_a=0.170, focus=0.5, reaction=0.03, single=50, burst=20, crossfire=15, curtain=15
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 16, "rect", (2, 8), "PINCER", (("diver", 5),)),
            Flight(510, 16, "rect", (2, 8), "SNAKE", (("gunner", 4), ("weaver", 4))),
            Flight(1080, 16, "rect", (2, 8), "PINCER", (("diver", 5), ("shell", 3))),
            Flight(1680, 16, "rect", (2, 8), "SNAKE", (("lancer", 3), ("carrier", 2))),
            Flight(2250, 16, "rect", (2, 8), "PINCER", (("diver", 5), ("gunner", 4))),
            Flight(2820, 17, "rect", (3, 6), "SNAKE", (("bomber", 3), ("weaver", 4))),
            Flight(3480, 18, "rect", (3, 6), "PINCER", (("diver", 6), ("splitter", 3))),
        ),
        cargo=3,
        ufo=True,
        boss="",
        card=False,
        squadron=3,
    ),
    Wave(  # 21
        number=21,
        name="VETERANS",
        line="NEW: PLATED VETERANS TAKE ONE MORE HIT",
        director=Director(
            slots=6, reload=38, v_a=0.170, focus=0.5, reaction=0.03, single=50, burst=20, crossfire=15, curtain=15
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 15, "rect", (3, 5), "MARCH", (("shell", 3), ("gunner", 4))),
            Flight(540, 15, "rect", (3, 5), "GLIDE", (("weaver", 4), ("lancer", 2))),
            Flight(1140, 15, "rect", (3, 5), "SNAKE", (("bomber", 2), ("diver", 3))),
            Flight(1740, 15, "rect", (3, 5), "PINCER", (("splitter", 2), ("gunner", 4))),
            Flight(2340, 15, "rect", (3, 5), "CAROUSEL", (("shell", 3), ("weaver", 4))),
            Flight(2970, 15, "rect", (3, 5), "COLUMNS", (("lancer", 3), ("diver", 3))),
            Flight(3570, 16, "rect", (2, 8), "MARCH", (("bomber", 2), ("gunner", 4))),
        ),
        cargo=3,
        ufo=True,
        boss="",
        card=True,
    ),
    Wave(  # 22
        number=22,
        name="GAUNTLET",
        line="EVERY FORMATION, ONE AFTER ANOTHER",
        director=Director(
            slots=6, reload=36, v_a=0.175, focus=0.5, reaction=0.03, single=50, burst=20, crossfire=15, curtain=15
        ),
        # placeholder shapes, every flight: §8.2 names them loosely, so these are the nearest legal ones
        flights=(
            Flight(0, 14, "rect", (2, 7), "MARCH", (("gunner", 4),)),
            Flight(510, 14, "rect", (2, 7), "GLIDE", (("shell", 3), ("weaver", 4))),
            Flight(1020, 14, "rect", (2, 7), "SNAKE", (("diver", 4), ("lancer", 2))),
            Flight(1500, 14, "rect", (2, 7), "PINCER", (("bomber", 2), ("gunner", 4))),
            Flight(2010, 14, "rect", (2, 7), "CAROUSEL", (("carrier", 1), ("splitter", 2))),
            Flight(2580, 14, "rect", (2, 7), "COLUMNS", (("shell", 3), ("weaver", 4))),
            Flight(3150, 14, "rect", (2, 7), "MARCH", (("lancer", 3), ("diver", 3))),
            Flight(3720, 15, "rect", (2, 8), "PINCER", (("gunner", 5), ("bomber", 2))),  # 2 rows: fits the band
        ),
        cargo=3,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 23
        number=23,
        name="LAST LINE",
        line="THE LAST FLEETS FIRE FASTER AS THEY THIN",
        director=Director(
            slots=6,
            reload=34,
            v_a=0.175,
            focus=0.5,
            reaction=0.03,
            single=50,
            burst=20,
            crossfire=15,
            curtain=15,
            frenzy=0.65,
        ),
        # every flight 2 rows (§8.2 names the shapes loosely), so three fit the band above y 12 and no flight waits
        # for the good bot (E-M1's D17); a COLUMNS flight has at most 7 columns, so its three hold 14, not 15
        flights=(
            Flight(0, 14, "rect", (2, 7), "COLUMNS", (("shell", 3), ("gunner", 5))),
            Flight(480, 15, "rect", (2, 8), "PINCER", (("weaver", 4), ("lancer", 3))),
            Flight(960, 15, "rect", (2, 8), "CAROUSEL", (("carrier", 1), ("diver", 4))),
            Flight(1440, 14, "rect", (2, 7), "COLUMNS", (("bomber", 2), ("splitter", 2))),
            Flight(1920, 15, "rect", (2, 8), "PINCER", (("shell", 3), ("gunner", 5))),
            Flight(2460, 15, "rect", (2, 8), "CAROUSEL", (("weaver", 4), ("lancer", 3))),
            Flight(3000, 14, "rect", (2, 7), "COLUMNS", (("carrier", 1), ("diver", 4))),
            Flight(3540, 16, "rect", (2, 8), "PINCER", (("bomber", 2), ("gunner", 5))),
        ),
        cargo=4,
        ufo=True,
        boss="",
        card=False,
    ),
    Wave(  # 24
        number=24,
        name="MOTHERSHIP",
        line="Clear the escorts, break the pods, then the core.",
        director=Director(
            slots=5, reload=40, v_a=0.170, focus=0.5, reaction=0.05, single=50, burst=20, crossfire=15, curtain=15
        ),  # placeholder: the boss wave's escort director
        flights=(),
        cargo=-1,
        ufo=False,
        boss="mothership",
        card=False,
    ),
)

# --- bosses (§9) -----------------------------------------------------------------------------------------------------
# Every boss's parts, phases and attacks as §9 gives them. Fields the spec leaves open (movement speeds and paths,
# twin and dart-pair telegraphs, pod positions and their path, escort mixes) are marked placeholder: T tunes them.
# Part HP tuned (T round 2, §19.3 step 1), about 2.1x §9's for the first three bosses and 1.2-1.3x for the
# MOTHERSHIP: the strong bot took 64-67 s on the first three. Staged immortal (strong, 100 seeds) they now take
# 124 / 122 / 124 / 127 s (good bot 170 / 180 / 176 / 183 s); validate.py's refitted model gives 129-148 s.


@dataclass(frozen=True, slots=True)
class PartRow:
    name: str  # the art key is f"{boss}.{name}" (with _charge and _cracked frames)
    w: int  # width in cells; the hit box is +-w/2 x +-0.45
    dx: float  # offset from the boss's x
    y: float  # its row at the station


@dataclass(frozen=True, slots=True)
class Attack:
    key: str  # the letter §9 gives it
    name: str
    source: str  # the part that fires it (spawns come out below the source's box)
    shot: str  # a SHOTS kind, "beam" (a hazard), or "spawn" (enemies of `kind`)
    count: int  # shots, beams or enemies; 0 for a continuous spiral
    spacing: int  # ticks between the shots of one volley; 0 for all at once
    spread: float  # fan and spray: the outermost |vx|; flak: its half-angle as vx
    telegraph: int  # ticks of charge pose or mark before the first shot (Normal; boss attacks have W >= 30)
    cooldown: int  # ticks between uses, divided by pressure; an attack never overlaps itself
    aim: str  # straight, aimed, lead, fan, spray, shelters, curtain, beam, converge, spiral, spawn
    live: int = 0  # beam: ticks live
    track: int = 0  # beam: ticks its mark follows the source before its x locks (0: fixed from its mark)
    width: float = 0.0  # beam: column width; curtain: the span of its pellets
    gap: float = 0.0  # curtain: the gap's width; a pair of beams: the distance between them
    at: float = 0.0  # fires once, when the phase's vulnerable hp falls to this share (0: on its cooldown)
    below: int = 0  # spawn: only while fewer than this many of the spawned kind live (0: always)
    kind: str = ""  # spawn: the enemy kind
    offset: float = 0.0  # spawn: cells below the source's box
    table: tuple[float, ...] = ()  # spiral: the vx it cycles through
    profiles: tuple[str, ...] = ("easy", "normal", "hard")  # the profiles it is used on


@dataclass(frozen=True, slots=True)
class BossPhase:
    name: str
    vulnerable: tuple[str, ...]  # part names open in this phase; empty: only the escorts can be hit
    hp: int  # hp each vulnerable part has when the phase starts
    move: str  # hold, patrol, sway, figure8, drift, track, ram, descend
    speed: float
    attacks: tuple[Attack, ...]
    path: tuple[tuple[float, float], ...] = ()  # waypoints (patrol span, sway span, figure-8 legs, ram rows)
    dip: tuple[float, int, int] = (0.0, 0, 0)  # (row, ticks held there, every ticks); (0, 0, 0) for none
    pressure: float = 1.0  # divides this phase's reload and cooldowns, with the profile's pressure
    escorts: tuple[Flight, ...] = ()  # flights this phase brings in, at their `at`
    dip_speed: float = 0.05  # cells a tick the boss moves to and from its dip row  # placeholder
    orbit: float = 0.0  # cells a tick the orbiting parts move along BossRow.orbit (0: they hold where they are)


@dataclass(frozen=True, slots=True)
class Variant:
    """A profile's change to a boss: field `field` of attack `attack` (or, with attack "escort", of every escort
    Flight) in phase `phase` (-1: every phase and the boss's own escorts) becomes `value`. validate.py refuses
    any other attack name, so a phase row's fields have no variants."""

    profile: str
    phase: int
    attack: str
    field: str
    value: object


@dataclass(frozen=True, slots=True)
class BossRow:
    kind: str  # model.Boss.kind and the art prefix
    name: str  # shown in the intro and on the boss bar (equals its wave's name)
    epithet: str
    wave: int
    station: tuple[float, float]  # where its centre settles in the intro
    parts: tuple[PartRow, ...]
    phases: tuple[BossPhase, ...]
    escorts: tuple[Flight, ...] = ()  # flights present from the wave's start (guards, the brood)
    variants: tuple[Variant, ...] = ()
    life: int = 1  # extra life at its kill (bosses 1-3), up to the profile's cap
    descent: float = 8.0  # cells it descends to its station during the boss intro
    orbit: tuple[tuple[float, float], ...] = ()  # the closed path (dx, dy from the station) its `pod` parts move
    #                                              round, in order; each pod starts where its PartRow puts it


_PATROL = ((14.0, 3.0), (45.0, 3.0))
_SPIRAL = (-0.15, -0.1125, -0.075, -0.0375, 0.0, 0.0375, 0.075, 0.1125, 0.15, 0.1125, 0.075, 0.0375, 0.0, -0.0375,
           -0.075, -0.1125)  # fmt: skip  # placeholder: the 16-step spiral table
# 8 straight legs, each 7 cells across and 0.5 down or up, so 90 ticks a leg (12 s round) at 7 / 90 a tick; the
# queen's y stays in 3.0..4.0, so her ring (y +-2.5) stays below the boss bar's row
_FIGURE8 = ((30.0, 3.5), (37.0, 3.0), (44.0, 3.5), (37.0, 4.0), (30.0, 3.5), (23.0, 3.0), (16.0, 3.5), (23.0, 4.0))
_GUARDS = (Flight(0, 4, "line", (4,), "MARCH", (("guard", 4),)),)

BOSSES = MappingProxyType(
    {
        "warden": BossRow(
            kind="warden",
            name="WARDEN",
            epithet="the gatekeeper",
            wave=6,
            station=(30.0, 3.0),
            parts=(PartRow("turret", 3, -7.0, 4.0), PartRow("core", 9, 0.0, 3.0), PartRow("turret", 3, 7.0, 4.0)),
            escorts=_GUARDS,  # 4 guards march below at y 6, firing through the wave's director (1 slot)
            phases=(
                BossPhase(
                    "TURRETS",
                    ("turret",),
                    100,
                    "patrol",
                    0.05,
                    (
                        # placeholder telegraph: the spec gives none, and boss attacks have W >= 30
                        Attack("A", "twin", "turret", "pellet", 2, 8, 0.0, 30, 55, "straight"),
                        Attack("B", "fan", "core", "pellet", 5, 0, 0.12, 30, 170, "fan"),
                    ),
                    path=_PATROL,
                ),
                BossPhase(
                    "CORE",
                    ("core",),
                    382,
                    "patrol",
                    0.08,
                    (
                        Attack("B", "fan", "core", "pellet", 7, 0, 0.18, 30, 150, "fan"),
                        Attack(
                            "C", "triple", "core", "dart", 3, 10, 0.0, 24, 130, "aimed", profiles=("normal", "hard")
                        ),
                        Attack("D", "guards", "core", "spawn", 4, 0, 0.0, 0, 0, "spawn", at=0.5, kind="guard"),
                    ),
                    path=_PATROL,
                    dip=(5.0, 120, 600),
                ),
            ),
            variants=(
                Variant("hard", 1, "C", "shot", "hunter"),
                Variant("hard", 1, "C", "aim", "lead"),
                Variant("hard", -1, "B", "count", 9),
            ),
        ),
        "hive_queen": BossRow(
            kind="hive_queen",
            name="HIVE QUEEN",
            epithet="the brood mother",
            wave=12,
            station=(30.0, 3.0),
            parts=(PartRow("queen", 11, 0.0, 3.0),),
            escorts=(Flight(0, 6, "ring", (6,), "ESCORT", (("weaver", 6),)),),
            phases=(
                BossPhase(
                    "BROOD",
                    ("queen",),
                    286,
                    "sway",
                    0.05,  # placeholder speed
                    (
                        # tuned (T round 5, Q2 per spec §27.2): eggs every 60 ticks, not 90 (FRENZY 69 -> 46, still
                        # 1.3x). Staged act II, 100 seeds: the good bot +1.24 hits a game, strong +0.41; the lives
                        # model puts Normal's good bot at about 37%. Easy keeps gentler eggs (variants below)
                        Attack("A", "eggs", "queen", "cluster", 1, 0, 0.0, 24, 60, "straight"),
                        Attack("B", "darts", "queen", "dart", 2, 0, 0.0, 30, 110, "aimed"),  # placeholder telegraph
                        Attack("R", "refill", "queen", "spawn", 6, 0, 0.0, 0, 1200, "spawn", below=3, kind="weaver"),
                    ),
                    path=((24.0, 3.0), (36.0, 3.0)),
                ),
                BossPhase(
                    "SWARM",
                    ("queen",),
                    233,
                    "figure8",
                    0.0778,  # 7 cells a leg in 90 ticks
                    (
                        Attack("C", "spray", "queen", "pellet", 10, 6, 0.15, 30, 240, "spray"),
                        Attack(
                            "S",
                            "divers",
                            "queen",
                            "spawn",
                            2,
                            0,
                            0.0,
                            0,
                            480,
                            "spawn",
                            kind="diver",
                            offset=3.0,
                            profiles=("normal", "hard"),
                        ),
                        Attack("B", "darts", "queen", "dart", 2, 0, 0.0, 30, 140, "aimed"),  # placeholder telegraph
                    ),
                    path=_FIGURE8,
                ),
                BossPhase(
                    "FRENZY",
                    ("queen",),
                    138,
                    "figure8",
                    0.1011,  # the path at 1.3x
                    (  # A, B and C at 1.3x the rate (cooldowns / 1.3); no spawns
                        Attack("A", "eggs", "queen", "cluster", 1, 0, 0.0, 24, 46, "straight"),
                        Attack("B", "darts", "queen", "dart", 2, 0, 0.0, 30, 108, "aimed"),
                        Attack("C", "spray", "queen", "pellet", 10, 6, 0.15, 30, 185, "spray"),
                    ),
                    path=_FIGURE8,
                ),
            ),
            variants=(
                # tuned (T round 5, Q4 Easy): on Easy BROOD's egg is a plain pellet (told by the queen's charge pose,
                # so never a lapse) and FRENZY's eggs keep the old 69. The split pellets were nearly all of the good
                # bot's Easy deaths here: staged act II, 2.54 -> 0.44 hits a game
                Variant("easy", 0, "A", "shot", "pellet"),
                Variant("easy", 2, "A", "cooldown", 69),
                Variant("hard", -1, "C", "aim", "spray_alternate"),
                Variant("hard", 0, "R", "count", 8),
                Variant("hard", -1, "escort", "size", 8),  # the brood has 8: the ring and its refill
                Variant("hard", -1, "escort", "dims", (8,)),
                Variant("hard", -1, "escort", "mix", (("weaver", 8),)),
                # tuned (T round 6, Q1 on Hard per spec §27.2): Hard's FRENZY lays eggs every 69 ticks (round 4's), not
                # Normal's 46, so the densest scene the tables make (round 5's one unavoidable verdict: eggs over the
                # spray and darts) is thinner. Staged act II on Hard, 100 seeds: strong -0.03 hits a game
                Variant("hard", 2, "A", "cooldown", 69),
                # tuned (T round 7, the same): on Hard BROOD lays eggs every 120 ticks, not Normal's 60, and FRENZY's
                # egg is a plain pellet (told by the queen's charge pose). HIVE QUEEN took 40% of the strong bot's
                # Hard lives (act II: 2.37 fatal events a run against a budget of 1.31). Staged act II on Hard, 100
                # seeds: strong -0.79 fatal events a game (BROOD at 120 alone -0.54, at 180 -0.73; FRENZY's pellet
                # alone -0.27; BROOD's egg a pellet -1.41, too much)
                Variant("hard", 0, "A", "cooldown", 120),
                Variant("hard", 2, "A", "shot", "pellet"),
            ),
        ),
        "dreadnought": BossRow(
            kind="dreadnought",
            name="DREADNOUGHT",
            epithet="the siege ship",
            wave=18,
            station=(30.0, 3.0),
            parts=(PartRow("battery", 5, -8.0, 3.0), PartRow("core", 9, 0.0, 3.0), PartRow("battery", 5, 8.0, 3.0)),
            escorts=_GUARDS,  # 4 guards in a line at y 6
            phases=(
                BossPhase(
                    "BROADSIDE",
                    ("battery",),
                    117,
                    "drift",
                    0.03,
                    (
                        Attack("A", "salvo", "battery", "slug", 3, 0, 0.0, 30, 140, "shelters"),
                        Attack("B", "curtain", "core", "pellet", 9, 0, 0.0, 36, 200, "curtain", width=30.0, gap=7.0),
                    ),
                ),
                BossPhase(
                    "BEAM",
                    ("core",),
                    307,
                    "track",
                    0.10,
                    (
                        Attack("C", "beam", "core", "beam", 1, 0, 0.0, 60, 220, "beam", live=40, track=24, width=3.0),
                        Attack("D", "needles", "core", "needle", 2, 0, 0.0, 24, 100, "straight"),
                    ),
                ),
                BossPhase(
                    "RAM",
                    ("core",),
                    175,
                    "ram",
                    0.0139,  # 5 rows in 360 ticks
                    (
                        Attack(
                            "C", "beams", "core", "beam", 2, 0, 0.0, 60, 240, "beam", live=40, width=3.0, gap=14.0
                        ),  # placeholder telegraph: fixed from its mark, locked >= 36 ahead
                        Attack("E", "flak", "core", "pellet", 4, 0, 0.12, 30, 120, "fan"),  # placeholder spread
                    ),
                    path=((30.0, 3.0), (30.0, 8.0)),
                    dip=(8.0, 360, 1080),  # descend over 6 s, hold 6 s, climb back
                ),
            ),
            variants=(
                Variant("easy", 1, "C", "telegraph", 72),  # x1.25: marked 90 ahead, tracks 54, locks 36 ahead
                Variant("easy", 1, "C", "track", 54),
                Variant("easy", 2, "C", "count", 1),
                Variant("hard", 1, "C", "telegraph", 56),  # x0.8: marked 45 ahead, tracks 9, locks 36 ahead
                Variant("hard", 1, "C", "track", 9),
                Variant("hard", 0, "B", "cooldown", 170),
                # tuned (T round 6, Q1 on Hard per spec §27.2): on Hard RAM's flak is 2 clusters in a spread, not 4
                # pellets. The strong bot dies almost only to split pellets it notices late, and DREADNOUGHT had none:
                # staged act III on Hard, 100 seeds, strong +0.62 hits and +0.97 fatal events a game (a cluster in
                # BEAM, where the core tracks the ship, gave +2.5 hits: far too many)
                Variant("hard", 2, "E", "shot", "cluster"),
                Variant("hard", 2, "E", "count", 2),
            ),
        ),
        "mothership": BossRow(
            kind="mothership",
            name="MOTHERSHIP",
            epithet="the armada",
            wave=24,
            station=(30.0, 2.0),
            parts=(
                PartRow("core", 15, 0.0, 2.0),
                PartRow("pod", 5, -11.0, 1.0),  # the pods start 12 cells apart round `orbit` (its length is 48)
                PartRow("pod", 5, 1.0, 1.0),
                PartRow("pod", 5, 11.0, 3.0),
                PartRow("pod", 5, -1.0, 3.0),
            ),
            orbit=((-11.0, -1.0), (11.0, -1.0), (11.0, 1.0), (-11.0, 1.0)),  # placeholder: a carousel round the
            #   core, a row above it and a row below (y 1 and 3 at the station); below it, a pod shields the core
            life=0,
            phases=(
                BossPhase(
                    "ARMADA",
                    (),
                    0,
                    "hold",
                    0.0,
                    (Attack("M", "mites", "core", "spawn", 4, 0, 0.0, 0, 720, "spawn", kind="mite"),),
                    escorts=(  # two flights of 16 (carriers, gunners, lancers), firing through the wave's director
                        Flight(0, 16, "rect", (2, 8), "MARCH", (("carrier", 2), ("gunner", 4), ("lancer", 3))),
                        Flight(900, 16, "rect", (2, 8), "MARCH", (("carrier", 2), ("gunner", 4), ("lancer", 3))),
                    ),  # placeholder mixes
                ),
                BossPhase(
                    "PODS",
                    ("pod",),
                    45,
                    "drift",
                    0.03,  # the core drifts  # placeholder speed
                    (
                        Attack("F", "pod darts", "pod", "hunter", 1, 0, 0.0, 30, 60, "lead"),  # placeholder telegraph
                        Attack(
                            "G", "crossfire", "pod", "dart", 2, 0, 0.0, 30, 150, "converge", profiles=("normal", "hard")
                        ),
                    ),
                    orbit=0.06,  # the pods orbit at 0.06
                ),
                BossPhase(
                    "CORE",
                    ("core",),
                    164,
                    "patrol",
                    0.06,
                    (
                        Attack("C", "beams", "core", "beam", 2, 0, 0.0, 60, 240, "beam", live=40, width=3.0, gap=14.0),
                        Attack("B", "curtain", "core", "pellet", 9, 0, 0.0, 36, 200, "curtain", width=30.0, gap=7.0),
                        Attack("A", "clusters", "core", "cluster", 1, 0, 0.0, 30, 120, "straight"),
                    ),
                    path=_PATROL,
                ),
                BossPhase(
                    "DESPERATION",
                    ("core",),
                    64,
                    "descend",
                    0.0017,  # 1 row every 10 s
                    (
                        Attack("H", "spiral", "core", "pellet", 0, 8, 0.15, 30, 8, "spiral", table=_SPIRAL),
                        Attack("C", "beam", "core", "beam", 1, 0, 0.0, 60, 300, "beam", live=40, width=3.0),
                    ),
                    path=((30.0, 2.0), (30.0, 7.0)),
                ),
            ),
            variants=(
                Variant("easy", 1, "F", "shot", "dart"),
                Variant("easy", 1, "F", "aim", "aimed"),
                Variant("easy", 3, "H", "spacing", 12),
                # tuned (T round 2): Hard's double spiral fires every 12 ticks, not 8 (1.5x Normal's pellets, not 2x).
                # At 8 the two arms filled every spot under the core: the good bot took 8-12 minutes (staged) to
                # 28 minutes (whole runs) to break it, and 8 of 100 Hard runs hit the cap. Now 11-71 s (median 39)
                Variant("hard", 3, "H", "count", 2),
                Variant("hard", 3, "H", "spacing", 12),
                # tuned (T rounds 5 and 6, Q1 on Hard per spec §27.2): CORE's clusters every 60 ticks on Hard, not 120.
                # The strong bot dies almost only to split pellets it notices late, and its act IV on Hard is far under
                # its budget. Round 5 set 80; staged act IV on Hard, 100 seeds, 60 gave strong +0.48 hits a game over
                # 80 (45 gave no more, and a pair every 80 fewer)
                Variant("hard", 2, "A", "cooldown", 60),
                # tuned (T round 6, the same): on Hard PODS' pod darts are aimed clusters. Staged act IV on Hard,
                # strong +0.89 hits and +0.97 fatal events a game
                Variant("hard", 1, "F", "shot", "cluster"),
                Variant("hard", 0, "escort", "size", 20),  # phase 1's two flights have 20
                Variant("hard", 0, "escort", "dims", (2, 10)),
            ),
        ),
    }
)

# --- pickups, upgrades, the bomb, combo and score (§10) --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PickupKind:
    kind: str  # model.PICKUPS
    letter: str  # drawn on the falling pickup
    weight: int  # draw weight among the six
    ticks: int  # timed kinds: duration; shield: the most it lasts (it ends at its last hit); 0: instant
    factor: float = 1.0  # rapid: player shot speed; slow: every enemy clock, shot, dive, boss and the UFO
    hits: int = 0  # shield: hits absorbed (DEFLECTOR makes it UPGRADES["DEFLECTOR"].value)
    amount: int = 0  # repair: hp added to every cell (destroyed cells rebuilt at this); bomb: bombs added


PICKUPS = MappingProxyType(
    {
        "rapid": PickupKind("rapid", "R", 24, 480, factor=1.5),
        "spread": PickupKind("spread", "S", 20, 480),
        "shield": PickupKind("shield", "+", 16, 600, hits=1),
        "slow": PickupKind("slow", "T", 18, 480, factor=0.6),
        "repair": PickupKind("repair", "L", 14, 0, amount=1),
        "bomb": PickupKind("bomb", "B", 8, 0, amount=1),
    }
)


@dataclass(frozen=True, slots=True)
class PickupRules:
    fall: float  # cells a tick
    catch: tuple[float, float]  # caught within |dx| < a, |dy| < b
    add: int  # a second pickup of a running kind adds this many ticks...
    most: int  # ...up to this
    phase_break: int  # pickups dropped at a boss phase break
    boss_kill: int  # pickups dropped at a boss kill


PICKUP = PickupRules(fall=0.045, catch=(2.0, 0.65), add=480, most=720, phase_break=1, boss_kill=2)


@dataclass(frozen=True, slots=True)
class Upgrade:
    name: str  # the key, shown on the card and in "TAKEN: name"
    code: str  # shown on the HUD (stacks as a digit: RR2) and used by the bots' fixed:<code> policy
    family: str  # offense, defense, utility: one card each
    stacks: int  # most times it can be taken
    weight: int  # draw weight within its family
    priority: tuple[int, ...]  # the bots' priority rank for each stack (lower first)
    value: float  # the effect's size: see each row
    extra: float = 0.0  # a second size, where the effect has two
    blurb: tuple[str, str] = ("", "")  # the card's two lines (presentation; U may reword; at most 13 characters)


UPGRADES = MappingProxyType(
    {
        # slots in flight
        "TRIPLE SHOT": Upgrade("TRIPLE SHOT", "TS", "offense", 1, 14, (1,), 3, blurb=("3 shots in", "flight")),
        # shot speed added per stack
        "RAIL ROUNDS": Upgrade("RAIL ROUNDS", "RR", "offense", 2, 12, (2, 6), 0.11, blurb=("faster shots", "")),
        # extra targets a killing shot flies on through
        "PIERCER": Upgrade("PIERCER", "PR", "offense", 1, 8, (5,), 1, blurb=("kills fly on", "through one")),
        # value: hits a SHIELD pickup absorbs; extra: the guard given now and at each act start
        "DEFLECTOR": Upgrade("DEFLECTOR", "DF", "defense", 1, 10, (3,), 2, 1, blurb=("a guard every", "act")),
        # ship speed added per stack
        "THRUSTERS": Upgrade("THRUSTERS", "TH", "defense", 2, 10, (4, 10), 0.06, blurb=("ship moves", "faster")),
        # shelter hp added now, and cap added, per stack
        "MASONRY": Upgrade("MASONRY", "MA", "defense", 2, 8, (9, 15), 1, blurb=("sturdier", "shelters")),
        # telegraph factor (the floor still applies)
        "SCANNER": Upgrade("SCANNER", "SC", "defense", 1, 6, (14,), 1.25, blurb=("longer", "warnings")),
        # value: bomb cap; extra: bombs that can be added per act (and +1 bomb now)
        "BOMB BAY": Upgrade("BOMB BAY", "BB", "utility", 1, 8, (7,), 3, 2, blurb=("carry 3 bombs", "+1 now")),
        # charge that makes a bomb
        "CAPACITOR": Upgrade("CAPACITOR", "CP", "utility", 1, 8, (8,), 600, blurb=("bombs charge", "faster")),
        # value: catch |dx|; extra: pickups fall at this factor
        "MAGNET": Upgrade("MAGNET", "MG", "utility", 1, 6, (11,), 3.5, 0.7, blurb=("catch pickups", "from farther")),
        # chain kills a miss takes off (instead of breaking it)
        "CHAIN LINK": Upgrade("CHAIN LINK", "CL", "utility", 1, 6, (12,), 3, blurb=("a miss dents", "the combo")),
        # value: ticks a timed pickup gives; extra: the most it can reach
        "LONG FUSE": Upgrade("LONG FUSE", "LF", "utility", 1, 5, (13,), 600, 900, blurb=("power-ups", "last longer")),
    }
)
FAMILIES = ("offense", "defense", "utility")  # the card's order


@dataclass(frozen=True, slots=True)
class BombRules:
    cap: int  # most bombs held (BOMB BAY: its value)
    charge: int  # charge that adds a bomb (CAPACITOR: its value); each shot kill adds its combo multiplier
    per_act: int  # bombs added per act by the charge and BOMB pickups together (BOMB BAY: its extra)
    lock: int  # ticks of no enemy fire
    invulnerable: int  # ticks the ship can't be hit
    radius: float  # the shockwave hits enemies within this of the ship...
    min_y: float  # ...at or below this row, once, for `damage`; bosses take none
    damage: int
    shot_points: int  # per enemy shot cleared (no combo)
    full_points: int  # x act: a BOMB pickup that can't add a bomb pays this
    last_stand: int  # bombs Last Stand adds (outside the act's limit, within the cap)
    first_act: int = 1  # the first act whose charge and BOMB pickups add bombs (earlier, `per_act` is 0)


# B-F's decision (open question 9, step 1): charge 800 -> 1,200 and no bomb earned in act I (first_act 2)
BOMB = BombRules(
    cap=2, charge=1200, per_act=1, lock=60, invulnerable=45, radius=5.0, min_y=11.0, damage=1, shot_points=10,
    full_points=500, last_stand=1, first_act=2,
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class ComboRules:
    step: int  # combo = min(most, 1 + (chain - 1) // step)
    most: int


COMBO = ComboRules(step=3, most=6)


@dataclass(frozen=True, slots=True)
class Scoring:
    """Points; every row but `veteran` is multiplied by the act. Enemy points are in ENEMIES."""

    veteran: float  # a veteran's points factor
    clear: int  # wave clear
    perfect: int  # no hit taken in the wave
    sweep: int  # a flight destroyed within ARRIVAL.sweep ticks of becoming targetable
    part: int  # a boss part broken
    boss: int  # a boss destroyed
    boss_perfect: int  # no hit taken in the boss wave


SCORING = Scoring(veteran=1.5, clear=500, perfect=1000, sweep=200, part=500, boss=5000, boss_perfect=5000)


@dataclass(frozen=True, slots=True)
class UfoRules:
    first: int  # first wave; regular waves only, at most once a wave
    delay: int  # play ticks before it may appear...
    jitter: int  # ...plus rng(0..jitter)
    min_enemies: int  # only while at least this many enemies stand
    y: float
    speed: float
    high: int  # points when the wave's shot count at the kill is `at` more than a multiple of `every`
    low: int
    every: int
    at: int
    box: tuple[float, float]  # placeholder: today's 2.0 x 0.45


UFO = UfoRules(
    first=2, delay=1200, jitter=600, min_enemies=8, y=1.0, speed=0.12, high=300, low=150, every=15, at=8,
    box=(2.0, 0.45),
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class ShelterRules:
    """Shelter geometry (§7.4); how many, where, their cap and their repair are in PROFILES."""

    top: int  # the top row of every shelter
    cells: tuple[tuple[int, int], ...]  # (dx, dy) from (centre, top): today's 16-cell shape
    crush_y: float  # enemies at or below this row crush the cells within `crush`
    crush: tuple[float, float]


SHELTERS = ShelterRules(
    top=14,
    cells=(
        (-2, 0), (-1, 0), (0, 0), (1, 0), (2, 0),
        (-3, 1), (-2, 1), (-1, 1), (0, 1), (1, 1), (2, 1), (3, 1),
        (-3, 2), (-2, 2), (2, 2), (3, 2),
    ),
    crush_y=13.0,
    crush=(1.5, 0.7),
)  # fmt: skip

# --- phases and timings (§14.1) --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Timing:
    intro: int  # the wave intro (`wave`); wave 1 also shows act I's title
    act_intro: int  # instead of the wave intro at waves 7, 13, 19
    boss_intro: int  # instead of the wave intro in boss waves
    restart: int  # the intro after a continue
    clear: int  # the clear panel
    arm: int  # ticks before the card or the continue prompt takes input
    prompt: int  # the continue prompt's countdown
    freeze: int  # the hit freeze, inside playing
    safe: int  # invulnerable after the mercy clear
    lockout: int  # no enemy fire after the mercy clear
    phase_break: int  # a boss phase break, inside playing
    absorb: int  # invulnerable after a shield or guard absorbs a hit
    last_stand: int  # RAPID ticks Last Stand gives
    notice: int  # a bottom-row notice
    boss_dying: int = 60  # a destroyed boss's last ticks inside playing (frozen like a phase break), then the clear


TIMING = Timing(
    intro=90, act_intro=180, boss_intro=150, restart=90, clear=90, arm=30, prompt=600, freeze=60, safe=120,
    lockout=60, phase_break=90, absorb=45, last_stand=300, notice=180,
)  # fmt: skip

# --- difficulty (§11) ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Profile:
    name: str  # a key of model.DIFFICULTIES
    line: str  # the menu's line (at most 52 characters)
    lives: int
    cap: int  # most lives
    last_stand: bool  # once per run
    continues: int  # per run (never in Endless)
    training: tuple[int, ...]  # waves that start with the training shield
    bombs: int  # at the start
    shelters: tuple[int, ...]  # each shelter's centre x
    shelter_cap: int  # most hp a cell holds
    shelter_wave: str  # at a wave start that isn't an act start: rebuild, patch (+1 per surviving cell) or none
    speed: float  # enemy shot speed factor: Game.difficulty, equal to model.DIFFICULTIES[name][1]
    aim: str  # tell: darts aim where the ship was when the tell began; fire: where it is when fired
    hunters: int  # first wave gunners fire hunter darts; 0 never
    lead: float  # hunter lead, x the ship's last-tick vx x the flight time
    burst: int  # added to every director row's burst weight
    crossfire: int  # added to every director row's crossfire weight
    no_crossfire: bool  # crossfire never drawn
    telegraph: float  # factor of every telegraph (then the floor)
    reaction: float  # factor of every director row's reaction share
    divers: int  # added to ACTS[...].divers (at least 1), from act `divers_from`
    divers_from: int
    dive_every: float  # factor of ACTS[...].dive_every
    dive_vy: float  # dive speed
    veteran: tuple[tuple[int, int, float], ...]  # (first wave, last wave, share of enemies plated)
    drop: float  # base drop chance per shot kill
    pressure: float  # divides every reload and boss cooldown; kept in LIMITS.pressure
    gunner_every: int  # every n-th gunner of a flight (slot order) is a scout instead; 0 never
    mites: int  # mites a splitter releases
    front_gunners: tuple[int, int]  # (from act, up to n front-row scouts per flight become gunners); (0, 0) never
    back_lancers: tuple[int, int]  # (from act, n back-row scouts per flight become lancers); (0, 0) never
    bosses: str  # the boss variants it uses (BOSSES[...].variants by profile)


PROFILES = MappingProxyType(
    {
        "easy": Profile(
            name="easy",
            line="Gentler fire, sturdy cover, one continue",
            lives=3,
            cap=5,
            last_stand=True,
            continues=1,
            training=(1, 2, 3),
            bombs=2,
            shelters=(10, 23, 36, 49),
            shelter_cap=3,
            shelter_wave="rebuild",
            speed=0.92,
            aim="tell",
            hunters=0,
            lead=0.5,
            burst=0,
            crossfire=0,
            no_crossfire=True,
            telegraph=1.25,
            reaction=0.5,
            divers=-1,
            divers_from=1,
            dive_every=1.2,
            dive_vy=0.18,
            veteran=((21, 23, 0.2),),
            drop=0.030,
            # tuned (T round 6, Q4 Easy, §19.3 step 7): 1.0 -> 1.05. The good bot's clean wins on Easy were 84% (band
            # 70-85%); staged act IV on Easy, 100 seeds, 1.1 gave it +0.95 hits a game. The lives model puts 1.05 at
            # about 78%
            pressure=1.05,
            gunner_every=2,
            mites=1,
            front_gunners=(0, 0),
            back_lancers=(0, 0),
            bosses="easy",
        ),
        "normal": Profile(
            name="normal",
            line="Aimed fire and smart volleys: the intended game",
            lives=3,
            cap=5,
            last_stand=True,
            continues=0,
            training=(1,),
            bombs=1,
            shelters=(10, 23, 36, 49),
            shelter_cap=3,
            shelter_wave="patch",
            speed=1.0,
            aim="fire",
            hunters=16,
            lead=0.5,
            burst=0,
            crossfire=0,
            no_crossfire=False,
            telegraph=1.0,
            reaction=1.0,
            divers=0,
            divers_from=1,
            dive_every=1.0,
            dive_vy=0.20,
            veteran=((21, 23, 0.2),),
            # tuned (T round 3, Q9): 0.025 -> 0.022; the longer bosses brought strong's uptime to about 28% (staged),
            # on the 28% margin; 0.020 measured 25.1%
            drop=0.022,
            pressure=1.0,
            gunner_every=0,
            mites=2,
            front_gunners=(0, 0),
            back_lancers=(0, 0),
            bosses="normal",
        ),
        "hard": Profile(
            name="hard",
            line="Leading shots, armoured fleets, thin cover",
            lives=3,
            cap=4,
            last_stand=False,
            continues=0,
            training=(),
            bombs=1,
            shelters=(13, 30, 47),
            shelter_cap=2,
            shelter_wave="none",
            speed=1.08,
            aim="fire",
            hunters=5,
            lead=0.75,
            burst=10,
            crossfire=10,
            no_crossfire=False,
            telegraph=0.8,
            reaction=1.25,
            divers=1,
            divers_from=2,
            dive_every=0.85,
            dive_vy=0.22,
            veteran=((7, 18, 0.1), (19, 24, 0.2)),
            drop=0.020,
            pressure=1.0,
            gunner_every=0,
            mites=2,
            front_gunners=(2, 2),
            back_lancers=(3, 1),
            bosses="hard",
        ),
    }
)

# --- Endless (§12) ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Modifier:
    name: str  # shown in the intro (NEW: name), the pause panel and as a count on the bottom row
    stat: str  # what it changes: reload, veteran, slots, divers, speed, flights, fuse, reaction
    mul: float = 1.0  # factor per stack
    add: float = 0.0  # added per stack
    cap: float = 0.0  # the bound the stacked value never passes (reload: its floor)
    ticks: int = 0  # flights: the repeated flight's delay; fuse: timed pickups' ticks


@dataclass(frozen=True, slots=True)
class EndlessRules:
    loop: tuple[int, int]  # after wave 24, the script loops these waves (acts II-IV) as loop 2, 3, ...
    first: int  # the wave the first modifier starts at...
    step: int  # ...and one more every this many waves, in ladder order, repeating
    ladder: tuple[Modifier, ...]
    loop_pressure: float  # multiplies pressure from loop 2 (kept in 0.8-1.1); the Campaign never reads it
    loop_life: int  # lives added at each loop's start, up to the cap (0: off)
    card_every: int  # an upgrade card after every this many waves
    fillers: tuple[str, str]  # what fills an empty card position: REFIT, then BOUNTY
    refit: int  # REFIT: bombs added (within the cap, outside the act's limit), and shelters rebuilt
    bounty: int  # BOUNTY: points x act
    mark: str  # boss names from loop 2 gain f"{mark} {roman(loop)}": HIVE QUEEN MK II


ENDLESS = EndlessRules(
    loop=(7, 24),
    # tuned (T round 8, Q7 on Hard per spec §27.2): the ladder starts with loop 2 (wave 25, not 31) and stacks one
    # more modifier every wave (not every 3), so loop 2's late waves and MOTHERSHIP MK II meet 10-18 of them. With
    # 31 / 3 the strong bot's Hard Endless median was wave 60 (band 35-50). Tune tier's 100 Endless seeds: 25 / 2
    # gave 56.5, 25 / 1 gave 42 (27 runs ended by wave 36, 64 by 42). loop_pressure 1.1 moved nothing (62 seeds)
    first=25,
    step=1,
    ladder=(
        Modifier("QUICK RELOAD", "reload", mul=0.92, cap=20),
        Modifier("ARMOURED", "veteran", add=0.10, cap=0.50),
        Modifier("EXTRA SLOT", "slots", add=1, cap=8),
        Modifier("DIVE STORM", "divers", mul=0.9, add=1, cap=5),  # +1 diver, dive interval x0.9; at most 5 divers
        Modifier("FAST SHOTS", "speed", mul=1.05, cap=1.25),  # the safe cap and the floors still apply
        Modifier("REINFORCED", "flights", add=1, cap=3, ticks=480),  # each regular wave's last flight repeated 8 s on
        Modifier("SHORT FUSE", "fuse", cap=360, ticks=360),
        Modifier("SHARP EYES", "reaction", add=0.02, cap=0.15),
    ),
    loop_pressure=1.0,
    loop_life=0,
    card_every=3,
    fillers=("REFIT", "BOUNTY"),
    refit=1,
    bounty=5000,
    mark="MK",
)

# --- determinism and caps (§2.2, §2.3, §4) ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Salts:
    """Each random stream is Rng(seed ^ salt); validate.py pins these three values."""

    rng: int  # the director: shooters, shot kinds, divers, reaction draws, curtain gaps, UFO timing
    drop: int  # exactly one draw per shot kill, used or not, plus the cargo choice
    offer: int  # the upgrade offers


SALTS = Salts(rng=0x243F6A8885A308D3, drop=0x13198A2E03707344, offer=0xA4093822299F31D0)


@dataclass(frozen=True, slots=True)
class Limits:
    """Every cap and floor. Floors and caps apply after every multiplier (F8)."""

    enemies: int  # alive at once, mites and escorts included
    enemy_shots: int  # MAX_ENEMY_SHOTS: the director and boss attacks refuse beyond it
    player_shots: int
    hazards: int
    pickups: int
    effects: int
    shelter_cells: int
    slots: int  # director slots
    reload: int  # reload_eff floor
    beat: int  # MARCH beat floor
    vy: float  # enemy shot speed cap
    warning: int  # F1: W floor
    flight: int  # F1: flight-time floor
    telegraph: int  # F3: telegraph floor
    dive_warning: int  # a dive's W floor
    mite_warning: int  # a mite's W floor
    safe_warning: int  # a safe volley is slowed to this W
    fire_y: float  # F2: no shooter fires from below this row
    spawn_y: float  # F2: no enemy shot spawns below this row
    hit_y: float  # W = telegraph + ceil((hit_y - y_spawn) / vy)
    dive_y: float  # a dive's W = wobble + ceil((dive_y - y) / vy)
    beam_lock: int  # a beam's x locks at least this many ticks before it goes live
    lane: float  # F4: every beam leaves a safe lane at least this wide
    gap: float  # F4: every curtain gap at least this wide...
    gap_x: tuple[float, float]  # ...inside these columns...
    gap_reach: float  # ...with its centre within this of the ship
    bucket: int  # shooters are the front enemy of each bucket of this many columns
    focus_reach: float  # focus draws from the buckets within this of the ship
    shape_height: int  # every formation shape at most this tall...
    shape_width: int  # ...and this wide (slot centres)
    field: tuple[float, float]  # ...inside these columns
    pressure: tuple[float, float]  # a profile's pressure stays inside this band (the tuning rule of §11)
    boss_spawn: float = 1.0  # F2: a boss shot spawns this far below its source part's row, outside every part's box


# B-F's decision (open question 9, step 2): enemy_shots 24 -> 32 and the reload floor 20 -> 16; slots stay 8
LIMITS = Limits(
    enemies=72, enemy_shots=32, player_shots=9, hazards=6, pickups=8, effects=24, shelter_cells=64, slots=8, reload=16,
    beat=4, vy=0.28, warning=30, flight=18, telegraph=18, dive_warning=70, mite_warning=60, safe_warning=60,
    fire_y=12.5, spawn_y=13.2, hit_y=17.55, dive_y=17.3, beam_lock=36, lane=6.0, gap=7.0, gap_x=(4.0, 55.0),
    gap_reach=10.0, bucket=3, focus_reach=6.0, shape_height=6, shape_width=52, field=(2.0, 57.0), pressure=(0.8, 1.25),
)  # fmt: skip
