"""Curses presentation and session orchestration; the simulation never reads a clock."""

from __future__ import annotations

import curses
import math
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .engine import Game
from .model import HEIGHT, TICK_RATE, WIDTH, Config
from .replay import Recorder
from .storage import ACHIEVEMENTS, board_key

MIN_HEIGHT = HEIGHT + 4
MODES = ("classic", "endless", "daily", "duel")
DIFFICULTIES = ("easy", "normal", "hard")
TERMINAL = {"game_over", "victory"}
ENTER = {10, 13, curses.KEY_ENTER}
ACTIONS = {
    curses.KEY_LEFT: "left",
    ord("a"): "left",
    ord("h"): "left",
    curses.KEY_RIGHT: "right",
    ord("d"): "right",
    ord("l"): "right",
    ord(" "): "fire",
    curses.KEY_UP: "fire",
    ord("w"): "fire",
    ord("b"): "bomb",
    ord("x"): "bomb",
    ord("f"): "autofire",
}
HELP = (
    ("MOVE", "Left / Right arrows, A / D, or H / L"),
    ("FIRE", "Space, Up, or W   |   F toggles autofire"),
    ("BOMB", "B or X clears danger; supplies are limited"),
    ("PAUSE", "P or Escape   |   M toggles sound"),
    ("SESSION", "R restarts   |   Q returns to the menu"),
    ("UPGRADES", "R rapid  S spread  + shield  T slow  L repair"),
    ("SCORE", "Chain kills for combos; hunt the mystery ship."),
    ("CLASSIC", "A complete campaign with escalating boss fights."),
    ("ENDLESS", "Keep climbing until the fleet overwhelms you."),
    ("DAILY", "One shared UTC seed, normal difficulty."),
    ("DUEL", "Two pilots, equal seeds. Swap after each life lost."),
)


class FixedClock:
    """Bounded fixed steps, with all paused time discarded."""

    def __init__(self, now: float):
        self.last = now
        self.accumulator = 0.0
        self.running = True

    def advance(self, now: float, running: bool) -> int:
        elapsed, self.last = max(0.0, now - self.last), now
        if not running or not self.running:
            self.running = running
            self.accumulator = 0.0
            return 0
        self.accumulator += min(elapsed, 0.25)
        due = int((self.accumulator + 1e-10) * TICK_RATE)
        self.accumulator -= due / TICK_RATE
        return min(due, 8)


class Controls:
    """Brief held-key windows smooth terminal repeat; one-shot actions stay discrete."""

    def __init__(self):
        self.held = {}
        self.pending = set()

    def press(self, action: str, now: float) -> None:
        if action in {"left", "right", "fire"}:
            if action != "fire":
                self.held.pop("right" if action == "left" else "left", None)
            self.held[action] = now + 0.10
        else:
            self.pending.add(action)

    def sample(self, now: float) -> tuple[str, ...]:
        self.held = {action: until for action, until in self.held.items() if until >= now}
        actions = tuple(sorted(self.held.keys() | self.pending))
        self.pending.clear()
        return actions


@dataclass
class Pilot:
    game: Game
    name: str
    recorder: Recorder | None = None
    finished: bool = False
    unlocked: tuple[str, ...] = ()


