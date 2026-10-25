"""The fire director (spec §6.3) and the enemy-shot launcher it shares with boss attacks (F1, F2, F3, F8). Step 7.

The director holds a reload clock. When it passes the reload, it draws a pattern whose slots fit the free slots and
whose shots fit under the shot cap, picks the shooters (the front enemy of each 3-cell bucket that stands in
formation at y <= 12.5, each firing its own kind), checks F1 and F2 for every shot, and starts the telegraph. A
volley holds its slots from its telegraph until its last shot ends. Every shot is classed safe (slowed so its
warning W is at least 60) or reaction (full speed; F1 only) by one `rng` draw per volley.
"""

from dataclasses import dataclass
from math import ceil, floor

from ..model import Hazard, Shot
from . import endless
from .tables import ENEMIES, LIMITS, PATTERNS, PROFILES, SHOTS, TELEGRAPHS, UPGRADES

SPAWN_DY = LIMITS.spawn_y - LIMITS.fire_y  # a shot spawns this far below its shooter (0.7, so F2's two rows agree)
ORDER = ("single", "burst", "crossfire", "curtain")
X_MIN, X_MAX = LIMITS.field


@dataclass(slots=True)
class Volley:
    """One director volley (hashed): its telegraph, then its shots, holding `slots` until its last shot ends."""

    id: int
    pattern: str
    cls: str  # safe or reaction
    slots: int
    shooters: list  # alien ids, one per shot (a burst: one shooter, three shots)
    kinds: list  # each shooter's shot kind
    tele: int  # its telegraph, after every multiplier and the floor
    timer: float  # telegraph ticks left (x0.6 a tick under SLOW)
    left: int = 0  # shots still to fire (a burst's later shots)
    wait: float = 0.0  # ticks to a burst's next shot
    shot: int = 0  # shots fired so far
    live: int = 0  # its shots in flight, counted each tick
    aim: float = 0.0  # the ship's x when the telegraph began (Easy's darts aim there)
    hazard: int = 0  # a curtain's gap mark
    gap: tuple = ()  # a curtain's gap (x0, x1)
    done: bool = False  # every shot fired


# --- shared rules: telegraphs, speeds, warning time ------------------------------------------------------------------


def telegraph(game, base) -> int:
    """A telegraph after the profile's factor and SCANNER, then the 18-tick floor (F3, F8); 0 stays 0 (no tell)."""
    if base <= 0:
        return 0
    factor = PROFILES[game.config.difficulty].telegraph
    if "SCANNER" in game.upgrades:
        factor *= UPGRADES["SCANNER"].value
    return max(LIMITS.telegraph, floor(base * factor + 0.5))


def shot_speed(game, kind, v_a) -> float:
    """A kind's vy: its factor of the act's pellet speed, times the profile's speed (and Endless's FAST SHOTS), then
    the 0.28 cap (F8)."""
    factor = game.difficulty
    if game.modifiers:
        factor *= endless.speed(len(game.modifiers))
    return min(LIMITS.vy, SHOTS[kind].speed * v_a * factor)


def plan(y, vy, tele, cls):
    """(vy, W, flight) of a shot spawned at row y, or None when it fails F1. A safe shot is slowed to W >= 60."""
    distance = LIMITS.hit_y - y
    if distance <= 0:
        return None
    if cls == "safe" and tele < LIMITS.safe_warning:
        vy = min(vy, distance / (LIMITS.safe_warning - tele))
    flight = ceil(distance / vy)
    w = tele + flight
    if w < LIMITS.warning or flight < LIMITS.flight:
        return None
    return vy, w, flight


def aim_vx(x, y, vy, target, bound) -> float:
    """An aimed shot's vx: one division by its fall time to the ship's row, within +-bound."""
    ticks = (LIMITS.hit_y - y) / vy
    return min(bound, max(-bound, (target - x) / ticks))


def lead_target(game, x, y, vy) -> float:
    """A hunter dart's target: the ship's x plus its lead times its last-tick vx times the fall time, in 2..57."""
    ticks = (LIMITS.hit_y - y) / vy
    target = game.player.x + PROFILES[game.config.difficulty].lead * game.player.vx * ticks
    return min(X_MAX, max(X_MIN, target))


