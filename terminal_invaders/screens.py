"""Phase 5's new cards, drawn by the Painter (ui.py), kept here so ui.py stays near its size (spec 1.1).

Each is a 60 x 24 card, centred in any window of 60 x 24 or more, with a hint row built from the keymap that names
every way out. They read the session, its options, the profile and the game (game.result(), hit_cause, wave_name,
upgrades, config) and never write the game. The settings and initials cards keep their own state here (the cursor,
the changes not yet saved, the slots); the Session holds them and passes their keys on.

* The settings card (scene `settings`, spec 4.2): a row per setting, then PILOT and BACK. A change sets the option
  at once and calls session.hooks.apply(key); a PALETTE, BACKGROUND or KEYS change is applied by the repaint rule of
  spec 2.9 (Repaints). Leaving the card saves what changed with one profile.update_settings().
* The initials card (scene `initials`, spec 6.2): three slots, typed or cycled; Enter renames the run's row (and the
  pilot), Esc skips. From the settings card's PILOT row it edits the pilot name instead.
* The results card (scene `results`, spec 8): every game's but Arcade's, whose panel stays over the 1978 block.
"""

from __future__ import annotations

import math
import string
import textwrap
from types import SimpleNamespace

from . import settings, ui
from .campaign.tables import ACTS, BOSSES, WAVES
from .keymap import label
from .model import TICK_RATE, WIDTH
from .storage import Placing, clean_name
from .term.cells import A_BOLD, A_REVERSE
from .theme import SLOT, resolve_theme

FIELD = 30  # the value field's width, between < at column 20 and > at column 51 (spec 4.2)
LABEL_X, LESS_X, FIELD_X, MORE_X = 4, 20, 21, 51
FIRST_ROW = 4  # the first setting's row; PILOT and BACK follow the 11 settings
CHANGE_X = 42  # the PILOT row's "Enter to change" starts at 44, or as far left as this for a longer key name
SAMPLE = (  # row 17: the palette in force, a glyph in each role (spec 4.2)
    ("/A\\", "player"),
    (" ship   ", ""),
    ("|", "player_shot"),
    (" yours   ", ""),
    ("! v", "enemy_shot"),
    (" theirs   ", ""),
    ("[R]", "pickup"),
    (" catch   ", ""),
    ("#", "shelter"),
    (" cover", ""),
)
SCHEMES = {"hold": "HOLD", "latched": "TAP", "timed": "TIMED"}  # the CONTROLS row's names for the scheme in use
STILL, APART = 0.5, 1.0  # a PALETTE, BACKGROUND or KEYS change waits STILL s still, APART s after the last one
REPAINTS = ("palette", "theme", "keys")  # KEYS too: to or from WASD, its apply restarts the terminal (ST-1)
CHARSET = string.ascii_uppercase + string.digits + "- "  # the initials' characters; the blank is last
SLOTS = 3
CARD_COMMANDS = frozenset({"up", "down", "left", "right", "select", "back", "erase"})


def key(keymap, context: str, action: str) -> str:
    """The first key of an action, as hints show it, or ? when none."""
    specs = keymap.keys_for(context, action)
    return label(specs[0]) if specs else "?"


def put(painter, y: int, x: int, text: str, role: str = "text", bold=False, dim=False, reverse=False) -> None:
    """Text in a role's style at a card position, in reverse video when asked (the current initials slot)."""
    if not reverse:
        painter.text(y, x, text, SLOT[role], bold, dim)
        return
    y, x = painter.top + y, painter.left + x
    if 0 <= y < painter.rows and 0 <= x < painter.columns:
        attr = painter.colors.get(SLOT["dim"] if dim else SLOT[role], 0) | (A_BOLD if bold else 0) | A_REVERSE
        painter.screen.addstr(y, x, text[: painter.columns - x], attr)


# --- the settings card ---------------------------------------------------------------------------------------------


