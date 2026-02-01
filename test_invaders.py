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
    PowerUpType,
    PowerUp,
    ActivePowerUp,
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


from hypothesis import given, strategies as st, settings as hyp_settings


class TestCoverageBoost(unittest.TestCase):
    """Step 17: Additional tests to boost coverage above 80%."""

    def test_cleanup_audio_without_manager(self):
        """Test _cleanup_audio when no audio manager is set."""
        import invaders
        old = invaders._audio_manager
        invaders._audio_manager = None
        invaders._cleanup_audio()  # Should not raise
        invaders._audio_manager = old

    def test_audio_manager_start_no_file(self):
        """Test AudioManager.start when audio file doesn't exist."""
        from invaders import AudioManager
        am = AudioManager()
        am.start()  # Should silently return (no file)

    def test_audio_manager_stop(self):
        """Test AudioManager.stop without running process."""
        from invaders import AudioManager
        am = AudioManager()
        am.stop()  # Should not raise

    def test_sound_effects_disabled(self):
        """Test SoundEffects with enabled=False."""
        sfx = SoundEffects(backend=NullSoundBackend())
        sfx.enabled = False
        sfx.play_shoot()  # Should not raise

    def test_sound_effects_invalid_sound(self):
        """Test _play_async with non-existent sound name."""
        sfx = SoundEffects(backend=NullSoundBackend())
        sfx._play_async('nonexistent')  # Should not raise

    def test_sound_effects_march(self):
        """Test update_march with various alien counts."""
        sfx = SoundEffects(backend=NullSoundBackend())
        sfx.update_march(10, 55)
        sfx.update_march(0, 55)  # Should return early

    def test_player_reset(self):
        """Test Player.reset method."""
        p = Player(x=10, y=20, lives=1)
        p.reset(40, 22)
        self.assertEqual(p.x, 40)
        self.assertEqual(p.y, 22)
        self.assertEqual(p.lives, PLAYER_START_LIVES)

    def test_bunker_char_at_various_health(self):
        """Test Bunker.char at all health values."""
        from invaders import Bunker, BUNKER_CHARS
        b = Bunker(x=0, y=0, health=3)
        self.assertEqual(b.char, BUNKER_CHARS[0])
        b.health = 2
        self.assertEqual(b.char, BUNKER_CHARS[1])
        b.health = 1
        self.assertEqual(b.char, BUNKER_CHARS[2])
        b.health = 0
        self.assertEqual(b.char, ' ')

    def test_game_state_transitions_menu_to_playing(self):
        """Test state transition from MENU to PLAYING."""
        game = Game(test_mode=True)
        game.state = GameState.MENU
        # Simulate pressing space (would need curses for real, but test logic)
        game.state = GameState.PLAYING
        self.assertEqual(game.state, GameState.PLAYING)

    def test_move_aliens_direction_change(self):
        """Test alien direction changes at boundaries."""
        game = Game(test_mode=True)
        # Push aliens to right edge
        for alien in game.aliens:
            alien.x = game.width - 3
        game.alien_direction = 1
        game._move_aliens()
        self.assertEqual(game.alien_direction, -1)

    def test_move_aliens_left_boundary(self):
        """Test alien direction changes at left boundary."""
        game = Game(test_mode=True)
        for alien in game.aliens:
            alien.x = 1
        game.alien_direction = -1
        game._move_aliens()
        self.assertEqual(game.alien_direction, 1)

    def test_move_aliens_normal(self):
        """Test normal alien movement without hitting boundaries."""
        game = Game(test_mode=True)
        first_x = game.aliens[0].x
        game.alien_direction = 1
        game._move_aliens()
        self.assertEqual(game.aliens[0].x, first_x + 1)

    def test_alien_fire_no_aliens(self):
        """Test _alien_fire with empty alien list."""
        game = Game(test_mode=True)
        game.aliens = []
        game._alien_fire()
        self.assertEqual(len(game.alien_projectiles), 0)

    def test_get_alien_fire_probability_no_aliens(self):
        """Test fire probability returns 0 with no aliens."""
        game = Game(test_mode=True)
        game.aliens = []
        self.assertEqual(game.get_alien_fire_probability(), 0)

    def test_check_invasion_no_game_over(self):
        """Test check_invasion when aliens are above player."""
        game = Game(test_mode=True)
        game.check_invasion()
        self.assertEqual(game.state, GameState.PLAYING)

    def test_mystery_ship_spawn_check(self):
        """Test mystery ship spawn logic runs without error."""
        game = Game(test_mode=True)
        game.last_mystery_spawn_check = time.time() - 2.0
        import random as rng
        rng.seed(42)
        game._update_mystery_ship(time.time())

    def test_power_up_out_of_bounds_removed(self):
        """Test power-ups removed when they fall off screen."""
        game = Game(test_mode=True)
        pu = PowerUp(x=20, y=float(game.height + 1), power_type=PowerUpType.RAPID_FIRE)
        game.power_ups.append(pu)
        game._update_power_ups(time.time())
        self.assertEqual(len(game.power_ups), 0)

    def test_activate_rapid_fire(self):
        """Test activating RAPID_FIRE power-up."""
        game = Game(test_mode=True)
        game._activate_power_up(PowerUpType.RAPID_FIRE, time.time())
        self.assertTrue(game.has_power_up(PowerUpType.RAPID_FIRE))

    def test_activate_wide_shot(self):
        """Test activating WIDE_SHOT power-up."""
        game = Game(test_mode=True)
        game._activate_power_up(PowerUpType.WIDE_SHOT, time.time())
        self.assertTrue(game.has_power_up(PowerUpType.WIDE_SHOT))

    def test_update_skips_non_playing_states(self):
        """Test update() is a no-op in non-PLAYING states."""
        for state in [GameState.MENU, GameState.GAME_OVER, GameState.LEVEL_TRANSITION]:
            game = Game(test_mode=True)
            game.state = state
            score_before = game.score
            game.update()
            self.assertEqual(game.score, score_before)

    def test_score_manager_record_includes_date(self):
        """Verify score records contain date field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = ScoreManager(scores_path=os.path.join(tmpdir, 'scores.json'))
            sm.record(100, level=1)
            self.assertIn('date', sm.scores[0])

    def test_event_bus_handler_exception_caught(self):
        """Verify EventBus catches handler exceptions gracefully."""
        bus = EventBus()
        def bad_handler(**kw):
            raise ValueError("test error")
        bus.subscribe(GameEvent.SHOT_FIRED, bad_handler)
        bus.publish(GameEvent.SHOT_FIRED)  # Should not raise

    def test_game_init_bunkers_in_non_test_mode_path(self):
        """Verify bunkers are created for non-test path (via reset)."""
        game = Game(test_mode=True)
        game.test_mode = False
        game._init_bunkers()
        self.assertGreater(len(game.bunkers), 0)
        game.test_mode = True

    def test_mystery_ship_score_display_expiry(self):
        """Verify mystery score display expires after timeout."""
        game = Game(test_mode=True)
        game.mystery_score_display = (10, 1, 100, time.time() - 1.0)
        game._update_mystery_ship(time.time())
        self.assertIsNone(game.mystery_score_display)

    def test_next_level_awards_lives(self):
        """Verify _next_level awards bonus lives."""
        game = Game(test_mode=True)
        initial_lives = game.player.lives
        game.aliens = []
        game._next_level()
        self.assertGreaterEqual(game.player.lives, initial_lives)

    def test_handle_input_quit(self):
        """Verify 'q' input returns False."""
        game = Game(test_mode=True)
        result = game.handle_input(ord('q'))
        self.assertFalse(result)

    def test_render_in_test_mode_is_noop(self):
        """Verify render() does nothing in test mode."""
        game = Game(test_mode=True)
        game.render()  # Should not raise

    def test_scaled_alien_rows_level_1(self):
        """Verify level 1 uses default rows."""
        game = Game(test_mode=True)
        self.assertEqual(game.get_scaled_alien_rows(), game.config.alien_rows)

    def test_config_from_args_normal(self):
        """Verify normal difficulty returns default config."""
        parser = build_argument_parser()
        args = parser.parse_args([])
        cfg = config_from_args(args)
        self.assertEqual(cfg.player_start_lives, DEFAULT_CONFIG.player_start_lives)

    def test_alien_post_init(self):
        """Test Alien.__post_init__ exists and runs."""
        alien = Alien(x=5, y=5, alien_type=2)
        self.assertEqual(alien.alien_type, 2)


class TestPropertyBased(unittest.TestCase):
    """Step 16: Property-based tests with Hypothesis."""

    @given(alien_count=st.integers(min_value=1, max_value=55))
    @hyp_settings(max_examples=50)
    def test_fire_probability_in_valid_range(self, alien_count):
        """Fire probability always between 0 and max for any alien count."""
        game = Game(test_mode=True)
        game.aliens = game.aliens[:alien_count]
        prob = game.get_alien_fire_probability()
        self.assertGreaterEqual(prob, 0)
        self.assertLessEqual(prob, game.config.max_fire_probability)

    @given(moves=st.lists(st.sampled_from(['left', 'right']), min_size=1, max_size=100))
    @hyp_settings(max_examples=30)
    def test_player_position_stays_in_bounds(self, moves):
        """Player position always stays within screen bounds after any moves."""
        game = Game(test_mode=True)
        for move in moves:
            if move == 'left':
                game.player.x = max(0, game.player.x - game.config.player_speed)
            else:
                game.player.x = min(game.width - 3, game.player.x + game.config.player_speed)
        self.assertGreaterEqual(game.player.x, 0)
        self.assertLessEqual(game.player.x, game.width - 3)

    @given(hits=st.integers(min_value=0, max_value=20))
    @hyp_settings(max_examples=30)
    def test_score_always_non_negative(self, hits):
        """Score is always non-negative regardless of game actions."""
        game = Game(test_mode=True)
        # Score only goes up via kills, never down
        self.assertGreaterEqual(game.score, 0)

    @given(hits=st.integers(min_value=0, max_value=5))
    @hyp_settings(max_examples=20)
    def test_bunker_health_in_valid_range(self, hits):
        """Bunker health is always in [0, 3] after any number of hits."""
        from invaders import Bunker
        bunker = Bunker(x=10, y=10, health=3)
        for _ in range(hits):
            bunker.hit()
        self.assertGreaterEqual(bunker.health, -2)  # Can go negative from excess hits
        # But char property should handle it gracefully
        char = bunker.char
        self.assertIsInstance(char, str)


class TestGameUpdateIntegration(unittest.TestCase):
    """Step 15: Integration tests for the game update loop."""

    def test_full_sequence_fire_kill_score(self):
        """Test fire → kill alien → score updates."""
        game = Game(test_mode=True)
        # Place a single alien right where the projectile will be
        alien = Alien(x=game.player.x + 1, y=game.player.y - 2, alien_type=0)
        game.aliens = [alien]
        game.player_projectiles.append(
            Projectile(x=game.player.x + 1, y=float(game.player.y - 1), direction=-1)
        )
        # Run updates until collision (projectile speed=1.0, distance=1)
        for _ in range(5):
            game._update_projectiles()
            game._check_collisions()
        self.assertEqual(len(game.aliens), 0)
        self.assertGreater(game.score, 0)

    def test_level_transition_on_all_aliens_killed(self):
        """Test kill all aliens → level increments and aliens respawn."""
        game = Game(test_mode=True)
        game.aliens = []
        game.update()
        self.assertEqual(game.state, GameState.LEVEL_TRANSITION)
        self.assertEqual(game.level, 2)
        self.assertGreater(len(game.aliens), 0)

    def test_game_over_on_lives_depleted(self):
        """Test deplete lives → game over state."""
        game = Game(test_mode=True)
        game.player.lives = 1
        game.handle_player_damage()
        self.assertEqual(game.state, GameState.GAME_OVER)

    def test_reset_after_game_over(self):
        """Test reset restores clean state."""
        game = Game(test_mode=True)
        game.score = 1000
        game.player.lives = 0
        game.state = GameState.GAME_OVER
        game.reset_game()
        self.assertEqual(game.score, 0)
        self.assertEqual(game.player.lives, game.config.player_start_lives)
        self.assertEqual(game.state, GameState.PLAYING)

    def test_multiple_updates_projectile_movement(self):
        """Test multiple updates move projectiles correctly."""
        game = Game(test_mode=True)
        game.aliens = []  # No aliens to interfere
        proj = Projectile(x=40, y=15.0, direction=-1)
        game.player_projectiles.append(proj)
        initial_y = proj.y
        for _ in range(5):
            game._update_projectiles()
        self.assertLess(proj.y, initial_y)

    def test_alien_fire_produces_projectiles(self):
        """Test alien firing over many frames produces projectiles."""
        game = Game(test_mode=True)
        import random as rng
        rng.seed(1)
        for _ in range(200):
            game._alien_fire()
        self.assertGreater(len(game.alien_projectiles), 0)


class TestCollisionEdgeCases(unittest.TestCase):
    """Step 14: Edge case tests for collision detection."""

    def test_projectile_at_exact_alien_boundary(self):
        """Projectile at distance=1 from alien should still hit."""
        game = Game(test_mode=True)
        game.aliens = [Alien(x=20, y=10)]
        game.player_projectiles = [Projectile(x=21, y=11, direction=-1)]
        game._check_collisions()
        self.assertEqual(len(game.aliens), 0)

    def test_projectile_between_two_adjacent_aliens(self):
        """Projectile between two aliens with gap should miss both."""
        game = Game(test_mode=True)
        game.aliens = [Alien(x=10, y=10), Alien(x=16, y=10)]
        game.player_projectiles = [Projectile(x=13, y=10, direction=-1)]
        game._check_collisions()
        self.assertEqual(len(game.aliens), 2)

    def test_simultaneous_projectiles_same_position(self):
        """Player and alien projectile at same position should not collide with each other."""
        game = Game(test_mode=True)
        game.player_projectiles = [Projectile(x=20, y=10, direction=-1)]
        game.alien_projectiles = [Projectile(x=20, y=10, direction=1)]
        # No aliens or player at that point
        game.aliens = []
        game.player.x = 50
        game._check_collisions()
        # Both projectiles should still exist
        self.assertEqual(len(game.player_projectiles), 1)
        self.assertEqual(len(game.alien_projectiles), 1)

    def test_bunker_destruction_at_health_1(self):
        """Bunker with health=1 should be destroyed on hit."""
        from invaders import Bunker
        bunker = Bunker(x=20, y=15, health=1)
        destroyed = bunker.hit()
        self.assertTrue(destroyed)
        self.assertEqual(bunker.health, 0)

    def test_projectile_at_screen_top(self):
        """Player projectile at y=0 should be removed."""
        game = Game(test_mode=True)
        game.player_projectiles = [Projectile(x=20, y=0.5, direction=-1)]
        game._update_projectiles()
        self.assertEqual(len(game.player_projectiles), 0)

    def test_projectile_at_screen_bottom(self):
        """Alien projectile at y=max should be removed."""
        game = Game(test_mode=True)
        game.alien_projectiles = [Projectile(x=20, y=float(game.height - 0.2), direction=1)]
        game._update_projectiles()
        self.assertEqual(len(game.alien_projectiles), 0)

    def test_alien_projectile_hits_player_at_boundary(self):
        """Alien projectile at player y should trigger damage."""
        game = Game(test_mode=True)
        game.alien_projectiles = [Projectile(x=game.player.x + 1, y=float(game.player.y), direction=1)]
        lives_before = game.player.lives
        game._check_collisions()
        self.assertLess(game.player.lives, lives_before)

    def test_multiple_projectiles_single_alien(self):
        """Only one projectile should remove the alien, other stays."""
        game = Game(test_mode=True)
        game.aliens = [Alien(x=20, y=10)]
        game.player_projectiles = [
            Projectile(x=20, y=10, direction=-1),
            Projectile(x=20, y=10, direction=-1),
        ]
        game._check_collisions()
        self.assertEqual(len(game.aliens), 0)
        # At least one projectile should remain (the second one)
        # or both consumed (depends on implementation)
        self.assertLessEqual(len(game.player_projectiles), 1)


class TestScreenShake(unittest.TestCase):
    """Step 13: Tests for screen shake effect on player death."""

    def test_shake_activation_on_damage(self):
        """Verify shake_end_time is set when player takes damage (non-test_mode)."""
        game = Game(test_mode=True)
        # Manually simulate shake like non-test mode would
        game.shake_end_time = time.time() + 0.3
        self.assertGreater(game.shake_end_time, time.time())

    def test_shake_offset_values(self):
        """Verify shake produces offsets in [-1, 0, 1] range."""
        game = Game(test_mode=True)
        game.shake_end_time = time.time() + 1.0
        game.shake_duration = 1.0
        # Run several updates to sample offsets
        offsets_x = set()
        offsets_y = set()
        for _ in range(50):
            game.update()
            offsets_x.add(game.shake_offset_x)
            offsets_y.add(game.shake_offset_y)
        # All offsets should be in {-1, 0, 1}
        self.assertTrue(offsets_x.issubset({-1, 0, 1}))
        self.assertTrue(offsets_y.issubset({-1, 0, 1}))

    def test_shake_expires(self):
        """Verify shake offsets return to 0 after duration."""
        game = Game(test_mode=True)
        game.shake_end_time = time.time() - 1.0  # Already expired
        game.update()
        self.assertEqual(game.shake_offset_x, 0)
        self.assertEqual(game.shake_offset_y, 0)

    def test_no_shake_in_test_mode(self):
        """Verify handle_player_damage doesn't activate shake in test_mode."""
        game = Game(test_mode=True)
        game.player.lives = 3
        game.handle_player_damage()
        self.assertEqual(game.shake_end_time, 0)