def launch(game, x, y, kind, vx, vy, volley, cls, w, flight, pose, source) -> Shot:
    """Spawn one enemy shot and log it in `fired`."""
    shot = Shot(x, y, False, vx, vy, 1, game._new_id(), kind, volley)
    game.shots.append(shot)
    game._log(shot.id, kind, w, flight, y, pose, source, cls)
    return shot


def shot_kind(game, alien) -> str:
    """What a shooter fires: its kind's shot; gunners switch to hunter darts from the profile's wave."""
    kind = ENEMIES[alien.kind].shot
    if kind == "dart":
        first = PROFILES[game.config.difficulty].hunters
        if first and game._row() >= first:
            return "hunter"
    return kind


def load(game) -> int:
    """Enemy shots in flight (a cluster counts the 3 pellets it splits into), plus every director volley's unfired
    shots and every boss attack's shots still owed (telegraphing or mid-volley), so nothing passes the shot cap."""
    total = 0
    for shot in game.shots:
        if not shot.friendly:
            total += 3 if shot.kind == "cluster" else 1
    for volley in game._volleys:
        if not volley.done:
            total += (len(volley.shooters) if volley.shot == 0 else 0) + volley.left
    for state in game._attacks:
        total += state.owed
    return total


def reload_eff(game) -> float:
    """reload x frenzy / pressure, then the reload floor (LIMITS.reload, F8)."""
    row = game._fire
    frenzy = 1.0
    if not game.boss and not game._pending and len(game.aliens) <= row.frenzy_left:
        frenzy = row.frenzy
    return max(LIMITS.reload, row.reload * frenzy / game._pressure())


# --- the director ----------------------------------------------------------------------------------------------------


def step(game, slow):
    """Step 7: volleys in progress, then the reload clock and a new volley (outside the lockout)."""
    if game._volleys:
        _advance(game, slow)
    if game._lockout:
        return
    game._dclock += slow
    if game._dclock < reload_eff(game):
        return
    if _decide(game):
        game._dclock = 0.0


def _advance(game, slow):
    counts = {}
    for shot in game.shots:
        if not shot.friendly and shot.volley:
            counts[shot.volley] = counts.get(shot.volley, 0) + 1
    by_id = None
    keep = []
    for volley in game._volleys:
        volley.live = counts.get(volley.id, 0)
        if not volley.done:
            if volley.shot == 0:
                volley.timer -= slow
                if volley.hazard:
                    mark_ttl(game, (volley.hazard,), volley.timer)
                if volley.timer <= 0:
                    by_id = by_id or {alien.id: alien for alien in game.aliens}
                    _fire(game, volley, by_id)
            else:
                volley.wait -= slow
                if volley.wait <= 0:
                    by_id = by_id or {alien.id: alien for alien in game.aliens}
                    _burst(game, volley, by_id)
        if volley.done and volley.live == 0 and not _has_shots(game, volley):  # its shots just fired still fly
            _end(game, volley)
        else:
            keep.append(volley)
    game._volleys = keep


def mark_ttl(game, ids, timer):
    """A telegraph's marks carry the ticks left until it fires (P fills a gap's dashes in for the last 15)."""
    ttl = max(0, ceil(timer))
    for hazard in game.hazards:
        if hazard.id in ids:
            hazard.ttl = ttl


def _end(game, volley):
    if volley.hazard:
        game.hazards = [hazard for hazard in game.hazards if hazard.id != volley.hazard]


def cancel(game):
    """Every volley in progress ends: its shooters stop telegraphing and its gap mark goes (the mercy clear, a bomb,
    the clear)."""
    if not game._volleys:
        return
    ids = {}
    for volley in game._volleys:
        for shooter in volley.shooters:
            ids[shooter] = True
        _end(game, volley)
    for alien in game.aliens:
        if alien.state == "telegraph" and alien.id in ids:
            alien.state = "formation"
    game._volleys = []


def _shooters(game) -> list:
    """The front enemy of each 3-cell bucket in formation at y <= 12.5 that fires a shot (F2), by bucket."""
    fronts = {}
    for alien in game.aliens:
        if alien.state != "formation" or alien.hp <= 0 or alien.y > LIMITS.fire_y or not ENEMIES[alien.kind].shot:
            continue
        key = floor(alien.x / LIMITS.bucket)
        best = fronts.get(key)
        if best is None or alien.y > best.y:
            fronts[key] = alien
    return [fronts[key] for key in sorted(fronts)]


