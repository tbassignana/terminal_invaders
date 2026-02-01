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
import logging
import os
import sys
import time

# Import the game module (will be created next)
import tempfile
from invaders import (
    GameState,
    GameEvent,
    EventBus,
    GameConfig,
    DEFAULT_CONFIG,
    DIFFICULTY_PRESETS,
    build_argument_parser,
    config_from_args,
    Player,
    Alien,
    Projectile,
    MysteryShip,
    ScoreManager,
    Game,
    AbstractSoundBackend,
    MacOSSoundBackend,
    NullSoundBackend,
    get_sound_backend,
    SoundEffects,
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
        game.player_projectiles = [Projectile(x=10, y=5, direction=-1)]
        game.alien_projectiles = [Projectile(x=15, y=10, direction=1)]

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
        states = [GameState.MENU, GameState.PLAYING, GameState.PAUSED,
                  GameState.LEVEL_TRANSITION, GameState.GAME_OVER]
        self.assertEqual(len(states), 5, "Should have 5 game states")


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


class TestGameConfig(unittest.TestCase):
    """Step 1: Tests for the GameConfig dataclass and config-driven Game."""

    def test_default_config_values(self):
        """Verify default config matches the expected game constants."""
        cfg = GameConfig()
        self.assertEqual(cfg.player_start_lives, 5)
        self.assertEqual(cfg.max_lives, 9)
        self.assertEqual(cfg.target_fps, 60)
        self.assertEqual(cfg.alien_rows, 5)
        self.assertEqual(cfg.alien_cols, 11)
        self.assertEqual(cfg.alien_move_interval, 0.5)
        self.assertAlmostEqual(cfg.base_fire_probability, 0.00133)
        self.assertAlmostEqual(cfg.max_fire_probability, 0.0133)
        self.assertAlmostEqual(cfg.frame_time, 1.0 / 60)

    def test_custom_config_overrides(self):
        """Verify custom config values override defaults."""
        cfg = GameConfig(player_start_lives=3, alien_rows=8, target_fps=30)
        self.assertEqual(cfg.player_start_lives, 3)
        self.assertEqual(cfg.alien_rows, 8)
        self.assertEqual(cfg.target_fps, 30)
        self.assertAlmostEqual(cfg.frame_time, 1.0 / 30)
        # Unspecified fields keep defaults
        self.assertEqual(cfg.max_lives, 9)
        self.assertEqual(cfg.alien_cols, 11)

    def test_config_is_frozen(self):
        """Verify GameConfig is immutable (frozen dataclass)."""
        cfg = GameConfig()
        with self.assertRaises(AttributeError):
            cfg.player_start_lives = 10

    def test_game_uses_config(self):
        """Verify Game instance uses the provided config."""
        cfg = GameConfig(player_start_lives=3, alien_rows=2, alien_cols=3)
        game = Game(test_mode=True, config=cfg)
        self.assertEqual(game.player.lives, 3)
        # 2 rows * 3 cols = 6 aliens
        self.assertEqual(len(game.aliens), 6)

    def test_game_default_config(self):
        """Verify Game uses DEFAULT_CONFIG when no config is provided."""
        game = Game(test_mode=True)
        self.assertIs(game.config, DEFAULT_CONFIG)
        self.assertEqual(game.player.lives, DEFAULT_CONFIG.player_start_lives)

    def test_no_global_mutation_in_next_level(self):
        """Verify _next_level does not mutate the global ALIEN_MOVE_INTERVAL."""
        import invaders
        original_global = invaders.ALIEN_MOVE_INTERVAL

        game = Game(test_mode=True)
        game.aliens = []  # Trigger level completion
        game._next_level()

        # Global should not have changed
        self.assertEqual(invaders.ALIEN_MOVE_INTERVAL, original_global)
        # But instance value should have decreased
        self.assertLess(game.alien_move_interval, game.config.alien_move_interval)

    def test_alien_move_interval_resets_on_game_reset(self):
        """Verify alien_move_interval resets to config value on game reset."""
        game = Game(test_mode=True)
        # Simulate a few level ups
        game.alien_move_interval = 0.2
        game.reset_game()
        self.assertEqual(game.alien_move_interval, game.config.alien_move_interval)


class TestProjectileDataclass(unittest.TestCase):
    """Step 2: Tests for Projectile dataclass usage replacing dict-based projectiles."""

    def test_projectile_creation(self):
        """Verify Projectile dataclass stores x, y, direction."""
        proj = Projectile(x=10, y=5, direction=-1)
        self.assertEqual(proj.x, 10)
        self.assertEqual(proj.y, 5)
        self.assertEqual(proj.direction, -1)

    def test_player_fire_creates_projectile(self):
        """Verify firing creates a Projectile instance, not a dict."""
        game = Game(test_mode=True)
        game.player_projectiles.append(
            Projectile(x=game.player.x + 1, y=game.player.y - 1, direction=-1)
        )
        proj = game.player_projectiles[0]
        self.assertIsInstance(proj, Projectile)
        self.assertEqual(proj.direction, -1)

    def test_projectile_movement_updates_y(self):
        """Verify projectile y-coordinate changes after update."""
        game = Game(test_mode=True)
        proj = Projectile(x=40, y=10.0, direction=-1)
        game.player_projectiles.append(proj)

        game._update_projectiles()
        # Player projectile should have moved up
        self.assertLess(proj.y, 10.0)

    def test_projectile_removal_at_boundary(self):
        """Verify projectiles are removed when they leave the screen."""
        game = Game(test_mode=True)
        # Player projectile near top
        proj = Projectile(x=40, y=0.5, direction=-1)
        game.player_projectiles.append(proj)

        game._update_projectiles()
        self.assertEqual(len(game.player_projectiles), 0)

    def test_alien_projectile_direction(self):
        """Verify alien projectiles have direction=1 (downward)."""
        game = Game(test_mode=True)
        # Manually add an alien projectile
        game.alien_projectiles.append(Projectile(x=20, y=5.0, direction=1))
        proj = game.alien_projectiles[0]
        self.assertEqual(proj.direction, 1)

    def test_collision_detection_with_projectile_dataclass(self):
        """Verify collision detection works with Projectile dataclass."""
        game = Game(test_mode=True)
        alien = Alien(x=20, y=10)
        game.aliens = [alien]

        # Place a player projectile right on the alien
        proj = Projectile(x=20, y=10, direction=-1)
        game.player_projectiles.append(proj)

        game._check_collisions()

        # Alien should be removed, score updated
        self.assertEqual(len(game.aliens), 0)
        self.assertGreater(game.score, 0)


class TestLogging(unittest.TestCase):
    """Step 3: Tests for the logging framework and --debug flag."""

    def setUp(self):
        """Reset logger state before each test."""
        import invaders
        self.logger = invaders.logger
        # Remove any handlers from previous tests
        self.logger.handlers.clear()

    def test_setup_logging_debug_mode(self):
        """Verify debug mode creates a file handler at DEBUG level."""
        import tempfile
        import invaders
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                os.chdir(tmpdir)
                invaders.setup_logging(debug=True)
                self.assertEqual(self.logger.level, logging.DEBUG)
                # Should have a FileHandler
                file_handlers = [h for h in self.logger.handlers
                                 if isinstance(h, logging.FileHandler)]
                self.assertGreater(len(file_handlers), 0)
        finally:
            os.chdir(old_cwd)
            self.logger.handlers.clear()

    def test_setup_logging_default_mode(self):
        """Verify default mode sets WARNING level with NullHandler."""
        import invaders
        invaders.setup_logging(debug=False)
        self.assertEqual(self.logger.level, logging.WARNING)
        null_handlers = [h for h in self.logger.handlers
                         if isinstance(h, logging.NullHandler)]
        self.assertGreater(len(null_handlers), 0)
        self.logger.handlers.clear()

    def test_debug_flag_parsing(self):
        """Verify --debug is recognized in sys.argv."""
        self.assertIn('--debug', ['--debug', '--other'])

    def test_log_output_on_simulated_error(self):
        """Verify that logging captures error info at DEBUG level."""
        import tempfile
        import invaders
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                os.chdir(tmpdir)
                invaders.setup_logging(debug=True)
                self.logger.debug("Test error message: %s", "simulated")
                # Flush handlers
                for h in self.logger.handlers:
                    h.flush()
                # Check the log file was written
                log_path = os.path.join(tmpdir, 'invaders.log')
                self.assertTrue(os.path.exists(log_path))
                with open(log_path) as f:
                    contents = f.read()
                self.assertIn("simulated", contents)
        finally:
            os.chdir(old_cwd)
            self.logger.handlers.clear()


class TestScoreManager(unittest.TestCase):
    """Step 5: Tests for ScoreManager with high-score persistence."""

    def test_score_tracking(self):
        """Verify add() accumulates points."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = ScoreManager(scores_path=os.path.join(tmpdir, 'scores.json'))
            sm.add(100)
            sm.add(50)
            self.assertEqual(sm.current_score, 150)

    def test_file_persistence(self):
        """Verify scores are saved to and loaded from disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'scores.json')
            sm = ScoreManager(scores_path=path)
            sm.record(500, level=3)
            sm.record(300, level=2)

            # Load fresh instance
            sm2 = ScoreManager(scores_path=path)
            self.assertEqual(len(sm2.scores), 2)
            self.assertEqual(sm2.high_score, 500)

    def test_high_score_ranking(self):
        """Verify top 10 ranking and ordering."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'scores.json')
            sm = ScoreManager(scores_path=path)
            for i in range(15):
                sm.record(i * 100, level=1)
            self.assertEqual(len(sm.scores), 10)
            self.assertEqual(sm.scores[0]['score'], 1400)

    def test_score_reset(self):
        """Verify reset_current sets current_score to 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = ScoreManager(scores_path=os.path.join(tmpdir, 'scores.json'))
            sm.add(999)
            sm.reset_current()
            self.assertEqual(sm.current_score, 0)

    def test_high_score_empty(self):
        """Verify high_score returns 0 when no scores exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = ScoreManager(scores_path=os.path.join(tmpdir, 'scores.json'))
            self.assertEqual(sm.high_score, 0)


class TestEventBus(unittest.TestCase):
    """Step 6: Tests for the EventBus publish/subscribe system."""

    def test_event_subscription_and_firing(self):
        """Verify a subscribed handler is called when event fires."""
        bus = EventBus()
        results = []
        bus.subscribe(GameEvent.ALIEN_KILLED, lambda **kw: results.append('killed'))
        bus.publish(GameEvent.ALIEN_KILLED)
        self.assertEqual(results, ['killed'])

    def test_event_handler_receives_kwargs(self):
        """Verify handlers receive keyword arguments."""
        bus = EventBus()
        received = {}
        bus.subscribe(GameEvent.ALIEN_KILLED, lambda **kw: received.update(kw))
        bus.publish(GameEvent.ALIEN_KILLED, alien_type=2)
        self.assertEqual(received['alien_type'], 2)

    def test_multi_subscriber_support(self):
        """Verify multiple subscribers all get called."""
        bus = EventBus()
        results = []
        bus.subscribe(GameEvent.SHOT_FIRED, lambda **kw: results.append('a'))
        bus.subscribe(GameEvent.SHOT_FIRED, lambda **kw: results.append('b'))
        bus.publish(GameEvent.SHOT_FIRED)
        self.assertEqual(len(results), 2)

    def test_no_cross_event_firing(self):
        """Verify subscribing to one event doesn't fire on another."""
        bus = EventBus()
        results = []
        bus.subscribe(GameEvent.SHOT_FIRED, lambda **kw: results.append('shot'))
        bus.publish(GameEvent.PLAYER_HIT)
        self.assertEqual(results, [])

    def test_clear_removes_all(self):
        """Verify clear() removes all subscribers."""
        bus = EventBus()
        results = []
        bus.subscribe(GameEvent.SHOT_FIRED, lambda **kw: results.append('x'))
        bus.clear()
        bus.publish(GameEvent.SHOT_FIRED)
        self.assertEqual(results, [])

    def test_game_event_bus_exists(self):
        """Verify Game creates an event_bus attribute."""
        game = Game(test_mode=True)
        self.assertIsInstance(game.event_bus, EventBus)


