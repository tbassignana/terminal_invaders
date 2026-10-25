"""The palette gate (Phase 5 spec 3.5-3.6, 3.8), run through tools/palette_check.py: the 12 colour tables against
each palette's floors, the 16-colour rules, and no flash in play across palettes and effects (WCAG 2.3.1)."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from terminal_invaders.app import FLASH_LIMIT, Renderer
from terminal_invaders.replay import Playback
from terminal_invaders.rules import make_game
from terminal_invaders.term.cells import CellBuffer, style
from terminal_invaders.term.color import xterm_rgb
from terminal_invaders.term.pixels import UPPER
from terminal_invaders.theme import ANSI16, COLOUR_PALETTES, PALETTES, SHADES, SLOT, TRUECOLOR, XTERM256, resolve_theme
from terminal_invaders.ui import Painter, Session
from tests.support.frames import options
from tests.support.terminals import capabilities, load

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "palette_check.py"
NAMES = [f"{tier}-{palette}-{bg}" for tier in ("truecolor", "256") for palette in COLOUR_PALETTES for bg in SHADES]


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check():
    path = list(sys.path)
    try:
        yield load_tool("palette_check_tool")
    finally:
        del sys.modules["palette_check_tool"]
        sys.path[:] = path


def table(check, name: str):
    return next(found for found in check.tables() if found.name == name)


def test_ciede2000_matches_reference_pairs(check):
    """Sharma, Wu and Dalal (2005), table 1, pairs 1, 7 and 17."""
    assert check.ciede2000((50, 2.6772, -79.7751), (50, 0, -82.7485)) == pytest.approx(2.0425, abs=1e-4)
    assert check.ciede2000((50, 0, 0), (50, -1, 2)) == pytest.approx(2.3669, abs=1e-4)
    assert check.ciede2000((50, 2.5, 0), (73, 25, -18)) == pytest.approx(27.1492, abs=1e-4)


def test_every_vision_keeps_greys_grey(check):
    assert check.ALL_VISIONS == ("normal", "protan", "deutan", "tritan", "achroma")
    for vision in check.ALL_VISIONS:
        assert check.simulate(0xFFFFFF, vision) == pytest.approx([1, 1, 1], abs=1e-3)
        assert check.simulate(0x000000, vision) == [0, 0, 0]
    grey = check.simulate(0x56B4E9, check.GREY)  # A shows a colour as its luminance, in every channel
    assert grey[0] == grey[1] == grey[2] == pytest.approx(check.luminance(check.simulate(0x56B4E9, "normal")))


def test_the_twelve_colour_tables_pass_their_palettes_floors(check):
    assert [found.name for found in check.tables()] == NAMES
    assert table(check, "256-default-light").colours["good"] == xterm_rgb(22)  # as the xterm cube shows it
    for found in check.tables():
        assert check.failures(found.name, found.colours, found.palette) == [], found.name


def test_the_pair_sets(check):
    assert len(check.HF) == 8 and len(check.HR) == 4 and len(check.HS) == 4 and len(check.RS) == 2
    assert check.DECOR == (("spark", "enemy_shot"), ("spark", "telegraph"))
    assert set(check.HF) == {(h, f) for h in check.HAZARD for f in ("player", "player_shot")}
    gated = set(check.HF + check.HR + check.HS + check.RS + check.DECOR)
    assert not gated & set(check.REPORTED_PAIRS)
    assert {("spark", "player"), ("spark", "player_shot"), ("spark", "pickup")} <= set(check.REPORTED_PAIRS)


def test_each_palettes_floors(check):
    """Spec 3.5's table: cvd widens HF + HR to 30 in protan and deutan and gates its thinnest pair in greyscale;
    contrast gates HF + HR in greyscale, sprites at 4.5:1 and every text role at 7:1."""
    floors = check.FLOORS
    assert floors["default"] == check.Floors(None, "reported", False, 3.0, None, 4.5)
    assert floors["cvd"] == check.Floors(30.0, "shots", True, 3.0, 3.0, 4.5)
    assert floors["contrast"] == check.Floors(None, "gated", True, 4.5, 3.0, 7.0)
    rules = {palette: {c.rule for c in check.checks(TRUECOLOR[palette, "dark"], palette)} for palette in floors}
    assert "RG" in rules["cvd"] and "RG" not in rules["default"] | rules["contrast"]
    assert "A" in rules["cvd"] and "A" in rules["contrast"] and "A" not in rules["default"]
    assert "shelter" not in rules["default"] and "shelter" in rules["cvd"] & rules["contrast"]
    for palette in floors:  # spark against the hazards (D) and against the background, in every palette
        assert {"D", "spark"} <= rules[palette]
    pickup_alien = {
        p: any(c.what.startswith("pickup/alien") for c in check.checks(TRUECOLOR[p, "dark"], p)) for p in floors
    }
    assert pickup_alien == {"default": False, "cvd": True, "contrast": True}


def test_cvd_keeps_the_enemy_shot_apart_from_your_ship_and_shot_in_greyscale(check):
    """A 1-pixel shot is a small field, where a protan or deutan viewer is left mostly with lightness."""
    for bg in SHADES:
        found = [c for c in check.checks(TRUECOLOR["cvd", bg], "cvd") if c.rule == "A"]
        assert [c.what.split()[0] for c in found] == ["enemy_shot/player", "enemy_shot/player_shot"]
        assert all(c.ok for c in found), found
    dark = {
        c.what.split()[0]: round(c.margin, 3) for c in check.checks(TRUECOLOR["cvd", "dark"], "cvd") if c.rule == "A"
    }
    assert dark == {"enemy_shot/player": 1.137, "enemy_shot/player_shot": 1.018}  # 2.27:1 and 2.04:1 between them


def test_contrast_text_is_seven_to_one(check):
    for bg in SHADES:
        for tier in ("truecolor", "256"):
            found = check.measure(table(check, f"{tier}-contrast-{bg}").colours)["contrast"]
            assert min(found[role] for role in check.TEXT) >= 7, (tier, bg)
            assert min(found[role] for role in check.SPRITES) >= 4.5, (tier, bg)


def test_the_new_palettes_figures_are_pinned(check):
    """Each table's binding check and its margin, as P's log records them."""
    found = {}
    for t in check.tables():
        worst = check.worst(t.colours, t.palette)
        found[t.name] = (worst.rule, round(worst.margin, 3))
    assert found == {
        "truecolor-default-dark": ("star", 1.026),
        "truecolor-default-light": ("sprite", 1.015),
        "truecolor-cvd-dark": ("D", 1.016),
        "truecolor-cvd-light": ("D", 1.091),
        "truecolor-contrast-dark": ("shelter", 1.143),
        "truecolor-contrast-light": ("spark", 1.011),
        "256-default-dark": ("sprite", 1.02),
        "256-default-light": ("HF", 1.034),
        "256-cvd-dark": ("D", 1.016),
        "256-cvd-light": ("D", 1.091),
        "256-contrast-dark": ("shelter", 1.143),
        "256-contrast-light": ("spark", 1.011),
    }
    assert (XTERM256["cvd", "dark"]["player_shot"], XTERM256["cvd", "dark"]["enemy_shot"]) == (111, 226)
    assert XTERM256["contrast", "light"]["warn"] == 52


def test_telegraph_marks_are_gated_against_what_a_player_could_confuse_them_with(check):
    """Phase 4 spec 15.5: is that a bullet, is that me, is that my shot? The default palette's measured worst."""
    pairs = [("telegraph", "enemy_shot"), ("telegraph", "player"), ("telegraph", "player_shot")]
    assert set(pairs) <= set(check.HF + check.HS)
    worst = {
        (tier, bg): round(
            min(check.measure(table(check, f"{tier}-default-{bg}").colours)["pairs"][p] for p in pairs), 1
        )
        for bg in SHADES
        for tier in ("truecolor", "256")
    }
    assert worst == {
        ("truecolor", "dark"): 27.2,
        ("truecolor", "light"): 24.3,
        ("256", "dark"): 21.3,
        ("256", "light"): 20.7,
    }
    assert {("default", "dark", "telegraph"), ("default", "light", "telegraph")} <= check.DELIBERATE_PINS


