"""The game's side of the sound (terminal_invaders.audio.Audio) against a fake helper on real pipes: the bytes each
update() sends, the repeat gaps, M, the status lines, and a close() that always sweeps the group."""

import io
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from terminal_invaders import app, cli
from terminal_invaders.arcade import machine
from terminal_invaders.audio import BOOT, ROOT, Audio
from terminal_invaders.audio.protocol import CUT, QUIT, cue_byte


class FakeHelper:
    """What Audio sees of its helper: real pipes, the argv it was started with, and a pid that only the fake killpg
    below knows. It never exits by itself unless exit() closes its status pipe, like a hung helper."""

    def __init__(self, argv, **options):
        self.argv, self.options = argv, options
        self.inbox, write = os.pipe()  # the game writes messages into this pipe
        read, self.outbox = os.pipe()  # and reads status lines from this one
        self.stdin, self.stdout = open(write, "wb", buffering=0), open(read, "rb", buffering=0)
        os.set_blocking(self.inbox, False)
        self.pid = 40000 + id(self) % 10000
        self.calls: list[str] = []
        self.killed = False

    def received(self) -> bytes:
        data = b""
        while True:
            try:
                chunk = os.read(self.inbox, 65536)
            except BlockingIOError:
                return data
            if not chunk:
                return data
            data += chunk

    def say(self, text: bytes) -> None:
        os.write(self.outbox, text)

    def exit(self) -> None:
        """The helper exits: its end of the status pipe closes."""
        if self.outbox is not None:
            os.close(self.outbox)
            self.outbox = None

    def wait(self, timeout=None):
        self.calls.append("wait")
        if not self.killed:
            raise subprocess.TimeoutExpired("helper", timeout)
        return -9

    def close(self) -> None:
        for fd in (self.inbox, self.outbox):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.inbox = self.outbox = None


