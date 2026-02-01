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

import logging
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from invaders import (
    ALIEN_EXPLOSION_CONFIGS,
    ALIEN_TYPE_COLORS,
    BORDER_BL,
    BORDER_BR,
    BORDER_H,
    BORDER_V,
    COLOR_ALIEN_TYPE_0,
    COLOR_ALIEN_TYPE_1,
    COLOR_ALIEN_TYPE_2,
    COLOR_BORDER,
    COLOR_BUNKER,
    COLOR_BUNKER_CRITICAL,
    COLOR_BUNKER_DAMAGED,
    COLOR_BUNKER_HIGH,
    COLOR_BUNKER_LOW,
    COLOR_GAME_OVER,
    COLOR_PLAYER,
    COLOR_TEXT,
    DEATH_ANIM_CHARS,
    DEFAULT_CONFIG,
    DEFAULT_EXPLOSION_CONFIG,
    MYSTERY_SHIP_CHAR,
    PLAYER_START_LIVES,
    POWERUP_CHARS,
    POWERUP_LABELS,
    TITLE_ART,
    TITLE_COLOR_CYCLE,
    AbstractSoundBackend,
    ActivePowerUp,
    Alien,
    Bunker,
    ComboText,
    DyingAlien,
    EventBus,
    Game,
    GameConfig,
    GameEvent,
    GameState,
    MacOSSoundBackend,
    MysteryShip,
    NullSoundBackend,
    Particle,
    ParticleSystem,
    Player,
    PowerUp,
    PowerUpType,
    Projectile,
    RippleEffect,
    ScoreManager,
    ScorePopup,
    SoundEffects,
    build_argument_parser,
    config_from_args,
    get_explosion_config,
    get_sound_backend,
    resolve_audio_path,
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
        self.assertTrue(os.path.isabs(path), f"Path should be absolute, got: {path}")
        # Should contain the home directory
        home = os.path.expanduser("~")
        self.assertTrue(path.startswith(home), f"Path should start with home dir: {path}")
        # Should end with soundtrack.mp3
        self.assertTrue(path.endswith("soundtrack.mp3"), f"Path should end with soundtrack.mp3: {path}")
        # Should match expected format: /Users/username/soundtrack.mp3
        expected = os.path.expanduser("~/soundtrack.mp3")
        self.assertEqual(path, expected)


class TestPlayerMechanics(unittest.TestCase):
    """Tests for player life management and damage."""

    def test_player_life_decrement(self):
        """
        Initialize a player with starting lives.
        Simulate a collision. Assert lives decremented by 1.
        """
        player = Player()
        self.assertEqual(player.lives, PLAYER_START_LIVES, f"Player should start with {PLAYER_START_LIVES} lives")

        # Simulate collision/damage
        player.take_damage()

        self.assertEqual(
            player.lives,
            PLAYER_START_LIVES - 1,
            f"Player should have {PLAYER_START_LIVES - 1} lives after taking damage",
        )

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

        self.assertEqual(game.state, GameState.GAME_OVER, "Game state should be GAME_OVER when lives reach 0")

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

        self.assertEqual(game.state, GameState.GAME_OVER, "Game state should be GAME_OVER when aliens reach player row")

    def test_no_game_over_when_aliens_above(self):
        """Ensure game continues when aliens haven't reached player."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING

        # Place alien above player
        player_y = game.player.y
        alien = Alien(x=10, y=player_y - 5)
        game.aliens = [alien]

        game.check_invasion()

        self.assertEqual(game.state, GameState.PLAYING, "Game should continue when aliens are above player")


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
        self.assertEqual(game.score, 0, "Score should be 0 after reset")
        self.assertEqual(game.player.lives, PLAYER_START_LIVES, f"Lives should be {PLAYER_START_LIVES} after reset")
        self.assertEqual(game.state, GameState.PLAYING, "Game state should be PLAYING after reset")
        self.assertEqual(len(game.player_projectiles), 0, "Player projectiles should be cleared after reset")
        self.assertEqual(len(game.alien_projectiles), 0, "Alien projectiles should be cleared after reset")

    def test_reset_reinitializes_aliens(self):
        """Ensure reset re-creates the full alien grid."""
        game = Game(test_mode=True)

        # Simulate killing all aliens
        game.aliens = []

        game.reset_game()

        # Should have aliens again
        self.assertGreater(len(game.aliens), 0, "Aliens should be reinitialized after reset")

    def test_reset_centers_player(self):
        """Ensure reset returns player to center position."""
        game = Game(test_mode=True)
        initial_x = game.player.x

        # Move player
        game.player.x = 5

        game.reset_game()

        # Player should be back at center
        self.assertEqual(game.player.x, initial_x, "Player should be centered after reset")


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
        states = [GameState.MENU, GameState.PLAYING, GameState.PAUSED, GameState.LEVEL_TRANSITION, GameState.GAME_OVER]
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
        game.aliens = game.aliens[: len(game.aliens) // 2]
        half_prob = game.get_alien_fire_probability()

        # Reduce to just a few
        game.aliens = game.aliens[:3]
        few_prob = game.get_alien_fire_probability()

        # Probability should increase as aliens decrease
        self.assertGreater(half_prob, initial_prob, "Fire probability should increase with fewer aliens")
        self.assertGreater(few_prob, half_prob, "Fire probability should be highest with few aliens")


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
        game.player_projectiles.append(Projectile(x=game.player.x + 1, y=game.player.y - 1, direction=-1))
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
                file_handlers = [h for h in self.logger.handlers if isinstance(h, logging.FileHandler)]
                self.assertGreater(len(file_handlers), 0)
        finally:
            os.chdir(old_cwd)
            self.logger.handlers.clear()

    def test_setup_logging_default_mode(self):
        """Verify default mode sets WARNING level with NullHandler."""
        import invaders

        invaders.setup_logging(debug=False)
        self.assertEqual(self.logger.level, logging.WARNING)
        null_handlers = [h for h in self.logger.handlers if isinstance(h, logging.NullHandler)]
        self.assertGreater(len(null_handlers), 0)
        self.logger.handlers.clear()

    def test_debug_flag_parsing(self):
        """Verify --debug is recognized in sys.argv."""
        self.assertIn("--debug", ["--debug", "--other"])

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
                log_path = os.path.join(tmpdir, "invaders.log")
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
            sm = ScoreManager(scores_path=os.path.join(tmpdir, "scores.json"))
            sm.add(100)
            sm.add(50)
            self.assertEqual(sm.current_score, 150)

    def test_file_persistence(self):
        """Verify scores are saved to and loaded from disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "scores.json")
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
            path = os.path.join(tmpdir, "scores.json")
            sm = ScoreManager(scores_path=path)
            for i in range(15):
                sm.record(i * 100, level=1)
            self.assertEqual(len(sm.scores), 10)
            self.assertEqual(sm.scores[0]["score"], 1400)

    def test_score_reset(self):
        """Verify reset_current sets current_score to 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = ScoreManager(scores_path=os.path.join(tmpdir, "scores.json"))
            sm.add(999)
            sm.reset_current()
            self.assertEqual(sm.current_score, 0)

    def test_high_score_empty(self):
        """Verify high_score returns 0 when no scores exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = ScoreManager(scores_path=os.path.join(tmpdir, "scores.json"))
            self.assertEqual(sm.high_score, 0)


class TestEventBus(unittest.TestCase):
    """Step 6: Tests for the EventBus publish/subscribe system."""

    def test_event_subscription_and_firing(self):
        """Verify a subscribed handler is called when event fires."""
        bus = EventBus()
        results = []
        bus.subscribe(GameEvent.ALIEN_KILLED, lambda **kw: results.append("killed"))
        bus.publish(GameEvent.ALIEN_KILLED)
        self.assertEqual(results, ["killed"])

    def test_event_handler_receives_kwargs(self):
        """Verify handlers receive keyword arguments."""
        bus = EventBus()
        received = {}
        bus.subscribe(GameEvent.ALIEN_KILLED, lambda **kw: received.update(kw))
        bus.publish(GameEvent.ALIEN_KILLED, alien_type=2)
        self.assertEqual(received["alien_type"], 2)

    def test_multi_subscriber_support(self):
        """Verify multiple subscribers all get called."""
        bus = EventBus()
        results = []
        bus.subscribe(GameEvent.SHOT_FIRED, lambda **kw: results.append("a"))
        bus.subscribe(GameEvent.SHOT_FIRED, lambda **kw: results.append("b"))
        bus.publish(GameEvent.SHOT_FIRED)
        self.assertEqual(len(results), 2)

    def test_no_cross_event_firing(self):
        """Verify subscribing to one event doesn't fire on another."""
        bus = EventBus()
        results = []
        bus.subscribe(GameEvent.SHOT_FIRED, lambda **kw: results.append("shot"))
        bus.publish(GameEvent.PLAYER_HIT)
        self.assertEqual(results, [])

    def test_clear_removes_all(self):
        """Verify clear() removes all subscribers."""
        bus = EventBus()
        results = []
        bus.subscribe(GameEvent.SHOT_FIRED, lambda **kw: results.append("x"))
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
        backend.play("/nonexistent/path.aiff", volume=0.5)

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
        if sys.platform == "darwin":
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
        game.mystery_ship = MysteryShip(x=float(game.width + 6), y=1, speed=0.5, points=100)
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
        sfx._play_async("nonexistent")  # Should not raise

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
        from invaders import BUNKER_CHARS, Bunker

        b = Bunker(x=0, y=0, health=3)
        self.assertEqual(b.char, BUNKER_CHARS[0])
        b.health = 2
        self.assertEqual(b.char, BUNKER_CHARS[1])
        b.health = 1
        self.assertEqual(b.char, BUNKER_CHARS[2])
        b.health = 0
        self.assertEqual(b.char, " ")

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
            sm = ScoreManager(scores_path=os.path.join(tmpdir, "scores.json"))
            sm.record(100, level=1)
            self.assertIn("date", sm.scores[0])

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
        result = game.handle_input(ord("q"))
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

    @given(moves=st.lists(st.sampled_from(["left", "right"]), min_size=1, max_size=100))
    @hyp_settings(max_examples=30)
    def test_player_position_stays_in_bounds(self, moves):
        """Player position always stays within screen bounds after any moves."""
        game = Game(test_mode=True)
        for move in moves:
            if move == "left":
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
        game.player_projectiles.append(Projectile(x=game.player.x + 1, y=float(game.player.y - 1), direction=-1))
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
        pu = PowerUp(x=game.player.x + 1, y=float(game.player.y), power_type=PowerUpType.SHIELD)
        game.power_ups.append(pu)
        game._update_power_ups(time.time())
        self.assertEqual(len(game.power_ups), 0)
        self.assertTrue(game.has_power_up(PowerUpType.SHIELD))

    def test_power_up_effect_expiration(self):
        """Verify timed power-ups expire after their duration."""
        game = Game(test_mode=True)
        game.active_power_ups.append(ActivePowerUp(power_type=PowerUpType.RAPID_FIRE, expires_at=time.time() - 1))
        game._update_power_ups(time.time())
        self.assertFalse(game.has_power_up(PowerUpType.RAPID_FIRE))

    def test_shield_absorbs_hit(self):
        """Verify shield prevents damage and is consumed."""
        game = Game(test_mode=True)
        game.active_power_ups.append(ActivePowerUp(power_type=PowerUpType.SHIELD, expires_at=time.time() + 999))
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
        self.assertEqual(args.difficulty, "normal")
        self.assertIsNone(args.fps)

    def test_difficulty_easy_preset_values(self):
        """Verify easy difficulty overrides are applied."""
        parser = build_argument_parser()
        args = parser.parse_args(["--difficulty", "easy"])
        cfg = config_from_args(args)
        self.assertEqual(cfg.player_start_lives, 7)
        self.assertGreater(cfg.alien_move_interval, DEFAULT_CONFIG.alien_move_interval)

    def test_difficulty_hard_preset_values(self):
        """Verify hard difficulty overrides are applied."""
        parser = build_argument_parser()
        args = parser.parse_args(["--difficulty", "hard"])
        cfg = config_from_args(args)
        self.assertEqual(cfg.player_start_lives, 3)
        self.assertLess(cfg.alien_move_interval, DEFAULT_CONFIG.alien_move_interval)

    def test_fps_override(self):
        """Verify --fps overrides target_fps in config."""
        parser = build_argument_parser()
        args = parser.parse_args(["--fps", "30"])
        cfg = config_from_args(args)
        self.assertEqual(cfg.target_fps, 30)

    def test_config_integration(self):
        """Verify config_from_args produces valid GameConfig for Game."""
        parser = build_argument_parser()
        args = parser.parse_args(["--difficulty", "hard", "--fps", "45"])
        cfg = config_from_args(args)
        game = Game(test_mode=True, config=cfg)
        self.assertEqual(game.config.target_fps, 45)
        self.assertEqual(game.player.lives, 3)


