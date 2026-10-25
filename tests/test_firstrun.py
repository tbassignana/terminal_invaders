"""A new player's first launch, scripted (Phase 5 spec 15.2): R1-R7 on the real loop, S1-S4 with the installed wheel.

R1-R7 run cli.main in this process on tests/support/faketerm.py's FakeTerminal (TerminalSession, app.play and the
loop as a person's terminal runs them), with input.VirtualInput typing the keys (macOS auto-repeat on the legacy
fixture), a seeded ui.secrets.randbits and a fresh HOME, at many times real time: apple-terminal-26 at 80 x 24 (tap
controls, the glyph arena) and kitty at 120 x 44 (hold controls, k = 2). A script drives each launch the way a
person following the screen would: it sends only keys the screen in front of it names (tools/playtest.py's rule),
and the loop's own Painter is watched frame by frame: every way out named (R1), every tip's text, cells and claims
(R3, R7). Each flow runs once per fixture (module fixtures); the tests read what it saw.

S1-S4 build the wheel and the sdist from a copy of the tree, install the wheel into a fresh venv and run the
installed `invaders` on a pty from an empty folder with an empty HOME (their names hold "wheel": `make firstrun`
leaves them out, `make firstrun-wheel` runs them alone). The build needs a Python with setuptools >= 77: this one,
INVADERS_WHEEL_BUILDER (make firstrun-wheel WHEEL_BUILDER=...), or a python3 on PATH.

Both are `gate` and `slow`: they run in G5.1's gate rows (and make firstrun / make firstrun-wheel), never in a plain
run."""

import functools
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections import Counter
from itertools import cycle
from pathlib import Path
from unittest import mock

import pytest

from terminal_invaders import app, cli, tutorial, ui
from terminal_invaders.attract import BANNER
from terminal_invaders.input import VirtualInput
from terminal_invaders.keymap import WAYS_OUT
from terminal_invaders.model import HEIGHT, TICK_RATE
from terminal_invaders.sprites import GLYPHS, SHIELDED, SHIP, SHOTS
from terminal_invaders.term.cells import CellBuffer
from terminal_invaders.term.simkeys import PROFILES
from terminal_invaders.theme import SLOT, resolve_theme
from tests.support.faketerm import FakeTerminal
from tests.support.ptyrun import Pty
from tests.support.terminals import capabilities, load

pytestmark = [pytest.mark.gate, pytest.mark.slow]

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {"apple-terminal-26": (80, 24), "kitty": (120, 44)}
SEED = 7  # ui.secrets.randbits' seed: the same invasion at every launch
TAP, GAP = 0.06, 0.2  # a tap's length, and the time after it before the script looks again
GUARD = 0.8  # tools/playtest.py GUARD_WAIT: a legacy terminal's same key again, past the KeyGate's repeat guard
FLY_LIMIT = 1500.0  # virtual seconds a run may take before the script gives up on its end
PLAYTEST_PATH = ROOT / "tools" / "playtest.py"


