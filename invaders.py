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


# ============================================================================
# ENUMS
# ============================================================================


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


@dataclass
class Alien:
    """Individual alien with position and type."""

    x: int
    y: int
    alien_type: int = 0
    alive: bool = True

    def __post_init__(self):
        """Set alien type based on row if not specified."""
        pass


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
class Bunker:
    """Defensive bunker that erodes on hits."""

    x: int
    y: int
    health: int = 3  # 3=full, 2=damaged, 1=critical, 0=destroyed
    flash_frames: int = 0  # Frames remaining for white flash on hit

    @property
    def char(self) -> str:
        """Get display character based on health."""
        if self.health <= 0:
            return " "
        return BUNKER_CHARS[3 - self.health]

    @property
    def color_pair(self) -> int:
        """Return color pair based on health: green(3) → yellow(2) → red(1)."""
        if self.flash_frames > 0:
            return COLOR_TEXT  # White flash
        if self.health >= 3:
            return COLOR_BUNKER  # Green (full)
        if self.health == 2:
            return COLOR_BUNKER_DAMAGED  # Yellow (damaged)
        return COLOR_BUNKER_CRITICAL  # Red (critical)

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

    def __post_init__(self) -> None:
        if self.trail is None:
            self.trail = []

    def record_position(self) -> None:
        """Record current position for trail rendering. Keep last 3 positions."""
        self.trail.append((self.x, self.y))
        if len(self.trail) > 3:
            self.trail.pop(0)


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


class PowerUpType(Enum):
    """Types of power-ups."""

    RAPID_FIRE = auto()
    SHIELD = auto()
    WIDE_SHOT = auto()


# Distinct display characters for each power-up type
POWERUP_CHARS = {
    PowerUpType.RAPID_FIRE: "R",
    PowerUpType.SHIELD: "S",
    PowerUpType.WIDE_SHOT: "W",
}

