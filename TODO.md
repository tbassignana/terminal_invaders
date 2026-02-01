# Terminal Invaders - Improvement TODO

> This file is used by the Ralph Loop. Each step is completed one at a time.
> Mark completed steps with `[x]`. The next `[ ]` item is the current task.

## Architecture & Code Quality

- [x] **Step 1:** Extract configuration into a Config dataclass — remove global state mutation
- [x] **Step 2:** Replace dict-based projectiles with the Projectile dataclass
- [x] **Step 3:** Add a logging framework with `--debug` CLI flag
- [x] **Step 4:** Create an abstract SoundBackend interface for cross-platform audio
- [x] **Step 5:** Add a ScoreManager with high-score persistence to `~/.invaders_scores.json`
- [x] **Step 6:** Implement an EventBus for decoupled game events
- [ ] **Step 7:** Add a CLI argument parser with game options and difficulty presets

## Gameplay Features

- [ ] **Step 8:** Add a Mystery Ship (UFO) that crosses the top of the screen
- [ ] **Step 9:** Add a pause system with `PAUSED` game state
- [ ] **Step 10:** Implement power-ups dropped by aliens (rapid fire, shield, wide shot)
- [ ] **Step 11:** Add combo scoring system with time-windowed multiplier
- [ ] **Step 12:** Implement difficulty scaling per level (speed, rows, bunker health)
- [ ] **Step 13:** Add screen shake effect on player death

## Testing & Quality

- [ ] **Step 14:** Add collision detection edge case tests (8+ new tests)
- [ ] **Step 15:** Add integration tests for the game update loop (5+ tests)
- [ ] **Step 16:** Add property-based tests with Hypothesis
- [ ] **Step 17:** Add test coverage reporting and enforce 80%+ coverage

## Performance & Robustness

- [ ] **Step 18:** Optimize collision detection with spatial partitioning
- [ ] **Step 19:** Add terminal resize handling
- [ ] **Step 20:** Add frame time smoothing and performance metrics with FPS counter

## Infrastructure & DevOps

- [ ] **Step 21:** Add pyproject.toml with project metadata and entry point
- [ ] **Step 22:** Add GitHub Actions CI pipeline
- [ ] **Step 23:** Add a Makefile for common development tasks
- [ ] **Step 24:** Add pre-commit hooks and ruff linting configuration
- [ ] **Step 25:** Add CHANGELOG.md, version management, and `--version` flag
