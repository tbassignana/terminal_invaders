#!/usr/bin/env python3
"""Headless bot playtests that measure balance: python3 tools/balance.py --help.

Bots drive terminal_invaders.engine.Game through step() and see it through PublicView, which hides the random stream
and private attributes. They read what the renderer draws, so their numbers survive engine refactors.
"""

import argparse
import json
import math
import multiprocessing
import os
import platform
import random
import re
import statistics
import sys
import time
from collections import Counter, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from terminal_invaders.engine import Game  # noqa: E402
from terminal_invaders.model import (  # noqa: E402
    CAMPAIGN_WAVES,
    DIFFICULTIES,
    HEIGHT,
    RULESET,
    TICK_RATE,
    WIDTH,
    Config,
)

# What a practised player has learned about the rules; bots plan with these, the engine never reads them.
PLAYER_Y = HEIGHT - 2
MUZZLE_Y = PLAYER_Y - 0.65
SHOT_SPEED = 0.55
SHIP_SPEED = 0.4
PICKUP_FALL = 0.045
SHELTER_ROWS = (14, 15, 16)
LEFT, RIGHT = 2, WIDTH - 3
TICKS_PER_MINUTE = 60 * TICK_RATE
COUNTERS = (
    "lives_lost",
    "shield_hits",
    "extra_lives",
    "life_awards",
    "pickups",
    "bombs_used",
    "bomb_kills",
    "combo_kills",
    "combo_total",
    "playing_ticks",
    "powered_ticks",
)
TABLE = (
    ("games", "games", "d"),
    ("wins", "win_rate", ".0%"),
    ("mean wave", "wave_mean", ".2f"),
    ("median", "wave_median", "g"),
    ("minutes", "minutes_median", ".2f"),
    ("accuracy", "accuracy", ".1%"),
    ("lost/min", "lives_lost_per_minute", ".2f"),
    ("bomb kills", "bomb_kill_share", ".0%"),
    ("power-ups", "powerup_uptime", ".0%"),
    ("combo", "combo_mean", ".2f"),
    ("+lives", "extra_lives_mean", ".2f"),
)
LEGEND = (
    "minutes: median game length; lost/min: lives lost per minute; bomb kills: share of kills made by bombs;\n"
    "power-ups: share of play with one active; combo: mean multiplier per shot kill; +lives: lives gained per game"
)


class PublicView:
    """A bot's window onto a Game: no random stream, no private attributes, no assignment."""

    __slots__ = ("_game",)

    def __init__(self, game: Game):
        object.__setattr__(self, "_game", game)

    def __getattr__(self, name):
        if name.startswith("_") or name == "rng":
            raise AttributeError(f"bots may not read Game.{name}")
        return getattr(self._game, name)

    def __setattr__(self, name, value):
        raise AttributeError("bots may not change the game")


class Formation:
    """Counts the fleet's marching beat from what moves on screen, as a player would."""

    def __init__(self):
        self.wave = self.size = self.slow_ticks = 0
        self.direction, self.clock = 1, 0
        self.phase, self.last = "", {}

    def update(self, game) -> None:
        if game.wave != self.wave:
            # A new fleet opens rightwards on odd waves and leftwards on even ones.
            self.wave, self.size, self.clock = game.wave, len(game.aliens), 0
            self.direction = 1 if game.wave % 2 else -1
        elif self.phase == "playing":
            step = next(
                (
                    alien.x - self.last[id(alien)][1]
                    for alien in game.aliens
                    if not alien.diving and id(alien) in self.last and not self.last[id(alien)][2]
                ),
                0,
            )
            if step:
                self.direction, self.clock = (1 if step > 0 else -1), 0
            else:
                # Power-up timers tick down before the fleet moves.
                self.clock += 0.55 if self.slow_ticks > 1 else 1.0
        self.phase, self.slow_ticks = game.phase, game.powerups.get("slow", 0)
        # Keep the aliens themselves so their ids cannot be reused while compared.
        self.last = {id(alien): (alien, alien.x, alien.diving) for alien in game.aliens}