class Repaints:
    """When a PALETTE, BACKGROUND or KEYS change is applied (spec 2.9): the field shows it at once, and the repaint
    follows once the value has stayed STILL seconds or the cursor left the row, and never sooner than APART seconds
    after the previous one, so a held Left/Right never swings the whole screen (nor, on KEYS, restarts the terminal)
    at the key rate. The Session keeps one; its update() runs on the loop's clock (Session.update_attract), on the
    card and after leaving it."""

    def __init__(self):
        self.pending: dict[str, float] = {}  # key -> when it last changed
        self.left: set[str] = set()  # pending keys whose row the cursor has left
        self.applied_at = -math.inf

    def change(self, key: str, now: float) -> None:
        self.pending[key] = now
        self.left.discard(key)

    def leave(self) -> None:
        """The cursor left the row, or the card: whatever is pending is due (by the APART rule)."""
        self.left |= set(self.pending)

    def due(self, now: float) -> list[str]:
        if not self.pending or now - self.applied_at < APART:
            return []
        if any(key in self.left or now - since >= STILL for key, since in self.pending.items()):
            return list(self.pending)
        return []

    def update(self, session, now: float) -> bool:
        """Apply what is due; True when something was (a full repaint follows)."""
        keys = self.due(now)
        if not keys:
            return False
        self.pending, self.left, self.applied_at = {}, set(), now
        for name in keys:
            session.hooks.apply(name)
        return True


class SettingsCard:
    """The settings card's cursor and what changed since it opened. Its rows: the 11 settings, PILOT, BACK."""

    ROWS = len(settings.SETTINGS) + 2

    def __init__(self):
        self.cursor = 0
        self.changes: dict[str, object] = {}  # setting key -> the value chosen on the card
        self.name: str | None = None  # a pilot name chosen on the card

    @property
    def row(self):
        """The selected setting's row, or None on PILOT or BACK."""
        return settings.SETTINGS[self.cursor] if self.cursor < len(settings.SETTINGS) else None

    def command(self, session, name: str, now: float) -> None:
        """A key on the card: Up/Down choose, Left/Right change, Enter = Right on a value (opens the editor on PILOT
        and leaves on BACK), Esc saves and goes back."""
        if name in ("up", "down"):
            if self.row is not None and self.row.key in REPAINTS:
                session.repaints.leave()
            self.cursor = (self.cursor + (1 if name == "down" else -1)) % self.ROWS
        elif name in ("left", "right") and self.row is not None:
            self.change(session, -1 if name == "left" else 1, now)
        elif name == "select":
            if self.row is not None:
                self.change(session, 1, now)
            elif self.cursor == len(settings.SETTINGS):
                if not settings.given(session.options, "name"):  # --name holds the row for this run
                    session.open_initials(pilot_mode=True)
            else:
                self.leave(session, now)
        elif name == "back":
            self.leave(session, now)

    def change(self, session, step: int, now: float) -> None:
        """The next or previous value of the selected row, unless the row is locked."""
        row, options = self.row, session.options
        if locked(session, row.key):
            return
        value = settings.get(options, row.key)
        values = row.values
        value = values[(values.index(value) + step) % len(values)] if value in values else values[0]
        settings.put(options, row.key, value)
        self.changes[row.key] = value
        if row.key in REPAINTS:
            session.repaints.change(row.key, now)
        else:
            session.hooks.apply(row.key)

    def leave(self, session, now: float) -> None:
        """Back to the title menu: what is pending is due, and what changed is saved once."""
        session.repaints.leave()
        session.repaints.update(session, now)
        save = getattr(session.profile, "update_settings", None)
        if (self.changes or self.name is not None) and callable(save):
            save(dict(self.changes), name=self.name)
        session.settings_card = None
        session.change_scene("menu")


def locked(session, key: str) -> bool:
    """Whether a row ignores Left/Right: a flag holds it, or (EFFECTS) reduced motion forces it off."""
    if settings.given(session.options, key):
        return True
    return key == "effects" and bool(getattr(session.options, "reduced_motion", False))


