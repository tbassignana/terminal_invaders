"""Job control under minishell, which gives the game a controlling terminal and a foreground process group:
^Z stops it with the terminal restored and fg brings it back paused, also after bg; a game started with & waits
for fg; kill %1 on a stopped game exits 143; ^C and ^\\ exit; a stray SIGCONT pauses. On a kitty terminal those
keys arrive as CSI u, and the game raises the signal itself."""

import os
import re
import signal
import sys
import time
from array import array

import pytest

from terminal_invaders.audio.helper import FILE_LEAD
from terminal_invaders.audio.protocol import BLOCK_FRAMES, CHANNELS, IDLE_AFTER, RATE, STALE
from tests.support import fakeplayer
from tests.support.audioprocs import alive, children, state
from tests.support.ptyrun import ShellPty
from tests.test_terminal import UP, leave, until

KITTY_KEYS = {b"\x1a": b"\x1b[122;5u", b"\x03": b"\x1b[99;5u", b"\x1c": b"\x1b[92;5u"}
FRAME_BYTES = 2 * CHANNELS  # one s16 frame of the file sink
VOICE = re.compile(r"audio: voice (\w+) (\d+)")  # the helper's status line, as the game logs it under --debug


def shell(directory, *arguments, fixture="xterm", background=False, env=None):
    tape = directory / "recording.json"
    arguments = ("--quickstart", "--seed", "42", "--record", str(tape), *arguments)
    return ShellPty(directory, *arguments, fixture=fixture, background=background, env=env)


def wait_until(condition, timeout: float):
    deadline = time.monotonic() + timeout
    while not (result := condition()) and time.monotonic() < deadline:
        time.sleep(0.01)
    return result


def ticks(terminal) -> int:
    """The tick the debug log's last frame drew, counted from its per-frame ticks."""
    return sum(frame[2] for frame in terminal.frames())


def key(terminal, legacy: bytes) -> bytes:
    return KITTY_KEYS[legacy] if terminal.doc["keys"]["protocol"] == "kitty" else legacy


@pytest.mark.parametrize("fixture", ["xterm", "ghostty"])
def test_ctrl_z_stops_with_the_terminal_restored_and_fg_resumes_paused(tmp_path, fixture):
    terminal = shell(tmp_path, "--debug", fixture=fixture)
    try:
        assert terminal.wait_for("SCORE")
        vt = terminal.stub.vt
        assert vt.kitty_depth == (fixture == "ghostty")
        terminal.send(key(terminal, b"\x1a"))
        stopped = terminal.event()
        assert stopped == {"stopped": "SIGTSTP", "termios": True, "pending": 0}
        assert not vt.alt_screen and vt.cursor_visible and not vt.sync and vt.kitty_depth == 0
        drawn = len(terminal.frames())  # every frame from before the stop is in the log by now
        terminal.fg()
        assert terminal.wait_for("Back from suspend")
        assert vt.alt_screen and vt.kitty_depth == (fixture == "ghostty")  # pushed again after a new probe
        terminal.wait(0.3)
        # The game steps the ticks that fall due before it decides to draw, and draws only while the terminal keeps up
        # with its acks, so ticks it played just before ^Z can show first in the frame it draws once back (each frame
        # logs the ticks since the one before). That frame is the pause's own: no frame after it has played a tick.
        back = [frame[2] for frame in terminal.frames()[drawn:]]
        assert back and not any(back[1:]), back  # still paused
        played, logged = ticks(terminal), len(terminal.loops())
        terminal.tap("p")  # resume: 3-2-1, then play
        assert terminal.wait_for("GET READY")
        assert until(lambda: ticks(terminal) > played + 20), ticks(terminal) - played  # a third of a second of play
        # and at speed, on the game's own clock: a second it logs from then on holds 40 ticks or more (60 Hz, less any
        # a loaded machine drops; play that came back at half speed holds 30)
        assert until(lambda: any(t >= 40 * s for s, t, *_ in terminal.loops()[logged:])), terminal.loops()[logged:]
        leave(terminal)
        assert terminal.event() == {"exit": 0, "termios": True, "pending": 0}
        terminal.finish()
    finally:
        terminal.close()


