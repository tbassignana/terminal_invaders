"""The plan library, the space-time check and latched taps (spec §18 "Planner").

A plan is (d1, k1, d2, k2): move d1 (-1, 0 or 1) for k1 ticks, then d2 for k2 ticks, then stand. The library is
stand; one leg of HOLDS ticks either way (18); and two legs, FIRST_LEGS one way then SECOND_LEGS the other (24): 43
plans. Positions are exact: the ship moves `speed` a tick and the field clamps it, as the engine does.

A plan is what the bot intends. Held keys (direct play, kitty) make it as it is. Under latched controls each plan is
the tap sequence that makes it under IntentDriver's TWICE and SETTLE (input.py): a tap starts a direction, a tap of
S stops it, a reversal is two taps TWICE apart (the first stops the ship), and no tap comes sooner than SETTLE
after the last one ended. `Latched` keeps the bot's own taps and turns a plan into the motion those taps make. A
motion is a flat tuple of legs (d, n, d, n, ...), then standing; everything here that takes a plan takes a motion
too (a plan is a two-leg motion). Over ssh a tap lands its usual `lag` steps late give or take the link's jitter
(`window`), so a motion is safe only if it is safe wherever its taps may land (`band`, `banded_hit`).
"""

from functools import cache, lru_cache
from math import floor, inf

from terminal_invaders.input import TICK_RATE, IntentDriver

from . import know, params

X_MIN, X_MAX = know.X_MIN, know.X_MAX
OPEN = 10**4  # a motion's last leg with no stop keyed: it runs on past any horizon
TWICE, SETTLE = IntentDriver.TWICE, IntentDriver.SETTLE


def library() -> tuple[tuple[int, int, int, int], ...]:
    plans = [(0, 0, 0, 0)]
    plans += [(d, k, 0, 0) for d in (-1, 1) for k in params.HOLDS]
    plans += [(d, k, -d, m) for d in (-1, 1) for k in params.FIRST_LEGS for m in params.SECOND_LEGS]
    return tuple(plans)


PLANS = library()
STAND = PLANS[0]


def length(plan) -> int:
    return sum(plan[1::2])


def position(plan, x0: float, speed: float, k: int) -> float:
    """The ship's x after k steps of the plan (or motion)."""
    if len(plan) == 4:
        d1, k1, d2, k2 = plan
        if k <= k1:
            x = x0 + speed * d1 * k
            return X_MIN if x < X_MIN else X_MAX if x > X_MAX else x
        x = x0 + speed * d1 * k1
        x = X_MIN if x < X_MIN else X_MAX if x > X_MAX else x
        x += speed * d2 * (min(k, k1 + k2) - k1)
        return X_MIN if x < X_MIN else X_MAX if x > X_MAX else x
    x, done = x0, 0
    for index in range(0, len(plan), 2):
        d, n = plan[index], plan[index + 1]
        if k <= done + n:
            x += speed * d * (k - done)
            return X_MIN if x < X_MIN else X_MAX if x > X_MAX else x
        x += speed * d * n
        x = X_MIN if x < X_MIN else X_MAX if x > X_MAX else x
        done += n
    return x


def remainder(plan, done: int):
    """What is left of a plan after `done` of its steps, as a plan (None once it is over)."""
    d1, k1, d2, k2 = plan
    if done < k1:
        return (d1, k1 - done, d2, k2)
    if done < k1 + k2:
        return (d2, k1 + k2 - done, 0, 0)
    return None


def direction(plan, step: int) -> int:
    """The plan's move at its step-th tick (1-based)."""
    d1, k1, d2, k2 = plan
    if step <= k1:
        return d1
    if step <= k1 + k2:
        return d2
    return 0