def painted(painter, options, value) -> bool:
    """Whether BACKGROUND `terminal` is painted anyway: HIGH CONTRAST paints its own background, but only where the
    tier has colours to paint with (truecolor and 256, not 16 colours or mono), which the theme in force says
    (FX-7)."""
    contrast = getattr(options, "palette", "default") == "contrast"
    return value == "terminal" and contrast and bool(getattr(painter.theme, "paint_background", False))


def palette_shown(painter, options, row, value) -> tuple[str, str] | None:
    """The PALETTE row's field and row 19 where the screen does not show the palette chosen (ST-3; spec 4.2: "a value
    this terminal can't honour shows what it gets"), else None: no colour (NO_COLOR, --mono, a terminal without
    colour), MONO under --color, or a palette 16 colours draw as DEFAULT. Read from the theme the screen is drawn
    in, so only once a change has been applied (spec 2.9); until then the field shows the name alone."""
    theme = painter.theme
    if getattr(theme, "palette", value) != value:
        return None
    label = row.label(value)
    if theme.tier == "mono" and value != "mono":
        if getattr(options, "color", "auto") == "mono":
            note = settings.MONO_FLAG_NOTE  # --mono or --color mono
        elif getattr(getattr(painter, "caps", None), "natural_color", None) not in (None, "mono"):
            note = settings.NO_COLOR_NOTE  # the terminal has colour, so NO_COLOR, over a palette nobody chose
        else:
            note = settings.MONO_TERMINAL_NOTE
        return f"{label}{settings.MONO_SUFFIX}", note
    if value == "mono" and theme.tier != "mono":  # only --color TIER beats the palette MONO
        return settings.MONO_COLOR, settings.MONO_COLOR_NOTE
    if theme.tier == "16" and value != "default":  # spec 3.6: 16 colours are default's but in cvd on dark
        shade = SimpleNamespace(color="16", theme=theme.name, paint_background=False)
        mine, base = resolve_theme(shade, value), resolve_theme(shade, "default")
        if (mine.fg, mine.bg, mine.attrs) == (base.fg, base.bg, base.attrs):
            return f"{label}{settings.SIXTEEN_SUFFIX}", settings.SIXTEEN_NOTE
    return None


def shown(painter, session, row) -> tuple[str, bool, str]:
    """What a setting's field shows, whether it is locked (dim), and its row 19 text (spec 4.1-4.2)."""
    options, key = session.options, row.key
    value = settings.get(options, key)
    profile_settings = getattr(session.profile, "settings", {})
    saved = profile_settings if isinstance(profile_settings, dict) else {}
    reduced = bool(getattr(options, "reduced_motion", False))
    if key == "effects" and reduced:
        return settings.REDUCED, True, settings.REDUCED_NOTE
    if settings.given(options, key):
        if key == "keys" and getattr(options, "keymap_file", None):
            return settings.CUSTOM, True, settings.CUSTOM_NOTE
        if key == "theme" and painted(painter, options, value):  # painted anyway, as the card says (ST-4)
            return settings.PAINTED_LOCKED, True, settings.PAINTED_LOCKED_NOTE
        flag = settings.typed_flag(options, key)  # as typed: --glyphs holds SCALE (ST-4)
        return f"{row.label(value)} ({flag})", True, settings.locked_note(key, saved, flag)
    if key == "theme" and painted(painter, options, value):
        return settings.PAINTED, False, settings.PAINTED_NOTE
    if key == "palette":
        honest = palette_shown(painter, options, row, value)
        if honest is not None:
            return honest[0], False, honest[1]
    if key == "controls":
        scheme = getattr(session.controls, "scheme", "latched")
        if value == "auto":
            return f"AUTO ({SCHEMES.get(scheme, scheme.upper())})", False, row.describe(value)
        if value == "hold" and scheme != "hold":
            return settings.HOLD_NEEDS, False, settings.HOLD_NOTE
    caps = getattr(painter, "caps", None)
    if key == "keys" and value == "wasd-physical" and caps is not None and not (caps.kitty_flags or 0) & 4:
        return settings.NOT_PHYSICAL, False, settings.NOT_PHYSICAL_NOTE
    if value == "auto" and key == "theme":
        return f"AUTO ({painter.theme.name.upper()})", False, row.describe(value)
    if value == "auto" and key == "scale":
        return f"AUTO ({settings.ROWS['scale'].label(str(painter.layout.k))})", False, row.describe(value)
    return row.label(value), False, row.describe(value)


