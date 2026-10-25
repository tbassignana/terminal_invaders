"""The campaign-2 Game (spec §3.2, §14): one deterministic tick per `step`, no terminal or clock I/O.

The tick runs §14.2's eleven steps in order: actions and edges; the phase work outside `playing`; controls; player
shots; the fleet (fleet.py); the boss (boss.py); the fire director (director.py); enemy shots; rams, beams,
pickups and the UFO; the resolve (at most one life a tick); and the phase checks. Every gameplay timer advances only
in `playing` (F7), and the hit freeze and boss breaks hold the schedule.
"""

import hashlib
import json
import math
from dataclasses import asdict, replace
from typing import NamedTuple

from ..model import ACTIONS, CAMPAIGN_WAVES, HEIGHT, MAX_EFFECTS, RULESET, WIDTH, Config, Effect, Pickup, Player, Shot
from ..model import UFO as Saucer
from ..rng import Rng
from . import boss as bosses
from . import director, endless, fleet
from .collide import buckets, first_cell, near, segment_hit
from .tables import (
    ACTS,
    ARRIVAL,
    BOMB,
    BOSSES,
    COMBO,
    ENDLESS,
    ENEMIES,
    FAMILIES,
    FIRE,
    LIMITS,
    PICKUP,
    PICKUPS,
    PLAYER,
    PROFILES,
    SALTS,
    SCORING,
    SHELTERS,
    SHOTS,
    TIMING,
    UFO,
    UPGRADES,
    WAVES,
)

ACTION_ORDER = ("left", "right", "fire", "bomb")  # the order of `_prev` and edges; autofire is accepted and ignored
TERMINAL = ("game_over", "victory")
REACH = max(kind.box[0] for kind in ENEMIES.values())  # the widest enemy hit box, for the x-bucket lookup
NEIGHBOURS = ((-1, 0), (1, 0), (0, -1), (0, 1))  # a slug's splash
TIMED = ("rapid", "spread", "slow")  # timed pickups that stack (SHIELD lasts until hit, at most its ticks)
RING, RING_TICKS = "()", 30  # the bomb's Effect: P draws a ring growing from the ship to the shockwave's reach

# Every Game attribute is in exactly one of these two (tests/test_engine.py names any that isn't).
DIGEST_FIELDS = (
    "config",
    "difficulty",
    "rng",
    "rng_drop",
    "rng_offer",
    "tick",
    "phase",
    "wave",
    "act",
    "loop",
    "player",
    "aliens",
    "shots",
    "pickups",
    "ufo",
    "bunkers",
    "boss",
    "hazards",
    "score",
    "kills",
    "shots_fired",
    "hits",
    "combo",
    "best_combo",
    "chain",
    "combo_timer",
    "bomb_charge",
    "bombs_this_act",
    "powerups",
    "upgrades",
    "offers",
    "cursor",
    "armed",
    "prompt",
    "continues",
    "modifiers",
    "staged",
    "last_stand_used",
    "bosses",
    "perfect_waves",
    "continues_used",
    "bomb_kills",
    "bombs_used",
    "picks",
    "_prev",
    "_serial",
    "_clock",
    "_play",
    "_next_flight",
    "_wave_hit",
    "_director",
    "_immortal",
    "_fire",
    "_flights",
    "_pending",
    "_freeze",
    "_lockout",
    "_gate",
    "_pvol",
    "_pierce",
    "_dclock",
    "_volleys",
    "_dive_clock",
    "_ufo_at",
    "_ufo_done",
    "_wave_shots",
    "_sweeps",
    "_bonus",
    "_march",
    "_brood",
    "_parent",
    "_attacks",
    "_motion",
    "_hits",
    "_landed",
    "_dirty",
)
# Presentation: never hashed, never read by the rules. `shelter_rev` counts shelter changes and `_seen` is
# observe()'s cache.
PRESENTATION = (
    "wave_name",
    "intro",
    "notice",
    "notes",
    "effects",
    "events",
    "hit_cause",
    "fired",
    "shelter_rev",
    "_seen",
)


class Fired(NamedTuple):
    """One new threat in `Game.fired` (for the judge and the tests, never the bots)."""

    id: int  # the shot's or the enemy's id
    kind: str  # a shot kind, "dive" or "mite"
    cls: str  # safe, reaction, dive, mite or boss
    w: int  # warning ticks: telegraph + flight to the ship's row (F1)
    flight: int  # flight ticks to the ship's row
    y: float  # spawn row
    pose: str  # the source's pose when it fired (POSES), "" for a boss part
    source: str  # the source's kind (gunner, diver, mite, ...) or the boss part's name


def held_interval(slots: int = FIRE.slots, speed: float = FIRE.speed) -> float:
    """Ticks between volleys while Space is held with nothing to hit: a slot frees when its shot leaves the top."""
    return max(FIRE.gate, math.ceil((FIRE.muzzle_y - FIRE.exit_y) / speed) / slots)


def script_wave(number: int) -> tuple[int, int]:
    """(the WAVES row number, the loop) a wave number plays; after wave 24, Endless loops ENDLESS.loop."""
    if number <= CAMPAIGN_WAVES:
        return number, 1
    first, last = ENDLESS.loop
    span = last - first + 1
    index = number - CAMPAIGN_WAVES - 1
    return first + index % span, 2 + index // span


