# Terminal Invaders - Ralph Loop Improvement Prompt

## Instructions

You are improving the Terminal Invaders project — a Python curses-based Space Invaders clone. Follow this loop precisely for each step.

### Loop Protocol

1. **Read this file** (`PROMPT.md`) to understand the process.
2. **Read `TODO.md`**. If it does not exist, generate it using the 25 improvements listed below.
3. **Identify the next uncompleted step** in `TODO.md` (the first item marked `[ ]`).
4. **Complete ONLY that step** by writing the necessary code changes.
5. **Write tests** that validate the code you just wrote. Add them to `test_invaders.py` (or a new test file if appropriate). Each step should have at least 2-3 new tests.
6. **Run the new tests** with `python3 -m pytest test_invaders.py -v` and debug until they pass.
7. **Run ALL cumulative tests** with `python3 -m pytest -v` and debug until ALL tests pass (not just the new ones).
8. **Mark the step as complete** in `TODO.md` by changing `[ ]` to `[x]`.
9. **Stage all changed files, commit, and push**:
   ```bash
   git add -A
   git commit -m "Step N: <description of improvement>"
   git push origin main
   ```
10. **Condense the session** and resume the loop for the next step.

### Critical Rules

- **One step per loop iteration.** Do not combine steps.
- **All tests must pass before committing.** Never commit with failing tests.
- **Push after every step.** Each improvement is an atomic, deployable commit.
- **Preserve backward compatibility** with existing game functionality. The game must remain playable after each step.
- **Do not modify test assertions from previous steps** unless a refactor explicitly requires it (and document why).
- **test_mode=True** must continue to work for all unit tests (no curses dependency in tests).

---

## The 25 Improvements

### Architecture & Code Quality

1. **Extract configuration into a Config dataclass**
   - Move all constants (FPS, speeds, probabilities, dimensions, colors) into a frozen `GameConfig` dataclass.
   - The `Game` class should accept a `config` parameter instead of relying on module-level globals.
   - Remove the `global ALIEN_MOVE_INTERVAL` mutation in `_next_level()` — store mutable speed state on the Game instance.
   - Tests: Verify default config values, verify custom config overrides, verify no global mutation.

2. **Replace dict-based projectiles with Projectile dataclass**
   - `player_projectiles` and `alien_projectiles` currently use `List[Dict]` with `{'x': ..., 'y': ...}`.
   - Refactor to use the existing `Projectile` dataclass (which is defined but never used).
   - Update all code that creates, reads, or modifies projectiles.
   - Tests: Verify projectile creation, movement, removal, and collision detection still works.

3. **Add a logging framework**
   - Add Python `logging` module with configurable log levels.
   - Replace all silent `except: pass` blocks with proper logging (at DEBUG or WARNING level).
   - Add a `--debug` CLI flag that enables DEBUG-level logging to a file (`invaders.log`).
   - Tests: Verify logger initialization, verify debug mode flag parsing, verify log output on simulated errors.

4. **Create an abstract SoundBackend interface for cross-platform audio**
   - Define an `AbstractSoundBackend` with methods: `play()`, `stop()`, `is_available()`.
   - Implement `MacOSSoundBackend` (current afplay logic) and `NullSoundBackend` (silent, for unsupported platforms/testing).
   - `SoundEffects` and `AudioManager` should use the backend interface.
   - Tests: Verify NullSoundBackend works silently, verify backend selection logic, verify MacOS backend path resolution.

5. **Add a ScoreManager with high-score persistence**
   - Create a `ScoreManager` class that tracks current score, high score, and score history.
   - Persist high scores to `~/.invaders_scores.json` (top 10 scores with date/level).
   - Display high score on the MENU and GAME_OVER screens.
   - Tests: Verify score tracking, file persistence (use tmp dir), high score ranking, score reset behavior.

