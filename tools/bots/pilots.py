"""The bots of spec §18's table. Each takes one Frame a tick and returns the actions for the next step.

skilled (sk_d6_p100, "perfect" in reports), strong (sk_d12_p92, spec §27) and every sk_dD_pP plan with the 43-plan
library against what they predict; novice is Phase 3's (noisy aim, slow taps, no dodging); naive chases the lowest
enemy holding fire; nobomb never bombs; bombhappy bombs at the first threat. Every bot takes the card with its pick
policy and always continues at the prompt.
"""

import random
from math import ceil, inf
from operator import attrgetter, itemgetter
from typing import NamedTuple

from . import know, params, picks, predict
from . import plan as planner
from .track import Perception

NOTHING = predict.Threats(1, 0).finish()  # no threat: the plan is chosen by where it ends
CHOICES = ("upgrade", "continue")  # the Session clears the controls at these phases' edges (spec §16.2)
# keyed: how the bot's keys reach the game (balance.py's --input names). Tap-keyed ones plan their taps (§18
# "Planner"), as (lag, spread) for plan.Latched: timed controls are keyed with taps too (balance.Keyboard), and over
# ssh a tap lands SSH_LAG steps late give or take SSH_SPREAD.
LATCHED = {"latched": (0, 0), "timed": (0, 0), "latched-ssh": (params.SSH_LAG, params.SSH_SPREAD)}


class Pilot:
    """What every bot shares: the phases, the card, the prompt, and its own keys under latched controls."""

    name = "pilot"
    delay = 0
    perceive = 1.0
    policy = "priority"

    def __init__(self, seed: int, *, policy: str | None = None, keyed: bool | str = False):
        """keyed: False for direct play, else the input its keys go through (True: held keys, as kitty's)."""
        self.seed = seed
        self.rng = random.Random(seed * params.RNG_SALT[0] + params.RNG_SALT[1])
        self.policy = picks.check(policy or self.policy)
        self.keyed = bool(keyed)
        self.keys = planner.Latched(*LATCHED[keyed]) if keyed in LATCHED else None
        self.beat = params.KEY_BEAT if keyed else 1
        self.quiet = params.KEY_QUIET if keyed else 0
        self.last = ()
        self.phase, self.since = "", 0
        self.want = None
        self.pressed = self.released = -inf

    def act(self, frame) -> tuple[str, ...]:
        keys = self.keys
        if frame.phase != self.phase:
            if keys is not None and (self.phase in CHOICES or frame.phase in CHOICES):
                keys.clear()
            self.phase, self.since, self.want = frame.phase, frame.tick, None
        elif keys is not None and frame.phase in CHOICES:
            keys.unlatch()
        self.observe(frame)
        phase = frame.phase
        if phase == "upgrade":
            out = self.card(frame)
        elif phase == "continue":
            out = self.prompt(frame)
        elif phase in ("game_over", "victory"):
            out = ()
        else:
            out = self.play(frame)
        self.last = out
        if keys is not None:
            keys.key(frame.tick, ("right" in out) - ("left" in out))
        return out

    def observe(self, frame) -> None:
        """Every tick, whatever the phase."""

    def play(self, frame) -> tuple[str, ...]:
        return ()

    # --- the card and the prompt (§18) -----------------------------------------------------------------------------

    def card(self, frame) -> tuple[str, ...]:
        """left/right edges to the offer the policy wants, then fire once the card is armed."""
        offers, cursor, armed = frame.card
        if not offers or frame.tick - self.since < self.quiet:
            return ()
        if self.want is None:
            self.want = picks.choose(self.policy, offers, frame.ship, self.rng)
        if cursor != self.want:
            count = len(offers)
            key = "right" if (self.want - cursor) % count <= (cursor - self.want) % count else "left"
            return self.tap(frame.tick, key)
        return self.tap(frame.tick, "fire") if armed else ()

    def prompt(self, frame) -> tuple[str, ...]:
        """Always continue: fire once the prompt is armed (TIMING.arm ticks after it opens)."""
        if frame.tick - self.since < max(know.ARM, self.quiet):
            return ()
        return self.tap(frame.tick, "fire")

    def tap(self, tick: int, key: str) -> tuple[str, ...]:
        """Hold `key` for `beat` ticks, then let go as long: each press is one edge (direct bots: beat 1)."""
        if key in self.last:
            if tick - self.pressed >= self.beat:
                self.released = tick
                return ()
            return (key,)
        if tick - self.released >= self.beat:
            self.pressed = tick
            return (key,)
        return ()


