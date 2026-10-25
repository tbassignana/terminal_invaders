"""The Frame bots of spec §18 and §27 (tools/bots): what they see and when, how they predict and plan, the drift test
that set their margins, their card and prompt handling, and the harness's logs (tools/balance.py). `strong`
(sk_d12_p92) is the bot the quality bar's strong rows judge; `skilled` ("perfect" in reports) shows every death
could be dodged."""

import ast
import dataclasses
import importlib.util
import math
import sys
from pathlib import Path

import pytest

from terminal_invaders.campaign.observe import Seen, observe
from terminal_invaders.campaign.tables import LIMITS, SHOTS, TIMING
from terminal_invaders.campaign.testing import staged
from terminal_invaders.model import TICK_RATE, Config

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
BOTS = TOOLS / "bots"
ALLOWED = {
    "terminal_invaders.campaign.tables",
    "terminal_invaders.campaign.observe",
    "terminal_invaders.input",
    "terminal_invaders.keymap",
}
# The drift test's scenes: (wave, boss phase, difficulty, bot). Together they fly every shot kind, from the fleet
# and from a boss, dives and mites, and every boss phase of acts III-IV, for the skilled bot, the strong one (12 ticks
# late) and the good one (18 ticks late).
SCENES = (
    (1, 0, "normal", "skilled"),  # pellets
    (5, 0, "normal", "skilled"),  # darts, dives
    (6, 1, "normal", "skilled"),  # WARDEN
    (11, 0, "normal", "skilled"),  # needles
    (13, 0, "normal", "skilled"),  # slugs
    (15, 0, "normal", "skilled"),  # clusters, mites
    (16, 0, "hard", "skilled"),  # hunter darts
    (18, 0, "normal", "skilled"),  # DREADNOUGHT's broadside: slug salvos, curtains
    (18, 1, "normal", "skilled"),  # its beam phase
    (18, 2, "normal", "skilled"),  # its last phase
    (24, 0, "normal", "skilled"),  # MOTHERSHIP: its escort phase
    (24, 1, "normal", "skilled"),  # its pods orbit the core and fire hunters
    (24, 2, "normal", "skilled"),
    (24, 3, "normal", "skilled"),
    (5, 0, "normal", "strong"),
    (15, 0, "normal", "strong"),
    (16, 0, "hard", "strong"),
    (18, 0, "normal", "strong"),
    (24, 1, "normal", "strong"),
    (12, 1, "normal", "sk_d18_p85"),  # HIVE QUEEN
    (20, 0, "normal", "sk_d18_p85"),  # squadron dives
)
SCENE_TICKS = 6000
STRAIGHT_BOUND, SPREAD_BOUND = 0.5, 1.5  # drift at +30 ticks: shots; clusters and dives (spec §22 B)


@pytest.fixture(scope="module")
def tools():
    """tools/balance.py and tools/bots, imported the way balance.py imports them."""
    added = [str(path) for path in (ROOT, TOOLS) if str(path) not in sys.path]
    for path in added:
        sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location("balance_for_bots", TOOLS / "balance.py")
    balance = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = balance
    try:
        spec.loader.exec_module(balance)
        import bots

        yield balance, bots
    finally:
        del sys.modules[spec.name]
        for path in added:
            sys.path.remove(path)


@pytest.fixture(scope="module")
def drift(tools):
    """Every scene's predictions against the engine: {(bot, category): {"mature": {h: errors}, "young": [...]}}."""
    balance, _bots = tools
    merged = {}
    for wave, phase, difficulty, bot in SCENES:
        out = balance.drift(
            wave,
            SCENE_TICKS,
            bot=bot,
            seed=wave,
            difficulty=difficulty,
            boss_phase=phase,
            horizons=(10, 20, 30, 45, 60),
        )
        for category, record in out.items():
            into = merged.setdefault((bot, category), {"mature": {}, "young": []})
            for h, errors in record["mature"].items():
                into["mature"].setdefault(h, []).extend(errors)
            into["young"] += record["young"]
    return merged


def allowed(n: int) -> float:
    """How many of n draws may lie past a true 99th percentile: 1%, plus three standard deviations of sampling."""
    return n / 100 + 3 * math.sqrt(n * 0.0099)


# --- the drift test (spec §18 "Prediction", "Margins") ---------------------------------------------------------------


def test_the_drift_test_flies_every_kind(drift):
    categories = {category for _bot, category in drift}
    kinds = {"pellet", "dart", "hunter", "needle", "slug", "cluster", "dive", "mite"}
    assert kinds <= categories
    assert {"boss pellet", "boss dart", "boss needle", "boss slug", "boss hunter", "boss cluster"} <= categories
    assert set(SHOTS) == kinds - {"dive", "mite"}  # a new shot kind needs a scene here


def test_tracks_spanning_15_ticks_drift_within_the_spec_bounds_at_30_ticks(drift):
    """At +30 ticks after the last sighting, 99% of mature predictions lie within 0.5 cells for shots and 1.5 for a
    cluster's pellets and dives (the whole-track least-squares slope over every Frame's sighting)."""
    checked = 0
    for (bot, category), record in drift.items():
        errors = record["mature"].get(30, [])
        if len(errors) < 30:
            continue
        kind = category.split()[-1]
        bound = SPREAD_BOUND if kind in ("cluster", "dive", "mite") else STRAIGHT_BOUND
        over = sum(error > bound for error in errors)
        assert over <= allowed(len(errors)), (bot, category, over, len(errors))
        checked += 1
    assert checked >= 15


def test_the_margins_cover_the_measured_99th_percentile(tools, drift):
    """params.MARGINS (refitted for spec §27 from 36 scenes x 3 seeds and the act III-IV boss phases x 12, for
    skilled, strong and the good bot, on other seeds) still cover this sample: at every horizon, no more than 1% of
    errors (allowing for sampling) lie past m0 + m1 x t."""
    _balance, bots = tools
    margins = bots.params.MARGINS
    for (bot, category), record in drift.items():
        m0, m1 = margins[category.split()[-1]]
        for h, errors in record["mature"].items():
            if len(errors) < 30:
                continue
            over = sum(error > m0 + m1 * h for error in errors)
            assert over <= allowed(len(errors)), (bot, category, h, over, len(errors))


def test_young_tracks_lie_inside_their_cone(drift):
    """A track spanning under 15 ticks is a cone of slopes (a shot seen once: its kind's speeds and vx bound; a
    split pellet: the split's vx and the cluster's vy; a dive: the dive rule), widened by the planner's margin."""
    checked = 0
    for (bot, category), record in drift.items():
        young = record["young"]
        if len(young) < 30:
            continue
        misses = len(young) - sum(young)
        assert misses <= allowed(len(young)), (bot, category, misses, len(young))
        checked += 1
    assert checked >= 15


def test_a_cluster_seen_standing_still_is_no_threat(tools):
    """An immortal staged game's hit freeze is invisible to the bot (its lives never drop), so a cluster first seen
    in it stands still: its fitted fall is 0. The planner skips it, as sweep() skips any shot not falling, instead
    of dividing by that fall (it did, in the drift test's MOTHERSHIP and HIVE QUEEN scenes)."""
    _balance, bots = tools
    sense = bots.track.Perception(6, 1.0, 3)
    for frame in frames_of(Config("normal", "classic", 3), 15, 300, bots):
        sense.see(frame)
        sense.ingest()
    sense.shots.clear()
    sense.dives.clear()
    tick = sense.seen.tick
    w = sense.clock_at(tick)
    still = bots.track.Track("cluster", tick - 20, w - 20, 30.0, 4.0)
    for back in range(19, -1, -1):
        still.add(tick - back, w - back, 30.0, 4.0)
    assert still.fit()[3] == 0.0 and bots.predict.cone(still, "cluster", 15, "normal", 4.0)[5] == 0.0
    sense.shots[10**9] = still
    threats = bots.predict.build(sense, bots.params.HORIZON, 0, set())
    assert not any(lo < 30.0 < hi for spans in threats.forb.values() for lo, hi in spans)


