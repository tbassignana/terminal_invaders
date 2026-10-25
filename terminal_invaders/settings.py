"""Phase 5's settings: the table of what can be set, validation, and the flag / saved / default merge (spec 4).

A setting comes, for one run, from the flag on the command line if given, else the profile's saved value if valid,
else the built-in default (spec 4.4, decision 13). Flags are never saved. The profile keeps the raw "settings"
object (storage.Profile.settings); this module interprets it: a saved value is valid only when its type is exactly
the row's and it is one of the row's values, so "sound": 1 or "fps": 60.0 are not. Unknown keys and invalid values
are ignored for the run and kept on every save (spec 4.3).

Each row's key is its key in the profile and the option it sets, except SOUND, whose key `sound` sets the option
`no_sound` (its negation, as `--no-sound` does). get() and put() read and set a row's value on the options, so the
settings card never needs to know that. The texts the card shows live here too: the value labels, each row's line
for row 19 (at most 58 characters, for every value), and the lines for a row a flag holds.
"""

from __future__ import annotations

from typing import NamedTuple

from .keymap import PRESETS
from .theme import PALETTE_LABELS, PALETTE_NAMES

DESCRIPTION_WIDTH = 58  # row 19 of the card (spec 4.2)


class Setting(NamedTuple):
    """One row of the settings card (spec 4.1)."""

    key: str  # the key in the profile's "settings" object, and the option it sets (sound sets no_sound)
    values: tuple  # the values it takes, in the card's order (Left/Right cycle them)
    labels: tuple[str, ...]  # what the card shows for each value, in the same order
    default: object  # the built-in default, equal to the base tree's argparse default (spec 4.4)
    type: type  # the exact type a saved value must have: str, bool or int
    description: str  # row 19's text when the row is selected, at most 58 characters
    flag: str | None  # the flag that wins for one run (--palette), or None (TITLE DEMO has none)
    title: str = ""  # the row's name on the card, e.g. PALETTE (columns 4-17)
    notes: tuple[str, ...] = ()  # row 19's text for each value, in values' order; "" (or none) is the description

    def label(self, value) -> str:
        """What the card shows for value; one this row does not list (--fps 45) as itself."""
        return self.labels[self.values.index(value)] if value in self.values else str(value).upper()

    def describe(self, value) -> str:
        """Row 19 for this row with value selected."""
        index = self.values.index(value) if value in self.values else len(self.notes)
        return (self.notes[index] if index < len(self.notes) else "") or self.description


SETTINGS: tuple[Setting, ...] = (
    Setting(
        "palette",
        PALETTE_NAMES,
        tuple(PALETTE_LABELS[name] for name in PALETTE_NAMES),
        "default",
        str,
        "Colours for the game. Arcade keeps its 1978 colours.",
        "--palette",
        "PALETTE",
        (
            "Today's colours. Arcade keeps its 1978 colours.",
            "Red-green safe: warm colours hurt. Arcade keeps 1978's.",
            "Low vision: white or black is yours, colour is theirs.",
            "No colour at all, as with --mono or NO_COLOR.",
        ),
    ),
    Setting(
        "theme",
        ("auto", "dark", "light", "terminal"),
        ("AUTO", "DARK", "LIGHT", "TERMINAL"),
        "auto",
        str,
        "Dark or light colours. AUTO matches your terminal.",
        "--theme",
        "BACKGROUND",
        (
            "AUTO matches your terminal's background, dark or light.",
            "Light colours on a dark background.",
            "Dark colours on a light background.",
            "Keep your terminal's own background colour.",
        ),
    ),
    Setting(
        "effects",
        ("full", "calm", "off"),
        ("FULL", "CALM", "OFF"),
        "full",
        str,
        "Sparks, shakes and stars in the Campaign modes.",
        "--effects",
        "EFFECTS",
        (
            "Sparks, a shake when you are hit, drifting stars.",
            "Bursts stay; no shake, stars hold still.",
            "No effects: the plain field.",
        ),
    ),
    Setting(
        "reduced_motion",
        (False, True),
        ("OFF", "ON"),
        False,
        bool,
        "ON: still poses, no effects; popups show as still text.",
        "--reduced-motion",
        "REDUCED MOTION",
    ),
    Setting(
        "scale",
        ("auto", "1", "2", "3", "4", "6"),
        ("AUTO", "TEXT", "2X", "3X", "4X", "6X"),
        "auto",
        str,
        "The largest pixel art to use; smaller windows use less.",
        "--scale",
        "SCALE",
        ("AUTO: the largest pixel art that fits the window.", "Text glyphs at every window size."),
    ),
    Setting(
        "controls",
        ("auto", "hold", "latched", "timed"),
        ("AUTO", "HOLD", "TAP", "TIMED"),
        "auto",
        str,
        "How keys move your ship.",
        "--controls",
        "CONTROLS",
        (
            "AUTO: hold keys where the terminal reports releases.",
            "Hold a key to move; let go to stop.",
            "Tap a direction to move; tap the other way to stop.",
            "Press to move; held keys are read from key repeat.",
        ),
    ),
    Setting(
        "keys",
        tuple(PRESETS),
        ("DEFAULT", "ARROWS", "VIM", "WASD"),
        "default",
        str,
        "Which keys play.",
        "--keys",
        "KEYS",
        (
            "A/D or arrows move, Space fires, B bombs.",
            "Arrows move, Space or Up fires, B bombs.",
            "H/L move, J stops, K or Space fires.",
            "WASD by key position, on any keyboard layout.",
        ),
    ),
    Setting(
        "fps",
        (60, 30, 20, 15),
        ("60", "30", "20", "15"),
        60,
        int,
        "Frames a second; the game itself always runs at 60.",
        "--fps",
        "FRAME RATE",
    ),
    Setting(
        "sound",
        (True, False),
        ("ON", "OFF"),
        True,
        bool,
        "Sound at launch. M turns it on or off while you play.",
        "--no-sound",
        "SOUND",
    ),
    Setting(
        "attract",
        ("auto", "on", "off"),
        ("AUTO", "ON", "OFF"),
        "auto",
        str,
        "A recorded run plays when the title is left alone.",
        None,
        "TITLE DEMO",
        ("AUTO: a demo on an idle title, unless reduced motion.", "", "No demo on the title."),
    ),
    Setting(
        "tips",
        ("auto", "always", "off"),
        ("AUTO", "ALWAYS", "OFF"),
        "auto",
        str,
        "Tips on the bottom row while you play.",
        "--tips",
        "TIPS",
        (
            "AUTO: tips in your first Campaign, Endless or Daily run.",
            "Tips on every Campaign, Endless or Daily run.",
            "No tips.",
        ),
    ),
)
ROWS = {row.key: row for row in SETTINGS}

