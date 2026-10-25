"""The real binary on a real pty: pause, resize, record, signals and restoration, crash, hangup, resize storm, slow
and stalled readers, no terminfo (G3), --probe, and Arcade mode's quickstart (Phase 3's G3.4).

On a loaded machine (another suite running beside this one) the game starts, reads keys and draws late. So a test
waits for what it needs to see (the game up, a key's effect on screen, the probe sent) with a deadline of UP, never
for a fixed time: a fixed sleep only stands for a scenario's length or a guard the game times itself. The spec's own
time bounds (a teardown within 1.7 s, a silent terminal's start within 0.3 s) are checked as they are."""

import json
import os
import random
import re
import signal
import subprocess
import sys
import termios
import time
from collections import Counter

import pytest

from terminal_invaders.app import text_hash
from terminal_invaders.engine import Game
from terminal_invaders.replay import Playback
from terminal_invaders.term.caps import REMOTE_RTT
from terminal_invaders.term.keys import PRESS, RELEASE, REPEAT, Key
from terminal_invaders.term.simkeys import encode
from terminal_invaders.term.tty import ENTER
from tests.support.ptyrun import LEAVE, ROOT, Pty, echoed, unread
from tests.support.terminals import load
from tests.support.vt import Pen

PENDIN = getattr(termios, "PENDIN", 0)  # macOS marks queued input for retyping: a transient kernel flag
UP = 10.0  # the longest a step may take to show on a loaded machine: a deadline, never a delay
# Past the game's repeat guard for a command key pressed again, counted from the first press's effect: 0.7 s, or
# 1.0 s once its probe took the terminal for a remote one, as a stub reply slower than 30 ms on a loaded machine does
GUARD = 1.2
# The Campaign's too-small notice (ui.small_notice) in a window smaller than the menu or the game: it names the size
# it needs. Its words are ui's, pinned by test_ui and the goldens (Phase 5 spec 9 F2 changed them).
MENU_NOTICE = "60 x 24"


def until(condition, seconds: float = UP) -> bool:
    """Until condition() holds, polling every 20 ms, for at most `seconds`; whether it held."""
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= deadline:
            return condition()
        time.sleep(0.02)
    return True


def game(directory, *arguments, **kwargs) -> Pty:
    """A recorded quickstart run on seed 42, as the Phase 0 pty tests played it."""
    tape = directory / "recording.json"
    return Pty(directory, "--quickstart", "--seed", "42", "--record", str(tape), *arguments, **kwargs)


def flown(terminal) -> Game:
    """The recorded run replayed: the engine's view of what the keys did."""
    (replay,) = Playback(terminal.tape).tapes
    played = Game(replay.config)
    for tick in range(replay.ticks):
        played.step(replay.actions_at(tick))
    assert replay.verify(played)
    return played


def leave(terminal, *, paused=False) -> None:
    """P, Q for the menu, then Esc Esc. Each key waits for the one before to take effect: the pause, then the menu (Q
    saves the replay, and a slow fsync would make the game read the next key late). On a legacy terminal a command key
    pressed again within the repeat guard could be an OS repeat, so the second Esc goes GUARD after the game took the
    first: when the cursor shows on QUIT. A window smaller than the menu (Arcade's smallest, 56 x 32) shows the menu's
    notice instead, with no cursor; there the game took the first Esc once it has read every byte sent. The caller
    reads the exit (a ShellPty's from minishell's report)."""
    if not paused:
        terminal.tap("p")
        assert terminal.wait_for("PAUSED", UP), terminal.screen()
    terminal.tap("q")
    assert until(lambda: "LAUNCH" in terminal.screen() or MENU_NOTICE in terminal.screen()), terminal.screen()
    drawn = "LAUNCH" in terminal.screen()
    terminal.tap("escape")  # QUIT
    if drawn:
        assert terminal.wait_for("> QUIT", UP), terminal.screen()
    else:  # raw mode: FIONREAD on the slave counts every byte the game has not read yet
        assert until(lambda: unread(terminal.slave) == 0), "the game read no key"
    terminal.wait(GUARD)
    terminal.tap("escape")


