# Terminal Invaders - Improvement TODO

> This file is used by the Ralph Loop. Each step is completed one at a time.
> Mark completed steps with `[x]`. The next `[ ]` item is the current task.

## Phase 1: Architecture & Code Quality (COMPLETED)

- [x] **Step 1:** Extract configuration into a Config dataclass — remove global state mutation
- [x] **Step 2:** Replace dict-based projectiles with the Projectile dataclass
- [x] **Step 3:** Add a logging framework with `--debug` CLI flag
- [x] **Step 4:** Create an abstract SoundBackend interface for cross-platform audio
- [x] **Step 5:** Add a ScoreManager with high-score persistence to `~/.invaders_scores.json`
- [x] **Step 6:** Implement an EventBus for decoupled game events
- [x] **Step 7:** Add a CLI argument parser with game options and difficulty presets

## Phase 1: Gameplay Features (COMPLETED)

- [x] **Step 8:** Add a Mystery Ship (UFO) that crosses the top of the screen
- [x] **Step 9:** Add a pause system with `PAUSED` game state
- [x] **Step 10:** Implement power-ups dropped by aliens (rapid fire, shield, wide shot)
- [x] **Step 11:** Add combo scoring system with time-windowed multiplier
- [x] **Step 12:** Implement difficulty scaling per level (speed, rows, bunker health)
- [x] **Step 13:** Add screen shake effect on player death

## Phase 1: Testing & Quality (COMPLETED)

- [x] **Step 14:** Add collision detection edge case tests (8+ new tests)
- [x] **Step 15:** Add integration tests for the game update loop (5+ tests)
- [x] **Step 16:** Add property-based tests with Hypothesis
- [x] **Step 17:** Add test coverage reporting and enforce 80%+ coverage

## Phase 1: Performance & Robustness (COMPLETED)

- [x] **Step 18:** Optimize collision detection with spatial partitioning
- [x] **Step 19:** Add terminal resize handling
- [x] **Step 20:** Add frame time smoothing and performance metrics with FPS counter

## Phase 1: Infrastructure & DevOps (COMPLETED)

- [x] **Step 21:** Add pyproject.toml with project metadata and entry point
- [x] **Step 22:** Add GitHub Actions CI pipeline
- [x] **Step 23:** Add a Makefile for common development tasks
- [x] **Step 24:** Add pre-commit hooks and ruff linting configuration
- [x] **Step 25:** Add CHANGELOG.md, version management, and `--version` flag

---

## Phase 2: Visual Impact Improvements

### Particle & Animation Systems

- [x] **Step 26:** Add a particle system engine (Particle dataclass + ParticleSystem manager)
- [x] **Step 27:** Add explosion particles on alien death (5-8 particles, directional spread)
- [x] **Step 28:** Add trail effect on player projectiles (`|` → `:` → `.` fading trail)
- [x] **Step 29:** Add alien death animation (brief `#` → `*` → `+` flash before removal)
- [x] **Step 30:** Add bunker damage visual effects (flash, color shift green→yellow→red, debris particles)
- [x] **Step 31:** Add player thrust/engine animation (flickering char below ship, directional tilt)

### Environment & Atmosphere

- [x] **Step 32:** Add starfield background (dim scrolling parallax stars)
- [x] **Step 33:** Add color-per-alien-type rendering (cyan/magenta/yellow + bold in frenzy mode)
- [ ] **Step 34:** Add HUD separator line and decorative border (box-drawing characters)
- [ ] **Step 35:** Add animated title screen with ASCII art (large block letters + color cycling)

### In-Game Visual Feedback

