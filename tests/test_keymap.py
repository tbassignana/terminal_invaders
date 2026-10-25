"""Keymap (spec 6.4): presets, lookup order, modifier rules, validation and the JSON loader; Phase 5's settings,
initials and attract contexts (Phase 5 spec 10)."""

import json
import tempfile
import unittest
from pathlib import Path

from terminal_invaders.keymap import (
    ABANDON,
    ACTIONS,
    DEFAULT,
    MENU_MIRRORS,
    OPTIONAL_PLAY,
    PRESETS,
    WAYS_OUT,
    Keymap,
    KeymapError,
    _mirror_menu,
    parse_spec,
    printable,
)
from terminal_invaders.term.keys import ALT, CAPS_LOCK, CTRL, NUM_LOCK, SHIFT, SUPER, Key


class PresetTests(unittest.TestCase):
    def test_every_preset_is_valid(self):
        for name in PRESETS:
            with self.subTest(preset=name):
                keymap = Keymap.preset(name)
                keymap.validate()
                self.assertEqual(keymap.has_physical, name == "wasd-physical")
        with self.assertRaises(KeymapError):
            Keymap.preset("dvorak")

    def test_default_table(self):
        keymap = Keymap.preset("default")
        lookups = {
            ("play", "q"): ("left", "control"),
            ("play", "z"): ("fire", "control"),
            ("play", "s"): ("stop", "control"),
            ("play", "down"): ("stop", "control"),
            ("play", "x"): ("bomb", "pulse"),
            ("play", "pause"): ("pause", "command"),
            ("play", "m"): ("sound", "command"),
            ("play", "f1"): ("stats", "command"),
            ("countdown", "space"): ("fire", "control"),
            ("countdown", "escape"): ("pause", "command"),
            ("pause", "enter"): ("resume", "command"),
            ("pause", "r"): ("restart", "command"),
            ("pause", "q"): ("menu", "command"),
            ("menu", "z"): ("up", "navigation"),
            ("menu", "q"): ("left", "navigation"),
            ("menu", "escape"): ("back", "command"),
            ("pages", "q"): ("page", "navigation"),
            ("pages", "backspace"): ("back", "command"),
            ("handoff", "space"): ("start", "command"),
            ("handoff", "p"): ("pause", "command"),
            ("results", "q"): ("menu", "command"),
            ("replay", "space"): ("pause", "command"),
            ("replay_pause", "q"): ("exit", "command"),
            ("replay_results", "enter"): ("exit", "command"),
        }
        for (context, name), expected in lookups.items():
            with self.subTest(context=context, key=name):
                self.assertEqual(keymap.lookup(context, Key(name)), expected)
        unbound = (
            ("play", "r"),
            ("play", "enter"),
            ("play", "f"),
            ("countdown", "b"),
            ("handoff", "q"),
            ("replay", "r"),
        )
        for context, name in unbound:
            self.assertIsNone(keymap.lookup(context, Key(name)), (context, name))

    def test_azerty_zqsd_plays(self):
        keymap = Keymap.preset("default")
        actions = {name: keymap.lookup("play", Key(name))[0] for name in "zqsd"}
        self.assertEqual(actions, {"z": "fire", "q": "left", "s": "stop", "d": "right"})

    def test_q_in_play_never_leaves(self):
        for name in PRESETS:
            keymap = Keymap.preset(name)
            for context in ("play", "countdown", "handoff"):
                for action in keymap.contexts[context]:
                    self.assertNotIn(action, ABANDON, (name, context))
                hit = keymap.lookup(context, Key("q"))
                self.assertTrue(hit is None or hit[0] not in ABANDON)

    def test_presets_change_play_and_menus(self):
        arrows, vim = Keymap.preset("arrows"), Keymap.preset("vim")
        self.assertEqual(arrows.keys_for("play", "fire"), ("space", "up"))
        self.assertIsNone(arrows.lookup("menu", Key("w")))
        self.assertEqual(vim.lookup("play", Key("k")), ("fire", "control"))
        self.assertEqual(vim.lookup("menu", Key("j")), ("down", "navigation"))
        self.assertEqual(vim.keys_for("play", "bomb"), ("b", "x"))

    def test_countdown_mirrors_play_controls(self):
        for name in PRESETS:
            keymap = Keymap.preset(name)
            for action in ("left", "right", "stop", "fire"):
                self.assertEqual(keymap.keys_for("countdown", action), keymap.keys_for("play", action))