def settings_hint(keymap) -> str:
    """Row 21: Up/Down choose  Left/Right change  Esc save and go back, from the settings context's keys."""
    up, down = key(keymap, "settings", "up"), key(keymap, "settings", "down")
    left, right = key(keymap, "settings", "left"), key(keymap, "settings", "right")
    return f"{up}/{down} choose  {left}/{right} change  {key(keymap, 'settings', 'back')} save and go back"


def settings_card(painter, session) -> None:
    """The settings card (spec 4.2, scene `settings`): the rows of settings.SETTINGS with their values centred in a
    30-character field, a flag-set row dim and locked, the PILOT and BACK rows, the live palette sample on row 17,
    the selected row's description on row 19 and the hint row on 21."""
    card = getattr(session, "settings_card", None) or SettingsCard()
    painter.center(1, "SETTINGS", color=SLOT["accent"], bold=True)
    painter.center(2, "Saved to your profile. A flag wins for one run.", dim=True)
    note = ""
    for index, row in enumerate(settings.SETTINGS):
        y, selected = FIRST_ROW + index, index == card.cursor
        value, dim, line = shown(painter, session, row)
        title_style = {"color": SLOT["accent"] if selected else SLOT["text"], "bold": selected}
        painter.text(y, LABEL_X - 2, ("> " if selected else "  ") + row.title, **title_style)
        painter.text(y, LESS_X, "<", dim=dim)
        painter.text(y, FIELD_X, f"{value[:FIELD]:^{FIELD}}", dim=dim)
        painter.text(y, MORE_X, ">", dim=dim)
        if selected:
            note = line
    options = session.options
    pilot_y, back_y = FIRST_ROW + len(settings.SETTINGS), FIRST_ROW + len(settings.SETTINGS) + 1
    for y, title in ((pilot_y, settings.PILOT), (back_y, settings.BACK)):
        selected = y - FIRST_ROW == card.cursor
        style = {"color": SLOT["accent"] if selected else SLOT["text"], "bold": selected}
        painter.text(y, LABEL_X - 2, ("> " if selected else "  ") + title, **style)
    name = str(options.name)
    if settings.given(options, "name"):
        painter.text(pilot_y, FIELD_X, f"{(name + ' (--name)')[:FIELD]:^{FIELD}}", dim=True)
        pilot_note = settings.PILOT_LOCKED
    else:
        select = key(session.keymap, "settings", "select")  # the key that opens the editor, as the keymap has it
        change = f"{select} to change"[: WIDTH - CHANGE_X]
        painter.text(pilot_y, FIELD_X, f"{name[:10]:^{FIELD}}")
        painter.text(pilot_y, max(CHANGE_X, min(44, WIDTH - len(change))), change, dim=True)
        pilot_note = settings.PILOT_NOTE.format(key=select)
    if card.cursor == len(settings.SETTINGS):
        note = pilot_note
    elif card.cursor == len(settings.SETTINGS) + 1:
        note = settings.BACK_NOTE
    x = (WIDTH - sum(len(text) for text, _ in SAMPLE)) // 2
    for text, role in SAMPLE:
        put(painter, 17, x, text, role or "text", bold=bool(role), dim=not role)
        x += len(text)
    painter.center(19, note[: settings.DESCRIPTION_WIDTH], color=SLOT["warn"])
    painter.center(21, settings_hint(session.keymap), dim=True)
    alert = session.note or session.profile.alert
    if not alert and getattr(session.profile, "settings_unreadable", False) is True:
        alert = settings.UNREADABLE
    painter.footer(session, alert=alert)


