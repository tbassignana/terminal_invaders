"""Small, serializable values shared by the simulation and terminal renderer."""

from dataclasses import dataclass

WIDTH, HEIGHT, TICK_RATE = 60, 20, 60
CAMPAIGN_WAVES = 12
MAX_SHOTS, MAX_EFFECTS, MAX_PICKUPS = 160, 24, 8
ACTIONS = frozenset({"left", "right", "fire", "bomb", "autofire"})
DIFFICULTIES = {"easy": (4, 0.78), "normal": (3, 1.0), "hard": (2, 1.25)}
MODES = ("classic", "endless", "daily")
PICKUPS = ("rapid", "spread", "shield", "slow", "repair")


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
    weapon: int = 1
    shield: int = 0
    invulnerable: int = 0


@dataclass(slots=True)
class Alien:
    x: float
    y: float
    kind: str = "scout"
    hp: int = 1
    diving: bool = False
    vx: float = 0.0


@dataclass(slots=True)
class Shot:
    x: float
    y: float
    friendly: bool = True
    vx: float = 0.0
    vy: float = -0.55
    damage: int = 1


@dataclass(slots=True)
class Pickup:
    x: float
    y: float
    kind: str


@dataclass(slots=True)
class Boss:
    x: float
    y: float
    hp: int
    max_hp: int
    kind: str = "mothership"
    vx: float = 0.075


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