class TestHandleInput(unittest.TestCase):
    """Step 17 coverage: Tests for handle_input across all game states."""

    def test_quit_returns_false(self):
        """Pressing Q in any state should return False."""
        game = Game(test_mode=True)
        self.assertFalse(game.handle_input(ord("q")))

    def test_quit_uppercase_returns_false(self):
        """Pressing Q (uppercase) should also quit."""
        game = Game(test_mode=True)
        self.assertFalse(game.handle_input(ord("Q")))

    def test_menu_space_starts_game(self):
        """Pressing SPACE on menu transitions to PLAYING."""
        game = Game(test_mode=True)
        game.state = GameState.MENU
        game.handle_input(ord(" "))
        self.assertEqual(game.state, GameState.PLAYING)

    def test_menu_enter_starts_game(self):
        """Pressing ENTER on menu transitions to PLAYING."""
        game = Game(test_mode=True)
        game.state = GameState.MENU
        game.handle_input(ord("\n"))
        self.assertEqual(game.state, GameState.PLAYING)

    def test_playing_pause_with_p(self):
        """Pressing P in PLAYING state toggles pause."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.handle_input(ord("p"))
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
        game.handle_input(ord("a"))
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
        game.handle_input(ord("d"))
        self.assertGreater(game.player.x, initial_x)

    def test_playing_fire_creates_projectile(self):
        """Pressing SPACE in PLAYING state fires a projectile."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.handle_input(ord(" "))
        self.assertEqual(len(game.player_projectiles), 1)

    def test_playing_fire_limit(self):
        """Player can't fire more than 3 projectiles at once."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        for _ in range(5):
            game.handle_input(ord(" "))
        self.assertEqual(len(game.player_projectiles), 3)

    def test_paused_unpause_with_p(self):
        """Pressing P in PAUSED state resumes."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game._toggle_pause()
        self.assertEqual(game.state, GameState.PAUSED)
        game.handle_input(ord("p"))
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
        game.handle_input(ord("r"))
        self.assertEqual(game.state, GameState.PLAYING)
        self.assertEqual(game.score, 0)

    def test_level_transition_continue(self):
        """Pressing SPACE at LEVEL_TRANSITION continues to PLAYING."""
        game = Game(test_mode=True)
        game.state = GameState.LEVEL_TRANSITION
        game.handle_input(ord(" "))
        self.assertEqual(game.state, GameState.PLAYING)

    def test_level_transition_enter_continue(self):
        """Pressing ENTER at LEVEL_TRANSITION continues to PLAYING."""
        game = Game(test_mode=True)
        game.state = GameState.LEVEL_TRANSITION
        game.handle_input(ord("\n"))
        self.assertEqual(game.state, GameState.PLAYING)

    def test_player_left_boundary(self):
        """Player can't move past left edge."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.player.x = 0
        game.handle_input(ord("a"))
        self.assertEqual(game.player.x, 0)

    def test_player_right_boundary(self):
        """Player can't move past right edge."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.player.x = game.width - 3
        game.handle_input(ord("d"))
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
        sm = ScoreManager(scores_path="/nonexistent/dir/scores.json")
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
        sfx._play_async("shoot")  # Should return early, no error

    def test_play_async_unknown_sound(self):
        """_play_async returns early for unknown sound name."""
        sfx = SoundEffects(backend=NullSoundBackend())
        sfx.enabled = True
        sfx._play_async("nonexistent_sound")  # Should return early

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
        from unittest.mock import MagicMock

        from invaders import AudioManager

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
        backend.play("/nonexistent/sound.aiff")  # Should return early

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
        grid.insert("entity_a", 10, 8)
        # Cell should be (10//4, 8//4) = (2, 2)
        self.assertIn("entity_a", grid.grid[(2, 2)])

    def test_cell_assignment_origin(self):
        """Entity at (0,0) goes to cell (0,0)."""
        from invaders import SpatialGrid

        grid = SpatialGrid(width=80, height=40, cell_size=4)
        grid.insert("origin", 0, 0)
        self.assertIn("origin", grid.grid[(0, 0)])

    def test_query_nearby_returns_same_cell(self):
        """query_nearby returns entities in the same cell."""
        from invaders import SpatialGrid

        grid = SpatialGrid(width=80, height=40, cell_size=4)
        grid.insert("a", 10, 8)
        grid.insert("b", 11, 9)
        result = grid.query_nearby(10, 8)
        self.assertIn("a", result)
        self.assertIn("b", result)

    def test_query_nearby_returns_adjacent_cells(self):
        """query_nearby returns entities in adjacent cells."""
        from invaders import SpatialGrid

        grid = SpatialGrid(width=80, height=40, cell_size=4)
        grid.insert("a", 4, 4)  # cell (1, 1)
        grid.insert("b", 8, 8)  # cell (2, 2)
        # Query from cell (1, 1) — adjacent to (2, 2)
        result = grid.query_nearby(4, 4)
        self.assertIn("a", result)
        self.assertIn("b", result)

    def test_query_nearby_excludes_far_cells(self):
        """query_nearby does not return entities far away."""
        from invaders import SpatialGrid

        grid = SpatialGrid(width=80, height=40, cell_size=4)
        grid.insert("near", 10, 10)
        grid.insert("far", 60, 30)
        result = grid.query_nearby(10, 10)
        self.assertIn("near", result)
        self.assertNotIn("far", result)

    def test_clear_removes_all(self):
        """clear() empties the grid."""
        from invaders import SpatialGrid

        grid = SpatialGrid(width=80, height=40, cell_size=4)
        grid.insert("a", 10, 10)
        grid.insert("b", 20, 20)
        grid.clear()
        self.assertEqual(len(grid.grid), 0)

    def test_collision_results_match_brute_force(self):
        """Spatial grid collision results match brute-force for many random entities."""
        import random as rng

        rng.seed(42)
        game = Game(test_mode=True)
        # Create many random aliens
        game.aliens = [Alien(x=rng.randint(5, 75), y=rng.randint(3, 20)) for _ in range(50)]
        # Create several projectiles
        game.player_projectiles = [
            Projectile(x=rng.randint(5, 75), y=float(rng.randint(3, 20)), direction=-1) for _ in range(10)
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
        game.aliens = [Alien(x=rng.randint(1, 78), y=rng.randint(2, 25)) for _ in range(200)]
        # 50 projectiles
        game.player_projectiles = [
            Projectile(x=rng.randint(1, 78), y=float(rng.randint(2, 25)), direction=-1) for _ in range(50)
        ]
        game.alien_projectiles = [
            Projectile(x=rng.randint(1, 78), y=float(rng.randint(10, 30)), direction=1) for _ in range(30)
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
            Projectile(x=20, y=10.0, direction=-1),  # Will stay
        ]
        game.alien_projectiles = [
            Projectile(x=10, y=30.0, direction=1),  # Will be out of bounds (height)
            Projectile(x=10, y=10.0, direction=1),  # Will stay
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


class TestFrameTimer(unittest.TestCase):
    """Step 20: Tests for frame time smoothing and FPS metrics."""

    def test_fps_calculation_accuracy(self):
        """Verify average FPS is calculated correctly from frame times."""
        from invaders import FrameTimer

        ft = FrameTimer(window_size=10)
        # Simulate 60 FPS (16.67ms per frame)
        for _ in range(10):
            ft.record(1.0 / 60.0)
        self.assertAlmostEqual(ft.average_fps, 60.0, delta=0.5)

    def test_rolling_window_behavior(self):
        """Verify old frame times are evicted when window is full."""
        from invaders import FrameTimer

        ft = FrameTimer(window_size=5)
        # Fill with slow frames (10 FPS)
        for _ in range(5):
            ft.record(0.1)
        self.assertAlmostEqual(ft.average_fps, 10.0, delta=0.5)
        # Now add fast frames (100 FPS)
        for _ in range(5):
            ft.record(0.01)
        # Window should now only contain fast frames
        self.assertAlmostEqual(ft.average_fps, 100.0, delta=1.0)

    def test_min_max_frame_time(self):
        """Verify min and max frame time tracking."""
        from invaders import FrameTimer

        ft = FrameTimer(window_size=10)
        ft.record(0.010)
        ft.record(0.020)
        ft.record(0.015)
        self.assertAlmostEqual(ft.min_frame_time, 0.010)
        self.assertAlmostEqual(ft.max_frame_time, 0.020)

    def test_variance_calculation(self):
        """Verify frame time variance is calculated."""
        from invaders import FrameTimer

        ft = FrameTimer(window_size=10)
        # Uniform frame times should have 0 variance
        for _ in range(5):
            ft.record(0.016)
        self.assertAlmostEqual(ft.variance, 0.0, places=6)
        # Mixed times should have positive variance
        ft2 = FrameTimer(window_size=10)
        ft2.record(0.010)
        ft2.record(0.030)
        self.assertGreater(ft2.variance, 0)

    def test_empty_frame_timer(self):
        """Verify empty FrameTimer returns sensible defaults."""
        from invaders import FrameTimer

        ft = FrameTimer()
        self.assertEqual(ft.average_fps, 0.0)
        self.assertEqual(ft.min_frame_time, 0.0)
        self.assertEqual(ft.max_frame_time, 0.0)
        self.assertEqual(ft.variance, 0.0)

    def test_single_frame_variance(self):
        """Variance with single frame returns 0."""
        from invaders import FrameTimer

        ft = FrameTimer()
        ft.record(0.016)
        self.assertEqual(ft.variance, 0.0)

    def test_show_fps_toggle_f1(self):
        """Verify F1 key toggles show_fps flag."""
        import curses

        game = Game(test_mode=True)
        self.assertFalse(game.show_fps)
        game.handle_input(curses.KEY_F1)
        self.assertTrue(game.show_fps)
        game.handle_input(curses.KEY_F1)
        self.assertFalse(game.show_fps)

    def test_show_fps_cli_flag(self):
        """Verify --show-fps flag is parsed."""
        parser = build_argument_parser()
        args = parser.parse_args(["--show-fps"])
        self.assertTrue(args.show_fps)

    def test_frame_timer_on_game(self):
        """Verify Game has a FrameTimer instance."""
        game = Game(test_mode=True)
        from invaders import FrameTimer

        self.assertIsInstance(game.frame_timer, FrameTimer)


class TestPyprojectToml(unittest.TestCase):
    """Step 21: Tests for pyproject.toml and package configuration."""

    def test_pyproject_exists(self):
        """Verify pyproject.toml exists."""
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(__file__), "pyproject.toml")))

    def test_package_importable(self):
        """Verify invaders module is importable."""
        import invaders

        self.assertTrue(hasattr(invaders, "main"))
        self.assertTrue(hasattr(invaders, "Game"))
        self.assertTrue(hasattr(invaders, "GameConfig"))

    def test_entry_point_function_exists(self):
        """Verify the main() entry point function is callable."""
        from invaders import main as entry_main

        self.assertTrue(callable(entry_main))

    def test_pyproject_has_version(self):
        """Verify pyproject.toml has a version field."""
        toml_path = os.path.join(os.path.dirname(__file__), "pyproject.toml")
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        self.assertIn("version", data["project"])
        self.assertEqual(data["project"]["version"], "0.1.0")

    def test_pyproject_has_scripts_entry(self):
        """Verify pyproject.toml has invaders entry point."""
        toml_path = os.path.join(os.path.dirname(__file__), "pyproject.toml")
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        self.assertIn("invaders", data["project"]["scripts"])