@pytest.mark.parametrize("fps", [30, 120])
def test_real_terminal_pause_resize_record_and_restore(tmp_path, fps):
    terminal = game(tmp_path, "--fps", str(fps))
    try:
        assert terminal.wait_for("SCORE", UP)  # the game is up: on a loaded machine it starts late
        terminal.wait(0.3)
        terminal.tap("p")
        assert terminal.wait_for("PAUSED", UP)  # paused before the window changes
        terminal.resize(40, 12)
        assert terminal.wait_for(MENU_NOTICE, UP)
        terminal.resize(60, 24)
        assert terminal.wait_for("Window resized", UP)
        terminal.wait(GUARD)  # past the guard since the last p, which the game had read when it showed PAUSED
        terminal.tap("p")  # resume: a countdown, as the window changed
        assert terminal.wait_for("GET READY", UP)
        terminal.tap("escape")  # back to the pause
        assert until(lambda: "GET READY" not in terminal.screen()), terminal.screen()
        assert "Window resized" in terminal.screen()
        terminal.wait(0.5)
        leave(terminal, paused=True)
        assert terminal.finish() == 0
        assert 1 <= json.loads(terminal.tape.read_text())["tapes"][0]["ticks"] < 100
    finally:
        terminal.close()


def test_sigterm_restores_terminal_and_finalizes_tape(tmp_path):
    terminal = game(tmp_path)
    try:
        assert terminal.wait_for(ENTER, UP)  # the terminal is the game's
        assert terminal.wait_for("SCORE", UP)  # and play has begun, so the tape is being recorded
        terminal.wait(0.25)
        terminal.process.send_signal(signal.SIGTERM)
        assert terminal.finish() == 143
    finally:
        terminal.close()


def hold(terminal, name: str, seconds: float, *, delay: float = 0.5, every: float = 0.033) -> None:
    """A key held down as a terminal sends it: a press, OS repeats every `every` after `delay`, and a release where
    the terminal reports releases (kitty CSI u while the game has the protocol pushed); legacy repeats are the
    press's bytes again."""
    with terminal.stub.lock:
        vt = terminal.stub.vt
        stack = vt.kitty["alt" if vt.alt_screen else "main"]
        kitty = bool(stack and stack[-1])
    start = time.monotonic()
    terminal.send(encode(Key(name, kind=PRESS), kitty=kitty))
    terminal.wait(delay)
    while time.monotonic() - start < seconds:
        terminal.send(encode(Key(name, kind=REPEAT), kitty=kitty))
        terminal.wait(every)
    terminal.send(encode(Key(name, kind=RELEASE), kitty=kitty))  # b"" on a legacy terminal


@pytest.mark.gate
@pytest.mark.parametrize("fixture", ["xterm", "ghostty"])
def test_real_gameplay_controls_roundtrip(tmp_path, fixture):
    # G2.2: legacy keys (latched controls) and kitty keys (hold controls) through the real binary. Past the wave's
    # intro, a held Space keeps firing (F is unbound: autofire is optional) and the marching line crosses the ship's
    # column, so a shot kills without the bomb; then a D tap moves the ship.
    terminal = game(tmp_path, fixture=fixture)
    try:
        assert terminal.wait_for("WAVE 01/24", UP)  # the game is up: on a loaded machine it starts late
        terminal.wait(1.6)  # past the intro
        hold(terminal, "space", 4.0)
        terminal.tap("d")
        terminal.wait(0.3)
        leave(terminal)
        assert terminal.finish() == 0
        played = flown(terminal)
        (replay,) = Playback(terminal.tape).tapes
        assert "right" in {action for tick in range(replay.ticks) for action in replay.actions_at(tick)}
        assert played.shots_fired >= 3
        assert played.kills - played.bomb_kills >= 1
        assert played.score > 0
    finally:
        terminal.close()


