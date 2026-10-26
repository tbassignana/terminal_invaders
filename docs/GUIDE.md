# Terminal Invaders guide

The full reference for players: every key, mode, setting and option. The [README](../README.md) has the short version.

## Contents

- [Controls](#controls)
- [Campaign](#campaign)
- [Arcade mode](#arcade-mode)
- [Settings](#settings)
- [Accessibility](#accessibility)
- [Options](#options)
- [Big windows](#big-windows)
- [Colours](#colours)
- [Terminals](#terminals)
- [Sound](#sound)
- [Your profile](#your-profile)
- [First run](#first-run)

## Controls

| Key | Action |
| --- | --- |
| Left / right arrows, A / D, or H / L | Move |
| Down, S or J | Stop |
| Space, Up, W or K | Fire (hold to keep firing) |
| B or X | Use a bomb |
| A / D or arrows, then Space (between waves) | Choose an upgrade |
| P or Escape | Pause |
| R (in the pause menu) | Restart |
| Q (in the pause menu) | Return to the menu |
| Escape twice (in the menu) | Quit |
| Up / Down, Left / Right, Escape (in SETTINGS) | Choose a row, change it, save and go back |
| Letters and digits, Up / Down, Left / Right, Backspace, Enter, Escape (on the initials card) | Type your initials, change a letter, move, erase, save, skip |
| Any key (during the title demo) | Stop the demo |
| M | Toggle sound |
| F1 | Toggle the stats line (fn+F1 on a Mac keyboard) |
| Ctrl-L | Redraw the screen |

On an AZERTY keyboard Z fires, Q moves left, S stops and D moves right, so Q never ends a run by accident.

On the initials card, letters and digits type, M included, so M doesn't toggle sound there; Space doesn't, and Up and Down reach a blank. For half a second after the card appears it takes no key, so the keys you were pressing as your last ship went don't type. During the title demo any key stops it and does nothing else: Enter doesn't start a game and Escape doesn't count toward quitting. With the cursor on QUIT, the menu says `Esc again, or Enter, to quit.` If the window is too small, the game says how big it needs to be and which key takes you back (`Esc Esc quits` on the title); other keys wait until the window is big enough.

Terminals that report key releases let you hold a key to move and let go to stop. Elsewhere the ship moves when you tap a direction and keeps going until you tap the other way or tap stop. Holding Space keeps firing in both cases, also while you tap keys to steer. Tapping Space counts as holding it only if you tap faster than about eight times a second, or about four times a second over SSH, where keys arrive less evenly. Without releases the game can't see you let go of Space once another key has taken its auto-repeat, so it keeps firing until you tap Space again or pause. It also can't tell Space is still down if you steer before Space starts to repeat (your keyboard's repeat delay, about half a second) or your keyboard repeats keys slowly, so press Space again. The game asks the terminal when it starts; `invaders --probe` says `release` after `keys kitty` when yours reports them.

| Terminal | Move by |
| --- | --- |
| kitty, Ghostty, foot | holding |
| iTerm2 3.5.6 or later, Alacritty 0.13 or later, Konsole 26.08 or later | holding |
| WezTerm with `enable_kitty_keyboard = true` | holding |
| Terminal.app, GNOME Terminal and other VTE terminals, xterm, WezTerm as installed | tapping |
| Older iTerm2 and Konsole, the Linux console | tapping |
| tmux, screen, mosh | tapping, whatever terminal they run in |
| SSH | the same as the terminal you connect from |

`--controls hold|latched|timed` picks a scheme yourself. With `timed` the ship moves while you hold a direction and stops soon after you let go, on any terminal. After a press shorter than your keyboard's repeat delay (about half a second), or once you press Space or B while moving, it keeps going until you stop it with S or a tap the other way.

`--keys arrows|vim|wasd-physical` switches to another key preset (`wasd-physical` binds keys by their place on the keyboard, on terminals that can report it). `--keymap FILE` loads your own bindings from a JSON file that starts from a preset, binds keys and unbinds them, for example `{"preset": "default", "bind": {"play.fire": ["space", "j"]}, "unbind": ["play.stop:j"]}`. A file the game can't use is refused with the reason before the game starts.

Without release events, the game can't tell a second press of the same key from the key auto-repeating, so a quick double press of Escape, P or Enter counts once: give the second press a moment (Arcade mode's fire button has its own rules; see [Arcade mode](#arcade-mode)). It learns how long your keyboard waits before repeating as you hold keys; until then, if yours waits longer than 0.7 s, holding P, B or Escape can count twice.

Resizing the window, switching away from a terminal that reports focus, or coming back from Ctrl-Z pauses the game; resuming then counts down 3-2-1 first.

Use the menus to choose a mode and difficulty. Scores save automatically when a run ends.

If the game is ever killed outright (`kill -9`), your terminal can be left on the game's screen or in an odd keyboard mode; type `reset` and press Enter to fix it.

## Campaign

The campaign is 24 waves in four acts. Each act brings new kinds of enemy, one or two at a time, and ends with a boss that changes its attack as you break it apart. A winning run takes about half an hour.

| Act | Waves | What's new | Boss |
| --- | --- | --- | --- |
| I. Contact | 1-6 | scouts, divers, gliding fleets, gunners that aim | the Warden |
| II. Swarm | 7-12 | weavers, snaking fleets, armoured shells, lancers with fast needles | the Hive Queen |
| III. Siege | 13-18 | bombers that smash shelters, circling fleets, splitters, hunters that aim ahead of you, bullet curtains | the Dreadnought |
| IV. Armada | 19-24 | carriers, squadron dives, armoured veterans | the Mothership |

You have two shots in the air at a time. Holding Space fires whenever one is free, but every shot that misses breaks your combo, so aiming pays. Each hit in a row builds the combo, up to x6, and multiplies your points.

Anything that aims at you warns you first. The enemy changes shape, a mark on the ground shows where a beam or a curtain will fall, and you hear a short chirp. You always get at least half a second's notice before anything can hit you. After you lose a ship, the screen clears of enemy fire and you get two seconds of safety.

The bomb (B) clears every enemy bullet on screen and damages enemies close to you. It doesn't clear the wave or hurt bosses. Hits in a row charge the next bomb; the BOMB meter shows how close it is. From act II on you can earn one new bomb in each act (a full meter in act I waits for act II), so save it for when nothing else will do.

After waves 3, 6, 9, 12, 15, 18 and 21 you choose one of three upgrades: an attack, a defence and a utility. Move with A / D or the arrows and take one with Space. TRIPLE SHOT, a third shot in the air, is always on the first card. Power-ups (R rapid, S spread, + shield, T slow, L repair, B bomb) drop from the cargo ship marked `$` in each wave from wave 2 (boss waves have none), from the mystery ship, from bosses, and now and then from any enemy.

You start with three ships and earn one more for each of the first three bosses. If a fleet reaches the ground, you lose a ship and that fleet is gone.

| | Easy | Normal | Hard |
| --- | --- | --- | --- |
| Enemy fire | slower; aimed shots go where you were when the warning began | aimed at you; from wave 16 some aim ahead of you | faster; aimed ahead of you from wave 5; more bursts and crossfire |
| Warnings | longer | | shorter |
| Shelters | four, rebuilt every wave | four, patched every wave and rebuilt every act | three thinner ones, rebuilt every act |
| Enemies | fewer gunners; armoured veterans near the end | armoured veterans near the end | armoured veterans from act II |
| Help | a training shield for waves 1-3, two bombs to start, Last Stand, one continue | a training shield for wave 1, Last Stand | none |
| Most ships | 5 | 5 | 4 |

Last Stand: the first time you're down to your last ship, you get a bomb and rapid fire. A continue (Easy) restarts the wave with two ships and your score back at zero.

Endless plays the campaign and then keeps going through acts II-IV, adding a new twist with every wave from wave 25 (faster reloads, more armour, more divers, faster shots, and more). The daily challenge is the campaign on normal, the same for everyone that day. In a two-player game, the turn passes at the start of each wave and whenever a ship is lost.

Replays and scores from the earlier 12-wave version of the game can still be checked with `--verify-replay`, but not watched, and their scores have their own page of FLIGHT RECORDS.

## Arcade mode

Pick ARCADE in the menu, or start it straight away:

```sh
python3 -m terminal_invaders --arcade --quickstart
```

(`invaders --arcade --quickstart` once installed.) Arcade mode is a port of the 1978 arcade game's own program, routine by routine, onto a model of the machine's memory, reading the original's own data tables (start-up values, scores, timings and messages) as it did, so it plays as a one-player game did on the cabinet:

- The fleet of 55 invaders, moved one invader per frame, so the rack ripples and speeds up as it thins out, steps 2 pixels (the last invader 3 to the right) and drops 8 at each edge, starting lower in later rounds.
- The three kinds of invader shot, their reload timing and the columns they come from, one shot of yours at a time, and the shields eroding wherever anything hits them.
- The saucer: when it appears, which way it flies, and what it scores (the 8th shot of each round and every 15th after it, the 23rd, 38th and so on, score 300).
- Three ships, an extra ship at 1,500 points, and the four-digit score that rolls over at 10,000 (your high-score board keeps the full score).
- The cabinet's colours: white, a red band at the top and green near the bottom, where invaders turn green as they reach the shields.

The invaders, the saucer, their explosions and the font are new. The new sprites keep the originals' outlines, column by column, so your shots hit them on the same pixels, and the saucer's score digits meet a shot where the original's did. The shots, their explosions, the cannon's wreck and the shields keep the original bitmaps, because those pixels decide which shots hit and how the shields wear down; the cannon is the original's outline with a small hatch of ours. The rules are exact; with the new art the game's drawing takes a few microseconds more or less, which now and then sends a game down a different path from the 1978 machine given the same inputs (in testing, 1 game in 207, and 12 more with a kill scored a frame later). Sounds follow the cabinet's sound circuits, approximated with the game's own effects. Coins, the attract screens and two-player games are left out: a game starts with one credit spent, and the menu stands in for the attract screen. If a lone invader wraps round past the edge of the screen into the machine's memory, as it can on the cabinet, the game ends there. Arcade mode has one difficulty, its own high-score board and its own replays (`--record` and `--verify-replay` work as for the other modes); `--seed`, `--difficulty`, `--scale`, `--glyphs` and `--reduced-motion` have no effect on it.

The 224x256 screen is scaled to the largest size that fits the window:

| Window (columns x rows) | Screen pixels in each half of a character cell |
| --- | --- |
| 224x128 or larger | 1, every pixel |
| 112x64 | 2 x 2 |
| 75x43 | 3 x 3 |
| 56x32 | 4 x 4, the smallest |

At 3 x 3 and 4 x 4 a pixel of the block covers several of the screen's, so invaders side by side can run together for a moment as the fleet steps. In a smaller window the game pauses and says how big it needs to be; enlarge the window or zoom out (a smaller font), then resume. The menus need the usual 60x24.

The keys are the usual ones (see [Controls](#controls)): move with the arrows, A / D or H / L, stop with Down or S, fire with Space, Up, W or K, pause with P or Escape. Fire works as the cabinet's button did: each press fires once, holding it doesn't fire again, and a tap while your last shot is still flying is lost. On terminals that don't report key releases (see [Controls](#controls)), the game tells a tap from the key repeating by its timing. A tap about your keyboard's repeat delay after the last one fires a moment late, once it's clear the key isn't repeating, and taps quicker than the key repeats straight after such a tap can be taken for the key repeating. Until the game has learned that delay from a key you've held, a held Space can fire a second time at its first repeat (with a key repeat slower than about 7 a second, up to four times more), and over SSH very fast taps can be taken for a held key. With a key repeat of 5 a second or slower (about 8 a second over SSH) the repeats can't be told from taps, so a held Space fires at each one; a faster key repeat, or a terminal that reports key releases, avoids that. Bombs and upgrades belong to the campaign and do nothing here.

## Settings

SETTINGS on the title menu sets the game up the way you like it. Your choices are saved in your profile and used every time you play. A command-line option still wins for that run, isn't saved, and shows its row dim with the option's name. Where your terminal can't show a choice, its row says what you get instead, such as `HIGH CONTRAST (16 COLOURS)` or `DEFAULT (MONO)`.

| Row | Choices | What it does |
| --- | --- | --- |
| PALETTE | Default, Colour-blind safe, High contrast, Mono | The colours (see [Accessibility](#accessibility)) |
| BACKGROUND | Auto, Dark, Light, Terminal | Dark or light background, or your terminal's own |
| EFFECTS | Full, Calm, Off | Bursts, shakes and stars in the Campaign modes |
| REDUCED MOTION | Off, On | Turns every effect off and keeps the score popups still |
| SCALE | Auto, Text, 2x, 3x, 4x, 6x | The largest arena scale (see [Big windows](#big-windows)) |
| CONTROLS | Auto, Hold, Tap, Timed | How the ship moves (see [Controls](#controls)) |
| KEYS | Default, Arrows, Vim, WASD | The key preset |
| FRAME RATE | 60, 30, 20, 15 | Frames per second |
| SOUND | On, Off | Sound at start (M still toggles it while you play, and that isn't saved) |
| TITLE DEMO | Auto, On, Off | After 30 seconds on an idle title, a recorded game plays, then the high scores (Auto: on unless reduced motion is on) |
| TIPS | Auto, Always, Off | The bottom-row tips (Auto: until your first Campaign, Endless or Daily run ends) |
| PILOT | your initials | Your name on the high scores |

Changes take effect at once, apart from TITLE DEMO (the next time the title is idle) and TIPS (the next run). Escape, or BACK, saves and goes back to the title.

## Accessibility

- **Colour-blind safe** is for red-green colour blindness: hazards are warm (yellow shots, orange divers) and your ship and shots are cool. **High contrast** is for low vision and greyscale screens; it paints its own background, even with `--theme terminal`, to keep its contrast. **Mono** uses no colour at all; shapes, bold, dim and reverse tell things apart. Arcade mode keeps the 1978 cabinet's colours under every palette but Mono.
- **Reduced motion** turns every effect off in the Campaign modes, and shows the score popups and `HIT` as still text. Arcade mode's 1978 screen has no effects and keeps its own look.
- **Effects** come in three levels: FULL (with a short shake when your ship is lost, a boss breaks a phase or a boss falls), CALM (the bursts stay, but no shake and the stars hold still) and OFF.
- No effect flashes the screen. Each shake counts as a flash, and there are at most three a second (WCAG 2.3.1). In SETTINGS a background change is applied at most once a second, however fast you press.
- The shake turns itself off over SSH and on slow links.
- `--mono` and `NO_COLOR` turn colour off. A palette you saved, or one given with `--palette`, wins over `NO_COLOR`; `--color` and `--mono` win over both.

## Options

`invaders --help` lists every option. The main ones:

| Option | What it does |
| --- | --- |
| `--endless`, `--daily`, `--arcade`, `--two-player` | Endless mode, the daily challenge, the 1978 rules (see [Arcade mode](#arcade-mode)), or two players taking turns (also `--mode classic\|endless\|daily\|arcade`; `classic`, or `campaign`, is the campaign) |
| `--difficulty easy\|normal\|hard` | Difficulty, normal by default |
| `--seed N` | Play a particular invasion again |
| `--name NAME` | Your name on the scoreboard |
| `--quickstart` | Skip the title screen |
| `--controls auto\|hold\|latched\|timed` | Hold keys, tap to move (latched), or hold on any terminal (timed); auto picks for your terminal |
| `--keys PRESET`, `--keymap FILE` | Key bindings: a preset, or your own file (see [Controls](#controls)) |
| `--color auto\|truecolor\|256\|16\|mono`, `--mono` | Colour tier (see [Colours](#colours)); it wins over a saved palette |
| `--palette default\|cvd\|contrast\|mono` | Colours: the usual ones, colour-blind safe, high contrast or mono (see [Accessibility](#accessibility)) |
| `--effects full\|calm\|off` | Effects in the Campaign modes |
| `--tips auto\|always\|off` | Bottom-row tips: until your first run ends, on every run, or never |
| `--theme auto\|dark\|light\|terminal` | Dark or light palette, or your terminal's own background |
| `--scale auto\|1\|2\|3\|4\|6`, `--glyphs` | Largest arena scale, or text sprites at any size (see [Big windows](#big-windows)) |
| `--fps N` | Frames per second, 15 to 120 (the game always runs at 60 Hz; you get 60, 30, 20 or 15) |
| `--show-fps` | Start with the stats line on (F1 toggles it) |
| `--reduced-motion` | Fewer moving effects |
| `--pacing auto\|adaptive\|fixed\|off` | How many frames may wait for the terminal's answer (see [Terminals](#terminals)). Inside tmux or mosh the answer comes from them, not from your terminal, and they pace their own output |
| `--no-kitty`, `--no-sync` | Don't use the kitty keyboard protocol, or synchronized output, for checking whether a problem comes from them |
| `--probe`, `--probe --json`, `--probe --raw` | Show what your terminal supports, as text, as JSON, or as a report for a bug (see [Terminals](#terminals)) |
| `--no-sound` | Start muted (M turns sound on; see [Sound](#sound)) |
| `--record FILE`, `--replay FILE`, `--verify-replay FILE` | Save your last run as a replay, watch one, or check one without playing it |
| `--scores` | Print the high scores for the chosen mode and difficulty (`--scores --arcade` for Arcade mode's) |
| `--profile FILE` | Keep your name and scores in a separate profile file |
| `--debug` | Write a diagnostic log (`invaders --help` says where) |

Most of these can also be set in SETTINGS. An option given on the command line wins for that run and isn't saved.

## Big windows

The bigger your window, the bigger the arena. At 120x44 and up the invaders are drawn as pixel art with half-block characters, at 2, 3, 4 or 6 times the size of the 60x24 field, and the arena sits centred in the window. `--scale 2` caps how large it gets, and `--glyphs` (or `--scale 1`) keeps the classic text sprites at any size. Menus stay at their usual size in the middle.

Terminals that draw `▀` two columns wide (East Asian ambiguous width, found with a cursor check at startup) or don't use UTF-8 get the pixel arena at half the vertical resolution, drawn with coloured spaces only; shots always stay visible.

## Colours

The game picks the richest colours your terminal supports: 24-bit colour, 256 colours, the 16 basic colours, or none. `--color truecolor|256|16|mono` picks a tier yourself, and `--mono` is short for `--color mono`. Setting `NO_COLOR` to any non-empty value turns colour off, unless you saved a palette or gave `--palette`; `--color` and `--mono` win over both. Without colour, the sprites' shapes tell them apart, and the text sprites add bold for your ship, your shots and diving invaders, reverse for pickups, and dim for shelters and stars.

In 24-bit and 256 colours the game paints its own background, dark or light to match your terminal's (it asks the terminal, or reads `COLORFGBG`). `--theme dark` or `--theme light` forces one. Every palette's dark and light colours are checked to keep shots, shelters, pickups and divers apart for people with red-green or blue-yellow colour blindness, and to keep text readable; `python3 tools/palette_check.py` prints the figures. `--theme terminal` keeps your terminal's own background instead, which means that check no longer holds.

## Terminals

The game talks to your terminal directly with ANSI escape sequences, not through curses, so it doesn't need a terminfo entry and works whatever `TERM` says. When it starts it asks the terminal what it supports; `invaders --probe` prints what it found, and `invaders --probe --json` prints the same as JSON.

If something looks wrong in your terminal, please report it with the output of `invaders --probe --raw`. That is the terminal's own answers in the format of the test fixtures in `tests/fixtures/terminals`, with the environment variables the game looks at (and only those; `SSH_*`, `TMUX` and the like show as `1`) and what the game decided.

Over SSH, the game sends a new frame only once the terminal has answered for the last few, so your keys don't queue up behind frames the link hasn't delivered yet. The further away the terminal, the more frames it lets travel at once (2 to 12). `--pacing fixed` keeps that at 2, and `--pacing off` just paces by the clock. F1 (or `--show-fps`) shows a stats line along the bottom: frames per second, the round trip to the terminal, the pacing window (`ACK OFF` when the terminal stops answering), bytes per frame, a key counter, then the control scheme, pixel scale and colour tier.

## Sound

The game has sound effects on macOS and Linux, and M mutes or unmutes them while you play.

Sound comes from a small helper process that the game starts and always cleans up, so a sound problem never ends or slows a game. On macOS it plays through Core Audio, or `afplay` if that fails. On Linux it uses PulseAudio or PipeWire through `libpulse-simple`, then `pw-cat`, `pacat` or `aplay`, whichever works first. With none of them the game plays silently. `INVADERS_AUDIO=none` turns sound off entirely, and `INVADERS_AUDIO=NAME` picks one: `pulse-simple`, `pw-cat`, `pacat` or `aplay` on Linux, `coreaudio` or `afplay` on macOS (a comma list tries them in that order). `python3 -m terminal_invaders.audio.check` plays a short test and reports what it found.

## Your profile

Your scores, name and settings live in one profile file, `profile-2.json`, in `~/Library/Application Support/terminal-invaders/` on macOS and in `~/.local/share/terminal-invaders/` on Linux (or in `$XDG_DATA_HOME/terminal-invaders/` if you set `XDG_DATA_HOME`). `--profile FILE` uses another file. Settings are kept under `settings`, and only the ones you changed are written. An older version of the game keeps them as they are. The game never destroys a profile: one it can't read is moved aside, one from a newer version is left alone, and the same rules apply to settings. Entering your initials renames your row on the board and your pilot name. A two-player game doesn't change your name.

## First run

Until your first Campaign, Endless or Daily run ends, tips show on the bottom row during play: how to move and fire, what the HUD means, and what each new danger looks like when it first appears. They never cover the game's own messages. To turn them off, or to see them on every run, use TIPS in SETTINGS.