@pytest.mark.parametrize("fixture", ["xterm", "ghostty"])
def test_ctrl_c_exits_130_and_ctrl_backslash_131(tmp_path, fixture):
    for legacy, code in ((b"\x03", 130), (b"\x1c", 131)):
        directory = tmp_path / str(code)
        directory.mkdir()
        terminal = shell(directory, fixture=fixture)
        try:
            assert terminal.wait_for("SCORE")
            terminal.send(key(terminal, legacy))
            assert terminal.event() == {"exit": code, "termios": True, "pending": 0}
            terminal.finish()  # the game's exit is in minishell's report; finish() checks the terminal
        finally:
            terminal.close()


def test_a_stray_sigcont_while_playing_pauses_and_repaints(tmp_path):
    terminal = shell(tmp_path, "--debug")
    try:
        assert terminal.wait_for("SCORE")
        terminal.wait(0.3)
        os.kill(terminal.game_pid, signal.SIGCONT)
        assert terminal.wait_for("Terminal restored")
        assert "Terminal back (resumed)" in terminal.log.read_text()
        assert terminal.output().count(b"\x1b[?1049h") == 2  # entered again, with a new probe
        leave(terminal, paused=True)
        assert terminal.event() == {"exit": 0, "termios": True, "pending": 0}
        terminal.finish()
    finally:
        terminal.close()


@pytest.mark.parametrize("fixture", ["xterm", "ghostty"])
def test_bg_after_ctrl_z_stops_again_for_the_terminal_and_fg_resumes_paused(tmp_path, fixture):
    terminal = shell(tmp_path, fixture=fixture)
    try:
        assert terminal.wait_for("SCORE")
        terminal.send(key(terminal, b"\x1a"))
        assert terminal.event() == {"stopped": "SIGTSTP", "termios": True, "pending": 0}
        terminal.bg()  # continued in the background: setting raw mode stops it (SIGTTOU), the terminal untouched
        assert terminal.event() == {"stopped": "SIGTTOU", "termios": True, "pending": 0}
        terminal.fg()  # its SIGCONT interrupts that tcsetattr (EINTR), which is made again
        assert terminal.wait_for("Back from suspend")
        leave(terminal, paused=True)
        assert terminal.event() == {"exit": 0, "termios": True, "pending": 0}
        terminal.finish()
    finally:
        terminal.close()


def test_a_game_started_in_the_background_waits_for_fg(tmp_path):
    terminal = shell(tmp_path, background=True)
    try:
        assert terminal.event() == {"stopped": "SIGTTOU", "termios": True, "pending": 0}
        terminal.fg()
        assert terminal.wait_for("SCORE")
        leave(terminal)
        assert terminal.event() == {"exit": 0, "termios": True, "pending": 0}
        terminal.finish()
    finally:
        terminal.close()


@pytest.mark.parametrize("fixture", ["xterm", "ghostty"])
def test_kill_on_a_stopped_game_exits_143_without_taking_the_terminal_back(tmp_path, fixture):
    terminal = shell(tmp_path, fixture=fixture)
    try:
        assert terminal.wait_for("SCORE")
        terminal.send(key(terminal, b"\x1a"))
        assert terminal.event() == {"stopped": "SIGTSTP", "termios": True, "pending": 0}
        terminal.kill_job()  # SIGTERM, then SIGCONT, with the job still in the background
        assert terminal.event() == {"exit": 143, "termios": True, "pending": 0}
        terminal.finish()
        assert terminal.output().count(b"\x1b[?1049h") == 1  # it never entered again
    finally:
        terminal.close()


def sound_helper(terminal) -> int:
    """The game's audio helper: its only child, which leads its own session and process group."""
    found = []
    assert wait_until(lambda: found.extend(children(terminal.game_pid)) or found, 3.0), "no audio helper"
    (helper,) = found
    return helper


def settled(path, quiet: float) -> bool:
    """Until the file has not grown for `quiet` s, polling every 50 ms, for at most UP; whether it settled."""
    deadline, size, since = time.monotonic() + UP, path.stat().st_size, time.monotonic()
    while time.monotonic() - since < quiet:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
        if (now := path.stat().st_size) != size:
            size, since = now, time.monotonic()
    return True


