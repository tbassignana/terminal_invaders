"""Flights (spec §7): placement, arrival (the band, the push, the wait), the seven movements, dives, mites,
carriers, landing and the shelter crush. Step 5 of the tick (§14.2).

A flight's enemies keep their slot in `home_x`/`home_y`; the flight moves every slot by one delta a tick (CAROUSEL
and ESCORT place them from the flight's origin instead). One pass over the enemies, in id order, applies the
deltas, moves each enemy by its state and gathers each flight's extents, which the next tick's decisions read.
"""

from dataclasses import dataclass, field
from math import ceil, floor

from ..model import Alien
from . import endless
from .collide import cells_near
from .tables import ACTS, ARRIVAL, DIVE, ENEMIES, FORMATIONS, LIMITS, PLAYER, PROFILES, SHAPES, SHELTERS, TELEGRAPHS

X_MIN, X_MAX = LIMITS.field
INF = 1e9
ELLIPSE = SHAPES["ring"].offsets  # the ring's table, in quarter cells
RING = len(ELLIPSE)  # its entries: a ring of n takes every (RING / n)-th


@dataclass(slots=True)
class FlightState:
    """One flight on the field (hashed). Its enemies have `flight == index`; an enemy's slot is `id - base`."""

    index: int
    movement: str  # a FORMATIONS key; a PINCER becomes its `then` once both halves have settled
    base: int
    slots: tuple  # ((ox, oy, col), ...): offsets from (x, y) and the slot's column, in slot order
    x: float  # the shape's centre x ...
    y: float  # ... and its top row (an ESCORT ring: the ring's centre)
    size: int
    escort: bool = False
    direction: int = 1
    clock: float = 0.0  # MARCH: ticks since the last beat; COLUMNS: since the last drop
    bounces: int = 0
    shift: float = 0.0  # CAROUSEL: how far the slots have circulated round the perimeter
    turn: int = 0  # COLUMNS: the column that drops next
    drops: list = field(default_factory=list)  # COLUMNS: rows each column has dropped
    push: float = 0.0  # cells it still has to be pushed down
    since: float = 0.0  # the play clock when it arrived (SWEEP)
    alive: int = 0
    clean: bool = True  # every enemy it lost was shot or bombed (no ram, landing or exit): SWEEP can pay
    formation: int = 0  # from the last pass: enemies in formation (the MARCH beat counts these)
    entering: int = 0  # from the last pass: enemies still entering
    lo: float = 0.0  # from the last pass: slot extents, widened by each enemy's oscillation
    hi: float = 0.0
    top: float = 0.0
    bottom: float = 0.0
    dx: float = 0.0  # this tick's movement of every slot
    dy: float = 0.0
    col: int = -1  # COLUMNS: the column dropping this tick
    drop: float = 0.0
    ring: tuple = ()  # an ESCORT ring: each slot's entry of the ellipse table (its place round the ring)


# --- shapes and placement (§7.2) -------------------------------------------------------------------------------------


def layout(flight) -> tuple:
    """The flight's slots in order: (ox, oy, col) from its centre x and top row (a ring: from its centre)."""
    shape, dims, size = SHAPES[flight.shape], flight.dims, flight.size
    out = []
    if flight.shape in ("rect", "checker", "line"):
        rows, cols = (1, dims[0]) if flight.shape == "line" else dims
        width = shape.spacing * (cols - 1) + (shape.shift if flight.shape == "checker" and rows > 1 else 0)
        for row in range(rows):
            shift = shape.shift if row % 2 else 0
            for col in range(cols):
                out.append((shape.spacing * col + shift - width / 2, float(shape.pitch * row), col))
    elif flight.shape == "vee":
        half = (dims[0] - 1) // 2
        width = shape.spacing * (dims[0] - 1)
        for row in range(half):
            reach = width / 2 - shape.spacing * row
            out += [(-reach, float(row), 0), (reach, float(row), 0)]
        out.append((0.0, float(half), 0))
    elif flight.shape == "diamond":
        widths = (1, 3, 1) if dims[0] == 5 else (1, 3, 5, 3, 1)
        for row, count in enumerate(widths):
            for col in range(count):
                out.append((shape.spacing * col - shape.spacing * (count - 1) / 2, float(row), 0))
    else:  # ring: every (16 / n)-th entry of the ellipse table, in quarter cells
        for where in ring_places(dims[0]):
            dx, dy = ELLIPSE[where]
            out.append((dx / 4, dy / 4, 0))
    if flight.shape in ("vee", "diamond", "ring"):
        out = [(ox, oy, floor((ox + 32) / 4)) for ox, oy, _ in out]
    return tuple(out[:size])