def roman(number: int) -> str:
    """Roman numerals for act and loop marks (1 and up)."""
    out = ""
    for value, letters in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L")):
        while number >= value:
            out, number = out + letters, number - value
    for value, letters in ((40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while number >= value:
            out, number = out + letters, number - value
    return out


def combo_for(chain: int) -> int:
    """The multiplier: kills 1-3 score x1, 4-6 x2, and so on, up to x6 (§10.3)."""
    return 1 if chain <= 0 else min(COMBO.most, 1 + (chain - 1) // COMBO.step)


class Game:
    """Owns every gameplay value, including three private, replayable random streams."""

    def __init__(self, config: Config = Config()):
        profile = PROFILES[config.difficulty]
        self.config = config
        self.difficulty = profile.speed
        self.rng = Rng(config.seed ^ SALTS.rng)
        self.rng_drop = Rng(config.seed ^ SALTS.drop)
        self.rng_offer = Rng(config.seed ^ SALTS.offer)
        self.tick = 0
        self.phase = "wave"
        self.wave = 0
        self.act = 1
        self.loop = 1
        self.player = Player(x=PLAYER.start_x, y=PLAYER.y, lives=profile.lives, bombs=profile.bombs, weapon=FIRE.slots)
        self.aliens = []
        self.shots = []
        self.pickups = []
        self.ufo = None
        self.bunkers = {}
        self.boss = None
        self.hazards = []
        self.score = self.kills = self.shots_fired = self.hits = 0
        self.combo, self.best_combo, self.chain, self.combo_timer = 1, 0, 0, 0
        self.bomb_charge = self.bombs_this_act = 0
        self.powerups = {}
        self.upgrades = {}
        self.offers = ()
        self.cursor = 1
        self.armed = 0
        self.prompt = 0
        self.continues = profile.continues if config.mode != "endless" else 0
        self.modifiers = []
        self.staged = False
        self.last_stand_used = False
        self.bosses = self.perfect_waves = self.continues_used = self.bomb_kills = self.bombs_used = 0
        self.picks = ()
        self._prev = ()
        self._serial = 0
        self._clock = 0  # ticks left in the current intro or clear
        self._play = 0.0  # play ticks since the wave started (x0.6 under SLOW): the schedule's and the UFO's clock
        self._next_flight = 0  # the wave's flights that have arrived (the next one's index)
        self._wave_hit = False  # a life was lost this wave (no PERFECT)
        self._director = {}  # staged() overrides of every wave's director row
        self._immortal = False
        self._fire = WAVES[0].director  # this wave's director row, with the overrides
        self._flights = []  # fleet.FlightState per flight that has arrived
        self._pending = []  # (play tick, key) of the flights still to arrive, in order
        self._freeze = 0  # the hit freeze's ticks left
        self._lockout = 0  # ticks of no enemy fire left (after the mercy clear or a bomb)
        self._gate = 0  # ticks until the next player volley may fire
        self._pvol = {}  # player volley id -> [shots in flight, hit, neutral]
        self._pierce = {}  # player shot id -> enemies it may still fly on through (PIERCER)
        self._dclock = 0.0  # the director's reload clock
        self._volleys = []  # director.Volley in progress
        self._dive_clock = 0.0
        self._ufo_at = 0  # the play tick the UFO may appear at (0: not this wave)
        self._ufo_done = False
        self._wave_shots = 0  # player volleys this wave (the UFO's 300 rule)
        self._sweeps = 0  # SWEEPs this wave
        self._bonus = 0  # SWEEP points this wave (the clear panel's total)
        self._march = 0  # the last march cue (1-4)
        self._brood = {}  # carrier id -> [launch clock, its mites alive]
        self._parent = {}  # mite id -> the carrier that launched it
        self._attacks = []  # boss.AttackState of the current boss phase
        self._motion = bosses.Motion()
        self._hits = []  # this tick's queued hits (their causes), resolved at step 10
        self._landed = []  # this tick's landed flights
        self._dirty = False  # an enemy was removed this step (the list is filtered once)
        self.wave_name = ""
        self.intro = ()
        self.notice = ""
        self.notes = []
        self.effects = []
        self.events = []
        self.hit_cause = ""
        self.fired = []
        self.shelter_rev = 0
        self._seen = None
        self._start_wave(1)

    # --- the tick (§14.2) ---------------------------------------------------------------------------------------------

    def step(self, actions: tuple[str, ...] = ()) -> None:
        """Apply this tick's held actions and advance exactly one tick; terminal phases don't tick."""
        if self.phase in TERMINAL:
            return
        actions = tuple(actions)
        unknown = [action for action in actions if action not in ACTIONS]
        if unknown:
            raise ValueError(f"Unknown actions: {sorted(dict.fromkeys(str(action) for action in unknown))}")
        held = tuple(name for name in ACTION_ORDER if name in actions)
        edges = tuple(name for name in held if name not in self._prev)
        self.tick += 1
        self.events = []
        self.fired = []
        phase = self.phase
        if phase == "playing":
            self._playing(held, edges)
        elif phase == "wave":
            self._move(held)
            if self.boss is not None and self.boss.state == "intro":
                bosses.intro(self)
            self._clock -= 1
        elif phase == "clear":
            self._move(held)
            if self.pickups:
                self._fall_pickups()
            self._clock -= 1
        elif phase == "upgrade":
            self._card(edges)
        elif phase == "continue":
            self._continue_prompt(edges)
        self._phase_checks()
        if self.effects:
            for effect in self.effects:
                effect.ttl -= 1
            self.effects = [effect for effect in self.effects if effect.ttl > 0]
        if self.notes:
            self.notes = [(text, ticks - 1) for text, ticks in self.notes if ticks > 1]
        self._prev = held

    def _playing(self, held, edges):
        if self._freeze:  # the hit freeze: effects only, then the mercy clear
            self._freeze -= 1
            if not self._freeze:
                self._mercy()
            return
        slow = PICKUPS["slow"].factor if self.powerups.get("slow") else 1.0
        self._move(held)  # 3. controls
        if self._gate:
            self._gate -= 1
        if "fire" in held:
            self._fire_volley()
        if "bomb" in edges:
            self._bomb()
        if self.shots:  # 4. player shots
            self._player_shots()
        frozen = bosses.frozen(self)
        if frozen:
            bosses.step(self, slow)
        else:
            self._play += slow
            fleet.step(self, slow)  # 5. schedule and fleet
            self._purge()
            if self.boss is not None:  # 6. boss
                bosses.step(self, slow)
                self._purge()
            director.step(self, slow)  # 7. director
            if self.shots:  # 8. enemy shots
                self._enemy_shots(slow)
            self._rams()  # 9. rams, beams, pickups, the UFO
            if self.hazards and bosses.beam_hits(self):
                self._hits.append("beam")
        if self.pickups:
            self._fall_pickups()
        if self._ufo_at or self.ufo is not None:
            self._ufo_step(slow)
        self._resolve()  # 10. resolve

    def _move(self, held):
        speed = PLAYER.speed + self.upgrades.get("THRUSTERS", 0) * UPGRADES["THRUSTERS"].value
        move = ("right" in held) - ("left" in held)
        x = self.player.x
        self.player.x = max(PLAYER.x_min, min(PLAYER.x_max, x + speed * move))
        self.player.vx = self.player.x - x

    # --- player fire (§5) ----------------------------------------------------------------------------------------------

    def _fire_volley(self):
        """Held fire fires whenever a slot is free, outside the gate (one SPREAD volley holds one slot)."""
        if self._gate or len(self._pvol) >= self.player.weapon:
            return
        speed = FIRE.speed + self.upgrades.get("RAIL ROUNDS", 0) * UPGRADES["RAIL ROUNDS"].value
        if self.powerups.get("rapid"):
            speed *= PICKUPS["rapid"].factor
        spread = FIRE.spread if self.powerups.get("spread") else (0.0,)
        pierce = int(UPGRADES["PIERCER"].value) if "PIERCER" in self.upgrades else 0
        volley = 0
        for vx in spread:
            shot = Shot(self.player.x, FIRE.muzzle_y, True, vx, -speed, FIRE.damage, self._new_id(), "player")
            volley = volley or shot.id
            shot.volley = volley
            self.shots.append(shot)
            if pierce:
                self._pierce[shot.id] = pierce
        self._pvol[volley] = [len(spread), False, False]
        self._gate = FIRE.gate
        self.shots_fired += 1
        self._wave_shots += 1
        self.events.append("fire")

    def _player_shots(self):
        """Step 4: player shots move and strike the first thing their sweep enters (enemies, parts, UFO, cells)."""
        table = None
        keep = []
        for shot in self.shots:
            if not shot.friendly:
                keep.append(shot)
                continue
            if table is None:
                table = buckets(self.aliens) if self.aliens else {}
            if not self._fly(shot, table):
                keep.append(shot)
        self.shots = keep
        self._purge()

    def _fly(self, shot, table) -> bool:
        """Move one player shot; True when it ends this tick."""
        x0, y0 = shot.x, shot.y
        x1, y1 = x0 + shot.vx, y0 + shot.vy
        while True:
            best, target, what = 2.0, None, ""
            if table:
                for alien in near(table, x0, x1, REACH):
                    if alien.hp <= 0:
                        continue
                    rx, ry = ENEMIES[alien.kind].box
                    enter = segment_hit(x0, y0, x1, y1, alien.x, alien.y, rx, ry)
                    if enter is not None and enter < best:
                        best, target, what = enter, alien, "alien"
            boss = self.boss
            if boss is not None and y1 < boss.y + 6:
                for part in boss.parts:
                    if part.state == "broken":
                        continue
                    enter = segment_hit(x0, y0, x1, y1, part.x, part.y, part.w / 2, 0.45)
                    if enter is not None and enter < best:
                        best, target, what = enter, part, "part"
            ufo = self.ufo
            if ufo is not None and y1 < ufo.y + 1:
                enter = segment_hit(x0, y0, x1, y1, ufo.x, ufo.y, UFO.box[0], UFO.box[1])
                if enter is not None and enter < best:
                    best, target, what = enter, ufo, "ufo"
            if self.bunkers and y1 <= SHELTERS.top + 3 and y0 >= SHELTERS.top - 1:
                enter, cell = first_cell(self.bunkers, x0, y0, x1, y1)
                if cell is not None and enter < best:
                    best, target, what = enter, cell, "cell"
            if target is None:
                break
            if what == "alien":
                self._mark(shot, "hit")
                alien = target
                alien.hp -= shot.damage
                if alien.hp > 0:
                    self.events.append("hit")
                    self._end_shot(shot)
                    return True
                self._kill(alien)
                left = self._pierce.get(shot.id, 0)
                if left <= 0:
                    self._end_shot(shot)
                    return True
                self._pierce[shot.id] = left - 1
                continue  # it flies on through
            if what == "part":
                self._mark(shot, bosses.damage(self, target))
            elif what == "ufo":
                self._mark(shot, "hit")
                self._ufo_kill()
            else:
                self._strike(target, 1)
            self._end_shot(shot)
            return True
        shot.x, shot.y = x1, y1
        if y1 < FIRE.exit_y:
            self._end_shot(shot)
            return True
        return False

    def _mark(self, shot, outcome):
        """A shot hit something (counted once per volley) or was absorbed by a shielded part (neutral)."""
        entry = self._pvol.get(shot.volley)
        if entry is None:
            return
        if outcome == "hit":
            if not entry[1]:
                entry[1] = True
                self.hits += 1
        else:
            entry[2] = True

    def _end_shot(self, shot):
        """A player shot ends; its volley's slot frees with its last shot, a miss if none hit (or was neutral)."""
        self._pierce.pop(shot.id, None)
        entry = self._pvol.get(shot.volley)
        if entry is None:
            return
        entry[0] -= 1
        if entry[0] <= 0:
            del self._pvol[shot.volley]
            if not entry[1] and not entry[2]:
                self._miss()

    def _miss(self):
        if "CHAIN LINK" in self.upgrades:
            self.chain = max(0, self.chain - int(UPGRADES["CHAIN LINK"].value))
        else:
            self.chain = 0
        self.combo = combo_for(self.chain)

    def _break_chain(self):
        self.chain, self.combo = 0, 1

    # --- kills, score, drops and the bomb charge (§10) -----------------------------------------------------------------

    def _kill(self, alien, shot=True):
        """An enemy dies: points (x combo for shot kills), the chain, the charge and one drop draw; or a bomb kill."""
        kind = ENEMIES[alien.kind]
        points = kind.points
        if alien.veteran:
            points = math.floor(points * SCORING.veteran)
        if shot:
            if alien.state == "diving":
                points *= kind.dive_points
            self._chain_kill()
            points *= self.combo
            self._drop(alien.x, alien.y, alien.cargo)
        else:
            self.bomb_kills += 1
            self.events.append("bomb_kill")
        self.score += points
        self.kills += 1
        self.events.append("kill")
        self._effect(alien.x, alien.y, f"+{points}")
        x, y = alien.x, alien.y
        self._gone(alien, True)
        if kind.release:
            spread = fleet.DIVE.mite_spread
            count = PROFILES[self.config.difficulty].mites
            for index in range(count):
                fleet.release_mite(self, x + spread * (2 * index - (count - 1)), y, early=True)  # steps 3-4

    def _chain_kill(self):
        """A shot kill (the UFO's too): the chain grows, the multiplier follows, and the bomb charges."""
        self.chain += 1
        self.combo = combo_for(self.chain)
        self.best_combo = max(self.best_combo, self.combo)
        need = self._charge_needed()
        self.bomb_charge = min(need, self.bomb_charge + self.combo)
        self._bank()

    def _charge_needed(self):
        return int(UPGRADES["CAPACITOR"].value) if "CAPACITOR" in self.upgrades else BOMB.charge

    def _bomb_cap(self):
        return int(UPGRADES["BOMB BAY"].value) if "BOMB BAY" in self.upgrades else BOMB.cap

    def _per_act(self):
        """Bombs the charge and BOMB pickups may add this act (none before BOMB.first_act: B-F's step 1)."""
        if self.act < BOMB.first_act:
            return 0
        return int(UPGRADES["BOMB BAY"].extra) if "BOMB BAY" in self.upgrades else BOMB.per_act

    def _can_add_bomb(self):
        return self.player.bombs < self._bomb_cap() and self.bombs_this_act < self._per_act()

    def _bank(self):
        """A full charge adds a bomb when one can be added; otherwise it holds at full (§5)."""
        if self.bomb_charge >= self._charge_needed():
            self.bomb_charge = self._charge_needed()
            if self._can_add_bomb():
                self.player.bombs += 1
                self.bombs_this_act += 1
                self.bomb_charge = 0
                self._effect(self.player.x, self.player.y - 1, "BOMB READY")
                self.notes.append(("BOMB READY", TIMING.notice))

    def _drop(self, x, y, guaranteed):
        """One rng_drop draw per shot kill, used or not: a cargo enemy always drops, the rest at the profile's rate."""
        roll = self.rng_drop.random()
        chance = PROFILES[self.config.difficulty].drop
        if guaranteed:
            self._spawn_pickup(x, y, roll)
        elif roll < chance:
            self._spawn_pickup(x, y, roll / chance)

    def _spawn_pickup(self, x, y, share):
        """A pickup whose kind the share (0..1) picks by weight; at most 8 fall at once."""
        if len(self.pickups) >= LIMITS.pickups:
            return
        total = sum(kind.weight for kind in PICKUPS.values())
        mark = share * total
        chosen = "bomb"
        for name, kind in PICKUPS.items():
            mark -= kind.weight
            if mark < 0:
                chosen = name
                break
        self.pickups.append(Pickup(min(PLAYER.x_max, max(PLAYER.x_min, x)), y, chosen, self._new_id()))

    def _drops(self, x, y, count):
        """Guaranteed pickups (boss breaks and kills), one rng_drop draw each, spread out a little."""
        for index in range(count):
            self._spawn_pickup(x + 3.0 * (index - (count - 1) / 2), y, self.rng_drop.random())

    def _gone(self, alien, killed):
        """An enemy leaves the field (killed, rammed, landed, or a mite out at the bottom); the list is filtered once
        the step ends. A flight whose enemies were all shot or bombed within 8 s of arriving pays SWEEP."""
        alien.hp = 0
        self._dirty = True
        if alien.flight >= 0:
            state = self._flights[alien.flight]
            state.alive -= 1
            if not killed:
                state.clean = False
            if not state.alive and state.clean and not state.escort and self._play - state.since <= ARRIVAL.sweep:
                bonus = SCORING.sweep * self.act
                self.score += bonus
                self._bonus += bonus
                self._sweeps += 1
                self.events.append("sweep")
        if alien.kind == "carrier":
            self._brood.pop(alien.id, None)
        parent = self._parent.pop(alien.id, None)
        if parent is not None and parent in self._brood:
            self._brood[parent][1] -= 1

    def _remove(self, alien):
        self._gone(alien, False)

    def _purge(self):
        if self._dirty:
            self.aliens = [alien for alien in self.aliens if alien.hp > 0]
            self._dirty = False

    # --- the bomb (§5) -------------------------------------------------------------------------------------------------

    def _bomb(self):
        player = self.player
        if not player.bombs:
            return
        player.bombs -= 1
        self.bombs_used += 1
        keep, cleared = [], 0
        for shot in self.shots:
            if shot.friendly:
                keep.append(shot)
            else:
                cleared += 1
        self.shots = keep
        self.score += cleared * BOMB.shot_points
        director.cancel(self)
        if self.boss is not None:
            bosses.cancel(self)
        self.hazards = []
        self._lockout = max(self._lockout, BOMB.lock)
        player.invulnerable = max(player.invulnerable, BOMB.invulnerable)
        reach = BOMB.radius * BOMB.radius
        for alien in self.aliens:
            if alien.hp <= 0 or alien.y < BOMB.min_y:
                continue
            dx, dy = alien.x - player.x, alien.y - player.y
            if dx * dx + dy * dy <= reach:
                alien.hp -= BOMB.damage
                if alien.hp <= 0:
                    self._kill(alien, shot=False)
                else:
                    self.events.append("hit")
        self._purge()
        self._effect(player.x, player.y, RING, RING_TICKS)
        self.events.append("bomb")
        self._bank()

    # --- enemy shots, rams, shelters (steps 8 and 9) -------------------------------------------------------------------

    def _enemy_shots(self, slow):
        player_x, player_y = self.player.x, PLAYER.y
        box_x, box_y = PLAYER.box
        keep, split = [], []
        bunkers = self.bunkers
        low, high = SHELTERS.top - 1, SHELTERS.top + 3
        for shot in self.shots:
            if shot.friendly:
                keep.append(shot)
                continue
            spec = SHOTS[shot.kind]
            x0, y0 = shot.x, shot.y
            x1, y1 = x0 + shot.vx * slow, y0 + shot.vy * slow
            sx, sy = spec.box
            if bunkers and y1 >= low - sy and y0 <= high + sy:
                _enter, cell = first_cell(bunkers, x0, y0, x1, y1, sx, sy)
                if cell is not None:
                    self._strike(cell, spec.shelter, spec.splash)
                    continue
            if y1 >= player_y - box_y - sy - 1:
                if segment_hit(x0, y0, x1, y1, player_x, player_y, box_x + sx, box_y + sy) is not None:
                    self._hits.append(shot.kind if shot.volley else f"{shot.kind} boss")
                    continue
            shot.x, shot.y = x1, y1
            if spec.split and y1 >= spec.split_y:
                split.append(shot)
            elif y1 <= HEIGHT:
                keep.append(shot)
        for cluster in split:
            for vx in SHOTS[cluster.kind].split:
                keep.append(
                    Shot(cluster.x, cluster.y, False, vx, cluster.vy, 1, self._new_id(), "pellet", cluster.volley)
                )
        self.shots = keep

    def _strike(self, cell, damage, splash=0):
        """A shelter cell takes damage (a slug also splashes its four neighbours)."""
        bunkers = self.bunkers
        hp = bunkers[cell] - damage
        if hp > 0:
            bunkers[cell] = hp
        else:
            del bunkers[cell]
        if splash:
            x, y = cell
            for dx, dy in NEIGHBOURS:
                near_cell = (x + dx, y + dy)
                if near_cell in bunkers:
                    hp = bunkers[near_cell] - splash
                    if hp > 0:
                        bunkers[near_cell] = hp
                    else:
                        del bunkers[near_cell]
        self.shelter_rev += 1

    def _rams(self):
        """A diving diver or mite that reaches the ship is destroyed (no points, no drop) and the ship is hit."""
        if self.player.invulnerable:
            return
        ram_x, ram_y = fleet.DIVE.ram
        x, y = self.player.x, PLAYER.y
        for alien in self.aliens:
            if alien.state == "diving" and alien.hp > 0 and abs(alien.x - x) < ram_x and abs(alien.y - y) < ram_y:
                self._hits.append(alien.kind)
                self._gone(alien, False)
        self._purge()

    # --- pickups (§10.1) -----------------------------------------------------------------------------------------------

    def _fall_pickups(self):
        magnet = "MAGNET" in self.upgrades
        fall = PICKUP.fall * (UPGRADES["MAGNET"].extra if magnet else 1.0)
        reach = UPGRADES["MAGNET"].value if magnet else PICKUP.catch[0]
        keep = []
        x, y = self.player.x, PLAYER.y
        for pickup in self.pickups:
            pickup.y += fall
            if abs(pickup.x - x) < reach and abs(pickup.y - y) < PICKUP.catch[1]:
                self._collect(pickup.kind)
            elif pickup.y < HEIGHT:
                keep.append(pickup)
        self.pickups = keep

    def _collect(self, kind):
        player = self.player
        if kind in TIMED:
            add, most = PICKUP.add, PICKUP.most
            if "LONG FUSE" in self.upgrades:
                add, most = int(UPGRADES["LONG FUSE"].value), int(UPGRADES["LONG FUSE"].extra)
            if self.modifiers:
                add, most = endless.fuse(add, most, len(self.modifiers))
            running = self.powerups.get(kind, 0)
            self.powerups[kind] = min(most, running + add) if running > 0 else add
        elif kind == "shield":
            hits = int(UPGRADES["DEFLECTOR"].value) if "DEFLECTOR" in self.upgrades else PICKUPS["shield"].hits
            player.shield_hits = max(player.shield_hits, hits)
            player.shield = PICKUPS["shield"].ticks
            self.powerups["shield"] = PICKUPS["shield"].ticks
        elif kind == "repair":
            self._repair()
        elif self._can_add_bomb():  # bomb
            player.bombs += PICKUPS["bomb"].amount
            self.bombs_this_act += 1
        else:
            self.score += BOMB.full_points * self.act
        self._effect(player.x, player.y - 1, kind.upper())
        self.events.append("pickup")

    def _repair(self):
        """REPAIR: every cell +1 hp up to the cap; destroyed cells come back at 1."""
        cap = self._shelter_cap()
        amount = PICKUPS["repair"].amount
        for cell in self._layout():
            hp = self.bunkers.get(cell)
            self.bunkers[cell] = amount if hp is None else min(cap, hp + amount)
        self.shelter_rev += 1

    # --- the UFO (§7.3) ------------------------------------------------------------------------------------------------

    def _ufo_step(self, slow):
        ufo = self.ufo
        if ufo is not None:
            ufo.x += ufo.vx * slow
            if ufo.x < -3 or ufo.x > WIDTH + 3:
                self.ufo = None
        elif not self._ufo_done and self._play >= self._ufo_at and len(self.aliens) >= UFO.min_enemies:
            right = self.wave % 2 == 1
            self.ufo = Saucer(-2.0 if right else WIDTH + 2.0, UFO.y, UFO.speed if right else -UFO.speed)
            self._ufo_done = True
            self.events.append("ufo")

    def _ufo_kill(self):
        ufo = self.ufo
        self._chain_kill()
        points = (UFO.high if self._wave_shots % UFO.every == UFO.at else UFO.low) * self.combo
        self.score += points
        self.kills += 1
        self._drop(ufo.x, ufo.y, True)
        self._effect(ufo.x, ufo.y, f"+{points}")
        self.ufo = None
        self.events.append("kill")

    # --- step 10: the resolve -------------------------------------------------------------------------------------------

    def _resolve(self):
        player = self.player
        protected = player.invulnerable > 0
        lost = ""
        if self._hits:
            if not protected:
                cause = self._hits[0]
                if player.shield_hits > 0:
                    player.shield_hits -= 1
                    if not player.shield_hits:
                        player.shield = 0
                        self.powerups.pop("shield", None)
                    player.invulnerable = TIMING.absorb
                    self.events.append("shield")
                elif player.guard > 0:
                    player.guard -= 1
                    player.invulnerable = TIMING.absorb
                    self.events.append("shield")
                    if self._training():
                        self.notes.append(("SHIELD DOWN - MOVE OUT FROM UNDER THE !", TIMING.notice))
                else:
                    lost = cause
                self._break_chain()
            self._hits = []
        if self._landed:
            for index in self._landed:
                for alien in self.aliens:
                    if alien.flight == index and alien.hp > 0:
                        self._gone(alien, False)
            self._purge()
            self._landed = []
            self.events.append("landed")
            self._break_chain()
            if not lost and not protected:
                lost = "landing"
        if lost:
            self._lose_life(lost)
            if self.phase != "playing":
                return
        for name in tuple(self.powerups):
            ticks = self.powerups[name] - 1
            if ticks > 0:
                self.powerups[name] = ticks
            else:
                del self.powerups[name]
                if name == "shield":
                    player.shield_hits = 0
        player.shield = self.powerups.get("shield", 0)
        if player.invulnerable:
            player.invulnerable -= 1
        if self._lockout:
            self._lockout -= 1

    def _training(self):
        row, loop = script_wave(self.wave)
        return loop == 1 and row in PROFILES[self.config.difficulty].training

    def _lose_life(self, cause):
        player = self.player
        self.hit_cause = cause
        self._wave_hit = True
        player.lives -= 1
        self.events.append("damage")
        self._effect(player.x, player.y - 1, "HIT", 45)
        if self._immortal:
            player.lives = 99
        if player.lives <= 0:
            reason = "THE FLEET LANDED" if cause == "landing" else "SHIP LOST"
            self.notice = reason
            if self.continues > 0 and self.config.mode != "endless":
                self._enter_continue()
            else:
                self._game_over(reason)
            return
        self._freeze = TIMING.freeze
        profile = PROFILES[self.config.difficulty]
        if player.lives == 1 and profile.last_stand and not self.last_stand_used:
            self.last_stand_used = True
            player.bombs = min(self._bomb_cap(), player.bombs + BOMB.last_stand)
            self.powerups["rapid"] = max(self.powerups.get("rapid", 0), TIMING.last_stand)
            self.notes.append(("LAST STAND", TIMING.notice))
            self.events.append("last_stand")

    def _mercy(self):
        """The hit freeze ends: every enemy shot, telegraph and hazard goes; then 120 ticks invulnerable and 60
        with no enemy fire (F6)."""
        self.shots = [shot for shot in self.shots if shot.friendly]
        director.cancel(self)
        if self.boss is not None:
            bosses.cancel(self)
        self.hazards = []
        for alien in self.aliens:
            if alien.state == "telegraph":
                if alien.flight < 0:  # a mite or a boss's diver has no slot to go back to: it goes (no points)
                    self._gone(alien, False)
                else:
                    alien.state = "formation"
        self._purge()
        self.player.invulnerable = TIMING.safe
        self._lockout = TIMING.lockout

    # --- step 11: phase checks and entries ----------------------------------------------------------------------------

    def _phase_checks(self):
        """Step 11: the wave clear, boss phases, the phase countdowns, and the phase changes with their entries."""
        phase = self.phase
        if phase == "wave":
            if self._clock <= 0:
                self.phase = "playing"
                self.events.append("wave")
                if self.boss is not None:
                    bosses.fight(self)
        elif phase == "playing":
            if self.boss is not None:
                bosses.check(self)
            if self._wave_done():
                self._enter_clear()
        elif phase == "clear" and self._clock <= 0:
            self._after_clear()

    def _wave_done(self):
        if self._wave_row().boss:
            return self.boss is None
        return not self._pending and not self.aliens

    def _after_clear(self):
        row = self._wave_row()
        endless = self.config.mode == "endless"
        if (row.card and not endless) or (endless and self.wave % ENDLESS.card_every == 0):
            self._enter_upgrade()
        elif self.wave >= CAMPAIGN_WAVES and not endless:
            self.phase = "victory"
            self.events.append("victory")
        else:
            self._start_wave(self.wave + 1)

    def _enter_clear(self):
        """Into `clear`: every enemy shot, hazard and telegraph goes with no points; player shots go as neutral."""
        if self._freeze:  # a life went on the tick the wave cleared: the mercy clear runs now (F6), and its
            self._freeze = 0  # invulnerability and lockout stay frozen through the clear and the intro (F7)
            self._mercy()
        self.phase, self._clock = "clear", TIMING.clear
        self.shots.clear()
        self.hazards.clear()
        self._pvol, self._pierce = {}, {}
        director.cancel(self)
        self._volleys = []
        self.ufo = None
        bonus = SCORING.clear * self.act
        lines = []
        if not self._wave_hit and not self._wave_row().boss:
            perfect = SCORING.perfect * self.act
            bonus += perfect
            self.perfect_waves += 1
            lines.append(f"PERFECT +{perfect:,}")
            self.events.append("perfect")
        if self._sweeps:
            lines.append(f"SWEEP x{self._sweeps}")
        self.score += bonus
        total = bonus + self._bonus
        self.notice = "\n".join([f"WAVE {self.wave:02d} CLEAR  +{total:,}", *lines, f"BEST COMBO x{self.best_combo}"])
        self.events.append("clear")

    def _phase_break(self, parts):
        """A boss phase break starts (boss.check has set its state): shots cleared, points, one pickup."""
        boss = self.boss
        self.shots = [shot for shot in self.shots if shot.friendly]
        director.cancel(self)
        self.score += SCORING.part * self.act * parts
        self._drops(boss.x, boss.y + 2, PICKUP.phase_break)
        self.events.append("phase")
        self._effect(boss.x, boss.y + 1, "PHASE BREAK", 60)

    def _boss_killed(self):
        """The last phase ends: points, two pickups, +1 life (bosses 1-3), escorts and enemy shots removed."""
        boss = self.boss
        row = BOSSES[boss.kind]
        points = SCORING.boss * self.act
        if not self._wave_hit:
            points += SCORING.boss_perfect * self.act
            self.perfect_waves += 1
            self.events.append("perfect")
        self.score += points
        self.bosses += 1
        self._drops(boss.x, boss.y + 2, PICKUP.boss_kill)
        life = max(row.life, 1) if self.config.mode == "endless" else row.life  # Endless: +1 at every boss
        if life:
            cap = PROFILES[self.config.difficulty].cap
            if self.player.lives < cap:
                self.player.lives = min(cap, self.player.lives + life)
                self.events.append("life")
                self.notes.append(("EXTRA LIFE", TIMING.notice))
        bosses.cancel(self)
        director.cancel(self)
        self.shots = [shot for shot in self.shots if shot.friendly]
        self.hazards = []
        self._pending = []
        for alien in self.aliens:
            self._gone(alien, False)
        self._purge()
        for part in boss.parts:
            part.hp, part.state = 0, "broken"
        boss.hp, boss.state, boss.timer = 0, "dying", TIMING.boss_dying
        self._effect(boss.x, boss.y + 1, f"+{points:,}", 90)
        self.events.append("boss")

    def _start_wave(self, number, restart=False):
        """Into `wave`: each wave start, a continue's restart included (§14.2)."""
        row_number, loop = script_wave(number)
        row = WAVES[row_number - 1]
        act = next(act for act in ACTS if act.first <= row_number <= act.last)
        profile = PROFILES[self.config.difficulty]
        act_start = row_number == act.first and not restart
        self.wave, self.act, self.loop = number, act.number, loop
        self.pickups.clear()
        if restart:
            self.aliens.clear()
            self.shots.clear()
            self._pvol, self._pierce = {}, {}
        self.hazards.clear()
        self.boss = None
        self.ufo = None
        if act_start:
            self._shelters("rebuild")
            self.bombs_this_act = 0
            self.events.append("act")
            if "DEFLECTOR" in self.upgrades:
                self.player.guard = max(self.player.guard, int(UPGRADES["DEFLECTOR"].extra))
            self._bank()
        elif not restart:
            self._shelters(profile.shelter_wave)
        if row_number in profile.training and loop == 1:
            self.player.guard = max(self.player.guard, 1)
        new = []
        if self.config.mode == "endless":
            if loop > 1 and row_number == ENDLESS.loop[0] and ENDLESS.loop_life and not restart:
                self.player.lives = min(profile.cap, self.player.lives + ENDLESS.loop_life)
            self.modifiers = endless.names(endless.due(number))
            new = self.modifiers[endless.due(number - 1) :]  # the modifier this wave adds, if any
        self._play, self._next_flight = 0.0, 0
        self._wave_hit = False
        self._fire = self._director_row(row)
        self._flights = []
        self._pending = [(flight.at, ("wave", index)) for index, flight in enumerate(row.flights)]
        extra, every = endless.flights(len(self.modifiers)) if self.modifiers and row.flights else (0, 0)
        for repeat in range(1, extra + 1):  # REINFORCED: the last flight again, `every` ticks apart
            last = len(row.flights) - 1
            self._pending.append((row.flights[last].at + repeat * every, ("wave", last)))
        self._freeze = 0
        self._volleys = []
        self._dclock = self._dive_clock = 0.0
        self._brood, self._parent = {}, {}
        self._attacks = []
        self._motion = bosses.Motion()
        self._wave_shots = self._sweeps = self._bonus = 0
        self._ufo_done = False
        self._ufo_at = 0
        if row.ufo and row_number >= UFO.first and not row.boss:
            self._ufo_at = UFO.delay + self.rng.randint(0, UFO.jitter)
        self.phase = "wave"
        self.wave_name = row.name
        boss = BOSSES[row.boss] if row.boss else None
        if restart:
            self._clock = TIMING.restart
        elif boss is not None:
            self._clock = TIMING.boss_intro
        else:
            self._clock = TIMING.act_intro if act_start and row_number != 1 else TIMING.intro
        if boss is not None:
            name = boss.name if loop == 1 else f"{boss.name} {ENDLESS.mark} {roman(loop)}"
            self.wave_name = name
            self.intro = ("WARNING", name, row.line)
            bosses.build(self, boss)
            self.events.append("siren")
        else:
            self.intro = (f"WAVE {number:02d}  ACT {act.roman}: {act.name}", row.name, row.line)
            if act_start:
                self.intro = (f"ACT {act.roman}  {act.name}", act.line, *self.intro)
        for name in new:  # Endless: the intro names each new modifier
            self.intro = (*self.intro, f"NEW: {name}")
            self.notes.append((f"NEW: {name}", TIMING.notice))

    def _director_row(self, row):
        """The wave's director row with staged()'s overrides, then Endless's modifiers (every floor and cap still
        applies)."""
        found = replace(row.director, **self._director) if self._director else row.director
        return endless.director_row(found, len(self.modifiers)) if self.modifiers else found

    def _enter_upgrade(self):
        offers = self._draw_offers()
        if not offers:  # nothing left to offer (can't happen with 15 ranks for 7 cards, or Endless's fillers)
            self._start_wave(self.wave + 1)
            return
        self.phase, self.cursor, self.armed, self.offers = "upgrade", 1, TIMING.arm, offers
        self.events.append("card")

    def _enter_continue(self):
        self.phase, self.prompt, self.armed = "continue", TIMING.prompt, TIMING.arm

    def _game_over(self, reason):
        self.phase, self.notice = "game_over", reason
        self.events.append("game_over")

    # --- the upgrade card (§10.2) --------------------------------------------------------------------------------------

    def _ranks_left(self, name):
        return UPGRADES[name].stacks - self.upgrades.get(name, 0)

    def _draw_offers(self):
        chosen = []
        for family in FAMILIES:
            pool = [up for up in UPGRADES.values() if up.family == family and self._open(up.name, chosen)]
            if not pool:  # the family with the most ranks left gives this card (ties in FAMILIES order)
                best, most = "", 0
                for other in FAMILIES:
                    ranks = sum(
                        self._ranks_left(up.name)
                        for up in UPGRADES.values()
                        if up.family == other and up.name not in chosen
                    )
                    if ranks > most:
                        best, most = other, ranks
                pool = [up for up in UPGRADES.values() if up.family == best and self._open(up.name, chosen)]
            if family == "offense" and not self.picks and self._open("TRIPLE SHOT", chosen):
                chosen.append("TRIPLE SHOT")  # the first card's offense card, with no draw
            elif pool:
                roll = self.rng_offer.randint(1, sum(up.weight for up in pool))
                for up in pool:
                    roll -= up.weight
                    if roll <= 0:
                        chosen.append(up.name)
                        break
            elif self.config.mode == "endless":
                filler = next((name for name in ENDLESS.fillers if name not in chosen), "")
                if filler:
                    chosen.append(filler)
        return tuple(chosen)

    def _open(self, name, chosen):
        return name not in chosen and self._ranks_left(name) > 0

    def _card(self, edges):
        if self.offers:
            if "left" in edges:
                self.cursor = (self.cursor - 1) % len(self.offers)
            if "right" in edges:
                self.cursor = (self.cursor + 1) % len(self.offers)
            if "fire" in edges and not self.armed:
                self._take(self.offers[self.cursor])
                self.offers, self.cursor = (), 1
                self._start_wave(self.wave + 1)
                return
        self.armed = max(0, self.armed - 1)

    def _take(self, name):
        """Apply one pick (a card's, or staged()'s), with its immediate effects."""
        player = self.player
        if name == "REFIT":
            player.bombs = min(self._bomb_cap(), player.bombs + ENDLESS.refit)
            self._shelters("rebuild")
        elif name == "BOUNTY":
            self.score += ENDLESS.bounty * self.act
        else:
            upgrade = UPGRADES[name]
            if self._ranks_left(name) <= 0:
                raise ValueError(f"{name} is already at its maximum")
            self.upgrades[name] = self.upgrades.get(name, 0) + 1
            if name == "TRIPLE SHOT":
                player.weapon = int(upgrade.value)
            elif name == "DEFLECTOR":
                player.guard = max(player.guard, int(upgrade.extra))
            elif name == "MASONRY":
                self._shelters("masonry")
            elif name == "BOMB BAY":
                player.bombs = min(self._bomb_cap(), player.bombs + 1)
        self.picks = (*self.picks, name)
        self.notes.append((f"TAKEN: {name}", TIMING.notice))
        self.events.append("pick")

    # --- the continue prompt (§5) --------------------------------------------------------------------------------------

    def _continue_prompt(self, edges):
        if not self.armed and "fire" in edges:
            self.continues -= 1
            self.continues_used += 1
            self.player.lives = 2
            self.score = self.chain = 0
            self.combo = 1
            self.events.append("continue")
            self._start_wave(self.wave, restart=True)
            return
        if not self.armed and "bomb" in edges:
            self._game_over(self.notice or "SHIP LOST")
            return
        self.prompt -= 1
        self.armed = max(0, self.armed - 1)
        if self.prompt <= 0:
            self._game_over(self.notice or "SHIP LOST")

    # --- shelters (§7.4) -----------------------------------------------------------------------------------------------

    def _shelter_cap(self):
        stacks = self.upgrades.get("MASONRY", 0)
        return PROFILES[self.config.difficulty].shelter_cap + stacks * int(UPGRADES["MASONRY"].value)

    def _layout(self):
        """Every shelter cell of the profile, standing or not."""
        return [
            (centre + dx, SHELTERS.top + dy)
            for centre in PROFILES[self.config.difficulty].shelters
            for dx, dy in SHELTERS.cells
        ]

    def _shelters(self, rule):
        """rebuild: every cell to the cap; patch: +1 on every surviving cell; masonry: +value now; none: nothing."""
        cap = self._shelter_cap()
        if rule == "rebuild":
            self.bunkers = dict.fromkeys(self._layout(), cap)
        elif rule in ("patch", "masonry"):
            amount = 1 if rule == "patch" else int(UPGRADES["MASONRY"].value)
            for cell, hp in self.bunkers.items():
                self.bunkers[cell] = min(cap, hp + amount)
        else:
            return
        self.shelter_rev += 1

    # --- helpers for the fleet, the director and the boss ---------------------------------------------------------------

    def _row(self):
        return script_wave(self.wave)[0]

    def _wave_row(self):
        return WAVES[script_wave(self.wave)[0] - 1]

    def _flight_row(self, key):
        """A pending flight's table row: ("wave", i), or ("boss", phase, i) with phase -1 for the boss's escorts,
        with the profile's "escort" variants."""
        if key[0] == "wave":
            return self._wave_row().flights[key[1]]
        return bosses.escort_of(self._wave_row().boss, key[1], key[2], self.config.difficulty)

    def _schedule(self, at, key):
        """Add a flight to the pending queue, keeping it in arrival order."""
        index = len(self._pending)
        while index and self._pending[index - 1][0] > at:
            index -= 1
        self._pending.insert(index, (at, key))

    def _escort_top(self):
        """Escort flights take the band with its top 2 cells below the boss's lowest part (at its station)."""
        row = BOSSES[self._wave_row().boss]
        return max(part.y for part in row.parts) + ARRIVAL.gap

    def _pressure(self):
        """The divisor of reload and boss cooldowns: the profile's, times the boss phase's, times Endless's loop."""
        pressure = PROFILES[self.config.difficulty].pressure
        boss = self.boss
        if boss is not None:
            pressure *= BOSSES[boss.kind].phases[boss.phase].pressure
        if self.loop > 1:
            pressure *= ENDLESS.loop_pressure
        return pressure

    def _log(self, ident, kind, w, flight, y, pose, source, cls=""):
        self.fired.append(Fired(ident, kind, cls or kind, w, flight, y, pose, source))

    def _effect(self, x, y, text, ttl=30):
        if len(self.effects) >= MAX_EFFECTS:
            self.effects.pop(0)
        self.effects.append(Effect(x, y, text, ttl))

    def _new_id(self):
        self._serial += 1
        return self._serial

    def result(self) -> dict:
        return {
            "score": self.score,
            "wave": self.wave,
            "kills": self.kills,
            "shots": self.shots_fired,
            "hits": self.hits,
            "best_combo": self.best_combo,
            "ticks": self.tick,
            "victory": self.phase == "victory",
            "act": self.act,
            "loop": self.loop,
            "bosses": self.bosses,
            "perfect_waves": self.perfect_waves,
            "continues_used": self.continues_used,
            "bomb_kills": self.bomb_kills,
            "bombs_used": self.bombs_used,
            "picks": self.picks,
        }

    def digest(self) -> str:
        """sha256 over the canonical JSON of DIGEST_FIELDS, the three rng states and the ruleset."""

        def encode(value):
            if hasattr(value, "__dataclass_fields__"):
                return asdict(value)
            raise TypeError(f"Cannot serialize {type(value).__name__}")

        state = {name: getattr(self, name) for name in DIGEST_FIELDS}
        for name in ("rng", "rng_drop", "rng_offer"):
            state[name] = getattr(self, name).getstate()
        state["bunkers"] = sorted((x, y, hp) for (x, y), hp in self.bunkers.items())
        state["ruleset"] = RULESET
        return hashlib.sha256(
            json.dumps(state, default=encode, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
