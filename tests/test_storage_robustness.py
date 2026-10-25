"""Corrupt local data and unavailable locks cannot interrupt the game."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_invaders.model import Config
from terminal_invaders.replay import Playback
from terminal_invaders.storage import MAX_DEPTH, Profile, atomic_json


class StorageRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "profile.json"

    def test_deep_profile_falls_back_and_retains_recoverable_original(self):
        original = "[" * 10000 + "]" * 10000
        self.path.write_text(original)
        profile = Profile(self.path)
        self.assertIn("Profile could not be loaded", profile.error)
        self.assertEqual(profile.data["stats"]["runs"], 0)
        self.assertTrue(profile.save())
        (moved,) = self.path.parent.glob("profile.json.corrupt-*")
        self.assertEqual(moved.read_text(), original)
        self.assertIsNone(Profile(self.path).error)

    def test_deep_replay_is_a_validation_error(self):
        self.path.write_text("[" * 10000 + "]" * 10000)
        with self.assertRaisesRegex(ValueError, "invalid replay structure"):
            Playback(self.path)

    def test_non_object_replay_is_a_structure_error(self):
        for value in ([], None, True, 42, "replay"):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, "invalid replay structure"):
                    Playback(self.path)

    def test_decoder_recursion_is_a_structure_error(self):
        self.path.write_text("[]")
        with patch("terminal_invaders.replay.json.loads", side_effect=RecursionError):
            with self.assertRaisesRegex(ValueError, "invalid replay structure"):
                Playback(self.path)

    def test_unsupported_version_has_a_distinct_error(self):
        self.path.write_text(json.dumps({"version": 999}))
        with self.assertRaisesRegex(ValueError, "unsupported replay version"):
            Playback(self.path)

    def test_unavailable_file_lock_records_exactly_once(self):
        profile = Profile(self.path)
        result = dict(score=120, wave=1, victory=False, kills=2, shots=3, hits=2, ticks=300, best_combo=2)
        with patch("fcntl.flock", side_effect=OSError("filesystem does not support locks")):
            unlocked = profile.record(result, Config(seed=42), "ACE")
        self.assertEqual(unlocked, ["First contact"])
        persisted = Profile(self.path)
        self.assertEqual(persisted.data["stats"]["runs"], 1)
        self.assertEqual(persisted.data["stats"]["kills"], 2)
        self.assertEqual(len(persisted.data["scores"]), 1)
        self.assertEqual(persisted.data["scores"][0]["score"], 120)

    def test_successful_reload_clears_an_old_read_error(self):
        self.path.write_text("bad json")
        profile = Profile(self.path)
        self.assertIsNotNone(profile.error)
        atomic_json(self.path, {"version": 1, "name": "FIXED"})
        data = profile._read()
        self.assertIsNone(profile.error)
        self.assertEqual(data["name"], "FIXED")

    def test_unknown_score_fields_cannot_poison_the_next_save(self):
        score = dict(
            mode="classic",
            difficulty="normal",
            seed=2,
            score=100,
            wave=1,
            date="2026-09-17",
            victory=False,
            name="ACE",
            unexpected=float("nan"),
        )
        self.path.write_text(json.dumps({"version": 1, "scores": [score]}))
        profile = Profile(self.path)
        self.assertNotIn("unexpected", profile.data["scores"][0])
        self.assertTrue(profile.save())
        self.assertEqual(Profile(self.path).data["scores"][0]["score"], 100)

    def test_unavailable_file_lock_saves_settings_exactly_once(self):
        profile = Profile(self.path)
        with patch("fcntl.flock", side_effect=OSError("filesystem does not support locks")):
            self.assertTrue(profile.update_settings({"palette": "cvd"}, name="ace"))
            self.assertTrue(profile.update_settings({"palette": "cvd"}))
        persisted = json.loads(self.path.read_text())
        self.assertEqual(persisted["settings"], {"palette": "cvd"})
        self.assertEqual(persisted["name"], "ACE")

    def test_settings_values_that_cannot_be_written_back_cannot_poison_the_next_save(self):
        self.path.write_text('{"version": 2, "settings": {"palette": "cvd", "gamma": NaN, "deep": [[Infinity]]}}')
        profile = Profile(self.path)
        self.assertEqual(profile.settings, {"palette": "cvd"})  # per key: the rest of the object stays
        self.assertTrue(profile.update_settings({"effects": "calm"}))
        self.assertEqual(json.loads(self.path.read_text())["settings"], {"palette": "cvd", "effects": "calm"})
        self.path.write_text('{"version": 2, "settings": NaN}')  # not an object, and not writable JSON either
        profile = Profile(self.path)
        self.assertEqual(profile.settings, {})
        self.assertFalse(profile.settings_unreadable)
        self.assertTrue(profile.update_settings({"effects": "calm"}))
        self.assertEqual(json.loads(self.path.read_text())["settings"], {"effects": "calm"})

    def test_deep_settings_fall_back_with_the_rest_of_the_profile(self):
        # Just over the limit (every Python decodes it), 10,000 (3.14 and later decode it) and 200,000 (none does):
        # the same fallback and the same message on every Python, wherever the depth is, and nothing destroyed.
        row = json.dumps(dict(mode="classic", difficulty="normal", seed=2, score=1, wave=1, date="d", victory=False))
        for levels in (MAX_DEPTH, 10_000, 200_000):
            deep = "[" * levels + "]" * levels
            places = {
                "settings": '{"version": 2, "settings": %s}',
                "a setting": '{"version": 2, "settings": {"palette": "cvd", "x": %s}}',
                "another key": '{"version": 2, "hologram": %s}',
                "stats": '{"version": 2, "stats": {"runs": 3, "x": %s}}',
                "a score": '{"version": 2, "scores": [%s]}' % row.replace("}", ', "x": %s}'),
            }
            for place, shape in places.items():
                with self.subTest(levels=levels, place=place):
                    original = shape % deep
                    self.path.write_text(original)
                    profile = Profile(self.path)
                    self.assertIn(f"Profile could not be loaded (nested more than {MAX_DEPTH} levels", profile.error)
                    self.assertEqual(profile.settings, {})
                    self.assertTrue(profile.update_settings({"palette": "cvd"}))
                    (moved,) = self.path.parent.glob("profile.json.corrupt-*")
                    self.assertEqual(moved.read_text(), original)
                    moved.unlink()
                    self.assertEqual(Profile(self.path).settings, {"palette": "cvd"})

    def test_a_profile_at_the_depth_limit_loads_plays_and_saves_unchanged(self):
        nest = lambda levels: "[" * levels + "]" * levels  # noqa: E731
        row = dict(mode="classic", difficulty="normal", seed=2, score=1, wave=1, date="d", victory=False)
        original = (  # each deep value reaches level MAX_DEPTH, the file's own object being level 1
            f'{{"version": 2, "hologram": {nest(MAX_DEPTH - 1)}, "stats": {{"x": {nest(MAX_DEPTH - 2)}}}, '
            f'"settings": {{"palette": "cvd", "x": {nest(MAX_DEPTH - 2)}}}, '
            f'"scores": [{json.dumps(row)[:-1]}, "x": {nest(MAX_DEPTH - 3)}}}]}}'
        )
        self.path.write_text(original)
        profile = Profile(self.path)
        self.assertIsNone(profile.error)

        def deeper(calls, method, *args):  # the save runs from deep in the game's own calls
            return method(*args) if calls == 0 else deeper(calls - 1, method, *args)

        self.assertTrue(deeper(200, profile.update_settings, {"effects": "calm"}))
        result = dict(score=120, wave=1, victory=False, kills=2, shots=3, hits=2, ticks=300, best_combo=2)
        deeper(200, profile.record, result, Config(seed=42), "ACE")
        self.assertIsNotNone(profile.last_placing)
        saved, before = json.loads(self.path.read_text()), json.loads(original)
        self.assertEqual(saved["hologram"], before["hologram"])
        self.assertEqual(saved["stats"]["x"], before["stats"]["x"])
        self.assertEqual(saved["settings"], {**before["settings"], "effects": "calm"})
        self.assertIn(before["scores"][0], saved["scores"])
        self.assertEqual(list(self.path.parent.glob("profile.json.*")), [])


if __name__ == "__main__":
    unittest.main()
