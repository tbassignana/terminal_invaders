"""Audio failures, ownership, and bounded playback without launching afplay."""

import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from terminal_invaders.audio import Audio


class Player:
    def __init__(self, args, **kwargs):
        self.args = args
        self.returncode = None
        self.calls = []
        self.stubborn = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        if not self.stubborn:
            self.returncode = 0

    def kill(self):
        self.calls.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.calls.append("wait")
        # Cleanup must happen only after the player has released the input file.
        assert Path(self.args[-1]).exists()
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.players = []
        self.platform = patch("terminal_invaders.audio.sys.platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.player_path = patch("terminal_invaders.audio._PLAYER")
        player_path = self.player_path.start()
        player_path.is_file.return_value = True
        player_path.__str__.return_value = "/usr/bin/afplay"
        self.addCleanup(self.player_path.stop)
        self.spawn = patch("terminal_invaders.audio.subprocess.Popen", side_effect=self.make_player).start()
        self.addCleanup(patch.stopall)
        self.clock = patch("terminal_invaders.audio.time.monotonic", return_value=1.0).start()

    def make_player(self, *args, **kwargs):
        player = Player(*args, **kwargs)
        self.players.append(player)
        return player

    def audio(self, **kwargs):
        audio = Audio(**kwargs)
        self.addCleanup(audio.close)
        return audio

    def test_disabled_platform_never_allocates_files_or_processes(self):
        with patch("terminal_invaders.audio.sys.platform", "linux"):
            audio = self.audio()
        with patch("terminal_invaders.audio.tempfile.TemporaryDirectory") as directory:
            self.assertFalse(audio.enabled)
            self.assertFalse(audio.toggle())
            audio.update(["fire"])
            directory.assert_not_called()
            self.spawn.assert_not_called()

    def test_disabled_option_can_be_toggled_on(self):
        audio = self.audio(enabled=False)
        audio.update(["fire"])
        self.spawn.assert_not_called()
        self.assertTrue(audio.toggle())
        audio.update(["fire"])
        self.assertEqual(len(self.players), 1)
        self.assertFalse(audio.toggle())
        self.assertEqual(self.players[0].calls, ["terminate", "wait"])

    def test_effects_are_valid_wave_files_and_shell_free(self):
        audio = self.audio()
        audio.update(["fire"])
        args, kwargs = self.spawn.call_args
        self.assertEqual(args[0][:3], ["/usr/bin/afplay", "-v", "0.55"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        with wave.open(args[0][-1]) as sound:
            self.assertEqual((sound.getnchannels(), sound.getsampwidth()), (1, 2))
            self.assertEqual(sound.getframerate(), 22050)
            self.assertGreater(sound.getnframes(), 1000)

    def test_effect_limit_prioritizes_damage_and_deduplicates(self):
        audio = self.audio()
        audio.update(["fire", "kill", "damage", "damage", "bomb", "victory"])
        self.assertEqual([Path(p.args[-1]).stem for p in self.players], ["damage", "bomb", "victory"])
        audio.update(["fire"])
        self.assertEqual(len(self.players), 3)

    def test_completed_effects_are_reaped_and_shots_rate_limited(self):
        audio = self.audio()
        audio.update(["fire"])
        self.players[0].returncode = 0
        self.clock.return_value = 1.05
        audio.update(["fire"])
        self.assertEqual(len(self.players), 1)
        self.clock.return_value = 1.2
        audio.update(["fire"])
        self.assertEqual(len(self.players), 2)

    def test_music_stops_on_pause_while_final_cues_finish(self):
        with tempfile.TemporaryDirectory() as folder:
            music = Path(folder) / "music with spaces.wav"
            music.write_bytes(b"test")
            unrelated = Player(["/usr/bin/afplay", str(music)])
            audio = self.audio(music=music)
            audio.update([], playing=False)
            self.assertEqual(len(self.players), 0)
            audio.update([])
            self.assertEqual(self.players[0].args[-1], str(music.resolve()))
            self.players[0].returncode = 0
            audio.update([])
            self.assertEqual(len(self.players), 2)
            audio.update(["victory"])
            audio.update([], playing=False)
            self.assertEqual(self.players[1].calls, ["terminate", "wait"])
            self.assertEqual(self.players[2].calls, [])
            self.players[2].returncode = 0
            audio.update([], playing=False)
            self.assertEqual(audio._effects, [])
            self.assertEqual(unrelated.calls, [])

    def test_close_kills_stubborn_owned_child_before_cleaning_files(self):
        audio = self.audio()
        audio.update(["fire"])
        folder = Path(self.players[0].args[-1]).parent
        self.players[0].stubborn = True
        audio.close()
        self.assertEqual(self.players[0].calls, ["terminate", "wait", "kill", "wait"])
        self.assertFalse(folder.exists())
        audio.close()
        self.assertFalse(audio.toggle())

    def test_spawn_failure_silently_disables_audio_and_reaps_existing_child(self):
        audio = self.audio()
        audio.update(["fire"])
        folder = Path(self.players[0].args[-1]).parent
        self.spawn.side_effect = OSError("device unavailable")
        audio.update(["damage"])
        self.assertFalse(audio.enabled)
        self.assertEqual(self.players[0].calls, ["terminate", "wait"])
        self.assertFalse(folder.exists())

    def test_bad_player_exit_disables_audio_and_stops_other_owned_children(self):
        audio = self.audio()
        audio.update(["damage", "fire"])
        self.players[0].returncode = 1
        audio.update([])
        self.assertFalse(audio.enabled)
        self.assertEqual(self.players[0].calls, [])
        self.assertEqual(self.players[1].calls, ["terminate", "wait"])

    def test_unknown_events_and_missing_music_do_not_spawn(self):
        audio = self.audio(music=Path("/missing/terminal-invaders-audio.wav"))
        audio.update(["not-a-cue"])
        self.spawn.assert_not_called()
        self.assertIsNone(audio._directory)

    def test_unwritable_temporary_directory_is_nonfatal(self):
        audio = self.audio()
        with patch("terminal_invaders.audio.tempfile.TemporaryDirectory", side_effect=PermissionError):
            audio.update(["fire"])
        self.assertFalse(audio.enabled)
        self.spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
