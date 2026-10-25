"""Key bindings per input context: presets, the key-spec grammar, validation and the JSON loader.

A key spec is [ctrl+|alt+|super+]*name, where name is one printable character or a named key; @name marks a
physical binding that matches the kitty base-layout key (flag 4). SHIFT and lock bits never matter.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .term.keys import ALT, CTRL, LOCKS, SHIFT, SUPER, Key, is_key_name

COMMAND, CONTROL, PULSE, NAVIGATION, SIGNAL = "command", "control", "pulse", "navigation", "signal"
PLAY_CONTROLS = {"left": CONTROL, "right": CONTROL, "stop": CONTROL, "fire": CONTROL}
ACTIONS = {
    "global": {"sound": COMMAND, "stats": COMMAND, "redraw": COMMAND}
    | {"sig_int": SIGNAL, "sig_tstp": SIGNAL, "sig_quit": SIGNAL},
    "play": PLAY_CONTROLS | {"bomb": PULSE, "autofire": PULSE, "pause": COMMAND},
    "countdown": PLAY_CONTROLS | {"pause": COMMAND},  # the controls are pre-armed; the engine does not tick
    "pause": {"resume": COMMAND, "restart": COMMAND, "menu": COMMAND},
    "menu": {"up": NAVIGATION, "down": NAVIGATION, "left": NAVIGATION, "right": NAVIGATION}
    | {"select": COMMAND, "back": COMMAND},
    "pages": {"page": NAVIGATION, "back": COMMAND},
    "handoff": {"start": COMMAND, "pause": COMMAND},
    "results": {"again": COMMAND, "menu": COMMAND},
    "replay": {"pause": COMMAND},
    "replay_pause": {"resume": COMMAND, "exit": COMMAND},
    "replay_results": {"exit": COMMAND},
}
# The settings and initials cards (Phase 5 spec 10) take the menu's actions and keys, as countdown takes play's:
# settings exactly the menu's; initials the menu's but its printable keys (letters, digits and Space are typed on
# that card), plus erase on Backspace. Neither adds a key the menu lacks but Backspace, which pages binds already,
# so a keymap file that was valid before stays valid. The title demo's context binds nothing: any key ends it, and
# Session.handle reads it before the Router (spec 5.3).
MENU_MIRRORS = ("settings", "initials")
ACTIONS |= {"settings": dict(ACTIONS["menu"]), "initials": dict(ACTIONS["menu"]) | {"erase": COMMAND}}
ACTIONS |= {"attract": {}}
ERASE = ("backspace",)  # the initials card's erase, unless the global context or the menu took Backspace
# Play actions a keymap may leave unbound. No preset binds autofire: the campaign ignores it (holding Space fires
# whenever a shot is free) and Arcade always did. It stays an action, so keymap files that bind it still load.
OPTIONAL_PLAY = frozenset({"autofire"})
RESERVED = {"sig_int": "ctrl+c", "sig_tstp": "ctrl+z", "sig_quit": "ctrl+\\"}
NO_EXIT = ("play", "countdown", "handoff")  # abandoning a run is only possible from the pause menu
ABANDON = frozenset(("menu", "restart", "quit", "exit", "back", "again"))
WAYS_OUT = {
    "play": ("pause",),
    "countdown": ("pause",),
    "pause": ("resume", "restart", "menu"),
    "menu": ("select", "back"),
    "pages": ("back",),
    "handoff": ("start", "pause"),
    "results": ("again", "menu"),
    "replay": ("pause",),
    "replay_pause": ("resume", "exit"),
    "replay_results": ("exit",),
    "settings": ("back",),
    "initials": ("select", "back"),
}
PREFIXES = {"ctrl+": CTRL, "alt+": ALT, "super+": SUPER}
SHORT = {"escape": "Esc", "backspace": "Bksp", "page_up": "PgUp", "page_down": "PgDn", "insert": "Ins", "delete": "Del"}
LABEL_PREFIXES = ((CTRL, "Ctrl+"), (ALT, "Alt+"), (SUPER, "Super+"))
PAUSE_KEYS = ("p", "escape", "pause")
DEFAULT = {
    "global": {"sound": ("m",), "stats": ("f1",), "redraw": ("ctrl+l",)} | {a: (k,) for a, k in RESERVED.items()},
    "play": {
        "left": ("left", "a", "q", "h"),
        "right": ("right", "d", "l"),
        "stop": ("down", "s", "j"),
        "fire": ("space", "up", "w", "z", "k"),
        "bomb": ("b", "x"),
        "pause": PAUSE_KEYS,
    },
    "countdown": {"pause": PAUSE_KEYS},
    "pause": {"resume": ("p", "escape", "enter", "pause"), "restart": ("r",), "menu": ("q",)},
    "menu": {
        "up": ("up", "w", "z", "k"),
        "down": ("down", "s", "j"),
        "left": ("left", "a", "q", "h"),
        "right": ("right", "d", "l"),
        "select": ("enter", "space"),
        "back": ("escape",),
    },
    "pages": {"page": ("left", "right", "space", "a", "d", "q", "h", "l"), "back": ("enter", "escape", "backspace")},
    "handoff": {"start": ("enter", "space"), "pause": ("escape", "p")},
    "results": {"again": ("enter",), "menu": ("q", "escape")},
    "replay": {"pause": ("p", "space", "escape")},
    "replay_pause": {"resume": ("p", "space", "escape", "enter"), "exit": ("q",)},
    "replay_results": {"exit": ("q", "escape", "enter")},
}
PRESETS = {
    "default": {},
    "arrows": {
        "play": {"left": ("left",), "right": ("right",), "stop": ("down",), "fire": ("space", "up"), "bomb": ("b",)},
        "menu": {"up": ("up",), "down": ("down",), "left": ("left",), "right": ("right",)},
        "pages": {"page": ("left", "right", "space")},
    },
    "vim": {
        "play": {"left": ("h",), "right": ("l",), "stop": ("j",), "fire": ("k", "space")},
        "menu": {"up": ("k", "up"), "down": ("j", "down"), "left": ("h", "left"), "right": ("l", "right")},
        "pages": {"page": ("h", "l", "left", "right", "space")},
    },
    "wasd-physical": {  # without kitty flag 4 every @x falls back to the logical x
        "play": {"left": ("@a", "left"), "right": ("@d", "right"), "stop": ("@s", "down")}
        | {"fire": ("@w", "space", "up"), "bomb": ("@b", "@x")},
    },
}


class KeymapError(ValueError):
    pass


def parse_spec(spec: str) -> tuple[int, str, bool]:
    """'ctrl+alt+x' -> (mods, name, physical)."""
    if not isinstance(spec, str):
        raise KeymapError(f"Key spec must be a string: {spec!r}")
    mods, rest = 0, spec
    while prefix := next((p for p in PREFIXES if rest.lower().startswith(p) and len(rest) > len(p)), ""):
        mods |= PREFIXES[prefix]
        rest = rest[len(prefix) :]
    physical = len(rest) > 1 and rest.startswith("@")
    name = rest[1:] if physical else rest
    name = name if len(name) == 1 and not name.isupper() else name.lower()
    if not is_key_name(name):
        raise KeymapError(f"Unknown key in {spec!r}: {name!r}")
    return mods, name, physical


RESERVED_BY = {parse_spec(spec)[:2]: action for action, spec in RESERVED.items()}


def label(spec: str) -> str:
    """A key spec as hints show it: 'a' -> 'A', 'space' -> 'Space', '@w' -> 'W', 'ctrl+x' -> 'Ctrl+X'."""
    mods, name, _ = parse_spec(spec)
    plain = len(name) == 1 or name[0] == "f" and name[1:].isdigit()
    return "".join(text for bit, text in LABEL_PREFIXES if mods & bit) + (
        SHORT.get(name) or (name.upper() if plain else name.capitalize())
    )


def printable(spec: str) -> bool:
    """Whether a key spec types a character with no Ctrl, Alt or Super: a letter, digit or sign, or Space (physical
    bindings included: on the initials card those keys are typed, spec 6.2)."""
    try:
        mods, name, _ = parse_spec(spec)
    except KeymapError:
        return False
    return not mods & (CTRL | ALT | SUPER) and (len(name) == 1 or name == "space")


def _mods(key: Key) -> int:
    return key.mods & ~(SHIFT | LOCKS)


class Keymap:
    """Bindings per context; lookup() turns a Key into (action, kind). preset() and load() validate."""

    def __init__(self, contexts: Mapping[str, Mapping[str, tuple[str, ...]]]):
        self.contexts = {
            ctx: {action: tuple(keys) for action, keys in actions.items()} for ctx, actions in contexts.items()
        }
        self.logical: dict[str, dict[tuple[int, str], str]] = {}
        self.physical: dict[str, dict[tuple[int, str], str]] = {}
        for ctx, actions in self.contexts.items():
            for action, specs in actions.items():
                if action not in ACTIONS.get(ctx, {}):
                    continue  # reported by validate()
                for spec in specs:
                    try:
                        mods, name, physical = parse_spec(spec)
                    except KeymapError:
                        continue  # reported by validate()
                    table = self.physical if physical else self.logical
                    table.setdefault(ctx, {}).setdefault((mods, name), action)
        self.has_physical = any(self.physical.values())

    @classmethod
    def preset(cls, name: str) -> Keymap:
        if not isinstance(name, str) or name not in PRESETS:
            raise KeymapError(f"Unknown keymap preset: {name} (choose from {', '.join(PRESETS)})")
        contexts = {ctx: dict(actions) for ctx, actions in DEFAULT.items()}
        for ctx, actions in PRESETS[name].items():
            contexts[ctx].update(actions)
        keymap = cls(_mirror_menu(_mirror_play(contexts)))
        keymap.validate()
        return keymap

    @classmethod
    def load(cls, path: Path) -> Keymap:
        """{"preset": "default", "bind": {"play.fire": ["space", "up"]}, "unbind": ["play.left:q"]}"""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KeymapError(f"Cannot read keymap {path}: {exc}") from None
        if not isinstance(data, dict) or set(data) - {"preset", "bind", "unbind"}:
            raise KeymapError("A keymap file is an object with only 'preset', 'bind' and 'unbind'")
        base = cls.preset(data.get("preset", "default"))
        contexts = {ctx: dict(actions) for ctx, actions in base.contexts.items()}
        bind, unbind = data.get("bind", {}), data.get("unbind", [])
        if not isinstance(bind, dict) or not isinstance(unbind, list):
            raise KeymapError("'bind' maps 'context.action' to a list of keys; 'unbind' lists 'context.action:key'")
        for target, keys in bind.items():
            ctx, action = _target(target)
            if ctx == "countdown" and action in PLAY_CONTROLS:
                raise KeymapError(f"{target} follows play.{action}; bind that instead")
            if ctx in MENU_MIRRORS:
                raise KeymapError(f"{target} follows menu.{action}; bind that instead")
            if not isinstance(keys, list):
                raise KeymapError(f"{target} must list its keys")
            contexts.setdefault(ctx, {})[action] = tuple(keys)
        for entry in unbind:
            target, _, spec = entry.partition(":") if isinstance(entry, str) else ("", "", "")
            ctx, action = _target(target)
            if ctx in MENU_MIRRORS:
                raise KeymapError(f"{target} follows menu.{action}; unbind that instead")
            keys = contexts.get(ctx, {}).get(action, ())
            if spec not in keys:
                if ctx == "play" and action in OPTIONAL_PLAY:
                    continue  # an older file that freed F for another action: F is free already
                raise KeymapError(f"Cannot unbind {entry!r}: not bound")
            contexts[ctx][action] = tuple(key for key in keys if key != spec)
        keymap = cls(_mirror_menu(_mirror_play(contexts)))
        keymap.validate()
        return keymap

    def lookup(self, context: str, key: Key) -> tuple[str, str] | None:
        """(action, kind): global first, then the context's logical binding, then a physical one."""
        logical = (_mods(key), key.name)
        physical = (_mods(key), key.base or key.name)
        for table, probe in ((self.logical, logical), (self.physical, physical)):
            for ctx in ("global", context):
                action = table.get(ctx, {}).get(probe)
                if action is not None:
                    return action, ACTIONS[ctx][action]
        return None

    def keys_for(self, context: str, action: str) -> tuple[str, ...]:
        return self.contexts.get(context, {}).get(action, ())

    def validate(self) -> None:
        """Raise KeymapError for the first rule this keymap breaks."""
        seen: dict[str, dict[tuple[int, str, bool], str]] = {}
        for ctx, actions in self.contexts.items():
            if ctx not in ACTIONS:
                raise KeymapError(f"Unknown context: {ctx}")
            table = seen.setdefault(ctx, {})
            for action, specs in actions.items():
                if action not in ACTIONS[ctx]:
                    if ctx in NO_EXIT and action in ABANDON:
                        raise KeymapError(f"{ctx}.{action}: leaving a run is only possible from the pause menu")
                    raise KeymapError(f"Unknown action: {ctx}.{action}")
                for spec in specs:
                    parsed = parse_spec(spec)
                    owner = RESERVED_BY.get(parsed[:2])
                    if owner and (ctx, action) != ("global", owner):
                        raise KeymapError(f"{spec} is reserved for {RESERVED[owner]} (global.{owner})")
                    if parsed in table:
                        raise KeymapError(f"{spec} is bound twice in {ctx}: {table[parsed]} and {action}")
                    table[parsed] = action
        for action, spec in RESERVED.items():
            if self.keys_for("global", action) != (spec,):
                raise KeymapError(f"global.{action} must stay bound to {spec}")
        shared = seen.get("global", {})
        for ctx, table in seen.items():
            for parsed, action in table.items():
                if ctx != "global" and parsed in shared:
                    raise KeymapError(f"{ctx}.{action} reuses a key of global.{shared[parsed]}")
        for ctx, ways in WAYS_OUT.items():
            if not any(self.keys_for(ctx, action) for action in ways):
                raise KeymapError(f"{ctx} needs a key for one of: {', '.join(ways)}")
        for action in ACTIONS["play"]:
            if action not in OPTIONAL_PLAY and not self.keys_for("play", action):
                raise KeymapError(f"play.{action} needs at least one key")


