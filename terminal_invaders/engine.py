"""Deterministic arcade rules. One step is 1/60 second; no terminal or clock I/O."""

import hashlib
import json
import random
from dataclasses import asdict

from .model import (
    ACTIONS,
    CAMPAIGN_WAVES,
    DIFFICULTIES,
    HEIGHT,
    MAX_EFFECTS,
    MAX_PICKUPS,
    MAX_SHOTS,
    PICKUPS,
    UFO,
    WIDTH,
    Alien,
    Boss,
    Config,
    Effect,
    Pickup,
    Player,
    Shot,
)


def segment_hit(x0, y0, x1, y1, x, y, rx, ry):
    """Return first intersection (0..1) of a swept shot with a hit box."""
    enter, leave = 0.0, 1.0
    for start, end, center, radius in ((x0, x1, x, rx), (y0, y1, y, ry)):
        delta = end - start
        if abs(delta) < 1e-12:
            if abs(start - center) > radius:
                return None
            continue
        near, far = sorted(((center - radius - start) / delta, (center + radius - start) / delta))
        enter, leave = max(enter, near), min(leave, far)
        if enter > leave:
            return None
    return enter


class Game:
    """Owns every gameplay value, including a private, replayable random stream."""

    def __init__(self, config: Config = Config()):
        self.config = config
        self.rng = random.Random(config.seed)
        lives, self.difficulty = DIFFICULTIES[config.difficulty]
        self.player = Player(lives=lives)
        self.tick = self.score = self.kills = self.shots_fired = self.hits = 0
        self.wave = self.combo = self.best_combo = self.combo_timer = 0
        self.bomb_charge = 0
        self.autofire = False
        self.phase = "wave"
        self.wave_timer = 0
        self.wave_name = self.notice = ""
        self.aliens = []
        self.shots = []
        self.pickups = []
        self.effects = []
        self.bunkers = {}
        self.boss = self.ufo = None
        self.powerups = {}
        self.events = []
        self._fire_cooldown = self._alien_clock = self._enemy_clock = 0
        self._dive_clock = 0
        self._ufo_clock = 0
        self._direction = 1
        self._wave_size = 0
        self._wave_damage = False
        self._next_life = 10000
        self._start_wave()

    def step(self, actions: tuple[str, ...] = ()) -> None:
        """Apply canonical input pulses and advance exactly one simulation tick."""
        if self.phase in {"game_over", "victory"}:
            return
        unknown = set(actions) - ACTIONS
        if unknown:
            raise ValueError(f"Unknown actions: {sorted(unknown)}")
        self.tick += 1
        self.events = []
        self._timers()
        self._controls(actions)
        if self.phase == "wave":
            self.wave_timer -= 1
            if self.wave_timer <= 0:
                self.phase = "playing"
                self.events.append("wave")
            return
        self._move_enemies()
        if self.phase == "game_over":
            return
        self._enemy_fire()
        self._move_shots()
        if self.phase == "game_over":
            return
        self._move_pickups()
        if self.phase == "playing" and not self.aliens and self.boss is None:
            self._complete_wave()

    def _timers(self):
        self._fire_cooldown = max(0, self._fire_cooldown - 1)
        self.player.invulnerable = max(0, self.player.invulnerable - 1)
        self.combo_timer = max(0, self.combo_timer - 1)
        if not self.combo_timer:
            self.combo = 0
        for kind in list(self.powerups):
            self.powerups[kind] -= 1
            if self.powerups[kind] <= 0:
                del self.powerups[kind]
                if kind == "shield":
                    self.player.shield = 0
        for effect in self.effects:
            effect.ttl -= 1
        self.effects = [effect for effect in self.effects if effect.ttl > 0]

    def _controls(self, actions):
        # Coalesce repeated terminal events: OS repeat rate cannot amplify damage.
        pressed = set(actions)
        move = int("right" in pressed) - int("left" in pressed)
        self.player.x = max(2, min(WIDTH - 3, self.player.x + 0.4 * move))
        if "autofire" in pressed:
            self.autofire = not self.autofire
        if self.phase != "playing":
            return
        if "bomb" in pressed:
            self._bomb()
        if ("fire" in pressed or self.autofire) and not self._fire_cooldown:
            self._fire()

    def _fire(self):
        if len(self.shots) >= MAX_SHOTS - 3:
            return
        velocities = (-0.13, 0.0, 0.13) if "spread" in self.powerups else (0.0,)
        for vx in velocities:
            self.shots.append(
                Shot(self.player.x, self.player.y - 0.65, vx=vx, damage=2 if self.player.weapon == 3 else 1)
            )
            self.shots_fired += 1
        self._fire_cooldown = 5 if "rapid" in self.powerups else 14 - self.player.weapon * 2
        self.events.append("fire")

    def _start_wave(self):
        self.wave += 1
        self.phase, self.wave_timer = "wave", 90
        self.shots.clear()
        self.pickups.clear()
        self.aliens.clear()
        self.ufo = self.boss = None
        self._direction = 1 if self.wave % 2 else -1
        self._alien_clock = self._enemy_clock = self._dive_clock = 0
        self._ufo_clock = self.rng.randint(700, 1200)
        self._wave_damage = False
        self.player.weapon = min(3, 1 + (self.wave - 1) // 3)
        self._repair(2 if self.wave == 1 else 1)
        if self.wave % 4 == 0:
            hp = int((22 + self.wave * 3) * self.difficulty)
            self.boss = Boss(WIDTH / 2, 3, hp, hp)
            self.wave_name = ("DREADNOUGHT", "THE HIVE", "FINAL ARMADA")[min(2, self.wave // 4 - 1)]
            self.aliens = [Alien(x, 6, "guard", 2) for x in (12, 22, 38, 48)]
        else:
            self.wave_name = ("FIRST CONTACT", "CROSS FIRE", "DIVE SQUADRON")[(self.wave - 1) % 3]
            rows = 3 + int(self.wave >= 6)
            for row in range(rows):
                for col in range(7):
                    x = 9 + col * 7 + (row % 2 if self.wave % 3 == 2 else 0)
                    y = 2 + row * 2 + (abs(col - 3) * 0.25 if self.wave % 3 == 0 else 0)
                    kind = "armor" if row == 0 and self.wave >= 3 else "scout"
                    if row == rows - 1 and self.wave >= 2:
                        kind = "diver"
                    self.aliens.append(Alien(x, y, kind, 2 if kind == "armor" else 1))
        self._wave_size = len(self.aliens)

    @staticmethod
    def _shelter_cells():
        for center in (10, 23, 36, 49):
            for dy in range(3):
                for dx in range(-3, 4):
                    if (dy == 0 and abs(dx) == 3) or (dy == 2 and abs(dx) <= 1):
                        continue
                    yield center + dx, 14 + dy

    def _repair(self, amount):
        for cell in self._shelter_cells():
            self.bunkers[cell] = min(3, self.bunkers.get(cell, 0) + amount)

    def _move_enemies(self):
        slow = 0.55 if "slow" in self.powerups else 1.0
        formation = [alien for alien in self.aliens if not alien.diving]
        self._alien_clock += slow
        cadence = max(5, (30 - min(self.wave, 30)) / self.difficulty)
        cadence *= 0.3 + 0.7 * len(self.aliens) / max(1, self._wave_size)
        if formation and self._alien_clock >= cadence:
            self._alien_clock = 0
            edge = any(not 2 <= alien.x + self._direction <= WIDTH - 3 for alien in formation)
            if edge:
                self._direction *= -1
            for alien in formation:
                alien.x += self._direction
                alien.y += 0.65 if edge else 0
                if alien.y >= self.player.y - 0.7:
                    self._game_over("THE INVASION LANDED")
                    return
                self._crush_shelter(alien.x, alien.y, 1.5)
        self._dive_clock += slow
        if self.wave >= 2 and self._dive_clock >= max(150, 500 - self.wave * 18):
            self._dive_clock = 0
            candidates = [alien for alien in formation if alien.kind == "diver"]
            if candidates and sum(alien.diving for alien in self.aliens) < 2:
                alien = self.rng.choice(candidates)
                alien.diving = True
                alien.vx = (self.player.x - alien.x) / 130
        for alien in self.aliens:
            if not alien.diving:
                continue
            alien.x = max(2, min(WIDTH - 3, alien.x + alien.vx * slow))
            alien.y += (0.065 + min(self.wave, 20) * 0.002) * slow
            self._crush_shelter(alien.x, alien.y, 1.2)
            if abs(alien.x - self.player.x) < 2 and abs(alien.y - self.player.y) < 0.8:
                self._damage_player()
                if self.phase == "game_over":
                    return
                alien.y = HEIGHT + 1
            if alien.y > HEIGHT:
                alien.diving, alien.y = False, 2.0
        if self.boss:
            boss = self.boss
            rage = 1.6 if boss.hp < boss.max_hp / 3 else 1
            boss.x += boss.vx * slow * rage
            if not 6 <= boss.x <= WIDTH - 7:
                boss.x = max(6, min(WIDTH - 7, boss.x))
                boss.vx *= -1
        self._ufo_clock -= 1
        if self.ufo:
            self.ufo.x += self.ufo.vx * slow
            if not -4 < self.ufo.x < WIDTH + 4:
                self.ufo = None
        elif self._ufo_clock <= 0:
            direction = self.rng.choice((-1, 1))
            self.ufo = UFO(-3 if direction > 0 else WIDTH + 3, vx=0.12 * direction)
            self._ufo_clock = self.rng.randint(900, 1500)
            self.events.append("ufo")

    def _crush_shelter(self, x, y, radius):
        if y < 13:
            return
        for cell in list(self.bunkers):
            if abs(cell[0] - x) <= radius and abs(cell[1] - y) <= 0.7:
                del self.bunkers[cell]

    def _enemy_fire(self):
        self._enemy_clock += 0.55 if "slow" in self.powerups else 1
        interval = max(16, (65 - min(self.wave, 25) * 2) / self.difficulty)
        if self._enemy_clock < interval or len(self.shots) > MAX_SHOTS - 6:
            return
        self._enemy_clock = 0
        speed = min(0.24, 0.09 + self.wave * 0.005) * self.difficulty
        if self.boss:
            boss = self.boss
            aim = max(-0.13, min(0.13, (self.player.x - boss.x) / 160))
            spread = 0.09 if boss.hp > boss.max_hp / 2 else 0.15
            for vx in (aim - spread, aim, aim + spread):
                self.shots.append(Shot(boss.x, boss.y + 1, False, vx, speed))
        if self.aliens:
            # Only the lowest surviving alien in each column can fire.
            columns = {}
            for alien in self.aliens:
                col = round(alien.x / 3)
                if col not in columns or alien.y > columns[col].y:
                    columns[col] = alien
            alien = self.rng.choice(list(columns.values()))
            aim = (self.player.x - alien.x) / 240 if self.wave >= 5 else 0.0
            self.shots.append(Shot(alien.x, alien.y + 0.7, False, max(-0.08, min(0.08, aim)), speed))

    def _targets(self, shot):
        for cell in self.bunkers:
            yield "bunker", cell, cell[0], cell[1], 0.48, 0.48
        if not shot.friendly:
            yield "player", self.player, self.player.x, self.player.y, 1.35, 0.45
            return
        for alien in self.aliens:
            yield "alien", alien, alien.x, alien.y, 1.35, 0.48
        if self.boss:
            yield "boss", self.boss, self.boss.x, self.boss.y, 4.0, 0.75
        if self.ufo:
            yield "ufo", self.ufo, self.ufo.x, self.ufo.y, 2.0, 0.45

    def _move_shots(self):
        remaining = []
        for index, shot in enumerate(self.shots):
            speed = 0.55 if not shot.friendly and "slow" in self.powerups else 1.0
            nx, ny = shot.x + shot.vx * speed, shot.y + shot.vy * speed
            nearest, collision = 2.0, None
            for kind, target, x, y, rx, ry in self._targets(shot):
                distance = segment_hit(shot.x, shot.y, nx, ny, x, y, rx, ry)
                if distance is not None and distance < nearest:
                    nearest, collision = distance, (kind, target)
            if collision:
                self._impact(shot, *collision)
                if self.phase == "game_over":
                    self.shots = remaining + self.shots[index + 1 :]
                    return
            elif -1 <= nx <= WIDTH and -1 <= ny <= HEIGHT:
                shot.x, shot.y = nx, ny
                remaining.append(shot)
        self.shots = remaining

    def _impact(self, shot, kind, target):
        if kind == "bunker":
            hp = self.bunkers[target] - shot.damage
            if hp <= 0:
                del self.bunkers[target]
            else:
                self.bunkers[target] = hp
            return
        if kind == "player":
            self._damage_player()
            return
        self.hits += 1
        if kind == "ufo":
            self._award(self.rng.choice((150, 250, 400)))
            self._drop(target.x, target.y, guaranteed=True)
            self._effect(target.x, target.y, "JACKPOT")
            self.ufo = None
            self.events.append("kill")
            return
        target.hp -= shot.damage
        if target.hp <= 0:
            self._kill(target, boss=kind == "boss")
        else:
            self._effect(target.x, target.y, "*", 8)
            self.events.append("hit")

    def _kill(self, target, boss=False, bomb=False):
        self.kills += 1
        if not bomb:
            self.combo = min(5, self.combo + 1)
            self.best_combo = max(self.best_combo, self.combo)
            self.combo_timer = 150
        points = 1000 + self.wave * 100 if boss else {"armor": 40, "guard": 50, "diver": 30, "scout": 20}[target.kind]
        reward = points * (1 if bomb else self.combo)
        self._award(reward)
        self.bomb_charge += int(not bomb)
        if self.bomb_charge >= 18:
            self.bomb_charge = 0
            self.player.bombs = min(3, self.player.bombs + 1)
            self._effect(self.player.x, self.player.y - 1, "BOMB READY")
        self._effect(target.x, target.y, f"+{reward}")
        self._drop(target.x, target.y, guaranteed=boss)
        if boss:
            self.boss = None
            self.events.append("boss")
        else:
            self.aliens.remove(target)
            self.events.append("kill")

    def _award(self, points):
        self.score += points
        while self.score >= self._next_life:
            self._next_life += 10000
            self.player.lives = min(6, self.player.lives + 1)
            self._effect(self.player.x, self.player.y - 2, "EXTRA LIFE")
            self.events.append("life")

    def _drop(self, x, y, guaranteed=False):
        if len(self.pickups) < MAX_PICKUPS and (guaranteed or self.rng.random() < 0.12):
            self.pickups.append(Pickup(x, y, self.rng.choice(PICKUPS)))

    def _move_pickups(self):
        remaining = []
        for pickup in self.pickups:
            pickup.y += 0.045
            if abs(pickup.x - self.player.x) < 2 and abs(pickup.y - self.player.y) < 0.65:
                self._collect(pickup.kind)
            elif pickup.y < HEIGHT:
                remaining.append(pickup)
        self.pickups = remaining

    def _collect(self, kind):
        if kind == "repair":
            self._repair(2)
        else:
            self.powerups[kind] = min(1200, self.powerups.get(kind, 0) + 600)
            if kind == "shield":
                self.player.shield = min(3, self.player.shield + 2)
        self._effect(self.player.x, self.player.y - 1, kind.upper())
        self.events.append("pickup")

    def _bomb(self):
        if not self.player.bombs:
            return
        self.player.bombs -= 1
        self.shots = [shot for shot in self.shots if shot.friendly]
        self.player.invulnerable = max(60, self.player.invulnerable)
        for alien in list(self.aliens):
            alien.hp -= 2
            if alien.hp <= 0:
                self._kill(alien, bomb=True)
        if self.boss:
            self.boss.hp -= 10
            if self.boss.hp <= 0:
                self._kill(self.boss, boss=True, bomb=True)
        self._effect(WIDTH / 2, HEIGHT / 2, "NOVA BOMB", 45)
        self.events.append("bomb")

    def _damage_player(self):
        if self.player.invulnerable or self.phase == "game_over":
            return
        if self.player.shield:
            self.player.shield -= 1
            self.player.invulnerable = 45
            if not self.player.shield:
                self.powerups.pop("shield", None)
            self.events.append("shield")
            return
        self.player.lives -= 1
        self.player.invulnerable = 150
        self.combo = self.combo_timer = 0
        self._wave_damage = True
        self.events.append("damage")
        if self.player.lives <= 0:
            self._game_over("DEFENDER LOST")
        elif self.player.lives == 1:
            self.player.bombs = max(1, self.player.bombs)
            self.powerups["rapid"] = max(300, self.powerups.get("rapid", 0))
            self._effect(self.player.x, self.player.y - 2, "LAST STAND", 120)

    def _complete_wave(self):
        for pickup in self.pickups:
            self._collect(pickup.kind)
        bonus = self.wave * 100 + (500 if not self._wave_damage else 0)
        self._award(bonus)
        self.notice = f"{'PERFECT WAVE' if not self._wave_damage else 'WAVE CLEAR'} +{bonus}"
        if self.wave >= CAMPAIGN_WAVES and self.config.mode != "endless":
            self.phase = "victory"
            self.events.append("victory")
            return
        self._start_wave()

    def _game_over(self, reason):
        self.phase, self.notice = "game_over", reason
        self.events.append("game_over")

    def _effect(self, x, y, text, ttl=30):
        self.effects.append(Effect(x, y, text, ttl))
        self.effects = self.effects[-MAX_EFFECTS:]

    def result(self) -> dict[str, int | bool]:
        return {
            "score": self.score,
            "wave": self.wave,
            "kills": self.kills,
            "shots": self.shots_fired,
            "hits": self.hits,
            "best_combo": self.best_combo,
            "ticks": self.tick,
            "victory": self.phase == "victory",
        }

    def digest(self) -> str:
        """Hash full simulation state, including timers, configuration and RNG."""

        def encode(value):
            if hasattr(value, "__dataclass_fields__"):
                return asdict(value)
            raise TypeError(f"Cannot serialize {type(value).__name__}")

        state = dict(vars(self))
        state["rng"] = self.rng.getstate()
        state["bunkers"] = sorted((x, y, hp) for (x, y), hp in self.bunkers.items())
        return hashlib.sha256(
            json.dumps(state, default=encode, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