def test_the_ungated_pairs_are_reported_not_gated(check):
    """boss/diver and boss/enemy_shot have no colour that clears both (Phase 4 spec 15.5): the bosses' rules keep
    them apart by place, and the tool prints every reported pair for every table."""
    assert check.REPORTED_PAIRS[:2] == (("boss", "diver"), ("boss", "enemy_shot"))
    measured = {t.name: check.measure(t.colours)["pairs"] for t in check.tables()}
    assert round(measured["truecolor-default-dark"]["boss", "diver"], 1) == 0.0  # the same orange
    assert round(measured["256-default-light"]["boss", "enemy_shot"], 1) == 0.0  # the same index, 130
    lines = []
    assert check.colour_gate(out=lines.append) == []
    reported = [line for line in lines if line.startswith("    reported ")]
    assert len(reported) == 12 and all("boss/diver" in line and "spark/pickup" in line for line in reported)


def test_sprites_and_text_contrast_with_the_background(check):
    arcade = {"arcade_white", "arcade_red", "arcade_green"}  # Arcade mode's bands colour its text (GAME OVER) too
    for t in check.tables():
        floors, found = check.FLOORS[t.palette], check.measure(t.colours)["contrast"]
        assert min(found[role] for role in check.SPRITES) >= floors.sprite, t.name
        assert min(found[role] for role in check.TEXT) >= floors.text, t.name
        assert min(found[role] for role in arcade) >= 4.5 and found["spark"] >= 3, t.name
    # light 256's pinned boss, 130, is 4.28:1 for deutan vision
    assert check.measure(table(check, "256-default-light").colours)["contrast"]["boss"] == pytest.approx(4.28, abs=0.01)


