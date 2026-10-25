"""Endless's modifier ladder (spec §12): which modifiers a wave carries, and what they change.

The ladder has no draw: from ENDLESS.first one more modifier stacks every ENDLESS.step waves, in ladder order,
repeating. So a game's stack is the ladder's first n entries, n = len(game.modifiers), and every effect below is a
pure function of n (cached by it). Each effect is clamped by its own cap, and the engine's floors and caps (F8)
still apply after it. Factors are repeated products, never `**` (no libm).
"""

from dataclasses import replace

from .tables import ENDLESS

_STACKS = {}  # n -> {stat: stacks}


def due(wave: int) -> int:
    """How many modifiers wave `wave` of an Endless run carries."""
    if wave < ENDLESS.first:
        return 0
    return 1 + (wave - ENDLESS.first) // ENDLESS.step


def names(count: int) -> list:
    """The stack's names in the order they arrived (a repeated modifier appears once per stack)."""
    return [ENDLESS.ladder[index % len(ENDLESS.ladder)].name for index in range(count)]


def stacks(count: int) -> dict:
    """stat -> how many times its modifier is stacked among the first `count`."""
    found = _STACKS.get(count)
    if found is None:
        found = {}
        for index in range(count):
            stat = ENDLESS.ladder[index % len(ENDLESS.ladder)].stat
            found[stat] = found.get(stat, 0) + 1
        _STACKS[count] = found
    return found


def modifier(stat: str):
    return next(m for m in ENDLESS.ladder if m.stat == stat)


def power(factor: float, times: int) -> float:
    out = 1.0
    for _ in range(times):
        out *= factor
    return out


def director_row(row, count):
    """QUICK RELOAD (reload x0.92 a stack, never below its floor), EXTRA SLOT (+1 a stack, at most 8) and SHARP EYES
    (reaction +0.02 a stack, at most 0.15) applied to a director row. None of them makes the row easier."""
    stacked = stacks(count)
    changes = {}
    n = stacked.get("reload", 0)
    if n:
        rule = modifier("reload")
        changes["reload"] = min(row.reload, max(rule.cap, row.reload * power(rule.mul, n)))
    n = stacked.get("slots", 0)
    if n:
        rule = modifier("slots")
        changes["slots"] = max(row.slots, min(int(rule.cap), row.slots + int(rule.add) * n))
    n = stacked.get("reaction", 0)
    if n:
        rule = modifier("reaction")
        changes["reaction"] = max(row.reaction, min(rule.cap, row.reaction + rule.add * n))
    return replace(row, **changes) if changes else row


def veteran(share: float, count: int) -> float:
    """ARMOURED: +10% plating a stack, at most 50%."""
    n = stacks(count).get("veteran", 0)
    if not n:
        return share
    rule = modifier("veteran")
    return max(share, min(rule.cap, share + rule.add * n))


def divers(most: int, every: float, count: int) -> tuple:
    """DIVE STORM: +1 diver at once a stack (at most 5) and the dive interval x0.9 a stack."""
    n = stacks(count).get("divers", 0)
    if not n:
        return most, every
    rule = modifier("divers")
    return max(most, min(int(rule.cap), most + int(rule.add) * n)), every * power(rule.mul, n)


def speed(count: int) -> float:
    """FAST SHOTS: enemy shot speed x1.05 a stack, at most x1.25 (the 0.28 cap and the safe slowing still apply)."""
    n = stacks(count).get("speed", 0)
    if not n:
        return 1.0
    rule = modifier("speed")
    return min(rule.cap, power(rule.mul, n))


def flights(count: int) -> tuple:
    """REINFORCED: (extra flights a regular wave gets, ticks between them): its last flight repeated, at most 3."""
    n = stacks(count).get("flights", 0)
    rule = modifier("flights")
    return min(int(rule.cap), int(rule.add) * n), rule.ticks


def fuse(add: int, most: int, count: int) -> tuple:
    """SHORT FUSE: a timed pickup lasts (and stacks to) at most 360 ticks."""
    if not stacks(count).get("fuse", 0):
        return add, most
    rule = modifier("fuse")
    return min(add, rule.ticks), min(most, int(rule.cap))