def playtest():
    """tools/playtest.py, whose named() is R1's rule (spec 15.2: the screen in front of a key names it)."""
    spec = importlib.util.spec_from_file_location("playtest_tool", PLAYTEST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


named = playtest().named

# The tips' texts with the default keys (spec 7.3), written out here, apart from tutorial.py's own copy.
MOVES = {
    "hold": "Hold A or D (or the arrows) to move",
    "latched": "Tap A or D to start; tap back or S to stop",
    "timed": "Press A or D to move; S stops",
}
SAME = {
    "fire": "Space fires. Hold it to keep firing",
    "hud": "GUN: shots ready to fire. BOMBS: bombs you hold",
    "meter": "BOMB %: hits in a row fill it; 100% earns a bomb",
    "telegraph": "An invader that changes shape is about to fire",
    "ships": "^ next to LIVES are your ships. Lose them all: over",
    "bomb": "B drops a bomb: it clears enemy fire",
    "pause": "P pauses. Q in the pause menu leaves the run",
    "pickup": "Catch the falling letter for a power-up",
}
BY_SCALE = {  # k = 1, then k >= 2
    "shield": ("The { } around your ship is a shield: one hit", "The cap over your ship is a shield: it takes one hit"),
    "beam": ("' and ^ show where a beam will strike: move away", "Dots and chevrons show where a beam will strike"),
    "gap": ("Stand between [ and ] to stay safe", "Stand between the ground brackets to stay safe"),
    "shelters": ("Shelters # stop shots, theirs and yours", "The blocks above you stop shots, theirs and yours"),
    "cargo": ("$ ships carry a power-up: shoot them", "Marked ships carry a power-up: shoot them"),
}
GLYPH_CLAIMS = "{}'^[]#$"  # the glyphs a tip may name (spec 15.2 R7)
LONGEST = {name: seconds * TICK_RATE for name, seconds in tutorial.SECONDS.items()}
LEARNED = {"move", "fire", "pause", "bomb", "pickup"}  # the tips whose done condition is the player's own doing
STARVED = 8 * TICK_RATE  # a tip current this long (row free, not taught) must have shown: 6 s + 1 s gap + slack


def expected_tip(name: str, scheme: str, k: int, variant: str) -> str:
    if name == "move":
        return "TIP  " + MOVES[scheme]
    if name == "marks":
        return "TIP  " + BY_SCALE[variant or "beam"][k > 1]
    if name in BY_SCALE:
        return "TIP  " + BY_SCALE[name][k > 1]
    return "TIP  " + SAME[name]


def described(name: str, game, variant: str) -> bool:
    """Whether what a tip describes is in the game, read from the game itself (R7: a tip only while it is there)."""
    player = game.player
    if name == "shield":
        return player.guard > 0
    if name == "marks":
        kinds = ("beam",) if variant == "beam" else ("gap", "lane")
        return any(hazard.kind in kinds and hazard.state != "live" for hazard in game.hazards)
    if name == "shelters":
        return bool(game.bunkers)
    if name == "cargo":  # on the field: its row one of the arena's
        return any(alien.cargo and 0 <= math.floor(alien.y + 0.5) < HEIGHT for alien in game.aliens)
    if name == "pickup":
        return bool(game.pickups)
    if name == "bomb":
        return player.bombs > 0
    if name == "ships":
        return player.lives >= 1
    if name == "meter":
        return game.bomb_charge > 0
    return True


def files_under(home: Path) -> dict[str, tuple[str, int]]:
    """Every file under HOME: its path, sha-256 and mtime."""
    found = {}
    for path in sorted(home.rglob("*")):
        if path.is_file():
            found[str(path.relative_to(home))] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
    return found


def data_folder() -> Path:
    """Where the game keeps the profile under HOME (storage.data_dir): Application Support on macOS, elsewhere
    XDG_DATA_HOME, which Flight sets to HOME/.local/share and the wheel runs leave unset (the same folder)."""
    return Path("Library/Application Support" if sys.platform == "darwin" else ".local/share") / "terminal-invaders"


def profile_files() -> list[str]:
    """Every file a launch that saves its profile leaves under HOME: the profile and its lock."""
    return [str(data_folder() / "profile-2.json"), str(data_folder() / "profile-2.lock")]


def profile_of(home: Path) -> dict:
    return json.loads((home / data_folder() / "profile-2.json").read_text())


# --- one launch on the virtual clock ------------------------------------------------------------------------------


class Flight:
    """One launch: cli.main(argv) on a FakeTerminal of the fixture, with HOME and the XDG folders under `home`, the
    keys a script sends (a generator of the virtual times to look again at) and what the loop drew.

    watch: check every frame with a tip (R3's cell diff and bottom row, R7's claims) and count the tips' triggers.
    audit: check every screen's ways out (R1). digests: keep (tick, digest) of every tick of play (R4)."""

    def __init__(self, fixture, home, argv=(), *, size=None, env=None, watch=False, audit=False, digests=False):
        self.fixture, self.doc = fixture, load(fixture)
        self.cols, self.rows = size or FIXTURES[fixture]
        self.kitty = self.doc["keys"]["protocol"] == "kitty"
        self.fake = FakeTerminal(self.doc, cols=self.cols, rows=self.rows, rtt=0.001, vt=(300, 100), limit=math.inf)
        self.fake.fd = os.open(os.devnull, os.O_RDWR)  # app.run closes it; FakeTerminal only compares it
        self.original = [*self.fake.attrs[:6], list(self.fake.attrs[6])]
        self.vin = VirtualInput(self.doc["keys"]["protocol"], PROFILES["macos-default"])
        self.home, self.argv, self.env = Path(home), list(argv), dict(env or {})
        self.home.mkdir(parents=True, exist_ok=True)
        self.watch, self.audit, self.keep_digests = watch, audit, digests
        self.patches: list = []  # more (target, attribute, value) for this launch
        self.session = None
        self.code = None
        self.last: dict[str, float] = {}  # each key's last event (a legacy terminal's guard)
        self.typed: list[tuple[float, bytes]] = []  # every byte typed, with its arrival: R4's key stream
        self.sent: list[dict] = []  # every key pressed, and whether the screen in front of it named it (R2)
        self.unnamed_ways: list[tuple] = []  # R1: (time, context, action, screen)
        self.audited: Counter = Counter()  # R1: screens checked per context
        self.tips: list[dict] = []  # every frame that showed a tip
        self.tip_rows = 0  # frames whose block's bottom row starts with TIP, however it got there
        self.claims: list[tuple] = []  # R7 failures
        self.cells: list[tuple] = []  # R3 failures of the cell diff and the bottom row's game parts
        self.digests: list[tuple[int, str]] = []
        self.current: Counter = Counter()  # ticks each tip's trigger held with the row free while not taught
        self.frames = self.compared = 0
        self.notes: dict = {}
        self.line = None  # the tip the frame being drawn showed
        self.twins: dict[int, ui.Painter] = {}
        self.signature = None

    # -- what is on screen ----------------------------------------------------------------------------------------
    @property
    def now(self) -> float:
        return self.fake.now

    def screen(self) -> list[str]:
        return [line[: self.cols].rstrip() for line in self.fake.vt.lines()[: self.rows]]

    def text(self) -> str:
        return "\n".join(self.screen())

    @property
    def game(self):
        pilot = self.session.pilot if self.session else None
        return pilot.game if pilot else None

    def playing(self) -> bool:
        session = self.session
        return (
            session is not None
            and session.scene == "playing"
            and session.countdown_at is None
            and self.game.phase == "playing"
        )

    def tip_shown(self) -> str:
        """The tip on the screen's bottom rows, or ""."""
        for line in self.screen()[-3:]:
            found = line.find("TIP  ")
            if found >= 0:
                return line[found:]
        return ""

    def restored(self) -> list[str]:
        """What a shell would find wrong after the exit (the empty list: nothing)."""
        vt, wrong = self.fake.vt, []
        if self.fake.attrs != self.original:
            wrong.append("termios")
        if vt.alt_screen or not vt.cursor_visible or not vt.autowrap or vt.kitty_depth:
            wrong.append("modes")
        if (vt.titles, vt.title) != ([], ""):
            wrong.append("title")
        return wrong

    # -- keys -----------------------------------------------------------------------------------------------------
    def feed(self, until: float) -> None:
        for arrival, data in self.vin.bytes_until(until):
            self.fake.type(data, at=arrival)
            self.typed.append((arrival, data))

    def note_key(self, key: str) -> None:
        session = self.session
        context = session.context if session else "start"
        self.sent.append({"key": key, "at": round(self.now, 3), "named": named(key, self.screen()), "context": context})

    def guard(self, key: str) -> float:
        return self.now if self.kitty or key not in self.last else max(self.now, self.last[key] + GUARD)

    def tap(self, key: str):
        at = self.guard(key)
        if at > self.now:
            yield at
        self.note_key(key)
        self.vin.tap(key, self.now, TAP)
        self.last[key] = self.now + TAP
        yield self.now + GAP

    def hold(self, key: str, seconds: float):
        at = self.guard(key)
        if at > self.now:
            yield at
        self.note_key(key)
        self.vin.press(key, self.now)
        self.vin.release(key, self.now + seconds)
        self.last[key] = self.now + seconds
        yield self.now + seconds + GAP

    def wait(self, seconds: float):
        yield self.now + seconds

    def until(self, condition, limit: float = 30.0, step: float = 0.1):
        end = self.now + limit
        while not condition():
            if self.now > end:
                raise AssertionError(f"waited {limit} s at {self.now:.1f} s for {condition}:\n{self.text()}")
            yield self.now + step

    def goto(self, row: int):
        """The menu cursor to a row, with the arrows the menu names."""
        while self.session.selection != row:
            before = self.session.selection
            yield from self.tap("down" if row > before else "up")
            yield from self.until(lambda: self.session.selection != before)

    def leave_menu(self):
        """Esc, then Esc again once the cursor is on QUIT: the game quits."""
        yield from self.until(lambda: self.session.scene == "menu" and "LAUNCH" in self.text())
        yield from self.tap("escape")
        yield from self.until(lambda: self.session.selection == ui.QUIT)
        yield from self.wait(1.0)
        yield from self.tap("escape")
        yield from self.wait(1.0)

    def fly(self, limit: float = FLY_LIMIT, stop=None):
        """Play as the hint row and the tips say, until the run is over (or stop() holds): hold Space to fire and
        move with the scheme's keys (tap D, S, A, S; or hold D, then A, under hold controls), Space on the upgrade
        card, B at the continue prompt and when the Bomb tip asks, Enter at a duel's handoff."""
        moves = cycle(("d", "a") if self.kitty else ("d", "s", "a", "s"))
        start = self.now
        while self.session.scene in ("playing", "paused", "handoff"):
            if self.now - start > limit:
                raise AssertionError(f"the run went on past {limit} s:\n{self.text()}")
            if stop is not None and stop():
                return
            session, game = self.session, self.game
            if session.scene == "handoff":
                yield from self.tap("enter")
                yield from self.wait(0.5)
                continue
            if session.scene != "playing" or session.countdown_at is not None:
                yield self.now + 0.3
                continue
            if game.phase == "upgrade":
                yield from self.tap("space") if game.armed <= 0 else self.wait(0.2)
                continue
            if game.phase == "continue":
                yield from self.tap("b")
                yield from self.wait(0.5)
                continue
            if game.phase in ("game_over", "victory"):
                yield self.now + 0.3
                continue
            if "drops a bomb" in self.tip_shown():
                yield from self.tap("b")
            move = next(moves)
            if self.kitty:
                at = self.guard("space")
                if at > self.now:
                    yield at
                self.note_key("space")
                self.vin.press("space", self.now)
                self.note_key(move)
                self.vin.press(move, self.now + 0.1)
                self.vin.release(move, self.now + 0.8)
                self.vin.release("space", self.now + 1.9)
                self.last.update({move: self.now + 0.8, "space": self.now + 1.9})
                yield self.now + 2.0
            else:  # a tap of another key ends Space's OS repeat on a Mac: move first, then hold Space
                yield from self.tap(move)
                at = self.guard("space")
                if at > self.now:
                    yield at
                self.note_key("space")
                self.vin.press("space", self.now)
                self.vin.release("space", self.now + 1.9)
                self.last["space"] = self.now + 1.9
                yield self.now + 2.0

    # -- the launch -----------------------------------------------------------------------------------------------
    def run(self, script) -> int:
        gen = script(self)
        flight = self

        def act():
            try:
                when = next(gen)
            except StopIteration:
                flight.fake.limit = flight.now + 30.0  # the game must exit soon after the script's last key
                return
            when = max(when, flight.now + 0.001)
            flight.feed(when)
            flight.fake.at(when, act)

        self.fake.at(1.0, act)
        seeded = random.Random(SEED)
        original_play, draw, tip, step = app.play, ui.Painter.draw, ui.Painter.tip, ui.Session.step
        self.original_draw = draw
        depth = [0]

        def play(session, term, **kwargs):
            flight.session = session
            return original_play(session, term, **kwargs)

        def watched_draw(painter, session, stats=""):
            if getattr(painter, "twin", False) or depth[0]:
                return draw(painter, session, stats)
            depth[0] += 1
            flight.line = None
            try:
                draw(painter, session, stats)
            finally:
                depth[0] -= 1
            flight.frame(painter, session)

        def watched_tip(painter, session, game, tail):
            line = tip(painter, session, game, tail)
            if not getattr(painter, "twin", False):
                flight.line = line
            return line

        def watched_step(session, now):
            if session is not flight.session:
                return step(session, now)
            game = session.pilot.game if session.pilot else None
            tick = game.tick if game else None
            step(session, now)
            after = session.pilot.game if session.pilot else None
            if after is None or (after is game and after.tick == tick):
                return
            if flight.keep_digests:
                flight.digests.append((after.tick, after.digest()))
            if flight.watch:
                flight.triggers(session, after)

        fake, doc = self.fake, self.doc
        patches = [
            mock.patch.object(app, "play", play),
            mock.patch.object(app, "open_tty", lambda *a: fake.fd),
            mock.patch.object(app, "user_env", lambda *a: dict(doc["env"])),
            mock.patch.object(app, "TerminalSession", functools.partial(app.TerminalSession, ops=fake.ops())),
            mock.patch.object(cli, "require_terminal", lambda command: None),
            mock.patch.object(ui.secrets, "randbits", seeded.getrandbits),
            mock.patch.object(ui.Painter, "draw", watched_draw),
            mock.patch.object(ui.Painter, "tip", watched_tip),
            mock.patch.object(ui.Session, "step", watched_step),
            *(mock.patch.object(target, name, value) for target, name, value in self.patches),
        ]
        home = self.home
        environ = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "INVADERS_AUDIO": "none",
        } | self.env
        with mock.patch.dict(os.environ, environ):
            if "INVADERS_ATTRACT_IDLE" not in self.env:
                os.environ.pop("INVADERS_ATTRACT_IDLE", None)  # the real title: a demo after 30 s idle
            for patch in patches:
                patch.start()
            try:
                self.code = cli.main(self.argv)
            finally:
                for patch in reversed(patches):
                    patch.stop()
        return self.code

    # -- what the loop drew ---------------------------------------------------------------------------------------
    def frame(self, painter, session) -> None:
        self.frames += 1
        shown = session.attract if session.scene == "attract" and session.attract is not None else session
        if shown.demo:
            self.notes["demo_writes"] = max(self.notes.get("demo_writes", 0), getattr(shown.profile, "writes", 0))
        buffer = painter.screen
        bottom = painter.top + painter.height - 1
        if 0 <= bottom < buffer.rows and buffer.row_text(bottom)[painter.left :].startswith("TIP"):
            self.tip_rows += 1
        if self.line is not None:
            self.tip_frame(painter, shown, self.line)
        elif self.watch and shown.scene == "playing" and self.frames % 60 == 0:
            self.compare(painter, shown, None)
        if self.audit:
            self.ways(painter, session, shown)

    def tip_frame(self, painter, session, line: str) -> None:
        game, coach, k = session.pilot.game, session.coach, painter.k
        name = coach.active if coach else None
        variant = coach.variant if coach else ""
        self.tips.append(
            {
                "tick": game.tick,
                "name": name,
                "line": line,
                "k": k,
                "scheme": session.controls.scheme,
                "variant": variant,
                "mode": game.config.mode,
                "replay": bool(session.replay),
                "demo": session.demo,
                "pilots": len(session.pilots),
                "done": bool(coach and name and coach.finished(name)),
            }
        )
        if not self.watch:
            return
        buffer = painter.screen
        top, left, width, height = painter.top, painter.left, painter.width, painter.height
        block = [buffer.row_text(y)[left : left + width] for y in range(top, top + height)]
        hud, field = block[1], "\n".join(block[2 : height - 2])
        claimed = [glyph for glyph in GLYPH_CLAIMS if glyph in line[len("TIP  ") :]]
        if k >= 2 and name != "ships" and claimed:
            self.claims.append((game.tick, k, line, f"a k >= 2 text names {claimed}"))
        where = hud if name == "ships" else field
        missing = [glyph for glyph in claimed if glyph not in where]
        if (k == 1 or name == "ships") and missing:
            self.claims.append((game.tick, k, line, f"{missing} not drawn"))
        if name is None or not described(name, game, variant):
            self.claims.append((game.tick, k, line, f"{name} not in the game"))
        self.compare(painter, session, line)

    def twin(self, painter) -> ui.Painter:
        """A Painter like the loop's on a buffer of its own, sharing its views, flash limiter and link (whose state
        a second draw of the same tick does not change)."""
        twin = self.twins.get(id(painter))
        rows, cols = painter.screen.getmaxyx()
        if twin is None or twin.screen.getmaxyx() != (rows, cols):
            buffer = CellBuffer(cols, rows, sanitizer=painter.screen.sanitizer)
            twin = ui.Painter.__new__(ui.Painter)
            twin.screen, twin.options, twin.theme, twin.caps = buffer, painter.options, painter.theme, painter.caps
            twin.colors, twin.views, twin.arcade_view = painter.colors, painter.views, painter.arcade_view
            twin.limiter, twin.link, twin.twin = painter.limiter, painter.link, True
            twin.resize()
            self.twins = {id(painter): twin}
        twin.arcade_view = painter.arcade_view
        return twin

    def compare(self, painter, session, line) -> None:
        """R3: the frame drawn again without the Coach differs from the loop's only on the block's bottom row, and
        there only by the tip: the game's parts (notices, timers, codes) are all there, uncut."""
        self.compared += 1
        twin = self.twin(painter)
        coach, session.coach = session.coach, None
        try:
            self.original_draw(twin, session)
        finally:
            session.coach = coach
        real, other = painter.screen, twin.screen
        bottom, cols, tick = painter.top + painter.height - 1, real.cols, session.pilot.game.tick
        if real.chars != other.chars or real.styles != other.styles:
            for y in range(real.rows):
                cut = slice(y * cols, (y + 1) * cols)
                if y != bottom and (real.chars[cut] != other.chars[cut] or real.styles[cut] != other.styles[cut]):
                    self.cells.append((tick, y, "a cell off the bottom row", line))
        span = slice(painter.left, painter.left + painter.width)
        mine, theirs = real.row_text(bottom)[span].rstrip(), other.row_text(bottom)[span].rstrip()
        if line is None:
            fine = mine == theirs
        else:
            game = session.pilot.game
            parts = ui.bottom_row(game, painter.options.reduced_motion)
            busy = any(color in (ui.NOTICE_ROLE, ui.PICKUP_ROLE) for _, color in parts)
            fine = not busy and mine in (line + (" | " + theirs if theirs else ""), line + "  " + theirs)
        if not fine:
            self.cells.append((tick, bottom, f"bottom row {mine!r} against {theirs!r}", line))

    def ways(self, painter, session, shown) -> None:
        """R1: every way out of the screen (keymap.WAYS_OUT[context], in play the hint row's move, fire, bomb and
        pause) has a key the screen names; the demo says any key; the too-small notice names its way back."""
        context = shown.context
        game = shown.pilot.game if shown.pilot else None
        small = ui.small_notice(shown) and not ui.arcade_frame(shown)
        signature = (context, shown.scene, game and game.phase, shown.countdown_at is None, small)
        if signature == self.signature and self.frames % 15:
            return
        self.signature = signature
        if ui.arcade_frame(shown) and not shown.demo:
            return  # Arcade's 1978 screens and notice are Phase 4's, kept as they were (spec 17)
        buffer = painter.screen
        screen = [buffer.row_text(y).rstrip() for y in range(buffer.rows)]
        self.audited[("small " if small else "") + context] += 1
        if context == "attract":
            required = []
            if not named("f12", screen):  # "Press any key" names every key, F12 among them
                self.unnamed_ways.append((round(self.now, 2), context, "any key", screen))
        elif small:
            ways = ui.SMALL_WAYS.get(context, ())
            required = [(context, ways[0])] if ways else []
        else:
            required = [(context, action) for action in WAYS_OUT.get(context, ())]
            if context == "play" and game is not None:
                extra = {"upgrade": ("left", "right", "fire"), "continue": ("fire", "bomb")}
                required += [("play", action) for action in extra.get(game.phase, ("left", "right", "fire", "bomb"))]
        for where, action in required:
            if not any(named(spec, screen) for spec in shown.keymap.keys_for(where, action)):
                self.unnamed_ways.append((round(self.now, 2), context, action, screen))

    def triggers(self, session, game) -> None:
        """Ticks each tip's trigger holds in wave play with the row free, while the tip is not taught."""
        coach = session.coach
        if coach is None or game.phase != "playing" or session.scene != "playing":
            return
        if not tutorial.row_free(game, session.options.reduced_motion):
            return
        for name in tutorial.TIPS:
            if name not in coach.done and coach.current(name, game):
                self.current[name] += 1