def test_a_dive_frozen_unseen_under_slow_starts_a_new_track(tools):
    """A boss's phase break stops the fleet where the bot cannot see it, so a diving mite stands still while the
    bot's clock runs. Under SLOW the skilled bot decides every 1.8 enemy ticks, under STILL: the track must still
    start again once the mite has stood STILL ticks, and once more at its first move, or the still sightings
    flatten its slope (the drift test's MOTHERSHIP armada scene once predicted a mite 6 cells short)."""
    _balance, bots = tools
    template = frames_of(Config("normal", "classic", 3), 15, 30, bots)[-1]
    ship = template.ship._replace(pickups=(("slow", 5),), lives=3, blinking=False, guarded=False)
    vy = bots.know.dive_vy("mite", "normal")  # a cell an enemy tick; SLOW runs the enemies at 0.6
    sense, y = bots.track.Perception(6, 1.0, 3), 1.0
    for tick in range(1, 161):
        if not 60 <= tick < 100:  # ticks 60-99: stopped, unseen
            y += vy * bots.know.SLOW
        mite = Seen(999, 30.0, round(y * 4) / 4, "mite", "diving")
        frame = dataclasses.replace(
            template, tick=tick, phase="playing", ship=ship, enemies=(mite,), shots=(), parts=(), hazards=()
        )
        sense.see(frame)
        if tick % 3 == 0:  # the skilled bot's cadence
            sense.ingest()
    track = sense.dives[999]
    assert track.w0 == pytest.approx(sense.clock_at(100))  # it starts at the first move after the stop
    assert track.fit()[3] == pytest.approx(vy, abs=0.01)


def test_a_tracking_beam_is_predicted_at_its_latest_x_until_it_locks(tools):
    _balance, bots = tools
    game = staged(Config("normal", "classic", 4), 18, boss_phase=1, immortal=True)
    pilot = bots.make("skilled", 4)
    sense, predict = pilot.sense, bots.predict
    unlocked, locked = {}, {}
    for _ in range(3000):
        pilot.act(observe(game))
        beams = [hazard for hazard in sense.seen.hazards if hazard.kind == "beam" and hazard.id in sense.marks]
        if beams and game.phase == "playing":
            threats = predict.Threats(bots.params.BEAM_HORIZON, 0)
            predict._beams(sense, threats, sense.clock_at(sense.now.tick), 1.0)
            spans = {span for spans in threats.forb.values() for span in spans}
            reach = bots.know.BOX_X + bots.params.MARGINS["beam"][0]
            for hazard in beams:
                if spans:
                    assert (hazard.x0 - reach, hazard.x1 + reach) in spans
                (locked if hazard.locked else unlocked).setdefault(hazard.id, set()).add(hazard.x0)
        game.step(pilot.last)
    assert unlocked and any(len(xs) > 1 for xs in unlocked.values())  # it moved, and the bot followed it
    assert locked and all(len(xs) == 1 for xs in locked.values())  # once locked, one x


# --- perception (§18 "What they see", "Perception") ----------------------------------------------------------------


def frames_of(config, wave, ticks, bots, bot="naive"):
    """Frames of a staged game played by a simple bot, to feed perceptions directly."""
    game = staged(config, wave, immortal=True)
    pilot, frames = bots.make(bot, config.seed), []
    for _ in range(ticks):
        frame = observe(game)
        frames.append(frame)
        game.step(pilot.act(frame))
    return frames


# The lapse-share checks pool wave 11 of these seeds: about 240 shots no tell gave away, against about 48 for one
# seed. On one seed, binomial noise alone put the shares outside their bounds whenever a Normal table change moved
# waves 1-11.
LAPSE_SEEDS = range(9, 14)
LAPSE_SAMPLE = 200  # at least this many untold shots in the pool, or the 3 SD bounds below no longer hold


@pytest.fixture(scope="module")
def lapse_frames(tools):
    """seed -> wave 11's Frames, Normal, 6,000 ticks played by the naive bot: its pellets and told needles. E-M2's
    HIVE QUEEN (wave 12) tells nearly every shot, so few lapse there."""
    _balance, bots = tools
    return {seed: frames_of(Config("normal", "classic", seed), 11, 6000, bots) for seed in LAPSE_SEEDS}


def test_own_ship_from_the_current_frame_the_rest_from_tick_minus_delay(tools):
    _balance, bots = tools
    frames = frames_of(Config("normal", "classic", 2), 5, 900, bots)
    for delay in (6, 18):
        sense = bots.track.Perception(delay, 1.0, 2)
        first = {}
        for frame in frames:
            for shot in frame.shots:
                if not shot.friendly:
                    first.setdefault(shot.id, frame.tick)
            sense.see(frame)
            sense.ingest()
            assert sense.now is frame
            assert sense.seen.tick == max(frames[0].tick, frame.tick - delay)
            for ident in sense.shots:
                assert frame.tick - first[ident] >= delay  # an enemy shot is known only delay ticks after it appears
    pilot = bots.make("skilled", 2)
    for frame in frames[:300]:
        pilot.act(frame)
    assert pilot.sense.now is frames[299] and pilot.sense.seen is frames[299 - pilot.delay]


def test_lapses_are_keyed_by_shot_id_whatever_the_cadence(tools, lapse_frames):
    """A shot lapses when its id's draw is at or above perceive and no tell gave it away: the same shots whatever
    the decision cadence, and none but those. About 15% of the shots no tell gave away lapse."""
    _balance, bots = tools
    track = bots.track
    assert track.unit(7, 1234) == track.unit(7, 1234) != track.unit(8, 1234)
    draws = [track.unit(3, ident) for ident in range(20000)]
    assert abs(sum(draw >= 0.85 for draw in draws) / len(draws) - 0.15) < 0.01

    def lapsed(frames, seed, perceive, cadence):
        sense, late = track.Perception(18, perceive, seed), set()
        for index, frame in enumerate(frames):
            sense.see(frame)
            if index % cadence == 0:
                sense.ingest()
            late |= sense.late
        return late

    lapses = untold_shots = 0
    for seed, frames in lapse_frames.items():
        untold = lapsed(frames, seed, 0.0, 1)  # with perceive 0, every shot no tell gave away lapses
        expected = {ident for ident in untold if track.unit(seed, ident) >= 0.85}
        assert all(lapsed(frames, seed, 0.85, cadence) == expected for cadence in (1, 3, 6, 11)), seed
        lapses, untold_shots = lapses + len(expected), untold_shots + len(untold)
    # the share of the shots no tell gave away (a told shot never lapses), pooled; the bounds are about 3 SD
    assert untold_shots >= LAPSE_SAMPLE
    assert 0.08 < lapses / untold_shots < 0.22  # about 15%


def test_a_shot_from_a_told_shooter_is_never_late(tools):
    """With perceive 0 every shot lapses, except one that appears just below a shooter whose tell was seen at least
    12 ticks before (lancers glow before their needles, gunners open their eye before they aim)."""
    _balance, bots = tools
    frames = frames_of(Config("normal", "classic", 5), 11, 3000, bots)
    sense = bots.track.Perception(18, 0.0, 5)
    seen = set()
    for frame in frames:
        sense.see(frame)
        seen |= {shot.id for shot in sense.seen.shots if not shot.friendly}
    told = {ident for ident in seen if ident in sense.notice and ident not in sense.late}
    assert told and sense.late and told | sense.late >= seen - set(sense.notice)


