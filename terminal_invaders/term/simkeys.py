"""Simulated keyboards for tests and the balance harness: physical holds -> OS key events -> terminal bytes.

Legacy terminals send a press and OS auto-repeats (indistinguishable from presses) and never a release; kitty
sends press, repeat and release events. Link adds SSH delivery: delay, jitter and keystroke slotting.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeVar

from .keys import ALT, CTRL, PRESS, RELEASE, REPEAT, SHIFT, Key

T = TypeVar("T")
CURSOR = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F"}
SS3 = {"f1": "P", "f2": "Q", "f3": "R", "f4": "S"}  # legacy F1-F4; with modifiers they become CSI 1 ; m P..S
TILDE = {"insert": 2, "delete": 3, "page_up": 5, "page_down": 6}
TILDE |= dict(zip((f"f{n}" for n in range(5, 13)), (15, 17, 18, 19, 20, 21, 23, 24)))
KITTY_FINALS = CURSOR | {"f1": "P", "f2": "Q", "f4": "S"}
KITTY_TILDE = TILDE | {"f3": 13}
TEXT = {"enter": "\r", "tab": "\t", "backspace": "\x7f", "escape": "\x1b", "space": " "}
CODES = {"tab": 9, "enter": 13, "escape": 27, "backspace": 127, "space": 32, "pause": 57362}
CODES |= {f"f{13 + n}": 57376 + n for n in range(23)}
EVENTS = {PRESS: 1, REPEAT: 2, RELEASE: 3}


@dataclass(frozen=True, slots=True)
class RepeatProfile:
    name: str
    delay: float  # OS initial repeat delay
    interval: float


PROFILES = {
    profile.name: profile
    for profile in (
        RepeatProfile("macos-default", 0.525, 0.090),
        RepeatProfile("macos-fast", 0.225, 0.030),
        RepeatProfile("gnome", 0.500, 0.030),
        RepeatProfile("kde", 0.600, 0.040),
        RepeatProfile("xorg", 0.660, 0.040),
    )
}


@dataclass(frozen=True, slots=True)
class Link:
    """SSH keystroke delivery: one-way delay, uniform jitter, then ObscureKeystrokeTiming slots; order is kept."""

    slot: float = 0.020
    jitter: float = 0.030
    rtt: float = 0.0

    def deliver(self, stream: Iterable[tuple[float, T]], rng) -> list[tuple[float, T]]:
        """rng: anything with random() -> [0, 1), such as random.Random or terminal_invaders.rng.Rng."""
        out: list[tuple[float, T]] = []
        last = -math.inf
        for sent, item in stream:
            arrival = sent + self.rtt / 2 + rng.random() * self.jitter
            if self.slot:
                arrival = math.ceil(arrival / self.slot - 1e-9) * self.slot
            last = max(last, arrival)
            out.append((last, item))
        return out


def key_events(holds: Iterable[tuple[str, float, float]], profile: RepeatProfile, *, kitty: bool) -> list[Key]:
    """Key events for physical holds (name, down, up). Only the most recently pressed key auto-repeats, and a
    newer press ends an older key's repeat for good."""
    ordered = sorted(holds, key=lambda hold: hold[1])
    events = []
    for index, (name, down, up) in enumerate(ordered):
        events.append(Key(name, kind=PRESS, certain=kitty, time=down))
        stop = min(up, ordered[index + 1][1]) if index + 1 < len(ordered) else up
        count = 0
        while (at := down + profile.delay + count * profile.interval) < stop:
            events.append(Key(name, kind=REPEAT if kitty else PRESS, certain=kitty, time=at))
            count += 1
        if kitty:
            events.append(Key(name, kind=RELEASE, certain=True, time=up))
    return sorted(events, key=lambda key: key.time)


def encode(key: Key, *, kitty: bool) -> bytes:
    """The bytes a terminal sends for this key event (b"" for a legacy release)."""
    return (_kitty(key) if kitty else _legacy(key)).encode()


def stream(
    holds: Iterable[tuple[str, float, float]],
    profile: RepeatProfile,
    *,
    kitty: bool,
    link: Link | None = None,
    rng=None,
):
    """Timed terminal bytes for physical holds, delivered over link when given."""
    sent = [(key.time, encode(key, kitty=kitty)) for key in key_events(holds, profile, kitty=kitty)]
    sent = [(at, data) for at, data in sent if data]
    return link.deliver(sent, rng) if link else sent


def _legacy(key: Key) -> str:
    if key.kind == RELEASE:
        return ""
    name, mods = key.name, key.mods
    final = CURSOR.get(name) or SS3.get(name)
    if final:
        return f"\x1b[1;{mods + 1}{final}" if mods else ("\x1b[" if name in CURSOR else "\x1bO") + final
    if name in TILDE:
        return f"\x1b[{TILDE[name]};{mods + 1}~" if mods else f"\x1b[{TILDE[name]}~"
    if name == "tab" and mods == SHIFT:
        return "\x1b[Z"
    if name not in TEXT and len(name) != 1:
        raise ValueError(f"{name} has no legacy encoding")
    text = TEXT.get(name, name)
    if mods & CTRL and "a" <= name <= "z":
        text = chr(ord(name) & 0x1F)
    elif mods & SHIFT:
        text = text.upper()
    return "\x1b" + text if mods & ALT else text


def _kitty(key: Key) -> str:
    """CSI code[::base] [; mods[:event]] u, or the legacy-shaped cursor and tilde forms."""
    event = EVENTS[key.kind]
    suffix = f"{key.mods + 1}" + (f":{event}" if event != 1 else "") if key.mods or event != 1 else ""
    final = KITTY_FINALS.get(key.name)
    if final:
        return f"\x1b[1;{suffix}{final}" if suffix else f"\x1b[{final}"
    tilde = KITTY_TILDE.get(key.name)
    if tilde:
        return f"\x1b[{tilde};{suffix}~" if suffix else f"\x1b[{tilde}~"
    base = f"::{_code(key.base)}" if key.base else ""
    return f"\x1b[{_code(key.name)}{base};{suffix}u" if suffix else f"\x1b[{_code(key.name)}{base}u"


def _code(name: str) -> int:
    return CODES[name] if name in CODES else ord(name)
