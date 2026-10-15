"""Replay format 2: one tape per pilot, strict headers, and golden runs that guard against engine drift."""

import json
from pathlib import Path

import pytest

from terminal_invaders import __version__
from terminal_invaders.cli import main
from terminal_invaders.engine import Game
from terminal_invaders.model import RULESET, Config
from terminal_invaders.replay import MAX_TAPES, Playback, Recorder, save_tapes

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Final (ticks, score, wave, victory) of each committed tape; a change here means gameplay changed.
GOLDEN = {
    "golden-classic-normal.json": (7596, 21050, 8, False),
    "golden-endless-hard.json": (7116, 19470, 8, False),
    "golden-daily.json": (7758, 60250, 12, True),
}


def recorded(config, pilot, ticks):
    game, recorder = Game(config), Recorder(config, pilot)
    for tick in range(ticks):
        actions = ("fire", "left" if (tick // 40) % 2 else "right") if tick % 3 else ()
        recorder.append(actions)
        game.step(actions)
    return recorder.tape(game)


def two_pilots(path):
    config = Config(seed=77)
    save_tapes(path, [recorded(config, 1, 600), recorded(config, 2, 300)])
    return json.loads(path.read_text())


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_golden_replays_verify_with_exact_results(name):
    (tape,) = Playback(FIXTURES / name).tapes
    assert (tape.ticks, tape.result["score"], tape.result["wave"], tape.result["victory"]) == GOLDEN[name]
    game = tape.simulate()
    assert tape.verify(game)
    assert (game.tick, game.score, game.wave, game.phase == "victory") == GOLDEN[name]


def test_golden_classic_run_reaches_a_boss_and_verifies_from_the_cli(capsys):
    (tape,) = Playback(FIXTURES / "golden-classic-normal.json").tapes
    assert tape.config == Config("normal", "classic", 1978) and tape.result["wave"] >= 4
    assert main(["--verify-replay", str(FIXTURES / "golden-classic-normal.json")]) == 0
    assert capsys.readouterr().out == "VERIFIED  pilot 1  7596 ticks  score 21050  wave 8\n"


def test_file_holds_every_pilot_and_verifies_each_tape(tmp_path, capsys):
    path = tmp_path / "duel.json"
    raw = two_pilots(path)
    assert {key: raw[key] for key in ("format", "ruleset", "engine")} == {
        "format": 2,
        "ruleset": RULESET,
        "engine": __version__,
    }
    assert [tape["pilot"] for tape in raw["tapes"]] == [1, 2]
    assert set(raw["tapes"][0]["result"]) == {"score", "wave", "kills", "victory"}
    assert main(["--verify-replay", str(path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("  ")[:3] for line in lines] == [
        ["VERIFIED", "pilot 1", "600 ticks"],
        ["VERIFIED", "pilot 2", "300 ticks"],
    ]


@pytest.mark.parametrize("field", ["inputs", "result", "config"])
def test_one_tampered_tape_fails_only_its_own_line(tmp_path, capsys, field):
    path = tmp_path / "duel.json"
    raw = two_pilots(path)
    second = raw["tapes"][1]
    if field == "inputs":
        second["inputs"] = second["inputs"][1:]
    elif field == "result":
        second["result"]["score"] += 10
    else:
        second["config"]["seed"] += 1
    path.write_text(json.dumps(raw))
    assert main(["--verify-replay", str(path)]) == 1
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == ["VERIFIED", "MISMATCH"]


def test_inputs_after_the_run_ended_fail_verification(tmp_path, capsys):
    path = tmp_path / "late.json"
    raw = json.loads((FIXTURES / "golden-classic-normal.json").read_text())
    raw["tapes"][0]["ticks"] += 10
    raw["tapes"][0]["inputs"].append([7600, ["fire"]])
    path.write_text(json.dumps(raw))
    assert main(["--verify-replay", str(path)]) == 1
    assert capsys.readouterr().out.startswith("MISMATCH  pilot 1  7596 ticks")


def test_version_one_and_foreign_rulesets_have_specific_errors(tmp_path, capsys):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": 1, "config": {}, "ticks": 0, "inputs": [], "digest": "0" * 64}))
    with pytest.raises(ValueError, match="version 1 replays came from an older engine and cannot be verified"):
        Playback(path)
    assert main(["--verify-replay", str(path)]) == 1
    assert "version 1 replays came from an older engine" in capsys.readouterr().err
    raw = two_pilots(path)
    raw.update(ruleset="arcade-1978", engine="9.0.0")
    path.write_text(json.dumps(raw))
    with pytest.raises(
        ValueError, match="recorded with ruleset arcade-1978 .engine 9.0.0.; this version plays campaign-1"
    ):
        Playback(path)


@pytest.mark.parametrize(
    "change",
    [
        {"format": 3},
        {"format": "2"},
        {"format": True},
        {"version": 2},
        {"engine": "1.0\x1b[2J"},
        {"engine": 1},
        {"ruleset": ""},
        {"ruleset": None},
        {"tapes": []},
        {"tapes": {}},
        {"tapes": "tape"},
        {"tapes": [None]},
        {"extra": True},
    ],
)
def test_invalid_headers_rejected(tmp_path, change):
    path = tmp_path / "duel.json"
    raw = two_pilots(path)
    raw.update(change)
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        Playback(path)


def test_pilots_must_be_numbered_in_order_and_bounded(tmp_path):
    path = tmp_path / "duel.json"
    raw = two_pilots(path)
    for tapes in (raw["tapes"][::-1], [raw["tapes"][1]], raw["tapes"][:1] * (MAX_TAPES + 1)):
        path.write_text(json.dumps({**raw, "tapes": tapes}))
        with pytest.raises(ValueError):
            Playback(path)
    raw["tapes"][1]["result"]["victory"] = 0
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="invalid replay result"):
        Playback(path)