def test_arrow_keys_move_the_ship(tmp_path):
    # DECCKM stays off, so terminals send CSI arrows; the SS3 forms (application cursor keys) decode as well.
    terminal = game(tmp_path)
    try:
        assert terminal.wait_for("SCORE", UP)  # keys sent before the probe's flush would be lost
        terminal.wait(0.4)
        for _ in range(10):
            terminal.send(b"\x1b[D")
            terminal.wait(0.05)
        terminal.send(b"\x1bOC")  # tap controls: the opposite way stops, a second tap starts
        terminal.wait(0.15)
        terminal.send(b"\x1bOC")
        terminal.wait(0.2)
        leave(terminal)
        assert terminal.finish() == 0
        (replay,) = Playback(terminal.tape).tapes
        moves = {action for tick in range(replay.ticks) for action in replay.actions_at(tick)}
        assert {"left", "right"} <= moves
    finally:
        terminal.close()


def test_a_crash_prints_its_traceback_after_the_terminal_is_restored(tmp_path):
    terminal = Pty(tmp_path, "--quickstart", "--seed", "42", "--debug", env={"INVADERS_FAULT": "raise:120"})
    try:
        assert terminal.exit(UP) == 1  # its start and 120 ticks of play first
        assert terminal.finish(traceback=True, tape=False) == 1
        output = terminal.output()
        assert output.index(b"Traceback") > output.rindex(LEAVE)
        assert b"INVADERS_FAULT: crash at tick 120" in output
    finally:
        terminal.close()


def test_a_closed_master_exits_129_without_hanging(tmp_path):
    terminal = game(tmp_path)
    try:
        assert terminal.wait_for("SCORE", UP)
        terminal.stub.stop()
        os.close(terminal.master)
        terminal.master = -1
        started = time.monotonic()
        assert terminal.exit(2.0) == 129
        assert time.monotonic() - started < 2.0
    finally:
        terminal.close()


def test_sighup_exits_129_and_restores(tmp_path):
    terminal = game(tmp_path)
    try:
        assert terminal.wait_for("SCORE", UP)
        terminal.process.send_signal(signal.SIGHUP)
        assert terminal.finish() == 129
    finally:
        terminal.close()


@pytest.mark.gate
def test_after_a_resize_storm_the_last_frame_is_on_screen_at_the_final_size(tmp_path):
    terminal = game(tmp_path, "--debug", fixture="ghostty")
    try:
        assert terminal.wait_for("SCORE", UP)
        sizes = random.Random(7)
        end = time.monotonic() + 1.0
        for left in range(200, 0, -1):
            terminal.resize(sizes.randint(40, 200), sizes.randint(12, 60))
            time.sleep(max(0.0, end - time.monotonic()) / left)
        terminal.resize(120, 44)
        assert until(lambda: [frame[:2] for frame in terminal.frames()[-1:]] == [(120, 44)])  # drawn at that size
        assert terminal.wait_for("Window resized", UP)
        leave(terminal, paused=True)  # the storm paused it (Window resized)
        assert terminal.finish() == 0
        frames = terminal.frames()
        assert frames[-1][:2] == (120, 44)
        hashes = terminal.screen_hashes()
        assert len(hashes) == len(frames)
        assert hashes == [frame[5] for frame in frames]  # every ESU showed its own frame, the last one included
    finally:
        terminal.close()


@pytest.mark.gate
@pytest.mark.slow  # timing-bound: the gate workflow runs it, not the CI matrix
@pytest.mark.parametrize(
    "fixture, pacing",
    [("ghostty", "auto"), ("ghostty", "off"), ("silent", "auto")],
    ids=["acks", "pacing-off", "silent"],
)
def test_a_slow_reader_keeps_the_simulation_at_60_hz(tmp_path, fixture, pacing):
    # Ack pacing, and clock pacing twice (by flag, and on a terminal that never answers): without acks nothing says
    # how much the pty can take, so the game's writes are timed instead.
    stress = {"INVADERS_FAULT": "stress:3000"}  # 3 KB frames: 180 KB/s wanted, so the reader is the bottleneck
    terminal = game(tmp_path, "--debug", "--pacing", pacing, fixture=fixture, size=(120, 44), env=stress)
    try:
        assert terminal.wait_for("SCORE", UP)
        terminal.wait(0.5)
        terminal.stub.throttle = (1024, 0.05)  # 1 KB every 50 ms
        terminal.wait(5.0)
        terminal.stub.throttle = None
        terminal.wait(1.0)
        loops = terminal.loops()
        terminal.tap("p")
        terminal.wait(0.5)
        drawn = terminal.frames()[-1]
        on_screen = text_hash([line[: drawn[0]] for line in terminal.stub.vt.lines()[: drawn[1]]])
        leave(terminal, paused=True)
        assert terminal.finish() == 0
        assert len(loops) >= 6
        # 59 ticks/s over the run, and in each logged second: a window that ends just before a tick is due holds
        # one tick less than its length says (59 in 1.001 s), so one tick of slack
        assert sum(ticks for _, ticks, *_ in loops) / sum(seconds for seconds, *_ in loops) >= 59, loops
        for seconds, ticks, _, _, longest in loops:
            assert ticks + 1 >= 59 * seconds, loops
            assert longest <= 20, loops
        assert min(frames for _, _, frames, _, _ in loops) < 20, loops  # frames were dropped, not queued
        assert on_screen == drawn[5]  # nothing lost once the reader recovered
        if terminal.stub.screens:  # synchronized output: every ESU showed its own frame, to the last
            assert terminal.screen_hashes()[-1] == terminal.frames()[-1][5]
    finally:
        terminal.close()