def _weights(game, free, cap) -> list:
    row, profile = game._fire, PROFILES[game.config.difficulty]
    weights = {
        "single": row.single,
        "burst": row.burst + profile.burst if row.burst else 0,
        "crossfire": 0 if profile.no_crossfire else (row.crossfire + profile.crossfire if row.crossfire else 0),
        "curtain": row.curtain if len(game.hazards) < LIMITS.hazards else 0,
    }
    out = []
    for name in ORDER:
        pattern = PATTERNS[name]
        if weights[name] > 0 and pattern.slots <= free and pattern.shots[1] <= cap:
            out.append((name, weights[name]))
    return out


def _decide(game) -> bool:
    row = game._fire
    free = min(row.slots, LIMITS.slots) - sum(volley.slots for volley in game._volleys)
    if free <= 0:
        return False
    shooters = _shooters(game)
    if not shooters:
        return False
    eligible = _weights(game, free, LIMITS.enemy_shots - load(game))
    if not eligible:
        return False
    name = eligible[0][0]
    if len(eligible) > 1:
        roll = game.rng.randint(1, sum(weight for _name, weight in eligible))
        for name, weight in eligible:  # noqa: B007
            roll -= weight
            if roll <= 0:
                break
    share = row.reaction * PROFILES[game.config.difficulty].reaction
    cls = "reaction" if share > 0 and game.rng.random() < share else "safe"
    volley = None
    if name == "burst":
        volley = _burst_volley(game, shooters, cls)
    elif name == "crossfire":
        volley = _crossfire_volley(game, shooters, cls)
    elif name == "curtain":
        volley = _curtain_volley(game, cls)
    if volley is None:
        volley = _single_volley(game, shooters, cls)
    if volley is None:
        return False
    game._volleys.append(volley)
    if volley.tele <= 0:
        _fire(game, volley, {alien.id: alien for alien in volley_aliens(game, volley)})
        if volley.done and volley.live == 0 and not _has_shots(game, volley):
            game._volleys.pop()
    return True


def volley_aliens(game, volley):
    wanted = volley.shooters
    return [alien for alien in game.aliens if alien.id in wanted]


def _has_shots(game, volley) -> bool:
    return any(not shot.friendly and shot.volley == volley.id for shot in game.shots)


def _tell(game, volley, aliens):
    if volley.tele > 0:
        for alien in aliens:
            alien.state = "telegraph"
        game.events.append("tell")


def _focus(game, candidates) -> list:
    """With probability `focus`, the shooters within 6 cells of the ship (if any); otherwise all of them."""
    focus = game._fire.focus
    if focus > 0 and game.rng.random() < focus:
        near = [alien for alien in candidates if abs(alien.x - game.player.x) <= LIMITS.focus_reach]
        if near:
            return near
    return candidates


def _single_volley(game, shooters, cls):
    legal = []
    for alien in shooters:
        kind = shot_kind(game, alien)
        tele = telegraph(game, SHOTS[kind].telegraph)
        y = alien.y + SPAWN_DY
        if plan(y, shot_speed(game, kind, game._fire.v_a), tele, cls) is not None:
            legal.append(alien)
    if not legal:
        return None
    pool = _focus(game, legal)
    alien = pool[game.rng.randint(0, len(pool) - 1)] if len(pool) > 1 else pool[0]
    kind = shot_kind(game, alien)
    tele = telegraph(game, SHOTS[kind].telegraph)
    volley = Volley(game._new_id(), "single", cls, 1, [alien.id], [kind], tele, float(tele), aim=game.player.x)
    _tell(game, volley, (alien,))
    return volley


