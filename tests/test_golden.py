"""Every scene draws exactly its golden frame, and the encoder shows that frame exactly through MiniVT.

The S1 goldens were written by the pre-refactor Painter (Phase 0's curses drawing code through a FakeScreen
adapter), so passing here proved that the CellBuffer facade draws identical screens; S10 regenerated them with the
role slots and the five tiers. Regenerate with `make frames`. The Arcade scenes (arcade-*) fly golden-arcade.json
(tools/make_golden.py); `tools/make_frames.py --campaign` leaves them.
"""

import subprocess
import sys
from functools import cache
from types import SimpleNamespace

import pytest

from terminal_invaders.term.encode import Encoder
from tests.support import frames
from tests.support.vt import MiniVT, assert_shows

CASES = [(index, tier) for index, scene in enumerate(frames.SCENES) for tier in scene[4]]
ARCADE_CASES = [(index, tier) for index, scene in enumerate(frames.ARCADE_SCENES) for tier in scene[4]]


@cache
def _build(build):
    return build()


def built(index: int):
    """Each scene's session, built once per build function: a staged scene shared by several sizes is built once."""
    return _build(frames.SCENES[index][3])


def case_id(case) -> str:
    index, tier = case
    return frames.filename(*frames.SCENES[index][:3], tier)[:-4]


def arcade_id(case) -> str:
    index, tier = case
    return frames.filename(*frames.ARCADE_SCENES[index][:3], tier)[:-4]


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_scene_draws_its_golden_frame(case):
    index, tier = case
    name, cols, rows, *_ = frames.SCENES[index]
    expected = (frames.GOLDEN / frames.filename(name, cols, rows, tier)).read_text()
    assert frames.golden(built(index), name, cols, rows, tier) == expected  # review a change, then make frames


def test_every_golden_frame_has_a_scene():
    expected = sorted(case_id(case) + ".txt" for case in CASES)
    campaign = [path.name for path in frames.GOLDEN.glob("*.txt") if not path.name.startswith("arcade-")]
    assert sorted(campaign) == expected


@pytest.mark.parametrize("case", ARCADE_CASES, ids=arcade_id)
def test_arcade_scene_draws_its_golden_frame(case):
    index, tier = case
    name, cols, rows, build, _ = frames.ARCADE_SCENES[index]
    expected = (frames.GOLDEN / frames.filename(name, cols, rows, tier)).read_text()
    assert frames.golden(build(), name, cols, rows, tier) == expected  # review a change, then make frames


def test_every_arcade_scene_has_its_golden_frame():
    expected = sorted(arcade_id(case) + ".txt" for case in ARCADE_CASES)
    assert sorted(path.name for path in frames.GOLDEN.glob("arcade-*.txt")) == expected


@pytest.mark.parametrize("case", ARCADE_CASES, ids=arcade_id)
def test_encoded_arcade_scene_shows_exactly(case):
    index, tier = case
    name, cols, rows, build, _ = frames.ARCADE_SCENES[index]
    buf, theme = frames.render(build(), cols, rows, tier)[0], frames.theme(tier)
    vt = MiniVT(cols, rows)
    vt.feed(b"\x1b[?7l")
    vt.feed(Encoder(theme, SimpleNamespace(utf8=True)).encode(None, buf))
    assert_shows(vt, buf, theme)


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_encoded_scene_shows_exactly(case):
    index, tier = case
    name, cols, rows, *_ = frames.SCENES[index]
    buf, theme = frames.render(built(index), cols, rows, tier, frames.CAPS.get(name))[0], frames.theme(tier)
    vt = MiniVT(cols, rows)
    vt.feed(b"\x1b[?7l")  # ENTER turns autowrap off
    vt.feed(Encoder(theme, SimpleNamespace(utf8=True)).encode(None, buf))
    assert_shows(vt, buf, theme)


def test_frame_text_names_every_style():
    index = next(n for n, scene in enumerate(frames.SCENES) if scene[0] == "boss")
    tier = ("16", "dark")
    text = frames.text("# header", frames.render(built(index), 80, 24, tier)[0], frames.theme(tier))
    rows = text.split("\n--- styles\n")[1].split("\n--- legend\n")[0].splitlines()
    legend = dict(line.split(" = ") for line in text.split("\n--- legend\n")[1].splitlines())
    roles = {letter: value.split("  sgr ")[0] for letter, value in legend.items()}
    assert len(rows) == 24 and all(len(row) == 82 for row in rows)
    assert {letter for row in rows for letter in row[1:-1]} == set(legend)
    assert {"good+bold", "boss+bold", "alien", "dim+dim", "shelter", "enemy_shot"} <= set(roles.values())  # WARDEN
    assert legend["."] == "default  sgr 0" and "boss+bold  sgr 0;1;91" in legend.values()