def snapshot(game, formation: Formation) -> dict:
    boss, ufo = game.boss, game.ufo
    return {
        "phase": game.phase,
        "px": game.player.x,
        "aliens": [(a.x, a.y, a.kind, a.hp, a.diving, a.vx) for a in game.aliens],
        "eshots": [(s.x, s.y, s.vx, s.vy, s) for s in game.shots if not s.friendly],
        "boss": (boss.x, boss.y, boss.vx, boss.hp, boss.max_hp) if boss else None,
        "ufo": (ufo.x, ufo.y, ufo.vx) if ufo else None,
        "pickups": [(p.x, p.y, p.kind) for p in game.pickups],
        "slow": "slow" in game.powerups,
        "dir": formation.direction,
        "clock": formation.clock,
        "wave": game.wave,
        "wave_size": formation.size,
    }


def cadence(s, difficulty):
    beat = max(5, (30 - min(s["wave"], 30)) / difficulty)
    return beat * (0.3 + 0.7 * len(s["aliens"]) / max(1, s["wave_size"]))


def formation_offsets(s, difficulty, horizon):
    """Sideways shift of the marching fleet for each of the next `horizon` ticks, assuming no kills."""
    formation = [a for a in s["aliens"] if not a[4]]
    out = [0] * (horizon + 1)
    if not formation:
        return out
    lo = min(a[0] for a in formation)
    hi = max(a[0] for a in formation)
    slow = 0.55 if s["slow"] else 1.0
    clock, direction, shift = s["clock"], s["dir"], 0
    beat = cadence(s, difficulty)
    for t in range(1, horizon + 1):
        clock += slow
        if clock >= beat:
            clock = 0
            if not (LEFT <= lo + direction and hi + direction <= RIGHT):
                direction *= -1
            shift += direction
            lo += direction
            hi += direction
        out[t] = shift
    return out


def shot_ticks_to(y):
    return max(1, math.ceil((MUZZLE_Y - (y + 0.48)) / SHOT_SPEED))


def row_blocked(bunkers, x, row):
    return any((cx, row) in bunkers and abs(cx - x) <= 0.48 for cx in (math.floor(x), math.ceil(x)))


def lane_blocked(bunkers, x):
    return any(row_blocked(bunkers, x, row) for row in SHELTER_ROWS)


class Bot:
    """Acts on what it saw `delay` ticks ago; aim noise comes from its own random stream."""

    delay = 0

    def __init__(self, difficulty: float, seed: int):
        self.difficulty = difficulty
        self.rng = random.Random(seed * 7919 + 17)
        self.history = deque(maxlen=self.delay + 1)
        self.formation = Formation()

    def observe(self, game) -> dict:
        self.formation.update(game)
        self.history.append(snapshot(game, self.formation))
        return self.history[0]

    def act(self, game) -> tuple[str, ...]:
        raise NotImplementedError


class Novice(Bot):
    """~15-tick reaction delay, noisy aim, slow tapping, no dodging, no bombs."""

    delay = 15

    def __init__(self, difficulty, seed):
        super().__init__(difficulty, seed)
        self.aim = None
        self.retarget = self.next_tap = 0

    def act(self, game):
        s = self.observe(game)
        if s["phase"] != "playing":
            return ()
        px = game.player.x  # a player always knows where their own ship is
        targets = []
        if s["aliens"]:
            columns = {}
            for a in s["aliens"]:
                col = round(a[0])
                if col not in columns or a[1] > columns[col][1]:
                    columns[col] = a
            targets = list(columns.values())
        elif s["boss"]:
            targets = [s["boss"]]
        if not targets:
            return ()
        if self.aim is None or game.tick >= self.retarget:
            target = min(targets, key=lambda a: abs(a[0] - px) + self.rng.uniform(0, 8))
            self.aim = target[0] + self.rng.gauss(0, 1.6)
            self.retarget = game.tick + self.rng.randint(45, 120)
        actions = []
        if self.aim - px > 0.6:
            actions.append("right")
        elif px - self.aim > 0.6:
            actions.append("left")
        if abs(self.aim - px) < 1.4 and game.tick >= self.next_tap:
            actions.append("fire")
            self.next_tap = game.tick + self.rng.randint(14, 28)
        return tuple(actions)


