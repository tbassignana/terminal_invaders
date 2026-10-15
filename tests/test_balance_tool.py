"""The balance tool's bots play real capped games and its report keeps a stable shape."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from terminal_invaders.engine import Game
from terminal_invaders.model import CAMPAIGN_WAVES, RULESET

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "balance.py"
SUMMARY_KEYS = {
    "mode",
    "bot",
    "difficulty",
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
    first, second = (balance.play("sk_d18_p85", "hard", "classic", 7, 900) for _ in range(2))
    del first["wall"], second["wall"]
    assert first == second


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
    for name in ("sk_d9", "sk_d9_p101", "wizard"):
        with pytest.raises(ValueError):
            balance.bot_class(name)
    with pytest.raises(SystemExit):
        balance.main(["--bots", "wizard"])
    assert balance.checkpoints(CAMPAIGN_WAVES) == list(range(1, CAMPAIGN_WAVES + 1))
    assert balance.checkpoints(236) == [1, *range(20, 221, 20)]


def test_report_and_json_cover_each_bot_and_difficulty(balance, tmp_path, capsys):
    path = tmp_path / "out" / "runs.json"
    arguments = ["--bots", "novice,skilled", "--difficulty", "easy", "hard", "--seeds", "2", "--max-minutes", "0.05"]
    assert balance.main([*arguments, "--jobs", "1", "--json", str(path)]) == 0
    report = capsys.readouterr().out
    assert f"Ruleset {RULESET}, classic, seeds 1000-1001" in report
    for bot in ("novice", "skilled"):
        assert sum(line.startswith(bot) for line in report.splitlines()) == 4
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ruleset"] == RULESET and data["max_ticks"] == 180 and len(data["runs"]) == 8
    assert [(row["bot"], row["difficulty"]) for row in data["summary"]] == [
        ("novice", "easy"),
        ("novice", "hard"),
        ("skilled", "easy"),
        ("skilled", "hard"),
    ]
    for row in data["summary"]:
        assert row.keys() == SUMMARY_KEYS and row["games"] == 2 and row["ends"] == {"cap": 2}
        assert len(row["survival"]) == CAMPAIGN_WAVES and row["win_rate"] == 0
        assert all(0 <= row[key] <= 1 for key in ("accuracy", "bomb_kill_share", "powerup_uptime"))
