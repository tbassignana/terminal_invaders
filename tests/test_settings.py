"""Phase 5 settings (spec 4.1, 4.3, 4.4, 3.2): the table, exact-type validation, the flag > saved > default merge in
cli.main, options.given, what the profile writes and keeps, and the tier rule with NO_COLOR."""

import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

import pytest

from terminal_invaders import app, cli, settings
from terminal_invaders.keymap import PRESETS, Keymap
from terminal_invaders.model import Config
from terminal_invaders.settings import SETTINGS, merge, valid
from terminal_invaders.storage import Profile
from terminal_invaders.term.caps import TIERS, EnvHints, Overrides, ProbeReplies, resolve
from terminal_invaders.theme import PALETTE_LABELS, PALETTE_NAMES

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "terminal_invaders" / "demos" / "campaign.json"
# vars(cli.defaults()) in the base tree (/tmp/claude-0/p5, Phase 4), literally, but for its keymap (the default
# preset, compared by its contexts): tools/balance.py, bench_render and the frame tests build Sessions from it.
BASE_DEFAULTS = {
    "color": "auto",
    "controls": "auto",
    "debug": False,
    "difficulty": "normal",
    "fixed_seed": False,
    "fps": 60,
    "json": False,
    "keymap_file": None,
    "keys": "default",
    "mode": "classic",
    "name": None,
    "no_kitty": False,
    "no_sound": False,
    "no_sync": False,
    "pacing": "auto",
    "probe": False,
    "profile": None,
    "quickstart": False,
    "raw": False,
    "record": None,
    "reduced_motion": False,
    "replay": None,
    "scale": "auto",
    "scores": False,
    "seed": None,
    "show_fps": False,
    "theme": "auto",
    "two_player": False,
    "verify_replay": None,
}
NEW_DEFAULTS = {"palette": "default", "effects": "full", "tips": "auto", "attract": "off", "given": frozenset()}
NEW_DEFAULTS["given_flags"] = {}  # the flag typed for a row whose flag has another name (ST-4)
# The built-in defaults of spec 4.4, each the base tree's argparse default (sound is --no-sound's negation)
BUILT_IN = {
    "palette": "default",
    "theme": "auto",
    "effects": "full",
    "reduced_motion": False,
    "scale": "auto",
    "controls": "auto",
    "keys": "default",
    "fps": 60,
    "sound": True,
    "attract": "auto",
    "tips": "auto",
    "tips_done": False,
}
RESULT = {"score": 10, "wave": 1, "kills": 1, "shots": 1, "hits": 1, "best_combo": 1, "ticks": 60, "victory": False}
# Per row: a saved value and a flag that sets a third value (bool rows: the flag's value, over the default saved)
SAVED_AND_FLAG = {
    "palette": ("cvd", ["--palette", "contrast"], "contrast"),
    "theme": ("light", ["--theme", "dark"], "dark"),
    "effects": ("calm", ["--effects", "off"], "off"),
    "reduced_motion": (True, None, None),
    "scale": ("2", ["--scale", "3"], "3"),
    "controls": ("hold", ["--controls", "timed"], "timed"),
    "keys": ("vim", ["--keys", "arrows"], "arrows"),
    "fps": (30, ["--fps", "20"], 20),
    "sound": (False, None, None),
    "attract": ("off", None, None),
    "tips": ("always", ["--tips", "off"], "off"),
}