# Short labels for HUD timer display
POWERUP_LABELS = {
    PowerUpType.RAPID_FIRE: "R",
    PowerUpType.SHIELD: "S",
    PowerUpType.WIDE_SHOT: "W",
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
        self.score = 0
        self.level = 1

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

        # Combo system
        self.combo_count = 0
        self.combo_last_kill_time: float = 0
        self.combo_window: float = 2.0  # seconds
        self.combo_max: int = 5

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
        """Create the initial alien grid (rows scaled by level)."""
        self.aliens = []
        cfg = self.config
        rows = self.get_scaled_alien_rows()
        start_x = (self.width - (cfg.alien_cols * cfg.alien_spacing_x)) // 2

        for row in range(rows):
            alien_type = row // 2  # Different types per rows
            for col in range(cfg.alien_cols):
                x = start_x + col * cfg.alien_spacing_x
                y = cfg.alien_start_y + row * cfg.alien_spacing_y
                self.aliens.append(Alien(x=x, y=y, alien_type=alien_type % 3))

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
                    self.bunkers.append(Bunker(x=bunker_x + dx, y=bunker_y + dy, health=health))

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
        total_aliens = cfg.alien_rows * cfg.alien_cols
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
                return

    def reset_game(self) -> None:
        """
        Perform a hard reset of the game state.
        Called when player presses 'R' at game over.
        """
        # Reset score
        self.score = 0

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

        # Clear dying aliens, score popups, and ripple effects
        self.dying_aliens.clear()
        self.score_popups.clear()
        self.ripple_effects.clear()

        # Reset invincibility
        self.invincible_until = 0

        # Reset combo
        self.combo_count = 0
        self.combo_last_kill_time = 0

        # Clear particles
        self.particle_system.clear()

        # Set state to playing
        self.state = GameState.PLAYING

    def update(self) -> None:
        """Main game update loop."""
        # Title screen color cycling runs in MENU state
        if self.state == GameState.MENU:
            self.title_color_frame += 1
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

        # Move aliens
        if current_time - self.last_alien_move_time >= self.alien_move_interval:
            self._move_aliens()
            self.last_alien_move_time = current_time

        # Update projectiles
        self._update_projectiles()

        # Alien firing
        self._alien_fire()

        # Update mystery ship
        self._update_mystery_ship(current_time)

        # Check collisions
        self._check_collisions()

        # Update power-ups
        self._update_power_ups(current_time)

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

        # Check invasion
        self.check_invasion()

        # Update marching beat (speeds up as aliens die - iconic Space Invaders sound)
        if self.sfx and self.aliens:
            total_aliens = self.config.alien_rows * self.config.alien_cols
            self.sfx.update_march(len(self.aliens), total_aliens)

        # Check level complete
        if not self.aliens:
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

    def _activate_power_up(self, power_type: PowerUpType, current_time: float) -> None:
        """Activate a collected power-up."""
        if power_type == PowerUpType.SHIELD:
            # Shield is a one-time use, set a long expiry
            self.active_power_ups.append(ActivePowerUp(power_type=power_type, expires_at=current_time + 999))
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

    def _register_kill(self, current_time: float) -> int:
        """Register an alien kill for combo tracking. Returns the multiplier."""
        if current_time - self.combo_last_kill_time <= self.combo_window:
            self.combo_count += 1
        else:
            self.combo_count = 1
        self.combo_last_kill_time = current_time
        return self.get_combo_multiplier()

    def _update_projectiles(self) -> None:
        """Update all projectile positions."""
        # Player projectiles move up (faster)
        for proj in self.player_projectiles[:]:
            proj.record_position()
            proj.y -= self.config.player_projectile_speed
            if proj.y < 0:
                self.player_projectiles.remove(proj)

        # Alien projectiles move down (slower for easier dodging)
        for proj in self.alien_projectiles[:]:
            proj.y += self.config.alien_projectile_speed
            if proj.y >= self.height:
                self.alien_projectiles.remove(proj)

    def _alien_fire(self) -> None:
        """Handle alien firing logic."""
        if not self.aliens:
            return

        fire_prob = self.get_alien_fire_probability()

        for alien in self.aliens:
            if random.random() < fire_prob:
                self.alien_projectiles.append(Projectile(x=alien.x + 1, y=alien.y + 1, direction=1))

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
                    self.aliens.remove(alien)
                    if proj in self.player_projectiles:
                        self.player_projectiles.remove(proj)
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
                    # Add dying alien animation
                    self.dying_aliens.append(DyingAlien(x=alien.x, y=alien.y))
                    # Spawn explosion particles at alien death position
                    if not self.test_mode:
                        self.particle_system.spawn(
                            x=float(alien.x),
                            y=float(alien.y),
                            count=random.randint(5, 8),
                            chars="*+.'`",
                            color_pair=ALIEN_TYPE_COLORS.get(alien.alien_type, COLOR_ALIEN),
                            speed_range=(2.0, 6.0),
                            lifetime_range=(0.3, 0.5),
                        )
                    self.event_bus.publish(GameEvent.ALIEN_KILLED, alien_type=alien.alien_type)
                    break

        # Alien projectiles vs player (single entity, no grid needed)
        for proj in self.alien_projectiles[:]:
            if abs(proj.x - self.player.x - 1) <= 1 and proj.y >= self.player.y:
                self.alien_projectiles.remove(proj)
                self.handle_player_damage()
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

    def get_scaled_alien_rows(self) -> int:
        """Return alien rows for the current level (extra row every 3 levels, max 8)."""
        extra = (self.level - 1) // 3
        return min(self.config.alien_rows + extra, 8)

    def get_scaled_bunker_health(self) -> int:
        """Return starting bunker health for the current level (reduced every 5 levels, min 1)."""
        reduction = (self.level - 1) // 5
        return max(1, 3 - reduction)

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
        self.state = GameState.LEVEL_TRANSITION

        # Green flash for level complete (0.15s)
        self.flash_active = True
        self.flash_color = COLOR_PLAYER  # Green
        self.flash_end_time = time.time() + 0.15

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
            if key == ord(" ") or key == ord("\n"):
                self.state = GameState.PLAYING
                if self.audio:
                    self.audio.start()

        elif self.state == GameState.PLAYING:
            if key == ord("p") or key == ord("P") or key == 27:  # P or Escape
                self._toggle_pause()
            elif key == curses.KEY_LEFT or key == ord("a"):
                self.player.x = max(0, self.player.x - self.config.player_speed)
                self.player_move_direction = -1
            elif key == curses.KEY_RIGHT or key == ord("d"):
                self.player.x = min(self.width - 3, self.player.x + self.config.player_speed)
                self.player_move_direction = 1
            elif key == ord(" "):
                # Fire projectile
                if len(self.player_projectiles) < 3:  # Limit active projectiles
                    self.player_projectiles.append(Projectile(x=self.player.x + 1, y=self.player.y - 1, direction=-1))
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

    def _render_menu(self) -> None:
        """Render the animated title screen with ASCII art and color cycling."""
        layout = self.get_title_layout()
        cycle_len = len(TITLE_COLOR_CYCLE)

        # Render each art line with color cycling (offset per line for rainbow)
        for i, (row, col, text) in enumerate(layout["art_lines"]):
            color_pair = TITLE_COLOR_CYCLE[(layout["color_index"] + i) % cycle_len]
            self._safe_addstr(row, col, text, curses.color_pair(color_pair) | curses.A_BOLD)

        # Subtitle (blink effect using frame counter)
        sub_r, sub_c, sub_text = layout["subtitle"]
        if (self.title_color_frame // 15) % 2 == 0:  # Blink every ~15 frames
            self._safe_addstr(sub_r, sub_c, sub_text, curses.color_pair(COLOR_TEXT))

        # Controls
        ctrl_r, ctrl_c, ctrl_text = layout["controls"]
        self._safe_addstr(ctrl_r, ctrl_c, ctrl_text, curses.color_pair(COLOR_TEXT) | curses.A_DIM)

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
        total_aliens = self.config.alien_rows * self.config.alien_cols
        frenzy = len(self.aliens) < total_aliens * 0.3
        for alien in self.aliens:
            char = ALIEN_CHARS[alien.alien_type][self.alien_animation_frame]
            color = ALIEN_TYPE_COLORS.get(alien.alien_type, COLOR_ALIEN)
            attr = curses.color_pair(color)
            if frenzy:
                attr |= curses.A_BOLD
            self._safe_addstr(alien.y, alien.x, char, attr)

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
            self._safe_addstr(proj.y, proj.x, self.config.projectile_alien, curses.color_pair(COLOR_GAME_OVER))

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

        # FPS counter (when enabled via F1 or --show-fps)
        if self.show_fps:
            fps_text = f"FPS: {self.frame_timer.average_fps:.0f}"
            self._safe_addstr(self.height - 1, 2, fps_text, curses.color_pair(COLOR_TEXT))

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
        """Render the game over screen."""
        self._render_game()  # Show final game state

        game_over_text = f"GAME OVER - Score: {self.score}"
        restart_text = "Press 'R' to Restart or 'Q' to Quit"

        center_y = self.height // 2
        self._safe_addstr(
            center_y,
            (self.width - len(game_over_text)) // 2,
            game_over_text,
            curses.color_pair(COLOR_GAME_OVER) | curses.A_BOLD,
        )
        self._safe_addstr(
            center_y + 2, (self.width - len(restart_text)) // 2, restart_text, curses.color_pair(COLOR_TEXT)
        )

    def _render_level_transition(self) -> None:
        """Render level transition screen with bonus lives info."""
        level_text = f"LEVEL {self.level}"
        bonus_text = f"+{self.lives_awarded} {'LIFE' if self.lives_awarded == 1 else 'LIVES'} BONUS!"
        lives_text = f"Lives: {self.player.lives}/{self.config.max_lives}"
        continue_text = "Press SPACE to Continue"

        center_y = self.height // 2
        self._safe_addstr(
            center_y - 2, (self.width - len(level_text)) // 2, level_text, curses.color_pair(COLOR_TEXT) | curses.A_BOLD
        )
        self._safe_addstr(
            center_y, (self.width - len(bonus_text)) // 2, bonus_text, curses.color_pair(COLOR_PLAYER) | curses.A_BOLD
        )
        self._safe_addstr(center_y + 1, (self.width - len(lives_text)) // 2, lives_text, curses.color_pair(COLOR_TEXT))
        self._safe_addstr(
            center_y + 3, (self.width - len(continue_text)) // 2, continue_text, curses.color_pair(COLOR_TEXT)
        )

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