def launch(fixture, home, script, argv=(), **kwargs) -> Flight:
    flight = Flight(fixture, home, argv, **kwargs)
    flight.run(script)
    return flight


# --- the flows ------------------------------------------------------------------------------------------------------


def first_run(f: Flight):
    """R2: first launch -> HOW TO PLAY -> LAUNCH -> pause and resume -> play to the run's end -> initials (typed) ->
    results -> menu -> FLIGHT RECORDS -> SETTINGS (palette to COLOUR-BLIND SAFE) -> quit."""
    yield from f.until(lambda: f.session is not None and "LAUNCH" in f.text())
    yield from f.goto(ui.MENU.index("HOW TO PLAY"))
    yield from f.tap("enter")
    yield from f.until(lambda: "FLIGHT MANUAL" in f.text())
    f.notes["legend"] = next(line.strip() for line in f.screen() if line.strip().startswith("LEGEND"))
    f.notes["play_k"] = f.session.hooks.renderer.painter.layout.k
    yield from f.tap("enter")  # Enter / Escape to return
    yield from f.until(lambda: "LAUNCH" in f.text())
    yield from f.goto(0)
    yield from f.tap("enter")
    yield from f.until(f.playing)
    yield from f.wait(1.5)
    yield from f.tap("p")
    yield from f.until(lambda: "PAUSED" in f.text())
    yield from f.wait(0.5)
    yield from f.tap("p")
    yield from f.until(lambda: f.session.scene == "playing" and "PAUSED" not in f.text())
    yield from f.fly()
    f.notes["taught"] = set(f.session.tutor.done)
    yield from f.until(lambda: f.session.scene == "initials" and "Type letters" in f.text())
    f.notes["score"] = f.game.score
    f.notes["wave"] = f.game.wave
    yield from f.wait(ui.SETTLE)  # the card takes no key in its first half second (SC-1)
    for key in "tim":
        yield from f.tap(key)
    yield from f.until(lambda: "SAVES AS  TIM" in f.text())
    yield from f.tap("enter")
    yield from f.until(lambda: f.session.scene == "results" and "menu" in f.text())
    f.notes["results"] = f.text()
    yield from f.tap("q")
    yield from f.until(lambda: f.session.scene == "menu" and "LAUNCH" in f.text())
    yield from f.goto(ui.MENU.index("FLIGHT RECORDS"))
    yield from f.tap("enter")
    yield from f.until(lambda: f.session.scene == "records")
    yield from f.wait(0.3)
    f.notes["records"] = f.text()
    yield from f.tap("enter")
    yield from f.until(lambda: f.session.scene == "menu")
    yield from f.goto(ui.MENU.index("SETTINGS"))
    yield from f.tap("enter")
    yield from f.until(lambda: f.session.scene == "settings" and "PALETTE" in f.text())
    yield from f.tap("right")
    yield from f.until(lambda: "COLOUR-BLIND SAFE" in f.text())
    f.notes["palette_row"] = next(line for line in f.screen() if "COLOUR-BLIND SAFE" in line)
    yield from f.tap("escape")  # Esc save and go back
    yield from f.leave_menu()


