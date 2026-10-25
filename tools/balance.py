#!/usr/bin/env python3
"""Headless bot playtests that measure balance: python3 tools/balance.py --help.

The bots (tools/bots, spec §18) see only `campaign.observe.Frame`s and the public tables: once a tick the harness
hands each its Frame and steps the game with the actions it returns. The harness is the judge: it may read the
game, and it keeps each game's shot log (from `game.fired`), its per-life log and its bombs and no-escape moments.

--input direct hands the actions to Game.step; the other modes key them as a player would on a simulated keyboard
and send the bytes through the game's own input path (spec 11.5), so the numbers include what the controls cost.
With direct and keyed inputs in one run, the report ends with spec 11.5's acceptance for the strong bot (gate row
I1); --check fails when it, or a quality-bar row of TARGETS (spec §19, §23.3, §27), does not pass, and when a game
stood still (STALL_TICKS of play with no hit on the fleet or a boss: the report lists each).

The bots (spec §27): `strong` (sk_d12_p92) plays like a strong person and is the one the strong rows judge (Q1, Q5-Q9,
I1); `skilled` keeps its definition, is printed as "perfect" (`--bots perfect` names it too), and must win almost
every game (row P); the audit replays the lives both lose (Q10), and finds a hit unavoidable only when no plan of the
library, and no two of them in a row, avoids it (spec §27.2), with fire as recorded and, for a landing, with fire
held too (spec §18). Spec §27.2 (provisional until the owner answers) tunes Normal for the good player and Hard for
the strong one: Q1 and Q7 (Endless) are the strong bot on Hard, while Q5, Q6, Q8, Q9 and I1 read its Normal Campaign
games, whose win rate is reported, not judged, as is the good bot's on Hard.

The tiers of spec §19.2: the smoke (`make balance`), the feasibility sweep (--sweep), staged acts (--staged ACT),
the tune tier (--tune), the gate (--gate) and the keyed-input check (--gate-input). Long runs split into resumable
chunks: --shard K/N plays every N-th game into a shard file in --out DIR, --merge DIR... reports them, and --plan
sizes N from a --profile run so a chunk takes at most 17 minutes. --tapes keeps the action tape of every game that
lost a life, and --audit DIR replays each one engine only for spec §18's fairness audit (gate row Q10). Every game
and audit line carries its build (a fingerprint of the code that played it): --out resumes only this build's games,
--audit audits only them, and --merge refuses games of several builds (--check fails on another build's).
"""