def _burst_volley(game, shooters, cls):
    """Three pellets 8 ticks apart from the shooter nearest above the ship (|dx| <= 8)."""
    pattern = PATTERNS["burst"]
    tele = telegraph(game, TELEGRAPHS[pattern.telegraph])
    speed = shot_speed(game, "pellet", game._fire.v_a)
    best = None
    for alien in shooters:
        dx = abs(alien.x - game.player.x)
        if dx <= pattern.reach and (best is None or dx < abs(best.x - game.player.x)):
            if plan(alien.y + SPAWN_DY, speed, tele, cls) is not None:
                best = alien
    if best is None:
        return None
    shots = pattern.shots[1]
    volley = Volley(game._new_id(), "burst", cls, pattern.slots, [best.id], ["pellet"], tele, float(tele))
    volley.left = shots - 1
    _tell(game, volley, (best,))
    return volley


def _crossfire_volley(game, shooters, cls):
    """Two gunners at least 8 cells left and right of the ship, aimed to arrive together."""
    pattern = PATTERNS["crossfire"]
    ship = game.player.x
    left = [alien for alien in shooters if ENEMIES[alien.kind].shot == "dart" and alien.x <= ship - pattern.reach]
    right = [alien for alien in shooters if ENEMIES[alien.kind].shot == "dart" and alien.x >= ship + pattern.reach]
    if not left or not right:
        return None
    pair = [
        left[game.rng.randint(0, len(left) - 1)] if len(left) > 1 else left[0],
        right[game.rng.randint(0, len(right) - 1)] if len(right) > 1 else right[0],
    ]
    tele = telegraph(game, TELEGRAPHS[pattern.telegraph])
    if _together(game, pair, tele, cls) is None:
        return None
    kinds = [shot_kind(game, alien) for alien in pair]
    volley = Volley(game._new_id(), "crossfire", cls, pattern.slots, [a.id for a in pair], kinds, tele, float(tele))
    volley.aim = ship
    _tell(game, volley, pair)
    return volley


def _together(game, pair, tele, cls):
    """The common fall time T of a crossfire's darts, or None when one fails F1."""
    longest = 0.0
    for alien in pair:
        vy = shot_speed(game, shot_kind(game, alien), game._fire.v_a)
        longest = max(longest, (LIMITS.hit_y - alien.y - SPAWN_DY) / vy)
    if cls == "safe":
        longest = max(longest, LIMITS.safe_warning - tele)
    for alien in pair:
        y = alien.y + SPAWN_DY
        if plan(y, (LIMITS.hit_y - y) / longest, tele, "reaction") is None:
            return None
    return longest


def _curtain_volley(game, cls):
    """5-7 pellets at once from the front row of a COLUMNS flight, with a gap of 7 cells marked for 30 ticks. A
    column's front is its lowest enemy that fires a shot, as for `_shooters` (F2: a diver never fires)."""
    pattern = PATTERNS["curtain"]
    half = pattern.gap / 2
    low, high = LIMITS.gap_x
    tele = telegraph(game, TELEGRAPHS[pattern.telegraph])
    speed = shot_speed(game, "pellet", game._fire.v_a)
    for state in game._flights:
        if state.movement != "COLUMNS" or not state.alive or state.escort:
            continue
        fronts = {}
        for alien in game.aliens:
            if alien.flight != state.index or alien.state != "formation" or alien.y > LIMITS.fire_y:
                continue
            if alien.hp <= 0 or not ENEMIES[alien.kind].shot:
                continue
            column = state.slots[alien.id - state.base][2]
            if column not in fronts or alien.y > fronts[column].y:
                fronts[column] = alien
        if len(fronts) < pattern.shots[0]:
            continue
        reach = min(pattern.reach, LIMITS.gap_reach) - half  # keeps the gap's centre within reach of the ship
        offset = game.rng.randint(-floor(reach), floor(reach))
        centre = min(high - half, max(low + half, game.player.x + offset))
        gap = (centre - half, centre + half)
        row = [
            fronts[column]
            for column in sorted(fronts)
            if not _in_gap(fronts[column], gap) and plan(fronts[column].y + SPAWN_DY, speed, tele, cls) is not None
        ]
        row.sort(key=lambda alien: (abs(alien.x - centre), alien.id))
        row = sorted(row[: pattern.shots[1]], key=lambda alien: alien.id)
        if len(row) < pattern.shots[0]:
            continue
        hazard = Hazard(game._new_id(), "gap", gap[0], gap[1], LIMITS.spawn_y, float(19), "lock", tele)
        game.hazards.append(hazard)
        volley = Volley(
            game._new_id(), "curtain", cls, pattern.slots, [a.id for a in row], ["pellet"] * len(row), tele, float(tele)
        )
        volley.hazard, volley.gap = hazard.id, gap
        _tell(game, volley, row)
        return volley
    return None


