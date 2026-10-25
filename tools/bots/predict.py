"""Prediction (spec §18): every noticed threat moved forward over the planner's horizon, as forbidden x-intervals
per future step, computed once per decision.

Straight movers (every shot kind, a cluster until it splits, a dive after launch) take their track's least-squares
slope; a young track (span < YOUNG) is a cone of slopes +-ROUNDING / span, and a shot seen once is the cone of its
kind's speeds and |vx| bound in this wave. A box from the tables grows by m0 + m1 x t for a prediction t ticks
ahead. A wobbling diver dives by the table's rule at the ship's x at launch, which depends on the plan, so it is
checked per plan. Beams are forbidden from their mark plus the telegraph for as long as they can be live.
"""

from functools import cache
from math import ceil

from . import know, params
from .track import Perception

SHOTS = know.SHOTS


class Threats:
    """Forbidden x-intervals per future step (1 = the step the bot's next actions make)."""

    __slots__ = ("forb", "horizon", "immune", "pending", "ticks", "beams")

    def __init__(self, horizon: int, immune: int):
        self.horizon, self.immune = horizon, immune
        self.forb = {}
        self.ticks = ()
        self.pending = []
        self.beams = 0

    def add(self, k: int, lo: float, hi: float) -> None:
        if k <= self.immune or k > self.horizon:
            return
        entry = self.forb.get(k)
        if entry is None:
            self.forb[k] = [(lo, hi)]
        else:
            entry.append((lo, hi))

    def finish(self) -> "Threats":
        self.ticks = tuple(sorted(self.forb))
        return self

    def empty(self) -> bool:
        return not self.ticks and not self.pending


class Dive:
    """A wobbling diver or mite: it launches at step k_launch toward wherever the plan has the ship then."""

    __slots__ = ("k_launch", "x", "y", "vy", "f", "m0", "m1", "age")

    def __init__(self, k_launch, x, y, vy, f, margin, age):
        self.k_launch, self.x, self.y, self.vy, self.f = k_launch, x, y, vy, f
        self.m0, self.m1 = margin
        self.age = age  # ticks from the sighting to step 0

    def first_hit(self, plan, x0, speed, before, immune):
        from .plan import position

        kl = self.k_launch
        vy = self.vy * self.f
        if vy <= 0:
            return None
        reach = know.RAM_Y + self.m0 + self.m1 * (self.age + before)
        first = kl + max(1, int((know.SHIP_Y - reach - self.y) / vy))
        last = kl + int((know.SHIP_Y + reach - self.y) / vy) + 1
        first, last = max(first, immune + 1), min(last, before - 1)
        if first > last:
            return None
        vx = know.dive_vx(position(plan, x0, speed, kl), self.x, self.y, self.vy) * self.f
        for k in range(first, last + 1):
            margin = self.m0 + self.m1 * (self.age + k)
            dy = self.y + vy * (k - kl) - know.SHIP_Y
            if -know.RAM_Y - margin < dy < know.RAM_Y + margin:
                if abs(self.x + vx * (k - kl) - position(plan, x0, speed, k)) < know.RAM_X + margin:
                    return k
        return None


def blocked(cells, x, y, vxl, vxh, vyl, vyh, x_spread, sx, sy, margin) -> bool:
    """The shot meets a standing shelter cell whatever its slope inside the cone: then it never reaches the ship."""
    cell = know.CELL
    left, right = x - x_spread, x + x_spread
    for row in know.SHELTER_ROWS:
        top, bottom = row - cell - sy, row + cell + sy
        if y >= bottom:
            continue
        early = (top - y) / vyh
        early = early if early > 0.0 else 0.0  # max(0.0, early)
        late = (bottom - y) / vyl
        a, b = vxl * early, vxl * late
        lo = left + (b if b < a else a) - margin  # min(a, b)
        a, b = vxh * early, vxh * late
        hi = right + (b if b > a else a) + margin  # max(a, b)
        cx = round((lo + hi) / 2)
        if (cx, row) in cells and cx - cell - sx <= lo and hi <= cx + cell + sx:
            return True
    return False