def test_strong_is_sk_d12_p92_and_lapses_like_the_good_bot(tools, lapse_frames):
    """Spec §27: strong reacts 12 ticks late, decides every 4 ticks, notices 92% of enemy shots at first sight and
    the other 8% 30 ticks late, and never lapses on a shot from a shooter whose tell it saw, as sk_d18_p85. The
    generic sk_d<D>_p<P> path builds it: the bot of that name plays the same, tick for tick."""
    _balance, bots = tools
    track, params = bots.track, bots.params
    generic = bots.pilots.skilled(12, 92)
    strong = bots.make("strong", 9)
    assert (generic.delay, generic.perceive, generic.__mro__[1]) == (12, 0.92, bots.Skilled)
    assert (strong.cadence, strong.window, params.LATE) == (4, 12 + 4 + params.REACTION_EXTRA, 30)
    assert strong.window - strong.delay - strong.cadence == bots.make("sk_d18_p85", 9).window - 18 - 6
    # the same decisions as the generic class's, on the same Frames
    game, twin = staged(Config("normal", "classic", 9), 8, immortal=True), generic(9)
    for _ in range(1500):
        frame = observe(game)
        actions = strong.act(frame)
        assert twin.act(frame) == actions, frame.tick
        game.step(actions)
    assert strong.decisions == twin.decisions > 300
    # lapses: 8% of the shots no tell gave away, each noticed 30 ticks after its first sighting

    def watch(frames, sense):
        """Every shot the perception noticed, with the seen tick it noticed it at, and those it noticed late."""
        noticed, late = {}, set()
        for frame in frames:
            sense.see(frame)
            sense.ingest()
            for ident, tick in sense.notice.items():
                noticed.setdefault(ident, tick)
            late |= sense.late
        return noticed, late

    lapses = untold_shots = 0
    for seed, frames in lapse_frames.items():
        first = {}
        for frame in frames:
            for shot in frame.shots:
                if not shot.friendly:
                    first.setdefault(shot.id, frame.tick)
        noticed, late = watch(frames, bots.make("strong", seed).sense)
        untold = {
            name: watch(frames, track.Perception(delay, 0.0, seed)) for name, delay in (("strong", 12), ("good", 18))
        }
        told_by = {name: set(seen) - lapsed for name, (seen, lapsed) in untold.items()}
        assert told_by["strong"] & set(untold["good"][0]) == told_by["good"] and told_by["good"], seed  # one tell rule
        expected = {ident for ident in untold["strong"][1] if track.unit(seed, ident) >= 0.92}
        assert late == expected, seed
        assert all(noticed[ident] == first[ident] + params.LATE for ident in expected), seed
        lapses, untold_shots = lapses + len(expected), untold_shots + len(untold["strong"][1])
    # the share of the shots no tell gave away (a told shot never lapses), pooled; the bounds are about 3 SD
    assert untold_shots >= LAPSE_SAMPLE
    assert 0.03 < lapses / untold_shots < 0.13  # about 8%


# --- the planner (§18 "Planner") -----------------------------------------------------------------------------------


def test_the_plan_library_is_the_43_plans(tools):
    _balance, bots = tools
    plan = bots.plan
    holds, first, second = (3, 6, 10, 14, 18, 24, 30, 38, 48), (6, 14, 24), (6, 14, 24, 38)
    expected = [(0, 0, 0, 0)] + [(d, k, 0, 0) for d in (-1, 1) for k in holds]
    expected += [(d, k, -d, m) for d in (-1, 1) for k in first for m in second]
    assert list(plan.PLANS) == expected and len(set(plan.PLANS)) == 43 and plan.STAND == (0, 0, 0, 0)
    assert [plan.direction((1, 3, -1, 2), step) for step in range(1, 8)] == [1, 1, 1, -1, -1, 0, 0]
    assert plan.position((1, 48, 0, 0), 55.0, 0.4, 48) == bots.know.X_MAX  # the field clamps it
    assert plan.position((-1, 6, 1, 14), 20.0, 0.5, 20) == pytest.approx(24.0)


def test_the_planner_takes_the_safe_plan_nearest_the_destination(tools):
    _balance, bots = tools
    plan, predict = bots.plan, bots.predict
    threats = predict.Threats(48, 0)
    for k in range(8, 20):
        threats.add(k, 18.0, 22.0)  # a column over the ship, from step 8
    threats.finish()
    assert plan.first_hit(threats, plan.STAND, 20.0, 0.4) == 8
    chosen, hit = plan.choose(threats, 20.0, 0.4, 20.0)
    assert hit is None and plan.first_hit(threats, chosen, 20.0, 0.4) is None
    assert chosen == (-1, 14, 1, 14)  # out of the column's way, then back to the destination
    walls = predict.Threats(48, 0)
    for k in range(1, 49):
        walls.add(k, 0.0, 60.0)
    chosen, hit = plan.choose(walls.finish(), 20.0, 0.4, 20.0)
    assert hit == 1  # nothing is safe: the plan hit last
    immune = predict.Threats(48, 10)
    for k in range(1, 49):
        immune.add(k, 18.0, 22.0)
    assert plan.first_hit(immune.finish(), plan.STAND, 20.0, 0.4) == 11  # invulnerable for the first 10 steps


def test_the_planner_keeps_its_course_and_counts_a_detour_twice(tools):
    """By the left wall, heading for x 19.5: the plan whose first leg the wall cuts short ends nearer, but it goes
    away first, and what is left of the plan being flown beats starting a new one."""
    _balance, bots = tools
    plan, predict = bots.plan, bots.predict
    assert [plan.remainder((1, 3, -1, 2), done) for done in (0, 2, 4, 5)] == [
        (1, 3, -1, 2),
        (1, 1, -1, 2),
        (-1, 1, 0, 0),
        None,
    ]
    speed, dest, x0 = 0.45, 19.5, 3.5
    wall = bots.know.X_MIN
    assert plan.cost((-1, 6, 1, 38), x0, speed, dest) == pytest.approx(abs(wall + 38 * speed - dest) + 2 * (x0 - wall))
    assert plan.cost((1, 38, 0, 0), x0, speed, dest) == pytest.approx(x0 + 38 * speed - dest)
    nothing = predict.Threats(48, 0).finish()
    assert plan.choose(nothing, x0, speed, dest) == ((1, 38, 0, 0), None)
    assert plan.choose(nothing, x0, speed, dest, (1, 35, 0, 0)) == ((1, 35, 0, 0), None)  # ends at 19.25


def test_a_shot_counts_for_what_it_meets_first(tools):
    """MOTHERSHIP's second phase: its pods orbit the shielded core, two in front of it and two behind. Under a pod
    in front the bot fires; under the core with no pod in front it does not. It stands at the nearest x under a
    part that no own shelter covers, and fires through its own shelter only at a flight about to land."""
    _balance, bots = tools
    game = staged(Config("normal", "classic", 3), 24, boss_phase=1, immortal=True)
    pilot = bots.make("skilled", 3)
    for _ in range(200):
        frame = observe(game)
        pilot.act(frame)
        game.step(())
    frame = observe(game)
    pilot.act(frame)
    targets, base, f = pilot.targets(frame)
    core = next(target for target in targets if target.block)
    pods = [target for target in targets if not target.block and abs(target.x - core.x) < core.half]
    front = [pod for pod in pods if pod.y > core.y]
    assert front and any(pod.y < core.y for pod in pods)  # one in front of the core and one behind it
    assert all(pilot.lane(pod.x, frame, targets, {}, base, f, set()) for pod in front)
    alone = [  # under the core, and under no pod in front of it (a pod behind it is hidden by the core)
        x / 4
        for x in range(round(4 * (core.x - core.half)) + 1, round(4 * (core.x + core.half)))
        if all(abs(x / 4 - pod.x) > pod.half for pod in front)
    ]
    assert alone and not any(pilot.lane(x, frame, targets, {}, base, f, set()) for x in alone)
    cells = {(column, row) for column in (23, 24, 25, 26) for row in bots.know.SHELTER_ROWS}
    assert pilot.spot(23.0, 22.75, 27.25, cells) == 26.75  # clear of the cell at 26 by more than a grid step
    assert pilot.spot(24.0, 23.0, 26.0, cells) is None and pilot.spot(30.0, 28.0, 32.0, cells) == 30.0
    target = bots.pilots.Target
    landing = target(1, 25.0, 13.5, 0.0, 0.0, 1.35, 0.5, 1, 10, 0)  # tier 0: its flight is about to land
    high = target(2, 25.0, 9.0, 0.0, 0.0, 1.35, 0.5, 1, 10, 3)
    assert pilot.lane(25.0, frame, [landing], {}, 0.0, 1.0, cells)  # it digs through its own shelter for that
    assert not pilot.lane(25.0, frame, [high], {}, 0.0, 1.0, cells)
    assert pilot.lane(25.0, frame, [high], {}, 0.0, 1.0, set())
    # with nothing else to hit, it stands under the covered one and digs through
    assert not any(hazard.kind == "gap" for hazard in pilot.sense.seen.hazards)
    pilot.pickup = lambda frame: None  # no pickup to fetch first
    speed = bots.know.ship_speed(frame.ship)
    assert pilot.destination(frame, [high], {}, 0.0, 1.0, 20.0, speed, cells) == 25.0 and pilot.dig
    assert pilot.lane(25.0, frame, [high], {}, 0.0, 1.0, cells)
    clear = target(3, 45.0, 9.0, 0.0, 0.0, 1.35, 0.5, 1, 10, 3)
    assert pilot.destination(frame, [high, clear], {}, 0.0, 1.0, 20.0, speed, cells) == 45.0 and not pilot.dig


