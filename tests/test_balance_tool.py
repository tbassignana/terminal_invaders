"""The balance tool's bots play real capped games, directly or keyed through the game's input path (spec 11.5), its
report keeps a stable shape, and it judges spec 11.5's acceptance as the spec owner decided it (decision 3), for the
strong bot (spec §27):

- kitty: the strong bot's median wave within -1 of direct, and its clear rate within -10 points, on every difficulty;
- latched: the same, on easy and normal;
- latched-ssh: the median within -1 and the clear rate within -15 points, on easy and normal;
- latched and latched-ssh on hard, and timed everywhere, are informational (spec §23.4, open question 8);
- a row is judged only with ACCEPT_GAMES (200) games of its input, against direct on the same seeds (gate row I1).

It also holds the quality bar (TARGETS, spec §19.1, §23.3, §27 and §27.2: strong for Q1 and Q7 on Hard and for Q5,
Q6, Q8, Q9 and I1 on Normal, the perfect bot for P, both audited for Q10; strong's Normal wins and the good bot's Hard
ones reported, not judged), the tiers and their shards, the action tapes and the fairness audit from them (one plan,
then two in a row, and for a landing the same again with fire held), --plan, --staged and the lives model's budget.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from terminal_invaders.engine import Game
from terminal_invaders.model import CAMPAIGN_WAVES, RULESET, TICK_RATE, Config
from terminal_invaders.term.keys import PRESS, RELEASE

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "balance.py"
SUMMARY_KEYS = {
    "mode",
    "bot",
    "difficulty",
    "input",
    "games",
    "win_rate",
    "wave_mean",
    "wave_median",
    "survival",
    "minutes_median",
    "accuracy",
    "lives_lost_per_minute",
    "bomb_kill_share",
    "powerup_uptime",
    "combo_mean",
    "extra_lives_mean",
    "ends",
    "deaths_per_act",
    "bombs_per_act",
    "no_escape_per_act",
    "deaths_noticed_late",
    "deaths_with_bomb",
    "causes",
    # B-M2: the tiers' rows, the gate's measures and §19.1's informational rows
    "kind",
    "wins_interval",
    "clean_win_rate",
    "continues_per_run",
    "wave1_rate",
    "winners_minutes_median",
    "bombs_per_run",
    "shots_per_minute_per_act",
    "in_flight_per_act",
    "extra_lives_per_act",
    "worst_wave_share",
    "wave_seconds",
    "pick_shares",
    "per_kind",
    "hits_per_second",
    "shots_per_kill",
    "loadouts",
    "profile",
    # the stall guard: games standing still, and the longest stretch of play with no hit
    "stalls",
    "quiet_max_seconds",
}


@pytest.fixture(scope="module")
def balance():
    spec = importlib.util.spec_from_file_location("balance_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[spec.name]


def test_every_bot_plays_a_capped_game(balance):
    for name in balance.BOTS:
        run = balance.play(name, "normal", "classic", 1000, 600)
        assert run["bot"] == name and run["ticks"] <= 600 and run["end"] in {"cap", "destroyed", "landed"}
        assert 0 < run["playing_ticks"] < run["ticks"]
        assert 0 <= run["hits"] <= run["shots"] and run["shots"] > 0
        assert run["cleared"] == run["wave"] - 1 and len(run["wave_ticks"]) == run["wave"]
        assert set(balance.COUNTERS) <= run.keys()


def test_runs_repeat_exactly_for_a_seed(balance):
    for keys in ("direct", "kitty", "latched-ssh"):  # latched-ssh draws its link's jitter from the seed
        first, second = (balance.play("sk_d18_p85", "hard", "classic", 7, 900, keys) for _ in range(2))
        del first["wall"], second["wall"]
        assert first == second and first["input"] == keys


def test_each_input_mode_keys_the_game_through_its_input_path(balance):
    modes = {"kitty": ("hold", True, False), "latched": ("latched", False, False), "timed": ("timed", False, False)}
    modes["latched-ssh"] = ("latched", False, True)
    for mode, (scheme, kitty, remote) in modes.items():
        keyboard = balance.Keyboard(mode, Config("normal", "classic", 3))
        session, game = keyboard.session, keyboard.game
        assert (session.controls.scheme, keyboard.decoder.kitty, keyboard.vin.kitty) == (scheme, kitty, kitty)
        assert (session.router.gate.repeat.remote, keyboard.vin.link is not None) == (remote, remote)
        assert game.config == Config("normal", "classic", 3) and session.scene == "playing"
        start = game.player.x
        for _ in range(45):  # kitty holds d down; latched and timed are keyed with taps: d once, then S
            keyboard.step(("right",))
        assert session.routed > 0 and game.player.x > start + 0.4 * 35, mode  # moving within a few ticks
        for _ in range(30):
            keyboard.step(())
        x = game.player.x
        for _ in range(10):
            keyboard.step(())
        assert game.player.x == x, mode  # let go, or tapped S: stopped


def test_timed_controls_move_while_a_key_is_held_and_latch_a_quick_press(balance):
    """The I1 timed row keys timed controls with taps, as latched (Keyboard). Their hold path still works through the
    same input path when keyed as a person holding keys (IntentDriver's "timed" scheme on the Keyboard's own
    VirtualInput, legacy bytes with the OS repeat): a hold longer than the repeat delay (0.525 s) moves the ship
    while held and stops it once the repeats stop; a press shorter than the delay latches, so the ship keeps moving
    after it until S, or until a quick press the other way, which stops it instead of reversing (TimedControls). A
    hold the other way turns the ship at once. Space pressed while a direction is held takes the OS repeat from it,
    so the game can't see the direction let go and the ship moves on until S (README, `--controls`)."""
    from terminal_invaders.input import IntentDriver

    keyboard = balance.Keyboard("timed", Config("normal", "classic", 3))
    game = keyboard.game
    assert (keyboard.session.controls.scheme, keyboard.driver.scheme) == ("timed", "latched")  # the bots tap
    keyboard.driver = IntentDriver("timed", keyboard.vin)  # held keys instead
    delay = keyboard.vin.profile.delay

    def press(actions, ticks):
        """The x of the ship after each of `ticks` steps with these actions held."""
        out = []
        for _ in range(ticks):
            keyboard.step(actions)
            out.append(game.player.x)
        return out

    def moves(xs, before):
        return [round(x - y, 6) for y, x in zip([before, *xs], xs)]

    game.player.x = start = 8.0
    held = press(("right",), round(1.5 * delay * TICK_RATE))  # 0.79 s: the delay, then the OS repeats d
    assert all(step > 0 for step in moves(held, start)[2:]), moves(held, start)  # moving all along
    after = press((), 30)
    gone = moves(after, held[-1])
    assert any(gone[:15]) and not any(gone[15:]), gone  # the repeats stop with the key: the ship stops
    quick = press(("right",), round(0.4 * delay * TICK_RATE))  # 0.2 s: let go before the OS repeats it
    latched = press((), 30)
    assert all(step > 0 for step in moves(latched, quick[-1])), moves(latched, quick[-1])  # still moving
    keyboard.vin.tap("s", game.tick / TICK_RATE)
    stopped = press((), 30)
    assert not any(moves(stopped, latched[-1])[3:]), moves(stopped, latched[-1])  # until S
    press(("left",), round(0.4 * delay * TICK_RATE))
    moving = press((), 20)
    assert all(step < 0 for step in moves(moving[1:], moving[0])), moves(moving[1:], moving[0])
    other = press(("right",), round(0.4 * delay * TICK_RATE)) + press((), 30)
    steps = moves(other, moving[-1])
    assert all(step <= 0 for step in steps) and not any(steps[4:]), steps  # the other way: it stops there
    game.player.x = start = 20.0
    held = press(("right",), round(1.5 * delay * TICK_RATE))
    turned = press(("left",), round(1.5 * delay * TICK_RATE))  # a hold the other way: it turns at once
    assert all(step < 0 for step in moves(turned, held[-1])[2:]), moves(turned, held[-1])
    gone = moves(press((), 30), turned[-1])
    assert any(gone[:15]) and not any(gone[15:]), gone
    game.player.x = start = 10.0
    held = press(("right",), round(1.5 * delay * TICK_RATE))
    firing = press(("right", "fire"), 20) + press(("fire",), 30)  # Space takes the OS repeat; then d is let go
    assert all(step > 0 for step in moves(firing, held[-1])), moves(firing, held[-1])  # moving on, unseen
    keyboard.vin.tap("s", game.tick / TICK_RATE)
    stopped = press(("fire",), 30)
    assert not any(moves(stopped, firing[-1])[3:]), moves(stopped, firing[-1])  # until S


