"""Profile storage never destroys data: newer, foreign, corrupt and legacy files all survive."""

import json
import logging
import sys
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from terminal_invaders.cli import main
from terminal_invaders.model import RULESET, WIDTH, Config
from terminal_invaders.storage import Profile, atomic_json, log_path, profile_path, state_dir


def result(score=1000):
    return dict(score=score, wave=2, kills=30, shots=50, hits=28, best_combo=3, ticks=3000, victory=False)


def row(score=100, **changes):
    fields = dict(mode="classic", difficulty="normal", seed=7, score=score, wave=1, date="2026-09-17", victory=False)
    return {**fields, "name": "ACE", **changes}


def listing(folder):
    return sorted(path.name for path in folder.iterdir())


def run_1_0(path, score):
    """What 1.0.0 does to its profile after a run: anything but version 1 is replaced, kept at most once as .bak."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        backup = path.with_suffix(".json.bak")
        if path.exists() and not backup.exists():
            backup.write_bytes(path.read_bytes())
        raw = {"version": 1, "scores": []}
    raw["scores"].append(row(score))
    atomic_json(path, raw)


def scores(path):
    return sorted(entry["score"] for entry in json.loads(path.read_text())["scores"])


def test_newer_profile_and_existing_backup_stay_byte_identical(tmp_path):
    path = tmp_path / "profile.json"
    newer = json.dumps({"version": 3, "name": "FUTURE", "scores": [row(500, ruleset=RULESET)], "hologram": [1]})
    path.write_text(newer)
    backup = path.with_suffix(".json.bak")
    backup.write_text("an older backup")
    before = listing(tmp_path)
    profile = Profile(path)
    assert profile.read_only
    assert "written by a newer version of Terminal Invaders; this session's scores will not be saved" in profile.error
    assert profile.data["name"] == "FUTURE" and profile.best(Config()) == 500
    assert profile.record(result(), Config(), "AAA") == []
    assert profile.save() is False
    assert path.read_text() == newer and backup.read_text() == "an older backup"
    assert listing(tmp_path) == before


def test_profile_upgraded_by_a_newer_build_mid_session_is_not_overwritten(tmp_path):
    path = tmp_path / "profile.json"
    profile = Profile(path)
    profile.record(result(), Config(), "AAA")
    newer = json.dumps({"version": 99, "scores": []})
    path.write_text(newer)
    assert profile.record(result(), Config(), "AAA") == []
    assert profile.read_only and "newer version" in profile.error
    assert path.read_text() == newer


def test_unknown_modes_difficulties_and_rulesets_survive_a_save(tmp_path):
    path = tmp_path / "profile.json"
    foreign = [
        row(99999, mode="survival", ruleset=RULESET),
        row(99998, difficulty="insane", ruleset=RULESET),
        *(row(90000 + n, ruleset="arcade-1978", extra={"lives": 3}) for n in range(15)),
        row(-5, ruleset=RULESET),
        row(77),
    ]
    known = [row(n, ruleset=RULESET) for n in range(12)]
    raw = {"version": 2, "scores": [*known, *foreign], "achievements": ["first_blood", "from_the_future"]}
    path.write_text(json.dumps(raw))
    profile = Profile(path)
    assert len(profile.data["scores"]) == 12
    assert profile.best(Config(seed=7)) == 11
    profile.record(result(5), Config(), "AAA")
    saved = json.loads(path.read_text())
    assert all(entry in saved["scores"] for entry in foreign)
    assert len([entry for entry in saved["scores"] if entry not in foreign]) == 10
    assert "from_the_future" in saved["achievements"]
    reloaded = Profile(path)
    assert len(reloaded.data["scores"]) == 10
    assert reloaded.data["achievements"] == ["first_blood"]
    assert max(entry["score"] for entry in reloaded.data["scores"]) == 11


def test_renamed_mode_in_a_version_one_profile_is_kept_with_its_ruleset(tmp_path):
    path = tmp_path / "profile.json"
    renamed = row(4321, mode="survival")
    path.write_text(json.dumps({"version": 1, "scores": [renamed, row(mode="gone", wave=float("nan"))]}))
    profile = Profile(path)
    assert profile.data["scores"] == []
    assert profile.record(result(), Config(), "AAA")
    saved = json.loads(path.read_text())["scores"]
    assert {**renamed, "ruleset": "campaign-1"} in saved
    assert len(saved) == 2


def test_keys_and_fields_from_other_builds_survive_a_save(tmp_path):
    path = tmp_path / "profile.json"
    linked = row(500, ruleset=RULESET, replay="runs/500.json")
    stats = dict(runs=4, kills=90, shots=100, hits=80, ticks=9000, best_combo=4, arcade_runs=12, bosses=3)
    settings = {"volume": 0.4, "palette": "amber"}
    raw = {"version": 2, "scores": [linked], "stats": stats, "achievements": ["arcade_ace"], "settings": settings}
    path.write_text(json.dumps({**raw, "broken": float("nan")}))
    Profile(path).record(result(111), Config(seed=7), "ACE")
    saved = json.loads(path.read_text())
    assert saved["settings"] == settings and "broken" not in saved
    assert saved["stats"] == {**stats, "runs": 5, "kills": 120, "shots": 150, "hits": 108, "ticks": 12000}
    assert linked in saved["scores"] and len(saved["scores"]) == 2
    assert saved["achievements"] == ["first_blood", "arcade_ace"]


def test_rows_past_two_thousand_survive_a_save(tmp_path):
    path = tmp_path / "profile.json"
    rows = [row(1000 + n, ruleset="arcade-1978", seed=20260000 + n // 10) for n in range(2050)]
    path.write_text(json.dumps({"version": 2, "scores": rows}))
    Profile(path).record(result(111), Config(), "AAA")
    saved = json.loads(path.read_text())["scores"]
    assert len(saved) == 2051 and all(entry in saved for entry in rows)


def test_corrupt_profile_is_moved_aside_on_the_first_save_not_lost(tmp_path):
    path = tmp_path / "profile.json"
    path.write_bytes(b"{bad json\xff")
    taken = tmp_path / "profile.json.corrupt-20261014T134550Z"
    taken.write_text("an earlier quarantine")
    profile = Profile(path)
    assert "moved aside, never overwritten, once a run is saved" in profile.error and not profile.read_only
    assert path.read_bytes() == b"{bad json\xff"
    clock = SimpleNamespace(now=lambda zone: datetime(2026, 10, 14, 13, 45, 50, tzinfo=zone))
    with patch("terminal_invaders.storage.datetime", clock):
        profile.record(result(), Config(), "AAA")
    moved = tmp_path / "profile.json.corrupt-20261014T134550Z.1"
    assert moved.read_bytes() == b"{bad json\xff"
    assert taken.read_text() == "an earlier quarantine"
    assert str(moved) in profile.error and profile.alert == f"Bad profile moved to {moved.name}"
    profile.record(result(), Config(), "AAA")
    assert str(moved) in profile.error
    assert Profile(path).data["stats"]["runs"] == 2


def test_commands_that_only_read_leave_an_unreadable_profile_in_place(tmp_path, capsys):
    path = tmp_path / "notes.txt"
    path.write_text("my notes\n")
    assert main(["--scores", "--profile", str(path)]) == 0
    assert "moved aside, never overwritten, once a run is saved" in capsys.readouterr().err
    assert listing(tmp_path) == ["notes.txt"] and path.read_text() == "my notes\n"


def test_profile_corrupted_mid_session_keeps_this_sessions_data(tmp_path):
    path = tmp_path / "profile.json"
    profile = Profile(path)
    profile.record(result(), Config(), "AAA")
    path.write_text("[truncated")
    profile.record(result(), Config(), "AAA")
    assert Profile(path).data["stats"]["runs"] == 2
    (moved,) = tmp_path.glob("profile.json.corrupt-*")
    assert moved.read_text() == "[truncated"


def test_profile_replaced_before_the_first_save_is_merged_not_moved(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text("{bad json")
    profile = Profile(path)
    atomic_json(path, {"version": 2, "name": "FRESH", "stats": {"runs": 4}})
    assert profile.save() is False and "changed on disk" in profile.error
    assert json.loads(path.read_text())["name"] == "FRESH"
    profile.record(result(), Config(), "AAA")
    assert profile.error is None and Profile(path).data["stats"]["runs"] == 5
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_profile_replaced_while_a_run_is_saved_is_reread_not_moved(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text("{bad json")
    profile = Profile(path)
    original = Profile._lock

    @contextmanager
    def writer_finishes_first(self):
        if not self._held:
            atomic_json(path, {"version": 2, "name": "FRESH", "stats": {"runs": 4}})
        with original(self) as held:
            yield held

    with patch.object(Profile, "_lock", writer_finishes_first):
        profile.record(result(), Config(), "AAA")
    assert profile.error is None and Profile(path).data["stats"]["runs"] == 5
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_unreadable_profile_is_left_alone(tmp_path):
    path = tmp_path / "profile.json"
    path.mkdir()
    profile = Profile(path)
    assert profile.read_only and "will not be saved" in profile.error
    assert profile.record(result(), Config(), "AAA") == []
    assert path.is_dir() and listing(tmp_path) == ["profile.json"]


def test_version_one_migration_keeps_every_score_and_writes_a_backup(tmp_path):
    path = tmp_path / "profile.json"
    scores = [
        row(300),
        row(200, difficulty="hard"),
        row(150, mode="endless", seed=99),
        *(row(n, mode="daily", seed=20260900 + n) for n in range(1, 6)),
    ]
    stats = dict(runs=8, kills=80, shots=100, hits=60, ticks=9000, best_combo=4)
    original = json.dumps({"version": 1, "name": "ACE", "scores": scores, "stats": stats, "achievements": ["veteran"]})
    path.write_text(original)
    older = tmp_path / "profile.v1.json.bak"
    older.write_text("from an earlier migration")
    profile = Profile(path)
    assert all(entry["ruleset"] == "campaign-1" for entry in profile.data["scores"])
    assert listing(tmp_path) == ["profile.json", "profile.v1.json.bak"]
    profile.record(result(), Config(seed=1), "ACE")
    profile.record(result(), Config(seed=2), "ACE")
    assert older.read_text() == "from an earlier migration"
    assert (tmp_path / "profile.v1.json.bak.1").read_text() == original
    assert not (tmp_path / "profile.v1.json.bak.2").exists()
    saved = json.loads(path.read_text())
    assert saved["version"] == 2 and saved["stats"]["runs"] == 10 and "veteran" in saved["achievements"]
    for entry in scores:
        assert {**entry, "ruleset": "campaign-1"} in saved["scores"]
    rulesets = [entry["ruleset"] for entry in saved["scores"]]
    assert sorted(rulesets) == ["campaign-1"] * len(scores) + [RULESET] * 2  # the two new runs are campaign-2's


def test_ruleset_separates_boards(tmp_path):
    path = tmp_path / "profile.json"
    profile = Profile(path)
    profile.record(result(800), Config(), "AAA")
    assert profile.best(Config()) == 800
    profile.data["scores"][0]["ruleset"] = "campaign-1"
    assert profile.best(Config()) == 0


def test_oversized_save_is_refused_and_the_file_kept(tmp_path):
    path = tmp_path / "profile.json"
    wide = [row(ruleset="arcade-1978", note="é" * 800) for _ in range(1000)]
    original = json.dumps({"version": 2, "scores": wide}, ensure_ascii=False)
    path.write_text(original, encoding="utf-8")
    profile = Profile(path)
    assert profile.error is None
    profile.record(result(), Config(), "AAA")
    assert "2 MB" in profile.error
    assert path.read_text(encoding="utf-8") == original


@pytest.fixture
def linux_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return tmp_path / "home"


@pytest.mark.parametrize("value", ["", "relative/data", "./data"])
def test_empty_or_relative_xdg_data_home_falls_back_to_default(linux_home, monkeypatch, value):
    monkeypatch.setenv("XDG_DATA_HOME", value)
    assert profile_path() == linux_home / ".local/share/terminal-invaders/profile-2.json"
    Profile().record(result(), Config(), "AAA")
    assert profile_path().exists()
    assert listing(linux_home.parent) == ["home"]


def test_absolute_xdg_directories_are_honoured(linux_home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert profile_path() == tmp_path / "data/terminal-invaders/profile-2.json"
    assert log_path() == tmp_path / "state/terminal-invaders/invaders.log"


@pytest.mark.parametrize("value", [None, "", "relative/state"])
def test_state_directory_defaults(linux_home, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_STATE_HOME", value)
    assert state_dir() == linux_home / ".local/state/terminal-invaders"


def test_macos_logs_live_in_library_logs(linux_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("XDG_STATE_HOME", "/ignored")
    assert log_path() == linux_home / "Library/Logs/terminal-invaders/invaders.log"


def test_debug_logs_to_the_state_directory_not_the_working_directory(linux_home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    with patch("terminal_invaders.cli.logging.basicConfig") as configure:
        assert main(["--debug", "--scores", "--profile", str(tmp_path / "profile.json")]) == 0
    expected = tmp_path / "state/terminal-invaders/invaders.log"
    configure.assert_called_once_with(filename=expected, level=logging.DEBUG)
    assert expected.parent.is_dir()
    assert not (tmp_path / "invaders.log").exists()


def test_corrupt_profile_that_cannot_be_moved_is_left_in_place(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text("{bad json")
    profile = Profile(path)
    with patch("terminal_invaders.storage.os.link", side_effect=PermissionError("read-only folder")):
        profile.record(result(), Config(), "AAA")
    assert "read-only folder" in profile.error and profile.alert == "Profile could not be saved; details shown on exit"
    assert path.read_text() == "{bad json"
    assert listing(tmp_path) == ["profile.json", "profile.lock"]


@pytest.mark.parametrize("content", [b"", b"[]", b'{"version": "2"}', b'{"version": 0}', b"\xff"])
def test_every_unusable_profile_is_moved_aside_byte_for_byte(tmp_path, content):
    path = tmp_path / "profile.json"
    path.write_bytes(content)
    profile = Profile(path)
    assert listing(tmp_path) == ["profile.json"] and path.read_bytes() == content
    assert profile.save() and json.loads(path.read_text())["version"] == 2
    (moved,) = tmp_path.glob("profile.json.corrupt-*")
    assert moved.read_bytes() == content and str(moved) in profile.error


NEWER_AND_LARGE = {"version": 3, "scores": [row(n, ruleset="campaign-3", ghost="x" * 60) for n in range(14000)]}


@pytest.mark.parametrize("content", [b" " * 2_000_001, json.dumps(NEWER_AND_LARGE).encode()], ids=["junk", "newer"])
def test_profile_larger_than_this_build_writes_is_left_alone(tmp_path, content):
    path = tmp_path / "profile.json"
    path.write_bytes(content)
    profile = Profile(path)
    assert len(content) > 2_000_000 and profile.read_only and "larger than 2 MB" in profile.error
    assert profile.record(result(), Config(), "AAA") == [] and profile.save() is False
    assert path.read_bytes() == content and listing(tmp_path) == ["profile.json"]


@pytest.mark.parametrize(
    "content, alert",
    [
        ('{"version": 3}', "Scores not saved: profile is from a newer version"),
        (" " * 2_000_001, "Scores not saved: profile is larger than 2 MB"),
        ("{bad json", "Profile unreadable: starting fresh, the old file is kept"),
    ],
    ids=["newer", "large", "corrupt"],
)
def test_in_game_alerts_fit_the_screen(tmp_path, content, alert):
    path = tmp_path / "profile-2.json"
    path.write_text(content)
    profile = Profile(path)
    assert profile.alert == alert and len(alert) <= WIDTH
    profile.save()
    assert len(profile.alert) <= WIDTH and (profile.read_only or profile.alert.startswith("Bad profile moved to"))


def test_long_profile_names_and_vanished_files_are_handled_at_save(tmp_path):
    path = tmp_path / "a-profile-with-a-rather-long-name.json"
    path.write_text("{bad json")
    profile = Profile(path)
    path.unlink()
    assert profile.save() and profile.error is None and not list(tmp_path.glob("*.corrupt-*"))
    path.write_text("{bad json")
    profile = Profile(path)
    assert profile.save() and profile.alert == "Bad profile moved aside; its new name is shown on exit"
    (moved,) = tmp_path.glob("*.corrupt-*")
    assert str(moved) in profile.error


def test_default_profile_imports_1_0_and_never_writes_its_file(linux_home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    folder = tmp_path / "data/terminal-invaders"
    folder.mkdir(parents=True)
    old = folder / "profile.json"
    run_1_0(old, 100)
    (folder / "profile.json.bak").write_text("a backup left by 1.0")
    profile = Profile()
    assert profile.path == folder / "profile-2.json" and profile.best(Config(seed=7)) == 0  # 1.0 played campaign-1
    assert [entry["score"] for entry in profile.old_boards()["classic", "normal"]] == [100]  # on the OLD RULES page
    # 1.0 and this build take turns: each keeps its own file, so 1.0 never meets a profile it would replace.
    for score in (111, 222, 333, 444):
        if score in (111, 333):
            Profile().record(result(score), Config(seed=7), "ACE")
        else:
            run_1_0(old, score)
    assert scores(folder / "profile-2.json") == [100, 111, 333]
    assert scores(old) == [100, 222, 444]
    assert (folder / "profile.json.bak").read_text() == "a backup left by 1.0"
    assert listing(folder) == ["profile-2.json", "profile-2.lock", "profile.json", "profile.json.bak"]


def test_unusable_1_0_profile_is_ignored_and_left_alone(linux_home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    folder = tmp_path / "data/terminal-invaders"
    folder.mkdir(parents=True)
    for content in ("{bad json", '{"version": 3}', " " * 2_000_001):
        (folder / "profile.json").write_text(content)
        profile = Profile()
        assert profile.error is None and not profile.read_only and profile.data["scores"] == []
        assert listing(folder) == ["profile.json"]


# Phase 5 (spec 4.3): a settings save is a save like a run's, under the same never-destroy-data rules.
NEWER = json.dumps({"version": 3, "name": "FUTURE", "settings": {"palette": "cvd"}, "hologram": [1]})


def test_settings_and_keys_from_other_builds_survive_every_kind_of_save(tmp_path):
    """The Phase 5 twin of test_keys_and_fields_from_other_builds_survive_a_save: a settings save, a run and a rename
    each keep unknown settings keys, invalid values, foreign top-level keys and every other field."""
    path = tmp_path / "profile.json"
    linked = row(500, ruleset=RULESET, replay="runs/500.json")
    stats = dict(runs=4, kills=90, shots=100, hits=80, ticks=9000, best_combo=4, arcade_runs=12, bosses=3)
    settings = {"volume": 0.4, "palette": "amber", "sound": 1, "fps": 30, "theme": "light", "hud": {"corner": [1, 2]}}
    raw = {"version": 2, "scores": [linked], "stats": stats, "achievements": ["arcade_ace"], "settings": settings}
    path.write_text(json.dumps({**raw, "hologram": [1], "broken": float("nan")}))
    profile = Profile(path)
    assert profile.settings == settings
    assert profile.update_settings({"theme": "dark", "fps": 60})  # fps back to its default: its key goes
    kept = {**{key: value for key, value in settings.items() if key != "fps"}, "theme": "dark"}
    saved = json.loads(path.read_text())
    assert saved == {**raw, "settings": kept, "name": "PILOT", "hologram": [1]}  # nothing else moved
    profile.record(result(111), Config(seed=7), "ACE")
    assert profile.rename(profile.last_placing, "BOB")
    saved = json.loads(path.read_text())
    assert saved["settings"] == kept and saved["hologram"] == [1] and "broken" not in saved
    assert linked in saved["scores"] and len(saved["scores"]) == 2 and saved["name"] == "BOB"
    assert saved["stats"] == {**stats, "runs": 5, "kills": 120, "shots": 150, "hits": 108, "ticks": 12000}
    assert saved["achievements"] == ["first_blood", "arcade_ace"]


@pytest.mark.parametrize(
    "content", [NEWER.encode(), b" " * 2_000_001, json.dumps(NEWER_AND_LARGE).encode()], ids=["newer", "junk", "large"]
)
def test_a_settings_save_on_a_read_only_profile_applies_for_the_session_and_writes_nothing(tmp_path, content):
    path = tmp_path / "profile.json"
    path.write_bytes(content)
    before = listing(tmp_path)
    profile = Profile(path)
    assert profile.read_only
    shown = profile.alert
    assert profile.update_settings({}) is True and profile.alert == shown  # nothing to save: no settings alert
    assert profile.update_settings({"effects": "calm"}, name="ANN") is False
    assert profile.alert == "Settings not saved: this profile is read-only" and len(profile.alert) <= WIDTH
    assert profile.settings["effects"] == "calm" and profile.data["name"] == "ANN"  # in force for this session
    assert path.read_bytes() == content and listing(tmp_path) == before


def test_a_profile_a_newer_build_replaces_mid_session_is_not_overwritten_by_a_settings_save(tmp_path):
    path = tmp_path / "profile.json"
    profile = Profile(path)
    assert profile.update_settings({"palette": "cvd"})
    path.write_text(NEWER)
    assert profile.update_settings({"palette": "contrast"}) is False
    assert profile.read_only and profile.alert == "Settings not saved: this profile is read-only"
    assert path.read_text() == NEWER and profile.settings["palette"] == "contrast"


def test_an_unreadable_profile_is_moved_aside_at_the_first_settings_save(tmp_path):
    path = tmp_path / "profile.json"
    path.write_bytes(b"{bad json\xff")
    profile = Profile(path)
    assert profile.settings == {} and path.read_bytes() == b"{bad json\xff"  # reading moves nothing
    assert profile.update_settings({"palette": "cvd"})
    (moved,) = tmp_path.glob("profile.json.corrupt-*")
    assert moved.read_bytes() == b"{bad json\xff" and profile.alert == f"Bad profile moved to {moved.name}"
    assert Profile(path).settings == {"palette": "cvd"}


def test_an_unreadable_profile_with_nothing_to_save_stays_where_it_is(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text("{bad json")
    assert Profile(path).update_settings({"palette": "default", "fps": 60}) is True
    assert path.read_text() == "{bad json" and not list(tmp_path.glob("*.corrupt-*"))


def test_a_version_one_profile_is_backed_up_before_its_first_settings_save(tmp_path):
    path = tmp_path / "profile.json"
    original = json.dumps({"version": 1, "name": "ACE", "scores": [row(300)], "achievements": ["veteran"]})
    path.write_text(original)
    profile = Profile(path)
    assert profile.update_settings({"effects": "calm"})
    assert profile.update_settings({"tips": "off"})
    assert (tmp_path / "profile.v1.json.bak").read_text() == original
    assert listing(tmp_path) == ["profile.json", "profile.lock", "profile.v1.json.bak"]
    saved = json.loads(path.read_text())
    assert saved["version"] == 2 and saved["settings"] == {"effects": "calm", "tips": "off"}
    assert saved["scores"] == [{**row(300), "ruleset": "campaign-1"}] and "veteran" in saved["achievements"]


def test_a_settings_save_that_would_pass_2_mb_is_refused_and_the_file_kept(tmp_path):
    path = tmp_path / "profile.json"
    wide = [row(ruleset="arcade-1978", note="é" * 800) for _ in range(1000)]
    original = json.dumps({"version": 2, "scores": wide}, ensure_ascii=False)
    path.write_text(original, encoding="utf-8")
    profile = Profile(path)
    assert profile.update_settings({"palette": "cvd"}) is False and "2 MB" in profile.error
    assert path.read_text(encoding="utf-8") == original


def test_a_settings_save_on_the_default_profile_never_writes_1_0s_file(linux_home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    folder = tmp_path / "data/terminal-invaders"
    folder.mkdir(parents=True)
    old = folder / "profile.json"
    run_1_0(old, 100)
    before = old.read_bytes()
    profile = Profile()
    assert profile.update_settings({"palette": "cvd"})
    assert old.read_bytes() == before and Profile().settings == {"palette": "cvd"}
    assert [entry["score"] for entry in Profile().old_boards()["classic", "normal"]] == [100]
    assert listing(folder) == ["profile-2.json", "profile-2.lock", "profile.json"]