@pytest.mark.parametrize(
    ("x", "columns", "upgrades"),
    [
        (8.24, (), ()),  # the core's left edge: its spot (8.5) is nearer than half the smallest move (1.2 cells)
        (9.16, (7, 8, 9, 11, 12), ("THRUSTERS", "THRUSTERS")),  # under its own shelter, beside a one-cell hole
    ],
    ids=["edge", "shelter"],
)
def test_the_ship_never_stands_where_it_cannot_hit_a_boss_part_that_holds_still(tools, x, columns, upgrades):
    """DREADNOUGHT's beam phase moves the boss only while its beam tracks the ship, so with the ship beyond the edge
    of its range the hull holds there (x 12.5). The ship stops up to half its smallest move from its spot, which
    once left it standing to the time cap where no shot hits: Endless seeds 6092 (wave 36) and 6059 (wave 90),
    Easy seed 1087 (wave 18). It steps to where a shot hits, and fires."""
    from terminal_invaders.campaign import boss as bosses

    _balance, bots = tools
    game = staged(Config("normal", "classic", 3), 18, boss_phase=1, upgrades=upgrades, immortal=True)
    game.aliens, game.pickups = [], []  # nothing else to shoot or fetch: only the core
    game.bunkers = {cell: hp for cell, hp in game.bunkers.items() if cell[0] in columns}
    boss, row = game.boss, bosses.row_of(game)
    assert (boss.phase, row.phases[boss.phase].move) == (1, "track")
    boss.x = bosses._bounds(row)[0]
    bosses._place(boss, row, game._motion.orbit)
    game.player.x = x
    core = next(part for part in boss.parts if part.name == "core")
    pilot, start = bots.make("strong", 3), core.hp
    frame = observe(game)
    pilot.act(frame)
    targets, base, f = pilot.targets(frame)
    assert not pilot.lane(frame.ship.x, frame, targets, {}, base, f, pilot.cells(frame.shelters))  # no hit from here
    fired = 0
    for _ in range(600):
        actions = pilot.act(observe(game))
        fired += "fire" in actions
        game.step(actions)
    assert boss.x == bosses._bounds(row)[0]  # the hull held still all along
    assert fired and core.hp < start


@pytest.mark.parametrize(
    ("x", "keyed", "upgrades"),
    [
        (8.24, False, ("THRUSTERS",)),  # Hard seed 1041: it stood at 8.25 for a spot at 8.44, by way of the wall
        (8.24, "latched", ("THRUSTERS",)),
        (8.05, "latched", ()),
    ],
    ids=["direct", "latched-thrusters", "latched"],
)
def test_the_plan_flown_toward_the_spot_to_fire_from_stops_where_a_shot_hits(tools, x, keyed, upgrades):
    """DREADNOUGHT's beam phase with the hull held at its left bound (the core at 12.5; a shot hits it only between
    x 8.25 and 16.75). Hard seeds 1041 and 1019 (wave 18) and Endless 6032 (at the right wall) stood still to their time caps:
    the bot sent the ship to the end of a plan the planner would not fly there. 'Left 24 ticks, then right 14' ends
    at 8.44 by way of the wall at 2.0, and choose() counts that detour twice, so toward 8.44 it stood. The plan the
    planner flies toward the destination now stops where a shot hits, under latched controls too, and it fires."""
    from terminal_invaders.campaign import boss as bosses

    _balance, bots = tools
    planner, nothing = bots.plan, bots.pilots.NOTHING
    # the cause, at the 1041 ship's speed (0.40 and one THRUSTERS stack of 0.06, today's tables)
    assert planner.position((-1, 24, 1, 14), 8.25, 0.46, 38) == pytest.approx(8.44)
    assert planner.choose(nothing, 8.25, 0.46, 8.44) == (planner.STAND, None)
    game = staged(Config("normal", "classic", 3), 18, boss_phase=1, upgrades=upgrades, immortal=True)
    game.aliens, game.pickups, game.bunkers = [], [], {}  # only the core to shoot, nothing to fetch, no shelter
    boss, row = game.boss, bosses.row_of(game)
    boss.x = bosses._bounds(row)[0]
    bosses._place(boss, row, game._motion.orbit)
    game.player.x = x
    core = next(part for part in boss.parts if part.name == "core")
    pilot, start = bots.make("strong", 3, keyed=keyed), core.hp
    frame = observe(game)
    pilot.act(frame)
    targets, base, f = pilot.targets(frame)
    cells = pilot.cells(frame.shelters)
    x0, speed = frame.ship.x, bots.know.ship_speed(frame.ship)
    assert not pilot.lane(x0, frame, targets, {}, base, f, cells)  # no hit from where it stands
    dest = pilot.destination(frame, targets, {}, base, f, x0, speed, cells)
    flown = planner.choose(nothing, x0, speed, dest, None, pilot.keys, frame.tick)
    motion = flown[0] if pilot.keys is None else flown[2]
    stop = planner.position(motion, x0, speed, sum(motion[1::2]))
    assert stop != x0 and pilot.lane(stop, frame, targets, {}, base, f, cells), (dest, motion, stop)
    fired = 0
    for _ in range(600):
        actions = pilot.act(observe(game))
        fired += "fire" in actions
        game.step(actions)
    assert fired and core.hp < start


# --- latched controls: every plan is the taps that make it (§18 "Planner") ------------------------------------------


def test_the_bots_key_mirror_keeps_the_drivers_state(tools):
    """Latched.key keys by IntentDriver's rules (TWICE, SETTLE and its tap length): the same direction and the same
    next ready time, whatever the bot asks for and when."""
    import random

    balance, bots = tools
    keyboard = balance.Keyboard("latched", Config("normal", "classic", 1))
    driver, mirror, rng = keyboard.driver, bots.plan.Latched(), random.Random(7)
    assert (driver.TWICE, driver.SETTLE) == (bots.plan.TWICE, bots.plan.SETTLE)
    want = 0
    for tick in range(3000):
        if rng.random() < 0.08:
            want = rng.choice((-1, 0, 1))
        driver.intend(tick, frozenset({-1: {"left"}, 0: set(), 1: {"right"}}[want]))
        mirror.key(tick, want)
        assert (mirror.dir, mirror.ready) == (driver.direction, driver.ready), tick