def launch(tmp_path, *arguments, saved=None, environ=None):
    """cli.main as a launch on a terminal, up to app.run: (the options run got, the enabled Audio got)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "profile.json"
    if saved is not None:
        path.write_text(json.dumps({"version": 2, "settings": saved}))
    terminal = Mock(isatty=lambda: True, fileno=Mock(side_effect=io.UnsupportedOperation))
    audio = Mock()
    with (
        patch("sys.stdin", terminal),
        patch("sys.stdout", terminal),
        patch("terminal_invaders.audio.Audio", audio),
        patch.object(app, "run") as run,
        patch.dict("os.environ", environ or {}),
    ):
        assert cli.main(["--profile", str(path), *arguments]) == 0
    return run.call_args.args[0], audio.call_args.kwargs["enabled"]


def test_the_rows_are_spec_4_1s_in_the_cards_order():
    assert [row.key for row in SETTINGS] == list(SAVED_AND_FLAG)
    titles = [row.title for row in SETTINGS]
    assert titles == [
        "PALETTE",
        "BACKGROUND",
        "EFFECTS",
        "REDUCED MOTION",
        "SCALE",
        "CONTROLS",
        "KEYS",
        "FRAME RATE",
        "SOUND",
        "TITLE DEMO",
        "TIPS",
    ]
    assert all(len(title) <= 14 for title in titles)  # the label columns, 4-17
    rows = settings.ROWS
    assert rows["palette"].values == PALETTE_NAMES and rows["palette"].labels == tuple(PALETTE_LABELS.values())
    assert rows["keys"].values == tuple(PRESETS)
    assert rows["scale"].labels == ("AUTO", "TEXT", "2X", "3X", "4X", "6X")
    assert rows["controls"].labels == ("AUTO", "HOLD", "TAP", "TIMED")
    assert rows["fps"].values == (60, 30, 20, 15) and rows["fps"].label(45) == "45"  # --fps 45 is shown as given
    assert rows["attract"].flag is None and rows["sound"].flag == "--no-sound"
    for row in SETTINGS:
        assert len(row.values) == len(row.labels) == len(set(row.values)) and len(row.notes) <= len(row.values)
        assert row.default in row.values and type(row.default) is row.type
        assert all(type(value) is row.type for value in row.values)


def test_every_text_fits_row_19_and_every_value_fits_the_30_column_field():
    """Spec 4.2: row 19 holds at most 58 characters, for every value of every row and every special case; a value,
    flag-locked or not, fits the field between < and > (the longest is COLOUR-BLIND SAFE (--palette), 29)."""
    for row in SETTINGS:
        for value in row.values:
            assert len(row.describe(value)) <= settings.DESCRIPTION_WIDTH, (row.key, value)
            assert len(row.label(value)) <= 30
            if row.flag:
                assert len(f"{row.label(value)} ({row.flag})") <= 30
                assert len(settings.locked_note(row.key, {row.key: value})) <= settings.DESCRIPTION_WIDTH
    for text in settings.TEXTS:
        assert len(text) <= settings.DESCRIPTION_WIDTH, text
    assert all(len(text) <= 30 for text in settings.FIELDS)
    assert max(len(f"{row.label(v)} ({row.flag})") for row in SETTINGS if row.flag for v in row.values) == 29
    assert settings.ROWS["effects"].describe("calm") == "Bursts stay; no shake, stars hold still."  # spec 2.8
    assert "Arcade keeps its 1978 colours" in settings.ROWS["palette"].description  # spec 3.1
    assert settings.locked_note("scale", {}) == "Set by --scale for this run. Saved: AUTO."  # spec 4.2


def test_a_saved_value_is_valid_only_with_the_rows_exact_type():
    for row in SETTINGS:
        for value in row.values:
            assert valid(row.key, value), (row.key, value)
        assert not valid(row.key, "amber") and not valid(row.key, None) and not valid(row.key, [])
    assert valid("tips_done", True) and valid("tips_done", False) and not valid("tips_done", 1)
    for key, value in (("sound", 1), ("reduced_motion", 0), ("fps", 60.0), ("fps", True), ("fps", "60")):
        assert not valid(key, value), (key, value)
    assert not valid("scale", 2) and valid("scale", "2")  # the flag's strings, as argparse gives them
    assert not valid("volume", 0.4)  # an unknown key is never a setting
    assert settings.is_default("fps", 60) and not settings.is_default("fps", 60.0)
    assert settings.is_default("sound", True) and not settings.is_default("sound", 1)
    assert settings.saved_value({"fps": 60.0}, "fps") == 60 and settings.saved_value({"fps": 30}, "fps") == 30
    assert settings.saved_value([1], "palette") == "default"  # a settings that is not an object reads as empty


def test_each_built_in_default_is_the_base_trees_argparse_default():
    assert {row.key: row.default for row in SETTINGS} | {"tips_done": settings.default("tips_done")} == BUILT_IN
    for row in SETTINGS:
        option = settings.option_name(row.key)
        if option in BASE_DEFAULTS:
            expected = not BASE_DEFAULTS[option] if row.key == "sound" else BASE_DEFAULTS[option]
            assert row.default == expected, row.key
    parsed = vars(cli.parser().parse_args([]))
    assert {key: parsed[key] for key in ("palette", "effects", "tips")} == {
        "palette": "default",
        "effects": "full",
        "tips": "auto",
    }


def test_defaults_are_the_base_trees_plus_exactly_the_new_options():
    made = vars(cli.defaults())
    assert made.pop("keymap").contexts == Keymap.preset("default").contexts
    assert made == BASE_DEFAULTS | NEW_DEFAULTS
    assert cli.defaults(palette="cvd").palette == "cvd"  # values still go over them


def test_options_given_come_from_a_second_parse_of_the_same_command_line():
    assert cli.given_options([]) == frozenset()
    assert cli.given_options(["--glyphs"]) == {"scale"}
    assert cli.given_options(["--no-sound"]) == {"no_sound"}
    assert cli.given_options(["--keymap", "keys.json"]) == {"keys"}
    assert cli.given_options(["--theme", "auto", "--fps", "60", "--name", "ace"]) == {"theme", "fps", "name"}
    every = "--palette cvd --effects calm --tips off --reduced-motion --scale 2 --controls hold --keys vim".split()
    assert cli.given_options(every) == {"palette", "effects", "tips", "reduced_motion", "scale", "controls", "keys"}
    assert cli.given_options(["--mono", "--seed", "4", "--arcade", "--show-fps", "--pacing", "off"]) == frozenset()
    assert set(settings.SETTING_DESTS.values()) == {settings.option_name(row.key) for row in SETTINGS if row.flag} | {
        "name"
    }


def card_states(row):
    """The ways a row can stand on the card, as options to set: as chosen, held by its flag (as typed: --glyphs for
    SCALE too), under reduced motion, and (KEYS) a --keymap file."""
    option = settings.option_name(row.key)
    states = [{}, {"reduced_motion": True}] + ([{"given": frozenset({option})}] if row.flag else [])
    if row.key == "scale":
        states.append({"given": frozenset({"scale"}), "given_flags": {"scale": "--glyphs"}})
    if row.key == "keys":
        states.append({"given": frozenset({"keys"}), "keymap_file": "keys.json"})
    return states


def test_every_value_the_card_composes_fits_its_field_and_row_19():
    """ST-8, spec 4.2 ("a test renders every value of every row, flag-locked and not"): what the card itself makes
    of each row (screens.shown: AUTO (...), TERMINAL (PAINTED), X (--flag), CUSTOM, ...) for every value and state,
    under each control scheme and palette, on truecolor, 256, 16-colour and mono terminals and one with kitty's
    flags: the field fits its 30 columns and row 19 its 58."""
    from terminal_invaders import screens
    from terminal_invaders.term.cells import CellBuffer
    from terminal_invaders.theme import resolve_theme
    from terminal_invaders.ui import Painter
    from tests.support.frames import session
    from tests.support.terminals import capabilities, load

    terminals = [capabilities(load(name)) for name in ("ghostty", "xterm", "linux-console", "dumb")]
    terminals.append(capabilities(load("ghostty"), Overrides(color="mono")))  # --mono over a colour terminal
    seen = 0
    for caps in terminals:
        for palette in PALETTE_NAMES:
            current = session(palette=palette)
            painter = Painter(CellBuffer(80, 24), current.options, resolve_theme(caps, palette), caps)
            base, controls = dict(vars(current.options)), current.controls
            for row in SETTINGS:
                for value in row.values:
                    for state in card_states(row):
                        for scheme in ("hold", "latched", "timed"):
                            vars(current.options).clear()
                            vars(current.options).update(base | state)
                            settings.put(current.options, row.key, value)
                            current.controls = SimpleNamespace(scheme=scheme)
                            field, _, note = screens.shown(painter, current, row)
                            assert len(field) <= 30, (row.key, value, state, field)
                            assert len(note) <= settings.DESCRIPTION_WIDTH, (row.key, value, state, note)
                            seen += 1
            current.controls = controls
    assert seen > 5000


def test_the_card_names_the_flag_that_was_typed(tmp_path):
    """ST-4: --glyphs holds SCALE for the run; the card says so by --glyphs, the flag typed, not the row's own."""
    assert cli.given_flags(["--glyphs"]) == {"scale": "--glyphs"}
    assert cli.given_flags([]) == cli.given_flags(["--scale", "1"]) == cli.given_flags(["--no-sound"]) == {}
    options, _ = launch(tmp_path, "--glyphs")
    assert (options.scale, options.given, options.given_flags) == ("1", {"scale"}, {"scale": "--glyphs"})
    flag = settings.typed_flag(options, "scale")
    assert (
        flag == "--glyphs" and settings.locked_note("scale", {}, flag) == "Set by --glyphs for this run. Saved: AUTO."
    )
    assert settings.typed_flag(launch(tmp_path, "--scale", "2")[0], "scale") == "--scale"
    assert settings.typed_flag(cli.defaults(), "scale") == "--scale"  # what tools and tests build a Session from
    assert settings.typed_flag(SimpleNamespace(), "scale") == "--scale"  # options with no given_flags at all
    assert settings.PAINTED_LOCKED == "TERMINAL (PAINTED) (--theme)"  # --theme terminal under HIGH CONTRAST