# --- the initials card ---------------------------------------------------------------------------------------------


class InitialsCard:
    """Three slots of A-Z, 0-9, - and blank, prefilled from a name (AAA for the default PILOT), the current slot
    marked by a ^ and reverse video (spec 6.2).

    pilot: the index of the pilot whose run placed (None on the settings card's PILOT row, which edits the pilot
    name); placing: that run's storage.Placing; rank: its rank as the profile reads it now; opened: when the card
    opened, on the Session's clock (a run's card takes no key for ui.SETTLE s after it)."""

    def __init__(self, name: str, *, pilot: int | None = None, placing=None, rank: int | None = None, opened=-math.inf):
        self.name, self.pilot, self.placing, self.rank, self.opened = str(name), pilot, placing, rank, opened
        start = "AAA" if self.name == "PILOT" else self.name[:SLOTS]
        self.slots = [char if char in CHARSET else " " for char in f"{start:<{SLOTS}}"[:SLOTS]]
        self.prefill = tuple(self.slots)
        self.cursor = 0
        self.at_end = False  # the last slot was just typed: Backspace clears it before moving left

    @property
    def saves_as(self) -> str:
        """The name Enter saves, exactly as row 11 shows it: the slots through clean_name (which drops blanks); the
        old name when every slot is blank, and the whole old name for the unchanged prefill."""
        if tuple(self.slots) == self.prefill or not "".join(self.slots).strip():
            return self.name
        return clean_name("".join(self.slots))

    def type(self, char: str) -> None:
        """A typed letter, digit or - sets the slot and moves on (the blank is reached with Up/Down)."""
        self.slots[self.cursor] = char.upper()
        self.at_end = self.cursor == SLOTS - 1
        self.cursor = min(SLOTS - 1, self.cursor + 1)

    def command(self, name: str) -> str | None:
        """Up/Down cycle the slot, Left/Right move, Backspace clears back one slot; select or back for the Session
        to act on (save or skip)."""
        if name in ("up", "down"):
            char = self.slots[self.cursor]
            index = CHARSET.index(char) if char in CHARSET else -1
            self.slots[self.cursor] = CHARSET[(index + (1 if name == "up" else -1)) % len(CHARSET)]
        elif name in ("left", "right"):
            self.cursor = max(0, min(SLOTS - 1, self.cursor + (1 if name == "right" else -1)))
        elif name == "erase":
            if not self.at_end:
                self.cursor = max(0, self.cursor - 1)
            self.slots[self.cursor] = " "
        elif name in ("select", "back"):
            return name
        self.at_end = False
        return None


def board_name(config) -> str:
    """A board as players see it: CAMPAIGN / NORMAL, ARCADE / 1978, DAILY / NORMAL."""
    return f"{ui.mode_name(config.mode)} / {ui.difficulty_label(config.mode, config.difficulty)}"


def initials_hints(keymap) -> tuple[str, str, str]:
    """Rows 13, 14 and 21 of the initials card, from the initials context's keys."""
    up, down = key(keymap, "initials", "up"), key(keymap, "initials", "down")
    left, right = key(keymap, "initials", "left"), key(keymap, "initials", "right")
    save, skip = key(keymap, "initials", "select"), key(keymap, "initials", "back")
    return (
        f"Type letters, or {up}/{down} to change, {left}/{right} to move",
        f"{save} save        {skip} skip",
        f"Type or {up}/{down}  {left}/{right} move  {save} save  {skip} skip",
    )