def sweep(threats, x, y, vxl, vxh, vyl, vyh, x_spread, base, f, fend, age, box, margin, cells, ram=False):
    """Forbidden intervals of a straight mover seen at (x, y), `base` enemy ticks before step 0, moving at a slope
    in [vxl, vxh] x [vyl, vyh] per enemy tick; the enemies' clock runs at f a tick (at 1 after step fend, when SLOW
    may have ended). x_spread widens its x (a split's offset). age: ticks from the sighting to step 0."""
    sx, sy = box
    m0, m1 = margin
    horizon = threats.horizon
    if vyh <= 0:
        return
    if cells and not ram and blocked(cells, x, y, vxl, vxh, vyl, vyh, x_spread, sx, sy, m0 + m1 * (age + horizon)):
        return
    reach_x = (know.RAM_X if ram else know.BOX_X + sx) + m0
    reach_y = (know.RAM_Y if ram else know.BOX_Y + sy) + m0
    # Only the steps it can be in the ship's band: from its fastest fall to its slowest.
    widest = m1 * (age + horizon)
    fast = f if fend >= horizon else 1.0
    start = ceil(((know.SHIP_Y - reach_y - widest - y) / vyh - base) / max(fast, 1e-9)) - 1
    stop = horizon if f <= 0 else int(((know.SHIP_Y + reach_y + widest - y) / max(vyl, 1e-9) - base) / f) + 2
    if start > horizon or stop < 1:
        return
    if start < 1:
        start = 1
    if stop > horizon:
        stop = horizon
    # The bots' hottest loop: the tables' constants are read once and Threats.add is inlined (k <= stop <= horizon
    # already).
    top, bottom = know.SHIP_Y + reach_y, know.SHIP_Y - reach_y  # the ship's band, before the margin
    exit_y = know.DIVE_EXIT
    forb, immune = threats.forb, threats.immune
    left, right = x - x_spread, x + x_spread
    last_y = last_lo = last_hi = None  # the step before's y_lo, x_lo and x_hi
    for k in range(start - 1, stop + 1):
        lo_t = base + f * k
        m = m1 * (age + k)
        y_lo = y + vyl * lo_t
        if k <= fend:  # one time for both ends
            y_hi, x_lo, x_hi = y + vyh * lo_t, left + vxl * lo_t, right + vxh * lo_t
        else:  # SLOW may have ended at step fend: from then on it may have moved at full speed
            hi_t = base + f * fend + (k - fend)
            y_hi = y + vyh * hi_t
            a, b = vxl * lo_t, vxl * hi_t
            x_lo = left + (a if a < b else b)
            a, b = vxh * lo_t, vxh * hi_t
            x_hi = right + (a if a > b else b)
        if last_y is None or ram:
            band_lo, span_lo, span_hi = y_lo, x_lo, x_hi
        else:  # it can be anywhere between where it was a step before and where it is now
            band_lo = last_y
            span_lo = last_lo if last_lo < x_lo else x_lo
            span_hi = last_hi if last_hi > x_hi else x_hi
        last_y, last_lo, last_hi = y_lo, x_lo, x_hi
        if k < 1:
            continue
        if band_lo > top + m:
            return
        if y_hi < bottom - m:
            continue
        if ram and y_lo > exit_y:
            return
        if k > immune:
            interval = (span_lo - reach_x - m, span_hi + reach_x + m)
            entry = forb.get(k)
            if entry is None:
                forb[k] = [interval]
            else:
                entry.append(interval)


def margin(kind: str, young: bool) -> tuple[float, float]:
    """(m0, m1) for a kind's prediction. A young track's place is known only to half a grid step (one or a few
    rounded sightings), so its m0 grows by that; a longer track's fitted place is what the drift test measured."""
    m0, m1 = params.MARGINS[kind]
    return (m0 + params.ROUNDING / 2, m1) if young else (m0, m1)


@cache
def limits(kind: str, wave: int, profile: str) -> tuple[float, float, float | None]:
    """A kind's |vx| bound and its fastest fall (x 1.001) in this wave, from the tables; and a dive's slowest fall
    (x 0.999). None for a shot: its slowest fall depends on where it was first seen (know.slowest_vy)."""
    if kind in SHOTS:
        return know.vx_bounds(wave, profile).get(kind, 0.12), know.nominal_vy(kind, wave, profile) * 1.001, None
    vy = know.dive_vy(kind, profile)  # a dive falls at the table's speed
    return know.DIVE.vx_max, vy * 1.001, vy * 0.999


def cone(track, kind, wave, profile, first_y):
    """(x, y, vxl, vxh, vyl, vyh) at the track's last sighting, per enemy tick."""
    x, y, vx, vy, span = track.fit()
    prior = track.prior
    bound, top, low = limits(kind, wave, profile)
    if low is None:
        low = know.slowest_vy(kind, wave, profile, first_y) * 0.999
    if prior is not None and span < params.YOUNG:
        pvx, pvy, pspan = prior
        if kind not in SHOTS:  # a dive: the rule is exact
            return x, y, pvx, pvx, pvy, pvy
        # a split pellet: its vx is the split's; its vy the cluster's, from whichever track spans longer
        if vy is not None and span >= pspan:
            pvy = vy
        width = params.ROUNDING / max(pspan, span, 1.0)
        return x, y, pvx, pvx, max(pvy - width, 0.005), pvy + width
    if vy is None:
        return x, y, -bound, bound, low, top
    width = params.ROUNDING / span if span < params.YOUNG else 0.0
    vxl, vxh, vyl, vyh = vx - width, vx + width, vy - width, vy + width
    # (Clamps as comparisons: the same values as max() and min(), without their calls' cost.)
    if abs(vx) <= bound + 0.02:  # the table's bound holds: keep the cone inside it
        if vxl < -bound:
            vxl = -bound
        if vxh > bound:
            vxh = bound
        if vxl > vxh:
            vxl = vxh = max(-bound, min(bound, vx))
    if top is not None and low - 0.02 <= vy <= top + 0.02:
        if vyl < low:
            vyl = low
        if vyh > top:
            vyh = top
        if vyl > vyh:
            vyl = vyh = max(low, min(top, vy))
    return x, y, vxl, vxh, 0.005 if vyl < 0.005 else vyl, vyh