def test_ctrl_z_never_stops_the_sound_helper_and_it_goes_idle(tmp_path):
    """The file sink writes in real time and the helper says at which of its frames each voice began (the game logs
    those lines once it reads them again, by its exit at the latest), so the checks count the file's frames, on the
    helper's own clock, never the test's: a slow machine wakes the test and the helper late."""
    sink = tmp_path / "sink.raw"
    terminal = shell(tmp_path, "--debug", env={"INVADERS_AUDIO": f"file:{sink}"})
    try:
        assert terminal.wait_for("SCORE")
        helper = sound_helper(terminal)
        assert wait_until(lambda: sink.exists() and sink.stat().st_size >= 96_000, 5.0)  # 0.5 s of sound
        assert any(array("h", sink.read_bytes()[:9_600]))  # the file starts with the cue that ends the wave intro
        terminal.send(b"\x1a")
        assert terminal.event() == {"stopped": "SIGTSTP", "termios": True, "pending": 0}
        stopped = sink.stat().st_size // FRAME_BYTES  # where the file had got to once the game was stopped
        assert "T" not in state(helper)  # its own session: the job's SIGTSTP never reached it
        assert settled(sink, 1.0), "the sound helper never went idle"  # it writes every 10 ms while it feeds
        sound = sink.read_bytes()
        assert "T" not in state(helper)
        terminal.fg()
        assert terminal.wait_for("Back from suspend")
        leave(terminal, paused=True)
        assert terminal.event() == {"exit": 0, "termios": True, "pending": 0}
        terminal.finish()
        assert wait_until(lambda: not alive(helper), 0.5)
        frames = len(sound) // FRAME_BYTES
        voices = [(name, int(at)) for name, at in VOICE.findall(terminal.log.read_text()) if int(at) < frames]
        # No cue while stopped: every voice began before the stop, or from a cue the game sent before it, which the
        # helper mixes within STALE of reading it or drops, into a file it keeps FILE_LEAD blocks ahead of the clock
        assert voices and max(at for _, at in voices) <= stopped + STALE * RATE + (FILE_LEAD + 1) * BLOCK_FRAMES, voices
        # and it idles IDLE_AFTER after the last one ends, by its own clock: the silence that follows is that long, plus
        # the file's lead and the cue's last block (under 0.1 s), less any stretch a late turn of the helper's skipped
        silence = (len(sound) - len(sound.rstrip(b"\0"))) // FRAME_BYTES / RATE
        assert silence <= IDLE_AFTER + 0.5, (silence, voices)
        # and the file stopped growing within 2.6 s of the stop: the cue that sounded last ended before it, or was a 70 ms
        # march note where a slow machine sent ^Z late (1.7 s measured); a cue sent as the game stopped, such as the
        # 1.5 s blast, would sound on while it is stopped and keep the helper feeding past that
        assert (frames - stopped) / RATE <= 2.6, (frames - stopped, voices)
    finally:
        terminal.close()


@pytest.mark.skipif(sys.platform == "darwin", reason="pipe players are Linux only")
def test_kill_on_a_stopped_game_ends_its_sound_and_no_player_output_reaches_the_terminal(tmp_path):
    heard = tmp_path / "heard.raw"
    players = fakeplayer.install(tmp_path / "bin")  # a pw-cat that writes to its stdout, stderr and /dev/tty
    env = {"INVADERS_AUDIO": "pw-cat", "FAKEPLAYER_MODE": "chatter", "FAKEPLAYER_OUT": str(heard)}
    terminal = shell(tmp_path, env=env | {"PATH": f"{players}{os.pathsep}{os.environ['PATH']}"})
    try:
        assert terminal.wait_for("SCORE")
        helper = sound_helper(terminal)
        assert wait_until(lambda: heard.exists() and heard.stat().st_size > 48_000, 3.0)  # the player is playing
        assert len(alive(helper)) == 2  # the helper and its player, in the helper's own group and session
        terminal.send(b"\x1a")
        assert terminal.event() == {"stopped": "SIGTSTP", "termios": True, "pending": 0}
        terminal.kill_job()
        assert terminal.event() == {"exit": 143, "termios": True, "pending": 0}
        terminal.finish()
        assert wait_until(lambda: not alive(helper), 0.5), alive(helper)
        assert fakeplayer.CHATTER.strip() not in terminal.output()
    finally:
        terminal.close()