def first_hit(threats, plan, x0: float, speed: float):
    """The first step (1..horizon) a threat overlaps the ship on this plan (or motion), else None."""
    forb = threats.forb
    hit = None
    if len(plan) == 4:
        d1, k1, d2, k2 = plan
        v1, v2 = speed * d1, speed * d2  # each leg's step (speed * d * k is (speed * d) * k)
        e1 = x0 + v1 * k1
        e1 = X_MIN if e1 < X_MIN else X_MAX if e1 > X_MAX else e1
        end = k1 + k2
        for k in threats.ticks:
            if k <= k1:
                x = x0 + v1 * k
            else:
                x = e1 + v2 * ((k if k < end else end) - k1)
            x = X_MIN if x < X_MIN else X_MAX if x > X_MAX else x
            for lo, hi in forb[k]:
                if lo < x < hi:
                    hit = k
                    break
            if hit is not None:
                break
    elif threats.ticks:
        legs = len(plan)
        index, start, x_start = 0, 0, x0  # the leg holding step k, the steps before it, and its starting x
        for k in threats.ticks:
            while index < legs and k > start + plan[index + 1]:
                x_start += speed * plan[index] * plan[index + 1]
                x_start = X_MIN if x_start < X_MIN else X_MAX if x_start > X_MAX else x_start
                start += plan[index + 1]
                index += 2
            x = x_start + speed * plan[index] * (k - start) if index < legs else x_start
            x = X_MIN if x < X_MIN else X_MAX if x > X_MAX else x
            for lo, hi in forb[k]:
                if lo < x < hi:
                    hit = k
                    break
            if hit is not None:
                break
    if threats.pending:
        for dive in threats.pending:
            k = dive.first_hit(plan, x0, speed, hit if hit is not None else threats.horizon + 1, threats.immune)
            if k is not None and (hit is None or k < hit):
                hit = k
    return hit


LENGTHS = tuple(length(plan) for plan in PLANS)


def cost(plan, x0: float, speed: float, dest: float) -> float:
    """How far from `dest` the plan (or motion) ends, plus twice the way its legs first go away from it."""
    x, away = x0, 0.0
    for index in range(0, len(plan), 2):
        d, n = plan[index], plan[index + 1]
        if not d or not n:
            continue
        end = x + speed * d * n
        end = X_MIN if end < X_MIN else X_MAX if end > X_MAX else end
        if d * (dest - x) < 0:
            away += abs(end - x)
        x = end
    return abs(x - dest) + 2.0 * away


def choose(threats, x0: float, speed: float, dest: float, current=None, keys=None, tick=0):
    """(plan, None) for the safe plan that ends nearest `dest`, counting twice any way it first goes away from
    it (ties to the shorter); with none safe, (the plan hit last, its first hit). `current`, what is left of the
    plan being flown, is a candidate too and wins ties. Without the detour cost and `current`, a ship whose
    destination sits under a firing column swaps between plans heading opposite ways and never gets there.

    keys: the bot's `Latched` taps, under latched controls. Each plan is then judged by the motion its taps make
    from `tick` (`Latched.motion`), and that motion comes back as a third item."""
    if keys is not None:
        return _choose_keyed(threats, x0, speed, dest, current, keys, tick)
    costs, best = _ranking(x0, speed, dest)
    best_cost = costs[best]
    if current is not None:
        current_cost = cost(current, x0, speed, dest)
        if current_cost <= best_cost:  # (cost, -1, -1) sorts before every plan of the same cost
            best = -1
    plan = current if best < 0 else PLANS[best]
    if threats.empty():
        return plan, None
    hit = first_hit(threats, plan, x0, speed)
    if hit is None:  # the usual case: the best plan is safe, no need to sort the rest
        return plan, None
    ranked = [(value, LENGTHS[index], index) for index, value in enumerate(costs)]
    if current is not None:
        ranked.append((current_cost, -1, -1))
    ranked.sort()
    best, best_hit = plan, hit
    for _distance, _length, index in ranked[1:]:
        plan = current if index < 0 else PLANS[index]
        hit = first_hit(threats, plan, x0, speed)
        if hit is None:
            return plan, None
        if hit > best_hit:
            best, best_hit = plan, hit
    return best, best_hit


@lru_cache(maxsize=64)
def _ranking(x0: float, speed: float, dest: float) -> tuple[tuple[float, ...], int]:
    """(_costs(), the index of the plan that comes first by (cost, length, index)). It depends on nothing but its
    arguments, so it is kept for the ship standing at its destination, which asks the same thing decision after
    decision (about half of all decisions)."""
    costs = _costs(x0, speed, dest)
    best, best_cost, best_length = 0, costs[0], LENGTHS[0]
    for index in range(1, len(costs)):  # min() of (cost, length, index), without building the tuples
        value = costs[index]
        if value < best_cost or (value == best_cost and LENGTHS[index] < best_length):
            best, best_cost, best_length = index, value, LENGTHS[index]
    return tuple(costs), best