@pytest.mark.parametrize("mode", ["latched", "timed", "latched-ssh"])
def test_every_plan_can_be_keyed_under_latched_controls(tools, mode):
    """Each of the 43 plans, keyed from a standing ship through the game's own input path: the ship flies the motion
    the bot predicts for it, exactly on a local terminal (latched or timed controls: both are keyed with taps), and
    over ssh inside the band its taps' jitter allows around it (plan.band over each tap's plan.window)."""
    balance, bots = tools
    plan = bots.plan
    speed = bots.know.PLAYER.speed
    worst = outside = 0.0
    # each seed is another draw of the link's jitter (at seed 2 a reversal's second tap lands three steps late)
    for seed in (2, 5, 6) if mode == "latched-ssh" else (5,):
        config = Config("normal", "classic", seed)
        keyboard = balance.Keyboard(mode, config)
        game = staged(config, 1, phase="playing", director={"reload": 10**6})
        keyboard.session.pilot.game = game
        keys = plan.Latched(*bots.pilots.LATCHED[mode])
        for flown in plan.PLANS:
            for _ in range(40):  # until the driver may key again and the ship stands
                keys.key(game.tick, 0)
                keyboard.step(())
            game.player.x = 30.0
            windows = []
            motion, start, x0 = keys.motion(flown, game.tick, windows), game.tick, game.player.x
            assert plan.position(motion, x0, speed, 0) == x0
            for step in range(1, 90):
                direction = plan.direction(flown, step)
                keys.key(game.tick, direction)
                keyboard.step(("right",) if direction > 0 else ("left",) if direction < 0 else ())
                k = game.tick - start
                planned = plan.position(motion, x0, speed, k)
                left, right = plan.band(windows, start, k, speed)
                worst = max(worst, abs(game.player.x - planned))
                outside = max(outside, planned - left - game.player.x, game.player.x - planned - right)
            assert game.phase == "playing"
    assert outside <= 1e-9  # never outside the band
    if mode == "latched-ssh":
        assert worst > 0.3  # the jitter is real: taps land a step off their usual lag
    else:
        assert worst < 1e-6


# --- over ssh: what the ship does wherever the link lands each tap (§18 "Planner", plan.window) -------------------

# Draws of the link's jitter, 0 to 1: enough to reach every 20 ms slot a byte can arrive in (one is 2/3 of the draw).
DRAWS = (0.0, *((n + 0.5) / 120 for n in range(120)), 1 - 1e-12)


class Draw:
    """An rng whose random() is always u (Link.deliver draws one number a byte)."""

    def __init__(self, u):
        self.u = u

    def random(self):
        return self.u


def arrivals(link, sent: float) -> list[float]:
    """Every time `link` can deliver a byte sent at `sent`, earliest first: Link.deliver over the draws."""
    return sorted({link.deliver([(sent, b"")], Draw(u))[0][0] for u in DRAWS})


class Landings:
    """balance.Keyboard's ssh link with the jitter of each byte chosen: the n-th byte arrives at the picks[n]-th
    time the link can deliver it (its last if it has fewer, the first when picks runs out), in order, as
    Link.deliver keeps it. `options` records how many times each byte could arrive at, and `landed` the step the
    Keyboard took each byte in."""

    def __init__(self, link, picks):
        self.link, self.picks, self.options, self.landed = link, picks, [], []

    def deliver(self, stream, _rng):
        out, last = [], -math.inf
        for sent, data in stream:
            times = arrivals(self.link, sent)
            n = len(self.options)
            self.options.append(len(times))
            last = max(last, times[min(self.picks[n], len(times) - 1) if n < len(self.picks) else 0])
            out.append((last, data))
        return out


def fly_over_ssh(balance, bots, scene, keys, picks):
    """One flight of `scene` through balance.Keyboard("latched-ssh") with each tap's arrival picked: the bot keys
    scene["before"] (a direction a tick from tick 90, the ship at scene["x"] then), then at the decision plans with
    plan.choose from where its ship is, as the strong bot does, with `keys` as its taps, and flies the plan for 48
    steps. Returns (the plan, as choose gave it, the steps the taps landed at, the first step a threat overlapped
    the ship or None, the Landings)."""
    plan, speed = bots.plan, bots.know.PLAYER.speed
    config = Config("normal", "classic", 5)
    keyboard = balance.Keyboard("latched-ssh", config)
    game = staged(config, 1, phase="playing", director={"reload": 10**6})  # no enemy fire: only the scene's
    keyboard.session.pilot.game = game
    link = keyboard.vin.link = Landings(keyboard.vin.link, picks)
    handle = keyboard.session.handle
    keyboard.session.handle = lambda key, now: (link.landed.append(game.tick), handle(key, now))
    assert game.tick == 90 and game.phase == "playing"
    game.player.x = scene["x"]
    for direction in scene["before"]:
        keys.key(game.tick, direction)
        keyboard.step(("right",) if direction > 0 else ("left",) if direction < 0 else ())
    start, x0 = game.tick, game.player.x
    threats = bots.predict.Threats(48, 0)
    for first, last, lo, hi in scene["threats"]:
        for step in range(max(first, start), last + 1):
            threats.add(step - start + 1, lo, hi)  # step k of the plan is the game's step start + k - 1
    threats.finish()
    chosen = plan.choose(threats, x0, speed, scene["dest"], None, keys, start)
    hit = None
    for k in range(1, 49):
        direction = plan.direction(chosen[0], k)
        keys.key(game.tick, direction)
        keyboard.step(("right",) if direction > 0 else ("left",) if direction < 0 else ())
        assert game.tick == start + k
        if hit is None and any(lo < game.player.x < hi for lo, hi in threats.forb.get(k, ())):
            hit = k
    return chosen[:2], tuple(link.landed), hit, link


def every_landing(balance, bots, scene, make_keys):
    """fly_over_ssh for every combination of arrivals the link can give the taps: [(plan, landed, hit)]."""
    out, picks = [], []
    while True:
        chosen, landed, hit, link = fly_over_ssh(balance, bots, scene, make_keys(), picks)
        picks += [0] * (len(link.options) - len(picks))
        out.append((chosen, landed, hit))
        n = len(picks) - 1
        while n >= 0 and picks[n] + 1 >= link.options[n]:
            n -= 1
        if n < 0:
            return out
        picks = picks[:n] + [picks[n] + 1]


def test_the_landing_window_is_where_balance_keyboards_ssh_link_can_land_a_tap(tools):
    """plan.window with SSH_LAG and SSH_SPREAD is the range of balance.Keyboard's own ssh link, measured through the
    Keyboard: a tap pressed on a step, and one pressed between two steps as a reversal's second tap is (TWICE after
    the first), at every phase of the link's slots against the steps and with every arrival its jitter can give,
    is taken from the window's earliest step to its latest and at no other, and most often at its planned step (by
    the share of the jitter's draws). The constants describe the link as a player learns it; changing the link (its
    slot, jitter or delay) or SSH_LAG or SSH_SPREAD alone fails here."""
    from collections import Counter

    balance, bots = tools
    plan, params = bots.plan, bots.params
    for offset in (0.0, plan.TWICE):
        # 60 presses 25 ticks apart: the link's 20 ms slots line up with the steps every 6 ticks, and 25 is 1 more
        # than a multiple of 6, so the presses cover every phase of the slots against the steps (10 times each)
        presses = [tick / TICK_RATE + offset for tick in range(120, 1620, 25)]
        steps = {t: [] for t in presses}  # the step the Keyboard took each arrival in, earliest first
        link = balance.Keyboard("latched-ssh", Config("normal", "classic", 1)).vin.link
        for pick in range(max(len(arrivals(link, t)) for t in presses)):  # 20 ms slots: up to 3 or 4 a tap
            keyboard = balance.Keyboard("latched-ssh", Config("normal", "classic", 1))
            keyboard.vin.link = Landings(link, [pick] * len(presses))  # each byte's pick-th arrival (or its last)
            game, handle, taken = keyboard.game, keyboard.session.handle, []
            keyboard.session.handle = lambda key, now: (taken.append(game.tick), handle(key, now))
            for t in presses:
                keyboard.vin.tap("s", t)  # S: a tap that leaves the standing ship standing
            while game.tick / TICK_RATE < presses[-1] + 0.2:
                keyboard.step(())
            assert len(taken) == len(presses)
            for t, step in zip(presses, taken, strict=True):
                if pick < len(arrivals(link, t)):
                    steps[t].append(step)
        reached, shares, windows = set(), Counter(), set()
        for t in presses:
            pressed = math.floor(t * TICK_RATE + 1e-9)  # the step it is pressed in
            planned, earliest, latest = plan.window(t, params.SSH_LAG, params.SSH_SPREAD)
            windows.add((planned - pressed, earliest - pressed, latest - pressed))
            times = arrivals(link, t)
            assert len(steps[t]) == len(times)
            reached |= {step - pressed for step in steps[t]}
            for u in DRAWS[1:-1]:  # how often each: the share of the jitter's draws that arrive then
                shares[steps[t][times.index(link.deliver([(t, b"")], Draw(u))[0][0])] - pressed] += 1
        ((planned, earliest, latest),) = windows  # the same from the step it is pressed in, at every phase
        assert (min(reached), max(reached)) == (earliest, latest), (offset, sorted(reached))
        assert shares.most_common(1)[0][0] == planned, (offset, shares)