def initials_card(painter, session) -> None:
    """The initials card (spec 6.2, scene `initials`): NEW HIGH SCORE, the rank and board, the score, three slots
    with the current one marked by a ^ and reverse video, SAVES AS and the keys."""
    card = getattr(session, "initials_card", None) or InitialsCard(session.options.name)
    keys, save_keys, hint = initials_hints(session.keymap)
    if card.pilot is None:  # the settings card's PILOT row
        painter.center(3, "YOUR INITIALS", color=SLOT["accent"], bold=True)
        painter.center(5, "They go on the high scores with every run.", dim=True)
        save_keys = save_keys.replace(" skip", " cancel")
        hint = hint.replace(" skip", " cancel")
    else:
        pilot = session.pilots[card.pilot] if card.pilot < len(session.pilots) else session.pilot
        game = pilot.game
        painter.center(3, "NEW HIGH SCORE", color=SLOT["accent"], bold=True)
        rank = f"#{card.rank}   " if card.rank else ""
        painter.center(5, f"{rank}{board_name(game.config)}", color=SLOT["good"])
        title = f"{pilot.name}   SCORE {game.score:,}" if len(session.pilots) > 1 else f"SCORE {game.score:,}"
        painter.center(6, title, bold=True)
    x = (painter.width - (4 * SLOTS - 3)) // 2  # the card is narrower than 60 for Arcade at 56-59 columns
    for index, char in enumerate(card.slots):
        current = index == card.cursor
        blank = char == " "
        put(painter, 9, x + 4 * index, "_" if blank else char, bold=True, dim=blank and not current, reverse=current)
    painter.text(10, x + 4 * card.cursor, "^", SLOT["accent"], True)
    painter.center(11, f"SAVES AS  {card.saves_as}")
    painter.center(13, keys, dim=True)
    painter.center(14, save_keys, color=SLOT["accent"])
    painter.center(21, hint, dim=True)
    painter.footer(session, sound=False)


# --- the results card ----------------------------------------------------------------------------------------------

SHOT_NAMES = {"pellet", "dart", "hunter", "needle", "slug", "cluster"}
COUNTS = {1: "once", 2: "twice"}
VALUE_X = 16  # the results rows' values (labels at column 3)
VALUE_WIDTH = WIDTH - VALUE_X - 2
RUN_ROWS = 15  # rows 4-18: the seed is on row 19 and the keys on row 21
LIST_ROWS = 3  # the most rows UPGRADES (or MODIFIERS) takes; a longer list ends with "+n more"


def cause_line(game) -> str:
    """What ended the run, in words (spec 8.2)."""
    if game.phase == "victory":
        return f"ALL {len(WAVES)} WAVES CLEARED"
    if game.phase != "game_over":
        return "THE TAPE ENDS HERE"
    cause = game.hit_cause or ""
    if cause in SHOT_NAMES:
        words = f"SHOT DOWN BY A {cause.upper()}"
    elif cause.endswith(" boss"):
        boss = boss_name(game)
        shot = cause[: -len(" boss")].upper()
        words = f"THE {boss}'S {shot}" if boss else f"A BOSS'S {shot}"
    elif cause == "beam":
        words = "CAUGHT IN A BEAM"
    elif cause == "landing":
        words = "THE FLEET LANDED"
    elif cause:
        words = f"RAMMED BY A {cause.upper()}"
    else:
        return "GAME OVER"
    return f"GAME OVER - {words}"


def boss_name(game) -> str:
    """The name of the boss of the wave reached: the boss on the field, else the wave's name when it is a boss's."""
    boss = getattr(game, "boss", None)
    if boss is not None and boss.kind in BOSSES:
        return game.wave_name or BOSSES[boss.kind].name
    names = {row.name for row in BOSSES.values()}
    return game.wave_name if any(game.wave_name.startswith(name) for name in names) else ""


def reached(game) -> str:
    """ACT II  WAVE 08  "SERPENT", or Endless's WAVE 031  "NAME"."""
    name = f'  "{game.wave_name}"' if game.wave_name else ""
    if game.config.mode == "endless":
        return f"WAVE {game.wave:03d}{name}"
    return f"ACT {ACTS[game.act - 1].roman}  WAVE {game.wave:02d}{name}"


