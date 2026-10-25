"""Admission of bound keys by action kind: one-shot keys ignore auto-repeat, and held keys never leak into a new
scene."""

from __future__ import annotations

from .keys import PRESS, RELEASE, Key
from .repeat import RepeatModel

ONE_SHOT = ("command", "pulse")
KINDS = ONE_SHOT + ("navigation", "control")


class KeyGate:
    """certain: the terminal reports press/repeat/release (kitty); otherwise RepeatModel classifies events."""

    def __init__(self, repeat: RepeatModel, *, certain: bool):
        self.repeat = repeat
        self.set_certain(certain)

    def set_certain(self, certain: bool) -> None:
        self.certain = certain
        self.down: set[str] = set()  # kitty: keys pressed and not yet released
        self.last: dict[str, float] = {}  # legacy: each key's latest event
        self.suppressed: set[str] = set()

    def admit(self, key: Key, kind: str | None, now: float) -> tuple[bool, bool]:
        """(admitted, edge) for a key bound to an action of this kind. kind None (a key unbound in this context)
        is never admitted, but it is tracked: it can be held into the next scene, and it ends another key's repeat
        run, as the OS repeats only the latest key."""
        if kind is not None and kind not in KINDS:
            raise ValueError(f"Unknown action kind: {kind}")
        if self.certain:
            return self._kitty(key, kind)
        return self._legacy(key, kind, now)

    def scene_changed(self, now: float) -> None:
        """Suppress every key that may still be held until its release (kitty), or until it has been quiet or comes
        as a press that cannot be its first OS repeat (a re-tap after its repeats, or after another key)."""
        if self.certain:
            self.suppressed = set(self.down)
        else:
            guard = self.repeat.guard
            self.suppressed = {name for name, last in self.last.items() if now - last < guard}

    def _kitty(self, key: Key, kind: str) -> tuple[bool, bool]:
        if key.kind == RELEASE:
            self.down.discard(key.name)
        else:
            self.down.add(key.name)
        if key.name in self.suppressed:
            if key.kind != PRESS:  # still the hold from before the scene change, or its release
                if key.kind == RELEASE:
                    self.suppressed.discard(key.name)
                return False, False
            self.suppressed.discard(key.name)  # a fresh press: the release was lost
        if kind is None or kind in ONE_SHOT and key.kind != PRESS or kind == "navigation" and key.kind == RELEASE:
            return False, False
        return True, key.kind == PRESS

    def _legacy(self, key: Key, kind: str, now: float) -> tuple[bool, bool]:
        edge, safe = self.repeat.observe(key.name, now)
        previous, self.last[key.name] = self.last.get(key.name, now), now
        if key.name in self.suppressed:
            if now - previous < self.repeat.guard and not self.repeat.fresh:
                return False, False
            self.suppressed.discard(key.name)
        if kind is None or kind in ONE_SHOT and not safe:
            return False, False
        return True, edge