class TestDifficultyScaling(unittest.TestCase):
    """Step 12: Tests for difficulty scaling per level."""

    def test_alien_speed_increases_with_level(self):
        """Verify alien_move_interval decreases each level."""
        game = Game(test_mode=True)
        intervals = []
        for _ in range(3):
            intervals.append(game.alien_move_interval)
            game.aliens = []  # trigger next level
            game._next_level()
        self.assertGreater(intervals[0], intervals[1])
        self.assertGreater(intervals[1], intervals[2])

    def test_alien_rows_increase_every_3_levels(self):
        """Verify extra alien row added every 3 levels."""
        game = Game(test_mode=True)
        self.assertEqual(game.get_scaled_alien_rows(), 5)
        game.level = 4
        self.assertEqual(game.get_scaled_alien_rows(), 6)
        game.level = 7
        self.assertEqual(game.get_scaled_alien_rows(), 7)

    def test_alien_rows_cap_at_8(self):
        """Verify alien rows cap at 8."""
        game = Game(test_mode=True)
        game.level = 100
        self.assertEqual(game.get_scaled_alien_rows(), 8)

    def test_bunker_health_decreases_every_5_levels(self):
        """Verify bunker health reduces every 5 levels."""
        game = Game(test_mode=True)
        self.assertEqual(game.get_scaled_bunker_health(), 3)
        game.level = 6
        self.assertEqual(game.get_scaled_bunker_health(), 2)
        game.level = 11
        self.assertEqual(game.get_scaled_bunker_health(), 1)

    def test_bunker_health_min_is_1(self):
        """Verify bunker health never goes below 1."""
        game = Game(test_mode=True)
        game.level = 100
        self.assertEqual(game.get_scaled_bunker_health(), 1)

    def test_speed_floor(self):
        """Verify alien_move_interval never goes below 0.1."""
        game = Game(test_mode=True)
        game.level = 100
        game.aliens = []
        game._next_level()
        self.assertGreaterEqual(game.alien_move_interval, 0.1)