@pytest.mark.gate  # G6
@pytest.mark.parametrize("fixture, pacing", [("ghostty", "auto"), ("xterm", "off")], ids=["acks", "pacing-off"])
def test_a_slow_reader_gets_a_short_teardown_and_no_reply_reaches_the_shell(tmp_path, fixture, pacing):
    # The frames it has not read are flushed, so LEAVE and the sentinel go out at once, and the sentinel's reply is
    # read while still raw: none of it is echoed into the shell (finish() counts input with ICANON off).
    terminal = game(tmp_path, "--pacing", pacing, fixture=fixture, size=(120, 44))
    try:
        assert terminal.wait_for("SCORE", UP)
        terminal.stub.throttle = (1000, 0.1)  # 10 KB/s
        terminal.wait(6.0)  # long enough that the code before the fix echoed late acks into the shell (T3)
        terminal.process.send_signal(signal.SIGTERM)
        sent = time.monotonic()
        terminal.process.wait(5)
        assert time.monotonic() - sent <= 1.7  # spec 4.6's worst case
        terminal.wait(1.0)  # the terminal reads on at its pace, and answers what it reads
        terminal.stub.throttle = None
        assert terminal.finish() == 143
    finally:
        terminal.close()


@pytest.mark.gate  # G6
def test_a_terminal_that_stopped_reading_still_gets_leave(tmp_path):
    # Clock pacing fills the pty while the terminal reads nothing; SIGTERM flushes what it has not read, so LEAVE
    # fits and follows the bytes it had half read, after CAN.
    stress = {"INVADERS_FAULT": "stress:3000"}
    terminal = game(tmp_path, "--debug", "--pacing", "off", fixture="ghostty", env=stress)
    try:
        assert terminal.wait_for("SCORE", UP)
        terminal.stub.stalled = True
        terminal.wait(2.0)
        terminal.process.send_signal(signal.SIGTERM)
        sent = time.monotonic()
        assert terminal.exit() == 143
        assert time.monotonic() - sent <= 1.7
        terminal.stub.stalled = False
        assert terminal.wait_for(LEAVE, UP)
        assert until(lambda: echoed(terminal.output()))  # the stub answered the sentinel, and the tty echoed it
        terminal.wait(0.2)  # anything else would come with it
        output = terminal.output()
        assert b"\x18" in output[output.rindex(b"\x1b[?2026h") :]  # the cut frame was cancelled
        vt = terminal.stub.vt
        assert not vt.alt_screen and vt.cursor_visible and vt.kitty_depth == 0 and not vt.paste and not vt.focus
        assert (vt.titles, vt.title) == ([], "")
        assert terminal.stub.error is None
        restored = termios.tcgetattr(terminal.slave)
        assert restored[3] & ~PENDIN == terminal.original[3] & ~PENDIN
        # Only the sentinel's reply came too late, from a terminal that read nothing for seconds: it is echoed
        assert echoed(output) == [b"^[[?62;22c"]
    finally:
        terminal.close()