# argparse dest -> the option name it puts in options.given when given on the command line (spec 4.4): a second
# parse with these dests set to argparse.SUPPRESS leaves only the given ones (--glyphs gives scale, --no-sound
# no_sound, --keymap keys). name is the PILOT row's (--name).
SETTING_DESTS: dict[str, str] = {
    **{row.key: row.key for row in SETTINGS if row.flag and row.key != "sound"},
    "no_sound": "no_sound",
    "keymap_file": "keys",
    "name": "name",
}
OPTIONS = {"sound": "no_sound"}  # a row's key -> the option it sets, where they differ

TIPS_DONE = "tips_done"  # the one key that is not a row: a bool, default False, set after the first run (spec 7.1)
EXTRA = {TIPS_DONE: (bool, False)}  # keys that are not rows: (type, default); any bool is valid

# The texts of the card's special cases (spec 4.1-4.3), each at most 58 characters
LOCKED = "Set by {flag} for this run. Saved: {saved}."  # a row a flag holds; {saved} is the saved value's label
CUSTOM = "CUSTOM"  # KEYS under --keymap FILE: shown, and the row locked
CUSTOM_NOTE = "Keys from the --keymap file, for this run."
PAINTED = "TERMINAL (PAINTED)"  # BACKGROUND terminal under HIGH CONTRAST
PAINTED_NOTE = "High contrast paints its own background to keep contrast."
PAINTED_LOCKED = f"{PAINTED} (--theme)"  # the same with --theme terminal given (ST-4)
PAINTED_LOCKED_NOTE = "Set by --theme; high contrast paints its own background."
REDUCED = "OFF (REDUCED MOTION)"  # EFFECTS while reduced motion is on
REDUCED_NOTE = "Effects are off while reduced motion is on."
HOLD_NEEDS = "TAP (HOLD NEEDS RELEASES)"  # CONTROLS HOLD on a terminal without key releases
HOLD_NOTE = "This terminal doesn't report key releases: you tap."
NOT_PHYSICAL = "WASD (NOT PHYSICAL HERE)"  # KEYS wasd-physical without kitty flag 4
NOT_PHYSICAL_NOTE = "This terminal can't tell key positions: WASD as letters."
PILOT = "PILOT"  # the card's PILOT row (the profile's name), edited as the initials card does
PILOT_NOTE = "Your initials on the high scores. {key} to change."  # {key}: the settings context's select (SC-5)
PILOT_LOCKED = "Set by --name for this run."
BACK = "BACK"
BACK_NOTE = "Save your settings and go back to the title."
# The PALETTE row where the screen does not show the palette (ST-3): the label with a suffix, or MONO under --color
MONO_SUFFIX = " (MONO)"
NO_COLOR_NOTE = "NO_COLOR is set: choose a palette to see colour."
MONO_FLAG_NOTE = "--mono or --color mono turns colour off for this run."
MONO_TERMINAL_NOTE = "This terminal has no colour: every palette shows as MONO."
MONO_COLOR = "MONO (--color)"
MONO_COLOR_NOTE = "--color turns colour on for this run."
SIXTEEN_SUFFIX = " (16 COLOURS)"
SIXTEEN_NOTE = "This terminal has 16 colours: this shows as DEFAULT."
READ_ONLY = "Settings not saved: this profile is read-only"  # the alert (spec 4.3)
UNREADABLE = "Settings not saved: this profile's settings are unreadable"  # a "settings" that is not an object
TEXTS = (CUSTOM_NOTE, PAINTED_NOTE, REDUCED_NOTE, HOLD_NOTE, NOT_PHYSICAL_NOTE, PILOT_NOTE, PILOT_LOCKED, BACK_NOTE)
TEXTS += (READ_ONLY, UNREADABLE, PAINTED_LOCKED_NOTE, NO_COLOR_NOTE, MONO_FLAG_NOTE, MONO_TERMINAL_NOTE)
TEXTS += (MONO_COLOR_NOTE, SIXTEEN_NOTE)
FIELDS = (CUSTOM, PAINTED, PAINTED_LOCKED, REDUCED, HOLD_NEEDS, NOT_PHYSICAL, MONO_COLOR)  # shown as they are