import argparse
import base64
import copy
import functools
import hashlib
import json
import math
import multiprocessing
import os
import platform
import random
import statistics
import sys
import time
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
for path in (ROOT, TOOLS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import bots  # noqa: E402
from bots import lives as lives_model  # noqa: E402
from bots import params as bot_params  # noqa: E402
from bots import picks as bot_picks  # noqa: E402
from bots import plan as bot_plan  # noqa: E402
from bots import predict as bot_predict  # noqa: E402

from terminal_invaders.app import configure  # noqa: E402
from terminal_invaders.campaign.observe import observe  # noqa: E402
from terminal_invaders.campaign.tables import ACTS as ACT_ROWS  # noqa: E402
from terminal_invaders.campaign.tables import LIMITS, SHOTS, TIMING, WAVES  # noqa: E402
from terminal_invaders.campaign.testing import DIRECTOR_KEYS, staged  # noqa: E402
from terminal_invaders.cli import defaults  # noqa: E402
from terminal_invaders.engine import Game  # noqa: E402
from terminal_invaders.input import IntentDriver, VirtualInput  # noqa: E402
from terminal_invaders.model import ACTIONS, CAMPAIGN_WAVES, DIFFICULTIES, RULESET, TICK_RATE, Config  # noqa: E402
from terminal_invaders.term.caps import EnvHints, Overrides, ProbeReplies, resolve  # noqa: E402
from terminal_invaders.term.decode import Decoder  # noqa: E402
from terminal_invaders.term.keys import Key  # noqa: E402
from terminal_invaders.term.simkeys import PROFILES, Link  # noqa: E402
from terminal_invaders.ui import Session  # noqa: E402

TICKS_PER_MINUTE = 60 * TICK_RATE
ACTS = 4
TERMINAL = ("game_over", "victory")
CHOICES = ("clear", "upgrade")  # a wave is over once its clear panel (or the card after it) is up
DEFAULT_TICKS = 60 * TICKS_PER_MINUTE
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
    "no_escape",
    "enemy_shots",
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
PER_ACT = (("deaths", "deaths_per_act"), ("bombs", "bombs_per_act"), ("no-escape", "no_escape_per_act"))
PER_ACT_FIRE = (
    ("shots/min", "shots_per_minute_per_act"),
    ("in flight", "in_flight_per_act"),
    ("+lives", "extra_lives_per_act"),
)
# --input: how a bot's actions reach the game, as (keyboard protocol, --controls, over ssh); direct calls Game.step
INPUTS = {
    "direct": None,
    "kitty": ("kitty", "auto", False),
    "latched": ("legacy", "auto", False),
    "timed": ("legacy", "timed", False),
    "latched-ssh": ("legacy", "auto", True),
}
KEYED = tuple(name for name, how in INPUTS.items() if how)
TERMINALS = {"kitty": {"TERM": "xterm-ghostty", "TERM_PROGRAM": "ghostty"}, "legacy": {"TERM": "xterm-256color"}}
# Spec 11.5's acceptance for the strong bot in classic (gate row I1, §23.4, §27), against direct on the same
# difficulty and seeds: the median waves and the clear rate it may lose, and the difficulties that applies to. Hard
# with latched input is reported, not judged (§24 open question 8); timed has no threshold. A row is judged only
# with at least ACCEPT_GAMES games of its input.
ACCEPT = {
    "kitty": (1, 0.10, tuple(DIFFICULTIES)),
    "latched": (1, 0.10, ("easy", "normal")),
    "latched-ssh": (1, 0.15, ("easy", "normal")),
}
ACCEPT_GAMES = 200
UNTHRESHOLDED = "reported, not judged (spec §24, open question 8: Hard with tap-to-move controls)"
KITTY_FLAGS = {"kitty": 11, "legacy": 0}  # what push_kitty verifies on each
REPLIES = ProbeReplies(dsr=True, da1=(62, 22), rtt=0.001)  # a local terminal that answers the probe
RETAP = 0.25  # a bot taps B again only after this long, as a player waits to see the bomb go off
LEGEND = (
    "minutes: median game length; lost/min: lives lost per minute; bomb kills: share of kills made by bombs;\n"
    "power-ups: share of play with one active; combo: mean multiplier per shot kill; +lives: lives gained per game;\n"
    "accuracy counts a volley once"
)
BOTS = bots.BOTS
QUICK = ("strong", "perfect", "sk_d18_p85", "naive", "novice")  # the smoke's bots (`make balance`)
I1_BOT = "strong"  # gate row I1 (spec §23.4, §27): keyed inputs against direct play, for this bot
LINK = 15  # a life lost within the reaction window plus this many ticks of a no-escape moment came after it
# A game standing still: this many ticks of play in a row with no hit on the fleet or a boss (and no kill, a bomb's
# included), whatever happens to the ship. A stall (a bot standing where its shots can hit nothing, a boss no shot
# can reach) runs to the time cap, an hour or more. The longest lull measured in play is under 3 minutes: the good
# bot under MOTHERSHIP's Hard DESPERATION on the untuned tables (82-124 s staged, seeds 1001-1003 and 1036; 144 and
# 170 s in whole games, seeds 1002 and 1000).
STALL_TICKS = 10 * 60 * TICK_RATE
AUDIT_STEP = 30  # the audit copies the game at the last multiple of this at least this many ticks before a hit...
AUDIT_AFTER = 120  # ...and a plan avoids the hit only if no life is lost until this many ticks after it
# The feasibility sweep (spec §22.3 step 4): staged waves with aimed darts, divers, bursts, crossfire, weavers and
# shells, immortal, at six director densities (slots, reload) from act II's row to beyond act IV's, each with these
# reaction and focus shares; skilled and sk_d18_p85 at every point, skilled 10 and 14 ticks late at three.
SWEEP_WAVES = (5, 8, 10)
SWEEP_POINTS = ((3, 72), (4, 56), (5, 44), (6, 34), (8, 26), (8, 20))
SWEEP_FIXED = {"reaction": 0.04, "focus": 0.5}
SWEEP_BOTS = ("skilled", "sk_d18_p85")
SWEEP_DELAYS = ("sk_d10_p100", "sk_d14_p100")
SWEEP_DELAY_POINTS = ((4, 56), (6, 34), (8, 20))  # act II's last row, act IV's last row (the plan's densest), the caps
SWEEP_SEEDS = 28  # games per wave, bot and point: 84 waves of about 1.2 minutes of play each, about 100 minutes
SWEEP_START = 1000
SWEEP_CAP = 10 * TICKS_PER_MINUTE  # no staged wave runs this long
# §19.1's budget for the strong player on Normal as lives lost a minute of play: each act's deaths (0.49 / 0.89 /
# 1.33 / 1.83) over its length in §8.2's estimates (7.1 / 8.1 / 8.5 / 9.1 minutes, boss included).
BUDGET = {"I": 0.07, "II": 0.11, "III": 0.16, "IV": 0.20}
# --staged ACT (spec §18): the loadout at each act's first wave until full runs give their median (--loadout DIR);
# act I is the game's own start.
LOADOUTS = {
    2: {"lives": 3, "bombs": 1, "picks": ["TRIPLE SHOT", "RAIL ROUNDS"]},
    3: {"lives": 3, "bombs": 1, "picks": ["TRIPLE SHOT", "RAIL ROUNDS", "DEFLECTOR", "THRUSTERS"]},
    4: {
        "lives": 3,
        "bombs": 1,
        "picks": ["TRIPLE SHOT", "RAIL ROUNDS", "DEFLECTOR", "THRUSTERS", "PIERCER", "RAIL ROUNDS"],
    },
}
STAGED_SEEDS = 200  # make balance-act: 200 games per bot from the act's first wave
SHARD_MINUTES = 17  # --plan: the most a shard may take on --jobs processes
PROFILE_SEEDS = 12  # --plan profiles this many seeds of each bot the tier plays


# --- the façade other gates use (bench_render.py:29, test_palette.py:159, test_render_gate.py:221) ----------------


class PublicView:
    """A window onto a Game for the façade: no random stream, no private attributes, no assignment."""

    __slots__ = ("_game",)

    def __init__(self, game: Game):
        object.__setattr__(self, "_game", game)

    def __getattr__(self, name):
        if name.startswith("_") or name == "rng":
            raise AttributeError(f"bots may not read Game.{name}")
        return getattr(self._game, name)

    def __setattr__(self, name, value):
        raise AttributeError("bots may not change the game")


class Skilled:
    """Phase 3's interface on the Frame bot: Skilled(difficulty, seed).act(PublicView(game)) -> actions. The
    difficulty is ignored (the bot reads the profile from its Frames); a new game gets a fresh bot."""

    def __init__(self, difficulty: float, seed: int):
        self.seed = seed
        self.played = None
        self.pilot = None

    def act(self, view) -> tuple[str, ...]:
        played = object.__getattribute__(view, "_game") if isinstance(view, PublicView) else view
        if played is not self.played:
            self.played, self.pilot = played, bots.make("skilled", self.seed)
        return self.pilot.act(observe(played))


class Offline:
    """The profile and the audio of a Session that saves nothing and plays nothing."""

    alert = error = None
    enabled = False
    data = {"name": "BOT", "scores": [], "stats": {}, "achievements": []}

    def best(self, config):
        return 0

    def record(self, result, config, name):
        return []

    def update(self, events=()):
        pass


def bot_class(name: str) -> type:
    return bots.kind(name)


# --- keyed play ----------------------------------------------------------------------------------------------------


class Keyboard:
    """A bot's actions keyed by a player on a simulated keyboard (macOS default repeat, and an SSH link for
    latched-ssh): IntentDriver -> VirtualInput -> bytes -> Decoder -> Session.handle (Router, KeyGate, the Controls
    make_controls picks for the terminal) -> Session.step (sample() -> Game.step), as the game's loop runs them. The
    Session keeps ticking through the card and the prompt (they are engine phases), so no scene is keyed here.

    Timed controls are keyed with taps, as latched ones are: on a legacy terminal a press shorter than the OS repeat
    delay (0.525 s here) latches under timed controls too, and a Space pressed after an arrow takes its repeats, so
    a held arrow reads as a tap. Held short presses (IntentDriver's timed keying) would leave the ship moving after
    the bot let go and stop it where the bot pressed the other way (the gate's side check: as asked half the ticks).
    Tapped, with S to stop and the other way twice to reverse, the ship does what the bot keyed (bots.pilots.LATCHED
    plans those taps)."""

    def __init__(self, mode: str, config: Config):
        protocol, controls, ssh = INPUTS[mode]
        env = TERMINALS[protocol] | ({"SSH_CONNECTION": "10.0.0.1 22 10.0.0.2 22"} if ssh else {})
        caps = resolve(EnvHints.from_env(env), REPLIES, Overrides(), kitty_verified=KITTY_FLAGS[protocol])
        options = defaults(
            mode=config.mode, difficulty=config.difficulty, seed=config.seed, fixed_seed=True, quickstart=True,
            name="BOT", controls=controls,
        )  # fmt: skip
        self.session = Session(options, Offline(), Offline())
        self.decoder = Decoder(esc_timeout=caps.esc_timeout, string_timeout=caps.string_timeout)
        configure(self.session, self.decoder, caps)
        # the bots' tap windows (plan.window, params.SSH_LAG and SSH_SPREAD) are this link's range: tests/test_bots.py
        link, rng = (Link(slot=0.02, jitter=0.03), random.Random(config.seed * 7919 + 29)) if ssh else (None, None)
        self.vin = VirtualInput(protocol, PROFILES["macos-default"], ssh=link, rng=rng)
        scheme = self.session.controls.scheme
        self.driver = IntentDriver("latched" if scheme == "timed" else scheme, self.vin)  # timed: tapped (above)
        self.bombed = -math.inf

    @property
    def game(self) -> Game:
        return self.session.pilot.game

    def step(self, actions: tuple[str, ...]) -> None:
        """The bot's actions for the coming tick; the game steps with what the controls sample."""
        tick = self.game.tick
        self.driver.intend(tick, frozenset(actions) & {"left", "right", "fire"})
        if "bomb" in actions and tick / TICK_RATE >= self.bombed + RETAP:
            self.driver.pulse("bomb", tick)
            self.bombed = tick / TICK_RATE
        now = (tick + 1) / TICK_RATE  # keys pressed at this tick arrive in time for the next
        for arrival, data in self.vin.bytes_until(now):
            self.decoder.expire(arrival)
            self.decoder.feed(data, arrival)
            self.dispatch()
        self.decoder.expire(now)
        self.dispatch()
        if not self.session.ticking:
            raise RuntimeError(f"the bot's keys left play for {self.session.scene}")
        self.session.step(now)

    def dispatch(self) -> None:
        for event in self.decoder.events():
            if not isinstance(event, Key):
                raise RuntimeError(f"not a key: {event}")
            self.session.handle(event, event.time)


# --- the judge: shot log and per-life log --------------------------------------------------------------------------


class Judge:
    """What the harness records from the game itself (the judge may read the game; the bots never do)."""

    SOURCES = ("diver", "mite")  # no shot may come from a diver or a mite...
    POSES = ("entering", "returning", "diving")  # ...or from an enemy in these poses (§7.3, F2)

    def __init__(self):
        self.fired = {}  # id -> Fired, every threat this game (pruned to the living)
        self.shots = {"count": 0, "min_w": None, "min_flight": None, "max_y": None, "bad_source": 0}
        self.shots |= {"min_dive_w": None, "min_mite_w": None, "dives": 0, "mites": 0, "bad": []}
        self.lives = []
        self.kinds = {}  # enemy kind -> [kills, hits taken, HP lost], in play outside the steps a bomb went off

    @staticmethod
    def low(log, key, value):
        log[key] = value if log[key] is None else min(log[key], value)

    def read(self, game) -> None:
        """After every step: the threats this tick added (`game.fired`)."""
        log = self.shots
        for fired in game.fired:
            self.fired[fired.id] = fired
            if fired.kind in ("dive", "mite"):
                log[fired.kind + "s"] += 1
                self.low(log, f"min_{fired.kind}_w", fired.w)
                continue
            log["count"] += 1
            self.low(log, "min_w", fired.w)
            self.low(log, "min_flight", fired.flight)
            log["max_y"] = fired.y if log["max_y"] is None else max(log["max_y"], fired.y)
            if fired.source in self.SOURCES or fired.pose in self.POSES:
                log["bad_source"] += 1
                if len(log["bad"]) < 20:  # the first few, for the engine's fix
                    log["bad"].append([game.wave, game.tick, fired.kind, fired.cls, fired.source, fired.pose, fired.y])
        if len(self.fired) > 4096:
            alive = {shot.id for shot in game.shots} | {alien.id for alien in game.aliens}
            self.fired = {ident: fired for ident, fired in self.fired.items() if ident in alive}

    def damage(self, before) -> None:
        """A step in play without a bomb: per enemy kind, the kills, the enemies hit and the HP they lost (`before`:
        each enemy with its HP when the step started; the engine adds to the list it holds and replaces it as they
        go, so the pairs are taken first)."""
        for alien, hp in before:
            if alien.hp < hp:
                entry = self.kinds.setdefault(alien.kind, [0, 0, 0])
                entry[0] += alien.hp <= 0
                entry[1] += 1
                entry[2] += hp - max(alien.hp, 0)

    def death(self, game, before, pilot) -> None:
        """A life lost this step: its wave, tick, cause and, for a shot, which one it was (the nearest of its kind
        that vanished), its class and source, whether the bot noticed it late, and the bombs in stock at the hit."""
        cause = game.hit_cause
        kind = cause.split()[0] if cause else ""
        entry = {"wave": game.wave, "act": game.act, "tick": game.tick, "cause": cause, "bombs": game.player.bombs}
        entry |= {"shot": "", "class": "", "source": "", "late": False}
        if kind in SHOTS:
            after = {id(shot) for shot in game.shots}
            x = game.player.x
            gone = [s for s in before if not s.friendly and s.kind == kind and id(s) not in after and s.y > 15]
            if gone:
                shot = min(gone, key=lambda s: abs(s.x - x) + abs(s.y - 18))
                fired = self.fired.get(shot.id)
                entry["shot"] = kind
                if fired is not None:
                    entry["class"], entry["source"] = fired.cls, fired.source
                sense = getattr(pilot, "sense", None)
                entry["late"] = bool(sense is not None and shot.id in sense.late)
        elif kind in ("diver", "mite"):
            entry["class"] = "dive" if kind == "diver" else "mite"
        self.lives.append(entry)


# --- one game ------------------------------------------------------------------------------------------------------


def builder(config, *, wave=None, act=None, immortal=False, director=None, loadout=None):
    """How a game starts: Game(config), or staged() at a wave (the sweep) or at an act's first wave with its
    loadout (--staged ACT). The audit rebuilds a recorded game the same way."""
    if act is not None:
        load = loadout if loadout is not None else LOADOUTS.get(act)
        start = ACT_ROWS[act - 1].first
        if load is None:
            return functools.partial(staged, config, start, immortal=immortal, director=director)
        return functools.partial(
            staged, config, start, upgrades=tuple(load["picks"]), lives=load["lives"], bombs=load["bombs"],
            immortal=immortal, director=director,
        )  # fmt: skip
    if wave is not None:
        return functools.partial(staged, config, wave, immortal=immortal, director=director)
    if immortal or director:
        raise ValueError("immortal and director need a staged game (a wave or an act)")
    return functools.partial(Game, config)


def over(game, wave=None, act=None, until=None) -> bool:
    """A staged or stopped game is done: its wave's clear panel (a staged wave), the act's last wave cleared (a
    staged act), or wave `until` cleared."""
    if wave is not None:
        return game.wave != wave or game.phase == "clear"
    last = ACT_ROWS[act - 1].last if act is not None else until
    if last is None:
        return False
    return game.wave > last or (game.wave == last and game.phase in CHOICES)


def play(
    bot: str,
    difficulty="normal",
    mode="classic",
    seed=0,
    max_ticks=DEFAULT_TICKS,
    input_mode="direct",
    policy=None,
    *,
    wave=None,
    act=None,
    until=None,
    immortal=False,
    director=None,
    loadout=None,
    audit=False,
    tape=False,
    profile=False,
) -> dict:
    """Play one game to its end or the tick cap; return its result plus balance counters and the judge's logs.

    wave: a staged game (spec §3.6) from that wave's start, played until the wave is over (its clear panel, or the
    game's end), `immortal` and with the `director` overrides. act: a staged game from that act's first wave with
    `loadout` (default LOADOUTS), until the act's boss is beaten. until: a game played until that wave is cleared
    (gate row Q3: wave 1). audit: keep the action tape and put the fairness audit's verdict (`audit_hits`) on every
    life lost. tape: keep the action tape in the run (run-length encoded) when a life was lost, for --audit DIR.
    profile: time observe, the bot, the engine and the harness. Staged, taped and audited games need direct input.
    The run keeps the bot's canonical name (perfect: skilled)."""
    bot = bots.canonical(bot)
    config = Config(difficulty, mode, seed)
    if (wave is not None or act is not None or audit or tape) and INPUTS[input_mode]:
        raise ValueError("staged, taped and audited games are played with direct input")
    keyboard = Keyboard(input_mode, config) if INPUTS[input_mode] else None
    build = builder(config, wave=wave, act=act, immortal=immortal, director=director, loadout=loadout)
    game = keyboard.game if keyboard else build()
    pilot = bots.make(bot, seed, policy=policy, keyed=input_mode if keyboard else False)
    judge = Judge()
    counts, powerups = Counter(), Counter()
    damage_waves, wave_ticks = [], [game.tick]
    acts = {"deaths": [0] * ACTS, "bombs": [0] * ACTS, "no_escape": [0] * ACTS, "minutes": [0.0] * ACTS}
    acts |= {"shots": [0] * ACTS, "in_flight": [0] * ACTS}  # enemy shots fired; enemy shots on the field, summed
    acts["lives"] = [0] * ACTS  # lives gained
    wave_play, playing_now = {}, 0  # playing ticks of every wave cleared; of the wave going on
    quiet = longest = longest_wave = longest_at = 0  # play since the last hit or kill; the longest such stretch
    loadouts = {str(game.act): [game.player.lives, game.player.bombs, list(game.result()["picks"])]}
    actions_tape = [] if (audit or tape) else None
    stopping = wave is not None or act is not None or until is not None
    clock = time.perf_counter
    timed = {"observe": 0.0, "bot": 0.0, "engine": 0.0, "harness": 0.0}
    steps = 0
    started = clock()
    while game.phase not in TERMINAL and game.tick < max_ticks:
        if stopping and over(game, wave, act, until):
            break
        steps += 1
        if profile:
            t0 = clock()
            frame = observe(game)
            t1 = clock()
            actions = pilot.act(frame)
            t2 = clock()
            timed["observe"] += t1 - t0
            timed["bot"] += t2 - t1
        else:
            actions = pilot.act(observe(game))
        if actions_tape is not None:
            actions_tape.append(actions)
        wave_now, phase, kills, lives, act_now = game.wave, game.phase, game.kills, game.player.lives, game.act
        hits = game.hits
        before = game.shots  # the engine replaces the list as shots go, so this one still holds the killer
        hps = [(alien, alien.hp) for alien in game.aliens] if phase == "playing" else None
        if profile:
            t3 = clock()
            (keyboard or game).step(actions)
            t4 = clock()
            timed["engine"] += t4 - t3
        else:
            (keyboard or game).step(actions)
        judge.read(game)
        if game.fired:
            acts["shots"][act_now - 1] += sum(fired.kind not in ("dive", "mite") for fired in game.fired)
        events = Counter(game.events)
        counts.update(lives_lost=events["damage"], shield_hits=events["shield"], pickups=events["pickup"])
        if events["life"]:
            # Awards past the lives cap add nothing, so count what the ship actually gained too.
            gained = game.player.lives - lives + events["damage"]
            counts.update(life_awards=events["life"], extra_lives=gained)
            acts["lives"][act_now - 1] += gained
        if events["damage"]:
            damage_waves += [wave_now] * events["damage"]
            acts["deaths"][act_now - 1] += events["damage"]
            judge.death(game, before, pilot)
        if events["bomb"]:
            counts.update(bombs_used=events["bomb"], bomb_kills=events["bomb_kill"])
            acts["bombs"][act_now - 1] += events["bomb"]
        if hps is not None and not events["bomb"]:
            judge.damage(hps)
        shot_kills = game.kills - kills - events["bomb_kill"]
        if shot_kills > 0:
            counts.update(combo_kills=shot_kills, combo_total=game.combo * shot_kills)
        if phase == "playing":
            counts.update(playing_ticks=1, powered_ticks=int(bool(game.powerups)))
            powerups.update(game.powerups.keys())
            acts["minutes"][act_now - 1] += 1 / TICKS_PER_MINUTE
            acts["in_flight"][act_now - 1] += sum(not shot.friendly for shot in game.shots)
            playing_now += 1
            if game.hits > hits or game.kills > kills:
                quiet = 0
            else:
                quiet += 1
                if quiet > longest:
                    longest, longest_wave, longest_at = quiet, wave_now, game.tick - quiet
        if phase != "clear" and game.phase == "clear":  # every cleared wave passes its clear panel
            wave_play[str(wave_now)] = playing_now
        if game.wave != wave_now:
            wave_ticks.append(game.tick)
            playing_now = quiet = 0
        if game.act != act_now:
            loadouts.setdefault(str(game.act), [game.player.lives, game.player.bombs, list(game.result()["picks"])])
        if profile:
            timed["harness"] += clock() - t4
    wall = clock() - started
    moments = link(pilot, judge.lives)
    for moment in moments:
        acts["no_escape"][moment["act"] - 1] += 1
    counts["no_escape"] = len(moments)
    counts["enemy_shots"] = judge.shots["count"]
    if audit and judge.lives:
        verdicts = audit_hits(build, actions_tape, [entry["tick"] for entry in judge.lives], bot_cadence(bot))
        for entry, verdict in zip(judge.lives, verdicts, strict=True):
            entry["audit"] = verdict
    result = game.result()
    stopped = stopping and over(game, wave, act, until)
    if game.phase == "game_over":
        end = "destroyed" if game.player.lives <= 0 else "landed"
    elif stopped:
        end = "cleared"
    else:
        end = "victory" if result["victory"] else "cap"
    if result["victory"] or (stopped and game.phase in CHOICES):
        cleared = result["wave"]
    else:
        cleared = result["wave"] - 1
    run = {
        "bot": bot,
        "difficulty": difficulty,
        "mode": mode,
        "input": input_mode,
        "seed": seed,
        **result,
        "picks": list(result["picks"]),
        "end": end,
        "cleared": cleared,
        "minutes": result["ticks"] / TICKS_PER_MINUTE,
        **{key: counts[key] for key in COUNTERS},
        "powerup_ticks": dict(sorted(powerups.items())),
        "damage_waves": damage_waves,
        "wave_ticks": wave_ticks,
        "wave_play": wave_play,
        "acts": {key: [round(value, 3) for value in values] for key, values in acts.items()},
        "loadouts": loadouts,
        "kinds": dict(sorted(judge.kinds.items())),
        "lives_log": judge.lives,
        "no_escape_log": moments,
        "shot_log": judge.shots,
        "quiet": {"ticks": longest, "wave": longest_wave, "tick": longest_at},  # the longest play with no hit
        "kind": "wave" if wave is not None else "act" if act is not None else "until" if until is not None else "game",
        "staged": None,
        "wall": round(wall, 3),
    }
    if wave is not None or act is not None:
        run["staged"] = {"wave": wave, "immortal": immortal, "director": dict(director or {})}
        if act is not None:
            run["staged"] |= {"act": act, "loadout": loadout if loadout is not None else LOADOUTS.get(act)}
    if until is not None:
        run["until"] = until
    if policy:
        run["policy"] = policy
    if tape and judge.lives:
        run["tape"] = encode_tape(actions_tape)
    if profile:
        run["profile"] = {key: round(value, 6) for key, value in timed.items()} | {"steps": steps}
    return run


def link(pilot, lives: list[dict]) -> list[dict]:
    """The bot's no-escape moments (no plan safe, the first predicted hit inside its reaction window), each marked
    `hit` when a life was lost within the window plus LINK ticks after it; and each life lost marked `seen` when such
    a moment came before it (the share b of the bombs model, spec §19.1)."""
    reach = getattr(pilot, "window", 0) + LINK
    moments = []
    for tick, _wave, act, bombs, bombed in getattr(pilot, "no_escape", ()):
        hit = any(0 <= entry["tick"] - tick <= reach for entry in lives)
        moments.append({"tick": tick, "act": act, "bombs": bombs, "bombed": bombed, "hit": hit})
    for entry in lives:
        entry["seen"] = any(0 <= entry["tick"] - moment["tick"] <= reach for moment in moments)
    return moments


# --- action tapes (run-length encoded, then deflated; spec §18 "the fairness audit") ---------------------------------

BITS = {name: 1 << index for index, name in enumerate(sorted(ACTIONS))}
NAMES = [tuple(name for name, bit in BITS.items() if mask & bit) for mask in range(1 << len(BITS))]


def encode_tape(tape) -> str:
    """Each step's actions as a bit mask, runs of the same mask as (mask, count as a varint), deflated, base64."""
    out = bytearray()
    last, count = None, 0
    for actions in tape:
        mask = 0
        for name in actions:
            mask |= BITS[name]
        if mask == last:
            count += 1
            continue
        if last is not None:
            _run(out, last, count)
        last, count = mask, 1
    if last is not None:
        _run(out, last, count)
    return base64.b64encode(zlib.compress(bytes(out), 9)).decode("ascii")


def _run(out: bytearray, mask: int, count: int) -> None:
    out.append(mask)
    while True:
        byte, count = count & 0x7F, count >> 7
        out.append(byte | (0x80 if count else 0))
        if not count:
            return


def decode_tape(text: str) -> list[tuple[str, ...]]:
    data = zlib.decompress(base64.b64decode(text))
    tape, index = [], 0
    while index < len(data):
        mask = data[index]
        index += 1
        count = shift = 0
        while True:
            byte = data[index]
            index += 1
            count |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        tape += [NAMES[mask]] * count
    return tape


# --- planned games, shards and workers --------------------------------------------------------------------------------


def job(bot, difficulty="normal", mode="classic", seed=0, **options) -> dict:
    """A planned game: play()'s arguments by name."""
    return {"bot": bot, "difficulty": difficulty, "mode": mode, "seed": seed, **options}


def play_job(planned) -> dict:
    """A planned game: play()'s positional arguments, or its keywords."""
    return play(**planned) if isinstance(planned, dict) else play(*planned)


def _indexed(item):
    index, planned = item
    return index, play_job(planned)


@functools.cache
def build() -> str:
    """This tree's build: a sha256 over RULESET and every file that can change a game or its audit (the game's code,
    the bots, this tool), so a shard file, an audit or a merge never mixes games played by other code (review BH1)."""
    digest = hashlib.sha256(str(RULESET).encode())
    files = [*sorted((ROOT / "terminal_invaders").rglob("*.py")), *sorted((TOOLS / "bots").glob("*.py"))]
    for path in [*files, Path(__file__).resolve()]:
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


class NoGames(ValueError):
    """A --merge, --audit or --loadout directory that is missing or holds no shard file."""


def job_key(planned) -> str:
    """A planned game's name in a shard file, so a stopped shard resumes where it was."""
    if not isinstance(planned, dict):
        return "/".join(str(part) for part in planned)
    if planned.get("wave") is not None:  # the sweep's staged waves (B-F's names)
        director = planned.get("director") or {}
        density = f"{director.get('slots', '-')}x{director.get('reload', '-')}"
        return f"{planned['bot']}/{planned.get('difficulty', 'normal')}/w{planned['wave']}/{density}/{planned['seed']}"
    parts = [planned["bot"], planned.get("difficulty", "normal"), planned.get("mode", "classic")]
    parts += [planned.get("input_mode", "direct"), str(planned["seed"])]
    if planned.get("policy"):
        parts.append(f"picks={planned['policy']}")
    if planned.get("act") is not None:
        parts.append(f"act{planned['act']}")
        if planned.get("loadout") is not None:
            load = planned["loadout"]
            parts.append(f"load={load['lives']},{load['bombs']},{'+'.join(load['picks'])}")
    if planned.get("until") is not None:
        parts.append(f"until{planned['until']}")
    if planned.get("immortal"):
        parts.append("immortal")
    if planned.get("director"):
        parts.append("dir:" + ",".join(f"{k}={v}" for k, v in sorted(planned["director"].items())))
    if planned.get("max_ticks", DEFAULT_TICKS) != DEFAULT_TICKS:
        parts.append(f"cap{planned['max_ticks']}")
    return "/".join(parts)


def weight(planned) -> float:
    """About how long a planned game runs, to play the longest first (a long game last would hold a shard up)."""
    if not isinstance(planned, dict):
        return planned[4] if len(planned) > 4 else DEFAULT_TICKS
    if planned.get("wave") is not None:
        return TICKS_PER_MINUTE
    if planned.get("until") is not None:
        return planned["until"] * TICKS_PER_MINUTE
    cap = planned.get("max_ticks", DEFAULT_TICKS)
    if planned.get("act") is not None:
        return min(cap, 9 * TICKS_PER_MINUTE)
    return cap if planned.get("mode") == "endless" else min(cap, 35 * TICKS_PER_MINUTE)


def run_all(jobs: list, workers: int, done=None) -> list[dict]:
    """Play every job, in worker processes when asked, the longest first; results keep job order. done(job, run) is
    called as each game finishes (a shard file writes it at once, so a stopped shard resumes)."""
    workers = min(workers, len(jobs))
    order = sorted(range(len(jobs)), key=lambda index: -weight(jobs[index]))
    runs = [None] * len(jobs)
    pool = multiprocessing.Pool(workers) if workers > 1 else None
    finished = 0
    try:
        items = [(index, jobs[index]) for index in order]
        stream = pool.imap_unordered(_indexed, items, chunksize=1) if pool else map(_indexed, items)
        for index, run in stream:
            runs[index] = run
            finished += 1
            if done is not None:
                done(jobs[index], run)
            if sys.stderr.isatty():
                print(f"\r{finished}/{len(jobs)} games", end="", file=sys.stderr, flush=True)
    finally:
        if pool is not None:
            pool.close()
            pool.join()
    if sys.stderr.isatty():
        print("\r\033[K", end="", file=sys.stderr, flush=True)
    return runs


def run_kind(run) -> str:
    """game (a whole run), until (stopped at a wave's clear), act (a staged act) or wave (a staged wave)."""
    if "kind" in run:
        return run["kind"]
    return "wave" if run.get("staged") else "game"


def read_runs(directories) -> list[dict]:
    """Every game in the directories' shard files (one JSON line each), each once (this build's line where a job
    has several), with the audit's verdicts (from the audit-*.jsonl files there) on its lives lost: the first audit
    of the same build whose verdicts are for the very ticks the game lost its lives at (review BH4). Raises NoGames
    for a directory that is missing or holds no shard file (review BH5)."""
    if isinstance(directories, (str, Path)):
        directories = [directories]
    runs, audits = {}, {}
    for directory in map(Path, directories):
        if not directory.is_dir():
            raise NoGames(f"no directory {directory}")
        paths = sorted(directory.glob("*.jsonl"))
        if all(path.name.startswith("audit-") for path in paths):
            raise NoGames(f"no shard file (*.jsonl) in {directory}")
        for path in paths:
            for entry in read_runs_file(path):
                if path.name.startswith("audit-"):
                    audits.setdefault(entry["job"], []).append(entry)
                else:
                    held = runs.get(entry["job"])
                    if held is None or (held.get("build") != build() and entry.get("build") == build()):
                        runs[entry["job"]] = entry
    for key, entries in audits.items():
        run = runs.get(key)
        audited = next((entry for entry in entries if run is not None and fits(run, entry)), None)
        if audited is not None:
            for entry, verdict in zip(run["lives_log"], audited["verdicts"], strict=True):
                entry["audit"] = verdict
    return list(runs.values())


def fits(run, audited) -> bool:
    """An audit line is this game's: the same build, and a verdict for each life lost, at its tick."""
    ticks = [verdict["tick"] for verdict in audited["verdicts"]]
    return audited.get("build") == run.get("build") and ticks == [entry["tick"] for entry in run["lives_log"]]


def read_runs_file(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def shard(value: str) -> tuple[int, int]:
    try:
        k, n = (int(part) for part in value.split("/"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected K/N, as 2/3") from exc
    if not 1 <= k <= n:
        raise argparse.ArgumentTypeError("K must be 1 to N")
    return k, n


# --- the tiers (spec §19.2, §23.3, §23.4) -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Row:
    """One row of a tier: a bot's games on a difficulty and mode, on a seed range."""

    name: str
    bot: str
    difficulty: str
    mode: str
    start: int
    seeds: int
    minutes: float = 60.0  # the cap on simulated minutes per game
    until: int | None = None  # each game stops when this wave ends
    tapes: bool = False  # keep the action tapes for the audit (gate row Q10)
    input: str = "direct"


# §23.3's gate on the held-out seeds, with §27's strong bot and row P, and §27.2's rows (provisional until the owner
# answers): Q1 plays the strong bot on Hard, and its games on Normal ("Q1 Normal", whose win rate is reported, not
# judged) are the ones Q5, Q6, Q8, Q9 and I1 read. Q7 plays Endless on Hard with Q1 (added after tuning round 5).
# Strong's Normal Endless median is informational, and neither tier plays it: in round 5's tune tier its 100 games
# took 2.7 of the 5.4 CPU-hours (69 to the 180-minute cap) for a number no row judges (wave 115). The tune tier
# (§19.2) plays the same rows on 100 tuning seeds each. Q1's and P's games keep their tapes: Q10 audits the lives
# both lose.
GATE = (
    Row("Q1", "strong", "hard", "classic", 5000, 400, tapes=True),
    Row("Q1 Normal", "strong", "normal", "classic", 5000, 400),
    Row("P", "skilled", "normal", "classic", 5000, 200, tapes=True),
    Row("Q2", "sk_d18_p85", "normal", "classic", 5000, 400),
    Row("Q3", "novice", "normal", "classic", 5000, 300, until=1),
    Row("Q4 Easy", "sk_d18_p85", "easy", "classic", 5000, 300),
    Row("Q4 Hard", "sk_d18_p85", "hard", "classic", 5000, 200),
    Row("Q7", "strong", "hard", "endless", 7000, 100, minutes=180),
)
TUNE = tuple(
    Row(
        row.name,
        row.bot,
        row.difficulty,
        row.mode,
        6000 if row.mode == "endless" else 1000,
        100,
        row.minutes,
        row.until,
        row.tapes,
    )  # fmt: skip
    for row in GATE
)
GATE_INPUT = tuple(Row(f"I1 {name}", I1_BOT, "normal", "classic", 5000, 200, input=name) for name in KEYED)
TIERS = {"gate": GATE, "tune": TUNE, "gate_input": GATE_INPUT}


def tier_jobs(rows, *, tapes=False, seeds=None, bots_=None, inputs=None) -> list[dict]:
    """Every game of a tier's rows, row by row and seed by seed. seeds: games per row instead of the row's own;
    bots_ and inputs: the rows of these bots and inputs only (--gate-input's --bots and --input)."""
    out = []
    for row in rows:
        if bots_ is not None and row.bot not in bots_:
            continue
        if inputs is not None and row.input not in inputs:
            continue
        for seed in range(row.start, row.start + (seeds or row.seeds)):
            planned = job(row.bot, row.difficulty, row.mode, seed, max_ticks=round(row.minutes * TICKS_PER_MINUTE))
            if row.input != "direct":
                planned["input_mode"] = row.input
            if row.until is not None:
                planned["until"] = row.until
            if (row.tapes or tapes) and row.input == "direct":
                planned["tape"] = True
            out.append(planned)
    return out


def sweep_jobs(seeds: int = SWEEP_SEEDS, start: int = SWEEP_START) -> list[dict]:
    """The sweep's games: staged immortal waves 5, 8 and 10 at each density point, for skilled and sk_d18_p85 at
    every point and skilled 10 and 14 ticks late at three; `seeds` games per wave, bot and point."""
    plan = [(bot, SWEEP_POINTS) for bot in SWEEP_BOTS] + [(bot, SWEEP_DELAY_POINTS) for bot in SWEEP_DELAYS]
    jobs = []
    for bot, points in plan:
        for slots, reload in points:
            director = {"slots": slots, "reload": reload, **SWEEP_FIXED}
            for wave in SWEEP_WAVES:
                for seed in range(start, start + seeds):
                    jobs.append(
                        {"bot": bot, "difficulty": "normal", "mode": "classic", "seed": seed, "max_ticks": SWEEP_CAP}
                        | {"wave": wave, "immortal": True, "director": director, "audit": True}
                    )
    return jobs


def median_loadouts(runs: list[dict]) -> dict:
    """Per act II-IV, over the whole classic runs that reached it: the median (low) lives and bombs at its first
    tick, and the picks most of them held then (spec §18: once full runs exist, these replace LOADOUTS)."""
    whole = [run for run in runs if run_kind(run) == "game" and run["mode"] == "classic"]
    out = {}
    for act in range(2, ACTS + 1):
        seen = [run["loadouts"][str(act)] for run in whole if str(act) in run.get("loadouts", {})]
        if not seen:
            continue
        out[act] = {
            "lives": statistics.median_low(entry[0] for entry in seen),
            "bombs": statistics.median_low(entry[1] for entry in seen),
            "picks": list(Counter(tuple(entry[2]) for entry in seen).most_common(1)[0][0]),
        }
    return out


def director_pair(value: str) -> tuple[str, float]:
    """--director KEY=VALUE: a director field (spec §3.6) and its number."""
    key, sep, number = value.partition("=")
    if not sep or key not in DIRECTOR_KEYS:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE with KEY one of {', '.join(DIRECTOR_KEYS)}")
    try:
        parsed = float(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{key}: expected a number") from exc
    return key, int(parsed) if parsed.is_integer() and "." not in number else parsed


# --- the fairness audit (spec §18; gate row Q10) --------------------------------------------------------------------


def bot_cadence(bot: str) -> int:
    """The ticks between a bot's decisions (params.cadence of its delay: 3 for perfect, 4 for strong, 6 for the good
    bot): where the audit's two plans in a row may switch (spec §27.2)."""
    return bot_params.cadence(bots.kind(bot).delay)


def audit_hits(build, tape: list, hits: list[int], cadence: int = bot_params.cadence(0)) -> list[dict]:
    """Spec §18's audit of the lives one game lost, engine only (the judge may read the game), with spec §27.2's
    search of two plans in a row. `build()` makes the game as it started, `tape[i]` holds the actions of its i-th
    step, `hits` the ticks the lives were lost at (the game's tick after the step that took each), and `cadence`
    the ticks between the bot's decisions (bot_cadence; the default is the finest, perfect's 3).

    For a hit at tick h: one copy of the game at t0, the last multiple of AUDIT_STEP at least AUDIT_STEP ticks
    before h (rng streams included). From it, with perfect information (the engine itself) and fire as recorded,
    these are flown in turn until one avoids the hit (search()):
    1. every plan of the library: its moves, then standing;
    2. two plans in a row (spec §27.2): every first plan, switched at each of its decision points (t0 + cadence,
       t0 + 2 cadence, ...) before the tick it lost its life when flown alone (a first plan already hit by then has
       lost it), to every second plan of the library, flown from the switch as a plan flies from t0;
    3. only if the ship held a bomb at t0: every plan with one bomb press at each tick from t0 to h;
    4. and then, with that bomb, the two plans of step 2 with the bomb pressed at the switch, for switches before h.
    5. A landing (the tape's life lost with hit_cause "landing", cause_of()) can't be dodged, only shot: when steps
       1-4 find nothing, they are flown again with fire held (fire pressed at every step, as Space held: the ship
       refires whenever a slot frees, a legal key sequence; spec §18).
    A sequence avoids the hit only if no life is lost from t0 until h + AUDIT_AFTER, so dodging one shot into another
    doesn't count. The search is deterministic and bounded: a sequence that moves the ship as one already flown
    does (tick for tick, with the same bomb press and the same fire) is not flown again, and every second plan flies
    from one copy of the game at its switch, made once for each distinct first plan so far, whichever first plan led
    there.

    Returns per hit {"tick", "t0", "avoided", "plan", "switch", "then", "bomb", "held", "dodged", "flights"}: avoided
    is False for an unavoidable hit; plan is the (first) plan of the first sequence found that avoids it, switch the
    tick its second plan `then` took over (both None for one plan), bomb its bomb tick, held True when it holds fire
    (step 5); dodged, for an unavoidable hit, is True when some single plan was still unhurt at h and lost its life
    later, standing after its last move; flights counts the sequences flown for this hit (the audit's cost)."""
    game = build()
    start = game.tick
    verdicts = []
    for hit in hits:
        t0 = max(start, (hit - AUDIT_STEP) // AUDIT_STEP * AUDIT_STEP)
        while game.tick < t0:
            game.step(tape[game.tick - start])
        base = copy.deepcopy(game)  # the replay goes on from `game`; every plan flies from a copy of this one
        verdict = {"tick": hit, "t0": t0, "avoided": False, "plan": None, "switch": None, "then": None, "bomb": None}
        verdict |= {"held": False, "dodged": False, "flights": 0}
        found, flights, later = search(base, tape, start, hit, cadence)
        verdict["flights"] += flights
        if found is None and cause_of(base, tape, start, hit) == "landing":
            found, flights, later_held = search(base, tape, start, hit, cadence, held=True)
            verdict["flights"] += flights
            later = later or later_held
        verdict |= found or {}
        verdict["dodged"] = later and not verdict["avoided"]
        verdicts.append(verdict)
    return verdicts


def cause_of(base, tape: list, start: int, hit: int) -> str:
    """The cause (hit_cause) of the life the tape lost at tick `hit`, replayed from a copy of `base` (the game at
    t0); "" if the replay lost none there."""
    game = copy.deepcopy(base)
    while game.tick < hit and game.phase not in TERMINAL:
        game.step(tape[game.tick - start])
    return game.hit_cause if game.tick == hit and "damage" in game.events else ""


def search(base, tape: list, start: int, hit: int, cadence: int, held: bool = False):
    """Steps 1-4 of audit_hits from `base`, the game at t0, for the hit at tick `hit`: with fire as recorded, or
    with fire held (step 5). Returns the verdict's {"avoided", "plan", "switch", "then", "bomb", "held"} for the
    first sequence found that loses no life until hit + AUDIT_AFTER (else None), the sequences flown, and whether
    some single plan was unhurt at the hit's tick and lost its life later."""
    t0, end = base.tick, hit + AUDIT_AFTER
    found, flights = None, 0
    lost = {}  # each plan's tick of its first life lost, flown alone
    for plan in bot_plan.PLANS:
        lost[plan] = fly(base, tape, start, plan, end, held=held)
        flights += 1
        if lost[plan] is None:
            found = {"avoided": True, "plan": list(plan)}
            break
    later = any(tick is not None and tick > hit for tick in lost.values())  # unhurt at h, hit after it
    if found is None:
        found, more = switched(base, tape, start, end, cadence, lost, held=held)
        flights += more
    if found is None and base.player.bombs > 0:
        for plan in bot_plan.PLANS:
            for tick in range(t0, hit):
                flights += 1
                if fly(base, tape, start, plan, end, tick, held=held) is None:
                    found = {"avoided": True, "plan": list(plan), "bomb": tick}
                    break
            if found is not None:
                break
        if found is None:
            found, more = switched(base, tape, start, end, cadence, lost, bomb_before=hit, held=held)
            flights += more
    if found is not None:
        found["held"] = held
    return found, flights, later


def switched(base, tape: list, start: int, end: int, cadence: int, lost: dict, bomb_before=None, held=False):
    """Step 2 of audit_hits (step 4 with bomb_before = h): two plans in a row from `base`, the game at t0, until tick
    `end`. `lost` holds each plan's tick of its first life lost, flown alone; a first plan switches only before it.
    With bomb_before, the bomb is pressed at the switch, for the switches before that tick. With held, fire is held
    throughout (step 5), and `lost` comes from the plans flown alone with fire held. Returns the verdict's
    {"avoided", "plan", "switch", "then", "bomb"} for the first sequence found that loses no life (else None), and
    the sequences flown."""
    t0, span = base.tick, end - base.tick
    singles = {legs(moves(plan, span), span) for plan in bot_plan.PLANS}  # step 1 flew these (step 3, every bomb)
    flown, shared, flights = set(), set(), 0
    for first in bot_plan.PLANS:
        last = lost[first] if bomb_before is None else min(lost[first], bomb_before)
        game = None  # the first plan flown alone, up to the switch
        for switch in range(t0 + cadence, last, cadence):
            before = moves(first, switch - t0)
            state = (switch, legs(before, span))
            if state in shared:  # the same moves to this switch: the same game there, its sequences all flown
                continue
            shared.add(state)
            if game is None:
                game = copy.deepcopy(base)
            while game.tick < switch:  # no life lost on the way: the first plan alone lost one only at lost[first]
                step_plan(game, tape, start, first, t0, held=held)
            bomb = None if bomb_before is None else switch
            for then in bot_plan.PLANS:
                motion = legs([*before, *moves(then, span)], span)
                if motion in singles or (motion, bomb) in flown:
                    continue
                flown.add((motion, bomb))
                flights += 1
                if fly(game, tape, start, then, end, bomb, held=held) is None:
                    found = {"avoided": True, "plan": list(first), "switch": switch, "then": list(then), "bomb": bomb}
                    return found, flights
    return None, flights


def moves(plan, ticks: int) -> list[tuple[int, int]]:
    """A plan's moves over exactly its first `ticks` ticks, as (direction, ticks) pairs: its two legs, then standing."""
    d1, k1, d2, k2 = plan
    first = min(k1, ticks)
    second = min(k2, ticks - first)
    return [(d1, first), (d2, second), (0, ticks - first - second)]


def legs(pairs, total: int) -> tuple:
    """A motion's first `total` ticks of (direction, ticks) pairs as flat legs (d, n, d, n, ...), each leg joined to
    the one before when it goes the same way and the standing at the end left out: two sequences that move the ship
    the same way tick for tick have the same legs."""
    out, left = [], total
    for d, n in pairs:
        n = min(n, left)
        if n <= 0:
            continue
        left -= n
        if out and out[-2] == d:
            out[-1] += n
        else:
            out += [d, n]
    if out and out[-2] == 0:
        del out[-2:]
    return tuple(out)


def step_plan(game, tape: list, start: int, plan, t0: int, bomb=None, held=False) -> None:
    """One step of `plan` flown from tick t0, with fire as the tape recorded it (with held, fire pressed whatever the
    tape holds: Space held) and a bomb press at the step from tick `bomb`."""
    index = game.tick - start
    actions = ["fire"] if held or (index < len(tape) and "fire" in tape[index]) else []
    move = bot_plan.direction(plan, game.tick - t0 + 1)
    if move:
        actions.append("right" if move > 0 else "left")
    if game.tick == bomb:
        actions.append("bomb")
    game.step(tuple(actions))


def fly(base, tape: list, start: int, plan, end: int, bomb=None, held=False):
    """Fly `plan` from a copy of `base` until tick `end` (or the game's end): its moves, then standing; fire as the
    tape recorded it, or held at every step (held); one bomb press at the step from tick `bomb`. The tick the first
    life was lost, else None."""
    game = copy.deepcopy(base)
    t0 = game.tick
    while game.tick < end and game.phase not in TERMINAL:
        step_plan(game, tape, start, plan, t0, bomb, held)
        if "damage" in game.events:
            return game.tick
    return None


def run_builder(run):
    """The game a recorded run started from (its config, and its staged start if any)."""
    config = Config(run["difficulty"], run["mode"], run["seed"])
    staged_at = run.get("staged") or {}
    return builder(
        config, wave=staged_at.get("wave"), act=staged_at.get("act"), immortal=staged_at.get("immortal", False),
        director=staged_at.get("director") or None, loadout=staged_at.get("loadout"),
    )  # fmt: skip


def audit_run(run) -> dict:
    """The audit of one recorded run's lives lost, from its tape: {"job", "verdicts"}."""
    hits = [entry["tick"] for entry in run["lives_log"]]
    verdicts = audit_hits(run_builder(run), decode_tape(run["tape"]), hits, bot_cadence(run["bot"]))
    return {"job": run["job"], "verdicts": verdicts}


def audit_dir(directory: Path, part: tuple[int, int], workers: int) -> tuple[int, int, list, int]:
    """--audit DIR: this shard's share of the recorded runs there with a tape (every one that lost a life),
    audited into DIR/audit-K-of-N.jsonl, resumable: a run is kept only when an audit there is its own (fits()).
    Only this build's runs are audited (a tape replays only on the code that recorded it). Returns (runs audited
    now, kept from before, the unavoidable hits of the share as (run, life) pairs, runs of another build skipped)."""
    k, n = part
    taped = [run for run in read_runs(directory) if run.get("tape")]
    runs = sorted((run for run in taped if run.get("build") == build()), key=lambda run: run["job"])
    share = runs[k - 1 :: n]
    path = Path(directory) / f"audit-{k}-of-{n}.jsonl"
    stored = {}
    for audited in read_runs_file(path) if path.exists() else ():
        stored.setdefault(audited["job"], []).append(audited)
    todo = [run for run in share if not any(fits(run, audited) for audited in stored.get(run["job"], ()))]
    workers = max(1, min(workers, len(todo)))
    pool = multiprocessing.Pool(workers) if workers > 1 else None
    try:
        with path.open("a", encoding="utf-8") as sink:
            for audited in pool.imap_unordered(audit_run, todo, chunksize=1) if pool else map(audit_run, todo):
                sink.write(json.dumps(audited | {"build": build()}) + "\n")
                sink.flush()
    finally:
        if pool is not None:
            pool.close()
            pool.join()
    mine = {run["job"] for run in share}
    unfair = [
        (run, entry)
        for run in read_runs(directory)
        if run["job"] in mine
        for entry in run["lives_log"]
        if "audit" in entry and not entry["audit"]["avoided"]
    ]
    return len(todo), len(share) - len(todo), unfair, len(taped) - len(runs)


# --- the report ----------------------------------------------------------------------------------------------------


def wilson(successes: int, games: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson 95% interval of a rate."""
    if games <= 0:
        return 0.0, 1.0
    p = successes / games
    centre = (p + z * z / (2 * games)) / (1 + z * z / games)
    half = z * math.sqrt(p * (1 - p) / games + z * z / (4 * games * games)) / (1 + z * z / games)
    return max(0.0, centre - half), min(1.0, centre + half)


def group_key(run) -> tuple:
    extra = run_kind(run)
    if extra == "act":
        extra = f"act {run['staged']['act']}"
    elif extra == "until":
        extra = f"until {run.get('until')}"
    return run["mode"], run["bot"], run["difficulty"], run["input"], extra


def summarise(runs: list[dict]) -> list[dict]:
    """One row per mode, bot, difficulty, input and kind of game, in the order the runs were planned."""
    groups = {}
    for run in runs:
        groups.setdefault(group_key(run), []).append(run)
    rows = []
    for (mode, bot, difficulty, input_mode, kind), group in groups.items():
        total = Counter()
        for run in group:
            total.update({key: run[key] for key in ("ticks", "shots", "hits", "kills", *COUNTERS)})
        top = max(run["cleared"] for run in group) if mode == "endless" else CAMPAIGN_WAVES
        waves = [run["wave"] for run in group]
        games = len(group)
        lives = [entry for run in group for entry in run["lives_log"]]
        wins = sum(run["victory"] for run in group)
        per_act = {
            key: [round(sum(run["acts"][key][index] for run in group) / games, 3) for index in range(ACTS)]
            for key in ("deaths", "bombs", "no_escape")
        }
        minutes = [sum(run["acts"]["minutes"][index] for run in group) for index in range(ACTS)]
        ticks = [minute * TICKS_PER_MINUTE for minute in minutes]
        lost = Counter(wave for run in group for wave in run["damage_waves"])
        winners = [run["minutes"] for run in group if run["victory"]]
        clears = {}
        for run in group:
            for wave, played in run.get("wave_play", {}).items():
                clears.setdefault(int(wave), []).append(played)
        picks = Counter(name for run in group for name in dict.fromkeys(run["picks"]))
        kinds = {}
        for run in group:
            for name, values in run.get("kinds", {}).items():
                entry = kinds.setdefault(name, [0, 0, 0])
                for index, value in enumerate(values):
                    entry[index] += value
        seconds = max(1, total["playing_ticks"]) / TICK_RATE
        profiled = [run["profile"] for run in group if run.get("profile")]
        quiet = [run["quiet"]["ticks"] for run in group if run.get("quiet")]  # games played before it was kept: none
        rows.append(
            {
                "mode": mode,
                "bot": bot,
                "difficulty": difficulty,
                "input": input_mode,
                "kind": kind,
                "games": games,
                "win_rate": wins / games,
                "wins_interval": list(wilson(wins, games)),
                "clean_win_rate": sum(run["victory"] and not run.get("continues_used") for run in group) / games,
                "continues_per_run": sum(run.get("continues_used", 0) for run in group) / games,
                "wave1_rate": sum(run["cleared"] >= 1 for run in group) / games,
                "wave_mean": statistics.fmean(waves),
                "wave_median": statistics.median(waves),
                "survival": [sum(run["cleared"] >= wave for run in group) / games for wave in range(1, top + 1)],
                "minutes_median": statistics.median(run["minutes"] for run in group),
                "winners_minutes_median": statistics.median(winners) if winners else None,
                "accuracy": total["hits"] / max(1, total["shots"]),
                "lives_lost_per_minute": total["lives_lost"] / max(1, total["ticks"]) * TICKS_PER_MINUTE,
                "bomb_kill_share": total["bomb_kills"] / max(1, total["kills"]),
                "powerup_uptime": total["powered_ticks"] / max(1, total["playing_ticks"]),
                "combo_mean": total["combo_total"] / max(1, total["combo_kills"]),
                "extra_lives_mean": total["extra_lives"] / games,
                "bombs_per_run": total["bombs_used"] / games,
                "ends": dict(Counter(run["end"] for run in group).most_common()),
                "deaths_per_act": per_act["deaths"],
                "bombs_per_act": per_act["bombs"],
                "no_escape_per_act": per_act["no_escape"],
                "shots_per_minute_per_act": [
                    round(sum(run["acts"]["shots"][index] for run in group) / minutes[index], 2)
                    if minutes[index]
                    else 0
                    for index in range(ACTS)
                ],
                "in_flight_per_act": [
                    round(sum(run["acts"]["in_flight"][index] for run in group) / ticks[index], 3)
                    if ticks[index]
                    else 0
                    for index in range(ACTS)
                ],
                "extra_lives_per_act": [
                    round(sum(run["acts"].get("lives", [0] * ACTS)[index] for run in group) / games, 3)
                    for index in range(ACTS)
                ],
                "deaths_noticed_late": sum(entry["late"] for entry in lives),
                "deaths_with_bomb": sum(entry["bombs"] > 0 for entry in lives),
                "causes": dict(Counter(entry["cause"] for entry in lives).most_common()),
                "worst_wave_share": max(lost.values()) / sum(lost.values()) if lost else 0.0,
                "wave_seconds": {
                    str(wave): [len(played), statistics.median(played) / TICK_RATE]
                    for wave, played in sorted(clears.items())
                },
                "pick_shares": {name: count / games for name, count in picks.most_common()},
                "per_kind": {
                    name: {
                        "kills": killed,
                        "hits_per_kill": round(hit / killed, 2) if killed else None,
                        "hp_per_second": round(hp / seconds, 3),
                    }
                    for name, (killed, hit, hp) in sorted(kinds.items(), key=lambda item: -item[1][0])
                },
                "hits_per_second": total["hits"] / seconds,
                "shots_per_kill": total["shots"] / max(1, total["kills"] - total["bomb_kills"]),
                "loadouts": median_loadouts(group) if kind == "game" else {},
                "profile": profile_row(profiled) if profiled else None,
                "stalls": sum(ticks >= STALL_TICKS for ticks in quiet),
                "quiet_max_seconds": max(quiet) / TICK_RATE if quiet else None,
            }
        )
    return rows


def profile_row(profiled: list[dict]) -> dict:
    """µs a tick of observe, the bot (without observe), the engine and the harness, and ticks a second, over the
    profiled games (each game's wall time is all four)."""
    steps = sum(entry["steps"] for entry in profiled) or 1
    parts = {key: sum(entry[key] for entry in profiled) for key in ("observe", "bot", "engine", "harness")}
    wall = sum(parts.values()) or 1e-9
    out = {key: round(value / steps * 1e6, 2) for key, value in parts.items()}
    return out | {
        "bot_observe": round(out["observe"] + out["bot"], 2),
        "ticks_per_s": round(steps / wall),
        "steps": steps,
    }


def checkpoints(top: int, columns=12) -> list[int]:
    """Wave numbers for a survival curve: every wave of the Campaign, or round steps when there are many more."""
    if top <= CAMPAIGN_WAVES:
        return list(range(1, top + 1))
    step = next(s * 10**e for e in range(9) for s in (1, 2, 5) if top <= s * 10**e * columns)
    return sorted({1, *range(step, top + 1, step)})


def label(row) -> str:
    text = f"{bots.shown(row['bot']):<12} {row['difficulty']:<10} {row['input']:<11}"
    kind = row.get("kind", "game")
    return text if kind == "game" else f"{text} [{kind}]"


def dash(value, spec="g") -> str:
    return "-" if value is None else format(value, spec)


def format_report(rows: list[dict]) -> str:
    lines = [f"{'bot':<12} {'difficulty':<10} {'input':<11}" + "".join(f"  {title:>6}" for title, _, _ in TABLE)]
    for row in rows:
        cells = (f"  {format(row[key], spec):>{max(6, len(title))}}" for title, key, spec in TABLE)
        lines.append(label(row) + "".join(cells))
    lines.append(LEGEND)
    waves = checkpoints(max((len(row["survival"]) for row in rows), default=1))
    lines += ["", f"{'cleared wave (% games)':<35}" + "".join(f"{wave:>4}" for wave in waves)]
    for row in rows:
        curve = row["survival"]
        cells = (f"{100 * curve[wave - 1]:>4.0f}" if wave <= len(curve) else f"{0:>4}" for wave in waves)
        lines.append(label(row) + "".join(cells))
    lines += ["", f"{'per game, acts I-IV':<35}" + "".join(f"  {title:<23}" for title, _ in PER_ACT)]
    for row in rows:
        cells = (" ".join(f"{value:>5.2f}" for value in row[key]) for _, key in PER_ACT)
        lines.append(label(row) + "".join(f"  {cell:<23}" for cell in cells))
    lines += ["", f"{'enemy fire and lives, acts I-IV':<35}" + "".join(f"  {title:<27}" for title, _ in PER_ACT_FIRE)]
    for row in rows:
        cells = (" ".join(f"{value:>6.2f}" for value in row[key]) for _, key in PER_ACT_FIRE)
        lines.append(label(row) + "".join(f"  {cell:<27}" for cell in cells))
    lines += ["", "lives lost by cause (noticed late; with a bomb in stock):"]
    for row in rows:
        causes = ", ".join(f"{cause or '?'} {count}" for cause, count in row["causes"].items()) or "none"
        lines.append(f"{label(row)}{causes} ({row['deaths_noticed_late']}; {row['deaths_with_bomb']})")
    waves = sorted({int(wave) for row in rows for wave in row["wave_seconds"]})
    if waves:
        shown = [wave for wave in waves if wave <= CAMPAIGN_WAVES] or waves[:24]
        lines += ["", f"{'wave length (median s of play)':<35}" + "".join(f"{wave:>5}" for wave in shown)]
        for row in rows:
            seconds = row["wave_seconds"]
            cells = (f"{seconds[str(wave)][1]:>5.0f}" if str(wave) in seconds else f"{'-':>5}" for wave in shown)
            lines.append(label(row) + "".join(cells))
    lines += ["", "more per row (clean wins, continues, wave 1 cleared, winners' minutes, worst wave's share of lives "]
    lines[-1] += "lost, bombs a run, hits a second, shots per kill):"
    for row in rows:
        winners = row["winners_minutes_median"]
        lines.append(
            f"{label(row)}{row['clean_win_rate']:.0%}, {row['continues_per_run']:.2f}, {row['wave1_rate']:.0%}, "
            f"{'-' if winners is None else f'{winners:.1f}'}, {row['worst_wave_share']:.0%}, "
            f"{row['bombs_per_run']:.2f}, {row['hits_per_second']:.2f}, {row['shots_per_kill']:.2f}"
        )
    kinds = [row for row in rows if row["per_kind"]]
    if kinds:
        lines += ["", "per enemy kind, in play outside bombs (kills; hits per kill; HP lost a second of play):"]
        for row in kinds:
            cells = ", ".join(
                f"{name} {entry['kills']}; {dash(entry['hits_per_kill'])}; {entry['hp_per_second']:.2f}"
                for name, entry in row["per_kind"].items()
            )
            lines.append(f"{label(row)}{cells}")
    picked = [row for row in rows if row["pick_shares"]]
    if picked:
        lines += ["", "picks (share of games that took each):"]
        for row in picked:
            shares = ", ".join(f"{name} {share:.0%}" for name, share in row["pick_shares"].items())
            lines.append(f"{label(row)}{shares}")
    return "\n".join(lines)


def format_notes(rows: list[dict]) -> list[str]:
    """§19.1's informational comparisons: fixed:<code> probes against priority (dominant at 10+ points), and
    bombhappy against skilled, the perfect bot it is built on (it must not beat it)."""
    lines = []
    reference = {(row["mode"], row["difficulty"], row["input"]): row for row in rows if row["bot"] == "skilled"}
    for row in rows:
        base = reference.get((row["mode"], row["difficulty"], row["input"]))
        if base is None or row["kind"] != "game" or base["kind"] != "game":
            continue
        gap = row["win_rate"] - base["win_rate"]
        if row["bot"].startswith(bots.FIXED):
            verdict = "dominant" if gap >= 0.10 else "a trap" if gap <= -0.10 else "within 10 points"
            lines.append(f"{label(row)}{100 * gap:+.0f} points against priority: {verdict}")
        elif row["bot"] == "bombhappy":
            base_name = bots.shown("skilled")
            verdict = f"beats {base_name} (an exploit)" if gap > 0 else f"does not beat {base_name}"
            lines.append(f"{label(row)}{100 * gap:+.0f} points against {base_name}: {verdict}")
    return lines


def shot_log(runs: list[dict]) -> dict:
    """The judge's shot log over every game (spec §18, gate row F): the extremes, and how many broke a rule."""
    logs = [run["shot_log"] for run in runs]

    def extreme(key, pick):
        values = [log[key] for log in logs if log[key] is not None]
        return pick(values) if values else None

    out = {key: sum(log[key] for log in logs) for key in ("count", "dives", "mites", "bad_source")}
    out["bad"] = [entry for log in logs for entry in log["bad"]][:40]
    out |= {key: extreme(key, min) for key in ("min_w", "min_flight", "min_dive_w", "min_mite_w")}
    out["max_y"] = extreme("max_y", max)
    out["violations"] = sum(violations(log) for log in logs)
    return out


def violations(log: dict) -> int:
    """How many of a game's shot-log extremes break F1, F2 and the dive and mite rules, plus shots from a diver,
    mite or moving enemy."""
    floors = (
        ("min_w", LIMITS.warning),
        ("min_flight", LIMITS.flight),
        ("min_dive_w", LIMITS.dive_warning),
        ("min_mite_w", LIMITS.mite_warning),
    )
    broken = sum(log[key] is not None and log[key] < floor for key, floor in floors)
    broken += log["max_y"] is not None and log["max_y"] > LIMITS.spawn_y + 1e-9
    return broken + log["bad_source"]


def format_shot_log(log: dict) -> str:
    def low(value):
        return "-" if value is None else f"{value:g}"

    return (
        f"shot log: {log['count']} shots, warning >= {low(log['min_w'])} ticks (floor {LIMITS.warning}), flight >= "
        f"{low(log['min_flight'])} (floor {LIMITS.flight}), fired from y <= {low(log['max_y'])} (cap {LIMITS.spawn_y}), "
        f"{log['bad_source']} from a diver, mite or moving enemy; {log['dives']} dives, warning >= "
        f"{low(log['min_dive_w'])} (floor {LIMITS.dive_warning}); {log['mites']} mites, warning >= "
        f"{low(log['min_mite_w'])} (floor {LIMITS.mite_warning}): {log['violations']} outside the rules"
    )


def stalls(runs: list[dict]) -> list[dict]:
    """The games that stood still (STALL_TICKS of play or more with no hit on the fleet or a boss), as failed checks
    for --check: a stalled game measures nothing, and Q7 would count the wave it stood in as reached."""
    out = []
    for run in runs:
        quiet = run.get("quiet") or {}
        if quiet.get("ticks", 0) >= STALL_TICKS:
            where = f"{bots.shown(run['bot'])} {run['difficulty']} {run['mode']} {run['input']} seed {run['seed']}"
            minutes = quiet["ticks"] / TICKS_PER_MINUTE
            detail = (
                f"{where}: wave {quiet['wave']}, {minutes:.1f} minutes of play with no hit from tick {quiet['tick']}"
            )
            out.append({"row": "stall", "what": "a game standing still", "result": "FAIL", "detail": detail})
    return out


def format_stalls(runs: list[dict], stalled: list[dict]) -> str:
    """One line on the games standing still, then each of them (the first 20)."""
    kept = [run for run in runs if run.get("quiet")]
    limit = STALL_TICKS / TICKS_PER_MINUTE
    line = f"stalls: {len(stalled)} of {len(kept)} games stood still ({limit:g}+ minutes of play with no hit on the "
    line += "fleet or a boss)"
    if len(kept) < len(runs):
        line += f"; {len(runs) - len(kept)} games played before stalls were recorded"
    if kept:
        top = max(kept, key=lambda run: run["quiet"]["ticks"])
        line += (
            f"; the longest stretch with no hit {top['quiet']['ticks'] / TICK_RATE:.0f} s ({bots.shown(top['bot'])} "
            f"{top['difficulty']} {top['mode']} seed {top['seed']}, wave {top['quiet']['wave']})"
        )
    return "\n".join([line, *(f"  {entry['detail']}" for entry in stalled[:20])])


def acceptance(rows: list[dict], minimum: int | None = None) -> list[dict]:
    """Spec 11.5 (gate row I1) for every keyed input of the strong bot (I1_BOT) in classic, against direct on the
    same difficulty: PASS or FAIL where a threshold applies and both the input and direct have `minimum` games, info
    otherwise (with the reason). report() hands it both on the seeds they share (matched_rows). A judged input with
    no direct games is a FAIL when it has `minimum` games itself (a gate-sized run merged without the gate's
    directory: review BH5), else info. minimum: ACCEPT_GAMES by default."""
    minimum = ACCEPT_GAMES if minimum is None else minimum
    judged = [row for row in rows if (row["bot"], row["mode"], row.get("kind", "game")) == (I1_BOT, "classic", "game")]
    direct = {}
    for row in judged:
        if row["input"] == "direct":
            direct.setdefault((row["difficulty"], row.get("reference_for")), row)
    results = []
    for row in judged:
        if row["input"] == "direct":
            continue
        reference = direct.get((row["difficulty"], row["input"]), direct.get((row["difficulty"], None)))
        waves_limit, clears_limit, levels = ACCEPT.get(row["input"], (None, None, ()))
        games = row.get("games", minimum)
        if reference is None:
            judged = waves_limit is not None and row["difficulty"] in levels and games >= minimum
            result = "FAIL" if judged else "info"
            rule = "no direct games on these seeds; merge the gate's directory" if judged else "no direct reference"
            results.append(
                {"input": row["input"], "difficulty": row["difficulty"], "waves": None, "clears": None}
                | {"result": result, "rule": rule, "games": games}
            )
            continue
        waves, clears = row["wave_median"] - reference["wave_median"], row["win_rate"] - reference["win_rate"]
        games = min(games, reference.get("games", minimum))  # the reference is judged on as many games
        if waves_limit is None:
            result, rule = "info", "no threshold"
        elif row["difficulty"] not in levels:
            result, rule = "info", f"no threshold on {row['difficulty']}: {UNTHRESHOLDED}"
        else:
            passed = waves >= -waves_limit and clears >= -clears_limit - 1e-9
            rule = f"median within -{waves_limit}, wins within -{100 * clears_limit:.0f} points"
            result = "PASS" if passed else "FAIL"
            if games < minimum:
                note = "would pass" if passed else "would fail"
                result, rule = f"info (n < {minimum})", f"{rule}; {note}, judged at {minimum} games"
        results.append(
            {"input": row["input"], "difficulty": row["difficulty"], "waves": waves, "clears": clears}
            | {"result": result, "rule": rule, "games": games}
        )
    return results


def format_acceptance(results: list[dict]) -> str:
    lines = [f"Spec 11.5 acceptance ({I1_BOT}, classic), each input against direct on the same difficulty:"]
    lines.append(f"{'input':<11} {'difficulty':<10} {'median':>7} {'wins':>6}  result")
    for result in results:
        cells = (
            f"{'-':>7} {'-':>6}"
            if result["waves"] is None
            else f"{result['waves']:>+7g} {100 * result['clears']:>+6.0f}"
        )
        lines.append(f"{result['input']:<11} {result['difficulty']:<10} {cells}  {result['result']} ({result['rule']})")
    return "\n".join(lines)


def matched_rows(runs: list[dict]) -> list[dict]:
    """Summary rows for the acceptance: per difficulty and keyed input, its strong (I1_BOT) classic games and the
    direct ones on the seeds both played (gate row I1 is judged against the direct games on the same difficulty and
    seeds, review BH5: on Normal, row "Q1 Normal"'s since spec §27.2); each direct row says which input it is the
    reference for. A keyed input with no direct game on any of its seeds keeps all its games, for acceptance() to
    report."""
    whole = [run for run in runs if run_kind(run) == "game" and (run["bot"], run["mode"]) == (I1_BOT, "classic")]
    keyed = [run for run in whole if run["input"] != "direct"]
    out = []
    for difficulty in dict.fromkeys(run["difficulty"] for run in keyed):
        direct = [run for run in whole if run["input"] == "direct" and run["difficulty"] == difficulty]
        played = {run["seed"] for run in direct}
        for name in dict.fromkeys(run["input"] for run in keyed if run["difficulty"] == difficulty):
            mine = [run for run in keyed if (run["difficulty"], run["input"]) == (difficulty, name)]
            seeds = {run["seed"] for run in mine} & played
            if seeds:
                reference = summarise([run for run in direct if run["seed"] in seeds])
                out += [row | {"reference_for": name} for row in reference]
                mine = [run for run in mine if run["seed"] in seeds]
            out += summarise(mine)
    return out


# --- the quality bar (spec §19.1, §23.3): TARGETS and --check -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """A gate row: what it measures over which games, its band, and the fewest games (or wins, or clears) it is
    judged on. Below that it is info, so a 12-seed run can neither pass nor fail the gate. judged=False: an
    informational row (spec §27.2), reported with the band it is expected in and never a FAIL."""

    row: str
    what: str
    bots: tuple[str, ...]
    difficulty: str
    mode: str
    metric: str
    band: tuple[float, float]
    games: int
    boss: tuple[float, float] = (0.0, 0.0)  # Q6: the boss waves' band
    judged: bool = True


# Gate row Q10 audits every life lost in the games that keep their tapes, Q1's (on Hard) and P's (spec §27, §27.2),
# and gate row F checks every game of the gate (review BH6): each row with its own minimum games.
Q10_NEED = {(row.bot, row.difficulty, row.mode): row.seeds for row in GATE if row.tapes}
F_NEED = {(row.bot, row.difficulty, row.mode): row.seeds for row in GATE}
F_ROWS = tuple(F_NEED)
INFORMATIONAL = "reported, not judged (spec §27.2: Normal is tuned for the good player, Hard for the strong one)"
TARGETS = (
    Target("Q1", "strong wins on Hard", ("strong",), "hard", "classic", "wins", (0.60, 0.75), 400),
    Target(
        "Q1 Normal", "strong wins on Normal", ("strong",), "normal", "classic", "wins", (0.90, 1.0), 400, judged=False
    ),
    Target("P", "perfect wins", ("skilled",), "normal", "classic", "wins", (0.90, 1.0), 200),
    Target("Q2", "good wins", ("sk_d18_p85",), "normal", "classic", "wins", (0.30, 0.45), 400),
    Target("Q3", "new player clears wave 1", ("novice",), "normal", "classic", "wave1", (0.75, 1.0), 300),
    Target("Q4 Easy", "good clean wins on Easy", ("sk_d18_p85",), "easy", "classic", "clean_wins", (0.70, 0.85), 300),
    Target("Q4 Normal", "= Q2", ("sk_d18_p85",), "normal", "classic", "wins", (0.30, 0.45), 400),
    Target("Q4 Hard", "good wins on Hard", ("sk_d18_p85",), "hard", "classic", "wins", (0.10, 0.20), 200, judged=False),
    Target("Q5", "winning run, median minutes", ("strong",), "normal", "classic", "winners_minutes", (30, 45), 100),
    Target(
        "Q6",
        "wave length, median s of play",
        ("strong",),
        "normal",
        "classic",
        "wave_lengths",
        (45, 90),
        50,
        boss=(90, 180),
    ),  # fmt: skip
    Target("Q7", "Endless on Hard, median wave", ("strong",), "hard", "endless", "median_wave", (35, 50), 100),
    Target("Q8", "bomb kills / kills", ("strong", "sk_d18_p85"), "normal", "classic", "bomb_kills", (0, 0.10), 400),
    Target("Q9", "power-up uptime", ("strong", "sk_d18_p85"), "normal", "classic", "uptime", (0, 0.30), 400),
    Target(
        "Q10",
        "unavoidable hits (audit)",
        tuple(dict.fromkeys(bot for bot, _difficulty, _mode in Q10_NEED)),
        "",  # Q1's games on Hard, P's on Normal: Q10_NEED
        "classic",
        "unavoidable",
        (0, 0),
        sum(Q10_NEED.values()),
    ),  # fmt: skip
    Target("F", "threats outside F1-F2 (shot log)", (), "", "", "violations", (0, 0), sum(F_NEED.values())),
)


def chosen(runs, bot, difficulty, mode, kinds=("game",)) -> list[dict]:
    return [
        run
        for run in runs
        if (run["bot"], run["difficulty"], run["mode"], run["input"]) == (bot, difficulty, mode, "direct")
        and run_kind(run) in kinds
    ]


def inside(value, band) -> bool:
    return band[0] - 1e-9 <= value <= band[1] + 1e-9


def verdict(target, value, n, need, interval=None, *, detail="", hard=False) -> dict:
    """PASS inside the band with enough games, FAIL outside it, info below the minimum. hard: a count that must
    be 0 fails at any number of games (an unfair hit is a fact, not a rate)."""
    if value is None:
        result = "info (no games)"
    elif hard and value > target.band[1]:
        result = "FAIL"
    elif n < need:
        result = f"info (n < {need})"
    else:
        result = "PASS" if inside(value, target.band) else "FAIL"
    return {
        "row": target.row,
        "what": target.what,
        "value": value,
        "interval": list(interval) if interval else None,
        "n": n,
        "need": need,
        "band": list(target.band),
        "result": result,
        "detail": detail,
    }


def judge_targets(runs: list[dict]) -> list[dict]:
    """Every gate row of TARGETS over these games: its value, Wilson interval for a rate, games, and result (an
    informational row's is info, whatever its value: informational())."""
    out = []
    for target in TARGETS:
        metric = target.metric
        if metric == "violations":
            games = [run for bot, difficulty, mode in F_ROWS for run in chosen(runs, bot, difficulty, mode, KINDS_ALL)]
            short = [row for row, need in F_NEED.items() if len(chosen(runs, *row, KINDS_ALL)) < need]
            value = sum(violations(run["shot_log"]) for run in games) if games else None
            detail = f"{len(games)} games" + (f"; short: {', '.join('/'.join(row) for row in short)}" if short else "")
            entry = verdict(target, value, len(games), target.games, detail=detail, hard=True)
            if short and entry["result"] == "PASS":  # every row needs its own games, not just as many in all
                entry["result"] = f"info (n < {target.games})"
            out.append(entry)
            continue
        if metric in ("bomb_kills", "uptime"):
            parts = []
            for bot in target.bots:
                games = chosen(runs, bot, target.difficulty, target.mode)
                if metric == "bomb_kills":
                    num, den = sum(run["bomb_kills"] for run in games), sum(run["kills"] for run in games)
                else:
                    num, den = sum(run["powered_ticks"] for run in games), sum(run["playing_ticks"] for run in games)
                parts.append((bot, num / den if den else None, len(games)))
            results = [verdict(target, value, n, target.games)["result"] for _bot, value, n in parts]
            worst = max((value for _bot, value, _n in parts if value is not None), default=None)
            entry = verdict(target, worst, min(n for _bot, _value, n in parts), target.games)
            entry["result"] = worst_result(results)
            entry["detail"] = "; ".join(
                f"{bot} {'-' if value is None else f'{value:.1%}'} ({n} games)" for bot, value, n in parts
            )
            out.append(entry)
            continue
        if metric == "unavoidable":
            out.append(unavoidable(target, runs))
            continue
        bot = target.bots[0]
        kinds = ("game", "until") if metric == "wave1" else ("game",)
        games = chosen(runs, bot, target.difficulty, target.mode, kinds)
        n = len(games)
        if metric in ("wins", "clean_wins", "wave1"):
            if metric == "wins":
                hits = sum(run["victory"] for run in games)
            elif metric == "clean_wins":
                hits = sum(run["victory"] and not run.get("continues_used") for run in games)
            else:
                hits = sum(run["cleared"] >= 1 for run in games)
            value = hits / n if n else None
            out.append(verdict(target, value, n, target.games, wilson(hits, n) if n else None))
        elif metric == "winners_minutes":
            winners = [run["minutes"] for run in games if run["victory"]]
            value = statistics.median(winners) if winners else None
            out.append(verdict(target, value, len(winners), target.games, detail=f"{len(winners)} wins of {n}"))
        elif metric == "median_wave":
            value = statistics.median(run["wave"] for run in games) if games else None
            out.append(verdict(target, value, n, target.games))
        elif metric == "wave_lengths":
            out.append(wave_lengths(target, games, chosen(runs, "sk_d18_p85", target.difficulty, target.mode)))
    return [entry if target.judged else informational(entry) for target, entry in zip(TARGETS, out, strict=True)]


def informational(entry: dict) -> dict:
    """An informational row (spec §27.2): its value and interval as measured, the band it is expected in, and a
    result that is never PASS or FAIL, so --check never fails on it."""
    low, high = entry["band"]
    expected = f"expected {low:.0%}-{high:.0%}" if high <= 1.0 else f"expected {low:g}-{high:g}"
    if entry["value"] is not None and entry["n"] >= entry["need"]:
        expected += ": inside" if inside(entry["value"], entry["band"]) else ": outside"
    result = "info (no games)" if entry["value"] is None else "info (not judged)"
    detail = "; ".join(part for part in (entry["detail"], f"{expected}; {INFORMATIONAL}") if part)
    return entry | {"result": result, "detail": detail}


def unavoidable(target, runs) -> dict:
    """Q10: the audit's unavoidable hits over every life lost in Q1's and P's games (Q10_NEED). A game counts once
    every life it lost is audited (a game that lost none has nothing to audit); each row needs its own games. One
    unavoidable hit fails at any number of games."""
    games = [run for row in Q10_NEED for run in chosen(runs, *row)]
    lost = [entry for run in games for entry in run["lives_log"]]
    audited = [entry for entry in lost if "audit" in entry]
    unfair = [
        f"{bots.shown(run['bot'])} seed {run['seed']} wave {entry['wave']} tick {entry['tick']}: {entry['cause']}"
        for run in games
        for entry in run["lives_log"]
        if "audit" in entry and not entry["audit"]["avoided"]
    ]

    def complete(rows):
        return sum(all("audit" in entry for entry in run["lives_log"]) for run in rows)

    counts = {row: complete(chosen(runs, *row)) for row in Q10_NEED}
    short = [row for row, need in Q10_NEED.items() if counts[row] < need]
    value = len(unfair) if games and (audited or not lost) else None
    entry = verdict(target, value, sum(counts.values()), target.games, hard=True)
    per_bot = ", ".join(f"{bots.shown(row[0])} {counts[row]}" for row in Q10_NEED)
    entry["detail"] = f"{len(audited)} of {len(lost)} lives lost audited; games fully audited: {per_bot}" + (
        "; " + "; ".join(unfair[:10]) if unfair else ""
    )
    if value is None and lost:
        entry["result"] = "info (not audited)"
    elif short and entry["result"] == "PASS":  # each row needs its own games, not just 600 in all
        entry["result"] = f"info (n < {target.games})"
    return entry


KINDS_ALL = ("game", "until")


def worst_result(results: list[str]) -> str:
    """FAIL if any fails, PASS if all pass, else the first info."""
    if "FAIL" in results:
        return "FAIL"
    return next((result for result in results if result != "PASS"), "PASS")


def wave_lengths(target, games, good) -> dict:
    """Q6: every regular wave's median playing time in its band and every boss wave's in the boss band, each with
    at least `games` clears; the good bot's medians beside them (reported, not judged: §24 open question 10)."""

    def medians(runs):
        clears = {}
        for run in runs:
            for wave, played in run.get("wave_play", {}).items():
                clears.setdefault(int(wave), []).append(played / TICK_RATE)
        return {wave: (len(seconds), statistics.median(seconds)) for wave, seconds in clears.items()}

    strong, weaker = medians(games), medians(good)
    results, cells = [], []
    for wave in range(1, CAMPAIGN_WAVES + 1):
        band = target.boss if WAVES[wave - 1].boss else target.band
        count, median = strong.get(wave, (0, None))
        if median is None or count < target.games:
            results.append("info")
        else:
            results.append("PASS" if inside(median, band) else "FAIL")
        good_median = weaker.get(wave, (0, None))[1]
        cells.append(
            f"{wave}: {'-' if median is None else f'{median:.0f}'}"
            f"{'' if good_median is None else f' ({good_median:.0f})'}{'!' if results[-1] == 'FAIL' else ''}"
        )
    worst = [strong[wave][1] for wave in strong if wave <= CAMPAIGN_WAVES]
    fewest = min((strong.get(wave, (0, None))[0] for wave in range(1, CAMPAIGN_WAVES + 1)), default=0)
    entry = verdict(target, max(worst) if worst else None, fewest, target.games)
    if not worst:
        entry["result"] = "info (no games)"
    elif "FAIL" in results:
        entry["result"] = "FAIL"
    elif all(result == "PASS" for result in results):
        entry["result"] = "PASS"
    else:
        entry["result"] = f"info (n < {target.games} clears of a wave)"
    entry["detail"] = "median s per wave, the good bot's in brackets: " + ", ".join(cells)
    entry["band"] = [*target.band, *target.boss]
    return entry


def format_targets(results: list[dict]) -> str:
    lines = ["The quality bar (spec §19.1, §23.3), judged with at least the games shown:"]
    lines.append(f"{'row':<10} {'what':<34} {'value':>8} {'95% interval':>15} {'n':>6} {'need':>5}  result")
    for result in results:
        value, rate = result["value"], result["band"][1] <= 1.0 and result["row"] not in ("Q10", "F")
        text = (
            "-"
            if value is None
            else f"{value:.1%}"
            if rate
            else f"{value:g}"
            if value == int(value)
            else f"{value:.1f}"
        )
        interval = result["interval"]
        span = f"{interval[0]:.1%}-{interval[1]:.1%}" if interval else ""
        lines.append(
            f"{result['row']:<10} {result['what']:<34} {text:>8} {span:>15} {result['n']:>6} {result['need']:>5}  "
            f"{result['result']}"
        )
        if result["detail"]:
            lines.append(f"{'':<11}{result['detail']}")
    return "\n".join(lines)


# --- the lives budget (spec §18: --merge prints each act's deaths, bombs and no-escape events against it) --------


# The middle of each judged win-rate band, by bot and difficulty. §27.2: Q1 is the strong bot's on Hard; its Normal
# games and the good bot's on Hard are informational, so they have no budget.
CENTRES = {("strong", "hard"): 0.675, ("sk_d18_p85", "normal"): 0.375, ("sk_d18_p85", "easy"): 0.775}


def budget_rows(runs: list[dict]) -> list[dict]:
    """Per bot and difficulty of whole classic games (direct): each act's deaths, bombs and no-escape moments per
    game that reached it; its fatal events (lives lost, plus bombs fired in a no-escape moment that cost no life);
    the share b of fatal events seen in time; and what the lives model (tools/bots/lives.py) allows for the row's
    target win rate (CENTRES), with no bombs (deaths) and with bombs absorbing (fatal events at the measured b)."""
    groups = {}
    for run in runs:
        if run_kind(run) == "game" and run["mode"] == "classic" and run["input"] == "direct":
            groups.setdefault((run["bot"], run["difficulty"]), []).append(run)
    out = []
    for (bot, difficulty), group in groups.items():
        reached = [sum(run["acts"]["minutes"][act] > 0 for run in group) for act in range(ACTS)]

        def per_game(values, reached=reached):
            return [round(value / count, 3) if count else 0.0 for value, count in zip(values, reached, strict=True)]

        deaths = [sum(run["acts"]["deaths"][act] for run in group) for act in range(ACTS)]
        bombs = [sum(run["acts"]["bombs"][act] for run in group) for act in range(ACTS)]
        moments = [moment for run in group for moment in run["no_escape_log"]]
        escape = [sum(moment["act"] == act + 1 for moment in moments) for act in range(ACTS)]
        absorbed = [
            sum(moment["act"] == act + 1 and moment["bombed"] and not moment["hit"] for moment in moments)
            for act in range(ACTS)
        ]
        fatal_total = sum(deaths) + sum(absorbed)
        seen = sum(entry["seen"] for run in group for entry in run["lives_log"]) + sum(absorbed)
        b = seen / fatal_total if fatal_total else 0.0
        fatal = per_game([d + a for d, a in zip(deaths, absorbed, strict=True)])
        rules = lives_model.rules(difficulty, bombs=bot != "nobomb")
        target = CENTRES.get((bot, difficulty))
        row = {
            "bot": bot,
            "difficulty": difficulty,
            "games": len(group),
            "reached": reached,
            "deaths": per_game(deaths),
            "bombs": per_game(bombs),
            "no_escape": per_game(escape),
            "fatal": fatal,
            "b": round(b, 3),
            "target": target,
            "win_rate": sum(run["victory"] for run in group) / len(group),
            "model_win": round(lives_model.predict(fatal, b, **rules), 3),
        }
        if target is not None:
            plain = lives_model.rules(difficulty, bombs=False)
            row["deaths_budget"] = [round(value, 4) for value in lives_model.budget(target, 0.0, **plain)]
            row["fatal_budget"] = [round(value, 4) for value in lives_model.budget(target, b, **rules)]
        out.append(row)
    return out


def format_budget(rows: list[dict]) -> str:
    lines = [
        "Lives budget (tools/bots/lives.py, bombs as absorbers), per game that reached each act, acts I-IV:",
        f"{'bot':<12} {'difficulty':<10} {'what':<12} {'act I':>7} {'II':>7} {'III':>7} {'IV':>7}",
    ]
    for row in rows:
        head = f"{bots.shown(row['bot']):<12} {row['difficulty']:<10}"

        def cells(values):
            return "".join(f" {value:>7.2f}" for value in values)

        lines.append(f"{head} {'reached':<12}" + "".join(f" {value:>7d}" for value in row["reached"]))
        lines.append(f"{head} {'deaths':<12}" + cells(row["deaths"]))
        if "deaths_budget" in row:
            lines.append(
                f"{head} {'  budget':<12}" + cells(row["deaths_budget"]) + f"  (no bombs, {row['target']:.1%})"
            )
        lines.append(f"{head} {'bombs':<12}" + cells(row["bombs"]))
        lines.append(f"{head} {'no-escape':<12}" + cells(row["no_escape"]))
        lines.append(f"{head} {'fatal':<12}" + cells(row["fatal"]) + f"  (b {row['b']:.2f})")
        if "fatal_budget" in row:
            lines.append(
                f"{head} {'  budget':<12}" + cells(row["fatal_budget"]) + f"  (b {row['b']:.2f}, {row['target']:.1%})"
            )
        lines.append(
            f"{head} wins {row['win_rate']:.1%}; the model gives {row['model_win']:.1%} for these fatal events"
        )
    return "\n".join(lines)


def format_profile(rows: list[dict]) -> str:
    """--profile: the split per bot (spec §2.3 and §23.7: the bot <= 60 µs a tick with observe, observe <= 20)."""
    lines = [
        "Cost (µs a tick, mean over the games; budget: bot <= 60 with observe, observe <= 20; judged on a quiet "
        "machine):",
        f"{'bot':<12} {'difficulty':<10} {'input':<11} {'observe':>8} {'bot':>8} {'bot+obs':>8} {'engine':>8} "
        f"{'harness':>8} {'ticks/s':>8}",
    ]
    for row in rows:
        cost = row["profile"]
        if not cost:
            continue
        flags = []
        if cost["bot_observe"] > 60:
            flags.append("bot over 60")
        if cost["observe"] > 20:
            flags.append("observe over 20")
        lines.append(
            f"{label(row)} {cost['observe']:>8.1f} {cost['bot']:>8.1f} {cost['bot_observe']:>8.1f} "
            f"{cost['engine']:>8.1f} {cost['harness']:>8.1f} {cost['ticks_per_s']:>8,}"
            + (f"  ({', '.join(flags)})" if flags else "")
        )
    return "\n".join(lines)


def format_audit(runs: list[dict]) -> str:
    """The fairness audit over every game it reached (spec §18, §27.2): lives lost audited, the unavoidable ones
    (cause, seed, tick), the ones a bomb, two plans in a row or (for a landing) fire held were needed for, and the
    sequences flown (its cost)."""
    lost = [(run, entry) for run in runs for entry in run["lives_log"]]
    audited = [(run, entry) for run, entry in lost if "audit" in entry]
    unfair = [(run, entry) for run, entry in audited if not entry["audit"]["avoided"]]
    verdicts = [entry["audit"] for _run, entry in audited]
    held = sum(bool(verdict.get("held")) for verdict in verdicts)  # each count below leaves out the others
    bombed = sum(verdict["bomb"] is not None and not verdict.get("held") for verdict in verdicts)
    paired = sum(
        verdict.get("switch") is not None and verdict["bomb"] is None and not verdict.get("held")
        for verdict in verdicts
    )
    flights = sum(verdict.get("flights", 0) for verdict in verdicts)
    lines = [
        f"fairness audit: {len(audited)} of {len(lost)} lives lost audited, {len(unfair)} unavoidable, {bombed} "
        f"avoided only with a bomb, {paired} only with two plans in a row, {held} landing{'' if held == 1 else 's'} "
        f"only with fire held; {flights:,} sequences flown"
    ]
    for run, entry in unfair:
        note = (
            "a plan dodged it and was hit later, standing; no two plans avoided it"
            if entry["audit"]["dodged"]
            else "every plan and every two plans in a row hit"
        )
        if entry["cause"] == "landing" and "held" in entry["audit"]:  # an audit since fire held was tried (step 5)
            note += ", with fire as recorded and with fire held"
        lines.append(
            f"  {bots.shown(run['bot'])} {run['difficulty']} seed {run['seed']} wave {entry['wave']} tick "
            f"{entry['tick']}: {entry['cause']} {entry['class']}, {entry['bombs']} bombs ({note})"
        )
    return "\n".join(lines)


def report(runs: list[dict], json_path, settings: dict) -> list[dict]:
    """Print the report of these games (the sweep's when they are all staged waves) and write --json. Returns the
    failed checks' results: the acceptance's, the quality bar's and the stalls'."""
    print()
    sweep = all(run_kind(run) == "wave" for run in runs)
    rows = sweep_rows(runs) if sweep else summarise(runs)
    print(format_sweep(rows) if sweep else format_report(rows))
    stalled = stalls(runs)
    print()
    print(format_stalls(runs, stalled))
    shots = shot_log(runs)
    print()
    print(format_shot_log(shots))
    if not sweep and any("audit" in entry for run in runs for entry in run["lives_log"]):
        print(format_audit(runs))
    accepted, targets, budget = [], [], []
    if not sweep:
        notes = format_notes(rows)
        if notes:
            print()
            print("\n".join(notes))
        budget = budget_rows(runs)
        if budget:
            print()
            print(format_budget(budget))
        if any(run_kind(run) in KINDS_ALL and run["input"] == "direct" for run in runs):
            targets = judge_targets(runs)
            print()
            print(format_targets(targets))
        accepted = acceptance(matched_rows(runs))
        if accepted:
            print()
            print(format_acceptance(accepted))
        if any(row["profile"] for row in rows):
            print()
            print(format_profile(rows))
    if json_path:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        light = [{key: value for key, value in run.items() if key != "tape"} for run in runs]
        data = {"ruleset": RULESET, "python": platform.python_version(), **settings, "summary": rows, "runs": light}
        data |= {"shot_log": shots, "targets": targets, "budget": budget, "stalls": stalled}
        json_path.write_text(json.dumps(data | {"acceptance": accepted}) + "\n", encoding="utf-8")
    return [result for result in (*accepted, *targets, *stalled) if result["result"] == "FAIL"]


# --- the feasibility sweep (spec §22.3 step 4, milestone B-F; `make balance-sweep`) ---------------------------------


def poisson_interval(count: int) -> tuple[float, float]:
    """A 95% interval for a Poisson count (Byar's approximation)."""
    low = 0.0 if count == 0 else count * (1 - 1 / (9 * count) - 1.96 / (3 * math.sqrt(count))) ** 3
    high = (count + 1) * (1 - 1 / (9 * (count + 1)) + 1.96 / (3 * math.sqrt(count + 1))) ** 3
    return low, high


def sweep_rows(runs: list[dict]) -> list[dict]:
    """One row per bot and density point, in the order the runs came: minutes of play, enemy shots a minute and in
    flight, and per minute the lives lost, no-escape moments, bombs and events (lives lost plus the no-escape
    moments that cost none: §19.1's fatal events, bombs absorbing them); b, the share of fatal events the bot saw
    coming; and the audit of every life lost."""
    groups = {}
    for run in runs:
        director = run["staged"]["director"]
        groups.setdefault((run["bot"], director["slots"], director["reload"]), []).append(run)
    rows = []
    for (bot, slots, reload), group in groups.items():
        ticks = sum(run["playing_ticks"] for run in group)
        minutes = ticks / TICKS_PER_MINUTE
        lives = [
            entry | {"seed": run["seed"], "wave": run["staged"]["wave"]} for run in group for entry in run["lives_log"]
        ]
        moments = [moment for run in group for moment in run["no_escape_log"]]
        spared = [moment for moment in moments if not moment["hit"]]
        absorbed = sum(moment["bombed"] for moment in spared)
        deaths = len(lives)
        fatal = deaths + absorbed  # a bomb fired in a no-escape moment is a death it didn't have
        seen = sum(entry["seen"] for entry in lives) + absorbed
        audited = [entry["audit"] for entry in lives if "audit" in entry]
        unfair = [entry for entry in lives if "audit" in entry and not entry["audit"]["avoided"]]
        per_wave = Counter(entry["wave"] for entry in lives)
        rows.append(
            {
                "bot": bot,
                "slots": slots,
                "reload": reload,
                "games": len(group),
                "minutes": minutes,
                "shots_per_minute": sum(sum(run["acts"]["shots"]) for run in group) / max(minutes, 1e-9),
                "in_flight": sum(sum(run["acts"]["in_flight"]) for run in group) / max(ticks, 1),
                "deaths": deaths,
                "no_escape": len(moments),
                "bombs": sum(run["bombs_used"] for run in group),
                "events": deaths + len(spared),
                "absorbed": absorbed,
                "false_alarms": len(spared) - absorbed,
                "b": seen / fatal if fatal else None,
                "late": sum(entry["late"] for entry in lives),
                "with_bomb": sum(entry["bombs"] > 0 for entry in lives),
                "shield_hits": sum(run["shield_hits"] for run in group),
                "causes": dict(Counter(f"{entry['cause']} {entry['class']}".strip() for entry in lives).most_common()),
                "per_wave": {str(wave): per_wave.get(wave, 0) for wave in SWEEP_WAVES},
                "audited": len(audited),
                "unavoidable": [
                    {key: entry[key] for key in ("seed", "wave", "tick", "cause", "class", "bombs")}
                    | {"dodged": entry["audit"]["dodged"]}
                    for entry in unfair
                ],
                "bombed_avoid": sum(verdict["bomb"] is not None for verdict in audited),
            }
        )
    return rows


def format_sweep(rows: list[dict]) -> str:
    """The sweep's table, its lives lost by cause and wave, and every hit the audit found unavoidable."""

    def rate(count, minutes):
        return f"{count / minutes:.3f}" if minutes else "-"

    head = (
        f"{'bot':<12} {'slots/reload':>12} {'games':>5} {'minutes':>7} {'shots/min':>9} {'in flight':>9} "
        f"{'lost':>4} {'lost/min (95%)':>21} {'no-esc/min':>10} {'bombs/min':>9} {'events/min':>10} {'b':>5} "
        f"{'late':>4} {'unavoidable':>11}"
    )
    lines = [head]
    for row in rows:
        minutes = row["minutes"]
        low, high = poisson_interval(row["deaths"])
        interval = f"{rate(row['deaths'], minutes)} ({low / minutes:.3f}-{high / minutes:.3f})" if minutes else "-"
        b = "-" if row["b"] is None else f"{row['b']:.2f}"
        lines.append(
            f"{bots.shown(row['bot']):<12} {row['slots']:>5}/{row['reload']:<6} {row['games']:>5} {minutes:>7.1f} "
            f"{row['shots_per_minute']:>9.1f} {row['in_flight']:>9.2f} {row['deaths']:>4} {interval:>21} "
            f"{rate(row['no_escape'], minutes):>10} {rate(row['bombs'], minutes):>9} "
            f"{rate(row['events'], minutes):>10} {b:>5} {row['late']:>4} "
            f"{len(row['unavoidable']):>4} of {row['audited']:<4}"
        )
    budget = ", ".join(f"act {act} {value:.2f}" for act, value in BUDGET.items())
    lines.append(
        f"The budget (spec §19.1, the strong player on Normal), lives lost a minute: {budget}; about 0.10-0.35 "
        "counting the "
        "no-escape moments (events)."
    )
    lines.append(
        "events: lives lost plus the no-escape moments that cost none; b: the share of the fatal events (lives "
        "lost, and bombs fired in a no-escape moment) that came after a no-escape moment; late: lives lost to a shot "
        "noticed late; unavoidable: lives lost that no plan of the audit avoids, of those audited."
    )
    lines += [
        "",
        "lives lost by cause and class; per wave 5 / 8 / 10; with a bomb in stock; absorbed and false alarms:",
    ]
    for row in rows:
        causes = ", ".join(f"{cause or '?'} {count}" for cause, count in row["causes"].items()) or "none"
        waves = " / ".join(str(row["per_wave"][str(wave)]) for wave in SWEEP_WAVES)
        lines.append(
            f"{bots.shown(row['bot']):<12} {row['slots']:>5}/{row['reload']:<6} {causes}; {waves}; {row['with_bomb']}; "
            f"{row['absorbed']} absorbed, {row['false_alarms']} false alarms"
        )
    unfair = [(row, entry) for row in rows for entry in row["unavoidable"]]
    lines += ["", f"unavoidable hits (spec §18's audit): {len(unfair)}"]
    for row, entry in unfair:
        note = "a plan dodged it and was hit later, standing" if entry["dodged"] else "every plan hit by then"
        lines.append(
            f"  {bots.shown(row['bot'])} {row['slots']}/{row['reload']} wave {entry['wave']} seed {entry['seed']} tick "
            f"{entry['tick']}: {entry['cause']} {entry['class']}, {entry['bombs']} bombs ({note})"
        )
    return "\n".join(lines)


# --- the drift test (spec §18 "Margins"; tests/test_bots.py) --------------------------------------------------------


def drift(
    wave: int,
    ticks: int,
    *,
    bot="skilled",
    seed=0,
    difficulty="normal",
    horizons=(10, 20, 30, 45),
    boss_phase=0,
    upgrades=(),
    director=None,
) -> dict:
    """The bot's predictions against the engine, in an immortal staged game: at every decision, for every track
    the bot holds, where it predicts the thing h ticks after its last sighting, and where the engine has it then.

    Returns {category: {"mature": {h: [error, ...]}, "young": [inside, ...]}}. An error is max(|dx|, |dy|) in
    cells, for tracks spanning YOUNG ticks or more; a young track records whether the truth lay inside its cone
    widened by the margin m0 + m1 x t the planner adds. Categories: the shot kinds ("boss <kind>" for a boss's),
    "cluster" (its three pellets after the split, against the split's cone), "dive" and "mite". A prediction across
    a stop the bot cannot see is not scored: the immortal ship's hit freeze, a boss's phase break or dying."""
    game = staged(
        Config(difficulty, "classic", seed), wave, immortal=True, boss_phase=boss_phase, upgrades=upgrades,
        director=director,
    )  # fmt: skip
    pilot = bots.make(bot, seed)
    sense = pilot.sense
    out = {}
    pending = {}  # tick -> [prediction, ...]
    classes = {}  # threat id -> its Fired class
    clusters = {}  # cluster id -> its last engine position, while it flies
    children = {}  # cluster id -> the ids of the pellets it split into, in split order
    decisions = pilot.decisions
    split = SHOTS["cluster"]
    # The last tick the enemies stood still where the bot cannot see it: the immortal ship's hit freeze (its lives
    # never drop) and a boss's phase break or dying (the fleet waits; enemy shots were cleared).
    freeze_end = last_stop = -(10**9)
    for _ in range(ticks):
        if game.phase in TERMINAL:
            break
        frame = observe(game)
        pilot.act(frame)  # the bot's clock now reaches this tick: score what comes due at it
        due = pending.pop(game.tick, ())
        if due and last_stop >= game.tick - max(horizons) - 1:
            due = [entry for entry in due if last_stop < game.tick - entry[4] - 1]
        if due:
            measure(out, due, game, sense, children)
        if pilot.decisions != decisions:
            decisions = pilot.decisions
            for table, dive in ((sense.shots, False), (sense.dives, True)):
                for ident, track in table.items():
                    if track.t1 != sense.seen.tick:
                        continue
                    kind = track.kind
                    if dive:
                        category = "mite" if kind == "mite" else "dive"
                    else:
                        category = f"boss {kind}" if classes.get(ident) == "boss" else kind
                    cone = bot_predict.cone(track, kind, frame.wave, difficulty, track.ya)
                    span = track.w1 - track.w0
                    mature = span >= bot_params.YOUNG and track.n >= 2
                    for h in horizons:
                        prediction = (category, ident, cone, track.w1, h, mature, kind == "cluster")
                        pending.setdefault(track.t1 + h, []).append(prediction)
        game.step(pilot.last)
        if "damage" in game.events:
            freeze_end = game.tick + TIMING.freeze
        if game.tick <= freeze_end or (game.boss is not None and game.boss.state in ("break", "dying")):
            last_stop = game.tick
        for entry in game.fired:
            classes[entry.id] = entry.cls
        now = {shot.id: shot for shot in game.shots if not shot.friendly}
        for ident in [i for i in clusters if i not in now]:
            x, y = clusters.pop(ident)
            new = [i for i, s in now.items() if s.kind == "pellet" and i > ident and abs(s.x - x) < 0.6]
            new = sorted(i for i in new if 0 <= now[i].y - y < 0.6)
            if len(new) == len(split.split):
                children[ident] = new
        for ident, shot in now.items():
            if shot.kind == "cluster":
                clusters[ident] = (shot.x, shot.y)
    return out


def measure(out, due, game, sense, children) -> None:
    """Score the predictions that come due at this tick against the engine."""
    split = SHOTS["cluster"]
    w_now = sense.clock_at(game.tick)
    now = {shot.id: shot for shot in game.shots if not shot.friendly}
    aliens = {alien.id: alien for alien in game.aliens}
    for category, ident, (x, y, vxl, vxh, vyl, vyh), w_last, h, mature, is_cluster in due:
        dw = w_now - w_last
        truths = []  # (true x, true y, predicted x at the sighting, vx low, vx high, x spread)
        if is_cluster:
            if ident not in children or vyh <= 0:  # the planner skips a cluster it sees not falling (predict.build)
                continue
            early, late = (split.split_y - y) / vyh, (split.split_y - y) / vyl
            mid, half = (early + late) / 2, (late - early) / 2
            for pellet, svx in zip(children[ident], split.split, strict=True):
                shot = now.get(pellet)
                if shot is not None:
                    truths.append((shot.x, shot.y, x - svx * mid, vxl + svx, vxh + svx, abs(svx) * half))
        elif category in ("dive", "mite"):
            alien = aliens.get(ident)
            if alien is not None and alien.state == "diving":
                truths.append((alien.x, alien.y, x, vxl, vxh, 0.0))
        else:
            shot = now.get(ident)
            if shot is not None:
                truths.append((shot.x, shot.y, x, vxl, vxh, 0.0))
        record = out.setdefault(category, {"mature": {}, "young": []})
        m0, m1 = bot_predict.margin(category.split()[-1], not mature)
        m = m0 + m1 * dw  # the margin the planner adds this far ahead
        for tx, ty, x0, lo, hi, spread in truths:
            if mature:
                px, py = x0 + (lo + hi) / 2 * dw, y + (vyl + vyh) / 2 * dw
                record["mature"].setdefault(h, []).append(max(abs(tx - px), abs(ty - py)))
            else:
                x_lo = x0 - spread + min(lo * dw, hi * dw) - m
                x_hi = x0 + spread + max(lo * dw, hi * dw) + m
                record["young"].append(x_lo <= tx <= x_hi and y + vyl * dw - m <= ty <= y + vyh * dw + m)


def percentile(values, share: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


# --- --plan: shard counts from a profile run (spec §19.2, §23.3) ------------------------------------------------------

# --plan profiles PROFILE_SEEDS whole classic games of each row's bot on the row's own difficulty, so a row is sized
# from its own games: its ticks a second, its length and, for a row that keeps its tapes, its audit. Spec §27.2 puts
# Q1's taped games on Hard, which Normal's games can't size (Q1's band asks the strong bot to lose about 4.5 lives a
# run, against the 0.86 it lost on Normal in round 3), and the factors per difficulty that came before (MB 9.6's: the
# good bot 16.5 minutes on Hard against 27 on Normal) planned Hard's games at half their length (round 3's median:
# 35.9 minutes). An Endless game is planned at its row's cap: it runs until the ship falls, and the strong bot can
# last to the cap (on Normal, the untuned tables' baseline: 96 of 100 games to 180 minutes; on Hard, where Q7 plays
# since §27.2, 3 of 16 in tuning round 5). --lengths DIR plans every row from the measured mean length of its games in
# DIR instead (an earlier run of the tier).
KEYED_PROBE = (2, 6000)  # --plan measures each keyed input's extra cost on this many seeds x ticks


def profile_key(planned) -> tuple:
    """The profiled games a planned game (or a profiled run) is sized from: (bot, difficulty, until), its bot's whole
    classic games on its difficulty, or stopped at wave `until` for a row stopped there."""
    return planned["bot"], planned.get("difficulty", "normal"), planned.get("until")


def plan_key(planned) -> tuple:
    """A planned game's row for --plan: (bot, difficulty, mode, input, until)."""
    return (
        planned["bot"],
        planned.get("difficulty", "normal"),
        planned.get("mode", "classic"),
        planned.get("input_mode", "direct"),
        planned.get("until"),
    )


def measured_lengths(runs: list[dict]) -> dict:
    """--lengths: the mean ticks of each row's whole games (and games stopped at a wave), by plan_key."""
    totals = {}
    for run in runs:
        if run_kind(run) not in KINDS_ALL:
            continue
        key = (run["bot"], run["difficulty"], run["mode"], run["input"], run.get("until"))
        entry = totals.setdefault(key, [0, 0])
        entry[0] += run["ticks"]
        entry[1] += 1
    return {key: ticks / games for key, (ticks, games) in totals.items()}


def plan_tier(
    jobs: list,
    workers: int,
    profiled: list[dict] | None = None,
    probe: dict | None = None,
    audits: dict | None = None,
    lengths: dict | None = None,
) -> dict:
    """The shard count N that keeps every shard of these games within SHARD_MINUTES on `workers` processes, and
    the audit's shard count for the games that keep their tapes (gate row Q10: Q1's and P's).

    profiled: PROFILE_SEEDS profiled games (12) of each bot and difficulty the tier plays (profile_key), whole classic
    games on that difficulty (wave 1 only for a row the tier stops at wave 1), with tapes for the rows whose games
    keep them; they give each row's ticks a second and its game length (an Endless row: its bot's on its difficulty,
    in classic). probe: each keyed input's extra µs a tick. audits: per taped profile_key, its profiled games and the
    seconds the audit took over those that lost a life (audit_cost). Each is measured here when not given. lengths:
    measured mean ticks by plan_key (measured_lengths); a row they hold is planned at that length (up to its cap),
    the others at the profiled length (a staged act: a quarter of it) and, in Endless, at the cap."""
    direct = {profile_key(planned) for planned in jobs}
    taped = {profile_key(planned) for planned in jobs if planned.get("tape")}
    if profiled is None:
        probes = [
            job(bot, difficulty, "classic", seed, profile=True, **({"until": until} if until is not None else {}))
            | ({"tape": True} if (bot, difficulty, until) in taped and until is None else {})
            for bot, difficulty, until in sorted(direct, key=str)
            for seed in range(1000, 1000 + PROFILE_SEEDS)
        ]
        profiled = run_all(probes, workers)
        if audits is None:
            audits = audit_cost([run for run in profiled if profile_key(run) in taped], workers)  # until is None
    keyed = sorted({planned.get("input_mode", "direct") for planned in jobs} - {"direct"})
    if probe is None:
        probe = keyed_cost(keyed, workers) if keyed else {}
    measured = {}
    for run in profiled:
        entry = measured.setdefault(profile_key(run), {"ticks": 0, "steps": 0, "wall": 0.0, "games": 0})
        cost = run["profile"]
        entry["ticks"] += run["ticks"]
        entry["steps"] += cost["steps"]
        entry["wall"] += sum(cost[key] for key in ("observe", "bot", "engine", "harness"))
        entry["games"] += 1
    rows = {}
    audit = {"games": 0, "taped": 0.0, "cpu_seconds": 0.0}
    for planned in jobs:
        key = plan_key(planned)
        bot, difficulty, mode, inputs, until = key
        base = measured.get(profile_key(planned))
        if base is None:
            raise ValueError(f"no profiled games of {bot} on {difficulty}")
        cap = planned.get("max_ticks", DEFAULT_TICKS)
        length = base["ticks"] / base["games"]
        if lengths and key in lengths and planned.get("act") is None:
            length = lengths[key]
        elif mode == "endless":
            length = cap  # it runs until the ship falls: the strong bot can last to the cap
        elif planned.get("act") is not None:
            length /= ACTS
        length = min(length, cap)
        rate = base["steps"] / base["wall"]
        if inputs != "direct":
            rate = 1 / (1 / rate + probe[inputs] / 1e6)
        row = rows.setdefault(key, {"games": 0, "ticks": length, "rate": rate})
        row["games"] += 1
        if planned.get("tape") and audits and profile_key(planned) in audits:
            spent = audits[profile_key(planned)]
            share = length / (base["ticks"] / base["games"])  # the audit replays the whole game
            audit["games"] += 1
            audit["taped"] += spent["taped"] / spent["games"]
            audit["cpu_seconds"] += spent["seconds"] / spent["games"] * share
    seconds = sum(row["games"] * row["ticks"] / row["rate"] for row in rows.values())
    shards = max(1, math.ceil(seconds / (workers * SHARD_MINUTES * 60)))
    plan = {"rows": rows, "cpu_seconds": seconds, "shards": shards, "workers": workers, "probe": probe}
    if audit["games"]:
        audit["shards"] = max(1, math.ceil(audit["cpu_seconds"] / (workers * SHARD_MINUTES * 60)))
        plan["audit"] = audit
    return plan


def _audit_seconds(run) -> tuple[tuple, float]:
    """A profiled run's audit, timed: (its profile_key, the seconds spec §18's audit of its lives lost took)."""
    started = time.perf_counter()
    hits = [entry["tick"] for entry in run["lives_log"]]
    audit_hits(run_builder(run), decode_tape(run["tape"]), hits, bot_cadence(run["bot"]))
    return profile_key(run), time.perf_counter() - started


def audit_cost(profiled: list[dict], workers: int) -> dict:
    """Per bot and difficulty of these profiled games (profile_key): {"games", "taped" (the games that lost a life
    and kept a tape), "seconds" (the audit of those, timed)}, which --plan scales to the tier's taped games (gate
    row Q10)."""
    out = {}
    for run in profiled:
        entry = out.setdefault(profile_key(run), {"games": 0, "taped": 0, "seconds": 0.0})
        entry["games"] += 1
        entry["taped"] += bool(run.get("tape"))
    taped = [run for run in profiled if run.get("tape")]
    if taped:
        workers = max(1, min(workers, len(taped)))
        pool = multiprocessing.Pool(workers) if workers > 1 else None
        try:
            for key, seconds in pool.imap_unordered(_audit_seconds, taped) if pool else map(_audit_seconds, taped):
                out[key]["seconds"] += seconds
        finally:
            if pool is not None:
                pool.close()
                pool.join()
    return out


def keyed_cost(inputs: list[str], workers: int) -> dict:
    """Each keyed input's extra µs a tick over direct play, on the same short games."""
    seeds, ticks = KEYED_PROBE
    planned = [
        job(I1_BOT, "normal", "classic", 1000 + seed, max_ticks=ticks, input_mode=name, profile=True)
        for name in ("direct", *inputs)
        for seed in range(seeds)
    ]
    runs = run_all(planned, workers)
    per = {}
    for run in runs:
        cost = run["profile"]
        entry = per.setdefault(run["input"], [0.0, 0])
        entry[0] += sum(cost[key] for key in ("observe", "bot", "engine", "harness"))
        entry[1] += cost["steps"]
    direct = per["direct"][0] / per["direct"][1]
    return {name: max(0.0, (per[name][0] / per[name][1] - direct) * 1e6) for name in inputs}


def format_plan(plan: dict, flag: str) -> str:
    lines = [f"{'bot':<12} {'difficulty':<10} {'mode':<8} {'input':<11} {'games':>6} {'min/game':>9} {'ticks/s':>8}"]
    for (bot, difficulty, mode, inputs, until), row in plan["rows"].items():
        name = bots.shown(bot) if until is None else f"{bots.shown(bot)} (to wave {until})"
        lines.append(
            f"{name:<12} {difficulty:<10} {mode:<8} {inputs:<11} {row['games']:>6} "
            f"{row['ticks'] / TICKS_PER_MINUTE:>9.1f} {row['rate']:>8,.0f}"
        )
    hours = plan["cpu_seconds"] / 3600
    lines.append(
        f"{hours:.1f} CPU-hours; at most {SHARD_MINUTES} minutes a shard on {plan['workers']} processes: "
        f"N = {plan['shards']}"
    )
    lines.append(
        f"run: tools/balance.py {flag} --shard K/{plan['shards']} --jobs {plan['workers']} --out DIR, K = 1..N"
    )
    audit = plan.get("audit")
    if audit:
        lines.append(
            f"audit (gate row Q10): about {audit['taped']:.0f} of the {audit['games']} taped games lose a life and "
            f"keep their tape, {audit['cpu_seconds'] / 3600:.1f} CPU-hours: N = {audit['shards']}"
        )
        lines.append(
            f"then: tools/balance.py --audit DIR --shard K/{audit['shards']} --jobs {plan['workers']}, K = 1..N"
        )
    return "\n".join(lines)


# --- the command line ------------------------------------------------------------------------------------------------


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


def act_number(value: str) -> int:
    number = counted(int)(value)
    if number > ACTS:
        raise argparse.ArgumentTypeError(f"there are {ACTS} acts")
    return number


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Bot playtests: win rates, pacing and economy per bot and difficulty, and the quality bar's "
        "tiers (spec §19)."
    )
    command.add_argument(
        "--bots",
        nargs="+",
        metavar="BOT",
        help=f"all, {', '.join(BOTS)}, perfect (skilled's name in reports), any sk_d<reaction ticks>_p<%% of shots "
        f"noticed> (strong is sk_d12_p92), or fixed:<upgrade code> (default: {' '.join(QUICK)}; {I1_BOT} with "
        "--gate-input)",
    )
    command.add_argument("--difficulty", nargs="+", choices=(*DIFFICULTIES, "all"), default=["normal"])
    command.add_argument("--mode", choices=("classic", "endless"), default="classic")
    command.add_argument(
        "--input",
        nargs="+",
        choices=(*INPUTS, "all"),
        help="how the bots' actions reach the game: direct to the engine, or keyed through the game's own input path "
        "(kitty: hold keys; latched or timed: a legacy terminal; latched-ssh: over an ssh link). Default direct; "
        "with --gate-input, every keyed input",
    )
    command.add_argument(
        "--picks", metavar="POLICY", help="every bot's pick policy: priority, first, random, fixed:CODE"
    )
    command.add_argument(
        "--seeds",
        type=counted(int),
        help=f"games per bot, difficulty and input (default 12; {STAGED_SEEDS} with --staged); with --sweep, per "
        f"wave, bot and point (default {SWEEP_SEEDS}); with --tune, --gate or --gate-input, per row instead of the "
        "row's own",
    )
    command.add_argument("--start-seed", type=int, default=1000, help="first game seed")
    command.add_argument("--max-minutes", type=counted(float), default=60.0, help="cap on simulated minutes per game")
    command.add_argument("--until", type=counted(int), metavar="WAVE", help="stop each game when this wave ends")
    command.add_argument("--jobs", type=counted(int), default=os.cpu_count() or 1, help="worker processes")
    command.add_argument("--json", type=Path, metavar="PATH", help="write every game's metrics as JSON")
    command.add_argument(
        "--check", action="store_true", help="exit 1 when the spec 11.5 acceptance or a quality-bar row fails"
    )
    command.add_argument("--profile", action="store_true", help="time observe, the bots, the engine and the harness")
    tiers = command.add_mutually_exclusive_group()
    tiers.add_argument(
        "--sweep",
        action="store_true",
        help="the feasibility sweep (spec 22.3 step 4) in place of --bots' games: staged immortal waves 5, 8 and 10 "
        "at six fire densities, every life lost audited",
    )
    tiers.add_argument(
        "--staged", type=act_number, metavar="ACT", help="start each game at the act's first wave with its loadout"
    )
    tiers.add_argument("--tune", action="store_true", help="the tune tier: the gate's rows on 100 tuning seeds each")
    tiers.add_argument("--gate", action="store_true", help="the gate (spec 23.3) on the held-out seeds, with tapes")
    tiers.add_argument(
        "--gate-input", action="store_true", help="the keyed-input check (spec 23.4, row I1) on seeds 5000-5199"
    )
    tiers.add_argument(
        "--merge", type=Path, nargs="+", metavar="DIR", help="report every game in these shard files (one build only)"
    )
    tiers.add_argument("--audit", type=Path, metavar="DIR", help="audit every taped game in DIR's shard files")
    command.add_argument("--immortal", action="store_true", help="with --staged: lives held at 99 (length tuning)")
    command.add_argument(
        "--director",
        type=director_pair,
        nargs="+",
        metavar="KEY=VALUE",
        help=f"with --staged: director overrides ({', '.join(DIRECTOR_KEYS)}), under every floor and cap",
    )
    command.add_argument(
        "--loadout", type=Path, metavar="DIR", help="with --staged: the median loadout of DIR's whole runs"
    )
    command.add_argument("--tapes", action="store_true", help="keep the action tape of every game that loses a life")
    command.add_argument(
        "--plan",
        action="store_true",
        help="with a tier: the shard count N from a 12-seed --profile run of each row's bot on its difficulty, "
        "then stop",
    )
    command.add_argument(
        "--lengths",
        type=Path,
        nargs="+",
        metavar="DIR",
        help="with --plan: each row's games last as long as its games in these shard files did on average (an "
        "earlier run of the tier), not the estimate (Endless: its cap)",
    )
    command.add_argument("--shard", type=shard, metavar="K/N", help="play only the K-th of N shares of the games")
    command.add_argument(
        "--out",
        type=Path,
        metavar="DIR",
        help="write each game to a shard file in DIR as it ends (one JSON line); games already there are kept if this "
        "build played them (others are played again)",
    )
    return command


def planned_games(args, command) -> tuple[list, str, str]:
    """The games the command line asks for, a description, and the shard file's stem."""
    max_ticks = max(1, round(args.max_minutes * TICKS_PER_MINUTE))
    if args.sweep:
        seeds = range(args.start_seed, args.start_seed + (args.seeds or SWEEP_SEEDS))
        what = f"B-F sweep, waves {' '.join(map(str, SWEEP_WAVES))}, seeds {seeds.start}-{seeds.stop - 1} per wave"
        return sweep_jobs(len(seeds), seeds.start), what, "sweep"
    names = bot_names(args, command)
    inputs = None if args.input is None else (list(INPUTS) if "all" in args.input else list(dict.fromkeys(args.input)))
    for name, flag in ((args.tune, "tune"), (args.gate, "gate"), (args.gate_input, "gate_input")):
        if name:
            chosen_bots = set(names) if args.bots else None
            jobs = tier_jobs(TIERS[flag], tapes=args.tapes, seeds=args.seeds, bots_=chosen_bots, inputs=inputs)
            if args.picks:
                for planned in jobs:
                    planned["policy"] = args.picks
            return jobs, f"{flag.replace('_', '-')} tier", flag.replace("_", "-")
    difficulties = list(DIFFICULTIES) if "all" in args.difficulty else list(dict.fromkeys(args.difficulty))
    inputs = inputs or ["direct"]
    seeds = range(args.start_seed, args.start_seed + (args.seeds or (STAGED_SEEDS if args.staged else 12)))
    options = {}
    if args.staged:
        options["act"] = args.staged
        if args.immortal:
            options["immortal"] = True
        if args.director:
            options["director"] = dict(args.director)
        if args.loadout:
            try:
                loadout = median_loadouts(read_runs(args.loadout)).get(args.staged)
            except NoGames as exc:
                command.error(str(exc))
            if loadout is None and args.staged > 1:
                command.error(f"no whole run in {args.loadout} reached act {args.staged}")
            options["loadout"] = loadout
        if inputs != ["direct"]:
            command.error("--staged games are played with direct input")
    elif args.immortal or args.director or args.loadout:
        command.error("--immortal, --director and --loadout go with --staged")
    if args.until:
        options["until"] = args.until
    if args.tapes:
        options["tape"] = True
    jobs = [
        job(bot, level, args.mode, seed, max_ticks=max_ticks, **options)
        | ({"input_mode": input_mode} if input_mode != "direct" else {})
        | ({"policy": args.picks} if args.picks else {})
        for bot in names
        for level in difficulties
        for input_mode in inputs
        for seed in seeds
    ]
    if args.tapes and inputs != ["direct"]:
        command.error("--tapes are kept for direct input only")
    what = f"{args.mode}, seeds {seeds.start}-{seeds.stop - 1}, capped at {args.max_minutes:g} min"
    if args.staged:
        what += f", from act {args.staged}'s first wave" + (" (immortal)" if args.immortal else "")
    return jobs, what, f"act{args.staged}" if args.staged else "games"


def bot_names(args, command) -> list[str]:
    default = [I1_BOT] if args.gate_input else list(QUICK)
    names = [name for value in (args.bots or default) for name in value.split(",") if name]
    names = list(BOTS) if "all" in names else list(dict.fromkeys(bots.canonical(name) for name in names))
    for name in names:
        try:
            bot_class(name)
        except ValueError as exc:
            command.error(str(exc))
    if args.picks:
        try:
            bot_picks.check(args.picks)
        except ValueError as exc:
            command.error(str(exc))
    return names


def main(argv: list[str] | None = None) -> int:
    command = parser()
    args = command.parse_args(argv)
    if args.merge:
        try:
            runs = read_runs(args.merge)
        except NoGames as exc:
            command.error(str(exc))
        where = " ".join(map(str, args.merge))
        if not runs:
            command.error(f"no games in {where}")
        builds = list(dict.fromkeys(run.get("build") or "unknown" for run in runs))
        if len(builds) > 1:  # review BH1: one report, one build (I1 merges the gate's games with the keyed ones)
            command.error(f"{where} hold games of {len(builds)} builds ({', '.join(builds)}): play them again")
        print(
            f"Ruleset {RULESET}, build {builds[0]}, Python {platform.python_version()}: {len(runs)} games from {where}"
        )
        stale = builds[0] != build()
        if stale:
            print(f"These games were played by build {builds[0]}, not this tree's {build()}: --check fails on them.")
        failed = report(runs, args.json, {"merged": [str(path) for path in args.merge], "build": builds[0]})
        return 1 if args.check and (failed or stale) else 0
    if args.audit:
        k, n = args.shard or (1, 1)
        started = time.perf_counter()
        try:
            done, kept, unfair, other = audit_dir(args.audit, (k, n), args.jobs)
        except NoGames as exc:
            command.error(str(exc))
        skipped = f", {other} of another build skipped" if other else ""
        print(
            f"Ruleset {RULESET}, build {build()}: audited {done} games ({kept} kept{skipped}) in "
            f"{time.perf_counter() - started:.1f}s, shard {k}/{n}: {len(unfair)} unavoidable hits"
        )
        for run, entry in unfair:
            print(f"  {run['job']} wave {entry['wave']} tick {entry['tick']}: {entry['cause']} {entry['class']}")
        return 1 if args.check and (unfair or other) else 0
    if args.lengths and not args.plan:
        command.error("--lengths sizes --plan's shards")
    jobs, what, stem = planned_games(args, command)
    if args.plan:
        if args.sweep:
            command.error("--plan sizes --tune, --gate, --gate-input, --staged and plain runs; the sweep has SHARDS")
        tiers = [name for name in ("tune", "gate", "gate-input") if getattr(args, name.replace("-", "_"))]
        flag = f"--{tiers[0]}" if tiers else f"--staged {args.staged}" if args.staged else ""
        lengths = None
        if args.lengths:
            try:
                lengths = measured_lengths(read_runs(args.lengths))
            except NoGames as exc:
                command.error(str(exc))
        plan = plan_tier(jobs, args.jobs, lengths=lengths)
        print(f"Ruleset {RULESET}, Python {platform.python_version()}: {what}, {len(jobs)} games")
        print(format_plan(plan, flag))
        return 0
    if args.profile:
        for planned in jobs:
            planned["profile"] = True
    k, n = args.shard or (1, 1)
    jobs = jobs[k - 1 :: n]
    if args.shard:
        what += f", shard {k}/{n}"
    keys = [job_key(planned) for planned in jobs]
    kept, sink, dropped = {}, None, 0
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / f"{stem}-{k}-of-{n}.jsonl"
        if path.exists():  # a stopped shard resumes, with this build's games only (review BH1)
            lines = read_runs_file(path)
            current = [run for run in lines if run.get("build") == build()]
            dropped = len(lines) - len(current)
            if dropped:  # rewritten without them: a merge must never read another build's line for a job
                path.write_text("".join(json.dumps(run) + "\n" for run in current), encoding="utf-8")
            wanted = set(keys)
            kept = {run["job"]: run for run in current if run["job"] in wanted}
        sink = path.open("a", encoding="utf-8")
    todo = [planned for planned, key in zip(jobs, keys, strict=True) if key not in kept]

    def done(planned, run):
        run["job"], run["build"] = job_key(planned), build()
        if sink is not None:
            sink.write(json.dumps(run) + "\n")
            sink.flush()

    started = time.perf_counter()
    try:
        played = run_all(todo, args.jobs, done)
    finally:
        if sink is not None:
            sink.close()
    elapsed = time.perf_counter() - started
    by_key = kept | {run["job"]: run for run in played}
    runs = [by_key[key] for key in keys]
    again = f", {len(kept)} kept from {args.out}" if kept else ""
    again += f", {dropped} of another build dropped" if dropped else ""
    line = (
        f"Ruleset {RULESET}, {what}, Python {platform.python_version()}, build {build()}: {len(played)} games in "
        f"{elapsed:.1f}s (--jobs {max(1, min(args.jobs, len(todo)))}){again}"
    )
    print(line)
    if not sys.stdout.isatty():  # the Makefile's shards send the report to /dev/null: this line still shows
        print(line, file=sys.stderr)
    settings = {"what": what, "mode": args.mode, "start_seed": args.start_seed, "seeds": args.seeds}
    settings["max_ticks"] = max(1, round(args.max_minutes * TICKS_PER_MINUTE))  # the cap of games not in a tier
    failed = report(runs, args.json, settings) if runs else []
    return 1 if args.check and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