def _costs(x0: float, speed: float, dest: float) -> list[float]:
    """cost() of every plan of PLANS, in order, inline: this runs 43 times a decision. A first leg shared by
    several two-leg plans is worked out once."""
    out = []
    append = out.append
    toward = dest - x0
    for d1, d2, firsts, seconds in _legs(speed):
        away_first = d1 * toward < 0
        for step1 in firsts:
            x = x0 + step1
            x = X_MIN if x < X_MIN else X_MAX if x > X_MAX else x
            away = (x - x0 if x > x0 else x0 - x) if away_first else 0.0
            if seconds is None:
                append((x - dest if x > dest else dest - x) + 2.0 * away)
                continue
            back = d2 * (dest - x) < 0  # the second leg goes away from dest
            for step2 in seconds:
                end = x + step2
                end = X_MIN if end < X_MIN else X_MAX if end > X_MAX else end
                total = away + (end - x if end > x else x - end) if back else away
                append((end - dest if end > dest else dest - end) + 2.0 * total)
    return out


@cache
def _legs(speed: float) -> tuple:
    """PLANS at this ship speed (it takes a few values a run: THRUSTERS), in order, as runs of plans that share
    their directions: (d1, d2, first legs, second legs) with each leg as speed x d x k. A run of one-leg plans
    has their first legs and no second legs (None); a run of two-leg plans shares one first leg."""
    runs = []
    for d1, k1, d2, k2 in PLANS:
        first = speed * d1 * k1
        last = runs[-1] if runs else None
        if not k2:
            if last is not None and last[3] is None and last[:2] == [d1, d2]:
                last[2].append(first)
            else:
                runs.append([d1, d2, [first], None])
        elif last is not None and last[3] is not None and last[:3] == [d1, d2, [first]]:
            last[3].append(speed * d2 * k2)
        else:
            runs.append([d1, d2, [first], [speed * d2 * k2]])
    return tuple(
        (d1, d2, tuple(firsts), None if seconds is None else tuple(seconds)) for d1, d2, firsts, seconds in runs
    )


def _choose_keyed(threats, x0, speed, dest, current, keys, tick):
    """choose() under latched controls: plans ranked and checked by the motion their taps make. Plans whose taps
    make the same motion (a 3-tick move and a 6-tick one both last until the stop can be keyed) are checked once.

    Over a link whose taps land sooner or later than planned (ssh: `keys.spread`), a safe motion must also be
    safe wherever its taps may land (`banded_hit`); the safe motion first in the ranking that is, else the safe
    one whose band is hit last (ties to the ranking). Its hit is None either way: the motion as planned is safe,
    so the bomb and the no-escape log read the same as before."""
    candidates = [(plan, keys.motion(plan, tick), LENGTHS[index]) for index, plan in enumerate(PLANS)]
    if current is not None:
        candidates.append((current, keys.motion(current, tick), -1))
    ranked = sorted(
        (cost(motion, x0, speed, dest), size, index) for index, (_plan, motion, size) in enumerate(candidates)
    )
    if threats.empty():
        plan, motion, _size = candidates[ranked[0][2]]
        return plan, None, motion
    spread = keys.spread
    hits, bands = {}, {}
    best, best_hit = None, 0
    loose, loose_hit = None, 0  # under a spread: the safe motion whose band is hit last
    for _cost, _size, index in ranked:
        plan, motion, _size = candidates[index]
        hit = hits.get(motion, 0)
        if hit == 0:
            hit = hits[motion] = first_hit(threats, motion, x0, speed)
        if hit is None:
            if not spread:
                return plan, None, motion
            band = bands.get(motion, 0)
            if band == 0:
                windows = []
                keys.motion(plan, tick, windows)
                band = bands[motion] = banded_hit(threats, motion, x0, speed, windows, tick)
            if band is None:
                return plan, None, motion
            if loose is None or band > loose_hit:
                loose, loose_hit = (plan, motion), band
            continue
        if best is None or hit > best_hit:
            best, best_hit = (plan, motion), hit
    if loose is not None:
        return loose[0], None, loose[1]
    return best[0], best_hit, best[1]


