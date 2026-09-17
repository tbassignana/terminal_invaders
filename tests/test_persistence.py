"""Behavioral checks at the persistence/replay boundary."""

import json
from unittest.mock import patch

import pytest

from terminal_invaders.cli import main, parser
from terminal_invaders.engine import Game
from terminal_invaders.model import Config
from terminal_invaders.replay import Playback, Recorder
from terminal_invaders.storage import Profile, atomic_json


def result(**changes):
    return dict(score=1000, wave=2, kills=30, shots=50, hits=28, best_combo=3, ticks=3000, victory=False, **changes)


def tape(path, ticks=420):
    game = Game(Config(seed=321))
    recording = Recorder(game.config)
    for tick in range(ticks):
        actions = ("autofire",) if tick == 90 else ("left",) if tick % 17 == 0 else ()
        recording.append(actions)
        game.step(actions)
    recording.save(path, game)
    return game


def test_record_roundtrip_and_headless_verification(tmp_path, capsys):
    path = tmp_path / "run.json"
    original = tape(path)
    replay = Playback(path)
    clone = Game(replay.config)
    for tick in range(replay.ticks):
        clone.step(replay.actions_at(tick))
    assert replay.verify(clone)
    assert clone.result() == original.result()
    assert main(["--verify-replay", str(path)]) == 0
    assert "VERIFIED" in capsys.readouterr().out


def test_tamper_detected(tmp_path, capsys):
    path = tmp_path / "run.json"
    tape(path)
    raw = json.loads(path.read_text())
    raw["config"]["seed"] += 1
    path.write_text(json.dumps(raw))
    assert main(["--verify-replay", str(path)]) == 1
    assert "MISMATCH" in capsys.readouterr().out


@pytest.mark.parametrize(
    "change",
    [
        {"version": 99},
        {"ticks": -1},
        {"ticks": True},
        {"ticks": 999999999},
        {"digest": "wrong"},
        {"inputs": [[0, ["quit"]]]},
        {"inputs": [[0, ["fire"]], [0, ["fire"]]]},
        {"inputs": [[9999999, ["fire"]]]},
        {"inputs": [[0, ["fire"] * 33]]},
        {"config": {"difficulty": "normal", "mode": "classic", "seed": []}},
    ],
)
def test_invalid_replays_rejected(tmp_path, change):
    path = tmp_path / "run.json"
    tape(path, 2)
    raw = json.loads(path.read_text())
    raw.update(change)
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        Playback(path)


def test_empty_replay(tmp_path):
    path = tmp_path / "run.json"
    tape(path, 0)
    assert Playback(path).verify(Game(Config(seed=321)))


def test_profile_roundtrip_and_board_separation(tmp_path):
    path = tmp_path / "profile.json"
    profile = Profile(path)
    config = Config(seed=1)
    assert "First contact" in profile.record(result(), config, "a\x1b[b")
    assert profile.best(config) == 1000
    assert profile.best(Config(difficulty="hard")) == 0
    assert profile.best(Config(mode="daily", seed=1)) == 0
    profile.record(result(), Config(mode="daily", seed=1), "BBB")
    loaded = Profile(path)
    assert loaded.data["stats"]["runs"] == 2
    assert loaded.data["stats"]["kills"] == 60
    assert loaded.data["scores"][0]["name"] == "AB"
    assert loaded.best(Config(mode="daily", seed=2)) == 0
    assert loaded.error is None


def test_simultaneous_profiles_merge_and_top_ten(tmp_path):
    path = tmp_path / "profile.json"
    first, second = Profile(path), Profile(path)
    for index in range(12):
        entry = result()
        entry["score"] = index
        (first if index % 2 else second).record(entry, Config(), "AAA")
    loaded = Profile(path)
    assert loaded.data["stats"]["runs"] == 12
    assert [r["score"] for r in loaded.data["scores"]] == list(range(11, 1, -1))


def test_corrupt_profile_preserved_on_save(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text("{bad json")
    profile = Profile(path)
    assert profile.error
    profile.record(result(), Config(), "AAA")
    assert path.with_suffix(".json.bak").read_text() == "{bad json"
    assert Profile(path).data["stats"]["runs"] == 1


def test_invalid_profile_fields_are_ignored(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"version": 1, "stats": {"runs": -99}, "scores": [None, {}], "achievements": 5}))
    profile = Profile(path)
    assert profile.data["scores"] == []
    assert profile.data["stats"]["runs"] == 0


def test_failed_atomic_write_keeps_original_and_removes_temp(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text("original")
    with patch("terminal_invaders.storage.os.fsync", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            atomic_json(path, {"new": True})
    assert path.read_text() == "original"
    assert list(tmp_path.iterdir()) == [path]


def test_failed_profile_save_does_not_crash_run(tmp_path):
    profile = Profile(tmp_path / "profile.json")
    with patch("terminal_invaders.storage.atomic_json", side_effect=OSError("read only")):
        profile.record(result(), Config(), "AAA")
    assert "read only" in profile.error
    assert profile.data["stats"]["runs"] == 1


@pytest.mark.parametrize(
    "arguments",
    [["--fps", "0"], ["--fps", "121"], ["--seed", "-1"], ["--fps", "foo"], ["--record", "a", "--replay", "b"]],
)
def test_invalid_cli_arguments(arguments):
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(arguments)
    assert exc.value.code == 2


def test_scores_do_not_require_terminal(tmp_path, capsys):
    assert main(["--scores", "--profile", str(tmp_path / "profile.json")]) == 0
    assert "No scores" in capsys.readouterr().out
