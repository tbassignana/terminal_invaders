"""Nothing Phase 5 adds changes a game (Phase 5 spec 11.3, 11.4 and 1.2).

* Pinned games: their tick, score, digest and result, pinned from the Phase 4 tree (/tmp/claude-0/p5) with this
  file's own harness, played through a real Session: 6 short ones (3 seeds x direct and latched), one long keyed
  game per scheme that crosses the first upgrade card and a life loss, and keyed Arcade games. `app.configure`'s
  source is pinned too: it is in a stream's file (app.py) but on the frozen keyed path (tools/balance.py imports it).
* Real draws with a write trap: a golden tape flown through a real Session with the Coach attached, every frame drawn
  by a Painter with the effects on, the game trapped while it is drawn and observed (a subclass whose __setattr__
  raises, its lists, dicts and sets swapped for read-only ones, and the model's mutable dataclasses refusing writes),
  and a deep snapshot of vars(game), presentation fields included, the same before and after; every tick's digest is
  a bare run's.
* Keyed play with and without a Coach (latched, timed, kitty, a first-run profile): the same actions and digests.
* The same digests whatever is shown: effects full, calm and off, every palette, tips on and off, the demo on and off.
* An AST scan of the presentation modules for randomness, clocks and writes to the game, with its own self-test; and
  the self-tests of tools/same_games.py and tools/frozen_check.py on synthetic inputs.

The module imports nothing Phase 5 adds at the top level, so that the harness (direct_game, keyed_game, arcade_game,
configure_source) also runs on the Phase 4 tree, where its pins were computed (tests/support has no new helper for
this: the pins' script imports this file with the base tree first on sys.path).
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib
import importlib.util
import inspect
import json
import pickle
import shutil
import subprocess
import sys
from dataclasses import is_dataclass
from functools import cache
from pathlib import Path
from types import GeneratorType, MappingProxyType, MethodType, SimpleNamespace

import pytest

from terminal_invaders import model
from terminal_invaders.campaign.observe import observe
from terminal_invaders.cli import defaults
from terminal_invaders.model import TICK_RATE, Config
from terminal_invaders.replay import Playback
from terminal_invaders.rules import make_game
from terminal_invaders.term.cells import CellBuffer
from terminal_invaders.ui import Painter, Session

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
FIXTURES = ROOT / "tests" / "fixtures"
TERMINAL = ("game_over", "victory")
TAPES = {
    "classic": FIXTURES / "golden-classic-normal.json",
    "endless": FIXTURES / "golden-endless-hard.json",
    "daily": FIXTURES / "golden-daily.json",
    "arcade": FIXTURES / "golden-arcade.json",
}
SIZES = {1: (80, 24), 2: (120, 44), 3: (180, 64)}  # a window whose arena scale is k
PALETTES = ("default", "cvd", "contrast", "mono")

# --- the pins (computed on the Phase 4 tree with this file's harness; see the module docstring) -------------------

# inspect.getsource(app.configure), sha-256 (spec 11.1)
CONFIGURE_SOURCE = "ecdf4ee298e0bf9ebae42684d63f8c06af25b826267ea70d5361daea15db17c0"
# (input, seed): (tick, score, digest, sha-256 of the result's JSON): the strong bot, normal, 1,800 ticks (spec 11.3)
SHORT_TICKS = 1800
SHORT = {
    ("direct", 9001): (
        1800,
        800,
        "5b3fc2401d467a783ec2f4379bb13a6532e5b5cbe522120a63d32bbf7acdb537",
        "5751ad4ad0de006ec78049768120b8062ee9f28511103707bfaa0d6cfb3bbe7d",
    ),
    ("direct", 9002): (
        1800,
        590,
        "95745bfb4d707914f038c59614c12e424c0a7fa70c47d7cefff80e57fd623713",
        "24d8a9a2c3f2157120e396eac5e0019079f129f817e892a7b39d3bdcb34797c2",
    ),
    ("direct", 9003): (
        1800,
        820,
        "5159fa0abccb4dc64b16a3dbc06ad521e9ceb31708b18c219afe1cbe0ac40afb",
        "ef38890c49063f2ba92596157947f6dcfe1a8a5a8870e1f239f1f6bd4942a015",
    ),
    ("latched", 9001): (
        1800,
        600,
        "e4be27eeb3dded4c1fe791498207e392180111f0417402a6e751a15290431507",
        "bb5715bf034fffe20aff25f7dde35f09f776274ad0ca128524bc66791f453f75",
    ),
    ("latched", 9002): (
        1800,
        800,
        "61359b7324a4d577066050b86f101b2fdf879b30f95209b641574044286a894b",
        "5751ad4ad0de006ec78049768120b8062ee9f28511103707bfaa0d6cfb3bbe7d",
    ),
    ("latched", 9003): (
        1800,
        410,
        "60b0dece3203663232515014bffa669db34056c3bf3433e3d815f2e05418c5d3",
        "95e2c65e8288546cac68e8b8937dd9294213c3396591da1700cbd602a991cd5d",
    ),
}
# One keyed game per scheme that crosses the first upgrade card and a life loss (spec 11.3), picked on the base tree
# (the first tick its card ended and its first life lost, from its own run): scheme: (bot, seed, difficulty, ticks).
# A strong bot loses no life before the card ends and a weak one dies before it, so the naive bot plays, on easy
# where normal's first life went before the card.
LONG_GAMES = {
    "latched": ("naive", 9001, "normal", 11600),  # card ends at 11,339, first life lost at 5,272
    "timed": ("naive", 9001, "normal", 11600),  # timed is keyed with taps, as latched (balance.Keyboard)
    "kitty": ("naive", 9001, "easy", 13400),  # card ends at 13,176, first life lost at 10,162
    "latched-ssh": ("naive", 9012, "easy", 12200),  # card ends at 11,995, first life lost at 5,425
}
LONG = {
    "latched": (
        11600,
        7880,
        "fb85b7c5ed1a97c70ed4d188854066e55e77ded70218145a5cf85c01d56d7569",
        "66167c376dfa86604c1506876bdd1bac342150a8780444cec7e238598015739c",
    ),
    "timed": (
        11600,
        7880,
        "fb85b7c5ed1a97c70ed4d188854066e55e77ded70218145a5cf85c01d56d7569",
        "66167c376dfa86604c1506876bdd1bac342150a8780444cec7e238598015739c",
    ),
    "kitty": (
        13400,
        5780,
        "8dfec004c072f5292fc8b3c09a2876a03aa5b3e9ffe8cbe7b47a8d500d8eadb0",
        "19fa039dc19c622237de0ab3ee8e6bd1df57a4cd377fae5d3a68b524524f38d8",
    ),
    "latched-ssh": (
        12200,
        6910,
        "281763c184100ebe3ce95b59c2ba54ae9d3b87fae86a8aa55ee83e50589bf81c",
        "7490575664c82a97afbfdd990264d1de3ce072897046c913648657a8c2c72024",
    ),
}
# Keyed Arcade (press-only fire, input.py:55-57): tools/make_golden.py's arcade policy keyed under latched controls,
# seed 9001, 3,600 ticks in the plain run and to its game over (slow).
ARCADE_SHORT_TICKS = 3600
ARCADE = {
    ("latched", 3600): (
        3600,
        670,
        "46710709c361129d067a4a244b7f5a887757e0162ba1e6a55ec43be774b9fb92",
        "f24623ba34a49b4f5bb5f0cfe3d601be9aa7f0b6e9cceef99a61b58c42fdc51d",
    ),
    ("latched", None): (
        8650,
        2080,
        "632f1655bc08311edae96bf5e621a93e672344738edecc39102d6c97f17eaae5",
        "de3c814b3ff3329ff69a8e741cb1ef8920716aa68bd4e010355661f245cf288f",
    ),
}


# --- the harness (runs on the Phase 4 tree too) -------------------------------------------------------------------


@contextlib.contextmanager
def tools_imported():
    """tools/balance.py, tools/bots and tools/make_golden.py, imported the way balance.py imports them."""
    added = [str(path) for path in (ROOT, TOOLS) if str(path) not in sys.path]
    for path in added:
        sys.path.insert(0, path)
    loaded = []
    try:
        modules = []
        for name, file in (("balance_for_invariance", "balance.py"), ("make_golden_for_invariance", "make_golden.py")):
            spec = importlib.util.spec_from_file_location(name, TOOLS / file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            loaded.append(name)
            spec.loader.exec_module(module)
            modules.append(module)
        import bots

        yield SimpleNamespace(balance=modules[0], make_golden=modules[1], bots=bots)
    finally:
        for name in loaded:
            sys.modules.pop(name, None)
        for path in added:
            sys.path.remove(path)


@pytest.fixture(scope="module")
def tools():
    with tools_imported() as found:
        yield found


def configure_source() -> str:
    from terminal_invaders import app

    return hashlib.sha256(inspect.getsource(app.configure).encode()).hexdigest()


def pin(game) -> tuple:
    """What a game is pinned by: (tick, score, digest, the sha-256 of its result as canonical JSON)."""
    result = json.dumps(game.result(), sort_keys=True, separators=(",", ":"), default=repr)
    return game.tick, game.score, game.digest(), hashlib.sha256(result.encode()).hexdigest()


def bot_session(config: Config, profile, **overrides) -> Session:
    """balance.Keyboard's Session options, for a direct game."""
    options = defaults(
        mode=config.mode, difficulty=config.difficulty, seed=config.seed, fixed_seed=True, quickstart=True,
        name="BOT", **overrides,
    )  # fmt: skip
    return Session(options, profile, profile)