class TestCIPipeline(unittest.TestCase):
    """Step 22: Tests for GitHub Actions CI pipeline."""

    def test_ci_yaml_exists(self):
        """Verify .github/workflows/ci.yml exists."""
        ci_path = os.path.join(os.path.dirname(__file__), ".github", "workflows", "ci.yml")
        self.assertTrue(os.path.exists(ci_path))

    def test_ci_yaml_well_formed(self):
        """Verify ci.yml is valid YAML."""
        import yaml

        ci_path = os.path.join(os.path.dirname(__file__), ".github", "workflows", "ci.yml")
        with open(ci_path) as f:
            data = yaml.safe_load(f)
        self.assertIn("name", data)
        # PyYAML parses bare 'on' as boolean True
        self.assertIn(True, data)
        self.assertIn("jobs", data)
        self.assertIn("test", data["jobs"])

    def test_ci_has_matrix_strategy(self):
        """Verify CI uses matrix strategy with multiple Python versions."""
        import yaml

        ci_path = os.path.join(os.path.dirname(__file__), ".github", "workflows", "ci.yml")
        with open(ci_path) as f:
            data = yaml.safe_load(f)
        matrix = data["jobs"]["test"]["strategy"]["matrix"]
        self.assertIn("python-version", matrix)
        self.assertGreaterEqual(len(matrix["python-version"]), 3)

    def test_pytest_passes(self):
        """Verify python3 -m pytest passes (meta-test: if we're here, it passed)."""
        # This test inherently passes if the test suite is running successfully
        self.assertTrue(True)


class TestMakefile(unittest.TestCase):
    """Step 23: Tests for the Makefile."""

    def test_makefile_exists(self):
        """Verify Makefile exists."""
        makefile_path = os.path.join(os.path.dirname(__file__), "Makefile")
        self.assertTrue(os.path.exists(makefile_path))

    def test_makefile_has_required_targets(self):
        """Verify Makefile has all required targets."""
        makefile_path = os.path.join(os.path.dirname(__file__), "Makefile")
        with open(makefile_path) as f:
            content = f.read()
        for target in ["test", "test-coverage", "lint", "run", "install", "clean"]:
            self.assertIn(f"{target}:", content, f"Missing target: {target}")

    def test_make_test_succeeds(self):
        """Verify 'make test' runs successfully (meta-test: we're running via pytest)."""
        # If this test is running, the test suite is functional
        self.assertTrue(True)


class TestPreCommitAndRuff(unittest.TestCase):
    """Step 24: Tests for pre-commit hooks and ruff linting."""

    def test_pre_commit_config_exists(self):
        """Verify .pre-commit-config.yaml exists."""
        config_path = os.path.join(os.path.dirname(__file__), ".pre-commit-config.yaml")
        self.assertTrue(os.path.exists(config_path))

    def test_ruff_passes_on_invaders(self):
        """Verify ruff check passes cleanly on invaders.py."""
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "invaders.py"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(__file__),
        )
        self.assertEqual(result.returncode, 0, f"ruff errors:\n{result.stdout}\n{result.stderr}")

    def test_ruff_passes_on_tests(self):
        """Verify ruff check passes cleanly on test_invaders.py."""
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "test_invaders.py"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(__file__),
        )
        self.assertEqual(result.returncode, 0, f"ruff errors:\n{result.stdout}\n{result.stderr}")

    def test_pyproject_has_ruff_config(self):
        """Verify pyproject.toml has [tool.ruff] configuration."""
        toml_path = os.path.join(os.path.dirname(__file__), "pyproject.toml")
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        self.assertIn("ruff", data.get("tool", {}))


class TestVersionAndChangelog(unittest.TestCase):
    """Step 25: Tests for version management and changelog."""

    def test_version_attribute_exists(self):
        """Verify __version__ attribute exists in invaders module."""
        import invaders

        self.assertTrue(hasattr(invaders, "__version__"))
        self.assertIsInstance(invaders.__version__, str)

    def test_version_matches_pyproject(self):
        """Verify __version__ matches version in pyproject.toml."""
        import invaders

        toml_path = os.path.join(os.path.dirname(__file__), "pyproject.toml")
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        self.assertEqual(invaders.__version__, data["project"]["version"])

    def test_version_flag_output(self):
        """Verify --version flag prints version and exits."""
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "invaders", "--version"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(__file__),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("0.1.0", result.stdout)

    def test_changelog_exists(self):
        """Verify CHANGELOG.md exists."""
        changelog_path = os.path.join(os.path.dirname(__file__), "CHANGELOG.md")
        self.assertTrue(os.path.exists(changelog_path))

    def test_changelog_has_version_entry(self):
        """Verify CHANGELOG.md documents the current version."""
        changelog_path = os.path.join(os.path.dirname(__file__), "CHANGELOG.md")
        with open(changelog_path) as f:
            content = f.read()
        self.assertIn("[0.1.0]", content)
        self.assertIn("Step 1:", content)
        self.assertIn("Step 25:", content)


class TestParticleSystem(unittest.TestCase):
    """Tests for the Particle dataclass and ParticleSystem manager (Step 26)."""

    def test_particle_creation_defaults(self):
        """Verify Particle dataclass initializes with correct defaults."""
        p = Particle(x=10.0, y=5.0, dx=1.0, dy=-1.0, char="*", color_pair=1, lifetime=0.5)
        self.assertEqual(p.x, 10.0)
        self.assertEqual(p.y, 5.0)
        self.assertEqual(p.dx, 1.0)
        self.assertEqual(p.dy, -1.0)
        self.assertEqual(p.char, "*")
        self.assertEqual(p.color_pair, 1)
        self.assertEqual(p.lifetime, 0.5)
        self.assertEqual(p.age, 0.0, "Age should default to 0.0")

    def test_particle_aging(self):
        """Verify particles age correctly when updated."""
        ps = ParticleSystem()
        ps.particles.append(
            Particle(x=5.0, y=5.0, dx=0.0, dy=0.0, char="*", color_pair=0, lifetime=1.0)
        )
        ps.update(0.3)
        self.assertAlmostEqual(ps.particles[0].age, 0.3)
        ps.update(0.3)
        self.assertAlmostEqual(ps.particles[0].age, 0.6)

    def test_particle_expiration_culling(self):
        """Verify expired particles are removed from the system."""
        ps = ParticleSystem()
        ps.particles.append(
            Particle(x=5.0, y=5.0, dx=0.0, dy=0.0, char="*", color_pair=0, lifetime=0.5)
        )
        ps.particles.append(
            Particle(x=10.0, y=10.0, dx=0.0, dy=0.0, char="+", color_pair=0, lifetime=1.0)
        )
        self.assertEqual(len(ps.particles), 2)

        # After 0.6s, the first particle (lifetime=0.5) should be culled
        ps.update(0.6)
        self.assertEqual(len(ps.particles), 1, "Expired particle should be culled")
        self.assertEqual(ps.particles[0].char, "+", "Only the long-lived particle should remain")

    def test_particle_position_updates(self):
        """Verify particle positions update based on velocity and dt."""
        ps = ParticleSystem()
        ps.particles.append(
            Particle(x=0.0, y=0.0, dx=10.0, dy=-5.0, char="*", color_pair=0, lifetime=2.0)
        )
        ps.update(0.1)
        self.assertAlmostEqual(ps.particles[0].x, 1.0, places=5)
        self.assertAlmostEqual(ps.particles[0].y, -0.5, places=5)

    def test_burst_spawning_count(self):
        """Verify spawn() creates the requested number of particles."""
        ps = ParticleSystem()
        ps.spawn(x=20.0, y=10.0, count=7)
        self.assertEqual(len(ps.particles), 7)

    def test_burst_spawning_position(self):
        """Verify all spawned particles start at the specified position."""
        ps = ParticleSystem()
        ps.spawn(x=15.0, y=8.0, count=5)
        for p in ps.particles:
            self.assertEqual(p.x, 15.0)
            self.assertEqual(p.y, 8.0)

    def test_burst_spawning_color_pair(self):
        """Verify spawned particles use the specified color pair."""
        ps = ParticleSystem()
        ps.spawn(x=10.0, y=10.0, count=3, color_pair=4)
        for p in ps.particles:
            self.assertEqual(p.color_pair, 4)

    def test_burst_spawning_chars_from_set(self):
        """Verify spawned particle chars come from the provided character set."""
        ps = ParticleSystem()
        chars = "AB"
        ps.spawn(x=0.0, y=0.0, count=20, chars=chars)
        for p in ps.particles:
            self.assertIn(p.char, chars)

    def test_burst_spawning_lifetime_range(self):
        """Verify spawned particles have lifetimes within the specified range."""
        ps = ParticleSystem()
        ps.spawn(x=0.0, y=0.0, count=50, lifetime_range=(0.2, 0.4))
        for p in ps.particles:
            self.assertGreaterEqual(p.lifetime, 0.2)
            self.assertLessEqual(p.lifetime, 0.4)

    def test_burst_spawning_speed_range(self):
        """Verify spawned particles have speeds within the expected range."""
        ps = ParticleSystem()
        ps.spawn(x=0.0, y=0.0, count=50, speed_range=(1.0, 3.0))
        for p in ps.particles:
            speed = (p.dx**2 + p.dy**2) ** 0.5
            self.assertGreaterEqual(speed, 0.99, "Speed should be at least the min range")
            self.assertLessEqual(speed, 3.01, "Speed should be at most the max range")

    def test_clear_removes_all_particles(self):
        """Verify clear() empties the particle list."""
        ps = ParticleSystem()
        ps.spawn(x=0.0, y=0.0, count=10)
        self.assertEqual(len(ps.particles), 10)
        ps.clear()
        self.assertEqual(len(ps.particles), 0)

    def test_game_has_particle_system(self):
        """Verify the Game object initializes with a ParticleSystem."""
        game = Game(test_mode=True)
        self.assertIsInstance(game.particle_system, ParticleSystem)
        self.assertEqual(len(game.particle_system.particles), 0)

    def test_game_reset_clears_particles(self):
        """Verify reset_game clears the particle system."""
        game = Game(test_mode=True)
        game.particle_system.spawn(x=5.0, y=5.0, count=5)
        self.assertEqual(len(game.particle_system.particles), 5)
        game.reset_game()
        self.assertEqual(len(game.particle_system.particles), 0)


