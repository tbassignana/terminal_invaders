"""Perception (spec §18): what a bot has seen, from a ring buffer of Frames.

The bot's own ship, its shots, the shelters, the card and the prompt come from the current Frame; enemies, enemy
shots, boss parts, hazards, pickups and the UFO from the Frame of `delay` ticks ago. Every time here is a Frame
tick; "seen" times are the delayed Frame's.

Motion is measured on the enemies' own clock: SLOW runs it at 0.6, the hit freeze and the phases outside play stop
it. The clock comes from the current Frames (the ship's pickups, its lives, the phase), so a track spanning a SLOW
pickup or a freeze stays one straight line.
"""

from collections import deque

from . import know, params

MASK = (1 << 64) - 1


def unit(seed: int, ident: int) -> float:
    """A uniform draw in [0, 1) keyed by (seed, id) alone (splitmix64), so cadence and pruning never change it."""
    z = (seed * 0x9E3779B97F4A7C15 + ident * 0xBF58476D1CE4E5B9 + params.LAPSE_SALT) & MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
    return ((z ^ (z >> 31)) >> 11) / float(1 << 53)


class Track:
    """Every sighting of one id since it was first noticed, as running least-squares sums on the enemies' clock.

    `prior` is what the rules say before the track is long enough: (vx, vy, span) for a cluster's pellet (the
    split's vx, the cluster's vy) or a dive whose launch was seen (the dive rule). `still` marks a dive's track
    started again because it stood still (a freeze the bot could not see): its first move starts it once more."""

    __slots__ = (
        "kind",
        "t0",
        "w0",
        "n",
        "sw",
        "sx",
        "sy",
        "sww",
        "swx",
        "swy",
        "t1",
        "w1",
        "x1",
        "y1",
        "prior",
        "late",
        "ya",
        "ws",
        "still",
    )

    def __init__(self, kind, t, w, x, y, prior=None, late=False):
        self.kind, self.t0, self.w0, self.prior, self.late, self.ya = kind, t, w, prior, late, y
        self.n = 0
        self.sw = self.sx = self.sy = self.sww = self.swx = self.swy = 0.0
        self.still = False
        self.add(t, w, x, y)

    def add(self, t, w, x, y):
        if not self.n or x != self.x1 or y != self.y1:
            if self.still:
                # A dive frozen unseen moves again. The bot's clock ran through the freeze, so its still sightings
                # sit at the wrong enemy times and would flatten the slope: the track starts at this sighting.
                self.t0, self.w0, self.ya, self.n, self.still = t, w, y, 0, False
                self.sw = self.sx = self.sy = self.sww = self.swx = self.swy = 0.0
            self.ws = w  # the clock when it reached this place
        d = w - self.w0
        self.n += 1
        self.sw += d
        self.sx += x
        self.sy += y
        self.sww += d * d
        self.swx += d * x
        self.swy += d * y
        self.t1, self.w1, self.x1, self.y1 = t, w, x, y

    def fit(self):
        """(x, y) on the fitted line at the last sighting, the slopes (vx, vy) per enemy tick, and the span in
        enemy ticks; slopes are None for a track seen at one instant only."""
        n = self.n
        span = self.w1 - self.w0
        if n < 2 or span <= 1e-9:
            return self.x1, self.y1, None, None, 0.0
        den = n * self.sww - self.sw * self.sw
        if den <= 1e-12:
            return self.x1, self.y1, None, None, 0.0
        vx = (n * self.swx - self.sw * self.sx) / den
        vy = (n * self.swy - self.sw * self.sy) / den
        lag = span - self.sw / n
        return self.sx / n + vx * lag, self.sy / n + vy * lag, vx, vy, span