def direct_game(found, seed: int, ticks: int, bot: str = "strong", difficulty: str = "normal"):
    """A bot's game through a real Session, its controls replaced by the bot (the input the bot reads is the game)."""
    session = bot_session(Config(difficulty, "classic", seed), found.balance.Offline())
    pilot = found.bots.make(bot, seed)
    session.controls.sample = lambda now: pilot.act(observe(session.pilot.game))
    game = session.pilot.game
    while game.tick < ticks and game.phase not in TERMINAL and session.scene == "playing":
        session.step((game.tick + 1) / TICK_RATE)
    return game


def keyed_game(found, mode: str, seed: int, ticks: int, bot: str = "strong", difficulty: str = "normal", each=None):
    """A bot's game keyed on balance.py's simulated keyboard through a real Session (balance.Keyboard). each: called
    with the Keyboard after every tick."""
    keyboard = found.balance.Keyboard(mode, Config(difficulty, "classic", seed))
    pilot = found.bots.make(bot, seed, keyed=mode)
    while keyboard.game.tick < ticks and keyboard.game.phase not in TERMINAL:
        keyboard.step(pilot.act(observe(keyboard.game)))
        if each is not None:
            each(keyboard)
    return keyboard.game


def arcade_game(found, mode: str, seed: int, ticks: int | None):
    """tools/make_golden.py's Arcade policy, keyed: Arcade fires on presses only (input.py:55-57)."""
    keyboard = found.balance.Keyboard(mode, Config("normal", "arcade", seed))
    while (ticks is None or keyboard.game.tick < ticks) and keyboard.game.phase not in TERMINAL:
        keyboard.step(found.make_golden.arcade_policy(keyboard.game))
    return keyboard.game


# --- spec 11.1 and 11.3: the pins ---------------------------------------------------------------------------------


def test_configure_source_is_phase_4s():
    assert configure_source() == CONFIGURE_SOURCE


@pytest.mark.parametrize("scheme,seed", sorted(SHORT))
def test_short_games_pinned(tools, scheme, seed):
    if scheme == "direct":
        game = direct_game(tools, seed, SHORT_TICKS)
    else:
        game = keyed_game(tools, scheme, seed, SHORT_TICKS)
    assert pin(game) == SHORT[scheme, seed]


@pytest.mark.slow
@pytest.mark.parametrize("scheme", sorted(LONG))
def test_long_keyed_games_pinned(tools, scheme):
    bot, seed, difficulty, ticks = LONG_GAMES[scheme]
    seen = {"card": None, "hit": None}

    def watch(keyboard):
        game = keyboard.game
        if game.phase == "upgrade" and seen["card"] is None:
            seen["card"] = game.tick
        if "damage" in game.events and seen["hit"] is None:
            seen["hit"] = game.tick

    game = keyed_game(tools, scheme, seed, ticks, bot=bot, difficulty=difficulty, each=watch)
    assert seen["card"] is not None and seen["hit"] is not None, seen  # it crossed both
    assert pin(game) == LONG[scheme]


def test_keyed_arcade_pinned(tools):
    assert pin(arcade_game(tools, "latched", 9001, ARCADE_SHORT_TICKS)) == ARCADE["latched", ARCADE_SHORT_TICKS]


@pytest.mark.slow
def test_keyed_arcade_to_game_over_pinned(tools):
    game = arcade_game(tools, "latched", 9001, None)
    assert game.phase == "game_over"
    assert pin(game) == ARCADE["latched", None]


# --- spec 11.4: the write trap ------------------------------------------------------------------------------------


class Write(AssertionError):
    """A write to the game while it was drawn or observed."""


def refuse(self, name, value=None):
    raise Write(f"{type(self).__name__}.{name} written while the game was drawn or observed")


def refuse_delete(self, name):
    raise Write(f"{type(self).__name__}.{name} deleted while the game was drawn or observed")


# The one field a view may write: Phase 3's Arcade view (arcade/view.py update(), frozen) takes the VRAM columns the
# machine marked since its last frame and leaves an empty set (arcade/memory.py's `dirty`, which the digest, a hash of
# the machine's memory, leaves out). Nothing else is allowed, and nothing at all on a Campaign game.
VIEW_OWNED = {"ArcadeGame": frozenset({"dirty"})}


def view_owned(game) -> frozenset[str]:
    return VIEW_OWNED.get(type(game).__name__, frozenset())


@cache
def trapped_class(cls: type) -> type:
    """cls with every attribute write and delete refused (the game's class while it is trapped), but VIEW_OWNED's."""
    allowed = VIEW_OWNED.get(cls.__name__, frozenset())

    def setter(self, name, value):
        if name not in allowed:
            refuse(self, name, value)
        object.__setattr__(self, name, value)

    return type(f"Trapped{cls.__name__}", (cls,), {"__setattr__": setter, "__delattr__": refuse_delete})