def _target(text: str) -> tuple[str, str]:
    ctx, dot, action = text.partition(".") if isinstance(text, str) else ("", "", "")
    if not dot or ctx not in ACTIONS or not action:
        raise KeymapError(f"Unknown binding target: {text!r} (use context.action)")
    return ctx, action


def _mirror_play(contexts: dict) -> dict:
    """The countdown pre-arms the play controls with exactly the play keys."""
    play = contexts.get("play", {})
    contexts["countdown"] = {**contexts.get("countdown", {}), **{a: play[a] for a in PLAY_CONTROLS if a in play}}
    return contexts


def _mirror_menu(contexts: dict) -> dict:
    """The settings card takes exactly the menu's keys (Phase 5 spec 10), and the initials card the menu's keys but
    the printable ones, which it types, plus erase on Backspace; so a keymap file that rebinds the menu moves them
    too. Backspace is left out where the global context or the menu already binds it, and Enter and Escape stand in
    for a select and a back that only printable keys had, so every file valid before stays valid."""
    menu = contexts.get("menu", {})
    contexts["settings"] = dict(menu)
    initials = {action: tuple(spec for spec in keys if not printable(spec)) for action, keys in menu.items()}
    used = {_parsed(spec) for ctx in ("global",) for keys in contexts.get(ctx, {}).values() for spec in keys}
    used |= {_parsed(spec) for keys in initials.values() for spec in keys}
    for action, spare in (("select", "enter"), ("back", "escape")):
        if action in menu and not initials.get(action) and _parsed(spare) not in used:
            initials[action] = (spare,)
            used.add(_parsed(spare))
    erase = tuple(spec for spec in ERASE if _parsed(spec) not in used)
    contexts["initials"] = initials | ({"erase": erase} if erase else {})
    return contexts


def _parsed(spec: str):
    """A spec's (mods, name, physical), or the spec itself when it does not parse (validate() reports it)."""
    try:
        return parse_spec(spec)
    except KeymapError:
        return spec
