"""Session orchestration and the Painter's scenes; the simulation never reads a clock."""

from __future__ import annotations

import math
import secrets
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple

from . import screens, tutorial
from .arcade import ArcadeGame
from .arcade.view import ArcadeView, arcade_layout
from .arena import GlyphArena, PixelArena
from .attract import BANNER, Attract, idle_wait
from .campaign.observe import charge_needed
from .campaign.tables import ACTS, ENDLESS, FAMILIES, PROFILES, UPGRADES, WAVES
from .engine import Game
from .input import MOVES, LatchedControls, Router
from .keymap import LABEL_PREFIXES as PREFIXES  # noqa: F401 (ui's names before Phase 5 moved them to keymap)
from .keymap import SHORT, SIGNAL, Keymap, label  # noqa: F401
from .layout import compute_layout
from .model import HEIGHT, TICK_RATE, WIDTH, Config
from .replay import Recorder, save_tapes
from .rules import make_game
from .storage import ACHIEVEMENTS, OLD_RULES, Placing, board_key
from .term.cells import A_BOLD, style
from .term.gate import KeyGate
from .term.keys import ALT, CTRL, PRESS, RELEASE, SUPER
from .term.repeat import RepeatModel
from .theme import SLOT

MIN_HEIGHT = HEIGHT + 4
DIM = SLOT["dim"]
# drawn at their own 60 x 24 size, centred, whatever the arena's scale (and, by is_card(), every results but Arcade's)
CARDS = {"menu", "help", "records", "settings", "initials", "attract"}
MODES = ("classic", "arcade", "endless", "daily", "duel")
DIFFICULTIES = ("easy", "normal", "hard")
TERMINAL = {"game_over", "victory"}
CHOICES = ("upgrade", "continue")  # the campaign's card and prompt: engine phases that tick, inside the playing scene
MENU = ("LAUNCH", "MODE", "DIFFICULTY", "FLIGHT RECORDS", "HOW TO PLAY", "SETTINGS", "QUIT")  # rows 12-18
QUIT = MENU.index("QUIT")  # the menu row Esc moves to; Esc again quits
CONTEXTS = {"menu": "menu", "help": "pages", "records": "pages", "playing": "play", "paused": "pause"}
CONTEXTS |= {"handoff": "handoff", "results": "results"}
# Phase 5's new scenes (spec 10): the settings card mirrors the menu's keys, the initials card the menu's but the
# printable ones (it types them), and the title demo binds nothing (any key ends it, read in Session.handle).
CONTEXTS |= {"settings": "settings", "initials": "initials", "attract": "attract"}
GLOBAL_COMMANDS = frozenset({"sound", "stats", "redraw"})
# While the Campaign too-small notice shows (spec 9 F2), only the global commands and these act: the way back of the
# screen in front of the player. Every other command is dropped after the KeyGate has seen its key.
SMALL_WAYS = {
    "menu": ("back",),
    "pages": ("back",),
    "settings": ("back",),
    "initials": ("back",),
    "pause": ("menu",),
    "results": ("menu",),
    "replay_pause": ("exit",),
    "replay_results": ("exit",),
    "play": ("pause",),
    "countdown": ("pause",),
    "handoff": ("pause",),
    "replay": ("pause",),
}
SMALL_TEXTS = {  # how the notice names the way back: the key, then what it does
    "menu": "{key} {key} quits",
    "pages": "{key} goes back",
    "settings": "{key} goes back",
    "initials": "{key} skips",
    "pause": "{key} leaves the run",
    "results": "{key} goes to the menu",
    "replay_pause": "{key} quits",
    "replay_results": "{key} quits",
}
SMALL_NEEDS = f"Enlarge the window: the game needs {WIDTH} x {MIN_HEIGHT}"
GAME_SCENES = ("playing", "paused", "handoff")  # the scenes where a game is in play (the notice then says Paused)
REPLAY_CONTEXTS = {"playing": "replay", "paused": "replay_pause", "results": "replay_results"}
MANUAL = "manual"  # pause reasons: manual, resized, small, focus, suspended, resumed
REASONS = {"resized": "Window resized", "focus": "Focus lost", "suspended": "Back from suspend"}
REASONS |= {"resumed": "Terminal restored"}  # small shows the full-screen resize text instead
COUNTDOWN_STEP = 0.3  # seconds per digit of the 3-2-1 countdown after an automatic pause
HINTS = {
    "hold": "Hold A/D or arrows  Space Fire  B Bomb  P Pause",
    "latched": "Tap A/D: move, S: stop  Space Fire  B Bomb  P Pause",
    "timed": "A/D or arrows Move, S Stop  Space Fire  B Bomb  P Pause",  # a quick press latches until S
    "card": "A/D choose  Space take  P Pause",  # the upgrade card, in every scheme
    "prompt": "Space continue  B end the run  P Pause",  # the continue prompt
    "replay": "P Pause, then Q Exit",
    "menu": "Arrows select  Enter choose  Esc Esc quit",
}
# HOW TO PLAY with the default keys, for tap controls in the glyph arena (spec 9 F6, Mac 3): help_lines() builds the
# MOVE, CONTROLS and LEGEND rows for the scheme in use and the scale this window plays at, and the key rows from the
# keymap (first binding only) when it is not the default one.
HELP = (
    ("MOVE", "Tap A/D to start; tap back or S to stop"),
    ("CONTROLS", "Tap: this terminal sends no key releases"),
    ("LEGEND", "/A\\ you {A} shielded  ! v : theirs  [R] catch"),
    ("FIRE", "Space fires; hold it to keep firing"),
    ("BOMB", "B clears enemy fire; combos earn more"),
    ("PAUSE", "P or Escape   |   M sound   |   F1 (fn+F1) stats"),
    ("SESSION", "In pause: R restarts, Q returns to the menu"),
    ("PICKUPS", "R rapid S spread + shield T slow L repair B bomb"),
    ("SCORE", "Hits in a row raise COMBO; a miss breaks it."),
    ("CAMPAIGN", "24 waves, 4 bosses, an upgrade every 3 waves."),
    ("ARCADE", "The 1978 rules: tap to fire, no upgrades."),
    ("ENDLESS", "Keep climbing until the fleet overwhelms you."),
    ("DAILY", "One shared UTC seed, normal difficulty."),
    ("DUEL", "Two pilots, same seed. Swap each wave and life."),
)
HELP_MOVES = {
    "hold": "Hold A/D or arrows; let go to stop",
    "latched": "Tap A/D to start; tap back or S to stop",
    "timed": "A/D or arrows; S stops",
}
HELP_CONTROLS = {  # the scheme in use, and why: the terminal's own (auto, or hold it cannot give), or a choice
    "hold": "Hold: this terminal reports key releases",
    "latched": "Tap: this terminal sends no key releases",
    "timed": "Timed, as chosen in SETTINGS or by --controls",
    "chosen": "Tap, as chosen in SETTINGS or by --controls",
}
LEGENDS = ("/A\\ you {A} shielded  ! v : theirs  [R] catch", "Shots falling are theirs. Catch falling letters")
PIXEL_HINT = "A bigger window (120 x 44 or more) shows pixel art."  # HOW TO PLAY row 19 in the glyph arena (F9)
MODE_NAMES = {"classic": "CAMPAIGN"}  # the key stays classic in tapes, boards and the CLI (spec decision 28)
DESCRIPTIONS = {  # the menu's line for each mode
    "classic": "24 waves in four acts, a boss at each act's end.",
    "arcade": "The 1978 original: 55 invaders, one shot.",
    "endless": "No final wave. How long can you hold the line?",
    "daily": "Today's UTC seed. One fleet for every pilot.",
    "duel": "Two pilots. Same fleet. Swap each wave and life.",
}
HELP_WIDTH = WIDTH - 11
PANELS = {  # the key lines of each panel and card with the default keymap
    "pause": ("P / Escape / Enter resume", "R restart   M sound   Q menu"),
    "replay_pause": ("P / Escape / Enter resume", "M sound   Q exit"),
    "countdown": ("P / Escape pause",),
    "handoff": ("Enter when ready",),
    "results": ("Enter fly again   Q menu",),
    "duel_results": ("Enter rematch   Q menu",),
    "replay_results": ("Q / Escape to exit",),
    "help": ("Enter / Escape to return",),
    "records": ("Left/Right switch page   Enter / Escape to return",),
    "card": ("A/D choose    Space take    P pause",),  # the upgrade card's key line
    "prompt": ("Space continue - the score restarts at 0", "B end the run"),  # the continue prompt's
}
PANEL_WIDTH = 52  # the widest line a panel holds
CARD = 18  # an upgrade card's columns: its two borders and 16 inside; three with 1-column gaps make the 56 of a panel
CARD_TEXT = 13  # the widest a card's name or blurb line may be (1 column before it, 2 after)
FILLERS = {  # Endless's cards for a family with nothing left to offer (tables.ENDLESS.fillers)
    "REFIT": (f"+{ENDLESS.refit} bomb and", "new shelters"),
    "BOUNTY": ("bonus points", "right now"),
}
CARDS_A_RUN = sum(1 for row in WAVES if row.card)  # the Campaign's and Daily's cards (7)
NOTICE_ROLE, PICKUP_ROLE = SLOT["warn"], SLOT["accent"]  # the bottom row: notices, then timed pickups, then codes
DEFAULT_KEYS = Keymap.preset("default").contexts
# Mono without half blocks caps the scale at 1, so it draws the glyph arena (Phase 5 spec 3.2); the golden identity
# tool sets it False to draw Phase 4's frames.
MONO_GLYPHS = True
# What the initials card types (spec 6.2). Space is not one: it is the fire key, so a player tapping fire as the last
# ship goes would type blanks (SC-1); Up/Down reach the blank.
TYPED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
SETTLE = 0.5  # s after a run's initials card opens in which it takes no key: play's keys may still be arriving