def test_the_tap_note_names_no_key_a_preset_may_not_bind():
    """ST-5: the CONTROLS TAP note is static, and S stops nothing under ARROWS (Down) or VIM (J)."""
    note = settings.ROWS["controls"].describe("latched")
    assert note == "Tap a direction to move; tap the other way to stop."
    assert not any(len(word.strip(".;,")) == 1 and word.isupper() for word in note.split())
    assert {Keymap.preset(preset).keys_for("play", "stop") for preset in ("arrows", "vim")} == {("down",), ("j",)}


def test_help_calls_theme_the_background():
    """ST-7: --help names --theme the background, as the card's BACKGROUND row does; --palette is the palette."""
    actions = {option: action for action in cli.parser()._actions for option in action.option_strings}
    assert actions["--theme"].help.startswith("the background:") and "palette" not in actions["--theme"].help
    assert actions["--palette"].help.startswith("colours:")


@pytest.mark.parametrize("key", list(SAVED_AND_FLAG))
def test_a_flag_beats_the_saved_value_which_beats_the_built_in_default(tmp_path, key):
    """In cli.main, after the profile opens and before the keymap and Audio are made (spec 4.4)."""
    saved, flag, flagged = SAVED_AND_FLAG[key]
    row = settings.ROWS[key]
    fresh, _ = launch(tmp_path / "fresh")
    assert settings.get(fresh, key) == row.default
    from_profile, enabled = launch(tmp_path / "saved", saved={key: saved})
    assert settings.get(from_profile, key) == saved and from_profile.given == frozenset()
    if flag is None and row.flag:  # a bool row: its flag sets the value the default is not, over a saved default
        flag, flagged = [row.flag], not row.default if key != "sound" else False
        over, _ = launch(tmp_path / "flag", *flag, saved={key: row.default})
    elif flag is not None:
        over, _ = launch(tmp_path / "flag", *flag, saved={key: saved})
    else:
        return  # TITLE DEMO has no flag
    assert settings.get(over, key) == flagged and settings.given(over, key)
    assert json.loads((tmp_path / "flag" / "profile.json").read_text())["settings"] == {
        key: row.default if row.flag and SAVED_AND_FLAG[key][1] is None else saved
    }  # a flag is never saved