- [ ] **Step 36:** Add score popup text on alien kill ("+30" floats upward, combo multiplier shown)
- [ ] **Step 37:** Add mystery ship visual effects (blink, wider sprite `=<UFO>=`, large explosion)
- [ ] **Step 38:** Add power-up visual indicators (blink, distinct chars R/S/W, HUD timer `[R 3s]`)
- [ ] **Step 39:** Add screen flash effect on level complete (green flash 0.15s)
- [ ] **Step 40:** Add wave/ripple effect on alien formation descent (`~` line fading bold→dim)
- [ ] **Step 41:** Add player invincibility blink after taking damage (2s blink + i-frames)
- [ ] **Step 42:** Add combo text effect with scaling emphasis ("COMBO x3!" center display)
- [ ] **Step 43:** Add smooth color transitions for bunker health (green→yellow→red color pairs)

### HUD & Screen Polish

- [ ] **Step 44:** Add bottom HUD bar with game stats (aliens remaining, power-ups, combo)
- [ ] **Step 45:** Add game over screen with death animation and stats (curtain fall + stats)
- [ ] **Step 46:** Add level transition countdown animation (3... 2... 1... GO! auto-advance)
- [ ] **Step 47:** Add shield visual aura around player (pulsing `(^A^)` brackets in cyan)
- [ ] **Step 48:** Add alien type-specific death explosions (varying particle count/chars per type)
- [ ] **Step 49:** Add progressive background color shift as aliens descend (black→dim red→deep red)
- [ ] **Step 50:** Add victory celebration animation when clearing a level (particle burst + "WAVE CLEAR!")

---

## Phase 3: Gameplay & Menu Improvements

### Main Menu System

- [ ] **Step 51:** Add main menu state machine with multiple screens (Play, High Scores, Options, Controls, Credits, Quit)
- [ ] **Step 52:** Add high scores display screen (top 10 table with rank, score, level, date)
- [ ] **Step 53:** Add options/settings menu screen (sound, music, difficulty, FPS toggles)
- [ ] **Step 54:** Add controls help screen (keybindings table, alien point values)
- [ ] **Step 55:** Add credits screen (scrolling text animation with version and repo link)
- [ ] **Step 56:** Add menu cursor animation and selection effects (bobbing `>`, flash on select)

### Gameplay Enhancements

- [ ] **Step 57:** Add alien formation patterns per level (rectangle, V-shape, diamond, wave)
- [ ] **Step 58:** Add boss alien every 5 levels (large sprite, multi-HP, spread shots, big score)
- [ ] **Step 59:** Add wave-based sub-levels (3 waves per level with escalating difficulty)
- [ ] **Step 60:** Add player weapon upgrade system (5 levels: single→dual→triple→quad + speed)
- [ ] **Step 61:** Add aliens with special behaviors (zigzag movement, shielded 2-HP aliens)
- [ ] **Step 62:** Add collectible coins/gems dropped by aliens (bronze/silver/gold with timed expiry)
- [ ] **Step 63:** Add progressive alien speed increase within a level (original SI acceleration)
- [ ] **Step 64:** Add "last stand" mechanic (buffs at 1 life: speed boost, double fire slots)
- [ ] **Step 65:** Add alien projectile variety (normal `!`, fast `v`, heavy `#` bunker-breaker)
- [ ] **Step 66:** Add screen-clearing bomb power-up (B pickup, destroys all alien projectiles)
- [ ] **Step 67:** Add alien dive-bomb attack (break formation, diagonal dive toward player)
- [ ] **Step 68:** Add score threshold milestones with bonus life rewards (1K, 2.5K, 5K, 10K, 25K, 50K)
- [ ] **Step 69:** Add "bullet time" slow motion power-up (50% alien speed for 4s, blue tint)
- [ ] **Step 70:** Add persistent statistics tracking (lifetime stats in `~/.invaders_stats.json`)
- [ ] **Step 71:** Add game over score submission flow (3-char initials entry, arcade style)
- [ ] **Step 72:** Add replay/ghost system (input recording + deterministic playback via seed)
- [ ] **Step 73:** Add achievement system (10 achievements, persistence, unlock popups)
- [ ] **Step 74:** Add endless/survival mode (continuous waves, escalating intensity, separate leaderboard)
- [ ] **Step 75:** Add two-player mode — alternating turns (independent scores, comparison scoreboard)