class Naive(Bot):
    """Chases the lowest alien and holds fire; never dodges or bombs."""

    def act(self, game):
        s = self.observe(game)
        if s["phase"] != "playing":
            return ("fire",)
        px = s["px"]
        if s["aliens"]:
            target = max(s["aliens"], key=lambda a: (a[1], -abs(a[0] - px)))[0]
        elif s["boss"]:
            target = s["boss"][0]
        else:
            return ("fire",)
        if target - px > 0.3:
            return ("fire", "right")
        if px - target > 0.3:
            return ("fire", "left")
        return ("fire",)


class Skilled(Bot):
    """Leads targets, dodges the enemy shots it notices, grabs pickups and uses bombs well."""

    delay = 6  # ~100 ms perception latency; extrapolates what it saw
    perceive = 1.0  # share of enemy shots it notices
    bombs = True
    horizon = 48

    def __init__(self, difficulty, seed):
        super().__init__(difficulty, seed)
        holds = (3, 6, 10, 14, 18, 24, 30, 38, 48)
        self.plans = [(0, 0)] + [(d, k) for d in (-1, 1) for k in holds]
        self.seen = {}

    def threats(self, s, game):
        d = self.delay
        sp = 0.55 if s["slow"] else 1.0
        bunkers = game.bunkers  # shelters change slowly enough to read live
        out = []
        if self.perceive < 1.0:
            seen = self.seen
            self.seen = {
                id(shot): seen[id(shot)] if id(shot) in seen else (shot, self.rng.random() < self.perceive)
                for *_, shot in s["eshots"]
            }
        for sx, sy, vx, vy, shot in s["eshots"]:
            if self.perceive < 1.0 and not self.seen[id(shot)][1]:
                continue
            sx, sy = sx + vx * sp * d, sy + vy * sp * d
            if sy > PLAYER_Y + 0.6:
                continue
            blocked = False
            for row in SHELTER_ROWS:
                if sy < row + 0.5:
                    tc = (row - sy) / (vy * sp)
                    if row_blocked(bunkers, sx + vx * sp * tc, row):
                        blocked = True
                        break
            if not blocked:
                out.append((sx, sy, vx * sp, vy * sp, 1.35 + 0.35, 0.45))
        dive_vy = (0.065 + min(s["wave"], 20) * 0.002) * sp
        for ax, ay, _, _, diving, avx in s["aliens"]:
            if diving:
                ax2 = max(LEFT, min(RIGHT, ax + avx * sp * d))
                out.append((ax2, ay + dive_vy * d, avx * sp, dive_vy, 2.0 + 0.3, 0.8))
        return out

    def first_hit(self, threats, px, direction, hold):
        """Earliest tick (1..horizon) a threat overlaps the ship on this plan, else None."""
        best = None
        for sx, sy, vx, vy, rx, ry in threats:
            if vy <= 0:
                continue
            t0 = max(1, math.floor((PLAYER_Y - ry - sy) / vy))
            t1 = math.ceil((PLAYER_Y + ry + 0.6 - sy) / vy)
            for t in range(t0, min(t1, self.horizon) + 1):
                y = sy + vy * t
                if abs(y - PLAYER_Y) > ry + vy:
                    continue
                x = sx + vx * t
                pxt = max(LEFT, min(RIGHT, px + SHIP_SPEED * direction * min(t, hold)))
                if abs(x - pxt) < rx:
                    if best is None or t < best:
                        best = t
                    break
        return best

    def predicted_targets(self, s, px, offs, claimed):
        """(fire_x, cost, kind) candidates: where to stand to hit each target."""
        d = self.delay
        sp = 0.55 if s["slow"] else 1.0
        columns = {}
        for i, a in enumerate(s["aliens"]):
            if a[4] or claimed.get(i, 0) >= a[3]:
                continue
            col = round(a[0])
            if col not in columns or a[1] > columns[col][1]:
                columns[col] = a
        out = []
        for ax, ay, *_ in columns.values():
            k = shot_ticks_to(ay)
            move = 0
            for _ in range(2):
                fx = ax + offs[min(len(offs) - 1, d + move + k)]
                move = int(abs(fx - px) / SHIP_SPEED)
            out.append((fx, move - (ay - 2) * 0.6, "alien"))
        dive_vy = (0.065 + min(s["wave"], 20) * 0.002) * sp
        for ax, ay, _, _, diving, avx in s["aliens"]:
            if diving and ay < PLAYER_Y - 3:
                k = (MUZZLE_Y - ay - dive_vy * d) / (SHOT_SPEED + dive_vy)
                fx = max(LEFT, min(RIGHT, ax + avx * sp * (d + k)))
                out.append((fx, abs(fx - px) / SHIP_SPEED - 12, "diver"))
        if s["boss"]:
            bx, by, bvx, bhp, bmax = s["boss"]
            rage = 1.6 if bhp < bmax / 3 else 1
            fx = bx + bvx * sp * rage * (d + shot_ticks_to(by))
            if not 6 <= fx <= WIDTH - 7:
                fx = 12 - fx if fx < 6 else 2 * (WIDTH - 7) - fx
            out.append((fx, abs(fx - px) / SHIP_SPEED - 4, "boss"))
        if s["ufo"]:
            ux, uy, uvx = s["ufo"]
            fx = ux + uvx * sp * (d + shot_ticks_to(uy) + abs(ux - px) / SHIP_SPEED)
            if LEFT <= fx <= RIGHT:
                out.append((fx, abs(fx - px) / SHIP_SPEED + 6, "ufo"))
        return out

    def alien_x_at(self, s, a, k, offs):
        """Predicted x of alien `a` when a shot fired now has travelled k ticks."""
        ax, _, _, _, diving, avx = a
        if diving:
            sp = 0.55 if s["slow"] else 1.0
            return max(LEFT, min(RIGHT, ax + avx * sp * (self.delay + k)))
        return ax + offs[min(len(offs) - 1, self.delay + k)]

    def lane_hit(self, s, x0, y0, vx, offs, claimed):
        """Index of the first not-yet-doomed alien a shot from (x0, y0) meets, or None."""
        sp = 0.55 if s["slow"] else 1.0
        dive_vy = (0.065 + min(s["wave"], 20) * 0.002) * sp
        best, best_k = None, None
        for i, a in enumerate(s["aliens"]):
            if claimed.get(i, 0) >= a[3]:
                continue
            if a[4]:
                k = (y0 - (a[1] + dive_vy * self.delay)) / (SHOT_SPEED + dive_vy)
            else:
                k = (y0 - (a[1] + 0.48)) / SHOT_SPEED
            if k < 0:
                continue
            k = max(1, math.ceil(k))
            if abs(x0 + vx * k - self.alien_x_at(s, a, k, offs)) < 1.3 and (best_k is None or k < best_k):
                best, best_k = i, k
        return best

    def claims(self, s, game, offs):
        """Damage already on its way to each alien from shots in flight."""
        claimed = {}
        for shot in game.shots:
            if shot.friendly:
                i = self.lane_hit(s, shot.x, shot.y, shot.vx, offs, claimed)
                if i is not None:
                    claimed[i] = claimed.get(i, 0) + shot.damage
        return claimed

    def would_hit(self, s, game, px, offs, claimed):
        d = self.delay
        if lane_blocked(game.bunkers, px):
            return False
        if self.lane_hit(s, px, MUZZLE_Y, 0.0, offs, claimed) is not None:
            return True
        sp = 0.55 if s["slow"] else 1.0
        if s["boss"]:
            bx, by, bvx, bhp, bmax = s["boss"]
            rage = 1.6 if bhp < bmax / 3 else 1
            if abs(bx + bvx * sp * rage * (d + shot_ticks_to(by)) - px) < 3.6:
                return True
        if s["ufo"]:
            ux, uy, uvx = s["ufo"]
            if not any(shot.friendly and shot.y < 8 and abs(shot.x - ux) < 6 for shot in game.shots):
                if abs(ux + uvx * sp * (d + shot_ticks_to(uy)) - px) < 1.7:
                    return True
        return False

    def act(self, game):
        s = self.observe(game)
        if s["phase"] != "playing" or game.phase != "playing":
            return ()
        player = game.player
        px = player.x
        bombs = player.bombs if self.bombs else 0
        bunkers = game.bunkers
        offs = formation_offsets(s, self.difficulty, self.delay + 90)
        claimed = self.claims(s, game, offs)
        # Destination: a reachable pickup first, else the cheapest target to line up under.
        dest = None
        for kx, ky, _ in s["pickups"]:
            t = (MUZZLE_Y - ky) / PICKUP_FALL
            if 0 < t < 90 and abs(kx - px) <= SHIP_SPEED * t - 1:
                dest = kx
                break
        if dest is None:
            candidates = self.predicted_targets(s, px, offs, claimed)
            if candidates:
                dest = min(candidates, key=lambda c: c[1])[0]
                if lane_blocked(bunkers, dest):
                    for off in (1.0, -1.0, 2.0, -2.0):
                        if not lane_blocked(bunkers, dest + off):
                            dest += off
                            break
        dest = px if dest is None else max(LEFT, min(RIGHT, dest))
        # Dodge: if the direct route is hit, take the safe hold plan that ends nearest the destination.
        threats = self.threats(s, game)
        immune = player.invulnerable
        hold = int(abs(dest - px) / SHIP_SPEED)
        move = 0 if hold == 0 else (1 if dest > px else -1)
        if threats:
            hit = self.first_hit(threats, px, move, hold)
            if hit is not None and hit > immune:
                best, best_key = None, None
                for plan in self.plans:
                    h = self.first_hit(threats, px, *plan)
                    safe = h is None or h <= immune
                    end = max(LEFT, min(RIGHT, px + SHIP_SPEED * plan[0] * plan[1]))
                    key = (0 if safe else 1, -(h or 999) if not safe else abs(end - dest))
                    if best_key is None or key < best_key:
                        best, best_key = plan, key
                if best_key[0] == 1 and -best_key[1] <= 4 and bombs and not player.shield:
                    return ("bomb",)
                move = best[0] if best[1] else 0
        # Bomb when the fleet is about to land, or before a full stock wastes charge.
        if bombs:
            if max((a[1] for a in s["aliens"] if not a[4]), default=0) >= 14.5:
                return ("bomb",)
            if bombs == 3 and game.bomb_charge >= 16 and len(s["aliens"]) >= 6:
                return ("bomb",)
        actions = ["right"] if move > 0 else ["left"] if move < 0 else []
        if self.would_hit(s, game, max(LEFT, min(RIGHT, px + SHIP_SPEED * move)), offs, claimed):
            actions.append("fire")
        return tuple(actions)


