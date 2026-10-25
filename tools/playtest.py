#!/usr/bin/env python3
"""The new-player panel's game (Phase 5 spec 15.3): one game behind a Unix socket, played one command at a time.

    python3 tools/playtest.py serve --run DIR --fixture apple-terminal-26 --seed 1 [--player-folder FOLDER]
        [--socket PATH] [--tree TREE]
    python3 tools/playtest.py send "press enter"

`serve` launches the game as a person does (bare `invaders`, a fresh HOME under DIR) at 80 x 24 on
tests/support/faketerm.py's FakeTerminal answering as the fixture's terminal, its keys from input.VirtualInput (macOS
auto-repeat on a legacy fixture). The game runs on a virtual clock that stands still between commands: each command
moves it on, then the reply is the screen. The game is the one the interpreter imports (the installed wheel), or
--tree's; the test support comes from this file's checkout.

`send CMD` is the whole interface a player has: it prints only the screen, as plain text. It finds its game by the
folder it is run from: the socket is $INVADERS_PLAYTEST_SOCKET if set, else one named after the working folder, which
`serve --player-folder FOLDER` listens on (default: serve's own working folder). So two runs at a time, each player
started in its own empty folder, send the same command line and each reaches its own game. The commands:

    press KEY [KEY ...]     taps: 60 ms each, 0.15 s apart, then 0.3 s
    hold SECONDS KEY[+KEY]  the keys held together for 0.1-10 s, then 0.3 s
    down KEY / up KEY       a key pressed (or let go) and left so, then 0.3 s
    wait SECONDS            0.1-10 s
    look                    the screen again; no time passes

Keys are generic names: a-z, 0-9, space, enter, escape, tab, backspace, up, down, left, right, f1-f12. On a legacy
fixture two taps of one key closer than the KeyGate's guard (0.70 s, term/repeat.py) would read as an auto-repeat,
which no person meets: the tool waits until GUARD_WAIT after a key's last event before it presses it again, and logs
each wait.

The hidden log (DIR/log.jsonl, which the player never sees) records every command, the screen's sha-256, how long
each took on the virtual clock, whether each key sent was named on the screen in front of it (R1's rule, spec 15.2:
its label() on screen, "Type letters" for letters and digits, "any key" for every key), the ground truth (scene,
context, mode, phase, wave, lives, score, kills, tick, and whether the screen shows a shooter's tell, which the
grader's question (c) needs) and the profile file whenever it changed; then the game's exit. `serve` started on an
existing log (a restart) empties HOME, replays the log and checks every screen's hash: a mismatch voids the run. It
refuses another run's log. SIGTERM, SIGHUP and ^C end it, and take the socket with it.
tools/playtest_grade.py grades a log. A run stops at LIMIT commands.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import json
import math
import os
import random
import re
import shutil
import signal
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLS, ROWS = 80, 24  # both fixtures, at k = 1 (spec 15.3: a plain-text dump of pixel art is not what a person sees)
FIXTURES = ("apple-terminal-26", "kitty")
SOCKET_ENV = "INVADERS_PLAYTEST_SOCKET"
STOPS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)  # each ends serve at once, whatever the game is doing
START = 1.0  # virtual seconds from launch to the first command: the title is up
TAP, GAP, SETTLE = 0.06, 0.15, 0.3  # a tap's length, the time between taps, the time after a command's last key
GUARD_WAIT = 0.70 + 0.1  # term/repeat.py GUARD (local) + 0.1 s: the least time between two taps of one key (legacy)
SPAN = (0.1, 10.0)  # hold and wait
LIMIT = 300  # commands a run may send (spec 15.3)
KEYS = frozenset(
    [*"abcdefghijklmnopqrstuvwxyz0123456789", "space", "enter", "escape", "tab", "backspace"]
    + ["up", "down", "left", "right", *(f"f{n}" for n in range(1, 13))]
)
ALIASES = {"esc": "escape", "return": "enter", "spacebar": "space", "bksp": "backspace", " ": "space"}
ALIASES |= {f"arrow{name}": name for name in ("up", "down", "left", "right")}
ALIASES |= {f"{name}arrow": name for name in ("up", "down", "left", "right")}
ARROWS = {"up": "↑", "down": "↓", "left": "←", "right": "→"}
WRITTEN_OUT = {"escape": "Escape", "backspace": "Backspace"}  # a short label's key in full, as ui.PANELS writes Escape
USAGE = (
    "commands: press KEY [KEY ...] | hold SECONDS KEY[+KEY] | down KEY | up KEY | wait SECONDS | look\n"
    "keys: a-z 0-9 space enter escape tab backspace up down left right f1-f12"
)


# --- commands -------------------------------------------------------------------------------------------------------


class CommandError(ValueError):
    pass


def key_name(word: str) -> str:
    name = word.strip().lower()
    name = ALIASES.get(name, name)
    if name not in KEYS:
        raise CommandError(f"unknown key: {word}")
    return name


def seconds(word: str) -> float:
    try:
        value = float(word)
    except ValueError:
        raise CommandError(f"not a number of seconds: {word}") from None
    if not (SPAN[0] <= value <= SPAN[1]) or not math.isfinite(value):
        raise CommandError(f"seconds must be {SPAN[0]:g}-{SPAN[1]:g}: {word}")
    return value


def parse(line: str) -> tuple[str, tuple]:
    """(verb, arguments) of a command line, or CommandError."""
    words = line.split()
    if not words:
        raise CommandError("empty command")
    verb, rest = words[0].lower(), words[1:]
    if verb == "press" and rest:
        return verb, tuple(key_name(word) for word in rest)
    if verb == "hold" and len(rest) == 2:
        return verb, (seconds(rest[0]), tuple(key_name(word) for word in rest[1].split("+")))
    if verb in ("down", "up") and len(rest) == 1:
        return verb, (key_name(rest[0]),)
    if verb == "wait" and len(rest) == 1:
        return verb, (seconds(rest[0]),)
    if verb == "look" and not rest:
        return verb, ()
    raise CommandError(f"not a command: {line.strip()}")


# --- what the screen names (R1, spec 15.2) --------------------------------------------------------------------------


def label_of(key: str) -> str:
    """The key as the game's hints write it (ui.label), or this file's copy of its rule for a game without one."""
    try:
        from terminal_invaders.ui import label

        return label(key)
    except (ImportError, ValueError):
        short = {"escape": "Esc", "backspace": "Bksp"}
        return short.get(key) or (key.upper() if len(key) == 1 or re.fullmatch(r"f\d+", key) else key.capitalize())


def named(key: str, screen: list[str]) -> bool:
    """Whether the screen in front of a key names it: its label as a word (a one-letter label in its case) or, for
    Esc and Bksp, the name written out (the pause panel's "P / Escape / Enter resume"), "Type letters" for a letter
    or digit, "any key" for every key, and "arrows" or the arrow glyph for an arrow."""
    text = "\n".join(screen)
    if re.search(r"\bany key\b", text, re.IGNORECASE):
        return True
    if len(key) == 1 and re.search(r"\btype letters\b", text, re.IGNORECASE):
        return True
    if key in ARROWS and (ARROWS[key] in text or re.search(r"\barrows?\b", text, re.IGNORECASE)):
        return True
    label = label_of(key)
    flags = 0 if len(label) == 1 else re.IGNORECASE
    return any(
        re.search(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", text, flags) is not None
        for word in (label, WRITTEN_OUT.get(key, label))
    )


def shows_run(screen: list[str], score: int) -> tuple[bool, bool]:
    """Whether a records screen shows a row with the run's score, and one with BOT and that score."""
    pattern = re.compile(rf"(?<![\d,]){score}(?![\d,])|(?<![\d,]){score:,}(?![\d,])")
    rows = [line for line in screen if pattern.search(line)]
    return bool(rows), any(re.search(r"(?<![A-Z])BOT(?![A-Z])", line) for line in rows)


def screen_hash(screen: list[str]) -> str:
    return hashlib.sha256("\n".join(screen).encode()).hexdigest()


def read_log(log: Path, fixture: str, seed: int) -> list[dict]:
    """A run's log, or SystemExit saying why it cannot be replayed: a line that is not a record (one cut short by a
    kill), or another run's header."""
    records = []
    for number, line in enumerate(log.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError:
                raise SystemExit(f"{log}: line {number} is not a log record (cut short?)") from None
    header = next((record["header"] for record in records if "header" in record), None)
    if header is None or header["fixture"] != fixture or header["seed"] != seed:
        raise SystemExit(f"{log}: not this run's log (fixture and seed differ)")
    return records


# --- the game, frozen between commands --------------------------------------------------------------------------------


class Gate:
    """FakeTerminal's clock gate (faketerm.py): the game's clock may run up to `allowed`, where it stands still
    until a command moves it on. Called in the game's thread; advance() in the server's."""

    def __init__(self, fake, allowed: float):
        self.fake, self.allowed = fake, allowed
        self.cond = threading.Condition()
        self.frozen = self.over = False

    def __call__(self, target: float) -> float | None:
        with self.cond:
            if self.allowed > self.fake.now:
                return self.allowed
            self.frozen = True
            self.cond.notify_all()
            while self.allowed <= self.fake.now:
                self.cond.wait()
            self.frozen = False
            return None

    def advance(self, until: float) -> None:
        """Let the game run to `until` and wait until it stands there (or has exited)."""
        with self.cond:
            self.allowed = max(self.allowed, until)
            self.cond.notify_all()
            while not self.over and not (self.frozen and self.fake.now >= self.allowed):
                self.cond.wait()

    def ended(self) -> None:
        with self.cond:
            self.over = True
            self.cond.notify_all()


class Game:
    """One launch of the game on a FakeTerminal, the keys of the commands typed into it, and the hidden log."""

    def __init__(self, run: Path, fixture: str, seed: int, log: Path):
        from terminal_invaders.input import VirtualInput
        from terminal_invaders.term.simkeys import PROFILES
        from tests.support.faketerm import FakeTerminal
        from tests.support.terminals import load

        self.run, self.fixture, self.seed, self.log = run, fixture, seed, log
        self.doc = load(fixture)
        self.kitty = self.doc["keys"]["protocol"] == "kitty"
        self.fake = FakeTerminal(self.doc, cols=COLS, rows=ROWS, rtt=0.001)
        self.fake.limit = math.inf
        self.fake.fd = os.open(os.devnull, os.O_RDWR)  # app.run closes it; FakeTerminal only compares it
        self.gate = self.fake.gate = Gate(self.fake, START)
        self.vin = VirtualInput(self.doc["keys"]["protocol"], PROFILES["macos-default"])
        self.fed = 0.0  # the virtual time up to which the keys' bytes are typed
        self.last: dict[str, float] = {}  # each key's last event (a legacy fixture's guard)
        self.session = None
        self.exit: int | None = None
        self.count = 0  # commands taken, the replayed ones included
        self.void: str | None = None
        self.profile_sha: str | None = None
        self.last_score: int | None = None  # the last run's score at its end (to find it on the records card)
        self.lock = threading.Lock()  # one command at a time
        self.writing = threading.Lock()  # one log line at a time (the exit's comes from the game's thread)

    # -- the keys -------------------------------------------------------------------------------------------------
    def screen(self) -> list[str]:
        return [line.rstrip() for line in self.fake.vt.lines()[:ROWS]]

    def run_to(self, until: float) -> None:
        """Type every key byte due before `until`, then let the game run to it."""
        until = max(until, self.fake.now)
        for arrival, data in self.vin.bytes_until(until):
            self.fake.type(data, at=arrival)
        self.fed = until
        self.gate.advance(until)

    def guarded(self, key: str, at: float, waits: list) -> float:
        """When the key may be pressed again on this terminal: at, or later on a legacy one (logged in waits)."""
        if self.kitty or key not in self.last:
            return at
        ready = self.last[key] + GUARD_WAIT
        if at < ready:
            waits.append({"key": key, "from": round(at, 4), "to": round(ready, 4)})
            return ready
        return at

    def check(self, verb: str, args: tuple) -> None:
        """A key is pressed only while it is up, and let go only while it is down."""
        pressed = args[1] if verb == "hold" else args if verb in ("press", "down") else ()
        held = [key for key in pressed if self.vin.down(key)]
        if held:
            raise CommandError(f"{held[0]} is down (send: up {held[0]})")
        if verb == "up" and not self.vin.down(args[0]):
            raise CommandError(f"{args[0]} is not down")

    def keys(self, verb: str, args: tuple) -> tuple[list[dict], list[dict]]:
        """Send a command's keys; (each key sent with its time, whether the screen in front of it named it and the
        scene it was pressed in, the guard's waits)."""
        sent, waits = [], []
        now = self.fake.now

        def mark(key: str, at: float) -> None:
            self.run_to(at)
            sent.append({"key": key, "at": round(at, 4), "named": named(key, self.screen()), "truth": self.state()})

        if verb == "press":
            at = now
            for key in args:
                at = self.guarded(key, at, waits)
                mark(key, at)
                self.vin.tap(key, at, TAP)
                self.last[key] = at + TAP
                at += GAP
            self.run_to(at - GAP + TAP + SETTLE)
        elif verb == "hold":
            length, keys = args
            at = max(self.guarded(key, now, waits) for key in keys)
            self.run_to(at)
            for key in keys:
                sent.append({"key": key, "at": round(at, 4), "named": named(key, self.screen()), "truth": self.state()})
                self.vin.press(key, at)
            for key in keys:
                self.vin.release(key, at + length)
                self.last[key] = at + length
            self.run_to(at + length + SETTLE)
        elif verb == "down":
            (key,) = args
            at = self.guarded(key, now, waits)
            mark(key, at)
            self.vin.press(key, at)
            self.last[key] = math.inf if not self.kitty else at  # held: its repeats go on until it is let go
            self.run_to(at + SETTLE)
        elif verb == "up":
            (key,) = args
            self.vin.release(key, now)
            if self.last.get(key) == math.inf:
                self.last[key] = now
            self.run_to(now + SETTLE)
        elif verb == "wait":
            self.run_to(now + args[0])
        return sent, waits

    # -- what the log records ---------------------------------------------------------------------------------------
    def state(self) -> dict:
        """The scene in front of a key, so the grader sees a pause or the menu that a command passes through (a
        `press p p` pauses and resumes in one command): the truth's scene, pause and game mode."""
        session = self.session
        if session is None:
            return {"scene": None}
        state = {"scene": session.scene, "pause_reason": session.pause_reason}
        state |= {"demo": getattr(session, "attract", None) is not None, "replay": bool(session.replay)}
        if session.pilots:
            state["game"] = {"mode": session.pilot.game.config.mode}
        return state

    def tell(self) -> bool:
        """Whether the screen shows a shooter in its tell pose, the one warning of a shot that the game draws: an enemy
        whose kind has one (sprites.TELLS) in state "telegraph" (the Telegraph tip's test, tutorial.py), in the game
        the screen draws: play, under a pause or countdown panel too, or the title's demo; never a card or the
        too-small notice. Pellets come with no tell, so in Campaign the first is wave 5's gunners'."""
        from terminal_invaders import sprites, ui

        drawn = self.session
        if drawn.scene == "attract":
            drawn = getattr(drawn, "attract", None)
        if drawn is None or not drawn.pilots or drawn.scene in ui.CARDS or not drawn.fits:
            return False
        if getattr(ui, "is_card", lambda session: False)(drawn):  # Phase 5's results card
            return False
        tells, aliens = getattr(sprites, "TELLS", {}), getattr(drawn.pilot.game, "aliens", ())
        return any(alien.state == "telegraph" and alien.kind in tells for alien in aliens)

    def truth(self) -> dict:
        session = self.session
        if session is None:
            return {"scene": None, "tell": False}
        game = session.pilot.game if session.pilots else None
        player = getattr(game, "player", None)
        found = {
            "scene": session.scene,
            "context": session.context,
            "mode": session.mode,
            "pause_reason": session.pause_reason,
            "countdown": session.countdown_at is not None,
            "demo": getattr(session, "attract", None) is not None,
            "replay": bool(session.replay),
            "pilots": len(session.pilots),
            "selection": session.selection,
            "tell": self.tell(),
        }
        if game is not None:
            found["game"] = {
                "mode": game.config.mode,
                "phase": game.phase,
                "wave": getattr(game, "wave", None),
                "lives": getattr(player, "lives", None),
                "score": getattr(game, "score", None),
                "kills": getattr(game, "kills", None),
                "tick": game.tick,
            }
            result = getattr(game, "result", None)
            if callable(result) and game.phase in ("game_over", "victory"):
                with contextlib.suppress(Exception):
                    found["game"]["score"] = result().get("score", found["game"]["score"])
        return found

    def profile(self) -> dict | None:
        """The profile file's scores and settings when it changed since the last look, else None."""
        from terminal_invaders.storage import profile_path

        path = profile_path()
        try:
            blob = path.read_bytes()
        except OSError:
            return None
        sha = hashlib.sha256(blob).hexdigest()
        if sha == self.profile_sha:
            return None
        self.profile_sha = sha
        try:
            data = json.loads(blob)
        except ValueError:
            return {"path": str(path), "unreadable": True}
        return {"path": str(path), "scores": data.get("scores"), "settings": data.get("settings")}

    def write(self, record: dict) -> None:
        with self.writing, self.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")

    # -- one command ------------------------------------------------------------------------------------------------
    def command(self, line: str, replayed: dict | None = None) -> str:
        """Do one command and return the screen (or a line saying why not); log it unless it is a replay."""
        with self.lock:
            if self.exit is not None:
                return "The game has exited."
            try:
                verb, args = parse(line)
                self.check(verb, args)
            except CommandError as exc:
                if replayed is None:
                    self.write({"n": None, "command": line, "error": str(exc)})
                return f"error: {exc}\n{USAGE}"
            if self.count >= LIMIT:
                if replayed is None:
                    self.write({"n": None, "command": line, "refused": f"{LIMIT} commands"})
                return f"The run is over: {LIMIT} commands."
            self.count += 1
            self.gate.advance(START)
            start = self.fake.now
            sent, waits = self.keys(verb, args)
            screen = self.screen()
            truth = self.truth()
            game = truth.get("game") or {}
            if game.get("phase") in ("game_over", "victory") and game.get("score") is not None:
                self.last_score = game["score"]
            if truth.get("scene") == "records" and self.last_score is not None:
                truth["shows_run"], truth["shows_bot"] = shows_run(screen, self.last_score)
            record = {
                "n": self.count,
                "command": line.strip(),
                "start": round(start, 4),
                "end": round(self.fake.now, 4),
                "keys": sent,
                "waits": waits,
                "screen_sha256": screen_hash(screen),
                "truth": truth,
            }
            profile = self.profile()
            if profile is not None:
                record["profile"] = profile
            if self.exit is not None:
                record["exited"] = self.exit
            if replayed is not None:
                if replayed.get("screen_sha256") != record["screen_sha256"] and self.void is None:
                    self.void = f"command {self.count} ({line.strip()}): the screen differs from the log"
                    self.write({"void": self.void})
            else:
                self.write(record)
            if self.exit is not None:
                return "The game has exited."
            return "\n".join(screen)

    def replay(self) -> int:
        """Play the log's commands again, checking each screen against it; the number replayed."""
        if not self.log.exists():
            return 0
        records = read_log(self.log, self.fixture, self.seed)
        if any("void" in record for record in records):
            self.void = next(record["void"] for record in records if "void" in record)
        done = 0
        for record in records:
            if isinstance(record.get("n"), int):
                self.command(record["command"], replayed=record)
                done += 1
        return done

    def finished(self, code: int) -> None:
        self.exit = code
        self.gate.ended()
        record = {"exit": code, "at": round(self.fake.now, 4)}
        profile = self.profile()
        if profile is not None:
            record["profile"] = profile
        self.write(record)


# --- serve and send ---------------------------------------------------------------------------------------------------


def listen(game: Game, path: Path) -> None:
    """Replay the log, then answer commands on the socket, one connection each, until the process ends."""
    try:
        game.replay()
    except BaseException as exc:  # a thread's SystemExit is silent: say why and end serve rather than hang
        print(f"playtest serve: {exc}", file=sys.stderr, flush=True)
        os._exit(2)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)
    (path.parent / f"{path.name}.ready").write_text(str(os.getpid()))
    while True:
        connection, _ = server.accept()
        with connection:
            try:
                connection.settimeout(30.0)  # a client that connects and never finishes its line
                data = b""
                while not data.endswith(b"\n"):
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                continue
            line = data.decode("utf-8", "replace").strip()
            if not data:  # a probe that connects and closes: no command, nothing to log or answer
                continue
            if line == "__shutdown__":  # the run's end (the panel's harness sends it); the socket goes with it
                with contextlib.suppress(OSError):
                    connection.sendall(b"bye\n")
                unlink_socket(path)
                os._exit(0)
            try:
                reply = game.command(line)
            except Exception as exc:  # the server stays up; the log has what was done
                reply = f"error: {type(exc).__name__}: {exc}"
            with contextlib.suppress(OSError):  # the client left before its reply: the command is logged all the same
                connection.sendall(reply.encode() + b"\n")


def unlink_socket(path: Path) -> None:
    for left in (path, path.parent / f"{path.name}.ready"):
        with contextlib.suppress(OSError):
            left.unlink()


def stop_on_signals(game: Game, path: Path) -> None:
    """SIGTERM, SIGHUP and SIGINT end serve at once: the game's own handler defers the first one to the loop's next
    turn, which never comes while the clock gate holds the game between commands."""

    def stop(signum, frame):
        if game.writing.acquire(timeout=2.0):  # a log line being written finishes first
            game.writing.release()
        unlink_socket(path)
        os._exit(128 + signum)

    for signum in STOPS:
        signal.signal(signum, stop)


def keep_signals():
    """signal.signal for the game's run: STOPS stay serve's. The game's own handlers for them (cli.main's, then its
    TerminalSession's) would defer or catch a signal, and the frozen game never takes the turn that acts on it. They
    are noted and never installed, so there is no moment, from serve's start to its end, when a signal is not
    serve's: none between the ready file and the game's first frame (gate G5, run 1: a ^C there hung serve). Other
    signals reach the game's handlers."""
    real, noted = signal.signal, {}

    def install(signum, handler):
        if signum not in STOPS:
            return real(signum, handler)
        previous = noted.get(signum, signal.getsignal(signum))
        noted[signum] = handler
        return previous

    return install


def socket_for(folder: Path) -> Path:
    """The socket of the game a player working in `folder` reaches (short enough for AF_UNIX)."""
    key = hashlib.sha256(str(folder.resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"invaders-playtest-{key}.sock"


def socket_path(given: str | None, folder: Path) -> Path:
    return Path(given or os.environ.get(SOCKET_ENV) or socket_for(folder))


def serve(args) -> int:
    if args.tree is not None:
        sys.path.insert(0, str(args.tree.resolve()))
    sys.path.append(str(ROOT))  # the test support (faketerm, terminals), after the game's own package
    run = args.run.resolve()
    home, log = run / "home", run / "log.jsonl"
    if log.exists():  # a restart: the replay rebuilds the profile from an empty HOME, as the first pass began on one
        try:
            read_log(log, args.fixture, args.seed)  # another run's log (a wrong --seed) keeps its profile
        except SystemExit as exc:
            print(f"playtest serve: {exc}", file=sys.stderr, flush=True)
            return 2
        shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        INVADERS_AUDIO="none",
    )
    os.environ.pop("INVADERS_ATTRACT_IDLE", None)  # the title's own 30 s
    from unittest import mock

    import terminal_invaders
    from terminal_invaders import app, cli, ui

    game = Game(run, args.fixture, args.seed, log)
    if not log.exists():
        header = {"fixture": args.fixture, "seed": args.seed, "cols": COLS, "rows": ROWS, "limit": LIMIT}
        header |= {"game": str(Path(terminal_invaders.__file__).resolve().parent)}
        header |= {"version": getattr(terminal_invaders, "__version__", None)}
        game.write({"header": header})
    original_play = app.play

    def play(session, term, **kwargs):
        game.session = session
        return original_play(session, term, **kwargs)

    fake, doc = game.fake, game.doc
    seeded = random.Random(args.seed)
    patches = [
        mock.patch.object(app, "play", play),
        mock.patch.object(app, "open_tty", lambda *a: fake.fd),
        mock.patch.object(app, "user_env", lambda *a: dict(doc["env"])),
        mock.patch.object(app, "TerminalSession", functools.partial(app.TerminalSession, ops=fake.ops())),
        mock.patch.object(cli, "require_terminal", lambda command: None),
        mock.patch.object(ui.secrets, "randbits", seeded.getrandbits),
        mock.patch.object(signal, "signal", keep_signals()),  # after stop_on_signals, below
    ]
    where = socket_path(args.socket, args.player_folder or Path.cwd())
    stop_on_signals(game, where)
    threading.Thread(target=listen, args=(game, where), daemon=True).start()
    for patch in patches:
        patch.start()
    try:
        code = cli.main([])
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    finally:
        for patch in reversed(patches):
            patch.stop()
    game.finished(code)
    threading.Event().wait()  # keep answering ("The game has exited.") until the run is stopped
    return 0


def send(args) -> int:
    path = socket_path(args.socket, Path.cwd())
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            client.sendall(" ".join(args.command).encode() + b"\n")
            data = b""
            while chunk := client.recv(65536):
                data += chunk
    except OSError:
        print("The game is not running.")
        return 1
    print(data.decode("utf-8", "replace").rstrip("\n"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    server = sub.add_parser("serve", help="hold one game behind the socket")
    server.add_argument("--run", type=Path, required=True, help="the run's folder: its log and the game's HOME")
    server.add_argument("--fixture", choices=FIXTURES, required=True)
    server.add_argument("--seed", type=int, required=True)
    server.add_argument("--player-folder", type=Path, help="the folder the player sends from (default: here)")
    server.add_argument("--socket", help=f"the socket (default ${SOCKET_ENV}, else the player folder's own)")
    server.add_argument("--tree", type=Path, help="play this source tree instead of the installed game")
    client = sub.add_parser("send", help="one command; prints the screen")
    client.add_argument("command", nargs="+")
    client.add_argument("--socket", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    return serve(args) if args.verb == "serve" else send(args)


if __name__ == "__main__":
    raise SystemExit(main())