class LookupTests(unittest.TestCase):
    def test_global_comes_first(self):
        self.assertEqual(Keymap.preset("default").lookup("results", Key("m")), ("sound", "command"))

    def test_logical_before_physical(self):
        keymap = Keymap(DEFAULT | {"play": DEFAULT["play"] | {"left": ("@a",), "right": ("q", "d")}})
        self.assertEqual(keymap.lookup("play", Key("q", base="a")), ("right", "control"))  # AZERTY q at US a
        self.assertEqual(keymap.lookup("play", Key("y", base="a")), ("left", "control"))
        self.assertEqual(keymap.lookup("play", Key("a")), ("left", "control"))  # no flag 4, or base == key
        self.assertIsNone(keymap.lookup("play", Key("a", base="q")))  # AZERTY a sits at US q

    def test_wasd_physical_on_azerty(self):
        keymap = Keymap.preset("wasd-physical")
        self.assertEqual(keymap.lookup("play", Key("z", base="w")), ("fire", "control"))
        self.assertEqual(keymap.lookup("play", Key("q", base="a")), ("left", "control"))
        self.assertIsNone(keymap.lookup("play", Key("a", base="q")))
        self.assertEqual(keymap.lookup("play", Key("x")), ("bomb", "pulse"))

    def test_shift_and_locks_are_ignored(self):
        keymap = Keymap.preset("default")
        for mods in (SHIFT, CAPS_LOCK, NUM_LOCK, SHIFT | CAPS_LOCK):
            self.assertEqual(keymap.lookup("play", Key("p", mods)), ("pause", "command"))
            self.assertEqual(keymap.lookup("pause", Key("c", CTRL | mods)), ("sig_int", "signal"))

    def test_alt_ctrl_and_super_must_match(self):
        keymap = Keymap.preset("default")
        for mods in (ALT, CTRL, SUPER, ALT | SHIFT):
            self.assertIsNone(keymap.lookup("pause", Key("q", mods)))  # Cmd+Q and Alt+Q never mean q
        self.assertEqual(keymap.lookup("play", Key("l", CTRL)), ("redraw", "command"))
        self.assertIsNone(keymap.lookup("play", Key("l", CTRL | ALT)))

    def test_signal_keys(self):
        keymap = Keymap.preset("default")
        for name, action in (("c", "sig_int"), ("z", "sig_tstp"), ("\\", "sig_quit")):
            self.assertEqual(keymap.lookup("menu", Key(name, CTRL)), (action, "signal"))

    def test_keys_for(self):
        keymap = Keymap.preset("default")
        self.assertEqual(keymap.keys_for("play", "left"), ("left", "a", "q", "h"))
        self.assertEqual(keymap.keys_for("play", "nothing"), ())
        self.assertEqual(keymap.keys_for("nowhere", "left"), ())


class SpecTests(unittest.TestCase):
    def test_grammar(self):
        cases = {
            "q": (0, "q", False),
            "Q": (0, "q", False),
            "ctrl+l": (CTRL, "l", False),
            "Alt+Ctrl+x": (ALT | CTRL, "x", False),
            "super+@a": (SUPER, "a", True),
            "@w": (0, "w", True),
            "@": (0, "@", False),
            "+": (0, "+", False),
            "ctrl++": (CTRL, "+", False),
            "Space": (0, "space", False),
            "F12": (0, "f12", False),
            "page_down": (0, "page_down", False),
            "é": (0, "é", False),
            "ctrl+\\": (CTRL, "\\", False),
        }
        for spec, parsed in cases.items():
            self.assertEqual(parse_spec(spec), parsed, spec)

    def test_bad_specs(self):
        for spec in ("", " ", "ctrl+", "ab", "f36", "shift+a", "@ab", "hyper+a", 7, None):
            with self.subTest(spec=spec), self.assertRaises(KeymapError):
                parse_spec(spec)