def clock(ticks: int) -> str:
    seconds = ticks // TICK_RATE
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def boss_list(count: int) -> list[str]:
    """The bosses beaten, in the order a run meets them (Endless's loops replay acts II-IV, so every boss but the
    WARDEN again); a boss beaten more than once gets a count."""
    order = [row.name for row in sorted(BOSSES.values(), key=lambda row: row.wave)]
    beaten: dict[str, int] = {}
    for index in range(count):
        name = order[index] if index < len(order) else order[1 + (index - len(order)) % (len(order) - 1)]
        beaten[name] = beaten.get(name, 0) + 1
    return [name + (f" x{times}" if times > 1 else "") for name, times in beaten.items()]


def upgrade_names(game) -> list[str]:
    """The upgrades owned by name, in pick order, a stack with its count: RAIL ROUNDS x2."""
    return [name + (f" x{stacks}" if stacks > 1 else "") for name, stacks in game.upgrades.items()]


def listed(names, width: int, most: int = LIST_ROWS) -> list[str]:
    """Names joined by commas in at most `most` lines of at most width, a name never split over two lines; when
    they need more, the last line ends with "+n more" for the names left out."""
    names = [name[:width] for name in names]
    for shown in range(len(names), -1, -1):
        rest = len(names) - shown
        lines, line = [], ""
        for name in [*names[:shown], *([f"+{rest} more"] if rest else [])]:
            joined = f"{line}, {name}" if line else name
            if len(joined) + 1 > width and line:  # room for the comma that ends the line
                lines.append(line + ",")
                line = name
            else:
                line = joined
        lines += [line] if line else []
        if len(lines) <= most:
            return lines
    return []


def unlock_list(names) -> str:
    names = list(names)
    shown_names = ", ".join(names[:2])
    return shown_names + (f", +{len(names) - 2} more" if len(names) > 2 else "")


def run_rows(session, pilot, replay: bool) -> list[tuple[str, str, str]]:
    """The single-pilot rows (label, value, a word after the value), the empty ones left out (spec 8.2)."""
    game = pilot.game
    result = game.result()
    placing = None if replay else getattr(pilot, "placing", None)
    placing = placing if isinstance(placing, Placing) else None
    rank = None
    if placing is not None:
        reader = getattr(session.profile, "rank", None)
        rank = reader(placing) if callable(reader) else placing.rank
    best_before = placing.best_before if placing is not None else None
    new_best = placing is not None and rank is not None and (best_before is None or game.score > best_before)
    rows = [("SCORE", f"{game.score:,}", "NEW BEST" if new_best else "")]
    if rank is not None:
        rows.append(("RANK", f"#{rank} ON {board_name(game.config)}", ""))
    if best_before is not None:
        rows.append(("BEST BEFORE", f"{best_before:,}", ""))
    rows.append(("REACHED", reached(game), ""))
    rows.append(("TIME", clock(result["ticks"]), ""))
    accuracy = 100 * result["hits"] / max(1, result["shots"])
    rows.append(("KILLS", f"{result['kills']:<8}ACCURACY {accuracy:.0f}%     BEST COMBO x{result['best_combo']}", ""))
    if result.get("bosses"):  # all four bosses are wider than one row: they take a second
        for number, line in enumerate(listed(boss_list(result["bosses"]), VALUE_WIDTH, 2)):
            rows.append(("BOSSES" if number == 0 else "", line, ""))
    if result.get("perfect_waves"):
        count = result["perfect_waves"]
        rows.append(("PERFECT", f"{count} wave{'' if count == 1 else 's'}", ""))
    if result.get("bombs_used") or result.get("bomb_kills"):
        rows.append(("BOMBS", f"{result['bombs_used']} used, {result['bomb_kills']} kills", ""))
    tail = []
    if result.get("continues_used"):
        count = result["continues_used"]
        tail.append(("CONTINUED", COUNTS.get(count, f"{count} times"), ""))
    if pilot.unlocked:
        tail.append(("UNLOCKED", unlock_list(pilot.unlocked), ""))
    lists = [("UPGRADES", upgrade_names(game))]
    if game.config.mode == "endless":
        lists.append(("MODIFIERS", list(getattr(game, "modifiers", ()))))
    lists = [(title, names) for title, names in lists if names]
    room = RUN_ROWS - len(rows) - len(tail)
    for index, (title, names) in enumerate(lists):
        later = len(lists) - index - 1  # a row kept for each list after this one
        for number, line in enumerate(listed(names, VALUE_WIDTH, max(1, min(LIST_ROWS, room - later)))):
            rows.append((title if number == 0 else "", line, ""))
            room -= 1
    return rows + tail


