"""Golden frames (spec 11.4, Phase 4 spec 20.3): deterministic sessions, rendered to text with one style letter per
cell.

A golden file is a header line, the rows between bars, the same rows as style letters ('.' is the default style)
and a legend that names each letter's roles and attributes and the SGR that shows it in the file's tier, so a diff
shows what changed and where, and a palette change shows in the legend. Every frame shows a real game state:

* the campaign's scenes of acts I-II come from golden-classic-normal's inputs, each at the first tick where its
  predicate holds (TAPE_MARKS), so a retune or a new tape moves nothing by hand. The tape is flown once per process
  (snapshots()), the game copied at each mark in that one pass, and each copy reused for every tier and size;
* scenes the tape doesn't reach (other kinds, shot kinds, beams and curtains, every boss phase, the later acts, the
  continue prompt, Endless) are built with campaign.testing.staged(), searched forward to their predicate with a
  scripted sweep (STAGED);
* Arcade mode's (ARCADE_SCENES, files named arcade-*) fly golden-arcade's inputs.

Phase 5 (spec 12.2): every scene renders with effects `off` unless it says otherwise, so the Campaign goldens are
Phase 4's apart from the named edits; the effect scenes come from fx_scenes, and the new screens' scenes (the
palettes, the settings and initials cards, the demo's banner, the tips and the centred too-small notice) follow.
"""

import argparse
import copy
import string
from collections.abc import Callable
from dataclasses import replace
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

from terminal_invaders import fx, screens, tutorial
from terminal_invaders.campaign.tables import ENDLESS, PROFILES
from terminal_invaders.campaign.testing import staged
from terminal_invaders.cli import defaults
from terminal_invaders.engine import Game
from terminal_invaders.model import WIDTH, Config
from terminal_invaders.replay import RESULT_KEYS, Playback, Tape
from terminal_invaders.rules import LEGACY
from terminal_invaders.term.cells import A_BOLD, A_DIM, A_REVERSE, CellBuffer
from terminal_invaders.theme import ROLES, resolve_theme
from terminal_invaders.ui import Painter, Session, arcade_frame

from . import fx_scenes

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden"
TAPE = ROOT / "tests" / "fixtures" / "golden-classic-normal.json"  # tools/make_golden.py: the skilled bot, seed 1978
ARCADE_TAPE = ROOT / "tests" / "fixtures" / "golden-arcade.json"  # tools/make_golden.py golden-arcade.json
# (tier, theme): 11.4's four, and the 16 colours of S1 to S9. Truecolor and 256 paint their own background.
TIERS = (("truecolor", "dark"), ("truecolor", "light"), ("256", "dark"), ("16", "dark"), ("mono", "dark"))
SCHEME = "latched"  # the Session's controls until a terminal says it reports releases
STATS = "FPS 60.0  RTT 42ms  W 3.4  B/f 812  K17  latched k{k} {tier}"  # what the Renderer passes with --show-fps
LETTERS = string.ascii_lowercase + string.ascii_uppercase + string.digits
ATTRS = ((A_BOLD, "bold"), (A_DIM, "dim"), (A_REVERSE, "reverse"))
DARK = (("truecolor", "dark"),)
TERMINAL = ("game_over", "victory")
SEED = 1978  # every staged scene's, as the tape's
IDLE = 60 * 60 * 60  # the most ticks the tape's run idles after its last input before the results are drawn anyway