def stream(typed):
    """A script that types a recorded key stream again, at the same virtual times (R4)."""

    def script(f: Flight):
        for arrival, data in typed:
            f.fake.type(data, at=arrival)
        yield max(arrival for arrival, _ in typed) + 1.0

    return script


def second_run(f: Flight):
    """The same HOME again (R3: no tip; R6: the saved palette in effect from the start): LAUNCH, play a while, quit."""
    yield from f.until(lambda: f.session is not None and "LAUNCH" in f.text())
    f.notes["palette"] = f.session.hooks.renderer.theme.palette
    f.notes["title_pen"] = title_pen(f)
    yield from f.tap("enter")
    yield from f.until(f.playing)
    started = f.now
    yield from f.fly(stop=lambda: f.now - started > 45.0)
    yield from f.tap("p")
    yield from f.until(lambda: "PAUSED" in f.text())
    yield from f.tap("q")
    yield from f.leave_menu()


def title_pen(f: Flight):
    """The colour the title's first letter is drawn in (the accent role)."""
    for y, line in enumerate(f.screen()):
        x = line.find("T E R M I N A L")
        if x >= 0:
            return f.fake.vt.cell(y, x)[1].fg
    return None


def other_mode(steps, seconds: float = 25.0):
    """A run that never gets the Coach (R3): menu keys to set it up, LAUNCH, play a while, back to the menu, quit."""

    def script(f: Flight):
        yield from f.until(lambda: f.session is not None and "LAUNCH" in f.text())
        for key in steps:
            yield from f.tap(key)
        yield from f.goto(0)
        yield from f.tap("enter")
        yield from f.until(lambda: f.session.scene in ("playing", "handoff") and f.game is not None)
        started = f.now
        yield from f.fly(stop=lambda: f.now - started > seconds)
        f.notes["mode"] = f.game.config.mode
        f.notes["pilots"] = len(f.session.pilots)
        f.notes["coach"] = f.session.coach
        if f.session.scene == "playing":
            yield from f.tap("p")
            yield from f.until(lambda: f.session.scene == "paused")
            yield from f.tap("q")
        else:
            yield from f.until(lambda: f.session.scene == "results")
            yield from f.tap("q")
        yield from f.leave_menu()

    return script


def watch_replay(f: Flight):
    """--replay of the first run's tape (R3: a replay shows no tip), then P and Q to leave it."""
    yield from f.until(lambda: f.session is not None and f.session.scene == "playing")
    yield from f.wait(25.0)
    f.notes["coach"] = f.session.coach
    yield from f.tap("p")
    yield from f.until(lambda: f.session.scene == "paused")
    yield from f.tap("q")
    yield from f.wait(1.0)


def small_first(f: Flight):
    """R5, first case: launched at 50 x 20; Enter and the arrows; then 80 x 24."""
    yield from f.until(lambda: f.session is not None and "Enlarge" in f.text())
    yield from f.wait(0.5)
    f.notes["notice"] = f.screen()
    f.notes["before"] = (f.session.scene, f.session.selection, len(f.session.pilots))
    for key in ("enter", "down", "down", "up", "enter", "right"):
        yield from f.tap(key)
    yield from f.wait(0.5)
    f.notes["after"] = (f.session.scene, f.session.selection, len(f.session.pilots), f.session.mode)
    f.fake.resize(80, 24)
    f.cols, f.rows = 80, 24
    yield from f.until(lambda: "LAUNCH" in f.text())
    yield from f.wait(0.3)
    f.notes["resized"] = (f.session.scene, f.session.selection, len(f.session.pilots), f.text())
    yield from f.leave_menu()


def small_quit(f: Flight):
    """R5, second case: Esc, Esc at 50 x 20 quits."""
    yield from f.until(lambda: f.session is not None and "Enlarge" in f.text())
    yield from f.tap("escape")
    yield from f.wait(1.0)
    yield from f.tap("escape")
    yield from f.wait(1.0)


def arcade_highlight(f: Flight):
    """R5, third case: at 80 x 24 with ARCADE highlighted, Up and Down move the highlight."""
    yield from f.until(lambda: f.session is not None and "LAUNCH" in f.text())
    yield from f.goto(1)
    yield from f.tap("right")
    yield from f.until(lambda: f.session.mode == "arcade")
    seen = [(f.session.selection, highlighted(f))]
    for key in ("up", "down", "down", "up"):
        yield from f.tap(key)
        yield from f.wait(0.2)
        seen.append((f.session.selection, highlighted(f)))
    f.notes["seen"] = seen
    f.notes["text"] = f.text()
    yield from f.goto(0)
    yield from f.leave_menu()