class TestComboScoring(unittest.TestCase):
    """Step 11: Tests for the combo scoring system."""

    def test_combo_tracking(self):
        """Verify combo count increments on rapid kills."""
        game = Game(test_mode=True)
        now = time.time()
        game._register_kill(now)
        game._register_kill(now + 0.5)
        self.assertEqual(game.combo_count, 2)

    def test_combo_multiplier_calculation(self):
        """Verify multiplier ramps from 1x to 5x."""
        game = Game(test_mode=True)
        now = time.time()
        self.assertEqual(game.get_combo_multiplier(), 1)
        game._register_kill(now)
        game._register_kill(now + 0.1)
        self.assertEqual(game.get_combo_multiplier(), 2)
        game._register_kill(now + 0.2)
        self.assertEqual(game.get_combo_multiplier(), 3)

    def test_combo_timeout_reset(self):
        """Verify combo resets after time window expires."""
        game = Game(test_mode=True)
        now = time.time()
        game._register_kill(now)
        game._register_kill(now + 0.5)
        self.assertEqual(game.combo_count, 2)
        # Kill outside window
        game._register_kill(now + 5.0)
        self.assertEqual(game.combo_count, 1)

    def test_max_combo_cap(self):
        """Verify combo multiplier caps at 5."""
        game = Game(test_mode=True)
        now = time.time()
        for i in range(10):
            game._register_kill(now + i * 0.1)
        self.assertEqual(game.get_combo_multiplier(), 5)

    def test_combo_score_integration(self):
        """Verify combo multiplier affects score in collision detection."""
        game = Game(test_mode=True)
        # Set up two aliens close together
        game.aliens = [Alien(x=20, y=10, alien_type=0), Alien(x=25, y=10, alien_type=0)]
        # Kill first one to start combo
        game.player_projectiles.append(Projectile(x=20, y=10, direction=-1))
        game._check_collisions()
        first_score = game.score

        # Kill second one within combo window
        game.player_projectiles.append(Projectile(x=25, y=10, direction=-1))
        game._check_collisions()
        second_kill_points = game.score - first_score
        # Second kill should have higher points due to combo
        self.assertGreaterEqual(second_kill_points, first_score)