class Session:
    """One screen state machine, including independent alternating duel runs."""

    def __init__(self, options, profile, audio, replay=None):
        self.options, self.profile, self.audio, self.replay = options, profile, audio, replay
        self.mode = "duel" if options.two_player else options.mode
        self.difficulty = options.difficulty
        self.scene, self.selection, self.page = "menu", 0, 0
        self.pilots, self.active = [], 0
        self.controls = Controls()
        self.note, self.quit = "", False
        self.replay_verified = None
        if replay or options.quickstart:
            self.start()

    @property
    def pilot(self):
        return self.pilots[self.active] if self.pilots else None

    def start(self):
        self.save_replay()
        seed = self.options.seed if self.options.fixed_seed else secrets.randbits(32)
        config = self.replay.config if self.replay else self.configuration(seed)
        count = 2 if self.mode == "duel" and not self.replay else 1
        self.pilots = [
            Pilot(
                Game(config),
                f"{self.options.name[:8]}-{n + 1}" if count == 2 else self.options.name,
                Recorder(config) if self.options.record and not self.replay else None,
            )
            for n in range(count)
        ]
        self.active, self.replay_verified = 0, None
        self.change_scene("handoff" if count == 2 else "playing")

    def configuration(self, seed=None):
        if self.mode == "daily":
            seed = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
        return Config(
            difficulty="normal" if self.mode == "daily" else self.difficulty,
            mode="classic" if self.mode == "duel" else self.mode,
            seed=self.options.seed if seed is None else seed,
        )

    def change_scene(self, scene):
        self.scene = scene
        self.controls = Controls()

    def save_replay(self):
        if self.pilot and self.pilot.recorder:
            try:
                self.pilot.recorder.save(self.options.record, self.pilot.game)
                self.note = ""
            except (OSError, ValueError) as exc:
                self.note = f"Replay not saved: {exc}"

    def menu(self):
        self.save_replay()
        self.change_scene("menu")

    def cycle(self, field, values, delta):
        value = values[(values.index(getattr(self, field)) + delta) % len(values)]
        setattr(self, field, value)

    def menu_action(self, delta=1):
        handlers = (
            self.start,
            lambda: self.cycle("mode", MODES, delta),
            lambda: self.cycle("difficulty", DIFFICULTIES, delta),
            lambda: self.change_scene("records"),
            lambda: self.change_scene("help"),
            lambda: setattr(self, "quit", True),
        )
        handlers[self.selection]()

    def handle(self, key, now):
        if ord("A") <= key <= ord("Z"):
            key += ord("a") - ord("A")
        if key == curses.KEY_F1:
            self.options.show_fps = not self.options.show_fps
            return
        if key == curses.KEY_RESIZE:
            self.controls = Controls()
            return
        if key == ord("m"):
            self.audio.toggle()
            return
        if self.replay:
            if key in {ord("q"), 27}:
                self.quit = True
            elif key == ord("p") and self.scene in {"playing", "paused"}:
                self.change_scene("paused" if self.scene == "playing" else "playing")
            return
        if self.scene == "menu":
            if key in {curses.KEY_UP, ord("k"), ord("w")}:
                self.selection = (self.selection - 1) % 6
            elif key in {curses.KEY_DOWN, ord("j"), ord("s")}:
                self.selection = (self.selection + 1) % 6
            elif key in ENTER | {ord(" ")} or key in {curses.KEY_LEFT, curses.KEY_RIGHT} and self.selection in {1, 2}:
                self.menu_action(-1 if key == curses.KEY_LEFT else 1)
            elif key == ord("q"):
                self.quit = True
            return
        if self.scene in {"help", "records"}:
            if key in {curses.KEY_LEFT, curses.KEY_RIGHT, ord(" ")} and self.scene == "records":
                self.page = 1 - self.page
            elif key in ENTER | {27, ord("q"), ord("h")}:
                self.change_scene("menu")
            return
        commands = {ord("q"): self.menu, ord("r"): self.start}
        if key in commands:
            commands[key]()
        elif key in {27, ord("p")} and self.scene in {"playing", "paused"}:
            self.change_scene("paused" if self.scene == "playing" else "playing")
        elif key in ENTER and self.scene in {"handoff", "paused", "results"}:
            self.change_scene("playing") if self.scene != "results" else self.start()
        elif self.scene == "playing" and key in ACTIONS:
            self.controls.press(ACTIONS[key], now)

    def step(self, now):
        if self.scene != "playing":
            return
        pilot = self.pilot
        game = pilot.game
        if self.replay and game.tick >= self.replay.ticks:
            self.finish_replay()
            return
        actions = self.replay.actions_at(game.tick) if self.replay else self.controls.sample(now)
        if pilot.recorder:
            pilot.recorder.append(actions)
        lives = game.player.lives
        game.step(actions)
        self.audio.update(game.events, playing=True)
        if self.replay:
            if game.tick >= self.replay.ticks or game.phase in TERMINAL:
                self.finish_replay()
            return
        if game.phase in TERMINAL and not pilot.finished:
            pilot.finished = True
            pilot.unlocked = tuple(self.profile.record(game.result(), game.config, pilot.name))
            self.save_replay()
        if all(p.finished for p in self.pilots):
            self.change_scene("results")
        elif len(self.pilots) == 2 and (pilot.finished or game.player.lives < lives):
            other = 1 - self.active
            if not self.pilots[other].finished:
                self.save_replay()
                self.active = other
                self.change_scene("handoff")

    def finish_replay(self):
        self.replay_verified = self.replay.verify(self.pilot.game)
        self.change_scene("results")