def window(t: float, lag: int, spread: int) -> tuple[int, int, int]:
    """(planned, earliest, latest): the step a tap pressed at time t lands at as planned (landing()), and the first
    and last it may land at. Over a link (spread > 0) it lands no more than `spread` steps sooner than planned, and
    it arrives less than lag + spread + 1 steps after it is pressed: a tap pressed between two steps (the second of
    a reversal, TWICE after the first) may land spread + 1 steps later than planned. With params.SSH_LAG and
    SSH_SPREAD that is exactly the range of balance.Keyboard's ssh link (tests/test_bots.py measures it)."""
    planned = landing(t, lag)
    if not spread:
        return planned, planned, planned
    return planned, planned - spread, floor(t * TICK_RATE + lag + spread + 1 - 1e-9)


def band(windows, tick: int, k: int, speed: float) -> tuple[float, float]:
    """How far left and right of its planned x the ship may be after step k of a motion from the step at `tick`
    when each tap of `windows` ((planned, change, earliest, latest) from Latched.motion) lands anywhere from its
    earliest step to its latest. A tap landing early moves the ship its new way at the steps before it was due,
    one landing late its old way at the steps after (only steps from `tick` on, up to step k); each tap's worst
    either side adds up."""
    last = tick + k - 1  # the last step taken by step k
    left = right = 0.0
    for planned, change, earliest, latest in windows:
        sooner = (min(last, planned - 1) - max(earliest, tick) + 1) * speed
        later = (min(last, latest - 1) - max(planned, tick) + 1) * speed
        sooner, later = max(0.0, sooner), max(0.0, later)
        if change > 0:
            right += change * sooner
            left += change * later
        else:
            right -= change * later
            left -= change * sooner
    return left, right


def banded_hit(threats, motion, x0: float, speed: float, windows, tick: int):
    """first_hit() for a motion from the step at `tick` whose taps each land anywhere in their `windows`
    (Latched.motion): the first step at which they could put the ship in a forbidden interval (band()), else None.
    Dives are checked on the motion as planned (first_hit)."""
    forb = threats.forb
    legs = len(motion)
    index, start, x_start = 0, 0, x0
    for k in threats.ticks:
        while index < legs and k > start + motion[index + 1]:
            x_start += speed * motion[index] * motion[index + 1]
            x_start = X_MIN if x_start < X_MIN else X_MAX if x_start > X_MAX else x_start
            start += motion[index + 1]
            index += 2
        x = x_start + speed * motion[index] * (k - start) if index < legs else x_start
        x = X_MIN if x < X_MIN else X_MAX if x > X_MAX else x
        left, right = band(windows, tick, k, speed) if windows else (0.0, 0.0)
        left, right = max(X_MIN, x - left), min(X_MAX, x + right)
        for lo, hi in forb[k]:
            if lo < right and left < hi:
                return k
    return None


# --- latched taps (§18: each plan is the tap sequence that makes it under TWICE and SETTLE) -------------------------


def landing(t: float, lag: int) -> int:
    """The step a key pressed at time t reaches the game in (a step at tick T takes the bytes of [T, T + 1) ticks),
    `lag` steps later over a link."""
    return floor(t * TICK_RATE + 1e-9) + lag