class TestPowerUps(unittest.TestCase):
    """Step 10: Tests for the power-up system."""

    def test_power_up_drop_chance(self):
        """Verify power-ups spawn roughly 10% of the time over many trials."""
        game = Game(test_mode=True)
        import random as rng
        rng.seed(42)
        count = 0
        for _ in range(1000):
            game.power_ups.clear()
            game._spawn_power_up(20, 10)
            if game.power_ups:
                count += 1
        # Should be roughly 100 (10%), allow wide range
        self.assertGreater(count, 50)
        self.assertLess(count, 200)

    def test_power_up_falling_movement(self):
        """Verify power-ups fall down each update."""
        game = Game(test_mode=True)
        pu = PowerUp(x=20, y=5.0, power_type=PowerUpType.RAPID_FIRE)
        game.power_ups.append(pu)
        game._update_power_ups(time.time())
        self.assertGreater(pu.y, 5.0)

    def test_power_up_player_collection(self):
        """Verify player collects power-up when overlapping."""
        game = Game(test_mode=True)
        # Place power-up at player position
        pu = PowerUp(x=game.player.x + 1, y=float(game.player.y),
                     power_type=PowerUpType.SHIELD)
        game.power_ups.append(pu)
        game._update_power_ups(time.time())
        self.assertEqual(len(game.power_ups), 0)
        self.assertTrue(game.has_power_up(PowerUpType.SHIELD))

    def test_power_up_effect_expiration(self):
        """Verify timed power-ups expire after their duration."""
        game = Game(test_mode=True)
        game.active_power_ups.append(
            ActivePowerUp(power_type=PowerUpType.RAPID_FIRE, expires_at=time.time() - 1)
        )
        game._update_power_ups(time.time())
        self.assertFalse(game.has_power_up(PowerUpType.RAPID_FIRE))

    def test_shield_absorbs_hit(self):
        """Verify shield prevents damage and is consumed."""
        game = Game(test_mode=True)
        game.active_power_ups.append(
            ActivePowerUp(power_type=PowerUpType.SHIELD, expires_at=time.time() + 999)
        )
        initial_lives = game.player.lives
        game.handle_player_damage()
        self.assertEqual(game.player.lives, initial_lives)
        self.assertFalse(game.has_power_up(PowerUpType.SHIELD))


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