def model_classes() -> tuple[type, ...]:
    """The model's mutable dataclasses (Effect, Player, Alien, Shot, ...): their instances refuse writes in the trap."""
    found = []
    for value in vars(model).values():
        if isinstance(value, type) and is_dataclass(value) and not value.__dataclass_params__.frozen:
            found.append(value)
    return tuple(found)


def read_only(value):
    """A read-only stand-in for one of the game's collections, or None for any other value."""
    kind = type(value)
    if kind is list:
        return tuple(value)
    if kind is dict:
        return MappingProxyType(value)
    if kind is set:
        return frozenset(value)
    return None


def freeze(value, root, depth: int = 0):
    """A picklable image of a value: its pickle, or for what pickle cannot take (the Arcade machine's generators and
    the objects holding them) its parts, a generator by where it is suspended; the game itself (root) as a mark."""
    if value is root:
        return ("the game",)
    try:
        return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    except (TypeError, AttributeError, pickle.PicklingError, RecursionError):
        pass
    kind, depth = type(value), depth + 1
    if depth > 6:
        return (kind.__qualname__, id(value))
    if isinstance(value, GeneratorType):
        frame = value.gi_frame
        return ("generator", value.__qualname__, None if frame is None else frame.f_lasti)
    if isinstance(value, dict):
        return ("dict", [(repr(key), freeze(item, root, depth)) for key, item in value.items()])
    if isinstance(value, (list, tuple, set, frozenset)):
        return (kind.__name__, [freeze(item, root, depth) for item in value])
    if isinstance(value, MethodType):
        return ("method", value.__func__.__qualname__, freeze(value.__self__, root, depth))
    state = dict(getattr(value, "__dict__", {}))
    for cls in kind.__mro__:
        for slot in cls.__dict__.get("__slots__", ()):
            if slot not in state and hasattr(value, slot):
                state[slot] = getattr(value, slot)
    parts = [(name, freeze(item, root, depth)) for name, item in state.items()]
    return (kind.__qualname__, getattr(value, "__qualname__", None), parts)


def snapshot(game) -> bytes:
    """A deep snapshot of vars(game), presentation fields included (effects, events, notes, notice, hit_cause, fired,
    _seen, shelter_rev, which Game.digest() leaves out)."""
    allowed = view_owned(game)
    state = {name: value for name, value in vars(game).items() if name not in allowed}
    try:
        return pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
    except (TypeError, AttributeError, pickle.PicklingError):
        frozen = {name: freeze(value, game) for name, value in state.items()}
        return pickle.dumps(frozen, protocol=pickle.HIGHEST_PROTOCOL)


@contextlib.contextmanager
def trap(game, what: str = "drawn"):
    """The game, read-only, for the block: its class swapped for one whose __setattr__ raises, its lists, dicts and
    sets for tuples, mapping proxies and frozensets (the same objects back afterwards, so every alias still holds),
    the model's mutable dataclasses refusing writes; and vars(game)'s snapshot must be the same after."""
    before = snapshot(game)
    cls, state, allowed = type(game), vars(game), view_owned(game)
    swapped = {}
    for name, value in list(state.items()):
        stand_in = None if name in allowed else read_only(value)
        if stand_in is not None:
            swapped[name] = value
            state[name] = stand_in
    classes = [(each, each.__dict__.get("__setattr__"), each.__dict__.get("__delattr__")) for each in model_classes()]
    game.__class__ = trapped_class(cls)
    for each, _set, _delete in classes:
        each.__setattr__, each.__delattr__ = refuse, refuse_delete
    try:
        yield
    finally:
        for each, setter, deleter in classes:
            for name, original in (("__setattr__", setter), ("__delattr__", deleter)):
                if original is None:
                    delattr(each, name)
                else:
                    setattr(each, name, original)
        object.__setattr__(game, "__class__", cls)
        state.update(swapped)
    after = snapshot(game)
    if after != before:
        raise Write(f"vars(game) changed while it was {what}: {changed_fields(before, after)}")


def changed_fields(before: bytes, after: bytes) -> list[str]:
    old, new = pickle.loads(before), pickle.loads(after)
    return sorted(
        name for name in old.keys() | new.keys() if pickle.dumps(old.get(name)) != pickle.dumps(new.get(name))
    )


# --- spec 11.4: real draws, real observer -------------------------------------------------------------------------


class TipsProfile:
    """frames.py's stub profile with what a Phase 5 Session may write after a run the Coach taught (spec 7.1): the
    settings it reads and an update_settings() that keeps nothing; no placing, so no initials card."""

    alert = error = None
    last_placing = None

    def __init__(self):
        self.data = {"name": "ACE", "scores": [], "stats": {}, "achievements": []}
        self.settings = {}
        self.writes = []

    def best(self, config):
        return 15000

    def old_boards(self):
        return {}

    def record(self, result, config, name, **_keywords):
        return []

    def update_settings(self, changes, name=None):
        self.writes.append(dict(changes))
        return True

    def rank(self, placing):
        return None


class Quiet:
    enabled = False

    def update(self, events=()):
        pass

    def toggle(self):
        return False


@cache
def tape(name: str):
    return Playback(TAPES[name]).tapes[0]


@cache
def bare(name: str, ticks: int | None = None) -> tuple[str, ...]:
    """Every tick's digest of the tape's game stepped bare: no Session, no Painter, no Coach."""
    recorded = tape(name)
    game, digests = make_game(recorded.config), []
    end = recorded.ticks if ticks is None else min(ticks, recorded.ticks)
    while game.tick < end and game.phase not in TERMINAL:
        game.step(recorded.actions_at(game.tick))
        digests.append(game.digest())
    return tuple(digests)


@cache
def key_ticks(name: str) -> frozenset[int]:
    """The slow run's drawn ticks: every 60th, and every one within 30 of a life lost, a boss phase break or kill."""
    recorded = tape(name)
    game, moments = make_game(recorded.config), []
    while game.tick < recorded.ticks and game.phase not in TERMINAL:
        game.step(recorded.actions_at(game.tick))
        if {"damage", "phase", "boss", "blast"} & set(game.events):  # blast: an Arcade cannon lost
            moments.append(game.tick)
    ticks = {tick for tick in range(recorded.ticks + 1) if tick % 60 == 0}
    ticks |= {tick + offset for tick in moments for offset in range(-30, 31)}
    return frozenset(ticks)


def theme_for(palette: str):
    from terminal_invaders.theme import resolve_theme

    tier = "mono" if palette == "mono" else "truecolor"
    return resolve_theme(SimpleNamespace(color=tier, theme="dark", paint_background=True), palette)


def painter_for(options, k: int, palette: str) -> Painter:
    """A Painter at scale k in a palette, with the link and the flash limiter the Renderer gives it (spec 14.3)."""
    from terminal_invaders import fx

    painter = Painter(CellBuffer(*SIZES[k]), options, theme_for(palette))
    painter.limiter, painter.link = fx.FlashLimiter(), fx.Link()
    assert painter.layout.k == k
    return painter


