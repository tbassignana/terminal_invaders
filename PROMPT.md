# Terminal Invaders - Ralph Loop Improvement Prompt

## Instructions

You are improving the Terminal Invaders project — a Python curses-based Space Invaders clone. Follow this loop precisely for each step.

### Loop Protocol

1. **Read this file** (`PROMPT.md`) to understand the process.
2. **Read `TODO.md`**. If it does not exist, generate it using the improvements listed below.
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

## Phase 1: Original 25 Improvements (COMPLETED)

Steps 1-25 are all marked `[x]` in TODO.md. They covered architecture refactoring, gameplay features (UFO, pause, power-ups, combos, difficulty scaling, screen shake), testing (edge cases, integration, property-based, coverage), performance (spatial partitioning, resize handling, FPS metrics), and infrastructure (pyproject.toml, CI, Makefile, pre-commit, changelog).

---

## Phase 2: Visual Impact Improvements (Steps 26-50)

These 25 steps make the game dramatically more visually impressive within the curses terminal.

### Particle & Animation Systems

26. **Add a particle system engine**
    - Create a `Particle` dataclass with: x, y, dx, dy, char, color_pair, lifetime, age.
    - Create a `ParticleSystem` class that manages a list of particles, updates positions each frame, culls expired particles, and renders them.
    - The system should support spawning bursts of particles at a position with configurable spread, speed, lifetime, and character set.
    - Integrate into `Game.update()` and `Game._render_game()`. Skip particles in `test_mode`.
    - Tests: Verify particle creation, aging, expiration/culling, position updates, burst spawning with correct count.

27. **Add explosion particles on alien death**
    - When an alien is killed, spawn 5-8 particles at the alien's position using characters like `*`, `+`, `.`, `'`, `` ` ``.
    - Particles should spread outward in random directions with short lifetime (0.3-0.5s).
    - Use the alien's color for particle color, fading to dim (`A_DIM`) in the last half of lifetime.
    - Tests: Verify particle burst spawns on alien kill, verify particle count range, verify particles inherit alien color.

28. **Add trail effect on player projectiles**
    - Player projectiles should leave a fading trail of 2-3 characters behind them: `|` → `:` → `.` (from newest to oldest).
    - Implement as dim-colored trailing characters rendered at previous positions.
    - Store a small position history (last 3 frames) on each projectile or use the particle system to emit trail particles.
    - Tests: Verify trail renders behind moving projectile, verify trail length, verify trail doesn't persist after projectile removal.

29. **Add alien death animation (brief flash before removal)**
    - When an alien is hit, don't remove it immediately. Instead, replace its sprite with an explosion character sequence: `#` → `*` → `+` → gone, over 3 frames (~0.15s).
    - Create a `dying_aliens` list that holds aliens mid-death-animation with a frame counter.
    - Render dying aliens in a bright/flash color (white or yellow bold).
    - Tests: Verify alien enters dying state on hit, verify dying animation frame progression, verify alien fully removed after animation completes, verify score still awarded immediately.

30. **Add bunker damage visual effects**
    - When a bunker block is hit, briefly flash it white (`A_REVERSE`) for 2 frames.
    - Add subtle debris particles (1-2 small chars like `.` or `,`) that fall downward from the hit bunker.
    - Bunker color should shift: green (full) → yellow (damaged) → red (critical).
    - Tests: Verify bunker flash on hit, verify color changes per health level, verify debris particle spawning.