class Profile:
    """Two boards of scores, career stats and three achievements, and two boards of old campaign-1 rows (the OLD
    RULES page); a finished run unlocks two more."""

    alert = error = None

    def __init__(self):
        rows = (("ACE", 21050, 8, "normal"), ("NOVA", 7770, 4, "normal"), ("HARDY", 99999, 12, "hard"))
        old = (("ACE", 21050, 8, "normal"), ("VEGA", 60250, 12, "normal"), ("HARDY", 19470, 8, "hard"))
        self.data = {
            "name": "ACE",
            "scores": [dict(name=n, score=s, wave=w, mode="classic", difficulty=d, seed=1978) for n, s, w, d in rows]
            + [
                dict(name=n, score=s, wave=w, mode="classic", difficulty=d, seed=1978, ruleset="campaign-1")
                for n, s, w, d in old
            ],
            "stats": dict(runs=12, kills=840, shots=2000, hits=1500, best_combo=9, ticks=216000),
            "achievements": ["first_blood", "combo_master", "sharpshooter"],
        }

    def best(self, config):
        return 1250 if config.mode == "arcade" else 15000

    def old_boards(self):
        """As storage.Profile.old_boards: campaign-1 rows per (mode, difficulty), best first."""
        boards = {}
        for row in self.data["scores"]:
            if row.get("ruleset") in LEGACY:
                boards.setdefault((row["mode"], row["difficulty"]), []).append(row)
        return {key: sorted(rows, key=lambda row: row["score"], reverse=True) for key, rows in boards.items()}

    def record(self, result, config, name, *, pilot=True):
        return ["Deep space", "High roller"]


class Audio:
    enabled = False

    def update(self, events=()):
        pass

    def toggle(self):
        self.enabled = not self.enabled


def options(**overrides) -> argparse.Namespace:
    """The goldens' options: effects off (spec 12.2) unless a scene sets them."""
    return defaults(**dict(seed=1978, fixed_seed=True, quickstart=True, name="ACE", effects="off") | overrides)


def session(replay=None, **overrides) -> Session:
    return Session(options(**overrides), Profile(), Audio(), replay)


@cache
def tape() -> Tape:
    return Playback(TAPE).tapes[0]


@cache
def arcade_tape() -> Tape:
    return Playback(ARCADE_TAPE).tapes[0]


def fly(game: Game, until: int = 1 << 30, idle_from: int = 1 << 30, source=tape) -> Game:
    """Step with the inputs of source's tape (none from idle_from on) until the tick, or until the run ends."""
    while game.tick < until and game.phase not in TERMINAL:
        game.step(source().actions_at(game.tick) if game.tick < idle_from else ())
    return game


class Mark(NamedTuple):
    """Where a tape scene is: `after` ticks past the first tick at which `when` holds of the game flown so far."""

    when: Callable[[Game], bool]
    after: int = 0


def at(tick: int) -> Mark:
    return Mark(lambda game: game.tick >= tick)


TAPE_MARKS = {
    "wave-intro": at(30),  # wave 1's intro, with act I's title
    "playing": at(600),
    "short-mismatch": at(900),  # the replay-mismatch scene's first tape
    "duel": at(3000),  # the duel's second pilot stops flying here
    "ufo": Mark(lambda game: game.ufo is not None and 8 <= game.ufo.x <= 52),
    "divers": Mark(lambda game: any(alien.state == "diving" for alien in game.aliens), 10),
    "clear": Mark(lambda game: game.phase == "clear", 20),
    "card": Mark(lambda game: game.phase == "upgrade" and game.armed == 0),  # the first card, as it arms
    "boss": Mark(lambda game: game.boss is not None and game.boss.state == "fighting", 120),  # the WARDEN
    "tape-end": Mark(lambda game: game.tick >= tape().ticks),
}


@cache
def snapshots() -> dict[str, Game]:
    """The golden tape flown once, with no input after its last tick until the run ends ("end"), and a copy of the
    game at each of TAPE_MARKS that it reaches."""
    game, due, taken = Game(tape().config), {}, {}
    left = dict(TAPE_MARKS)
    while True:
        for name, mark in list(left.items()):
            if mark.when(game):
                due[name] = game.tick + mark.after
                del left[name]
        for name, tick in list(due.items()):
            if game.tick >= tick:
                taken[name] = copy.deepcopy(game)
                del due[name]
        if game.phase in TERMINAL or game.tick >= tape().ticks + IDLE:
            break
        game.step(tape().actions_at(game.tick))
    taken["end"] = game
    return taken