class Flight:
    """A golden tape flown through a real (not a replay) Session: the tape's actions are what its controls produce,
    so the Session's own path runs (recorder, controls.fired(), audio, the Coach, follow_phase, the results)."""

    def __init__(self, name: str, *, tips: bool = True, before=None, **overrides):
        self.recorded = tape(name)
        config = self.recorded.config
        options = defaults(
            mode=config.mode, difficulty=config.difficulty, seed=config.seed, fixed_seed=True, quickstart=False,
            name="ACE", tips="always" if tips else "off", **overrides,
        )  # fmt: skip
        self.profile = TipsProfile()
        self.session = session = Session(options, self.profile, Quiet())
        session.configuration = lambda seed=None: config  # a daily tape keeps its own day
        if tips:
            session.enable_tips()
        self.before = before(session) if before is not None else None  # on the title, before the run starts
        session.start()
        self.coach = session.coach  # attached by the Session itself (start(), spec 7.1), or None: never by hand (TL-8)
        self.observed = 0
        self.actions = ()
        self.trapping = True  # the observe in the trap (False: ticks another test traps, flown to reach its start)
        if self.coach is not None:
            observe_tick = self.coach.observe

            def observed(game, actions, events):
                self.observed += 1
                if not self.trapping:
                    return observe_tick(game, actions, events)
                with trap(game, "observed"):
                    observe_tick(game, actions, events)

            self.coach.observe = observed
        session.controls.sample = self.sample

    def sample(self, now):
        self.actions = self.recorded.actions_at(self.session.pilot.game.tick)
        return self.actions

    @property
    def game(self):
        return self.session.pilot.game

    def step(self) -> bool:
        """One tick; False once the tape or the game is over."""
        session, game = self.session, self.game
        if session.scene != "playing" or game.tick >= self.recorded.ticks or game.phase in TERMINAL:
            return False
        seen = self.observed
        session.step((game.tick + 1) / TICK_RATE)
        if self.coach is not None:  # the Session showed its Coach the tick it stepped (TL-8: nobody does it for it)
            assert self.observed == seen + 1, f"tick {game.tick}: Session.step did not show the Coach the tick"
        return True


def draw(painter: Painter, flight: Flight) -> None:
    game = flight.game
    with trap(game):
        painter.draw(flight.session, "")


def fly_drawn(name: str, ticks: int, painters, drawn=None, **flight) -> Flight:
    """Fly the tape for `ticks` ticks (None: to its end), drawing each painter after the ticks `drawn` holds (None:
    every tick), the game trapped; every tick's digest must be the bare run's."""
    run = Flight(name, **flight)
    digests = bare(name)
    while (ticks is None or run.game.tick < ticks) and run.step():
        tick = run.game.tick
        assert run.game.digest() == digests[tick - 1], f"{name}: tick {tick} differs from the bare run"
        if drawn is None or tick in drawn:
            for painter in painters:
                draw(painter, run)
    return run


DRAWN, PART = 3600, 1200  # spec 11.4's plain run: the classic tape's first 3,600 ticks, in three parts


@pytest.mark.parametrize("k", [1, 2])
@pytest.mark.parametrize("start", range(0, DRAWN, PART))
def test_real_draws_change_nothing(k, start):
    """Spec 11.4's plain run: every frame of the classic tape's first 3,600 ticks (in three parts) at k = 1 and 2, in
    default and cvd, with the effects on and the Coach attached. A scale's two Painters draw each tick of one flight
    (each draws what it draws alone, and the Coach does the same: one scale, so its tips' scale rule holds); the
    ticks before a part are flown again, untrapped, to reach it (the part they belong to traps them)."""
    run = Flight("classic")
    painters = [painter_for(run.session.options, k, palette) for palette in ("default", "cvd")]
    digests = bare("classic", DRAWN)
    run.trapping = False
    while run.game.tick < start and run.step():
        pass
    run.trapping = True
    while run.game.tick < start + PART and run.step():
        assert run.game.digest() == digests[run.game.tick - 1]
        for painter in painters:
            draw(painter, run)
    assert run.game.tick == start + PART
    assert run.coach is not None and run.observed >= PART


@pytest.mark.slow
@pytest.mark.parametrize("name", sorted(TAPES))
def test_real_draws_change_nothing_every_tape(name):
    """Spec 11.4's full run: every tape to its end, every 60th tick and every tick within 30 of a life lost, a phase
    break or a boss kill, in every palette at k = 1-3, then the results card (the Arcade tape in its own block)."""
    options = Flight(name).session.options
    painters = [painter_for(options, k, palette) for k in SIZES for palette in PALETTES]
    run = fly_drawn(name, None, painters, key_ticks(name))
    assert run.game.phase in TERMINAL or run.game.tick == run.recorded.ticks
    if run.session.scene == "results":
        for painter in painters:
            draw(painter, run)
    if name != "arcade":
        assert run.coach is not None and run.observed == run.game.tick


def test_the_coach_is_attached_by_the_session_when_tips_are_on():
    """With TIPS `always` and enable_tips(), tutorial.wanted() says yes and Session.start() attaches the Coach itself
    (spec 7.1); the other tests' Flights rely on it, since nothing attaches one by hand (TL-8)."""
    from terminal_invaders import tutorial

    run = Flight("classic")
    assert tutorial.wanted(run.session.options, run.profile, 1, run.game.config)
    assert run.coach is not None and run.coach is run.session.coach


# --- the trap's own test ------------------------------------------------------------------------------------------


def mutants():
    """Draws that write the game, each in a way the trap must stop: (name, the write)."""

    def score(game):
        game.score += 1

    def alias_list(game):
        shots = game.shots
        shots.append(None)

    def effect(game):
        game.effects[0].ttl -= 1

    def nested(game):
        game.player.x += 1

    def mapping(game):
        game.upgrades["mutant"] = 1

    def hidden(game):
        game._fire.__dict__["mutant"] = 1 if hasattr(game._fire, "__dict__") else None
        vars(game)["notice"] = "MUTANT"  # around __setattr__: only the snapshot sees it

    def delete(game):
        del game.notice

    return [score, alias_list, effect, nested, mapping, hidden, delete]


@pytest.mark.parametrize("write", mutants(), ids=lambda write: write.__name__)
def test_the_trap_stops_every_kind_of_write(write):
    run = Flight("classic", tips=False)
    while run.step() and not run.game.effects:
        pass
    game = run.game
    assert game.effects and game.shots is not None
    before = snapshot(game)
    with pytest.raises((Write, AttributeError, TypeError)):
        with trap(game):
            write(game)
    if write.__name__ != "hidden":
        assert snapshot(game) == before  # stopped before it wrote
    assert type(game).__name__ == "Game" and type(game.shots) is list  # and the game is itself again
    for each in model_classes():
        assert "__setattr__" not in each.__dict__ or each.__dict__["__setattr__"] is not refuse


# --- spec 11.4: keyed play with and without a Coach ---------------------------------------------------------------