class Fits(NamedTuple):
    """What the window holds: the campaign's block (60 x 24 at least) and the Arcade block (56 x 32 at least)."""

    campaign: bool
    arcade: bool


def arcade_frame(session) -> bool:
    """Whether the scene draws an Arcade game's block: its play, pause, countdown and results."""
    return session.scene not in CARDS and session.pilot is not None and session.pilot.game.config.mode == "arcade"


def is_card(session) -> bool:
    """Whether the scene draws as a 60 x 24 card: the CARDS scenes, and (Phase 5 spec 8.1) the results of any game
    but Arcade's, whose panel stays over the 1978 block."""
    if session.scene in CARDS:
        return True
    return session.scene == "results" and session.pilot is not None and session.pilot.game.config.mode != "arcade"


def arcade_card(session) -> bool:
    """Whether the scene is the initials card of an Arcade run (SC-4). Arcade plays from 56 x 32, so its card shows
    in a window that holds the Arcade block as well as in one that holds a card's 60 x 24: the card's lines are 54
    columns at most, and it is drawn as wide as the window up to 60."""
    card = session.initials_card if session.scene == "initials" else None
    if card is None or card.pilot is None:
        return False
    pilot = session.pilots[card.pilot] if card.pilot < len(session.pilots) else session.pilot
    return pilot is not None and pilot.game.config.mode == "arcade"


def small_notice(session) -> bool:
    """Whether the frame drawn shows the Campaign too-small notice: the window is under the block of what is drawn
    (a card's 60 x 24 or the running Campaign game's block; Arcade keeps its own notice, and an Arcade run's
    initials card also shows where the Arcade block fits). Phase 5 spec 9 F2: while it shows, Session.command drops
    every command but the global ones and the scene's way back (SMALL_WAYS)."""
    if arcade_card(session):
        return not (session.room.campaign or session.room.arcade)
    return not arcade_frame(session) and not session.room.campaign


def first_key(keymap: Keymap, context: str, action: str) -> str:
    """An action's first key as hints show it, or ? when it has none."""
    specs = keymap.keys_for(context, action)
    return label(specs[0]) if specs else "?"


def small_way(session) -> str:
    """The too-small notice's last line: the way back from the screen in front of the player, named from the keymap
    (Esc Esc quits on the title, Q leaves the run from the pause, Esc goes back from a card)."""
    context = session.context
    ways = SMALL_WAYS.get(context)
    if not ways:
        return "Press any key" if context == "attract" else ""
    keys = first_key(session.keymap, context, ways[0])
    return SMALL_TEXTS.get(context, "{key} pauses").format(key=keys)


def difficulty_label(mode: str, difficulty: str) -> str:
    """What the DIFFICULTY row shows: Arcade plays the 1978 rules, Daily is always normal."""
    return "1978" if mode == "arcade" else "NORMAL" if mode == "daily" else difficulty.upper()


def mode_name(mode: str) -> str:
    """A mode as players see it: classic is shown as CAMPAIGN."""
    return MODE_NAMES.get(mode, mode.upper())


def menu_line(session) -> str:
    """The menu's description row: the selected mode's line, or the difficulty's own line (spec 11) while the cursor
    is on the DIFFICULTY row of a mode that has one (Arcade plays the 1978 rules, Daily is always normal), or on
    QUIT how to quit from there (Phase 5 spec 9 F3: a quick second Esc can be dropped as a possible auto-repeat)."""
    if session.selection == QUIT:
        keymap = session.keymap
        return f"{first_key(keymap, 'menu', 'back')} again, or {first_key(keymap, 'menu', 'select')}, to quit."
    if session.selection == 2 and session.mode != "arcade":
        return PROFILES["normal" if session.mode == "daily" else session.difficulty].line
    return DESCRIPTIONS[session.mode]


def act_numeral(game) -> str:
    """The act as the HUD and results show it: I to IV."""
    return ACTS[game.act - 1].roman


def wave_field(game) -> str:
    """Row 0 at column 39 (spec 15.2): WAVE 09/24  ACT II, or WAVE 031  ENDLESS."""
    if game.config.mode == "endless":
        return f"WAVE {game.wave:03d}  ENDLESS"
    return f"WAVE {game.wave:02d}/{len(WAVES)}  ACT {act_numeral(game)}"


def lives_field(lives: int) -> str:
    """A ^ per ship, five at most (the cap); a staged game held at 99 shows its count."""
    return "^" * max(0, lives) if lives <= 5 else f"^ x{lives}"


def gun_field(game) -> str:
    """Row 1 at column 36: GUN with a | per free shot slot and a . per slot in flight (a SPREAD's three share one)."""
    busy = len(dict.fromkeys(shot.volley for shot in game.shots if shot.friendly))
    slots = max(0, game.player.weapon)
    free = max(0, slots - busy)
    return "GUN " + "|" * free + "." * (slots - free)


def bomb_field(game) -> str:
    """Row 1 at column 44: BOMB nn% of the charge a bomb needs, or BOMB FULL when a full charge can't add one yet."""
    need = charge_needed(game)
    if game.bomb_charge >= need:
        return "BOMB FULL"
    return f"BOMB {min(100, math.floor(100 * game.bomb_charge / need))}%"


def upgrade_codes(game) -> str:
    """The upgrades owned, as their codes in pick order, stacks as a digit: TS RR2 DF."""
    return " ".join(UPGRADES[name].code + (str(stacks) if stacks > 1 else "") for name, stacks in game.upgrades.items())


def notices(game, reduced_motion: bool) -> list[str]:
    """The bottom row's notices: the game's own (LAST STAND, TAKEN: name, ...), then SAFE ns, which stands in for the
    ship's blink under reduced motion."""
    shown = [text for text, ticks in game.notes if ticks > 0]
    if reduced_motion and game.player.invulnerable > 0:
        shown.append(f"SAFE {math.ceil(game.player.invulnerable / TICK_RATE)}s")
    return shown


def bottom_row(game, reduced_motion: bool) -> list[tuple[str, int]]:
    """The bottom row (spec 15.2) as (text, slot) parts: notices, then timed pickups with their seconds left, then the
    upgrade codes after a bar, and Endless's modifier count. Painter cuts it to the width."""
    parts = [(text, NOTICE_ROLE) for text in notices(game, reduced_motion)]
    for name, ticks in game.powerups.items():
        if ticks > 0:
            parts.append((f"{name.upper()} {math.ceil(ticks / TICK_RATE)}s", PICKUP_ROLE))
    codes = upgrade_codes(game)
    if codes:
        parts.append(("| " + codes if parts else codes, DIM))
    if game.config.mode == "endless" and game.modifiers:
        parts.append((f"MODS {len(game.modifiers)}", DIM))
    return parts


