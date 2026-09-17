# Terminal Invaders

A fast, dependency-free Space Invaders arcade game for macOS terminals and zsh. Python 3.10+; also runs on Linux. Fits **60 × 24** cells and centers in larger windows.

```zsh
python3 invaders.py
```

For an installed `invaders` command:

```zsh
./setup.sh
source .venv/bin/activate
invaders
```

The installer uses a local virtual environment and leaves your shell configuration alone. You can also install with `python3 -m pip install .` in an existing environment. `python3 -m terminal_invaders` works directly from the checkout.

## The game

- **Classic:** twelve waves, three boss encounters, and a final victory.
- **Endless:** escalating waves without a finish line.
- **Daily:** a shared UTC date seed at normal difficulty, with its own daily board.
- **Duel:** two independent pilots face the same seed, alternating after each lost life.
- Three difficulties; accelerating formations, armored enemies, diving attackers, aimed shots, and mystery ships.
- Destructible shelters that repair between waves. Swept collisions keep fast shots from passing through targets.
- Chain kills for up to **5×** points. Perfect waves earn bonuses; every 10,000 points grants a life, up to six.
- Weapons improve through the campaign. Pickups grant rapid fire, spread shots, shields, slowed enemies, or shelter repairs. Unclaimed drops are collected when you clear a wave.
- A **nova bomb** clears hostile fire and damages the fleet. Eighteen direct kills recharge a bomb, up to three. Your last life grants a short rapid-fire boost and an emergency bomb.
- Local high scores, career statistics, eight achievements, reproducible seeds, and verified input replays.
- Generated retro sound effects on macOS, optional music, monochrome rendering, and reduced motion. No runtime packages or network connection required.

## Controls

| Key | Action |
| --- | --- |
| `←` / `→`, `A` / `D`, `H` / `L` | Move |
| `Space`, `↑`, `W` | Fire |
| `F` | Toggle autofire |
| `B` / `X` | Nova bomb |
| `P` / `Escape` | Pause or resume |
| `M` | Toggle sound |
| `F1` | Toggle FPS display |
| `R` | Restart the run |
| `Q` | Return to menu; quit from menu |
| Arrows or `W` / `S`; `Enter` / `Space` | Navigate and select menus |

Terminal keyboards report key presses and repeat events, rather than releases. Brief movement windows smooth repeats; autofire lets you concentrate on dodging. Pause and undersized windows freeze the simulation, including pickups, invulnerability, and enemy timers. Enlarging the window restores the same playfield without moving enemies or changing difficulty.

## Runs and replays

```zsh
invaders --quickstart --difficulty hard --name ACE
invaders --daily
invaders --endless --seed 1978
invaders --two-player
invaders --seed 42 --record run.json
invaders --replay run.json
invaders --verify-replay run.json       # headless, exit 0 only on a matching checksum
invaders --scores --difficulty hard
invaders --mono --reduced-motion --no-sound
invaders --music "$HOME/Music/arcade.mp3"
```

`--fps 15..120` changes rendering only: gameplay always advances at 60 ticks per second. A private seeded random stream, canonical input tape, versioned settings, and full-state SHA-256 checksum make replay independent of frame rate and terminal dimensions. During playback, `P` pauses and `Q` exits; gameplay keys cannot change the recording. Replays can include unfinished runs and support up to four hours. Legacy 0.1 seed-only recordings are rejected because they lack enough information for reliable playback.

`--record` writes the most recently played pilot's run on completion, restart, return to menu, or exit. In duel mode, it records that pilot independently, not the combined match. Starting another run replaces the file. Choose a fresh filename to keep each run. Completed live runs save scores automatically; replays and abandoned runs do not enter the leaderboards. Daily seed and difficulty override `--seed` and `--difficulty`.

Profiles live at `~/Library/Application Support/terminal-invaders/profile.json` on macOS and `$XDG_DATA_HOME/terminal-invaders/profile.json` (normally `~/.local/share`) on Linux. Use `--profile FILE` for a separate career. Boards keep ten scores per mode/difficulty, and daily results for the latest 31 dates. Atomic writes and a local file lock protect saves; malformed profiles are retained as `.json.bak` when replaced. The old `~/.invaders_*.json` files are untouched; the new rules start fresh boards.

Sound is optional and uses only game-owned `afplay` processes. The checked-in `soundtrack.mp3` can be selected explicitly with `--music ./soundtrack.mp3`; it is not required or included in the Python package. `--no-sound` starts muted; `--no-music` disables a supplied soundtrack. `--help` lists every option.

## Small modules, clear boundaries

| Module | Responsibility |
| --- | --- |
| `model.py` | Arena constants, settings, entity values |
| `engine.py` | Fixed-tick rules, collision handling, progression, state checksum |
| `ui.py` | Curses drawing, keyboard input, fixed clock, menus and duel sessions |
| `storage.py` | Validated profiles, boards, achievements, atomic writes |
| `replay.py` | Versioned sparse recordings and validation |
| `audio.py` | Generated effects and bounded, owned audio processes |
| `cli.py` | Argument validation and application lifecycle |

The engine has no terminal, clock, filesystem, or audio dependencies. Rendering never consumes gameplay randomness. There is no global event bus, threaded audio backend hierarchy, duplicated compatibility constants, or spatial index for a fleet of a few dozen aliens. Entity counts and catch-up work are bounded.

## Development

```zsh
python3 -m pip install -e '.[test]'
make test
make lint
python3 -m pytest --cov=terminal_invaders --cov-report=term-missing
```

Tests cover deterministic runs, collision ordering, damage and upgrades, campaign transitions, recording round trips and corruption, profile recovery, concurrent profile merging, audio process ownership, terminal input, and session states. CI runs on macOS and Linux with Python 3.10, 3.12, and 3.14.