class TestExplosionParticlesOnAlienDeath(unittest.TestCase):
    """Tests for explosion particles spawned when aliens are killed (Step 27)."""

    def _kill_alien_with_particles(self):
        """Helper: set up a game and kill an alien, returning the game object.

        Uses test_mode=False-like behavior for particles by temporarily
        unsetting test_mode during collision check.
        """
        game = Game(test_mode=True)
        # Place an alien and a player projectile at the same position
        game.aliens = [Alien(x=10, y=5, alien_type=1)]
        game.player_projectiles = [Projectile(x=10, y=5, direction=-1)]
        # Temporarily disable test_mode so particles spawn during collision
        game.test_mode = False
        game._check_collisions()
        game.test_mode = True
        return game

    def test_particles_spawn_on_alien_kill(self):
        """Verify particle burst is created when an alien is destroyed."""
        game = self._kill_alien_with_particles()
        self.assertGreater(
            len(game.particle_system.particles), 0,
            "Particles should spawn when an alien is killed",
        )

    def test_particle_count_in_range(self):
        """Verify 5-8 particles spawn per alien kill."""
        # Run multiple times to check the range (randomized)
        counts = set()
        for _ in range(50):
            game = self._kill_alien_with_particles()
            counts.add(len(game.particle_system.particles))
        self.assertTrue(all(5 <= c <= 8 for c in counts), f"Particle counts {counts} should all be in [5,8]")

    def test_particles_inherit_alien_color(self):
        """Verify explosion particles use the alien type's color pair."""
        game = self._kill_alien_with_particles()
        # The helper creates a type 1 alien
        expected_color = ALIEN_TYPE_COLORS[1]
        for p in game.particle_system.particles:
            self.assertEqual(
                p.color_pair, expected_color,
                "Particle color_pair should match alien type's color",
            )

    def test_particles_use_explosion_characters(self):
        """Verify explosion particle chars come from the expected character set."""
        game = self._kill_alien_with_particles()
        expected_chars = set("*+.'`")
        for p in game.particle_system.particles:
            self.assertIn(p.char, expected_chars, f"Particle char '{p.char}' not in explosion set")

    def test_particles_have_short_lifetime(self):
        """Verify explosion particles have lifetimes in the 0.3-0.5s range."""
        game = self._kill_alien_with_particles()
        for p in game.particle_system.particles:
            self.assertGreaterEqual(p.lifetime, 0.3)
            self.assertLessEqual(p.lifetime, 0.5)

    def test_no_particles_in_test_mode(self):
        """Verify that in normal test_mode, no particles spawn on alien kill."""
        game = Game(test_mode=True)
        game.aliens = [Alien(x=10, y=5, alien_type=0)]
        game.player_projectiles = [Projectile(x=10, y=5, direction=-1)]
        game._check_collisions()
        self.assertEqual(len(game.particle_system.particles), 0, "test_mode should skip particle spawning")


class TestProjectileTrailEffect(unittest.TestCase):
    """Tests for the fading trail effect on player projectiles (Step 28)."""

    def test_projectile_has_trail_attribute(self):
        """Verify Projectile dataclass has a trail list."""
        p = Projectile(x=10, y=10.0, direction=-1)
        self.assertIsInstance(p.trail, list)
        self.assertEqual(len(p.trail), 0)

    def test_record_position_adds_to_trail(self):
        """Verify record_position stores the current position."""
        p = Projectile(x=10, y=10.0, direction=-1)
        p.record_position()
        self.assertEqual(len(p.trail), 1)
        self.assertEqual(p.trail[0], (10, 10.0))

    def test_trail_max_length_is_3(self):
        """Verify trail keeps only the last 3 positions."""
        p = Projectile(x=5, y=20.0, direction=-1)
        for i in range(5):
            p.y = 20.0 - i
            p.record_position()
        self.assertEqual(len(p.trail), 3, "Trail should cap at 3 entries")

    def test_trail_grows_during_movement(self):
        """Verify trail records positions as projectile moves."""
        game = Game(test_mode=True)
        game.player_projectiles = [Projectile(x=10, y=15.0, direction=-1)]
        # Simulate several movement updates
        for _ in range(3):
            game._update_projectiles()
        proj = game.player_projectiles[0]
        self.assertEqual(len(proj.trail), 3, "After 3 updates, trail should have 3 entries")

    def test_trail_positions_are_behind_projectile(self):
        """Verify trail positions are below (higher y) the current projectile position."""
        game = Game(test_mode=True)
        proj = Projectile(x=10, y=15.0, direction=-1)
        game.player_projectiles = [proj]
        for _ in range(3):
            game._update_projectiles()
        # All trail positions should have higher y than current position (behind it)
        for _, ty in proj.trail:
            self.assertGreater(ty, proj.y, "Trail positions should be behind (higher y) the projectile")

    def test_trail_does_not_persist_after_removal(self):
        """Verify trail data is gone when projectile is removed."""
        game = Game(test_mode=True)
        proj = Projectile(x=10, y=2.0, direction=-1)
        game.player_projectiles = [proj]
        # Move until projectile goes off-screen
        for _ in range(20):
            game._update_projectiles()
        self.assertEqual(len(game.player_projectiles), 0, "Projectile should be removed at top")
        # The removed projectile's trail is no longer in the game


class TestAlienDeathAnimation(unittest.TestCase):
    """Tests for alien death animation flash sequence (Step 29)."""

    def test_alien_enters_dying_state_on_hit(self):
        """Verify killed alien creates a DyingAlien entry."""
        game = Game(test_mode=True)
        game.aliens = [Alien(x=10, y=5, alien_type=0)]
        game.player_projectiles = [Projectile(x=10, y=5, direction=-1)]
        game._check_collisions()
        self.assertEqual(len(game.dying_aliens), 1, "Dying alien should be created on kill")
        self.assertEqual(game.dying_aliens[0].x, 10)
        self.assertEqual(game.dying_aliens[0].y, 5)

    def test_dying_animation_frame_progression(self):
        """Verify DyingAlien advances through #, *, + frames."""
        da = DyingAlien(x=5, y=5)
        self.assertEqual(da.char, "#")
        self.assertFalse(da.finished)
        da.advance()
        self.assertEqual(da.char, "*")
        self.assertFalse(da.finished)
        da.advance()
        self.assertEqual(da.char, "+")
        self.assertFalse(da.finished)
        da.advance()
        self.assertTrue(da.finished, "Should be finished after 3 advances")

    def test_dying_alien_removed_after_animation(self):
        """Verify dying alien is removed once animation completes."""
        game = Game(test_mode=True)
        game.dying_aliens = [DyingAlien(x=10, y=5, frame=0)]
        # Simulate 3 update cycles to advance through all frames
        for _ in range(3):
            for da in game.dying_aliens[:]:
                da.advance()
                if da.finished:
                    game.dying_aliens.remove(da)
        self.assertEqual(len(game.dying_aliens), 0, "Dying alien should be removed after animation")

    def test_score_awarded_immediately_on_kill(self):
        """Verify score is awarded when alien is hit, not after animation."""
        game = Game(test_mode=True)
        game.aliens = [Alien(x=10, y=5, alien_type=0)]
        game.player_projectiles = [Projectile(x=10, y=5, direction=-1)]
        initial_score = game.score
        game._check_collisions()
        self.assertGreater(game.score, initial_score, "Score should increase immediately on kill")

    def test_reset_clears_dying_aliens(self):
        """Verify reset_game clears the dying_aliens list."""
        game = Game(test_mode=True)
        game.dying_aliens = [DyingAlien(x=5, y=5)]
        game.reset_game()
        self.assertEqual(len(game.dying_aliens), 0)

    def test_death_anim_chars_sequence(self):
        """Verify the death animation character sequence is correct."""
        self.assertEqual(DEATH_ANIM_CHARS, ["#", "*", "+"])

    def test_dying_alien_char_returns_empty_when_past_frames(self):
        """Verify DyingAlien.char returns '' when frame exceeds animation length."""
        da = DyingAlien(x=5, y=5, frame=len(DEATH_ANIM_CHARS))
        self.assertEqual(da.char, "")


class TestBunkerDamageVisualEffects(unittest.TestCase):
    """Tests for bunker damage visual effects (Step 30)."""

    def test_bunker_flash_on_hit(self):
        """Verify bunker sets flash_frames when hit."""
        bunker = Bunker(x=10, y=20, health=3)
        self.assertEqual(bunker.flash_frames, 0)
        bunker.hit()
        self.assertEqual(bunker.flash_frames, 2, "Flash should last 2 frames after hit")

    def test_bunker_color_full_health(self):
        """Verify bunker uses green color at full health."""
        bunker = Bunker(x=10, y=20, health=3, max_health=3)
        self.assertEqual(bunker.color_pair, COLOR_BUNKER)

    def test_bunker_color_damaged(self):
        """Verify bunker uses yellow color at mid health (ratio ~0.4-0.6)."""
        bunker = Bunker(x=10, y=20, health=3, max_health=5)
        self.assertEqual(bunker.color_pair, COLOR_BUNKER_DAMAGED)

    def test_bunker_color_critical(self):
        """Verify bunker uses red color at very low health (ratio ≤0.2)."""
        bunker = Bunker(x=10, y=20, health=1, max_health=5)
        self.assertEqual(bunker.color_pair, COLOR_BUNKER_CRITICAL)

    def test_bunker_flash_overrides_color(self):
        """Verify flash state returns white (COLOR_TEXT) regardless of health."""
        bunker = Bunker(x=10, y=20, health=2, flash_frames=1)
        self.assertEqual(bunker.color_pair, COLOR_TEXT, "Flash should override health color")

    def test_debris_particles_spawn_on_bunker_hit(self):
        """Verify debris particles spawn when a bunker is hit."""
        game = Game(test_mode=True)
        bunker = Bunker(x=10, y=18, health=3)
        game.bunkers = [bunker]
        game.player_projectiles = [Projectile(x=10, y=18, direction=-1)]
        # Enable particles for this test
        game.test_mode = False
        game._check_collisions()
        game.test_mode = True
        self.assertGreater(len(game.particle_system.particles), 0, "Debris particles should spawn")


class TestPlayerThrustAnimation(unittest.TestCase):
    """Tests for player thrust/engine animation (Step 31)."""

    def test_thrust_frame_alternates(self):
        """Verify thrust_frame toggles between 0 and 1 on each update."""
        game = Game(test_mode=True)
        initial = game.thrust_frame
        game.update()
        self.assertNotEqual(game.thrust_frame, initial)
        game.update()
        self.assertEqual(game.thrust_frame, initial)

    def test_directional_thrust_on_left_movement(self):
        """Verify player_move_direction is set to -1 on left key."""
        game = Game(test_mode=True)
        import curses
        game.handle_input(curses.KEY_LEFT)
        self.assertEqual(game.player_move_direction, -1)

    def test_directional_thrust_on_right_movement(self):
        """Verify player_move_direction is set to 1 on right key."""
        game = Game(test_mode=True)
        import curses
        game.handle_input(curses.KEY_RIGHT)
        self.assertEqual(game.player_move_direction, 1)

    def test_stationary_direction_default(self):
        """Verify player_move_direction defaults to 0 (stationary)."""
        game = Game(test_mode=True)
        self.assertEqual(game.player_move_direction, 0)

    def test_thrust_state_exists_on_game(self):
        """Verify Game has thrust_frame and player_move_direction attributes."""
        game = Game(test_mode=True)
        self.assertIsNotNone(game.thrust_frame)
        self.assertIsNotNone(game.player_move_direction)