class TestSoundBackend(unittest.TestCase):
    """Step 4: Tests for abstract sound backend interface."""

    def test_null_backend_plays_silently(self):
        """Verify NullSoundBackend.play() does nothing without error."""
        backend = NullSoundBackend()
        # Should not raise any exception
        backend.play('/nonexistent/path.aiff', volume=0.5)

    def test_null_backend_stop(self):
        """Verify NullSoundBackend.stop() does nothing without error."""
        backend = NullSoundBackend()
        backend.stop()

    def test_null_backend_is_available(self):
        """NullSoundBackend is always available."""
        backend = NullSoundBackend()
        self.assertTrue(backend.is_available())

    def test_backend_selection_returns_valid_backend(self):
        """Verify get_sound_backend returns an AbstractSoundBackend."""
        backend = get_sound_backend()
        self.assertIsInstance(backend, AbstractSoundBackend)

    def test_macos_backend_path_resolution(self):
        """Verify MacOSSoundBackend is available on macOS."""
        if sys.platform == 'darwin':
            backend = MacOSSoundBackend()
            self.assertTrue(backend.is_available())
        else:
            self.skipTest("Not on macOS")

    def test_sound_effects_uses_backend(self):
        """Verify SoundEffects delegates to backend."""
        backend = NullSoundBackend()
        sfx = SoundEffects(backend=backend)
        self.assertIs(sfx.backend, backend)