@pytest.mark.gate
@pytest.mark.parametrize("fixture", ["ghostty", "kitty", "term-unset", "dumb"])
def test_g3_starts_and_plays_without_terminfo(tmp_path, fixture):
    nowhere = {"TERMINFO": "/nonexistent", "TERMINFO_DIRS": "/nonexistent"}
    terminal = Pty(tmp_path, "--seed", "42", "--record", str(tmp_path / "recording.json"), fixture=fixture, env=nowhere)
    try:
        assert ("TERM" in terminal.environment) == (fixture != "term-unset")
        assert terminal.wait_for("LAUNCH", UP)
        terminal.tap("enter")
        assert terminal.wait_for("SCORE", UP)
        terminal.tap("d")
        terminal.wait(1.0)
        terminal.tap("space")
        terminal.wait(1.0)
        leave(terminal)
        assert terminal.finish() == 0
        assert flown(terminal).tick >= 100
    finally:
        terminal.close()


@pytest.mark.gate  # G3.3
def test_a_silent_terminal_starts_and_leaves_at_once_on_clock_pacing(tmp_path):
    terminal = Pty(tmp_path, "--debug", fixture="silent")
    try:
        assert terminal.wait_for("LAUNCH", UP)
        started = terminal.arrival(b"LAUNCH") - terminal.arrival(ENTER)
        assert started <= 0.3, started
        terminal.tap("escape")  # QUIT
        assert terminal.wait_for("> QUIT", UP)
        terminal.wait(GUARD)
        sent = time.monotonic()
        terminal.tap("escape")
        assert terminal.finish(tape=False) == 0
        assert terminal.arrival(LEAVE) - sent <= 0.3
        output = terminal.output()
        assert output.count(b"\x1b[5n") == output.count(b"\x1b[c") == 1  # the probe's: no acks, no sentinel
        assert "ack none" in terminal.log.read_text()
    finally:
        terminal.close()


def local(probe, base, attempts: int = 8):
    """probe(directory) -> (local, outcome) until local: whether the probe's round trip was a local terminal's (at
    most caps.REMOTE_RTT). The stub answers from the test process, and on a loaded machine it can answer later than
    that: the probe then rightly calls the terminal remote, so it runs again, `attempts` times at most, each in a
    directory of its own under `base`. Returns the outcome of the first local probe."""
    for attempt in range(attempts):
        directory = base / f"probe{attempt}"
        directory.mkdir()
        fast, outcome = probe(directory)
        if fast:
            return outcome
    pytest.fail(f"no probe round trip within {REMOTE_RTT * 1000:.0f} ms in {attempts} probes: too loaded to tell")


def test_probe_reports_the_terminal_and_restores_it(tmp_path):
    def probe(directory):
        terminal = Pty(directory, "--probe", fixture="kitty")
        try:
            assert terminal.finish(tape=False) == 0
            output = terminal.output().decode()
        finally:
            terminal.close()
        rtt = float(re.search(r"^probe_rtt +(\S+)", output, re.MULTILINE)[1])
        where = "local" if rtt <= REMOTE_RTT else "remote"  # caps.resolve: a slow round trip is a remote terminal
        assert f"kitty 0.43.1  {where}  truecolor dark  sync  keys kitty 11 release" in output
        return where == "local", output

    output = local(probe, tmp_path)
    assert "kitty_flags      11" in output
    assert "\x1b[>11u" in output and output.index("\x1b[<u") < output.index("kitty 0.43.1")


class Redirected(Pty):
    """The game with its standard output in a file, as in invaders --probe --raw > fixture.json."""

    def launch(self) -> subprocess.Popen:
        with open(self.directory / "stdout", "wb") as stdout:
            return subprocess.Popen(
                self.game() + self.arguments,
                cwd=ROOT,
                env=self.environment,
                stdin=self.slave,
                stdout=stdout,
                stderr=self.slave,
                start_new_session=True,
            )


def report(terminal: Pty) -> dict:
    """The JSON --probe printed on the terminal once it had left it."""
    output = terminal.output()
    text = output[output.rindex(LEAVE) :].decode().replace("\r\n", "\n")
    return json.loads(text[text.index("{") : text.rindex("}") + 1])