def test_stars_never_read_as_shots(check):
    for t in check.tables():
        found = check.measure(t.colours)
        assert found["star"] < 2 and found["pairs"]["enemy_shot", "star"] >= 20, t.name


def test_256_tables_use_only_the_fixed_cube_and_greys():
    assert all(16 <= index <= 255 for table in XTERM256.values() for index in table.values())


@pytest.mark.parametrize("palette, bg", [(p, bg) for p in COLOUR_PALETTES for bg in SHADES])
def test_256_pins_are_the_nearest_colours_except_deliberate_pins(check, palette, bg):
    assert check.pin_failures(palette, bg) == []
    if palette != "default":  # each new colour is a cube or grey entry: the truecolor table holds the same colours
        assert all(TRUECOLOR[palette, bg][role] == xterm_rgb(XTERM256[palette, bg][role]) for role in PALETTES[palette])
    assert all(key[0] == "default" for key in check.DELIBERATE_PINS)


def test_the_gate_names_what_a_palette_misses(check, monkeypatch):
    bad = dict(TRUECOLOR["default", "dark"], enemy_shot=0xCC0098, dim=0x30364A, star=0x989900)  # shots as shelters
    lines = check.failures("bad", bad)
    assert "bad: enemy_shot/shelter 0.0 (N) < 20" in lines
    assert any(line.startswith("bad: dim ") and line.endswith(" < 4.5") for line in lines), lines
    assert any(line.startswith("bad: star ") and line.endswith("reads as a shot") for line in lines)
    # an A failure: contrast gates HF + HR in greyscale; a hazard as light as your ship
    grey = dict(TRUECOLOR["contrast", "dark"], player=0x8A8A8A, diver=0xC08A00)
    failed = check.failures("grey", grey, "contrast")
    assert any(line.startswith("grey: diver/player ") and "(A) < 20" in line for line in failed), failed
    # cvd's A rule fails with spec 3.3's witnesses (the two shots too close in lightness)
    witness = dict(TRUECOLOR["cvd", "dark"], enemy_shot=xterm_rgb(184), player_shot=xterm_rgb(195))
    line = "witness: enemy_shot/player_shot 8.6 (A) and 1.44:1 between them < 20 and 2:1"
    assert line in check.failures("witness", witness, "cvd")
    monkeypatch.setitem(check.TRUECOLOR, ("default", "dark"), bad)
    lines = []
    assert check.colour_gate(out=lines.append) != []
    assert any(line.startswith("truecolor-default-dark  worst HS enemy_shot/shelter 0.0") for line in lines), lines
    assert "256-default-dark: enemy_shot is 100, the nearest is" in " ".join(check.pin_failures("default", "dark"))