@pytest.fixture
def first_run(tmp_path):
    """A fresh profile file in tmp_path (no settings yet, so TIPS auto teaches its first run)."""
    from terminal_invaders.storage import Profile

    count = iter(range(100))
    return lambda: Profile(tmp_path / f"profile-{next(count)}.json")


def keyed_with_coach(found, mode: str, seed: int, ticks: int, profile, coach: bool, bot="strong", difficulty="normal"):
    """balance.Keyboard's keyed game on a real profile, the run started again after enable_tips() (coach) or without
    it; every tick's (actions, digest) and whether a Coach watched."""
    balance = found.balance
    made = []

    def session(options, _profile, audio, replay=None):
        made.append(Session(options, profile, audio, replay))
        return made[-1]

    keep, balance.Session = balance.Session, session
    try:
        keyboard = balance.Keyboard(mode, Config(difficulty, "classic", seed))
    finally:
        balance.Session = keep
    session = keyboard.session
    assert session is made[0]
    if coach:
        session.enable_tips()
    session.start()  # the quickstart run again, now that tips may be on (app.play's order: Session, then tips)
    log, called = [], []  # the Coach is the Session's own, or none: never attached by hand (TL-8)
    sample = session.controls.sample

    def sampled(now):
        log.append(sample(now))
        return log[-1]

    session.controls.sample = sampled
    if session.coach is not None:
        observe_tick = session.coach.observe

        def observed(game, actions, events):
            called.append(game.tick)
            with trap(game, "observed"):
                observe_tick(game, actions, events)

        session.coach.observe = observed
    pilot = found.bots.make(bot, seed, keyed=mode)
    out = []
    while keyboard.game.tick < ticks and keyboard.game.phase not in TERMINAL:
        game, seen = keyboard.game, len(called)
        keyboard.step(pilot.act(observe(game)))  # one Session.step, one tick
        if session.coach is not None:  # the Session showed its Coach the tick (TL-8: nobody does it for it)
            assert len(called) == seen + 1, f"tick {game.tick}: Session.step did not show the Coach the tick"
        out.append((log[-1], game.digest()))
    return out, session.coach is not None


@pytest.mark.parametrize("mode", ["latched", "timed", "kitty"])
def test_keyed_play_is_the_same_with_and_without_a_coach(tools, first_run, mode):
    without, watched = keyed_with_coach(tools, mode, 9001, 2400, first_run(), coach=False)
    assert not watched
    with_coach, watched = keyed_with_coach(tools, mode, 9001, 2400, first_run(), coach=True)
    assert watched
    assert len(with_coach) == len(without) == 2400
    for tick, (one, other) in enumerate(zip(without, with_coach), 1):
        assert one == other, f"{mode}: tick {tick}"


@pytest.mark.slow
@pytest.mark.parametrize("scheme", sorted(LONG))
def test_long_keyed_games_with_a_coach_keep_their_pins(tools, first_run, scheme):
    bot, seed, difficulty, ticks = LONG_GAMES[scheme]
    out, watched = keyed_with_coach(tools, scheme, seed, ticks, first_run(), coach=True, bot=bot, difficulty=difficulty)
    assert watched
    assert out[-1][1] == LONG[scheme][2]


# --- spec 11.4: the same digests whatever is shown ----------------------------------------------------------------

SHOWN_TICKS = 1200
SHOWN = [  # each setting's every value; the defaults' run (effects full, palette default, no tips, no demo) once
    dict(effects="full", palette="default", tips=False, demo=False),
    *(dict(effects=level) for level in ("calm", "off")),
    *(dict(palette=palette) for palette in PALETTES if palette != "default"),
    dict(tips=True),
    dict(demo=True),
    dict(reduced_motion=True),
]


def play_demo(menu: Session, ticks: int = 600) -> int:
    """A title demo before the menu's game: the Session's own, the nested replay Session of spec 5.1
    (update_attract on the loop's clock, with INVADERS_ATTRACT_IDLE=0, as the app loop runs it), each frame drawn.
    Returns the ticks it played; nothing stands in for a Session that plays none (TL-8)."""
    painter = painter_for(menu.options, 2, "default")
    now, played = 0.0, 0
    while played < ticks and now < 30.0:
        now += 1 / TICK_RATE
        menu.update_attract(now)
        shown = menu.attract
        if shown is not None and shown.ticking:
            shown.step(now)
            played += 1
            with trap(shown.pilot.game):
                painter.draw(shown, "")
    return played


@pytest.mark.parametrize("shown", SHOWN, ids=lambda shown: "-".join(f"{key}={value}" for key, value in shown.items()))
def test_the_same_digests_whatever_is_shown(shown, monkeypatch):
    shown = dict(shown)
    palette, tips, demo = shown.pop("palette", "default"), shown.pop("tips", False), shown.pop("demo", False)
    if demo:
        monkeypatch.setenv("INVADERS_ATTRACT_IDLE", "0")
        shown["attract"] = "on"
    run = Flight("classic", tips=tips, palette=palette, before=play_demo if demo else None, **shown)
    if demo:
        assert run.before == 600, f"the Session's title demo played {run.before} ticks before the run, not 600"
    painter = painter_for(run.session.options, 2, palette)
    digests = bare("classic", DRAWN)  # the real draws' bare run, whose first SHOWN_TICKS are these
    while run.game.tick < SHOWN_TICKS and run.step():
        assert run.game.digest() == digests[run.game.tick - 1], f"{shown}: tick {run.game.tick}"
        draw(painter, run)
    assert run.game.tick == SHOWN_TICKS
    assert (run.coach is not None) == tips


# --- spec 11.4: the AST scan --------------------------------------------------------------------------------------

PACKAGE = ROOT / "terminal_invaders"
SCANNED = ("fx.py", "arena.py", "screens.py", "tutorial.py", "attract.py")  # whole modules
# ui.py: the Painter's methods and the module's functions it draws with (Session, the clock and the hooks are not
# presentation: Session.step is the one place a game is stepped)
UI_SKIPPED = ("Session", "FixedClock", "NoHooks", "Pilot", "Fits")
FORBIDDEN = frozenset({"random", "secrets", "time", "rng", "rng_drop", "rng_offer"})
DYNAMIC = frozenset({"__import__", "importlib", "eval", "exec"})  # a way round the names above
RNG_ATTRIBUTES = frozenset({"rng", "rng_drop", "rng_offer"})
# attributes that reach randomness or the clock through another module (ui.secrets, os.urandom, ...)
RANDOM_ATTRIBUTES = frozenset({"random", "secrets", "time", "urandom", "randbits", "getrandbits", "monotonic"})
RNG_ALLOWED = frozenset({"splitmix64", "MASK64"})  # rng.py's pure functions: fx.mix folds into them (spec 1.2)
MUTATORS = frozenset(
    {"append", "pop", "sort", "clear", "remove", "insert", "extend", "update", "setdefault"}  # spec 1.2
    | {"add", "discard", "popitem", "reverse", "__setitem__", "__delitem__", "__setattr__", "__delattr__"}
)
ROOTS = frozenset({"game", "effect"})  # and any `.game` attribute (session.pilot.game, self.pilot.game)
GAME_METHODS = frozenset({"result", "digest"})
COPIES = frozenset({"getattr", "next", "min", "max", "iter", "reversed", "enumerate", "zip", "sorted", "filter"})


