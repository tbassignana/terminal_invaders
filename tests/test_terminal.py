"""Exercise real curses in a PTY, including restoration and signal cleanup."""

import fcntl
import json
import os
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class Terminal:
    def __init__(self, directory, *arguments):
        self.master, self.slave = os.openpty()
        self.resize(80, 30)
        self.original = termios.tcgetattr(self.slave)
        self.tape = directory / "recording.json"
        self.output = bytearray()
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "terminal_invaders",
                "--no-sound",
                "--quickstart",
                "--seed",
                "42",
                "--record",
                str(self.tape),
                "--profile",
                str(directory / "profile.json"),
                *arguments,
            ],
            cwd=ROOT,
            env={**os.environ, "TERM": "xterm-256color"},
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
        )

    def resize(self, width, height):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        if hasattr(self, "process"):
            self.process.send_signal(signal.SIGWINCH)

    def send(self, keys):
        os.write(self.master, keys.encode())

    def drain(self, seconds=0.1):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if ready:
                self.output.extend(os.read(self.master, 65536))

    def finish(self):
        deadline = time.monotonic() + 5
        while self.process.poll() is None and time.monotonic() < deadline:
            self.drain(0.05)
        self.process.wait(timeout=1)
        self.drain(0.01)
        restored = termios.tcgetattr(self.slave)
        # macOS marks queued input for retyping; this is a transient kernel flag.
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        self.original[3] &= ~getattr(termios, "PENDIN", 0)
        assert restored == self.original
        assert b"Traceback" not in self.output
        assert self.tape.exists()
        verified = subprocess.run(
            [sys.executable, "-m", "terminal_invaders", "--verify-replay", str(self.tape)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert verified.returncode == 0, verified.stderr + verified.stdout

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        os.close(self.master)
        os.close(self.slave)


@pytest.mark.parametrize("fps", [30, 120])
def test_real_terminal_pause_resize_record_and_restore(tmp_path, fps):
    terminal = Terminal(tmp_path, "--fps", str(fps))
    try:
        terminal.drain(0.3)
        terminal.send("p")
        terminal.drain(0.1)
        terminal.resize(40, 12)
        terminal.drain(0.1)
        assert b"RESIZE TO AT LEAST" in terminal.output
        terminal.resize(60, 24)
        terminal.drain(0.1)
        terminal.send("p")
        terminal.drain(0.2)
        terminal.send("q")
        terminal.drain(0.1)
        terminal.send("q")
        terminal.finish()
        assert terminal.process.returncode == 0
        assert 1 <= json.loads(terminal.tape.read_text())["ticks"] < 100
    finally:
        terminal.close()


def test_sigterm_restores_terminal_and_finalizes_tape(tmp_path):
    terminal = Terminal(tmp_path)
    try:
        terminal.drain(0.25)
        terminal.process.send_signal(signal.SIGTERM)
        terminal.finish()
        assert terminal.process.returncode == 143
    finally:
        terminal.close()


def test_real_gameplay_controls_roundtrip(tmp_path):
    from terminal_invaders.engine import Game
    from terminal_invaders.replay import Playback

    terminal = Terminal(tmp_path)
    try:
        terminal.drain(1.7)
        terminal.send("fd")
        terminal.drain(0.3)
        terminal.send("b")
        terminal.drain(0.2)
        terminal.send("q")
        terminal.drain(0.1)
        terminal.send("q")
        terminal.finish()
        replay = Playback(terminal.tape)
        game = Game(replay.config)
        for tick in range(replay.ticks):
            game.step(replay.actions_at(tick))
        assert game.shots_fired > 0
        assert game.kills > 0
        assert game.score > 0
        assert replay.verify(game)
    finally:
        terminal.close()