class Novice(Pilot):
    """Phase 3's novice, unchanged: about 15 ticks late, noisy aim, slow tapping, no dodging, no bombs."""

    name = "novice"
    delay = params.NOVICE_DELAY
    policy = "first"

    def __init__(self, seed, **options):
        super().__init__(seed, **options)
        self.frames = []
        self.aim = None
        self.retarget = self.next_tap = 0

    def observe(self, frame):
        self.frames.append(frame)
        if len(self.frames) > self.delay + 1:
            del self.frames[0]

    def play(self, frame):
        seen = self.frames[0]
        if seen.phase != "playing" or frame.phase != "playing":
            return ()
        px = frame.ship.x  # a player always knows where their own ship is
        targets = []
        if seen.enemies:
            columns = {}
            for enemy in seen.enemies:
                column = round(enemy.x)
                if column not in columns or enemy.y > columns[column][1]:
                    columns[column] = (enemy.x, enemy.y)
            targets = list(columns.values())
        else:
            parts = [part for part in seen.parts if part.state != "broken"]
            if parts:
                targets = [(sum(part.x for part in parts) / len(parts), parts[0].y)]
        if not targets:
            return ()
        rng = self.rng
        if self.aim is None or frame.tick >= self.retarget:
            target = min(targets, key=lambda t: abs(t[0] - px) + rng.uniform(0, params.NOVICE_CHOICE))
            self.aim = target[0] + rng.gauss(0, params.NOVICE_NOISE)
            self.retarget = frame.tick + rng.randint(*params.NOVICE_RETARGET)
        actions = []
        if self.aim - px > params.NOVICE_DEADBAND:
            actions.append("right")
        elif px - self.aim > params.NOVICE_DEADBAND:
            actions.append("left")
        if abs(self.aim - px) < params.NOVICE_WINDOW and frame.tick >= self.next_tap:
            actions.append("fire")
            self.next_tap = frame.tick + rng.randint(*params.NOVICE_TAP)
        return tuple(actions)


class Naive(Pilot):
    """Chases the lowest enemy holding fire; never dodges or bombs (sees the current Frame)."""

    name = "naive"

    def play(self, frame):
        if frame.phase != "playing":
            return ("fire",)
        px = frame.ship.x
        if frame.enemies:
            target = max(frame.enemies, key=lambda e: (e.y, -abs(e.x - px))).x
        else:
            parts = [part for part in frame.parts if part.state != "broken"]
            if not parts:
                return ("fire",)
            target = parts[0].x
        if target - px > 0.3:
            return ("fire", "right")
        if px - target > 0.3:
            return ("fire", "left")
        return ("fire",)


class Target(NamedTuple):
    """Something a shot can hit, as seen: position and velocity per enemy tick, its half-width and row half-height.
    `block`: a shot that meets it ends there without hurting it (a shielded part)."""

    ident: int
    x: float
    y: float
    vx: float
    vy: float
    half: float
    high: float
    hp: int
    points: int
    tier: int
    block: bool = False
    pose: str = ""
    kind: str = ""


# A Target from all its field values in order, without the Python-level __new__ (a decision builds dozens).
_new = tuple.__new__