def _in_gap(alien, gap) -> bool:
    """A shooter that could stand in the gap (or within half a cell of it) by the time it fires: a weaver keeps
    zig-zagging round its slot through the telegraph, so its whole span counts (F4). COLUMNS has no SNAKE offset."""
    reach = 0.5 + ENEMIES[alien.kind].amplitude
    return gap[0] - reach <= alien.home_x <= gap[1] + reach


# --- firing ----------------------------------------------------------------------------------------------------------


def _standing(alien) -> bool:
    """A shooter fires only if it still stands in formation at y <= 12.5 (F2)."""
    return alien is not None and alien.hp > 0 and alien.state in ("telegraph", "formation") and alien.y <= LIMITS.fire_y


def _fire(game, volley, by_id):
    """The telegraph ends: every shooter still standing fires (F1 and F2 checked again at the row it fires from)."""
    volley.shot = 1
    if volley.pattern == "crossfire":
        _fire_crossfire(game, volley, by_id)
    else:
        for alien_id, kind in zip(volley.shooters, volley.kinds, strict=True):
            alien = by_id.get(alien_id)
            if _standing(alien):
                _shoot(game, volley, alien, kind, volley.tele)
    if volley.left > 0 and _standing(by_id.get(volley.shooters[0])):
        volley.wait = PATTERNS["burst"].spacing
    else:
        _finish(volley, by_id)


def _burst(game, volley, by_id):
    alien = by_id.get(volley.shooters[0])
    if not _standing(alien):
        _finish(volley, by_id)
        return
    volley.left -= 1
    _shoot(game, volley, alien, "pellet", volley.tele + PATTERNS["burst"].spacing * volley.shot)
    volley.shot += 1
    if volley.left > 0:
        volley.wait = PATTERNS["burst"].spacing
    else:
        _finish(volley, by_id)


def _finish(volley, by_id):
    volley.done, volley.left = True, 0
    for alien_id in volley.shooters:
        alien = by_id.get(alien_id)
        if alien is not None and alien.state == "telegraph":
            alien.state = "formation"


def _shoot(game, volley, alien, kind, tele):
    y = alien.y + SPAWN_DY
    planned = plan(y, shot_speed(game, kind, game._fire.v_a), tele, volley.cls)
    if planned is None:
        return
    vy, w, flight = planned
    path = SHOTS[kind].path
    vx = 0.0
    if path == "aimed":
        target = volley.aim if PROFILES[game.config.difficulty].aim == "tell" else game.player.x
        vx = aim_vx(alien.x, y, vy, target, SHOTS[kind].vx_max)
    elif path == "lead":
        vx = aim_vx(alien.x, y, vy, lead_target(game, alien.x, y, vy), SHOTS[kind].vx_max)
    launch(game, alien.x, y, kind, vx, vy, volley.id, volley.cls, w, flight, alien.state, alien.kind)


def _fire_crossfire(game, volley, by_id):
    pair = [by_id.get(alien_id) for alien_id in volley.shooters]
    longest = _together(game, pair, volley.tele, volley.cls) if all(_standing(a) for a in pair) else None
    for alien, kind in zip(pair, volley.kinds, strict=True):
        if not _standing(alien):
            continue
        if longest is None:  # its partner is gone (or the pair can't arrive together): it fires on its own
            _shoot(game, volley, alien, kind, volley.tele)
            continue
        y = alien.y + SPAWN_DY
        planned = plan(y, (LIMITS.hit_y - y) / longest, volley.tele, "reaction")
        if planned is None:
            continue
        vy, w, flight = planned
        if SHOTS[kind].path == "lead":
            target = lead_target(game, alien.x, y, vy)
        else:
            target = volley.aim if PROFILES[game.config.difficulty].aim == "tell" else game.player.x
        vx = aim_vx(alien.x, y, vy, target, SHOTS[kind].vx_max)
        launch(game, alien.x, y, kind, vx, vy, volley.id, volley.cls, w, flight, alien.state, alien.kind)