31. **Add player thrust/engine animation**
    - The player ship `^A^` should have a flickering engine effect below it that alternates each frame.
    - Alternate between rendering a dim `v` or `^` character one row below the ship center, in green dim.
    - When the player moves left/right, tilt the thrust character slightly (e.g., `\` when moving right, `/` when moving left, `v` when stationary).
    - Tests: Verify thrust character alternates per frame, verify directional thrust on movement, verify no thrust rendered in test_mode.

32. **Add starfield background**
    - Generate a static starfield of randomly placed dim `.` and `*` characters across the screen background.
    - Stars should be rendered first (behind all game entities) using `A_DIM` with white color.
    - The starfield should slowly scroll downward (1 pixel per second) to create a parallax depth effect.
    - Regenerate the starfield on screen resize. Store star positions in a list.
    - Tests: Verify star count is proportional to screen area, verify stars scroll downward over time, verify starfield regenerates on resize.

33. **Add color-per-alien-type rendering**
    - Currently all aliens use `COLOR_ALIEN` (magenta). Instead, assign distinct colors per alien type:
      - Type 0 (top rows): Cyan
      - Type 1 (middle rows): Magenta
      - Type 2 (bottom rows): Yellow
    - Add new color pairs `COLOR_ALIEN_TYPE_0`, `COLOR_ALIEN_TYPE_1`, `COLOR_ALIEN_TYPE_2`.
    - Aliens should also use `A_BOLD` attribute when in frenzy mode (< 30% remaining).
    - Tests: Verify different color pair assignment per alien type, verify bold attribute activation in frenzy mode.

34. **Add HUD separator line and decorative border**
    - Draw a thin horizontal line (using `─` or `-`) below the score/lives header (row 1).
    - Draw thin vertical borders on the left and right edges using `│` or `|`.
    - Draw corner characters `┌`, `┐`, `└`, `┘` at the four corners.
    - Use `COLOR_TEXT` with `A_DIM` for the border so it doesn't distract.
    - Tests: Verify border characters are placed at correct positions, verify border respects screen dimensions.

35. **Add animated title screen with ASCII art**
    - Replace the plain "SPACE INVADERS" text on the menu with large ASCII art block letters (5-7 lines tall).
    - The title should use a color cycling effect: rotate through green → cyan → white → yellow → green over time.
    - Display animated alien sprites below the title that march back and forth as a preview.
    - Tests: Verify ASCII art title has correct line count, verify color cycling state advances over time.

36. **Add score popup text on alien kill**
    - When an alien is killed, display the point value (e.g., "+30") briefly at the kill position.
    - The popup should float upward by 1 row over 0.5s, then fade out.
    - Use bold yellow text for the popup. If a combo is active, show the multiplier too (e.g., "+30 x3").
    - Store active popups in a list of `ScorePopup` dataclass instances.
    - Tests: Verify popup creation on kill with correct score value, verify popup floats upward, verify popup expires after duration.

37. **Add mystery ship visual effects**
    - The mystery ship should have a blinking effect: alternate between `A_BOLD` and `A_NORMAL` each frame.
    - Render the mystery ship with a distinct color (red, bold) and a wider sprite: `=<UFO>=`.
    - When the mystery ship is destroyed, create a larger explosion (10-12 particles) with the point value popup.
    - Tests: Verify mystery ship blink state alternates, verify wider sprite rendering, verify large explosion on destruction.

38. **Add power-up visual indicators**
    - Falling power-ups should blink (toggle visibility every 4 frames) to draw attention.
    - Each power-up type should have a distinct character and color:
      - `RAPID_FIRE`: `R` in red bold
      - `SHIELD`: `S` in cyan bold
      - `WIDE_SHOT`: `W` in yellow bold
    - When a power-up is active, display a small status indicator in the HUD: e.g., `[R 3s]` showing type and remaining seconds.
    - Tests: Verify power-up blink toggling, verify distinct characters per type, verify HUD indicator shows remaining time.

39. **Add screen flash effect on level complete**
    - When all aliens are cleared and the level transitions, flash the entire screen green briefly (0.15s) before showing the level transition screen.
    - Implement by temporarily setting `screen.bkgd()` to green color pair, then reverting.
    - Tests: Verify green flash activates on level complete, verify flash duration.

40. **Add wave/ripple effect on alien formation descent**
    - When the alien formation steps downward (hits a wall and descends), briefly render a horizontal "ripple" line across the screen at the descent row.
    - The ripple should be a line of `~` characters that fades from bold to dim over 0.2s.
    - Tests: Verify ripple triggers on alien descent, verify ripple position matches descent row, verify ripple fades and expires.

41. **Add player invincibility blink after taking damage**
    - After the player takes damage and respawns, the player sprite should blink (toggle visible/invisible) for 2 seconds to indicate invincibility frames.
    - During the blink period, the player cannot take additional damage.
    - The blink should alternate every 4 frames.
    - Tests: Verify blink state activates after damage, verify player is invincible during blink, verify blink ends after 2 seconds.

42. **Add combo text effect with scaling emphasis**
    - When the combo multiplier reaches 3x or higher, display a large "COMBO x3!" (or x4, x5) text in the center of the screen.
    - The text should use `A_BOLD | A_REVERSE` for maximum impact and fade out over 0.5s.
    - At x5 (max combo), add a brief screen flash in yellow.
    - Tests: Verify combo text appears at 3x+, verify text content matches multiplier, verify fade-out timing.

43. **Add smooth color transitions for bunker health**
    - Extend the color system so bunkers smoothly transition: green → green_bold → yellow → yellow_dim → red → red_dim.
    - Add new color pairs: `COLOR_BUNKER_FULL`, `COLOR_BUNKER_DAMAGED`, `COLOR_BUNKER_CRITICAL`.
    - Map bunker health levels to these pairs so the battlefield gradually turns red as bunkers erode.
    - Tests: Verify correct color pair returned for each health level.

44. **Add bottom HUD bar with game stats**
    - Add a bottom status bar (last row) showing: current level, aliens remaining (count/total), active power-ups, and combo status.
    - Format: `Lv:3 | Aliens: 28/55 | [R 3s] [S] | Combo: x3`
    - Use `A_REVERSE` background for the bar to make it visually distinct.
    - Tests: Verify bottom bar content updates with game state, verify alien count accuracy, verify power-up display.

45. **Add game over screen with death animation and stats**
    - When game over triggers, play a brief "curtain fall" animation: fill the screen row by row from top to bottom with `#` characters over 0.5s.
    - After the curtain, display the game over screen with: final score, level reached, aliens killed, and high score comparison.
    - Track `aliens_killed` counter during gameplay for the stats display.
    - Tests: Verify aliens_killed counter increments, verify stats display values match game state.

46. **Add level transition countdown animation**
    - Replace the static "Press SPACE to Continue" level transition with an auto-countdown: "3... 2... 1... GO!"
    - Each number should be displayed large (3 lines tall) with bold text, cycling through colors.
    - Auto-advance to PLAYING state after the countdown finishes (3 seconds total), or allow SPACE to skip.
    - Tests: Verify countdown state progression, verify auto-advance after 3 seconds, verify skip with SPACE.

47. **Add shield visual aura around player when shield power-up is active**
    - When the SHIELD power-up is active, render a visual aura around the player ship.
    - Display `(` to the left and `)` to the right of the ship, and `^` above center, all in cyan bold.
    - The aura should pulse: alternate between bold and normal every 0.25s.
    - Tests: Verify aura renders when shield is active, verify aura not rendered without shield, verify pulse alternation.

48. **Add alien type-specific death explosions**
    - Each alien type should have a distinct explosion effect:
      - Type 0 (small): 3-4 particles, quick fade, small spread.
      - Type 1 (medium): 5-7 particles, medium spread, uses `*` and `+`.
      - Type 2 (large): 8-10 particles, wide spread, uses `#`, `@`, `*`.
    - Top-row aliens (highest points) should have the most dramatic explosions.
    - Tests: Verify particle count varies by alien type, verify particle characters match type specifications.

49. **Add progressive background color shift as aliens descend**
    - As the alien formation gets closer to the player (measured by lowest alien Y), gradually shift the background tint.
    - When aliens are in the top half: normal (black background).
    - When aliens pass 60% screen height: shift to a very dim dark red tint.
    - When aliens pass 80% screen height: deeper red tint with `A_BOLD` on border elements.
    - Implement using curses `init_pair()` dynamic recoloring of the background pair.
    - Tests: Verify background state changes based on lowest alien Y position, verify correct thresholds.

50. **Add victory celebration animation when clearing a level**
    - When all aliens are destroyed, before transitioning to the level screen:
      - Spawn a large burst of 20-30 particles across the screen in random colors.
      - Flash each bunker (if any remain) in a rapid color cycle for 0.3s.
      - Display a brief "WAVE CLEAR!" text in the center with bold, reverse attribute.
    - This replaces the simple green flash from step 39 by extending it with particles.
    - Tests: Verify celebration particle burst count, verify "WAVE CLEAR!" text display.

---

## Phase 3: Gameplay & Menu Improvements (Steps 51-75)

These 25 steps enhance gameplay depth and build a rich main menu system.

### Main Menu System

51. **Add a main menu state machine with multiple screens**
    - Refactor the menu from a single screen to a navigable menu system with screens: `MAIN`, `HIGH_SCORES`, `OPTIONS`, `CONTROLS`, `CREDITS`.
    - Create a `MenuState` enum and `MenuSystem` class that tracks current screen and cursor position.
    - The main menu should display selectable options: "Play", "High Scores", "Options", "Controls", "Credits", "Quit".
    - Use arrow keys (up/down) to navigate and SPACE/ENTER to select.
    - Tests: Verify menu state transitions, verify cursor wrapping (top↔bottom), verify each menu option navigates to correct screen.

52. **Add high scores display screen**
    - The "High Scores" menu screen shows the top 10 scores from `ScoreManager`.
    - Display in a formatted table: Rank, Score, Level, Date.
    - Highlight the highest score in bold yellow. If the list is empty, show "No scores yet — play a game!".
    - Add "Back" option to return to main menu.
    - Tests: Verify score table formatting, verify empty state message, verify back navigation.

53. **Add options/settings menu screen**
    - The "Options" screen allows toggling: Sound Effects (on/off), Music (on/off), Difficulty (easy/normal/hard), Show FPS (on/off).
    - Display current values next to each option. Use left/right arrows to change values.
    - Options should immediately apply to the game config/state.
    - Add "Back" option to return to main menu.
    - Tests: Verify option value changes, verify difficulty change updates config, verify sound toggle works.

54. **Add controls help screen**
    - The "Controls" screen displays all keybindings in a formatted table.
    - Group by context: Menu controls, Gameplay controls, Power-up info, Scoring info.
    - Include visual alien sprites next to their point values.
    - Add "Back" option to return to main menu.
    - Tests: Verify controls screen contains expected keybinding entries, verify back navigation.

55. **Add credits screen**
    - The "Credits" screen shows project credits with scrolling text animation.
    - Content: game name, version, "Built with Python curses", contributor info, link to GitHub repo.
    - Text scrolls upward slowly (like movie credits). Press any key or wait for scroll to finish to return.
    - Tests: Verify credits content includes version, verify scroll position advances over time.

56. **Add menu cursor animation and selection effects**
    - The menu cursor should be a `>` character that bobs left/right with a small animation (±1 char oscillation).
    - When an option is selected (ENTER/SPACE), flash the selected row briefly with `A_REVERSE` before transitioning.
    - Add a subtle sound effect on cursor movement and selection (using existing SoundEffects system).
    - Tests: Verify cursor position tracks selected index, verify selection triggers flash state.

### Gameplay Enhancements

57. **Add alien formation patterns per level**
    - Instead of always using the same rectangular grid, define 3-4 formation patterns that rotate each level.
    - Patterns: standard rectangle, V-shape (chevron), diamond, and wave (sinusoidal offset per column).
    - Create a `FormationPattern` enum and a function that generates alien positions for each pattern.
    - Tests: Verify each pattern generates correct alien count, verify different pattern selected per level, verify positions are valid (within screen bounds).

58. **Add boss alien every 5 levels**
    - Every 5th level (5, 10, 15...) spawns a boss alien instead of the normal formation.
    - The boss is a large 5-char-wide sprite with multiple hit points (HP = level * 2, min 10).
    - The boss moves in a unique pattern (sinusoidal left-right across the screen) and fires 3-way spread shots.
    - When destroyed, award a large score bonus (boss HP * 50 points) and spawn many particles.
    - Tests: Verify boss spawns on levels 5, 10, 15, verify boss HP scales with level, verify boss score calculation, verify boss movement pattern.

59. **Add wave-based sub-levels**
    - Each level is now composed of 3 waves. Wave 1: standard formation. Wave 2: faster aliens. Wave 3: more aliens + faster + more aggressive.
    - Display "Wave 1/3", "Wave 2/3", "Wave 3/3" in the HUD.
    - Only advance to the next level after all 3 waves are cleared.
    - Award a "wave clear" bonus of 100 * wave_number points.
    - Tests: Verify wave counter increments, verify difficulty scales per wave, verify level advances after wave 3, verify wave clear bonus.

60. **Add player weapon upgrade system**
    - Track a `weapon_level` on the player (starts at 1, max 5).
    - Weapon level increases by 1 every 2 levels (levels 2, 4, 6, 8, 10 → max level 5).
    - Level 1: single shot. Level 2: faster projectiles. Level 3: dual shot (2 parallel). Level 4: triple shot (spread). Level 5: quad shot + faster.
    - Weapon level resets to 1 on game over.
    - Tests: Verify weapon level progression per level, verify projectile count per weapon level, verify weapon level caps at 5, verify reset on game over.

61. **Add aliens with special behaviors**
    - Introduce 2 new special alien types beyond the base 3:
      - `ZIGZAG` alien: Moves in a zigzag pattern instead of following formation. Worth 50 points.
      - `SHIELDED` alien: Has 2 HP, first hit removes shield (visual change), second hit kills. Worth 40 points.
    - Special aliens spawn randomly mixed into the formation (10% chance per slot on levels 3+).
    - Tests: Verify zigzag movement differs from standard, verify shielded alien survives first hit, verify special alien point values, verify spawn chance on eligible levels.

62. **Add collectible coins/gems dropped by aliens**
    - In addition to power-ups, killed aliens have a 15% chance of dropping a coin (separate from power-ups).
    - Coins are worth a flat bonus: bronze (10pts), silver (25pts), gold (50pts) — randomly weighted.
    - Coins fall faster than power-ups and disappear if not collected in 3 seconds.
    - Display total coins collected in the game over stats.
    - Tests: Verify coin drop rate, verify coin point values, verify coin expiration after 3 seconds, verify coin counter.

63. **Add progressive alien speed increase within a level**
    - Currently alien speed only changes between levels. Add within-level acceleration.
    - As aliens are destroyed within a level, the remaining aliens speed up proportionally (like the original Space Invaders).
    - When 75% remain: 1.1x speed. 50%: 1.3x. 25%: 1.6x. Last 5 aliens: 2.0x speed.
    - Tests: Verify speed multiplier at each threshold, verify speed resets on new level, verify speed scales correctly with alien count.

64. **Add a "last stand" mechanic**
    - When the player is on their last life, activate "last stand" mode.
    - Visual indicator: player ship turns red, HUD flashes a warning.
    - Gameplay buff: projectile speed +25%, fire rate cooldown halved (3 → 6 active projectiles allowed).
    - Tests: Verify last stand activates at 1 life, verify projectile speed increase, verify increased projectile limit, verify deactivation when life is gained.

65. **Add alien projectile variety**
    - Instead of all alien shots being identical, add 3 projectile types:
      - Normal: `!` — standard speed, standard damage.
      - Fast: `↓` or `v` — 1.5x speed, standard damage.
      - Heavy: `#` — 0.7x speed, destroys bunker blocks in 1 hit regardless of health.
    - Alien type determines which projectiles they fire (type 0: normal, type 1: mix, type 2: heavy chance).
    - Tests: Verify projectile type assignment per alien type, verify fast projectile speed, verify heavy projectile one-hit bunker destruction.

66. **Add screen-clearing bomb power-up**
    - Add a new power-up type: `BOMB` — when collected, destroys all alien projectiles currently on screen.
    - Visual: The `B` power-up falls in red. On activation, flash all alien projectiles white then remove them.
    - Award 5 points per projectile cleared.
    - Limited to max 1 bomb activation per 10 seconds (cooldown).
    - Tests: Verify bomb clears all alien projectiles, verify per-projectile score bonus, verify cooldown enforcement.

67. **Add alien dive-bomb attack**
    - Occasionally (5% chance per alien move cycle, increasing with fewer aliens), a single alien breaks formation and dive-bombs toward the player.
    - The diving alien moves diagonally downward at 2x speed toward the player's X position.
    - If the diving alien reaches the player row without being shot, it deals damage and self-destructs.
    - If shot during dive, award 2x normal points.
    - Tests: Verify dive-bomb triggering, verify diagonal movement toward player, verify damage on reaching player row, verify bonus points when shot mid-dive.

68. **Add score threshold milestones with rewards**
    - At score milestones (1000, 2500, 5000, 10000, 25000, 50000), award the player a bonus life.
    - Display a prominent "EXTRA LIFE!" message with particle effects when a milestone is reached.
    - Track which milestones have been awarded so they don't repeat.
    - Tests: Verify milestone detection at each threshold, verify bonus life awarded (once per milestone), verify milestone tracking prevents re-award.

69. **Add "bullet time" slow motion power-up**
    - Add a new power-up: `SLOW_MO` — slows all alien movement and projectile speed to 50% for 4 seconds.
    - Player speed is unaffected (feels faster by comparison).
    - Visual: dim the background slightly and add a subtle blue tint during slow-mo.
    - HUD shows `[SLOW 3s]` countdown.
    - Tests: Verify slow-mo halves alien movement interval, verify alien projectile speed reduction, verify player speed unchanged, verify effect expiration.

70. **Add persistent statistics tracking**
    - Create a `StatsTracker` class that persists to `~/.invaders_stats.json`.
    - Track lifetime stats: total games played, total aliens killed, total score earned, highest level reached, total time played, favorite difficulty, longest combo.
    - Display lifetime stats on a new "Stats" menu screen accessible from the main menu.
    - Tests: Verify stats increment correctly, verify persistence to/from file, verify stats display values.

71. **Add game over score submission flow**
    - When game over occurs, if the score qualifies for the top 10, show a "NEW HIGH SCORE!" message.
    - Allow the player to enter a 3-character name/initials (like classic arcade machines).
    - Input 3 characters using arrow keys to cycle A-Z per position, ENTER to confirm.
    - Store the name with the score in `ScoreManager`.
    - Tests: Verify high score detection, verify initial entry cycling (A→Z→A), verify name stored with score record.

72. **Add replay/ghost system**
    - Record all player inputs (key + frame number) during a game into an `InputRecording` list.
    - After game over, offer "Watch Replay" option that plays back the inputs against the same random seed.
    - Store the random seed at game start; restore it for replay to reproduce identical alien behavior.
    - Tests: Verify input recording captures keys with frame numbers, verify replay with same seed reproduces same game state after N frames.

73. **Add achievement system**
    - Define 10 achievements: "First Kill" (kill 1 alien), "Combo Master" (reach x5 combo), "Boss Slayer" (defeat a boss), "Sharpshooter" (80%+ accuracy in a level), "Survivor" (complete level with no damage), "Pacifist" (survive 10 seconds without firing), "Millionaire" (reach 10000 score), "Marathon" (reach level 10), "Collector" (collect 5 power-ups in one game), "UFO Hunter" (shoot 3 mystery ships).
    - Display unlocked achievements on a menu screen with checkmarks.
    - Show a brief popup when an achievement unlocks during gameplay.
    - Persist achievements to `~/.invaders_achievements.json`.
    - Tests: Verify at least 3 achievement trigger conditions, verify persistence, verify unlock popup state.

74. **Add endless/survival mode**
    - Add "Survival Mode" as a menu option alongside the standard game.
    - In survival mode: no levels, aliens continuously respawn in waves of increasing intensity.
    - Score multiplier increases by 0.1x every 30 seconds survived.
    - Display a survival timer in the HUD. Separate high score leaderboard for survival mode.
    - Tests: Verify survival mode starts without level transitions, verify escalating spawn rate, verify timer display increments, verify separate score persistence.

75. **Add two-player mode (alternating turns)**
    - Add "2 Player" option to the main menu.
    - Players alternate turns: Player 1 plays until they lose a life, then Player 2 plays.
    - Each player has independent score and lives. Display whose turn it is between switches.
    - Game over when both players lose all lives. Show comparison scoreboard at the end.
    - Tests: Verify player turn alternation on life loss, verify independent score tracking, verify game over requires both players at 0 lives, verify final scoreboard.

---

## Completion Promise

The loop is complete when all 75 items in `TODO.md` are marked `[x]`. At that point, Terminal Invaders will have:

- **Phase 1** (1-25): Clean architecture, core gameplay, testing, CI/CD
- **Phase 2** (26-50): Particle systems, explosions, starfield, color theming, animated menus, visual polish
- **Phase 3** (51-75): Full menu system, boss battles, wave system, weapon upgrades, achievements, survival mode, 2P mode

Max loops: 75