def test_16_colours_follow_the_structural_rules(check, monkeypatch):
    assert check.ansi16_failures() == []
    assert set(ANSI16) == {(p, bg) for p in COLOUR_PALETTES for bg in SHADES}
    for (palette, bg), table16 in ANSI16.items():  # default's table, but for cvd dark's two moves
        changed = {role for role in table16 if table16[role] != ANSI16["default", bg][role]}
        assert changed == ({"pickup", "telegraph"} if (palette, bg) == ("cvd", "dark") else set())
    assert ANSI16["cvd", "dark"]["pickup"] == (95, 0) and ANSI16["cvd", "dark"]["telegraph"] == (90, 0)
    broken = dict(ANSI16["default", "dark"], pickup=ANSI16["default", "dark"]["enemy_shot"], spark=(34, 0))
    monkeypatch.setitem(check.ANSI16, ("default", "dark"), broken)
    lines = check.ansi16_failures()
    assert "16-default-dark: enemy_shot and pickup share code 93" in lines
    assert "16-default-dark: spark uses the telegraph's code 34" in lines
    assert "16-default-dark: spark is 34, not the alien's 37" in lines


def test_16_colours_are_reported_under_two_reference_palettes(check):
    lines = check.ansi16_reports()
    assert len(lines) == 6 * 2 and all(line.endswith("(reported)") for line in lines)
    assert sum("under xterm:" in line for line in lines) == sum("under Terminal.app:" in line for line in lines) == 6
    cvd = next(line for line in lines if line.startswith("16-cvd-dark under xterm"))
    assert "enemy_shot/pickup" not in cvd  # cvd dark's pickup is off the enemy shot's red-green axis (4.1 in default)