@pytest.mark.parametrize("name", ["xterm", "dumb"])
def test_probe_raw_may_go_to_a_file_and_records_the_environment_the_user_set(tmp_path, name):
    expected = load(name)

    def probe(directory):
        terminal = Redirected(directory, "--probe", "--raw", fixture=name)
        try:
            assert terminal.finish(tape=False) == 0
        finally:
            terminal.close()
        doc = json.loads((directory / "stdout").read_text())
        # A fixture keeps no round trip, but with no SSH_* variable set only one over REMOTE_RTT makes it remote
        assert not expected["expect"]["remote"] and "SSH_CONNECTION" not in doc["env"]
        return not doc["expect"]["remote"], doc

    doc = local(probe, tmp_path)
    assert doc["env"] == expected["env"]  # nothing CPython's locale coercion put in (PEP 538)
    assert (doc["replies"], doc["expect"]) == (expected["replies"], expected["expect"])


def test_a_latin1_locale_is_not_utf8_where_python_coerced_it(tmp_path):
    terminal = Pty(tmp_path, "--probe", "--json", fixture="silent", env={"LANG": "en_US.ISO-8859-1"})
    try:
        assert terminal.finish(tape=False) == 0
        caps = report(terminal)  # silent: no CPR, so the locale alone decides (5.5)
        assert (caps["utf8"], caps["unicode_blocks"]) == (False, False)
    finally:
        terminal.close()


@pytest.mark.parametrize("signum, code", [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)])
def test_a_signal_during_the_probe_exits_as_in_play_without_a_report(tmp_path, signum, code):
    terminal = Pty(tmp_path, "--probe", fixture="silent", env={"SSH_CONNECTION": "1"})  # silent over SSH: a 1 s probe
    try:
        # The probe is sent (its handlers were set before ENTER) and waits a second for replies that never come
        assert terminal.wait_for(b"\x1b[c", UP)
        terminal.process.send_signal(signum)
        assert terminal.finish(tape=False) == code
        assert b"kitty_flags" not in terminal.output()
    finally:
        terminal.close()


def lit_cells(terminal, cols: int, top: int, bottom: int) -> int:
    """Cells in screen rows top to bottom that show pixels: a half block, or a space painted unlike most of them.

    A cell still in the terminal's default pen has not been painted. It is never a pixel and never the background:
    the pty hands the stub a big frame in pieces, so a check can land when only part of the arena is painted."""
    with terminal.stub.lock:
        cells = [terminal.stub.vt.cell(y, x) for y in range(top, bottom) for x in range(cols)]
    painted = Counter(pen for char, pen in cells if char == " " and pen != Pen())
    background = painted.most_common(1)[0][0] if painted else Pen()
    return sum(char in "▀▄█" or (char == " " and pen not in (background, Pen())) for char, pen in cells)