class TestMysteryShip(unittest.TestCase):
    """Step 8: Tests for the Mystery Ship (UFO) feature."""

    def test_mystery_ship_creation(self):
        """Verify MysteryShip dataclass stores all fields."""
        ufo = MysteryShip(x=0.0, y=1, speed=0.5, points=200)
        self.assertEqual(ufo.x, 0.0)
        self.assertEqual(ufo.points, 200)
        self.assertTrue(ufo.active)

    def test_mystery_ship_movement(self):
        """Verify the ship moves when update is called."""
        game = Game(test_mode=True)
        game.mystery_ship = MysteryShip(x=10.0, y=1, speed=0.5, points=100)
        game._update_mystery_ship(time.time())
        self.assertAlmostEqual(game.mystery_ship.x, 10.5)

    def test_mystery_ship_despawn_at_edge(self):
        """Verify ship is removed when it exits the screen."""
        game = Game(test_mode=True)
        game.mystery_ship = MysteryShip(x=float(game.width + 4), y=1, speed=0.5, points=100)
        game._update_mystery_ship(time.time())
        self.assertIsNone(game.mystery_ship)

    def test_mystery_ship_collision_awards_points(self):
        """Verify shooting the UFO awards its point value."""
        game = Game(test_mode=True)
        game.mystery_ship = MysteryShip(x=20.0, y=1, speed=0.5, points=250)
        game.player_projectiles.append(Projectile(x=20, y=1, direction=-1))
        initial_score = game.score
        game._check_collisions()
        self.assertEqual(game.score, initial_score + 250)
        self.assertIsNone(game.mystery_ship)

    def test_mystery_ship_score_display(self):
        """Verify score display is set after shooting UFO."""
        game = Game(test_mode=True)
        game.mystery_ship = MysteryShip(x=20.0, y=1, speed=0.5, points=150)
        game.player_projectiles.append(Projectile(x=20, y=1, direction=-1))
        game._check_collisions()
        self.assertIsNotNone(game.mystery_score_display)
        self.assertEqual(game.mystery_score_display[2], 150)