def valid(key: str, value: object) -> bool:
    """Whether a saved value can be used: key is a row's (or tips_done) and type(value) is exactly the row's type
    and value is one of its values (spec 4.3)."""
    if key in ROWS:
        row = ROWS[key]
        return type(value) is row.type and value in row.values
    if key in EXTRA:
        return type(value) is EXTRA[key][0]
    return False


def default(key: str) -> object:
    """The built-in default of a row or of tips_done."""
    return ROWS[key].default if key in ROWS else EXTRA[key][1]


def is_default(key: str, value: object) -> bool:
    """Whether value is key's built-in default, exactly (True is not 1): the profile then holds no key for it."""
    return (key in ROWS or key in EXTRA) and type(value) is type(default(key)) and value == default(key)


def saved_value(saved: dict, key: str) -> object:
    """The value the profile's settings object holds for key if it is valid, else the built-in default."""
    value = saved.get(key) if isinstance(saved, dict) else None
    return value if valid(key, value) else default(key)


def option_name(key: str) -> str:
    """The option a row sets: its key, but no_sound for sound."""
    return OPTIONS.get(key, key)


def get(options, key: str) -> object:
    """The row's value as the options hold it (sound is not options.no_sound); the default for options without it."""
    if key == "sound":
        return not getattr(options, "no_sound", False)
    return getattr(options, key, ROWS[key].default)


def put(options, key: str, value: object) -> None:
    """Set the row's option to value (sound sets no_sound to its negation)."""
    if key == "sound":
        options.no_sound = not value
    else:
        setattr(options, key, value)


def given(options, key: str) -> bool:
    """Whether a flag on this command line holds the row (options.given), so the card shows it locked."""
    return option_name(key) in getattr(options, "given", frozenset())


def typed_flag(options, key: str) -> str | None:
    """The flag that holds a row for this run, as the player typed it (options.given_flags, from cli.given_flags):
    --glyphs holds SCALE, whose own flag is --scale (ST-4). The row's own flag otherwise."""
    return getattr(options, "given_flags", {}).get(key) or ROWS[key].flag


def locked_note(key: str, saved: dict, flag: str | None = None) -> str:
    """Row 19 for a row a flag holds: which flag (`flag`, as typed_flag() gives it; the row's own by default), and
    what the profile has saved."""
    row = ROWS[key]
    return LOCKED.format(flag=flag or row.flag, saved=row.label(saved_value(saved, key)))


def merge(options, saved: dict):
    """Set every setting on options from its flag if given (options.given), else the valid saved value, else the
    built-in default, and return options (spec 4.4). cli.main calls it right after it opens the profile and before
    it builds the keymap and Audio. A saved object that is not a dict counts as empty; the profile's name is not a
    row (cli.main takes it, as before)."""
    saved = saved if isinstance(saved, dict) else {}
    for row in SETTINGS:
        if not given(options, row.key):
            put(options, row.key, saved_value(saved, row.key))
    return options
