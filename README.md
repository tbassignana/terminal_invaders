# Terminal Invaders

Space Invaders for your terminal. Clear the fleet, dodge incoming fire, and try to keep your shelters in one piece.

Play through a 12-wave campaign, see how far you get in endless mode, try the daily challenge, or take turns with a second player. There are bosses, power-ups, combo bonuses, and local high scores along the way.

Runs on macOS and Linux with Python 3.10 or newer. You'll need a terminal at least 60 columns wide and 24 rows tall.

## Play

From the project folder:

```sh
python3 invaders.py
```

To install the `invaders` command:

```sh
./setup.sh
source .venv/bin/activate
invaders
```

## Controls

| Key | Action |
| --- | --- |
| Left / right arrows or A / D | Move |
| Space | Fire |
| F | Toggle autofire |
| B | Use a bomb |
| P or Escape | Pause |
| M | Toggle sound |
| R | Restart |
| Q | Return to the menu, or quit from the menu |

Use the menus to choose a mode and difficulty. Scores save automatically when a run ends. Run `invaders --help` for more options, including replays, custom music, and reduced motion.

## Working on the game

```sh
python3 -m pip install -e '.[test]'
make test
make lint
```
