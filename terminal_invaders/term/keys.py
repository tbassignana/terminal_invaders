"""Input events: keys with kitty-style modifiers and event kinds, and terminal replies."""

from __future__ import annotations

from dataclasses import dataclass

SHIFT, ALT, CTRL, SUPER, HYPER, META, CAPS_LOCK, NUM_LOCK = 1, 2, 4, 8, 16, 32, 64, 128
LOCKS = CAPS_LOCK | NUM_LOCK
MODS = SHIFT | ALT | CTRL | SUPER | HYPER | META
PRESS, REPEAT, RELEASE = "press", "repeat", "release"
NAMED = frozenset(
    ("space", "enter", "escape", "tab", "backspace", "up", "down", "left", "right", "home", "end", "insert")
    + ("delete", "page_up", "page_down", "pause")
    + tuple(f"f{n}" for n in range(1, 36))
)
PRIVATE_USE = range(57344, 63744)
# Kitty functional codes; every other private-use code (locks, PRINT, MENU, KP_BEGIN, media, bare modifiers)
# is dropped. Keypad keys are normalised to the main keys they mirror.
KITTY = {9: "tab", 13: "enter", 27: "escape", 127: "backspace", 32: "space", 57362: "pause"}
KITTY |= {57399 + n: str(n) for n in range(10)}
KITTY |= dict(zip(range(57409, 57417), (".", "/", "*", "-", "+", "enter", "=", ",")))
KITTY |= dict(zip(range(57417, 57427), ("left", "right", "up", "down", "page_up", "page_down", "home", "end")))
KITTY |= {57425: "insert", 57426: "delete"}
KITTY |= {57376 + n: f"f{13 + n}" for n in range(23)}


@dataclass(frozen=True, slots=True)
class Key:
    name: str  # a printable character (letters lower case) or a name in NAMED
    mods: int = 0  # lock bits already stripped
    kind: str = PRESS  # press | repeat | release
    base: str | None = None  # kitty base-layout key (flag 4), for @physical bindings
    certain: bool = False  # the protocol reports event types (kitty)
    time: float = 0.0


@dataclass(frozen=True, slots=True)
class Reply:
    kind: str  # kitty_flags da1 da2 decrpm dsr cpr xtversion osc11 focus paste downgrade unknown
    value: object = None
    time: float = 0.0


def is_key_name(name: str) -> bool:
    return name in NAMED or (len(name) == 1 and name.isprintable() and name != " ")


def kitty_name(code: int) -> str | None:
    """Key name for a kitty (or modifyOtherKeys) code point; None when the key is dropped."""
    if code in KITTY:
        return KITTY[code]
    if code in PRIVATE_USE or not 0x20 < code < 0x110000:
        return None
    char = chr(code)
    return char if char.isprintable() else None