# Moments where the link's jitter decides a hit, each from tick 90 of a staged wave 1 with no enemy fire: `before`
# keys a direction a tick until the decision (tick 90 + its length), and each threat covers lo < x < hi on the steps
# from `first` to `last`. A pellet falls just past where the plan toward `dest` stops (a stop that lands late runs
# into it) or just short of it (a start that lands late, or a stop that lands early, leaves the ship in it), on
# either side of the band; a reversal's second tap, pressed between two steps, lands three steps late; a tap the
# bot counts as landed may still be on its way at the decision (plan.Latched.settling).
SSH_SCENES = {
    "late-stop-right": {"x": 20.0, "dest": 26.0, "before": (), "threats": [(107, 167, 26.6, 29.4)]},
    "late-stop-left": {"x": 40.0, "dest": 34.0, "before": (), "threats": [(107, 167, 30.6, 33.4)]},
    "short-stop-right": {"x": 20.0, "dest": 26.0, "before": (), "threats": [(107, 167, 23.4, 26.2)]},
    "short-stop-left": {"x": 40.0, "dest": 34.0, "before": (), "threats": [(107, 167, 33.8, 36.6)]},
    # a pellet just ahead of the ship for one step: only a start that lands at once (early) reaches it
    "early-start-right": {"x": 20.0, "dest": 26.0, "before": (0,) * 3, "threats": [(93, 93, 20.2, 20.6)]},
    # moving right from tick 122; at 152 the plan turns left: two taps of A, the second pressed at step 159.2
    "reversal": {"x": 20.0, "dest": 10.0, "before": (0,) * 32 + (1,) * 30, "threats": [(162, 164, 32.2, 32.6)]},
    # the same reversal, and the decision at 161, the step after its second tap's planned one (160)
    "settling": {
        "x": 20.0,
        "dest": 5.0,
        "before": (0,) * 32 + (1,) * 30 + (-1,) * 9,
        "threats": [(181, 181, 23.45, 23.75)],
    },
}


@pytest.mark.parametrize("name", SSH_SCENES)
def test_over_ssh_the_ship_is_never_hit_wherever_the_link_lands_its_taps(tools, name):
    """Each moment flown through balance.Keyboard("latched-ssh") once for every combination of arrivals its link
    can give the taps (its jitter drawn anywhere from 0 to 1). A bot that trusts each tap to land as planned
    (plan.Latched with SSH_LAG, no spread) flies a plan that is safe as planned, and some landing runs the ship into
    the threat. The ssh bot (bots.make keyed "latched-ssh") flies a plan the ship is safe on wherever its taps land."""
    balance, bots = tools
    scene = SSH_SCENES[name]
    trusting = every_landing(balance, bots, scene, lambda: bots.plan.Latched(bots.params.SSH_LAG))
    assert all(hit is None for (_plan, hit), _landed, _k in trusting)  # each plan it flies is safe as planned
    assert any(k is not None for _chosen, _landed, k in trusting)  # and the jitter can make it hit
    flights = every_landing(balance, bots, scene, lambda: bots.make("strong", 1, keyed="latched-ssh").keys)
    hits = [(chosen, landed, k) for chosen, landed, k in flights if k is not None]
    assert not hits, f"{len(hits)} of {len(flights)} landings hit: (plan, taps landed at, step) {hits[:3]}"


# A moment with nothing safe wherever the taps land: standing is hit as planned (a pellet on the ship at steps
# 110-112), a start right that lands at once (early) meets a pellet at step 93, and one left at step 94.
NOTHING_SAFE = {
    "x": 20.0,
    "dest": 26.0,
    "before": (0,) * 3,
    "threats": [(93, 93, 20.2, 20.6), (94, 94, 19.0, 19.4), (110, 112, 19.8, 20.2)],
}


def test_with_nothing_safe_across_the_jitter_the_ssh_bot_flies_a_plan_safe_as_planned_hit_last(tools):
    """When no plan is safe wherever its taps may land, the ssh bot flies a plan that is safe as planned (so its hit,
    the bomb and the no-escape log read as before) and whose band is hit last: wherever the link lands its taps, the
    ship is hit no sooner, and here later, than the trusting bot's (the first step it is hit at over every landing),
    leaving the bot more steps to plan again."""
    balance, bots = tools
    trusting = every_landing(balance, bots, NOTHING_SAFE, lambda: bots.plan.Latched(bots.params.SSH_LAG))
    flights = every_landing(balance, bots, NOTHING_SAFE, lambda: bots.make("strong", 1, keyed="latched-ssh").keys)
    for landings in (trusting, flights):
        assert all(hit is None for (_plan, hit), _landed, _k in landings)  # safe as planned
        assert any(k is None for _chosen, _landed, k in landings)  # not hit on some landings
        assert any(k is not None for _chosen, _landed, k in landings)  # but not wherever they land
    first = min(k for _chosen, _landed, k in trusting if k is not None)
    assert min(k for _chosen, _landed, k in flights if k is not None) > first, (first, flights[:3])