def highlighted(f: Flight) -> str:
    """The menu row the screen marks with >, or ""."""
    for line in f.screen():
        stripped = line.strip()
        if stripped.startswith("> "):
            return stripped[2:].split()[0]
    return ""


def placing(f: Flight):
    """R6: a staged run (wave 5, one life) that places: initials typed (A N N, a double letter 0.15 s apart),
    cycled (Left, Left, Up: BNN), saved; the board shows them."""
    yield from f.until(lambda: f.session is not None and "LAUNCH" in f.text())
    yield from f.tap("enter")
    yield from f.until(lambda: f.session.scene == "playing")
    yield from f.until(lambda: f.session.scene == "initials" and "Type letters" in f.text(), limit=60.0)
    f.notes["card"] = f.text()
    yield from f.wait(ui.SETTLE)  # the card takes no key in its first half second (SC-1)
    f.note_key("a")
    f.vin.tap("a", f.now, TAP)
    yield f.now + 0.15
    f.note_key("n")
    f.vin.tap("n", f.now, TAP)
    yield f.now + 0.15
    f.note_key("n")  # the double letter, 0.15 s after the first N
    f.vin.tap("n", f.now, TAP)
    yield f.now + 0.5
    f.notes["typed"] = f.text()
    for key in ("left", "left", "up"):
        yield from f.tap(key)
    yield from f.wait(0.3)
    f.notes["cycled"] = f.text()
    yield from f.tap("enter")
    yield from f.until(lambda: f.session.scene == "results")
    yield from f.tap("q")
    yield from f.until(lambda: f.session.scene == "menu")
    yield from f.goto(ui.MENU.index("FLIGHT RECORDS"))
    yield from f.tap("enter")
    yield from f.until(lambda: f.session.scene == "records")
    yield from f.wait(0.3)
    f.notes["records"] = f.text()
    yield from f.tap("enter")
    yield from f.goto(0)
    yield from f.leave_menu()


def demo(f: Flight):
    """R6: the title demo after the idle time (INVADERS_ATTRACT_IDLE=3); a held Down ends it and its repeats do
    nothing in the menu; a held Enter ends it and launches nothing; Esc ends it and is not the first of Esc Esc; the
    profile is the same before and after."""
    yield from f.until(lambda: f.session is not None and "LAUNCH" in f.text())
    yield from f.wait(1.0)
    f.notes["early"] = f.session.scene
    f.notes["files"] = [files_under(f.home)]
    yield from f.until(lambda: BANNER in f.text(), limit=10.0)
    f.notes["started"] = f.now
    ticks = f.session.attract.pilot.game.tick
    yield from f.wait(1.0)
    f.notes["ticked"] = f.session.attract is not None and f.session.attract.pilot.game.tick > ticks
    for key in ("down", "enter"):
        yield from f.until(lambda: BANNER in f.text(), limit=10.0)
        yield from f.hold(key, 1.5)
        yield from f.wait(0.5)
        f.notes[key] = (f.session.scene, f.session.selection, len(f.session.pilots), BANNER in f.text())
    yield from f.until(lambda: BANNER in f.text(), limit=10.0)
    yield from f.tap("escape")
    yield from f.until(lambda: f.session.scene == "menu")
    yield from f.wait(0.3)
    f.notes["after_esc"] = (f.session.scene, f.session.selection)
    yield from f.tap("escape")  # the first Esc of Esc Esc, if the demo's was not
    yield from f.wait(0.5)
    f.notes["second_esc"] = (f.session.scene, f.session.selection, f.session.quit)
    f.notes["files"].append(files_under(f.home))
    yield from f.wait(1.0)
    yield from f.tap("escape")
    yield from f.wait(1.0)


def staged_first_game(wave: int = 5, **how):
    """ui.make_game with the Session's first Campaign game staged: wave `wave`, play under way, one life (or `how`:
    staged()'s keywords)."""
    from terminal_invaders.campaign.testing import staged
    from terminal_invaders.model import Config

    original, first = ui.make_game, [True]

    def make_game(config):
        if first[0] and config.mode == "classic":
            first[0] = False
            return staged(Config("normal", "classic", 1978), wave, **({"lives": 1, "phase": "playing"} | how))
        return original(config)

    return make_game


# --- the module's runs, one set per fixture -----------------------------------------------------------------------


@pytest.fixture(scope="module", params=list(FIXTURES))
def fixture(request):
    return request.param


@pytest.fixture(scope="module")
def folder(fixture, tmp_path_factory):
    return tmp_path_factory.mktemp(f"firstrun-{fixture}")


@pytest.fixture(scope="module")
def first(fixture, folder):
    started = time.monotonic()
    tape = folder / "first-tape.json"
    flight = launch(
        fixture, folder / "home-first", first_run, ["--record", str(tape)], watch=True, audit=True, digests=True
    )
    flight.notes["tape"], flight.notes["wall"] = tape, time.monotonic() - started
    return flight


@pytest.fixture(scope="module")
def presented(fixture, folder, first):
    """R2's key stream again with tips off, effects off and the colour-blind palette (R4)."""
    tape = folder / "presented-tape.json"
    argv = ["--record", str(tape), "--tips", "off", "--effects", "off", "--palette", "cvd"]
    flight = launch(fixture, folder / "home-presented", stream(first.typed), argv, digests=True)
    flight.notes["tape"] = tape
    return flight


@pytest.fixture(scope="module")
def again(fixture, first):
    return launch(fixture, first.home, second_run, watch=True, audit=True)


@pytest.fixture(scope="module")
def others(fixture, folder, first):
    """A duel, an Arcade game (in a window that holds the Arcade block) and a replay: no tips (R3)."""
    arcade = None if fixture == "kitty" else (80, 32)
    return {
        "duel": launch(fixture, folder / "home-duel", other_mode(["down", "left"]), watch=True),
        "arcade": launch(fixture, folder / "home-arcade", other_mode(["down", "right"]), size=arcade, watch=True),
        "replay": launch(
            fixture, folder / "home-replay", watch_replay, ["--replay", str(first.notes["tape"])], watch=True
        ),
    }


@pytest.fixture(scope="module")
def small(fixture, folder):
    return {
        "first": launch(fixture, folder / "home-small", small_first, size=(50, 20), audit=True),
        "quit": launch(fixture, folder / "home-small-quit", small_quit, size=(50, 20), audit=True),
        "arcade": launch(fixture, folder / "home-arcade-menu", arcade_highlight, size=(80, 24), audit=True),
    }


@pytest.fixture(scope="module")
def flows(fixture, folder):
    placed = Flight(fixture, folder / "home-placing", audit=True)
    placed.patches.append((ui, "make_game", staged_first_game()))
    placed.run(placing)
    shown = launch(fixture, folder / "home-demo", demo, env={"INVADERS_ATTRACT_IDLE": "3"}, watch=True, audit=True)
    return {"placing": placed, "demo": shown}


# Staged waves (wave, boss phase) whose tips the first run's waves never reach: gunners' tells (5), a curtain's gap
# (17), the DREADNOUGHT's escorts and lanes (18) and its beams (18, phase 2).
TAUGHT = ((5, 0), (17, 0), (18, 0), (18, 2))


def taught_wave(f: Flight):
    """A staged wave with TIPS always (the tips the first run's waves never reach): LAUNCH, play 50 s, leave."""
    yield from f.until(lambda: f.session is not None and "LAUNCH" in f.text())
    yield from f.tap("enter")
    yield from f.until(lambda: f.session.scene == "playing")
    started = f.now
    yield from f.fly(stop=lambda: f.now - started > 50.0)
    f.notes["taught"] = set(f.session.tutor.done)
    yield from f.tap("p")
    yield from f.until(lambda: f.session.scene == "paused")
    yield from f.tap("q")
    yield from f.leave_menu()


