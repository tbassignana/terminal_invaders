# Terminal Invaders

A terminal-based Space Invaders clone for macOS, built with Python curses.

## Features

- Classic Space Invaders gameplay in your terminal
- 60 FPS smooth rendering with frame time metrics
- Animated aliens with color cycling
- Erosion-based bunker defense system
- Frenzy mode (aliens fire faster as their numbers decrease)
- Level progression with difficulty scaling and bonus lives
- Mystery Ship (UFO) for bonus points
- Power-ups: rapid fire, shield, wide shot
- Combo scoring with time-windowed multiplier (up to 5x)
- Pause/resume with P or Escape
- Screen shake effect on player death
- High-score persistence to `~/.invaders_scores.json`
- Optional looping soundtrack and retro sound effects
- Configurable difficulty presets (easy, normal, hard)
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
| `--debug` | Enable debug logging to `invaders.log` |
| `--version` | Show version and exit |

## Gameplay

- Destroy all aliens before they reach the bottom
- Use bunkers for cover (they erode when hit)
- Shoot the Mystery Ship crossing the top for bonus points (50-300)
- Catch power-ups dropped by destroyed aliens
- Build combos by killing aliens in quick succession
- Earn bonus lives by completing levels
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
- **ScoreManager** with JSON persistence
- **AbstractSoundBackend** with MacOS and Null implementations
- 228 tests, 81%+ line coverage, enforced via CI