class NoBomb(Skilled):
    """Skilled play that never bombs, isolating what bombs contribute."""

    bombs = False


class BombHappy(Skilled):
    """Skilled play that bombs as soon as it holds one (exploit probe)."""

    def act(self, game):
        if game.phase == "playing" and game.player.bombs and len(game.aliens) >= 8:
            self.observe(game)  # keep perception and the marching count up to date
            return ("bomb",)
        return super().act(game)


def skilled(delay: int, perceive: int) -> type:
    """Skilled play reacting `delay` ticks late and noticing `perceive` percent of enemy shots."""
    return type(f"sk_d{delay}_p{perceive}", (Skilled,), {"delay": delay, "perceive": perceive / 100})


BOTS = {
    "novice": Novice,
    "naive": Naive,
    "skilled": Skilled,
    **{f"sk_d{d}_p{p}": skilled(d, p) for d in (6, 12, 18, 24, 30) for p in (100, 85, 70)},
    "nobomb": NoBomb,
    "bombhappy": BombHappy,
}
QUICK = ("skilled", "sk_d18_p85", "naive", "novice")


def bot_class(name: str) -> type:
    if name in BOTS:
        return BOTS[name]
    match = re.fullmatch(r"sk_d(\d{1,3})_p(\d{1,3})", name)
    if match and int(match[2]) <= 100:
        return skilled(int(match[1]), int(match[2]))
    raise ValueError(f"unknown bot {name!r}; use all, {', '.join(BOTS)} or sk_d<ticks>_p<percent>")


