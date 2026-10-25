"""Behavioral checks at the persistence/replay boundary."""

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from terminal_invaders import storage
from terminal_invaders.campaign.testing import staged
from terminal_invaders.cli import main, parser
from terminal_invaders.engine import Game
from terminal_invaders.model import RULESET, Config
from terminal_invaders.replay import Playback, Recorder
from terminal_invaders.rules import ARCADE, LEGACY, make_game
from terminal_invaders.storage import KNOWN, OLD_RULES, RULESETS, Placing, Profile, atomic_json, board_key


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
    (replay,) = Playback(path).tapes
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
    raw["tapes"][0]["config"]["seed"] += 1
    path.write_text(json.dumps(raw))
    assert main(["--verify-replay", str(path)]) == 1
    assert "MISMATCH" in capsys.readouterr().out


@pytest.mark.parametrize(
    "change",
    [
        {"pilot": 2},
        {"result": {"score": 0, "wave": 1, "kills": 0}},
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
    raw["tapes"][0].update(change)
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        Playback(path)


def test_empty_replay(tmp_path):
    path = tmp_path / "run.json"
    tape(path, 0)
    assert Playback(path).tapes[0].verify(Game(Config(seed=321)))


def test_replays_and_scores_carry_the_shared_ruleset(tmp_path):
    game = tape(tmp_path / "run.json")
    Profile(tmp_path / "profile.json").record(game.result(), game.config, "AAA")
    assert json.loads((tmp_path / "run.json").read_text())["ruleset"] == RULESET
    assert [row["ruleset"] for row in json.loads((tmp_path / "profile.json").read_text())["scores"]] == [RULESET]


def test_an_arcade_run_keeps_a_board_of_its_own(tmp_path, capsys):
    assert RULESETS == (RULESET, ARCADE)
    game = make_game(Config(mode="arcade"))
    for tick in range(600):
        game.step(("fire",) if tick % 30 == 0 else ())
    path = tmp_path / "profile.json"
    profile = Profile(path)
    profile.record(result(), Config(seed=1), "ALICE")
    profile.record(game.result() | {"score": 2500, "wave": 3}, game.config, "BOB")
    rows = json.loads(path.read_text())["scores"]
    assert {(row["name"], row["mode"], row["ruleset"]) for row in rows} == {
        ("ALICE", "classic", RULESET),
        ("BOB", "arcade", ARCADE),
    }
    loaded = Profile(path)
    assert loaded.best(Config(mode="arcade", seed=99)) == 2500 and loaded.best(Config(seed=1)) == 1000
    assert loaded.data["stats"]["runs"] == 2 and loaded.data["stats"]["ticks"] == 3000 + 600
    for arguments in (["--arcade"], ["--arcade", "--difficulty", "hard"]):  # the 1978 rules have one difficulty
        assert main(["--scores", *arguments, "--profile", str(path)]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "ARCADE" and lines[1].split() == ["1.", "BOB", "2500", "round", "3"] and len(lines) == 2
    assert main(["--scores", "--profile", str(path)]) == 0
    # spec 16.3: --scores names the classic mode CAMPAIGN
    assert capsys.readouterr().out.splitlines() == ["CAMPAIGN / NORMAL", " 1. ALICE           1000  wave 2"]


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
    (moved,) = tmp_path.glob("profile.json.corrupt-*")
    assert moved.read_text() == "{bad json"
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


# --- spec §17: campaign-2 rows rank and show; campaign-1 rows load, rank apart and fill the OLD RULES page ----------


def score_row(score, ruleset=RULESET, **changes):
    fields = dict(mode="classic", difficulty="normal", seed=7, score=score, wave=1, date="2026-09-17", victory=False)
    return {**fields, "name": "ACE", "ruleset": ruleset, **changes}


def test_the_known_rulesets():
    assert RULESET == "campaign-2" and tuple(LEGACY) == ("campaign-1",)
    assert KNOWN == (RULESET, ARCADE, "campaign-1")
    assert OLD_RULES == "Scores from the 12-wave campaign (old rules)."


def test_campaign_1_rows_load_and_rank_apart(tmp_path):
    path = tmp_path / "profile.json"
    old = [score_row(100 * n, "campaign-1") for n in range(1, 13)]
    old += [score_row(50, "campaign-1", mode="endless", difficulty="hard")]
    new = [score_row(n) for n in (70, 90, 80)]
    foreign = [score_row(99999, "campaign-0"), score_row(5, "campaign-3", extra={"lives": 9})]
    path.write_text(json.dumps({"version": 2, "scores": [*old, *new, *foreign]}))
    profile = Profile(path)
    assert len(profile.data["scores"]) == len(old) + len(new)  # campaign-1 rows load; unknown rulesets don't
    assert profile.best(Config()) == 90  # the HUD's HI reads campaign-2 only
    boards = profile.old_boards()
    assert list(boards) == [("classic", "normal"), ("endless", "hard")]  # MODES x DIFFICULTIES order
    assert [entry["score"] for entry in boards["classic", "normal"]] == [1200 - 100 * n for n in range(10)]
    assert [entry["score"] for entry in boards["endless", "hard"]] == [50]
    profile.record(result(), Config(seed=1), "BOB")
    saved = json.loads(path.read_text())["scores"]
    assert all(entry in saved for entry in foreign)  # kept verbatim
    kept_old = sorted(entry["score"] for entry in saved if entry["ruleset"] == "campaign-1")
    assert kept_old == [50, *range(300, 1300, 100)]  # campaign-1's board keeps its own ten
    assert sorted(entry["score"] for entry in saved if entry["ruleset"] == RULESET) == [70, 80, 90, 1000]
    reloaded = Profile(path)
    assert reloaded.best(Config()) == 1000 and reloaded.old_boards() == profile.old_boards()


def test_no_campaign_1_rows_means_no_old_rules_page(tmp_path):
    profile = Profile(tmp_path / "profile.json")
    profile.record(result(), Config(seed=1), "AAA")
    assert profile.old_boards() == {}


def test_daily_retention_counts_each_ruleset_apart(tmp_path):
    path = tmp_path / "profile.json"
    recent = [score_row(100 + n, mode="daily", seed=20261001 + n) for n in range(31)]
    older = [score_row(5000, "campaign-1", mode="daily", seed=20250101)]
    path.write_text(json.dumps({"version": 2, "scores": [*recent, *older]}))
    Profile(path).record(result(), Config(mode="daily", seed=20261101), "BOB")
    saved = json.loads(path.read_text())["scores"]
    assert older[0] in saved  # campaign-2's 31 newer dates don't push campaign-1's last daily out
    seeds = sorted(entry["seed"] for entry in saved if entry["ruleset"] == RULESET)
    assert len(seeds) == 31 and seeds[0] == 20261002  # campaign-2 keeps its 31 newest dates


def test_a_staged_game_cant_be_recorded(tmp_path):
    game = staged(Config(seed=3), 2)
    with pytest.raises(ValueError, match="a staged game can't be recorded"):
        Recorder(game.config).save(tmp_path / "run.json", game)
    assert not (tmp_path / "run.json").exists()


def test_an_unknown_ruleset_names_what_this_version_plays_and_verifies(tmp_path):
    path = tmp_path / "run.json"
    tape(path, 2)
    raw = json.loads(path.read_text())
    raw["ruleset"] = "campaign-0"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError) as caught:
        Playback(path)
    assert str(caught.value).endswith(f"this version plays {RULESET} and {ARCADE} and verifies campaign-1")


# Phase 5 (spec 4.3, 6.3): settings, the pilot name, where a run placed, its rank and its initials.
def run(score):
    return {**result(), "score": score}


def on_disk(path):
    return json.loads(path.read_text())


def writes_under_the_lock(profile, action):
    """What action wrote, and whether the profile held its lock at each write."""
    seen = []

    def spy(path, data):
        seen.append((data, profile._held))
        atomic_json(path, data)

    with patch.object(storage, "atomic_json", spy):
        returned = action()
    return returned, seen


def test_two_running_copies_keep_each_others_settings(tmp_path):
    path = tmp_path / "profile.json"
    first, second = Profile(path), Profile(path)
    assert first.update_settings({"palette": "cvd"})
    assert second.update_settings({"fps": 30})
    assert on_disk(path)["settings"] == {"palette": "cvd", "fps": 30}
    first.record(run(500), Config(), "AAA")  # a run's save carries the other copy's settings through
    assert second.update_settings({"palette": "default"})  # deletes only the key it changed
    assert on_disk(path)["settings"] == {"fps": 30} and on_disk(path)["stats"]["runs"] == 1
    assert first.settings == {"palette": "cvd", "fps": 30}  # as first last read the file (at its run)


def test_a_settings_save_re_reads_the_file_once_it_holds_the_lock(tmp_path):
    path = tmp_path / "profile.json"
    first, other = Profile(path), Profile(path)
    original = Profile._lock

    @contextmanager
    def other_copy_saves_first(self):
        if self is first and not self._held:
            other.update_settings({"theme": "light"}, name="ZED")
            other.record(run(900), Config(), "ZED")
        with original(self) as held:
            yield held

    with patch.object(Profile, "_lock", other_copy_saves_first):
        saved, seen = writes_under_the_lock(first, lambda: first.update_settings({"effects": "off"}))
    assert saved and [held for _, held in seen] == [False, False, True]  # the other copy's two saves, then first's
    assert on_disk(path)["settings"] == {"theme": "light", "effects": "off"}
    assert on_disk(path)["name"] == "ZED" and on_disk(path)["stats"]["runs"] == 1  # the other copy's run is kept


def test_update_settings_sets_the_name_and_saves_only_on_a_change(tmp_path):
    path = tmp_path / "profile.json"
    profile = Profile(path)
    assert profile.update_settings({}) and not path.exists()
    assert (
        profile.update_settings({}, name="ann") and on_disk(path)["name"] == "ANN" and "settings" not in on_disk(path)
    )
    for changes in ({}, {"palette": "default"}, {"palette": None}, {"tips_done": False}):
        assert writes_under_the_lock(profile, lambda: profile.update_settings(changes, name="ANN")) == (True, [])
    assert profile.update_settings({"sound": False, "keys": "vim"})
    assert on_disk(path)["settings"] == {"sound": False, "keys": "vim"}
    assert profile.update_settings({"sound": None})  # None deletes a key too
    assert on_disk(path)["settings"] == {"keys": "vim"}


def test_tips_done_is_written_once(tmp_path):
    path = tmp_path / "profile.json"
    profile = Profile(path)
    profile.record(run(100), Config(), "ACE")
    later = Profile(path)
    done = []
    for copy in (profile, profile, later, Profile(path)):
        done.append(writes_under_the_lock(copy, lambda: copy.update_settings({"tips_done": True})))
    assert [returned for returned, _ in done] == [True] * 4
    assert [len(seen) for _, seen in done] == [1, 0, 0, 0]  # the later copy re-reads it and has nothing to write
    assert on_disk(path)["settings"] == {"tips_done": True} and Profile(path).settings["tips_done"] is True


def test_a_duel_row_keeps_the_profiles_name(tmp_path):
    path = tmp_path / "profile.json"
    profile = Profile(path)
    profile.record(run(500), Config(), "ACE")
    profile.record(run(400), Config(), "P2", pilot=False)
    assert profile.last_placing.rank == 2
    saved = on_disk(path)
    assert saved["name"] == "ACE" and sorted(row["name"] for row in saved["scores"]) == ["ACE", "P2"]
    profile.record(run(300), Config(), "BEA")
    assert on_disk(path)["name"] == "BEA"  # a pilot's run sets it, as before


def test_last_placing_says_where_the_saved_run_landed(tmp_path):
    path = tmp_path / "profile.json"
    profile = Profile(path)
    assert profile.last_placing is None
    profile.record(run(500), Config(), "ACE")
    first = profile.last_placing
    assert first == Placing(1, ("campaign-2", "classic", "normal", None), first.date, None, 500)
    assert first.board == board_key(Config()) and first.date == on_disk(path)["scores"][0]["date"]
    profile.record(run(900), Config(), "ACE")
    assert (profile.last_placing.rank, profile.last_placing.best_before) == (1, 500)
    profile.record(run(100), Config(difficulty="hard"), "ACE")
    assert (profile.last_placing.rank, profile.last_placing.best_before) == (1, None)  # a board of its own
    profile.record(run(10), Config(mode="daily", seed=20261022), "ACE")
    assert profile.last_placing.board == ("campaign-2", "daily", "normal", 20261022)
    for score in range(1000, 1010):
        profile.record(run(score), Config(), "ACE")
    profile.record(run(1), Config(), "ACE")
    assert profile.last_placing.rank is None and profile.last_placing.best_before == 1009  # did not place


def test_last_placing_is_reset_on_entry_and_set_only_after_a_save_that_worked(tmp_path):
    path = tmp_path / "profile.json"
    profile = Profile(path)
    profile.record(run(500), Config(), "ACE")
    assert profile.last_placing.rank == 1
    with patch.object(storage, "atomic_json", side_effect=OSError("disk full")):
        assert profile.record(run(600), Config(), "ACE") == []
    assert profile.last_placing is None and "disk full" in profile.error
    profile.record(run(700), Config(), "ACE")
    assert profile.last_placing.rank == 1
    path.write_text('{"version": 3}')  # a newer build took the file over
    assert profile.record(run(800), Config(), "ACE") == [] and profile.last_placing is None
    newer = tmp_path / "newer.json"
    newer.write_text('{"version": 3}')
    read_only = Profile(newer)
    read_only.last_placing = Placing(1, board_key(Config()), "2026-10-22", None, 5)
    assert read_only.record(run(800), Config(), "ACE") == [] and read_only.last_placing is None


def test_rank_is_the_rows_place_as_this_session_last_read_the_board(tmp_path):
    path = tmp_path / "profile.json"
    first, second = Profile(path), Profile(path)
    first.record(run(500), Config(), "AAA")
    placing = first.last_placing
    assert first.rank(placing) == 1
    second.record(run(900), Config(), "ZED")
    assert first.rank(placing) == 1  # in memory: no file read
    assert first.update_settings({"palette": "cvd"})  # any save re-reads
    assert first.rank(placing) == 2 and second.rank(placing) == 2
    assert first.rank(None) is None and first.rank(tuple(placing)) is None
    assert first.rank(placing._replace(score=501)) is None and first.rank(placing._replace(date="x")) is None
    assert first.rank(placing._replace(board=board_key(Config(difficulty="hard")))) is None
    for score in range(1000, 1009):
        second.record(run(score), Config(), "ZED")
    first.update_settings({"palette": "default"})
    assert first.rank(placing) is None  # pruned: 11th


def test_rename_sets_the_rows_initials_under_the_lock_and_keeps_every_other_run(tmp_path):
    path = tmp_path / "profile.json"
    first, second = Profile(path), Profile(path)
    first.record(run(500), Config(), "AAA")
    placing = first.last_placing
    second.record(run(900), Config(), "ZED")
    second.update_settings({"fps": 30})
    renamed, seen = writes_under_the_lock(first, lambda: first.rename(placing, "ann"))
    assert renamed and [held for _, held in seen] == [True]
    saved = on_disk(path)
    assert sorted((row["score"], row["name"]) for row in saved["scores"]) == [(500, "ANN"), (900, "ZED")]
    assert saved["name"] == "ANN" and saved["stats"]["runs"] == 2 and saved["settings"] == {"fps": 30}
    assert first.rank(placing) == 2  # the identity is (board, date, score): the name is free to change
    assert first.rename(placing, "BOB", pilot=False)  # the second pilot of a duel
    assert on_disk(path)["name"] == "ANN" and {row["name"] for row in on_disk(path)["scores"]} == {"BOB", "ZED"}


def test_rename_does_nothing_when_the_row_is_gone(tmp_path):
    path = tmp_path / "profile.json"
    first, second = Profile(path), Profile(path)
    first.record(run(5), Config(), "AAA")
    placing = first.last_placing
    for score in range(100, 110):
        second.record(run(score), Config(), "ZED")  # another copy pruned it meanwhile
    before = path.read_bytes()
    assert writes_under_the_lock(first, lambda: first.rename(placing, "ANN")) == (False, [])
    assert path.read_bytes() == before and first.rank(placing) is None
    assert first.rename(None, "ANN") is False and first.rename(tuple(placing), "ANN") is False
    newer = tmp_path / "newer.json"
    newer.write_text('{"version": 3, "scores": []}')
    assert Profile(newer).rename(placing, "ANN") is False and newer.read_text() == '{"version": 3, "scores": []}'
