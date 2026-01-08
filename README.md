# Terminal Invaders

A terminal-based Space Invaders clone for macOS, built with Python curses.

## Features

- Classic Space Invaders gameplay in your terminal
- 60 FPS smooth rendering
- Animated aliens with color cycling
- Erosion-based bunker defense system
- Frenzy mode (aliens fire faster as their numbers decrease)
- Level progression with bonus lives
- Optional looping soundtrack
- Retro sound effects using macOS system sounds

## Requirements

- macOS
- Python 3.x
- Terminal with at least 60x24 character size

## Quick Start

1. Clone the repository:
   ```bash
   git clone https://github.com/tbassignana/terminal_invaders.git
   cd terminal_invaders
   ```

2. Run the setup script to add the `invaders` command to your shell:
   ```bash
   chmod +x setup.sh
   ./setup.sh
   ```

3. Reload your shell configuration:
   ```bash
   source ~/.zshrc   # or ~/.bash_profile for bash
   ```

4. Launch the game:
   ```bash
   invaders
   ```

## Manual Launch

You can also run the game directly without setup:
```bash
python3 invaders.py
```

## Controls

| Key | Action |
|-----|--------|
| `A` / `←` | Move left |
| `D` / `→` | Move right |
| `Space` | Fire |
| `Q` | Quit |
| `R` | Restart (at game over) |

## Soundtrack

To enable background music, place an MP3 file at `~/soundtrack.mp3`. The game will loop it automatically during gameplay.  An AI generated soundtrack.mp3 from Suno is included in the repository

## Gameplay

- Destroy all aliens before they reach the bottom
- Use bunkers for cover (they erode when hit)
- Earn bonus lives by completing levels
- Watch out for increased alien fire rate as you eliminate them