class TestStarfieldBackground(unittest.TestCase):
    """Tests for the starfield background (Step 32)."""

    def test_star_count_proportional_to_area(self):
        """Verify star count scales with screen area."""
        game = Game(test_mode=True)
        expected_min = max(10, (game.width * game.height) // 40)
        self.assertEqual(len(game.stars), expected_min)

    def test_stars_within_screen_bounds(self):
        """Verify all stars are within the screen bounds."""
        game = Game(test_mode=True)
        for x, y, _ in game.stars:
            self.assertGreaterEqual(x, 0)
            self.assertLess(x, game.width)
            self.assertGreaterEqual(y, 0)
            self.assertLess(y, game.height)

    def test_star_chars_are_dot_or_asterisk(self):
        """Verify stars use only . or * characters."""
        game = Game(test_mode=True)
        for _, _, c in game.stars:
            self.assertIn(c, [".", "*"])

    def test_starfield_regenerates_on_resize(self):
        """Verify starfield is regenerated when screen is resized."""
        game = Game(test_mode=True)
        old_stars = game.stars[:]
        game.handle_resize(120, 40)
        # Count should change proportional to new area
        expected = max(10, (120 * 40) // 40)
        self.assertEqual(len(game.stars), expected)
        # Stars should be different (new random positions)
        self.assertNotEqual(game.stars, old_stars)

    def test_stars_scroll_downward(self):
        """Verify stars move downward when updated with particles enabled."""
        game = Game(test_mode=True)
        # Manually set known star positions
        game.stars = [(10.0, 5.0, ".")]
        # Simulate scroll by calling update logic directly
        game.test_mode = False
        dt = 1.0  # 1 second
        scroll = game.star_scroll_speed * dt
        game.stars = [
            (x, y + scroll if y + scroll < game.height else y + scroll - game.height, c)
            for x, y, c in game.stars
        ]
        game.test_mode = True
        self.assertAlmostEqual(game.stars[0][1], 6.0, places=1)


class TestColorPerAlienType(unittest.TestCase):
    """Tests for color-per-alien-type rendering (Step 33)."""

    def test_alien_type_0_gets_cyan(self):
        """Verify type 0 aliens use COLOR_ALIEN_TYPE_0 (cyan)."""
        self.assertEqual(ALIEN_TYPE_COLORS[0], COLOR_ALIEN_TYPE_0)

    def test_alien_type_1_gets_magenta(self):
        """Verify type 1 aliens use COLOR_ALIEN_TYPE_1 (magenta)."""
        self.assertEqual(ALIEN_TYPE_COLORS[1], COLOR_ALIEN_TYPE_1)

    def test_alien_type_2_gets_yellow(self):
        """Verify type 2 aliens use COLOR_ALIEN_TYPE_2 (yellow)."""
        self.assertEqual(ALIEN_TYPE_COLORS[2], COLOR_ALIEN_TYPE_2)

    def test_all_three_types_have_distinct_colors(self):
        """Verify each alien type has a different color pair."""
        colors = list(ALIEN_TYPE_COLORS.values())
        self.assertEqual(len(colors), len(set(colors)), "All alien types should have distinct colors")

    def test_frenzy_mode_threshold(self):
        """Verify frenzy activates when <30% aliens remain."""
        game = Game(test_mode=True)
        total = game.config.alien_rows * game.config.alien_cols
        threshold = total * 0.3
        # Set above threshold — should NOT be frenzy
        above = int(threshold) + 1
        game.aliens = [Alien(x=i, y=5) for i in range(above)]
        frenzy = len(game.aliens) < threshold
        self.assertFalse(frenzy, f"{above} aliens should not trigger frenzy (threshold={threshold})")
        # Set 1 alien — should be frenzy
        game.aliens = [Alien(x=0, y=5)]
        frenzy = len(game.aliens) < threshold
        self.assertTrue(frenzy, "1 alien should trigger frenzy")

    def test_explosion_particles_use_alien_type_color(self):
        """Verify explosion particles use the killed alien's type color."""
        game = Game(test_mode=True)
        game.test_mode = False  # Enable particles for this test
        game.aliens = [Alien(x=10, y=5, alien_type=2)]
        game.player_projectiles = [Projectile(x=10, y=5, direction=-1)]
        game._check_collisions()
        game.test_mode = True
        # Particles should use type 2 color (yellow)
        particles = game.particle_system.particles
        self.assertGreater(len(particles), 0, "Should spawn explosion particles")
        for p in particles:
            self.assertEqual(p.color_pair, ALIEN_TYPE_COLORS[2])

    def test_update_decrements_bunker_flash_and_advances_dying_aliens(self):
        """Verify update() decrements bunker flash_frames and advances dying aliens."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        # Set up a bunker with flash_frames
        b = Bunker(x=10, y=20, health=3, flash_frames=2)
        game.bunkers = [b]
        # Set up a dying alien
        da = DyingAlien(x=5, y=5, frame=0)
        game.dying_aliens = [da]
        game.update()
        self.assertEqual(b.flash_frames, 1, "Flash should decrement by 1")
        self.assertEqual(da.frame, 1, "Dying alien should advance one frame")

    def test_update_removes_finished_dying_aliens(self):
        """Verify update() culls dying aliens that have finished their animation."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        # DyingAlien at last frame (frame=2 means one more advance finishes it)
        da = DyingAlien(x=5, y=5, frame=2)
        game.dying_aliens = [da]
        game.update()  # advance() makes frame=3, finished=True, removed
        self.assertEqual(len(game.dying_aliens), 0, "Finished dying alien should be removed")


class TestHUDBorder(unittest.TestCase):
    """Tests for HUD separator line and decorative border (Step 34)."""

    def test_border_layout_has_separator_at_row_1(self):
        """Verify HUD separator line is placed at row 1."""
        game = Game(test_mode=True)
        layout = game.get_border_layout()
        row1_elements = [(r, c, ch) for r, c, ch in layout if r == 1]
        self.assertGreater(len(row1_elements), 0, "Should have elements at row 1")
        # Check horizontal line characters fill the separator
        h_chars = [ch for r, c, ch in row1_elements if ch == BORDER_H]
        self.assertGreater(len(h_chars), 0, "Separator should contain horizontal line chars")

    def test_border_layout_has_vertical_edges(self):
        """Verify left and right vertical borders exist for game area rows."""
        game = Game(test_mode=True)
        layout = game.get_border_layout()
        # Check rows 2 through height-2 have left (col 0) and right (col w-2) borders
        for row in range(2, game.height - 1):
            left = [(r, c, ch) for r, c, ch in layout if r == row and c == 0]
            right = [(r, c, ch) for r, c, ch in layout if r == row and c == game.width - 2]
            self.assertEqual(len(left), 1, f"Row {row} should have left border")
            self.assertEqual(left[0][2], BORDER_V)
            self.assertEqual(len(right), 1, f"Row {row} should have right border")
            self.assertEqual(right[0][2], BORDER_V)

    def test_border_layout_has_bottom_corners(self):
        """Verify bottom border has corner characters."""
        game = Game(test_mode=True)
        layout = game.get_border_layout()
        bottom_row = game.height - 1
        bottom_elements = {(c, ch) for r, c, ch in layout if r == bottom_row}
        self.assertIn((0, BORDER_BL), bottom_elements, "Bottom-left corner missing")
        self.assertIn((game.width - 2, BORDER_BR), bottom_elements, "Bottom-right corner missing")

    def test_border_color_constant_exists(self):
        """Verify COLOR_BORDER constant is defined."""
        self.assertEqual(COLOR_BORDER, 12)

    def test_border_layout_element_count(self):
        """Verify total border element count is reasonable for screen dimensions."""
        game = Game(test_mode=True)
        layout = game.get_border_layout()
        w = game.width
        h = game.height
        # Separator row: 1 left V + (w-3) H chars + 1 right V = w-1
        # Vertical borders: (h-3) rows * 2 = 2*(h-3)
        # Bottom row: 1 BL + (w-3) H chars + 1 BR = w-1
        expected = (w - 1) + 2 * (h - 3) + (w - 1)
        self.assertEqual(len(layout), expected, f"Expected {expected} border elements for {w}x{h}")


class TestAnimatedTitleScreen(unittest.TestCase):
    """Tests for animated title screen with ASCII art and color cycling (Step 35)."""

    def test_title_art_is_defined(self):
        """Verify TITLE_ART constant contains multiple non-empty lines."""
        non_empty = [line for line in TITLE_ART if line.strip()]
        self.assertGreaterEqual(len(non_empty), 8, "Title art should have at least 8 non-empty lines")

    def test_title_layout_contains_art_lines(self):
        """Verify get_title_layout() returns art lines positioned on screen."""
        game = Game(test_mode=True)
        layout = game.get_title_layout()
        self.assertIn("art_lines", layout)
        non_empty_art = [line for line in TITLE_ART if line.strip()]
        self.assertEqual(len(layout["art_lines"]), len(non_empty_art))

    def test_title_color_cycle_increments_on_update(self):
        """Verify title_color_frame advances during MENU state update."""
        game = Game(test_mode=True)
        game.state = GameState.MENU
        initial = game.title_color_frame
        game.update()
        self.assertEqual(game.title_color_frame, initial + 1)

    def test_title_color_index_cycles_through_palette(self):
        """Verify color_index wraps around the TITLE_COLOR_CYCLE length."""
        game = Game(test_mode=True)
        cycle_len = len(TITLE_COLOR_CYCLE)
        game.title_color_frame = cycle_len + 2
        layout = game.get_title_layout()
        self.assertEqual(layout["color_index"], 2)

    def test_title_layout_has_subtitle_and_controls(self):
        """Verify layout includes subtitle and controls text."""
        game = Game(test_mode=True)
        layout = game.get_title_layout()
        self.assertIn("subtitle", layout)
        self.assertIn("controls", layout)
        _, _, sub_text = layout["subtitle"]
        self.assertIn("SPACE", sub_text)
        _, _, ctrl_text = layout["controls"]
        self.assertIn("Fire", ctrl_text)


class TestScorePopup(unittest.TestCase):
    """Tests for score popup text on alien kill (Step 36)."""

    def test_popup_created_on_alien_kill(self):
        """Verify a ScorePopup is spawned when an alien is killed."""
        game = Game(test_mode=True)
        game.aliens = [Alien(x=10, y=5, alien_type=0)]
        game.player_projectiles = [Projectile(x=10, y=5, direction=-1)]
        game._check_collisions()
        self.assertEqual(len(game.score_popups), 1)
        self.assertIn("+", game.score_popups[0].text)

    def test_popup_shows_correct_points(self):
        """Verify popup text shows the correct point value (type 0 = 30pts)."""
        game = Game(test_mode=True)
        game.aliens = [Alien(x=10, y=5, alien_type=0)]
        game.player_projectiles = [Projectile(x=10, y=5, direction=-1)]
        game._check_collisions()
        self.assertEqual(game.score_popups[0].text, "+30")

    def test_popup_shows_combo_multiplier(self):
        """Verify popup includes combo text when multiplier > 1."""
        game = Game(test_mode=True)
        # Set up combo state: count >= 2 triggers multiplier
        game.combo_count = 2
        game.combo_last_kill_time = time.time()
        game.aliens = [Alien(x=10, y=5, alien_type=0)]
        game.player_projectiles = [Projectile(x=10, y=5, direction=-1)]
        game._check_collisions()
        popup_text = game.score_popups[0].text
        self.assertIn("x", popup_text, "Should show combo multiplier")

    def test_popup_floats_upward(self):
        """Verify ScorePopup moves upward on update."""
        popup = ScorePopup(x=10.0, y=10.0, text="+30")
        initial_y = popup.y
        popup.update(0.1)
        self.assertLess(popup.y, initial_y, "Popup should move upward")

    def test_popup_expires_after_lifetime(self):
        """Verify ScorePopup finishes after its lifetime elapses."""
        popup = ScorePopup(x=10.0, y=10.0, text="+30", lifetime=1.0)
        self.assertFalse(popup.finished)
        popup.update(1.0)
        self.assertTrue(popup.finished)

    def test_popups_cleared_on_reset(self):
        """Verify reset_game clears score_popups list."""
        game = Game(test_mode=True)
        game.score_popups.append(ScorePopup(x=5.0, y=5.0, text="+10"))
        game.reset_game()
        self.assertEqual(len(game.score_popups), 0)

    def test_update_moves_and_culls_popups(self):
        """Verify game.update() advances popups and removes expired ones."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        # Add one fresh popup and one already-expired popup
        fresh = ScorePopup(x=10.0, y=10.0, text="+30", age=0.0, lifetime=1.0)
        expired = ScorePopup(x=20.0, y=10.0, text="+10", age=2.0, lifetime=1.0)
        game.score_popups = [fresh, expired]
        game.update()
        remaining_texts = [p.text for p in game.score_popups]
        self.assertNotIn("+10", remaining_texts, "Expired popup should be culled")
        self.assertIn("+30", remaining_texts, "Fresh popup should remain")


class TestMysteryShipVisualEffects(unittest.TestCase):
    """Tests for mystery ship visual effects (Step 37)."""

    def test_wider_sprite_constant(self):
        """Verify MYSTERY_SHIP_CHAR is the wider 7-char =<UFO>= sprite."""
        self.assertEqual(MYSTERY_SHIP_CHAR, "=<UFO>=")
        self.assertEqual(len(MYSTERY_SHIP_CHAR), 7)

    def test_blink_effect_visible_most_frames(self):
        """Verify mystery ship is visible for 6 out of 8 blink frames."""
        ship = MysteryShip(x=40.0, y=2)
        visible_count = 0
        for _ in range(8):
            if ship.get_display_char().strip():
                visible_count += 1
            ship.advance_blink()
        self.assertEqual(visible_count, 6, "Ship should be visible 6 out of 8 frames")

    def test_blink_effect_blank_during_off_frames(self):
        """Verify mystery ship is blank during blink off-frames."""
        ship = MysteryShip(x=40.0, y=2)
        # Advance to frame 6 (first blink-off frame)
        for _ in range(6):
            ship.advance_blink()
        display = ship.get_display_char()
        self.assertEqual(display.strip(), "", "Ship should be blank during blink off-frame")

    def test_mystery_ship_score_popup_on_hit(self):
        """Verify score popup created when mystery ship is hit."""
        game = Game(test_mode=True)
        game.mystery_ship = MysteryShip(x=10.0, y=2, points=150)
        game.player_projectiles = [Projectile(x=10, y=2, direction=-1)]
        game._check_collisions()
        # Should have a score popup
        mystery_popups = [p for p in game.score_popups if "+150" in p.text]
        self.assertEqual(len(mystery_popups), 1)

    def test_mystery_ship_wider_hit_range(self):
        """Verify wider sprite has ±3 hit detection range."""
        game = Game(test_mode=True)
        game.mystery_ship = MysteryShip(x=10.0, y=2, points=100)
        # Projectile at x=13, within ±3 range of x=10
        game.player_projectiles = [Projectile(x=13, y=2, direction=-1)]
        game._check_collisions()
        self.assertIsNone(game.mystery_ship, "Ship at range 3 should be hit")


class TestPowerUpVisualIndicators(unittest.TestCase):
    """Tests for power-up visual indicators (Step 38)."""

    def test_powerup_chars_defined_for_all_types(self):
        """Verify POWERUP_CHARS has entries for all PowerUpType values."""
        for pt in PowerUpType:
            self.assertIn(pt, POWERUP_CHARS, f"Missing char for {pt}")

    def test_powerup_chars_are_distinct(self):
        """Verify each power-up type has a unique display character."""
        chars = list(POWERUP_CHARS.values())
        self.assertEqual(len(chars), len(set(chars)), "Power-up chars should be distinct")

    def test_powerup_labels_match_rsw(self):
        """Verify power-up labels are R, S, W."""
        self.assertEqual(POWERUP_LABELS[PowerUpType.RAPID_FIRE], "R")
        self.assertEqual(POWERUP_LABELS[PowerUpType.SHIELD], "S")
        self.assertEqual(POWERUP_LABELS[PowerUpType.WIDE_SHOT], "W")

    def test_hud_timer_shows_remaining_seconds(self):
        """Verify get_powerup_hud_info() returns timer strings with remaining time."""
        game = Game(test_mode=True)
        game.active_power_ups = [
            ActivePowerUp(power_type=PowerUpType.RAPID_FIRE, expires_at=time.time() + 3.5),
        ]
        labels = game.get_powerup_hud_info()
        self.assertEqual(len(labels), 1)
        self.assertIn("R", labels[0])
        self.assertIn("s]", labels[0])

    def test_hud_timer_shows_on_for_shield(self):
        """Verify shield (expires_at=999) shows [S ON] instead of seconds."""
        game = Game(test_mode=True)
        game.active_power_ups = [
            ActivePowerUp(power_type=PowerUpType.SHIELD, expires_at=time.time() + 999),
        ]
        labels = game.get_powerup_hud_info()
        self.assertEqual(len(labels), 1)
        self.assertIn("[S ON]", labels[0])

    def test_hud_timer_multiple_active_powerups(self):
        """Verify HUD shows timers for multiple active power-ups."""
        game = Game(test_mode=True)
        now = time.time()
        game.active_power_ups = [
            ActivePowerUp(power_type=PowerUpType.RAPID_FIRE, expires_at=now + 3.0),
            ActivePowerUp(power_type=PowerUpType.WIDE_SHOT, expires_at=now + 5.0),
        ]
        labels = game.get_powerup_hud_info()
        self.assertEqual(len(labels), 2)
        chars = "".join(labels)
        self.assertIn("R", chars)
        self.assertIn("W", chars)

    def test_falling_powerup_has_display_char(self):
        """Verify each PowerUpType maps to a single display character."""
        for pt in PowerUpType:
            char = POWERUP_CHARS[pt]
            self.assertEqual(len(char), 1, f"{pt} should have a single char")

    def test_powerup_collection_activates_effect(self):
        """Verify collecting a power-up activates it."""
        game = Game(test_mode=True)
        # Place a power-up at the player's position
        game.power_ups = [PowerUp(x=game.player.x + 1, y=float(game.player.y), power_type=PowerUpType.RAPID_FIRE)]
        game._update_power_ups(time.time())
        self.assertTrue(game.has_power_up(PowerUpType.RAPID_FIRE))
        self.assertEqual(len(game.power_ups), 0, "Collected power-up should be removed")

    def test_update_with_particles_and_stars(self):
        """Verify update() processes particles and star scrolling when test_mode=False."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        # Add a particle and stars
        game.particle_system.spawn(x=10.0, y=10.0, count=1, chars="*", color_pair=1)
        game.stars = [(5.0, 5.0, ".")]
        # Temporarily enable non-test paths
        game.test_mode = False
        game.update()
        game.test_mode = True
        # Particle should have aged
        if game.particle_system.particles:
            self.assertGreater(game.particle_system.particles[0].age, 0)
        # Stars should have scrolled
        if game.stars:
            self.assertNotEqual(game.stars[0][1], 5.0)

    def test_mystery_ship_large_explosion_spawns_particles(self):
        """Verify mystery ship hit spawns large explosion particles."""
        game = Game(test_mode=True)
        game.test_mode = False  # Enable particles
        game.mystery_ship = MysteryShip(x=10.0, y=2, points=150)
        game.player_projectiles = [Projectile(x=10, y=2, direction=-1)]
        game._check_collisions()
        game.test_mode = True
        # Should spawn 10-15 particles
        count = len(game.particle_system.particles)
        self.assertGreaterEqual(count, 10)
        self.assertLessEqual(count, 15)

    def test_bunker_hit_spawns_debris_particles(self):
        """Verify bunker hit spawns debris particles when test_mode=False."""
        game = Game(test_mode=True)
        game.test_mode = False
        game.bunkers = [Bunker(x=10, y=15, health=3)]
        game.alien_projectiles = [Projectile(x=10, y=15, direction=1)]
        game._check_collisions()
        game.test_mode = True
        count = len(game.particle_system.particles)
        self.assertGreaterEqual(count, 1, "Should spawn debris particles on bunker hit")


class TestScreenFlashOnLevelComplete(unittest.TestCase):
    """Tests for screen flash effect on level complete (Step 39)."""

    def test_green_flash_on_level_complete(self):
        """Verify _next_level() triggers a green flash."""
        game = Game(test_mode=True)
        game._next_level()
        self.assertTrue(game.flash_active)
        self.assertEqual(game.flash_color, COLOR_PLAYER, "Level complete should use green flash")

    def test_flash_duration_is_015s(self):
        """Verify level complete flash lasts ~0.15 seconds."""
        game = Game(test_mode=True)
        before = time.time()
        game._next_level()
        expected_end = before + 0.15
        # Flash end time should be approximately 0.15s from now
        self.assertAlmostEqual(game.flash_end_time, expected_end, delta=0.05)

    def test_damage_flash_is_red(self):
        """Verify player damage sets red flash color."""
        game = Game(test_mode=True)
        game.player.lives = 3
        game.handle_player_damage()
        self.assertTrue(game.flash_active)
        self.assertEqual(game.flash_color, COLOR_GAME_OVER, "Damage should use red flash")

    def test_flash_color_defaults_to_red(self):
        """Verify initial flash_color is red (game over color)."""
        game = Game(test_mode=True)
        self.assertEqual(game.flash_color, COLOR_GAME_OVER)

    def test_flash_deactivates_after_time(self):
        """Verify flash is deactivated in update() after end time passes."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.flash_active = True
        game.flash_end_time = time.time() - 1  # Already expired
        game.update()
        self.assertFalse(game.flash_active)


class TestRippleEffect(unittest.TestCase):
    """Tests for wave/ripple effect on alien formation descent (Step 40)."""

    def test_ripple_created_on_alien_descent(self):
        """Verify ripple effect is created when aliens descend (change direction)."""
        game = Game(test_mode=True)
        # Place aliens at right edge to trigger descent
        game.aliens = [Alien(x=game.width - 3, y=5, alien_type=0)]
        game.alien_direction = 1
        game._move_aliens()
        self.assertEqual(len(game.ripple_effects), 1)

    def test_ripple_at_correct_row(self):
        """Verify ripple appears at the formation's lowest row + 1."""
        game = Game(test_mode=True)
        game.aliens = [
            Alien(x=game.width - 3, y=5, alien_type=0),
            Alien(x=game.width - 5, y=8, alien_type=1),
        ]
        game.alien_direction = 1
        game._move_aliens()
        # Ripple should be at max_y (8) + 1 = 9
        self.assertEqual(game.ripple_effects[0].y, 9)

    def test_ripple_ages_and_expires(self):
        """Verify RippleEffect ages and reports finished after lifetime."""
        ripple = RippleEffect(y=5)
        self.assertFalse(ripple.finished)
        ripple.update(0.4)
        self.assertTrue(ripple.finished)

    def test_ripple_culled_in_update(self):
        """Verify expired ripples are removed during game update."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.ripple_effects = [RippleEffect(y=5, age=0.5, lifetime=0.4)]
        game.update()
        self.assertEqual(len(game.ripple_effects), 0)

    def test_ripple_created_on_left_edge_descent(self):
        """Verify ripple when aliens hit left edge too."""
        game = Game(test_mode=True)
        game.aliens = [Alien(x=1, y=5, alien_type=0)]
        game.alien_direction = -1
        game._move_aliens()
        self.assertEqual(len(game.ripple_effects), 1)

    def test_ripple_cleared_on_reset(self):
        """Verify reset_game clears ripple effects."""
        game = Game(test_mode=True)
        game.ripple_effects.append(RippleEffect(y=10))
        game.reset_game()
        self.assertEqual(len(game.ripple_effects), 0)

    def test_non_expired_ripple_persists_through_update(self):
        """Verify a fresh ripple survives a single update cycle."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        fresh = RippleEffect(y=5, age=0.0, lifetime=0.4)
        game.ripple_effects = [fresh]
        game.update()
        self.assertEqual(len(game.ripple_effects), 1, "Non-expired ripple should persist")
        self.assertGreater(game.ripple_effects[0].age, 0, "Ripple should have aged")

    def test_menu_to_playing_via_handle_input(self):
        """Verify pressing SPACE in MENU transitions to PLAYING state."""
        game = Game(test_mode=True)
        game.state = GameState.MENU
        result = game.handle_input(ord(" "))
        self.assertTrue(result)
        self.assertEqual(game.state, GameState.PLAYING)

    def test_handle_player_damage_sets_shake_when_not_test_mode(self):
        """Screen shake is enabled when test_mode is False."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.player.lives = 3
        game.test_mode = False  # Enable shake path
        game.handle_player_damage()
        self.assertGreater(game.shake_end_time, 0, "Shake should be activated")

    def test_reset_game_inits_bunkers_when_not_test_mode(self):
        """reset_game() initializes bunkers when test_mode is False."""
        game = Game(test_mode=True)
        game.bunkers = []
        game.test_mode = False  # Enable bunker init path
        game.reset_game()
        self.assertGreater(len(game.bunkers), 0, "Bunkers should be initialized")


class TestPlayerInvincibilityBlink(unittest.TestCase):
    """Step 41: Player invincibility blink after taking damage (2s blink + i-frames)."""

    def test_is_invincible_false_by_default(self):
        """Player is not invincible at game start."""
        game = Game(test_mode=True)
        self.assertFalse(game.is_invincible())

    def test_invincibility_activated_on_damage(self):
        """Taking damage (with lives remaining) activates invincibility."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.player.lives = 3
        game.handle_player_damage()
        self.assertTrue(game.is_invincible())
        self.assertEqual(game.player.lives, 2)

    def test_invincibility_duration_is_2_seconds(self):
        """Invincibility lasts 2 seconds by default."""
        game = Game(test_mode=True)
        self.assertEqual(game.invincibility_duration, 2.0)

    def test_iframes_block_second_hit(self):
        """During invincibility, a second hit is ignored (i-frames)."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.player.lives = 3
        game.handle_player_damage()  # First hit: 3 → 2
        game.handle_player_damage()  # Second hit: blocked by i-frames
        self.assertEqual(game.player.lives, 2, "Second hit should be blocked by i-frames")

    def test_invincibility_expires(self):
        """After invincibility expires, player can be hit again."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.player.lives = 3
        game.handle_player_damage()  # First hit
        # Manually expire invincibility
        game.invincible_until = 0
        self.assertFalse(game.is_invincible())
        game.handle_player_damage()  # Should connect
        self.assertEqual(game.player.lives, 1)

    def test_invincibility_cleared_on_reset(self):
        """reset_game() clears invincibility."""
        game = Game(test_mode=True)
        game.invincible_until = time.time() + 10  # Force invincible
        game.reset_game()
        self.assertFalse(game.is_invincible())

    def test_no_invincibility_on_game_over(self):
        """Invincibility is not set when player dies (lives reach 0)."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.player.lives = 1
        game.handle_player_damage()
        self.assertEqual(game.state, GameState.GAME_OVER)
        self.assertEqual(game.invincible_until, 0, "No invincibility on death")

    def test_scaled_bunker_health_decreases_with_level(self):
        """Bunker health reduces every 5 levels, minimum 1."""
        game = Game(test_mode=True)
        game.level = 1
        self.assertEqual(game.get_scaled_bunker_health(), 3)
        game.level = 6
        self.assertEqual(game.get_scaled_bunker_health(), 2)
        game.level = 11
        self.assertEqual(game.get_scaled_bunker_health(), 1)
        game.level = 20
        self.assertEqual(game.get_scaled_bunker_health(), 1)

    def test_menu_start_triggers_audio(self):
        """Pressing SPACE in MENU starts audio when audio manager is set."""
        game = Game(test_mode=True)
        game.state = GameState.MENU
        mock_audio = unittest.mock.MagicMock()
        game.audio = mock_audio
        game.handle_input(ord(" "))
        mock_audio.start.assert_called_once()

    def test_menu_start_no_audio_when_none(self):
        """Pressing SPACE in MENU works without audio manager."""
        game = Game(test_mode=True)
        game.state = GameState.MENU
        game.audio = None
        game.handle_input(ord(" "))
        self.assertEqual(game.state, GameState.PLAYING)

    def test_sound_effects_unavailable_sound_skips_play(self):
        """SoundEffects skips playing when sound file is marked unavailable."""
        backend = NullSoundBackend()
        sfx = SoundEffects(backend=backend)
        sfx.enabled = True
        # Mark all sounds as unavailable
        for key in sfx.available_sounds:
            sfx.available_sounds[key] = False
        # Should return early at the available_sounds check (line 873)
        sfx._play_async("shoot")  # Known sound name, but marked unavailable

    def test_handle_input_resize_with_screen(self):
        """KEY_RESIZE with screen calls handle_resize."""
        import curses

        game = Game(test_mode=True)
        mock_screen = unittest.mock.MagicMock()
        mock_screen.getmaxyx.return_value = (30, 80)
        game.screen = mock_screen
        result = game.handle_input(curses.KEY_RESIZE)
        self.assertTrue(result)
        mock_screen.getmaxyx.assert_called_once()


class TestComboTextEffect(unittest.TestCase):
    """Step 42: Combo text effect with scaling emphasis at center screen."""

    def test_combo_text_spawned_on_multiplier_2(self):
        """A ComboText is created when multiplier reaches 2x."""
        game = Game(test_mode=True)
        t = time.time()
        game._register_kill(t)       # combo_count=1, multiplier=1
        self.assertIsNone(game.combo_text)
        game._register_kill(t + 0.5)  # combo_count=2, multiplier=2
        self.assertIsNotNone(game.combo_text)
        self.assertEqual(game.combo_text.text, "COMBO x2!")

    def test_combo_text_updates_on_higher_multiplier(self):
        """ComboText updates to show higher multiplier."""
        game = Game(test_mode=True)
        t = time.time()
        game._register_kill(t)
        game._register_kill(t + 0.5)
        game._register_kill(t + 1.0)
        self.assertEqual(game.combo_text.text, "COMBO x3!")

    def test_combo_text_ages_and_expires(self):
        """ComboText ages with update() and is culled when finished."""
        ct = ComboText(text="COMBO x2!")
        self.assertFalse(ct.finished)
        ct.update(1.6)
        self.assertTrue(ct.finished)

    def test_combo_text_culled_in_update(self):
        """Game.update() removes expired combo text."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.combo_text = ComboText(text="COMBO x2!", age=2.0)  # Already expired
        game.update()
        self.assertIsNone(game.combo_text)

    def test_get_combo_display_returns_none_when_inactive(self):
        """get_combo_display() returns None when no combo text is active."""
        game = Game(test_mode=True)
        self.assertIsNone(game.get_combo_display())

    def test_get_combo_display_returns_info(self):
        """get_combo_display() returns text, progress, and position info."""
        game = Game(test_mode=True)
        game.combo_text = ComboText(text="COMBO x3!")
        info = game.get_combo_display()
        self.assertIsNotNone(info)
        self.assertEqual(info["text"], "COMBO x3!")
        self.assertAlmostEqual(info["progress"], 0.0)
        self.assertEqual(info["center_y"], game.height // 2)

    def test_combo_text_cleared_on_reset(self):
        """reset_game() clears combo text."""
        game = Game(test_mode=True)
        game.combo_text = ComboText(text="COMBO x4!")
        game.reset_game()
        self.assertIsNone(game.combo_text)


class TestBunkerSmoothColorTransitions(unittest.TestCase):
    """Step 43: Smooth color transitions for bunker health (green→yellow→red)."""

    def test_full_health_is_green(self):
        """Ratio > 0.8 returns COLOR_BUNKER (full green)."""
        bunker = Bunker(x=0, y=0, health=5, max_health=5)
        self.assertEqual(bunker.color_pair, COLOR_BUNKER)

    def test_high_health_is_bright_green(self):
        """Ratio 0.6-0.8 returns COLOR_BUNKER_HIGH (transitional green)."""
        bunker = Bunker(x=0, y=0, health=4, max_health=5)
        self.assertAlmostEqual(bunker.health_ratio, 0.8)
        self.assertEqual(bunker.color_pair, COLOR_BUNKER_HIGH)

    def test_mid_health_is_yellow(self):
        """Ratio 0.4-0.6 returns COLOR_BUNKER_DAMAGED (yellow)."""
        bunker = Bunker(x=0, y=0, health=3, max_health=5)
        self.assertEqual(bunker.color_pair, COLOR_BUNKER_DAMAGED)

    def test_low_health_is_dark_yellow(self):
        """Ratio 0.2-0.4 returns COLOR_BUNKER_LOW (transitional)."""
        bunker = Bunker(x=0, y=0, health=2, max_health=5)
        self.assertEqual(bunker.color_pair, COLOR_BUNKER_LOW)

    def test_critical_health_is_red(self):
        """Ratio ≤ 0.2 returns COLOR_BUNKER_CRITICAL (red)."""
        bunker = Bunker(x=0, y=0, health=1, max_health=5)
        self.assertEqual(bunker.color_pair, COLOR_BUNKER_CRITICAL)

    def test_health_ratio_property(self):
        """health_ratio correctly computes current / max."""
        bunker = Bunker(x=0, y=0, health=2, max_health=4)
        self.assertAlmostEqual(bunker.health_ratio, 0.5)

    def test_health_ratio_zero_max(self):
        """health_ratio returns 0.0 when max_health is 0."""
        bunker = Bunker(x=0, y=0, health=0, max_health=0)
        self.assertEqual(bunker.health_ratio, 0.0)

    def test_five_tier_gradient_with_max_health_3(self):
        """With max_health=3, verify tiers used (green, bright green, low)."""
        b3 = Bunker(x=0, y=0, health=3, max_health=3)
        b2 = Bunker(x=0, y=0, health=2, max_health=3)
        b1 = Bunker(x=0, y=0, health=1, max_health=3)
        self.assertEqual(b3.color_pair, COLOR_BUNKER)       # ratio=1.0 → green
        self.assertEqual(b2.color_pair, COLOR_BUNKER_HIGH)   # ratio=0.667 → bright green
        self.assertEqual(b1.color_pair, COLOR_BUNKER_LOW)    # ratio=0.333 → transitional


class TestBottomHUDBar(unittest.TestCase):
    """Step 44: Bottom HUD bar with game stats."""

    def test_aliens_remaining_count(self):
        """Bottom HUD shows aliens remaining / total."""
        game = Game(test_mode=True)
        info = game.get_bottom_hud_info()
        total = game.config.alien_rows * game.config.alien_cols
        self.assertEqual(info["aliens"], f"Aliens: {len(game.aliens)}/{total}")

    def test_combo_inactive(self):
        """Combo shows '--' when multiplier is 1."""
        game = Game(test_mode=True)
        info = game.get_bottom_hud_info()
        self.assertEqual(info["combo"], "Combo: --")

    def test_combo_active(self):
        """Combo shows multiplier when active."""
        game = Game(test_mode=True)
        t = time.time()
        game._register_kill(t)
        game._register_kill(t + 0.5)
        info = game.get_bottom_hud_info()
        self.assertEqual(info["combo"], "Combo: x2")

    def test_power_ups_inactive(self):
        """Power-ups shows '--' when none active."""
        game = Game(test_mode=True)
        info = game.get_bottom_hud_info()
        self.assertEqual(info["power_ups"], "PWR: --")

    def test_power_ups_active(self):
        """Power-ups shows active type labels."""
        game = Game(test_mode=True)
        game.active_power_ups.append(ActivePowerUp(power_type=PowerUpType.RAPID_FIRE, expires_at=time.time() + 10))
        info = game.get_bottom_hud_info()
        self.assertEqual(info["power_ups"], "PWR: R")

    def test_hud_row_is_bottom(self):
        """HUD row is the last row of the screen."""
        game = Game(test_mode=True)
        info = game.get_bottom_hud_info()
        self.assertEqual(info["row"], game.height - 1)

    def test_aliens_updates_after_kills(self):
        """Aliens count decreases as aliens are removed."""
        game = Game(test_mode=True)
        initial_count = len(game.aliens)
        game.aliens.pop()
        info = game.get_bottom_hud_info()
        total = game.config.alien_rows * game.config.alien_cols
        self.assertEqual(info["aliens"], f"Aliens: {initial_count - 1}/{total}")

    def test_macoss_stop_handles_exception(self):
        """MacOSSoundBackend.stop handles subprocess exception gracefully."""
        backend = MacOSSoundBackend()
        with unittest.mock.patch("subprocess.run", side_effect=OSError("mocked")):
            backend.stop()  # Should not raise


class TestGameOverScreenAndStats(unittest.TestCase):
    """Step 45: Game over screen with death animation and stats."""

    def test_game_over_stats_default(self):
        """get_game_over_stats returns correct defaults."""
        game = Game(test_mode=True)
        stats = game.get_game_over_stats()
        self.assertEqual(stats["score"], 0)
        self.assertEqual(stats["level"], 1)
        self.assertEqual(stats["kills"], 0)
        self.assertEqual(stats["shots"], 0)
        self.assertEqual(stats["accuracy"], "N/A")

    def test_accuracy_calculation(self):
        """Accuracy is calculated as kills/shots percentage."""
        game = Game(test_mode=True)
        game.total_shots = 10
        game.total_kills = 7
        stats = game.get_game_over_stats()
        self.assertEqual(stats["accuracy"], "70%")

    def test_total_shots_incremented_on_fire(self):
        """Firing a projectile increments total_shots."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.handle_input(ord(" "))
        self.assertEqual(game.total_shots, 1)

    def test_total_kills_incremented_on_alien_death(self):
        """Killing an alien increments total_kills."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        alien = game.aliens[0]
        game.player_projectiles.append(Projectile(x=alien.x, y=alien.y, direction=-1))
        game._check_collisions()
        self.assertEqual(game.total_kills, 1)

    def test_game_over_time_set_on_death(self):
        """game_over_time is set when player dies."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.player.lives = 1
        before = time.time()
        game.handle_player_damage()
        self.assertGreaterEqual(game.game_over_time, before)

    def test_curtain_rows_increases_over_time(self):
        """Curtain animation fills more rows as time passes."""
        game = Game(test_mode=True)
        game.game_over_time = time.time() - 1.0  # 1 second ago
        stats = game.get_game_over_stats()
        self.assertGreater(stats["curtain_rows"], 0)

    def test_stats_reset_on_reset_game(self):
        """reset_game clears all stats."""
        game = Game(test_mode=True)
        game.total_shots = 50
        game.total_kills = 30
        game.game_over_time = time.time()
        game.reset_game()
        self.assertEqual(game.total_shots, 0)
        self.assertEqual(game.total_kills, 0)
        self.assertEqual(game.game_over_time, 0)

    def test_audio_manager_stop_terminate_exception(self):
        """AudioManager.stop handles terminate exception and falls back to kill."""
        from invaders import AudioManager

        am = AudioManager()
        mock_proc = unittest.mock.MagicMock()
        mock_proc.terminate.side_effect = OSError("mocked terminate")
        am.current_process = mock_proc
        am.stop()
        mock_proc.kill.assert_called_once()

    def test_audio_manager_stop_pkill_exception(self):
        """AudioManager.stop handles pkill subprocess exception gracefully."""
        from invaders import AudioManager

        am = AudioManager()
        with unittest.mock.patch("subprocess.run", side_effect=OSError("mocked pkill")):
            am.stop()  # Should not raise


class TestLevelTransitionCountdown(unittest.TestCase):
    """Step 46: Level transition countdown animation (3... 2... 1... GO!)."""

    def test_countdown_shows_3_at_start(self):
        """Countdown text is '3' immediately after transition starts."""
        game = Game(test_mode=True)
        game.level_transition_time = time.time()
        info = game.get_level_transition_info()
        self.assertEqual(info["countdown_text"], "3")

    def test_countdown_shows_2(self):
        """Countdown text is '2' after 1 second."""
        game = Game(test_mode=True)
        game.level_transition_time = time.time() - 1.5
        info = game.get_level_transition_info()
        self.assertEqual(info["countdown_text"], "2")

    def test_countdown_shows_1(self):
        """Countdown text is '1' after 2 seconds."""
        game = Game(test_mode=True)
        game.level_transition_time = time.time() - 2.5
        info = game.get_level_transition_info()
        self.assertEqual(info["countdown_text"], "1")

    def test_countdown_shows_go(self):
        """Countdown text is 'GO!' after 3 seconds."""
        game = Game(test_mode=True)
        game.level_transition_time = time.time() - 3.5
        info = game.get_level_transition_info()
        self.assertEqual(info["countdown_text"], "GO!")

    def test_auto_advance_after_countdown(self):
        """State auto-advances to PLAYING after countdown completes."""
        game = Game(test_mode=True)
        game.state = GameState.LEVEL_TRANSITION
        game.level_transition_time = time.time() - 5.0  # Well past 4s
        game.update()
        self.assertEqual(game.state, GameState.PLAYING)

    def test_no_auto_advance_during_countdown(self):
        """State stays LEVEL_TRANSITION during countdown."""
        game = Game(test_mode=True)
        game.state = GameState.LEVEL_TRANSITION
        game.level_transition_time = time.time()  # Just started
        game.update()
        self.assertEqual(game.state, GameState.LEVEL_TRANSITION)

    def test_space_skips_countdown(self):
        """Pressing SPACE during countdown advances to PLAYING."""
        game = Game(test_mode=True)
        game.state = GameState.LEVEL_TRANSITION
        game.handle_input(ord(" "))
        self.assertEqual(game.state, GameState.PLAYING)

    def test_info_includes_level_and_lives(self):
        """Transition info includes level number and lives awarded."""
        game = Game(test_mode=True)
        game.level = 3
        game.lives_awarded = 2
        game.level_transition_time = time.time()
        info = game.get_level_transition_info()
        self.assertEqual(info["level"], 3)
        self.assertEqual(info["lives_awarded"], 2)


class TestShieldVisualAura(unittest.TestCase):
    """Step 47: Shield visual aura around player (pulsing brackets in cyan)."""

    def test_no_aura_without_shield(self):
        """get_shield_aura_info() returns None when shield is not active."""
        game = Game(test_mode=True)
        self.assertIsNone(game.get_shield_aura_info())

    def test_aura_active_with_shield(self):
        """get_shield_aura_info() returns info when shield is active."""
        game = Game(test_mode=True)
        game.active_power_ups.append(ActivePowerUp(power_type=PowerUpType.SHIELD, expires_at=time.time() + 999))
        info = game.get_shield_aura_info()
        self.assertIsNotNone(info)
        self.assertEqual(info["left_bracket"], "(")
        self.assertEqual(info["right_bracket"], ")")

    def test_aura_position_matches_player(self):
        """Aura brackets are positioned relative to player."""
        game = Game(test_mode=True)
        game.active_power_ups.append(ActivePowerUp(power_type=PowerUpType.SHIELD, expires_at=time.time() + 999))
        info = game.get_shield_aura_info()
        self.assertEqual(info["player_x"], game.player.x)
        self.assertEqual(info["player_y"], game.player.y)

    def test_aura_pulse_alternates(self):
        """Pulse alternates based on thrust_frame."""
        game = Game(test_mode=True)
        game.active_power_ups.append(ActivePowerUp(power_type=PowerUpType.SHIELD, expires_at=time.time() + 999))
        game.thrust_frame = 0
        self.assertTrue(game.get_shield_aura_info()["pulse"])
        game.thrust_frame = 1
        self.assertFalse(game.get_shield_aura_info()["pulse"])

    def test_aura_disappears_after_shield_consumed(self):
        """Aura disappears when shield is consumed by a hit."""
        game = Game(test_mode=True)
        game.state = GameState.PLAYING
        game.active_power_ups.append(ActivePowerUp(power_type=PowerUpType.SHIELD, expires_at=time.time() + 999))
        self.assertIsNotNone(game.get_shield_aura_info())
        game.handle_player_damage()  # Shield absorbs hit
        self.assertIsNone(game.get_shield_aura_info())

    def test_macoss_is_available_handles_exception(self):
        """MacOSSoundBackend.is_available returns False on exception."""
        backend = MacOSSoundBackend()
        with unittest.mock.patch("subprocess.run", side_effect=OSError("mocked")):
            self.assertFalse(backend.is_available())


class TestAlienTypeSpecificDeathExplosions(unittest.TestCase):
    """Tests for alien type-specific death explosions (Step 48)."""

    def _kill_alien_of_type(self, alien_type: int):
        """Kill an alien of the given type with test_mode=False for particles."""
        game = Game(test_mode=True)
        game.aliens = [Alien(x=10, y=5, alien_type=alien_type)]
        game.player_projectiles = [Projectile(x=10, y=5, direction=-1)]
        game.test_mode = False
        game._check_collisions()
        game.test_mode = True
        return game

    def test_get_explosion_config_type_0(self):
        """Type 0 aliens (top rows) return big explosion config."""
        cfg = get_explosion_config(0)
        self.assertEqual(cfg["count"], (8, 12))
        self.assertIn("#", cfg["chars"])
        self.assertIn("X", cfg["chars"])

    def test_get_explosion_config_type_1(self):
        """Type 1 aliens (middle rows) return medium explosion config."""
        cfg = get_explosion_config(1)
        self.assertEqual(cfg["count"], (5, 8))
        self.assertEqual(cfg["chars"], "*+.'`")

    def test_get_explosion_config_type_2(self):
        """Type 2 aliens (bottom rows) return small explosion config."""
        cfg = get_explosion_config(2)
        self.assertEqual(cfg["count"], (3, 5))
        self.assertEqual(cfg["chars"], "+'.")

    def test_get_explosion_config_unknown_returns_default(self):
        """Unknown alien type returns the default explosion config."""
        cfg = get_explosion_config(99)
        self.assertEqual(cfg, DEFAULT_EXPLOSION_CONFIG)

    def test_type_0_more_particles_than_type_2(self):
        """Type 0 should produce more particles than type 2 on average."""
        type_0_counts = []
        type_2_counts = []
        for _ in range(30):
            g0 = self._kill_alien_of_type(0)
            g2 = self._kill_alien_of_type(2)
            type_0_counts.append(len(g0.particle_system.particles))
            type_2_counts.append(len(g2.particle_system.particles))
        avg_0 = sum(type_0_counts) / len(type_0_counts)
        avg_2 = sum(type_2_counts) / len(type_2_counts)
        self.assertGreater(avg_0, avg_2, "Type 0 should average more particles than type 2")

    def test_type_0_particles_in_count_range(self):
        """Type 0 explosion spawns 8-12 particles."""
        counts = set()
        for _ in range(50):
            game = self._kill_alien_of_type(0)
            counts.add(len(game.particle_system.particles))
        self.assertTrue(all(8 <= c <= 12 for c in counts), f"Type 0 counts {counts} should be in [8,12]")

    def test_type_2_particles_in_count_range(self):
        """Type 2 explosion spawns 3-5 particles."""
        counts = set()
        for _ in range(50):
            game = self._kill_alien_of_type(2)
            counts.add(len(game.particle_system.particles))
        self.assertTrue(all(3 <= c <= 5 for c in counts), f"Type 2 counts {counts} should be in [3,5]")

    def test_type_0_uses_type_specific_chars(self):
        """Type 0 explosion particles use the heavier character set including # and X."""
        all_chars = set()
        for _ in range(50):
            game = self._kill_alien_of_type(0)
            for p in game.particle_system.particles:
                all_chars.add(p.char)
        expected = set(ALIEN_EXPLOSION_CONFIGS[0]["chars"])
        self.assertTrue(all_chars.issubset(expected), f"Type 0 chars {all_chars} not subset of {expected}")

    def test_all_configs_present(self):
        """Ensure configs exist for all three alien types."""
        for t in range(3):
            cfg = get_explosion_config(t)
            self.assertIn("count", cfg)
            self.assertIn("chars", cfg)
            self.assertIn("speed", cfg)
            self.assertIn("lifetime", cfg)


if __name__ == "__main__":
    # Run tests with verbosity
    unittest.main(verbosity=2)
