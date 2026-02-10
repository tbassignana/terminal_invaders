# Terminal Invaders

A terminal-based Space Invaders clone for macOS, built with Python curses.

## Features

### Core Gameplay
- Classic Space Invaders gameplay in your terminal
- 60 FPS smooth rendering with frame time metrics
- Animated aliens with color cycling and unique formation patterns
- Erosion-based bunker defense system
- Frenzy mode (aliens fire faster as their numbers decrease)
- Level progression with difficulty scaling and bonus lives
- Wave-based sub-levels (3 waves per level)

### Combat
- Mystery Ship (UFO) for bonus points (50-300)
- Boss aliens every 5 levels
- Alien dive-bomb attacks (break formation, diagonal dive toward player)
- Alien projectile variety (normal, fast, heavy bunker-breaker)
- Aliens with special behaviors
- Progressive alien speed increase within each level

### Power-ups & Upgrades
- Power-ups: rapid fire, shield, wide shot, bullet time (slow-mo)
- Screen-clearing bomb pickup
- 5-level player weapon upgrade system
- Collectible coins and gems dropped by aliens
- "Last stand" mechanic (buffs at 1 life: speed boost, double fire)

### Scoring & Progression
- Combo scoring with time-windowed multiplier (up to 5x)
- Score threshold milestones with bonus life rewards
- High-score persistence with arcade-style 3-character initials entry
- Persistent statistics tracking to `~/.invaders_stats.json`
- Achievement system (10 achievements with unlock popups)

### Game Modes
- **Classic** — standard level-based campaign
- **Endless/Survival** — continuous waves with escalating intensity and separate leaderboard
- **Two-Player** — alternating turns with independent scores and comparison scoreboard

### Replay & Polish
- Replay recording and deterministic playback via seed
- Screen shake effect on player death
- Particle effects
- Configurable difficulty presets (easy, normal, hard)
- Optional looping soundtrack and retro sound effects
- Pause/resume with P or Escape
- FPS counter (toggle with F1 or `--show-fps`)

## Requirements

- macOS (Linux works without sound)
- Python 3.10+
- Terminal with at least 60x24 character size

## Quick Start

```bash
git clone https://github.com/tbassignana/terminal_invaders.git
cd terminal_invaders
pip install -e .
invaders
```

Or run directly without installing:

```bash
python3 invaders.py
```

## Controls

| Key | Action |
|-----|--------|
| `A` / `←` | Move left |
| `D` / `→` | Move right |
| `Space` | Fire |
| `P` / `Escape` | Pause / Resume |
| `W` / `↑` | Menu navigation up |
| `S` / `↓` | Menu navigation down |
| `F1` | Toggle FPS counter |
| `Q` | Quit |
| `R` | Restart (at game over) |

## CLI Options

```
invaders --help
```

| Flag | Description |
|------|-------------|
| `--difficulty <easy\|normal\|hard>` | Difficulty preset (default: normal) |
| `--no-sound` | Disable sound effects |
| `--no-music` | Disable background music |
| `--show-fps` | Display FPS counter |
| `--fps <N>` | Set target FPS (default: 60) |
| `--endless` | Start in endless/survival mode |
| `--two-player` | Start in two-player alternating turns mode |
| `--record <FILE>` | Record replay to a JSON file |
| `--replay <FILE>` | Play back a recorded replay |
| `--debug` | Enable debug logging to `invaders.log` |
| `--version` | Show version and exit |

## Gameplay

- Destroy all aliens before they reach the bottom
- Use bunkers for cover (they erode when hit)
- Shoot the Mystery Ship crossing the top for bonus points (50-300)
- Catch power-ups dropped by destroyed aliens
- Build combos by killing aliens in quick succession
- Upgrade your weapon by collecting enough pickups (5 levels)
- Survive boss fights every 5 levels
- Earn bonus lives by completing levels and hitting score milestones
- Difficulty increases each level: faster aliens, more rows, weaker bunkers

## Soundtrack

To enable background music, place an MP3 file at `~/soundtrack.mp3`. The game will loop it automatically during gameplay.

## Development

```bash
pip install -e ".[test]"    # Install with test dependencies
make test                   # Run tests
make test-coverage          # Run tests with HTML coverage report
make lint                   # Run ruff linter
make clean                  # Remove build artifacts
```

## Architecture

- **GameConfig** dataclass for all constants (no global mutation)
- **EventBus** for decoupled publish/subscribe game events
- **SpatialGrid** for optimized collision detection
- **FrameTimer** for rolling-window performance metrics
- **ScoreManager** / **StatsManager** with JSON persistence
- **AchievementManager** for unlock tracking and popup display
- **ReplayRecorder** / **ReplayPlayer** for deterministic replay via seed
- **BossAlien** / **DivingAlien** for advanced enemy behaviors
- **ParticleSystem** for visual effects
- **AbstractSoundBackend** with macOS and Null implementations
- 691 tests with coverage enforced via CI