def duel_column(pilot) -> list[str]:
    """One pilot's column of the duel's results: name, score, reached, accuracy, best combo, cause."""
    game, result = pilot.game, pilot.game.result()
    accuracy = 100 * result["hits"] / max(1, result["shots"])
    reach = f"WAVE {game.wave:02d}" if game.config.mode != "endless" else f"WAVE {game.wave:03d}"
    lines = [
        pilot.name,
        f"SCORE {game.score:,}",
        f"REACHED ACT {ACTS[game.act - 1].roman} {reach}" if game.config.mode != "endless" else f"REACHED {reach}",
        f"ACCURACY {accuracy:.0f}%",
        f"BEST COMBO x{result['best_combo']}",
    ]
    if pilot.verified is not None:
        lines.append("VERIFIED" if pilot.verified else "MISMATCH")
    cause = cause_line(game).removeprefix("GAME OVER - ")
    return lines + textwrap.wrap(cause, 26)[:2]


def results_card(painter, session) -> None:
    """The results card (spec 8, scene `results` for every game but Arcade's): what ended the run in words, score,
    rank and best before, where it reached, time, kills, bosses, bombs, upgrades by name, continues and unlocks; the
    duel's two columns; a replay's VERIFIED or MISMATCH."""
    game, replay, keymap = session.pilot.game, bool(session.replay), session.keymap
    duel = len(session.pilots) == 2
    if replay:
        title = "REPLAY VERIFIED" if session.replay_verified else "REPLAY MISMATCH"
        keys = ui.panel_keys("replay_results", keymap)[0]
    elif duel:
        first, second = session.pilots
        winner = max(session.pilots, key=lambda pilot: pilot.game.score)
        title = "DUEL DRAW" if first.game.score == second.game.score else f"{winner.name} WINS"
        keys = ui.panel_keys("duel_results", keymap)[0]
    else:
        title = "EARTH SAVED" if game.phase == "victory" else "SIGNAL LOST"
        keys = ui.panel_keys("results", keymap)[0]
    good = game.phase == "victory" or replay and session.replay_verified
    painter.center(1, title, color=SLOT["good" if good else "danger"], bold=True)
    if duel:
        for column, pilot in zip((3, 31), session.pilots):
            for row, line in enumerate(duel_column(pilot), 4):
                painter.text(row, column, line[:27], SLOT["accent"] if row == 4 else SLOT["text"], row == 4)
    else:
        line = cause_line(game)
        if game.config.mode == "daily":
            seed = str(game.config.seed)
            day = f"DAILY {seed[:4]}-{seed[4:6]}-{seed[6:]}" if len(seed) == 8 else f"DAILY {seed}"
            line = f"{line}   {day}" if len(line) + len(day) + 3 <= WIDTH - 2 else line
        painter.center(2, line, color=SLOT["warn"])
        for row, (name, value, extra) in enumerate(run_rows(session, session.pilot, replay)[:RUN_ROWS], 4):
            painter.text(row, 3, name, SLOT["accent"], True)
            painter.text(row, VALUE_X, value[:VALUE_WIDTH])
            if extra:
                painter.text(row, VALUE_X + len(value) + 4, extra, SLOT["good"], True)
    seeds = sorted({pilot.game.config.seed for pilot in session.pilots})
    painter.center(19, "SEED " + " ".join(str(seed) for seed in seeds), dim=True)
    painter.center(21, keys, color=SLOT["accent"])
    painter.footer(session)
