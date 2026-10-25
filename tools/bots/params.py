"""The bots' constants (spec §18), frozen.

tests/test_bots.py pins every value here: changing one moves the quality bar, not the game, so it is an owner
decision. The margins were set from the drift test at B-M1 (tests/test_bots.py, `balance.drift`), as the 99th
percentile of each kind's prediction error, and refitted once by the same method for spec §27 (with `strong` among
the drift bots, on the final content); they are never tuned to a win rate.
"""

from types import MappingProxyType

# --- perception (§18 "What they see", "Perception") -----------------------------------------------------------------

SKILLED = (6, 1.0)  # skilled = sk_d6_p100: (delay in ticks, share of shots noticed at first sight); "perfect"
STRONG = (12, 0.92)  # strong = sk_d12_p92, 200 ms late (spec §27): halfway between skilled and the good bot
GOOD = (18, 0.85)  # sk_d18_p85, 300 ms late
LATE = 30  # a lapsed shot is noticed this many ticks after it first appears
TELL_LEAD = 12  # a shot whose shooter showed its tell this many ticks or more before it appeared...
TELL_REACH = 1.5  # ...within this many cells below the shooter, is always noticed
TELL_MEMORY = 90  # ticks a tell is remembered after the pose ends (a burst's later pellets, a spray)
VELOCITY_SPAN = 24  # ticks between the two Frames an enemy's (or a part's, the UFO's) velocity is read from
LAPSE_SALT = 0x5EED_1A75  # mixes the bot's seed with a shot id for the lapse draw


def cadence(delay: int) -> int:
    """Ticks between decisions: 3 for skilled (6 ticks late), 4 for strong (12), 6 for the good bot (18)."""
    return max(3, delay // 3)


# --- prediction (§18 "Prediction", "Margins") ----------------------------------------------------------------------

YOUNG = 15  # a track spanning fewer ticks is a cone of slopes
DENSE = 30  # every Frame's sighting goes into a track until it spans this many ticks; then the decisions' Frames
STILL = 3.0  # a diving enemy unmoved this long on the enemies' clock was frozen unseen (a dive moves 0.25 in 1.4)
ROUNDING = 0.25  # the Frame's grid: the slope of a track spanning s ticks is off by at most ROUNDING / s
# m0 + m1 x t added to a hit box for a prediction t ticks after the sighting, per kind (the planner adds half a
# grid step to m0 for a young track, predict.margin). Set from the drift test (balance.drift) and frozen: the
# lowest line, in steps of 0.01 and 0.001, over the 99th percentile of the error at t = 7 (skilled's first planned
# step), 10, 20, 30, 45 and 60 ticks, taking the highest of any scene with 300 errors or more, for skilled, strong
# and sk_d18_p85 and for a kind's boss and fleet shots alike. 36 immortal staged scenes (waves 1-24, every boss
# phase, easy to hard), each 3 seeds x 20,000 ticks, and the 14 boss-phase scenes of acts III-IV 9 seeds more (they
# end within minutes, too soon for 300 errors at +60). Refitted for spec §27; B-M1's values were pellet (0.12,
# 0.006), dart (0.13, 0.016), hunter (0.17, 0.009), needle (0.11, 0.014), slug (0.08, 0.009), cluster (0.16, 0.003),
# dive (0.09, 0.007), mite (0.08, 0.007). Beams are fixed from their marks, so the drift test has nothing to
# measure: they keep the spec's initial m0.
MARGINS = MappingProxyType(
    {
        "pellet": (0.12, 0.006),
        "dart": (0.09, 0.017),
        "hunter": (0.07, 0.019),
        "needle": (0.05, 0.011),
        "slug": (0.08, 0.009),
        "cluster": (0.15, 0.004),
        "dive": (0.10, 0.007),
        "mite": (0.10, 0.009),
        "beam": (0.15, 0.0),
    }
)

# --- the planner (§18 "Planner") -----------------------------------------------------------------------------------

HORIZON = 48  # ticks the first_hit check looks ahead...
BEAM_HORIZON = 72  # ...while a beam is marked
HOLDS = (3, 6, 10, 14, 18, 24, 30, 38, 48)  # move one way this long, then stand (18 plans, with stand: 19)
FIRST_LEGS = (6, 14, 24)  # two legs: one way this long...
SECOND_LEGS = (6, 14, 24, 38)  # ...then the other way this long (24 plans)
REACTION_EXTRA = 4  # the bomb's reaction window is delay + cadence + this
IMMUNE_SLACK = 2  # ticks before the end of an estimated invulnerability the bot stops counting on it

# --- fire and destination (§18 "Fire") -----------------------------------------------------------------------------

LANDING_ROWS = 3.0  # the lowest flight is the first target once it is within this many rows of landing
AIM_SLACK = 0.25  # a shot is a predicted hit when it passes within the target's half-width less this
PICKUP_SLACK = 0.5  # a pickup is reachable when the ship can get within its catch reach less this

# --- the novice (§18: unchanged from Phase 3) ----------------------------------------------------------------------

NOVICE_DELAY = 15
NOVICE_NOISE = 1.6  # aim noise, cells (gauss sigma)
NOVICE_RETARGET = (45, 120)  # ticks between new aims
NOVICE_TAP = (14, 28)  # ticks between taps of fire
NOVICE_CHOICE = 8.0  # uniform(0, this) added to each target's distance when choosing one
NOVICE_DEADBAND = 0.6  # moves only when the aim is further than this
NOVICE_WINDOW = 1.4  # fires only within this of the aim
RNG_SALT = (7919, 17)  # random.Random(seed x 7919 + 17), as in Phase 3

# --- the card, the prompt and keyed play (§18 "Card and prompt") ---------------------------------------------------

KEY_BEAT = 12  # keyed bots hold each card key this many ticks, then let go as long (TWICE and SETTLE fit inside)
KEY_QUIET = 45  # keyed bots let every key go this long after a phase edge (the 0.7 s repeat guard, and a release)
# Latched controls (§18 "Planner"): a plan is the tap sequence that makes it. IntentDriver (input.py) taps for this
# long (s; its TWICE and SETTLE are read from it), and over the ssh link a tap reaches the game this many steps
# late most often (the link's 0-30 ms jitter and 20 ms slots: 0, 1 or 2 steps, 1 in about half the taps)...
TAP = 0.06
SSH_LAG = 1
# ...and up to this many steps sooner or later than that (0 or 2 steps in the other half; a tap pressed between two
# steps, the second of a reversal, up to one more later: the link takes under 50 ms, SSH_LAG + SSH_SPREAD + 1
# steps, plan.window). A plan the ssh bot flies is safe wherever its taps may land, as a player who knows the link
# is loose leaves room for it. It describes the link, not the player: no delay, lapse share, cadence or plan of the
# bot changes with it. The bot never reads the link itself: tests/test_bots.py measures balance.Keyboard's link
# (Link(slot=0.02, jitter=0.03)) and checks that plan.window with SSH_LAG and SSH_SPREAD is exactly its range.
SSH_SPREAD = 1