def test_a_bot_asking_for_a_bomb_every_tick_taps_b_four_times_a_second(balance):
    keyboard = balance.Keyboard("kitty", Config("normal", "classic", 3))
    game, handle, keys = keyboard.game, keyboard.session.handle, []
    keyboard.session.handle = lambda key, now: (keys.append(key), handle(key, now))
    while game.phase != "playing":
        keyboard.step(())
    game.player.bombs, bombs = 3, 0
    for _ in range(TICK_RATE // 2):
        keyboard.step(("bomb",))
        bombs += "bomb" in game.events
    assert [(key.name, key.kind) for key in keys] == [("b", PRESS), ("b", RELEASE)] * 2  # at 0 and 0.25 s
    assert bombs >= 1  # the first may clear the wave, and there are none in the next one's intro


@pytest.mark.gate  # G0: the balance smoke (spec 11.5)
def test_every_input_mode_smoke(balance, tmp_path, capsys):
    path = tmp_path / "smoke.json"
    arguments = ["--bots", "skilled", "--input", "all", "--seeds", "3", "--max-minutes", str(3000 / TICK_RATE / 60)]
    assert balance.main([*arguments, "--jobs", "1", "--json", str(path)]) == 0
    assert "input" in capsys.readouterr().out.splitlines()[2]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["max_ticks"] == 3000 and [row["input"] for row in data["summary"]] == list(balance.INPUTS)
    for run in data["runs"]:
        assert run["ticks"] == 3000 or run["end"] in {"destroyed", "landed"}
        assert run["shots"] > 0 and run["kills"] > 0 and run["playing_ticks"] > 0


def summary_row(input_mode, difficulty, wins, median, bot="strong", mode="classic"):
    return dict(mode=mode, bot=bot, difficulty=difficulty, input=input_mode, win_rate=wins, wave_median=median)


def test_the_acceptance_applies_the_latched_thresholds_to_easy_and_normal_only(balance):
    """The thresholds of the module docstring: hard on latched terminals and timed are reported, never failed. Three
    rows are the gate run's (latched 93% and 70%, latched-ssh 53% on hard); the others sit on or just past a
    threshold."""
    rows = [summary_row("direct", level, 1.0, 12) for level in ("easy", "normal", "hard")]
    rows += [summary_row("direct", "hard", 0.0, 1, bot="naive"), summary_row("kitty", "hard", 0.0, 1, bot="naive")]
    rows += [summary_row("kitty", "easy", 0.9, 12), summary_row("kitty", "normal", 1.0, 11)]
    rows += [summary_row("kitty", "hard", 0.89, 12), summary_row("latched", "easy", 1.0, 10)]
    rows += [summary_row("latched", "normal", 0.93, 12), summary_row("latched", "hard", 0.70, 12)]
    rows += [summary_row("latched-ssh", "easy", 0.86, 12), summary_row("latched-ssh", "normal", 0.84, 12)]
    rows += [summary_row("latched-ssh", "hard", 0.53, 11), summary_row("timed", "hard", 0.0, 1)]
    results = balance.acceptance(rows)
    verdicts = {(result["input"], result["difficulty"]): result["result"] for result in results}
    assert verdicts == {
        ("kitty", "easy"): "PASS",  # -10 points, -0 waves
        ("kitty", "normal"): "PASS",  # -1 wave
        ("kitty", "hard"): "FAIL",  # kitty's thresholds hold on hard too
        ("latched", "easy"): "FAIL",  # -2 waves
        ("latched", "normal"): "PASS",
        ("latched", "hard"): "info",
        ("latched-ssh", "easy"): "PASS",  # -14 points
        ("latched-ssh", "normal"): "FAIL",  # -16 points
        ("latched-ssh", "hard"): "info",
        ("timed", "hard"): "info",
    }  # the naive bot's rows are not judged: 11.5 is for the strong bot
    notes = {result["rule"] for result in results if result["result"] == "info"}
    assert notes == {"no threshold", f"no threshold on hard: {balance.UNTHRESHOLDED}"}
    assert "open question 8" in balance.UNTHRESHOLDED and "tap-to-move" in balance.UNTHRESHOLDED
    report = balance.format_acceptance(results).splitlines()
    assert report[0].startswith("Spec 11.5 acceptance") and len(report) == 2 + len(results)
    assert report[-1].split()[:4] == ["timed", "hard", "-11", "-100"]
    # No direct games to compare with (review BH5): a gate-sized keyed run fails, as I1 can't be judged; fewer is info
    (alone,) = balance.acceptance([summary_row("kitty", "easy", 1.0, 12)])  # a row with no count counts as enough
    assert (alone["result"], alone["rule"]) == ("FAIL", "no direct games on these seeds; merge the gate's directory")
    (few,) = balance.acceptance([summary_row("kitty", "easy", 1.0, 12) | {"games": 12}])
    assert (few["result"], few["rule"]) == ("info", "no direct reference")
    assert balance.format_acceptance([alone]).splitlines()[-1].split()[:5] == ["kitty", "easy", "-", "-", "FAIL"]


def test_check_fails_the_run_when_the_acceptance_fails(balance, capsys, monkeypatch):
    arguments = ["--bots", "strong", "--input", "direct", "kitty", "--seeds", "1", "--max-minutes", "0.05"]
    assert balance.main([*arguments, "--jobs", "1", "--check"]) == 0
    assert "info (n < 200)" in capsys.readouterr().out  # a game a side: judged at 200
    monkeypatch.setattr(balance, "ACCEPT_GAMES", 1)
    assert balance.main([*arguments, "--jobs", "1", "--check"]) == 0
    out = capsys.readouterr().out
    assert "Spec 11.5 acceptance (strong, classic)" in out and "kitty       normal" in out and "PASS" in out
    monkeypatch.setitem(balance.ACCEPT, "kitty", (-1, 0.0, ("normal",)))  # kitty must beat direct by a wave
    assert balance.main([*arguments, "--jobs", "1"]) == 0  # reports only, without --check
    assert balance.main([*arguments, "--jobs", "1", "--check"]) == 1
    assert capsys.readouterr().out.count("FAIL (median within --1") == 2
    assert balance.main(["--bots", "naive", "--seeds", "1", "--max-minutes", "0.05", "--jobs", "1", "--check"]) == 0
    assert "acceptance" not in capsys.readouterr().out  # no keyed input, nothing to judge
    keyed = ["--bots", "perfect", "--input", "direct", "kitty", "--seeds", "1", "--max-minutes", "0.05", "--jobs", "1"]
    assert balance.main([*keyed, "--check"]) == 0
    assert "acceptance" not in capsys.readouterr().out  # I1 is the strong bot's: skilled's keyed games aren't judged


def test_bots_see_only_public_state(balance):
    view = balance.PublicView(Game())
    assert view.player.lives == 3 and view.phase == "wave"
    for name in ("rng", "_direction", "_alien_clock", "_wave_size"):
        with pytest.raises(AttributeError):
            getattr(view, name)
    with pytest.raises(AttributeError):
        view.score = 10**6
    assert not re.search(r"\bgame\.(rng\b|_)", TOOL.read_text(encoding="utf-8"))


def test_bot_names_and_survival_columns(balance):
    assert balance.bot_class("sk_d9_p50").delay == 9 and balance.bot_class("sk_d9_p50").perceive == 0.5
    strong = balance.bot_class("strong")
    assert (strong.delay, strong.perceive) == (12, 0.92) and balance.bot_class("sk_d12_p92") is strong
    assert balance.bot_class("perfect") is balance.bot_class("skilled")
    assert balance.QUICK == ("strong", "perfect", "sk_d18_p85", "naive", "novice") and balance.I1_BOT == "strong"
    args = balance.parser().parse_args(["--bots", "perfect,strong", "sk_d12_p92", "skilled"])
    assert balance.bot_names(args, balance.parser()) == ["skilled", "strong"]  # one name for each bot's games
    assert balance.bot_names(balance.parser().parse_args(["--gate-input"]), balance.parser()) == ["strong"]
    assert balance.bot_names(balance.parser().parse_args([]), balance.parser()) == [
        "strong",
        "skilled",
        "sk_d18_p85",
        "naive",
        "novice",
    ]
    for name in ("sk_d9", "sk_d9_p101", "wizard"):
        with pytest.raises(ValueError):
            balance.bot_class(name)
    with pytest.raises(SystemExit):
        balance.main(["--bots", "wizard"])
    assert balance.checkpoints(CAMPAIGN_WAVES) == list(range(1, CAMPAIGN_WAVES + 1))
    assert balance.checkpoints(236) == [1, *range(20, 221, 20)]


def test_report_and_json_cover_each_bot_and_difficulty(balance, tmp_path, capsys):
    path = tmp_path / "out" / "runs.json"
    arguments = ["--bots", "novice,perfect", "--difficulty", "easy", "hard", "--seeds", "2", "--max-minutes", "0.05"]
    assert balance.main([*arguments, "--jobs", "1", "--json", str(path)]) == 0
    report = capsys.readouterr().out
    assert f"Ruleset {RULESET}, classic, seeds 1000-1001" in report
    tables = report.split("\n\nshot log: ")[0]  # the rows' tables, before the shot log, budget and quality bar
    for bot in ("novice", "perfect"):  # a row per difficulty in each of the six tables, and in per enemy kind's
        assert 12 <= sum(line.startswith(bot) for line in tables.splitlines()) <= 14
    assert not any(line.startswith("skilled") for line in report.splitlines())  # printed as perfect (spec §27)
    for title in ("per game, acts I-IV", "enemy fire and lives, acts I-IV", "lives lost by cause", "more per row"):
        assert title in report
    (shots,) = [line for line in report.splitlines() if line.startswith("shot log: ")]
    assert shots.endswith(": 0 outside the rules") and "Lives budget" in report and "The quality bar" in report
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ruleset"] == RULESET and data["max_ticks"] == 180 and len(data["runs"]) == 8
    assert data["shot_log"]["count"] == sum(run["shot_log"]["count"] for run in data["runs"])
    assert [(row["bot"], row["difficulty"]) for row in data["summary"]] == [  # kept under the bot's own name
        ("novice", "easy"),
        ("novice", "hard"),
        ("skilled", "easy"),
        ("skilled", "hard"),
    ]
    for row in data["summary"]:
        assert row.keys() == SUMMARY_KEYS and row["games"] == 2 and row["ends"] == {"cap": 2}
        assert len(row["survival"]) == CAMPAIGN_WAVES and row["win_rate"] == 0
        assert all(0 <= row[key] <= 1 for key in ("accuracy", "bomb_kill_share", "powerup_uptime"))
        assert all(len(row[key]) == 4 for key in ("deaths_per_act", "bombs_per_act", "no_escape_per_act"))


# --- the feasibility sweep and the audit (spec §22.3 step 4, §18) ---------------------------------------------------


def test_the_sweep_plays_spec_22_3s_games(balance):
    jobs = balance.sweep_jobs()
    seeds = balance.SWEEP_SEEDS
    assert len(jobs) == (2 * 6 + 2 * 3) * 3 * seeds and len({balance.job_key(job) for job in jobs}) == len(jobs)
    points = {}
    for job in jobs:
        director = job["director"]
        assert (director["reaction"], director["focus"]) == (0.04, 0.5) and job["immortal"] and job["audit"]
        assert job["difficulty"] == "normal" and job["mode"] == "classic" and job["wave"] in (5, 8, 10)
        points.setdefault(job["bot"], set()).add((director["slots"], director["reload"]))
    every = {(3, 72), (4, 56), (5, 44), (6, 34), (8, 26), (8, 20)}
    assert points["skilled"] == points["sk_d18_p85"] == every
    assert points["sk_d10_p100"] == points["sk_d14_p100"] == {(4, 56), (6, 34), (8, 20)}
    assert seeds * 3 * 1.2 >= 100  # about 100 minutes of play per bot and point (a staged wave plays 1.1-1.5)


def test_a_staged_game_plays_its_wave_and_repeats(balance):
    director = {"slots": 5, "reload": 44, "reaction": 0.04, "focus": 0.5}
    first, second = (
        balance.play("skilled", seed=1000, wave=8, immortal=True, director=director, audit=True) for _ in range(2)
    )
    del first["wall"], second["wall"]
    assert first == second and first["end"] == "cleared" and first["wave"] == 8
    assert first["staged"] == {"wave": 8, "immortal": True, "director": director}
    assert first["playing_ticks"] > 45 * TICK_RATE and first["enemy_shots"] == sum(first["acts"]["shots"]) > 0
    assert sum(first["acts"]["in_flight"]) > first["playing_ticks"] / 2  # more than half a shot on the field
    with pytest.raises(ValueError, match="direct input"):
        balance.play("skilled", seed=1, wave=8, input_mode="kitty")


def test_a_game_standing_still_is_reported_as_a_stall_and_fails_check(balance, capsys, monkeypatch):
    """The stall guard: STALL_TICKS of play with no hit on the fleet or a boss, whatever the ship does, marks the game
    standing still (DREADNOUGHT's beam phase once stood to the time cap that way, and Endless counted its wave as
    reached). A bot that hits keeps the stretch short; a bot that never fires is flagged, and --check fails."""
    assert balance.STALL_TICKS == 10 * 60 * TICK_RATE
    playing = balance.play("strong", "normal", "classic", 1000, 1800)
    assert 0 < playing["quiet"]["ticks"] < 10 * TICK_RATE and playing["quiet"]["wave"] == 1  # a lull between flights
    assert balance.stalls([playing]) == []

    class Idle:  # stands still and never fires, as the bot stood under DREADNOUGHT
        def __init__(self, *args, **options):
            pass

        def act(self, frame):
            return ()

    monkeypatch.setattr(balance.bots, "make", Idle)
    idle = balance.play("strong", "normal", "classic", 1000, 1800)
    quiet = idle["quiet"]
    assert idle["hits"] == idle["kills"] == 0 and quiet["wave"] == 1
    assert quiet["ticks"] == idle["playing_ticks"] and quiet["tick"] == idle["ticks"] - quiet["ticks"]
    assert balance.stalls([idle]) == []  # 30 s of play: too short to be a stall
    monkeypatch.setattr(balance, "STALL_TICKS", 20 * TICK_RATE)
    (stall,) = balance.stalls([playing, idle])
    assert stall["result"] == "FAIL" and "seed 1000: wave 1" in stall["detail"]
    old = {key: value for key, value in playing.items() if key != "quiet"}  # a game played before the guard
    assert balance.report([old, idle], None, {}) == [stall]
    out = capsys.readouterr().out
    assert "stalls: 1 of 1 games stood still" in out and "1 games played before stalls were recorded" in out
    assert f"  {stall['detail']}" in out
    row = balance.summarise([idle])[0]
    assert row["stalls"] == 1 and row["quiet_max_seconds"] == quiet["ticks"] / TICK_RATE


def test_no_escape_moments_and_lives_lost_are_linked(balance):
    class Pilot:
        window = 13
        no_escape = [(100, 5, 1, 1, True), (500, 5, 1, 0, False), (900, 5, 1, 0, False)]

    lives = [{"tick": 100 + 13 + balance.LINK}, {"tick": 950}, {"tick": 2000}]
    moments = balance.link(Pilot, lives)
    assert [moment["hit"] for moment in moments] == [True, False, False]
    assert [entry["seen"] for entry in lives] == [True, False, False]
    assert moments[0] == {"tick": 100, "act": 1, "bombs": 1, "bombed": True, "hit": True}


def test_the_sweep_counts_events_bombs_and_the_seen_share(balance):
    audit = {"tick": 0, "t0": 0, "plan": None, "bomb": None}
    lives = [
        {"tick": 500, "cause": "dart", "class": "reaction", "late": False, "bombs": 0, "seen": True}
        | {"audit": audit | {"avoided": True, "dodged": False}},
        {"tick": 900, "cause": "pellet", "class": "safe", "late": True, "bombs": 1, "seen": False}
        | {"audit": audit | {"avoided": False, "dodged": True}},
    ]
    moments = [
        {"tick": 480, "act": 1, "bombs": 0, "bombed": False, "hit": True},
        {"tick": 200, "act": 1, "bombs": 1, "bombed": True, "hit": False},  # a bomb absorbed it
        {"tick": 300, "act": 1, "bombs": 0, "bombed": False, "hit": False},  # a false alarm
    ]
    run = {"bot": "skilled", "seed": 7, "staged": {"wave": 8, "director": {"slots": 3, "reload": 72}}}
    run |= {"playing_ticks": 3600, "acts": {"shots": [0, 60, 0, 0], "in_flight": [0, 7200, 0, 0]}}
    run |= {"bombs_used": 1, "shield_hits": 0, "lives_log": lives, "no_escape_log": moments}
    (row,) = balance.sweep_rows([run])
    assert (row["minutes"], row["shots_per_minute"], row["in_flight"]) == (1.0, 60.0, 2.0)
    assert (row["deaths"], row["no_escape"], row["bombs"], row["events"]) == (2, 3, 1, 4)
    assert (row["absorbed"], row["false_alarms"], row["late"], row["with_bomb"]) == (1, 1, 1, 1)
    assert row["b"] == 2 / 3  # a life lost after a no-escape moment and an absorbed one, of three fatal events
    assert row["per_wave"] == {"5": 0, "8": 2, "10": 0} and row["audited"] == 2
    assert row["unavoidable"] == [
        {"seed": 7, "wave": 8, "tick": 900, "cause": "pellet", "class": "safe", "bombs": 1, "dodged": True}
    ]
    text = balance.format_sweep([row])
    assert "unavoidable hits (spec §18's audit): 1" in text and "a plan dodged it and was hit later" in text
    assert text.splitlines()[1].split()[:6] == ["perfect", "3/72", "1", "1.0", "60.0", "2.00"]


def walled(rows, bombs=0):
    """A staged game in play with no director fire, and rows of enemy pellets falling under the shelters:
    (y, vy, xs) each, xs "ship" for one pellet over the ship, "all" for one every half cell across the field."""
    from terminal_invaders.campaign.testing import staged
    from terminal_invaders.model import Shot

    def build():
        config = Config("normal", "classic", 5)
        game = staged(config, 5, phase="playing", immortal=True, bombs=bombs, director={"reload": 10**6})
        ident = 10**6
        for y, vy, xs in rows:
            for x in [game.player.x] if xs == "ship" else [i / 2 for i in range(120)]:
                ident += 1
                game.shots.append(Shot(x, y, friendly=False, vy=vy, id=ident, kind="pellet"))
        return game

    return build


def recorded_hits(build, tape):
    game, hits = build(), []
    for actions in tape:
        game.step(actions)
        if "damage" in game.events:
            hits.append(game.tick)
    return hits


def test_the_audit_tells_a_dodge_from_a_wall_and_counts_dodging_into_another_shot(balance):
    tape = [()] * 300  # the recorded ship stands still and holds fire
    over = walled([(16.0, 0.1, "ship")])
    hits = recorded_hits(over, tape)
    (verdict,) = balance.audit_hits(over, tape, hits)
    assert len(hits) == 1 and verdict["avoided"] and verdict["plan"] != [0, 0, 0, 0] and verdict["bomb"] is None
    start = over().tick  # the intro has run out: the audit never copies the game before it started
    assert verdict["t0"] == max(start, (hits[0] - 30) // 30 * 30) and not verdict["dodged"]

    wall = walled([(16.0, 0.1, "all")])
    (verdict,) = balance.audit_hits(wall, tape, recorded_hits(wall, tape))
    assert not verdict["avoided"] and not verdict["dodged"]  # no plan, no two in a row, and no bomb in stock
    assert verdict["switch"] is None and verdict["flights"] > len(balance.bot_plan.PLANS)  # two plans were flown
    with_bomb = walled([(16.0, 0.1, "all")], bombs=1)
    hits = recorded_hits(with_bomb, tape)
    (verdict,) = balance.audit_hits(with_bomb, tape, hits)
    assert verdict["avoided"] and verdict["t0"] <= verdict["bomb"] < hits[0] and verdict["switch"] is None

    # The recorded ship's hit clears the slow wall behind it (the mercy clear); a plan that dodges the first pellet
    # meets the wall before the hit's tick + 120, so it avoids nothing.
    then = walled([(16.0, 0.1, "ship"), (16.0, 0.02, "all")])
    hits = recorded_hits(then, tape)
    (verdict,) = balance.audit_hits(then, tape, hits)
    assert len(hits) == 1 and not verdict["avoided"] and verdict["dodged"]
    assert verdict["flights"] > len(balance.bot_plan.PLANS)  # two plans in a row were flown, and none avoided it


def gapped(walls):
    """A staged game in play from a tick the audit's windows start on (a multiple of 30), with no director fire and
    no shelters, and walls of enemy pellets falling to the ship: (ticks, offset) each, a wall reaching the top of the
    ship's box that many ticks after the start, one pellet every half cell but for a gap the ship fits through, centred
    `offset` cells from where the ship stands."""
    from terminal_invaders.campaign.testing import staged
    from terminal_invaders.model import Shot

    speed, top = 0.25, 18.0 - 0.4  # the pellets' fall a tick; the top of the ship's box (y 18, half-height 0.4)

    def build():
        config = Config("normal", "classic", 5)
        game = staged(config, 5, phase="playing", immortal=True, bombs=0, director={"reload": 10**6})
        while game.tick % 30:
            game.step(())
        game.bunkers = {}
        ident = 10**6
        for ticks, offset in walls:
            centre = game.player.x + offset
            for x in [i / 2 for i in range(121)]:
                if abs(x - centre) > 1.0 + 1.1:  # the gap: 1 cell each side of its centre, and the ship's half-width
                    ident += 1
                    game.shots.append(Shot(x, top - speed * ticks, friendly=False, vy=speed, id=ident, kind="pellet"))
        return game

    return build


def test_the_audit_tries_two_plans_in_a_row(balance, monkeypatch):
    """Spec §27.2: a wall with a gap at the ship 8 ticks after t0, then one 30 ticks after t0 with its gap 6 cells to
    the left. Standing, the ship passes the first wall and the second hits it. Every plan of the library moves from
    t0, so it meets the first wall or, moving little or not at all, the second: no single plan avoids the hit.
    Standing until a decision point after the first wall, then moving, does. With the second gap too far to reach in
    time (24 cells: 60 ticks at full speed), no two plans in a row avoid it either."""
    tape = [()] * 400  # the recorded ship stands still and holds fire
    pair = gapped([(8, 0.0), (30, -6.0)])
    hits = recorded_hits(pair, tape)
    start = pair().tick
    assert len(hits) == 1 and not recorded_hits(gapped([]), tape)  # the walls' hit, and nothing else
    for bot in ("strong", "skilled"):
        cadence = balance.bot_cadence(bot)
        (verdict,) = balance.audit_hits(pair, tape, hits, cadence)
        assert verdict["avoided"] and verdict["bomb"] is None and not verdict["dodged"]
        first, switch, then = tuple(verdict["plan"]), verdict["switch"], tuple(verdict["then"])
        assert verdict["t0"] < switch < hits[0] and (switch - verdict["t0"]) % cadence == 0  # a decision point
        assert first in balance.bot_plan.PLANS and then in balance.bot_plan.PLANS
        assert verdict["flights"] > len(balance.bot_plan.PLANS)
        # the sequence found loses no life, flown again here: the first plan to the switch, then the second
        game = pair()
        while game.tick < verdict["t0"]:
            game.step(tape[game.tick - start])
        while game.tick < switch:
            balance.step_plan(game, tape, start, first, verdict["t0"])
            assert "damage" not in game.events
        assert balance.fly(game, tape, start, then, hits[0] + balance.AUDIT_AFTER) is None
    paired = verdict
    with monkeypatch.context() as patched:  # one plan only, as the audit was before §27.2: unavoidable
        patched.setattr(balance, "switched", lambda *args, **kwargs: (None, 0))
        (verdict,) = balance.audit_hits(pair, tape, hits, 4)
        assert not verdict["avoided"] and verdict["flights"] == len(balance.bot_plan.PLANS)

    far = gapped([(8, 0.0), (30, -24.0)])
    hits = recorded_hits(far, tape)
    (verdict,) = balance.audit_hits(far, tape, hits, balance.bot_cadence("strong"))
    assert len(hits) == 1 and not verdict["avoided"] and verdict["switch"] is None
    assert verdict["flights"] > 2 * len(balance.bot_plan.PLANS)  # every first plan was switched before it was hit
    life = {"wave": 5, "cause": "pellet", "class": "safe", "bombs": 0}
    lives = [life | {"tick": audited["tick"], "audit": audited} for audited in (paired, verdict)]
    text = balance.format_audit([{"bot": "strong", "difficulty": "hard", "seed": 1, "lives_log": lives}])
    assert (
        "2 of 2 lives lost audited, 1 unavoidable, 0 avoided only with a bomb, 1 only with two plans in a row" in text
    )
    assert f"{paired['flights'] + verdict['flights']:,} sequences flown" in text
    assert "(every plan and every two plans in a row hit)" in text


def landing(offset, bombs=0):
    """A staged game in play from a tick the audit's windows start on (a multiple of 30), with no director fire and
    no shelters, and one enemy on the field: a scout of the first flight, `offset` cells across from the ship at row
    8, pushed down (as an arrival pushes older flights, a quarter row a tick) to the landing row, which it reaches 30
    ticks later. The wave's next flight arrives long after the audit's window."""
    from terminal_invaders.campaign.tables import ARRIVAL
    from terminal_invaders.campaign.testing import staged

    def build():
        config = Config("normal", "classic", 5)
        game = staged(config, 5, phase="playing", immortal=True, bombs=bombs, director={"reload": 10**6})
        while not game._flights or game._flights[0].entering or game.tick % 30:
            game.step(())
        game.bunkers = {}
        scout = next(alien for alien in game.aliens if alien.flight == 0 and alien.kind == "scout")
        scout.home_x = scout.x = game.player.x + offset
        scout.home_y = scout.y = 8.0
        game.aliens = [scout]
        flight = game._flights[0]
        flight.alive, flight.push = 1, ARRIVAL.land - scout.y
        return game

    return build


def test_the_audit_judges_a_landing_with_fire_held_too(balance, monkeypatch):
    """Spec §18: a landing can't be dodged, only shot, so the audit also flies it with fire held. A lone scout 5
    cells left of the ship lands 30 ticks after t0, and the recorded ship holds no fire: with fire as recorded no
    plan and no two plans in a row avoid it, as the audit found before. With fire held (fire in every step's
    actions, as Space held), moving left for 10 ticks shoots it first. Out of reach (20 cells away), nothing avoids
    it, with fire as recorded or held. With a bomb in stock, fire held is flown with the bomb too (though here the
    bomb with fire as recorded comes first: its invulnerability outlasts the landing). A hit by a shot is never flown
    with fire held."""
    plans, cadence = balance.bot_plan.PLANS, balance.bot_cadence("strong")
    tape = [()] * 400  # the recorded ship stands still and never fires
    near = landing(-5.0)
    hits = recorded_hits(near, tape)
    game = near()
    start = game.tick
    for actions in tape[: hits[0] - start]:
        game.step(actions)
    assert len(hits) == 1 and hits[0] == start + 30 and game.hit_cause == "landing"

    (verdict,) = balance.audit_hits(near, tape, hits, cadence)
    assert verdict["avoided"] and verdict["held"] and verdict["t0"] == start and not verdict["dodged"]
    plan = tuple(verdict["plan"])
    assert plan == (-1, 10, 0, 0) and verdict["switch"] is None and verdict["bomb"] is None
    with monkeypatch.context() as patched:  # not judged as a landing, as the audit was before: unavoidable
        patched.setattr(balance, "cause_of", lambda *args: "")
        (recorded,) = balance.audit_hits(near, tape, hits, cadence)
    assert not recorded["avoided"] and not recorded["held"] and recorded["flights"] > len(plans)
    # every sequence with fire as recorded first, then the single plans with fire held, in order, up to this one
    assert verdict["flights"] == recorded["flights"] + plans.index(plan) + 1
    # flown again: with fire held it loses no life; with fire as recorded, or standing with fire held, the scout lands
    end = hits[0] + balance.AUDIT_AFTER
    assert balance.fly(near(), tape, start, plan, end, held=True) is None
    assert balance.fly(near(), tape, start, plan, end) == hits[0]
    assert balance.fly(near(), tape, start, (0, 0, 0, 0), end, held=True) is not None

    far = landing(-20.0)
    hits = recorded_hits(far, tape)
    with monkeypatch.context() as patched:
        patched.setattr(balance, "cause_of", lambda *args: "")
        (recorded,) = balance.audit_hits(far, tape, hits, cadence)
    (unfair,) = balance.audit_hits(far, tape, hits, cadence)
    assert len(hits) == 1 and not unfair["avoided"] and not unfair["held"] and unfair["switch"] is None
    assert unfair["flights"] == 2 * recorded["flights"]  # the same sequences again, with fire held
    armed = landing(-20.0, bombs=1)
    assert recorded_hits(armed, tape) == hits
    (bombed,) = balance.audit_hits(armed, tape, hits, cadence)
    assert bombed["avoided"] and not bombed["held"] and bombed["bomb"] is not None  # fire as recorded and a bomb
    found, flights, _later = balance.search(armed(), tape, start, hits[0], cadence, held=True)
    assert found == {"avoided": True, "plan": [0, 0, 0, 0], "bomb": start, "held": True}
    assert flights == recorded["flights"] + 1  # every plan and two plans in a row with fire held, then with a bomb

    flown = []  # a wall of pellets: the held search never runs
    fly = balance.fly

    def spy(*args, held=False, **kwargs):
        flown.append(held)
        return fly(*args, held=held, **kwargs)

    monkeypatch.setattr(balance, "fly", spy)
    wall = walled([(16.0, 0.1, "all")])
    (shot,) = balance.audit_hits(wall, tape, recorded_hits(wall, tape), cadence)
    assert not shot["avoided"] and len(flown) == shot["flights"] and not any(flown)

    life = {"wave": 5, "cause": "landing", "class": "", "bombs": 0}
    lives = [life | {"tick": audited["tick"], "audit": audited} for audited in (verdict, unfair)]
    text = balance.format_audit([{"bot": "strong", "difficulty": "hard", "seed": 1, "lives_log": lives}])
    assert (
        "2 of 2 lives lost audited, 1 unavoidable, 0 avoided only with a bomb, 0 only with two plans in a row, "
        "1 landing only with fire held;" in text
    )
    assert "(every plan and every two plans in a row hit, with fire as recorded and with fire held)" in text
    before = {key: value for key, value in unfair.items() if key != "held"}  # an audit from before fire held
    text = balance.format_audit(
        [{"bot": "strong", "difficulty": "hard", "seed": 1, "lives_log": [life | {"audit": before, "tick": 1}]}]
    )
    assert "0 landings only with fire held;" in text and "(every plan and every two plans in a row hit)" in text


def test_sweep_shards_resume_and_merge(balance, tmp_path, capsys):
    out = tmp_path / "sweep"
    arguments = ["--sweep", "--seeds", "1", "--shard", "2/27", "--out", str(out), "--jobs", "1"]
    assert balance.main(arguments) == 0
    first = capsys.readouterr().out
    assert "B-F sweep, waves 5 8 10, seeds 1000-1000 per wave, shard 2/27" in first and ": 2 games in" in first
    lines = (out / "sweep-2-of-27.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all(json.loads(line)["job"] for line in lines)
    assert balance.main(arguments) == 0  # resumed: nothing left to play
    assert (
        ": 0 games in" in capsys.readouterr().out and len((out / "sweep-2-of-27.jsonl").read_text().splitlines()) == 2
    )
    assert balance.main(["--merge", str(out), "--json", str(tmp_path / "merged.json")]) == 0
    merged = capsys.readouterr().out
    assert "2 games from" in merged and "slots/reload" in merged and "unavoidable hits (spec §18's audit): 0" in merged
    assert merged.rstrip().endswith(": 0 outside the rules")
    data = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    assert len(data["runs"]) == 2 and {row["bot"] for row in data["summary"]} <= set(balance.SWEEP_BOTS)
    with pytest.raises(SystemExit):
        balance.main(["--merge", str(tmp_path / "empty")])
    with pytest.raises(SystemExit):
        balance.main(["--sweep", "--shard", "4/3"])


# --- B-M2: the quality bar, the tiers, tapes and the audit from them, --plan, --staged (spec §18-§19, §23.3-§23.4) --


def test_targets_hold_spec_23_3s_rows_and_minimum_games(balance):
    """§23.3's rows as §27 and §27.2 changed them: strong for Q1 and Q7 (Endless) on Hard and for Q5, Q6, Q8 and Q9
    on Normal, the new row P (perfect wins), Q10 over Q1's and P's lives, F over every game of the gate, and the strong
    bot's Normal wins and the good bot's Hard ones reported, not judged."""
    rows = {target.row: target for target in balance.TARGETS}
    assert list(rows) == [
        "Q1",
        "Q1 Normal",
        "P",
        "Q2",
        "Q3",
        "Q4 Easy",
        "Q4 Normal",
        "Q4 Hard",
        "Q5",
        "Q6",
        "Q7",
        "Q8",
        "Q9",
        "Q10",
        "F",
    ]
    expected = {
        "Q1": (("strong",), "hard", "classic", (0.60, 0.75), 400),  # §27.2: Hard is the strong player's level
        "Q1 Normal": (("strong",), "normal", "classic", (0.90, 1.0), 400),  # informational, expected >= 90%
        "P": (("skilled",), "normal", "classic", (0.90, 1.0), 200),
        "Q2": (("sk_d18_p85",), "normal", "classic", (0.30, 0.45), 400),
        "Q3": (("novice",), "normal", "classic", (0.75, 1.0), 300),
        "Q4 Easy": (("sk_d18_p85",), "easy", "classic", (0.70, 0.85), 300),
        "Q4 Normal": (("sk_d18_p85",), "normal", "classic", (0.30, 0.45), 400),
        "Q4 Hard": (("sk_d18_p85",), "hard", "classic", (0.10, 0.20), 200),  # informational
        "Q5": (("strong",), "normal", "classic", (30, 45), 100),  # strong's Normal wins
        "Q6": (("strong",), "normal", "classic", (45, 90), 50),
        "Q7": (("strong",), "hard", "endless", (35, 50), 100),  # §27.2: with Q1, on Hard
        "Q8": (("strong", "sk_d18_p85"), "normal", "classic", (0, 0.10), 400),
        "Q9": (("strong", "sk_d18_p85"), "normal", "classic", (0, 0.30), 400),
        "Q10": (("strong", "skilled"), "", "classic", (0, 0), 600),  # Q1's lives on Hard, P's on Normal: Q10_NEED
    }
    for name, (bots, difficulty, mode, band, games) in expected.items():
        target = rows[name]
        assert (target.bots, target.difficulty, target.mode, target.band, target.games) == (
            bots,
            difficulty,
            mode,
            band,
            games,
        ), name
    assert rows["Q6"].boss == (90, 180) and rows["F"].band == (0, 0) and rows["F"].games == 2300
    assert [name for name, target in rows.items() if not target.judged] == ["Q1 Normal", "Q4 Hard"]
    assert balance.Q10_NEED == {("strong", "hard", "classic"): 400, ("skilled", "normal", "classic"): 200}
    assert balance.F_NEED == {
        ("strong", "hard", "classic"): 400,  # Q1
        ("strong", "normal", "classic"): 400,  # Q1 Normal (Q5, Q6, Q8, Q9 and I1 read it)
        ("skilled", "normal", "classic"): 200,  # P
        ("sk_d18_p85", "normal", "classic"): 400,  # Q2
        ("novice", "normal", "classic"): 300,  # Q3
        ("sk_d18_p85", "easy", "classic"): 300,  # Q4 Easy
        ("sk_d18_p85", "hard", "classic"): 200,  # Q4 Hard
        ("strong", "hard", "endless"): 100,  # Q7
    }  # every game of the gate
    # the lives budget: the middle of each judged win-rate band; informational rows have none
    assert balance.CENTRES == {
        ("strong", "hard"): 0.675,
        ("sk_d18_p85", "normal"): 0.375,
        ("sk_d18_p85", "easy"): 0.775,
    }


def test_the_tiers_play_spec_19_2s_seeds(balance):
    gate = balance.tier_jobs(balance.GATE)
    by_row = {}
    for row in balance.GATE:
        by_row[row.name] = [
            job for job in gate if (job["bot"], job["difficulty"], job["mode"]) == (row.bot, row.difficulty, row.mode)
        ]
    seeds = {name: [job["seed"] for job in jobs] for name, jobs in by_row.items()}
    assert seeds["Q1"] == seeds["Q1 Normal"] == seeds["Q2"] == list(range(5000, 5400))
    assert seeds["P"] == list(range(5000, 5200))
    assert seeds["Q3"] == seeds["Q4 Easy"] == list(range(5000, 5300)) and seeds["Q4 Hard"] == list(range(5000, 5200))
    assert seeds["Q7"] == list(range(7000, 7100)) and len(gate) == 400 + 400 + 200 + 400 + 300 + 300 + 200 + 100
    assert [(row.name, row.bot, row.difficulty) for row in balance.GATE if row.bot in ("strong", "skilled")] == [
        ("Q1", "strong", "hard"),  # §27.2: Q1 is judged on Hard...
        ("Q1 Normal", "strong", "normal"),  # ...and Q5, Q6, Q8, Q9 and I1 read the strong bot's Normal games
        ("P", "skilled", "normal"),
        ("Q7", "strong", "hard"),  # §27.2: Endless with Q1, on Hard; strong's Normal Endless is played by no tier
    ]
    assert all(job["until"] == 1 for job in by_row["Q3"]) and not any("until" in job for job in by_row["Q1"])
    assert {job["max_ticks"] for job in by_row["Q7"]} == {180 * 3600} and {
        job["max_ticks"] for job in by_row["Q1"] + by_row["Q1 Normal"] + by_row["P"]
    } == {60 * 3600}
    assert all(job.get("tape") for job in by_row["Q1"] + by_row["P"])  # Q10 audits both
    untaped = ("Q1 Normal", "Q2", "Q3", "Q4 Easy", "Q4 Hard", "Q7")
    assert not any(job.get("tape") for row in untaped for job in by_row[row])
    assert len({balance.job_key(job) for job in gate}) == len(gate)
    tune = balance.tier_jobs(balance.TUNE)
    assert len(tune) == 8 * 100 and {job["seed"] for job in tune if job["mode"] == "endless"} == set(range(6000, 6100))
    for jobs in (gate, tune):  # Endless only on Hard, to its 180-minute cap
        endless = [job for job in jobs if job["mode"] == "endless"]
        assert len(endless) == 100 and {(job["bot"], job["difficulty"], job["max_ticks"]) for job in endless} == {
            ("strong", "hard", 180 * 3600)
        }
    assert {job["seed"] for job in tune if job["mode"] == "classic"} == set(range(1000, 1100))
    strong = [job for job in tune if (job["bot"], job["mode"]) == ("strong", "classic")]
    for level in ("normal", "hard"):  # the tune tier plays the strong bot on both, 100 games each
        assert [job["seed"] for job in strong if job["difficulty"] == level] == list(range(1000, 1100))
    assert len(strong) == 200 and all(bool(job.get("tape")) == (job["difficulty"] == "hard") for job in strong)
    keyed = balance.tier_jobs(balance.GATE_INPUT)
    assert [job["input_mode"] for job in keyed[::200]] == ["kitty", "latched", "timed", "latched-ssh"]
    assert len(keyed) == 800 and {job["seed"] for job in keyed} == set(range(5000, 5200))
    assert all((job["bot"], job["difficulty"]) == ("strong", "normal") and not job.get("tape") for job in keyed)
    assert len(balance.tier_jobs(balance.GATE_INPUT, inputs=["latched"], seeds=3)) == 3


def test_wilson_intervals(balance):
    assert balance.wilson(0, 10) == pytest.approx((0.0, 0.2775), abs=1e-4)
    assert balance.wilson(5, 10) == pytest.approx((0.2366, 0.7634), abs=1e-4)
    low, high = balance.wilson(270, 400)
    assert low < 0.675 < high and high - low == pytest.approx(0.0912, abs=1e-3)  # about ±4.6 points at 400 games


def fake_run(bot="strong", difficulty="normal", mode="classic", seed=1, *, victory=False, **extra):
    """A recorded run with what the quality bar reads."""
    run = {"bot": bot, "difficulty": difficulty, "mode": mode, "input": "direct", "seed": seed, "kind": "game"}
    run |= {"victory": victory, "continues_used": 0, "cleared": 24 if victory else 5, "wave": 24 if victory else 6}
    run |= {"minutes": 33.0, "wave_play": {}, "lives_log": [], "kills": 100, "bomb_kills": 2}
    run |= {"powered_ticks": 10, "playing_ticks": 100}
    run["shot_log"] = {"min_w": 60, "min_flight": 60, "min_dive_w": 99, "min_mite_w": 80, "max_y": 12.0}
    run["shot_log"] |= {"bad_source": 0, "count": 50, "dives": 0, "mites": 0, "bad": []}
    run |= dict.fromkeys(("lives_lost", "shield_hits", "extra_lives", "life_awards", "pickups", "bombs_used"), 0)
    run |= dict.fromkeys(("combo_kills", "combo_total", "no_escape", "enemy_shots"), 0)
    run |= {"ticks": 1000, "shots": 10, "hits": 5, "end": "cap", "damage_waves": [], "picks": [], "no_escape_log": []}
    keys = ("deaths", "bombs", "no_escape", "minutes", "shots", "in_flight", "lives")
    run["acts"] = {key: [0, 0, 0, 0] for key in keys}
    return run | extra


def results(balance, runs):
    return {entry["row"]: entry for entry in balance.judge_targets(runs)}


def test_a_gate_row_is_judged_only_with_its_minimum_games(balance):
    wins = [fake_run(difficulty="hard", seed=seed, victory=seed < 270) for seed in range(400)]  # Q1 is on Hard
    q1 = results(balance, wins)["Q1"]
    assert q1["result"] == "PASS" and q1["value"] == 0.675 and q1["n"] == 400
    assert q1["interval"] == pytest.approx(list(balance.wilson(270, 400)))
    assert results(balance, wins[1:])["Q1"]["result"] == "info (n < 400)"  # 399 games: not judged
    losses = [fake_run(difficulty="hard", seed=seed, victory=seed < 220) for seed in range(400)]
    assert results(balance, losses)["Q1"]["result"] == "FAIL"
    normal = [fake_run(seed=seed, victory=seed < 270) for seed in range(400)]  # the strong bot on Normal
    assert results(balance, normal)["Q1"]["result"] == "info (no games)"  # §27.2: never Q1's games
    assert results(balance, normal[:12])["Q8"]["result"] == "info (n < 400)"
    judged = results(balance, wins + normal)
    assert judged["Q1"]["value"] == 0.675 and judged["Q1"]["n"] == 400
    assert judged["Q8"]["result"] == "info (no games)"  # no good-bot games: Q8 needs both bots
    assert judged["Q5"]["result"] == "PASS" and judged["Q5"]["value"] == 33.0 and judged["Q5"]["n"] == 270
    hard_wins = [run | {"minutes": 50.0} for run in wins]  # Q5 counts the strong bot's Normal wins only
    assert results(balance, hard_wins + normal)["Q5"]["value"] == 33.0
    assert results(balance, hard_wins)["Q5"]["result"] == "info (no games)"
    assert judged["Q10"]["result"] == "info (n < 600)" and judged["Q10"]["value"] == 0  # P's games are missing
    perfect = [fake_run("skilled", seed=seed, victory=seed < 181) for seed in range(200)]
    judged = results(balance, wins + perfect)
    assert judged["Q10"]["result"] == "PASS" and judged["Q10"]["n"] == 600  # no life lost, nothing unfair
    assert (judged["P"]["result"], judged["P"]["value"], judged["P"]["n"]) == ("PASS", 0.905, 200)
    assert judged["Q1"]["value"] == 0.675  # the perfect bot's games are not Q1's
    assert results(balance, wins + perfect[:-1])["P"]["result"] == "info (n < 200)"
    assert results(balance, wins + perfect[:-1])["Q10"]["result"] == "info (n < 600)"  # P short by one game
    beaten = [fake_run("skilled", seed=seed, victory=seed < 179) for seed in range(200)]
    assert results(balance, beaten)["P"]["result"] == "FAIL"  # 89.5%
    # Q10 and F are counts that must be 0: one unfair hit or one bad shot fails at any number of games.
    life = {"wave": 3, "tick": 900, "cause": "pellet", "audit": {"avoided": False}}
    few = [fake_run(difficulty="hard", lives_log=[life]), fake_run(difficulty="hard", seed=2)]
    assert (
        results(balance, few)["Q10"]["result"] == "FAIL"
        and "strong seed 1 wave 3 tick 900" in results(balance, few)["Q10"]["detail"]
    )
    on_perfect = results(balance, [fake_run("skilled", lives_log=[life])])["Q10"]  # P's lives are audited too
    assert on_perfect["result"] == "FAIL" and "perfect seed 1 wave 3 tick 900" in on_perfect["detail"]
    on_normal = results(balance, [fake_run(lives_log=[life])])["Q10"]  # §27.2: Q10 audits Q1's Hard games, not these
    assert on_normal["result"] == "info (no games)" and on_normal["value"] is None
    unaudited = [fake_run(difficulty="hard", lives_log=[{"wave": 3, "tick": 900, "cause": "pellet"}])]
    assert results(balance, unaudited)["Q10"]["result"] == "info (not audited)"
    bad = fake_run(bot="novice")
    bad["shot_log"] = bad["shot_log"] | {"min_w": 20}
    assert (
        results(balance, [bad])["F"]["result"] == "FAIL" and results(balance, few)["F"]["result"] == "info (n < 2300)"
    )


def test_q7_judges_the_strong_bots_endless_on_hard(balance, tmp_path, capsys):
    """Spec §27.2: Q7 is the strong bot's median wave in Endless on Hard, 35-50 over 100 games (seeds 7000-7099 in
    the gate). Its Normal Endless games are no row's (round 5: wave 115), and no tier plays them."""
    hard = [fake_run(difficulty="hard", mode="endless", seed=7000 + seed, wave=40) for seed in range(100)]
    q7 = results(balance, hard)["Q7"]
    assert (q7["value"], q7["n"], q7["result"]) == (40, 100, "PASS")
    normal = [fake_run(mode="endless", seed=7000 + seed, wave=115) for seed in range(100)]
    assert results(balance, normal)["Q7"]["result"] == "info (no games)"
    assert results(balance, hard + normal)["Q7"]["value"] == 40  # the Normal games are not counted
    assert results(balance, [run | {"wave": 60} for run in hard])["Q7"]["result"] == "FAIL"
    assert results(balance, hard[1:])["Q7"]["result"] == "info (n < 100)"
    lines = [run | {"job": f"strong/{run['difficulty']}/endless/direct/{run['seed']}"} for run in hard + normal]
    (tmp_path / "gate-1-of-1.jsonl").write_text(
        "".join(json.dumps(run | {"build": balance.build()}) + "\n" for run in lines), encoding="utf-8"
    )
    balance.main(["--merge", str(tmp_path)])
    assert re.search(r"Q7 +Endless on Hard, median wave +40 .* 100 +100  PASS", capsys.readouterr().out)


def test_the_audit_steps_at_each_bots_cadence(balance):
    """The audit's second plan starts at a decision point of the bot that lost the life: every 3 ticks for the
    perfect bot, 4 for the strong one, 6 for the good one (bots/params.cadence of its reaction delay)."""
    cadences = [balance.bot_cadence(bot) for bot in ("skilled", "strong", "sk_d18_p85", "novice")]
    assert cadences == [3, 4, 6, 5]


def test_gate_row_f_needs_every_row_s_games(balance):  # review BH6: Q1's 400 alone, or Q1 and Q7, are not F
    need = dict(balance.F_NEED)
    assert sum(need.values()) == 2300 and set(need) == set(balance.F_ROWS)
    assert need == {(row.bot, row.difficulty, row.mode): row.seeds for row in balance.GATE}  # every game of the gate
    games = {row: [fake_run(*row, seed=seed) for seed in range(count)] for row, count in need.items()}
    q1, q7 = games[("strong", "hard", "classic")], games[("strong", "hard", "endless")]
    assert results(balance, q1)["F"]["result"] == "info (n < 2300)"
    assert results(balance, q1 + q7)["F"]["result"] == "info (n < 2300)"
    every = [run for runs in games.values() for run in runs]
    assert results(balance, every)["F"]["result"] == "PASS" and results(balance, every)["F"]["n"] == 2300
    normal = games[("strong", "normal", "classic")]  # §27.2's informational row is checked too
    short = results(balance, [run for run in every if run not in normal])["F"]
    assert short["result"] == "info (n < 2300)" and "short: strong/normal/classic" in short["detail"]
    perfect = games[("skilled", "normal", "classic")]
    extra = [run for run in every if run not in perfect]
    extra += [fake_run(difficulty="hard", seed=1000 + seed) for seed in range(200)]
    short = results(balance, extra)["F"]  # 2,300 games, but P's 200 are Q1's instead
    assert short["result"] == "info (n < 2300)" and "short: skilled/normal/classic" in short["detail"]
    every[0]["shot_log"] = every[0]["shot_log"] | {"min_w": 20}
    assert results(balance, every[:1])["F"]["result"] == "FAIL"  # one bad shot still fails at any count


def test_the_strong_bot_on_normal_and_the_good_bot_on_hard_are_reported_not_judged(balance, tmp_path, capsys):
    """Spec §27.2: Normal is tuned for the good player and Hard for the strong one, so the strong bot's Normal wins
    (expected at least 90%) and the good bot's Hard ones (the old Q4 Hard band, 10-20%) are informational: the report
    prints their value, interval and expected band, and --check never fails on them."""
    normal = [fake_run(seed=seed, victory=seed < 200) for seed in range(400)]  # 50%
    hard = [fake_run("sk_d18_p85", "hard", seed=seed, victory=seed < 120) for seed in range(200)]  # 60%
    judged = results(balance, normal + hard)
    for row, (wins, games) in (("Q1 Normal", (200, 400)), ("Q4 Hard", (120, 200))):
        entry = judged[row]
        assert (entry["value"], entry["n"], entry["result"]) == (wins / games, games, "info (not judged)")
        assert entry["interval"] == pytest.approx(list(balance.wilson(wins, games)))
        assert "spec §27.2" in entry["detail"]
    assert "expected 90%-100%: outside" in judged["Q1 Normal"]["detail"]
    assert "expected 10%-20%: outside" in judged["Q4 Hard"]["detail"]
    calm = [fake_run("sk_d18_p85", "hard", seed=seed, victory=seed < 30) for seed in range(200)]  # 15%
    assert "expected 10%-20%: inside" in results(balance, calm)["Q4 Hard"]["detail"]
    few = results(balance, hard[:10])["Q4 Hard"]  # too few games to say: no inside or outside
    assert few["result"] == "info (not judged)" and "expected 10%-20%; reported" in few["detail"]
    assert results(balance, normal)["Q4 Hard"]["result"] == "info (no games)"
    assert balance.budget_rows(normal + hard)[0]["target"] is None  # no lives budget for them either
    # --merge --check: these rows far outside their bands, and nothing else judged, still exit 0
    lines = [run | {"job": f"{run['bot']}/{run['difficulty']}/classic/direct/{run['seed']}"} for run in normal + hard]
    (tmp_path / "tune-1-of-1.jsonl").write_text(
        "".join(json.dumps(run | {"build": balance.build()}) + "\n" for run in lines), encoding="utf-8"
    )
    assert balance.main(["--merge", str(tmp_path), "--check"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"Q1 Normal +strong wins on Normal +50\.0% .* 400 +400  info \(not judged\)", out)
    assert re.search(r"Q4 Hard +good wins on Hard +60\.0% .* 200 +200  info \(not judged\)", out)
    assert "FAIL" not in out


def test_q3_counts_wave_1_clears_of_games_stopped_there(balance):
    runs = [fake_run("novice", seed=seed, kind="until", cleared=int(seed < 240), wave=1) for seed in range(300)]
    q3 = results(balance, runs)["Q3"]
    assert (q3["value"], q3["n"], q3["result"]) == (0.8, 300, "PASS")
    run = balance.play("novice", seed=1000, until=1)
    assert run["kind"] == "until" and run["until"] == 1 and run["wave"] == 1 and run["end"] in ("cleared", "destroyed")
    assert run["cleared"] == (1 if run["end"] == "cleared" else 0)


def test_q6_judges_every_wave_and_prints_the_good_bots_beside(balance):
    play = {str(wave): (130 if balance.WAVES[wave - 1].boss else 60) * TICK_RATE for wave in range(1, 25)}
    strong = [fake_run(seed=seed, victory=True, wave_play=play) for seed in range(50)]
    good = [fake_run("sk_d18_p85", seed=seed, wave_play={"1": 100 * TICK_RATE}) for seed in range(3)]
    q6 = results(balance, strong + good)["Q6"]
    assert q6["result"] == "PASS" and "1: 60 (100)" in q6["detail"] and q6["band"] == [45, 90, 90, 180]
    assert results(balance, strong[:49])["Q6"]["result"].startswith("info (n < 50")
    slow = {**play, "3": 95 * TICK_RATE}
    assert results(balance, [fake_run(seed=seed, wave_play=slow) for seed in range(50)])["Q6"]["result"] == "FAIL"


def test_action_tapes_round_trip(balance):
    import random

    rng = random.Random(5)
    names = sorted(balance.BITS)
    tape = []
    for length in (1, 127, 128, 16384, 3, 20000):
        actions = tuple(name for name in names if rng.random() < 0.4)
        tape += [actions] * length
        tape += [(), ("fire",)]
    text = balance.encode_tape(tape)
    assert [set(step) for step in balance.decode_tape(text)] == [set(step) for step in tape]
    assert len(text) < 200 and balance.decode_tape(balance.encode_tape([])) == []


def test_tapes_audited_from_a_directory_match_the_inline_audit(balance, tmp_path, capsys):
    out = tmp_path / "games"
    arguments = ["--bots", "novice", "--seeds", "2", "--max-minutes", "3", "--tapes", "--out", str(out), "--jobs", "1"]
    assert balance.main(arguments) == 0
    assert balance.main(["--audit", str(out), "--jobs", "1"]) == 0
    printed = capsys.readouterr().out
    assert "audited 2 games (0 kept)" in printed and ": 0 unavoidable hits" in printed
    assert balance.main(["--audit", str(out), "--jobs", "1", "--check"]) == 0
    assert "audited 0 games (2 kept)" in capsys.readouterr().out  # resumed
    runs = balance.read_runs(out)
    assert len(runs) == 2 and all(run["tape"] and run["lives_log"] for run in runs)
    for run in runs:
        assert len(balance.decode_tape(run["tape"])) == run["ticks"]
        inline = balance.play("novice", "normal", "classic", run["seed"], 3 * 3600, audit=True)
        assert [entry["audit"] for entry in run["lives_log"]] == [entry["audit"] for entry in inline["lives_log"]]
    assert balance.main(["--merge", str(out), "--check"]) == 0
    merged = capsys.readouterr().out
    lost = sum(len(run["lives_log"]) for run in runs)
    assert f"fairness audit: {lost} of {lost} lives lost audited, 0 unavoidable" in merged


def test_shards_split_the_games_and_merge_from_several_directories(balance, tmp_path, capsys):
    common = ["--bots", "naive", "--seeds", "3", "--max-minutes", "0.05", "--jobs", "1"]
    for k in (1, 2):
        assert balance.main([*common, "--shard", f"{k}/2", "--out", str(tmp_path / "a")]) == 0
    assert balance.main([*common, "--start-seed", "2000", "--out", str(tmp_path / "b")]) == 0
    capsys.readouterr()
    assert sorted(path.name for path in (tmp_path / "a").iterdir()) == ["games-1-of-2.jsonl", "games-2-of-2.jsonl"]
    assert [run["seed"] for run in balance.read_runs(tmp_path / "a")] == [1000, 1002, 1001]
    assert balance.main(["--merge", str(tmp_path / "a"), str(tmp_path / "b"), "--json", str(tmp_path / "m.json")]) == 0
    assert "6 games from" in capsys.readouterr().out
    data = json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))
    assert sorted(run["seed"] for run in data["runs"]) == [1000, 1001, 1002, 2000, 2001, 2002]


def test_a_changed_build_replays_its_shard_and_never_merges_with_the_old_games(balance, tmp_path, capsys, monkeypatch):
    # review BH1: --out resumes only this build's games; --merge refuses two builds and --check fails another's
    out = tmp_path / "games"
    arguments = ["--bots", "naive", "--seeds", "2", "--max-minutes", "0.05", "--out", str(out), "--jobs", "1"]
    assert balance.main(arguments) == 0
    first = capsys.readouterr()
    path = out / "games-1-of-1.jsonl"
    old = balance.read_runs_file(path)
    assert [run["build"] for run in old] == [balance.build()] * 2 and f"build {balance.build()}: 2 games" in first.out
    assert f"build {balance.build()}: 2 games" in first.err  # stdout redirected (the Makefile's shards): shown anyway
    assert balance.main(arguments) == 0
    assert ": 0 games in" in capsys.readouterr().out  # the same build resumes
    monkeypatch.setattr(balance, "build", lambda: "edited")  # the tree changed
    assert balance.main(arguments) == 0
    printed = capsys.readouterr().out
    assert ": 2 games in" in printed and "2 of another build dropped" in printed
    assert [run["build"] for run in balance.read_runs_file(path)] == ["edited"] * 2  # rewritten: no stale line left
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "games-1-of-1.jsonl").write_text(
        "".join(json.dumps(run | {"job": f"old/{run['job']}"}) + "\n" for run in old)
    )
    with pytest.raises(SystemExit):
        balance.main(["--merge", str(out), str(stale)])  # two builds in one report
    assert "2 builds" in capsys.readouterr().err
    assert balance.main(["--merge", str(stale)]) == 0  # an old directory still reads, with a warning...
    assert "not this tree's edited: --check fails on them" in capsys.readouterr().out
    assert balance.main(["--merge", str(stale), "--check"]) == 1  # ...but never passes the check
    assert balance.main(["--merge", str(out), "--check"]) == 0
    capsys.readouterr()


def test_an_audit_attaches_only_to_the_game_and_build_it_audited(balance, tmp_path, capsys, monkeypatch):
    # review BH4: verdicts are matched to the lives by tick and build, and a game without its own audit is audited
    out = tmp_path / "games"
    arguments = ["--bots", "novice", "--seeds", "2", "--max-minutes", "3", "--tapes", "--out", str(out), "--jobs", "1"]
    assert balance.main(arguments) == 0
    assert balance.main(["--audit", str(out), "--jobs", "1"]) == 0
    capsys.readouterr()
    audits, games = out / "audit-1-of-1.jsonl", out / "games-1-of-1.jsonl"
    assert {entry["build"] for entry in balance.read_runs_file(audits)} == {balance.build()}
    played = balance.read_runs_file(games)
    replayed = [run | {"lives_log": [life | {"tick": life["tick"] + 7} for life in run["lives_log"]]} for run in played]
    games.write_text("".join(json.dumps(run) + "\n" for run in replayed), encoding="utf-8")  # other hits now
    assert not [life for run in balance.read_runs(out) for life in run["lives_log"] if "audit" in life]
    assert balance.main(["--audit", str(out), "--jobs", "1"]) == 0
    assert "audited 2 games (0 kept)" in capsys.readouterr().out  # the old verdicts are not theirs: audited again
    for run in balance.read_runs(out):  # the new audit (appended after the old one) is the one attached
        assert [life["audit"]["tick"] for life in run["lives_log"]] == [life["tick"] for life in run["lives_log"]]
    games.write_text("".join(json.dumps(run) + "\n" for run in played), encoding="utf-8")
    other = [entry | {"build": "other"} for entry in balance.read_runs_file(audits)]
    audits.write_text("".join(json.dumps(entry) + "\n" for entry in other), encoding="utf-8")
    assert not [life for run in balance.read_runs(out) for life in run["lives_log"] if "audit" in life]  # other build
    monkeypatch.setattr(balance, "build", lambda: "edited")
    assert balance.main(["--audit", str(out), "--jobs", "1", "--check"]) == 1  # these games aren't this build's
    assert "audited 0 games (0 kept, 2 of another build skipped)" in capsys.readouterr().out


def test_i1_without_its_direct_reference_is_reported_not_dropped(balance, tmp_path, capsys, monkeypatch):
    # review BH5: I1 compares the seeds both inputs played, with enough games on each side, and says when it can't
    direct = [fake_run(seed=seed, victory=seed % 2 == 0) for seed in range(5000, 5200)]
    keyed = [fake_run(seed=seed, victory=seed % 2 == 0, input="kitty") for seed in range(5000, 5200)]
    (alone,) = balance.acceptance(balance.matched_rows(keyed))
    assert alone["result"] == "FAIL" and "merge the gate's directory" in alone["rule"]
    (thin,) = balance.acceptance(balance.matched_rows(keyed + direct[:1]))  # one direct game is no reference
    assert thin["result"] == "info (n < 200)" and thin["games"] == 1
    (full,) = balance.acceptance(balance.matched_rows(keyed + direct))
    assert (full["result"], full["games"]) == ("PASS", 200)
    shifted = [fake_run(seed=seed, victory=True) for seed in range(5100, 5300)]  # direct on 5100-5299 only
    rows = balance.matched_rows(keyed + shifted)
    assert [(row["input"], row["games"]) for row in rows] == [("direct", 100), ("kitty", 100)]  # the 100 shared
    played = tmp_path / "keyed"
    arguments = ["--bots", "strong", "--input", "kitty", "--seeds", "2", "--max-minutes", "0.05", "--jobs", "1"]
    assert balance.main([*arguments, "--out", str(played)]) == 0
    assert balance.main(["--merge", str(played), "--check"]) == 0  # 2 keyed games, no direct ones: info, not judged
    assert "info (no direct reference)" in capsys.readouterr().out
    monkeypatch.setattr(balance, "ACCEPT_GAMES", 2)
    assert balance.main(["--merge", str(played), "--check"]) == 1  # a judged number of keyed games alone: I1 fails
    assert "FAIL (no direct games on these seeds; merge the gate's directory)" in capsys.readouterr().out
    (tmp_path / "empty").mkdir()
    for missing in (tmp_path / "nowhere", tmp_path / "empty"):
        with pytest.raises(balance.NoGames):
            balance.read_runs(missing)
        with pytest.raises(SystemExit) as stop:
            balance.main(["--merge", str(played), str(missing), "--check"])
        assert stop.value.code == 2
    capsys.readouterr()


def test_plan_sizes_the_gate_at_17_minutes_a_shard(balance, capsys):
    """Each row is sized from its own bot's profiled games on its own difficulty. With every bot at 6,000 ticks a
    second (spec §19.2's planning rate); the strong bot's whole game 35 minutes on Hard and 32 on Normal, the
    perfect bot's 29, the good bot's 27 on Normal, 37 on Easy and 33 on Hard; and Endless (Q7, on Hard since spec
    §27.2) at its 180-minute cap and the strong bot's Hard rate (the strong bot can last that long: 3 of round 5's 16
    Hard games did), the gate is 13.2 CPU-hours: 24 shards on 2 processes. The audit of Q1's (Hard) and P's taped games is sized from the timed audits
    of their own rows' profiled games."""

    def profiled(bot, minutes, rate, until=None, difficulty="normal"):
        ticks = round(minutes * 3600)
        run = {"bot": bot, "difficulty": difficulty, "ticks": ticks}
        run["profile"] = {"steps": ticks, "observe": 0.0, "bot": 0.0, "harness": 0.0, "engine": ticks / rate}
        return run | ({"until": until} if until is not None else {})

    runs = [profiled("strong", 32, 6000), profiled("strong", 35, 6000, difficulty="hard")]
    runs += [profiled("skilled", 29, 6000), profiled("sk_d18_p85", 27, 6000), profiled("novice", 1, 12000, until=1)]
    runs += [profiled("sk_d18_p85", 37, 6000, difficulty="easy"), profiled("sk_d18_p85", 33, 6000, difficulty="hard")]
    plan = balance.plan_tier(balance.tier_jobs(balance.GATE), 2, profiled=runs, probe={})
    rows = [8400, 7680, 3480, 6480, 90, 6660, 3960, 10800]  # Q1, Q1 Normal, P, Q2, Q3, Q4 Easy, Q4 Hard, Q7 (s)
    assert plan["cpu_seconds"] == pytest.approx(sum(rows)) and plan["shards"] == 24  # 47,550 s
    assert plan["rows"][("strong", "hard", "classic", "direct", None)]["ticks"] == 35 * 3600  # not 32 x 16.5/27
    assert plan["rows"][("sk_d18_p85", "hard", "classic", "direct", None)]["ticks"] == 33 * 3600
    endless = plan["rows"][("strong", "hard", "endless", "direct", None)]
    assert endless["ticks"] == 180 * 3600  # Q7's cap, not a share of a Campaign game
    assert "audit" not in plan  # no timed audits given: nothing to size it from
    text = balance.format_plan(plan, "--gate")
    assert "N = 24" in text and "--gate --shard K/24 --jobs 2" in text and "perfect " in text and "skilled" not in text
    # measured lengths (--lengths DIR): an Endless game of 60 minutes, a good bot's Hard one of 45 against 33 profiled
    lengths = {
        ("strong", "hard", "endless", "direct", None): 60 * 3600,
        ("sk_d18_p85", "hard", "classic", "direct", None): 45 * 3600,
    }
    measured = balance.plan_tier(balance.tier_jobs(balance.GATE), 2, profiled=runs, probe={}, lengths=lengths)
    assert measured["cpu_seconds"] == pytest.approx(sum(rows) - 10800 + 3600 - 3960 + 5400)
    assert measured["shards"] == 21  # 41,790 s on 2 processes, 17 minutes a shard
    # every strong Hard game of 12 lost a life (120 s of audit in all), 3 of the perfect bot's 12 (9 s)
    audits = {
        ("strong", "hard", None): {"games": 12, "taped": 12, "seconds": 120.0},
        ("skilled", "normal", None): {"games": 12, "taped": 3, "seconds": 9.0},
    }
    audited = balance.plan_tier(balance.tier_jobs(balance.GATE), 2, profiled=runs, probe={}, audits=audits)
    audit = audited["audit"]
    assert (audit["games"], audit["taped"]) == (600, pytest.approx(400 + 50))
    assert audit["cpu_seconds"] == pytest.approx(400 * 10 + 200 * 0.75) and audit["shards"] == 3  # 4,150 s
    text = balance.format_plan(audited, "--gate")
    assert "audit (gate row Q10): about 450 of the 600 taped games" in text and "N = 3" in text
    assert "--audit DIR --shard K/3 --jobs 2" in text
    on_normal = {("strong", "normal", None): audits[("strong", "hard", None)], **audits}  # Normal's games are not Q1's
    assert balance.plan_tier(balance.tier_jobs(balance.GATE), 2, profiled=runs, probe={}, audits=on_normal)[
        "audit"
    ] == (audit)
    keyed = balance.plan_tier(
        balance.tier_jobs(balance.GATE_INPUT), 2, profiled=runs[:1], probe=dict.fromkeys(balance.KEYED, 30.0)
    )
    rate = 1 / (1 / 6000 + 30e-6)
    assert keyed["cpu_seconds"] == pytest.approx(800 * 32 * 3600 / rate)
    with pytest.raises(ValueError, match="no profiled games of strong on hard"):
        balance.plan_tier(balance.tier_jobs(balance.GATE), 2, profiled=runs[:1], probe={})
    with pytest.raises(SystemExit):
        balance.main(["--sweep", "--plan"])


def fake_profile(jobs, workers, done=None):
    """run_all for --plan: each planned game as a profiled run of 32 minutes (1 to wave 1), at 6,000 ticks a second."""
    out = []
    for job in jobs:
        ticks = 3600 if job.get("until") else 32 * 3600
        run = {"bot": job["bot"], "difficulty": job["difficulty"], "ticks": ticks}
        run["profile"] = {"steps": ticks, "observe": 0.0, "bot": 0.0, "harness": 0.0, "engine": ticks / 6000}
        out.append(run | ({"until": job["until"]} if job.get("until") else {}))
    return out


def test_plan_profiles_12_seeds_of_each_row_the_tier_plays(balance, capsys, monkeypatch):
    """--plan profiles each row's bot on the row's own difficulty (spec §27.2 puts Q1's taped games on Hard), whole
    classic games, with tapes for the rows that keep them."""
    asked = []

    def run_all(jobs, workers, done=None):
        asked.extend(jobs)
        return fake_profile(jobs, workers)

    monkeypatch.setattr(balance, "run_all", run_all)
    assert balance.main(["--gate", "--plan", "--jobs", "2"]) == 0
    out = capsys.readouterr().out
    assert "gate tier, 2300 games" in out and "--gate --shard K/" in out and "audit (gate row Q10)" in out
    profiled = {(job["bot"], job["difficulty"], job.get("until")) for job in asked}
    assert profiled == {
        ("strong", "hard", None),
        ("strong", "normal", None),
        ("skilled", "normal", None),
        ("sk_d18_p85", "normal", None),
        ("sk_d18_p85", "easy", None),
        ("sk_d18_p85", "hard", None),
        ("novice", "normal", 1),
    }
    assert len(asked) == 7 * balance.PROFILE_SEEDS and all(job["profile"] for job in asked)
    assert {job["mode"] for job in asked} == {"classic"}  # Q7's Endless row is sized from strong's Hard games
    taped = {(job["bot"], job["difficulty"]) for job in asked if job.get("tape")}
    assert taped == {("strong", "hard"), ("skilled", "normal")}  # Q1's and P's rows: they size the audit too


def test_plan_lengths_come_from_the_games_of_an_earlier_run(balance, tmp_path, capsys, monkeypatch):
    """--plan --lengths DIR: each row's mean game length in DIR's shard files replaces the estimate (Endless's cap
    among them); rows DIR has no games of keep it."""

    def game(bot, difficulty, mode, seed, minutes, **extra):
        job = f"{bot}/{difficulty}/{mode}/direct/{seed}"
        return {"job": job, "bot": bot, "difficulty": difficulty, "mode": mode, "input": "direct", "seed": seed,
                "ticks": round(minutes * 3600), "kind": "game"} | extra  # fmt: skip

    shard = [game("strong", "hard", "endless", 6000, 50), game("strong", "hard", "endless", 6001, 70)]
    shard += [game("sk_d18_p85", "hard", "classic", 1000, 44), game("strong", "normal", "classic", 1000, 9, kind="act")]
    (tmp_path / "tune-1-of-1.jsonl").write_text("".join(json.dumps(run) + "\n" for run in shard), encoding="utf-8")
    lengths = balance.measured_lengths(balance.read_runs(tmp_path))
    assert lengths == {
        ("strong", "hard", "endless", "direct", None): 60 * 3600,  # the mean of 50 and 70 minutes
        ("sk_d18_p85", "hard", "classic", "direct", None): 44 * 3600,
    }  # a staged act is not a whole game

    monkeypatch.setattr(balance, "run_all", fake_profile)
    monkeypatch.setattr(balance, "audit_cost", lambda runs, workers: {})

    def minutes(argv):  # min/game of each row --plan prints, by (bot, difficulty, mode)
        assert balance.main(argv) == 0
        rows = [line.split() for line in capsys.readouterr().out.splitlines()]
        return {tuple(row[:3]): row[5] for row in rows if row and row[0] in ("strong", "sk_d18_p85")}

    planned = minutes(["--tune", "--plan", "--jobs", "2", "--lengths", str(tmp_path)])
    assert planned[("strong", "hard", "endless")] == "60.0" and planned[("sk_d18_p85", "hard", "classic")] == "44.0"
    assert planned[("strong", "normal", "classic")] == "32.0"  # no whole game of it in the directory: profiled
    assert planned[("strong", "hard", "classic")] == "32.0"  # Q1: its own profiled games on Hard
    assert planned[("sk_d18_p85", "easy", "classic")] == "32.0"  # the good bot's own on Easy, with no factor
    assert minutes(["--tune", "--plan", "--jobs", "2"])[("strong", "hard", "endless")] == "180.0"  # its cap
    with pytest.raises(SystemExit):
        balance.main(["--tune", "--lengths", str(tmp_path)])  # only with --plan
    capsys.readouterr()


def test_plan_times_the_audit_of_the_profiled_games_that_lost_a_life(balance):
    run = balance.play("novice", seed=1000, max_ticks=3 * 3600, tape=True, profile=True)  # it loses its lives
    calm = balance.play("novice", seed=1001, max_ticks=60, profile=True)  # no life lost: no tape, nothing to audit
    assert run["tape"] and run["lives_log"] and "tape" not in calm
    cost = balance.audit_cost([run, calm], 1)
    key = ("novice", "normal", None)  # per bot and difficulty (balance.profile_key)
    assert cost.keys() == {key} and (cost[key]["games"], cost[key]["taped"]) == (2, 1)
    assert cost[key]["seconds"] > 0
    jobs = [balance.job("novice", seed=seed, tape=True, max_ticks=3 * 3600) for seed in range(10)]
    plan = balance.plan_tier(jobs, 1, profiled=[run], probe={}, audits=cost)
    assert plan["audit"]["games"] == 10 and plan["audit"]["taped"] == pytest.approx(5)
    assert plan["audit"]["cpu_seconds"] == pytest.approx(10 * cost[key]["seconds"] / 2)


def test_profile_splits_a_tick_and_counts_steps(balance, capsys):
    run = balance.play("skilled", seed=1000, max_ticks=900, profile=True)
    cost = run["profile"]
    assert cost["steps"] == run["ticks"] == 900 and all(cost[key] > 0 for key in ("observe", "bot", "engine"))
    assert balance.main(["--bots", "skilled", "--seeds", "1", "--max-minutes", "0.1", "--profile", "--jobs", "1"]) == 0
    out = capsys.readouterr().out
    assert "Cost (µs a tick" in out and "ticks/s" in out


def test_staged_acts_start_with_their_loadout_and_stop_at_the_acts_end(balance):
    for act, first in ((2, 7), (3, 13), (4, 19)):
        game = balance.builder(Config("normal", "classic", 3), act=act)()
        loadout = balance.LOADOUTS[act]
        assert game.wave == first and game.player.lives == 3 and game.player.bombs == 1
        assert list(game.result()["picks"]) == loadout["picks"]
    assert balance.LOADOUTS[4]["picks"].count("RAIL ROUNDS") == 2
    run = balance.play("skilled", seed=1000, act=1, immortal=True)
    assert run["kind"] == "act" and run["end"] == "cleared" and run["wave"] == run["cleared"] == 6
    assert run["staged"] == {"wave": None, "immortal": True, "director": {}, "act": 1, "loadout": None}
    assert sorted(run["wave_play"]) == ["1", "2", "3", "4", "5", "6"]
    short = balance.play("sk_d18_p85", seed=1, act=3, max_ticks=300, director={"reload": 90})
    assert (
        short["staged"]["loadout"] == balance.LOADOUTS[3] and short["loadouts"]["3"][2] == balance.LOADOUTS[3]["picks"]
    )
    assert short["staged"]["director"] == {"reload": 90} and short["end"] == "cap"
    with pytest.raises(ValueError, match="staged game"):
        balance.builder(Config("normal", "classic", 3), immortal=True)


def test_median_loadouts_replace_the_defaults_once_whole_runs_exist(balance, tmp_path, capsys):
    picks = ["TRIPLE SHOT", "THRUSTERS"]
    runs = [fake_run(seed=seed, loadouts={"1": [3, 1, []], "2": [3 + seed % 3, seed % 2, picks]}) for seed in range(5)]
    runs.append(fake_run(seed=9, loadouts={"2": [5, 2, ["RAIL ROUNDS"]], "3": [4, 1, ["RAIL ROUNDS", "PIERCER"]]}))
    loadouts = balance.median_loadouts(runs)
    assert loadouts[2] == {"lives": 4, "bombs": 0, "picks": picks}
    assert loadouts[3] == {"lives": 4, "bombs": 1, "picks": ["RAIL ROUNDS", "PIERCER"]} and 4 not in loadouts
    with pytest.raises(SystemExit):  # --loadout DIR needs whole runs in DIR that reached the act
        balance.main(["--staged", "2", "--loadout", str(tmp_path), "--seeds", "1"])
    for arguments in (
        ["--immortal"],
        ["--director", "slots=4"],
        ["--staged", "5"],
        ["--staged", "2", "--input", "kitty"],
    ):
        with pytest.raises(SystemExit):
            balance.main([*arguments, "--seeds", "1"])
    capsys.readouterr()


def test_director_overrides_parse_by_field(balance):
    assert balance.director_pair("slots=5") == ("slots", 5) and balance.director_pair("reaction=0.04") == (
        "reaction",
        0.04,
    )
    assert isinstance(balance.director_pair("reload=44.0")[1], float)
    for value in ("slots", "nope=3", "slots=many"):
        with pytest.raises(Exception, match="KEY=VALUE|expected a number"):
            balance.director_pair(value)
    args = balance.parser().parse_args(["--staged", "2", "--director", "slots=4", "reload=50"])
    assert dict(args.director) == {"slots": 4, "reload": 50} and args.staged == 2


def test_the_acceptance_compares_keyed_runs_with_direct_on_the_same_seeds(balance):
    direct = [fake_run(seed=seed, victory=seed % 2 == 0) for seed in range(10)]  # 50% wins on seeds 0-9...
    direct += [fake_run(seed=seed, victory=True) for seed in range(10, 20)]  # ...and 100% on seeds 10-19
    keyed = [fake_run(seed=seed, victory=seed % 2 == 0, input="kitty") for seed in range(10)]
    rows = balance.matched_rows(direct + keyed)
    assert [(row["input"], row["games"], row["win_rate"]) for row in rows] == [("direct", 10, 0.5), ("kitty", 10, 0.5)]
    (result,) = balance.acceptance(rows, minimum=10)
    assert result["result"] == "PASS" and result["clears"] == 0
    (result,) = balance.acceptance(rows)
    assert result["result"] == "info (n < 200)" and "would pass" in result["rule"]


def test_merge_prints_each_acts_deaths_bombs_and_no_escape_against_the_budget(balance):
    acts = {"minutes": [7.0, 8.0, 0.0, 0.0], "deaths": [1, 2, 0, 0], "bombs": [1, 0, 0, 0]}
    moments = [{"act": 1, "bombed": True, "hit": False}, {"act": 2, "bombed": False, "hit": True}]
    lives = [{"seen": False}, {"seen": True}, {"seen": False}]
    runs = [
        fake_run(difficulty="hard", acts=acts, no_escape_log=moments, lives_log=lives),
        fake_run(difficulty="hard", seed=2, acts=acts | {"deaths": [0] * 4}),
    ]
    runs[1]["no_escape_log"], runs[1]["lives_log"] = [], []
    (row,) = balance.budget_rows(runs)
    assert row["reached"] == [2, 2, 0, 0] and row["deaths"] == [0.5, 1.0, 0.0, 0.0] and row["bombs"] == [1.0, 0.0, 0, 0]
    assert row["no_escape"] == [0.5, 0.5, 0.0, 0.0] and row["fatal"] == [1.0, 1.0, 0.0, 0.0]
    assert row["b"] == pytest.approx(2 / 4)  # one life lost after a no-escape moment, one bomb absorbed: of 4
    # Q1's budget is the strong bot's on Hard (spec §27.2), with Hard's lives: a cap of 4 and no Last Stand
    assert row["deaths_budget"] == pytest.approx([0.469, 0.845, 1.267, 1.736], abs=0.001) and row["target"] == 0.675
    text = balance.format_budget([row])
    assert "Lives budget" in text and "(no bombs, 67.5%)" in text and "(b 0.50, 67.5%)" in text
    (normal,) = balance.budget_rows([run | {"difficulty": "normal"} for run in runs])  # informational: no budget
    assert (
        normal["target"] is None
        and "deaths_budget" not in normal
        and "(no bombs" not in balance.format_budget([normal])
    )