@pytest.fixture(scope="module")
def taught(fixture, folder):
    """The TAUGHT waves staged (immortal: the run lasts 50 s), tips always (R3, R7)."""
    flights = {}
    for wave, phase in TAUGHT:
        flight = Flight(fixture, folder / f"home-wave-{wave}-{phase}", ["--tips", "always"], watch=True)
        staging = staged_first_game(wave, lives=None, immortal=True, boss_phase=phase)
        flight.patches.append((ui, "make_game", staging))
        flight.run(taught_wave)
        flights[f"wave {wave}" + (f" phase {phase}" if phase else "")] = flight
    return flights


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_profile_helpers_look_where_the_game_saves(platform, tmp_path, monkeypatch):
    """profile_of and S2's file list follow storage on Linux and macOS, with XDG_DATA_HOME as Flight sets it and as
    the wheel runs leave it (unset): R2, R6, S2 and S3 read the profile through them."""
    from terminal_invaders.storage import Profile

    monkeypatch.setattr(sys, "platform", platform)
    for home, data in ((tmp_path / "flight", ".local/share"), (tmp_path / "wheel", None)):
        monkeypatch.setenv("HOME", str(home))
        if data:
            monkeypatch.setenv("XDG_DATA_HOME", str(home / data))
        else:
            monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        profile = Profile()
        profile.data["name"] = "TIM"
        assert profile.save()
        assert profile_of(home)["name"] == "TIM"
        assert sorted(str(path.relative_to(home)) for path in home.rglob("*") if path.is_file()) == profile_files()


# --- R1-R7 ----------------------------------------------------------------------------------------------------------


def test_r1_every_screen_names_its_ways_out(first, again, small, flows):
    flights = {"first run": first, "second run": again, **small, **flows}
    for name, flight in flights.items():
        assert flight.unnamed_ways == [], (name, flight.unnamed_ways[0][:3], "\n".join(flight.unnamed_ways[0][3]))
    audited = sum((flight.audited for flight in flights.values()), Counter())
    for context in ("menu", "pages", "play", "pause", "initials", "results", "settings", "attract", "small menu"):
        assert audited[context] > 0, (context, audited)


def test_r2_a_first_run_sends_only_named_keys_and_ends_saved(first):
    unnamed = [key for key in first.sent if not key["named"]]
    assert unnamed == [], unnamed[:5]
    assert first.code == 0 and first.restored() == []
    profile = profile_of(first.home)
    assert profile["settings"].get("palette") == "cvd" and profile["settings"].get("tips_done") is True
    assert profile["name"] == "TIM"
    rows = [row for row in profile["scores"] if row["name"] == "TIM"]
    assert [row["score"] for row in rows] == [first.notes["score"]]
    assert re.search(rf"TIM\s+{first.notes['score']:,}\b|TIM\s+{first.notes['score']}\b", first.notes["records"])
    assert "COLOUR-BLIND SAFE" in first.notes["palette_row"]
    print(
        f"\nR2 {first.fixture}: {len(first.sent)} keys, all named; run to wave {first.notes['wave']}, score "
        f"{first.notes['score']}; {first.now:.0f} s virtual, {first.notes['wall']:.0f} s wall"
    )


def test_r3_tips_show_their_text_only_on_the_bottom_row_and_never_hide_the_games(first, taught, again, others, flows):
    scheme, k = ("hold", 2) if first.fixture == "kitty" else ("latched", 1)
    shown, learned, variants = Counter(), set(), set()
    for name, flight in {"first run": first, **taught}.items():
        tips = flight.tips
        assert tips, f"{name}: no tip"
        assert {tip["scheme"] for tip in tips} == {scheme} and {tip["k"] for tip in tips} == {k}, name
        wrong = [tip for tip in tips if tip["line"] != expected_tip(tip["name"], scheme, k, tip["variant"])]
        assert wrong == [], (name, wrong[:3])
        assert [tip for tip in tips if tip["done"]] == [], f"{name}: a tip still showing once done"
        here = Counter(tip["name"] for tip in tips)
        for tip in here:  # one frame a tick at 60 fps: a tip never outlasts its time
            assert longest_showing(tips, tip) <= LONGEST[tip] + 2, (name, tip)
        # every tip shown, learned before it could show (the player did what it teaches), or never current long
        assert flight.notes["taught"] - set(here) <= LEARNED, (name, flight.notes["taught"] - set(here))
        starved = {tip: ticks for tip, ticks in flight.current.items() if ticks >= STARVED and tip not in here}
        assert starved == {}, (name, starved, dict(flight.current))
        assert flight.compared > len(tips) and flight.cells == [], (name, flight.cells[:3])
        shown += here
        learned |= flight.notes["taught"] - set(here)
        variants |= {tip["variant"] for tip in tips if tip["name"] == "marks"}
        print(f"\nR3 {first.fixture} {name}: frames per tip {dict(here)}; {flight.compared} frames diffed")
    assert set(shown) | learned == set(tutorial.TIPS) and variants == {"beam", "gap"}, (shown, learned, variants)
    # none on the same profile's second run, a duel, Arcade, a replay or the demo
    assert again.tips == [] and again.tip_rows == 0 and again.session.coach is None
    for name, flight in [*others.items(), ("demo", flows["demo"])]:
        assert flight.tips == [] and flight.tip_rows == 0, name
        assert flight.code == 0, name
    assert others["duel"].notes["pilots"] == 2 and others["arcade"].notes["mode"] == "arcade"
    assert all(others[name].notes["coach"] is None for name in others)


def longest_showing(tips: list[dict], name: str) -> int:
    """The most ticks one showing of a tip lasted (consecutive frames of it, a gap of 3 ticks or less between)."""
    ticks = [tip["tick"] for tip in tips if tip["name"] == name]
    longest, start, last = 0, None, None
    for tick in ticks:
        if last is None or tick - last > 3:
            start = tick
        last = tick
        longest = max(longest, tick - start)
    return longest


def test_r4_tips_effects_and_palette_change_nothing_the_game_does(first, presented):
    assert presented.code == 0
    assert first.digests and first.digests == presented.digests
    tape, other = first.notes["tape"], presented.notes["tape"]
    assert tape.read_bytes() == other.read_bytes()
    with mock.patch.object(sys, "stdout", new=Capture()) as printed:
        assert cli.main(["--verify-replay", str(tape)]) == 0
    assert printed.text.startswith("VERIFIED"), printed.text
    print(f"\nR4 {first.fixture}: {len(first.digests)} ticks, the same digests and tape; {printed.text.strip()}")


class Capture:
    def __init__(self):
        self.text = ""

    def write(self, text):
        self.text += text

    def flush(self):
        pass


def test_r5_a_window_too_small_names_its_size_and_way_out(small):
    first = small["first"]
    lines = [(y, line) for y, line in enumerate(first.notes["notice"]) if line.strip()]
    assert [line.strip() for _, line in lines] == [
        "Enlarge the window: the game needs 60 x 24",
        "Now 50 x 20",
        "Esc Esc quits",
    ]
    for _, line in lines:  # centred in the 50 x 20 window
        assert len(line) - len(line.lstrip()) == (50 - len(line.strip())) // 2, line
    assert lines[0][0] == (20 - len(lines)) // 2
    scene, selection, pilots = first.notes["before"]
    assert first.notes["after"] == (scene, selection, 0, "classic") and scene == "menu"
    assert first.notes["resized"][:3] == ("menu", selection, 0) and "> LAUNCH" in first.notes["resized"][3]
    assert first.code == 0 and first.restored() == []
    quit_ = small["quit"]
    assert quit_.code == 0 and quit_.restored() == [] and quit_.session.pilots == []
    arcade = small["arcade"]
    assert [selection for selection, _ in arcade.notes["seen"]] == [1, 0, 1, 2, 1]
    assert [row for _, row in arcade.notes["seen"]] == ["MODE", "LAUNCH", "MODE", "DIFFICULTY", "MODE"]
    assert "ARCADE" in arcade.notes["text"] and arcade.code == 0


