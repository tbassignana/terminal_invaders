"""`staged()` (spec §3.6): a real game at the start of any wave, for golden frames, tuning runs, benches and the smoke.

Not reachable from Config, the CLI or a tape: a staged game sets `staged = True`, and replay.Recorder refuses it.
"""

from dataclasses import fields

from ..model import CAMPAIGN_WAVES
from .boss import floor_hp
from .game import Game, script_wave
from .tables import BOSSES, ENDLESS, PROFILES, WAVES, Director

PHASES = ("wave", "playing", "upgrade", "continue")
DIRECTOR_KEYS = tuple(field.name for field in fields(Director))


def staged(
    config,
    wave,
    *,
    upgrades=(),
    lives=None,
    bombs=None,
    phase="wave",
    boss_phase=0,
    ticks=0,
    immortal=False,
    director=None,
) -> Game:
    """A game at the start of `wave` (its intro), built through the engine's own wave start.

    upgrades: names applied as picks, in order. phase: "wave" (the intro), "playing" (the intro run out with no
    input), "upgrade" (the card that comes before `wave`) or "continue" (Easy's prompt, as if the last life had just
    gone in `wave`). boss_phase: the boss's phase index, reached through real phase breaks (the game is then in
    `playing`). ticks: steps with no input afterwards. immortal: lives held at 99 (length tuning and density sweeps
    only). director: overrides of the director row by field name, for this wave and every later one (every floor
    and cap still applies).
    """
    if type(wave) is not int or wave < 1 or (wave > CAMPAIGN_WAVES and config.mode != "endless"):
        raise ValueError(f"no wave {wave!r} in mode {config.mode}")
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {', '.join(PHASES)}")
    overrides = dict(director or {})
    unknown = [key for key in overrides if key not in DIRECTOR_KEYS]
    if unknown:
        raise ValueError(f"unknown director fields: {', '.join(unknown)}")
    game = Game(config)
    game.player.guard = 0  # wave 1's training shield isn't the staged wave's; its own start gives it back if it has one
    for name in upgrades:
        game._take(name)
    game.events, game.notes = [], []
    if phase == "upgrade":
        row, _loop = script_wave(wave - 1) if wave > 1 else (0, 1)
        endless = config.mode == "endless"
        if wave == 1 or (not endless and not WAVES[row - 1].card) or (endless and (wave - 1) % ENDLESS.card_every):
            raise ValueError(f"no upgrade card comes before wave {wave}")
        game._start_wave(wave - 1)
        game._enter_upgrade()
    else:
        game._start_wave(wave)
    game._director = overrides
    game._fire = game._director_row(game._wave_row())
    if lives is not None:
        game.player.lives = lives
    if bombs is not None:
        game.player.bombs = bombs
    if immortal:
        game._immortal = True
        game.player.lives = 99
    if boss_phase:
        if game.boss is None or not 0 <= boss_phase < game.boss.phases or phase in ("upgrade", "continue"):
            raise ValueError(f"wave {wave} has no boss phase {boss_phase}")
        _to_phase(game, boss_phase)
    if phase == "playing":
        while game.phase == "wave":
            game.step(())
    elif phase == "continue":
        if PROFILES[config.difficulty].continues == 0 or config.mode == "endless":
            raise ValueError("only Easy has a continue, and never in Endless")
        game.player.lives = 0
        game.notice = "SHIP LOST"
        game._enter_continue()
    game.staged = True
    for _ in range(ticks):
        game.step(())
    return game


def _to_phase(game, target):
    """Run the boss through real phase breaks: its open parts are worn down to their floors, then each break plays
    out with no input. The game is left in `playing`, at the first tick of phase `target`."""
    while game.phase == "wave":
        game.step(())
    row = BOSSES[game.boss.kind]
    while game.boss.phase < target:
        phase = game.boss.phase
        spec = row.phases[phase]
        if not spec.vulnerable:  # an escorts-only phase: its flights are taken as arrived and destroyed
            game._pending = []
            for alien in game.aliens:
                game._gone(alien, False)
            game._purge()
        for part in game.boss.parts:
            if part.name in spec.vulnerable:
                low = floor_hp(row, part.name, phase)
                game.boss.hp -= part.hp - low
                part.hp = low
        game._phase_checks()
        while game.boss.state == "break":
            game.step(())