def build(sense: Perception, horizon_ticks: int, immune: int, cells) -> Threats:
    """Every noticed threat's forbidden intervals for the coming steps."""
    now, seen = sense.now, sense.seen
    tick = now.tick
    wave, profile = seen.wave, now.profile
    w_now = sense.clock_at(tick)
    slow = know.timed(now.ship, "slow")
    f = know.SLOW if slow else 1.0
    fend = 60 * (slow - 1) if slow else 10**6  # SLOW is certain to last this many steps
    marked = (sense.marks and any(mark[2] == "beam" for mark in sense.marks.values())) or (
        seen.hazards and any(hazard.kind == "beam" for hazard in seen.hazards)
    )
    horizon = params.BEAM_HORIZON if marked else params.HORIZON
    threats = Threats(min(horizon, horizon_ticks), immune)
    for track in sense.shots.values():
        kind = track.kind
        spec = SHOTS.get(kind)
        if spec is None:
            continue
        x, y, vxl, vxh, vyl, vyh = cone(track, kind, wave, profile, track.ya)
        base = w_now - track.w1
        age = tick - track.t1
        young = track.w1 - track.w0 < params.YOUNG
        if kind == "cluster":
            split_y = spec.split_y
            if y >= split_y or vyh <= 0:  # split already, or not falling (sweep() skips those too)
                continue
            early, late = (split_y - y) / vyh, (split_y - y) / vyl
            for svx in spec.split:
                # a pellet x(t) = x + vx t + svx (t - t_split): a straight mover with its offset's spread
                mid = (early + late) / 2
                spread = abs(svx) * (late - early) / 2
                sweep(
                    threats, x - svx * mid, y, vxl + svx, vxh + svx, vyl, vyh, spread, base, f, fend, age,
                    SHOTS["pellet"].box, margin("cluster", young), cells,
                )  # fmt: skip
            continue
        sweep(threats, x, y, vxl, vxh, vyl, vyh, 0.0, base, f, fend, age, spec.box, margin(kind, young), cells)
    for track in sense.dives.values():
        x, y, vxl, vxh, vyl, vyh = cone(track, track.kind, wave, profile, track.y1)
        grown = margin("mite" if track.kind == "mite" else "dive", track.w1 - track.w0 < params.YOUNG)
        sweep(
            threats, x, y, vxl, vxh, vyl, vyh, 0.0, w_now - track.w1, f, fend, tick - track.t1, (0.0, 0.0), grown,
            cells, ram=True,
        )  # fmt: skip
    _wobbles(sense, threats, w_now, f, fend)
    _beams(sense, threats, w_now, f)
    return threats.finish()


def _wobbles(sense, threats, w_now, f, fend):
    """Wobbling divers and mites (always noticed): a dive by the table's rule, toward the ship's x at launch."""
    now, seen = sense.now, sense.seen
    tick = now.tick
    profile = now.profile
    divers, scanned = know.DIVERS, sense.scanned
    for start, last, x, y, kind in sense.tells.values():
        if last != scanned or kind not in divers:
            continue
        launch = max(start + know.dive_tell(kind), seen.tick + 1)
        vy = know.dive_vy(kind, profile)
        grown = margin("mite" if kind == "mite" else "dive", True)  # from one rounded sighting of the wobble
        if launch <= tick:  # it has launched by now, unseen: the ship's x then is known
            ship_x = sense.ship_x.get(launch, now.ship.x)
            vx = know.dive_vx(ship_x, x, y, vy)
            base = w_now - sense.clock_at(launch)
            sweep(
                threats, x, y, vx, vx, vy, vy, 0.0, base, f, fend, tick - seen.tick, (0.0, 0.0), grown, None,
                ram=True,
            )  # fmt: skip
        elif launch - tick < threats.horizon:
            threats.pending.append(Dive(launch - tick, x, y, vy, f, grown, tick - seen.tick))


def _beams(sense, threats, w_now, f):
    """Beams: forbidden from their mark plus the shortest telegraph until the longest can have ended."""
    now, seen = sense.now, sense.seen
    hazards = [hazard for hazard in seen.hazards if hazard.kind == "beam"]
    if not hazards:
        return
    boss = seen.parts[0].boss if seen.parts else ""
    scanner = know.stacks(now.ship, "SCANNER") > 0
    earliest, latest, live = know.beam_timing(boss, now.profile, scanner) if boss else (36, 90, 60)
    m0, _m1 = params.MARGINS["beam"]
    reach = know.BOX_X + m0
    for hazard in hazards:
        mark = sense.marks.get(hazard.id)
        if mark is None:
            continue
        first, lit = mark[0], mark[1]
        if lit is not None:
            start, stop = lit, lit + live + 1
        else:
            start, stop = first + earliest, first + latest + live + 1
        for k in range(1, threats.horizon + 1):
            w = w_now + f * k
            if w < start - 1:
                continue
            if w > stop:
                break
            threats.add(k, hazard.x0 - reach, hazard.x1 + reach)
        threats.beams += 1