class Skilled(Pilot):
    """Plans each move with the 43-plan library against what it predicts from what it saw `delay` ticks ago;
    fires only on a predicted hit; bombs only when no plan is safe and the hit is inside its reaction window."""

    name = "skilled"
    delay, perceive = params.SKILLED
    bombs = True
    happy = False  # bombhappy: bombs at the first threat

    def __init__(self, seed, **options):
        super().__init__(seed, **options)
        self.sense = Perception(self.delay, self.perceive, seed)
        self.cadence = params.cadence(self.delay)
        self.window = self.delay + self.cadence + params.REACTION_EXTRA
        self.plan, self.plan_at = planner.STAND, 0
        self.next = 0
        self.fire_next = self.bomb_next = False
        self.last_fire = -inf
        self.own = {}
        self.blinking = self.guarded = False
        self.immune_until = -inf
        self.bombed_at = -inf
        self.pressing = False  # a no-escape moment is going on
        self.no_escape = []  # (tick, wave, act, bombs in stock, bombed)
        self.decisions = 0
        self.threats = None
        self.dig = False  # nothing can be hit but through an own shelter: shoot through it
        self._cells, self._columns = None, set()
        self._shelters, self._cell_set = None, set()
        self._bullet = (None, 0.0)

    # --- every tick ------------------------------------------------------------------------------------------------

    def observe(self, frame):
        self.sense.see(frame)
        ship = frame.ship
        tick = frame.tick
        own = self.own
        fresh = False
        for shot in frame.shots:
            if shot.friendly and shot.id not in own:
                own[shot.id] = tick
                fresh = True
        if fresh:
            self.last_fire = tick
            if len(own) > 64:
                for ident in [i for i, t in own.items() if t < tick - 120]:
                    del own[ident]
        if ship.blinking and not self.blinking:
            if self.bombed_at >= tick - 1:
                until = tick + know.ABSORB  # BOMB.invulnerable: the same 45 ticks
            elif self.guarded and not ship.guarded:
                until = tick + know.ABSORB
            else:
                until = tick + know.SAFE  # the mercy clear
            self.immune_until = until - params.IMMUNE_SLACK
        elif not ship.blinking:
            self.immune_until = -inf
        self.blinking, self.guarded = ship.blinking, ship.guarded

    def play(self, frame):
        tick = frame.tick
        sense = self.sense
        if frame.phase != "playing":
            dest = self.pickup(frame)
            if self.keys is not None:
                return self.tapped(frame, dest)
            self.plan, self.plan_at = planner.STAND, tick
            if dest is None:
                return ()
            if dest > frame.ship.x + 0.3:
                return ("right",)
            if dest < frame.ship.x - 0.3:
                return ("left",)
            return ()
        if tick < sense.frozen_until:  # the hit freeze: nothing moves, nothing to decide
            self.next = tick
            return ()
        if tick >= self.next or sense.alarm:
            self.decide(frame)
            self.next = tick + self.cadence
        move = planner.direction(self.plan, tick + 1 - self.plan_at)
        out = ["right"] if move > 0 else ["left"] if move < 0 else []
        if self.fire_next:
            out.append("fire")
            self.fire_next = False
        if self.bomb_next:
            self.bomb_next = False
            if "bomb" not in self.last:
                out.append("bomb")
                self.bombed_at = tick + 1
        return tuple(out)

    def tapped(self, frame, dest):
        """Outside play under latched controls (the intro, the clear panel): toward a pickup, or standing, by the
        plan whose taps end nearest it."""
        tick = frame.tick
        if tick >= self.next:
            x0, speed = frame.ship.x, know.ship_speed(frame.ship)
            current = planner.remainder(self.plan, tick - self.plan_at)
            target = x0 if dest is None else dest
            self.plan, _hit, _motion = planner.choose(NOTHING, x0, speed, target, current, self.keys, tick)
            self.plan_at, self.next = tick, tick + self.cadence
        move = planner.direction(self.plan, tick + 1 - self.plan_at)
        return ("right",) if move > 0 else ("left",) if move < 0 else ()

    # --- a decision ------------------------------------------------------------------------------------------------

    def decide(self, frame):
        sense = self.sense
        sense.ingest()
        self.decisions += 1
        ship = frame.ship
        tick = frame.tick
        x0, speed = ship.x, know.ship_speed(ship)
        cells = self.cells(frame.shelters)
        immune = max(0, int(self.immune_until - tick)) if ship.blinking else 0
        threats = predict.build(sense, params.BEAM_HORIZON, immune, cells)
        self.threats = threats
        targets, base, f = self.targets(frame)
        claimed = self.claims(frame, targets, base, f)
        current = planner.remainder(self.plan, tick - self.plan_at)
        dest = self.destination(frame, targets, claimed, base, f, x0, speed, cells, current)
        keys = self.keys
        if keys is None:
            plan, hit = planner.choose(threats, x0, speed, dest, current)
            motion = plan
        else:  # latched: each plan is judged by the motion its taps make
            plan, hit, motion = planner.choose(threats, x0, speed, dest, current, keys, tick)
        self.plan, self.plan_at = plan, tick
        protected = ship.guarded or ship.blinking
        if self.happy and not protected and ship.bombs > 0 and not threats.empty():
            stand = planner.STAND if keys is None else keys.motion(planner.STAND, tick)
            if planner.first_hit(threats, stand, x0, speed) is not None:
                self.bomb_next = True
        if hit is not None and hit <= self.window and not protected:
            if not self.pressing:
                self.no_escape.append((tick, frame.wave, frame.act, ship.bombs, bool(self.bombs and ship.bombs > 0)))
                self.pressing = True
            if self.bombs and ship.bombs > 0:
                self.bomb_next = True
        else:
            self.pressing = False
        x1 = planner.position(motion, x0, speed, 1)
        if ship.free_slots > 0 and tick + 1 - self.last_fire >= know.GATE:
            if self.lane(x1, frame, targets, claimed, base, f, cells):
                self.fire_next = True

    def targets(self, frame):
        """What its shots can hit, as seen, with velocities from the Frame VELOCITY_SPAN ticks before."""
        sense = self.sense
        seen = sense.seen
        old = sense.old(params.VELOCITY_SPAN)
        seen_at = sense.clock_at(seen.tick)
        span = seen_at - sense.clock_at(old.tick)
        base = sense.clock_at(frame.tick) - seen_at
        f = know.SLOW if know.timed(frame.ship, "slow") else 1.0
        # Only the two lowest of each column can be the first a shot meets (and the next, once one is claimed).
        fronts = {}
        for enemy in seen.enemies:
            column = int(enemy.x + 0.5)
            pair = fronts.get(column)
            if pair is None:
                fronts[column] = [enemy, None]
            elif enemy.y > pair[0].y:
                pair[0], pair[1] = enemy, pair[0]
            elif pair[1] is None or enemy.y > pair[1].y:
                pair[1] = enemy
        then = {enemy.id: enemy for enemy in old.enemies} if span > 0 and old.enemies is not seen.enemies else {}
        dives = sense.dives
        out = []
        landing = know.LANDING - params.LANDING_ROWS
        for enemy in [enemy for pair in fronts.values() for enemy in pair if enemy is not None]:
            kind = enemy.kind
            half, high = know.ENEMY_BOX[kind]
            pose = enemy.pose
            vx = vy = 0.0
            x = enemy.x
            if pose == "diving":
                track = dives.get(enemy.id)
                if track is not None:
                    x, y, vxl, vxh, vyl, vyh = predict.cone(track, kind, seen.wave, seen.profile, track.ya)
                    vx, vy = (vxl + vxh) / 2, (vyl + vyh) / 2
            else:
                before = then.get(enemy.id)
                if before is not None and before.pose == pose:
                    if kind == "weaver":
                        x = (enemy.x + before.x) / 2  # its zig-zag is a triangle: half a period apart, the slot
                    else:
                        vx = (enemy.x - before.x) / span
                        vy = (enemy.y - before.y) / span
            if pose in ("formation", "telegraph") and enemy.y >= landing:
                tier = 0
            elif pose == "telegraph" and kind in know.SHOOTERS and kind not in know.DIVERS:
                tier = 1
            else:
                tier = 3
            out.append(_new(Target, (enemy.id, x, enemy.y, vx, vy, half, high, know.hp(enemy), know.points(enemy),
                                     tier, False, pose, kind)))  # fmt: skip
        if seen.parts:
            thenp = {part.id: part for part in old.parts} if span > 0 else {}
            for part in seen.parts:
                if part.state == "broken":
                    continue
                before = thenp.get(part.id)
                vx = (part.x - before.x) / span if before is not None else 0.0
                shielded = part.state == "shielded"
                out.append(_new(Target, (part.id, part.x, part.y, vx, 0.0, part.w / 2, know.PART_HALF_Y, 99, 0,
                                         2, shielded, "", "part")))  # fmt: skip
        if seen.ufo is not None:
            x, y = seen.ufo
            vx = 0.0
            if old.ufo is not None and span > 0:
                vx = (x - old.ufo[0]) / span
            out.append(Target(-1, x, y, vx, 0.0, know.UFO_BOX[0], know.UFO_BOX[1], 1, 150, 4, False, "", "ufo"))
        out.sort(key=attrgetter("y"), reverse=True)  # the lowest first (ties keep their order)
        return out, base, f

    @staticmethod
    def meets(target, x, y, speed, base, f):
        """(steps, predicted x) for a shot at (x, y) going up at `speed` to reach the target's row."""
        ty = target.y + target.vy * base
        gap = y - (ty + target.high)
        if gap < 0:
            return None, None
        steps = ceil(gap / (speed + target.vy * f))
        steps = steps if steps > 1 else 1  # max(1, steps), without the call (this runs a few dozen times a decision)
        return steps, target.x + target.vx * (base + f * steps)

    def claims(self, frame, targets, base, f):
        """Damage already on its way: the first thing each own shot in flight is predicted to meet."""
        claimed = {}
        speed = self.bullet_speed(frame.ship)
        meets = self.meets
        for shot in frame.shots:
            if not shot.friendly:
                continue
            for target in targets:
                if target.y >= shot.y:
                    continue
                steps, x = meets(target, shot.x, shot.y, speed, base, f)
                if steps is not None and abs(shot.x - x) < target.half:
                    claimed[target.ident] = claimed.get(target.ident, 0) + 1
                    break
        return claimed

    def lane(self, x, frame, targets, claimed, base, f, cells) -> bool:
        """Fire from x hits something worth hitting: not a shielded part, not a doomed target, and not an own
        shelter, unless what is above it is about to land (the shots dig through)."""
        sheltered = self.sheltered(x, cells)
        speed = self.bullet_speed(frame.ship)
        slack, muzzle, meets = params.AIM_SLACK, know.MUZZLE_Y, self.meets
        met = []  # what a shot from x meets, first met first: a part in front of a shielded core takes the shot
        for target in targets:
            steps, tx = meets(target, x, muzzle, speed, base, f)
            if steps is not None and abs(x - tx) < target.half - slack:
                met.append((steps, -target.y, target))
        met.sort(key=itemgetter(0, 1))  # by steps, then the lowest first
        for _steps, _y, target in met:
            if target.block:
                return False
            if claimed.get(target.ident, 0) >= target.hp:
                continue
            return not sheltered or target.tier == 0 or self.dig
        return False

    def bullet_speed(self, ship) -> float:
        """know.bullet_speed(ship), worked out once for each Frame's ship (claims, lane and destination ask)."""
        if ship is not self._bullet[0]:
            self._bullet = (ship, know.bullet_speed(ship))
        return self._bullet[1]

    def cells(self, shelters) -> set:
        """The (x, y) of every standing shelter cell. observe() hands on the same tuple while no cell changes, so
        the set is built again only for a new one (the tuple is kept, so its identity cannot be reused)."""
        if shelters is not self._shelters:
            self._shelters, self._cell_set = shelters, {(x, y) for x, y, _tier in shelters}
        return self._cell_set

    def columns(self, cells) -> set:
        """The columns an own shelter cell stands in (worked out once per set of cells)."""
        if cells is not self._cells:
            rows = know.SHELTER_ROWS
            self._cells, self._columns = cells, {x for x, y in cells if y in rows}
        return self._columns

    def sheltered(self, x, cells) -> bool:
        column = round(x)
        return abs(column - x) < know.CELL and column in self.columns(cells)

    def pickup(self, frame):
        """The nearest falling pickup the ship can reach in time, else None."""
        sense = self.sense
        seen = sense.seen
        if not seen.pickups:
            return None
        ship = frame.ship
        reach, fall = know.catch_reach(ship)
        speed = know.ship_speed(ship)
        best, best_t = None, inf
        for pickup in seen.pickups:
            y = pickup.y + fall * (frame.tick - seen.tick)
            t = (know.SHIP_Y - know.PICKUP.catch[1] - y) / fall
            if t <= 0:
                continue
            if abs(pickup.x - ship.x) - (reach - params.PICKUP_SLACK) <= speed * t and t < best_t:
                best, best_t = pickup.x, t
        return best

    def destination(self, frame, targets, claimed, base, f, x0, speed, cells, current=None) -> float:
        """A curtain's gap, else a reachable pickup, else where to stand to shoot the best target. current: what is
        left of the plan being flown (a boss part that holds still may send the ship to where it ends)."""
        seen = self.sense.seen
        gaps = [hazard for hazard in seen.hazards if hazard.kind == "gap"]
        if gaps:
            gap = min(gaps, key=lambda hazard: abs((hazard.x0 + hazard.x1) / 2 - x0))
            return (gap.x0 + gap.x1) / 2
        dest = self.pickup(frame)
        if dest is not None:
            return dest
        bullet = self.bullet_speed(frame.ship)
        best, best_key, aim = None, None, None
        behind, behind_key, hidden = None, None, None  # the best that only a shot through an own shelter reaches
        self.dig = False
        muzzle, spot = know.MUZZLE_Y, self.spot
        slack, rounding, x_min, x_max = params.AIM_SLACK, params.ROUNDING, know.X_MIN, know.X_MAX
        for target in targets:
            if target.block or claimed.get(target.ident, 0) >= target.hp:
                continue
            if best_key is not None and (target.tier if target.tier < 3 else 3) > best_key[0]:
                continue  # its key can't come first, and with a best found the one behind a shelter isn't needed
            # Where to fire from to meet it, refined once for the time the ship takes to get there: meets(),
            # inline (its gap and steps).
            tx, ty, tvx, tvy, high = target.x, target.y, target.vx, target.vy, target.high
            rise = bullet + tvy * f
            fx = tx + tvx * base
            for _ in range(2):
                travel = abs(fx - x0) / speed
                gap = muzzle - (ty + tvy * (base + f * travel) + high)
                if gap < 0:
                    break
                steps = ceil(gap / rise)
                fx = tx + tvx * (base + f * (travel + (steps if steps > 1 else 1)))  # max(1, steps)
            if gap < 0:
                continue
            # Anywhere within `reach` of its predicted x hits it. A wide part fires from its middle, so stand at the
            # nearest x still under it; anything else, at its middle. Not behind an own shelter, unless the target
            # is about to land or nothing else can be hit: then the shots dig through.
            # (max() and min() as comparisons, here and above: the same values without the calls, which cost more
            # than all the rest of this loop's arithmetic.)
            reach = target.half - slack - rounding
            reach = reach if reach > 0.0 else 0.0  # max(0.0, reach)
            lo, hi = fx - reach, fx + reach
            lo, hi = lo if lo > x_min else x_min, hi if hi < x_max else x_max  # max(x_min, lo), min(x_max, hi)
            if lo > hi:
                continue
            start = x0 if target.kind == "part" else fx
            start = lo if lo > start else start  # max(start, lo)
            start = hi if hi < start else start  # min(start, hi)
            fx = spot(start, lo, hi, cells)
            covered = fx is None
            if covered:
                fx = start
            travel = abs(fx - x0) / speed
            if target.tier == 0:
                key = (0, -target.y, travel)
            elif target.tier in (1, 2):
                key = (target.tier, travel)
            else:
                cost = travel - 0.6 * (target.y - 2) - target.points / 10
                if target.pose == "diving":
                    cost -= 6
                if target.kind == "ufo":
                    cost += 6
                key = (3, cost)
            if covered and target.tier != 0:
                if behind_key is None or key < behind_key:
                    behind, behind_key, hidden = fx, key, target
            elif best_key is None or key < best_key:
                best, best_key, aim = fx, key, target
        if best is None and behind is not None:
            self.dig = True
            best, aim = behind, hidden
        if best is None:
            return x0
        if aim.kind == "part" and aim.vx == 0.0 and not self.lane(x0, frame, targets, claimed, base, f, cells):
            return self.stop_to_fire(frame, targets, claimed, base, f, x0, speed, cells, best, current)
        return best

    def stop_to_fire(self, frame, targets, claimed, base, f, x0, speed, cells, dest, current) -> float:
        """A boss part that holds still, and no shot from where the ship stands hits anything: the end of the plan
        nearest `dest` from which one does (ties to the shorter plan; what is left of the plan being flown first),
        else `dest`.

        The ship comes to rest up to half its smallest move from its destination (3 ticks of the library: 0.6-0.78
        cells; 16 ticks under latched controls), and the spots above allow a quarter cell: the nearest x under a
        wide part, or the nearest x clear of an own shelter cell. So it can stop where it never fires. A moving
        target soon gives it a new spot; a part that holds still never does (DREADNOUGHT's beam phase, its hull at
        the edge of its range while its beam tracks a ship beyond it), and the game would stand still to its cap.
        Every plan's end is checked, so the ship goes where it stops.

        An end counts only if the plan the planner flies toward it (choose() with no threat about) stops where a
        shot hits too. That need not be the plan that ends there: choose() counts a detour twice, so toward the end
        of a plan that runs into a wall and back it may fly a shorter plan, or stand. Hard seed 1041, wave 18: the
        ship stood at 8.25, and 'left 24 ticks, then right 14' ends at 8.44, under the core, by way of the wall at
        2.0. Toward 8.44 the planner ranked that plan at 12.5 and standing at 0.19, so the ship stood where no shot
        hits until the time cap (Hard 1019 too, and Endless 6032 at the right wall)."""
        keys, tick = self.keys, frame.tick
        candidates = [(plan if keys is None else keys.motion(plan, tick), size, 0)
                      for plan, size in zip(planner.PLANS, planner.LENGTHS, strict=True)]  # fmt: skip
        if current is not None:
            candidates.append((current if keys is None else keys.motion(current, tick), -1, -1))
        ends = sorted(
            (abs(end - dest), size, rank, end)
            for motion, size, rank in candidates
            for end in (planner.position(motion, x0, speed, sum(motion[1::2])),)
        )
        tried = {x0}
        for _away, _size, _rank, end in ends:
            if end in tried:
                continue
            tried.add(end)
            if not self.lane(end, frame, targets, claimed, base, f, cells):
                continue
            flown = planner.choose(NOTHING, x0, speed, end, current, keys, tick)
            motion = flown[0] if keys is None else flown[2]  # under latched controls, the motion its taps make
            stop = planner.position(motion, x0, speed, sum(motion[1::2]))
            if stop == end or (stop != x0 and self.lane(stop, frame, targets, claimed, base, f, cells)):
                return end
        return dest

    def spot(self, start, lo, hi, cells):
        """The x nearest `start` in [lo, hi], on a half-cell grid, that no own shelter covers within a grid step
        either side (the ship stops near its destination, not on it); else None."""
        reach = know.CELL + params.ROUNDING
        columns = self.columns(cells)
        if not columns or _clear(start, columns, reach):
            return start
        # Every point is nearer than reach to the column it rounds to (half a cell at most): with all of those
        # columns standing, none is clear.
        if reach > 0.5 and columns.issuperset(range(round(lo), round(hi) + 1)):
            return None
        points = {lo + 0.5 * index for index in range(int((hi - lo) / 0.5) + 1)} | {hi}
        # The nearest clear point; of two as near, the one the set gives first (as a stable sort of it would).
        best, nearest = None, inf
        for x in points:
            away = abs(x - start)
            if away < nearest and _clear(x, columns, reach):
                best, nearest = x, away
        return best


def _clear(x, columns, reach) -> bool:
    """No own shelter column covers x within `reach` (Skilled.spot)."""
    c = round(x)
    if c in columns and abs(c - x) < reach:
        return False
    return not ((c - 1 in columns and x - c + 1 < reach) or (c + 1 in columns and c + 1 - x < reach))


class NoBomb(Skilled):
    """Skilled play that never bombs, isolating what bombs contribute (no-escape moments are still logged)."""

    name = "nobomb"
    bombs = False


class BombHappy(Skilled):
    """Skilled play that bombs at the first threat it predicts (an exploit probe: it must not beat skilled)."""

    name = "bombhappy"
    happy = True


def skilled(delay: int, perceive: int) -> type:
    """Skilled play reacting `delay` ticks late and noticing `perceive` percent of enemy shots at first sight."""
    name = f"sk_d{delay}_p{perceive}"
    return type(name, (Skilled,), {"name": name, "delay": delay, "perceive": perceive / 100})
