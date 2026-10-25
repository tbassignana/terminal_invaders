"""Small, serializable values shared by the simulation and terminal renderer."""

from dataclasses import dataclass, field

WIDTH, HEIGHT, TICK_RATE = 60, 20, 60
RULESET = "campaign-2"  # campaign/__init__.py re-exports it; bump on any gameplay change
CAMPAIGN_WAVES = 24
MAX_SHOTS, MAX_EFFECTS, MAX_PICKUPS = 160, 24, 8
ACTIONS = frozenset({"left", "right", "fire", "bomb", "autofire"})  # unchanged: Arcade tapes and keymaps hold autofire
DIFFICULTIES = {"easy": (3, 0.92), "normal": (3, 1.0), "hard": (3, 1.08)}  # (lives, shot speed); keys unchanged
MODES = ("classic", "endless", "daily", "arcade")  # unchanged; "classic" is shown as CAMPAIGN
PICKUPS = ("rapid", "spread", "shield", "slow", "repair", "bomb")
PHASES = ("wave", "playing", "clear", "upgrade", "continue", "game_over", "victory")
POSES = ("entering", "formation", "telegraph", "diving", "returning")  # Alien.state values


@dataclass(frozen=True, slots=True)
class Config:
    difficulty: str = "normal"
    mode: str = "classic"
    seed: int = 0

    def __post_init__(self):
        if self.difficulty not in DIFFICULTIES:
            raise ValueError(f"Unknown difficulty: {self.difficulty}")
        if self.mode not in MODES:
            raise ValueError(f"Unknown mode: {self.mode}")
        if type(self.seed) is not int:
            raise ValueError("Seed must be an integer")


@dataclass(slots=True)
class Player:
    x: float = WIDTH / 2
    y: float = HEIGHT - 2
    lives: int = 3
    bombs: int = 1
    weapon: int = 1  # shot slots: the Campaign sets 2, TRIPLE SHOT 3 (the HUD's GUN)
    shield: int = 0  # ticks left on a SHIELD pickup
    invulnerable: int = 0
    guard: int = 0  # untimed one-hit shields (training shield, DEFLECTOR); drawn as the shielded ship
    shield_hits: int = 0  # hits the SHIELD pickup still absorbs (1; 2 with DEFLECTOR)
    vx: float = 0.0  # last tick's movement, for the lead of hunter darts


@dataclass(slots=True)
class Alien:
    x: float
    y: float
    kind: str = "scout"
    hp: int = 1
    diving: bool = False  # True exactly when state == "diving"
    vx: float = 0.0
    id: int = 0
    state: str = "formation"  # one of POSES
    flight: int = -1  # index of its flight in the wave; -1 for mites and escorts
    home_x: float = 0.0  # its slot, moved by the formation
    home_y: float = 0.0
    max_hp: int = 1
    veteran: bool = False
    cargo: bool = False  # always drops a pickup when shot
    timer: int = 0  # ticks left in the current state (telegraph, return)


@dataclass(slots=True)
class Shot:
    x: float
    y: float
    friendly: bool = True
    vx: float = 0.0
    vy: float = -0.55
    damage: int = 1
    id: int = 0
    kind: str = "player"  # player, pellet, dart, hunter, needle, slug, cluster
    volley: int = 0  # player shots: the slot a volley holds (a SPREAD's three share it)


@dataclass(slots=True)
class Pickup:
    x: float
    y: float
    kind: str
    id: int = 0


@dataclass(slots=True)
class Part:
    """One boss part, one model row tall; its art key is f"{boss.kind}.{name}"."""

    id: int
    name: str  # "turret", "core", "pod", "battery", "queen"
    x: float
    y: float
    w: int  # width in cells: the hit box is +-w/2 x +-0.45
    hp: int
    max_hp: int
    state: str = "shielded"  # shielded, open, charge (telegraphing), broken


@dataclass(slots=True)
class Boss:
    x: float
    y: float
    hp: int  # hp and max_hp sum the parts
    max_hp: int
    kind: str = "mothership"
    vx: float = 0.075
    parts: list[Part] = field(default_factory=list)
    phase: int = 0  # 0-based index into its phases
    phases: int = 1
    state: str = "intro"  # intro, fighting, break, dying
    timer: int = 0


@dataclass(slots=True)
class Hazard:
    """Beams and their warnings, curtain-gap and safe-lane marks."""

    id: int
    kind: str  # beam, gap, lane
    x0: float
    x1: float
    y0: float
    y1: float
    # warn (telegraph role; a tracking beam may still move), lock (its x is final), or live (drawn as enemy fire;
    # beams only). Only the DREADNOUGHT's phase-2 beam tracks.
    state: str = "warn"
    ttl: int = 0


@dataclass(slots=True)
class UFO:
    x: float
    y: float = 1.0
    vx: float = 0.12


@dataclass(slots=True)
class Effect:
    x: float
    y: float
    text: str
    ttl: int = 30