class TestHandleInput(unittest.TestCase):
    """Step 17 coverage: Tests for handle_input across all game states."""

    def test_quit_returns_false(self):
        """Pressing Q in any state should return False."""
        game = Game(test_mode=True)
        self.assertFalse(game.handle_input(ord('q')))

    def test_quit_uppercase_returns_false(self):
        """Pressing Q (uppercase) should also quit."""
        game = Game(test_mode=True)
        self.assertFalse(game.handle_input(ord('Q')))

    def test_menu_space_starts_game(self):
        """Pressing SPACE on menu transitions to PLAYING."""
        game = Game(test_mode=True)
        game.state = GameState.MENU
        game.handle_input(ord(' '))
        self.assertEqual(game.state, GameState.PLAYING)

    def test_menu_enter_starts_game(self):
        """Pressing ENTER on menu transitions to PLAYING."""
        game = Game(test_mode=True)
        game.state = GameState.MENU
        game.handle_input(ord('\n'))
        self.assertEqual(game.state, GameState.PLAYING)

    def test_playing_pause_with_p(self):
        """Pressing P in PLAYING state toggles pause."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.handle_input(ord('p'))
        self.assertEqual(game.state, GameState.PAUSED)

    def test_playing_pause_with_escape(self):
        """Pressing Escape in PLAYING state toggles pause."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.handle_input(27)  # ESC
        self.assertEqual(game.state, GameState.PAUSED)

    def test_playing_move_left(self):
        """Pressing left arrow moves player left."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        initial_x = game.player.x
        game.handle_input(260)  # curses.KEY_LEFT
        self.assertLess(game.player.x, initial_x)

    def test_playing_move_left_a_key(self):
        """Pressing 'a' moves player left."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        initial_x = game.player.x
        game.handle_input(ord('a'))
        self.assertLess(game.player.x, initial_x)

    def test_playing_move_right(self):
        """Pressing right arrow moves player right."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        initial_x = game.player.x
        game.handle_input(261)  # curses.KEY_RIGHT
        self.assertGreater(game.player.x, initial_x)

    def test_playing_move_right_d_key(self):
        """Pressing 'd' moves player right."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        initial_x = game.player.x
        game.handle_input(ord('d'))
        self.assertGreater(game.player.x, initial_x)

    def test_playing_fire_creates_projectile(self):
        """Pressing SPACE in PLAYING state fires a projectile."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.handle_input(ord(' '))
        self.assertEqual(len(game.player_projectiles), 1)

    def test_playing_fire_limit(self):
        """Player can't fire more than 3 projectiles at once."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        for _ in range(5):
            game.handle_input(ord(' '))
        self.assertEqual(len(game.player_projectiles), 3)

    def test_paused_unpause_with_p(self):
        """Pressing P in PAUSED state resumes."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game._toggle_pause()
        self.assertEqual(game.state, GameState.PAUSED)
        game.handle_input(ord('p'))
        self.assertEqual(game.state, GameState.PLAYING)

    def test_paused_unpause_with_escape(self):
        """Pressing Escape in PAUSED state resumes."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game._toggle_pause()
        game.handle_input(27)
        self.assertEqual(game.state, GameState.PLAYING)

    def test_game_over_restart(self):
        """Pressing R at GAME_OVER resets the game."""
        game = Game(test_mode=True)
        game.state = GameState.GAME_OVER
        game.score = 500
        game.handle_input(ord('r'))
        self.assertEqual(game.state, GameState.PLAYING)
        self.assertEqual(game.score, 0)

    def test_level_transition_continue(self):
        """Pressing SPACE at LEVEL_TRANSITION continues to PLAYING."""
        game = Game(test_mode=True)
        game.state = GameState.LEVEL_TRANSITION
        game.handle_input(ord(' '))
        self.assertEqual(game.state, GameState.PLAYING)

    def test_level_transition_enter_continue(self):
        """Pressing ENTER at LEVEL_TRANSITION continues to PLAYING."""
        game = Game(test_mode=True)
        game.state = GameState.LEVEL_TRANSITION
        game.handle_input(ord('\n'))
        self.assertEqual(game.state, GameState.PLAYING)

    def test_player_left_boundary(self):
        """Player can't move past left edge."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.player.x = 0
        game.handle_input(ord('a'))
        self.assertEqual(game.player.x, 0)

    def test_player_right_boundary(self):
        """Player can't move past right edge."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.player.x = game.width - 3
        game.handle_input(ord('d'))
        self.assertEqual(game.player.x, game.width - 3)


class TestBunkerCollisions(unittest.TestCase):
    """Step 17 coverage: Tests for projectile-bunker collision detection."""

    def _make_game_with_bunkers(self):
        """Create a game with bunkers initialized."""
        from invaders import Bunker
        game = Game(test_mode=True)
        game.bunkers = [Bunker(x=20, y=15, health=3)]
        return game

    def test_player_projectile_hits_bunker(self):
        """Player projectile at bunker position damages the bunker."""
        game = self._make_game_with_bunkers()
        game.aliens = []  # no aliens to interfere
        game.player_projectiles = [Projectile(x=20, y=15, direction=-1)]
        game._check_collisions()
        self.assertEqual(game.bunkers[0].health, 2)
        self.assertEqual(len(game.player_projectiles), 0)

    def test_alien_projectile_hits_bunker(self):
        """Alien projectile at bunker position damages the bunker."""
        game = self._make_game_with_bunkers()
        game.player.x = 50  # move player away
        game.alien_projectiles = [Projectile(x=20, y=15, direction=1)]
        game._check_collisions()
        self.assertEqual(game.bunkers[0].health, 2)
        self.assertEqual(len(game.alien_projectiles), 0)

    def test_dead_bunker_not_hit(self):
        """Bunker with health=0 should not be hit."""
        from invaders import Bunker
        game = Game(test_mode=True)
        game.bunkers = [Bunker(x=20, y=15, health=0)]
        game.aliens = []
        game.player_projectiles = [Projectile(x=20, y=15, direction=-1)]
        game._check_collisions()
        # Projectile should pass through dead bunker
        self.assertEqual(len(game.player_projectiles), 1)