def test_the_shape_check_runs_as_a_script():
    done = subprocess.run([sys.executable, "-B", TOOL, "--shapes"], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout
    assert [line.split()[1] for line in done.stdout.splitlines()] == ["k=2", "k=3", "k=4", "k=6", "k=1"]


@pytest.mark.slow
def test_the_tool_runs_as_a_script(check):
    done = subprocess.run([sys.executable, "-B", TOOL, "--quick"], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout
    lines = done.stdout.splitlines()
    assert [line.split()[0] for line in lines if not line.startswith(" ")][:12] == NAMES
    assert sum(line.startswith("mono k=") for line in lines) == 5
    assert check.RULE in lines  # the report states the rule it judges by
    assert sum(" frames " in line for line in lines) == len(check.flash_runs(every=False))


def test_the_flash_option_runs_the_full_set_and_quick_the_plain_one(check, monkeypatch):
    """G5.7's command, `--flash`, runs what its row says (FX-5): the staged scenes at k = 1, 2, 3, 4 and 6 in every
    palette; --quick runs the plain test run's set, and --all flies the whole golden tapes too (FX-1)."""
    asked = []

    def gate(out=print, every=True, whole=False):
        asked.append((every, whole))
        return []

    monkeypatch.setattr(check, "flash_gate", gate)
    assert check.main(["--flash"]) == check.main(["--flash", "--quick"]) == check.main(["--flash", "--all"]) == 0
    assert asked == [(True, False), (False, False), (True, True)]
    full = [arguments for kind, arguments in check.flash_runs() if kind == "staged"]
    every = (*COLOUR_PALETTES, "mono")
    assert {(k, palette) for _, k, _, palette in full} == {(k, p) for k in (1, 2, 3, 4, 6) for p in every}
    assert [a for kind, a in check.flash_runs() if kind == "tape"] == TAPE  # the window
    tapes = [arguments for kind, arguments in check.flash_runs(whole=True) if kind == "tape"]
    assert {(tape, end) for tape, _, end, _ in tapes if end is None} == {(tape, None) for tape in check.WHOLE_TAPES}
    quick = check.flash_runs(every=False)
    assert {(k, a[3]) for kind, a in quick if kind == "staged" for k in a[1:2]} == {
        (k, p) for k in (1, 2, 3) for p in ("default", "cvd")
    }
    assert [a for kind, a in quick if kind == "tape"] == [
        ("golden-classic-normal.json", 8600, 8720, bg) for bg in SHADES
    ]


# ---- flashes (spec 3.8) ----


def dark_theme():
    return resolve_theme(capabilities(load("ghostty")), "default")


def test_a_full_screen_flash_would_be_seen(check):
    lit, half = CellBuffer(80, 24), CellBuffer(80, 24)
    lit.fill(0, 0, 80, 24, st=style(bg=SLOT["player_shot"]))  # white over the whole screen
    half.fill(0, 0, 80, 12, UPPER, style(SLOT["player_shot"]))  # upper halves lit in the top 12 rows
    meter = check.Meter(dark_theme(), 80, 24)
    meter.feed(CellBuffer(80, 24))
    assert meter.feed(lit) == (80 * 24, 43 * 16, 0)  # every cell; every cell of a box
    meter = check.Meter(dark_theme(), 80, 24)
    meter.feed(CellBuffer(80, 24))
    assert meter.feed(half) == (80 * 12, 43 * 12, 0)  # half blocks count half their light, over 10%
    assert meter.feed(half.copy()) == (0, 0, 0)  # no change, no transition


def test_a_transition_counts_from_the_last_extreme(check):
    """A change spread over frames counts once, when it reaches 0.1 from the cell's last extreme; going on the same
    way is no new transition, and coming back is."""
    meter = check.Meter(dark_theme(), 1, 1)
    meter.looks = {(False, True, n): (value, False) for n, value in enumerate((0.0, 0.05, 0.1, 0.15, 0.04))}
    counts = []
    for n in range(5):
        buf = CellBuffer(1, 1)
        buf.fill(0, 0, 1, 1, " ", n)
        counts.append(meter.feed(buf)[0])
    assert counts == [0, 0, 1, 0, 1]  # 0 -> 0.05 -> 0.1 fires once; 0.15 widens it; back to 0.04 fires


def test_the_box_rule(check):
    """A quarter of one 43 x 16 box is a flash, though it is a small share of a large screen."""
    meter = check.Meter(dark_theme(), 240, 84)
    cells = [y * 240 + x for y in range(16) for x in range(43) if (x + y) % 4 == 0]
    assert len(cells) == 172 and len(cells) / (240 * 84) < 0.01  # a quarter of a box
    assert meter.most(cells) == 172 and meter.most(cells[:-1]) == 171
    failed = ("box: tick 10: 25% of a 43 x 16 box flashes (the effects' own)",)
    assert check.judge("box", [(10, 172, 0)], 43 * 16, {}).failures == failed
    assert check.judge("box", [(10, 171, 0)], 43 * 16, {}).failures == ()


def test_play_is_judged_on_the_screen_and_the_effects_on_the_box(check):
    """FX-1, spec 3.8 as amended: a frame's own motion (both Painters make the transition) is held to 25% of the
    screen, Phase 4's rule; the box rule counts only the transitions the effects add."""
    screen = 180 * 64
    play = check.Frame(10, most=0, most_red=0, screen=344)  # a formation flying in: half a box, 3% of the screen
    assert check.judge("play", [play], 43 * 16, {}, screen).failures == ()
    whole = check.judge("play", [check.Frame(10, 0, 0, screen // 4)], 43 * 16, {}, screen)
    assert whole.failures == ("play: tick 10: 25% of the screen flashes",) and whole.screen == 0.25
    both = check.judge("fx", [check.Frame(11, 172, 0, screen // 4)], 43 * 16, {}, screen)
    assert both.failures == ("fx: tick 11: 25% of a 43 x 16 box flashes (the effects' own); 25% of the screen flashes",)
    red = check.judge("red", [check.Frame(12, 0, 0, 0, screen // 4)], 43 * 16, {}, screen)
    assert red.failures == ("red: tick 12: 25% of the screen in saturated-red flashes",)


def test_the_effects_own_flashes_are_where_the_frames_differ_or_off_makes_none(check):
    """FX-1: an effect's coming (the frames differ after it), its going (the frames agree again, but only the
    effects-on frame flashes there), and a flash both frames make the same way in a cell the effect still tints are
    all the effects'. Only a flash both frames make the same way and leave alike is play's."""
    on, off = CellBuffer(4, 1), CellBuffer(4, 1)
    on.put(0, 0, "A", 1), off.put(0, 0, "A", 1)  # 0: both flash up and show the same: play's
    on.put(0, 1, "*", 2)  # 1: a spark comes; the frames differ
    on.put(0, 3, "#", 3), off.put(0, 3, "#", 4)  # 3: both flash up, but the effect tints the cell
    fired, plain = {0: 1, 1: 1, 2: 2, 3: 1}, {0: 1, 3: 1}  # 2: a spark goes; the frames agree there again
    assert check.own_flashes(fired, plain, on, off) == {1: 1, 2: 2, 3: 1}
    assert check.own_flashes({0: 2}, {0: 1}, on, off) == {0: 2}  # the same cell flashing the other way


def test_saturated_red_is_judged_per_cell_colour(check):
    assert check.saturated_red(xterm_rgb(88)) and check.saturated_red(xterm_rgb(124))  # contrast and cvd light danger
    assert not check.saturated_red(0xFF7A6B) and not check.saturated_red(0xFF6060)  # default's danger, arcade_red
    assert not check.saturated_red(0x000000)
    log = {("HIT", 100): 101}  # a red area fails even in a kick's own frames, which rule 1 lets through
    assert check.judge("red", [(103, 400, 0)], 43 * 16, log).failures == ()
    failed = check.judge("red", [(103, 400, 200)], 43 * 16, log).failures
    assert failed == ("red: tick 103: 200 saturated-red transitions of the effects in a 43 x 16 box",)


def test_kicks_count_once_under_the_three_a_second_rule(check):
    """Each admitted kick is one flash, however many of its frames change; with any frame over the area outside a
    kick, at most 3 in any 60 ticks."""
    box = 43 * 16
    three = {("HIT", 100): 101, ("PHASE BREAK", 120): 121, ("+10,000", 150): 151}
    frames = [(tick, box, 0) for tick in range(101, 160)]  # every kick frame over the area
    run = check.judge("kicks", frames[:7] + frames[20:27] + frames[50:], box, three)
    assert run.failures == () and run.kicks == 3 and run.window == 3
    four = three | {("HIT", 155): 156}
    assert check.judge("kicks", [], box, four).failures == ("kicks: 4 flashes in 60 ticks (at most 3)",)
    late = three | {("HIT", 161): 162}  # 61 ticks after the first: another window
    assert check.judge("kicks", [], box, late).failures == ()
    over = check.judge("kicks", [(200, box, 0)], box, three)  # a frame over the area outside any kick
    assert over.failures == ("kicks: tick 200: 100% of a 43 x 16 box flashes (the effects' own)",)


def test_a_kicks_frames_run_from_age_1_to_the_frame_after_it(check):
    assert check.kick_ticks({("HIT", 100): 101}) == set(range(101, 108))  # 6 ticks shifted, then the frame back
    assert check.kick_ticks({("+10,000", 50): 51}) == set(range(51, 62))  # a boss kill's 10
    assert check.kick_ticks({("PHASE BREAK", 0): 1}) == set(range(1, 8))


_TOOL = load_tool("palette_check_runs")
PLAY = [arguments for kind, arguments in _TOOL.flash_runs(every=False) if kind == "play"]
STAGED = [arguments for kind, arguments in _TOOL.flash_runs(every=False) if kind == "staged"]
STAGED_SLOW = [a for kind, a in _TOOL.flash_runs(every=True) if kind == "staged" and a not in STAGED]
TAPE = [arguments for kind, arguments in _TOOL.flash_runs(every=False) if kind == "tape"]
TAPE_SLOW = [a for kind, a in _TOOL.flash_runs(whole=True) if kind == "tape" and a not in TAPE]
del sys.modules["palette_check_runs"]


def tape_id(arguments) -> str:
    tape, start, end, background = arguments
    return f"{tape.removesuffix('.json')}-{'whole' if end is None else f'{start}-{end}'}-{background}"


@pytest.mark.parametrize("arguments", PLAY, ids=[f"{a[0]}-{a[3]}-{a[1]}-{a[2][0]}x{a[2][1]}" for a in PLAY])
def test_no_frame_of_play_flashes_the_screen(check, arguments):
    """Phase 4's runs in every palette, with effects full: while the skilled bot plays (kills, pickups, a new run
    after a game over), no frame changes the luminance of a quarter of the screen or of any 43 x 16 box."""
    run = check.play_run(*arguments)
    assert run.frames == 1199 and run.failures == (), run.failures
    assert run.share < 0.25


@pytest.mark.parametrize("arguments", STAGED, ids=["-".join(map(str, a)) for a in STAGED])
def test_the_staged_scenes_do_not_flash(check, arguments):
    """A ship lost, a phase break and a boss kill, each with its kick on and off and its burst and debris to their
    end: no flash but the admitted kick, no red area, at most 3 in 60 ticks."""
    run = check.staged_run(*arguments)
    assert run.failures == () and run.kicks == 1 and run.window == 1, run
    assert run.frames == check.LEAD + check.TAIL
    if arguments[:3] == ("ship-lost", 3, "light"):  # the kick's worst case (spec 2.5): its frames are the effects' own
        assert run.kick_share > 0.1, run


@pytest.mark.parametrize("arguments", TAPE, ids=map(tape_id, TAPE))
def test_the_tape_window_of_a_formation_flying_in_does_not_flash(check, arguments):
    """FX-1: golden-classic-normal's wave 3 flying in at k = 3 changes over a quarter of a 43 x 16 box with effects
    off, which is Phase 4's own motion; it is judged by Phase 4's whole-screen rule, and what the effects add by the
    box rule, and both pass."""
    run = check.tape_run(*arguments)
    assert run.frames == 120 and run.failures == (), run.failures
    assert run.screen < 0.05 and run.share < 0.25
    tape, start, end, background = arguments  # the same window with effects off, through the box rule alone
    recorded = Playback(check.TAPES / tape).tapes[0]
    game, shown = make_game(recorded.config), check.flash_options(effects="off")
    session = Session(shown, check.StubProfile(), check.SilentAudio())
    session.pilots[0].game = game
    theme, buf = check.theme_for("truecolor", background, "default"), CellBuffer(*check.TAPE_SIZE)
    painter, meter, worst = Painter(buf, shown, theme), check.Meter(theme, *check.TAPE_SIZE), 0
    while game.tick < end:
        game.step(recorded.actions_at(game.tick))
        if game.tick >= start:
            painter.draw(session)
            worst = max(worst, meter.feed(buf)[1])
    assert worst >= check.FLASH_AREA * 43 * 16  # Phase 4's motion alone fills a quarter of a box


@pytest.mark.slow
@pytest.mark.parametrize("arguments", TAPE_SLOW, ids=map(tape_id, TAPE_SLOW))
def test_the_whole_golden_tapes_do_not_flash(check, arguments):
    """FX-1: each Campaign golden tape flown whole at k = 3, a frame for every tick it recorded (golden-endless-hard
    has 36,000), and no flash."""
    run = check.tape_run(*arguments)
    assert run.failures == () and run.frames == Playback(check.TAPES / arguments[0]).tapes[0].ticks, run


@pytest.mark.slow
@pytest.mark.parametrize("arguments", STAGED_SLOW, ids=["-".join(map(str, a)) for a in STAGED_SLOW])
def test_the_staged_scenes_do_not_flash_in_any_palette_or_scale(check, arguments):
    run = check.staged_run(*arguments)
    assert run.failures == () and run.kicks == 1, run


def test_the_flash_limiter_allows_three_a_second():
    """Phase 1's wall-clock hook stays as it is, unused (a wall clock could decide differently live and in a
    replay): kicks go through fx.FlashLimiter, on game ticks."""
    renderer = Renderer(options(), capabilities(load("ghostty")), (80, 24))
    assert FLASH_LIMIT == 3
    assert [renderer.may_flash(t / 10) for t in range(10)] == [True] * 3 + [False] * 7  # 0.0, 0.1 and 0.2
    assert renderer.may_flash(1.0) and not renderer.may_flash(1.05)  # 0.0 is a second old now: one more
    assert renderer.may_flash(1.15) and renderer.may_flash(2.0)
