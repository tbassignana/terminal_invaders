# Working on Terminal Invaders

How the game is built, tested and tuned. The [README](../README.md) covers playing it, and the [guide](GUIDE.md) covers every key, mode and option.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
make test
make lint
```

The game itself needs only Python 3.11 or newer and its standard library; on a Mac, make the venv with a Python 3.11 or newer (`python3.13 -m venv .venv`), not the system's 3.9. The `dev` extra adds pytest, pytest-cov, ruff and build. `./setup.sh` rebuilds `.venv` with a plain install of the game, so run the pip line again after it. `make test` takes several minutes.

## How it fits together

| Part | What it does |
| --- | --- |
| `terminal_invaders/term/` | The terminal layer, in place of curses: owns the tty, asks the terminal what it supports, decodes keys (the kitty keyboard protocol included), encodes frames as ANSI, and paces frames by the terminal's answers over slow links. Its pure modules do no I/O. |
| `terminal_invaders/campaign/` | The Campaign's rules: 24 waves in four acts, bosses, upgrades and Endless, built from fixed tables. |
| `terminal_invaders/arcade/` | Arcade mode: the 1978 game's program ported routine by routine onto a model of the machine's memory. |
| `terminal_invaders/audio/` | Sound: a helper process mixes and plays it, and the game only sends it one-byte messages. |
| `terminal_invaders/legacy/` | The earlier 12-wave rules, kept so old replays still verify. |
| `app.py`, `ui.py`, `arena.py` | The main loop and the Renderer, the Session and its scenes, and the play field's drawing. |
| `cli.py`, `keymap.py`, `input.py` | The options, the key bindings, and the control schemes (hold, tap and timed). |
| `theme.py`, `sprites.py` | The palettes and the sprite art. |
| `fx.py`, `screens.py`, `settings.py`, `tutorial.py`, `attract.py` | Effects, the newer cards, SETTINGS, first-run tips and the title demo. Tests check that effects, palettes, tips and the demo never change how a game plays out. |
| `replay.py`, `rng.py`, `storage.py` | Replays, the random stream (PCG32, so a seed plays the same game on any Python) and the profile. |
| `tools/` | Bots and the balance harness, benchmarks, golden-frame and palette checks, and the media script. |

The simulation never reads a clock or does I/O. Every game is a seed plus the keys pressed, so any run can be replayed exactly and checked against a digest of its state (`--record`, `--replay`, `--verify-replay`).

## Tests and CI

`make test` runs the unit and integration tests. CI (`.github/workflows/ci.yml`) runs them with lint and coverage (at least 90% overall, and for each module of the terminal layer, the Campaign, the effects and the newer screens: `tools/term_coverage.py`) on Ubuntu and macOS with Python 3.11 to 3.15. Separately, on Python 3.13, it runs the palette checks and the golden frames, and builds, checks and installs the wheel.

After a change to how the game draws, `make frames` rewrites the golden frames (`make frames-check` lists what would change), and `make palette-check` runs the palettes' contrast and flash checks.

`python3 -m pytest -o addopts="" -m "gate or slow"` runs what `.github/workflows/gate.yml` runs on pushes to `main` and nightly: the acceptance checks (`-m gate`, Linux), including the SSH-link runs in `tests/test_ssh_lag.py` and the 60 fps runs in `tests/test_render_gate.py`, which take a minute each, and the slow tests. The wheel checks among them build the game, so they need `python -m pip install 'setuptools>=77'` in the same environment (or `make firstrun-wheel WHEEL_BUILDER=python3.13`). `-m audio` plays through a real sound server.

## Balance

The difficulty is tuned by bots that play like people:

| Bot | Reacts | Target |
| --- | --- | --- |
| `strong` | in a fifth of a second; now and then it notices a shot half a second late | wins 60 to 75% on Hard |
| `sk_d18_p85` | in three tenths of a second, and notices a shot late more often | wins 30 to 45% on Normal, and 70 to 85% on Easy without a continue |
| `perfect` (`skilled`) | in a tenth of a second, and sees every shot | wins at least nine games in ten on Normal |

- `make balance` has the bots play a quick set of seeded games and prints win rates, pacing and economy for each. It also replays every life lost and exits 1 if one could not have been dodged or a shot broke the rules.
- `make balance-full` runs every bot on every difficulty, plus long Endless games, which takes much longer. `make balance-act ACT=n` plays one act at a time.
- `make balance-tune` and `make balance-gate` run the full quality-bar check. They take hours, so they split into resumable chunks with `--shard`.
- `python3 tools/balance.py --input kitty|latched|timed|latched-ssh` (or `all`) has the bots press keys instead, the way a player would on that kind of terminal, through the game's own input handling. `make balance-input` has the strong bot play 200 seeds through each of those and compares them with the same seeds played directly (run `make balance-gate` first; `--check` exits 1 when it fails).
- `python3 -m terminal_invaders.campaign.validate` checks the wave tables, and `python3 tools/balance.py --help` lists every option.

## Benchmarks

`python3 tools/bench_render.py` times drawing and encoding a frame at four window sizes while a bot plays, plus the busiest scenes at 240x70 (wave 23 with every flight on screen, then the last boss's final phase), and `--arcade` Arcade mode's screen at 224x128 and 112x64. `--check` fails when a frame takes longer than its threshold (a few milliseconds, well inside a 60 fps frame) or the effects cost too much; its docstring lists the thresholds. `python3 tools/bench_engine.py` times the game's rules on their own, over a whole recorded game and a few of the busiest scenes; `--check` fails when they miss their budget.

## Screenshots and GIFs

`python3 tools/make_media.py` remakes every image in `docs/media` from the game itself, in about 40 seconds. Each gameplay frame is a real game state from a recorded game in `tests/fixtures`, and the title and SETTINGS cards are drawn as a new player sees them, all by the game's own renderer. It needs Pillow, ffmpeg and the DejaVu Sans Mono font, and says which is missing. `--only hero.gif` makes one file, and the tool's docstring says what each file shows. Run it again after a change to how the game looks, so the README matches the game.