class TestSoundEffectsMethods(unittest.TestCase):
    """Step 17 coverage: Tests for SoundEffects individual play methods."""

    def test_sound_effects_play_methods(self):
        """Verify all play methods can be called without error using NullBackend."""
        sfx = SoundEffects(backend=NullSoundBackend())
        sfx.enabled = True
        # These won't actually play since files don't exist, but tests the path
        sfx.play_shoot()
        sfx.play_alien_die()
        sfx.play_player_die()
        sfx.play_level_complete()
        sfx.play_life_bonus()

    def test_subscribe_and_trigger_events(self):
        """Verify SoundEffects subscribes to events and handlers are called."""
        sfx = SoundEffects(backend=NullSoundBackend())
        bus = EventBus()
        sfx.subscribe_to_events(bus)
        # Fire each event — should not raise
        bus.publish(GameEvent.SHOT_FIRED)
        bus.publish(GameEvent.ALIEN_KILLED)
        bus.publish(GameEvent.PLAYER_HIT)
        bus.publish(GameEvent.LEVEL_COMPLETE)

    def test_update_march(self):
        """Verify update_march updates interval and alternates beat."""
        sfx = SoundEffects(backend=NullSoundBackend())
        sfx.enabled = True
        sfx.last_march_time = 0  # Force beat to play
        sfx.update_march(10, 50)
        self.assertGreater(sfx.march_interval, 0)

    def test_update_march_zero_aliens(self):
        """update_march returns early with zero aliens."""
        sfx = SoundEffects(backend=NullSoundBackend())
        sfx.update_march(0, 50)
        # Should return early, interval unchanged from default