def test_the_band_adds_each_taps_worst_landing(tools):
    """Over ssh a tap pressed at a step lands one step later as planned, or at the step itself, or two later; the
    second tap of a reversal, pressed between steps (TWICE after the first), as late as three steps on."""
    _balance, bots = tools
    plan = bots.plan
    assert plan.window(30 / 60, 1, 1) == (31, 30, 32)
    assert plan.window(30 / 60 + plan.TWICE, 1, 1) == (38, 37, 40)  # pressed at step 37.2: its bytes by step 40.2
    assert plan.window(30 / 60, 0, 0) == (30, 30, 30) and plan.window(30 / 60, 1, 0) == (31, 31, 31)  # as planned
    keys = plan.Latched(1, 1)
    windows = []
    assert keys.motion((1, 10, 0, 0), 0, windows) == (0, 1, 1, 16)  # the stop keyed at tick 16 (SETTLE), lands at 17
    assert windows == [(1, 1, 0, 2), (17, -1, 16, 18)]
    assert plan.band(windows, 0, 1, 0.4) == (0.0, pytest.approx(0.4))  # the start may land a step early
    assert plan.band(windows, 0, 5, 0.4) == (pytest.approx(0.4), pytest.approx(0.4))  # or a step late
    assert plan.band(windows, 0, 30, 0.4) == (pytest.approx(0.8), pytest.approx(0.8))  # and the stop either way too
    assert plan.band(windows, 5, 30, 0.4) == (pytest.approx(0.4), pytest.approx(0.4))  # the start is past: landed
    for tick in range(30):
        keys.key(tick, 1)
    windows = []
    assert keys.motion((-1, 24, 0, 0), 30, windows)[:4] == (1, 1, 0, 7)
    assert windows[:2] == [(31, -1, 30, 32), (38, -1, 37, 40)]  # the reversal's taps: the second as late as 40
    assert plan.band(windows[:2], 30, 30, 0.4) == (pytest.approx(0.8), pytest.approx(1.2))
    for tick in range(30, 39):
        keys.key(tick, -1)
    assert keys.settling == [(38, -1, 37, 40)]  # landed as planned at 38, but its bytes may still be on the way
    windows = []
    keys.motion((-1, 24, 0, 0), 39, windows)
    assert windows[0] == (38, -1, 37, 40)
    assert plan.band(windows[:1], 39, 1, 0.4) == (0.0, pytest.approx(0.4))  # step 39 may still stand
    keys.key(39, -1)
    assert keys.settling == []  # from step 40 on it has landed, wherever it did
    # A looser link (spread 2): the reversal's second tap may land as late as step 41, so it is still settling at
    # the decision for step 40 and is dropped only once no later step can take it.
    wide = plan.Latched(1, 2)
    for tick in range(40):
        wide.key(tick, 1 if tick < 30 else -1)
    assert wide.settling == [(38, -1, 36, 41)]
    windows = []
    wide.motion((-1, 24, 0, 0), 40, windows)
    assert windows[0] == (38, -1, 36, 41)
    assert plan.band(windows[:1], 40, 1, 0.4) == (0.0, pytest.approx(0.4))  # step 40 may still stand
    wide.key(40, -1)
    assert wide.settling == []
    keys.settling.append((38, -1, 37, 40))
    keys.clear()
    assert keys.settling == [] and keys.moving == 0
    local = plan.Latched()
    local.key(0, 1)
    local.key(1, 1)
    windows = []
    local.motion((1, 10, 0, 0), 2, windows)
    assert local.settling == [] and plan.band(windows, 2, 30, 0.4) == (0.0, 0.0)  # locally a tap lands as planned


def test_with_nothing_safe_across_the_jitter_a_tie_goes_to_the_ranking(tools, monkeypatch):
    """When every motion is safe as planned but none across the band, the bot flies the one whose band is hit last,
    and among motions hit at the same step the first in the ranking: the plan it would fly without the jitter."""
    _balance, bots = tools
    plan = bots.plan

    class Busy:  # threats; the checks patched below answer for them
        def empty(self):
            return False

    monkeypatch.setattr(plan, "first_hit", lambda *args: None)  # every motion is safe as planned
    first, hit, _ = plan._choose_keyed(Busy(), 30.0, 0.4, 36.0, None, plan.Latched(1, 0), 0)  # no jitter
    assert hit is None
    monkeypatch.setattr(plan, "banded_hit", lambda *args: 5)  # with it, every band is hit at step 5
    chosen, hit, _ = plan._choose_keyed(Busy(), 30.0, 0.4, 36.0, None, plan.Latched(1, 1), 0)
    assert (chosen, hit) == (first, None)
    later = {}  # a band hit later than the first's wins the tie-break
    monkeypatch.setattr(plan, "banded_hit", lambda threats, motion, *args: later.setdefault(motion, 5 + len(later)))
    chosen, hit, _ = plan._choose_keyed(Busy(), 30.0, 0.4, 36.0, None, plan.Latched(1, 1), 0)
    assert hit is None and chosen != first


@pytest.mark.parametrize(
    "mode, expected",
    [("direct", None), ("kitty", None), ("latched", (0, 0)), ("timed", (0, 0)), ("latched-ssh", (1, 1))],
)
def test_keyed_bots_plan_the_taps_of_their_input(tools, mode, expected):
    """Latched and timed controls are keyed with taps (a press shorter than the OS repeat delay latches under timed
    too), so the bot plans them; over ssh with the link's usual lag and its jitter either way."""
    balance, bots = tools
    pilot = bots.make("strong", 1, keyed=mode if mode != "direct" else False)
    assert (None if pilot.keys is None else (pilot.keys.lag, pilot.keys.spread)) == expected
    if mode != "direct":
        keyboard = balance.Keyboard(mode, Config("normal", "classic", 1))
        assert keyboard.driver.scheme == ("hold" if mode == "kitty" else "latched")
    assert (bots.params.SSH_LAG, bots.params.SSH_SPREAD) == (1, 1)


def test_a_reversal_stands_for_the_second_taps_seven_steps(tools):
    _balance, bots = tools
    keys = bots.plan.Latched()
    keys.key(0, 1)  # right: lands at once
    assert keys.motion(bots.plan.STAND, 1)[:2] == (1, 15)  # S keyed at tick 16 (0.20 s + a tap after), lands then
    for tick in range(1, 30):
        keys.key(tick, 1)
    legs = keys.motion((-1, 24, 0, 0), 30)
    assert legs[:4] == (0, 7, -1, 17)  # the first tap stops the ship, the second (TWICE later) moves it left


# --- the lives model with bombs as absorbers (tools/bots/lives.py, spec §19.1) ------------------------------------


def test_the_lives_model_gives_spec_19_1s_table(tools):
    _balance, bots = tools
    from bots import lives

    assert lives.rules("normal") == lives.NORMAL
    total = sum(lives.WEIGHTS)
    for b, events in ((0.0, 4.54), (0.3, 6.32), (0.5, 7.68), (0.8, 8.82)):
        assert lives.solve(0.675, b, **lives.rules("normal")) * total == pytest.approx(events, abs=0.02), b
    plain = lives.rules("normal", bombs=False)
    assert lives.budget(0.675, 0.0, **plain) == pytest.approx([0.49, 0.89, 1.33, 1.83], abs=0.01)
    for b in (0.0, 0.5):  # no bombs: b makes no difference
        assert lives.solve(0.675, b, **plain) * total == pytest.approx(4.54, abs=0.02)
    rules = lives.rules("normal")
    assert lives.win(0.5, 0.5, **rules) > lives.win(1.0, 0.5, **rules) > lives.win(1.0, 0.0, **rules)
    assert lives.predict([0.0] * 4, 0.5, **rules) == pytest.approx(1.0)


# --- params, imports ---------------------------------------------------------------------------------------------