class Case(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.helpers: list[FakeHelper] = []
        patcher = patch.dict(os.environ, {"INVADERS_AUDIO": "file:/dev/null"})
        patcher.start()
        self.addCleanup(patcher.stop)
        killpg = patch("os.killpg", side_effect=self.killpg)
        self.killpg_calls = killpg.start().call_args_list
        self.addCleanup(killpg.stop)

    def tearDown(self):
        for helper in self.helpers:
            helper.close()
            for pipe in (helper.stdin, helper.stdout):
                pipe.close()

    def popen(self, argv, **options):
        helper = FakeHelper(argv, **options)
        self.helpers.append(helper)
        return helper

    def killpg(self, pid, sig):
        for helper in self.helpers:
            if helper.pid == pid:
                helper.calls.append("killpg")
                helper.killed = True
                return
        raise ProcessLookupError

    def audio(self, **options) -> Audio:
        return Audio(clock=lambda: self.now, popen=self.popen, **options)


class MessagesTests(Case):
    def test_the_helper_starts_in_its_own_session(self):
        audio = self.audio()
        (helper,) = self.helpers
        self.assertEqual(helper.argv, [sys.executable, "-I", "-S", "-c", BOOT, str(ROOT)])
        self.assertEqual(ROOT, Path(__file__).resolve().parents[1])
        self.assertTrue(helper.options["start_new_session"])
        self.assertEqual((helper.options["stderr"], helper.options["cwd"]), (subprocess.DEVNULL, "/"))
        self.assertFalse(os.get_blocking(helper.stdin.fileno()) or os.get_blocking(helper.stdout.fileno()))
        self.assertEqual(audio.pid, helper.pid)

    def test_each_update_sends_its_new_cues_in_one_write(self):
        audio = self.audio()
        (helper,) = self.helpers
        audio.update(["fire", "kill"])
        self.assertEqual(helper.received(), cue_byte("fire") + cue_byte("kill"))
        audio.update(())
        audio.update()
        self.assertEqual(helper.received(), b"")  # nothing new, nothing written
        audio.update(["damage", "not-a-cue"])
        self.assertEqual(helper.received(), cue_byte("damage"))

    def test_repeat_gaps_drop_a_cue_too_soon_after_the_last_and_duplicates_count_once(self):
        audio = self.audio()
        (helper,) = self.helpers
        audio.update(["fire", "fire"])
        self.assertEqual(helper.received(), cue_byte("fire"))
        self.now = 0.119
        audio.update(["fire", "kill"])
        self.assertEqual(helper.received(), cue_byte("kill"))  # fire's gap is 120 ms
        self.now = 0.120
        audio.update(["fire"])
        self.assertEqual(helper.received(), cue_byte("fire"))

    def test_arcade_gaps_keep_the_saucer_drones_and_drop_the_game_overs_second_blast(self):
        audio = self.audio()
        (helper,) = self.helpers
        frame = 1 / 60
        for start, (name, every) in enumerate(machine._REPEATS.items()):  # re-sent while their bits stay on
            sent = []
            for k, late in enumerate((0, 0.03, 0, 0.03, 0)):  # every other repeat run late by a stall or a batch
                self.now = start + k * every * frame + late
                audio.update([name])
                sent.append(helper.received())
            self.assertEqual(sent, [cue_byte(name)] * 5)  # no repeat dropped: the drone has no hole
        sent = []
        for tick in (600, 659, 787, 974):  # a blast, the invasion game over's second rise, then deaths 187 frames on
            self.now = tick * frame
            audio.update(["blast"])
            sent.append(helper.received())
        self.assertEqual(sent, [cue_byte("blast"), b"", cue_byte("blast"), cue_byte("blast")])

    def test_a_full_pipe_drops_the_cues_whole_and_keeps_the_helper(self):
        audio = self.audio()
        (helper,) = self.helpers
        filled = 0
        try:
            while True:
                filled += os.write(helper.stdin.fileno(), b"x" * 4096)
        except BlockingIOError:
            pass
        audio.update(["fire"])
        self.assertEqual(len(helper.received()), filled)  # only the filler: the write was dropped whole
        audio.update(())
        self.assertEqual(helper.received(), b"")  # a late cue is worse than none: it is not sent again
        self.now = 1.0
        audio.update(["kill"])
        self.assertEqual(helper.received(), cue_byte("kill"))
        self.assertTrue(audio.available)


class ToggleTests(Case):
    def test_m_off_silences_at_once_and_m_on_sends_cues_again(self):
        audio = self.audio()
        (helper,) = self.helpers
        self.assertFalse(audio.toggle())
        self.assertEqual(helper.received(), CUT)
        audio.update(["fire"])
        self.assertEqual(helper.received(), b"")
        self.assertTrue(audio.toggle())
        self.assertEqual(helper.received(), b"")
        audio.update(["fire"])
        self.assertEqual(helper.received(), cue_byte("fire"))

    def test_no_sound_starts_nothing_until_m(self):
        audio = self.audio(enabled=False)
        self.assertEqual((self.helpers, audio.pid, audio.enabled), ([], None, False))
        audio.update(["fire"])
        self.assertTrue(audio.toggle())
        (helper,) = self.helpers
        audio.update(["fire"])
        self.assertEqual(helper.received(), cue_byte("fire"))

    def test_invaders_audio_none_turns_sound_off_entirely(self):
        with patch.dict(os.environ, {"INVADERS_AUDIO": " None "}):
            audio = self.audio()
        self.assertEqual((self.helpers, audio.available, audio.enabled, audio.toggle()), ([], False, False, False))
        audio.close()

    def test_a_helper_that_cannot_start_leaves_the_game_silent(self):
        audio = Audio(popen=Mock(side_effect=OSError("no python")))
        self.assertEqual((audio.available, audio.enabled, audio.pid, audio.toggle()), (False, False, None, False))
        audio.update(["fire"])
        audio.close()


class StatusTests(Case):
    def test_status_lines_are_read_every_quarter_second_and_name_the_backend(self):
        audio = self.audio()
        (helper,) = self.helpers
        lines = []
        audio.on_status = lines.append
        helper.say(b"backend pulse-simple\nstats cpu=1.0 lead=30.0/31.0/40.0/50.0 under")
        audio.update(())
        self.assertIsNone(audio.backend)  # 0.25 s have not passed since the spawn
        self.now = 0.25
        with self.assertLogs(level="DEBUG") as logs:
            audio.update(())
            helper.say(b"runs=0 callbacks=0\n")
            audio.poll_status()
        self.assertEqual(audio.backend, "pulse-simple")
        self.assertEqual(
            lines, ["backend pulse-simple", "stats cpu=1.0 lead=30.0/31.0/40.0/50.0 underruns=0 callbacks=0"]
        )
        self.assertIn("DEBUG:root:audio: backend pulse-simple", logs.output)
        self.assertTrue(all(line.startswith("DEBUG") for line in logs.output))

    def test_silent_turns_sound_off_and_the_helper_exiting_sweeps_its_group(self):
        audio = self.audio()
        (helper,) = self.helpers
        helper.say(b"fallback pw-cat: no pw-cat\nsilent: no backend\n")
        helper.exit()
        audio.poll_status()
        self.assertEqual((audio.available, audio.enabled, audio.pid, audio.toggle()), (False, False, None, False))
        self.assertEqual(helper.calls, ["killpg", "wait"])
        audio.update(["fire"])
        audio.close()

    def test_a_helper_that_stops_reading_is_swept_at_once(self):
        audio = self.audio()
        (helper,) = self.helpers
        os.close(helper.inbox)
        helper.inbox = None
        began = time.monotonic()
        audio.update(["fire"])
        self.assertLess(time.monotonic() - began, 0.1)  # no CLOSE_WAIT: it is gone or broken
        self.assertEqual((audio.available, audio.enabled, audio.pid), (False, False, None))
        self.assertEqual(helper.calls, ["killpg", "wait"])


class CloseTests(Case):
    def test_close_asks_the_helper_to_quit_and_returns_when_it_has(self):
        audio = self.audio()
        (helper,) = self.helpers
        helper.say(b"bye\n")
        helper.exit()
        began = time.monotonic()
        audio.close()
        self.assertLess(time.monotonic() - began, 0.1)
        self.assertEqual(helper.received(), QUIT)
        self.assertEqual(helper.calls, ["killpg", "wait"])  # the sweep still runs: a player may be left
        self.assertTrue(helper.stdin.closed and helper.stdout.closed)

    def test_close_kills_a_hung_helper_group_before_reaping_and_is_idempotent(self):
        audio = self.audio()
        (helper,) = self.helpers
        began = time.monotonic()
        audio.close()
        took = time.monotonic() - began
        self.assertGreaterEqual(took, 0.29)
        self.assertLess(took, 0.35)
        self.assertEqual(helper.calls, ["killpg", "wait"])
        audio.close()
        audio.update(["fire"])
        self.assertEqual((helper.calls, audio.toggle(), audio.enabled), (["killpg", "wait"], False, False))

    def test_close_sweeps_even_when_interrupted(self):
        audio = self.audio()
        (helper,) = self.helpers
        with patch("select.select", side_effect=SystemExit(143)), self.assertRaises(SystemExit):
            audio.close()  # the SIGTERM handler raising inside the wait
        self.assertEqual(helper.calls, ["killpg", "wait"])

    def test_a_group_of_zombies_is_fine(self):
        audio = self.audio()
        (helper,) = self.helpers
        helper.exit()
        helper.killed = True
        with patch("os.killpg", side_effect=PermissionError(1, "Operation not permitted")):
            audio.close()
        self.assertEqual(helper.calls, ["wait"])


class CliTests(unittest.TestCase):
    def main(self, *arguments):
        terminal = Mock(isatty=lambda: True, fileno=Mock(side_effect=io.UnsupportedOperation))
        with TemporaryDirectory() as directory, patch("sys.stdin", terminal), patch("sys.stdout", terminal):
            with patch.object(app, "run"), patch("terminal_invaders.audio.Audio") as made:
                self.assertEqual(cli.main(["--profile", str(Path(directory) / "p.json"), *arguments]), 0)
            (call,) = made.call_args_list
            made.return_value.close.assert_called_once_with()
            return call.kwargs

    def test_no_sound_starts_the_game_muted(self):
        for arguments, expected in [((), dict(enabled=True)), (("--no-sound",), dict(enabled=False))]:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.main(*arguments), expected)


class IsolationTests(unittest.TestCase):
    def test_the_simulation_and_the_sound_never_import_each_other(self):
        code = (
            "import sys; import terminal_invaders.engine, terminal_invaders.model, terminal_invaders.replay, "
            "terminal_invaders.rng; print(sorted(m for m in sys.modules if m.startswith('terminal_invaders.audio')))"
        )
        found = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True)
        self.assertEqual(found.stdout.strip(), "[]")
        package = Path(ROOT, "terminal_invaders", "audio")
        for path in package.glob("*.py"):
            text = path.read_text()
            for name in ("engine", "model", "replay", "rng", "ui", "app", "cli"):
                with self.subTest(module=path.name, name=name):
                    self.assertNotRegex(text, rf"from \.\.({name})\b|terminal_invaders\.{name}\b|from \.\. import")


if __name__ == "__main__":
    unittest.main()