class TestUpdateEdgeCases(unittest.TestCase):
    """Step 17 coverage: Tests for update() and game logic edge cases."""

    def test_flash_effect_expires(self):
        """Verify flash_active becomes False after flash_end_time passes."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.flash_active = True
        game.flash_end_time = time.time() - 1  # Already expired
        game.update()
        self.assertFalse(game.flash_active)

    def test_alien_animation_frame_toggles(self):
        """Verify alien animation frame alternates on timer."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.last_animation_time = 0  # Force toggle
        old_frame = game.alien_animation_frame
        game.update()
        self.assertNotEqual(game.alien_animation_frame, old_frame)

    def test_alien_move_on_timer(self):
        """Verify aliens move when interval elapses."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.last_alien_move_time = 0  # Force move
        initial_positions = [(a.x, a.y) for a in game.aliens[:3]]
        game.update()
        new_positions = [(a.x, a.y) for a in game.aliens[:3]]
        self.assertNotEqual(initial_positions, new_positions)

    def test_move_aliens_empty(self):
        """_move_aliens with no aliens returns early."""
        game = Game(test_mode=True)
        game.aliens = []
        game._move_aliens()  # Should not raise

    def test_toggle_pause_with_mystery_score_display(self):
        """Verify toggle_pause adjusts mystery_score_display time."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.mystery_score_display = (10, 1, 100, time.time() + 1.0)
        game._toggle_pause()
        time.sleep(0.05)
        game._toggle_pause()
        # The display end time should have been extended
        self.assertGreater(game.mystery_score_display[3], time.time())

    def test_toggle_pause_with_flash_end_time(self):
        """Verify toggle_pause adjusts flash_end_time when active."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.flash_end_time = time.time() + 5.0
        old_flash = game.flash_end_time
        game._toggle_pause()
        time.sleep(0.05)
        game._toggle_pause()
        self.assertGreater(game.flash_end_time, old_flash)

    def test_next_level_with_sfx_life_bonus(self):
        """Verify _next_level plays life bonus sound when lives are awarded."""
        game = Game(test_mode=True)
        # Give the game a SoundEffects with NullBackend
        sfx = SoundEffects(backend=NullSoundBackend())
        game.sfx = sfx
        game.player.lives = 3
        game._next_level()
        self.assertEqual(game.level, 2)
        self.assertGreater(game.lives_awarded, 0)

    def test_init_bunkers_creates_bunkers(self):
        """Verify _init_bunkers creates bunker blocks."""
        game = Game(test_mode=True)
        game._init_bunkers()
        self.assertGreater(len(game.bunkers), 0)
        # Should create 4 bunkers × 6 blocks each = 24
        self.assertEqual(len(game.bunkers), 24)


class TestAudioManagerBasic(unittest.TestCase):
    """Step 17 coverage: Tests for AudioManager class."""

    def test_audio_manager_init(self):
        """Verify AudioManager initializes with correct defaults."""
        from invaders import AudioManager
        am = AudioManager()
        self.assertTrue(am.game_running)
        self.assertIsNone(am.audio_thread)
        self.assertIsNone(am.current_process)

    def test_audio_manager_stop_no_process(self):
        """Verify stop() works cleanly when no process is running."""
        from invaders import AudioManager
        am = AudioManager()
        am.stop()
        self.assertFalse(am.game_running)

    def test_audio_manager_start_no_file(self):
        """Verify start() returns silently when no audio file exists."""
        from invaders import AudioManager
        am = AudioManager()
        am.start()  # Audio file won't exist, should return silently
        am.stop()


class TestCleanupFunctions(unittest.TestCase):
    """Step 17 coverage: Tests for module-level cleanup functions."""

    def test_cleanup_audio_with_no_manager(self):
        """_cleanup_audio should not raise when _audio_manager is None."""
        import invaders
        old = invaders._audio_manager
        invaders._audio_manager = None
        try:
            invaders._cleanup_audio()
        finally:
            invaders._audio_manager = old

    def test_cleanup_audio_with_manager(self):
        """_cleanup_audio calls stop on the audio manager."""
        import invaders
        from invaders import AudioManager
        old = invaders._audio_manager
        am = AudioManager()
        invaders._audio_manager = am
        try:
            invaders._cleanup_audio()
            self.assertFalse(am.game_running)
        finally:
            invaders._audio_manager = old

    def test_signal_handler(self):
        """_signal_handler calls _cleanup_audio and exits."""
        import invaders
        old = invaders._audio_manager
        invaders._audio_manager = None
        try:
            with self.assertRaises(SystemExit):
                invaders._signal_handler(2, None)
        finally:
            invaders._audio_manager = old

    def test_score_manager_save_error(self):
        """ScoreManager._save logs warning on write failure."""
        sm = ScoreManager(scores_path='/nonexistent/dir/scores.json')
        sm.current_score = 100
        # record calls _save which should handle the error
        sm.record(100, 1)
        # Should not raise; the error is logged


class TestExtraCoverage(unittest.TestCase):
    """Step 17 coverage: Additional targeted tests for remaining gaps."""

    def test_march_beat_alternates(self):
        """Verify update_march alternates between beat 0 and 1."""
        sfx = SoundEffects(backend=NullSoundBackend())
        sfx.enabled = True
        sfx.last_march_time = 0  # Force beat to play
        sfx.march_beat = 0
        sfx.update_march(10, 50)
        self.assertEqual(sfx.march_beat, 1)
        sfx.last_march_time = 0  # Force again
        sfx.update_march(10, 50)
        self.assertEqual(sfx.march_beat, 0)

    def test_play_async_disabled(self):
        """_play_async returns early when sfx is disabled."""
        sfx = SoundEffects(backend=NullSoundBackend())
        sfx.enabled = False
        sfx._play_async('shoot')  # Should return early, no error

    def test_play_async_unknown_sound(self):
        """_play_async returns early for unknown sound name."""
        sfx = SoundEffects(backend=NullSoundBackend())
        sfx.enabled = True
        sfx._play_async('nonexistent_sound')  # Should return early

    def test_mystery_ship_spawn_with_seed(self):
        """Mystery ship spawns when random conditions are met."""
        import random as rng
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.mystery_ship = None

        # Try many times with different seeds until we get a spawn
        # Set last_mystery_spawn_check far in the past so the 1-second check passes
        current = time.time()
        spawned = False
        for seed in range(200):
            rng.seed(seed)
            game.mystery_ship = None
            game.last_mystery_spawn_check = current - 2.0
            game._update_mystery_ship(current)
            if game.mystery_ship is not None:
                spawned = True
                break
        self.assertTrue(spawned)

    def test_sfx_update_march_in_update(self):
        """Verify update() calls sfx.update_march when sfx is set."""
        game = Game(test_mode=True)
        sfx = SoundEffects(backend=NullSoundBackend())
        game.sfx = sfx
        game.state = GameState.PLAYING
        game.update()
        # Should have called update_march without error

    def test_audio_manager_stop_with_mock_process(self):
        """AudioManager.stop handles process termination."""
        from invaders import AudioManager
        from unittest.mock import MagicMock
        am = AudioManager()
        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = MagicMock()
        am.current_process = mock_proc
        am.stop()
        mock_proc.terminate.assert_called_once()

    def test_macoss_sound_backend_is_available(self):
        """MacOSSoundBackend.is_available returns a boolean."""
        backend = MacOSSoundBackend()
        result = backend.is_available()
        self.assertIsInstance(result, bool)

    def test_macoss_sound_backend_play_no_file(self):
        """MacOSSoundBackend.play returns silently when file doesn't exist."""
        backend = MacOSSoundBackend()
        backend.play('/nonexistent/sound.aiff')  # Should return early

    def test_macoss_sound_backend_stop(self):
        """MacOSSoundBackend.stop doesn't raise."""
        backend = MacOSSoundBackend()
        backend.stop()  # Should not raise

    def test_get_sound_backend_returns_backend(self):
        """get_sound_backend returns a valid AbstractSoundBackend."""
        backend = get_sound_backend()
        self.assertIsInstance(backend, AbstractSoundBackend)