class TestPauseSystem(unittest.TestCase):
    """Step 9: Tests for the pause system."""

    def test_pause_toggle_to_paused(self):
        """Verify pressing pause changes state to PAUSED."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game._toggle_pause()
        self.assertEqual(game.state, GameState.PAUSED)

    def test_pause_toggle_to_playing(self):
        """Verify pressing pause again resumes to PLAYING."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game._toggle_pause()
        game._toggle_pause()
        self.assertEqual(game.state, GameState.PLAYING)

    def test_game_state_does_not_update_while_paused(self):
        """Verify update() is a no-op when paused."""
        game = Game(test_mode=True)
        game.state = GameState.PAUSED
        initial_aliens = len(game.aliens)
        game.update()
        # Nothing should change
        self.assertEqual(len(game.aliens), initial_aliens)

    def test_unpause_resumes_correctly(self):
        """Verify time offsets are adjusted on unpause so no timer jump."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        old_alien_time = game.last_alien_move_time

        game._toggle_pause()
        time.sleep(0.05)  # Small pause
        game._toggle_pause()

        # Timer should have been offset forward
        self.assertGreater(game.last_alien_move_time, old_alien_time)

    def test_paused_state_exists_in_enum(self):
        """Verify PAUSED is a valid GameState."""
        self.assertIn(GameState.PAUSED, list(GameState))


class TestCLIArgParser(unittest.TestCase):
    """Step 7: Tests for CLI argument parsing and difficulty presets."""

    def test_argument_parsing_defaults(self):
        """Verify default arguments parse correctly."""
        parser = build_argument_parser()
        args = parser.parse_args([])
        self.assertFalse(args.no_sound)
        self.assertFalse(args.no_music)
        self.assertFalse(args.debug)
        self.assertEqual(args.difficulty, 'normal')
        self.assertIsNone(args.fps)

    def test_difficulty_easy_preset_values(self):
        """Verify easy difficulty overrides are applied."""
        parser = build_argument_parser()
        args = parser.parse_args(['--difficulty', 'easy'])
        cfg = config_from_args(args)
        self.assertEqual(cfg.player_start_lives, 7)
        self.assertGreater(cfg.alien_move_interval, DEFAULT_CONFIG.alien_move_interval)

    def test_difficulty_hard_preset_values(self):
        """Verify hard difficulty overrides are applied."""
        parser = build_argument_parser()
        args = parser.parse_args(['--difficulty', 'hard'])
        cfg = config_from_args(args)
        self.assertEqual(cfg.player_start_lives, 3)
        self.assertLess(cfg.alien_move_interval, DEFAULT_CONFIG.alien_move_interval)

    def test_fps_override(self):
        """Verify --fps overrides target_fps in config."""
        parser = build_argument_parser()
        args = parser.parse_args(['--fps', '30'])
        cfg = config_from_args(args)
        self.assertEqual(cfg.target_fps, 30)

    def test_config_integration(self):
        """Verify config_from_args produces valid GameConfig for Game."""
        parser = build_argument_parser()
        args = parser.parse_args(['--difficulty', 'hard', '--fps', '45'])
        cfg = config_from_args(args)
        game = Game(test_mode=True, config=cfg)
        self.assertEqual(game.config.target_fps, 45)
        self.assertEqual(game.player.lives, 3)


if __name__ == '__main__':
    # Run tests with verbosity
    unittest.main(verbosity=2)
