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

import curses
import os
import sys
import time
import random
import threading
import subprocess
import atexit
import signal
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

# Global audio manager reference for cleanup on crash/exit
_audio_manager: Optional['AudioManager'] = None


def _cleanup_audio():
    """Global cleanup function to stop audio on any exit."""
    global _audio_manager
    if _audio_manager:
        _audio_manager.stop()
    # Also kill any stray afplay processes as a safety net
    try:
        subprocess.run(['pkill', '-9', 'afplay'],
                      stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _signal_handler(signum, frame):
    """Handle signals to ensure clean shutdown."""
    _cleanup_audio()
    sys.exit(0)


# Register cleanup handlers
atexit.register(_cleanup_audio)
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# ============================================================================
# CONSTANTS
# ============================================================================

PLAYER_START_LIVES = 5
MAX_LIVES = 9  # Cap on maximum lives
TARGET_FPS = 60
FRAME_TIME = 1.0 / TARGET_FPS

# Screen dimensions (will be adjusted based on terminal size)
MIN_WIDTH = 60
MIN_HEIGHT = 24

# Alien grid configuration
ALIEN_ROWS = 5
ALIEN_COLS = 11
ALIEN_SPACING_X = 4
ALIEN_SPACING_Y = 2
ALIEN_START_Y = 3

# Movement speeds
PLAYER_SPEED = 1
ALIEN_MOVE_INTERVAL = 0.5  # seconds between alien movements
PLAYER_PROJECTILE_SPEED = 1.0   # Player laser speed
ALIEN_PROJECTILE_SPEED = 0.4    # Alien laser speed (slower = easier to dodge)

# Input responsiveness (lower = faster lateral movement via quicker key repeat)
INPUT_TIMEOUT_MS = 8  # milliseconds to wait for input (default was ~16ms at 60fps)

# Firing configuration (reduced by 1/3 from original)
BASE_FIRE_PROBABILITY = 0.00133  # Base probability per alien per frame (was 0.002)
MAX_FIRE_PROBABILITY = 0.0133    # Maximum probability in frenzy mode (was 0.02)

# Visual characters
PLAYER_CHAR = "^A^"
ALIEN_CHARS = [
    ["/-\\", "\\-/"],  # Type 1 animation frames
    ["<O>", "<o>"],    # Type 2 animation frames
    ["/M\\", "\\W/"],  # Type 3 animation frames
]
PROJECTILE_PLAYER = "|"
PROJECTILE_ALIEN = "!"
BUNKER_CHARS = ['O', 'o', '.']  # Erosion states: full, damaged, nearly destroyed

# Color pairs
COLOR_PLAYER = 1
COLOR_ALIEN = 2
COLOR_BUNKER = 3
COLOR_TEXT = 4
COLOR_PROJECTILE = 5
COLOR_GAME_OVER = 6


# ============================================================================
# ENUMS
# ============================================================================

class GameState(Enum):
    """Game state machine states."""
    MENU = auto()
    PLAYING = auto()
    LEVEL_TRANSITION = auto()
    GAME_OVER = auto()


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def resolve_audio_path() -> str:
    """
    Resolve the audio file path using os.path.expanduser.

    Returns:
        Absolute path to ~/soundtrack.mp3
    """
    return os.path.expanduser('~/soundtrack.mp3')


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


@dataclass
class Bunker:
    """Defensive bunker that erodes on hits."""
    x: int
    y: int
    health: int = 3  # 3=full, 2=damaged, 1=critical, 0=destroyed

    @property
    def char(self) -> str:
        """Get display character based on health."""
        if self.health <= 0:
            return ' '
        return BUNKER_CHARS[3 - self.health]

    def hit(self) -> bool:
        """
        Process a hit on the bunker.
        Returns True if bunker is destroyed.
        """
        self.health -= 1
        return self.health <= 0


@dataclass
class Projectile:
    """Projectile fired by player or alien."""
    x: int
    y: int
    direction: int  # -1 for up (player), 1 for down (alien)


# ============================================================================
# AUDIO SYSTEM
# ============================================================================

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
                    ['afplay', audio_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                self.current_process.wait()
            except Exception:
                break  # Exit on any error

    def stop(self) -> None:
        """Stop audio playback and clean up."""
        self.game_running = False

        # Kill any running afplay process
        if self.current_process:
            try:
                self.current_process.terminate()
                self.current_process.wait(timeout=1)
            except Exception:
                try:
                    self.current_process.kill()
                except Exception:
                    pass

        # Also kill any stray afplay processes
        try:
            subprocess.run(['pkill', '-9', 'afplay'],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
        except Exception:
            pass


class SoundEffects:
    """
    Retro-style sound effects mimicking original Space Invaders.

    Uses macOS system sounds and afplay for non-blocking audio.
    The original Space Invaders had:
    - Marching beat that sped up as aliens decreased
    - Shooting "pew" sound
    - Alien explosion sound
    - Player death sound
    - UFO sound
    """

    # macOS system sound paths
    SOUNDS = {
        'shoot': '/System/Library/Sounds/Pop.aiff',
        'alien_die': '/System/Library/Sounds/Bottle.aiff',
        'player_die': '/System/Library/Sounds/Basso.aiff',
        'level_complete': '/System/Library/Sounds/Glass.aiff',
        'march1': '/System/Library/Sounds/Tink.aiff',
        'march2': '/System/Library/Sounds/Pop.aiff',
        'life_bonus': '/System/Library/Sounds/Hero.aiff',
    }

    def __init__(self):
        self.enabled = True
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
        """Play a sound asynchronously (non-blocking)."""
        if not self.enabled:
            return
        if sound_name not in self.SOUNDS:
            return
        if not self.available_sounds.get(sound_name, False):
            return

        def play():
            try:
                # afplay with volume control
                subprocess.run(
                    ['afplay', '-v', str(volume), self.SOUNDS[sound_name]],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2
                )
            except Exception:
                pass

        thread = threading.Thread(target=play, daemon=True)
        thread.start()

    def play_shoot(self):
        """Player shooting sound."""
        self._play_async('shoot', 0.3)

    def play_alien_die(self):
        """Alien explosion sound."""
        self._play_async('alien_die', 0.4)

    def play_player_die(self):
        """Player death sound."""
        self._play_async('player_die', 0.6)

    def play_level_complete(self):
        """Level completion fanfare."""
        self._play_async('level_complete', 0.5)

    def play_life_bonus(self):
        """Extra life awarded sound."""
        self._play_async('life_bonus', 0.5)

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
                self._play_async('march1', 0.2)
            else:
                self._play_async('march2', 0.2)

            self.march_beat = 1 - self.march_beat
            self.last_march_time = current_time


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

    def __init__(self, test_mode: bool = False):
        """
        Initialize game.

        Args:
            test_mode: If True, skip curses initialization for unit testing.
        """
        self.test_mode = test_mode
        self.state = GameState.PLAYING if test_mode else GameState.MENU
        self.score = 0
        self.level = 1

        # Screen dimensions (defaults for test mode)
        self.width = 80
        self.height = 24

        # Initialize player at center-bottom
        self.player = Player()
        self._init_player_position()

        # Game entities
        self.aliens: List[Alien] = []
        self.player_projectiles: List[Dict] = []
        self.alien_projectiles: List[Dict] = []
        self.bunkers: List[Bunker] = []

        # Animation state
        self.alien_animation_frame = 0
        self.last_animation_time = time.time()
        self.alien_direction = 1  # 1=right, -1=left
        self.last_alien_move_time = time.time()

        # Flash effect state
        self.flash_active = False
        self.flash_end_time = 0

        # Audio
        self.audio: Optional[AudioManager] = None

        # Sound effects
        self.sfx: Optional[SoundEffects] = None
        if not test_mode:
            self.sfx = SoundEffects()

        # Curses screen
        self.screen = None

        # Level completion bonus tracking
        self.lives_awarded = 0  # Lives awarded at last level completion

        # Initialize aliens
        self._init_aliens()

        if not test_mode:
            self._init_bunkers()

    def _init_player_position(self) -> None:
        """Set player starting position."""
        self.player.x = self.width // 2 - 1
        self.player.y = self.height - 2
        self._initial_player_x = self.player.x

    def _init_aliens(self) -> None:
        """Create the initial alien grid."""
        self.aliens = []
        start_x = (self.width - (ALIEN_COLS * ALIEN_SPACING_X)) // 2

        for row in range(ALIEN_ROWS):
            alien_type = row // 2  # Different types per rows
            for col in range(ALIEN_COLS):
                x = start_x + col * ALIEN_SPACING_X
                y = ALIEN_START_Y + row * ALIEN_SPACING_Y
                self.aliens.append(Alien(x=x, y=y, alien_type=alien_type % 3))

    def _init_bunkers(self) -> None:
        """Create defensive bunkers."""
        self.bunkers = []
        bunker_y = self.height - 6
        bunker_spacing = self.width // 5

        for i in range(4):
            bunker_x = bunker_spacing * (i + 1) - 2
            # Create bunker block (3x2)
            for dx in range(-1, 2):
                for dy in range(2):
                    self.bunkers.append(Bunker(x=bunker_x + dx, y=bunker_y + dy))

    def get_alien_fire_probability(self) -> float:
        """
        Calculate alien firing probability based on remaining aliens.
        Implements 'frenzy mode' - fewer aliens = more shooting.

        Returns:
            Probability of each alien firing per frame.
        """
        if not self.aliens:
            return 0

        # Calculate ratio of destroyed aliens
        total_aliens = ALIEN_ROWS * ALIEN_COLS
        remaining = len(self.aliens)
        destroyed_ratio = 1 - (remaining / total_aliens)

        # Linear interpolation between base and max probability
        probability = BASE_FIRE_PROBABILITY + (
            destroyed_ratio * (MAX_FIRE_PROBABILITY - BASE_FIRE_PROBABILITY)
        )

        return min(probability, MAX_FIRE_PROBABILITY)

    def handle_player_damage(self) -> None:
        """
        Handle player taking damage from alien projectile.
        - Decrements life
        - Triggers flash effect
        - Clears projectiles
        - Resets position
        - Checks for game over
        """
        self.player.take_damage()

        # Play death sound
        if self.sfx:
            self.sfx.play_player_die()

        if self.player.lives <= 0:
            self.state = GameState.GAME_OVER
        else:
            # Flash effect
            self.flash_active = True
            self.flash_end_time = time.time() + 0.2

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
        self.player.lives = PLAYER_START_LIVES
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
        self.last_alien_move_time = time.time()

        # Set state to playing
        self.state = GameState.PLAYING

    def update(self) -> None:
        """Main game update loop."""
        if self.state != GameState.PLAYING:
            return

        current_time = time.time()

        # Update flash effect
        if self.flash_active and current_time >= self.flash_end_time:
            self.flash_active = False

        # Update alien animation
        if current_time - self.last_animation_time >= 0.5:
            self.alien_animation_frame = 1 - self.alien_animation_frame
            self.last_animation_time = current_time

        # Move aliens
        if current_time - self.last_alien_move_time >= ALIEN_MOVE_INTERVAL:
            self._move_aliens()
            self.last_alien_move_time = current_time

        # Update projectiles
        self._update_projectiles()

        # Alien firing
        self._alien_fire()

        # Check collisions
        self._check_collisions()

        # Check invasion
        self.check_invasion()

        # Update marching beat (speeds up as aliens die - iconic Space Invaders sound)
        if self.sfx and self.aliens:
            total_aliens = ALIEN_ROWS * ALIEN_COLS
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
            for alien in self.aliens:
                alien.y += 1
        elif min_x <= 2 and self.alien_direction < 0:
            self.alien_direction = 1
            for alien in self.aliens:
                alien.y += 1
        else:
            for alien in self.aliens:
                alien.x += self.alien_direction

    def _update_projectiles(self) -> None:
        """Update all projectile positions."""
        # Player projectiles move up (faster)
        for proj in self.player_projectiles[:]:
            proj['y'] -= PLAYER_PROJECTILE_SPEED
            if proj['y'] < 0:
                self.player_projectiles.remove(proj)

        # Alien projectiles move down (slower for easier dodging)
        for proj in self.alien_projectiles[:]:
            proj['y'] += ALIEN_PROJECTILE_SPEED
            if proj['y'] >= self.height:
                self.alien_projectiles.remove(proj)

    def _alien_fire(self) -> None:
        """Handle alien firing logic."""
        if not self.aliens:
            return

        fire_prob = self.get_alien_fire_probability()

        for alien in self.aliens:
            if random.random() < fire_prob:
                self.alien_projectiles.append({
                    'x': alien.x + 1,
                    'y': alien.y + 1
                })

    def _check_collisions(self) -> None:
        """Check all collision types."""
        # Player projectiles vs aliens
        for proj in self.player_projectiles[:]:
            for alien in self.aliens[:]:
                if (abs(proj['x'] - alien.x) <= 1 and
                    abs(proj['y'] - alien.y) <= 1):
                    self.aliens.remove(alien)
                    if proj in self.player_projectiles:
                        self.player_projectiles.remove(proj)
                    self.score += 10 * (3 - alien.alien_type)
                    if self.sfx:
                        self.sfx.play_alien_die()
                    break

        # Alien projectiles vs player
        for proj in self.alien_projectiles[:]:
            if (abs(proj['x'] - self.player.x - 1) <= 1 and
                proj['y'] >= self.player.y):
                self.alien_projectiles.remove(proj)
                self.handle_player_damage()
                break

        # Projectiles vs bunkers
        for proj in self.player_projectiles[:]:
            for bunker in self.bunkers[:]:
                if bunker.health > 0 and proj['x'] == bunker.x and proj['y'] == bunker.y:
                    bunker.hit()
                    if proj in self.player_projectiles:
                        self.player_projectiles.remove(proj)
                    break

        for proj in self.alien_projectiles[:]:
            for bunker in self.bunkers[:]:
                if bunker.health > 0 and proj['x'] == bunker.x and proj['y'] == bunker.y:
                    bunker.hit()
                    if proj in self.alien_projectiles:
                        self.alien_projectiles.remove(proj)
                    break

    def _next_level(self) -> None:
        """Advance to next level and award bonus lives."""
        # Award lives based on level completed (level 1 = +1, level 2 = +2, etc.)
        completed_level = self.level
        self.lives_awarded = completed_level
        lives_to_add = min(completed_level, MAX_LIVES - self.player.lives)
        self.player.lives = min(self.player.lives + lives_to_add, MAX_LIVES)

        # Play sound effects
        if self.sfx:
            self.sfx.play_level_complete()
            if lives_to_add > 0:
                self.sfx.play_life_bonus()

        self.level += 1
        self.state = GameState.LEVEL_TRANSITION
        self._init_aliens()
        self._init_bunkers()

        # Speed up aliens slightly each level
        global ALIEN_MOVE_INTERVAL
        ALIEN_MOVE_INTERVAL = max(0.1, ALIEN_MOVE_INTERVAL - 0.05)

    def handle_input(self, key: int) -> bool:
        """
        Handle keyboard input.

        Returns:
            False if game should quit, True otherwise.
        """
        if key == ord('q') or key == ord('Q'):
            return False

        if self.state == GameState.MENU:
            if key == ord(' ') or key == ord('\n'):
                self.state = GameState.PLAYING
                if self.audio:
                    self.audio.start()

        elif self.state == GameState.PLAYING:
            if key == curses.KEY_LEFT or key == ord('a'):
                self.player.x = max(0, self.player.x - PLAYER_SPEED)
            elif key == curses.KEY_RIGHT or key == ord('d'):
                self.player.x = min(self.width - 3, self.player.x + PLAYER_SPEED)
            elif key == ord(' '):
                # Fire projectile
                if len(self.player_projectiles) < 3:  # Limit active projectiles
                    self.player_projectiles.append({
                        'x': self.player.x + 1,
                        'y': self.player.y - 1
                    })
                    if self.sfx:
                        self.sfx.play_shoot()

        elif self.state == GameState.GAME_OVER:
            if key == ord('r') or key == ord('R'):
                self.reset_game()

        elif self.state == GameState.LEVEL_TRANSITION:
            if key == ord(' ') or key == ord('\n'):
                self.state = GameState.PLAYING

        return True

    def render(self) -> None:
        """Render the game to the screen."""
        if self.test_mode or not self.screen:
            return

        self.screen.clear()

        # Handle flash effect
        if self.flash_active:
            self.screen.bkgd(' ', curses.color_pair(COLOR_GAME_OVER))
        else:
            self.screen.bkgd(' ')

        if self.state == GameState.MENU:
            self._render_menu()
        elif self.state == GameState.PLAYING:
            self._render_game()
        elif self.state == GameState.GAME_OVER:
            self._render_game_over()
        elif self.state == GameState.LEVEL_TRANSITION:
            self._render_level_transition()

        self.screen.refresh()

    def _render_menu(self) -> None:
        """Render the main menu."""
        title = "SPACE INVADERS"
        subtitle = "Press SPACE to Start"
        controls = "Controls: A/D or Arrow Keys to Move, SPACE to Fire, Q to Quit"

        center_y = self.height // 2
        self._safe_addstr(center_y - 2, (self.width - len(title)) // 2,
                         title, curses.color_pair(COLOR_TEXT) | curses.A_BOLD)
        self._safe_addstr(center_y, (self.width - len(subtitle)) // 2,
                         subtitle, curses.color_pair(COLOR_TEXT))
        self._safe_addstr(center_y + 2, (self.width - len(controls)) // 2,
                         controls, curses.color_pair(COLOR_TEXT))

    def _render_game(self) -> None:
        """Render the main gameplay."""
        # Score and lives header
        score_text = f"Score: {self.score}"
        lives_display = f"Lives: {'<A> ' * self.player.lives}"
        level_text = f"Level: {self.level}"

        self._safe_addstr(0, 2, score_text, curses.color_pair(COLOR_TEXT))
        self._safe_addstr(0, self.width // 2 - len(level_text) // 2,
                         level_text, curses.color_pair(COLOR_TEXT))
        self._safe_addstr(0, self.width - len(lives_display) - 2,
                         lives_display, curses.color_pair(COLOR_TEXT))

        # Render aliens
        for alien in self.aliens:
            char = ALIEN_CHARS[alien.alien_type][self.alien_animation_frame]
            self._safe_addstr(alien.y, alien.x, char, curses.color_pair(COLOR_ALIEN))

        # Render bunkers
        for bunker in self.bunkers:
            if bunker.health > 0:
                self._safe_addstr(bunker.y, bunker.x, bunker.char,
                                 curses.color_pair(COLOR_BUNKER))

        # Render player
        self._safe_addstr(self.player.y, self.player.x, PLAYER_CHAR,
                         curses.color_pair(COLOR_PLAYER))

        # Render projectiles
        for proj in self.player_projectiles:
            self._safe_addstr(proj['y'], proj['x'], PROJECTILE_PLAYER,
                             curses.color_pair(COLOR_PROJECTILE))

        for proj in self.alien_projectiles:
            self._safe_addstr(proj['y'], proj['x'], PROJECTILE_ALIEN,
                             curses.color_pair(COLOR_GAME_OVER))

    def _render_game_over(self) -> None:
        """Render the game over screen."""
        self._render_game()  # Show final game state

        game_over_text = f"GAME OVER - Score: {self.score}"
        restart_text = "Press 'R' to Restart or 'Q' to Quit"

        center_y = self.height // 2
        self._safe_addstr(center_y, (self.width - len(game_over_text)) // 2,
                         game_over_text,
                         curses.color_pair(COLOR_GAME_OVER) | curses.A_BOLD)
        self._safe_addstr(center_y + 2, (self.width - len(restart_text)) // 2,
                         restart_text, curses.color_pair(COLOR_TEXT))

    def _render_level_transition(self) -> None:
        """Render level transition screen with bonus lives info."""
        level_text = f"LEVEL {self.level}"
        bonus_text = f"+{self.lives_awarded} {'LIFE' if self.lives_awarded == 1 else 'LIVES'} BONUS!"
        lives_text = f"Lives: {self.player.lives}/{MAX_LIVES}"
        continue_text = "Press SPACE to Continue"

        center_y = self.height // 2
        self._safe_addstr(center_y - 2, (self.width - len(level_text)) // 2,
                         level_text,
                         curses.color_pair(COLOR_TEXT) | curses.A_BOLD)
        self._safe_addstr(center_y, (self.width - len(bonus_text)) // 2,
                         bonus_text,
                         curses.color_pair(COLOR_PLAYER) | curses.A_BOLD)
        self._safe_addstr(center_y + 1, (self.width - len(lives_text)) // 2,
                         lives_text, curses.color_pair(COLOR_TEXT))
        self._safe_addstr(center_y + 3, (self.width - len(continue_text)) // 2,
                         continue_text, curses.color_pair(COLOR_TEXT))

    def _safe_addstr(self, y, x, text: str, attr: int = 0) -> None:
        """Safely add string to screen, handling boundary issues."""
        try:
            # Convert to int to handle float coordinates from projectile speed
            y_int = int(y)
            x_int = int(x)
            if 0 <= y_int < self.height and 0 <= x_int < self.width:
                # Truncate if necessary
                max_len = self.width - x_int - 1
                if max_len > 0:
                    self.screen.addstr(y_int, x_int, text[:max_len], attr)
        except curses.error:
            pass  # Ignore curses errors at boundaries

    def run(self) -> None:
        """Main game loop with curses."""
        def main(stdscr):
            # Setup curses
            self.screen = stdscr
            curses.curs_set(0)  # Hide cursor
            stdscr.nodelay(True)  # Non-blocking input
            stdscr.timeout(INPUT_TIMEOUT_MS)  # Fast input polling for responsive movement

            # Initialize colors
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(COLOR_PLAYER, curses.COLOR_GREEN, -1)
            curses.init_pair(COLOR_ALIEN, curses.COLOR_MAGENTA, -1)
            curses.init_pair(COLOR_BUNKER, curses.COLOR_CYAN, -1)
            curses.init_pair(COLOR_TEXT, curses.COLOR_YELLOW, -1)
            curses.init_pair(COLOR_PROJECTILE, curses.COLOR_WHITE, -1)
            curses.init_pair(COLOR_GAME_OVER, curses.COLOR_RED, -1)

            # Get screen dimensions
            self.height, self.width = stdscr.getmaxyx()

            # Check minimum size
            if self.width < MIN_WIDTH or self.height < MIN_HEIGHT:
                stdscr.addstr(0, 0, f"Terminal too small! Need {MIN_WIDTH}x{MIN_HEIGHT}")
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
                        pass

                    # Update game state
                    self.update()

                    # Render
                    self.render()

                    # Frame rate limiting
                    elapsed = time.time() - frame_start
                    sleep_time = FRAME_TIME - elapsed
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
    game = Game()
    game.run()


if __name__ == '__main__':
    main()
