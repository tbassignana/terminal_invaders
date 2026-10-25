"""The lives model with bombs as absorbers (spec §19.1; ported from scratch/spec-author/bombs_model.py).

Fatal events (hits that would cost a life) come per section of a run as Poisson counts: each act's regular waves,
then its boss, weighted REG and BOSS (B's lives model) times one scale s. A share b of them come with a warning the
bot sees in time (no plan safe, the hit inside its reaction window), and a bomb in stock absorbs one of those. Every
other fatal event costs a life. The lives rules are the profile's: its start and cap, one more after each of bosses
1-3, and Last Stand's bomb the first time the ship is down to its last life. Bombs: the profile's start, BOMB.cap,
and BOMB.per_act added at the start of acts II-IV (the one earned during the act before, simplified as the spec
author's model does).

    win(s, b)          P(win) at scale s
    solve(0.675, b)    the scale that wins 67.5% of runs
    budget(0.675, b)   fatal events per act at that scale (b = 0: lives lost per act, §19.1's 0.49 / 0.89 / ...)
    predict(per_act, b) P(win) for measured fatal events per act

It reads no game, only the public tables.
"""

import math

from terminal_invaders.campaign.tables import BOMB, PROFILES

REG = (0.30, 0.55, 0.80, 1.00)  # relative weight of fatal events in each act's regular waves...
BOSS = (0.20, 0.35, 0.55, 0.85)  # ...and in its boss
WEIGHTS = tuple(r + b for r, b in zip(REG, BOSS, strict=True))
ACTS = len(REG)
NORMAL = {"lives": 3, "cap": 5, "bombs": 1, "bomb_cap": 2, "earned": (0, 1, 1, 1), "last_stand": True}


def rules(profile: str = "normal", *, bombs: bool = True) -> dict:
    """A profile's lives and bomb rules, from the tables. bombs=False: a run without any (the nobomb bot)."""
    row = PROFILES[profile]
    out = {"lives": row.lives, "cap": row.cap, "last_stand": row.last_stand}
    if bombs:
        return out | {"bombs": row.bombs, "bomb_cap": BOMB.cap, "earned": (0,) + (BOMB.per_act,) * (ACTS - 1)}
    return out | {"bombs": 0, "bomb_cap": 0, "earned": (0,) * ACTS}


def pois(lam: float, kmax: int = 25) -> list[float]:
    p = [math.exp(-lam)]
    for k in range(1, kmax):
        p.append(p[-1] * lam / k)
    return p


def _add(dist: dict, key, p: float) -> None:
    dist[key] = dist.get(key, 0.0) + p


def section(dist: dict, lam: float, b: float, bomb_cap: int, last_stand: bool) -> dict:
    """dist {(lives, bombs, Last Stand used): p} after a section of Poisson(lam) fatal events, taken one by one."""
    out = {}
    for start, pr in dist.items():
        for k, q in enumerate(pois(lam)):
            states = {start: 1.0}
            for _ in range(k):
                after = {}
                for (left, stock, spent), p in states.items():
                    if left <= 0:
                        _add(after, (left, stock, spent), p)
                        continue
                    rest = 1.0
                    if stock > 0:  # seen in time with a bomb in stock: absorbed
                        _add(after, (left, stock - 1, spent), p * b)
                        rest = 1.0 - b
                    left, spent2 = left - 1, spent
                    if left == 1 and last_stand and not spent:
                        stock, spent2 = min(bomb_cap, stock + BOMB.last_stand), True
                    _add(after, (left, stock, spent2), p * rest)
                states = after
            for key, p in states.items():
                _add(out, key, pr * q * p)
    return out


def predict(per_act, b: float, *, lives=3, cap=5, bombs=1, bomb_cap=2, earned=(0, 1, 1, 1), last_stand=True) -> float:
    """P(a run survives every section) with these fatal events per act, each act's split between its regular waves
    and its boss as REG and BOSS weigh them."""
    dist = {(lives, bombs, False): 1.0}
    for act in range(ACTS):
        if act > 0 and earned[act]:
            grown = {}
            for (left, stock, spent), p in dist.items():
                _add(grown, (left, min(bomb_cap, stock + earned[act]), spent), p)
            dist = grown
        share = REG[act] / WEIGHTS[act]
        for lam, boss in ((per_act[act] * share, False), (per_act[act] * (1 - share), True)):
            dist = section(dist, lam, b, bomb_cap, last_stand)
            dist = {key: p for key, p in dist.items() if key[0] > 0}
            if boss and act < ACTS - 1:  # one more life after bosses 1-3
                grown = {}
                for (left, stock, spent), p in dist.items():
                    _add(grown, (min(cap, left + 1), stock, spent), p)
                dist = grown
    return sum(dist.values())


def win(s: float, b: float, **lives_rules) -> float:
    """P(win) at scale s: s x WEIGHTS fatal events per act."""
    return predict([s * weight for weight in WEIGHTS], b, **lives_rules)


def solve(target: float, b: float, **lives_rules) -> float:
    """The scale s at which a run wins `target` of the time."""
    lo, hi = 0.0, 8.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if win(mid, b, **lives_rules) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def budget(target: float, b: float, **lives_rules) -> list[float]:
    """Fatal events per act a run may have and still win `target` of the time (b = 0: lives lost per act)."""
    s = solve(target, b, **lives_rules)
    return [s * weight for weight in WEIGHTS]
