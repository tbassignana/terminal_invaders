"""Corrupt local data and unavailable locks cannot interrupt the game."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_invaders.model import Config
from terminal_invaders.replay import Playback
from terminal_invaders.storage import Profile, atomic_json


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
        self.assertEqual(self.path.with_suffix(".json.bak").read_text(), original)
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


if __name__ == "__main__":
    unittest.main()