class Latched:
    """The bot's own keys under latched controls: IntentDriver's state (the direction it keyed last, and when it may
    key the next), the taps still on their way, and the ship's latched direction as LatchedControls applies them.

    key(tick, direction) is called once a tick with the direction the bot hands the Keyboard for that step; the bot
    never sees the driver, it keys by the same rules itself. lag: the steps a tap takes to reach the game (over
    ssh, about one); spread: how many steps sooner or later than that it may land (the link's jitter, `window`),
    which the planner allows for (`banded_hit`)."""

    __slots__ = ("dir", "ready", "moving", "taps", "settling", "lag", "spread", "held")

    def __init__(self, lag: int = 0, spread: int = 0):
        self.lag, self.spread = lag, spread
        self.dir = 0  # the direction the bot keyed last (IntentDriver.direction)
        self.ready = -inf  # the time (s) it may key the next (IntentDriver.ready)
        self.moving = 0  # the latched direction the ship moves with at the next step
        self.taps = []  # [(the step it lands at, its direction or 0 for S, its earliest step, its latest)], in order
        self.settling = []  # taps landed as planned that may still be on their way: (step, change, earliest, latest)
        self.held = 0  # the direction the last step moved with

    @staticmethod
    def taps_for(old: int, new: int, t: float, lag: int, spread: int = 0) -> tuple[list, float]:
        """IntentDriver._move under latched controls: the taps that take the driver from old to new at time t, as
        (step, direction, earliest, latest) (window()), and the time it may key the next."""
        if new == 0:
            keyed = [(t, 0)]
        elif old == -new:
            keyed = [(t, new), (t + TWICE, new)]
        else:
            keyed = [(t, new)]
        taps = []
        for at, tap in keyed:
            planned, earliest, latest = window(at, lag, spread)
            taps.append((planned, tap, earliest, latest))
        return taps, t + (len(taps) - 1) * TWICE + params.TAP + SETTLE

    @staticmethod
    def apply(moving: int, tap: int) -> int:
        """LatchedControls.feed for a tap that reaches it: S stops; a tap the other way stops; from a stop it moves."""
        if tap == 0 or moving == -tap:
            return 0
        return tap if moving == 0 else moving

    def key(self, tick: int, direction: int) -> None:
        """The bot wants `direction` for the step at `tick`: key it as IntentDriver does, then land what is due."""
        t = tick / TICK_RATE
        if direction != self.dir and t >= self.ready:
            taps, self.ready = self.taps_for(self.dir, direction, t, self.lag, self.spread)
            self.dir = direction
            self.taps += taps
        if self.settling:  # from the next step on, those that cannot land after it any more are settled
            self.settling = [tap for tap in self.settling if tap[3] > tick + 1]
        while self.taps and self.taps[0][0] <= tick:
            step, tap, earliest, latest = self.taps.pop(0)
            moving = self.apply(self.moving, tap)
            if moving != self.moving and latest > tick + 1:
                self.settling.append((step, moving - self.moving, earliest, latest))
            self.moving = moving
        self.held = self.moving

    def clear(self) -> None:
        """The Session's controls.clear() at a card or prompt edge: the latched direction is gone (the driver's is
        not, and taps on their way still arrive)."""
        self.moving = 0
        self.settling = []

    def unlatch(self) -> None:
        """On the card and the prompt the Session unlatches after each step that moved."""
        if self.held:
            self.moving = self.held = 0
            self.settling = []

    def motion(self, plan, tick: int, windows: list | None = None) -> tuple:
        """The legs the ship moves through from the step at `tick` if the bot keys `plan` from it: each change of
        the plan's direction is keyed at the first tick the driver allows, while the plan still wants it. Given a
        list, `windows` gets each tap that changes the ship's direction, as (step, change, earliest, latest), with
        those already landed as planned that may still be on their way (band())."""
        d1, k1, d2, k2 = plan
        pieces = ((1, d1), (k1 + 1, d2), (k1 + k2 + 1, 0)) if k1 else ((1, 0),)
        old, ready, lag, spread = self.dir, self.ready, self.lag, self.spread
        taps = list(self.taps)
        last = len(pieces) - 1
        for index, (start, value) in enumerate(pieces):
            if value == old or (index < last and pieces[index + 1][0] <= start):
                continue
            at = tick + start - 1
            if at / TICK_RATE < ready:
                at = max(at, floor(ready * TICK_RATE) - 1)
                while at / TICK_RATE < ready:
                    at += 1
            if index < last and at >= tick + pieces[index + 1][0] - 1:
                continue  # the plan has moved on before the driver could key this
            more, ready = self.taps_for(old, value, at / TICK_RATE, lag, spread)
            taps += more
            old = value
        if windows is not None:
            windows += self.settling
        legs, moving, now = [], self.moving, tick
        for step, tap, earliest, latest in taps:
            if step > now:
                legs += (moving, step - now)
                now = step
            after = self.apply(moving, tap)
            if windows is not None and after != moving:
                windows.append((step, after - moving, earliest, latest))
            moving = after
        if moving:  # no stop to come: the motion runs on past any horizon
            legs += (moving, OPEN)
        return merge(legs)


def merge(legs) -> tuple:
    """Adjacent legs the same way as one, empty legs and the standing at the end dropped."""
    out = []
    for index in range(0, len(legs), 2):
        d, n = legs[index], legs[index + 1]
        if n <= 0:
            continue
        if out and out[-2] == d:
            out[-1] += n
        else:
            out += (d, n)
    while out and out[-2] == 0:
        del out[-2:]
    return tuple(out)