def snapshot(name: str) -> Game:
    """A fresh copy of the tape's game at a mark, or an error naming what the tape never reaches."""
    taken = snapshots()
    if name not in taken:
        legacy = " (it is a campaign-1 tape)" if Playback(TAPE).legacy else ""
        raise LookupError(f"{TAPE.name}{legacy} never reaches the {name!r} scene: regenerate it (make_golden.py)")
    return copy.deepcopy(taken[name])


def short_tape(pilot: int, ticks: int, mark: str) -> Tape:
    """The golden tape cut at a mark's tick, as a tape of its own."""
    game = snapshot(mark)
    assert game.tick == ticks, (mark, game.tick, ticks)
    inputs = {tick: tape().actions_at(tick) for tick in range(ticks) if tape().actions_at(tick)}
    result = game.result()
    return Tape(pilot, game.config, ticks, inputs, game.digest(), {key: result[key] for key in RESULT_KEYS})


def playing(current: Session, game) -> Session:
    """current, playing game as its first pilot's."""
    current.pilots[0].game = game
    current.match_controls()
    return current


def flying(mark: str, **overrides):
    """A session playing the tape's game at a mark."""

    def build():
        return playing(session(**overrides), snapshot(mark))

    return build


def scene(name: str, **values):
    def build():
        current = session(quickstart=False)
        current.change_scene(name)
        for field, value in values.items():
            setattr(current, field, value)
        return current

    return build


def paused(reason="manual"):
    def build():
        current = flying("playing")()
        current.pause(reason)
        return current

    return build


def countdown():
    current = paused("resized")()
    current.resume(0.0)
    current.update_countdown(0.35)  # the second digit
    return current


def results():
    current = flying("end")()
    current.step(0)
    return current


def duel_results():
    """The first pilot loses the whole tape; the second flies it to tick 3000, then idles until the run ends."""
    current = session(two_player=True)
    current.change_scene("playing")
    current.pilots[0].game = snapshot("end")
    current.step(0)
    current.change_scene("playing")
    current.pilots[1].game = fly(snapshot("duel"), idle_from=0)
    current.step(0)
    return current


def replaying(tapes, ticks=1 << 30):
    def build():
        current = session(SimpleNamespace(tapes=tapes()))
        while current.scene == "playing" and ticks > sum(pilot.game.tick for pilot in current.pilots):
            current.step(0)
        return current

    return build


def verified():
    """The whole golden tape watched: its game at the tape's end, which the session verifies, then the results."""
    current = session(SimpleNamespace(tapes=[tape()]))
    playing(current, snapshot("tape-end")).step(0)
    return current


def mismatched():
    return [short_tape(1, 900, "short-mismatch"), replace(short_tape(2, 600, "playing"), digest="0" * 64)]


