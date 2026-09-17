# Changelog

## 1.0.0

Rebuilt the game around a deterministic 60 Hz simulation and a small standard-library package. Replaced the 4,200-line application and its implementation-coupled tests with separate engine, terminal, audio, profile, replay, and CLI modules and behavioral tests.

- Added a complete twelve-wave campaign, daily UTC challenges, reproducible seeds, verified recordings, headless replay validation, autofire, charged nova bombs, and an alternating same-seed duel.
- Reworked enemy progression, armor, diving attacks, bosses, combos, weapons, shelters, pickups, score bonuses, and last-stand assistance.
- Separated rendering from simulation, bounded work and entity counts, added swept projectile collisions, and made pause/resize freeze every gameplay timer.
- Replaced unsafe global audio cleanup with bounded game-owned players and synthesized effects. Added mono and reduced-motion options.
- Unified local scores, statistics, and achievements in a validated, atomically written profile with concurrent-session merging.
- Preserved direct `python3 invaders.py` launch; added module and packaged commands. Simplified installation without changing shell configuration.
- Added macOS/Linux CI and deterministic, persistence, audio, input, and session regression checks.

Version 0.1 history remains in Git. Its profile files remain untouched; the new game uses fresh leaderboards and a new replay format.
