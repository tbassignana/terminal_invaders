"""The title demo's two tapes (spec 5.1): pinned like test_replay.py's goldens, cut by tools/make_attract.py, shipped
as package data and loaded with structural checks only."""

import importlib.util
import json
import logging
import shutil
import stat
import time
import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest

from terminal_invaders import tapes
from terminal_invaders.replay import FORMAT_VERSION, Playback, Tape
from terminal_invaders.rules import ARCADE, CAMPAIGN

ROOT = Path(__file__).resolve().parents[1]
DEMOS = ROOT / "terminal_invaders" / "demos"
FIXTURES = ROOT / "tests" / "fixtures"
_spec = importlib.util.spec_from_file_location("make_attract_tool", ROOT / "tools" / "make_attract.py")
make_attract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_attract)

# (ticks, score, wave, victory) at the cut and the digest of the game there. A change here means gameplay changed:
# bump the ruleset, regenerate the goldens (tools/make_golden.py), then these tapes (tools/make_attract.py).
PINS = {
    "campaign.json": ((5400, 3690, 2, False), "4b7df4976a0a86dc03e377a4f8548f44f65c704d3a090e6e7101bb448608f1a9"),
    "arcade.json": ((5400, 1160, 2, False), "d065ce405b611b1545d1f133327574caba2d8a37001bf3ea099f97b38561ed9e"),
}
SOURCES = {"campaign.json": "golden-classic-normal.json", "arcade.json": "golden-arcade.json"}


def packed(data: dict) -> bytes:
    """A replay file's bytes, as atomic_json writes them."""
    return (json.dumps(data, separators=(",", ":"), allow_nan=False) + "\n").encode()


@pytest.mark.parametrize("name", tapes.TAPES)
def test_each_tape_is_pinned(name):
    (pins, digest) = PINS[name]
    playback = Playback(DEMOS / name)
    (tape,) = playback.tapes
    assert (tape.ticks, tape.result["score"], tape.result["wave"], tape.result["victory"]) == pins
    assert tape.digest == digest
    assert playback.ruleset == (CAMPAIGN if name == "campaign.json" else ARCADE) and not playback.legacy
    assert tape.config == Playback(FIXTURES / SOURCES[name]).tapes[0].config  # seed 1978, normal
    raw = json.loads((DEMOS / name).read_text())
    assert raw["format"] == FORMAT_VERSION
    golden = json.loads((FIXTURES / SOURCES[name]).read_text())
    assert raw["engine"] == golden["engine"] == "2.0.0.dev0"  # the golden's, not the running __version__
    assert raw["tapes"][0]["inputs"] == [row for row in golden["tapes"][0]["inputs"] if row[0] < 5400]
    assert stat.S_IMODE((DEMOS / name).stat().st_mode) == 0o644  # package data, readable by all


def test_the_campaign_tape_plays_first_and_the_files_are_the_sizes_the_spec_gives():
    assert tapes.TAPES == ("campaign.json", "arcade.json") and tapes.FOLDER == "demos"
    assert sorted(path.name for path in DEMOS.iterdir()) == sorted(tapes.TAPES)
    assert round((DEMOS / "campaign.json").stat().st_size / 1000, 1) == 33.5
    assert round((DEMOS / "arcade.json").stat().st_size / 1000, 1) == 40.3


def test_make_attract_writes_the_shipped_files_byte_for_byte_and_again_the_same(tmp_path, capsys):
    """Run once as the tool (it writes, re-reads and verifies by simulation), then cut again in this process: both
    equal the shipped bytes, so the digests pinned above are the simulation's."""
    assert make_attract.main(["--out", str(tmp_path)]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed == [
        "campaign.json: 5400 ticks of golden-classic-normal.json, score 3690, wave 2, verified",
        "arcade.json: 5400 ticks of golden-arcade.json, score 1160, round 2, verified",
    ]
    for name, source in make_attract.CUTS.items():
        shipped = (DEMOS / name).read_bytes()
        assert (tmp_path / name).read_bytes() == shipped, name
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o644
        assert packed(make_attract.cut(FIXTURES / source)) == shipped, name  # the second run
    assert list(make_attract.CUTS) == list(tapes.TAPES) and make_attract.CUT == 5400


def test_make_attract_refuses_a_cut_past_the_goldens_end():
    with pytest.raises(ValueError, match="only 17986 ticks"):
        make_attract.cut(FIXTURES / "golden-arcade.json", 20_000)


def test_the_tapes_are_declared_package_data_under_demos():
    package_data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]
    assert package_data == {"terminal_invaders": ["demos/*.json"]}
    for name in tapes.TAPES:
        assert fnmatch(f"demos/{name}", package_data["terminal_invaders"][0])
    assert not (DEMOS / "__init__.py").exists()  # data, not a package: setuptools' find leaves it to package-data