def test_the_keymap_and_the_sound_are_made_from_the_merged_options(tmp_path):
    options, enabled = launch(tmp_path, saved={"keys": "vim", "sound": False})
    assert options.keymap.contexts == Keymap.preset("vim").contexts and enabled is False
    options, enabled = launch(tmp_path / "flags", "--keys", "arrows", saved={"keys": "vim", "sound": True})
    assert options.keymap.contexts == Keymap.preset("arrows").contexts and enabled is True
    keymap = tmp_path / "keys.json"
    keymap.write_text(json.dumps({"preset": "default", "bind": {"play.fire": ["f"]}}))
    options, _ = launch(tmp_path / "file", "--keymap", str(keymap), saved={"keys": "vim"})
    assert options.keys == "default" and "keys" in options.given  # --keymap holds the KEYS row (CUSTOM)
    assert options.keymap.contexts == Keymap.load(keymap).contexts != Keymap.preset("vim").contexts
    options, enabled = launch(tmp_path / "muted", "--no-sound", saved={"sound": True})
    assert options.no_sound and enabled is False


def test_invalid_and_unknown_saved_values_are_ignored_for_the_run_and_kept(tmp_path):
    saved = {"palette": "amber", "volume": 0.4, "sound": 1, "fps": 60.0, "reduced_motion": 0, "effects": "calm"}
    options, enabled = launch(tmp_path, saved=saved)
    assert (options.palette, options.fps, options.reduced_motion, options.effects) == ("default", 60, False, "calm")
    assert enabled is True  # "sound": 1 is not a bool
    profile = Profile(tmp_path / "profile.json")
    assert profile.settings == saved
    assert profile.update_settings({"effects": "off"})
    assert json.loads((tmp_path / "profile.json").read_text())["settings"] == {**saved, "effects": "off"}


