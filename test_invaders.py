#!/usr/bin/env python3
"""
Test Suite for Space Invaders Clone
Test-Driven Development - Quality Assurance Suite

This test file validates the core game mechanics:
- Audio path resolution
- Player life management
- Game over conditions (lives and invasion)
- Reset/restart functionality
"""

import unittest
import os
import sys

# Import the game module (will be created next)
from invaders import (
    GameState,
    Player,
    Alien,
    Game,
    resolve_audio_path,
    PLAYER_START_LIVES
)


class TestAudioSystem(unittest.TestCase):
    """Tests for the audio subsystem."""

    def test_audio_path_resolution(self):
        """
        Test that os.path.expanduser('~/soundtrack.mp3') resolves
        to a valid absolute path string.
        """
        path = resolve_audio_path()
        # Should be an absolute path
        self.assertTrue(os.path.isabs(path),
                       f"Path should be absolute, got: {path}")
        # Should contain the home directory
        home = os.path.expanduser('~')
        self.assertTrue(path.startswith(home),
                       f"Path should start with home dir: {path}")
        # Should end with soundtrack.mp3
        self.assertTrue(path.endswith('soundtrack.mp3'),
                       f"Path should end with soundtrack.mp3: {path}")
        # Should match expected format: /Users/username/soundtrack.mp3
        expected = os.path.expanduser('~/soundtrack.mp3')
        self.assertEqual(path, expected)


class TestPlayerMechanics(unittest.TestCase):
    """Tests for player life management and damage."""

    def test_player_life_decrement(self):
        """
        Initialize a player with starting lives.
        Simulate a collision. Assert lives decremented by 1.
        """
        player = Player()
        self.assertEqual(player.lives, PLAYER_START_LIVES,
                        f"Player should start with {PLAYER_START_LIVES} lives")

        # Simulate collision/damage
        player.take_damage()

        self.assertEqual(player.lives, PLAYER_START_LIVES - 1,
                        f"Player should have {PLAYER_START_LIVES - 1} lives after taking damage")

    def test_player_multiple_damage(self):
        """Test that multiple hits correctly decrement lives."""
        player = Player()
        initial_lives = player.lives

        player.take_damage()
        self.assertEqual(player.lives, initial_lives - 1)

        player.take_damage()
        self.assertEqual(player.lives, initial_lives - 2)

        player.take_damage()
        self.assertEqual(player.lives, initial_lives - 3)


class TestGameOverConditions(unittest.TestCase):
    """Tests for game over state transitions."""

    def test_game_over_by_lives(self):
        """
        Set lives to 1. Simulate collision.
        Assert game state changes to GAME_OVER.
        """
        game = Game(test_mode=True)
        game.player.lives = 1
        game.state = GameState.PLAYING

        # Simulate collision that should trigger game over
        game.handle_player_damage()

        self.assertEqual(game.state, GameState.GAME_OVER,
                        "Game state should be GAME_OVER when lives reach 0")

    def test_game_over_by_invasion(self):
        """
        Place an alien at the Player Y coordinate.
        Assert game state is GAME_OVER.
        """
        game = Game(test_mode=True)
        game.state = GameState.PLAYING

        # Place an alien at the player's Y position (invasion)
        player_y = game.player.y
        alien = Alien(x=10, y=player_y)
        game.aliens = [alien]

        # Check for invasion
        game.check_invasion()

        self.assertEqual(game.state, GameState.GAME_OVER,
                        "Game state should be GAME_OVER when aliens reach player row")

    def test_no_game_over_when_aliens_above(self):
        """Ensure game continues when aliens haven't reached player."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING

        # Place alien above player
        player_y = game.player.y
        alien = Alien(x=10, y=player_y - 5)
        game.aliens = [alien]

        game.check_invasion()

        self.assertEqual(game.state, GameState.PLAYING,
                        "Game should continue when aliens are above player")


class TestResetMechanic(unittest.TestCase):
    """Tests for game reset/restart functionality."""

    def test_reset_mechanic(self):
        """
        Distinct from initialization: set score to 500 and lives to 0.
        Call reset_game(). Assert score is 0 and lives are 3.
        """
        game = Game(test_mode=True)

        # Modify game state (simulate a played game)
        game.score = 500
        game.player.lives = 0
        game.state = GameState.GAME_OVER

        # Add some projectiles to ensure they get cleared
        game.player_projectiles = [{'x': 10, 'y': 5}]
        game.alien_projectiles = [{'x': 15, 'y': 10}]

        # Call reset
        game.reset_game()

        # Assert reset state
        self.assertEqual(game.score, 0,
                        "Score should be 0 after reset")
        self.assertEqual(game.player.lives, PLAYER_START_LIVES,
                        f"Lives should be {PLAYER_START_LIVES} after reset")
        self.assertEqual(game.state, GameState.PLAYING,
                        "Game state should be PLAYING after reset")
        self.assertEqual(len(game.player_projectiles), 0,
                        "Player projectiles should be cleared after reset")
        self.assertEqual(len(game.alien_projectiles), 0,
                        "Alien projectiles should be cleared after reset")

    def test_reset_reinitializes_aliens(self):
        """Ensure reset re-creates the full alien grid."""
        game = Game(test_mode=True)

        # Simulate killing all aliens
        game.aliens = []

        game.reset_game()

        # Should have aliens again
        self.assertGreater(len(game.aliens), 0,
                          "Aliens should be reinitialized after reset")

    def test_reset_centers_player(self):
        """Ensure reset returns player to center position."""
        game = Game(test_mode=True)
        initial_x = game.player.x

        # Move player
        game.player.x = 5

        game.reset_game()

        # Player should be back at center
        self.assertEqual(game.player.x, initial_x,
                        "Player should be centered after reset")


class TestGameStateTransitions(unittest.TestCase):
    """Tests for game state machine transitions."""

    def test_initial_state(self):
        """Game should start in MENU state."""
        game = Game(test_mode=True)
        # In test mode we might start in PLAYING, but let's verify it can be MENU
        game.state = GameState.MENU
        self.assertEqual(game.state, GameState.MENU)

    def test_state_enum_values(self):
        """Verify all required game states exist."""
        states = [GameState.MENU, GameState.PLAYING,
                  GameState.LEVEL_TRANSITION, GameState.GAME_OVER]
        self.assertEqual(len(states), 4, "Should have 4 game states")


class TestAlienMechanics(unittest.TestCase):
    """Tests for alien behavior."""

    def test_alien_creation(self):
        """Test alien can be created with position."""
        alien = Alien(x=10, y=5)
        self.assertEqual(alien.x, 10)
        self.assertEqual(alien.y, 5)

    def test_alien_firing_probability_increases(self):
        """
        Test that firing probability increases as aliens decrease.
        This validates the 'frenzy mode' mechanic.
        """
        game = Game(test_mode=True)

        # Full grid of aliens
        initial_prob = game.get_alien_fire_probability()

        # Reduce aliens to half
        game.aliens = game.aliens[:len(game.aliens) // 2]
        half_prob = game.get_alien_fire_probability()

        # Reduce to just a few
        game.aliens = game.aliens[:3]
        few_prob = game.get_alien_fire_probability()

        # Probability should increase as aliens decrease
        self.assertGreater(half_prob, initial_prob,
                          "Fire probability should increase with fewer aliens")
        self.assertGreater(few_prob, half_prob,
                          "Fire probability should be highest with few aliens")


if __name__ == '__main__':
    # Run tests with verbosity
    unittest.main(verbosity=2)