def card_rows(game) -> list[list[str]]:
    """The upgrade card's three cards (Offense, Defense, Utility), each as its 6 rows of 18 columns: the top border,
    the offer's family, the name (> before the selected one), two lines of blurb with the stack the pick would make
    (1/2), and the bottom border. The selected card has # and = borders."""
    cards = []
    for index, name in enumerate(game.offers):
        selected = index == game.cursor
        upgrade = UPGRADES.get(name)
        blurb = upgrade.blurb if upgrade else FILLERS.get(name, ("", ""))
        stack = ""
        if upgrade and upgrade.stacks > 1:
            stack = f"{game.upgrades.get(name, 0) + 1}/{upgrade.stacks}"
        if upgrade:  # a family with nothing left fills its slot from another family: name the card's own (10.2)
            family = upgrade.family.upper()
        else:  # Endless's fillers keep their slot's name
            family = FAMILIES[index].upper() if index < len(FAMILIES) else ""
        inside = [
            f" {family:<15}",
            (">" if selected else " ") + f"{name[:15]:<15}",
            f" {blurb[0][:CARD_TEXT]:<{CARD_TEXT}}  ",
            f" {blurb[1][: CARD_TEXT - len(stack)]:<{CARD_TEXT - len(stack)}}{stack}  ",
        ]
        side, edge = ("#", "#" + "=" * (CARD - 2) + "#") if selected else ("|", "+" + "-" * (CARD - 2) + "+")
        cards.append([edge, *(side + line + side for line in inside), edge])
    return cards


def modifier_lines(game) -> list[str]:
    """Endless's modifier stack for the pause panel (spec 12), in the order they came, wrapped to a panel's width."""
    modifiers = getattr(game, "modifiers", ()) if game.config.mode == "endless" else ()
    return textwrap.wrap("MODS " + ", ".join(modifiers), PANEL_WIDTH) if modifiers else []


def card_title(game) -> str:
    """CHOOSE AN UPGRADE   1 of 7 in the Campaign and Daily; Endless has no last card: CARD 9."""
    number = len(game.picks) + 1
    count = f"CARD {number}" if game.config.mode == "endless" else f"{number} of {CARDS_A_RUN}"
    return f"CHOOSE AN UPGRADE   {count}"


def hint(scheme: str, keymap: Keymap) -> str:
    """The hint row for a control scheme, the upgrade card ("card") or the continue prompt ("prompt"), or the replay
    banner, or the menu. With a keymap other than the default one it is built from the bindings, and when it would
    be too wide its parts go by name: first Bomb, then the move part shrinks to its keys. Pause always stays."""
    if keymap.contexts == DEFAULT_KEYS:
        return HINTS[scheme]

    def key(context: str, action: str) -> str:
        specs = keymap.keys_for(context, action)
        return label(specs[0]) if specs else "?"

    if scheme == "replay":
        return f"{key('replay', 'pause')} Pause, then {key('replay_pause', 'exit')} Exit"
    if scheme == "menu":
        back = key("menu", "back")
        return f"{key('menu', 'up')}/{key('menu', 'down')} select  {key('menu', 'select')} choose  {back} {back} quit"
    keys = f"{key('play', 'left')}/{key('play', 'right')}"
    fire, bomb = key("play", "fire"), key("play", "bomb")
    if scheme == "card":
        parts = {"move": f"{keys} choose", "fire": f"{fire} take"}
    elif scheme == "prompt":
        parts = {"fire": f"{fire} continue", "bomb": f"{bomb} end the run"}
    else:
        moves = {"hold": f"Hold {keys}", "timed": f"{keys} Move, {key('play', 'stop')} Stop"}
        move = moves.get(scheme) or f"Tap {keys}: move, {key('play', 'stop')}: stop"
        parts = {"move": move, "fire": f"{fire} Fire", "bomb": f"{bomb} Bomb"}
    pause = f"{key('play', 'pause')} Pause"
    room = WIDTH - len(pause) - 2  # what the parts before Pause may take
    if len("  ".join(parts.values())) > room:
        parts.pop("bomb", None)
    if len("  ".join(parts.values())) > room and "move" in parts:
        parts["move"] = keys
    head = "  ".join(parts.values())
    if len(head) > room:  # key names longer than any preset's: whole words, as many as fit
        cut = head[: max(0, room) + 1]
        head = cut.rsplit(" ", 1)[0].rstrip() if " " in cut else cut[:room]
    return (f"{head}  {pause}" if head else pause)[:WIDTH]


def help_lines(keymap: Keymap, scheme: str = "latched", k: int = 1, controls: str = "auto"):
    """HOW TO PLAY's rows (spec 9 F6): MOVE for the scheme in use, CONTROLS saying which this terminal uses (controls:
    the option, so a scheme chosen by hand says so), LEGEND in the words of the arena this window draws (glyphs at
    k = 1, pixel art from 2), and the key rows from the keymap's first bindings when it is not the default one."""
    rows = dict(HELP)
    default = keymap.contexts == DEFAULT_KEYS

    def key(context: str, action: str) -> str:
        return first_key(keymap, context, action)

    if default:
        rows["MOVE"] = HELP_MOVES.get(scheme, HELP_MOVES["latched"])
    else:
        keys, stop = f"{key('play', 'left')}/{key('play', 'right')}", key("play", "stop")
        moves = {"hold": f"Hold {keys}; let go to stop", "timed": f"{keys}; {stop} stops"}
        rows["MOVE"] = moves.get(scheme, f"Tap {keys} to start; tap back or {stop} to stop")
        stats = key("global", "stats")
        stats = f"{stats} (fn+{stats})" if stats[:1] == "F" and stats[1:].isdigit() else stats
        rows["FIRE"] = f"{key('play', 'fire')} fires; hold it to keep firing"
        rows["BOMB"] = f"{key('play', 'bomb')} clears enemy fire; combos earn more"
        rows["PAUSE"] = f"{key('play', 'pause')}   |   {key('global', 'sound')} sound   |   {stats} stats"
        rows["SESSION"] = f"In pause: {key('pause', 'restart')} restarts, {key('pause', 'menu')} returns to the menu"
    chosen = scheme == "latched" and controls == "latched"
    rows["CONTROLS"] = HELP_CONTROLS["chosen" if chosen else scheme if scheme in HELP_CONTROLS else "latched"]
    rows["LEGEND"] = LEGENDS[0 if k == 1 else 1]
    return tuple((name, fit(rows[name], HELP_WIDTH)) for name, _ in HELP)


def fit(text: str, width: int) -> str:
    """text, or as many of its words as fit before ' ...' (a long list of keys from a keymap)."""
    return text if len(text) <= width else textwrap.shorten(text, width, placeholder=" ...")


def panel_keys(panel: str, keymap: Keymap) -> tuple[str, ...]:
    """A panel's key lines (PANELS), built from the bindings when the keymap is not the default one."""
    if keymap.contexts == DEFAULT_KEYS:
        return PANELS[panel]

    def keys(context: str, action: str, most: int = 1) -> str:
        return " / ".join(label(spec) for spec in keymap.keys_for(context, action)[:most]) or "?"

    sound, back = keys("global", "sound"), keys("pages", "back", 2)
    again, menu = keys("results", "again"), keys("results", "menu")
    fire, pause = keys("play", "fire"), keys("play", "pause")
    built = {
        "pause": (
            f"{keys('pause', 'resume', 3)} resume",
            f"{keys('pause', 'restart')} restart   {sound} sound   {keys('pause', 'menu')} menu",
        ),
        "replay_pause": (
            f"{keys('replay_pause', 'resume', 3)} resume",
            f"{sound} sound   {keys('replay_pause', 'exit')} exit",
        ),
        "countdown": (f"{keys('countdown', 'pause', 2)} pause",),
        "handoff": (f"{keys('handoff', 'start')} when ready",),
        "results": (f"{again} fly again   {menu} menu",),
        "duel_results": (f"{again} rematch   {menu} menu",),
        "replay_results": (f"{keys('replay_results', 'exit', 2)} to exit",),
        "help": (f"{back} to return",),
        "records": (f"{keys('pages', 'page', 2)} switch page   {back} to return",),
        "card": (f"{keys('play', 'left')}/{keys('play', 'right')} choose    {fire} take    {pause} pause",),
        "prompt": (f"{fire} continue - the score restarts at 0", f"{keys('play', 'bomb')} end the run"),
    }
    return tuple(fit(line, PANEL_WIDTH) for line in built[panel])