def play(bot: str, difficulty="normal", mode="classic", seed=0, max_ticks=60 * TICKS_PER_MINUTE) -> dict:
    """Play one game to its end or the tick cap; return its result plus balance counters."""
    game = Game(Config(difficulty, mode, seed))
    pilot = bot_class(bot)(game.difficulty, seed)
    view = PublicView(game)
    counts, powerups = Counter(), Counter()
    damage_waves, wave_ticks = [], [0]
    started = time.perf_counter()
    while game.phase not in ("game_over", "victory") and game.tick < max_ticks:
        actions = pilot.act(view)
        wave, phase, kills, lives = game.wave, game.phase, game.kills, game.player.lives
        combo = game.combo if game.combo_timer > 1 else 0  # a chain whose timer ends this tick restarts
        game.step(actions)
        events = Counter(game.events)
        counts.update(lives_lost=events["damage"], shield_hits=events["shield"], pickups=events["pickup"])
        if events["life"]:
            # Awards past the lives cap add nothing, so count what the ship actually gained too.
            counts.update(life_awards=events["life"], extra_lives=game.player.lives - lives + events["damage"])
        damage_waves += [wave] * events["damage"]
        killed = game.kills - kills
        if events["bomb"]:
            counts.update(bombs_used=1, bomb_kills=killed)
        elif killed:
            counts.update(combo_kills=killed, combo_total=sum(min(game.combo, combo + i) for i in range(1, killed + 1)))
        if phase == "playing":
            counts.update(playing_ticks=1, powered_ticks=int(bool(game.powerups)))
            powerups.update(game.powerups.keys())
        if game.wave != wave:
            wave_ticks.append(game.tick)
    result = game.result()
    if game.phase == "game_over":
        end = "destroyed" if game.player.lives <= 0 else "landed"
    else:
        end = "victory" if result["victory"] else "cap"
    return {
        "bot": bot,
        "difficulty": difficulty,
        "mode": mode,
        "seed": seed,
        **result,
        "end": end,
        "cleared": result["wave"] if result["victory"] else result["wave"] - 1,
        "minutes": result["ticks"] / TICKS_PER_MINUTE,
        **{key: counts[key] for key in COUNTERS},
        "powerup_ticks": dict(sorted(powerups.items())),
        "damage_waves": damage_waves,
        "wave_ticks": wave_ticks,
        "wall": round(time.perf_counter() - started, 3),
    }