def test_params_are_pinned(tools):
    _balance, bots = tools
    params = bots.params
    pinned = {
        "SKILLED": (6, 1.0),
        "STRONG": (12, 0.92),  # spec §27: sk_d12_p92
        "GOOD": (18, 0.85),
        "LATE": 30,
        "TELL_LEAD": 12,
        "TELL_REACH": 1.5,
        "TELL_MEMORY": 90,
        "VELOCITY_SPAN": 24,
        "LAPSE_SALT": 0x5EED_1A75,
        "YOUNG": 15,
        "DENSE": 30,
        "STILL": 3.0,
        "ROUNDING": 0.25,
        "HORIZON": 48,
        "BEAM_HORIZON": 72,
        "HOLDS": (3, 6, 10, 14, 18, 24, 30, 38, 48),
        "FIRST_LEGS": (6, 14, 24),
        "SECOND_LEGS": (6, 14, 24, 38),
        "REACTION_EXTRA": 4,
        "IMMUNE_SLACK": 2,
        "LANDING_ROWS": 3.0,
        "AIM_SLACK": 0.25,
        "PICKUP_SLACK": 0.5,
        "NOVICE_DELAY": 15,
        "NOVICE_NOISE": 1.6,
        "NOVICE_RETARGET": (45, 120),
        "NOVICE_TAP": (14, 28),
        "NOVICE_CHOICE": 8.0,
        "NOVICE_DEADBAND": 0.6,
        "NOVICE_WINDOW": 1.4,
        "RNG_SALT": (7919, 17),
        "KEY_BEAT": 12,
        "KEY_QUIET": 45,
        "TAP": 0.06,
        "SSH_LAG": 1,
        "SSH_SPREAD": 1,
    }
    margins = {  # refitted for spec §27 (tools/bots/params.py has B-M1's values)
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
    names = {name for name in vars(params) if name.isupper()}
    assert names == set(pinned) | {"MARGINS"}
    assert {name: getattr(params, name) for name in pinned} == pinned
    assert dict(params.MARGINS) == margins
    assert [params.cadence(delay) for delay in (6, 12, 18, 30)] == [3, 4, 6, 10]
    with pytest.raises(TypeError):
        params.MARGINS["pellet"] = (0.0, 0.0)
    # the bots these pin (spec §27): strong is sk_d12_p92, and perfect is skilled itself
    for name, (delay, perceive, cadence) in {"skilled": (6, 1.0, 3), "strong": (12, 0.92, 4)}.items():
        pilot = bots.make(name, 1)
        assert (pilot.delay, pilot.perceive, pilot.cadence) == (delay, perceive, cadence), name
        assert (pilot.sense.delay, pilot.sense.perceive, pilot.policy, pilot.bombs) == (
            delay,
            perceive,
            "priority",
            True,
        )
    assert bots.kind("perfect") is bots.Skilled is bots.BOTS["skilled"] and bots.shown("skilled") == "perfect"
    assert bots.kind("strong") is bots.kind("sk_d12_p92") is bots.STRONG and bots.STRONG.name == "sk_d12_p92"


def test_bot_modules_import_only_the_public_tables_observe_input_and_keymap():
    for path in sorted(BOTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # within tools/bots
                    continue
                names = [node.module]
            else:
                continue
            for name in names:
                if name == "terminal_invaders" or name.startswith("terminal_invaders."):
                    assert name in ALLOWED, (path.name, name)
                else:
                    assert name.split(".")[0] in sys.stdlib_module_names, (path.name, name)


def test_bot_names(tools):
    _balance, bots = tools
    assert set(bots.BOTS) == {"novice", "naive", "strong", "skilled", "sk_d18_p85", "sk_d30_p70", "nobomb", "bombhappy"}
    assert bots.ALIASES == {"perfect": "skilled", "sk_d12_p92": "strong"}
    assert [bots.canonical(name) for name in ("perfect", "sk_d12_p92", "skilled", "naive")] == [
        "skilled",
        "strong",
        "skilled",
        "naive",
    ]
    assert [bots.shown(name) for name in ("skilled", "strong", "sk_d18_p85")] == ["perfect", "strong", "sk_d18_p85"]
    good = bots.kind("sk_d18_p85")
    assert (good.delay, good.perceive) == (18, 0.85) and (bots.kind("skilled").delay, bots.Skilled.perceive) == (6, 1.0)
    name = "fixed:" + bots.know.code("TRIPLE SHOT")
    fixed = bots.make(name, 1)
    assert fixed.policy == name and isinstance(fixed, bots.Skilled)
    for name in ("sk_d9", "sk_d9_p101", "fixed:NOPE", "wizard"):
        with pytest.raises(ValueError):
            bots.kind(name)


# --- the card and the prompt (§18 "Card and prompt") ---------------------------------------------------------------


def take_card(balance, bots, mode, policy, seed=11):
    """A staged card before wave 4 played by a skilled bot, directly or keyed: (the offer it wanted, the pick)."""
    config = Config("normal", "classic", seed)
    game = staged(config, 4, phase="upgrade")
    keyboard = None
    if mode != "direct":
        keyboard = balance.Keyboard(mode, config)
        keyboard.session.pilot.game = game
    pilot = bots.make("skilled", seed, policy=policy, keyed=mode if keyboard is not None else False)
    wanted, start = None, game.tick
    while game.phase == "upgrade" and game.tick - start < 600:
        frame = observe(game)
        actions = pilot.act(frame)
        if wanted is None and pilot.want is not None:
            wanted = frame.card[0][pilot.want]
        (keyboard or game).step(actions)
    return wanted, game.result()["picks"]


@pytest.mark.parametrize("mode", ["direct", "kitty", "latched", "latched-ssh"])
def test_the_bot_takes_the_offer_its_policy_wants(tools, mode):
    """Keyed, it lets every key go after the phase edge (a held Space is suppressed until released), then taps
    left or right once per cursor step and presses Space fresh."""
    balance, bots = tools
    offers = observe(staged(Config("normal", "classic", 11), 4, phase="upgrade")).card[0]
    last = f"fixed:{bots.know.code(offers[-1])}"
    for policy, index in (("first", 0), ("random", None), ("priority", None), (last, -1)):
        wanted, picks = take_card(balance, bots, mode, policy)
        assert picks == (wanted,), (mode, policy)
        if index is not None:
            assert wanted == offers[index]


@pytest.mark.parametrize("mode", ["direct", "kitty", "latched", "latched-ssh"])
def test_the_bot_always_continues(tools, mode):
    balance, bots = tools
    config = Config("easy", "classic", 3)
    game = staged(config, 5, phase="continue")
    keyboard = None
    if mode != "direct":
        keyboard = balance.Keyboard(mode, config)
        keyboard.session.pilot.game = game
    pilot = bots.make("skilled", 3, keyed=mode if keyboard is not None else False)
    start, continues = game.tick, game.continues
    while game.phase == "continue" and game.tick - start < 600:
        (keyboard or game).step(pilot.act(observe(game)))
    assert game.phase == "wave" and game.continues == continues - 1 and game.player.lives > 0
    assert game.tick - start >= TIMING.arm  # not before the prompt is armed


# --- the harness: repeatable runs, the shot log, the per-life log --------------------------------------------------


def test_runs_repeat_exactly_per_seed_picks_included(tools):
    balance, _bots = tools
    first, second = (balance.play("skilled", "easy", "classic", 5, 12500, "direct", "random") for _ in range(2))
    del first["wall"], second["wall"]
    assert first == second and len(first["picks"]) >= 1


def test_the_shot_log_reads_game_fired(tools):
    balance, _bots = tools
    run = balance.play("skilled", "normal", "classic", 21, 12000)
    log = run["shot_log"]
    assert log["count"] == run["enemy_shots"] > 0 and log["bad_source"] == 0
    assert log["min_w"] >= LIMITS.warning and log["min_flight"] >= LIMITS.flight
    assert log["dives"] > 0 and log["min_dive_w"] >= LIMITS.dive_warning


def test_the_per_life_log(tools):
    balance, _bots = tools
    run = balance.play("novice", "hard", "classic", 4, 20000)
    lives = run["lives_log"]
    assert lives and len(lives) == run["lives_lost"] == sum(run["acts"]["deaths"])
    for entry in lives:
        assert entry.keys() == {"wave", "act", "tick", "cause", "bombs", "shot", "class", "source", "late", "seen"}
        assert entry["cause"] and 1 <= entry["act"] <= 4 and entry["late"] is False  # the novice tracks nothing
        if entry["shot"]:
            assert entry["shot"] in SHOTS and entry["class"] in ("safe", "reaction", "boss")
    skilled = balance.play("sk_d30_p70", "hard", "classic", 4, 20000)
    assert all(isinstance(entry["late"], bool) for entry in skilled["lives_log"])
    assert skilled["no_escape"] == sum(skilled["acts"]["no_escape"])


def test_the_facade_plays_through_a_public_view(tools):
    balance, _bots = tools
    from terminal_invaders.engine import Game

    game = Game(Config("easy", "classic", 42))
    bot = balance.Skilled(game.difficulty, 42)
    for _ in range(900):
        actions = bot.act(balance.PublicView(game))
        assert set(actions) <= {"left", "right", "fire", "bomb"}
        game.step(actions)
    assert game.kills > 0
    pilot = bot.pilot
    bot.act(balance.PublicView(Game(Config("easy", "classic", 43))))
    assert bot.pilot is not pilot  # a new game gets a fresh bot