class Painter:
    """Clipped ASCII drawing on curses' diffed virtual screen."""

    def __init__(self, screen, options):
        self.screen, self.options = screen, options
        self.colors = {}
        if not options.mono and curses.has_colors():
            self.init_colors()
        self.resize()

    def init_colors(self):
        try:
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            for pair, color in enumerate(
                (
                    curses.COLOR_CYAN,
                    curses.COLOR_GREEN,
                    curses.COLOR_YELLOW,
                    curses.COLOR_RED,
                    curses.COLOR_MAGENTA,
                    curses.COLOR_WHITE,
                ),
                1,
            ):
                curses.init_pair(pair, color, background)
                self.colors[pair] = curses.color_pair(pair)
        except curses.error:
            self.colors.clear()

    def resize(self):
        self.rows, self.columns = self.screen.getmaxyx()
        self.top = max(0, (self.rows - MIN_HEIGHT) // 2)
        self.left = max(0, (self.columns - WIDTH) // 2)
        return self.columns >= WIDTH and self.rows >= MIN_HEIGHT

    def text(self, y, x, text, color=6, bold=False, dim=False):
        y, x, text = self.top + y, self.left + x, str(text)
        if not 0 <= y < self.rows or x >= self.columns:
            return
        if x < 0:
            text, x = text[-x:], 0
        text = text[: max(0, self.columns - x)]
        attr = self.colors.get(color, 0) | (curses.A_BOLD if bold else 0) | (curses.A_DIM if dim else 0)
        if text:
            try:
                self.screen.addstr(y, x, text, attr)
            except curses.error:
                pass  # curses raises after successfully drawing the bottom-right cell.

    def center(self, y, text, **style):
        self.text(y, max(0, (WIDTH - len(text)) // 2), text, **style)

    def sprite(self, x, y, text, color=6, bold=False):
        if 0 <= round(y) < HEIGHT:
            start = round(x) - len(text) // 2
            clipped = text[max(0, -start) : max(0, WIDTH - start)]
            self.text(round(y) + 2, max(0, start), clipped, color, bold)

    def panel(self, title, lines):
        width = min(56, max(34, *(len(line) + 4 for line in lines), len(title) + 4))
        x, y = (WIDTH - width) // 2, (MIN_HEIGHT - len(lines) - 4) // 2
        for row in range(len(lines) + 4):
            self.text(y + row, x, " " * width)
        self.text(y, x, "+" + "-" * (width - 2) + "+", 1)
        self.center(y + 1, title, color=1, bold=True)
        for row, line in enumerate(lines, y + 3):
            self.center(row, line)
        self.text(y + len(lines) + 3, x, "+" + "-" * (width - 2) + "+", 1)

    def menu(self, session):
        self.center(1, "T E R M I N A L", color=1, bold=True)
        for row, line in enumerate(
            (
                " ___ _  ___   ___   ___  ___ ___  ___",
                "|_ _| \\| \\ \\ / /_\\ |   \\| __| _ \\/ __|",
                " | || .` |\\ V / _ \\| |) | _||   /\\__ \\",
                "|___|_|\\_| \\_/_/ \\_\\___/|___|_|_\\|___/",
            ),
            3,
        ):
            self.center(row, line, color=2, bold=True)
        self.center(8, "[o]       /W\\       <X>       /W\\       [o]", color=5)
        self.center(10, "EARTH NEEDS ANOTHER QUARTER", dim=True)
        values = (
            "LAUNCH",
            f"MODE         < {session.mode.upper():^9} >",
            f"DIFFICULTY   < {('normal' if session.mode == 'daily' else session.difficulty).upper():^9} >",
            "FLIGHT RECORDS",
            "HOW TO PLAY",
            "QUIT",
        )
        for index, value in enumerate(values):
            selected = index == session.selection
            self.text(12 + index, 13, ("> " if selected else "  ") + value, 1 if selected else 6, selected)
        descriptions = {
            "classic": "A finite campaign. Every wave brings a new threat.",
            "endless": "No final wave. How long can you hold the line?",
            "daily": "Today's UTC seed. One fleet for every pilot.",
            "duel": "Two pilots. Same fleet. Alternate after a lost life.",
        }
        self.center(19, descriptions[session.mode], color=3)
        self.center(21, "Up/Down select   Left/Right change   Enter launch", dim=True)
        self.footer(session)

    def footer(self, session):
        sound = "ON" if getattr(session.audio, "enabled", False) else "OFF"
        self.text(23, 0, f"PILOT {session.options.name}  |  SOUND {sound} [M]", dim=True)
        message = session.note or session.profile.error
        if message:
            self.center(22, str(message)[:WIDTH], color=3)

    def help(self, session):
        self.center(1, "FLIGHT MANUAL", color=1, bold=True)
        for row, (label, detail) in enumerate(HELP, 4):
            self.text(row, 1, f"{label:<9}", 3, True)
            self.text(row, 11, detail)
        self.center(18, "Scores save automatically when a run finishes.", dim=True)
        self.center(19, "Small windows and pause stop the entire simulation.", dim=True)
        self.center(21, "Enter / Escape to return", color=1)
        self.footer(session)

    def records(self, session):
        data = session.profile.data
        config = session.configuration()
        title = f"FLIGHT RECORDS - {config.mode.upper()} / {config.difficulty.upper()}"
        self.center(1, title if not session.page else "CAREER & ACHIEVEMENTS", color=1, bold=True)
        if not session.page:
            self.text(3, 1, "PILOT          SCORE  WAVE  MODE       DIFFICULTY", 3)
            scores = [score for score in data.get("scores", []) if board_key(score) == board_key(config)]
            scores = sorted(scores, key=lambda score: score.get("score", 0), reverse=True)[:13]
            for row, score in enumerate(scores, 5):
                self.text(
                    row,
                    1,
                    f"{score.get('name', 'PILOT')[:12]:<12} {score.get('score', 0):>7}"
                    f"  {score.get('wave', 0):>3}  {score.get('mode', ''):<10} {score.get('difficulty', '')}",
                )
            if not scores:
                self.center(10, "Your first flight belongs here.", dim=True)
        else:
            stats = data.get("stats", {})
            shots, hits = stats.get("shots", 0), stats.get("hits", 0)
            self.text(4, 4, f"RUNS {stats.get('runs', 0):>6}     KILLS {stats.get('kills', 0):>7}", 2)
            self.text(6, 4, f"ACCURACY {100 * hits / max(1, shots):5.1f}%   BEST COMBO {stats.get('best_combo', 0)}", 2)
            self.text(8, 4, f"FLIGHT TIME {stats.get('ticks', 0) / TICK_RATE / 60:.1f} minutes", 2)
            achievements = data.get("achievements", [])
            for index, achievement in enumerate(achievements[:16]):
                self.text(11 + index // 2, 3 + 28 * (index % 2), "* " + ACHIEVEMENTS[achievement][0][:24], 3)
            if not achievements:
                self.center(13, "Achievements unlock as you play.", dim=True)
        self.center(21, "Left/Right switch page   Enter / Escape to return", color=1)
        self.footer(session)

    def arena(self, session, fps):
        game, player = session.pilot.game, session.pilot.game.player
        best = session.profile.best(game.config)
        label = f"P{session.active + 1}" if len(session.pilots) > 1 else "SCORE"
        self.text(0, 0, f"{label} {game.score:07d}   HI {max(best, game.score):07d}", 2, True)
        self.text(0, 39, f"WAVE {game.wave:02d}  {game.config.mode.upper()}", 1, True)
        self.text(1, 0, f"LIVES {'^' * max(0, player.lives):<8} BOMBS {player.bombs}  COMBO x{game.combo}", 3)
        self.text(1, 36, f"GUN {player.weapon}", 1)
        if self.options.show_fps:
            self.text(1, 48, f"{fps:5.1f} FPS", dim=True)
        else:
            self.text(1, 44, f"CHARGE {game.bomb_charge:02d}/18", dim=True)
        for index in range(30):
            x, y = (index * 37 + 11) % WIDTH, (index * 13 + 3) % HEIGHT
            self.text(y + 2, x, ".", dim=True)
        for (x, y), hp in game.bunkers.items():
            self.sprite(x, y, "#" if hp > 1 else ":", 2)
        frame = 0 if self.options.reduced_motion else (game.tick // 20) % 2
        for alien in game.aliens:
            sprites = {
                "scout": ("/W\\", "\\W/"),
                "armor": ("[X]", "{X}"),
                "guard": ("{#}", "[#]"),
                "diver": ("<V>", "<v>"),
            }
            sprite = sprites.get(alien.kind, ("[o]", "{o}"))[frame]
            self.sprite(alien.x, alien.y, sprite, 4 if alien.diving else 5, alien.hp > 1)
        if game.ufo:
            self.sprite(game.ufo.x, game.ufo.y, "=<=>=", 3, True)
        if game.boss:
            boss = game.boss
            self.sprite(boss.x, boss.y, "[/=W=\\]", 4, True)
            bar = round(12 * boss.hp / max(1, boss.max_hp))
            self.center(2, "BOSS [" + "=" * bar + " " * (12 - bar) + "]", color=4)
        for pickup in game.pickups:
            symbol = {"rapid": "R", "spread": "S", "shield": "+", "slow": "T", "repair": "L"}.get(pickup.kind, "?")
            self.sprite(pickup.x, pickup.y, "[" + symbol + "]", 3, True)
        for shot in game.shots:
            self.sprite(shot.x, shot.y, "|" if shot.friendly else "!", 1 if shot.friendly else 4)
        if not player.invulnerable or self.options.reduced_motion or game.tick % 12 < 7:
            self.sprite(player.x, player.y, "{A}" if player.shield else "/A\\", 1, True)
        if not self.options.reduced_motion:
            for effect in game.effects:
                self.sprite(effect.x, effect.y, effect.text, 3)
        self.text(22, 0, "AD/arrows Move  Space Fire  B Bomb  F Auto  P Pause  Q Menu", dim=True)
        buffs = "  ".join(
            f"{name.upper()} {math.ceil(ticks / TICK_RATE)}s" for name, ticks in game.powerups.items() if ticks > 0
        )
        self.text(23, 0, ("AUTO " + ("ON" if game.autofire else "OFF") + "  " + buffs)[:WIDTH], 1)
        if game.phase == "wave":
            self.panel(f"WAVE {game.wave:02d}", [game.wave_name, "READY YOUR DEFENSES"])
        if session.replay:
            self.text(22, 0, "REPLAY  P Pause  Q Exit".ljust(WIDTH), 3)

    def results(self, session):
        game = session.pilot.game
        if session.replay:
            title = "REPLAY VERIFIED" if session.replay_verified else "REPLAY MISMATCH"
            lines = [f"SCORE {game.score:,}    WAVE {game.wave}", "Q / Escape to exit"]
        elif len(session.pilots) == 2:
            first, second = session.pilots
            winner = max(session.pilots, key=lambda pilot: pilot.game.score)
            title = "DUEL DRAW" if first.game.score == second.game.score else f"{winner.name} WINS"
            lines = [f"{pilot.name}: {pilot.game.score:,}" for pilot in session.pilots]
            lines += ["Enter rematch   Q menu"]
        else:
            title = "EARTH SAVED" if game.phase == "victory" else "SIGNAL LOST"
            accuracy = 100 * game.hits / max(1, game.shots_fired)
            lines = [
                f"SCORE {game.score:,}    WAVE {game.wave}",
                f"{game.kills} kills   {accuracy:.0f}% accuracy   x{game.best_combo} combo",
            ]
            lines += ["UNLOCKED " + name for name in session.pilot.unlocked[:2]]
            lines += ["Enter fly again   Q menu"]
        lines.insert(-1, f"SEED {game.config.seed}")
        self.panel(title, lines)
        if session.note or session.profile.error:
            self.center(23, str(session.note or session.profile.error)[:WIDTH], color=3)

    def draw(self, session, fps):
        self.screen.erase()
        if not self.resize():
            self.text(max(0, self.rows // 2 - 1), 0, "RESIZE TO AT LEAST 60 x 24", 3, True)
            self.text(max(0, self.rows // 2), 0, f"Current: {self.columns} x {self.rows} | Paused")
        elif session.scene in {"menu", "help", "records"}:
            getattr(self, session.scene)(session)
        else:
            self.arena(session, fps)
            if session.scene == "paused":
                self.panel(
                    "PAUSED",
                    [
                        f"SEED {session.pilot.game.config.seed}",
                        "P / Escape / Enter resume",
                        "R restart   M sound   Q menu",
                    ],
                )
            elif session.scene == "handoff":
                self.panel(f"{session.pilot.name}, YOU'RE UP", ["Same fleet. Fresh focus.", "Enter when ready"])
            elif session.scene == "results":
                self.results(session)
        self.screen.noutrefresh()
        curses.doupdate()


def _loop(screen, session):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    curses.set_escdelay(25)
    screen.keypad(True)
    painter = Painter(screen, session.options)
    now = time.monotonic()
    clock = FixedClock(now)
    next_frame, fps_start, frames, fps = now, now, 0, 0.0
    interval = 1 / session.options.fps
    dirty, was_running = True, False
    while not session.quit:
        now = time.monotonic()
        running = painter.resize() and session.scene == "playing"
        for _ in range(clock.advance(now, running)):
            if session.scene != "playing":
                break
            session.step(now)
        running = running and session.scene == "playing"
        if running != was_running:
            next_frame, fps_start, frames, fps = now, now, 0, 0.0
            dirty, was_running = True, running
        session.audio.update((), playing=running)
        if dirty or running and now >= next_frame:
            painter.draw(session, fps)
            next_frame, dirty = now + interval, False
            frames += int(running)
            if running and now - fps_start >= 1:
                fps, frames, fps_start = frames / (now - fps_start), 0, now
        delay = min(next_frame - time.monotonic(), 1 / TICK_RATE) if running else 0.10
        screen.timeout(max(1, math.ceil(delay * 1000)))
        key = screen.getch()
        screen.nodelay(True)
        for index in range(32):
            if key < 0:
                break
            if painter.resize() or key in {ord("q"), curses.KEY_RESIZE}:
                session.handle(key, time.monotonic())
            dirty = True
            if index < 31:
                key = screen.getch()
    session.save_replay()


def run(options, profile, audio, replay=None) -> None:
    """Restore terminal and owned audio processes on every exit path."""
    session = Session(options, profile, audio, replay)
    try:
        curses.wrapper(_loop, session)
    finally:
        session.save_replay()
        audio.close()