def edited(**contexts):
    merged = {ctx: dict(actions) for ctx, actions in DEFAULT.items()}
    for ctx, actions in contexts.items():
        merged.setdefault(ctx, {}).update(actions)
    merged["countdown"] = merged["countdown"] | {a: merged["play"][a] for a in ("left", "right", "stop", "fire")}
    return Keymap(_mirror_menu(merged))  # the settings and initials cards follow the menu, as in preset()


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, keymap, fragment):
        with self.assertRaises(KeymapError) as caught:
            keymap.validate()
        self.assertIn(fragment, str(caught.exception))

    def test_duplicate_keys(self):
        self.assert_invalid(edited(play={"bomb": ("b", "q")}), "bound twice in play")
        self.assert_invalid(edited(play={"bomb": ("b", "B")}), "bound twice in play")
        self.assert_invalid(edited(menu={"select": ("enter", "m")}), "reuses a key of global.sound")

    def test_reserved_ctrl_keys(self):
        self.assert_invalid(edited(play={"bomb": ("ctrl+c",)}), "reserved for ctrl+c")
        self.assert_invalid(edited(play={"bomb": ("ctrl+@z",)}), "reserved for ctrl+z")
        self.assert_invalid(edited(**{"global": {"sig_int": ("ctrl+x",)}}), "global.sig_int must stay bound")
        edited(play={"bomb": ("ctrl+alt+c", "b")}).validate()

    def test_quit_in_play(self):
        self.assert_invalid(edited(play={"menu": ("y",)}), "only possible from the pause menu")
        self.assert_invalid(edited(handoff={"restart": ("y",)}), "only possible from the pause menu")

    def test_unknown_contexts_actions_and_keys(self):
        self.assert_invalid(edited(lobby={"go": ("g",)}), "Unknown context: lobby")
        self.assert_invalid(edited(play={"jump": ("g",)}), "Unknown action: play.jump")
        self.assert_invalid(edited(play={"bomb": ("bb",)}), "Unknown key")
        self.assertIsNone(edited(play={"jump": ("g",), "bomb": ("bb", "b")}).lookup("play", Key("g")))

    def test_every_context_keeps_a_way_out(self):
        self.assert_invalid(edited(pages={"back": ()}), "pages needs a key")
        self.assert_invalid(edited(pause={"resume": (), "restart": (), "menu": ()}), "pause needs a key")
        edited(pause={"resume": (), "restart": ()}).validate()

    def test_every_play_action_but_autofire_needs_a_key(self):
        self.assertEqual(OPTIONAL_PLAY, {"autofire"})
        for action in ACTIONS["play"].keys() - OPTIONAL_PLAY:
            self.assert_invalid(
                edited(play={action: ()}), "play needs a key" if action == "pause" else f"play.{action}"
            )
        edited(play={"autofire": ()}).validate()


class AutofireTests(unittest.TestCase):
    """Spec 16.1: F is no longer bound, and play.autofire stays an action a keymap file may bind."""

    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.json"
            path.write_text(json.dumps(data))
            return Keymap.load(path)

    def test_every_preset_loads_with_autofire_and_f_unbound(self):
        for name in PRESETS:
            with self.subTest(preset=name):
                keymap = Keymap.preset(name)
                self.assertEqual(keymap.keys_for("play", "autofire"), ())
                self.assertEqual(self.load({"preset": name}).contexts, keymap.contexts)
                for context in ("play", "countdown"):
                    self.assertIsNone(keymap.lookup(context, Key("f")))
        self.assertNotIn("autofire", DEFAULT["play"])

    def test_a_file_that_binds_autofire_still_loads(self):
        keymap = self.load({"bind": {"play.autofire": ["f"]}})
        self.assertEqual(keymap.lookup("play", Key("f")), ("autofire", "pulse"))
        arrows = self.load({"preset": "arrows", "bind": {"play.bomb": ["n"]}})  # test_app's keymap file
        self.assertEqual((arrows.keys_for("play", "bomb"), arrows.keys_for("play", "autofire")), (("n",), ()))

    def test_a_file_that_freed_f_for_another_action_still_loads(self):
        keymap = self.load({"unbind": ["play.autofire:f"], "bind": {"play.bomb": ["b", "f"]}})
        self.assertEqual(keymap.lookup("play", Key("f")), ("bomb", "pulse"))
        with self.assertRaises(KeymapError) as caught:
            self.load({"unbind": ["play.bomb:f"]})  # any other action's key must be bound to be unbound
        self.assertIn("not bound", str(caught.exception))

    def test_unbinding_every_key_of_any_other_play_action_is_still_refused(self):
        for action in ACTIONS["play"].keys() - OPTIONAL_PLAY:
            keys = Keymap.preset("default").keys_for("play", action)
            with self.subTest(action=action), self.assertRaises(KeymapError) as caught:
                self.load({"unbind": [f"play.{action}:{key}" for key in keys]})
            self.assertIn("play needs a key" if action == "pause" else f"play.{action}", str(caught.exception))


class LoadTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.json"
            path.write_text(data if isinstance(data, str) else json.dumps(data))
            return Keymap.load(path)

    def test_preset_bind_and_unbind(self):
        keymap = self.load({"preset": "default", "bind": {"play.fire": ["space", "up"]}, "unbind": ["play.left:q"]})
        self.assertEqual(keymap.keys_for("play", "fire"), ("space", "up"))
        self.assertEqual(keymap.keys_for("countdown", "fire"), ("space", "up"))
        self.assertIsNone(keymap.lookup("play", Key("q")))
        self.assertIsNone(keymap.lookup("play", Key("z")))
        self.assertEqual(self.load({"preset": "vim"}).keys_for("play", "left"), ("h",))
        self.assertEqual(self.load({}).keys_for("play", "left"), ("left", "a", "q", "h"))

    def test_rebinding_a_key_needs_an_unbind(self):
        data = {"bind": {"play.bomb": ["q"]}}
        with self.assertRaises(KeymapError):
            self.load(data)
        keymap = self.load(data | {"unbind": ["play.left:q"]})
        self.assertEqual(keymap.lookup("play", Key("q")), ("bomb", "pulse"))
        self.assertEqual(
            self.load({"unbind": ["pages.page::"], "bind": {"pages.page": [":", "a"]}}).keys_for("pages", "page"),
            ("a",),
        )

    def test_errors(self):
        cases = {
            "{": "Cannot read keymap",
            "[]": "only 'preset', 'bind' and 'unbind'",
            json.dumps({"keys": {}}): "only 'preset', 'bind' and 'unbind'",
            json.dumps({"preset": "nope"}): "Unknown keymap preset",
            json.dumps({"preset": ["default"]}): "Unknown keymap preset",
            json.dumps({"bind": []}): "'bind' maps",
            json.dumps({"unbind": "play.left:q"}): "'bind' maps",
            json.dumps({"bind": {"fire": ["x"]}}): "Unknown binding target",
            json.dumps({"bind": {"lobby.fire": ["x"]}}): "Unknown binding target",
            json.dumps({"bind": {"play.fire": "space"}}): "must list its keys",
            json.dumps({"bind": {"play.fire": [3]}}): "must be a string",
            json.dumps({"bind": {"play.menu": ["y"]}}): "only possible from the pause menu",
            json.dumps({"bind": {"countdown.fire": ["space"]}}): "follows play.fire",
            json.dumps({"unbind": ["play.left:y"]}): "not bound",
            json.dumps({"unbind": [5]}): "Unknown binding target",
        }
        for data, fragment in cases.items():
            with self.subTest(data=data), self.assertRaises(KeymapError) as caught:
                self.load(data)
            self.assertIn(fragment, str(caught.exception))
        with self.assertRaises(KeymapError):
            Keymap.load(Path("/nonexistent/keys.json"))


# Keymap files a Phase 4 build loads: test_app.py's and this file's own, and some made for Phase 5 (one moves the
# sound to a letter the menu does not use; others rebind the menu to letters, or give it Backspace).
PHASE4_FILES = (
    {},
    {"preset": "arrows", "bind": {"play.bomb": ["n"]}},
    {"preset": "vim"},
    {"preset": "wasd-physical"},
    {"bind": {"play.autofire": ["f"]}},
    {"unbind": ["play.autofire:f"], "bind": {"play.bomb": ["b", "f"]}},
    {"preset": "default", "bind": {"play.fire": ["space", "up"]}, "unbind": ["play.left:q"]},
    {"bind": {"play.bomb": ["q"]}, "unbind": ["play.left:q"]},
    {"unbind": ["pages.page::"], "bind": {"pages.page": [":", "a"]}},
    {"bind": {"global.sound": ["y"]}},
    {"bind": {"menu.select": ["e"]}},
    {"bind": {"menu.back": ["q"], "menu.left": ["left", "a", "h"]}},
    {"bind": {"menu.back": ["escape", "backspace"]}},
    {"bind": {"menu.select": ["enter", "space", "1"], "menu.up": ["up", "w", "8"]}},
)