def test_loading_is_structural_only_and_fast(monkeypatch):
    """No simulation at load time (spec 5.1): a loader call stays under 50 ms once the modules are imported."""
    tapes.attract_tapes()  # warm: imports and the first read
    monkeypatch.setattr(Tape, "simulate", lambda self: pytest.fail("a tape was simulated at load time"))
    monkeypatch.setattr(Tape, "verify", lambda self, game: pytest.fail("a tape was verified at load time"))
    best = float("inf")
    for _ in range(5):
        began = time.perf_counter()
        loaded = tapes.attract_tapes()
        best = min(best, time.perf_counter() - began)
    assert best < 0.050, best
    assert [playback.ruleset for playback in loaded] == [CAMPAIGN, ARCADE]
    assert [playback.tapes[0].digest for playback in loaded] == [digest for _, digest in PINS.values()]


def test_the_tapes_are_read_through_importlib_resources(monkeypatch, tmp_path):
    asked = []

    def files(package):
        asked.append(package)
        return tmp_path

    (tmp_path / "demos").mkdir()
    shutil.copy(DEMOS / "arcade.json", tmp_path / "demos" / "arcade.json")
    monkeypatch.setattr(tapes.resources, "files", files)
    loaded = tapes.attract_tapes()
    assert asked == ["terminal_invaders", "terminal_invaders"] and [p.ruleset for p in loaded] == [ARCADE]


def corrupt(kind: str, raw: dict) -> str | None:
    """A broken copy of the Campaign tape's text, by kind."""
    tape = raw["tapes"][0]
    if kind == "not json":
        return '{"format": 2, "ruleset": '
    if kind == "two pilots":
        raw["tapes"] = [tape, {**tape, "pilot": 2}]
    elif kind == "legacy ruleset":
        raw["ruleset"] = "campaign-1"
    elif kind == "unknown ruleset":
        raw["ruleset"] = "campaign-9"
    elif kind == "no ticks":
        tape.update(ticks=0, inputs=[])
    elif kind == "tick out of range":
        tape["inputs"].append([5400, ["fire"]])
    elif kind == "format 1":
        raw["format"] = 1
    elif kind == "not an object":
        return "[]"
    elif kind == "missing":
        return None
    return json.dumps(raw)


@pytest.mark.parametrize(
    "kind",
    [
        "not json",
        "two pilots",
        "legacy ruleset",
        "unknown ruleset",
        "no ticks",
        "tick out of range",
        "format 1",
        "not an object",
        "missing",
    ],
)
def test_a_corrupt_or_missing_tape_is_skipped_and_logged(monkeypatch, tmp_path, caplog, kind):
    (tmp_path / "demos").mkdir()
    shutil.copy(DEMOS / "arcade.json", tmp_path / "demos" / "arcade.json")
    text = corrupt(kind, json.loads((DEMOS / "campaign.json").read_text()))
    if text is not None:
        (tmp_path / "demos" / "campaign.json").write_text(text)
    monkeypatch.setattr(tapes.resources, "files", lambda package: tmp_path)
    with caplog.at_level(logging.DEBUG):
        loaded = tapes.attract_tapes()
    assert [playback.ruleset for playback in loaded] == [ARCADE]  # the good one still plays
    assert any("campaign.json skipped" in record.getMessage() for record in caplog.records)
    with pytest.raises((OSError, ValueError)):
        tapes.load_tape("campaign.json")


def test_no_tape_at_all_gives_an_empty_list(monkeypatch, tmp_path):
    monkeypatch.setattr(tapes.resources, "files", lambda package: tmp_path)
    assert tapes.attract_tapes() == []