class Scanner:
    """One function's (or module's) names bound from the game, then its reads of randomness and its writes."""

    def __init__(self, where: str):
        self.where, self.findings = where, []

    def report(self, node, what: str) -> None:
        self.findings.append(f"{self.where}:{getattr(node, 'lineno', 0)}: {what}")

    # what is rooted at the game
    def rooted(self, node, tainted, games=None) -> bool:
        """Whether node names the game, part of it, or an Effect: a chain of attributes and subscripts down to a
        tainted name or through a `.game` attribute (games: the names that are the game itself, for `.game`)."""
        while isinstance(node, (ast.Attribute, ast.Subscript, ast.Starred)):
            if isinstance(node, ast.Attribute) and node.attr == "game":
                return True
            node = node.value
        if isinstance(node, ast.Name):
            return node.id in tainted
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in COPIES:
            return any(self.rooted(arg, tainted) for arg in node.args)
        if isinstance(node, ast.IfExp):
            return self.rooted(node.body, tainted) or self.rooted(node.orelse, tainted)
        if isinstance(node, ast.BoolOp):
            return any(self.rooted(value, tainted) for value in node.values)
        if isinstance(node, ast.NamedExpr):
            return self.rooted(node.value, tainted)
        return False

    @classmethod
    def names(cls, target) -> list[str]:
        """The names a target binds (not those it only reads, as `self` in `self.x = ...`)."""
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, ast.Starred):
            return cls.names(target.value)
        if isinstance(target, (ast.Tuple, ast.List)):
            return [name for element in target.elts for name in cls.names(element)]
        return []

    def bindings(self, body: ast.AST) -> tuple[set[str], set[str]]:
        """(the names bound from the game or an effect, the names that are the game itself), to a fixpoint."""
        tainted, games = set(ROOTS), {"game"}
        while True:
            size = len(tainted), len(games)
            for node in ast.walk(body):
                pairs = []
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                            pairs += zip(target.elts, node.value.elts)
                        else:
                            pairs.append((target, node.value))
                elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                    pairs.append((node.target, node.value))
                elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                    pairs.append((node.target, node.iter))
                elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                    pairs.append((node.optional_vars, node.context_expr))
                for target, value in pairs:
                    if self.rooted(value, tainted):
                        tainted.update(self.names(target))
                        if self.is_game(value, games) and isinstance(target, ast.Name):
                            games.add(target.id)
            if (len(tainted), len(games)) == size:
                return tainted, games

    @staticmethod
    def is_game(node, games) -> bool:
        return (isinstance(node, ast.Name) and node.id in games) or (
            isinstance(node, ast.Attribute) and node.attr == "game"
        )

    def scan(self, body: ast.AST) -> None:
        tainted, games = self.bindings(body)
        for node in ast.walk(body):
            self.randomness(node)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
                targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
                for target in targets:
                    for part in ast.walk(target) if isinstance(target, (ast.Tuple, ast.List)) else [target]:
                        if isinstance(part, (ast.Attribute, ast.Subscript)) and self.rooted(part.value, tainted):
                            self.report(node, f"{type(node).__name__.lower()} to {ast.unparse(part)}")
                        elif (
                            isinstance(node, ast.AugAssign) and isinstance(part, ast.Name) and part.id in tainted
                            and part.id not in ROOTS | games
                        ):  # fmt: skip
                            self.report(node, f"in-place {ast.unparse(node).strip()} on a name bound from the game")
                        elif isinstance(part, ast.Name) and part.id in games and not isinstance(node, ast.Delete):
                            pass  # rebinding a local name is no write
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in ("setattr", "delattr") and node.args:
                    if self.rooted(node.args[0], tainted):
                        self.report(node, f"{func.id}() on {ast.unparse(node.args[0])}")
                elif isinstance(func, ast.Attribute):
                    if func.attr in ("__setattr__", "__delattr__") and node.args and self.rooted(node.args[0], tainted):
                        self.report(node, f"{ast.unparse(func)}() on {ast.unparse(node.args[0])}")
                    elif func.attr in MUTATORS and self.rooted(func.value, tainted):
                        self.report(node, f"mutating call {ast.unparse(func)}()")
                    elif self.is_game(func.value, games) and func.attr not in GAME_METHODS:
                        self.report(node, f"Game method call {ast.unparse(func)}()")

    def randomness(self, node) -> None:
        if isinstance(node, ast.Name) and node.id in FORBIDDEN | DYNAMIC:
            self.report(node, f"the name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in RNG_ATTRIBUTES | RANDOM_ATTRIBUTES:
            self.report(node, f"the attribute .{node.attr}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if {alias.name.split(".")[0], alias.asname} & (FORBIDDEN | DYNAMIC) or alias.name.endswith(".rng"):
                    self.report(node, f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[-1]
            top = (node.module or "").split(".")[0]
            if module in (FORBIDDEN | DYNAMIC) - {"rng"} or top in (FORBIDDEN | DYNAMIC) - {"rng"}:
                self.report(node, f"from {node.module} import ...")
            elif module == "rng":
                for alias in node.names:
                    if alias.name not in RNG_ALLOWED:
                        self.report(node, f"from {node.module} import {alias.name}")
            for alias in node.names:
                if alias.name in FORBIDDEN or alias.asname in FORBIDDEN:
                    self.report(node, f"import of the name {alias.asname or alias.name}")


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def scan_source(source: str, where: str, *, ui: bool = False) -> list[str]:
    """The scan's findings for one module's source, one function (or method, with the functions inside it) at a
    time, and the module's own statements as one more (ui: only the Painter's methods and the module's functions)."""
    tree = ast.parse(source)
    scanner = Scanner(where)
    rest = []
    for node in tree.body:
        if isinstance(node, FUNCTIONS):
            scanner.scan(node)
        elif isinstance(node, ast.ClassDef):
            if ui and node.name in UI_SKIPPED:
                continue
            for member in node.body:
                if isinstance(member, FUNCTIONS):
                    scanner.scan(member)
                elif not ui:
                    rest.append(member)
        elif not ui:
            rest.append(node)
    if rest:
        scanner.scan(ast.Module(body=rest, type_ignores=[]))
    return scanner.findings


def scanned_files() -> list[tuple[Path, bool]]:
    return [*((PACKAGE / name, False) for name in SCANNED), (PACKAGE / "ui.py", True)]


def test_presentation_reads_no_randomness_and_writes_no_game():
    findings = []
    for path, ui in scanned_files():
        assert path.is_file(), path
        findings += scan_source(path.read_text(encoding="utf-8"), path.name, ui=ui)
    assert findings == []


def test_the_scan_scans_the_painter():
    tree = ast.parse((PACKAGE / "ui.py").read_text(encoding="utf-8"))
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    assert "Painter" in classes and "Painter" not in UI_SKIPPED


SCAN_MUTANTS = {
    "import random": "import random\n",
    "from time import monotonic": "from time import monotonic\n",
    "import secrets as s": "import secrets as s\n",
    "from .rng import Rng": "from .rng import Rng\n",
    "game.rng": "def f(game):\n    return game.rng.random()\n",
    "rng_drop": "def f(g):\n    return g.rng_drop\n",
    "time name": "def f():\n    return time\n",
    "assign": "def f(game):\n    game.score = 1\n",
    "augassign": "def f(game):\n    game.score += 1\n",
    "item": "def f(game):\n    game.upgrades['x'] = 1\n",
    "del": "def f(game):\n    del game.notice\n",
    "setattr": "def f(game):\n    setattr(game, 'notice', '')\n",
    "object setattr": "def f(game):\n    object.__setattr__(game.player, 'x', 1)\n",
    "append": "def f(game):\n    game.events.append('x')\n",
    "alias append": "def f(game):\n    shots = game.shots\n    shots.append(1)\n",
    "loop alias": "def f(game):\n    for alien in game.aliens:\n        alien.hp = 0\n",
    "enumerate alias": "def f(game):\n    for i, alien in enumerate(game.aliens):\n        alien.x += 1\n",
    "comprehension": "def f(game):\n    [shot.__setattr__('y', 0) for shot in game.shots]\n",
    "effect": "def f(effect):\n    effect.ttl -= 1\n",
    "session game": "def f(session):\n    session.pilot.game.phase = 'x'\n",
    "self game": "def f(self):\n    self.pilot.game.effects.clear()\n",
    "game alias": "def f(session):\n    g = session.pilot.game\n    g.notes.sort()\n",
    "step": "def f(session):\n    session.pilot.game.step(())\n",
    "private method": "def f(game):\n    game._effect(0, 0, 'x', 1)\n",
    "walrus": "def f(game):\n    if (p := game.player):\n        p.lives = 9\n",
    "in place alias": "def f(game):\n    shots = game.shots\n    shots += [1]\n",
    "tuple target": "def f(game):\n    a, game.score = 1, 2\n",
    "pair alias": "def f(game):\n    a, b = game.player, 1\n    a.x = 0\n",
    "dunder import": "def f():\n    return __import__('random').random()\n",
    "importlib": "import importlib\n",
    "from importlib": "from importlib import import_module\n",
    "eval": "def f():\n    return eval('1')\n",
    "module secrets": "def f(ui):\n    return ui.secrets.randbits(8)\n",
    "os urandom": "def f(os):\n    return os.urandom(1)\n",
    "clock attribute": "def f(clock):\n    return clock.monotonic()\n",
}
SCAN_CLEAN = {
    "reads": "def f(game):\n    return game.score, game.result(), game.digest(), len(game.shots)\n",
    "allowed rng": "from .rng import splitmix64, MASK64\n",
    "own list": "def f(game):\n    rows = [shot.x for shot in game.shots]\n    rows.append(1)\n    return rows\n",
    "own copy": "def f(game):\n    shots = list(game.shots)\n    shots.sort()\n",
    "rebinding": "def f(game):\n    game = None\n    x = 1\n    x += 2\n",
    "own state": "def f(self, game):\n    self.seen = game.shelter_rev\n    self.done.add('x')\n",
    "result copy": "def f(game):\n    result = game.result()\n",
}


@pytest.mark.parametrize("name", sorted(SCAN_MUTANTS))
def test_the_scan_finds_each_planted_construct(name):
    assert scan_source(SCAN_MUTANTS[name], name), name


@pytest.mark.parametrize("name", sorted(SCAN_CLEAN))
def test_the_scan_passes_plain_reads(name):
    assert scan_source(SCAN_CLEAN[name], name) == []


def test_the_scan_skips_only_the_session_side_of_ui():
    source = "class Session:\n    def step(self):\n        self.pilot.game.step(())\n"
    source += "class Painter:\n    def draw(self, session):\n        session.pilot.game.score = 0\n"
    source += "def helper(game):\n    game.notes.append(1)\n"
    found = scan_source(source, "ui.py", ui=True)
    assert len(found) == 2 and ":6:" in found[0] and ":8:" in found[1], found


# --- the self-tests of tools/same_games.py and tools/frozen_check.py ---------------------------------------------


def run_tool(name: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(TOOLS / name), *map(str, args)], capture_output=True, text=True, timeout=120
    )


def rows_dir(path: Path, rows: list, extra: str = "") -> Path:
    path.mkdir()
    (path / "shard-0.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows) + extra)
    (path / "audit-0.jsonl").write_text("not a game row\n")  # audit files are not games
    return path


def game_rows() -> list[dict]:
    rows = []
    for number, (mode, seed) in enumerate([("kitty", 9000), ("latched", 9000), ("timed", 9001)]):
        rows.append(
            dict(job=f"strong/normal/{mode}/{seed}", score=1000 + number, wave=3, digest=f"{number:064x}",
                 lives=[1, 2], wall=1.5 + number, build="abc", profile={"engine": 0.1})
        )  # fmt: skip
    return rows


def test_same_games_passes_the_same_games_with_other_timings(tmp_path):
    base = rows_dir(tmp_path / "base", game_rows())
    timed = [row | dict(wall=99.0, build="def", profile={"engine": 9.0}) for row in reversed(game_rows())]
    new = rows_dir(tmp_path / "new", timed)
    out = run_tool("same_games.py", base, new, "--expect", 3)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "ignored fields: wall, build, profile" in out.stdout and "OK" in out.stdout


@pytest.mark.parametrize("change", ["score", "removed", "error", "nested", "duplicate", "count", "garbage"])
def test_same_games_fails_on_each_planted_change(tmp_path, change):
    base, rows, extra = rows_dir(tmp_path / "base", game_rows()), game_rows(), ""
    if change == "score":
        rows[1]["score"] += 10
    elif change == "removed":
        del rows[2]
    elif change == "error":
        rows[0] = dict(job=rows[0]["job"], error="Traceback ...")
    elif change == "nested":
        rows[2]["lives"][1] = 3
    elif change == "duplicate":
        rows.append(dict(rows[0]))
    elif change == "garbage":
        extra = "{not json\n"
    new = rows_dir(tmp_path / "new", rows, extra)
    out = run_tool("same_games.py", base, new, *(("--expect", 4) if change == "count" else ()))
    assert out.returncode == 1, out.stdout
    expected = {
        "score": "differs: strong/normal/latched/9000 .score: 1001 != 1011",
        "removed": "missing from NEW: strong/normal/timed/9001",
        "error": "is an error row: Traceback",
        "nested": ".lives[1]: 2 != 3",
        "duplicate": "a second row for strong/normal/kitty/9000",
        "count": "BASE holds 3 games, not 4",
        "garbage": "not JSON",
    }[change]
    assert expected in out.stdout


def synthetic_tree(root: Path) -> tuple[Path, Path, int]:
    """A small tree with its sha list and owned list: every spec 14.2 path, a few frozen files and two Campaign
    goldens (which the check leaves out). Returns (sha list, owned list, frozen count)."""
    sys.path.insert(0, str(TOOLS))
    try:
        import frozen_check
    finally:
        sys.path.remove(str(TOOLS))
    files = {path: f"owned {path}\n" for path in sorted(frozen_check.OWNED_PATHS)}
    frozen = {
        "terminal_invaders/engine.py": "engine\n",
        "terminal_invaders/campaign/game.py": "game\n",
        "tests/fixtures/golden-classic-normal.json": "{}\n",
        "tests/golden/arcade-menu-80x24-mono-dark.txt": "arcade frame\n",
    }
    goldens = {"tests/golden/menu-80x24-mono-dark.txt": "menu\n", "tests/golden/playing-80x24-16-dark.txt": "play\n"}
    lines = []
    for path, text in (files | frozen | goldens).items():
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        lines.append(f"{hashlib.sha256(text.encode()).hexdigest()}  ./{path}\n")
    sha = root.parent / "base-sha.txt"
    sha.write_text("".join(lines))
    owned = root.parent / "owned.txt"
    owned.write_text("# spec 14.2\n" + "".join(f"{path}\n" for path in files))
    return sha, owned, len(frozen)


def test_frozen_check_passes_an_unchanged_tree(tmp_path):
    sha, owned, count = synthetic_tree(tmp_path / "tree")
    (tmp_path / "tree" / "terminal_invaders" / "ui.py").write_text("an owned file may change\n")
    (tmp_path / "tree" / "tests" / "golden" / "menu-80x24-mono-dark.txt").write_text("a golden may change\n")
    (tmp_path / "tree" / "terminal_invaders" / "fx.py").write_text("a new file outside the frozen folders\n")
    cache_dir = tmp_path / "tree" / "terminal_invaders" / "campaign" / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "game.cpython-312.pyc").write_bytes(b"cache")
    out = run_tool("frozen_check.py", sha, owned, "--root", tmp_path / "tree", "--expect", count)
    assert out.returncode == 0, out.stdout
    assert f"= {count} frozen files" in out.stdout and "OK" in out.stdout
    assert "terminal_invaders/engine.py" in out.stdout.splitlines()


@pytest.mark.parametrize("change", ["changed", "missing", "added", "count", "owned-extra", "owned-missing", "twice"])
def test_frozen_check_fails_on_each_planted_change(tmp_path, change):
    sha, owned, count = synthetic_tree(tmp_path / "tree")
    tree = tmp_path / "tree"
    lines = owned.read_text().splitlines()
    if change == "changed":
        (tree / "terminal_invaders" / "engine.py").write_text("engine, changed\n")
    elif change == "missing":
        (tree / "tests" / "golden" / "arcade-menu-80x24-mono-dark.txt").unlink()
    elif change == "added":
        (tree / "tests" / "fixtures" / "new.json").write_text("{}\n")
    elif change == "count":
        count += 1
    elif change == "owned-extra":
        owned.write_text("\n".join([*lines, "terminal_invaders/engine.py"]) + "\n")
    elif change == "owned-missing":
        owned.write_text("\n".join(line for line in lines if line != "Makefile") + "\n")
    elif change == "twice":
        owned.write_text("\n".join([*lines, "Makefile"]) + "\n")
    out = run_tool("frozen_check.py", sha, owned, "--root", tree, "--expect", count, "--quiet")
    assert out.returncode == 1, out.stdout
    expected = {
        "changed": "FAIL changed: terminal_invaders/engine.py",
        "missing": "FAIL missing: tests/golden/arcade-menu-80x24-mono-dark.txt",
        "added": "FAIL not in the base list: tests/fixtures/new.json",
        "count": f"frozen files, not {count}",
        "owned-extra": "owned but not a spec 14.2 path: terminal_invaders/engine.py",
        "owned-missing": "a spec 14.2 path missing from the owned list: Makefile",
        "twice": "owned twice: Makefile",
    }[change]
    assert expected in out.stdout


# --- spec 11.3: the real loop (tools/same_loop.py), against itself -----------------------------------------------


@pytest.mark.slow
def test_same_loop_gives_the_same_tapes_and_digests_twice(tmp_path):
    """The same game twice through app.play on the FakeTerminal (one fixture): the tool's own repeatability, which
    its base-against-new use (G5 rows) relies on; a planted difference in the copy must fail the comparison."""
    one, two = tmp_path / "one", tmp_path / "two"
    for out in (one, two):
        done = subprocess.run(
            [sys.executable, "-B", str(TOOLS / "same_loop.py"), str(ROOT), str(out), "--fixtures", "kitty", "--cap", "40"],
            capture_output=True, text=True, timeout=600,
        )  # fmt: skip
        assert done.returncode == 0, done.stdout + done.stderr
    same = run_tool("same_loop.py", "--compare", one, two)
    assert same.returncode == 0, same.stdout
    planted = tmp_path / "planted"
    shutil.copytree(two, planted)
    record = next(planted.glob("*.json"))
    data = json.loads(record.read_text())
    data["digests"][len(data["digests"]) // 2][1] = "0" * 64
    record.write_text(json.dumps(data))
    differs = run_tool("same_loop.py", "--compare", one, planted)
    assert differs.returncode == 1 and "differ" in differs.stdout


# what a Phase 5 tree's run --demo --settings reports when every part ran (as same_loop's child writes it)
RAN = dict(coach_observed=600, demo_shown=True, applied=["palette"])
RAN["settings"] = "asked: 5 rows down and back; back on LAUNCH; saved palette cvd"


@pytest.mark.parametrize(
    "base, change, fails",
    [
        (False, {}, None),
        (False, {"coach_observed": 0}, "the Coach observed 0 ticks"),
        (False, {"demo_shown": False}, "--demo: no title demo played"),
        (False, {"settings": "asked: 5 rows down and back; back on LAUNCH; saved palette None"}, "no palette saved"),
        (False, {"settings": "no SETTINGS row"}, "no palette saved"),
        (False, {"applied": []}, "Hooks.apply never applied the palette"),
        (True, {"coach_observed": None, "demo_shown": False, "settings": "no SETTINGS row", "applied": []}, None),
    ],
)
def test_same_loop_fails_a_run_that_did_not_show_the_phase_5_parts(tmp_path, monkeypatch, capsys, base, change, fails):
    """run() --demo --settings, its child stubbed to write a run that went as `change` says: a Phase 5 tree's run
    fails when the Coach never observed, no demo played, or no palette was saved and applied (TL-13); a base tree,
    which has none of those modules, is not asked for them."""
    spec = importlib.util.spec_from_file_location("same_loop_for_invariance", TOOLS / "same_loop.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tree = ROOT
    if base:
        tree = tmp_path / "base"
        (tree / "terminal_invaders").mkdir(parents=True)

    def child(command, env):
        data = dict(size=[120, 44], scheme="hold", exit=0, end="game_over", ticks=600, score=0, wave=1, digests=[])
        Path(command[6]).write_text(json.dumps(data | {"tape": {}, "unready": None} | RAN | change))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(tool, "subprocess", SimpleNamespace(run=child))
    code = tool.run(tree, tmp_path / "out", ["kitty"], demo=True, settings=True)
    out = capsys.readouterr().out
    assert code == (1 if fails else 0), out
    assert ("FAIL" not in out) if fails is None else (out.count("kitty: FAIL") == 1 and fails in out), out
