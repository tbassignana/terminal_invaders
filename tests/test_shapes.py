"""The mono shape gate (Phase 5 spec 3.7), run through tools/palette_check.py: every S pair a player must tell apart
by shape alone stays apart at k = 1, 2, 3, 4 and 6; the burst glyphs are drawn nowhere else; Phase 4's px2 player shot
fails the gate (the mutant); the art keys validate.py asks for are all still drawn."""

import importlib.util
import sys
from pathlib import Path

import pytest

from terminal_invaders import fx, sprites
from tests.support import phase4_art

TOOL = Path(__file__).resolve().parents[1] / "tools" / "palette_check.py"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("palette_check_shapes", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[spec.name]


def test_the_s_pairs(gate):
    pairs = gate.shape_pairs()
    assert len(pairs) == len(set(pairs)) == 77
    enemy = [kind for kind in sprites.SHOTS if kind != "player"]
    for shot in (*enemy, "beam_live"):
        assert (shot, "player") in pairs and (shot, "pickup") in pairs
    assert all(("beam_warn", shot) in pairs for shot in (*enemy, "player"))
    assert all((shot, tile) in pairs for shot in enemy for tile in ("solid", "cracked"))
    assert ("diving", "diver") in pairs and ("mite_diving", "mite") in pairs
    for part in sprites.PARTS:
        frames = [part + suffix for suffix in sprites.PART_FRAMES]
        assert sum(a in frames and b in frames for a, b in pairs) == 6  # each part's four frames, pairwise


@pytest.mark.parametrize("k", [2, 3, 4, 6])
def test_every_pair_is_two_cells_apart_in_mono(gate, k):
    """Each frame of each pair packed alone through the mono half-block packer, moving sprites at both pixel phases:
    the trimmed footprints differ, or 2 cells or more do. Boss turret frames are reported, not gated."""
    figures = gate.shape_figures(k)
    gated = {pair: n for pair, n in figures.items() if not gate.reported_shape(*pair)}
    assert all(n is None or n >= 2 for n in gated.values()), {pair: n for pair, n in gated.items() if n is not None}
    assert min(n for n in gated.values() if n is not None) == {2: 2, 3: 2, 4: 4, 6: 4}[k]


def test_turret_frames_are_reported(gate):
    """Spec 3.7: boss turret frames that differ by 1 cell at k = 2-3 are reported (they are also telegraphed)."""
    lines = []
    assert gate.shape_gate(lines.append) == []
    assert "reported warden.turret/warden.turret_charge 1" in lines[0]
    assert "reported warden.turret/warden.turret_cracked 1" in lines[1]
    assert lines[2].endswith("reported none") and lines[3].endswith("reported none")


def test_k1_glyphs_differ_but_for_the_allowlist(gate):
    failed, allowed = gate.glyph_failures()
    assert failed == []
    assert allowed == ["k=1 needle/cracked: both draw ':'", "k=1 mite_diving/mite: both draw 'w'"]
    assert set(gate.SHAPE_ALLOW) == {("needle", "cracked"), ("mite_diving", "mite")}
    assert all(gate.SHAPE_ALLOW.values())  # each with its reason


def test_the_burst_glyphs_are_drawn_nowhere_else(gate):
    burst = {frame.strip() for frames in fx.BURST.values() for frame in frames}
    s_glyphs = {glyph for pair in gate.shape_pairs() for key in pair for glyph in gate.glyph_frames(key)}
    assert not burst & s_glyphs and not burst & gate.drawn_glyphs()
    chars = {char for frame in burst for char in frame if char != " "}
    assert chars == {"&", ";", ","} and not chars & set("".join(gate.drawn_glyphs()))
    assert gate.burst_failures() == []


def test_a_burst_glyph_drawn_elsewhere_fails(gate, monkeypatch):
    monkeypatch.setitem(sprites.SHOTS, "pellet", ";")
    assert gate.burst_failures() == ["burst glyph ';' is drawn elsewhere in the arena"]


@pytest.mark.parametrize("entry, fails", [("player", "needle/player"), ("pellet", "pellet/cracked")])
def test_phase_4s_px2_art_fails_the_gate(gate, monkeypatch, entry, fails):
    """The mutants of spec 3.7: Phase 4's 1 x 3 player shot, or its 2-pixel pellet zig-zag, put back at px2."""
    monkeypatch.setitem(sprites.SPRITES2, entry, phase4_art.SPRITES2[entry])
    failed = gate.shape_gate(lambda line: None)
    assert any(line.startswith("mono k=2 ") and fails in line for line in failed), failed
    assert not any(line.startswith(("mono k=3 ", "mono k=6 ")) for line in failed)  # px3 is not touched


def test_the_px2_edits():
    """Spec 3.7: px2 only, in every tier: the player's shot 1 x 4 solid, the pellet a 1-pixel zig-zag and its mirror.
    phase4_art keeps the rows they replaced."""
    shot = sprites.artwork(2)["player"]
    assert len(shot) == 1 and (shot[0].w, shot[0].h) == (1, 4) and all(shot[0].px)
    pellet = sprites.SPRITES2["pellet"][1]
    assert pellet == [["#.", ".#", "#.", ".#"], [".#", "#.", ".#", "#."]]
    assert phase4_art.SPRITES2["player"][1] == [["#", "#", "#"]]
    assert phase4_art.SPRITES2["pellet"][1] == [["#.", "#.", ".#", ".#"], [".#", ".#", "#.", "#."]]


def test_the_art_validate_asks_for_is_all_drawn():
    """frozen campaign/validate.py calls these: their keys and meaning are kept."""
    assert sprites.missing_art() == [] and sprites.missing_pickups() == []
    assert sprites.missing_art(("no_such_key",)) == ["no_such_key"]