def test_portability_is_per_key(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text('{"version": 2, "settings": {"palette": "cvd", "volume": NaN, "fps": Infinity}}')
    profile = Profile(path)
    assert profile.settings == {"palette": "cvd"}  # the NaN drops its own key, not the object
    assert merge(cli.defaults(), profile.settings).palette == "cvd"
    profile.update_settings({"tips_done": True})
    assert json.loads(path.read_text())["settings"] == {"palette": "cvd", "tips_done": True}


def test_only_values_that_differ_from_the_built_in_default_are_written(tmp_path):
    path = tmp_path / "profile.json"
    profile = Profile(path)
    changes = {row.key: row.default for row in SETTINGS}
    assert profile.update_settings(changes) and not path.exists()  # nothing to write: nothing written
    assert profile.update_settings({"palette": "cvd", "effects": "full", "fps": 60, "sound": False})
    assert json.loads(path.read_text())["settings"] == {"palette": "cvd", "sound": False}
    assert profile.update_settings({"palette": "default"})  # back to the default deletes its key
    assert json.loads(path.read_text())["settings"] == {"sound": False}
    assert profile.update_settings({"sound": True})
    assert "settings" not in json.loads(path.read_text())  # an empty object is not written at all
    assert Profile(path).settings == {}


@pytest.mark.parametrize("value", [[1, 2], "cvd", 7, None, True])
def test_a_settings_entry_that_is_not_an_object_is_kept_and_the_defaults_used(tmp_path, value):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"version": 2, "name": "ACE", "settings": value}))
    profile = Profile(path)
    assert profile.settings == {} and profile.settings_unreadable
    options, _ = launch(tmp_path, saved=None)
    assert options.palette == "default"
    assert profile.update_settings({"palette": "cvd"}) is False
    assert profile.alert == settings.UNREADABLE == "Settings not saved: this profile's settings are unreadable"
    assert json.loads(path.read_text())["settings"] == value
    profile.record(RESULT, Config(), "ACE")
    assert json.loads(path.read_text())["settings"] == value  # a run's save keeps it as it is too


def test_verify_replay_probe_and_scores_read_no_settings(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"version": 2, "settings": {"keys": "vim", "palette": "cvd"}}))
    out = io.StringIO()
    with (
        patch.object(Profile, "settings", new_callable=PropertyMock) as read,
        patch.object(cli, "merge", side_effect=AssertionError("merged")) as merged,
        patch("sys.stdout", out),
    ):
        assert cli.main(["--verify-replay", str(DEMO), "--profile", str(path)]) == 0
        assert cli.main(["--scores", "--profile", str(path)]) == 0
        with (
            patch.object(cli, "require_probe_terminal"),
            patch.object(app, "run_probe_command", return_value=0) as probe,
        ):
            assert cli.main(["--probe", "--profile", str(path)]) == 0
    assert "VERIFIED" in out.getvalue() and "No scores yet" in out.getvalue()
    read.assert_not_called()
    merged.assert_not_called()
    flags = probe.call_args.args[0]
    assert flags.keymap.contexts == Keymap.preset("default").contexts and flags.palette == "default"


def test_help_names_the_keys():
    out = io.StringIO()
    with patch("sys.stdout", out), pytest.raises(SystemExit) as exited:
        cli.main(["--help"])
    assert exited.value.code == 0
    text = " ".join(out.getvalue().split())
    assert " ".join(cli.EPILOG.split()) in text
    for words in ("arrows or A/D move", "Space fires", "B bombs", "P pauses", "Esc Esc quits", "SETTINGS"):
        assert words in text
    for flag in ("--palette", "--effects", "--tips"):
        assert flag in text