def sweep(tick: int) -> tuple[str, ...]:
    """The staged scenes' scripted pilot: Space held, sweeping the field and back, 140 ticks each way, so front rows
    fall and the shooters behind them get their turn."""
    return ("fire", "left" if (tick // 140) % 2 else "right")


def tells(kind: str):
    return lambda game: any(alien.kind == kind and alien.state == "telegraph" for alien in game.aliens)


def stands(kind: str):
    return lambda game: any(alien.kind == kind and alien.state == "formation" for alien in game.aliens)


def flies(kind: str):
    return lambda game: any(not shot.friendly and shot.kind == kind and 6 <= shot.y <= 13 for shot in game.shots)


def hazard(kind: str, state: str = ""):
    return lambda game: any(h.kind == kind and (not state or h.state == state) for h in game.hazards)


def ladder(game: Game) -> None:
    """Endless's modifiers due by the game's wave (one at ENDLESS.first and one more every ENDLESS.step waves, in
    ladder order), for an engine that leaves game.modifiers empty when a wave is staged; a no-op once it fills them."""
    if not game.modifiers and game.wave >= ENDLESS.first:
        due = (game.wave - ENDLESS.first) // ENDLESS.step + 1
        game.modifiers = [ENDLESS.ladder[n % len(ENDLESS.ladder)].name for n in range(due)]


def staging(
    wave: int,
    when=None,
    otherwise=None,
    *,
    difficulty="normal",
    mode="classic",
    after=0,
    pilot=sweep,
    limit=7200,
    phase="playing",
    boss_phase=0,
    setup=None,
    **overrides,
):
    """A session playing a staged game (lives held up while it is searched), `after` ticks past the first tick at
    which `when` holds as `pilot` plays it, or else at which `otherwise` does (a tell that this wave's volleys never
    show, say); the HUD then shows the profile's lives. setup, when given, is applied to the game before it is
    flown."""
    config = Config(difficulty, mode, SEED)

    def search(predicate):
        game = staged(config, wave, phase=phase, boss_phase=boss_phase, immortal=predicate is not None)
        if setup:
            setup(game)
        for _ in range(limit if predicate else 0):
            if predicate(game):
                break
            game.step(pilot(game.tick) if pilot else ())
        else:
            if predicate is not None:
                return None
        for _ in range(after):
            game.step(pilot(game.tick) if pilot else ())
        game.player.lives = min(game.player.lives, PROFILES[difficulty].lives)
        return game

    def build():
        game = search(when) or (search(otherwise) if otherwise else None)
        if game is None:
            raise LookupError(f"staged wave {wave} never shows its scene in {limit} ticks")
        return playing(session(**overrides), game)

    return build


@cache
def arcade(until: int) -> Session:
    """An Arcade game flown with golden-arcade's inputs, shared by the scenes at the same tick."""
    current = session(mode="arcade")
    fly(current.pilot.game, until, source=arcade_tape)
    return current


# The tape's scenes (scene, columns, rows, build, tiers): from 120x44 the arena is drawn in pixels (k = 2, and k = 3
# from 180x64); the menus stay a 60 x 24 card.
SCENES = [
    ("menu", 80, 24, scene("menu"), TIERS),
    ("help", 80, 24, scene("help"), TIERS),
    ("records", 80, 24, scene("records"), TIERS),
    ("career", 80, 24, scene("records", page=1), TIERS),
    ("old-rules", 80, 24, scene("records", page=2), TIERS),
    ("wave-intro", 80, 24, flying("wave-intro"), TIERS),
    ("playing", 80, 24, flying("playing"), TIERS),
    ("playing", 100, 30, flying("playing"), TIERS),
    ("ufo", 80, 24, flying("ufo"), TIERS),
    ("divers", 80, 24, flying("divers"), TIERS),
    ("boss", 80, 24, flying("boss"), TIERS),
    ("show-fps", 80, 24, flying("playing", show_fps=True), TIERS),
    ("paused", 80, 24, paused(), TIERS),
    ("paused-resized", 80, 24, paused("resized"), TIERS),
    ("paused-focus", 80, 24, paused("focus"), TIERS),
    ("paused-suspended", 80, 24, paused("suspended"), TIERS),
    ("paused-resumed", 80, 24, paused("resumed"), TIERS),
    ("countdown", 80, 24, countdown, TIERS),
    ("handoff", 80, 24, lambda: session(two_player=True), TIERS),
    ("results", 80, 24, results, TIERS),
    ("duel-results", 80, 24, duel_results, TIERS),
    ("replay", 80, 24, replaying(lambda: [tape()], 600), TIERS),
    ("replay-verified", 80, 24, verified, TIERS),
    ("replay-mismatch", 80, 24, replaying(mismatched), TIERS),
    ("too-small", 40, 12, paused("small"), TIERS),  # Phase 5: as a resize leaves it (paused), so the notice names Q
    ("playing", 120, 44, flying("playing"), TIERS),
    ("ufo", 120, 44, flying("ufo"), TIERS),
    ("divers", 120, 44, flying("divers"), TIERS),
    ("boss", 120, 44, flying("boss"), TIERS),
    ("paused", 120, 44, paused(), TIERS),
    ("show-fps", 120, 44, flying("playing", show_fps=True), TIERS),
    ("wave-intro", 180, 64, flying("wave-intro"), TIERS),
    ("playing", 180, 64, flying("playing"), TIERS),
    ("ufo", 180, 64, flying("ufo"), TIERS),
    ("divers", 180, 64, flying("divers"), TIERS),
    ("boss", 180, 64, flying("boss"), TIERS),
    ("countdown", 180, 64, countdown, TIERS),
    ("results", 180, 64, results, TIERS),
    ("replay", 180, 64, replaying(lambda: [tape()], 600), TIERS),
    ("reduced-motion", 180, 64, flying("divers", reduced_motion=True), TIERS),
    ("ambiguous-wide", 180, 64, flying("divers"), TIERS),  # half blocks unsafe: pooled 2:1
    ("menu", 240, 70, scene("menu"), TIERS),
    ("playing", 240, 70, flying("playing"), TIERS),
    ("paused", 240, 70, paused(), TIERS),
]
# Phase 4's scenes (spec 20.3), each in the five tiers at 120x44 and in truecolor dark at 80x24 and 180x64.
BOSS_WAVES = (("warden", 6, 2), ("hive_queen", 12, 3), ("dreadnought", 18, 3), ("mothership", 24, 4))
STAGED = [
    *(
        (f"kind-{kind}", staging(wave, tells(kind), stands(kind)))
        for kind, wave in (
            ("diver", 3),
            ("gunner", 5),
            ("guard", 6),
            ("weaver", 8),  # the first bursts: a weaver's tell
            ("shell", 9),
            ("lancer", 11),
            ("bomber", 13),
            ("splitter", 15),
            ("carrier", 19),
            ("mite", 19),  # a carrier's
        )
    ),
    ("veteran", staging(8, lambda game: any(alien.veteran for alien in game.aliens), difficulty="hard")),
    ("cargo", staging(2, lambda game: any(alien.cargo and alien.state == "formation" for alien in game.aliens))),
    *(
        (f"shot-{kind}", staging(wave, flies(kind)))
        for kind, wave in (("pellet", 1), ("dart", 5), ("hunter", 16), ("needle", 11), ("slug", 13), ("cluster", 15))
    ),
    ("beam-warn", staging(18, hazard("beam", "warn"), boss_phase=1, pilot=None, after=10)),
    ("beam-live", staging(18, hazard("beam", "live"), boss_phase=1, pilot=None, after=5)),
    ("curtain", staging(17, hazard("gap"), after=10)),
    ("warning", staging(6, phase="wave", pilot=None, after=60)),  # the WARDEN's intro
    *(
        (f"boss-{kind.replace('_', '-')}-{number + 1}", staging(wave, boss_phase=number, pilot=None, after=240))
        for kind, wave, phases in BOSS_WAVES
        for number in range(phases)
    ),
    *((f"act-{act}", staging(wave, phase="wave", pilot=None, after=30)) for act, wave in ((2, 7), (3, 13), (4, 19))),
    ("clear", flying("clear")),
    ("card", flying("card")),
    ("prompt", staging(2, phase="continue", difficulty="easy", pilot=None, after=40)),
    ("endless", staging(40, mode="endless", pilot=None, after=300, setup=ladder)),
    ("reduced-motion-tell", staging(5, tells("gunner"), stands("gunner"), reduced_motion=True)),
    *((f"backdrop-{act}", staging(wave, pilot=None, after=300)) for act, wave in ((1, 2), (2, 8), (3, 14), (4, 20))),
]
SCENES += [
    (name, cols, rows, build, tiers)
    for name, build in STAGED
    for cols, rows, tiers in ((120, 44, TIERS), (80, 24, DARK), (180, 64, DARK))
]
SCENES += fx_scenes.entries(session, playing, TIERS, DARK)  # Phase 5's 70 effect frames (spec 2.12)


# Phase 5's new screens (spec 12.2).
def settings_card():
    current = session(quickstart=False)
    current.open_settings()
    return current


def initials_card():
    """A finished run's initials card, as a placing run shows it: rank #3 on its board."""
    current = results()
    current.initials_card = screens.InitialsCard(current.pilot.name, pilot=0, rank=3)
    current.change_scene("initials")
    return current


def demo():
    """The title demo's tape session at tick 600 (the Campaign tape is golden-classic-normal's first 5,400 ticks)."""
    current = replaying(lambda: [tape()], 600)()
    current.demo = True
    return current


def menu_in(name: str):
    return lambda: session(quickstart=False, palette=name)


def coached(name: str, build, *, k: int = 1, variant: str = "", pilot=None, shown: int = 30, limit: int = 7200):
    """A session playing a game that a Coach watched from its first tick (built by `build`, flown by `pilot`) until
    the tip `name` (with the Marks tip's variant) had shown `shown` ticks; the other tips count as taught."""

    def make():
        current = session(tips="always")
        current.enable_tips()
        current.start()
        coach = current.coach
        coach.done |= set(tutorial.TIPS) - {name}
        coach.view = tutorial.View(SCHEME, k, WIDTH * k)
        game = build()
        for _ in range(limit):
            if coach.active == name and coach.variant == variant and coach.visible and coach.shown >= shown:
                break
            actions = pilot(game.tick) if pilot else ()
            game.step(actions)
            coach.observe(game, actions, tuple(game.events))
        else:
            raise LookupError(f"the {name} tip never shows in {limit} ticks")
        game.player.lives = min(game.player.lives, PROFILES[game.config.difficulty].lives)
        return playing(current, game)

    return make


def wave(number: int, boss_phase: int = 0):
    return lambda: staged(Config("normal", "classic", SEED), number, boss_phase=boss_phase, immortal=True)


PALETTE_TIERS = (("truecolor", "dark"), ("truecolor", "light"), ("256", "dark"))
for name in ("cvd", "contrast"):  # busy play, a boss and the menu in the two new colour palettes
    SCENES += [
        (f"palette-{name}-divers", 120, 44, flying("divers", palette=name), PALETTE_TIERS),
        (f"palette-{name}-boss", 120, 44, flying("boss", palette=name), PALETTE_TIERS),
        (f"palette-{name}-menu", 80, 24, menu_in(name), PALETTE_TIERS),
    ]
SCENES += [
    ("palette-cvd-divers", 120, 44, flying("divers", palette="cvd"), (("16", "dark"),)),
    ("palette-cvd-boss", 120, 44, flying("boss", palette="cvd"), (("16", "dark"),)),
    ("settings", 80, 24, settings_card, TIERS),
    ("settings", 240, 70, settings_card, DARK),
    ("initials", 80, 24, initials_card, TIERS),
    ("demo", 80, 24, demo, DARK),
    ("demo", 120, 44, demo, DARK),
    ("tip-move", 80, 24, coached("move", lambda: Game(tape().config)), DARK),
    ("tip-telegraph", 80, 24, coached("telegraph", wave(5), pilot=sweep), DARK),
    ("tip-marks-beam", 80, 24, coached("marks", wave(18, 1), variant="beam", shown=5), DARK),
    ("tip-marks-gap", 80, 24, coached("marks", wave(17), variant="gap", pilot=sweep, shown=10), DARK),
    ("tip-shield", 120, 44, coached("shield", lambda: Game(tape().config), k=2), DARK),
    ("tip-marks", 120, 44, coached("marks", wave(18, 1), k=2, variant="beam", shown=10), DARK),
    ("too-small-menu", 50, 20, scene("menu"), (("truecolor", "dark"), ("mono", "dark"))),
]
CAPS = {"ambiguous-wide": SimpleNamespace(unicode_blocks=False)}  # the terminal, where a scene needs one
# (scene, columns, rows, build, tiers): the Arcade block at p = 2 in every tier, at p = 1 and p = 4, and the screen a
# terminal too small for p = 4 shows.
ARCADE_SCENES = (
    ("arcade-playing", 112, 64, lambda: arcade(900), TIERS),
    ("arcade-playing", 224, 128, lambda: arcade(900), DARK),
    ("arcade-playing", 56, 32, lambda: arcade(900), DARK),
    ("arcade-too-small", 80, 24, lambda: arcade(900), DARK),
)


def filename(name: str, cols: int, rows: int, tier: tuple[str, str]) -> str:
    return f"{name}-{cols}x{rows}-{tier[0]}-{tier[1]}.txt"


def describe(st: int, theme) -> str:
    """A style as 'role[/background role][+bold][+dim][+reverse]  sgr <parameters from a reset pen>'."""
    bg, attrs = st >> 8 & 255, [(bit, name, code) for (bit, name), code in zip(ATTRS, "127") if st & bit]
    roles = ROLES[st & 255] + (f"/{ROLES[bg]}" if bg else "") + "".join(f"+{name}" for _, name, _ in attrs)
    colours = [part for part in (theme.fg[st & 255], theme.bg[bg]) if part not in ("", "39", "49")]
    return f"{roles}  sgr " + ";".join(["0", *(code for _, _, code in attrs), *colours])


def text(header: str, buf: CellBuffer, theme) -> str:
    letters, legend, styles = {0: "."}, [f". = {describe(0, theme)}"], []
    for y in range(buf.rows):
        row = buf.styles[y * buf.cols : (y + 1) * buf.cols]
        for st in row:
            if st not in letters:
                letters[st] = LETTERS[len(legend) - 1]
                legend.append(f"{letters[st]} = {describe(st, theme)}")
        styles.append("".join(letters[st] for st in row))
    chars = (f"|{buf.row_text(y)}|" for y in range(buf.rows))
    return "\n".join((header, *chars, "--- styles", *(f"|{row}|" for row in styles), "--- legend", *legend)) + "\n"


def theme(tier: tuple[str, str], palette: str = "default"):
    """The theme the game resolves for a tier and theme with --theme auto: truecolor and 256 paint their own bg."""
    color, name = tier
    caps = SimpleNamespace(color=color, theme=name, paint_background=color in ("truecolor", "256"))
    return resolve_theme(caps, palette)


def palette(current: Session) -> str:
    """The session's palette; an Arcade frame keeps the default one (spec 3.1)."""
    return "default" if arcade_frame(current) else getattr(current.options, "palette", "default")


def render(current: Session, cols: int, rows: int, tier: tuple[str, str], caps=None) -> tuple[CellBuffer, int]:
    """The CellBuffer the Painter draws for a session, and the scale it used: the arena's k, or the Arcade block's p.
    Each render has a fresh flash limiter, as each Renderer does."""
    buf = CellBuffer(cols, rows)
    painter = Painter(buf, current.options, theme(tier, palette(current)), caps)
    painter.limiter = fx.FlashLimiter()
    painter.draw(current, STATS.format(k=painter.layout.k, tier=tier[0]) if current.options.show_fps else "")
    return buf, painter.arcade.p if arcade_frame(current) else painter.layout.k


def golden(current: Session, name: str, cols: int, rows: int, tier: tuple[str, str], render=render) -> str:
    buf, scale = render(current, cols, rows, tier, CAPS.get(name))
    game = current.pilot.game if current.pilot else None
    seed, tick = (game.config.seed, game.tick) if game else (current.options.seed, 0)
    scale = f"p={scale}" if arcade_frame(current) else f"k={scale}"
    header = f"# scene={name} size={cols}x{rows} {scale} tier={tier[0]} theme={tier[1]} seed={seed} tick={tick}"
    return text(f"{header} scheme={SCHEME}", buf, theme(tier, palette(current)))


def frames(render=render, arcade: bool = True):
    """(file name, golden text) for every scene in its tiers, and every Arcade scene in its tiers unless not arcade.
    Each scene's session is built once (the staged scenes share one build across their sizes)."""
    built = {}
    for name, cols, rows, build, tiers in [*SCENES, *(ARCADE_SCENES if arcade else ())]:
        if build not in built:
            built[build] = build()
        for tier in tiers:
            yield filename(name, cols, rows, tier), golden(built[build], name, cols, rows, tier, render)