def ring_places(count) -> list:
    """The ellipse-table entries a ring of `count` takes, in slot order."""
    return [slot * RING // count for slot in range(count)]


def ring_at(where) -> tuple:
    """A ring slot's offset from the ring's centre at table position `where` (fractional: between two entries the
    slot moves straight from one to the next)."""
    whole = floor(where)
    share = where - whole
    ax, ay = ELLIPSE[whole % RING]
    bx, by = ELLIPSE[(whole + 1) % RING]
    return (ax + (bx - ax) * share) / 4, (ay + (by - ay) * share) / 4


def height(slots) -> float:
    return max(oy for _ox, oy, _col in slots) - min(oy for _ox, oy, _col in slots)


def _spread(free, count):
    """`count` of the slots in `free`, evenly spread across them."""
    return [free[(2 * pick + 1) * len(free) // (2 * count)] for pick in range(count)]


def kinds_for(flight, slots, profile, act) -> list:
    """Each slot's enemy kind: the mix by ENEMIES[kind].rows (front is the lowest row), scouts elsewhere, then the
    profile's enemy mix (§11)."""
    kinds = ["scout"] * len(slots)
    rows = sorted(dict.fromkeys(oy for _ox, oy, _col in slots))
    middle = (rows[0] + rows[-1]) / 2
    for kind, count in flight.mix:
        where = ENEMIES[kind].rows
        if where == "any":
            groups = [rows]
        elif where == "front":
            groups = [[row] for row in reversed(rows)]
        elif where == "back":
            groups = [[row] for row in rows]
        else:  # mid: the middle rows first, then the front, then the back
            groups = [[row] for row in sorted(rows, key=lambda row: (abs(row - middle), -row))]
        left = count
        for group in groups:
            free = [slot for slot in range(len(slots)) if slots[slot][1] in group and kinds[slot] == "scout"]
            take = min(left, len(free))
            if take:
                for slot in _spread(free, take):
                    kinds[slot] = kind
                left -= take
            if not left:
                break
    if profile.gunner_every:
        seen = 0
        for slot, kind in enumerate(kinds):
            if kind == "gunner":
                seen += 1
                if seen % profile.gunner_every == 0:
                    kinds[slot] = "scout"
    front, back = rows[-1], rows[0]
    for (first, most), kind, row in ((profile.front_gunners, "gunner", front), (profile.back_lancers, "lancer", back)):
        if first and act >= first:
            free = [slot for slot in range(len(slots)) if slots[slot][1] == row and kinds[slot] == "scout"]
            for slot in _spread(free, min(most, len(free))) if free else ():
                kinds[slot] = kind
    return kinds


def veteran_share(game) -> float:
    """The share of enemies plated: the profile's for the wave, plus Endless's ARMOURED."""
    row = game._row()
    share = 0.0
    for first, last, part in PROFILES[game.config.difficulty].veteran:
        if first <= row <= last:
            share = part
            break
    if game.modifiers:
        share = endless.veteran(share, len(game.modifiers))
    return share


def place(game, flight, top, escort=False, ring=None):
    """A flight arrives: its enemies enter from above (a PINCER from the sides) toward their slots. `ring`: an ESCORT
    ring's table entries for its slots (a refill's empty places); a ring flight takes every (16 / n)-th by default."""
    profile = PROFILES[game.config.difficulty]
    if flight.shape == "ring":
        ring = tuple(ring if ring is not None else ring_places(flight.dims[0]))[: flight.size]
        slots = tuple(
            (ELLIPSE[where][0] / 4, ELLIPSE[where][1] / 4, floor(ELLIPSE[where][0] / 16 + 8)) for where in ring
        )
    else:
        ring = ()
        slots = layout(flight)
    kinds = kinds_for(flight, slots, profile, game.act)
    movement = flight.movement
    boss = game.boss
    if movement == "ESCORT" and boss is not None:
        x0, y0 = boss.x, boss.y
    else:
        x0, y0 = ARRIVAL.centre, top
    index = len(game._flights)
    state = FlightState(index, movement, game._serial + 1, slots, x0, y0, len(slots), escort=escort, ring=ring)
    if ring and movement == "ESCORT":
        slots = tuple((*ring_at(where + _turned(game)), col) for where, (_ox, _oy, col) in zip(ring, slots))
    state.direction = 1 if index % 2 == 0 else -1
    state.since = game._play
    state.alive = state.entering = len(slots)
    if movement == "COLUMNS":
        state.drops = [0.0] * (max(col for _ox, _oy, col in slots) + 1)
    cargo = -1
    if not escort and game._next_flight == game._wave_row().cargo:
        back = [slot for slot in range(len(slots)) if slots[slot][1] == slots[0][1]]
        cargo = back[game.rng_drop.randint(0, len(back) - 1)]
    share = veteran_share(game)
    counted = sum(other.size for other in game._flights)
    pincer = FORMATIONS["PINCER"].entry
    left_edge = max((ox for ox, _oy, _col in slots if ox < 0), default=0.0)
    right_edge = min((ox for ox, _oy, _col in slots if ox >= 0), default=0.0)
    for slot, (ox, oy, _col) in enumerate(slots):
        kind = kinds[slot]
        number = counted + slot
        veteran = share > 0 and floor((number + 1) * share) > floor(number * share)
        hp = ENEMIES[kind].hp + int(veteran)
        home_x, home_y = x0 + ox, y0 + oy
        x, y = home_x, home_y - ARRIVAL.rise
        if movement == "PINCER":
            y = home_y
            x = pincer[0] + (ox - left_edge) if ox < 0 else pincer[1] + (ox - right_edge)
        alien = Alien(x, y, kind, hp, id=game._new_id(), state="entering", flight=index, home_x=home_x, home_y=home_y)
        alien.max_hp, alien.veteran, alien.cargo = hp, veteran, slot == cargo
        game.aliens.append(alien)
        if kind == "carrier":
            game._brood[alien.id] = [0.0, 0]
    _extents(state, game.aliens[-len(slots) :])
    game._flights.append(state)
    return state


def _turned(game) -> float:
    """How far round its table an ESCORT ring has turned: every ring turns together, on the play clock."""
    return game._play * FORMATIONS["ESCORT"].orbit


def _extents(state, aliens):
    snake = FORMATIONS["SNAKE"].amplitude if state.movement == "SNAKE" else 0.0
    state.lo = min(alien.home_x - ENEMIES[alien.kind].amplitude - snake for alien in aliens)
    state.hi = max(alien.home_x + ENEMIES[alien.kind].amplitude + snake for alien in aliens)
    state.top = min(alien.home_y for alien in aliens)
    state.bottom = max(alien.home_y for alien in aliens)


def arrive(game, flight, top, escort=False) -> bool:
    """§7.3's band rule: arrive, push the older flights down by the overlap, or wait (False)."""
    if len(game.aliens) + flight.size > LIMITS.enemies:
        return False
    band = top + height(layout(flight)) + ARRIVAL.gap
    field_ = [state for state in game._flights if state.alive and state.movement != "ESCORT"]
    highest = min((state.top + state.push for state in field_), default=INF)
    if highest < band:
        push = band - highest
        if max(state.bottom + state.push for state in field_) + push > ARRIVAL.push_floor:
            return False
        for state in field_:
            state.push += push
    place(game, flight, top, escort)
    return True


def arrivals(game):
    """Due flights arrive in order; one that has to wait holds every later one behind it."""
    pending = game._pending
    while pending and game._play >= pending[0][0]:
        key = pending[0][1]
        escort = key[0] == "boss"
        flight = game._flight_row(key)
        top = game._escort_top() if escort else ARRIVAL.top
        if not arrive(game, flight, top, escort):
            return
        pending.pop(0)
        if not escort:
            game._next_flight += 1


# --- movement (§7.2) -------------------------------------------------------------------------------------------------


def _can_drop(flights, state, bottom, amount) -> bool:
    """Keeping flights apart: no drop that would bring this flight within ARRIVAL.apart above a lower flight."""
    limit = bottom + amount + ARRIVAL.apart
    for other in flights:
        if other is not state and other.alive and other.top > state.top and other.top < limit:
            if other.movement != "ESCORT":
                return False
    return True


def move_flights(game, slow):
    """Each flight's movement for this tick, as one delta for its slots."""
    flights = game._flights
    act = game.act - 1
    cue = True
    for state in flights:
        state.dx = state.dy = 0.0
        state.col = -1
        if not state.alive:
            continue
        if state.push > 0:
            moved = min(state.push, ARRIVAL.entry * slow)
            state.push = max(0.0, state.push - moved)
            state.dy = moved
        movement = state.movement
        if state.entering and movement != "ESCORT":
            state.y += state.dy  # a push moves the origin too (CAROUSEL places its slots from it; COLUMNS reads it)
            continue
        spec = FORMATIONS[movement]
        if movement == "MARCH":
            state.clock += slow
            low, high = spec.scale
            beat = max(LIMITS.beat, spec.speed[act] * (low + high * state.formation / state.size))
            if state.clock >= beat:
                state.clock = 0.0
                step = spec.step * state.direction
                if (step > 0 and state.hi + step > X_MAX) or (step < 0 and state.lo + step < X_MIN):
                    state.direction = -state.direction  # bug a: the edge beat drops and reverses, with no step
                    if _can_drop(flights, state, state.bottom, spec.drop):
                        state.dy += spec.drop
                else:
                    state.dx = step
                if cue:
                    game._march = game._march % 4 + 1
                    game.events.append(f"march{game._march}")
            cue = False
        elif movement in ("GLIDE", "SNAKE", "CAROUSEL"):
            speed = spec.drift if movement == "CAROUSEL" else spec.speed[act]
            if movement == "CAROUSEL":
                state.shift += spec.speed[act] * slow
            step = speed * slow * state.direction
            if (step > 0 and state.hi + step > X_MAX) or (step < 0 and state.lo + step < X_MIN):
                state.direction = -state.direction
                state.bounces += 1
                if spec.bounces and state.bounces % spec.bounces == 0:
                    if _can_drop(flights, state, state.bottom, spec.drop):
                        state.dy += spec.drop
            else:
                state.dx = step
        elif movement == "COLUMNS":
            state.clock += slow
            if state.clock >= spec.speed[act]:
                state.clock = 0.0
                column = state.turn % len(state.drops)
                state.turn += 1
                deepest = max(oy for _ox, oy, col in state.slots if col == column)
                bottom = state.y + state.dy + deepest + state.drops[column]
                if _can_drop(flights, state, bottom, spec.drop):
                    state.drops[column] += spec.drop
                    state.col, state.drop = column, spec.drop
        elif movement == "ESCORT":
            boss = game.boss
            if boss is not None:
                lag = spec.speed[act]
                state.dx, state.dy = (boss.x - state.x) / lag, (boss.y - state.y) / lag
        state.x += state.dx
        state.y += state.dy


def _carousel(state, slot):
    """A CAROUSEL slot's place on the perimeter of the flight's rectangle, as an offset from (x, y)."""
    xs = [ox for ox, _oy, _col in state.slots]
    left, width = min(xs), max(xs) - min(xs)
    tall = max(oy for _ox, oy, _col in state.slots)
    perimeter = 2 * (width + tall)
    if perimeter <= 0:
        return state.slots[slot][0], state.slots[slot][1]
    s = (slot * perimeter / len(state.slots) + state.shift) % perimeter
    if s < width:
        return left + s, 0.0
    s -= width
    if s < tall:
        return left + width, s
    s -= tall
    if s < width:
        return left + width - s, tall
    return left, tall - (s - width)


def tri(phase: int, period: int) -> float:
    """The integer-indexed triangle wave, 0 -> 1 -> 0 -> -1 -> 0 over `period` ticks; exact, no libm."""
    quarter = period // 4
    phase %= period
    if phase < quarter:
        return phase / quarter
    if phase < 3 * quarter:
        return 1 - (phase - quarter) / quarter
    return (phase - 3 * quarter) / quarter - 1


def dive_speed(game, kind) -> float:
    return PROFILES[game.config.difficulty].dive_vy * ENEMIES[kind].dive_speed


def dive_warning(game, kind, y) -> tuple[int, int]:
    """(W, flight) of a dive or mite launched from row y: the wobble, then the fall to the ship's row (F1)."""
    fall = ceil((LIMITS.dive_y - y) / dive_speed(game, kind))
    return TELEGRAPHS["mite" if kind == "mite" else "dive"] + fall, fall


def update(game, slow):
    """The pass over every enemy, in id order: slots, positions by state, telegraph wobbles, dives, landings, crush."""
    flights = game._flights
    for state in flights:
        state.lo = state.top = INF
        state.hi = state.bottom = -INF
        state.formation = state.entering = 0
    clock = floor(game._play)
    snake = FORMATIONS["SNAKE"]
    pincer = FORMATIONS["PINCER"].speed[game.act - 1] * slow
    entry = ARRIVAL.entry * slow
    home = DIVE.home * slow
    land, crush_y = ARRIVAL.land, SHELTERS.crush_y
    crush_x, crush_dy = SHELTERS.crush
    bunkers = game.bunkers
    turned = _turned(game)
    boss = game.boss
    carriers = []
    for alien in game.aliens:
        if alien.hp <= 0:
            continue
        index = alien.flight
        state = flights[index] if index >= 0 else None
        kind = ENEMIES[alien.kind]
        if state is not None:
            movement = state.movement
            if movement == "CAROUSEL":
                ox, oy = _carousel(state, alien.id - state.base)
                alien.home_x, alien.home_y = state.x + ox, state.y + oy
            elif movement == "ESCORT":
                if state.ring:
                    ox, oy = ring_at(state.ring[alien.id - state.base] + turned)
                else:
                    ox, oy, _col = state.slots[alien.id - state.base]
                alien.home_x, alien.home_y = state.x + ox, state.y + oy
            else:
                if state.dx:
                    alien.home_x += state.dx
                if state.dy:
                    alien.home_y += state.dy
                if state.col >= 0 and state.slots[alien.id - state.base][2] == state.col:
                    alien.home_y += state.drop
        pose = alien.state
        if pose == "formation" or pose == "telegraph":
            x = alien.home_x
            if kind.amplitude or (state is not None and state.movement == "SNAKE"):
                column = state.slots[alien.id - state.base][2] if state is not None else 0
                if kind.amplitude:
                    x += kind.amplitude * tri(clock + kind.phase * column, kind.period)
                if state is not None and state.movement == "SNAKE":
                    x += snake.amplitude * tri(clock + snake.phase * column, snake.period)
            alien.x, alien.y = x, alien.home_y
            if pose == "telegraph" and kind.behaviour in ("dive", "mite"):
                alien.timer -= 1
                if alien.timer <= 0 and not game._lockout:  # F6: it keeps wobbling through a lockout
                    _launch(game, alien)
            elif state is not None and alien.y >= land and index not in game._landed:
                game._landed.append(index)
        elif pose == "entering":
            if state is not None and state.movement == "PINCER":
                gap = alien.home_x - alien.x
                alien.y = alien.home_y
                if -pincer <= gap <= pincer:
                    alien.x, alien.state = alien.home_x, "formation"
                else:
                    alien.x += pincer if gap > 0 else -pincer
            else:
                alien.x = alien.home_x
                alien.y += entry
                if alien.y >= alien.home_y:
                    alien.y, alien.state = alien.home_y, "formation"
        elif pose == "diving":
            vy = dive_speed(game, alien.kind) * slow
            alien.x += alien.vx * slow
            alien.y += vy
            if alien.y > DIVE.exit_y:
                if state is None and (alien.kind == "mite" or boss is None):  # a mite leaves at the bottom: gone
                    game._remove(alien)
                    continue
                if state is None:  # a boss's diver returns to her, from above her
                    alien.home_x = boss.x
                alien.state, alien.diving, alien.vx = "returning", False, 0.0
                alien.x, alien.y = alien.home_x, DIVE.reenter_y  # bug d: above its slot's current position
        elif pose == "returning":
            if state is None and boss is not None:
                alien.home_x = boss.x  # she moves: it follows her back
            gap_x, gap_y = alien.home_x - alien.x, alien.home_y - alien.y
            if -home <= gap_x <= home and -home <= gap_y <= home:
                if state is None:  # back with her: it rejoins her brood and leaves the field (no points)
                    game._remove(alien)
                    continue
                alien.x, alien.y, alien.state = alien.home_x, alien.home_y, "formation"
            else:
                alien.x += min(home, max(-home, gap_x))
                alien.y += min(home, max(-home, gap_y))
        if alien.y >= crush_y and bunkers and alien.state not in ("diving", "returning"):
            for cell in cells_near(bunkers, alien.x, alien.y, crush_x, crush_dy):
                del bunkers[cell]
                game.shelter_rev += 1
        if alien.kind == "carrier":
            carriers.append(alien)
        if state is not None:
            if alien.state == "formation" or alien.state == "telegraph":
                state.formation += 1
            elif alien.state == "entering":
                state.entering += 1
            reach = kind.amplitude + (snake.amplitude if state.movement == "SNAKE" else 0.0)
            if alien.home_x - reach < state.lo:
                state.lo = alien.home_x - reach
            if alien.home_x + reach > state.hi:
                state.hi = alien.home_x + reach
            if alien.home_y < state.top:
                state.top = alien.home_y
            if alien.home_y > state.bottom:
                state.bottom = alien.home_y
    for state in flights:
        if state.movement == "PINCER" and state.alive and not state.entering:
            state.movement = FORMATIONS["PINCER"].then
    return carriers


def _launch(game, alien):
    """A wobble ends: the dive starts toward the ship's x now (F1: W >= 70 for a dive, 60 for a mite, or no dive)."""
    w, fall = dive_warning(game, alien.kind, alien.y)
    need = LIMITS.mite_warning if alien.kind == "mite" else LIMITS.dive_warning
    if w < need or fall < LIMITS.flight:
        if alien.flight < 0:  # a mite or a boss's diver has no slot to stay in: it goes (no points)
            game._remove(alien)
            return
        alien.state = "formation"  # it sank below the dive ceiling during its wobble: no dive
        return
    vy = dive_speed(game, alien.kind)
    ticks = (PLAYER.y - alien.y) / vy
    alien.vx = min(DIVE.vx_max, max(-DIVE.vx_max, (game.player.x - alien.x) / ticks))
    alien.state, alien.diving = "diving", True
    game._log(alien.id, alien.kind if alien.kind == "mite" else "dive", w, fall, alien.y, "telegraph", alien.kind)


def dives(game, slow):
    """Every dive interval the director may pick divers in formation under the dive ceiling (§7.3)."""
    act, profile = ACTS[game.act - 1], PROFILES[game.config.difficulty]
    game._dive_clock += slow
    every = act.dive_every * profile.dive_every
    most = max(1, act.divers + (profile.divers if game.act >= profile.divers_from else 0))
    if game.modifiers:
        most, every = endless.divers(most, every, len(game.modifiers))
    if game._dive_clock < every:
        return
    game._dive_clock = 0.0
    if game._lockout:
        return
    busy = sum(1 for alien in game.aliens if alien.kind == "diver" and alien.state in ("telegraph", "diving"))
    candidates = [
        alien
        for alien in game.aliens
        if alien.kind == "diver"
        and alien.state == "formation"
        and alien.hp > 0
        and alien.flight >= 0
        and dive_warning(game, "diver", alien.y)[0] >= LIMITS.dive_warning
    ]
    for _ in range(min(game._wave_row().squadron, most - busy)):
        if not candidates:
            return
        alien = candidates.pop(game.rng.randint(0, len(candidates) - 1))
        alien.state, alien.timer = "telegraph", TELEGRAPHS["dive"]
        game.events.append("tell")


def release_mite(game, x, y, parent=0, early=False) -> bool:
    """A mite wobbles where it is released, only where its dive will meet W >= 60 and under the enemy cap. `early`:
    released before step 5 (a splitter killed by a shot or the bomb), whose pass this same tick would otherwise take
    the first tick off its 30-tick wobble (F9)."""
    if len(game.aliens) >= LIMITS.enemies or dive_warning(game, "mite", y)[0] < LIMITS.mite_warning:
        return False
    timer = TELEGRAPHS["mite"] + (1 if early else 0)
    mite = Alien(x, y, "mite", 1, id=game._new_id(), state="telegraph", home_x=x, home_y=y, timer=timer)
    game.aliens.append(mite)
    if parent:
        game._parent[mite.id] = parent
        game._brood[parent][1] += 1
    game.events.append("tell")
    return True


def launches(game, carriers, slow):
    """Carriers launch a mite every ENEMIES["carrier"].launch ticks while fewer than its `children` live."""
    spec = ENEMIES["carrier"]
    for carrier in carriers:
        brood = game._brood.get(carrier.id)
        if brood is None or carrier.hp <= 0:
            continue
        if brood[0] < spec.launch:
            brood[0] += slow
        elif brood[1] < spec.children and carrier.state == "formation" and not game._lockout:
            if release_mite(game, carrier.x, carrier.y + 1.0, carrier.id):
                brood[0] = 0.0


def step(game, slow):
    """Step 5: arrivals, movement, the pass, dives, carriers."""
    arrivals(game)
    move_flights(game, slow)
    carriers = update(game, slow)
    dives(game, slow)
    if carriers:
        launches(game, carriers, slow)