def test_make_frames_rewrites_the_goldens_and_removes_stale_ones(tmp_path):
    (tmp_path / "gone-80x24-mono-dark.txt").write_text("a scene that no longer exists")
    (tmp_path / "menu-80x24-mono-dark.txt").write_text("out of date")
    (tmp_path / "arcade-gone-80x24-mono-dark.txt").write_text("not the campaign's to remove")
    tool, out = frames.ROOT / "tools" / "make_frames.py", ["--out", tmp_path, "--campaign"]
    done = subprocess.run([sys.executable, tool, *out], capture_output=True, text=True, check=True)
    assert "removed gone-80x24-mono-dark.txt" in done.stdout and "wrote menu-80x24-mono-dark.txt" in done.stdout
    goldens = sorted(path for path in frames.GOLDEN.glob("*.txt") if not path.name.startswith("arcade-"))
    written = sorted(path.name for path in tmp_path.glob("*.txt"))
    assert written == sorted([path.name for path in goldens] + ["arcade-gone-80x24-mono-dark.txt"])
    assert all((tmp_path / path.name).read_text() == path.read_text() for path in goldens)


def test_make_frames_lists_each_change_with_its_sha256(tmp_path, monkeypatch, capsys):
    """Spec 12.2: every changed file listed with its old and new sha-256, a new one with its sha-256, a removed one
    with the one it had; --check writes nothing and fails while a file would change (frames.py's list stubbed)."""
    import hashlib
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_frames_for_test", frames.ROOT / "tools" / "make_frames.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    drawn = [("same-80x24-mono-dark.txt", "same\n"), ("changed-80x24-mono-dark.txt", "new\n")]
    drawn.append(("added-80x24-mono-dark.txt", "added\n"))
    monkeypatch.setattr(tool, "frames", lambda arcade=True: iter(drawn))
    for name, text in (("same", "same\n"), ("changed", "old\n"), ("gone", "gone\n")):
        (tmp_path / f"{name}-80x24-mono-dark.txt").write_text(text)

    def sha(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    before = {path.name: path.read_text() for path in tmp_path.iterdir()}
    assert tool.main(["--out", str(tmp_path), "--check"]) == 1
    assert {path.name: path.read_text() for path in tmp_path.iterdir()} == before  # --check wrote nothing
    out = capsys.readouterr().out
    assert f"would write changed-80x24-mono-dark.txt  {sha('old' + chr(10))} -> {sha('new' + chr(10))}" in out
    assert f"would write added-80x24-mono-dark.txt  new {sha('added' + chr(10))}" in out
    assert f"would remove gone-80x24-mono-dark.txt  {sha('gone' + chr(10))}" in out
    assert "1 changed, 1 new, 1 removed, 1 unchanged" in out and "same-80x24" not in out
    assert "wrote" not in out and "removed gone" not in out  # --check claims no write (TL-10)
    assert tool.main(["--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "wrote changed-80x24-mono-dark.txt" in out and "removed gone-80x24-mono-dark.txt" in out
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(name for name, _ in drawn)
    assert (tmp_path / "changed-80x24-mono-dark.txt").read_text() == "new\n"
    capsys.readouterr()
    assert tool.main(["--out", str(tmp_path), "--check"]) == 0
    assert "0 changed, 0 new, 0 removed, 3 unchanged" in capsys.readouterr().out


IDENTITY = frames.ROOT / "tools" / "golden_identity.py"
DRAW_PATCHED_BACK = """
import sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tools"]
import golden_identity
golden_identity.patch_back()
for name, text in golden_identity.rendered(set(sys.argv[3:])).items():
    (Path(sys.argv[2]) / name).write_text(text)
"""


def test_golden_identity_passes_phase_4s_frames_and_lists_the_cards(tmp_path):
    """Spec 12.2's identity proof on a small base folder drawn by this tree with the edits patched back: a non-card
    frame that differs fails, a card frame that differs is listed, a base file no scene draws fails."""
    base = tmp_path / "base"
    base.mkdir()
    names = ["records-80x24-mono-dark.txt", "career-80x24-mono-dark.txt", "menu-80x24-mono-dark.txt"]
    run = [sys.executable, "-B", "-c", DRAW_PATCHED_BACK, str(frames.ROOT), str(base), *names]
    subprocess.run(run, check=True, capture_output=True, text=True, timeout=120)
    assert sorted(path.name for path in base.iterdir()) == sorted(names)
    same = subprocess.run([sys.executable, "-B", IDENTITY, base], capture_output=True, text=True, timeout=120)
    assert same.returncode == 0, same.stdout + same.stderr
    assert "3 Phase 4 frames" in same.stdout and "3 identical, 0 card frames differ, 0 failures" in same.stdout
    for name in names[1:]:
        path = base / name
        path.write_text(path.read_text().replace("|", "!", 1))
    (base / "gone-80x24-mono-dark.txt").write_text("a scene this tree no longer draws\n")
    listed = tmp_path / "differ.txt"
    out = subprocess.run(
        [sys.executable, "-B", IDENTITY, base, "--list", listed], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 1, out.stdout
    assert "1 identical, 1 card frames differ, 2 failures" in out.stdout
    assert "card menu-80x24-mono-dark.txt" in out.stdout
    assert "FAIL differs outside the card list: career-80x24-mono-dark.txt" in out.stdout
    assert "FAIL no scene draws gone-80x24-mono-dark.txt any more" in out.stdout
    assert listed.read_text().split() == [
        "menu-80x24-mono-dark.txt", "career-80x24-mono-dark.txt", "gone-80x24-mono-dark.txt",
    ]  # fmt: skip