def test_r6_initials_settings_and_the_demo(first, again, flows):
    placed = flows["placing"]
    assert "NEW HIGH SCORE" in placed.notes["card"] and "SAVES AS  ANN" in placed.notes["typed"]
    assert "SAVES AS  BNN" in placed.notes["cycled"] and re.search(r"BNN\s+\d", placed.notes["records"])
    assert [row["name"] for row in profile_of(placed.home)["scores"]] == ["BNN"] and placed.code == 0
    # the palette chosen on the first run's SETTINGS, saved, is the second launch's from its first frame
    cvd = resolve_theme(capabilities(load(first.fixture)), "cvd").fg[SLOT["accent"]]
    assert again.notes["palette"] == "cvd" and again.notes["title_pen"] == ("rgb", *map(int, cvd.split(";")[2:]))
    shown = flows["demo"]
    assert shown.notes["early"] == "menu" and shown.notes["ticked"]
    assert 3.0 <= shown.notes["started"] <= 4.5  # the idle time (3 s) after the title came up, at launch
    assert shown.notes["down"] == ("menu", 0, 0, False)  # ended, and no repeat moved the cursor
    assert shown.notes["enter"] == ("menu", 0, 0, False)  # ended, and nothing launched
    assert shown.notes["after_esc"] == ("menu", 0)
    assert shown.notes["second_esc"] == ("menu", ui.QUIT, False)  # the first Esc of Esc Esc: the demo's was not
    assert shown.code == 0 and shown.notes.get("demo_writes", 0) == 0
    before, after = shown.notes["files"]
    assert before == after


def test_r7_tips_and_the_legend_name_only_glyphs_on_screen(first, taught):
    for name, flight in {"first run": first, **taught}.items():
        assert flight.claims == [], (name, flight.claims[:3])
        assert flight.tips, name
    k = first.notes["play_k"]
    assert k == (2 if first.fixture == "kitty" else 1)
    legend = first.notes["legend"].removeprefix("LEGEND").strip()
    assert legend == ui.LEGENDS[0 if k == 1 else 1]
    if k == 1:
        drawn = {SHIP, SHIELDED, SHOTS["pellet"], SHOTS["dart"], SHOTS["needle"], GLYPHS["pickup"]}
        assert {"/A\\", "{A}", "!", "v", ":", "[R]"} <= drawn
    else:
        assert not any(glyph in legend for glyph in "{}'^[]#$!:/\\")


# --- S1-S4: the installed wheel on a pty ----------------------------------------------------------------------------

UP = 10.0  # the longest a step may take to show on a loaded machine (test_terminal.py's): a deadline, never a delay
PTY_GUARD = 1.2  # test_terminal.py's GUARD: a legacy terminal's same key again, past the repeat guard on a pty
COPY_IGNORED = ("__pycache__", "*.pyc", "*.egg-info", "build", "dist", ".venv", ".git", ".pytest_cache", ".ruff_cache")
STAGED_LAUNCHER = """import sys

from terminal_invaders import ui
from terminal_invaders.campaign.testing import staged
from terminal_invaders.cli import main
from terminal_invaders.model import Config

original, first = ui.make_game, [True]


def make_game(config):
    if first[0] and config.mode == "classic":  # the first Campaign game: wave 5's aimed fire, one life
        first[0] = False
        return staged(Config("normal", "classic", 1978), 5, lives=1, phase="playing")
    return original(config)


ui.make_game = make_game
raise SystemExit(main(sys.argv[1:]))
"""
CLEAN_ENV = {  # what a person's shell would not have: this tree on the path, its venv, the tests' own settings
    "PYTHONPATH": None,
    "VIRTUAL_ENV": None,
    "XDG_STATE_HOME": None,
    "XDG_DATA_HOME": None,
    "XDG_CONFIG_HOME": None,
    "INVADERS_ATTRACT_IDLE": None,
    "INVADERS_AUDIO": None,
    "PYTHONDONTWRITEBYTECODE": "1",
}


def builder() -> str:
    """A Python whose setuptools builds this pyproject (>= 77): INVADERS_WHEEL_BUILDER, this one, or python3."""
    probe = "import setuptools, sys; sys.exit(int(setuptools.__version__.split('.')[0]) < 77)"
    for python in (os.environ.get("INVADERS_WHEEL_BUILDER"), sys.executable, shutil.which("python3")):
        if python and subprocess.run([python, "-B", "-c", probe], capture_output=True).returncode == 0:
            return python
    pytest.fail("no Python with setuptools >= 77 to build the wheel: make firstrun-wheel WHEEL_BUILDER=/path/to/python")


def build(python: str, source: Path, out: Path, *kinds: str) -> list[Path]:
    """setuptools.build_meta's wheel and/or sdist of the project in source, into out: one process each (a second
    build in the same process writes where the first one's state says)."""
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for kind in kinds:
        done = subprocess.run(
            [
                python,
                "-B",
                "-c",
                f"import sys, setuptools.build_meta as b; print(b.build_{kind}(sys.argv[1]))",
                str(out),
            ],
            cwd=source,
            capture_output=True,
            text=True,
            env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert done.returncode == 0, done.stderr[-3000:]
        made.append(out / done.stdout.split()[-1])
        assert made[-1].is_file(), made[-1]
    return made


def venv_with(wheel: Path, folder: Path) -> Path:
    """A fresh venv of this Python with the wheel installed, offline and alone (--no-index --no-deps)."""
    subprocess.run([sys.executable, "-m", "venv", str(folder)], check=True, capture_output=True)
    python = folder / "bin" / "python"
    installed = subprocess.run(
        [python, "-m", "pip", "install", "--no-index", "--no-deps", "--disable-pip-version-check", "-q", str(wheel)],
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr[-3000:]
    return folder


def run_in(folder: Path, home: Path, *command) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key not in CLEAN_ENV} | {"HOME": str(home)}
    return subprocess.run([str(part) for part in command], cwd=folder, env=env, capture_output=True, text=True)


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    """S1's builds and venvs: the wheel and sdist from a copy of this tree, a venv with the wheel, a wheel built from
    the sdist and a venv with that one."""
    started = time.monotonic()
    work = tmp_path_factory.mktemp("wheel")
    python = builder()
    source = work / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(*COPY_IGNORED))
    built = build(python, source, work / "dist", "wheel", "sdist")
    wheel_file = next(path for path in built if path.suffix == ".whl")
    sdist_file = next(path for path in built if path.name.endswith(".tar.gz"))
    with tarfile.open(sdist_file) as archive:
        archive.extractall(work / "unpacked", filter="data")
    (unpacked,) = (work / "unpacked").iterdir()
    (again,) = build(python, unpacked, work / "dist-from-sdist", "wheel")
    empty = work / "empty"
    empty.mkdir()
    return {
        "builder": python,
        "wheel": wheel_file,
        "sdist": sdist_file,
        "venv": venv_with(wheel_file, work / "venv"),
        "from_sdist": venv_with(again, work / "venv-from-sdist"),
        "empty": empty,
        "work": work,
        "seconds": time.monotonic() - started,
    }


def test_s1_wheel_and_sdist_install_and_run_from_an_empty_folder(wheel):
    from terminal_invaders import __version__

    empty, home = wheel["empty"], wheel["work"] / "home-s1"
    home.mkdir()
    version = run_in(empty, home, wheel["venv"] / "bin/invaders", "--version")
    assert version.returncode == 0 and version.stdout.strip() == f"Terminal Invaders {__version__}", version
    shown = run_in(empty, home, wheel["venv"] / "bin/invaders", "--help")
    assert shown.returncode == 0, shown.stderr
    for key in ("Space", "P", "Esc"):
        assert re.search(rf"(?<![A-Za-z]){key}(?![A-Za-z])", shown.stdout), key
    check = "from terminal_invaders.tapes import attract_tapes; assert len(attract_tapes()) == 2"
    for venv in (wheel["venv"], wheel["from_sdist"]):
        imported = run_in(empty, home, venv / "bin/python", "-I", "-c", check)
        assert imported.returncode == 0, imported.stderr
        where = run_in(
            empty, home, venv / "bin/python", "-I", "-c", "import terminal_invaders; print(terminal_invaders.__file__)"
        )
        assert str(venv) in where.stdout and str(ROOT) not in where.stdout, where.stdout
    assert list(empty.iterdir()) == [] and list(home.iterdir()) == []
    print(
        f"\nS1: built with {wheel['builder']}, {wheel['wheel'].name} and {wheel['sdist'].name}; {wheel['seconds']:.0f} s"
    )