def tier(environ, overrides=Overrides()):
    caps = resolve(EnvHints.from_env(environ), ProbeReplies(), overrides)
    return caps.color, caps.natural_color


def test_the_tier_rule():
    """Spec 3.2, first match wins: --mono or --color; the palette mono; NO_COLOR with a chosen palette gives the tier
    the terminal would have had without it (no-color.org); else today's detection, NO_COLOR giving mono."""
    truecolor, no_color = {"COLORTERM": "truecolor"}, {"COLORTERM": "truecolor", "NO_COLOR": "1"}
    assert tier(truecolor) == ("truecolor", "truecolor")
    assert tier(no_color) == ("mono", "truecolor")  # untouched: NO_COLOR still gives mono
    assert tier(no_color, Overrides(palette="cvd", palette_chosen=True)) == ("truecolor", "truecolor")
    assert tier(no_color, Overrides(palette="default", palette_chosen=True)) == ("truecolor", "truecolor")
    assert tier(truecolor, Overrides(palette="mono", palette_chosen=True)) == ("mono", "truecolor")
    assert tier(no_color, Overrides(palette="mono")) == ("mono", "truecolor")
    for chosen in (True, False):
        for level in TIERS:
            overrides = Overrides(color=level, palette="cvd", palette_chosen=chosen)
            assert tier(no_color, overrides) == (level, level)  # --color and --mono beat everything
        assert tier(truecolor, Overrides(color="256", palette="mono", palette_chosen=chosen))[0] == "256"
    assert tier({"TERM": "xterm-256color", "NO_COLOR": "1"}, Overrides(palette="contrast", palette_chosen=True)) == (
        "256",
        "256",
    )


def test_the_palette_in_force_and_whether_it_was_chosen_reach_the_probe():
    defaults = cli.defaults()
    assert app.overrides(defaults) == Overrides()  # what tests and tools build: as Phase 4's
    given = cli.defaults(given=frozenset({"palette"}))
    assert app.overrides(given) == Overrides(palette="default", palette_chosen=True)  # --palette default
    saved = cli.defaults(palette="cvd")  # from the profile: not a flag, but a choice
    assert app.overrides(saved) == Overrides(palette="cvd", palette_chosen=True)
    assert app.overrides(cli.defaults(palette="amber")).palette == "default"
    assert app.overrides(SimpleNamespace(**{**vars(defaults), "given": ()})).palette_chosen is False
    bare = SimpleNamespace(color="auto", theme="auto", pacing="auto", no_kitty=False, no_sync=False)
    assert app.overrides(bare) == Overrides()  # options from before Phase 5 (fakes)


def test_no_color_with_a_saved_palette_end_to_end(tmp_path):
    """A palette saved in SETTINGS beats NO_COLOR at the next launch; an untouched one does not (owner question 2)."""
    environ = {"NO_COLOR": "1", "COLORTERM": "truecolor"}
    env = EnvHints.from_env(environ)
    for saved, expected in (({"palette": "cvd"}, "truecolor"), ({}, "mono"), ({"palette": "mono"}, "mono")):
        options, _ = launch(tmp_path / str(len(saved)) / str(saved.get("palette")), saved=saved)
        assert resolve(env, ProbeReplies(), app.overrides(options)).color == expected, saved
    options, _ = launch(tmp_path / "flag", "--palette", "default", saved={})
    assert resolve(env, ProbeReplies(), app.overrides(options)).color == "truecolor"
    options, _ = launch(tmp_path / "color", "--color", "16", saved={"palette": "cvd"})
    assert resolve(env, ProbeReplies(), app.overrides(options)).color == "16"


def test_natural_color_has_a_default_so_fakes_and_fixtures_keep_working():
    caps = resolve(EnvHints(), ProbeReplies(), Overrides())
    assert list(type(caps).__dataclass_fields__)[-1] == "natural_color"
    assert type(caps).__dataclass_fields__["natural_color"].default is None
    assert replace(caps, natural_color=None).natural_color is None
