#!/usr/bin/env python3
"""
Space Invaders Clone - Terminal Edition
A production-grade, curses-based Space Invaders game for macOS.

Features:
- Full state machine (MENU, PLAYING, LEVEL_TRANSITION, GAME_OVER)
- Non-blocking audio with looping soundtrack
- 60 FPS gameplay
- Animated aliens with color cycling
- Erosion-based bunker system
- Frenzy mode (increased firing as aliens decrease)

Author: Claude AI
License: MIT
"""

__version__ = "0.1.0"

import argparse
import atexit
import curses
import json
import logging
import math
import os
import random
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

# ============================================================================
# LOGGING
# ============================================================================

logger = logging.getLogger("invaders")


def setup_logging(debug: bool = False) -> None:
    """Configure the logging framework.

    Args:
        debug: If True, enable DEBUG-level logging to invaders.log.
    """
    if debug:
        handler = logging.FileHandler("invaders.log", mode="w")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    else:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.WARNING)


# Global audio manager reference for cleanup on crash/exit
_audio_manager: Optional["AudioManager"] = None


def _cleanup_audio():
    """Global cleanup function to stop audio on any exit."""
    global _audio_manager
    if _audio_manager:
        _audio_manager.stop()
    # Also kill any stray afplay processes as a safety net
    try:
        subprocess.run(["pkill", "-9", "afplay"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        logger.debug("Failed to kill stray afplay processes during cleanup", exc_info=True)


def _signal_handler(signum, frame):
    """Handle signals to ensure clean shutdown."""
    _cleanup_audio()
    sys.exit(0)


# Register cleanup handlers
atexit.register(_cleanup_audio)
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# ============================================================================
# CONFIGURATION
# ============================================================================


@dataclass(frozen=True)
class GameConfig:
    """Frozen configuration dataclass holding all game constants."""

    # Player
    player_start_lives: int = 5
    max_lives: int = 9

    # Timing
    target_fps: int = 60
    input_timeout_ms: int = 8

    # Screen dimensions (minimums; actual size comes from terminal)
    min_width: int = 60
    min_height: int = 24

    # Alien grid
    alien_rows: int = 5
    alien_cols: int = 11
    alien_spacing_x: int = 4
    alien_spacing_y: int = 2
    alien_start_y: int = 3

    # Movement speeds
    player_speed: int = 1
    alien_move_interval: float = 0.5
    player_projectile_speed: float = 1.0
    alien_projectile_speed: float = 0.4

    # Firing
    base_fire_probability: float = 0.00133
    max_fire_probability: float = 0.0133

    # Visual characters
    player_char: str = "^A^"
    projectile_player: str = "|"
    projectile_alien: str = "!"

    @property
    def frame_time(self) -> float:
        return 1.0 / self.target_fps


# Default config instance used for module-level backward-compatible constants
DEFAULT_CONFIG = GameConfig()

# Difficulty presets — each overrides specific GameConfig fields
DIFFICULTY_PRESETS = {
    "easy": {
        "player_start_lives": 7,
        "alien_move_interval": 0.7,
        "base_fire_probability": 0.001,
        "max_fire_probability": 0.01,
        "player_projectile_speed": 1.2,
        "alien_projectile_speed": 0.3,
    },
    "normal": {},  # Use all defaults
    "hard": {
        "player_start_lives": 3,
        "alien_move_interval": 0.35,
        "base_fire_probability": 0.002,
        "max_fire_probability": 0.02,
        "player_projectile_speed": 0.8,
        "alien_projectile_speed": 0.6,
    },
}


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(description="Terminal Invaders — Space Invaders in your terminal")
    parser.add_argument("--no-sound", action="store_true", help="Disable sound effects")
    parser.add_argument("--no-music", action="store_true", help="Disable background music")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging to invaders.log")
    parser.add_argument(
        "--difficulty", choices=["easy", "normal", "hard"], default="normal", help="Difficulty preset (default: normal)"
    )
    parser.add_argument("--fps", type=int, default=None, help="Target FPS (default: 60)")
    parser.add_argument("--show-fps", action="store_true", help="Display FPS counter during gameplay")
    parser.add_argument("--version", action="version", version=f"Terminal Invaders {__version__}")
    return parser


def config_from_args(args: argparse.Namespace) -> GameConfig:
    """Build a GameConfig from parsed CLI arguments."""
    overrides = dict(DIFFICULTY_PRESETS.get(args.difficulty, {}))
    if args.fps is not None:
        overrides["target_fps"] = args.fps
    return GameConfig(**overrides)


# Legacy module-level constants (kept for backward compatibility)
PLAYER_START_LIVES = DEFAULT_CONFIG.player_start_lives
MAX_LIVES = DEFAULT_CONFIG.max_lives
TARGET_FPS = DEFAULT_CONFIG.target_fps
FRAME_TIME = DEFAULT_CONFIG.frame_time
MIN_WIDTH = DEFAULT_CONFIG.min_width
MIN_HEIGHT = DEFAULT_CONFIG.min_height
ALIEN_ROWS = DEFAULT_CONFIG.alien_rows
ALIEN_COLS = DEFAULT_CONFIG.alien_cols
ALIEN_SPACING_X = DEFAULT_CONFIG.alien_spacing_x
ALIEN_SPACING_Y = DEFAULT_CONFIG.alien_spacing_y
ALIEN_START_Y = DEFAULT_CONFIG.alien_start_y
PLAYER_SPEED = DEFAULT_CONFIG.player_speed
ALIEN_MOVE_INTERVAL = DEFAULT_CONFIG.alien_move_interval
PLAYER_PROJECTILE_SPEED = DEFAULT_CONFIG.player_projectile_speed
ALIEN_PROJECTILE_SPEED = DEFAULT_CONFIG.alien_projectile_speed
INPUT_TIMEOUT_MS = DEFAULT_CONFIG.input_timeout_ms
BASE_FIRE_PROBABILITY = DEFAULT_CONFIG.base_fire_probability
MAX_FIRE_PROBABILITY = DEFAULT_CONFIG.max_fire_probability
PLAYER_CHAR = DEFAULT_CONFIG.player_char
PROJECTILE_PLAYER = DEFAULT_CONFIG.projectile_player
PROJECTILE_ALIEN = DEFAULT_CONFIG.projectile_alien

# Characters not in config (lists/tuples can't be in frozen dataclass easily)
ALIEN_CHARS = [
    ["/-\\", "\\-/"],  # Type 1 animation frames
    ["<O>", "<o>"],  # Type 2 animation frames
    ["/M\\", "\\W/"],  # Type 3 animation frames
]
BUNKER_CHARS = ["O", "o", "."]  # Erosion states: full, damaged, nearly destroyed

# Color pairs
COLOR_PLAYER = 1
COLOR_ALIEN = 2
COLOR_BUNKER = 3
COLOR_TEXT = 4
COLOR_PROJECTILE = 5
COLOR_GAME_OVER = 6
COLOR_BUNKER_DAMAGED = 7   # Yellow for damaged bunkers
COLOR_BUNKER_CRITICAL = 8  # Red for critical bunkers
COLOR_ALIEN_TYPE_0 = 9     # Cyan for top-row aliens
COLOR_ALIEN_TYPE_1 = 10    # Magenta for middle-row aliens
COLOR_ALIEN_TYPE_2 = 11    # Yellow for bottom-row aliens
COLOR_BORDER = 12          # Dim cyan for HUD border
COLOR_POWERUP = 13         # Bright white for power-up pickups
COLOR_BUNKER_HIGH = 14     # Bright green (high health, between full and damaged)
COLOR_BUNKER_LOW = 15      # Dark red/yellow (low health, between damaged and critical)
COLOR_BG_DANGER_LOW = 16   # White-on-red background (mild danger)
COLOR_BG_DANGER_HIGH = 17  # White-on-red background (high danger)

# Box-drawing characters for HUD border
BORDER_H = "─"   # Horizontal line
BORDER_V = "│"   # Vertical line
BORDER_TL = "┌"  # Top-left corner
BORDER_TR = "┐"  # Top-right corner
BORDER_BL = "└"  # Bottom-left corner
BORDER_BR = "┘"  # Bottom-right corner

# Map alien type (0, 1, 2) to its color pair
ALIEN_TYPE_COLORS = {
    0: COLOR_ALIEN_TYPE_0,
    1: COLOR_ALIEN_TYPE_1,
    2: COLOR_ALIEN_TYPE_2,
}

# Type-specific explosion configs: (count_min, count_max, chars, speed_range, lifetime_range)
# Type 0 (top rows, rare) = big dramatic explosion
# Type 1 (middle rows) = medium explosion
# Type 2 (bottom rows, common) = small quick explosion
ALIEN_EXPLOSION_CONFIGS = {
    0: {"count": (8, 12), "chars": "#*+X'`", "speed": (3.0, 8.0), "lifetime": (0.4, 0.7)},
    1: {"count": (5, 8), "chars": "*+.'`", "speed": (2.0, 6.0), "lifetime": (0.3, 0.5)},
    2: {"count": (3, 5), "chars": "+'.", "speed": (1.5, 4.0), "lifetime": (0.2, 0.4)},
}

DEFAULT_EXPLOSION_CONFIG = {
    "count": (5, 8),
    "chars": "*+.'`",
    "speed": (2.0, 6.0),
    "lifetime": (0.3, 0.5),
}


def get_explosion_config(alien_type: int) -> dict:
    """Return the explosion particle config for a given alien type."""
    return ALIEN_EXPLOSION_CONFIGS.get(alien_type, DEFAULT_EXPLOSION_CONFIG)


# ============================================================================
# ALIEN FORMATION PATTERNS
# ============================================================================
# Each pattern is a function(rows, cols) -> set of (row, col) positions to include.
# Level 1 always uses "rectangle". Patterns cycle starting from level 2.

FORMATION_NAMES = ["rectangle", "v_shape", "diamond", "wave"]


def _formation_rectangle(rows: int, cols: int) -> set:
    """Full rectangular grid (classic)."""
    return {(r, c) for r in range(rows) for c in range(cols)}


def _formation_v_shape(rows: int, cols: int) -> set:
    """V-shape: widens from top center to bottom edges."""
    positions = set()
    mid = cols // 2
    for r in range(rows):
        spread = int((r / max(rows - 1, 1)) * (mid))
        for c in range(cols):
            if mid - spread <= c <= mid + spread:
                positions.add((r, c))
    return positions


def _formation_diamond(rows: int, cols: int) -> set:
    """Diamond shape: widest at middle row, narrow at top and bottom."""
    positions = set()
    mid_r = rows // 2
    mid_c = cols // 2
    for r in range(rows):
        dist = abs(r - mid_r)
        half_w = max(1, mid_c - dist)
        for c in range(cols):
            if mid_c - half_w <= c <= mid_c + half_w:
                positions.add((r, c))
    return positions


def _formation_wave(rows: int, cols: int) -> set:
    """Wave pattern: sinusoidal vertical offset, skip positions below threshold."""
    positions = set()
    for r in range(rows):
        for c in range(cols):
            # Use sine wave to create undulating rows
            offset = int(1.5 * math.sin(c * math.pi / max(cols - 1, 1)))
            effective_row = r + offset
            if 0 <= effective_row < rows:
                positions.add((effective_row, c))
    return positions


FORMATION_FUNCS = {
    "rectangle": _formation_rectangle,
    "v_shape": _formation_v_shape,
    "diamond": _formation_diamond,
    "wave": _formation_wave,
}


def get_formation_for_level(level: int) -> str:
    """Return the formation pattern name for a given level.

    Level 1 is always rectangle. Levels 2+ cycle through the other patterns.
    """
    if level <= 1:
        return "rectangle"
    non_rect = [n for n in FORMATION_NAMES if n != "rectangle"]
    return non_rect[(level - 2) % len(non_rect)]


def get_formation_positions(name: str, rows: int, cols: int) -> set:
    """Return the set of (row, col) positions for a named formation."""
    func = FORMATION_FUNCS.get(name, _formation_rectangle)
    return func(rows, cols)


# ASCII art title for animated title screen (each line is one row)
TITLE_ART = [
    " ███ ████  ██  ██ ██ █████",
    " █   █  █ █  █ █  █  █    ",
    " ███ ████ ████ █  █  ███  ",
    "   █ █    █  █ █  █  █    ",
    " ███ █    █  █  ██   █████",
    "",
    " █ █  █ █  █ ██  ███ ████ ███ ",
    " █ █  █ █  █ █ █ █ █ █  █ █  █",
    " █ █  █ █  █ █ █ █ █ █  █ ███ ",
    " █ █  ██ ████ █ █ █ █ ████ █ █",
    " █ █  █   █ █ ██  ███ █  █ █ █",
]

# Color pairs to cycle through for title animation
TITLE_COLOR_CYCLE = [
    COLOR_ALIEN_TYPE_0,   # Cyan
    COLOR_ALIEN_TYPE_1,   # Magenta
    COLOR_ALIEN_TYPE_2,   # Yellow
    COLOR_PLAYER,         # Green
    COLOR_PROJECTILE,     # White
    COLOR_GAME_OVER,      # Red
]

# Credits screen text lines (scrolls upward)
CREDITS_LINES = [
    "TERMINAL INVADERS",
    "",
    f"Version {__version__}",
    "",
    "---",
    "",
    "A curses-based Space Invaders clone",
    "for macOS terminals",
    "",
    "---",
    "",
    "Developed with Claude AI",
    "",
    "github.com/tbassignana/terminal_invaders",
    "",
    "---",
    "",
    "Inspired by the original",
    "Space Invaders (1978)",
    "by Tomohiro Nishikado",
    "",
    "---",
    "",
    "Thanks for playing!",
    "",
]


# ============================================================================
# ENUMS
# ============================================================================


class MenuScreen(Enum):
    """Sub-screens within the main menu."""

    MAIN = auto()
    HIGH_SCORES = auto()
    OPTIONS = auto()
    CONTROLS = auto()
    CREDITS = auto()


# Main menu items: (label, target MenuScreen or None for actions)
MENU_ITEMS = [
    ("Play", None),          # Starts the game
    ("High Scores", MenuScreen.HIGH_SCORES),
    ("Options", MenuScreen.OPTIONS),
    ("Controls", MenuScreen.CONTROLS),
    ("Credits", MenuScreen.CREDITS),
    ("Quit", None),          # Quits the game
]


class GameState(Enum):
    """Game state machine states."""

    MENU = auto()
    PLAYING = auto()
    PAUSED = auto()
    LEVEL_TRANSITION = auto()
    GAME_OVER = auto()


class GameEvent(Enum):
    """Events published via the EventBus."""

    ALIEN_KILLED = auto()
    PLAYER_HIT = auto()
    LEVEL_COMPLETE = auto()
    GAME_OVER = auto()
    SHOT_FIRED = auto()


class EventBus:
    """Simple publish/subscribe event system."""

    def __init__(self):
        self._subscribers: Dict[GameEvent, List] = {}

    def subscribe(self, event: GameEvent, handler) -> None:
        """Register a handler for an event."""
        if event not in self._subscribers:
            self._subscribers[event] = []
        self._subscribers[event].append(handler)

    def publish(self, event: GameEvent, **kwargs) -> None:
        """Fire an event and call all registered handlers."""
        for handler in self._subscribers.get(event, []):
            try:
                handler(**kwargs)
            except Exception:
                logger.debug("EventBus handler error for %s", event, exc_info=True)

    def clear(self) -> None:
        """Remove all subscribers."""
        self._subscribers.clear()


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def resolve_audio_path() -> str:
    """
    Resolve the audio file path using os.path.expanduser.

    Returns:
        Absolute path to ~/soundtrack.mp3
    """
    return os.path.expanduser("~/soundtrack.mp3")


# ============================================================================
# DATA CLASSES
# ============================================================================


@dataclass
class Player:
    """Player ship with position and lives."""

    x: int = 0
    y: int = 0
    lives: int = PLAYER_START_LIVES
    width: int = 3  # Width of player sprite

    def take_damage(self) -> None:
        """Decrement player lives when hit."""
        self.lives -= 1

    def reset(self, center_x: int, y: int) -> None:
        """Reset player to starting position and lives."""
        self.x = center_x
        self.y = y
        self.lives = PLAYER_START_LIVES


ALIEN_BEHAVIOR_NORMAL = "normal"
ALIEN_BEHAVIOR_ZIGZAG = "zigzag"
ALIEN_BEHAVIOR_SHIELDED = "shielded"

# Probability of special alien behaviors per type (increases with alien_type)
SPECIAL_ALIEN_CHANCE = {0: 0.15, 1: 0.10, 2: 0.05}


@dataclass
class Alien:
    """Individual alien with position, type, HP, and behavior."""

    x: int
    y: int
    alien_type: int = 0
    alive: bool = True
    hp: int = 1  # 1 for normal, 2 for shielded
    behavior: str = ALIEN_BEHAVIOR_NORMAL
    zigzag_offset: float = 0.0  # Current zigzag displacement
    zigzag_phase: float = 0.0  # Phase offset for zigzag oscillation
    base_x: int = 0  # Original x for zigzag calculations

    def __post_init__(self):
        """Store base x for zigzag movement."""
        self.base_x = self.x

    def take_hit(self) -> bool:
        """Reduce HP. Return True if the alien is killed."""
        self.hp -= 1
        return self.hp <= 0

    def is_shielded(self) -> bool:
        """Return True if this alien has more than 1 HP."""
        return self.hp > 1


# Death animation characters: # → * → + → gone
DEATH_ANIM_CHARS = ["#", "*", "+"]
DEATH_ANIM_FRAMES = len(DEATH_ANIM_CHARS)


@dataclass
class DyingAlien:
    """An alien mid-death-animation (flash sequence before removal)."""

    x: int
    y: int
    frame: int = 0  # current animation frame (0-based)

    @property
    def char(self) -> str:
        """Return the current death animation character."""
        if 0 <= self.frame < len(DEATH_ANIM_CHARS):
            return DEATH_ANIM_CHARS[self.frame]
        return ""

    @property
    def finished(self) -> bool:
        """Return True when the death animation is complete."""
        return self.frame >= DEATH_ANIM_FRAMES

    def advance(self) -> None:
        """Advance to the next animation frame."""
        self.frame += 1


# Dive-bomb attack constants
DIVE_BOMB_CHANCE = 0.003  # Per-frame chance of a dive-bomb attack (per alien check cycle)
DIVE_BOMB_SPEED = 0.6  # Vertical speed of diving alien (cells per frame)
DIVE_BOMB_MIN_LEVEL = 3  # Dive-bombs start from level 3


@dataclass
class DivingAlien:
    """An alien that has broken formation to dive toward the player."""

    alien: Alien  # Reference to the actual alien
    dx: float = 0.0  # Horizontal speed per frame (toward player)
    dy: float = DIVE_BOMB_SPEED  # Vertical speed per frame (always downward)
    float_x: float = 0.0  # Precise float x position
    float_y: float = 0.0  # Precise float y position

    def __post_init__(self):
        self.float_x = float(self.alien.x)
        self.float_y = float(self.alien.y)


@dataclass
class ScorePopup:
    """Floating score text that drifts upward on alien kill."""

    x: float
    y: float
    text: str  # e.g. "+30" or "+60 x2!"
    age: float = 0.0
    lifetime: float = 1.0
    dy: float = -2.0  # Upward velocity (negative = up)

    @property
    def finished(self) -> bool:
        """Return True when popup has expired."""
        return self.age >= self.lifetime

    def update(self, dt: float) -> None:
        """Age and move the popup."""
        self.age += dt
        self.y += self.dy * dt


@dataclass
class RippleEffect:
    """A horizontal wave/ripple line triggered when aliens descend."""

    y: int  # Row where the ripple appears
    age: float = 0.0
    lifetime: float = 0.4  # Fades over 0.4 seconds

    @property
    def finished(self) -> bool:
        """Return True when the ripple has fully faded."""
        return self.age >= self.lifetime

    def update(self, dt: float) -> None:
        """Age the ripple effect."""
        self.age += dt


@dataclass
class ComboText:
    """Center-screen combo notification (e.g. 'COMBO x3!')."""

    text: str
    age: float = 0.0
    lifetime: float = 1.5  # Display duration in seconds

    @property
    def finished(self) -> bool:
        """Return True when the combo text has fully faded."""
        return self.age >= self.lifetime

    def update(self, dt: float) -> None:
        """Age the combo text."""
        self.age += dt


@dataclass
class Bunker:
    """Defensive bunker that erodes on hits."""

    x: int
    y: int
    health: int = 3  # 3=full, 2=damaged, 1=critical, 0=destroyed
    max_health: int = 3  # Starting health for ratio-based color gradient
    flash_frames: int = 0  # Frames remaining for white flash on hit

    @property
    def char(self) -> str:
        """Get display character based on health."""
        if self.health <= 0:
            return " "
        return BUNKER_CHARS[3 - self.health]

    @property
    def health_ratio(self) -> float:
        """Return health as a 0.0-1.0 ratio (current / max)."""
        if self.max_health <= 0:
            return 0.0
        return self.health / self.max_health

    @property
    def color_pair(self) -> int:
        """Return color pair based on health ratio: green → green-yellow → yellow → yellow-red → red.

        Uses 5-tier gradient for smooth visual transitions as bunker degrades.
        """
        if self.flash_frames > 0:
            return COLOR_TEXT  # White flash
        ratio = self.health_ratio
        if ratio > 0.8:
            return COLOR_BUNKER  # Full green
        if ratio > 0.6:
            return COLOR_BUNKER_HIGH  # Bright green (transitioning)
        if ratio > 0.4:
            return COLOR_BUNKER_DAMAGED  # Yellow
        if ratio > 0.2:
            return COLOR_BUNKER_LOW  # Dark yellow/red (transitioning)
        return COLOR_BUNKER_CRITICAL  # Red

    def hit(self) -> bool:
        """
        Process a hit on the bunker.
        Returns True if bunker is destroyed.
        """
        self.health -= 1
        self.flash_frames = 2  # Flash white for 2 frames
        return self.health <= 0


@dataclass
class Projectile:
    """Projectile fired by player or alien."""

    x: int
    y: float
    direction: int  # -1 for up (player), 1 for down (alien)
    trail: list = None  # Position history for trail effect (last 3 positions)
    proj_type: str = "normal"  # "normal", "fast", or "heavy"

    def __post_init__(self) -> None:
        if self.trail is None:
            self.trail = []

    def record_position(self) -> None:
        """Record current position for trail rendering. Keep last 3 positions."""
        self.trail.append((self.x, self.y))
        if len(self.trail) > 3:
            self.trail.pop(0)


# Alien projectile types: char, speed multiplier, bunker damage
ALIEN_PROJ_TYPES = {
    "normal": {"char": "!", "speed_mult": 1.0, "bunker_damage": 1},
    "fast": {"char": "v", "speed_mult": 1.8, "bunker_damage": 1},
    "heavy": {"char": "#", "speed_mult": 0.7, "bunker_damage": 3},  # Bunker-breaker
}
# Weighted probabilities for random selection (normal=60%, fast=25%, heavy=15%)
ALIEN_PROJ_WEIGHTS = {"normal": 60, "fast": 25, "heavy": 15}

MYSTERY_SHIP_CHAR = "=<UFO>="  # Wider 7-char sprite


@dataclass
class MysteryShip:
    """UFO that crosses the top of the screen for bonus points."""

    x: float
    y: int = 1
    speed: float = 0.5
    points: int = 100
    active: bool = True
    blink_frame: int = 0  # Toggles for blink effect

    def get_display_char(self) -> str:
        """Return display character (blinks every 4 frames)."""
        if self.blink_frame % 8 < 6:  # Visible 6 out of 8 frames
            return MYSTERY_SHIP_CHAR
        return " " * len(MYSTERY_SHIP_CHAR)  # Blank during blink

    def advance_blink(self) -> None:
        """Advance the blink animation frame."""
        self.blink_frame += 1


BOSS_SPRITE = "[=<BOSS>=]"  # 10-char wide boss sprite
BOSS_HP_PER_LEVEL = 5  # HP per boss level tier (level 5=5HP, level 10=10HP, etc.)
BOSS_POINTS = 500  # Base points for defeating a boss
BOSS_FIRE_INTERVAL = 1.5  # Seconds between boss shots

# Weapon upgrade system: kills needed for each level, projectile offsets, speed multiplier
WEAPON_MAX_LEVEL = 5
WEAPON_KILLS_PER_LEVEL = 15  # Kills to reach next weapon level
# Projectile x-offsets relative to player center (player.x + 1) for each weapon level
WEAPON_PATTERNS = {
    1: [(0, 0)],                            # Single: center
    2: [(-1, 0), (1, 0)],                   # Dual: left + right
    3: [(-1, 0), (0, 0), (1, 0)],           # Triple: left + center + right
    4: [(-2, 0), (-1, 0), (1, 0), (2, 0)],  # Quad: wide spread
    5: [(-2, 0), (-1, 0), (0, 0), (1, 0), (2, 0)],  # Quad+ (5 shots + speed boost for level 5 is handled separately)
}
WEAPON_SPEED_BONUS = {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 5: 1.3}  # Level 5 = 30% faster

# Last Stand mechanic — buffs when player is at 1 life
LAST_STAND_SPEED_MULT = 2  # Double movement speed
LAST_STAND_FIRE_SLOT_MULT = 2  # Double max projectile slots

# Score milestones that award bonus lives
SCORE_MILESTONES = [1000, 2500, 5000, 10000, 25000, 50000]


@dataclass
class BossAlien:
    """Boss alien that appears every 5 levels."""

    x: float
    y: int
    hp: int
    max_hp: int
    direction: int = 1  # 1=right, -1=left
    speed: float = 0.3
    last_fire_time: float = 0
    hit_flash_until: float = 0  # Brief flash when hit

    def is_alive(self) -> bool:
        """Return True if boss still has HP."""
        return self.hp > 0

    def take_hit(self) -> int:
        """Reduce HP by 1. Return remaining HP."""
        self.hp = max(0, self.hp - 1)
        self.hit_flash_until = time.time() + 0.1
        return self.hp

    def get_display(self) -> str:
        """Return the boss sprite (flashes on hit)."""
        if time.time() < self.hit_flash_until:
            return "#" * len(BOSS_SPRITE)
        return BOSS_SPRITE

    def get_hp_bar(self) -> str:
        """Return a text HP bar like [####----]."""
        filled = self.hp
        empty = self.max_hp - self.hp
        return f"[{'#' * filled}{'-' * empty}]"


class PowerUpType(Enum):
    """Types of power-ups."""

    RAPID_FIRE = auto()
    SHIELD = auto()
    WIDE_SHOT = auto()
    BOMB = auto()
    BULLET_TIME = auto()

BULLET_TIME_DURATION = 4.0  # Seconds of slow motion
BULLET_TIME_SPEED_MULT = 0.5  # 50% alien speed during bullet time

# Distinct display characters for each power-up type
POWERUP_CHARS = {
    PowerUpType.RAPID_FIRE: "R",
    PowerUpType.SHIELD: "S",
    PowerUpType.WIDE_SHOT: "W",
    PowerUpType.BOMB: "B",
    PowerUpType.BULLET_TIME: "T",
}

# Short labels for HUD timer display
POWERUP_LABELS = {
    PowerUpType.RAPID_FIRE: "R",
    PowerUpType.SHIELD: "S",
    PowerUpType.WIDE_SHOT: "W",
    PowerUpType.BOMB: "B",
    PowerUpType.BULLET_TIME: "T",
}


@dataclass
class PowerUp:
    """A falling power-up pickup."""

    x: int
    y: float
    power_type: PowerUpType
    speed: float = 0.3


@dataclass
class ActivePowerUp:
    """An active power-up effect on the player."""

    power_type: PowerUpType
    expires_at: float


# Collectible coin/gem types with display char, color description, and point value
COLLECTIBLE_TYPES = {
    "bronze": {"char": "o", "points": 10, "color": COLOR_BUNKER_DAMAGED},    # Yellow
    "silver": {"char": "O", "points": 25, "color": COLOR_TEXT},              # White
    "gold": {"char": "$", "points": 50, "color": COLOR_ALIEN_TYPE_2},        # Yellow-bold
}
COLLECTIBLE_EXPIRY = 5.0  # Seconds before a collectible despawns
COLLECTIBLE_DROP_CHANCE = 0.15  # 15% chance per alien kill


@dataclass
class Collectible:
    """A falling coin/gem that awards bonus points when collected."""

    x: int
    y: float
    coin_type: str  # "bronze", "silver", or "gold"
    spawn_time: float
    speed: float = 0.2

    @property
    def points(self) -> int:
        """Return point value for this collectible type."""
        return COLLECTIBLE_TYPES.get(self.coin_type, {}).get("points", 10)

    @property
    def char(self) -> str:
        """Return display character for this collectible type."""
        return COLLECTIBLE_TYPES.get(self.coin_type, {}).get("char", "o")

    def is_expired(self, current_time: float) -> bool:
        """Return True if this collectible has timed out."""
        return current_time - self.spawn_time >= COLLECTIBLE_EXPIRY


# ============================================================================
# PARTICLE SYSTEM
# ============================================================================


@dataclass
class Particle:
    """A single visual particle for effects like explosions and trails."""

    x: float
    y: float
    dx: float  # horizontal velocity (chars/sec)
    dy: float  # vertical velocity (chars/sec)
    char: str
    color_pair: int
    lifetime: float  # total lifetime in seconds
    age: float = 0.0  # current age in seconds


class ParticleSystem:
    """Manages a collection of particles: spawning, updating, culling, and rendering."""

    def __init__(self) -> None:
        self.particles: List[Particle] = []

    def spawn(
        self,
        x: float,
        y: float,
        count: int,
        chars: str = "*+.'`",
        color_pair: int = 0,
        speed_range: Tuple[float, float] = (2.0, 8.0),
        lifetime_range: Tuple[float, float] = (0.3, 0.5),
    ) -> None:
        """Spawn a burst of particles at (x, y) spreading in random directions."""
        for _ in range(count):
            angle = random.uniform(0, 2 * math.pi)
            speed = random.uniform(speed_range[0], speed_range[1])
            dx = math.cos(angle) * speed
            dy = math.sin(angle) * speed
            char = random.choice(chars)
            lt = random.uniform(lifetime_range[0], lifetime_range[1])
            self.particles.append(
                Particle(x=x, y=y, dx=dx, dy=dy, char=char, color_pair=color_pair, lifetime=lt)
            )

    def update(self, dt: float) -> None:
        """Advance all particles by dt seconds, cull expired ones."""
        alive = []
        for p in self.particles:
            p.age += dt
            if p.age < p.lifetime:
                p.x += p.dx * dt
                p.y += p.dy * dt
                alive.append(p)
        self.particles = alive

    def clear(self) -> None:
        """Remove all particles."""
        self.particles.clear()


# ============================================================================
# SCORE MANAGER
# ============================================================================

DEFAULT_SCORES_PATH = os.path.expanduser("~/.invaders_scores.json")


class ScoreManager:
    """Tracks current score, high score, and persists top 10 scores."""

    def __init__(self, scores_path: str = DEFAULT_SCORES_PATH):
        self.scores_path = scores_path
        self.current_score = 0
        self.scores: List[Dict] = []
        self._load()

    def _load(self) -> None:
        """Load scores from disk."""
        try:
            with open(self.scores_path, "r") as f:
                self.scores = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.scores = []

    def _save(self) -> None:
        """Persist scores to disk."""
        try:
            with open(self.scores_path, "w") as f:
                json.dump(self.scores, f, indent=2)
        except OSError:
            logger.warning("Failed to save scores to %s", self.scores_path, exc_info=True)

    @property
    def high_score(self) -> int:
        """Return the highest recorded score, or 0."""
        if not self.scores:
            return 0
        return max(entry["score"] for entry in self.scores)

    def record(self, score: int, level: int) -> None:
        """Record a finished game score and persist."""
        entry = {
            "score": score,
            "level": level,
            "date": datetime.now().isoformat(),
        }
        self.scores.append(entry)
        # Keep only top 10
        self.scores.sort(key=lambda e: e["score"], reverse=True)
        self.scores = self.scores[:10]
        self._save()

    def reset_current(self) -> None:
        """Reset current score (for a new game)."""
        self.current_score = 0

    def add(self, points: int) -> None:
        """Add points to the current score."""
        self.current_score += points


# ============================================================================
# AUDIO SYSTEM
# ============================================================================


class AbstractSoundBackend(ABC):
    """Abstract interface for sound playback backends."""

    @abstractmethod
    def play(self, path: str, volume: float = 0.5) -> None:
        """Play a sound file at the given volume (non-blocking)."""

    @abstractmethod
    def stop(self) -> None:
        """Stop all currently playing sounds."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this backend can play audio on the current platform."""


class MacOSSoundBackend(AbstractSoundBackend):
    """Sound backend using macOS afplay command."""

    def play(self, path: str, volume: float = 0.5) -> None:
        if not os.path.exists(path):
            return

        def _play():
            try:
                subprocess.run(
                    ["afplay", "-v", str(volume), path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2
                )
            except Exception:
                logger.debug("MacOSSoundBackend: failed to play '%s'", path, exc_info=True)

        thread = threading.Thread(target=_play, daemon=True)
        thread.start()

    def stop(self) -> None:
        try:
            subprocess.run(["pkill", "-9", "afplay"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            logger.debug("MacOSSoundBackend: failed to stop sounds", exc_info=True)

    def is_available(self) -> bool:
        try:
            result = subprocess.run(["which", "afplay"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return result.returncode == 0
        except Exception:
            return False


class NullSoundBackend(AbstractSoundBackend):
    """Silent sound backend for testing and unsupported platforms."""

    def play(self, path: str, volume: float = 0.5) -> None:
        pass

    def stop(self) -> None:
        pass

    def is_available(self) -> bool:
        return True


def get_sound_backend() -> AbstractSoundBackend:
    """Select the best available sound backend for the current platform."""
    if sys.platform == "darwin":
        backend = MacOSSoundBackend()
        if backend.is_available():
            return backend
    return NullSoundBackend()


class AudioManager:
    """
    Non-blocking audio manager using daemon threads.

    Handles looping background music using macOS afplay command.
    """

    def __init__(self):
        self.game_running = True
        self.audio_thread: Optional[threading.Thread] = None
        self.current_process: Optional[subprocess.Popen] = None

    def start(self) -> None:
        """Start the audio loop in a daemon thread."""
        audio_path = resolve_audio_path()
        if not os.path.exists(audio_path):
            return  # No audio file, skip silently

        self.audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
        self.audio_thread.start()

    def _audio_loop(self) -> None:
        """Main audio loop - plays soundtrack repeatedly."""
        audio_path = resolve_audio_path()

        while self.game_running:
            try:
                # Using subprocess.call blocks until playback completes
                # Then loop restarts immediately
                self.current_process = subprocess.Popen(
                    ["afplay", audio_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                self.current_process.wait()
            except Exception:
                logger.warning("Audio loop encountered an error", exc_info=True)
                break

    def stop(self) -> None:
        """Stop audio playback and clean up."""
        self.game_running = False

        # Kill any running afplay process
        if self.current_process:
            try:
                self.current_process.terminate()
                self.current_process.wait(timeout=1)
            except Exception:
                logger.debug("Failed to terminate afplay gracefully, killing", exc_info=True)
                try:
                    self.current_process.kill()
                except Exception:
                    logger.debug("Failed to kill afplay process", exc_info=True)

        # Also kill any stray afplay processes
        try:
            subprocess.run(["pkill", "-9", "afplay"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            logger.debug("Failed to kill stray afplay processes", exc_info=True)


class SoundEffects:
    """
    Retro-style sound effects mimicking original Space Invaders.

    Uses a pluggable SoundBackend for cross-platform audio.
    The original Space Invaders had:
    - Marching beat that sped up as aliens decreased
    - Shooting "pew" sound
    - Alien explosion sound
    - Player death sound
    - UFO sound
    """

    # macOS system sound paths
    SOUNDS = {
        "shoot": "/System/Library/Sounds/Pop.aiff",
        "alien_die": "/System/Library/Sounds/Bottle.aiff",
        "player_die": "/System/Library/Sounds/Basso.aiff",
        "level_complete": "/System/Library/Sounds/Glass.aiff",
        "march1": "/System/Library/Sounds/Tink.aiff",
        "march2": "/System/Library/Sounds/Pop.aiff",
        "life_bonus": "/System/Library/Sounds/Hero.aiff",
    }

    def __init__(self, backend: Optional[AbstractSoundBackend] = None):
        self.enabled = True
        self.backend = backend or get_sound_backend()
        self.march_beat = 0  # Alternates between 0 and 1
        self.last_march_time = 0
        self.march_interval = 0.5  # Starting interval (speeds up)
        self._verify_sounds()

    def _verify_sounds(self):
        """Check which sounds are available."""
        self.available_sounds = {}
        for name, path in self.SOUNDS.items():
            self.available_sounds[name] = os.path.exists(path)

    def _play_async(self, sound_name: str, volume: float = 0.5):
        """Play a sound via the backend."""
        if not self.enabled:
            return
        if sound_name not in self.SOUNDS:
            return
        if not self.available_sounds.get(sound_name, False):
            return
        self.backend.play(self.SOUNDS[sound_name], volume)

    def subscribe_to_events(self, event_bus: "EventBus") -> None:
        """Subscribe sound effects to game events via the EventBus."""
        event_bus.subscribe(GameEvent.SHOT_FIRED, lambda **kw: self.play_shoot())
        event_bus.subscribe(GameEvent.ALIEN_KILLED, lambda **kw: self.play_alien_die())
        event_bus.subscribe(GameEvent.PLAYER_HIT, lambda **kw: self.play_player_die())
        event_bus.subscribe(GameEvent.LEVEL_COMPLETE, lambda **kw: self.play_level_complete())

    def play_shoot(self):
        """Player shooting sound."""
        self._play_async("shoot", 0.3)

    def play_alien_die(self):
        """Alien explosion sound."""
        self._play_async("alien_die", 0.4)

    def play_player_die(self):
        """Player death sound."""
        self._play_async("player_die", 0.6)

    def play_level_complete(self):
        """Level completion fanfare."""
        self._play_async("level_complete", 0.5)

    def play_life_bonus(self):
        """Extra life awarded sound."""
        self._play_async("life_bonus", 0.5)

    def update_march(self, alien_count: int, total_aliens: int):
        """
        Update the marching beat - plays faster as fewer aliens remain.
        This is the iconic Space Invaders "heartbeat" that speeds up.
        """
        if alien_count <= 0:
            return

        current_time = time.time()

        # Calculate march speed based on remaining aliens
        # Fewer aliens = faster beat (more tense!)
        ratio = alien_count / total_aliens
        # Interval ranges from 0.5s (full) to 0.1s (almost empty)
        self.march_interval = 0.1 + (ratio * 0.4)

        if current_time - self.last_march_time >= self.march_interval:
            # Alternate between two beat sounds
            if self.march_beat == 0:
                self._play_async("march1", 0.2)
            else:
                self._play_async("march2", 0.2)

            self.march_beat = 1 - self.march_beat
            self.last_march_time = current_time


# ============================================================================
# FRAME TIMER / PERFORMANCE METRICS
# ============================================================================


class FrameTimer:
    """Rolling-window frame time tracker for performance metrics.

    Tracks the last N frame times and provides average FPS, min/max
    frame time, and frame time variance.
    """

    def __init__(self, window_size: int = 60):
        self.window_size = window_size
        self.frame_times: List[float] = []

    def record(self, frame_time: float) -> None:
        """Record a frame time (in seconds)."""
        self.frame_times.append(frame_time)
        if len(self.frame_times) > self.window_size:
            self.frame_times.pop(0)

    @property
    def average_fps(self) -> float:
        """Return average FPS over the rolling window."""
        if not self.frame_times:
            return 0.0
        avg_time = sum(self.frame_times) / len(self.frame_times)
        return 1.0 / avg_time if avg_time > 0 else 0.0

    @property
    def min_frame_time(self) -> float:
        """Return the minimum frame time in the window."""
        return min(self.frame_times) if self.frame_times else 0.0

    @property
    def max_frame_time(self) -> float:
        """Return the maximum frame time in the window."""
        return max(self.frame_times) if self.frame_times else 0.0

    @property
    def variance(self) -> float:
        """Return the variance of frame times in the window."""
        if len(self.frame_times) < 2:
            return 0.0
        avg = sum(self.frame_times) / len(self.frame_times)
        return sum((t - avg) ** 2 for t in self.frame_times) / len(self.frame_times)


# ============================================================================
# SPATIAL PARTITIONING
# ============================================================================


class SpatialGrid:
    """Grid-based spatial index for efficient collision detection.

    Divides the screen into cells and allows querying entities in the same
    or adjacent cells, reducing collision checks from O(n*m) to roughly O(n+m).
    """

    def __init__(self, width: int, height: int, cell_size: int = 4):
        self.cell_size = cell_size
        self.cols = max(1, width // cell_size + 1)
        self.rows = max(1, height // cell_size + 1)
        self.grid: Dict[Tuple[int, int], List] = {}

    def clear(self) -> None:
        """Remove all entities from the grid."""
        self.grid.clear()

    def _cell(self, x: float, y: float) -> Tuple[int, int]:
        """Return the grid cell coordinates for a given position."""
        return (int(x) // self.cell_size, int(y) // self.cell_size)

    def insert(self, entity, x: float, y: float) -> None:
        """Insert an entity at the given position."""
        cell = self._cell(x, y)
        if cell not in self.grid:
            self.grid[cell] = []
        self.grid[cell].append(entity)

    def query_nearby(self, x: float, y: float) -> List:
        """Return all entities in the same cell or adjacent cells."""
        cx, cy = self._cell(x, y)
        result = []
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                key = (cx + dx, cy + dy)
                if key in self.grid:
                    result.extend(self.grid[key])
        return result


# ============================================================================
# MAIN GAME CLASS
# ============================================================================


class Game:
    """
    Main game controller implementing the Space Invaders game logic.

    Handles:
    - Game state machine
    - Entity management (player, aliens, projectiles, bunkers)
    - Collision detection
    - Rendering
    - Input handling
    """

    def __init__(self, test_mode: bool = False, config: Optional[GameConfig] = None):
        """
        Initialize game.

        Args:
            test_mode: If True, skip curses initialization for unit testing.
            config: Game configuration. Uses DEFAULT_CONFIG if not provided.
        """
        self.config = config or DEFAULT_CONFIG
        self.test_mode = test_mode
        self.state = GameState.PLAYING if test_mode else GameState.MENU
        self.menu_screen: MenuScreen = MenuScreen.MAIN
        self.menu_selection: int = 0  # Index into MENU_ITEMS
        self.options_selection: int = 0  # Index into options items
        self.credits_scroll_offset: float = 0.0  # Scrolls upward over time
        self.menu_cursor_frame: int = 0  # For bobbing animation
        self.menu_flash_until: float = 0  # Flash on select timestamp
        self.sound_enabled: bool = True
        self.music_enabled: bool = True
        self.score_manager = ScoreManager() if not test_mode else None
        self.score = 0
        self.level = 1
        self.initial_alien_count: int = 0  # Set by _init_aliens()
        self.current_wave: int = 1  # Sub-wave within a level (1-3)
        self.max_waves: int = 3  # Waves per level
        self.weapon_level: int = 1  # Weapon upgrade level (1-5)
        self.milestones_reached: List[int] = []  # Score milestones already awarded

        # Game stats tracking (for game over screen)
        self.total_shots: int = 0
        self.total_kills: int = 0
        self.game_over_time: float = 0  # When GAME_OVER state was entered

        # Level transition countdown (3... 2... 1... GO!)
        self.level_transition_time: float = 0  # When LEVEL_TRANSITION started
        self.level_countdown_duration: float = 4.0  # 3s countdown + 1s "GO!"

        # Mutable speed state (changes per level, not stored in frozen config)
        self.alien_move_interval = self.config.alien_move_interval

        # Screen dimensions (defaults for test mode)
        self.width = 80
        self.height = 24
        self.too_small = False  # True when terminal is below minimum size

        # Frame timer for performance metrics
        self.frame_timer = FrameTimer()
        self.show_fps = False  # Toggled by F1 or --show-fps

        # Initialize player at center-bottom
        self.player = Player(lives=self.config.player_start_lives)
        self._init_player_position()

        # Game entities
        self.aliens: List[Alien] = []
        self.player_projectiles: List[Projectile] = []
        self.alien_projectiles: List[Projectile] = []
        self.bunkers: List[Bunker] = []
        self.dying_aliens: List[DyingAlien] = []
        self.diving_aliens: List[DivingAlien] = []
        self.score_popups: List[ScorePopup] = []
        self.ripple_effects: List[RippleEffect] = []

        # Animation state
        self.alien_animation_frame = 0
        self.last_animation_time = time.time()
        self.alien_direction = 1  # 1=right, -1=left
        self.last_alien_move_time = time.time()

        # Player thrust animation state
        self.thrust_frame = 0  # Alternates 0/1 each frame for flicker
        self.player_move_direction = 0  # -1=left, 0=stationary, 1=right

        # Title screen animation
        self.title_color_frame = 0  # Color cycle counter for title screen

        # Starfield background
        self.stars: List[Tuple[float, float, str]] = []  # [(x, y, char), ...]
        self.star_scroll_speed = 1.0  # pixels per second
        self._generate_starfield()

        # Flash effect state
        self.flash_active = False
        self.flash_end_time = 0
        self.flash_color = COLOR_GAME_OVER  # Red for death, green for level complete

        # Mystery ship (UFO)
        self.mystery_ship: Optional[MysteryShip] = None
        self.last_mystery_spawn_check = time.time()
        self.mystery_score_display: Optional[Tuple[int, int, int, float]] = None  # (x, y, pts, end_time)

        # Boss alien (every 5 levels)
        self.boss: Optional[BossAlien] = None

        # Pause tracking
        self.pause_start_time: float = 0

        # Screen shake
        self.shake_end_time: float = 0
        self.shake_duration: float = 0.3
        self.shake_offset_x: int = 0
        self.shake_offset_y: int = 0

        # Invincibility (i-frames after taking damage)
        self.invincible_until: float = 0
        self.invincibility_duration: float = 2.0  # seconds of blink + i-frames

        # Power-ups
        self.power_ups: List[PowerUp] = []
        self.active_power_ups: List[ActivePowerUp] = []
        self.bomb_flash_until: float = 0  # Screen flash on bomb use
        self.bomb_clear_count: int = 0  # Projectiles cleared by last bomb

        # Collectibles (coins/gems)
        self.collectibles: List[Collectible] = []

        # Combo system
        self.combo_count = 0
        self.combo_last_kill_time: float = 0
        self.combo_window: float = 2.0  # seconds
        self.combo_max: int = 5
        self.combo_text: Optional[ComboText] = None

        # Particle system (visual-only, skipped in test_mode)
        self.particle_system = ParticleSystem()
        self._last_update_time: float = time.time()

        # Event bus
        self.event_bus = EventBus()

        # Audio
        self.audio: Optional[AudioManager] = None

        # Sound effects
        self.sfx: Optional[SoundEffects] = None
        if not test_mode:
            self.sfx = SoundEffects()
            self.sfx.subscribe_to_events(self.event_bus)

        # Curses screen
        self.screen = None

        # Level completion bonus tracking
        self.lives_awarded = 0  # Lives awarded at last level completion

        # Initialize aliens
        self._init_aliens()

        if not test_mode:
            self._init_bunkers()

    def _generate_starfield(self) -> None:
        """Generate random star positions proportional to screen area."""
        area = self.width * self.height
        star_count = max(10, area // 40)  # ~1 star per 40 chars
        self.stars = []
        for _ in range(star_count):
            x = random.uniform(0, self.width - 1)
            y = random.uniform(0, self.height - 1)
            char = random.choice([".", "*"])
            self.stars.append((x, y, char))

    def _init_player_position(self) -> None:
        """Set player starting position."""
        self.player.x = self.width // 2 - 1
        self.player.y = self.height - 2
        self._initial_player_x = self.player.x

    def _init_aliens(self) -> None:
        """Create the alien formation (pattern and rows scaled by level)."""
        self.aliens = []
        cfg = self.config
        rows = self.get_scaled_alien_rows()
        start_x = (self.width - (cfg.alien_cols * cfg.alien_spacing_x)) // 2

        pattern_name = get_formation_for_level(self.level)
        positions = get_formation_positions(pattern_name, rows, cfg.alien_cols)

        for row in range(rows):
            alien_type = row // 2  # Different types per rows
            for col in range(cfg.alien_cols):
                if (row, col) not in positions:
                    continue
                x = start_x + col * cfg.alien_spacing_x
                y = cfg.alien_start_y + row * cfg.alien_spacing_y
                # Assign special behaviors based on level and chance
                behavior = ALIEN_BEHAVIOR_NORMAL
                hp = 1
                if self.level >= 3:
                    chance = SPECIAL_ALIEN_CHANCE.get(alien_type % 3, 0.05)
                    roll = random.random()
                    if roll < chance / 2:
                        behavior = ALIEN_BEHAVIOR_SHIELDED
                        hp = 2
                    elif roll < chance:
                        behavior = ALIEN_BEHAVIOR_ZIGZAG
                alien = Alien(
                    x=x, y=y, alien_type=alien_type % 3,
                    hp=hp, behavior=behavior,
                    zigzag_phase=random.uniform(0, math.pi * 2),
                )
                self.aliens.append(alien)
        self.initial_alien_count = len(self.aliens)

        # Spawn boss on boss levels (every 5 levels)
        if self.is_boss_level():
            boss_hp = (self.level // 5) * BOSS_HP_PER_LEVEL
            self.boss = BossAlien(
                x=float(self.width // 2 - len(BOSS_SPRITE) // 2),
                y=2,
                hp=boss_hp,
                max_hp=boss_hp,
            )
        else:
            self.boss = None

    def _init_bunkers(self) -> None:
        """Create defensive bunkers (health scaled by level)."""
        self.bunkers = []
        bunker_y = self.height - 6
        bunker_spacing = self.width // 5
        health = self.get_scaled_bunker_health()

        for i in range(4):
            bunker_x = bunker_spacing * (i + 1) - 2
            # Create bunker block (3x2)
            for dx in range(-1, 2):
                for dy in range(2):
                    self.bunkers.append(Bunker(x=bunker_x + dx, y=bunker_y + dy, health=health, max_health=health))

    def get_alien_fire_probability(self) -> float:
        """
        Calculate alien firing probability based on remaining aliens.
        Implements 'frenzy mode' - fewer aliens = more shooting.

        Returns:
            Probability of each alien firing per frame.
        """
        if not self.aliens:
            return 0

        cfg = self.config
        # Calculate ratio of destroyed aliens
        total_aliens = self.initial_alien_count or 1
        remaining = len(self.aliens)
        destroyed_ratio = 1 - (remaining / total_aliens)

        # Linear interpolation between base and max probability
        probability = cfg.base_fire_probability + (
            destroyed_ratio * (cfg.max_fire_probability - cfg.base_fire_probability)
        )

        return min(probability, cfg.max_fire_probability)

    def is_invincible(self) -> bool:
        """Return True if the player is currently in i-frames (invincible after damage)."""
        return time.time() < self.invincible_until

    def handle_player_damage(self) -> None:
        """
        Handle player taking damage from alien projectile.
        - Respects i-frames (invincibility window after previous hit)
        - Decrements life
        - Triggers flash effect and invincibility blink
        - Clears projectiles
        - Resets position
        - Checks for game over
        """
        # I-frames: ignore damage while invincible
        if self.is_invincible():
            return

        # Shield absorbs one hit
        if self.has_power_up(PowerUpType.SHIELD):
            self._consume_shield()
            return

        self.player.take_damage()

        # Notify subscribers
        self.event_bus.publish(GameEvent.PLAYER_HIT)

        if self.player.lives <= 0:
            self.state = GameState.GAME_OVER
            self.game_over_time = time.time()
            self.event_bus.publish(GameEvent.GAME_OVER)
        else:
            # Red flash effect for damage
            self.flash_active = True
            self.flash_color = COLOR_GAME_OVER  # Red
            self.flash_end_time = time.time() + 0.2

            # Activate invincibility blink (2s i-frames)
            self.invincible_until = time.time() + self.invincibility_duration

            # Screen shake (not in test_mode)
            if not self.test_mode:
                self.shake_end_time = time.time() + self.shake_duration

            # Clear projectiles
            self.player_projectiles.clear()
            self.alien_projectiles.clear()

            # Reset player position
            self.player.x = self._initial_player_x

    def check_invasion(self) -> None:
        """
        Check if any alien has reached the player's row.
        Triggers instant GAME_OVER if so.
        """
        player_y = self.player.y
        for alien in self.aliens:
            if alien.y >= player_y:
                self.state = GameState.GAME_OVER
                self.game_over_time = time.time()
                return

    def reset_game(self) -> None:
        """
        Perform a hard reset of the game state.
        Called when player presses 'R' at game over.
        """
        # Reset score and stats
        self.score = 0
        self.total_shots = 0
        self.total_kills = 0
        self.game_over_time = 0
        self.milestones_reached.clear()

        # Reset player
        self.player.lives = self.config.player_start_lives
        self.player.x = self._initial_player_x

        # Clear all projectiles
        self.player_projectiles.clear()
        self.alien_projectiles.clear()

        # Re-initialize aliens
        self._init_aliens()

        # Reset bunkers
        if not self.test_mode:
            self._init_bunkers()

        # Reset alien movement
        self.alien_direction = 1
        self.alien_move_interval = self.config.alien_move_interval
        self.last_alien_move_time = time.time()

        # Reset mystery ship
        self.mystery_ship = None
        self.mystery_score_display = None

        # Clear power-ups
        self.power_ups.clear()
        self.active_power_ups.clear()
        self.bomb_flash_until = 0
        self.bomb_clear_count = 0

        # Clear dying aliens, diving aliens, score popups, and ripple effects
        self.dying_aliens.clear()
        self.diving_aliens.clear()
        self.score_popups.clear()
        self.ripple_effects.clear()

        # Reset invincibility
        self.invincible_until = 0

        # Reset combo
        self.combo_count = 0
        self.combo_last_kill_time = 0
        self.combo_text = None

        # Clear particles
        self.particle_system.clear()

        # Set state to playing
        self.state = GameState.PLAYING

    def update(self) -> None:
        """Main game update loop."""
        # Title screen color cycling runs in MENU state
        if self.state == GameState.MENU:
            self.title_color_frame += 1
            self.menu_cursor_frame += 1
            # Scroll credits when on credits screen
            if self.menu_screen == MenuScreen.CREDITS:
                self.credits_scroll_offset += 0.05
                # Reset when all lines have scrolled past
                if self.credits_scroll_offset > len(CREDITS_LINES) + self.height // 2:
                    self.credits_scroll_offset = 0.0
            return

        # Auto-advance level transition countdown
        if self.state == GameState.LEVEL_TRANSITION:
            elapsed = time.time() - self.level_transition_time
            if elapsed >= self.level_countdown_duration:
                self.state = GameState.PLAYING
            # Update celebration particles during transition
            if not self.test_mode:
                now = time.time()
                dt = now - self._last_update_time
                self._last_update_time = now
                self.particle_system.update(dt)
            return

        if self.state not in (GameState.PLAYING,):
            return

        current_time = time.time()

        # Update thrust animation
        self.thrust_frame = 1 - self.thrust_frame

        # Update flash effect
        if self.flash_active and current_time >= self.flash_end_time:
            self.flash_active = False

        # Update screen shake
        if current_time < self.shake_end_time:
            remaining = self.shake_end_time - current_time
            intensity = remaining / self.shake_duration  # Decay from 1 to 0
            self.shake_offset_x = random.choice([-1, 0, 1]) if intensity > 0.3 else 0
            self.shake_offset_y = random.choice([-1, 0, 1]) if intensity > 0.3 else 0
        else:
            self.shake_offset_x = 0
            self.shake_offset_y = 0

        # Update alien animation
        if current_time - self.last_animation_time >= 0.5:
            self.alien_animation_frame = 1 - self.alien_animation_frame
            self.last_animation_time = current_time

        # Move aliens (speed increases as more aliens die — classic SI acceleration)
        effective_interval = self.get_effective_move_interval()
        if current_time - self.last_alien_move_time >= effective_interval:
            self._move_aliens()
            self.last_alien_move_time = current_time

        # Update projectiles
        self._update_projectiles()

        # Alien firing
        self._alien_fire()

        # Dive-bomb attacks (aliens break formation)
        self._trigger_dive_bomb()
        self._update_diving_aliens()

        # Update mystery ship
        self._update_mystery_ship(current_time)

        # Update boss alien
        self._update_boss(current_time)

        # Check collisions
        self._check_collisions()

        # Update weapon level based on kills
        self._update_weapon_level()

        # Check score milestones for bonus lives
        self._check_score_milestones()

        # Update power-ups
        self._update_power_ups(current_time)

        # Update collectibles
        self._update_collectibles(current_time)

        # Update particles
        dt = current_time - self._last_update_time
        self._last_update_time = current_time
        if not self.test_mode:
            self.particle_system.update(dt)

        # Scroll starfield downward
        if not self.test_mode:
            scroll = self.star_scroll_speed * dt
            self.stars = [
                (x, y + scroll if y + scroll < self.height else y + scroll - self.height, c)
                for x, y, c in self.stars
            ]

        # Update bunker flash effects
        for bunker in self.bunkers:
            if bunker.flash_frames > 0:
                bunker.flash_frames -= 1

        # Update dying alien animations
        for da in self.dying_aliens[:]:
            da.advance()
            if da.finished:
                self.dying_aliens.remove(da)

        # Update score popups (float upward, cull expired)
        for popup in self.score_popups[:]:
            popup.update(dt)
            if popup.finished:
                self.score_popups.remove(popup)

        # Update ripple effects (fade and cull)
        for ripple in self.ripple_effects[:]:
            ripple.update(dt)
            if ripple.finished:
                self.ripple_effects.remove(ripple)

        # Update combo text (age and cull when expired)
        if self.combo_text:
            self.combo_text.update(dt)
            if self.combo_text.finished:
                self.combo_text = None

        # Check invasion
        self.check_invasion()

        # Update marching beat (speeds up as aliens die - iconic Space Invaders sound)
        if self.sfx and self.aliens:
            self.sfx.update_march(len(self.aliens), self.initial_alien_count or 1)

        # Check wave/level complete (all aliens dead AND boss dead if boss level)
        boss_alive = self.boss and self.boss.is_alive()
        if not self.aliens and not boss_alive:
            if self.current_wave < self.max_waves:
                self._next_wave()
            else:
                self._next_level()

    def _move_aliens(self) -> None:
        """Move alien formation."""
        if not self.aliens:
            return

        # Check bounds
        min_x = min(a.x for a in self.aliens)
        max_x = max(a.x for a in self.aliens)

        # Change direction at edges
        if max_x >= self.width - 4 and self.alien_direction > 0:
            self.alien_direction = -1
            max_y = max(a.y for a in self.aliens)
            for alien in self.aliens:
                alien.y += 1
            # Trigger ripple at the formation's new lowest row
            self.ripple_effects.append(RippleEffect(y=max_y + 1))
        elif min_x <= 2 and self.alien_direction < 0:
            self.alien_direction = 1
            max_y = max(a.y for a in self.aliens)
            for alien in self.aliens:
                alien.y += 1
            self.ripple_effects.append(RippleEffect(y=max_y + 1))
        else:
            for alien in self.aliens:
                alien.x += self.alien_direction
                alien.base_x += self.alien_direction

        # Apply zigzag oscillation for zigzag-behavior aliens
        for alien in self.aliens:
            if alien.behavior == ALIEN_BEHAVIOR_ZIGZAG:
                alien.zigzag_phase += 0.3
                alien.zigzag_offset = math.sin(alien.zigzag_phase) * 1.5
                alien.x = int(alien.base_x + alien.zigzag_offset)

    def _update_mystery_ship(self, current_time: float) -> None:
        """Spawn and update the mystery ship (UFO)."""
        # Clear expired score display
        if self.mystery_score_display and current_time >= self.mystery_score_display[3]:
            self.mystery_score_display = None

        if self.mystery_ship and self.mystery_ship.active:
            # Move the ship and advance blink animation
            self.mystery_ship.x += self.mystery_ship.speed
            self.mystery_ship.advance_blink()
            # Despawn at screen edge (wider sprite = ±5)
            if self.mystery_ship.x < -5 or self.mystery_ship.x > self.width + 5:
                self.mystery_ship = None
        else:
            # Random spawn check (~once per 25 seconds at 60fps)
            # Probability per frame: 1/(25*60) ≈ 0.000667
            if current_time - self.last_mystery_spawn_check >= 1.0:
                self.last_mystery_spawn_check = current_time
                if random.random() < 1.0 / 25.0:
                    direction = random.choice([-1, 1])
                    start_x = -5.0 if direction == 1 else float(self.width + 5)
                    points = random.choice([50, 100, 150, 200, 250, 300])
                    self.mystery_ship = MysteryShip(x=start_x, y=2, speed=0.5 * direction, points=points)

    def _spawn_power_up(self, x: int, y: int) -> None:
        """Possibly spawn a power-up at the given position (10% chance)."""
        if random.random() < 0.1:
            power_type = random.choice(list(PowerUpType))
            self.power_ups.append(PowerUp(x=x, y=float(y), power_type=power_type))

    def _spawn_collectible(self, x: int, y: int) -> None:
        """Possibly spawn a collectible coin/gem at the given position."""
        if random.random() < COLLECTIBLE_DROP_CHANCE:
            # Weighted: bronze 60%, silver 30%, gold 10%
            roll = random.random()
            if roll < 0.1:
                coin_type = "gold"
            elif roll < 0.4:
                coin_type = "silver"
            else:
                coin_type = "bronze"
            self.collectibles.append(
                Collectible(x=x, y=float(y), coin_type=coin_type, spawn_time=time.time())
            )

    def _update_power_ups(self, current_time: float) -> None:
        """Move falling power-ups and check collection; expire active effects."""
        # Move falling power-ups
        for pu in self.power_ups[:]:
            pu.y += pu.speed
            if pu.y >= self.height:
                self.power_ups.remove(pu)
                continue
            # Check player collection
            if abs(pu.x - self.player.x - 1) <= 1 and abs(pu.y - self.player.y) <= 1:
                self.power_ups.remove(pu)
                self._activate_power_up(pu.power_type, current_time)

        # Expire active power-ups
        self.active_power_ups = [ap for ap in self.active_power_ups if ap.expires_at > current_time]

    def _update_collectibles(self, current_time: float) -> None:
        """Move falling collectibles, check collection, expire old ones."""
        for coll in self.collectibles[:]:
            coll.y += coll.speed
            # Remove if off screen or expired
            if coll.y >= self.height or coll.is_expired(current_time):
                self.collectibles.remove(coll)
                continue
            # Check player collection
            if abs(coll.x - self.player.x - 1) <= 1 and abs(coll.y - self.player.y) <= 1:
                self.collectibles.remove(coll)
                self.score += coll.points
                self.score_popups.append(
                    ScorePopup(x=float(coll.x), y=float(coll.y), text=f"+{coll.points}")
                )

    def _activate_power_up(self, power_type: PowerUpType, current_time: float) -> None:
        """Activate a collected power-up."""
        if power_type == PowerUpType.BOMB:
            # Instant use: clear all alien projectiles
            cleared = len(self.alien_projectiles)
            self.alien_projectiles.clear()
            self.bomb_clear_count = cleared
            self.bomb_flash_until = current_time + 0.2  # Brief screen flash
        elif power_type == PowerUpType.SHIELD:
            # Shield is a one-time use, set a long expiry
            self.active_power_ups.append(ActivePowerUp(power_type=power_type, expires_at=current_time + 999))
        elif power_type == PowerUpType.BULLET_TIME:
            # Bullet time: 4-second slow motion
            self.active_power_ups.append(ActivePowerUp(power_type=power_type, expires_at=current_time + BULLET_TIME_DURATION))
        else:
            # Timed effects last 5 seconds
            self.active_power_ups.append(ActivePowerUp(power_type=power_type, expires_at=current_time + 5.0))

    def has_power_up(self, power_type: PowerUpType) -> bool:
        """Check if a specific power-up is currently active."""
        return any(ap.power_type == power_type for ap in self.active_power_ups)

    def _consume_shield(self) -> None:
        """Remove the shield power-up (used on hit)."""
        self.active_power_ups = [ap for ap in self.active_power_ups if ap.power_type != PowerUpType.SHIELD]

    def get_combo_multiplier(self) -> int:
        """Return the current combo multiplier (1x to 5x)."""
        if self.combo_count < 2:
            return 1
        return min(self.combo_count, self.combo_max)

    def get_shield_aura_info(self) -> Optional[dict]:
        """Return shield aura display info (testable without curses).

        Returns None if shield is not active, otherwise:
            left_bracket: "(" char at player.x - 1
            right_bracket: ")" char at player.x + 3 (after 3-char sprite)
            player_x: player x position
            player_y: player y position
            pulse: bool (alternating for visual pulse effect)
        """
        if not self.has_power_up(PowerUpType.SHIELD):
            return None
        return {
            "left_bracket": "(",
            "right_bracket": ")",
            "player_x": self.player.x,
            "player_y": self.player.y,
            "pulse": self.thrust_frame == 0,
        }

    def get_combo_display(self) -> Optional[dict]:
        """Return combo text display info (testable without curses).

        Returns None if no combo text is active, otherwise a dict:
            text: The combo string (e.g. "COMBO x3!")
            progress: Float 0.0 to 1.0 (age / lifetime)
            center_x: Column for centering on screen
            center_y: Row at screen center
        """
        if not self.combo_text:
            return None
        progress = self.combo_text.age / self.combo_text.lifetime if self.combo_text.lifetime > 0 else 1.0
        return {
            "text": self.combo_text.text,
            "progress": progress,
            "center_x": (self.width - len(self.combo_text.text)) // 2,
            "center_y": self.height // 2,
        }

    def _register_kill(self, current_time: float) -> int:
        """Register an alien kill for combo tracking. Returns the multiplier."""
        if current_time - self.combo_last_kill_time <= self.combo_window:
            self.combo_count += 1
        else:
            self.combo_count = 1
        self.combo_last_kill_time = current_time
        multiplier = self.get_combo_multiplier()
        # Spawn center-screen combo text for multipliers >= 2
        if multiplier >= 2:
            self.combo_text = ComboText(text=f"COMBO x{multiplier}!")
        return multiplier

    def _update_projectiles(self) -> None:
        """Update all projectile positions."""
        # Player projectiles move up (faster with weapon speed bonus)
        speed_mult = WEAPON_SPEED_BONUS.get(self.weapon_level, 1.0)
        for proj in self.player_projectiles[:]:
            proj.record_position()
            proj.y -= self.config.player_projectile_speed * speed_mult
            if proj.y < 0:
                self.player_projectiles.remove(proj)

        # Alien projectiles move down (slower for easier dodging)
        bt_mult = BULLET_TIME_SPEED_MULT if self.has_power_up(PowerUpType.BULLET_TIME) else 1.0
        for proj in self.alien_projectiles[:]:
            type_speed = ALIEN_PROJ_TYPES.get(proj.proj_type, ALIEN_PROJ_TYPES["normal"])["speed_mult"]
            proj.y += self.config.alien_projectile_speed * type_speed * bt_mult
            if proj.y >= self.height:
                self.alien_projectiles.remove(proj)

    def _alien_fire(self) -> None:
        """Handle alien firing logic."""
        if not self.aliens:
            return

        fire_prob = self.get_alien_fire_probability()
        proj_types = list(ALIEN_PROJ_WEIGHTS.keys())
        proj_weights = list(ALIEN_PROJ_WEIGHTS.values())

        for alien in self.aliens:
            if random.random() < fire_prob:
                pt = random.choices(proj_types, weights=proj_weights, k=1)[0]
                self.alien_projectiles.append(
                    Projectile(x=alien.x + 1, y=alien.y + 1, direction=1, proj_type=pt)
                )

    def _trigger_dive_bomb(self) -> None:
        """Possibly start a dive-bomb attack from a random alien."""
        if self.level < DIVE_BOMB_MIN_LEVEL or not self.aliens:
            return
        # Only one dive-bomb at a time
        if self.diving_aliens:
            return
        if random.random() < DIVE_BOMB_CHANCE:
            alien = random.choice(self.aliens)
            # Calculate horizontal direction toward player
            dx = 0.3 if self.player.x > alien.x else -0.3
            self.diving_aliens.append(DivingAlien(alien=alien, dx=dx))
            # Remove from formation so it moves independently
            self.aliens.remove(alien)

    def _update_diving_aliens(self) -> None:
        """Move diving aliens diagonally toward the player."""
        for diver in self.diving_aliens[:]:
            diver.float_x += diver.dx
            diver.float_y += diver.dy
            diver.alien.x = int(diver.float_x)
            diver.alien.y = int(diver.float_y)
            # Off screen: remove
            if diver.float_y >= self.height or diver.float_x < 0 or diver.float_x >= self.width:
                self.diving_aliens.remove(diver)

    def get_diving_aliens_info(self) -> list:
        """Return diving alien positions for display/testing."""
        return [{"x": d.alien.x, "y": d.alien.y, "dx": d.dx, "dy": d.dy} for d in self.diving_aliens]

    def _update_boss(self, current_time: float) -> None:
        """Update boss alien movement and firing."""
        if not self.boss or not self.boss.is_alive():
            return

        # Move boss left-right
        self.boss.x += self.boss.direction * self.boss.speed
        sprite_len = len(BOSS_SPRITE)
        if self.boss.x <= 1:
            self.boss.direction = 1
        elif self.boss.x + sprite_len >= self.width - 1:
            self.boss.direction = -1

        # Boss fires spread shots (3 projectiles)
        if current_time - self.boss.last_fire_time >= BOSS_FIRE_INTERVAL:
            self.boss.last_fire_time = current_time
            cx = int(self.boss.x) + sprite_len // 2
            for offset in [-2, 0, 2]:
                self.alien_projectiles.append(
                    Projectile(x=cx + offset, y=self.boss.y + 1, direction=1)
                )

    def _check_collisions(self) -> None:
        """Check all collision types using spatial partitioning.

        Uses a SpatialGrid to reduce collision checks from O(n*m) to
        roughly O(n+m) by only checking entities in nearby grid cells.
        """
        current_time = time.time()

        # Build spatial grid for aliens
        alien_grid = SpatialGrid(self.width, self.height, cell_size=4)
        for alien in self.aliens:
            alien_grid.insert(alien, alien.x, alien.y)

        # Build spatial grid for bunkers (only alive ones)
        bunker_grid = SpatialGrid(self.width, self.height, cell_size=4)
        for bunker in self.bunkers:
            if bunker.health > 0:
                bunker_grid.insert(bunker, bunker.x, bunker.y)

        # Player projectiles vs aliens (spatial lookup)
        for proj in self.player_projectiles[:]:
            nearby_aliens = alien_grid.query_nearby(proj.x, proj.y)
            for alien in nearby_aliens:
                if alien not in self.aliens:
                    continue
                if abs(proj.x - alien.x) <= 1 and abs(proj.y - alien.y) <= 1:
                    if proj in self.player_projectiles:
                        self.player_projectiles.remove(proj)
                    killed = alien.take_hit()
                    if not killed:
                        # Alien survived (shielded) — just absorb the projectile
                        break
                    self.aliens.remove(alien)
                    self.total_kills += 1
                    multiplier = self._register_kill(current_time)
                    points = 10 * (3 - alien.alien_type) * multiplier
                    self.score += points
                    # Score popup floats upward from kill position
                    popup_text = f"+{points}"
                    if multiplier > 1:
                        popup_text += f" x{multiplier}!"
                    self.score_popups.append(
                        ScorePopup(x=float(alien.x), y=float(alien.y), text=popup_text)
                    )
                    self._spawn_power_up(alien.x, alien.y)
                    self._spawn_collectible(alien.x, alien.y)
                    # Add dying alien animation
                    self.dying_aliens.append(DyingAlien(x=alien.x, y=alien.y))
                    # Spawn type-specific explosion particles at alien death position
                    if not self.test_mode:
                        ecfg = get_explosion_config(alien.alien_type)
                        self.particle_system.spawn(
                            x=float(alien.x),
                            y=float(alien.y),
                            count=random.randint(*ecfg["count"]),
                            chars=ecfg["chars"],
                            color_pair=ALIEN_TYPE_COLORS.get(alien.alien_type, COLOR_ALIEN),
                            speed_range=ecfg["speed"],
                            lifetime_range=ecfg["lifetime"],
                        )
                    self.event_bus.publish(GameEvent.ALIEN_KILLED, alien_type=alien.alien_type)
                    break

        # Alien projectiles vs player (single entity, no grid needed)
        for proj in self.alien_projectiles[:]:
            if abs(proj.x - self.player.x - 1) <= 1 and proj.y >= self.player.y:
                self.alien_projectiles.remove(proj)
                self.handle_player_damage()
                break

        # Diving aliens vs player (body collision)
        for diver in self.diving_aliens[:]:
            if abs(diver.alien.x - self.player.x - 1) <= 1 and abs(diver.alien.y - self.player.y) <= 1:
                self.diving_aliens.remove(diver)
                self.handle_player_damage()
                break

        # Player projectiles vs diving aliens
        for proj in self.player_projectiles[:]:
            for diver in self.diving_aliens[:]:
                if abs(proj.x - diver.alien.x) <= 1 and abs(proj.y - diver.alien.y) <= 1:
                    pts = (diver.alien.alien_type + 1) * 10
                    self.score += pts
                    self.total_kills += 1
                    self.score_popups.append(
                        ScorePopup(x=float(diver.alien.x), y=float(diver.alien.y), text=f"+{pts}")
                    )
                    self.diving_aliens.remove(diver)
                    if proj in self.player_projectiles:
                        self.player_projectiles.remove(proj)
                    break

        # Player projectiles vs bunkers (spatial lookup)
        for proj in self.player_projectiles[:]:
            nearby_bunkers = bunker_grid.query_nearby(proj.x, proj.y)
            for bunker in nearby_bunkers:
                if bunker.health > 0 and proj.x == bunker.x and proj.y == bunker.y:
                    bunker.hit()
                    # Spawn debris particles falling downward
                    if not self.test_mode:
                        self.particle_system.spawn(
                            x=float(bunker.x), y=float(bunker.y),
                            count=random.randint(1, 2), chars=".,",
                            color_pair=bunker.color_pair,
                            speed_range=(0.5, 2.0), lifetime_range=(0.3, 0.5),
                        )
                    if proj in self.player_projectiles:
                        self.player_projectiles.remove(proj)
                    break

        # Alien projectiles vs bunkers (spatial lookup)
        for proj in self.alien_projectiles[:]:
            nearby_bunkers = bunker_grid.query_nearby(proj.x, proj.y)
            for bunker in nearby_bunkers:
                if bunker.health > 0 and proj.x == bunker.x and proj.y == bunker.y:
                    damage = ALIEN_PROJ_TYPES.get(proj.proj_type, ALIEN_PROJ_TYPES["normal"])["bunker_damage"]
                    for _ in range(damage):
                        if bunker.health > 0:
                            bunker.hit()
                    # Spawn debris particles falling downward
                    if not self.test_mode:
                        self.particle_system.spawn(
                            x=float(bunker.x), y=float(bunker.y),
                            count=random.randint(1, 2), chars=".,",
                            color_pair=bunker.color_pair,
                            speed_range=(0.5, 2.0), lifetime_range=(0.3, 0.5),
                        )
                    if proj in self.alien_projectiles:
                        self.alien_projectiles.remove(proj)
                    break

        # Player projectiles vs mystery ship (wider sprite = ±3 hit range)
        if self.mystery_ship and self.mystery_ship.active:
            for proj in self.player_projectiles[:]:
                if abs(proj.x - self.mystery_ship.x) <= 3 and abs(proj.y - self.mystery_ship.y) <= 1:
                    pts = self.mystery_ship.points
                    self.score += pts
                    self.total_kills += 1
                    # Score popup for mystery ship
                    self.score_popups.append(
                        ScorePopup(x=float(self.mystery_ship.x), y=float(self.mystery_ship.y), text=f"+{pts}")
                    )
                    self.mystery_score_display = (int(self.mystery_ship.x), self.mystery_ship.y, pts, time.time() + 1.0)
                    # Large explosion (10-15 particles, wider spread)
                    if not self.test_mode:
                        self.particle_system.spawn(
                            x=float(self.mystery_ship.x),
                            y=float(self.mystery_ship.y),
                            count=random.randint(10, 15),
                            chars="*#@+.'`",
                            color_pair=COLOR_GAME_OVER,
                            speed_range=(3.0, 8.0),
                            lifetime_range=(0.5, 0.8),
                        )
                    self.mystery_ship = None
                    if proj in self.player_projectiles:
                        self.player_projectiles.remove(proj)
                    break

        # Player projectiles vs boss alien (wide hitbox ±5)
        if self.boss and self.boss.is_alive():
            boss_cx = int(self.boss.x) + len(BOSS_SPRITE) // 2
            for proj in self.player_projectiles[:]:
                if abs(proj.x - boss_cx) <= 5 and abs(proj.y - self.boss.y) <= 1:
                    if proj in self.player_projectiles:
                        self.player_projectiles.remove(proj)
                    remaining = self.boss.take_hit()
                    if remaining <= 0:
                        # Boss defeated
                        multiplier = self._register_kill(time.time())
                        points = BOSS_POINTS * (self.level // 5) * multiplier
                        self.score += points
                        self.total_kills += 1
                        self.score_popups.append(
                            ScorePopup(x=float(self.boss.x), y=float(self.boss.y), text=f"+{points}")
                        )
                        if not self.test_mode:
                            self.particle_system.spawn(
                                x=float(boss_cx), y=float(self.boss.y),
                                count=20, chars="*#@+X'`",
                                color_pair=COLOR_GAME_OVER,
                                speed_range=(4.0, 10.0),
                                lifetime_range=(0.6, 1.0),
                            )
                        self.boss = None
                    break

    def get_scaled_alien_rows(self) -> int:
        """Return alien rows for the current level (extra row every 3 levels, max 8)."""
        extra = (self.level - 1) // 3
        return min(self.config.alien_rows + extra, 8)

    def get_current_formation(self) -> str:
        """Return the formation pattern name for the current level."""
        return get_formation_for_level(self.level)

    def is_boss_level(self) -> bool:
        """Return True if the current level is a boss level (every 5 levels)."""
        return self.level >= 5 and self.level % 5 == 0

    def get_boss_info(self) -> Optional[dict]:
        """Return boss state info for display/testing, or None if no boss."""
        if not self.boss:
            return None
        return {
            "x": self.boss.x,
            "y": self.boss.y,
            "hp": self.boss.hp,
            "max_hp": self.boss.max_hp,
            "hp_bar": self.boss.get_hp_bar(),
            "sprite": self.boss.get_display(),
            "alive": self.boss.is_alive(),
        }

    def get_effective_move_interval(self) -> float:
        """Return the current alien move interval with SI-style acceleration.

        As aliens are destroyed, remaining aliens move faster.
        At full count: base interval. At 1 alien: 20% of base interval.
        """
        total = self.initial_alien_count or 1
        remaining = max(len(self.aliens), 1)
        # Linear interpolation: ratio goes from 1.0 (all alive) to ~0.0 (all dead)
        ratio = remaining / total
        # Scale factor: 1.0 at full → 0.2 at nearly empty
        speed_factor = 0.2 + 0.8 * ratio
        interval = max(0.05, self.alien_move_interval * speed_factor)
        # Bullet time: stretch interval (aliens move slower)
        if self.has_power_up(PowerUpType.BULLET_TIME):
            interval /= BULLET_TIME_SPEED_MULT
        return interval

    def get_wave_info(self) -> dict:
        """Return current wave state for display/testing."""
        return {
            "current_wave": self.current_wave,
            "max_waves": self.max_waves,
            "is_final_wave": self.current_wave >= self.max_waves,
        }

    def get_weapon_info(self) -> dict:
        """Return weapon upgrade state for display/testing."""
        kills_for_next = WEAPON_KILLS_PER_LEVEL * self.weapon_level
        pattern = WEAPON_PATTERNS.get(self.weapon_level, WEAPON_PATTERNS[1])
        return {
            "level": self.weapon_level,
            "max_level": WEAPON_MAX_LEVEL,
            "shots_per_fire": len(pattern),
            "speed_bonus": WEAPON_SPEED_BONUS.get(self.weapon_level, 1.0),
            "kills_to_next": max(0, kills_for_next - self.total_kills),
            "is_maxed": self.weapon_level >= WEAPON_MAX_LEVEL,
        }

    def _update_weapon_level(self) -> None:
        """Recalculate weapon level based on total kills."""
        new_level = min(WEAPON_MAX_LEVEL, 1 + self.total_kills // WEAPON_KILLS_PER_LEVEL)
        self.weapon_level = max(1, new_level)

    def _check_score_milestones(self) -> None:
        """Award bonus life for each score milestone reached."""
        for milestone in SCORE_MILESTONES:
            if self.score >= milestone and milestone not in self.milestones_reached:
                self.milestones_reached.append(milestone)
                if self.player.lives < self.config.max_lives:
                    self.player.lives += 1
                    self.score_popups.append(
                        ScorePopup(x=float(self.width // 2), y=float(self.height // 2), text="+1 LIFE!")
                    )

    def get_milestone_info(self) -> dict:
        """Return milestone status for display/testing."""
        next_milestone = None
        for m in SCORE_MILESTONES:
            if m not in self.milestones_reached:
                next_milestone = m
                break
        return {
            "reached": list(self.milestones_reached),
            "next": next_milestone,
            "total_milestones": len(SCORE_MILESTONES),
        }

    def get_special_alien_counts(self) -> dict:
        """Return counts of special aliens for display/testing."""
        zigzag = sum(1 for a in self.aliens if a.behavior == ALIEN_BEHAVIOR_ZIGZAG)
        shielded = sum(1 for a in self.aliens if a.behavior == ALIEN_BEHAVIOR_SHIELDED)
        return {"zigzag": zigzag, "shielded": shielded, "normal": len(self.aliens) - zigzag - shielded}

    def get_collectibles_info(self) -> list:
        """Return collectible state for display/testing."""
        return [
            {"x": c.x, "y": c.y, "type": c.coin_type, "points": c.points, "char": c.char}
            for c in self.collectibles
        ]

    def is_last_stand(self) -> bool:
        """Return True if player is at exactly 1 life (last stand buff active)."""
        return self.player.lives == 1

    def get_last_stand_info(self) -> dict:
        """Return last stand state for display/testing."""
        active = self.is_last_stand()
        return {
            "active": active,
            "speed_mult": LAST_STAND_SPEED_MULT if active else 1,
            "fire_slot_mult": LAST_STAND_FIRE_SLOT_MULT if active else 1,
        }

    def get_scaled_bunker_health(self) -> int:
        """Return starting bunker health for the current level (reduced every 5 levels, min 1)."""
        reduction = (self.level - 1) // 5
        return max(1, 3 - reduction)

    def _next_wave(self) -> None:
        """Advance to the next wave within the current level.

        Respawns aliens with slightly increased speed, keeps bunkers and score.
        """
        self.current_wave += 1
        # Escalate difficulty: 5% speed increase per wave
        wave_scale = 0.95 ** (self.current_wave - 1)
        self.alien_move_interval = max(0.1, self.alien_move_interval * wave_scale)
        # Respawn aliens (keeps bunkers intact)
        self._init_aliens()
        # Small score bonus for clearing a wave
        wave_bonus = 50 * self.current_wave
        self.score += wave_bonus
        self.score_popups.append(
            ScorePopup(
                x=float(self.width // 2),
                y=float(self.height // 2),
                text=f"WAVE {self.current_wave}! +{wave_bonus}",
            )
        )

    def _next_level(self) -> None:
        """Advance to next level and award bonus lives."""
        # Award lives based on level completed (level 1 = +1, level 2 = +2, etc.)
        completed_level = self.level
        self.lives_awarded = completed_level
        lives_to_add = min(completed_level, self.config.max_lives - self.player.lives)
        self.player.lives = min(self.player.lives + lives_to_add, self.config.max_lives)

        # Notify subscribers
        self.event_bus.publish(GameEvent.LEVEL_COMPLETE)
        if self.sfx and lives_to_add > 0:
            self.sfx.play_life_bonus()

        self.level += 1
        self.current_wave = 1  # Reset wave counter for new level
        self.state = GameState.LEVEL_TRANSITION
        self.level_transition_time = time.time()

        # Green flash for level complete (0.15s)
        self.flash_active = True
        self.flash_color = COLOR_PLAYER  # Green
        self.flash_end_time = time.time() + 0.15

        # Victory celebration particle burst
        if not self.test_mode:
            cx = float(self.width // 2)
            cy = float(self.height // 2)
            for color in [COLOR_PLAYER, COLOR_TEXT, COLOR_ALIEN_TYPE_0]:
                self.particle_system.spawn(
                    x=cx, y=cy, count=8, chars="*+.~!",
                    color_pair=color, speed_range=(3.0, 10.0),
                    lifetime_range=(0.5, 1.2),
                )

        self._init_aliens()
        self._init_bunkers()

        # Difficulty scaling: speed up aliens (+10% per level)
        scale = 0.9 ** (self.level - 1)  # Each level multiplies interval by 0.9
        self.alien_move_interval = max(0.1, self.config.alien_move_interval * scale)

    def _toggle_pause(self) -> None:
        """Toggle between PLAYING and PAUSED states, adjusting timers."""
        if self.state == GameState.PLAYING:
            self.state = GameState.PAUSED
            self.pause_start_time = time.time()
        elif self.state == GameState.PAUSED:
            # Offset all time-based state by the duration of the pause
            pause_duration = time.time() - self.pause_start_time
            self.last_animation_time += pause_duration
            self.last_alien_move_time += pause_duration
            self.last_mystery_spawn_check += pause_duration
            if self.flash_end_time > 0:
                self.flash_end_time += pause_duration
            if self.mystery_score_display:
                x, y, pts, end = self.mystery_score_display
                self.mystery_score_display = (x, y, pts, end + pause_duration)
            self.state = GameState.PLAYING

    def handle_resize(self, new_width: int, new_height: int) -> None:
        """Handle terminal resize by recalculating positions and checking bounds.

        Args:
            new_width: New terminal width.
            new_height: New terminal height.
        """
        self.width = new_width
        self.height = new_height

        # Check minimum size
        self.too_small = new_width < self.config.min_width or new_height < self.config.min_height

        if self.too_small:
            return

        # Clamp player position to new bounds
        self.player.x = max(0, min(self.player.x, self.width - 3))
        self.player.y = self.height - 2
        self._initial_player_x = self.width // 2 - 1

        # Remove projectiles that are now outside bounds
        self.player_projectiles = [
            p for p in self.player_projectiles if 0 <= p.x < self.width and 0 <= p.y < self.height
        ]
        self.alien_projectiles = [p for p in self.alien_projectiles if 0 <= p.x < self.width and 0 <= p.y < self.height]

        # Regenerate starfield for new dimensions
        self._generate_starfield()

        # Clamp alien positions
        for alien in self.aliens:
            alien.x = max(0, min(alien.x, self.width - 4))
            alien.y = max(0, min(alien.y, self.height - 3))

    def handle_input(self, key: int) -> bool:
        """
        Handle keyboard input.

        Returns:
            False if game should quit, True otherwise.
        """
        if key == ord("q") or key == ord("Q"):
            return False

        # Handle terminal resize
        if key == curses.KEY_RESIZE:
            if self.screen:
                new_h, new_w = self.screen.getmaxyx()
                self.handle_resize(new_w, new_h)
            return True

        # Toggle FPS display with F1
        if key == curses.KEY_F1:
            self.show_fps = not self.show_fps
            return True

        if self.state == GameState.MENU:
            if self.menu_screen == MenuScreen.MAIN:
                if key == curses.KEY_UP or key == ord("w"):
                    self.menu_selection = (self.menu_selection - 1) % len(MENU_ITEMS)
                elif key == curses.KEY_DOWN or key == ord("s"):
                    self.menu_selection = (self.menu_selection + 1) % len(MENU_ITEMS)
                elif key == ord(" ") or key == ord("\n"):
                    self.menu_flash_until = time.time() + 0.15
                    label, target = MENU_ITEMS[self.menu_selection]
                    if label == "Play":
                        self.state = GameState.PLAYING
                        if self.audio:
                            self.audio.start()
                    elif label == "Quit":
                        return False
                    elif target is not None:
                        self.menu_screen = target
                        if target == MenuScreen.CREDITS:
                            self.credits_scroll_offset = 0.0
            elif self.menu_screen == MenuScreen.OPTIONS:
                if key == 27 or key == curses.KEY_BACKSPACE or key == 127:
                    self.menu_screen = MenuScreen.MAIN
                elif key == curses.KEY_UP or key == ord("w"):
                    self.options_selection = (self.options_selection - 1) % 4
                elif key == curses.KEY_DOWN or key == ord("s"):
                    self.options_selection = (self.options_selection + 1) % 4
                elif key == ord(" ") or key == ord("\n") or key == curses.KEY_LEFT or key == curses.KEY_RIGHT:
                    self.toggle_option(self.options_selection)
            else:
                # Sub-screen: Escape or Backspace returns to main menu
                if key == 27 or key == curses.KEY_BACKSPACE or key == 127:
                    self.menu_screen = MenuScreen.MAIN

        elif self.state == GameState.PLAYING:
            if key == ord("p") or key == ord("P") or key == 27:  # P or Escape
                self._toggle_pause()
            elif key == curses.KEY_LEFT or key == ord("a"):
                speed = self.config.player_speed * (LAST_STAND_SPEED_MULT if self.is_last_stand() else 1)
                self.player.x = max(0, self.player.x - speed)
                self.player_move_direction = -1
            elif key == curses.KEY_RIGHT or key == ord("d"):
                speed = self.config.player_speed * (LAST_STAND_SPEED_MULT if self.is_last_stand() else 1)
                self.player.x = min(self.width - 3, self.player.x + speed)
                self.player_move_direction = 1
            elif key == ord(" "):
                # Fire projectile(s) based on weapon level; last stand doubles slots
                ls_mult = LAST_STAND_FIRE_SLOT_MULT if self.is_last_stand() else 1
                max_proj = (3 + self.weapon_level) * ls_mult
                if len(self.player_projectiles) < max_proj:
                    pattern = WEAPON_PATTERNS.get(self.weapon_level, WEAPON_PATTERNS[1])
                    cx = self.player.x + 1
                    for dx, dy in pattern:
                        self.player_projectiles.append(
                            Projectile(x=cx + dx, y=self.player.y - 1 + dy, direction=-1)
                        )
                    self.total_shots += 1
                    self.event_bus.publish(GameEvent.SHOT_FIRED)

        elif self.state == GameState.PAUSED:
            if key == ord("p") or key == ord("P") or key == 27:  # P or Escape
                self._toggle_pause()

        elif self.state == GameState.GAME_OVER:
            if key == ord("r") or key == ord("R"):
                self.reset_game()

        elif self.state == GameState.LEVEL_TRANSITION:
            if key == ord(" ") or key == ord("\n"):
                self.state = GameState.PLAYING

        return True

    def render(self) -> None:
        """Render the game to the screen."""
        if self.test_mode or not self.screen:
            return

        self.screen.clear()

        # Show warning if terminal is too small
        if self.too_small:
            warning = f"Terminal too small! Need {self.config.min_width}x{self.config.min_height}"
            try:
                self.screen.addstr(0, 0, warning[: self.width - 1])
            except curses.error:
                pass
            self.screen.refresh()
            return

        # Handle flash effect (red for damage, green for level complete)
        if self.flash_active:
            self.screen.bkgd(" ", curses.color_pair(self.flash_color))
        elif self.state == GameState.PLAYING and self.aliens:
            # Progressive background color shift as aliens descend
            danger = self.get_descent_danger_ratio()
            if danger >= 0.75:
                self.screen.bkgd(" ", curses.color_pair(COLOR_BG_DANGER_HIGH) | curses.A_BOLD)
            elif danger >= 0.5:
                self.screen.bkgd(" ", curses.color_pair(COLOR_BG_DANGER_LOW) | curses.A_DIM)
            else:
                self.screen.bkgd(" ")
        else:
            self.screen.bkgd(" ")

        if self.state == GameState.MENU:
            self._render_menu()
        elif self.state == GameState.PLAYING:
            self._render_game()
        elif self.state == GameState.PAUSED:
            self._render_game()
            self._render_pause_overlay()
        elif self.state == GameState.GAME_OVER:
            self._render_game_over()
        elif self.state == GameState.LEVEL_TRANSITION:
            self._render_level_transition()

        self.screen.refresh()

    def get_title_layout(self) -> dict:
        """Return title screen layout data (testable without curses).

        Returns dict with keys:
            art_lines: list of (row, col, text) for each ASCII art line
            subtitle: (row, col, text)
            controls: (row, col, text)
            color_index: current color cycle index
        """
        art = TITLE_ART
        art_height = len(art)
        # Center the art block vertically, with subtitle and controls below
        art_top = self.height // 2 - art_height // 2 - 2

        art_lines = []
        for i, line in enumerate(art):
            if line:
                col = (self.width - len(line)) // 2
                art_lines.append((art_top + i, col, line))

        subtitle = "Press SPACE to Start"
        controls = "A/D or Arrows = Move | SPACE = Fire | Q = Quit"

        sub_row = art_top + art_height + 1
        ctrl_row = sub_row + 2

        color_idx = self.title_color_frame % len(TITLE_COLOR_CYCLE)

        return {
            "art_lines": art_lines,
            "subtitle": (sub_row, (self.width - len(subtitle)) // 2, subtitle),
            "controls": (ctrl_row, (self.width - len(controls)) // 2, controls),
            "color_index": color_idx,
        }

    def get_high_scores_display(self) -> dict:
        """Return high scores display data (testable without curses).

        Returns dict with:
            title: "HIGH SCORES"
            rows: list of dicts with rank, score, level, date
            empty: True if no scores recorded
        """
        scores = self.score_manager.scores if self.score_manager else []
        rows = []
        for i, entry in enumerate(scores[:10]):
            date_str = entry.get("date", "")
            # Parse ISO date and format as short date
            try:
                dt = datetime.fromisoformat(date_str)
                date_short = dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                date_short = "----"
            rows.append({
                "rank": i + 1,
                "score": entry.get("score", 0),
                "level": entry.get("level", 0),
                "date": date_short,
            })
        return {
            "title": "HIGH SCORES",
            "rows": rows,
            "empty": len(rows) == 0,
        }

    def get_menu_info(self) -> dict:
        """Return current menu state info (testable without curses).

        Returns dict with:
            screen: current MenuScreen
            items: list of (label, selected) tuples for MAIN screen
            selection: current selection index
        """
        items = [(label, i == self.menu_selection) for i, (label, _) in enumerate(MENU_ITEMS)]
        return {
            "screen": self.menu_screen,
            "items": items,
            "selection": self.menu_selection,
        }

    def get_menu_cursor_info(self) -> dict:
        """Return menu cursor animation state (testable without curses).

        Returns dict with:
            bob_offset: 0 or 1 for cursor bobbing effect
            flash_active: True if selection flash is active
            cursor_char: the cursor character ("> " or ">>")
        """
        # Bob every 15 frames (quarter-second at 60fps)
        bob = 1 if (self.menu_cursor_frame // 15) % 2 == 0 else 0
        flash = time.time() < self.menu_flash_until
        cursor = ">>" if flash else "> " if bob else " >"
        return {
            "bob_offset": bob,
            "flash_active": flash,
            "cursor_char": cursor,
        }

    def get_options_display(self) -> dict:
        """Return options screen display data (testable without curses).

        Returns dict with:
            title: "OPTIONS"
            items: list of dicts with label, value, selected
            selection: current selection index
        """
        items = [
            {"label": "Sound Effects", "value": "ON" if self.sound_enabled else "OFF",
             "selected": self.options_selection == 0},
            {"label": "Music", "value": "ON" if self.music_enabled else "OFF",
             "selected": self.options_selection == 1},
            {"label": "Show FPS", "value": "ON" if self.show_fps else "OFF",
             "selected": self.options_selection == 2},
            {"label": "Difficulty", "value": self._get_current_difficulty(),
             "selected": self.options_selection == 3},
        ]
        return {
            "title": "OPTIONS",
            "items": items,
            "selection": self.options_selection,
        }

    def _get_current_difficulty(self) -> str:
        """Return the current difficulty name based on config values."""
        for name, preset in DIFFICULTY_PRESETS.items():
            if not preset:
                continue
            if all(getattr(self.config, k, None) == v for k, v in preset.items()):
                return name.capitalize()
        return "Normal"

    def toggle_option(self, index: int) -> None:
        """Toggle the option at the given index."""
        if index == 0:
            self.sound_enabled = not self.sound_enabled
            if self.sfx:
                self.sfx.enabled = self.sound_enabled
        elif index == 1:
            self.music_enabled = not self.music_enabled
            if self.audio:
                if self.music_enabled:
                    self.audio.start()
                else:
                    self.audio.stop()
        elif index == 2:
            self.show_fps = not self.show_fps

    def get_credits_display(self) -> dict:
        """Return credits screen data (testable without curses).

        Returns dict with:
            lines: list of (row, text) tuples for visible credit lines
            scroll_offset: current scroll position
        """
        visible = []
        center_y = self.height // 2
        for i, line in enumerate(CREDITS_LINES):
            y = center_y + i - int(self.credits_scroll_offset)
            if 0 <= y < self.height:
                visible.append((y, line))
        return {
            "lines": visible,
            "scroll_offset": self.credits_scroll_offset,
        }

    def get_controls_display(self) -> dict:
        """Return controls help screen data (testable without curses).

        Returns dict with:
            title: "CONTROLS"
            keybindings: list of (key, action) tuples
            alien_points: list of (type_name, sprite, points) tuples
        """
        keybindings = [
            ("A / LEFT", "Move Left"),
            ("D / RIGHT", "Move Right"),
            ("SPACE", "Fire"),
            ("P / ESC", "Pause"),
            ("F1", "Toggle FPS"),
            ("Q", "Quit"),
            ("R", "Restart (Game Over)"),
        ]
        alien_points = [
            ("Top Row", ALIEN_CHARS[0][0], "30 pts"),
            ("Middle Row", ALIEN_CHARS[1][0], "20 pts"),
            ("Bottom Row", ALIEN_CHARS[2][0], "10 pts"),
            ("Mystery Ship", MYSTERY_SHIP_CHAR, "50-200 pts"),
        ]
        return {
            "title": "CONTROLS",
            "keybindings": keybindings,
            "alien_points": alien_points,
        }

    def _render_menu(self) -> None:
        """Render the animated title screen with ASCII art and color cycling."""
        layout = self.get_title_layout()
        cycle_len = len(TITLE_COLOR_CYCLE)

        # Render each art line with color cycling (offset per line for rainbow)
        for i, (row, col, text) in enumerate(layout["art_lines"]):
            color_pair = TITLE_COLOR_CYCLE[(layout["color_index"] + i) % cycle_len]
            self._safe_addstr(row, col, text, curses.color_pair(color_pair) | curses.A_BOLD)

        if self.menu_screen == MenuScreen.MAIN:
            # Render menu items below title art with animated cursor
            menu_info = self.get_menu_info()
            cursor_info = self.get_menu_cursor_info()
            sub_r, _, _ = layout["subtitle"]
            for i, (label, selected) in enumerate(menu_info["items"]):
                if selected:
                    prefix = cursor_info["cursor_char"]
                else:
                    prefix = "  "
                text = f"{prefix}{label}"
                col = (self.width - len(text)) // 2
                if selected and cursor_info["flash_active"]:
                    attr = curses.color_pair(COLOR_TEXT) | curses.A_BOLD | curses.A_REVERSE
                elif selected:
                    attr = curses.color_pair(COLOR_TEXT) | curses.A_BOLD
                else:
                    attr = curses.color_pair(COLOR_TEXT)
                self._safe_addstr(sub_r + i, col, text, attr)

            # Controls hint at bottom
            ctrl_r, ctrl_c, ctrl_text = layout["controls"]
            self._safe_addstr(ctrl_r, ctrl_c, ctrl_text, curses.color_pair(COLOR_TEXT) | curses.A_DIM)
        else:
            if self.menu_screen == MenuScreen.HIGH_SCORES:
                self._render_high_scores_screen()
            elif self.menu_screen == MenuScreen.OPTIONS:
                self._render_options_screen()
            elif self.menu_screen == MenuScreen.CONTROLS:
                self._render_controls_screen()
            elif self.menu_screen == MenuScreen.CREDITS:
                self._render_credits_screen()
            back_text = "Press ESC to return"
            if self.menu_screen == MenuScreen.CREDITS:
                back_y = self.height - 2  # Fixed at bottom for scrolling credits
            elif self.menu_screen == MenuScreen.HIGH_SCORES:
                back_y = self.height // 2 + 13
            elif self.menu_screen in (MenuScreen.OPTIONS, MenuScreen.CONTROLS):
                back_y = self.height // 2 + 10
            else:
                back_y = self.height // 2 + 4
            self._safe_addstr(
                back_y, (self.width - len(back_text)) // 2,
                back_text, curses.color_pair(COLOR_TEXT) | curses.A_DIM
            )

    def _render_high_scores_screen(self) -> None:
        """Render the high scores display table."""
        hs = self.get_high_scores_display()
        center_y = self.height // 2 - 6
        title = hs["title"]
        self._safe_addstr(center_y, (self.width - len(title)) // 2, title, curses.color_pair(COLOR_TEXT) | curses.A_BOLD)

        if hs["empty"]:
            empty_text = "No scores yet. Play a game!"
            self._safe_addstr(center_y + 2, (self.width - len(empty_text)) // 2, empty_text, curses.color_pair(COLOR_TEXT))
        else:
            header = f"{'Rank':>4}  {'Score':>8}  {'Level':>5}  {'Date':>10}"
            self._safe_addstr(center_y + 2, (self.width - len(header)) // 2, header, curses.color_pair(COLOR_TEXT) | curses.A_DIM)
            for row in hs["rows"]:
                line = f"  {row['rank']:>2}.  {row['score']:>8}  {'Lv':>3}{row['level']:<2}  {row['date']:>10}"
                r = center_y + 3 + row["rank"]
                attr = curses.color_pair(COLOR_TEXT)
                if row["rank"] == 1:
                    attr |= curses.A_BOLD
                self._safe_addstr(r, (self.width - len(line)) // 2, line, attr)

    def _render_options_screen(self) -> None:
        """Render the options/settings screen."""
        opts = self.get_options_display()
        center_y = self.height // 2 - 3
        title = opts["title"]
        self._safe_addstr(center_y, (self.width - len(title)) // 2, title, curses.color_pair(COLOR_TEXT) | curses.A_BOLD)

        for i, item in enumerate(opts["items"]):
            prefix = "> " if item["selected"] else "  "
            line = f"{prefix}{item['label']}: {item['value']}"
            row = center_y + 2 + i
            attr = curses.color_pair(COLOR_TEXT) | curses.A_BOLD if item["selected"] else curses.color_pair(COLOR_TEXT)
            self._safe_addstr(row, (self.width - len(line)) // 2, line, attr)

    def _render_controls_screen(self) -> None:
        """Render the controls help screen with keybindings and alien point values."""
        ctrl = self.get_controls_display()
        center_y = self.height // 2 - 7
        title = ctrl["title"]
        self._safe_addstr(center_y, (self.width - len(title)) // 2, title, curses.color_pair(COLOR_TEXT) | curses.A_BOLD)

        # Keybindings table
        for i, (key, action) in enumerate(ctrl["keybindings"]):
            line = f"  {key:<12} {action}"
            self._safe_addstr(center_y + 2 + i, (self.width - 30) // 2, line, curses.color_pair(COLOR_TEXT))

        # Alien point values
        pts_y = center_y + 2 + len(ctrl["keybindings"]) + 1
        pts_title = "ALIEN POINTS"
        self._safe_addstr(pts_y, (self.width - len(pts_title)) // 2, pts_title, curses.color_pair(COLOR_TEXT) | curses.A_BOLD)
        for i, (name, sprite, pts) in enumerate(ctrl["alien_points"]):
            line = f"  {sprite}  {name:<12} {pts}"
            self._safe_addstr(pts_y + 1 + i, (self.width - 30) // 2, line, curses.color_pair(COLOR_TEXT))

    def _render_credits_screen(self) -> None:
        """Render the credits screen with scrolling text."""
        creds = self.get_credits_display()
        for row, text in creds["lines"]:
            attr = curses.color_pair(COLOR_TEXT)
            if text.startswith("TERMINAL") or text.startswith("---"):
                attr |= curses.A_BOLD
            col = (self.width - len(text)) // 2
            self._safe_addstr(row, col, text, attr)

    def _render_game(self) -> None:
        """Render the main gameplay."""
        # Render starfield background (behind everything)
        for sx, sy, sc in self.stars:
            self._safe_addstr(int(sy), int(sx), sc, curses.color_pair(COLOR_PROJECTILE) | curses.A_DIM)

        # Score and lives header
        score_text = f"Score: {self.score}"
        lives_display = f"Lives: {'<A> ' * self.player.lives}"
        level_text = f"Level: {self.level}"

        self._safe_addstr(0, 2, score_text, curses.color_pair(COLOR_TEXT))
        self._safe_addstr(0, self.width // 2 - len(level_text) // 2, level_text, curses.color_pair(COLOR_TEXT))
        self._safe_addstr(0, self.width - len(lives_display) - 2, lives_display, curses.color_pair(COLOR_TEXT))

        # Render HUD separator and decorative border
        self._render_hud_border()

        # Render aliens (color per type, bold in frenzy mode)
        total_aliens = self.initial_alien_count or 1
        frenzy = len(self.aliens) < total_aliens * 0.3
        for alien in self.aliens:
            char = ALIEN_CHARS[alien.alien_type][self.alien_animation_frame]
            color = ALIEN_TYPE_COLORS.get(alien.alien_type, COLOR_ALIEN)
            attr = curses.color_pair(color)
            if frenzy:
                attr |= curses.A_BOLD
            self._safe_addstr(alien.y, alien.x, char, attr)

        # Render diving aliens (bold, blinking to stand out from formation)
        for diver in self.diving_aliens:
            char = ALIEN_CHARS[diver.alien.alien_type][self.alien_animation_frame]
            attr = curses.color_pair(COLOR_GAME_OVER) | curses.A_BOLD
            self._safe_addstr(diver.alien.y, diver.alien.x, char, attr)

        # Render ripple effects (~ line fading bold→dim on alien descent)
        for ripple in self.ripple_effects:
            progress = ripple.age / ripple.lifetime if ripple.lifetime > 0 else 1.0
            if progress < 0.33:
                attr = curses.color_pair(COLOR_ALIEN) | curses.A_BOLD
            elif progress < 0.66:
                attr = curses.color_pair(COLOR_ALIEN)
            else:
                attr = curses.color_pair(COLOR_ALIEN) | curses.A_DIM
            ripple_line = "~" * min(self.width - 3, self.width)
            self._safe_addstr(ripple.y, 1, ripple_line, attr)

        # Render mystery ship (wider sprite with blink effect)
        if self.mystery_ship and self.mystery_ship.active:
            display = self.mystery_ship.get_display_char()
            # Center the wider sprite around the ship x position
            draw_x = int(self.mystery_ship.x) - len(MYSTERY_SHIP_CHAR) // 2
            self._safe_addstr(
                self.mystery_ship.y, draw_x, display,
                curses.color_pair(COLOR_GAME_OVER) | curses.A_BOLD,
            )

        # Render mystery ship score display (brief score flash after hit)
        if self.mystery_score_display:
            mx, my, mpts, _ = self.mystery_score_display
            score_text = str(mpts)
            self._safe_addstr(my, mx, score_text, curses.color_pair(COLOR_GAME_OVER) | curses.A_BOLD)

        # Render dying aliens (death animation flash)
        for da in self.dying_aliens:
            self._safe_addstr(da.y, da.x, da.char, curses.color_pair(COLOR_TEXT) | curses.A_BOLD)

        # Render bunkers (color shifts green → yellow → red based on health)
        for bunker in self.bunkers:
            if bunker.health > 0:
                attr = curses.color_pair(bunker.color_pair)
                if bunker.flash_frames > 0:
                    attr |= curses.A_REVERSE  # White flash effect
                self._safe_addstr(bunker.y, bunker.x, bunker.char, attr)

        # Render falling power-ups (blink effect with distinct type chars)
        for pu in self.power_ups:
            char = POWERUP_CHARS.get(pu.power_type, "?")
            attr = curses.color_pair(COLOR_POWERUP) | curses.A_BOLD
            # Blink: visible 4 out of 6 frames based on thrust_frame counter
            if self.thrust_frame == 0:
                attr |= curses.A_REVERSE  # Alternate visual emphasis
            self._safe_addstr(int(pu.y), pu.x, char, attr)

        # Render active power-up HUD timers (top-right area, below lives)
        hud_info = self.get_powerup_hud_info()
        for i, label in enumerate(hud_info):
            self._safe_addstr(0, self.width - 30 - i * 8, label, curses.color_pair(COLOR_POWERUP) | curses.A_BOLD)

        # Render player (blink during invincibility i-frames)
        player_visible = True
        if self.is_invincible():
            # Blink at ~4 Hz: visible every other 1/8s cycle
            player_visible = int(time.time() * 8) % 2 == 0
        if player_visible:
            self._safe_addstr(self.player.y, self.player.x, self.config.player_char, curses.color_pair(COLOR_PLAYER))

        # Render shield aura (pulsing cyan brackets when shield active)
        aura = self.get_shield_aura_info()
        if aura:
            aura_attr = curses.color_pair(COLOR_ALIEN_TYPE_0)  # Cyan
            if aura["pulse"]:
                aura_attr |= curses.A_BOLD
            self._safe_addstr(aura["player_y"], aura["player_x"] - 1, aura["left_bracket"], aura_attr)
            self._safe_addstr(aura["player_y"], aura["player_x"] + 3, aura["right_bracket"], aura_attr)

        # Render player thrust/engine animation
        thrust_y = self.player.y + 1
        thrust_x = self.player.x + 1  # Center of 3-char-wide ship
        if thrust_y < self.height:
            if self.player_move_direction < 0:
                thrust_char = "/"
            elif self.player_move_direction > 0:
                thrust_char = "\\"
            else:
                thrust_char = "v" if self.thrust_frame == 0 else "^"
            self._safe_addstr(thrust_y, thrust_x, thrust_char, curses.color_pair(COLOR_PLAYER) | curses.A_DIM)

        # Reset movement direction for next frame (stationary unless a key is pressed)
        self.player_move_direction = 0

        # Render projectiles and trails
        trail_chars = ["|", ":", "."]  # newest to oldest
        for proj in self.player_projectiles:
            self._safe_addstr(proj.y, proj.x, self.config.projectile_player, curses.color_pair(COLOR_PROJECTILE))
            # Render fading trail behind the projectile
            for i, (tx, ty) in enumerate(reversed(proj.trail)):
                if i < len(trail_chars):
                    self._safe_addstr(int(ty), int(tx), trail_chars[i], curses.color_pair(COLOR_PROJECTILE) | curses.A_DIM)

        for proj in self.alien_projectiles:
            proj_info = ALIEN_PROJ_TYPES.get(proj.proj_type, ALIEN_PROJ_TYPES["normal"])
            proj_attr = curses.color_pair(COLOR_GAME_OVER)
            if proj.proj_type == "heavy":
                proj_attr |= curses.A_BOLD
            self._safe_addstr(proj.y, proj.x, proj_info["char"], proj_attr)

        # Render particles
        for p in self.particle_system.particles:
            attr = curses.color_pair(p.color_pair)
            # Dim particles in the last half of their lifetime for fade-out
            if p.age > p.lifetime * 0.5:
                attr |= curses.A_DIM
            self._safe_addstr(int(p.y), int(p.x), p.char, attr)

        # Render score popups (float upward with fade effect)
        for popup in self.score_popups:
            attr = curses.color_pair(COLOR_TEXT) | curses.A_BOLD
            # Fade: bold in first third, normal in middle, dim in last third
            progress = popup.age / popup.lifetime if popup.lifetime > 0 else 1.0
            if progress > 0.66:
                attr = curses.color_pair(COLOR_TEXT) | curses.A_DIM
            elif progress > 0.33:
                attr = curses.color_pair(COLOR_TEXT)
            self._safe_addstr(int(popup.y), int(popup.x), popup.text, attr)

        # Render combo text at center screen with emphasis
        combo_info = self.get_combo_display()
        if combo_info:
            attr = curses.color_pair(COLOR_TEXT) | curses.A_BOLD
            if combo_info["progress"] > 0.66:
                attr = curses.color_pair(COLOR_TEXT) | curses.A_DIM
            elif combo_info["progress"] < 0.33:
                attr |= curses.A_REVERSE
            self._safe_addstr(combo_info["center_y"], combo_info["center_x"], combo_info["text"], attr)

        # Bottom HUD bar (aliens remaining, combo, power-ups)
        hud = self.get_bottom_hud_info()
        hud_row = hud["row"]
        hud_attr = curses.color_pair(COLOR_TEXT) | curses.A_DIM
        self._safe_addstr(hud_row, 2, hud["aliens"], hud_attr)
        self._safe_addstr(hud_row, self.width // 2 - len(hud["combo"]) // 2, hud["combo"], hud_attr)
        self._safe_addstr(hud_row, self.width - len(hud["power_ups"]) - 2, hud["power_ups"], hud_attr)
        # Last stand blinking indicator (bold + blink when at 1 life)
        if hud["last_stand"]:
            ls_text = hud["last_stand"]
            ls_x = self.width // 2 - len(ls_text) // 2
            ls_attr = curses.color_pair(COLOR_PLAYER) | curses.A_BOLD
            self._safe_addstr(hud_row - 1, ls_x, ls_text, ls_attr)

        # FPS counter (when enabled via F1 or --show-fps, on row above bottom HUD)
        if self.show_fps:
            fps_text = f"FPS: {self.frame_timer.average_fps:.0f}"
            self._safe_addstr(self.height - 2, 2, fps_text, curses.color_pair(COLOR_TEXT))

    def get_powerup_hud_info(self) -> list:
        """Return list of HUD timer strings for active power-ups (testable).

        Example: ["[R 3s]", "[S 5s]"]
        """
        current = time.time()
        labels = []
        for ap in self.active_power_ups:
            remaining = max(0, ap.expires_at - current)
            if remaining > 900:  # Shield has ~999s, show as active
                label_char = POWERUP_LABELS.get(ap.power_type, "?")
                labels.append(f"[{label_char} ON]")
            else:
                label_char = POWERUP_LABELS.get(ap.power_type, "?")
                labels.append(f"[{label_char} {int(remaining)}s]")
        return labels

    def get_bottom_hud_info(self) -> dict:
        """Return bottom HUD bar info (testable without curses).

        Returns dict with:
            aliens: str like "Aliens: 12/25"
            combo: str like "Combo: x3" or "Combo: --"
            power_ups: str like "PWR: R S" or "PWR: --"
            row: int (bottom row for rendering)
        """
        total = self.initial_alien_count or 1
        remaining = len(self.aliens)
        aliens_str = f"Aliens: {remaining}/{total}"

        multiplier = self.get_combo_multiplier()
        combo_str = f"Combo: x{multiplier}" if multiplier > 1 else "Combo: --"

        active_types = [POWERUP_LABELS.get(ap.power_type, "?") for ap in self.active_power_ups]
        power_str = f"PWR: {' '.join(active_types)}" if active_types else "PWR: --"

        last_stand_str = "!! LAST STAND !!" if self.is_last_stand() else ""

        return {
            "aliens": aliens_str,
            "combo": combo_str,
            "power_ups": power_str,
            "last_stand": last_stand_str,
            "row": self.height - 1,
        }

    def get_descent_danger_ratio(self) -> float:
        """Return how far aliens have descended toward the player (0.0=safe, 1.0=invaded).

        Used for progressive background color shift as aliens approach.
        Returns 0.0 if no aliens present.
        """
        if not self.aliens:
            return 0.0
        lowest_alien_y = max(a.y for a in self.aliens)
        start_y = self.config.alien_start_y
        player_y = self.player.y
        if player_y <= start_y:
            return 0.0
        return max(0.0, min(1.0, (lowest_alien_y - start_y) / (player_y - start_y)))

    def get_level_transition_info(self) -> dict:
        """Return level transition countdown info (testable without curses).

        Returns dict with:
            level: current level number
            lives_awarded: lives gained
            countdown_text: "3", "2", "1", or "GO!"
            elapsed: seconds since transition started
            wave_clear: "WAVE CLEAR!" shown in first 1.5s, else ""
        """
        elapsed = time.time() - self.level_transition_time if self.level_transition_time > 0 else 0
        if elapsed < 1.0:
            countdown_text = "3"
        elif elapsed < 2.0:
            countdown_text = "2"
        elif elapsed < 3.0:
            countdown_text = "1"
        else:
            countdown_text = "GO!"
        wave_clear = "WAVE CLEAR!" if elapsed < 1.5 else ""
        return {
            "level": self.level,
            "lives_awarded": self.lives_awarded,
            "countdown_text": countdown_text,
            "elapsed": elapsed,
            "wave_clear": wave_clear,
        }

    def get_game_over_stats(self) -> dict:
        """Return game over statistics (testable without curses).

        Returns dict with:
            score: final score
            level: level reached
            kills: total aliens killed
            shots: total shots fired
            accuracy: str like "75%" or "N/A"
            curtain_rows: int (number of rows to fill for curtain animation)
        """
        accuracy = f"{(self.total_kills / self.total_shots * 100):.0f}%" if self.total_shots > 0 else "N/A"
        elapsed = time.time() - self.game_over_time if self.game_over_time > 0 else 0
        # Curtain fills 1 row every 0.05s, up to full height
        curtain_rows = min(self.height, int(elapsed / 0.05))
        return {
            "score": self.score,
            "level": self.level,
            "kills": self.total_kills,
            "shots": self.total_shots,
            "accuracy": accuracy,
            "curtain_rows": curtain_rows,
        }

    def get_border_layout(self) -> list:
        """Return list of (row, col, char) tuples describing the HUD border.

        This method computes the border layout without curses, making it testable.
        """
        elements = []
        w = self.width
        h = self.height

        # HUD separator row (row 1, below the score/level/lives header)
        hud_sep_row = 1
        elements.append((hud_sep_row, 0, BORDER_V))
        for col in range(1, max(1, w - 2)):
            elements.append((hud_sep_row, col, BORDER_H))
        elements.append((hud_sep_row, max(0, w - 2), BORDER_V))

        # Left and right vertical borders (rows 2 through h-2)
        for row in range(2, h - 1):
            elements.append((row, 0, BORDER_V))
            elements.append((row, max(0, w - 2), BORDER_V))

        # Bottom border
        bottom_row = h - 1
        elements.append((bottom_row, 0, BORDER_BL))
        for col in range(1, max(1, w - 2)):
            elements.append((bottom_row, col, BORDER_H))
        elements.append((bottom_row, max(0, w - 2), BORDER_BR))

        return elements

    def _render_hud_border(self) -> None:
        """Render HUD separator line and decorative border using box-drawing chars."""
        border_attr = curses.color_pair(COLOR_BORDER) | curses.A_DIM
        for row, col, char in self.get_border_layout():
            self._safe_addstr(row, col, char, border_attr)

    def _render_pause_overlay(self) -> None:
        """Render pause overlay on top of the game."""
        pause_text = "PAUSED"
        resume_text = "Press P or Escape to Resume"
        center_y = self.height // 2
        self._safe_addstr(
            center_y, (self.width - len(pause_text)) // 2, pause_text, curses.color_pair(COLOR_TEXT) | curses.A_BOLD
        )
        self._safe_addstr(
            center_y + 2, (self.width - len(resume_text)) // 2, resume_text, curses.color_pair(COLOR_TEXT)
        )

    def _render_game_over(self) -> None:
        """Render the game over screen with curtain fall animation and stats."""
        self._render_game()  # Show final game state

        stats = self.get_game_over_stats()

        # Curtain fall: fill rows from top with dim block chars
        curtain_char = "░" * self.width
        for row in range(stats["curtain_rows"]):
            self._safe_addstr(row, 0, curtain_char, curses.color_pair(COLOR_TEXT) | curses.A_DIM)

        # Game over title
        game_over_text = "GAME OVER"
        center_y = self.height // 2 - 3
        self._safe_addstr(
            center_y,
            (self.width - len(game_over_text)) // 2,
            game_over_text,
            curses.color_pair(COLOR_GAME_OVER) | curses.A_BOLD,
        )

        # Stats block
        stat_lines = [
            f"Score: {stats['score']}",
            f"Level: {stats['level']}",
            f"Kills: {stats['kills']}  Shots: {stats['shots']}",
            f"Accuracy: {stats['accuracy']}",
        ]
        for i, line in enumerate(stat_lines):
            self._safe_addstr(
                center_y + 2 + i,
                (self.width - len(line)) // 2,
                line,
                curses.color_pair(COLOR_TEXT),
            )

        # Restart prompt
        restart_text = "Press 'R' to Restart or 'Q' to Quit"
        self._safe_addstr(
            center_y + 7, (self.width - len(restart_text)) // 2, restart_text, curses.color_pair(COLOR_TEXT)
        )

    def _render_level_transition(self) -> None:
        """Render level transition screen with countdown and bonus lives info."""
        info = self.get_level_transition_info()
        level_text = f"LEVEL {info['level']}"
        bonus_text = f"+{info['lives_awarded']} {'LIFE' if info['lives_awarded'] == 1 else 'LIVES'} BONUS!"
        lives_text = f"Lives: {self.player.lives}/{self.config.max_lives}"
        countdown = info["countdown_text"]

        center_y = self.height // 2

        # "WAVE CLEAR!" celebration text (shown in first 1.5s)
        wave_clear = info["wave_clear"]
        if wave_clear:
            wc_attr = curses.color_pair(COLOR_PLAYER) | curses.A_BOLD | curses.A_REVERSE
            self._safe_addstr(
                center_y - 5, (self.width - len(wave_clear)) // 2, wave_clear, wc_attr
            )

        self._safe_addstr(
            center_y - 3, (self.width - len(level_text)) // 2, level_text, curses.color_pair(COLOR_TEXT) | curses.A_BOLD
        )
        self._safe_addstr(
            center_y - 1, (self.width - len(bonus_text)) // 2, bonus_text, curses.color_pair(COLOR_PLAYER) | curses.A_BOLD
        )
        self._safe_addstr(center_y, (self.width - len(lives_text)) // 2, lives_text, curses.color_pair(COLOR_TEXT))

        # Countdown display (large, bold, centered)
        cd_attr = curses.color_pair(COLOR_TEXT) | curses.A_BOLD
        if countdown == "GO!":
            cd_attr = curses.color_pair(COLOR_PLAYER) | curses.A_BOLD | curses.A_REVERSE
        self._safe_addstr(center_y + 2, (self.width - len(countdown)) // 2, countdown, cd_attr)

        skip_text = "Press SPACE to skip"
        self._safe_addstr(
            center_y + 4, (self.width - len(skip_text)) // 2, skip_text, curses.color_pair(COLOR_TEXT) | curses.A_DIM
        )

        # Render celebration particles
        for p in self.particle_system.particles:
            attr = curses.color_pair(p.color_pair)
            if p.age > p.lifetime * 0.5:
                attr |= curses.A_DIM
            self._safe_addstr(int(p.y), int(p.x), p.char, attr)

    def _safe_addstr(self, y, x, text: str, attr: int = 0) -> None:
        """Safely add string to screen, handling boundary issues and shake offset."""
        try:
            # Convert to int to handle float coordinates from projectile speed
            y_int = int(y) + self.shake_offset_y
            x_int = int(x) + self.shake_offset_x
            if 0 <= y_int < self.height and 0 <= x_int < self.width:
                # Truncate if necessary
                max_len = self.width - x_int - 1
                if max_len > 0:
                    self.screen.addstr(y_int, x_int, text[:max_len], attr)
        except curses.error:
            logger.debug("Curses draw error at (%d, %d)", y_int, x_int)

    def run(self) -> None:
        """Main game loop with curses."""

        def main(stdscr):
            # Setup curses
            self.screen = stdscr
            curses.curs_set(0)  # Hide cursor
            stdscr.nodelay(True)  # Non-blocking input
            stdscr.timeout(self.config.input_timeout_ms)  # Fast input polling for responsive movement

            # Initialize colors
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(COLOR_PLAYER, curses.COLOR_GREEN, -1)
            curses.init_pair(COLOR_ALIEN, curses.COLOR_MAGENTA, -1)
            curses.init_pair(COLOR_BUNKER, curses.COLOR_CYAN, -1)
            curses.init_pair(COLOR_TEXT, curses.COLOR_YELLOW, -1)
            curses.init_pair(COLOR_PROJECTILE, curses.COLOR_WHITE, -1)
            curses.init_pair(COLOR_GAME_OVER, curses.COLOR_RED, -1)
            curses.init_pair(COLOR_BUNKER_DAMAGED, curses.COLOR_YELLOW, -1)
            curses.init_pair(COLOR_BUNKER_CRITICAL, curses.COLOR_RED, -1)
            curses.init_pair(COLOR_ALIEN_TYPE_0, curses.COLOR_CYAN, -1)
            curses.init_pair(COLOR_ALIEN_TYPE_1, curses.COLOR_MAGENTA, -1)
            curses.init_pair(COLOR_ALIEN_TYPE_2, curses.COLOR_YELLOW, -1)
            curses.init_pair(COLOR_BORDER, curses.COLOR_CYAN, -1)
            curses.init_pair(COLOR_POWERUP, curses.COLOR_WHITE, -1)
            curses.init_pair(COLOR_BUNKER_HIGH, curses.COLOR_GREEN, -1)
            curses.init_pair(COLOR_BUNKER_LOW, curses.COLOR_YELLOW, -1)
            curses.init_pair(COLOR_BG_DANGER_LOW, curses.COLOR_WHITE, curses.COLOR_RED)
            curses.init_pair(COLOR_BG_DANGER_HIGH, curses.COLOR_WHITE, curses.COLOR_RED)

            # Get screen dimensions
            self.height, self.width = stdscr.getmaxyx()

            # Check minimum size
            if self.width < self.config.min_width or self.height < self.config.min_height:
                stdscr.addstr(0, 0, f"Terminal too small! Need {self.config.min_width}x{self.config.min_height}")
                stdscr.refresh()
                stdscr.getch()
                return

            # Reinitialize with proper dimensions
            self._init_player_position()
            self._init_aliens()
            self._init_bunkers()

            # Setup audio with global reference for cleanup
            global _audio_manager
            self.audio = AudioManager()
            _audio_manager = self.audio

            running = True
            try:
                while running:
                    frame_start = time.time()

                    # Handle input
                    try:
                        key = stdscr.getch()
                        if key != -1:
                            running = self.handle_input(key)
                    except Exception:
                        logger.debug("Input handling error", exc_info=True)

                    # Update game state
                    self.update()

                    # Render
                    self.render()

                    # Frame rate limiting and metrics
                    elapsed = time.time() - frame_start
                    self.frame_timer.record(elapsed)
                    sleep_time = self.config.frame_time - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)
            finally:
                # Always cleanup audio, even on crash
                if self.audio:
                    self.audio.stop()

        # Run with curses wrapper
        curses.wrapper(main)


# ============================================================================
# ENTRY POINT
# ============================================================================


def main():
    """Main entry point for the game."""
    parser = build_argument_parser()
    args = parser.parse_args()

    setup_logging(debug=args.debug)
    logger.debug("Game starting with args: %s", args)

    config = config_from_args(args)
    game = Game(config=config)

    if args.no_sound and game.sfx:
        game.sfx.enabled = False
    if args.no_music:
        game.audio = None  # Prevent audio manager from starting
    if args.show_fps:
        game.show_fps = True

    game.run()


if __name__ == "__main__":
    main()