class Phase5ContextTests(unittest.TestCase):
    """Phase 5 spec 10: settings takes the menu's keys, initials the menu's but the printable ones plus erase on
    Backspace, and attract binds nothing; no new key, so every Phase 4-valid keymap file stays valid."""

    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.json"
            path.write_text(json.dumps(data))
            return Keymap.load(path)

    def keymaps(self):
        for name in PRESETS:
            yield f"preset {name}", Keymap.preset(name)
        for data in PHASE4_FILES:
            yield json.dumps(data), self.load(data)

    def test_the_new_contexts(self):
        self.assertEqual(MENU_MIRRORS, ("settings", "initials"))
        self.assertEqual(ACTIONS["settings"], ACTIONS["menu"])
        self.assertEqual(ACTIONS["initials"], ACTIONS["menu"] | {"erase": "command"})
        self.assertEqual(ACTIONS["attract"], {})
        self.assertEqual(WAYS_OUT["settings"], ("back",))
        self.assertEqual(WAYS_OUT["initials"], ("select", "back"))
        self.assertNotIn("attract", WAYS_OUT)  # it binds nothing: any key ends the demo

    def test_settings_mirrors_the_menu(self):
        for label, keymap in self.keymaps():
            with self.subTest(keymap=label):
                self.assertEqual(keymap.contexts["settings"], keymap.contexts["menu"])

    def test_initials_binds_no_letter_digit_or_space(self):
        for label, keymap in self.keymaps():
            with self.subTest(keymap=label):
                for action, specs in keymap.contexts["initials"].items():
                    for spec in specs:
                        self.assertFalse(printable(spec), (action, spec))
                        mods, name, _ = parse_spec(spec)
                        self.assertFalse(len(name) == 1 and name.isalnum(), (action, spec))
                self.assertTrue(keymap.keys_for("initials", "select") or keymap.keys_for("initials", "back"))
                self.assertIsNone(keymap.lookup("initials", Key("t")))
                self.assertIsNone(keymap.lookup("initials", Key("space")))

    def test_initials_keys_with_the_default_keymap(self):
        keymap = Keymap.preset("default")
        self.assertEqual(keymap.keys_for("initials", "up"), ("up",))
        self.assertEqual(keymap.keys_for("initials", "down"), ("down",))
        self.assertEqual(keymap.keys_for("initials", "left"), ("left",))
        self.assertEqual(keymap.keys_for("initials", "right"), ("right",))
        self.assertEqual(keymap.keys_for("initials", "select"), ("enter",))
        self.assertEqual(keymap.keys_for("initials", "back"), ("escape",))
        self.assertEqual(keymap.lookup("initials", Key("backspace")), ("erase", "command"))
        self.assertEqual(keymap.lookup("settings", Key("q")), ("left", "navigation"))  # the menu's AZERTY left
        self.assertEqual(keymap.lookup("settings", Key("escape")), ("back", "command"))
        self.assertIsNone(keymap.lookup("initials", Key("q")))
        self.assertEqual(keymap.lookup("initials", Key("m")), ("sound", "command"))  # Session.handle types it first
        self.assertIsNone(keymap.lookup("attract", Key("enter")))

    def test_a_menu_reached_only_by_letters_still_has_a_way_out_of_initials(self):
        keymap = self.load({"bind": {"menu.select": ["e"]}})
        self.assertEqual(keymap.keys_for("initials", "select"), ("enter",))
        keymap = self.load({"bind": {"menu.back": ["q"], "menu.left": ["left", "a", "h"]}})
        self.assertEqual(keymap.keys_for("initials", "back"), ("escape",))

    def test_backspace_in_the_menu_leaves_no_erase(self):
        keymap = self.load({"bind": {"menu.back": ["escape", "backspace"]}})
        self.assertEqual(keymap.keys_for("initials", "back"), ("escape", "backspace"))
        self.assertEqual(keymap.keys_for("initials", "erase"), ())

    def test_a_file_rebinding_the_menu_moves_the_cards(self):
        keymap = self.load({"bind": {"menu.select": ["enter", "space", "1"], "menu.up": ["up", "w", "8"]}})
        self.assertEqual(keymap.keys_for("settings", "select"), ("enter", "space", "1"))
        self.assertEqual(keymap.keys_for("initials", "select"), ("enter",))
        self.assertEqual(keymap.keys_for("initials", "up"), ("up",))

    def test_the_cards_follow_the_menu_and_cannot_be_bound(self):
        for data, fragment in (
            ({"bind": {"settings.back": ["x"]}}, "follows menu.back"),
            ({"bind": {"initials.erase": ["delete"]}}, "follows menu.erase"),
            ({"unbind": ["settings.back:escape"]}, "follows menu.back"),
            ({"bind": {"attract.back": ["x"]}}, "Unknown action: attract.back"),
        ):
            with self.subTest(data=data), self.assertRaises(KeymapError) as caught:
                self.load(data)
            self.assertIn(fragment, str(caught.exception))