def play_job(job: tuple) -> dict:
    return play(*job)


def run_all(jobs: list[tuple], workers: int) -> list[dict]:
    """Play every job, in worker processes when asked; results keep job order."""
    workers = min(workers, len(jobs))
    if workers <= 1:
        return [play_job(job) for job in jobs]
    runs = []
    with multiprocessing.Pool(workers) as pool:
        for run in pool.imap(play_job, jobs):
            runs.append(run)
            if sys.stderr.isatty():
                print(f"\r{len(runs)}/{len(jobs)} games", end="", file=sys.stderr, flush=True)
    if sys.stderr.isatty():
        print("\r\033[K", end="", file=sys.stderr, flush=True)
    return runs


def summarise(runs: list[dict]) -> list[dict]:
    """One row per mode, bot and difficulty, in the order the runs were planned."""
    groups = {}
    for run in runs:
        groups.setdefault((run["mode"], run["bot"], run["difficulty"]), []).append(run)
    rows = []
    for (mode, bot, difficulty), group in groups.items():
        total = Counter()
        for run in group:
            total.update({key: run[key] for key in ("ticks", "shots", "hits", "kills", *COUNTERS)})
        top = max(run["cleared"] for run in group) if mode == "endless" else CAMPAIGN_WAVES
        waves = [run["wave"] for run in group]
        rows.append(
            {
                "mode": mode,
                "bot": bot,
                "difficulty": difficulty,
                "games": len(group),
                "win_rate": sum(run["victory"] for run in group) / len(group),
                "wave_mean": statistics.fmean(waves),
                "wave_median": statistics.median(waves),
                "survival": [sum(run["cleared"] >= wave for run in group) / len(group) for wave in range(1, top + 1)],
                "minutes_median": statistics.median(run["minutes"] for run in group),
                "accuracy": total["hits"] / max(1, total["shots"]),
                "lives_lost_per_minute": total["lives_lost"] / max(1, total["ticks"]) * TICKS_PER_MINUTE,
                "bomb_kill_share": total["bomb_kills"] / max(1, total["kills"]),
                "powerup_uptime": total["powered_ticks"] / max(1, total["playing_ticks"]),
                "combo_mean": total["combo_total"] / max(1, total["combo_kills"]),
                "extra_lives_mean": total["extra_lives"] / len(group),
                "ends": dict(Counter(run["end"] for run in group).most_common()),
            }
        )
    return rows