6. **Implement an EventBus for decoupled game events**
   - Create a simple publish/subscribe event system (`EventBus` class).
   - Define events: `ALIEN_KILLED`, `PLAYER_HIT`, `LEVEL_COMPLETE`, `GAME_OVER`, `SHOT_FIRED`.
   - Refactor `SoundEffects` to subscribe to events instead of being called directly.
   - Refactor `ScoreManager` to subscribe to `ALIEN_KILLED` events.
   - Tests: Verify event subscription, event firing, event handler execution, multi-subscriber support.

7. **Add a CLI argument parser with game options**
   - Use `argparse` to add: `--no-sound`, `--no-music`, `--debug`, `--difficulty <easy|normal|hard>`, `--fps <N>`.
   - Difficulty presets adjust: alien speed, fire probability, player lives, and projectile speed.
   - Wire CLI args into `GameConfig`.
   - Tests: Verify argument parsing, verify difficulty preset values, verify config integration.

### Gameplay Features

8. **Add a Mystery Ship (UFO) that crosses the screen**
   - Create a `MysteryShip` dataclass with position, speed, point value (50-300 random), and active state.
   - The UFO spawns randomly (average once per 25 seconds) and moves horizontally across the top of the screen.
   - Player can shoot it for bonus points. Display the point value briefly when hit.
   - Tests: Verify spawn timing, movement, collision with player projectile, point award, despawn at edge.

9. **Add a pause system**
   - Press `P` or `Escape` to toggle pause.
   - Add a `PAUSED` state to `GameState` enum.
   - Display "PAUSED" overlay text. Game timer should not advance while paused.
   - Audio should pause/resume with the game.
   - Tests: Verify pause toggle, verify game state doesn't update while paused, verify unpause resumes correctly.

10. **Implement power-ups dropped by aliens**
    - When certain aliens die, they have a 10% chance of dropping a power-up.
    - Power-up types: `RAPID_FIRE` (faster shooting for 5s), `SHIELD` (absorb one hit), `WIDE_SHOT` (triple-width projectile for 5s).
    - Power-ups fall slowly and must be caught by the player.
    - Tests: Verify power-up drop chance, falling movement, player collection, effect application, effect expiration.

11. **Add combo scoring system**
    - Track consecutive kills within a time window (2 seconds).
    - Combo multiplier: 2 kills = x2, 3 kills = x3, up to x5 max.
    - Display combo counter on screen when active.
    - Tests: Verify combo tracking, multiplier calculation, timeout reset, max combo cap, score integration.

12. **Implement difficulty scaling per level**
    - Each level increases: alien speed (+10%), fire probability (+5%), projectile speed (+5%).
    - Every 3 levels, add an extra row of aliens (up to max 8 rows).
    - Every 5 levels, reduce bunker health by 1 starting point.
    - Tests: Verify scaling formulas, verify alien row additions, verify bunker health adjustments, verify cap limits.

13. **Add screen shake effect on player death**
    - When the player is hit, apply a brief (0.3s) screen shake by offsetting render coordinates.
    - Implement as a render modifier that adds random ±1 pixel offsets to all draw calls.
    - Smooth decay over the shake duration.
    - Tests: Verify shake activation on damage, verify shake offset values, verify shake expires, verify no shake in test_mode.

### Testing & Quality

14. **Add collision detection edge case tests**
    - Test projectile hitting exact alien boundary.
    - Test projectile passing between two adjacent aliens.
    - Test simultaneous player and alien projectile at same position.
    - Test bunker hit at health=1 (destruction).
    - Test projectile at screen boundaries (y=0, y=max).
    - At least 8 new focused test cases.

15. **Add integration tests for the game update loop**
    - Test a full game sequence: start → fire → kill alien → score updates.
    - Test level transition: kill all aliens → verify level increments and aliens respawn.
    - Test game over flow: deplete lives → verify game over state.
    - Test reset after game over: verify clean state.
    - At least 5 integration tests that exercise `update()` method.

16. **Add property-based tests with Hypothesis**
    - Install `hypothesis` library.
    - Test that alien fire probability is always between 0 and MAX_FIRE_PROBABILITY for any alien count.
    - Test that player position stays within bounds after any sequence of movements.
    - Test that score is always non-negative.
    - Test that bunker health is always in range [0, 3].
    - At least 4 property-based tests.

