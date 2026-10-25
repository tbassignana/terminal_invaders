"""Replay format 2: one tape per pilot, strict headers, and golden runs that guard against engine drift."""

import json
from pathlib import Path

import pytest

from terminal_invaders import __version__
from terminal_invaders.cli import main
from terminal_invaders.model import RULESET, Config
from terminal_invaders.replay import MAX_TAPES, Playback, Recorder, Tape, save_tapes
from terminal_invaders.rules import ARCADE, make_game

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Final (ticks, score, wave, victory) of each committed campaign-2 tape (tools/make_golden.py: the skilled bot through
# tools/balance.py's façade); a change here means gameplay changed, and a gameplay change must bump RULESET (spec 17).
GOLDEN = {
    "golden-classic-normal.json": (72000, 112430, 16, False),
    "golden-endless-hard.json": (36000, 38960, 9, False),
    "golden-daily.json": (117340, 292810, 24, True),
}
# The digest of each tape's final state: everything DIGEST_FIELDS covers, the three random streams and the ruleset.
GOLDEN_DIGESTS = {
    "golden-classic-normal.json": "48f1144ed199958d19375e9606cda9be64c5d7a03c54c68b102e498b5ea94dcf",
    "golden-endless-hard.json": "51b277ded8e815aa5b615961c46acdd337a5a78b6f7a29a76b6684ac8837221e",
    "golden-daily.json": "b2659050fb947fe7369994efca7affb592807ffaa632546cd5b918fdcb27bef6",
}
# Final (ticks, score, round) of golden-arcade.json (tools/make_golden.py); a change here means the arcade game changed.
GOLDEN_ARCADE = (17986, 5070, 5)


def recorded(config, pilot, ticks):
    game, recorder = make_game(config), Recorder(config, pilot)
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
    assert tape.digest == game.digest() == GOLDEN_DIGESTS[name]  # a moved digest: bump RULESET (spec 17)
    assert tape.ruleset == RULESET and game.result()["victory"] == (name == "golden-daily.json")


def test_golden_classic_run_reaches_a_boss_and_verifies_from_the_cli(capsys):
    (tape,) = Playback(FIXTURES / "golden-classic-normal.json").tapes
    assert tape.config == Config("normal", "classic", 1978) and tape.result["wave"] >= 7  # past the WARDEN
    assert main(["--verify-replay", str(FIXTURES / "golden-classic-normal.json")]) == 0
    assert capsys.readouterr().out == "VERIFIED  pilot 1  72000 ticks  score 112430  wave 16\n"


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
    path = tmp_path / "late.json"  # golden-daily ends in victory; the other two stop at their caps while playing
    raw = json.loads((FIXTURES / "golden-daily.json").read_text())
    raw["tapes"][0]["ticks"] += 10
    raw["tapes"][0]["inputs"].append([117346, ["fire"]])
    path.write_text(json.dumps(raw))
    assert main(["--verify-replay", str(path)]) == 1
    assert capsys.readouterr().out.startswith("MISMATCH  pilot 1  117340 ticks")


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
        ValueError,
        match="recorded with ruleset arcade-1978 .engine 9.0.0.; this version plays campaign-2 and arcade-1978-1 and verifies campaign-1$",
    ):
        Playback(path)


def test_golden_arcade_replay_verifies_from_the_cli(capsys, monkeypatch):
    """The saved demo game ends in a game over by itself and prints VERIFIED (spec 8, gate G3.3). The game checked is
    the one the CLI simulates, so the tape runs once."""
    playback = Playback(FIXTURES / "golden-arcade.json")
    (tape,) = playback.tapes
    assert playback.ruleset == ARCADE and tape.config == Config("normal", "arcade", 1978)
    assert (tape.ticks, tape.result["score"], tape.result["wave"]) == GOLDEN_ARCADE
    games, simulate = [], Tape.simulate
    monkeypatch.setattr(Tape, "simulate", lambda self: games.append(simulate(self)) or games[-1])
    assert main(["--verify-replay", str(FIXTURES / "golden-arcade.json")]) == 0
    (game,) = games
    assert game.phase == "game_over" and (game.tick, game.score, game.wave) == GOLDEN_ARCADE
    expected = f"VERIFIED  pilot 1  {tape.ticks} ticks  score {game.score}  round {game.wave}\n"
    assert capsys.readouterr().out == expected


def test_an_arcade_game_round_trips_under_its_own_ruleset(tmp_path, capsys):
    path = tmp_path / "arcade.json"
    save_tapes(path, [recorded(Config(mode="arcade"), 1, 900)])
    raw = json.loads(path.read_text())
    assert raw["ruleset"] == ARCADE and raw["tapes"][0]["config"]["mode"] == "arcade"
    assert Playback(path).ruleset == ARCADE
    assert main(["--verify-replay", str(path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("VERIFIED  pilot 1  900 ticks  score ") and out.endswith("  round 1\n")  # rounds, not waves
    raw["tapes"][0]["inputs"] = raw["tapes"][0]["inputs"][::2]  # every other input: the program sees less fire
    path.write_text(json.dumps(raw))
    assert main(["--verify-replay", str(path)]) == 1
    assert capsys.readouterr().out.startswith("MISMATCH  pilot 1  900 ticks")


def test_a_replay_holds_one_ruleset_and_each_tape_must_belong_to_it(tmp_path):
    path = tmp_path / "mixed.json"
    with pytest.raises(ValueError, match="a replay holds the tapes of one ruleset"):
        save_tapes(path, [recorded(Config(seed=77), 1, 60), recorded(Config(mode="arcade"), 2, 60)])
    raw = two_pilots(path)
    raw["tapes"][1]["config"]["mode"] = "arcade"  # an arcade tape in a campaign file
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="invalid replay settings"):
        Playback(path)
    raw["tapes"][1]["config"]["mode"] = "classic"
    raw["ruleset"] = ARCADE  # campaign tapes in an arcade file
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="invalid replay settings"):
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
