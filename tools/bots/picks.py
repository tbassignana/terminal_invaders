"""Pick policies for the upgrade card (spec §18): which offer a bot takes.

priority: the table's per-stack rank (`Upgrade.priority`), Endless fillers last; first: the leftmost offer; random:
one drawn from the bot's own rng; fixed:<code>: that upgrade whenever it is offered, else priority.
"""

from . import know

POLICIES = ("priority", "first", "random")


def check(policy: str) -> str:
    if policy in POLICIES:
        return policy
    if policy.startswith("fixed:"):
        code = policy.split(":", 1)[1]
        if code in {know.code(name) for name in know.UPGRADES}:
            return policy
        raise ValueError(f"no upgrade has the code {code!r}")
    raise ValueError(f"unknown pick policy {policy!r}; use {', '.join(POLICIES)} or fixed:<code>")


def choose(policy: str, offers: tuple[str, ...], ship, rng) -> int:
    """The index of the offer the policy takes."""
    if not offers:
        return 0
    if policy == "first":
        return 0
    if policy == "random":
        return rng.randrange(len(offers))
    if policy.startswith("fixed:"):
        code = policy.split(":", 1)[1]
        for index, name in enumerate(offers):
            if know.code(name) == code:
                return index
    ranks = [know.priority(name, know.stacks(ship, name)) for name in offers]
    return ranks.index(min(ranks))