def checkpoints(top: int, columns=12) -> list[int]:
    """Wave numbers for a survival curve: every wave, or round steps when there are many."""
    step = next(s * 10**e for e in range(9) for s in (1, 2, 5) if top <= s * 10**e * columns)
    return sorted({1, *range(step, top + 1, step)})


def format_report(rows: list[dict]) -> str:
    lines = [f"{'bot':<12} {'difficulty':<10}" + "".join(f"  {title:>6}" for title, _, _ in TABLE)]
    for row in rows:
        cells = (f"  {format(row[key], spec):>{max(6, len(title))}}" for title, key, spec in TABLE)
        lines.append(f"{row['bot']:<12} {row['difficulty']:<10}" + "".join(cells))
    lines.append(LEGEND)
    waves = checkpoints(max((len(row["survival"]) for row in rows), default=1))
    lines += ["", f"{'cleared wave (% games)':<23}" + "".join(f"{wave:>5}" for wave in waves)]
    for row in rows:
        curve = row["survival"]
        cells = (f"{100 * curve[wave - 1]:>5.0f}" if wave <= len(curve) else f"{0:>5}" for wave in waves)
        lines.append(f"{row['bot']:<12} {row['difficulty']:<10}" + "".join(cells))
    return "\n".join(lines)


def counted(kind):
    def parse(value: str):
        try:
            number = kind(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("expected an integer" if kind is int else "expected a number") from exc
        if not number > 0 or not math.isfinite(number):
            raise argparse.ArgumentTypeError("must be positive")
        return number

    return parse


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Bot playtests: win rates, pacing and economy per bot and difficulty."
    )
    command.add_argument(
        "--bots",
        nargs="+",
        default=list(QUICK),
        metavar="BOT",
        help=f"all, {', '.join(BOTS)}, or any sk_d<reaction ticks>_p<%% of shots seen> (default: {' '.join(QUICK)})",
    )
    command.add_argument("--difficulty", nargs="+", choices=(*DIFFICULTIES, "all"), default=["normal"])
    command.add_argument("--mode", choices=("classic", "endless"), default="classic")
    command.add_argument("--seeds", type=counted(int), default=12, help="games per bot and difficulty")
    command.add_argument("--start-seed", type=int, default=1000, help="first game seed")
    command.add_argument("--max-minutes", type=counted(float), default=60.0, help="cap on simulated minutes per game")
    command.add_argument("--jobs", type=counted(int), default=os.cpu_count() or 1, help="worker processes")
    command.add_argument("--json", type=Path, metavar="PATH", help="write every game's metrics as JSON")
    return command


def main(argv: list[str] | None = None) -> int:
    command = parser()
    args = command.parse_args(argv)
    names = [name for value in args.bots for name in value.split(",") if name]
    bots = list(BOTS) if "all" in names else list(dict.fromkeys(names))
    for name in bots:
        try:
            bot_class(name)
        except ValueError as exc:
            command.error(str(exc))
    difficulties = list(DIFFICULTIES) if "all" in args.difficulty else list(dict.fromkeys(args.difficulty))
    seeds = range(args.start_seed, args.start_seed + args.seeds)
    max_ticks = max(1, round(args.max_minutes * TICKS_PER_MINUTE))
    jobs = [(bot, level, args.mode, seed, max_ticks) for bot in bots for level in difficulties for seed in seeds]
    started = time.perf_counter()
    runs = run_all(jobs, args.jobs)
    elapsed = time.perf_counter() - started
    rows = summarise(runs)
    print(
        f"Ruleset {RULESET}, {args.mode}, seeds {seeds.start}-{seeds.stop - 1}, capped at {args.max_minutes:g} min, "
        f"Python {platform.python_version()}: {len(runs)} games in {elapsed:.1f}s (--jobs {min(args.jobs, len(jobs))})"
    )
    print()
    print(format_report(rows))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        settings = {"mode": args.mode, "start_seed": seeds.start, "seeds": len(seeds), "max_ticks": max_ticks}
        data = {"ruleset": RULESET, "python": platform.python_version(), **settings, "summary": rows, "runs": runs}
        args.json.write_text(json.dumps(data) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