class Installed(Pty):
    """The installed `invaders` (or a launcher run by the venv's python -I) on a pty, from an empty folder, with an
    empty absolute HOME and nothing of this tree or its tests in the environment."""

    def __init__(self, home: Path, wheel: dict, *arguments, launcher: Path | None = None, fixture: str):
        self.wheel, self.launcher = wheel, launcher
        super().__init__(home, *arguments, fixture=fixture, size=FIXTURES[fixture], env=CLEAN_ENV)

    def game(self) -> list[str]:
        if self.launcher is not None:
            return [str(self.wheel["venv"] / "bin/python"), "-I", str(self.launcher)]
        return [str(self.wheel["venv"] / "bin/invaders")]

    def launch(self) -> subprocess.Popen:
        return subprocess.Popen(
            self.game() + self.arguments,
            cwd=self.wheel["empty"],
            env=self.environment,
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            start_new_session=True,
        )


def on_screen(terminal, text: str, seconds: float = UP) -> bool:
    deadline = time.monotonic() + seconds
    while text not in terminal.screen():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def again_after_guard(terminal, key: str, kitty: bool) -> None:
    if not kitty:
        terminal.wait(PTY_GUARD)
    terminal.tap(key)


def quit_installed(terminal, kitty: bool) -> None:
    """Esc, then Esc again past the repeat guard: QUIT, then the exit. On a legacy terminal the first Esc waits out
    the guard of the Esc that left the card before it."""
    again_after_guard(terminal, "escape", kitty)
    assert on_screen(terminal, "> QUIT"), terminal.screen()
    terminal.wait(PTY_GUARD)
    terminal.tap("escape")


@pytest.fixture(scope="module", params=list(FIXTURES))
def wheel_home(request, wheel):
    """One empty absolute HOME per fixture, shared by S2, S3 and S4 in that order."""
    home = wheel["work"] / f"home-{request.param}"
    home.mkdir()
    return request.param, home


def test_s2_wheel_first_launch_plays_pauses_and_saves_a_setting(wheel, wheel_home):
    fixture, home = wheel_home
    kitty = fixture == "kitty"
    terminal = Installed(home, wheel, fixture=fixture)
    try:
        assert on_screen(terminal, "> LAUNCH", 3.0 - (time.monotonic() - terminal.started)), terminal.screen()
        menu = time.monotonic() - terminal.started  # the menu within 3 s of the launch
        terminal.tap("enter")
        assert on_screen(terminal, "CONTACT"), terminal.screen()  # the intro: ACT I  CONTACT
        assert on_screen(terminal, "SCORE"), terminal.screen()
        deadline = time.monotonic() + UP
        while "CONTACT" in terminal.screen() and time.monotonic() < deadline:
            time.sleep(0.02)
        ended = time.monotonic()
        assert on_screen(terminal, "TIP  ", 3.0), terminal.screen()  # within 3 s of the intro's end
        tip = time.monotonic() - ended
        terminal.tap("p")
        assert on_screen(terminal, "PAUSED"), terminal.screen()
        terminal.tap("q")
        assert on_screen(terminal, "> LAUNCH"), terminal.screen()
        terminal.tap("escape")  # LAUNCH -> QUIT (the menu stops at its ends, PN-1) -> SETTINGS
        assert on_screen(terminal, "> QUIT"), terminal.screen()
        again_after_guard(terminal, "up", kitty)
        assert on_screen(terminal, "> SETTINGS"), terminal.screen()
        terminal.tap("enter")
        assert on_screen(terminal, "PALETTE"), terminal.screen()
        terminal.tap("down")  # PALETTE -> BACKGROUND -> EFFECTS
        again_after_guard(terminal, "down", kitty)
        terminal.wait(0.3)
        terminal.tap("right")  # FULL -> CALM
        assert on_screen(terminal, "CALM"), terminal.screen()
        terminal.tap("escape")
        assert until_text(terminal, lambda screen: "> SETTINGS" in screen and "PALETTE" not in screen), (
            terminal.screen()
        )
        quit_installed(terminal, kitty)
        assert terminal.finish(tape=False) == 0
    finally:
        terminal.close()
    files = sorted(str(path.relative_to(home)) for path in home.rglob("*") if path.is_file())
    assert files == profile_files()
    assert profile_of(home)["settings"]["effects"] == "calm"
    print(f"\nS2 {fixture}: menu {menu:.1f} s after launch, first tip {tip:.1f} s after the intro; files: {files}")


def until_text(terminal, condition, seconds: float = UP) -> bool:
    deadline = time.monotonic() + seconds
    while not condition(terminal.screen()):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def test_s3_wheel_staged_run_takes_initials_and_shows_the_board(wheel, wheel_home):
    fixture, home = wheel_home
    kitty = fixture == "kitty"
    launcher = wheel["work"] / f"staged_launch_{fixture}.py"  # outside the repo: the venv's package, not this tree
    launcher.write_text(STAGED_LAUNCHER)
    terminal = Installed(home, wheel, "--quickstart", launcher=launcher, fixture=fixture)
    try:
        assert on_screen(terminal, "NEW HIGH SCORE", 30.0), terminal.screen()
        assert on_screen(terminal, "Type letters"), terminal.screen()
        terminal.wait(0.5)
        for letter in "tim":
            terminal.tap(letter)
            terminal.wait(0.3)
        assert on_screen(terminal, "SAVES AS  TIM"), terminal.screen()
        terminal.tap("enter")
        assert on_screen(terminal, "Q menu"), terminal.screen()
        terminal.tap("q")
        assert on_screen(terminal, "> LAUNCH"), terminal.screen()
        terminal.tap("down")
        for _ in range(2):
            again_after_guard(terminal, "down", kitty)
        assert on_screen(terminal, "> FLIGHT RECORDS"), terminal.screen()
        terminal.tap("enter")
        assert until_text(terminal, lambda screen: re.search(r"TIM\s+\d", screen)), terminal.screen()
        again_after_guard(terminal, "enter", kitty)
        assert on_screen(terminal, "> FLIGHT RECORDS"), terminal.screen()
        quit_installed(terminal, kitty)
        assert terminal.finish(tape=False) == 0
    finally:
        terminal.close()
    profile = profile_of(home)
    assert [row["name"] for row in profile["scores"]] == ["TIM"] and profile["settings"]["tips_done"] is True


def test_s4_wheel_second_launch_has_no_tips_and_keeps_the_setting(wheel, wheel_home):
    fixture, home = wheel_home
    kitty = fixture == "kitty"
    terminal = Installed(home, wheel, fixture=fixture)
    try:
        assert on_screen(terminal, "> LAUNCH"), terminal.screen()
        terminal.tap("enter")
        assert on_screen(terminal, "SCORE"), terminal.screen()
        seen = until_text(terminal, lambda screen: "TIP  " in screen, 6.0)  # the intro and 4.5 s of wave 1
        assert not seen, terminal.screen()
        terminal.tap("p")
        assert on_screen(terminal, "PAUSED"), terminal.screen()
        terminal.tap("q")
        assert on_screen(terminal, "> LAUNCH"), terminal.screen()
        terminal.tap("escape")  # LAUNCH -> QUIT (the menu stops at its ends, PN-1) -> SETTINGS
        assert on_screen(terminal, "> QUIT"), terminal.screen()
        again_after_guard(terminal, "up", kitty)
        assert on_screen(terminal, "> SETTINGS"), terminal.screen()
        terminal.tap("enter")
        assert until_text(terminal, lambda screen: re.search(r"EFFECTS\s.*CALM", screen)), terminal.screen()
        terminal.tap("escape")
        assert until_text(terminal, lambda screen: "> SETTINGS" in screen and "PALETTE" not in screen), (
            terminal.screen()
        )
        quit_installed(terminal, kitty)
        assert terminal.finish(tape=False) == 0
    finally:
        terminal.close()
