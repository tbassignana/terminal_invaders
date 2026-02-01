# Changelog

All notable changes to Terminal Invaders are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-01-31

### Added

- **Architecture & Code Quality**
  - Step 1: Extracted configuration into a frozen `GameConfig` dataclass, removing global state mutation
  - Step 2: Replaced dict-based projectiles with the `Projectile` dataclass
  - Step 3: Added logging framework with `--debug` CLI flag for file-based debug logging
  - Step 4: Created abstract `SoundBackend` interface with `MacOSSoundBackend` and `NullSoundBackend`
  - Step 5: Added `ScoreManager` with high-score persistence to `~/.invaders_scores.json`
  - Step 6: Implemented `EventBus` for decoupled game events (publish/subscribe pattern)
  - Step 7: Added CLI argument parser with `--no-sound`, `--no-music`, `--debug`, `--difficulty`, `--fps`

- **Gameplay Features**
  - Step 8: Mystery Ship (UFO) that crosses the top of the screen for bonus points
  - Step 9: Pause system with `PAUSED` game state (P or Escape to toggle)
  - Step 10: Power-ups dropped by aliens (rapid fire, shield, wide shot)
  - Step 11: Combo scoring system with time-windowed multiplier (up to 5x)
  - Step 12: Difficulty scaling per level (speed, rows, bunker health)
  - Step 13: Screen shake effect on player death with smooth decay

- **Testing & Quality**
  - Step 14: Collision detection edge case tests (8 new tests)
  - Step 15: Integration tests for the game update loop (6 tests)
  - Step 16: Property-based tests with Hypothesis (4 tests)
  - Step 17: Test coverage reporting with pytest-cov, enforcing 80%+ coverage (81%)

- **Performance & Robustness**
  - Step 18: Spatial partitioning grid for optimized collision detection
  - Step 19: Terminal resize handling with position clamping and minimum size warning
  - Step 20: Frame time smoothing with rolling window, FPS counter (F1 or `--show-fps`)

- **Infrastructure & DevOps**
  - Step 21: `pyproject.toml` with project metadata, entry point, and tool configuration
  - Step 22: GitHub Actions CI pipeline with Python 3.10/3.11/3.12 matrix
  - Step 23: Makefile with test, test-coverage, lint, run, install, and clean targets
  - Step 24: Pre-commit hooks and ruff linting/formatting configuration
  - Step 25: CHANGELOG.md, `__version__` attribute, and `--version` CLI flag