def old_boards(profile) -> dict:
    """The OLD RULES page's boards (storage.Profile.old_boards: campaign-1 rows per (mode, difficulty)), or none for
    a profile without them."""
    boards = getattr(profile, "old_boards", None)
    boards = boards() if callable(boards) else None
    return boards if isinstance(boards, dict) else {}


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

    def next_tick_at(self) -> float:
        """When the next tick falls due, if the clock keeps running."""
        return self.last + max(0.0, 1 / TICK_RATE - self.accumulator)


class NoHooks:
    """A Session's hooks until the terminal's replace them (app.Hooks, Phase 5 spec 4.5): tests, bench_render and
    tools/balance.py's Sessions need none."""

    def apply(self, name: str) -> None:
        """A setting changed: without a terminal there is nothing to repaint or re-key."""


@dataclass
class Pilot:
    game: Game | ArcadeGame
    name: str
    recorder: Recorder | None = None
    finished: bool = False
    unlocked: tuple[str, ...] = ()
    verified: bool | None = None
    placing: object = None  # where the run's row landed on its board (profile.last_placing right after its record)


class Session:
    """One screen state machine, including independent alternating duel runs.

    Keys reach it through the Router (the keymap's context for the scene, then the KeyGate). Until the backend
    calls set_scheme() for its terminal, it plays with tap controls and a legacy gate."""

    def __init__(self, options, profile, audio, replay=None):
        self.options, self.profile, self.audio, self.replay = options, profile, audio, replay
        self.mode = "duel" if options.two_player else options.mode
        self.difficulty = options.difficulty
        self.scene, self.selection, self.page = "menu", 0, 0
        self.pilots, self.active = [], 0
        self.keymap = options.keymap
        self.router = Router(self.keymap, KeyGate(RepeatModel(remote=False), certain=False))
        self.controls = LatchedControls()
        self.note, self.quit = "", False
        self.routed = 0  # key events routed so far; the stats line shows it (mod 100)
        self.replay_verified = None
        self.now, self.room, self.redraw = 0.0, Fits(True, True), False  # now: the latest input or tick time
        self.pause_reason, self.paused_from = MANUAL, "playing"
        self.countdown_at, self.countdown = None, 0  # when the countdown ends, and the digit it shows
        # Phase 5 (spec 7, 5.3): the first-run Coach the run is taught by (the Session's one Coach, `tutor`, while
        # attached), and the title demo's nested replay Session, which the app loop ticks and draws in this one's
        # place (`demo` marks that nested Session).
        self.coach = self.tutor = None
        self.tips = self.tips_written = False  # enable_tips() was called; this session wrote tips_done
        self.attract = self.attractor = None
        self.demo = False
        self.last_key_at: float | None = None  # the loop's time of the latest key (the demo's idle wait)
        self.hooks = NoHooks()  # the settings card calls hooks.apply(name); app.play hands in the terminal's
        self.repaints = screens.Repaints()  # PALETTE, BACKGROUND and KEYS changes wait for this rule (spec 2.9)
        self.settings_card = self.initials_card = None  # the cards' state while they show (screens.py)
        self.initials_queue: list[int] = []  # the pilots whose runs placed, still to enter their initials
        if replay or options.quickstart:
            self.start()

    @property
    def pilot(self):
        return self.pilots[self.active] if self.pilots else None

    def enable_tips(self) -> None:
        """Let start() attach the first-run Coach (Phase 5 spec 7.1): app.play, the real terminal path, calls it
        once, so tests, bench_render and tools/balance.py's keyed Sessions never get one. A run --quickstart began
        before app.play gets it now, as a run started from the title would."""
        self.tips = True
        if self.pilots and self.coach is None:
            self.attach_coach(self.pilot.game.config, len(self.pilots))

    @property
    def context(self) -> str:
        """The scene's input context; the global context's keys work in every one."""
        if self.demo:
            return "attract"
        if self.replay:
            return REPLAY_CONTEXTS[self.scene]
        return "countdown" if self.countdown_at is not None else CONTEXTS[self.scene]

    @property
    def fits(self) -> bool:
        """Whether the window holds the current game's block (before the first game, the selected mode's)."""
        arcade = self.pilot.game.config.mode == "arcade" if self.pilot else self.mode == "arcade"
        return self.room.arcade if arcade else self.room.campaign

    @property
    def ticking(self) -> bool:
        return self.scene == "playing" and self.countdown_at is None and self.fits

    def start(self):
        self.save_replay()
        if self.replay:
            configs = [tape.config for tape in self.replay.tapes]
        else:
            seed = self.options.seed if self.options.fixed_seed else secrets.randbits(32)
            configs = [self.configuration(seed)] * (2 if self.mode == "duel" else 1)
        count = len(configs)
        self.pilots = [
            Pilot(
                make_game(config),
                f"{self.options.name[:8]}-{n + 1}" if count == 2 else self.options.name,
                Recorder(config, n + 1) if self.options.record and not self.replay else None,
            )
            for n, config in enumerate(configs)
        ]
        self.active, self.replay_verified = 0, None
        self.attach_coach(configs[0], count)
        self.match_controls()
        self.change_scene("handoff" if count == 2 and not self.replay else "playing")
        if not self.fits and self.pilot.game.config.mode == "arcade":
            self.pause("small")

    def attach_coach(self, config, pilots: int) -> None:
        """The first-run tips for this run (spec 7.1), only on the real terminal path (enable_tips), never for a
        replay or the demo, and as tutorial.wanted() says; with TIPS always every run is taught from the start."""
        self.coach = None
        if not self.tips or self.replay or self.demo:
            return
        tips = getattr(self.options, "tips", "off")
        if tips == "auto" and self.tips_written:  # the first run ended this session (a read-only profile included)
            return
        if not tutorial.wanted(self.options, self.profile, pilots, config):
            return
        if self.tutor is None:
            self.tutor = tutorial.Coach(self.keymap, self.options)
        self.tutor.keymap = self.keymap  # SETTINGS may have changed the keys since
        self.tutor.run = None  # a new run, whatever game it is (an id can be reused)
        if tips == "always":
            self.tutor.done.clear()
        self.coach = self.tutor

    def configuration(self, seed=None):
        if self.mode == "daily":
            seed = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
        return Config(
            difficulty="normal" if self.mode in ("daily", "arcade") else self.difficulty,
            mode="classic" if self.mode == "duel" else self.mode,
            seed=self.options.seed if seed is None else seed,
        )

    def change_scene(self, scene):
        """Keys held into the new scene are suppressed until released (or quiet), and the controls start empty."""
        self.scene, self.countdown_at, self.countdown = scene, None, 0
        self.router.gate.scene_changed(self.now)
        self.controls.clear()

    def set_scheme(self, controls, certain: bool) -> None:
        """The control scheme for this terminal, and whether its keys report press, repeat and release; the note of
        the scheme it replaces goes with it."""
        if self.note == self.controls.note:
            self.note = ""
        self.controls = controls
        self.match_controls()
        self.router.gate.set_certain(certain)
        if controls.note:
            self.note = controls.note

    def match_controls(self) -> None:
        """The active game's fire rule for the controls: an Arcade game fires on presses only (spec decision 15), and
        so does the campaign's card or prompt, where each press of Space is one choice and a held one is one press."""
        game = self.pilot.game if self.pilot is not None else None
        self.controls.press_fire = game is not None and (game.config.mode == "arcade" or game.phase in CHOICES)

    def follow_phase(self, before: str, after: str, actions: tuple[str, ...], now: float) -> None:
        """The card and the prompt are engine phases inside play, so the Session does what a scene change does at
        their edges: keys still held are suppressed and the controls start empty, so a latched direction or a held
        Space never carries in or out (spec 10.2, 16.2). On them, a tick that held a direction unlatches it, so the
        next tap, even of the same way, is a new edge for the engine."""
        if before != after and (before in CHOICES or after in CHOICES):
            self.router.gate.scene_changed(now)
            self.controls.clear()
            self.match_controls()
        elif after in CHOICES and not MOVES.keys().isdisjoint(actions):
            self.controls.unlatch()

    def save_replay(self):
        recorded = [pilot for pilot in self.pilots if pilot.recorder]
        if recorded:
            try:
                save_tapes(self.options.record, [pilot.recorder.tape(pilot.game) for pilot in recorded])
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
            self.open_records,
            lambda: self.change_scene("help"),
            self.open_settings,
            lambda: setattr(self, "quit", True),
        )
        handlers[self.selection]()

    def open_records(self) -> None:
        """FLIGHT RECORDS opens on its first page (spec 9 F8)."""
        self.page = 0
        self.change_scene("records")

    def open_settings(self) -> None:
        """The settings card (spec 4.2), from the title menu only."""
        self.settings_card = screens.SettingsCard()
        self.change_scene("settings")

    def open_initials(self, pilot_mode: bool = False) -> None:
        """The initials card for the settings card's PILOT row (pilot_mode), else for the next pilot whose run still
        places (its rank read again first, spec 6.1); the results when none is left."""
        if pilot_mode:
            self.initials_card = screens.InitialsCard(self.options.name)
            self.change_scene("initials")
            return
        rank_of = getattr(self.profile, "rank", None)
        while self.initials_queue:
            index = self.initials_queue.pop(0)
            pilot = self.pilots[index]
            rank = rank_of(pilot.placing) if callable(rank_of) else pilot.placing.rank
            if isinstance(rank, int) and 1 <= rank <= 10:
                card = screens.InitialsCard(pilot.name, pilot=index, placing=pilot.placing, rank=rank, opened=self.now)
                self.initials_card = card
                self.change_scene("initials")
                return
        self.initials_card = None
        self.change_scene("results")

    def initials_done(self, save: bool) -> None:
        """Enter (save) or Esc on the initials card: the run's row (and, for a single pilot, the pilot name) takes
        the name SAVES AS shows; then the next card, or the results. From the settings card, back to it."""
        card = self.initials_card
        name = card.saves_as
        if card.pilot is None:
            if save and name != card.name:
                self.options.name = name  # at once: the footer, the next run's record (spec 4.1)
                if self.settings_card is not None:
                    self.settings_card.name = name
            self.initials_card = None
            self.change_scene("settings")
            return
        pilot = self.pilots[card.pilot]
        duel = len(self.pilots) == 2
        rename = getattr(self.profile, "rename", None)
        if save and name != pilot.name and callable(rename) and rename(card.placing, name, pilot=not duel):
            pilot.name = name
            if not duel:
                self.options.name = name
        self.open_initials()

    def handle(self, key, now: float) -> None:
        """A decoded key, routed by the keymap's context for this scene. While the title demo runs any key ends it
        and does nothing else (the signal keys still act); on the initials card letters and digits are typed."""
        self.now, self.routed, self.last_key_at = now, self.routed + 1, now
        if self.attractor is not None:
            self.attractor.cycles = 0  # a key: the demo may run its cycles again
        if self.scene == "attract":
            hit = self.keymap.lookup(self.context, key)
            if hit and hit[1] == SIGNAL:
                self.router.route(self, key, now)
                return
            self.router.gate.admit(key, None, now)  # tracked, never admitted: its repeats stay out of the menu
            if key.kind != RELEASE:
                self.stop_demo(now)
            return
        if self.scene == "initials" and self.type_initial(key, now):
            return
        self.router.route(self, key, now)

    def type_initial(self, key, now: float) -> bool:
        """A letter, digit or - on the initials card, ahead of the bindings (spec 6.2): it goes through the KeyGate's
        bookkeeping and types only as a fresh press (a held key types once, a key held from the game over types
        nothing), and never while the card settles (settling()). True when the key was a typed one, typed or not."""
        if key.mods & (CTRL | ALT | SUPER) or key.name not in TYPED:
            return False
        gate = self.router.gate
        gate.admit(key, None, now)
        if small_notice(self) or self.initials_card is None or self.settling(now):
            return True  # nothing is typed on a card the window can't show, or before the player can have seen it
        fresh = key.kind == PRESS if gate.certain else gate.repeat.press is True and key.name not in gate.suppressed
        if fresh:
            self.initials_card.type(key.name)
        return True

    def settling(self, now: float) -> bool:
        """Whether a run's initials card opened less than SETTLE s ago (SC-1). The last ship goes while the player is
        still moving, firing or pausing; until then the card takes no key, typed or bound (a tap of A or D, of Up,
        which fires, or of Esc, which pauses, would type, cycle or skip), whatever the gate makes of it (a key held
        from the game over it suppresses already). The PILOT row's editor, opened by its own key, takes keys at
        once."""
        card = self.initials_card
        return card is not None and card.pilot is not None and now - card.opened < SETTLE

    def stop_demo(self, now: float) -> None:
        """End the title demo (a key, a focus-out, a resize that no longer fits): the menu, cursor where it was."""
        if self.attractor is not None:
            self.attractor.stop(self, now)
        else:
            self.attract = None
            self.change_scene("menu")

    def update_attract(self, now: float) -> bool:
        """The loop's hook (app.py), on its clock: a PALETTE, BACKGROUND or KEYS change waiting to apply (spec 2.9),
        and the title demo's cycle (spec 5.2). True when the screen should be drawn again."""
        changed = self.repaints.update(self, now)
        if self.replay or self.demo:
            return changed
        if self.attractor is None:
            self.attractor = Attract(self.options, idle=idle_wait())
        return self.attractor.update(self, now) or changed

    def command(self, name: str, now: float) -> None:
        """Run a named command; each context's bindings decide which commands its keys reach. While the too-small
        notice shows, only the global commands and the screen's way back act (spec 9 F2)."""
        self.now = now
        if name not in GLOBAL_COMMANDS and small_notice(self) and name not in SMALL_WAYS.get(self.context, ()):
            return
        if self.scene == "settings" and name in screens.CARD_COMMANDS:
            if self.settings_card is None:
                self.settings_card = screens.SettingsCard()
            self.settings_card.command(self, name, now)
            return
        if self.scene == "initials" and name in screens.CARD_COMMANDS:
            if self.initials_card is None:
                self.initials_card = screens.InitialsCard(self.options.name)
            if self.settling(now):
                return
            verdict = self.initials_card.command(name)
            if verdict is not None:
                self.initials_done(verdict == "select")
            return
        match name:
            case "stats":
                self.options.show_fps = not self.options.show_fps
            case "sound":
                self.audio.toggle()
            case "redraw":
                self.redraw = True
            case "up" | "down":  # the cursor stops at LAUNCH and at QUIT: Up never wraps round to QUIT (PN-1)
                self.selection = max(0, min(len(MENU) - 1, self.selection + (1 if name == "down" else -1)))
            case "left" | "right":
                if self.selection in {1, 2}:
                    self.menu_action(-1 if name == "left" else 1)
            case "select":
                self.menu_action()
            case "back":
                if self.scene != "menu":
                    self.change_scene("menu")
                elif self.selection != QUIT:
                    self.selection = QUIT
                else:
                    self.quit = True
            case "page":
                if self.scene == "records":
                    self.page = (self.page + 1) % (3 if old_boards(self.profile) else 2)
            case "menu":
                self.menu()
            case "restart" | "again":
                self.start()
            case "pause":
                self.pause(MANUAL if self.countdown_at is None else self.pause_reason)  # the countdown goes back
            case "resume":
                self.resume(now)
            case "start":
                self.change_scene("playing")
            case "exit":
                self.quit = True
            case "erase":
                pass  # the initials card's, which takes it above
            case _:
                raise ValueError(f"Unknown command: {name}")

    def pause(self, reason: str) -> None:
        """Stop play (or, by hand, a handoff). An automatic reason also replaces the reason of a paused game."""
        if self.scene == "playing" or (self.scene == "handoff" and reason == MANUAL):
            if reason == MANUAL and self.scene == "playing" and self.coach is not None:
                self.coach.on_pause()  # the Pause tip is done
            self.paused_from, self.pause_reason = self.scene, reason
            self.change_scene("paused")
        elif self.scene == "paused" and reason != MANUAL:
            self.pause_reason = reason

    def resume(self, now: float) -> None:
        """Back where the pause began: at once after a manual pause, else after a 3-2-1 countdown; refused while
        the window is too small."""
        if not self.fits:
            return
        self.change_scene(self.paused_from)
        if self.paused_from == "playing" and self.pause_reason != MANUAL:
            self.countdown_at, self.countdown = now + 3 * COUNTDOWN_STEP, 3

    def update_countdown(self, now: float) -> bool:
        """Count down on the wall clock; True when the display changed (the next digit, or play began)."""
        if self.countdown_at is None:
            return False
        digit = math.ceil(round((self.countdown_at - now) / COUNTDOWN_STEP, 9))
        if digit <= 0:
            self.countdown_at, self.countdown = None, 0
            return True
        changed, self.countdown = digit != self.countdown, digit
        return changed

    def countdown_deadline(self) -> float | None:
        """When the countdown shows its next digit, or ends."""
        return None if self.countdown_at is None else self.countdown_at - (self.countdown - 1) * COUNTDOWN_STEP

    def on_resize(self, fits: Fits | bool) -> None:
        """The window changed size (fits: what it holds, or one bool for both blocks): play pauses, which forgets
        held keys (only play and its countdown feed the controls, and leaving them clears the controls)."""
        self.room = fits if isinstance(fits, Fits) else Fits(fits, fits)
        if self.attract is not None:
            self.attract.room = self.room
        if self.scene == "attract" and not (self.attract.fits if self.attract is not None else self.room.campaign):
            self.stop_demo(self.now)  # a window that no longer holds the demo ends it
        self.pause("resized" if self.fits else "small")

    def on_focus(self, focused: bool) -> None:
        """FocusOut pauses play, which forgets held keys, and ends the title demo; FocusIn changes nothing, so the
        game stays paused."""
        if not focused:
            if self.scene == "attract":
                self.stop_demo(self.now)
            self.pause("focus")

    def step(self, now):
        self.now = now
        if self.scene != "playing":
            return
        pilot = self.pilot
        game = pilot.game
        tape = self.replay.tapes[self.active] if self.replay else None
        if tape and game.tick >= tape.ticks:
            self.finish_replay()
            return
        actions = tape.actions_at(game.tick) if tape else self.controls.sample(now)
        if pilot.recorder:
            pilot.recorder.append(actions)
        lives, phase = game.player.lives, game.phase
        game.step(actions)
        if "fire" in game.events:
            self.controls.fired()
        self.audio.update(game.events)
        if self.coach:
            self.coach.observe(game, actions, tuple(game.events))
        if tape:
            if game.tick >= tape.ticks or game.phase in TERMINAL:
                self.finish_replay()
            return
        self.follow_phase(phase, game.phase, actions, now)
        new_wave = phase in ("clear", "upgrade") and game.phase == "wave"  # not after a continue: that turn passed
        if game.phase in TERMINAL and not pilot.finished:
            pilot.finished = True
            if len(self.pilots) == 2:  # a duel's rows never change the saved pilot name (spec 6.3)
                unlocked = self.profile.record(game.result(), game.config, pilot.name, pilot=False)
            else:
                unlocked = self.profile.record(game.result(), game.config, pilot.name)
            pilot.unlocked = tuple(unlocked)
            pilot.placing = getattr(self.profile, "last_placing", None)  # right after its record: P2's can't move it
            self.save_replay()
            self.tips_learned()
        if all(p.finished for p in self.pilots):
            self.runs_over()
        elif len(self.pilots) == 2 and (pilot.finished or game.player.lives < lives or new_wave):
            other = 1 - self.active
            if not self.pilots[other].finished:
                self.save_replay()
                self.active = other
                self.match_controls()
                self.change_scene("handoff")

    def tips_learned(self) -> None:
        """A run the Coach taught was recorded: the profile's settings get tips_done, once (spec 7.1)."""
        if self.coach is None or self.tips_written:
            return
        self.tips_written = True
        update = getattr(self.profile, "update_settings", None)
        if getattr(self.profile, "read_only", False) is not True and callable(update):
            update({"tips_done": True})

    def runs_over(self) -> None:
        """Every pilot finished: the initials card for each run that placed on its board (a storage.Placing with a
        rank of 1-10 on a profile that can be written: never a stub's, a Mock's or a replay's), then the results."""
        writable = getattr(self.profile, "read_only", False) is not True
        self.initials_queue = [
            index
            for index, pilot in enumerate(self.pilots)
            if writable and isinstance(pilot.placing, Placing) and pilot.placing.rank is not None
        ]
        self.open_initials()

    def finish_replay(self):
        """Verify the tape just watched, then play the next pilot's tape or show the results (the title demo shows
        the high scores instead: its results are never drawn)."""
        self.pilot.verified = self.replay.tapes[self.active].verify(self.pilot.game)
        if self.active + 1 < len(self.pilots):
            self.active += 1
            self.match_controls()
            self.change_scene("playing")
        else:
            self.replay_verified = all(pilot.verified for pilot in self.pilots)
            self.page = 0
            self.change_scene("records" if self.demo else "results")


