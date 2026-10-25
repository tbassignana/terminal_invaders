"""The Frame bots (spec §18, §27). A bot sees only `campaign.observe.Frame`s and the public tables.

    pilot = bots.make("strong", seed)
    actions = pilot.act(observe(game))   # once a tick, then game.step(actions)

`strong` (sk_d12_p92) plays like a strong person: the quality bar's strong rows judge it. `skilled` (sk_d6_p100,
"perfect" in reports) reacts a tenth of a second late and notices every shot: it shows each death could be dodged.

No module here imports anything from terminal_invaders but campaign.tables, campaign.observe, input and keymap
(tests/test_bots.py checks).
"""

import re

from . import params, picks
from .pilots import BombHappy, Naive, NoBomb, Novice, Pilot, Skilled, skilled


def _reacting(pair: tuple[int, float]) -> type:
    """The sk_d<D>_p<P> bot of a params pair: (delay in ticks, share of shots noticed at first sight)."""
    delay, share = pair
    return skilled(delay, round(100 * share))


STRONG = _reacting(params.STRONG)  # sk_d12_p92 (spec §27)
GOOD = _reacting(params.GOOD)  # sk_d18_p85
BOTS = {
    "novice": Novice,
    "naive": Naive,
    "strong": STRONG,
    "skilled": Skilled,
    "sk_d18_p85": GOOD,
    "sk_d30_p70": skilled(30, 70),
    "nobomb": NoBomb,
    "bombhappy": BombHappy,
}
# Other names for bots of BOTS (spec §27): skilled is "perfect" in reports, and strong is exactly sk_d12_p92.
ALIASES = {"perfect": "skilled", STRONG.name: "strong"}
SHOWN = {"skilled": "perfect"}  # the name a report prints for a bot
NAME = re.compile(r"sk_d(\d{1,3})_p(\d{1,3})")
FIXED = "fixed:"


def canonical(name: str) -> str:
    """The name a bot's games are kept and judged under: the bot an alias names (perfect: skilled), else itself."""
    return ALIASES.get(name, name)


def shown(name: str) -> str:
    """The name a report prints for a bot (skilled: perfect)."""
    return SHOWN.get(name, name)


def kind(name: str) -> type:
    """The class a bot name stands for: a BOTS name or an alias of one, any sk_d<ticks>_p<percent>, or
    fixed:<code> (skilled play that takes that upgrade whenever it is offered)."""
    name = canonical(name)
    if name in BOTS:
        return BOTS[name]
    match = NAME.fullmatch(name)
    if match and int(match[2]) <= 100:
        return skilled(int(match[1]), int(match[2]))
    if name.startswith(FIXED):
        picks.check(name)
        return type(name, (Skilled,), {"name": name, "policy": name})
    raise ValueError(
        f"unknown bot {name!r}; use {', '.join(BOTS)}, {', '.join(ALIASES)}, sk_d<ticks>_p<percent> or fixed:<code>"
    )


def make(name: str, seed: int, *, policy: str | None = None, keyed: bool | str = False) -> Pilot:
    """A fresh bot for one game. policy: a pick policy instead of the bot's own; keyed: False for direct play, or
    the input its keys go through (balance.py's --input name: it holds card keys longer, and under latched controls
    it plans taps)."""
    return kind(name)(seed, policy=policy, keyed=keyed)


__all__ = ["ALIASES", "BOTS", "Pilot", "canonical", "kind", "make", "shown"]