class Perception:
    """The ring buffer, the enemies' clock, tracks, noticing (lapse and tells), dive tells and hazard marks."""

    def __init__(self, delay: int, perceive: float, seed: int):
        self.delay, self.perceive, self.seed = delay, perceive, seed
        size = delay + params.VELOCITY_SPAN + 2
        self.frames = deque(maxlen=size)
        self.clock = {}  # tick -> the enemies' clock at that Frame
        self.ship_x = {}  # tick -> the ship's x in that (current) Frame
        self.keep = size + 256
        self.now = None  # the current Frame
        self.seen = None  # the delayed Frame
        self.prev_seen = None
        self.lives = None
        self.blinking = False
        self.guarded = False
        self.frozen_until = -1  # the hit freeze: steps up to this tick move nothing
        self.notice = {}  # shot id -> the seen tick it is noticed at (lapsed shots: 30 later)
        self.lapsed = deque()  # (noticed tick, id) of shots noticed late, in order
        self.late = set()  # ids noticed late
        self.shots = {}  # shot id -> Track (noticed enemy shots)
        self.dives = {}  # enemy id -> Track (diving divers and mites)
        self.tells = {}  # enemy id -> [first seen telegraphing, last seen telegraphing, x, y, kind]
        self.part_tells = {}  # part id -> [first seen charging, last seen charging, x, y, w]
        self.marks = {}  # hazard id -> [the clock when first seen, when first seen live, kind]
        self.launches = {}  # enemy id -> (seen tick it launched, x, y) for dives whose wobble was seen to end
        self.priors = {}  # pellet id -> (vx, vy, span) inherited from the cluster it split from
        self.scanned = -1  # the last delayed tick the enemies were scanned at
        self.alarm = False  # a threat was noticed since the last decision
        self.resets = 0

    # --- every tick ------------------------------------------------------------------------------------------------

    def see(self, frame) -> None:
        tick = frame.tick
        prev = self.now
        self.now = frame
        ship = frame.ship
        self.ship_x[tick] = ship.x
        if prev is not None and frame.phase == "playing" and prev.phase == "playing":
            if self.lives is not None and ship.lives < self.lives:
                self.frozen_until = tick + know.FREEZE  # the hit freeze: then the mercy clear
            elif ship.blinking and not self.blinking and not (self.guarded and not ship.guarded):
                if ship.bombs >= prev.ship.bombs:  # not our bomb, not an absorb: a mercy clear (immortal)
                    self.reset()
        self.lives, self.blinking, self.guarded = ship.lives, ship.blinking, ship.guarded
        # The enemies' clock for the step that made this Frame: stopped outside play and in the hit freeze.
        last = self.clock.get(tick - 1)
        if last is None:
            self.clock[tick] = float(tick)
        else:
            step = 0.0
            if (
                prev is not None
                and prev.phase == "playing"
                and not self.frozen_until - know.FREEZE < tick <= self.frozen_until
            ):
                step = know.SLOW if know.timed(prev.ship, "slow") else 1.0
            self.clock[tick] = last + step
        old = tick - self.keep
        self.clock.pop(old, None)
        self.ship_x.pop(old, None)
        self.frames.append(frame)
        self.prev_seen = self.seen
        index = len(self.frames) - 1 - self.delay
        self.seen = self.frames[index if index >= 0 else 0]
        self._notice()
        if (self.shots or self.dives) and self.seen is not self.prev_seen:
            self._sight()

    def _sight(self) -> None:
        """Every Frame's sighting goes into a track until it spans DENSE enemy ticks; after that, the decisions'
        Frames do (a slope from 15 ticks of 0.25-cell rounding needs the in-between Frames; from 30, it does not)."""
        seen = self.seen
        tick = seen.tick
        w = self.clock.get(tick, float(tick))
        dense = params.DENSE
        tracks = self.shots
        if tracks and seen.shots is not (self.prev_seen.shots if self.prev_seen is not None else None):
            for shot in seen.shots:
                if shot.friendly:  # its own shots have no track (ids are never reused)
                    continue
                track = tracks.get(shot.id)
                if track is not None and track.t1 < tick and track.w1 - track.w0 < dense:
                    track.add(tick, w, shot.x, shot.y)
        dives = self.dives
        if dives:
            for enemy in seen.enemies:
                if enemy.pose != "diving":
                    continue
                track = dives.get(enemy.id)
                if track is not None and track.t1 < tick and track.w1 - track.w0 < dense:
                    if track.x1 != enemy.x or track.y1 != enemy.y:
                        track.add(tick, w, enemy.x, enemy.y)

    def reset(self) -> None:
        """Forget every track (after a freeze the bot could not tell from its lives)."""
        self.shots.clear()
        self.dives.clear()
        self.resets += 1

    def _notice(self) -> None:
        seen, prev = self.seen, self.prev_seen
        tick = seen.tick
        if prev is None or seen.enemies is not prev.enemies:
            self._tells(seen)
        if seen.parts and (prev is None or seen.parts is not prev.parts):
            self._part_tells(seen)
        if seen.hazards is not (prev.hazards if prev is not None else None):
            w = self.clock_at(tick)
            for hazard in seen.hazards:
                mark = self.marks.get(hazard.id)
                if mark is None:
                    self.marks[hazard.id] = [w, w if hazard.live else None, hazard.kind]
                    self.alarm = True
                elif hazard.live and mark[1] is None:
                    mark[1] = w
                    self.alarm = True
        if prev is None or seen.shots is not prev.shots:
            notice = self.notice
            fresh = None
            for shot in seen.shots:
                if shot.friendly or shot.id in notice:
                    continue
                fresh = fresh or []
                fresh.append(shot)
            if fresh:
                for shot in fresh:
                    late = self.perceive < 1.0 and not self._told(shot, tick)
                    late = late and unit(self.seed, shot.id) >= self.perceive
                    if late:
                        notice[shot.id] = tick + params.LATE
                        self.lapsed.append((tick + params.LATE, shot.id))
                        self.late.add(shot.id)
                    else:
                        notice[shot.id] = tick
                        self.alarm = True
                self._inherit(fresh, seen)
        lapsed = self.lapsed
        while lapsed and lapsed[0][0] <= tick:
            lapsed.popleft()
            self.alarm = True

    def _tells(self, seen) -> None:
        """Tell poses as they start and end. The enemies tuple is rescanned whenever it changes, so a pose that
        was there at the last scan has been there ever since."""
        tick, last = seen.tick, self.scanned
        tells = self.tells
        for enemy in seen.enemies:
            pose = enemy.pose
            if pose == "telegraph":
                tell = tells.get(enemy.id)
                if tell is None or tell[1] != last:
                    tells[enemy.id] = [tick, tick, enemy.x, enemy.y, enemy.kind]
                    self.launches.pop(enemy.id, None)  # a new wobble: its last dive is over
                    if enemy.kind in know.DIVERS:
                        self.alarm = True
                else:
                    tell[1], tell[2], tell[3] = tick, enemy.x, enemy.y
            elif pose == "diving" and enemy.id not in self.launches:
                tell = tells.get(enemy.id)
                if tell is not None and tell[1] == last:
                    # the first Frame of the dive shows where it launched from, at the ship's x then
                    self.launches[enemy.id] = (tick, enemy.x, enemy.y)
                    self.alarm = True
        self.scanned = tick
        if len(tells) > 64:
            for ident in [i for i, tell in tells.items() if tell[1] < tick - params.TELL_MEMORY]:
                del tells[ident]
        if len(self.launches) > 64:
            alive = {enemy.id for enemy in seen.enemies}
            for ident in [i for i in self.launches if i not in alive]:
                del self.launches[ident]

    def _part_tells(self, seen) -> None:
        tick = seen.tick
        for part in seen.parts:
            if part.state == "charge":
                tell = self.part_tells.get(part.id)
                if tell is None or tell[1] < tick - 1:
                    self.part_tells[part.id] = [tick, tick, part.x, part.y, part.w]
                else:
                    tell[1], tell[2], tell[3] = tick, part.x, part.y

    def _told(self, shot, tick) -> bool:
        """A shot first seen within TELL_REACH below a shooter that began its tell TELL_LEAD or more ticks ago."""
        reach, lead, memory = params.TELL_REACH, params.TELL_LEAD, params.TELL_MEMORY
        for start, end, x, y, kind in self.tells.values():
            if kind in know.DIVERS or start > tick - lead or end < tick - memory:
                continue
            if abs(shot.x - x) <= reach and 0.0 <= shot.y - y <= reach:
                return True
        for start, end, x, y, w in self.part_tells.values():
            if start > tick - lead or end < tick - memory:
                continue
            if abs(shot.x - x) <= w / 2 + reach and 0.0 <= shot.y - y <= reach + 1.0:
                return True
        return False

    def _inherit(self, fresh, seen) -> None:
        """A cluster that vanished where pellets appeared split: they inherit its vy and the split's vx."""
        clusters = [(ident, track) for ident, track in self.shots.items() if track.kind == "cluster"]
        if not clusters:
            return
        here = {shot.id for shot in seen.shots}
        pellets = [shot for shot in fresh if shot.kind == "pellet"]
        split = know.SHOTS["cluster"].split
        for ident, track in clusters:
            if ident in here:
                continue
            x, y, _vx, vy, span = track.fit()
            if vy is None:
                continue
            w = self.clock.get(seen.tick, track.w1)
            y += vy * (w - track.w1)
            near = sorted(s.id for s in pellets if abs(s.x - x) <= 0.75 and abs(s.y - y) <= 0.75)
            if len(near) == len(split):
                for pellet, vx in zip(near, split, strict=True):
                    self.priors[pellet] = (vx, vy, span)
            del self.shots[ident]

    # --- at a decision ----------------------------------------------------------------------------------------------

    def ingest(self) -> None:
        """Add the delayed Frame's sightings to the tracks of every noticed shot and diving enemy; drop the gone."""
        seen = self.seen
        tick = seen.tick
        w = self.clock.get(tick, float(tick))
        notice, tracks = self.notice, self.shots
        priors = self.priors
        for shot in seen.shots:
            if shot.friendly:
                continue
            track = tracks.get(shot.id)
            if track is not None:
                if track.t1 < tick:
                    track.add(tick, w, shot.x, shot.y)
            elif notice.get(shot.id, tick + 1) <= tick:
                tracks[shot.id] = Track(
                    shot.kind, tick, w, shot.x, shot.y, priors.pop(shot.id, None), shot.id in self.late
                )
        if len(tracks) > 0:
            gone = [ident for ident, track in tracks.items() if track.t1 < tick]
            for ident in gone:
                del tracks[ident]
        dives = self.dives
        for enemy in seen.enemies:
            if enemy.pose != "diving":
                continue
            track = dives.get(enemy.id)
            if track is not None:
                if track.t1 == tick:
                    continue
                if track.x1 == enemy.x and track.y1 == enemy.y and w - track.ws >= params.STILL:
                    # it stood still while the clock ran: a freeze the bot could not see (a boss's phase break);
                    # start again. Counted from when it got here, since under SLOW one decision is under STILL.
                    dives[enemy.id] = Track(enemy.kind, tick, w, enemy.x, enemy.y, track.prior)
                    dives[enemy.id].still = True
                else:
                    track.add(tick, w, enemy.x, enemy.y)
            else:
                dives[enemy.id] = Track(enemy.kind, tick, w, enemy.x, enemy.y, self._launch(enemy, tick))
        if dives:
            gone = [ident for ident, track in dives.items() if track.t1 < tick]
            for ident in gone:
                del dives[ident]
        if len(notice) > 512:
            live = {shot.id for shot in seen.shots}
            for ident in [i for i, t in notice.items() if i not in live and t < tick - params.LATE]:
                del notice[ident]
                self.late.discard(ident)
        if len(self.priors) > 64:
            live = {shot.id for shot in seen.shots}
            for ident in [i for i in self.priors if i not in live]:
                del self.priors[ident]
        if len(self.marks) > 32:
            live = {hazard.id for hazard in seen.hazards}
            for ident in [i for i in self.marks if i not in live]:
                del self.marks[ident]
        self.alarm = False

    def _launch(self, enemy, tick):
        """The dive rule for a dive whose launch was seen: from the wobble's place, at the ship's x then."""
        launch = self.launches.get(enemy.id)
        if launch is None:
            return None
        ship_x = self.ship_x.get(launch[0])
        if ship_x is None:
            return None
        vy = know.dive_vy(enemy.kind, self.now.profile)
        return know.dive_vx(ship_x, launch[1], launch[2], vy), vy, float(params.YOUNG)

    def clock_at(self, tick: int) -> float:
        return self.clock.get(tick, float(tick))

    def old(self, ticks: int):
        """The Frame `ticks` before the delayed one (or the oldest kept)."""
        index = len(self.frames) - 1 - self.delay - ticks
        return self.frames[index if index >= 0 else 0]