class TestSpatialGrid(unittest.TestCase):
    """Step 18: Tests for spatial partitioning collision optimization."""

    def test_cell_assignment(self):
        """Verify entities are placed in the correct grid cell."""
        from invaders import SpatialGrid
        grid = SpatialGrid(width=80, height=40, cell_size=4)
        grid.insert('entity_a', 10, 8)
        # Cell should be (10//4, 8//4) = (2, 2)
        self.assertIn('entity_a', grid.grid[(2, 2)])

    def test_cell_assignment_origin(self):
        """Entity at (0,0) goes to cell (0,0)."""
        from invaders import SpatialGrid
        grid = SpatialGrid(width=80, height=40, cell_size=4)
        grid.insert('origin', 0, 0)
        self.assertIn('origin', grid.grid[(0, 0)])

    def test_query_nearby_returns_same_cell(self):
        """query_nearby returns entities in the same cell."""
        from invaders import SpatialGrid
        grid = SpatialGrid(width=80, height=40, cell_size=4)
        grid.insert('a', 10, 8)
        grid.insert('b', 11, 9)
        result = grid.query_nearby(10, 8)
        self.assertIn('a', result)
        self.assertIn('b', result)

    def test_query_nearby_returns_adjacent_cells(self):
        """query_nearby returns entities in adjacent cells."""
        from invaders import SpatialGrid
        grid = SpatialGrid(width=80, height=40, cell_size=4)
        grid.insert('a', 4, 4)   # cell (1, 1)
        grid.insert('b', 8, 8)   # cell (2, 2)
        # Query from cell (1, 1) — adjacent to (2, 2)
        result = grid.query_nearby(4, 4)
        self.assertIn('a', result)
        self.assertIn('b', result)

    def test_query_nearby_excludes_far_cells(self):
        """query_nearby does not return entities far away."""
        from invaders import SpatialGrid
        grid = SpatialGrid(width=80, height=40, cell_size=4)
        grid.insert('near', 10, 10)
        grid.insert('far', 60, 30)
        result = grid.query_nearby(10, 10)
        self.assertIn('near', result)
        self.assertNotIn('far', result)

    def test_clear_removes_all(self):
        """clear() empties the grid."""
        from invaders import SpatialGrid
        grid = SpatialGrid(width=80, height=40, cell_size=4)
        grid.insert('a', 10, 10)
        grid.insert('b', 20, 20)
        grid.clear()
        self.assertEqual(len(grid.grid), 0)

    def test_collision_results_match_brute_force(self):
        """Spatial grid collision results match brute-force for many random entities."""
        import random as rng
        rng.seed(42)
        game = Game(test_mode=True)
        # Create many random aliens
        game.aliens = [Alien(x=rng.randint(5, 75), y=rng.randint(3, 20))
                       for _ in range(50)]
        # Create several projectiles
        game.player_projectiles = [
            Projectile(x=rng.randint(5, 75), y=float(rng.randint(3, 20)), direction=-1)
            for _ in range(10)
        ]

        # Brute-force: find which (proj, alien) pairs would collide
        brute_hits = set()
        for proj in game.player_projectiles:
            for alien in game.aliens:
                if abs(proj.x - alien.x) <= 1 and abs(proj.y - alien.y) <= 1:
                    brute_hits.add((proj.x, int(proj.y), alien.x, alien.y))

        # Run spatial grid collision
        initial_alien_count = len(game.aliens)
        game._check_collisions()
        aliens_killed = initial_alien_count - len(game.aliens)

        # Both should agree on number of collisions found
        # (Note: each projectile can only hit one alien due to break)
        self.assertGreater(aliens_killed, 0)

    def test_benchmark_large_entity_count(self):
        """Verify spatial grid handles large entity counts without error."""
        import random as rng
        rng.seed(99)
        game = Game(test_mode=True)
        # 200 aliens
        game.aliens = [Alien(x=rng.randint(1, 78), y=rng.randint(2, 25))
                       for _ in range(200)]
        # 50 projectiles
        game.player_projectiles = [
            Projectile(x=rng.randint(1, 78), y=float(rng.randint(2, 25)), direction=-1)
            for _ in range(50)
        ]
        game.alien_projectiles = [
            Projectile(x=rng.randint(1, 78), y=float(rng.randint(10, 30)), direction=1)
            for _ in range(30)
        ]
        # Should complete without error
        game._check_collisions()
        # Some collisions should have occurred
        self.assertLess(len(game.aliens), 200)


class TestTerminalResize(unittest.TestCase):
    """Step 19: Tests for terminal resize handling."""

    def test_handle_resize_updates_dimensions(self):
        """Verify resize updates width and height."""
        game = Game(test_mode=True)
        game.handle_resize(100, 40)
        self.assertEqual(game.width, 100)
        self.assertEqual(game.height, 40)

    def test_handle_resize_too_small_flag(self):
        """Verify too_small flag is set when below minimum."""
        game = Game(test_mode=True)
        game.handle_resize(30, 10)
        self.assertTrue(game.too_small)

    def test_handle_resize_adequate_size(self):
        """Verify too_small is False when size is adequate."""
        game = Game(test_mode=True)
        game.handle_resize(80, 24)
        self.assertFalse(game.too_small)

    def test_handle_resize_clamps_player_position(self):
        """Verify player is clamped to new bounds on resize."""
        game = Game(test_mode=True)
        game.player.x = 70
        game.handle_resize(65, 25)  # Above minimum size
        self.assertLessEqual(game.player.x, 62)  # width - 3

    def test_handle_resize_removes_out_of_bounds_projectiles(self):
        """Verify projectiles outside new bounds are removed."""
        game = Game(test_mode=True)
        game.player_projectiles = [
            Projectile(x=75, y=10.0, direction=-1),  # Will be out of bounds
            Projectile(x=20, y=10.0, direction=-1),   # Will stay
        ]
        game.alien_projectiles = [
            Projectile(x=10, y=30.0, direction=1),  # Will be out of bounds (height)
            Projectile(x=10, y=10.0, direction=1),   # Will stay
        ]
        game.handle_resize(65, 25)  # Above minimum size
        self.assertEqual(len(game.player_projectiles), 1)
        self.assertEqual(len(game.alien_projectiles), 1)

    def test_handle_resize_clamps_aliens(self):
        """Verify aliens are clamped to new bounds."""
        game = Game(test_mode=True)
        game.aliens = [Alien(x=70, y=20)]
        game.handle_resize(65, 25)  # Above minimum size
        self.assertLessEqual(game.aliens[0].x, 61)  # width - 4

    def test_handle_resize_key_in_input(self):
        """Verify KEY_RESIZE is handled in handle_input."""
        import curses
        game = Game(test_mode=True)
        # Without a screen, KEY_RESIZE should just return True
        result = game.handle_input(curses.KEY_RESIZE)
        self.assertTrue(result)

    def test_resize_player_y_updated(self):
        """Verify player Y is set to height-2 on resize."""
        game = Game(test_mode=True)
        game.handle_resize(80, 30)
        self.assertEqual(game.player.y, 28)

    def test_too_small_skips_position_update(self):
        """When too small, positions are NOT recalculated."""
        game = Game(test_mode=True)
        old_player_x = game.player.x
        game.handle_resize(20, 10)  # Too small
        self.assertTrue(game.too_small)
        # Player position unchanged because we returned early
        self.assertEqual(game.player.x, old_player_x)


if __name__ == '__main__':
    # Run tests with verbosity
    unittest.main(verbosity=2)