@pytest.mark.parametrize("size, p", [((112, 64), 2), ((56, 32), 4)])
def test_arcade_quickstart_shows_the_score_header_and_the_fleet_and_quits_cleanly(tmp_path, size, p):
    terminal = Pty(tmp_path, "--arcade", "--quickstart", "--record", str(tmp_path / "recording.json"), size=size)
    try:
        assert terminal.wait_for((" " * (8 // p - 1)).join("SCORE<1>"), UP)
        rack = (60 // (2 * p), 140 // (2 * p))  # display y 64-135: the fleet's five rows when a game starts
        deadline = time.monotonic() + 8.0
        while (lit := lit_cells(terminal, size[0], *rack)) < 55 and time.monotonic() < deadline:
            terminal.wait(0.1)
        assert lit >= 55, terminal.screen()
        terminal.tap("space")
        terminal.wait(1.0)
        leave(terminal)
        assert terminal.finish() == 0  # verifies the recording too
        (tape,) = Playback(terminal.tape).tapes
        assert tape.config.mode == "arcade" and tape.ticks >= 30
    finally:
        terminal.close()


def test_arcade_in_a_window_too_small_for_it_says_so_and_quits_cleanly(tmp_path):
    terminal = Pty(tmp_path, "--arcade", "--quickstart", "--record", str(tmp_path / "recording.json"), size=(80, 24))
    try:
        for line in ("ARCADE NEEDS 56 x 32", "Current: 80 x 24 | Paused", "Enlarge the window or zoom out"):
            assert terminal.wait_for(line, UP), terminal.screen()
        terminal.resize(112, 64)
        assert terminal.wait_for("Window resized", UP)
        terminal.wait(0.5)
        leave(terminal, paused=True)
        assert terminal.finish() == 0
    finally:
        terminal.close()


# --- Phase 5 (spec 12.3): the settings card, the menu at 80 x 24, the initials card and the title demo -------------

STAGED = """import sys

sys.dont_write_bytecode = True
sys.path.insert(0, {root!r})
from terminal_invaders import ui
from terminal_invaders.campaign.testing import staged
from terminal_invaders.cli import main
from terminal_invaders.model import Config

original, first = ui.make_game, [True]


def make_game(config):
    if first[0] and config.mode == "classic":  # the first Campaign game: wave 5's aimed fire, one life
        first[0] = False
        return staged(Config("normal", "classic", 1978), 5, lives=1, phase="playing")
    return original(config)


ui.make_game = make_game
raise SystemExit(main(sys.argv[1:]))
"""


class StagedPty(Pty):
    """The game through a launcher whose first Campaign game is staged (a short run that places on the board)."""

    def game(self) -> list[str]:
        launcher = self.directory / "staged_launch.py"
        launcher.write_text(STAGED.format(root=str(ROOT)))
        return [sys.executable, "-B", str(launcher), "--no-sound", "--profile", str(self.directory / "p.json")]


def on_screen(terminal, text: str, seconds: float = UP) -> bool:
    """Until text is on the screen now (wait_for also finds it in the output of a screen gone by)."""
    return until(lambda: text in terminal.screen(), seconds)


def quit_from_menu(terminal, escape_twice: bool = False) -> None:
    """From the menu: Esc moves the cursor to QUIT, then Enter quits. escape_twice: Esc, Esc instead (the second past
    the repeat guard), for a run that pressed Enter within the guard, where another Enter would read as a repeat."""
    terminal.tap("escape")
    assert on_screen(terminal, "> QUIT", UP), terminal.screen()
    if escape_twice:
        terminal.wait(GUARD)
        terminal.tap("escape")
    else:
        terminal.tap("enter")


def accent_of_title(terminal):
    """The colour the menu's title is drawn in: the accent role."""
    with terminal.stub.lock:
        for y, line in enumerate(terminal.stub.vt.lines()):
            x = line.find("T E R M I N A L")
            if x >= 0:
                return terminal.stub.vt.cell(y, x)[1].fg
    return None


def test_settings_saves_the_colour_blind_palette_and_the_next_launch_starts_in_it(tmp_path):
    terminal = Pty(tmp_path, fixture="kitty", size=(80, 24))
    try:
        assert on_screen(terminal, "> LAUNCH", UP), terminal.screen()
        default = accent_of_title(terminal)
        for _ in range(5):
            terminal.tap("down")
        assert on_screen(terminal, "> SETTINGS", UP), terminal.screen()
        terminal.tap("enter")
        assert on_screen(terminal, "PALETTE", UP), terminal.screen()
        terminal.tap("right")
        assert on_screen(terminal, "COLOUR-BLIND SAFE", UP), terminal.screen()
        terminal.tap("escape")  # saves and goes back, the cursor where it was
        assert until(lambda: "> SETTINGS" in terminal.screen() and "PALETTE" not in terminal.screen()), (
            terminal.screen()
        )
        assert json.loads((tmp_path / "p.json").read_text())["settings"]["palette"] == "cvd"
        quit_from_menu(terminal)
        assert terminal.finish(tape=False) == 0
    finally:
        terminal.close()
    again = Pty(tmp_path, fixture="kitty", size=(80, 24))
    try:
        assert on_screen(again, "> LAUNCH", UP), again.screen()
        assert until(lambda: accent_of_title(again) is not None)
        assert default == ("rgb", 86, 180, 233) and accent_of_title(again) == ("rgb", 95, 175, 215)  # theme.py's
        quit_from_menu(again)
        assert again.finish(tape=False) == 0
    finally:
        again.close()


def test_at_80_x_24_with_arcade_chosen_the_menu_still_moves(tmp_path):
    terminal = Pty(tmp_path, fixture="apple-terminal-26", size=(80, 24))
    try:
        assert on_screen(terminal, "> LAUNCH", UP), terminal.screen()
        terminal.tap("down")
        assert on_screen(terminal, "> MODE", UP), terminal.screen()
        terminal.tap("right")
        assert on_screen(terminal, "ARCADE", UP), terminal.screen()
        terminal.tap("up")  # the menu is never locked: Arcade's block (56 x 32) only matters once it plays
        assert on_screen(terminal, "> LAUNCH", UP), terminal.screen()
        quit_from_menu(terminal)
        assert terminal.finish(tape=False) == 0
    finally:
        terminal.close()


@pytest.mark.gate
@pytest.mark.slow  # a staged run to its end, 8-11 s (spec 12: a new test over 5 s is slow)
@pytest.mark.parametrize(
    "fixture, letters, apart, name", [("kitty", "tim", 0.3, "TIM"), ("apple-terminal-26", "ann", 0.15, "ANN")]
)
def test_a_placing_run_takes_typed_initials_and_the_board_shows_them(tmp_path, fixture, letters, apart, name):
    # INVADERS_AUDIO unset (the suite sets "none"): with --no-sound the footer then reads SOUND OFF, and a toggle would
    # change it (to ON, or to n/a where no backend starts); with "none" it reads SOUND n/a whatever M does
    terminal = StagedPty(tmp_path, "--quickstart", fixture=fixture, size=(80, 24), env={"INVADERS_AUDIO": None})
    try:
        assert on_screen(terminal, "NEW HIGH SCORE", 30.0), terminal.screen()
        assert on_screen(terminal, "Type letters", UP), terminal.screen()
        terminal.wait(0.5)
        for letter in letters:  # M types: it never toggles the sound on this card
            terminal.tap(letter)
            terminal.wait(apart)
        assert on_screen(terminal, f"SAVES AS  {name}", UP), terminal.screen()
        terminal.tap("enter")
        assert on_screen(terminal, "Q menu", UP), terminal.screen()
        terminal.tap("q")
        assert on_screen(terminal, "> LAUNCH", UP), terminal.screen()
        assert "SOUND OFF [M]" in terminal.screen()  # muted at launch, still muted: M typed, it toggled nothing
        for _ in range(3):
            terminal.tap("down")
            terminal.wait(GUARD if fixture != "kitty" else 0.1)
        assert on_screen(terminal, "> FLIGHT RECORDS", UP), terminal.screen()
        terminal.tap("enter")
        assert until(lambda: re.search(rf"{name}\s+\d", terminal.screen())), terminal.screen()
        terminal.wait(GUARD)  # Enter again: past the repeat guard of the one that opened the page
        terminal.tap("enter")
        assert on_screen(terminal, "> FLIGHT RECORDS", UP), terminal.screen()
        quit_from_menu(terminal, escape_twice=True)
        assert terminal.finish(tape=False) == 0
        assert [row["name"] for row in json.loads((tmp_path / "p.json").read_text())["scores"]] == [name]
    finally:
        terminal.close()


@pytest.mark.gate
@pytest.mark.slow  # the idle wait and a held key, about 6 s (over 5 s: slow)
def test_the_title_demo_starts_when_idle_and_a_held_enter_ends_it_without_launching(tmp_path):
    terminal = Pty(tmp_path, fixture="apple-terminal-26", size=(80, 24), env={"INVADERS_ATTRACT_IDLE": "2"})
    try:
        assert on_screen(terminal, "> LAUNCH", UP), terminal.screen()
        assert on_screen(terminal, "DEMO  Press any key", UP), terminal.screen()
        hold(terminal, "enter", 1.5)  # the first Enter ends the demo; its OS repeats must not launch
        assert until(lambda: "> LAUNCH" in terminal.screen() and "DEMO" not in terminal.screen()), terminal.screen()
        terminal.wait(0.5)
        assert "> LAUNCH" in terminal.screen() and "SCORE" not in terminal.screen()
        quit_from_menu(terminal, escape_twice=True)
        assert terminal.finish(tape=False) == 0
        assert not (tmp_path / "p.json").exists()  # the demo writes no profile
    finally:
        terminal.close()