17. **Add test coverage reporting and enforce minimum coverage**
    - Install `pytest-cov`.
    - Add a `pytest.ini` or `pyproject.toml` with coverage configuration.
    - Run coverage report and identify untested code paths.
    - Add tests to reach at least 80% line coverage on `invaders.py`.
    - Tests: The coverage report itself validates this step.

### Performance & Robustness

18. **Optimize collision detection with spatial partitioning**
    - Current collision detection is O(projectiles × entities) — quadratic.
    - Implement a simple grid-based spatial index that divides the screen into cells.
    - Only check collisions between entities in the same or adjacent cells.
    - Tests: Verify spatial index cell assignment, verify collision results match brute-force, benchmark with large entity counts.

19. **Add terminal resize handling**
    - Detect `curses.KEY_RESIZE` events during gameplay.
    - Gracefully handle resize: recalculate positions, re-render, and show warning if below minimum size.
    - Prevent crashes from writing outside resized terminal bounds.
    - Tests: Verify resize detection logic, verify minimum size check, verify position recalculation.

20. **Add frame time smoothing and performance metrics**
    - Track frame times over a rolling window (last 60 frames).
    - Calculate average FPS, min/max frame time, and frame time variance.
    - Display FPS counter in debug mode (toggled with F1 or `--show-fps` flag).
    - Tests: Verify FPS calculation accuracy, verify rolling window behavior, verify debug display toggle.

### Infrastructure & DevOps

21. **Add pyproject.toml with project metadata and tool configuration**
    - Create `pyproject.toml` with: project name, version, description, Python version requirement, optional dependencies (hypothesis, pytest-cov).
    - Configure pytest, coverage, and any linting tools.
    - Add `[project.scripts]` entry point for `invaders` command.
    - Tests: Verify the project can be installed with `pip install -e .` and the entry point works.

22. **Add GitHub Actions CI pipeline**
    - Create `.github/workflows/ci.yml` that runs on push and PR to main.
    - Steps: checkout, setup Python 3.12, install dependencies, run pytest with coverage, upload coverage report.
    - Add a matrix strategy for Python 3.10, 3.11, 3.12.
    - Tests: Validate the YAML is well-formed. Validate that `python3 -m pytest` passes (the CI will do the rest).

23. **Add a Makefile for common development tasks**
    - Targets: `make test`, `make test-coverage`, `make lint`, `make run`, `make install`, `make clean`.
    - `make lint` should run `flake8` or `ruff` on the codebase.
    - `make test-coverage` should produce an HTML coverage report.
    - Tests: Verify Makefile targets exist and `make test` executes successfully.

24. **Add pre-commit hooks for code quality**
    - Create `.pre-commit-config.yaml` with hooks for: trailing whitespace, YAML check, Python AST check, ruff (linting + formatting).
    - Add `ruff.toml` or `[tool.ruff]` in pyproject.toml for linting configuration.
    - Fix any linting issues found by ruff across the codebase.
    - Tests: Verify ruff passes cleanly on all Python files.

25. **Add a CHANGELOG.md and version management**
    - Create `CHANGELOG.md` following Keep a Changelog format.
    - Add a `__version__` attribute to `invaders.py`.
    - Document all 24 previous improvements as entries in the changelog.
    - Add version display to the game menu screen and `--version` CLI flag.
    - Tests: Verify `__version__` exists and matches pyproject.toml, verify `--version` flag output.

---

## Completion Promise

The loop is complete when all 25 items in `TODO.md` are marked `[x]`. At that point, the Terminal Invaders project will have:

- Clean, configurable architecture with no global state mutation
- Rich gameplay with UFOs, power-ups, combos, and difficulty scaling
- Comprehensive test suite with 80%+ coverage
- CI/CD pipeline with automated testing
- Professional project tooling (Makefile, pre-commit, pyproject.toml)
- Persistent high scores and polished UX

Max loops: 25