def max_scale(options, theme=None, caps=None) -> int | None:
    """--scale (--glyphs is 1); auto is the largest scale that fits. Mono without half blocks draws the glyph arena
    (Phase 5 spec 3.2: nothing else carries shape there), unless MONO_GLYPHS is off (the golden identity tool)."""
    if MONO_GLYPHS and getattr(theme, "tier", None) == "mono" and caps is not None and not caps.unicode_blocks:
        return 1
    return None if options.scale == "auto" else int(options.scale)


class Painter:
    """Clipped ASCII drawing on a cell surface (a CellBuffer); the backend presents what was drawn.

    Scenes draw in a frame: the cards (CARDS: menu, help, records, settings, initials, the demo's high scores; and
    every results but Arcade's, is_card) at 60 x 24, and play in the whole block of the arena's scale k, with the
    HUD fields' columns scaled by k, the footer on the block's last two rows and panels centred.
    An Arcade game's scenes draw in the Arcade block instead (arcade_layout), the 1978 screen at its scale p.
    caps decides whether the pixel arena may use half blocks (None: it may)."""

    def __init__(self, screen, options, theme, caps=None):
        self.screen, self.options, self.theme, self.caps = screen, options, theme, caps
        self.colors = theme.styles()
        self.views = {1: GlyphArena()}
        self.arcade_view = None  # made on the first Arcade frame
        self.resize()

    def resize(self):
        """Follow the screen's size; returns whether the campaign's block fits (self.arcade says the Arcade's)."""
        self.rows, self.columns = self.screen.getmaxyx()
        self.layout = compute_layout(self.columns, self.rows, max_k=max_scale(self.options, self.theme, self.caps))
        self.arcade = arcade_layout(self.columns, self.rows)
        k = self.layout.k
        if k not in self.views:
            self.views[k] = PixelArena(k, self.theme, self.caps)
        self.view = self.views[k]
        self.place("playing")
        return self.layout.fits

    def place(self, scene, arcade=False, card=None):
        """The frame the scene draws in, centred in the block: the block itself, or a 60 x 24 card (card: whether it
        is one; by default, the CARDS scenes); arcade: the Arcade block."""
        if arcade:
            block = self.arcade
            self.k, self.width, self.height, self.left, self.top = 1, block.width, block.height, block.left, block.top
            return
        layout = self.layout
        card = scene in CARDS if card is None else card
        self.k = 1 if card else layout.k
        self.width, self.height = (WIDTH, MIN_HEIGHT) if card else (layout.width, layout.height)
        self.left = layout.left + (layout.width - self.width) // 2
        self.top = layout.top + (layout.height - self.height) // 2
        if card and self.columns < WIDTH:  # an Arcade run's initials card at 56-59 columns: the window's width
            self.width, self.left = self.columns, 0

    def text(self, y, x, text, color=6, bold=False, dim=False):
        """Text in a slot's style (dim: secondary text, the dim role, whose mono and 16-colour style is A_DIM)."""
        y, x, text = self.top + y, self.left + x, str(text)
        if not 0 <= y < self.rows or x >= self.columns:
            return
        if x < 0:
            text, x = text[-x:], 0
        text = text[: max(0, self.columns - x)]
        attr = self.colors.get(DIM if dim else color, 0) | (A_BOLD if bold else 0)
        if text:
            self.screen.addstr(y, x, text, attr)

    def center(self, y, text, **style):
        self.text(y, max(0, (self.width - len(text)) // 2), text, **style)

    def panel(self, title, lines, title_color=1, styles=None):
        """A bordered panel centred in the frame: the title, then each line centred (styles: per line, the text()
        keywords it is drawn with)."""
        width = min(56, max(34, *(len(line) + 4 for line in lines), len(title) + 4))
        x, y = (self.width - width) // 2, (self.height - len(lines) - 4) // 2
        for row in range(len(lines) + 4):
            self.text(y + row, x, " " * width)
        self.text(y, x, "+" + "-" * (width - 2) + "+", 1)
        self.center(y + 1, title, color=title_color, bold=True)
        for row, line in enumerate(lines, y + 3):
            self.center(row, line, **(styles[row - y - 3] if styles else {}))
        self.text(y + len(lines) + 3, x, "+" + "-" * (width - 2) + "+", 1)

    def card(self, session):
        """The upgrade card (spec 15.3), a selection variant of panel: three 18-column cards, the selected one with #
        and = borders, > before its name and the name in accent bold (bold and the border carry it in mono), what
        is owned, and the key line, dim until the card is armed."""
        game = session.pilot.game
        width, rows = CARD * 3 + 2, 9
        x, y = (self.width - width) // 2, (self.height - rows) // 2
        for row in range(-1, rows + 1):
            self.text(y + row, x, " " * width)
        self.center(y, card_title(game), color=1, bold=True)
        for index, card in enumerate(card_rows(game)):
            left, chosen = x + index * (CARD + 1), index == game.cursor
            border = {"color": 1 if chosen else 6, "bold": chosen}
            for row, line in enumerate(card):
                if row in (0, len(card) - 1):
                    self.text(y + 1 + row, left, line, **border)
                    continue
                self.text(y + 1 + row, left, line[0], **border)
                self.text(y + 1 + row, left + CARD - 1, line[-1], **border)
                inside = {"dim": True} if row == 1 else {"color": 1, "bold": True} if row == 2 and chosen else {}
                self.text(y + 1 + row, left + 1, line[1:-1], **inside)
        codes = upgrade_codes(game)
        if codes:
            self.text(y + 7, x + 1, f"OWNED  {codes}"[: width - 2], dim=True)
        armed = game.armed <= 0
        self.center(y + 8, panel_keys("card", session.keymap)[0], color=1 if armed else 6, dim=not armed)

    def prompt(self, session):
        """Easy's continue prompt (spec 15.3): the reason, the seconds left, and the two choices, dim until armed."""
        game = session.pilot.game
        fire, bomb = panel_keys("prompt", session.keymap)
        left = f"{game.continues} continue{'' if game.continues == 1 else 's'} left"
        bomb = bomb.ljust(max(len(fire) - len(left), len(bomb) + 2)) + left
        armed = game.armed <= 0
        style = {"color": 1} if armed else {"dim": True}
        lines = [f"CONTINUE?   {math.ceil(game.prompt / TICK_RATE)}", fire, bomb]
        self.panel(game.notice or "SHIP LOST", lines, title_color=SLOT["danger"], styles=[{"color": 3}, style, style])

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
        values = dict.fromkeys(MENU, "")
        values |= {"MODE": f"         < {mode_name(session.mode):^9} >"}
        values |= {"DIFFICULTY": f"   < {difficulty_label(session.mode, session.difficulty):^9} >"}
        for index, value in enumerate(name + values[name] for name in MENU):  # rows 12-18
            selected = index == session.selection
            self.text(12 + index, 13, ("> " if selected else "  ") + value, 1 if selected else 6, selected)
        self.center(19, menu_line(session), color=3)
        self.center(21, hint("menu", session.keymap), dim=True)
        self.footer(session)

    def footer(self, session, alert=None, sound=True):
        """Rows 22-23 of a card: the alert (alert, else the session's note or the profile's alert), then the pilot
        (NEW PILOT while the name is the default, spec 9 F4) and the sound with its key (n/a without a backend, F10;
        no key in the demo, where any key ends it). sound False leaves the sound out (the initials card types M)."""
        name = str(session.options.name)
        parts = ["NEW PILOT" if name == "PILOT" else f"PILOT {name}"]
        if sound:
            audio = session.audio
            if not getattr(audio, "available", True):
                parts.append("SOUND n/a")
            else:
                state = "ON" if getattr(audio, "enabled", False) else "OFF"
                demo = session.demo or session.scene == "attract"
                key = "" if demo else f" [{first_key(session.keymap, 'global', 'sound')}]"
                parts.append(f"SOUND {state}{key}")
        self.text(23, 0, "  |  ".join(parts), dim=True)
        message = alert or session.note or session.profile.alert
        if message:
            self.center(22, str(message)[:WIDTH], color=3)

    def help(self, session):
        """HOW TO PLAY: the rows for this window's scheme and scale (spec 9 F6); in the glyph arena of a window that
        could draw pixel art, row 19 says a bigger window shows it (F9)."""
        self.center(1, "FLIGHT MANUAL", color=1, bold=True)
        k = self.layout.k
        rows = help_lines(session.keymap, session.controls.scheme, k, getattr(self.options, "controls", "auto"))
        for row, (name, detail) in enumerate(rows, 4):
            self.text(row, 1, f"{name:<9}", 3, True)
            self.text(row, 11, detail)
        self.center(18, "Scores save automatically when a run finishes.", dim=True)
        pixels = k == 1 and max_scale(self.options, self.theme, self.caps) != 1
        self.center(19, PIXEL_HINT if pixels else "Small windows and pause stop the entire simulation.", dim=True)
        self.center(21, panel_keys("help", session.keymap)[0], color=1)
        self.footer(session)

    def score_row(self, row, score, difficulty):
        """One score row of a records page: pilot, score, wave (or round), mode as players call it, difficulty."""
        mode = mode_name(score.get("mode", "")).lower() if score.get("mode") else ""
        self.text(
            row,
            1,
            f"{score.get('name', 'PILOT')[:12]:<12} {score.get('score', 0):>7}"
            f"  {score.get('wave', 0):>3}  {mode:<10} {difficulty}",
        )

    def records(self, session):
        """FLIGHT RECORDS' pages; the title demo's high scores are page 0 with the demo's banner (spec 5.2)."""
        data = session.profile.data
        config = session.configuration()
        demo = session.demo or session.scene == "attract"
        page = 0 if demo else session.page
        name = f"{mode_name(config.mode)} / {difficulty_label(config.mode, config.difficulty)}"
        titles = (f"FLIGHT RECORDS - {name}", "CAREER & ACHIEVEMENTS", "FLIGHT RECORDS - OLD RULES")
        self.center(1, titles[page], color=1, bold=True)
        if page == 0:
            arcade = config.mode == "arcade"  # rounds, and the 1978 rules in place of a difficulty
            self.text(3, 1, f"PILOT          SCORE  {'ROUND' if arcade else 'WAVE '} MODE       DIFFICULTY", 3)
            scores = [score for score in data.get("scores", []) if board_key(score) == board_key(config)]
            scores = sorted(scores, key=lambda score: score.get("score", 0), reverse=True)[:13]
            for row, score in enumerate(scores, 5):
                self.score_row(row, score, "1978" if arcade else score.get("difficulty", ""))
            if not scores:
                self.center(10, "Your first flight belongs here.", dim=True)
        elif page == 2:  # campaign-1's rows (spec 17), only offered while there are any
            boards = old_boards(session.profile)
            self.center(3, OLD_RULES, dim=True)
            self.text(5, 1, "PILOT          SCORE  WAVE  MODE       DIFFICULTY", 3)
            each = max(1, min(10, 13 // max(1, len(boards))))  # the top rows of every board that fits in 13 rows
            rows = [score for board in boards.values() for score in board[:each]][:13]
            for row, score in enumerate(rows, 7):
                self.score_row(row, score, score.get("difficulty", ""))
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
        self.center(21, BANNER if demo else panel_keys("records", session.keymap)[0], color=1)
        self.footer(session)

    def attract(self, session):
        """The title demo between tapes: the high scores (the records card's page 0) with the demo's banner."""
        self.records(session)

    def settings(self, session):
        screens.settings_card(self, session)

    def initials(self, session):
        screens.initials_card(self, session)

    def results_card(self, session):
        screens.results_card(self, session)

    def tip(self, session, game, tail: int) -> str | None:
        """The first-run tip for the bottom row (spec 7.2), or None: only in wave play with no countdown, only while
        the game's notices and timers leave the row free, and only when it fits beside the codes (tail columns)."""
        coach = session.coach
        if coach is None or session.replay or session.scene != "playing" or session.countdown_at is not None:
            return None
        room = self.width - tail
        line = coach.tip(session.controls.scheme, self.k, room)
        if line is None or game.phase != "playing" or not tutorial.row_free(game, self.options.reduced_motion):
            return None
        return line

    def arena(self, session):
        """The campaign's play scene: the HUD rows, the arena (which draws the boss bar on block row 2), the hint and
        bottom rows, and the panel of the game's phase: the intro, the clear, the upgrade card or the prompt (left out
        while the PAUSED, countdown or handoff panel takes its place)."""
        game, player = session.pilot.game, session.pilot.game.player
        best = session.profile.best(game.config)
        label, k, bottom = f"P{session.active + 1}" if len(session.pilots) > 1 else "SCORE", self.k, self.height - 1
        self.text(0, 0, f"{label} {game.score:07d}   HI {max(best, game.score):07d}", 2, True)
        self.text(0, 39 * k, wave_field(game), 1, True)
        self.text(1, 0, f"LIVES {lives_field(player.lives):<5}  BOMBS {player.bombs}  COMBO x{game.combo}", 3)
        self.text(1, 36 * k, gun_field(game), 1)
        charge = bomb_field(game)
        self.text(1, 44 * k, charge, 1 if charge == "BOMB FULL" else 6, dim=charge != "BOMB FULL")
        self.view.draw(self, session)
        if session.demo:
            self.text(bottom - 1, 0, BANNER.ljust(self.width), 3)
        elif session.replay:
            self.text(bottom - 1, 0, f"REPLAY  {hint('replay', session.keymap)}".ljust(self.width), 3)
        else:
            scheme = {"upgrade": "card", "continue": "prompt"}.get(game.phase, session.controls.scheme)
            self.text(bottom - 1, 0, hint(scheme, session.keymap), dim=True)
        parts = bottom_row(game, self.options.reduced_motion)
        if not any(color in (NOTICE_ROLE, PICKUP_ROLE) for _, color in parts):  # only the codes: a tip may go first
            tail = [
                ("| " + text if index == 0 and not text.startswith("MODS ") else text, color)
                for index, (text, color) in enumerate(parts)
            ]
            line = self.tip(session, game, sum(len(text) + (1 if text.startswith("| ") else 2) for text, _ in tail))
            if line:
                self.text(bottom, 0, line[:3], SLOT["accent"], True)
                self.text(bottom, 3, line[3:])
                parts = [(line, None), *tail]
        x = -2
        for text, color in parts:
            x += 1 if text.startswith("| ") else 2  # two spaces between parts, one before the bar: SHIELD 9s | TS
            if x >= self.width:
                break
            if color is not None:  # the tip, drawn above in its two roles
                self.text(bottom, x, text[: self.width - x], color, dim=color == DIM)
            x += len(text)
        if session.scene in ("paused", "handoff") or session.countdown_at is not None:
            return  # the PAUSED, countdown or handoff panel stands in for the phase's, which would show round it
        if game.phase == "wave" and game.intro:
            title, *lines = game.intro
            self.panel(title, lines, title_color=SLOT["danger"] if title == "WARNING" else 1)
        elif game.phase == "clear" and game.notice:
            title, *lines = game.notice.split("\n")
            self.panel(title, lines, title_color=SLOT["good"])
        elif game.phase == "upgrade" and game.offers:
            self.card(session)
        elif game.phase == "continue":
            self.prompt(session)

    def arcade_screen(self, session):
        """An Arcade game: the 1978 screen fills the block; a replay's banner takes its last row, which the program
        leaves blank."""
        if self.arcade_view is None:
            self.arcade_view = ArcadeView(self.theme, self.caps)
        self.arcade_view.draw(self, session)
        if session.replay:
            banner = BANNER if session.demo else f"REPLAY  {hint('replay', session.keymap)}"
            self.text(self.height - 1, 0, banner[: self.width].ljust(self.width), 3)

    def results(self, session):
        """An Arcade game's results: a panel over the 1978 block, the game's or its replay's (every other game's
        results are the results card, screens.results_card; is_card). A hand-made replay file can hold more than
        one tape: each pilot gets a line. The 1978 program has no seed, so the panel shows none."""
        game = session.pilot.game
        if session.replay:
            title = "REPLAY VERIFIED" if session.replay_verified else "REPLAY MISMATCH"
            lines = [f"SCORE {game.score:,}    ROUND {game.wave}"]
            if len(session.pilots) > 1:
                lines = [
                    f"P{n} {pilot.game.score:,}  WAVE {pilot.game.wave}  {'OK' if pilot.verified else 'MISMATCH'}"
                    for n, pilot in enumerate(session.pilots, 1)
                ]
            lines += panel_keys("replay_results", session.keymap)
        else:
            title = "GAME OVER"
            accuracy = 100 * game.hits / max(1, game.shots_fired)
            lines = [f"SCORE {game.score:,}    ROUND {game.wave}", f"{game.kills} kills   {accuracy:.0f}% accuracy"]
            lines += ["UNLOCKED " + name for name in session.pilot.unlocked[:2]]
            lines += panel_keys("results", session.keymap)
        self.panel(title, lines)
        if session.note or session.profile.alert:
            self.center(self.height - 1, str(session.note or session.profile.alert)[: self.width], color=3)

    def draw(self, session, stats: str = ""):
        """The scene; stats, when given, replaces the footer row, cut to the frame's width. The title demo draws
        its tape's session while one plays."""
        if session.scene == "attract" and session.attract is not None:
            self.draw(session.attract, stats)
            return
        self.screen.erase()
        fits = self.resize()
        arcade = arcade_frame(session)
        card = is_card(session)
        fits = self.arcade.fits if arcade else fits or (arcade_card(session) and self.arcade.fits)
        self.place(session.scene, arcade, card)
        if self.theme.paint_background:
            self.letterbox()
        if not fits and arcade:  # Phase 4's notice, unchanged (spec 9 F2)
            self.text(max(0, self.rows // 2 - 1), 0, "ARCADE NEEDS 56 x 32", 3, True)
            self.text(max(0, self.rows // 2), 0, f"Current: {self.columns} x {self.rows} | Paused")
            self.text(max(0, self.rows // 2 + 1), 0, "Enlarge the window or zoom out")
        elif not fits:
            self.small(session)
        elif card:
            (self.results_card if session.scene == "results" else getattr(self, session.scene))(session)
        else:
            (self.arcade_screen if arcade else self.arena)(session)
            if session.scene == "paused":
                reason = [REASONS[session.pause_reason]] if session.pause_reason in REASONS else []
                seed = [] if arcade else [f"SEED {session.pilot.game.config.seed}"]
                keys = panel_keys("replay_pause" if session.replay else "pause", session.keymap)
                self.panel("PAUSED", [*reason, *seed, *modifier_lines(session.pilot.game), *keys])
            elif session.countdown_at is not None:
                self.panel(str(session.countdown), ["GET READY", *panel_keys("countdown", session.keymap)])
            elif session.scene == "handoff":
                card = ["Same fleet. Fresh focus.", *panel_keys("handoff", session.keymap)]
                self.panel(f"{session.pilot.name}, YOU'RE UP", card)
            elif session.scene == "results":
                self.results(session)
        if stats and fits:
            self.text(self.height - 1, 0, stats[: self.width].ljust(self.width), dim=True)
        self.screen.noutrefresh()

    def small(self, session):
        """The Campaign too-small notice (spec 9 F2), centred in the window: what the game needs, the window's size
        (Paused while a game is in play) and the way back from the screen in front of the player."""
        self.left = self.top = 0
        self.width, self.height = self.columns, self.rows
        size = f"Now {self.columns} x {self.rows}"
        if session.pilot is not None and session.scene in GAME_SCENES:
            size += " | Paused"
        lines = []
        for text, look in ((SMALL_NEEDS, {"color": 3, "bold": True}), (size, {}), (small_way(session), {"dim": True})):
            # a size such as 60 x 24 never splits over two rows
            wrapped = textwrap.wrap(text.replace(" x ", "\xa0x\xa0"), max(1, self.columns))
            lines += [(line.replace("\xa0", " "), look) for line in wrapped]
        for row, (line, look) in enumerate(lines, max(0, (self.rows - len(lines)) // 2)):
            self.center(row, line, **look)

    def letterbox(self):
        """Painted backgrounds: the cells around the frame in the letterbox colour (the frame is already bg)."""
        screen, edge = self.screen, style(bg=SLOT["letterbox"])
        right, bottom = self.left + self.width, self.top + self.height
        screen.fill(0, 0, self.columns, self.top, st=edge)
        screen.fill(bottom, 0, self.columns, self.rows - bottom, st=edge)
        screen.fill(self.top, 0, self.left, self.height, st=edge)
        screen.fill(self.top, right, self.columns - right, self.height, st=edge)
