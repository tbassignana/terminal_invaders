"""The game's loop on a FakeTerminal (spec 11.2): idle cadence, bounded input, frame gating by the output queue and
the ack pacer, resizes, early keys, kitty keys and their downgrade, focus, paste, the countdown, repaints and
faults; then run(), --probe and the cli on a real pty."""

import copy
import dataclasses
import errno
import io
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import termios
import threading
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from terminal_invaders import app, cli, fx, screens, tapes
from terminal_invaders.attract import ReadOnlyProfile, SilentAudio
from terminal_invaders.campaign import Game as CampaignGame
from terminal_invaders.input import HOLD_NOTE, HoldControls, LatchedControls, TimedControls, make_controls
from terminal_invaders.keymap import Keymap
from terminal_invaders.model import Config
from terminal_invaders.replay import LEGACY_REFUSAL, Recorder
from terminal_invaders.term.caps import Capabilities, EnvHints
from terminal_invaders.term.decode import Decoder
from terminal_invaders.term.encode import ack_tag, tag_count
from terminal_invaders.term.keys import Key, Reply
from terminal_invaders.term.pacing import FLOOR, TICK, AckPacer
from terminal_invaders.term.tty import ENTER, TITLE, TerminalGone, TerminalSession
from terminal_invaders.ui import Painter, Session
from tests.support.faketerm import FakeTerminal, SizedResponder
from tests.support.frames import options, session
from tests.support.ptyrun import Stub
from tests.support.terminals import capabilities, load, names, taps

DRAW = Painter.draw
SGR = re.compile(rb"\x1b\[([\d;]*)m")
# The colour parameters each tier's frames use: painted truecolor and 256 never fall back to the default colours
FORMS = {"truecolor": {"38;2", "48;2"}, "256": {"38;5", "48;5"}, "16": {"fg16", "bg16", "default"}, "mono": set()}
FRAME = app.Renderer.frame
ADAPT, EXPIRE, HELD, SENT = AckPacer._adapt, AckPacer.expire, AckPacer.held, AckPacer.sent
OFF = AckPacer("off")  # clock pacing: tracks nothing, so tests can share it
ROOT = Path(__file__).resolve().parents[1]
# G3: a whole session on a FakeTerminal (probe, kitty push, play, pause, quit, teardown) in a fresh interpreter.
HEADLESS = """
import sys
import terminal_invaders.audio, terminal_invaders.cli
from tests.test_app import game, play
fake, current, term = game()
fake.type(b"\\x1b[112u", at=0.5)
play(fake, current, term, 1.0)
assert current.scene == "paused" and current.pilot.game.tick > 20, (current.scene, current.pilot.game.tick)
print(sorted(name for name in sys.modules if "curses" in name))
"""


def colour_forms(data: bytes) -> set[str]:
    """The kinds of colour parameter in every SGR of the data: 38;2 or 48;2 (truecolor), 38;5 or 48;5 (256), fg16 or
    bg16 (30-37 and 90-97, 40-47 and 100-107) and default (39, 49); attributes are not colours."""
    forms = set()
    for match in SGR.finditer(data):
        params = [int(part or 0) for part in match[1].split(b";")]
        while params:
            code = params.pop(0)
            if code in (38, 48):
                kind = params.pop(0)
                forms.add(f"{code};{kind}")
                del params[: 3 if kind == 2 else 1]
            elif 30 <= code <= 37 or 90 <= code <= 97:
                forms.add("fg16")
            elif 40 <= code <= 47 or 100 <= code <= 107:
                forms.add("bg16")
            elif code in (39, 49):
                forms.add("default")
    return forms


def game(fixture="ghostty", *, cols=80, rows=24, rtt=0.001, select=None, **settings):
    """A FakeTerminal, a Session and a TerminalSession on it, as app.run() would make them."""
    doc = load(fixture)
    fake = FakeTerminal(doc, cols=cols, rows=rows, rtt=rtt, vt=(200, 80))
    current = session(**settings)
    ops = fake.ops() if select is None else replace(fake.ops(), select=select(fake.select))
    term = TerminalSession(fake.fd, EnvHints.from_env(doc["env"]), app.overrides(current.options), ops=ops)
    return fake, current, term


def play(fake, current, term, until: float, **kwargs):
    """Enter, play until the virtual clock reads `until`, quit and restore: (renderer, the loop's select waits)."""
    fake.at(until, lambda: setattr(current, "quit", True))
    with term:
        start = len(fake.selects)
        renderer = app.play(current, term, **kwargs)
        waits = [timeout for r, _, timeout in fake.selects[start:] if term.wakeup in r]
    return renderer, waits


def watch_frames(record):
    """Renderer.frame, calling record(renderer) before each frame is built."""

    def frame(renderer, *args):
        record(renderer)
        return FRAME(renderer, *args)

    return patch.object(app.Renderer, "frame", frame)


CPR = re.compile(rb"\x1b\[(\d+);(\d+)R")


def winch_inside_a_write(fixture, cols, rows):
    """The game on a FakeTerminal; from 3 s, inside a write of the flush in an iteration whose select() found the tty
    readable and writable, the terminal's SIGWINCH comes (it shrank to cols x rows), and just after it the next ack
    reply, clamped by the shrink. done holds (when, the reply, as clamped)."""
    last, done = {}, []

    def watch(select):
        def watched(r, w, x, timeout):
            found = select(r, w, x, timeout)
            if r and w:
                last["found"] = found
            return found

        return watched

    fake, current, term = game(fixture, rtt=0.1, select=watch)
    write = term.out.write

    def writing(fd, data):
        n = write(fd, data)
        found = last.get("found")
        if not done and fake.now >= 3.0 and found and fake.fd in found[0] and fake.fd in found[1]:
            fake.resize(cols, rows)  # the SIGWINCH, as the game writes
            for i, (when, reply) in enumerate(fake.inbox):
                match = CPR.search(reply)
                if when > fake.now and match:
                    clamped = (min(int(match[1]), rows), min(int(match[2]), cols))
                    done.append((fake.now, (int(match[1]), int(match[2])), clamped))
                    fake.inbox[i] = (fake.now, CPR.sub(b"\x1b[%d;%dR" % clamped, reply, count=1))  # just after it
                    break
        return n

    term.out.write = writing
    return fake, current, term, done


def nudge_before_frames(fake, done):
    """From 3 s, before each frame, a focus-in report (no change) due at once, so that the select() after it finds
    the tty readable as well as writable."""
    seen = set()

    def record(renderer):
        if fake.now >= 3.0 and not done and fake.now not in seen:
            seen.add(fake.now)
            fake.type(b"\x1b[I", at=fake.now)

    return watch_frames(record)


class LoopTests(unittest.TestCase):
    def test_idle_screens_draw_twice_and_wait_a_tenth_of_a_second(self):
        """Once each frame's reply is in: while one is awaited the wait is a tick, so that a stall inside a select()
        that the reply woke early goes uncounted for a tick at most (pacing.py)."""
        for scene in ("menu", "help", "records", "paused", "handoff", "results", "too_small"):
            with self.subTest(scene=scene):
                small = scene == "too_small"
                fake, current, term = game(cols=30 if small else 60)
                current.scene = "playing" if small else scene
                fake.at(0.5, lambda: fake.resize(31 if small else 70, 24))
                with patch.object(Painter, "draw", autospec=True, side_effect=DRAW) as draw:
                    _, waits = play(fake, current, term, 1.0)
                self.assertEqual(draw.call_count, 2)  # the first image, then the resize
                self.assertEqual(current.pilot.game.tick, 0)
                self.assertLessEqual(set(waits), {0.1, TICK})
                self.assertEqual((waits.count(TICK), waits[-1]), (4, 0.1))  # each frame: its write, then its reply

    def test_buffered_input_reaches_the_session_in_order(self):
        fake, current, term = game("xterm")
        typed = b"f" * 32 + b"pq"
        fake.type(typed, at=0.5)
        with patch.object(current, "handle", wraps=current.handle) as handle:
            play(fake, current, term, 1.0)
        self.assertEqual([call.args[0].name for call in handle.call_args_list], list(typed.decode()))

    def test_at_most_64_events_an_iteration_and_the_tty_waits_meanwhile(self):
        fake, current, term = game("xterm")
        fake.type(b"f" * 150, at=0.5)
        batches, events = [], Decoder.events

        def counted(decoder, limit=64):
            batch = events(decoder, limit)
            batches.append((limit, len(batch)))
            return batch

        with patch.object(Decoder, "events", counted), patch.object(current, "handle") as handle:
            play(fake, current, term, 1.0)
        self.assertEqual(handle.call_count, 150)  # none lost
        self.assertEqual(max(n for limit, n in batches if limit == app.READ_LIMIT), 64)
        self.assertIn(([term.wakeup], [], 0.0), fake.selects)  # 86 events still waiting: no read, no sleep

    def test_no_frame_is_built_while_output_waits(self):
        fake, current, term = game()
        built = []
        fake.at(0.5, lambda: setattr(fake, "room", 0))  # the terminal stops reading
        fake.at(1.5, lambda: setattr(fake, "room", None))
        with watch_frames(lambda renderer: built.append((fake.now, term.out.empty()))):
            play(fake, current, term, 2.0)
        self.assertTrue(all(empty for _, empty in built))
        self.assertLessEqual(len([now for now, _ in built if 0.5 < now < 1.5]), 1)  # the one that is stuck
        self.assertGreater(len([now for now, _ in built if now >= 1.5]), 20)

    def test_the_fixed_pacer_keeps_two_frames_in_flight(self):
        fake, current, term = game(rtt=0.1, pacing="fixed")
        inflight = []
        with watch_frames(lambda renderer: inflight.append(len(term.pacer.inflight))):
            renderer, _ = play(fake, current, term, 2.0)
        self.assertEqual(term.pacer.mode, "fixed")
        self.assertLessEqual(max(inflight), 1)
        self.assertLess(renderer.frames, 50)  # two frames a 100 ms round trip, not 60 a second
        fake, current, term = game("silent", rtt=0.1)
        renderer, _ = play(fake, current, term, 2.0)
        self.assertEqual(term.pacer.mode, "off")  # no acks: clock pacing
        self.assertGreater(renderer.frames, 100)  # 60 a second from the end of the 250 ms probe

    def test_the_adaptive_pacer_widens_its_window_to_keep_60_fps_on_a_long_round_trip(self):
        """--pacing auto. In truecolor the wave banner's exit at 4.1 s is a 1 KB frame, several times the frames just
        before it, but no size jump against the largest frame of the last 5 s (the 5 KB first frame, and the banner's
        own 1 KB frames), so the window is never reset. Before decision 6 it started again from the floor there, and
        this test ran in 16 colours only."""
        for color in ("16", "truecolor"):
            with self.subTest(color=color):
                fake, current, term = game(rtt=0.1, color=color)
                seen = []

                def record(renderer, fake=fake, term=term, seen=seen):
                    seen.append((fake.now, len(term.pacer.inflight), term.pacer.window))

                with watch_frames(record):
                    play(fake, current, term, 6.0)
                self.assertEqual(term.pacer.mode, "adaptive")
                settled = [inflight for now, inflight, _ in seen if now >= 4.0]
                self.assertGreaterEqual(len(settled), 2 * 55)  # about 60 a second, where the fixed rule gives under 20
                self.assertIn(max(settled), range(5, 9))  # about a round trip of frames: 6, the target is 8
                self.assertLess(term.pacer.window, 9)
                windows = [window for *_, window in seen]
                self.assertEqual(windows, sorted(windows))  # no repaint, size jump or back-off after the first frame

    def test_a_stall_of_the_game_itself_is_no_congestion(self):
        """The game descheduled for 170 ms at 4.5 s: in a select() that found replies, in one that found none (those
        that came meanwhile wait for the next), in the read that follows a select(), while it builds a frame, or at
        the top of an iteration. The replies that came meanwhile are read late. The loop tells the pacer how long
        they may have waited (read(), and held() for a frame's bytes), so the lower bounds of their round trips show
        no queue: the window neither backs off nor holds, and they are samples still. Untold, W falls to the floor,
        and taking no sample of them instead (S12) left none at all when stalls came often."""

        def pause():
            if not done and fake.now >= 4.5:
                done.append(fake.now)
                fake.now += 0.17  # the host ran nothing of the game

        def watch(select):
            def watched(r, w, x, timeout):
                found = select(r, w, x, timeout)
                if r and where == ("select" if fake.fd in found[0] else "select, nothing read"):
                    pause()  # select() returned 170 ms late
                return found

            return watched

        def paused(method):
            def call(*args):
                pause()
                return method(*args)

            return call

        for where in ("select", "select, nothing read", "read", "frame", "check"):
            for told in (True, False):
                with self.subTest(where=where, told=told):
                    done, seen, bounds = [], [], []
                    fake, current, term = game(rtt=0.1, select=watch)
                    if where in ("read", "check"):
                        setattr(term, where, paused(getattr(term, where)))

                    def record(renderer, fake=fake, term=term, seen=seen, where=where):
                        if where == "frame":
                            pause()
                        seen.append((fake.now, term.pacer.window, term.pacer.hold_until))

                    def adapt(pacer, now, rtt, least, nbytes, bounds=bounds):
                        bounds.append((now, rtt, least))
                        ADAPT(pacer, now, rtt, least, nbytes)

                    untold = {"read": lambda *args: None, "held": lambda *args: None} if not told else {}
                    with watch_frames(record), patch.object(AckPacer, "_adapt", adapt):
                        with patch.multiple(AckPacer, **untold) if untold else nullcontext():
                            play(fake, current, term, 7.0)
                    self.assertTrue(done)
                    before = max(window for now, window, _ in seen if now < 4.5)
                    after = [(window, hold) for now, window, hold in seen if now >= 4.5]
                    if not told:
                        self.assertEqual(min(window for window, _ in after), FLOOR)
                        continue
                    self.assertEqual({hold for _, hold in after}, {-math.inf})
                    self.assertGreaterEqual(min(window for window, _ in after), before)
                    late = [(rtt, least) for now, rtt, least in bounds if done[0] < now <= done[0] + 0.3]
                    self.assertGreater(max(rtt for rtt, _ in late), 0.25)  # the stall is in the upper bounds
                    # and none in the lower ones but a tick, at most, of one in a select() that returned early
                    self.assertLess(max(least for _, least in late), 0.1 + TICK + 0.001)
                    self.assertGreater(sum(now > done[0] for now, *_ in bounds), 60)

    def test_stalls_again_and_again_leave_the_pacer_its_samples(self):
        """F1 of the S12 review: a loaded host stalls the game 40 ms every 100 ms on a 100 ms link. Taking no sample
        of a round trip a stall had touched left none at all: W at the floor, no base RTT, and 15-20 fps."""
        fake, current, term = game(rtt=0.1)
        last, built = [0.0], []
        check = term.check

        def stalling():
            if fake.now >= 0.3 and fake.now - last[0] >= 0.1:
                last[0] = fake.now
                fake.now += 0.04
            check()

        term.check = stalling
        with watch_frames(lambda renderer: built.append(fake.now)):
            play(fake, current, term, 10.0)
        self.assertGreater(sum(now >= 5.0 for now in built) / 5.0, 35)
        self.assertGreater(term.pacer.window, 4)
        self.assertAlmostEqual(term.pacer.base_rtt, 0.1, delta=0.002)

    def test_replies_that_stop_mean_clock_pacing_whatever_the_stalls(self):
        """F6 of the S12 review: replies stop at 4 s, while the host stalls the game 45 ms every 0.7 s. Restarting
        every frame's loss clock at each stall meant no frame was ever lost: the window stayed full and the screen
        froze. Only a frame's own wait in the output queue moves its timeout."""
        fake, current, term = game(rtt=0.1)
        fake.at(4.0, lambda: setattr(fake.responder, "answer", lambda *args: b""))
        last, built = [0.0], []
        check = term.check

        def stalling():
            if fake.now - last[0] >= 0.7:
                last[0] = fake.now
                fake.now += 0.045
            check()

        term.check = stalling
        with watch_frames(lambda renderer: built.append(fake.now)):
            play(fake, current, term, 10.0)
        self.assertTrue(term.pacer.disabled)
        self.assertGreater(sum(now >= 7.0 for now in built) / 3.0, 50)

    def test_a_frame_is_timed_from_when_it_was_built(self):
        """F4 of the S12 review's loop lens: a pause while the game builds a frame is in the waits of the replies
        that came during it, not in that frame's round trip, which starts once it is built."""
        for pause in (0.025, 0.17):
            with self.subTest(pause=pause):
                fake, current, term = game(rtt=0.1)
                done = []

                def record(renderer, fake=fake, done=done, pause=pause):
                    if not done and fake.now >= 4.5:
                        done.append(fake.now)
                        fake.now += pause

                sent = patch.object(AckPacer, "sent", autospec=True, side_effect=AckPacer.sent)
                read = patch.object(AckPacer, "read", autospec=True, side_effect=AckPacer.read)
                with watch_frames(record), sent as sent, read as read:
                    play(fake, current, term, 5.0)
                times = [call.args[1] for call in sent.call_args_list]
                self.assertIn(done[0] + pause, times)
                self.assertNotIn(done[0], times)
                waits = [call.args[2] for call in read.call_args_list if call.args[1] > done[0]]
                self.assertGreaterEqual(waits[0], pause - 1e-9)

    def test_an_idle_select_that_waits_out_its_timeout_is_no_wait(self):
        """An idle screen waits a tenth of a second at a time, polling the tty: a reply that ends such a wait waited
        on nothing."""
        fake, current, term = game(cols=60, rtt=0.05)
        current.scene = "menu"
        for at in (0.5, 1.0, 1.5):
            fake.at(at, lambda at=at: fake.resize(60 + round(10 * at), 24))  # a frame, and its reply
        with patch.object(AckPacer, "read", autospec=True, side_effect=AckPacer.read) as read:
            play(fake, current, term, 2.0)
        self.assertEqual({call.args[2] for call in read.call_args_list}, {0.0})
        self.assertEqual(len(term.pacer.rtts), 4)
        self.assertAlmostEqual(term.pacer.srtt, 0.05, delta=0.001)

    def test_replies_read_in_an_iteration_free_their_frames_before_any_expires(self):
        """A frame's reply comes 10 ms before its loss timeout, and the game reads it 10 ms after (a 20 ms scheduling
        hiccup): the reply still answers that frame, and no frame is given up on."""
        done, losses = [], []

        def late(select):
            def hiccup(r, w, x, timeout):
                found = select(r, w, x, timeout)
                if not done and fake.now >= 4.5 and fake.fd in found[0]:
                    done.append(fake.now)
                    fake.now += 0.02
                return found

            return hiccup

        def expire(pacer, now):
            EXPIRE(pacer, now)
            losses.append(pacer.losses)

        fake, current, term = game(rtt=0.1, select=late)
        fake.at(4.0, lambda: setattr(fake, "rtt", 0.99))  # the first reply after is 10 ms short of the timeout
        with patch.object(AckPacer, "expire", expire):
            play(fake, current, term, 7.0)
        self.assertTrue(done)
        self.assertEqual(max(losses), 0)
        self.assertEqual(term.pacer.owed, len(term.pacer.inflight))

    def test_frames_are_given_up_on_only_up_to_the_last_look_at_the_tty(self):
        """F1 of the S12 review's loop lens: the game descheduled for 0.95 s after a read (in the decoder), or in the
        write that follows a select() that found nothing to read. The replies that came meanwhile wait unread in the
        tty, and giving up on frames up to the time after the stall lost them all, three losses and clock pacing. Up
        to the last look none is lost, nor the frame being written."""
        for where in ("read", "write"):
            with self.subTest(where=where):
                done, readable, losses = [], [], []

                def watch(select, readable=readable):
                    def watched(r, w, x, timeout):
                        found = select(r, w, x, timeout)
                        if r:
                            readable.append(fake.fd in found[0])
                        return found

                    return watched

                fake, current, term = game(rtt=0.1, select=watch)

                def stall(method, fake=fake, done=done, readable=readable, where=where):
                    def call(*args, **kwargs):
                        if not done and fake.now >= 4.5 and readable[-1] == (where == "read"):
                            done.append(fake.now)
                            fake.now += 0.95
                        return method(*args, **kwargs)

                    return call

                def expire(pacer, now, losses=losses):
                    EXPIRE(pacer, now)
                    losses.append((pacer.losses, pacer.disabled))

                with patch.object(AckPacer, "expire", expire):
                    if where == "read":
                        with patch.object(Decoder, "feed", stall(Decoder.feed)):
                            play(fake, current, term, 7.0)
                    else:
                        term.out.flush_some = stall(term.out.flush_some)
                        play(fake, current, term, 7.0)
                self.assertTrue(done)
                self.assertEqual(max(lost for lost, _ in losses), 0)
                self.assertNotIn(True, {disabled for _, disabled in losses})

    def test_a_key_backlog_is_time_the_tty_was_not_read(self):
        """F2 of the S12 review's loop lens: while 64 events or more wait, the loop does not poll the tty, and the
        replies that come meanwhile wait for the backlog. A burst of 640 legacy keys, 1 ms of work each: the first
        read after it is 0.64 s from the last look."""
        fake, current, term = game("xterm", rtt=0.1)
        fake.type(b"f" * 640, at=4.5)
        handle = current.handle

        def slow(key, at):
            if fake.now >= 4.5 and key.name == "f":
                fake.now += 0.001
            return handle(key, at)

        with patch.object(current, "handle", slow):
            with patch.object(AckPacer, "read", autospec=True, side_effect=AckPacer.read) as read:
                play(fake, current, term, 6.0)
        waits = [call.args[2] for call in read.call_args_list if call.args[1] > 4.5]
        self.assertGreater(waits[0], 0.6)

    def test_a_new_pacer_counts_no_wait_from_before_it(self):
        """F3 of the S12 review's loop lens: a reprobe (or a suspend) makes a new pacer. The game did not read the tty
        through the settle and the probe, but none of that is a wait for the new pacer's replies: here a key that
        came as the probe ended, which the first select() after it finds."""
        fake, current, term = game(rtt=0.05)
        fake.type(b"\x1b[112u", at=3.0)  # p (kitty): pause
        fake.at(3.5, lambda: setattr(term, "reprobe", True))
        resume = app.Hooks.after_resume

        def resumed(hooks, caps, reason):
            resume(hooks, caps, reason)
            fake.type(b"x")

        with patch.object(app.Hooks, "after_resume", resumed):
            with patch.object(AckPacer, "read", autospec=True, side_effect=AckPacer.read) as read:
                play(fake, current, term, 5.0)
        old, new = read.call_args_list[0].args[0], term.pacer
        self.assertIsNot(old, new)
        waits = [call.args[2] for call in read.call_args_list if call.args[0] is new]
        self.assertLess(waits[0], 0.001)

    def test_a_frame_held_up_by_the_game_while_queued_counts_that_wait(self):
        """A frame's bytes wait in the output queue for a terminal that reads nothing from 4.5 s to 4.8 s, and the game
        stalls 170 ms with them still queued. That is the frame's held time only if the tty could take the bytes when
        the stall ended: the tty fills only by the game's writes, so one still full then was full all through it (a
        wait on the terminal, not the game). One writable then is counted for the whole stall, though the terminal
        made room only 0.1 s into it (start 4.7): the game cannot tell when, and the over-count only lowers the lower
        bound, the safe side for a back-off (F5 and D2 of the S14 review). Nor is 2 ms of work an iteration while the
        terminal reads nothing."""
        for start, work, expected in ((4.6, 0.0, 0.0), (4.7, 0.0, 0.17), (None, 0.002, 0.0)):
            with self.subTest(start=start, work=work):
                fake, current, term = game(rtt=0.1)
                fake.at(4.5, lambda fake=fake: setattr(fake, "room", 0))
                fake.at(4.8, lambda fake=fake: setattr(fake, "room", None))
                done, check = [], term.check

                def stalling(fake=fake, check=check, done=done, start=start, work=work):
                    if 4.5 <= fake.now < 4.8:
                        fake.now += work
                    if start is not None and not done and fake.now >= start:
                        done.append(fake.now)
                        fake.now += 0.17
                    check()

                term.check = stalling
                waits = {}

                named = []

                def held(pacer, seconds, sent=None, waits=waits, named=named):
                    if pacer.inflight:
                        named.append(sent == pacer.inflight[-1].sent)  # the newest frame built, whose bytes are last
                    if pacer.inflight and sent in (None, pacer.inflight[-1].sent):
                        waits[pacer.inflight[-1].sent] = waits.get(pacer.inflight[-1].sent, 0.0) + seconds
                    HELD(pacer, seconds, sent)

                with patch.object(AckPacer, "held", held):
                    play(fake, current, term, 5.5)
                self.assertTrue(named)
                self.assertNotIn(False, named)
                stuck = [sent for sent in waits if 4.5 <= sent < 4.6]  # built once the terminal stopped reading
                self.assertLessEqual(len(stuck), 1)
                self.assertAlmostEqual(sum(waits.get(sent, 0.0) for sent in stuck), expected, delta=0.001)
                self.assertFalse([sent for sent, wait in waits.items() if wait and sent not in stuck])

    def test_a_read_that_leaves_input_in_the_tty_is_no_look(self):
        """A 6 KB paste is more than a read takes: what follows it in the tty (the replies that came meanwhile) waited
        from the last look before it, through a 170 ms stall in that first read or in the select() that found it."""
        for where in ("read", "select"):
            with self.subTest(where=where):
                box, done = {}, []

                def wrap(select, box=box, done=done, where=where):
                    def watched(r, w, x, timeout):
                        fake = box["fake"]
                        found = select(r, w, x, timeout)
                        if where == "select" and not done and fake.now >= 4.5 and fake.fd in found[0]:
                            done.append(fake.now)
                            fake.now += 0.17
                        return found

                    return watched

                fake, current, term = game(rtt=0.1, select=wrap)
                box["fake"] = fake
                fake.type(b"\x1b[200~" + b"a" * 6000 + b"\x1b[201~", at=4.5)
                read = term.read

                def stalled(n, fake=fake, done=done, read=read, where=where):
                    data = read(n)
                    if where == "read" and not done and fake.now >= 4.5:
                        done.append(fake.now)
                        fake.now += 0.17
                    return data

                term.read = stalled
                with patch.object(AckPacer, "read", autospec=True, side_effect=AckPacer.read) as told:
                    play(fake, current, term, 5.0)
                waits = [call.args[2] for call in told.call_args_list if call.args[1] > done[0]]
                least = 0.17 - (TICK if where == "select" else 1e-9)  # one woken early: counted past its timeout
                self.assertGreaterEqual(waits[0], least)  # the first read's unread is the stall too
                self.assertGreaterEqual(waits[1], least)

    def test_a_stall_in_a_select_a_reply_woke_is_counted_past_a_tick_while_paused(self):
        """L1 of the S13 review: off play the select() waits 0.1 s, and a stall inside one that a key or a reply woke
        early goes uncounted up to that timeout. While replies are awaited the wait is a tick, so the lower bounds
        read after an 80 ms stall are within a tick of the round trip. Paused, with keys at 60 Hz (each a frame),
        they were 0.18, 0.163 and 0.147 s on a 0.1 s link: a back-off and a 2 s hold, into the play resumed at 4.8 s.
        The round trip is off the tick grid (105 ms), so that a reply wakes a tick-long select() early (D1 of the S14
        review: on the grid every select() ran to its timeout, and the stall came only once nothing was in flight)."""
        box, done, bounds, holds = {}, [], [], []

        def wrap(select):
            def watched(r, w, x, timeout):
                fake = box["fake"]
                start = fake.now
                found = select(r, w, x, timeout)
                if not done and fake.now >= 4.45 and found[0] and timeout and fake.now < start + timeout:
                    done.append((fake.now, timeout, current.scene, len(term.pacer.inflight)))
                    fake.now += 0.08  # descheduled once it was woken, inside its timeout
                return found

            return watched

        def adapt(pacer, now, rtt, least, nbytes):
            bounds.append((now, rtt, least))
            ADAPT(pacer, now, rtt, least, nbytes)

        fake, current, term = game(rtt=0.105, select=wrap)
        box["fake"] = fake
        fake.type(b"\x1b[112u", at=4.0)  # p: pause
        for i in range(18):
            fake.type(b"\x1b[120u", at=4.2 + i / 60)  # x: no command while paused, but each key is a frame
        fake.type(b"\x1b[112u", at=4.8)  # p: resume
        with patch.object(AckPacer, "_adapt", adapt), watch_frames(lambda r: holds.append(term.pacer.hold_until)):
            play(fake, current, term, 7.0)
        self.assertTrue(done)
        at, timeout, scene, inflight = done[0]
        self.assertEqual((timeout, scene), (TICK, "paused"))
        self.assertGreater(inflight, 0)
        late = [least for now, _, least in bounds if at <= now <= at + 0.3]
        self.assertTrue(late)
        self.assertLess(max(late), 0.105 + TICK + 0.001)
        self.assertEqual(set(holds), {-math.inf})

    def test_while_playing_every_tick_is_stepped_on_time_whatever_the_round_trip(self):
        """D3 of the S14 review: off play the select() waits a tick at most while replies are awaited. In play it
        waits to the next tick: a tick from now instead stepped ticks up to a tick late, 8 ms at the median on a link
        whose round trip is off the tick grid, as every real one is."""
        for rtt in (0.105, 0.033):
            with self.subTest(rtt=rtt):
                fake, current, term = game(rtt=rtt)
                for k in range(80):
                    fake.type(b"a", at=2.0 + 0.0937 * k)
                steps, step = [], type(current).step

                def stepped(session, now, steps=steps, step=step):
                    steps.append(now)
                    return step(session, now)

                with patch.object(type(current), "step", stepped):
                    play(fake, current, term, 10.0)
                late = [now - (steps[0] + k * TICK) for k, now in enumerate(steps) if now >= 3.0]
                self.assertGreater(len(late), 400)
                self.assertLess(max(late), 0.001)

    def test_a_stall_after_a_read_copied_the_tty_is_time_the_next_read_waited(self):
        """P4 of the S13 review: the game descheduled 170 ms just after a read copied the tty's bytes, before it read
        the clock. The replies that came meanwhile wait for the next read, which counts from just before the first:
        their lower bounds show no queue. Counted from after it, they carried the whole stall, and W backed off to the
        floor for 2 s on a clean link. Each read takes 20 us here, so that the two reads are apart."""
        fake, current, term = game(rtt=0.1)
        read, done, bounds = term.read, [], []

        def reading(n):
            data = read(n)
            fake.now += 20e-6
            if not done and fake.now >= 4.5:
                done.append(fake.now)
                fake.now += 0.17
            return data

        term.read = reading

        def adapt(pacer, now, rtt, least, nbytes):
            bounds.append((now, rtt, least))
            ADAPT(pacer, now, rtt, least, nbytes)

        with patch.object(AckPacer, "_adapt", adapt):
            play(fake, current, term, 6.0)
        late = [least for now, _, least in bounds if done[0] <= now <= done[0] + 0.4]
        self.assertGreater(len(late), 3)
        self.assertLess(max(late), 0.1 + TICK + 0.001)
        self.assertEqual(term.pacer.hold_until, -math.inf)

    def test_replies_read_behind_64_events_are_handled_before_any_frame_is_given_up_on(self):
        """L3 of the S13 review: the game descheduled 1.2 s in a select() on a 100 ms link, and 200 legacy keys (a
        paste without bracketed paste) arrive as the stall begins, ahead of the replies to the frames in flight. The
        read after it takes them all, and the loop handles 64 events an iteration: the frames are given up on only
        once no reply it read is left unhandled. Given up on at once, all were lost: clock pacing, then W at the
        floor."""
        for keys in (40, 200):
            with self.subTest(keys=keys):
                box, done, log = {}, [], []

                def wrap(select, box=box, done=done, keys=keys):
                    def watched(r, w, x, timeout):
                        fake = box["fake"]
                        if not done and fake.now >= 4.5:
                            done.append(fake.now)
                            fake.type(b"f" * keys, at=fake.now + 0.0001)
                            fake.now += 1.2
                        return select(r, w, x, timeout)

                    return watched

                def expire(pacer, now, log=log):
                    EXPIRE(pacer, now)
                    log.append((pacer.losses, pacer.disabled))

                fake, current, term = game("xterm", rtt=0.1, select=wrap)
                box["fake"] = fake
                with patch.object(AckPacer, "expire", expire):
                    play(fake, current, term, 7.0)
                self.assertTrue(done)
                self.assertEqual(max(lost for lost, _ in log), 0)
                self.assertNotIn(True, {disabled for _, disabled in log})

    def test_a_stall_between_the_select_and_the_write_is_the_frame_s_held_time(self):
        """L4 of the S13 review: a 170 ms stall inside flush_some(), before it writes the frame built in this
        iteration. The tty could take the bytes since the select() said so, so they waited on the game: the frame's
        lower bound is the round trip. Counted from after the write, it kept the whole stall (0.27 s on a 0.1 s
        link)."""
        fake, current, term = game(rtt=0.1)
        flush, done, bounds = term.out.flush_some, [], []

        def stalled(*args, **kwargs):
            if not done and fake.now >= 4.5 and term.out.pending() == term.out.frame_len:
                done.append(fake.now)
                fake.now += 0.17
            return flush(*args, **kwargs)

        term.out.flush_some = stalled

        def adapt(pacer, now, rtt, least, nbytes):
            bounds.append((now, rtt, least))
            ADAPT(pacer, now, rtt, least, nbytes)

        with patch.object(AckPacer, "_adapt", adapt):
            play(fake, current, term, 6.0)
        late = [least for now, _, least in bounds if done[0] <= now <= done[0] + 0.4]
        self.assertTrue(late)
        self.assertLess(max(late), 0.1 + TICK + 0.001)

    def test_in_play_on_the_virtual_clock_no_reply_waits_unread(self):
        """The game's work takes no virtual time, so each reply is read as it comes: unread is 0 at every read.
        Counted from a select()'s start instead of its end, an empty poll made every read a tick late."""
        fake, current, term = game(rtt=0.1)
        reads = []
        with patch.object(AckPacer, "read", lambda pacer, at, unread: reads.append(unread)):
            play(fake, current, term, 3.0)
        self.assertGreater(len(reads), 100)
        self.assertEqual(max(reads), 0.0)

    def test_a_stall_with_nothing_queued_is_no_frame_s_held_time(self):
        """A 170 ms stall at the top of an iteration that builds no frame, with nothing in the output queue: replies
        may have waited on it, but no frame's bytes did."""
        fake, current, term = game(rtt=0.25)
        current.scene = "menu"
        fake.at(1.0, lambda: fake.resize(81, 24))
        done, check, waits = [], term.check, []

        def stalling():
            if not done and fake.now >= 1.1:
                done.append(fake.now)
                fake.now += 0.17
            check()

        term.check = stalling
        with patch.object(AckPacer, "held", lambda pacer, seconds, sent=None: waits.append(seconds)):
            play(fake, current, term, 2.0)
        self.assertTrue(done)
        self.assertEqual([wait for wait in waits if wait], [])

    def test_a_frame_built_into_a_full_tty_waits_on_the_terminal_not_the_game(self):
        """A 170 ms stall just after a frame is built, before the select(): its bytes waited on the game only if the
        tty could take them, as the select() then says. With the terminal reading nothing from 4.5 s to 4.8 s, the
        frame built then has no held time; with it reading, the frame has the whole stall."""
        for full, expected in ((False, 0.17), (True, 0.0)):
            with self.subTest(full=full):
                fake, current, term = game(rtt=0.1)
                if full:
                    fake.at(4.5, lambda fake=fake: setattr(fake, "room", 0))
                    fake.at(4.8, lambda fake=fake: setattr(fake, "room", None))
                done, waits = [], {}

                def sent(pacer, now, *args, fake=fake, done=done, **kwargs):
                    SENT(pacer, now, *args, **kwargs)
                    if not done and fake.now >= 4.5:
                        done.append(now)
                        fake.now += 0.17

                def held(pacer, seconds, sent=None, waits=waits):
                    waits[sent] = waits.get(sent, 0.0) + seconds
                    HELD(pacer, seconds, sent)

                with patch.object(AckPacer, "sent", sent), patch.object(AckPacer, "held", held):
                    play(fake, current, term, 5.5)
                self.assertTrue(done)
                self.assertAlmostEqual(waits.get(done[0], 0.0), expected, delta=0.001)

    def test_a_stall_after_a_select_is_held_time_only_if_the_tty_could_take_the_bytes(self):
        """The terminal reads nothing from 4.5 s to 4.8 s, so a frame's bytes sit queued, and the game stalls 170 ms
        once: inside a select() that waited out its timeout with the tty still full, inside the one that found it
        writable again at 4.8 s, or draining the wakeup pipe (a resize) with the tty full. Only the second is the
        frame's held time: from the select()'s timeout on, as the game cannot tell when inside it the stall began."""
        cases = (("timeout", 4.55, 0.0, 0.0), ("writable", 4.8, 0.17 - TICK - 0.001, 0.17), ("wakeup", 4.6, 0.0, 0.0))
        for where, when, least, most in cases:
            with self.subTest(where=where):
                box, done, waits = {}, [], {}

                def wrap(select, box=box, done=done, where=where, when=when):
                    def watched(r, w, x, timeout):
                        fake = box["fake"]
                        found = select(r, w, x, timeout)
                        writable = fake.fd in found[1]
                        main = bool(r) and fake.fd in w and fake.now >= when and not done
                        if main and where == "timeout" and not writable or main and where == "writable" and writable:
                            done.append(fake.now)
                            fake.now += 0.17
                        return found

                    return watched

                fake, current, term = game(rtt=0.1, select=wrap)
                box["fake"] = fake
                fake.at(4.5, lambda fake=fake: setattr(fake, "room", 0))
                fake.at(4.8, lambda fake=fake: setattr(fake, "room", None))
                if where == "wakeup":
                    fake.at(4.6, lambda fake=fake: fake.resize(80, 24))
                    drain = term.drain_wakeup

                    def draining(fake=fake, done=done, drain=drain):
                        if not done and fake.now >= 4.6:
                            done.append(fake.now)
                            fake.now += 0.17
                        drain()

                    term.drain_wakeup = draining

                def held(pacer, seconds, sent=None, waits=waits):
                    waits[sent] = waits.get(sent, 0.0) + seconds
                    HELD(pacer, seconds, sent)

                with patch.object(AckPacer, "held", held):
                    play(fake, current, term, 5.5)
                self.assertTrue(done)
                stuck = [sent for sent in waits if sent is not None and 4.5 <= sent < 4.55]
                self.assertEqual(len(stuck), 1)
                self.assertGreaterEqual(waits[stuck[0]], least)
                self.assertLessEqual(waits[stuck[0]], most + 0.001)

    def test_only_an_ack_reply_left_unhandled_holds_off_giving_up_on_frames(self):
        decoder = Decoder(esc_timeout=0.05, string_timeout=0.5)
        self.assertFalse(app.acks_queued(decoder, "cpr"))
        decoder.queue.extend([Reply("focus", True, 1.0), Reply("dsr", 0, 1.0)])
        self.assertFalse(app.acks_queued(decoder, "cpr"))
        self.assertFalse(app.acks_queued(decoder, None))
        self.assertTrue(app.acks_queued(decoder, "dsr"))
        decoder.queue.append(Reply("cpr", (4, 2), 1.0))
        self.assertTrue(app.acks_queued(decoder, "cpr"))

    def test_a_read_waits_from_the_last_look_not_from_the_select(self):
        """A 170 ms stall in the write, after a select() that found both replies to read and room to write: the read
        that follows counts it, and the next one (input that came just after it) counts from just before that read,
        not from the select() again."""
        box, found, done, reads = {}, [], [], []

        def wrap(select):
            def watched(r, w, x, timeout):
                fake = box["fake"]
                if not found and fake.now >= 4.5 and fake.fd in r and fake.fd in w:
                    fake.type(b"\x1b[I", at=fake.now)  # a focus report, ready as the select() starts
                    found.append(fake.now)
                return select(r, w, x, timeout)

            return watched

        fake, current, term = game(rtt=0.1, select=wrap)
        box["fake"] = fake
        flush = term.out.flush_some

        def stalled(*args, **kwargs):
            if found and not done:
                done.append(fake.now)
                fake.now += 0.17
            return flush(*args, **kwargs)

        term.out.flush_some = stalled
        read, typed = term.read, []

        def reading(n):
            data = read(n)
            if done and not typed:  # the next select() finds this at once: no empty poll looks in between
                typed.append(fake.now)
                fake.type(b"\x1b[O", at=fake.now)
            return data

        term.read = reading
        with patch.object(AckPacer, "read", lambda pacer, at, unread: reads.append((at, unread))):
            play(fake, current, term, 5.5)
        self.assertTrue(done)
        after = [(at, unread) for at, unread in reads if at > done[0]]
        self.assertAlmostEqual(after[0][1], 0.17, delta=0.001)
        self.assertEqual(after[1][0], typed[0])
        self.assertLess(max(unread for _, unread in after[1:]), TICK)

    def test_frames_tagged_off_a_shrunk_screen_free_their_slots_when_the_game_learns_its_size(self):
        """P2 of the S13 review: a terminal reached over a 100 ms link shrinks to 50 columns, and hears of it 50 ms
        before the game does. The frames it reads meanwhile are tagged on columns 51-60, whose CUP it clamps onto
        column 50, so their replies do not name them until the game learns the size. With the window full (fixed
        pacing), the screen at the new size waited for the oldest to be given up on, about 1 s; it now goes out as
        soon as the game learns the size."""
        fake, current, term = game(rtt=0.1, pacing="fixed")
        answer, shrunk, learned, drawn = fake.responder.answer, [], [], []

        def clamped(queries, cprs=()):
            seen = fake.responder.cprs_seen
            if shrunk:
                cprs = [(row, min(col, 50)) if i >= seen else (row, col) for i, (row, col) in enumerate(cprs)]
            return answer(queries, cprs)

        fake.responder.answer = clamped

        def learn():
            learned.append(fake.now)
            fake.resize(50, 24)

        def frame(renderer, *args):
            data = FRAME(renderer, *args)
            if renderer.buffer.cols == 50 and not drawn:
                drawn.append(fake.now)
            elif not shrunk and fake.now >= 3.0 and renderer.last_tag and renderer.last_tag[1] > 52:
                shrunk.append(fake.now)  # the terminal shrinks as it reads this frame
                fake.at(fake.now + 0.05, learn)
            return data

        with patch.object(app.Renderer, "frame", frame):
            play(fake, current, term, 6.0)
        self.assertTrue(learned and drawn)
        self.assertLess(drawn[0] - learned[0], 0.15)

    def test_a_reply_read_with_the_resize_signal_is_handled_at_the_new_size(self):
        """F1 of the S14 review's law lens, its second path: the select() that delivers a SIGWINCH can also read a
        reply the shrink clamped, which was handled before the game learned the size at the next iteration's top, and
        so could still settle a frame tagged on the new screen's last row or column. The pacer now learns it first."""
        fake, current, term = game(rtt=0.1)
        answer, shrunk, events = fake.responder.answer, [], []

        def answering(queries, cprs=()):
            out = answer(queries, cprs)
            if out and not shrunk and fake.now >= 3.0:
                shrunk.append(fake.now + fake.rtt)
                fake.at(fake.now + fake.rtt, lambda: fake.resize(70, 22))  # as this reply comes
            return out

        fake.responder.answer = answering
        resized, acked = AckPacer.resized, AckPacer.acked

        def resizing(pacer, cols, rows):
            events.append(("resized", (cols, rows)))
            resized(pacer, cols, rows)

        def acking(pacer, now, tag=None):
            events.append(("acked", now))
            acked(pacer, now, tag)

        with patch.object(AckPacer, "resized", resizing), patch.object(AckPacer, "acked", acking):
            play(fake, current, term, 3.5)
        at = shrunk[0]
        learned = events.index(("resized", (70, 22)))
        read = next(index for index, (kind, value) in enumerate(events) if kind == "acked" and value >= at)
        self.assertEqual(events[read], ("acked", at))
        self.assertLess(learned, read)

    def test_a_screen_that_grows_keeps_every_frame_in_flight(self):
        """D7 of the S14 review: the loop gives resized() the columns first. Swapped, a grow to 100x30 dropped every
        frame tagged from column 30 on (resized(30, 100): tag_area keeps off the last column), as no loss, and their
        replies were no samples."""
        fake, current, term = game(rtt=0.1)
        kept, grown = [], []
        resized = AckPacer.resized

        def resizing(pacer, cols, rows):
            before = [frame.tag for frame in pacer.inflight]
            resized(pacer, cols, rows)
            kept.append((before, [frame.tag for frame in pacer.inflight]))

        def grow(renderer):
            if not grown and fake.now >= 3.0 and renderer.last_tag and renderer.last_tag[1] > 40:
                grown.append(fake.now)
                fake.at(fake.now + 0.02, lambda: fake.resize(100, 30))

        with patch.object(AckPacer, "resized", resizing), watch_frames(grow):
            play(fake, current, term, 4.0)
        self.assertTrue(grown)
        before, after = kept[-1]
        self.assertTrue([col for _, col in before if col > 40])
        self.assertEqual(after, before)

    def test_time_blocked_in_a_write_is_no_frame_s_held_time(self):
        """F1 of the S14 review's loop lens: the tty fd blocks, so a write the select() let through can wait for room
        (here 50 ms) while the terminal reads. The bytes then wait on the terminal, not on the game: no held time.
        Counted as held, a slow reader's queue left the lower bounds, and W never backed off."""
        fake, current, term = game(rtt=0.1)
        write, done, waits = term.out.write, [], {}

        def blocking(fd, data):
            first = term.out.pending() == term.out.frame_len  # the frame's first write
            n = write(fd, data)
            if not done and fake.now >= 4.5 and first:
                done.append(term.pacer.inflight[-1].sent)
                fake.now += 0.05
            return n

        term.out.write = blocking

        def held(pacer, seconds, sent=None):
            waits[sent] = waits.get(sent, 0.0) + seconds
            HELD(pacer, seconds, sent)

        with patch.object(AckPacer, "held", held):
            play(fake, current, term, 5.5)
        self.assertTrue(done)
        self.assertLess(waits.get(done[0], 0.0), 0.001)

    def test_a_stall_before_writing_bytes_left_from_an_earlier_iteration_is_their_frame_s_held_time(self):
        """D7 of the S14 review, for L4: the terminal reads nothing from 4.5 s to 4.8 s, so a frame's bytes stay
        queued across iterations, and the game stalls 170 ms in flush_some() before it writes them, once the tty
        takes bytes again: they waited on the game."""
        fake, current, term = game(rtt=0.1)
        fake.at(4.5, lambda: setattr(fake, "room", 0))
        fake.at(4.8, lambda: setattr(fake, "room", None))
        flush, done, waits = term.out.flush_some, [], {}

        def stalled(*args, **kwargs):
            if not done and fake.now >= 4.8 and not term.out.empty():
                done.append(fake.now)
                fake.now += 0.17
            return flush(*args, **kwargs)

        term.out.flush_some = stalled

        def held(pacer, seconds, sent=None):
            waits[sent] = waits.get(sent, 0.0) + seconds
            HELD(pacer, seconds, sent)

        with patch.object(AckPacer, "held", held):
            play(fake, current, term, 5.5)
        self.assertTrue(done)
        stuck = [sent for sent in waits if sent is not None and 4.5 <= sent < 4.55]
        self.assertEqual(len(stuck), 1)
        self.assertAlmostEqual(waits[stuck[0]], 0.17, delta=0.001)

    def test_a_stall_just_after_the_last_look_at_a_full_tty_is_held_time_once_the_tty_takes_bytes(self):
        """F2 of the S14 review's loop lens: the terminal reads nothing from 4.5 s to 4.8 s, and the game is
        descheduled for 170 ms just after it last finds the tty full, from 4.75 s on: on the way out of the poll
        before the select(), or of a select() that waited out its timeout. The terminal reads again meanwhile, so the
        bytes waited on the game from 4.8 s on. The poll's stall fell in no span, and the select()'s was counted from
        after it."""
        polls = {
            "poll": lambda r, w, timeout: not r and w and timeout == 0,
            "select": lambda r, w, timeout: r and w and timeout,
        }
        for where, which in polls.items():
            with self.subTest(where=where):
                box, done, waits = {}, [], {}

                def wrap(select, box=box, done=done, which=which):
                    def watched(r, w, x, timeout):
                        fake = box["fake"]
                        found = select(r, w, x, timeout)
                        loop = sys._getframe(1).f_code.co_name == "loop"
                        if loop and which(r, w, timeout) and not found[0] and not found[1]:
                            if not done and fake.now >= 4.75:
                                done.append(fake.now)
                                fake.now += 0.17
                        return found

                    return watched

                fake, current, term = game(rtt=0.1, select=wrap)
                box["fake"] = fake
                fake.at(4.5, lambda fake=fake: setattr(fake, "room", 0))
                fake.at(4.8, lambda fake=fake: setattr(fake, "room", None))

                def held(pacer, seconds, sent=None, waits=waits):
                    waits[sent] = waits.get(sent, 0.0) + seconds
                    HELD(pacer, seconds, sent)

                with patch.object(AckPacer, "held", held):
                    play(fake, current, term, 5.5)
                self.assertTrue(done)
                stuck = [sent for sent in waits if sent is not None and 4.5 <= sent < 4.6]
                self.assertEqual(len(stuck), 1)
                self.assertGreaterEqual(waits[stuck[0]], done[0] + 0.17 - 4.8 - 0.001)

    def test_a_key_backlog_is_scanned_for_acks_only_when_a_frame_is_due_to_be_given_up_on(self):
        """F3 of the S14 review's loop lens: draining a backlog at 64 events an iteration, a scan of the whole queue
        for an ack in every iteration cost O(N^2): for a paste the Linux console does not bracket, 11 ms of scans per
        4096 events, as much as handling the keys. Only a frame due to be given up on needs it."""
        fake, current, term = game("linux-console")
        fake.type(b"a" * 8192, at=1.0)
        with patch.object(app, "acks_queued", autospec=True, side_effect=app.acks_queued) as scans:
            play(fake, current, term, 2.0)
        self.assertEqual(scans.call_count, 0)

    def test_on_a_screen_shrunk_to_one_row_the_clamped_replies_are_acks_not_f3(self):
        """CPRT-1 and F3 of the S14 review: a terminal shrunk to one row clamps the CUP of the ack queries it reads
        after onto row 1, and those replies read like F3 with modifiers. They reached the session as keys, owed stayed
        at the window (a restart there waited its whole settle time), and on a legacy terminal the burst taught the
        RepeatModel 16 ms repeat gaps for the session. Now the replies still owed when the game learns the size are
        acks."""
        fake, current, term = game("xterm", rtt=0.1)
        fake.responder = SizedResponder(load("xterm"))
        answer, shrunk, keys = fake.responder.answer, [], []

        def clamped(queries, cprs=()):
            seen = fake.responder.cprs_seen
            if shrunk:
                cprs = [(1, col) if i >= seen else (row, col) for i, (row, col) in enumerate(cprs)]
            return answer(queries, cprs)

        fake.responder.answer = clamped

        def shrink(renderer):
            if not shrunk and fake.now >= 3.0:
                shrunk.append(fake.now)  # the terminal shrinks as it reads this frame; the game hears 50 ms later
                fake.at(fake.now + 0.05, lambda: fake.resize(80, 1))

        handle = type(current).handle

        def handling(session, key, at):
            keys.append(key.name)
            return handle(session, key, at)

        with watch_frames(shrink), patch.object(type(current), "handle", handling):
            play(fake, current, term, 4.0)
        self.assertTrue(shrunk)
        self.assertNotIn("f3", keys)
        self.assertEqual(term.pacer.owed, 0)
        self.assertEqual(current.router.gate.repeat.intervals, [])

    def test_a_fifo_pacer_on_clock_pacing_still_waits_a_tick_at_most_while_frames_are_in_flight(self):
        """D10 of the S14 review: with DSR acks (a terminal that answers no CPR) frames are matched in order, so they
        are tracked while clock pacing is on, and the reply that turns ack pacing back on is a sample. Paused, the
        select() waits a tick at most while they are in flight, as for CPR acks, so that a stall inside one a reply
        woke counts past a tick. It waited 0.1 s, as nothing has a deadline while clock pacing is on."""
        doc = load("ghostty")
        doc = {**doc, "replies": re.sub(r"\x1b\[\d+;\d+R", "", doc["replies"])}
        fake = FakeTerminal(doc, rtt=0.1, vt=(200, 80))
        current, waits = session(), []

        def watched(r, w, x, timeout):
            if term.wakeup in r and current.scene == "paused" and term.pacer.disabled and term.pacer.inflight:
                waits.append(timeout)
            return fake.select(r, w, x, timeout)

        ops = replace(fake.ops(), select=watched)
        term = TerminalSession(fake.fd, EnvHints.from_env(doc["env"]), app.overrides(current.options), ops=ops)
        answer = fake.responder.answer
        fake.responder.answer = lambda *args: b"" if 3.0 <= fake.now < 4.3 else answer(*args)  # replies lost
        fake.type(b"\x1b[112u", at=2.5)  # p: pause
        for i in range(90):
            fake.type(b"\x1b[120u", at=3.0 + i / 60)  # x: no command while paused, but each key is a frame
        play(fake, current, term, 5.0)
        self.assertEqual(term.caps.ack, "dsr")
        self.assertTrue(waits)
        self.assertLessEqual(max(waits), TICK)

    def test_a_probe_on_one_row_asks_again_once_the_screen_is_taller(self):
        """CPRT-2 of the S14 review: on one row the probe's CUPs to rows 2 and 3 clamp onto row 1, so its CPRs read as
        F3 and Shift+F3. No CPR echo, so DSR acks matched in order for the rest of the session, and the two keys
        reached the game. Its F3 keys are now dropped, and once the screen is taller the game probes again at the
        pause."""
        fake, current, term = game("ghostty", rtt=0.02)
        fake.responder = SizedResponder(load("ghostty"))
        fake.vt.resize(80, 24)
        seen, keys = [], []

        def resize(cols, rows):
            fake.vt.resize(cols, rows)
            fake.resize(cols, rows)

        fake.at(1.0, lambda: resize(80, 1))
        fake.type(b"x", at=1.5)  # bare text in kitty mode: a downgrade, so a probe at this (small) pause
        fake.at(1.7, lambda: resize(70, 1))  # still one row: no probe
        fake.at(1.8, lambda: seen.append((term.caps.ack, term.pacer.tagged, term.small_probe)))
        fake.at(2.0, lambda: resize(80, 24))
        handle = type(current).handle

        def handling(session, key, at):
            keys.append(key.name)
            return handle(session, key, at)

        with patch.object(type(current), "handle", handling):
            play(fake, current, term, 3.0)
        self.assertEqual(seen, [("dsr", False, True)])
        self.assertEqual((term.caps.ack, term.pacer.tagged, term.small_probe), ("cpr", True, False))
        self.assertEqual(bytes(fake.output).count(ENTER), 3)
        self.assertNotIn("f3", keys)

    def test_replies_tmux_clamps_onto_one_row_before_the_pane_s_sigwinch_are_acks(self):
        """OR1 of the S15 review: tmux resizes a pane's grid at once, and answers at its new size, but sends the pane
        its TIOCSWINSZ (so the game's SIGWINCH) for a second resize only when a 250 ms timer ends (tmux 3.4
        window_pane_resize; a live run gave 142-193 ms of CSI 1;5R replies before the 1-row SIGWINCH). Over ssh
        (200 ms) a drag takes the pane from 80x24 to 80x16 and, 50 ms later, to 80x1. The decoder read a row-1 CPR as
        an ack only once the game had learned the one row: 10 F3 keys reached the session, owed and clamped stayed at
        10, and the RepeatModel learnt 16 ms gaps. Now it reads as many as the replies owed."""
        doc = load("tmux-3.6-in-ghostty")
        env = {k: v for k, v in doc["env"].items() if k not in ("TMUX", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")}
        doc = dict(doc, env=env | {"SSH_CONNECTION": "1"})
        fake = FakeTerminal(doc, rtt=0.2, vt=(80, 24))
        fake.responder = SizedResponder(doc)
        current = session()
        term = TerminalSession(fake.fd, EnvHints.from_env(doc["env"]), app.overrides(current.options), ops=fake.ops())
        fake.at(4.9, lambda: fake.vt.resize(80, 16))  # tmux's grid, as it parses the frames written then
        fake.at(5.1, lambda: fake.resize(80, 16))  # the first resize: TIOCSWINSZ at once
        fake.at(4.95, lambda: fake.vt.resize(80, 1))
        fake.at(5.35, lambda: fake.resize(80, 1))  # the second: when the 250 ms timer ends
        keys, owed = [], []
        fake.at(6.0, lambda: owed.append((term.pacer.owed, term.hooks.decoder.clamped)))
        handle = type(current).handle

        def handling(session, key, at):
            keys.append(key.name)
            return handle(session, key, at)

        with patch.object(type(current), "handle", handling):
            play(fake, current, term, 7.0)
        self.assertEqual(term.caps.ack, "cpr")
        self.assertEqual((keys.count("f3"), owed), (0, [(0, 0)]))
        self.assertEqual(current.router.gate.repeat.intervals, [])

    def test_a_size_whose_sigwinch_comes_inside_a_write_is_learned_before_the_replies_read_after_it(self):
        """W1 of the S15 review: the size a SIGWINCH announces was learned after drain_wakeup, before the flush and
        the read. One that came inside a write of the flush (where a backed-up game spends most of its time) was
        learned only at the next iteration's top, after the replies read in this one were handled at the old size:
        a reply clamped onto one row then read as F3, and one clamped by a shrink settled no frame. Here the shrink's
        SIGWINCH comes inside such a write, and the next ack reply, clamped by it, just after."""
        for fixture, cols, rows in (("xterm", 80, 1), ("ghostty", 70, 22)):
            with self.subTest(fixture=fixture, size=(cols, rows)):
                fake, current, term, done = winch_inside_a_write(fixture, cols, rows)
                if rows == 1:
                    fake.responder = SizedResponder(load(fixture))
                events, keys = [], []
                resized, acked, handle = AckPacer.resized, AckPacer.acked, type(current).handle

                def resizing(pacer, cols, rows, events=events, resized=resized):
                    events.append(("resized", (cols, rows), fake.now))
                    resized(pacer, cols, rows)

                def acking(pacer, now, tag=None, events=events, acked=acked):
                    events.append(("acked", tag, now))
                    acked(pacer, now, tag)

                def handling(session, key, at, keys=keys, handle=handle):
                    keys.append(key.name)
                    return handle(session, key, at)

                with (
                    patch.object(AckPacer, "resized", resizing),
                    patch.object(AckPacer, "acked", acking),
                    patch.object(type(current), "handle", handling),
                    nudge_before_frames(fake, done),
                ):
                    play(fake, current, term, 4.0)
                self.assertTrue(done)  # the window was hit
                at, _, reply = done[0]
                learned = next(i for i, e in enumerate(events) if e[0] == "resized" and e[1] == (cols, rows))
                handled = next(i for i, e in enumerate(events) if e[0] == "acked" and e[2] >= at and e[1] == reply)
                self.assertLess(learned, handled)
                self.assertNotIn("f3", keys)

    def test_a_game_started_on_one_row_asks_again_in_the_menu_once_the_screen_is_taller(self):
        """OR2 of the S15 review: `invaders` starts on the menu, and a probe on one row picks DSR acks. The game
        probed again only at a pause, so a game started on one row and grown in the menu played every frame matched
        in order. It now probes again outside play: in the menu, a second after the grow, the acks are CPRs."""
        for fixture in ("xterm", "ghostty"):
            with self.subTest(fixture=fixture):
                fake, current, term = game(fixture, rows=1, rtt=0.05, quickstart=False)
                fake.responder = SizedResponder(load(fixture))
                fake.vt.resize(80, 1)
                seen, acks = [], []

                def resize(cols, rows, fake=fake):
                    fake.vt.resize(cols, rows)
                    fake.resize(cols, rows)

                fake.at(1.0, lambda: resize(80, 24))
                fake.at(2.0, lambda: seen.append((current.scene, term.caps.ack, term.pacer.tagged, term.reprobe)))
                fake.type(b"\r", at=2.5)  # Start
                with watch_frames(lambda r: acks.append(r.caps.ack) if current.scene == "playing" else None):
                    play(fake, current, term, 6.0)
                self.assertEqual(seen, [("menu", "cpr", True, False)])
                self.assertEqual(set(acks), {"cpr"})
                self.assertEqual(bytes(fake.output).count(ENTER), 2)

    def test_a_screen_that_grows_while_the_probe_is_out_is_probed_again(self):
        """OR3 of the S15 review: small_probe was read after the probe and the kitty push, so a window one row high
        when the probe went out and taller when its replies came read as a full-size probe. Its CUPs had clamped onto
        row 1: F3 and Shift+F3 reached the game, and DSR acks stayed for the session. small_probe now takes the
        smaller of the sizes before and after the probe, and as nothing is drawn yet the game asks again at once:
        with --quickstart S16 waited for a pause, so the whole first run went on DSR acks (LD3 of the S16 review)."""
        for fixture, grow_at in (("xterm", 0.02), ("ghostty", 0.02), ("ghostty", 0.07)):
            with self.subTest(fixture=fixture, grow_at=grow_at):
                fake, current, term = game(fixture, rows=1, rtt=0.05)
                fake.responder = SizedResponder(load(fixture))
                fake.vt.resize(80, 1)
                seen, keys = [], []

                def resize(cols, rows, fake=fake):
                    fake.vt.resize(cols, rows)
                    fake.resize(cols, rows)

                fake.at(grow_at, lambda: resize(80, 24))
                fake.at(0.5, lambda: seen.append((term.caps.ack, term.small_probe)))
                fake.at(3.5, lambda: seen.append((term.caps.ack, term.pacer.tagged, term.small_probe)))
                handle = type(current).handle

                def handling(session, key, at, keys=keys, handle=handle):
                    keys.append(key.name)
                    return handle(session, key, at)

                with patch.object(type(current), "handle", handling):
                    play(fake, current, term, 4.0)
                self.assertEqual((seen, current.scene), ([("cpr", False), ("cpr", True, False)], "playing"))
                self.assertNotIn("f3", keys)

    def test_a_downgrade_then_a_resize_before_the_pause_still_probes_again(self):
        """DM2 of the S15 review: check() ORs a small probe's reprobe into the one a kitty downgrade set. Assigned,
        the resize that pauses the game cleared it, and as kitty mode was off by then no later downgrade set it
        again: tap controls and the old caps for good."""
        fake, current, term = game("ghostty", rtt=0.02)
        flags = []
        fake.type(b"x", at=2.0)  # bare text in kitty mode during play: a downgrade, a probe at the next pause
        fake.at(2.2, lambda: flags.append(term.reprobe))
        fake.at(2.3, lambda: fake.resize(100, 30))  # which pauses the game
        play(fake, current, term, 3.0)
        self.assertEqual((flags, current.scene), ([True], "paused"))
        self.assertEqual(bytes(fake.output).count(ENTER), 2)  # the first probe, and the one at the resize's pause

    def test_a_screen_that_shrinks_to_one_row_while_the_probe_is_out_is_a_small_probe(self):
        """OR3's other direction (the S16 review): the probe goes out at 80x24 and the terminal reads it on one row,
        so its CPRs come back on row 1 and read as F3. small_probe takes the smaller size: the keys are dropped."""
        for fixture in ("xterm", "ghostty"):
            with self.subTest(fixture=fixture):
                fake, current, term = game(fixture, rtt=0.05)
                fake.responder = SizedResponder(load(fixture))
                seen, keys = [], []
                fake.vt.resize(80, 1)  # one row high already: the game hears of it at 0.02
                fake.at(0.02, lambda fake=fake: fake.resize(80, 1))
                fake.at(0.5, lambda: seen.append((term.caps.ack, term.small_probe)))
                handle = type(current).handle

                def handling(session, key, at, keys=keys, handle=handle):
                    keys.append(key.name)
                    return handle(session, key, at)

                with patch.object(type(current), "handle", handling):
                    play(fake, current, term, 1.0)
                self.assertEqual((seen, keys), ([("dsr", True)], []))

    def test_a_game_started_on_one_row_asks_again_on_the_help_screen_once_the_screen_is_taller(self):
        """Outside play is any scene but play: the help screen too, not only the menu."""
        fake, current, term = game("xterm", rows=1, rtt=0.05, quickstart=False)
        fake.responder = SizedResponder(load("xterm"))
        fake.vt.resize(80, 1)
        fake.at(0.5, lambda: setattr(current, "scene", "help"))

        def resize(cols, rows):
            fake.vt.resize(cols, rows)
            fake.resize(cols, rows)

        fake.at(1.0, lambda: resize(80, 24))
        seen = []
        fake.at(2.0, lambda: seen.append((current.scene, term.caps.ack, term.reprobe)))
        play(fake, current, term, 2.5)
        self.assertEqual(seen, [("help", "cpr", False)])

    def test_a_grow_in_the_menu_with_a_dsr_ack_in_flight_probes_again_onto_cpr_acks(self):
        """MUT-inherit-old-tagged-crash of the S17 review: started on one row (DSR acks), a key redraws the menu and
        the screen grows before its reply comes, so the restart's new CPR pacer meets the DSR pacer's frame still in
        flight, which carries no tag: kept as a tagged one, it failed the next resized()."""
        for fixture in ("xterm", "ghostty"):
            with self.subTest(fixture=fixture):
                fake, current, term = game(fixture, rows=1, rtt=0.1, quickstart=False)
                fake.responder = SizedResponder(load(fixture))
                fake.vt.resize(80, 1)
                seen = []

                def resize(cols, rows, fake=fake):
                    fake.vt.resize(cols, rows)
                    fake.resize(cols, rows)

                fake.type(b"\x1b[B", at=0.99)  # down: the menu is drawn again, its DSR query in flight
                fake.at(1.0, lambda term=term: seen.append(len(term.pacer.inflight)))
                fake.at(1.02, lambda: resize(80, 24))
                fake.at(2.0, lambda current=current, term=term: seen.append((current.scene, term.caps.ack)))
                play(fake, current, term, 2.5)
                self.assertEqual(seen, [1, ("menu", "cpr")])

    def test_a_restart_soon_after_a_restart_waits_for_the_replies_owed(self):
        """RST-settle-srtt-reset of the S17 review: a restart's new pacer has measured no round trip, so the next
        restart waited 0.15 s whatever the link. The replies owed then came into its probe, and one read on a row-one
        flap the game never heard of was an F3 key. It waits three of the round trips its probe measured."""
        for fixture, rtt in (("ghostty", 0.25), ("xterm", 0.2)):
            with self.subTest(fixture=fixture, rtt=rtt):
                fake, current, term = game(fixture, rtt=rtt)
                fake.responder = SizedResponder(load(fixture))
                fake.vt.resize(80, 24)
                keys, waits, enters = [], [], []
                restart, enter, feed = TerminalSession.restart, TerminalSession.enter, fake.vt.feed

                def restarting(self, reason, waits=waits):
                    waits.append(self.settle_time())
                    return restart(self, reason)

                def entering(self, skip=frozenset(), enters=enters, fake=fake):
                    enter(self, skip)
                    enters.append(fake.now)
                    if len(enters) == 2:  # the first restart done: its first frame is read on one row (a flap)
                        fake.vt.resize(80, 1)

                def feeding(data, enters=enters, fake=fake, feed=feed, term=term):
                    n = feed(data)
                    if len(enters) == 2 and b"\x1b[6n" in data and fake.vt.rows == 1:
                        fake.vt.resize(80, 24)  # back, before the game hears of it
                        fake.at(fake.now + 0.02, lambda: term.pending.add(signal.SIGCONT))  # the second restart
                    return n

                fake.vt.feed = feeding
                fake.at(3.0, lambda term=term: term.pending.add(signal.SIGCONT))  # the first restart (a pause)
                handle = type(current).handle

                def handling(session, key, at, keys=keys, handle=handle):
                    keys.append(key.name)
                    return handle(session, key, at)

                with (
                    patch.object(type(current), "handle", handling),
                    patch.object(TerminalSession, "restart", restarting),
                    patch.object(TerminalSession, "enter", entering),
                ):
                    play(fake, current, term, 6.0)
                self.assertEqual(len(waits), 2)
                self.assertAlmostEqual(waits[1], 3 * rtt, delta=0.02)  # S17: 0.15 s
                self.assertNotIn("f3", keys)

    def test_shift_f3_in_its_ss3_form_is_no_shift_r_in_the_pause_menu(self):
        """DEC-ss3-param-letter of the S17 review: ESC O 2 R, Shift+F3 as some terminals send it, was read as Shift+R,
        which the pause menu takes for restart: the run started over."""
        fake, current, term = game("konsole-old", rtt=0.001)
        seen = []
        fake.type(b"p", at=3.0)
        fake.at(3.4, lambda: seen.append((current.scene, current.pilot.game.tick)))
        fake.type(b"\x1bO2R", at=3.5)
        fake.at(3.9, lambda: seen.append((current.scene, current.pilot.game.tick)))
        play(fake, current, term, 4.0)
        self.assertEqual(seen[1], seen[0])

    def test_a_bound_modified_f3_key_is_a_key_in_play_over_a_link(self):
        """LD1 of the S16 review: a keymap may bind a modified F3, which a terminal without kitty keys sends as
        CSI 1;m R, like a reply clamped onto row 1. S16 read every such sequence as a reply while one was owed, all
        the time over a link of a frame's RTT or more, so the key never worked there. No tag takes its columns now."""
        cases = [
            ("xterm", 0.001, "ctrl+f3", b"\x1b[1;5R"),
            ("xterm", 0.1, "ctrl+f3", b"\x1b[1;5R"),
            ("vte-gnome", 0.05, "alt+f3", b"\x1b[1;3R"),
            ("apple-terminal-15", 0.05, "f3", b"\x1b[1;2R"),  # shift does not matter: Shift+F3 is F3
            ("ssh-apple-terminal", 0.05, "ctrl+f3", b"\x1b[1;5R"),
        ]
        for fixture, rtt, binding, sequence in cases:
            with self.subTest(fixture=fixture, rtt=rtt, binding=binding), TemporaryDirectory() as folder:
                path = Path(folder) / "keys.json"
                path.write_text(json.dumps({"bind": {"play.pause": ["p", "escape", "pause", binding]}}))
                fake, current, term = game(fixture, rtt=rtt, keymap=Keymap.load(path))
                seen = []
                fake.at(2.99, lambda: seen.append((current.scene, term.caps.ack, term.pacer.owed > 0 or rtt < 0.01)))
                fake.type(sequence, at=3.0)
                fake.at(3.5, lambda: seen.append((current.scene,)))
                play(fake, current, term, 4.0)
                self.assertEqual(seen, [("playing", "cpr", True), ("paused",)])

    def test_a_held_modified_f3_key_leaves_clock_pacing_on(self):
        """LD2 of the S16 review: held while the terminal answers no ack query, each repeat of Ctrl+F3 read as a reply
        that ended clock pacing, so play ran at a frame a second for as long as it was held. No tag takes its column
        (encode.TAG_SKIP), so it is a key, and never reaches the pacer, where any reply ends clock pacing."""
        fake, current, term = game("xterm", rtt=0.05)
        answer = fake.responder.answer
        fake.responder.answer = lambda *args: b"" if fake.now >= 2.0 else answer(*args)
        for i in range(100):  # held from 3 s to 8 s
            fake.type(b"\x1b[1;5R", at=3.0 + 0.05 * i)
        frames = []
        with watch_frames(lambda r: frames.append(fake.now) if current.ticking else None):
            play(fake, current, term, 9.0)
        per_second = [sum(1 for t in frames if s <= t < s + 1) for s in range(9)]
        self.assertGreater(min(per_second[3:8]), 50, per_second)
        self.assertTrue(term.pacer.disabled)

    def test_a_restart_onto_dsr_acks_leaves_no_reply_owed_to_the_decoder(self):
        """OR1 of the S15 review, residue (the S16 review): clamped was set only with CPR acks, so a restart at one
        row onto DSR acks kept the count the old session left, and that many CSI 1;c R keys read as replies."""
        fake, current, term = game("xterm", rtt=0.1)
        fake.responder = SizedResponder(load("xterm"))
        answer, snaps = fake.responder.answer, []

        def answering(queries, cprs=(), fake=fake):
            if 4.95 <= fake.now < 6.45 and "cpr" in queries[fake.responder.seen :]:
                fake.responder.seen, fake.responder.cprs_seen = len(queries), len(cprs)
                return b""  # replies lost
            return answer(queries, cprs)

        fake.responder.answer = answering

        def shrink():
            fake.vt.resize(80, 1)
            fake.resize(80, 1)

        fake.at(5.0, shrink)
        fake.type(b"\x1b[I", at=6.4)  # a focus report: a read, before which the loop sets clamped
        fake.at(6.45, lambda: snaps.append((term.caps.ack, term.hooks.decoder.clamped > 0)))
        fake.at(6.5, lambda: term.pending.add(signal.SIGCONT))  # a restart, on one row: DSR acks
        feeds = []

        def watch():
            decoder = term.hooks.decoder
            feed = decoder.feed

            def feeding(chunk, at):
                feeds.append((term.caps.ack, decoder.clamped, term.pacer.owed))
                return feed(chunk, at)

            decoder.feed = feeding

        fake.at(6.6, watch)  # after the restart began: the probe reads with a decoder of its own
        play(fake, current, term, 8.0)
        self.assertEqual(snaps, [("cpr", True)])
        self.assertTrue(any(owed for _, _, owed in feeds), feeds)  # DSR replies owed as they were read
        self.assertEqual({(ack, clamped) for ack, clamped, _ in feeds}, {("dsr", 0)})

    def test_a_short_outage_on_a_two_megabit_link_is_followed_by_play_at_s13_s_rate(self):
        """The critic's finding of the S16 review: on the reference SSH link (2 Mbit, 100 ms) with frames of about
        1.1 KB, the probe kept in flight across a 2 s outage was answered with the outage's round trip, and with two
        frames queued behind the backlog that backed W off to the floor and held it 2 s: 63 frames in the 3 s after
        the outage, against 145 on S13 and S15. The game-to-terminal direction is a FIFO bottleneck of 250 KB/s plus
        RTT/2 (as tests/support/relay.py); during the outage nothing goes, and replies due then come at its end."""
        rate, outage, half = 250_000, (5.0, 7.0), 0.05

        class Link(FakeTerminal):
            free = last = 0.0
            started = False

            def write(self, fd, data):
                got = []
                self.type = lambda reply, at=None: got.append(reply)
                try:
                    n = FakeTerminal.write(self, fd, data)
                finally:
                    del self.type
                if not self.started:
                    for reply in got:
                        FakeTerminal.type(self, reply, self.now + self.rtt)
                    return n
                start = max(self.now, self.free)
                if outage[0] <= start < outage[1]:
                    start = outage[1]
                self.free = start + n / rate
                for reply in got:
                    at = self.free + 2 * half
                    if outage[0] <= at < outage[1]:
                        at = outage[1]
                    at = self.last = max(at, self.last + 0.0005)
                    FakeTerminal.type(self, reply, at)
                return n

        built, sent = [], AckPacer.sent

        def sending(pacer, now, nbytes, *, full, tag=None):
            built.append(now)
            fake.started = True
            return sent(pacer, now, nbytes, full=full, tag=tag)

        with patch.object(sys.modules[__name__], "FakeTerminal", Link):
            fake, current, term = game(rtt=2 * half)
        fake.limit = 71.0
        with patch.object(AckPacer, "sent", sending):
            play(fake, current, term, 11.0, stress=1000)
        self.assertGreaterEqual(sum(7.0 <= t < 10.0 for t in built), 130)

    def test_off_play_the_wait_is_a_tenth_once_no_frame_is_in_flight_though_replies_are_owed(self):
        """DM3 of the S15 review: off play the select() waits a tick at most while a frame is in flight, and a
        tenth of a second otherwise. owed counts lost replies until a later reply proves them lost, so the menu woke
        at 60 Hz for as long as one reply, or all of them, went unanswered when owed decided."""
        for lost in ("one", "all"):
            with self.subTest(lost=lost):
                waits = []

                def watch(select, waits=waits):
                    def watched(r, w, x, timeout):
                        if term.wakeup in r:
                            waits.append((timeout, term.pacer.owed, len(term.pacer.inflight)))
                        return select(r, w, x, timeout)

                    return watched

                fake, current, term = game(cols=60, rtt=0.1, select=watch)
                current.scene = "menu"
                answer, end = fake.responder.answer, 2.2 if lost == "one" else math.inf
                fake.responder.answer = lambda *args, end=end: b"" if 2.0 <= fake.now < end else answer(*args)
                for i in range(1 if lost == "one" else 3):
                    fake.at(2.1 + 0.3 * i, lambda i=i: fake.resize(70 + i, 24))  # a redraw: one frame
                play(fake, current, term, 8.0)
                idle = [timeout for timeout, owed, inflight in waits if owed and not inflight]
                self.assertTrue(idle)
                self.assertEqual(set(idle), {0.1})
                self.assertEqual(term.pacer.owed, 1 if lost == "one" else 3)

    def test_replies_clamped_onto_one_row_are_acks_however_the_shrink_s_signal_comes(self):
        """DM4 of the S15 review: the replies a shrink to one row clamps onto row 1 are acks when the SIGWINCH is
        read in the same select() as the first of them (over ssh the window change and the replies often come
        together), when it lands outside the select() after the loop asked for it, and when they come after the
        screen grew back."""
        for case in ("same select", "outside the select", "grown back"):
            with self.subTest(case=case):
                fake, current, term = game("xterm", rtt=0.2 if case == "grown back" else 0.1)
                fake.responder = SizedResponder(load("xterm"))
                answer, shrunk, signalled, keys = fake.responder.answer, [], [], []

                def answering(queries, cprs=(), fake=fake, case=case, answer=answer, shrunk=shrunk):
                    if case != "outside the select" and not shrunk and fake.now >= 3.0:
                        if "cpr" in queries[fake.responder.seen :]:
                            shrunk.append(fake.now)
                            if case == "same select":  # this reply is clamped, and comes with the SIGWINCH
                                fake.at(fake.now + fake.rtt, lambda: fake.resize(80, 1))
                            else:  # one row for 0.1 s from now; the game hears 50 ms later
                                fake.at(fake.now + 0.05, lambda: fake.resize(80, 1))
                                fake.at(fake.now + 0.15, lambda: fake.resize(80, 24))
                    if shrunk and (case != "grown back" or fake.now < shrunk[0] + 0.1):
                        seen = fake.responder.cprs_seen
                        cprs = [(1, col) if i >= seen else (row, col) for i, (row, col) in enumerate(cprs)]
                    return answer(queries, cprs)

                fake.responder.answer = answering
                read, handle = term.read, type(current).handle

                def reading(n, fake=fake, read=read, shrunk=shrunk, signalled=signalled):
                    data = read(n)
                    if shrunk and not signalled and fake.now >= shrunk[0] + 0.05:
                        signalled.append(fake.now)
                        fake.resize(80, 1)  # handled now: pending, past this iteration's size_changed()
                    return data

                def shrinking(renderer, fake=fake, shrunk=shrunk):
                    if not shrunk and fake.now >= 3.0:
                        shrunk.append(fake.now)  # the terminal shrinks as it reads this frame
                        fake.type(b"\x1b[I", at=fake.now + 0.05)  # something to read when the game hears of it

                def handling(session, key, at, keys=keys, handle=handle):
                    keys.append(key.name)
                    return handle(session, key, at)

                if case == "outside the select":
                    term.read = reading
                watching = watch_frames(shrinking) if case == "outside the select" else nullcontext()
                with watching, patch.object(type(current), "handle", handling):
                    play(fake, current, term, 5.0 if case == "grown back" else 4.0)
                self.assertTrue(signalled if case == "outside the select" else shrunk)
                self.assertNotIn("f3", keys)
                self.assertEqual(term.pacer.owed, 0)

    def test_the_session_matches_replies_by_tag_wherever_the_terminal_answers_cpr(self):
        """T1 of the S13 review: every fixture that answers CPR gets a tagged pacer, so a reply lost mid-stream leaves
        each later round trip its own. Matched in order, with fixed pacing, the lost frame stayed at the head for
        good: every round trip read twice the link's, and the game ran at half the rate."""
        for name in names():
            with self.subTest(fixture=name):
                fake, current, term = game(name)
                play(fake, current, term, 0.6)
                self.assertEqual(term.pacer.tagged, term.caps.ack == "cpr")
        fake, current, term = game(rtt=0.1, pacing="fixed")
        answer, dropped, built = fake.responder.answer, [], []

        def losing(*args):
            out = answer(*args)
            if out and not dropped and fake.now >= 3.0:
                dropped.append(fake.now)
                return b""
            return out

        fake.responder.answer = losing
        with watch_frames(lambda renderer: built.append(fake.now)):
            play(fake, current, term, 6.0)
        self.assertTrue(dropped)
        self.assertAlmostEqual(term.pacer.srtt, 0.1, delta=0.002)
        self.assertGreater(sum(4.0 <= t < 6.0 for t in built), 38)  # 2 frames per round trip

    def test_pacing_off_sends_no_ack_queries(self):
        fake, current, term = game(rtt=0.1, pacing="off")
        renderer, _ = play(fake, current, term, 2.0)
        self.assertEqual((term.pacer.mode, term.caps.ack), ("off", None))
        self.assertEqual(bytes(fake.output).count(b"\x1b[5n"), 1)  # the probe's DSR only
        self.assertGreater(renderer.frames, 100)

    def test_a_storm_of_resizes_is_one_relayout(self):
        fake, current, term = game()
        sizes = [(60 + n % 40, 24 + n % 20) for n in range(50)]
        fake.at(0.5, lambda: [fake.resize(*size) for size in sizes])
        with patch.object(app.Renderer, "relayout", autospec=True, side_effect=app.Renderer.relayout) as relayout:
            renderer, _ = play(fake, current, term, 1.0)
        self.assertEqual(relayout.call_count, 2)  # startup, then the storm
        self.assertEqual((renderer.buffer.cols, renderer.buffer.rows), sizes[-1])
        buffer = renderer.buffer
        screen = [line[: buffer.cols] for line in fake.screen[: buffer.rows]]
        self.assertEqual(screen, [buffer.row_text(y) for y in range(buffer.rows)])

    def test_at_30_fps_each_frame_carries_two_ticks(self):
        fake, current, term = game(fps=30)
        ticks = []
        with watch_frames(lambda renderer: ticks.append(renderer.ticks_pending)):
            play(fake, current, term, 1.0)
        self.assertEqual(ticks[0], 0)
        self.assertEqual(set(ticks[1:]), {2})
        self.assertAlmostEqual(len(ticks), 30, delta=2)
        self.assertGreaterEqual(current.pilot.game.tick, 58)

    def test_keys_typed_during_the_probe_reach_the_session(self):
        fake, current, term = game(rtt=0.05)
        fake.type(b"p", at=0.01)  # after the input flush, before the replies
        play(fake, current, term, 0.5)
        self.assertEqual(current.scene, "paused")
        self.assertEqual(current.pilot.game.tick, 0)

    def test_a_key_typed_during_the_probe_of_a_kitty_terminal_is_a_tap_and_not_held(self):
        fake, current, term = game("ghostty", rtt=0.05)
        fake.type(b"d", at=0.025)  # a legacy key: the push has not happened, so no release will come for it
        xs = []
        fake.at(1.0, lambda: xs.append(current.pilot.game.player.x))
        fake.at(2.0, lambda: xs.append((current.controls.scheme, current.controls.held, current.pilot.game.player.x)))
        play(fake, current, term, 2.1)
        self.assertEqual(xs[1], ("hold", {}, xs[0]))  # moved once, then stood still
        self.assertEqual(xs[0], 30.4)

    def test_a_window_too_small_pauses_play_and_resume_waits_for_it_to_fit(self):
        # S7 replaces Phase 0's rule (a small window let only q through) with a pause that refuses to resume.
        fake, current, term = game("xterm", cols=40, rows=12)
        fake.type(b"p", at=0.3)  # refused while small
        fake.at(0.4, lambda: fake.resize(41, 12))
        seen = []
        fake.at(0.6, lambda: seen.append((current.scene, current.pause_reason, current.pilot.game.tick)))
        fake.at(0.7, lambda: fake.resize(80, 24))
        fake.at(0.8, lambda: seen.append((current.scene, current.pause_reason, current.pilot.game.tick)))
        fake.type(b"p", at=1.5)  # resume: 3-2-1, then play
        play(fake, current, term, 3.0)
        self.assertEqual(seen, [("paused", "small", 0), ("paused", "resized", 0)])
        self.assertAlmostEqual(current.pilot.game.tick, (3.0 - 2.4) * 60, delta=2)

    def test_the_results_panel_is_drawn_after_the_last_tick_without_a_key(self):
        fake, current, term = game("ghostty")
        fake.at(0.5, lambda: setattr(current.pilot.game, "phase", "game_over"))  # the last life goes
        shown = []
        fake.at(1.0, lambda: shown.append((current.scene, "SIGNAL LOST" in "\n".join(fake.vt.lines()))))
        play(fake, current, term, 1.1)
        self.assertEqual(shown, [("results", True)])

    def test_the_countdown_draws_each_digit_once_and_then_play_ticks(self):
        fake, current, term = game("xterm")
        fake.at(0.5, lambda: fake.resize(90, 30))  # pauses: Window resized
        fake.type(b"p", at=1.0)
        shown = []
        with watch_frames(lambda renderer: shown.append((fake.now, current.context, current.pilot.game.tick))):
            _, waits = play(fake, current, term, 2.5)
        counting = [(round(now, 3), tick) for now, context, tick in shown if context == "countdown"]
        self.assertEqual([now for now, _ in counting], [1.0, 1.3, 1.6])
        self.assertEqual(len({tick for _, tick in counting}), 1)  # the engine does not tick meanwhile
        played = [(now, tick) for now, context, tick in shown if now > 1.9]
        self.assertTrue(played and all(context == "play" for now, context, _ in shown if now > 1.9))
        self.assertAlmostEqual(played[-1][1] - counting[0][1], (2.5 - 1.9) * 60, delta=2)

    def test_kitty_keys_hold_the_ship_and_release_stops_it(self):
        fake, current, term = game("ghostty")
        fake.type(b"\x1b[100u", at=0.5)  # d down
        fake.type(b"\x1b[100;1:2u", at=0.9)  # an OS repeat
        fake.type(b"\x1b[100;1:3u", at=1.0)  # d up
        moves = []
        fake.at(0.75, lambda: moves.append(current.pilot.game.player.x))
        fake.at(1.25, lambda: moves.append(current.pilot.game.player.x))
        fake.at(1.5, lambda: moves.append(current.pilot.game.player.x))
        play(fake, current, term, 1.6)
        self.assertEqual((term.caps.kitty_flags, current.controls.scheme), (11, "hold"))
        self.assertIn(b"\x1b[>11u", bytes(fake.output))
        self.assertGreater(moves[1], moves[0])
        self.assertEqual(moves[1], moves[2])  # the release stopped it
        self.assertEqual(fake.vt.kitty_depth, 0)  # popped on the way out

    def test_controls_follow_the_flag_and_say_when_the_terminal_cannot_hold(self):
        for fixture, controls, expected, note in (
            ("ghostty", "auto", HoldControls, ""),
            ("ghostty", "latched", LatchedControls, ""),
            ("ghostty", "timed", TimedControls, ""),
            ("xterm", "auto", LatchedControls, ""),
            ("xterm", "hold", LatchedControls, HOLD_NOTE),
        ):
            with self.subTest(fixture=fixture, controls=controls):
                fake, current, term = game(fixture, controls=controls)
                play(fake, current, term, 0.3)
                self.assertIs(type(current.controls), expected)
                self.assertEqual(current.note, note)

    def test_no_kitty_keeps_legacy_keys_on_a_kitty_terminal(self):
        fake, current, term = game("ghostty", no_kitty=True)
        play(fake, current, term, 0.5)
        self.assertNotIn(b"\x1b[>11u", bytes(fake.output))
        self.assertEqual((term.caps.kitty_flags, current.controls.scheme), (0, "latched"))

    def test_a_bare_key_in_kitty_mode_means_tap_controls_until_the_next_pause_reprobes(self):
        fake, current, term = game("ghostty")
        states = []

        def record():
            gate = current.router.gate
            states.append((term.kitty_depth, current.controls.scheme, gate.certain, fake.vt.kitty_depth, current.note))

        def reset():  # the terminal dropped the protocol, as a reset does
            fake.vt.kitty["alt"].clear()
            fake.type(b"d")

        fake.at(0.4, record)
        fake.at(0.5, reset)
        fake.at(0.6, record)
        fake.type(b"p", at=1.0)
        fake.at(1.5, record)
        play(fake, current, term, 2.0)
        # kitty_depth stays 1 after the downgrade: nothing says the flags are gone, and the restart pops them
        tapping = (1, "latched", False, 0, app.DOWNGRADE_NOTE)
        self.assertEqual(states, [(1, "hold", True, 1, ""), tapping, (1, "hold", True, 1, "")])  # the note goes too
        self.assertEqual(bytes(fake.output).count(ENTER), 2)  # entered again with a fresh probe
        self.assertEqual(current.scene, "paused")
        self.assertEqual(fake.vt.kitty_depth, 0)

    def test_tap_controls_after_a_downgrade_over_ssh_keep_the_wider_held_fire_gap(self):
        # Space events 0.2 s apart, then a steer as soon after: a held Space over SSH (gap 0.25), taps locally (0.12)
        for fixture, held in (("ghostty", False), ("ssh-iterm2", True)):
            with self.subTest(fixture=fixture):
                fake, current, term = game(fixture)
                seen = []

                def reset():  # the terminal dropped the protocol: a bare key
                    fake.vt.kitty["alt"].clear()
                    fake.type(b"d")

                def check():
                    controls = copy.deepcopy(current.controls)
                    for n in range(4):
                        controls.feed("fire", Key("space", time=10 + 0.2 * n), True, 10 + 0.2 * n)
                    controls.feed("right", Key("d", time=10.8), True, 10.8)
                    seen.append((controls.scheme, controls.held_fire))

                fake.at(0.5, reset)
                fake.at(0.6, check)
                play(fake, current, term, 0.8)
                self.assertEqual(seen, [("latched", held)])

    def test_a_false_downgrade_still_leaves_one_push_to_pop(self):
        fake, current, term = game("ghostty")
        states = []

        def record():
            states.append((term.kitty_depth, list(fake.vt.kitty["alt"]), current.controls.scheme))

        fake.type(b"\x1b", at=0.4)  # a kitty key cut after its ESC for longer than any timeout: '[' reads as bare
        fake.type(b"[100u\x1b[100;1:3u", at=1.0)
        fake.at(1.1, record)
        fake.type(b"\x1b[112u\x1b[112;1:3u", at=1.2)  # p: the pause re-probes, with the flags still pushed
        fake.at(1.6, record)
        play(fake, current, term, 2.0)
        self.assertEqual(states, [(1, [11], "latched"), (1, [11], "hold")])  # popped, then pushed: one level
        self.assertEqual((fake.vt.kitty["alt"], fake.vt.kitty["main"]), ([], []))

    def test_focus_loss_pauses_and_a_paste_moves_nothing(self):
        fake, current, term = game("xterm")
        fake.type(b"\x1b[200~ddddqqqq\x1b[201~", at=0.5)
        seen = []
        fake.at(0.7, lambda: seen.append((current.scene, current.controls.direction)))
        fake.type(b"\x1b[O", at=0.8)
        fake.type(b"\x1b[I", at=1.0)
        play(fake, current, term, 1.2)
        self.assertEqual(seen, [("playing", 0)])
        self.assertEqual((current.scene, current.pause_reason), ("paused", "focus"))

    def test_ctrl_l_repaints_every_cell_without_erasing(self):
        fake, current, term = game("xterm")
        current.scene = "menu"
        fake.at(0.5, lambda: fake.vt.feed(b"\x1b[5;5Hgarbage"))  # the screen got scribbled on
        fake.type(b"\x0c", at=0.6)
        marks, shown = [], []
        fake.at(0.55, lambda: marks.append(len(fake.output)))
        fake.at(0.9, lambda: (marks.append(len(fake.output)), shown.extend(line[:80] for line in fake.vt.lines()[:24])))
        renderer, _ = play(fake, current, term, 1.0)
        repaint = bytes(fake.output)[marks[0] : marks[1]]
        for y in range(1, 25):
            self.assertIn(f"\x1b[{y};1H".encode(), repaint)  # every row written again, the scribbled one too
        self.assertNotIn(b"\x1b[2J", repaint)
        self.assertNotIn(b"\x1b[J", repaint)
        self.assertEqual(shown, [renderer.buffer.row_text(y) for y in range(24)])

    def test_a_crash_restores_the_terminal_first(self):
        fake, current, term = game()
        with self.assertRaisesRegex(RuntimeError, "crash at tick 30"):
            play(fake, current, term, 5.0, crash_at=30)
        self.assertFalse(fake.vt.alt_screen)
        self.assertEqual(fake.attrs, FakeTerminal().attrs)
        self.assertEqual(current.pilot.game.tick, 30)

    def test_a_terminal_that_hangs_up_is_gone(self):
        fake, current, term = game()
        fake.at(0.5, lambda: setattr(fake, "dead", True))  # reads and writes fail: the read says so first
        with self.assertRaises(TerminalGone):
            play(fake, current, term, 2.0)

    def test_a_terminal_that_stops_accepting_output_is_gone(self):
        fake, current, term = game()
        fake.at(0.5, lambda: setattr(fake, "write_error", errno.EIO))  # reads still work
        with self.assertRaises(TerminalGone) as gone:
            play(fake, current, term, 2.0)
        self.assertEqual(str(gone.exception), "the terminal stopped accepting output")

    def test_f1_shows_the_stats_line_and_it_counts_the_keys_routed(self):
        fake, current, term = game("xterm")
        fake.type(b"\x1bOP", at=0.5)  # F1
        fake.type(b"dd", at=0.8)
        shown = []
        fake.at(2.1, lambda: shown.extend(fake.vt.lines()[:24]))
        with self.assertLogs(level="DEBUG") as logs:
            play(fake, current, term, 2.2)
        stats = shown[23][10:70]
        self.assertRegex(stats, r"^FPS (59|60|61)\.\d  RTT \dms  W \d\.\d  B/f \d+  K03  latched k1 256 *$")
        logged = [line.split("Stats: ")[1] for line in logs.output if "Stats: " in line]
        self.assertEqual(len(logged), 2)  # once a second
        self.assertRegex(logged[-1], r"^FPS [\d.]+  RTT \dms  W \d\.\d  B/f \d+  K03  latched k1 256$")

    def test_the_linux_console_f_keys_are_f_keys_and_not_shifted_letters(self):
        fake, current, term = game("linux-console")
        fake.type(b"\x1b[[A", at=0.5)  # F1, as the Linux console sends it (ESC [ [ A..E for F1-F5)
        fake.type(b"\x1b[[B\x1b[[D", at=2.5)  # F2 and F4, once the wave plays: their letters bomb and move
        seen = []
        fake.at(2.4, lambda: seen.append((current.pilot.game.phase, current.pilot.game.player.x)))
        fake.at(3.0, lambda: seen.append((fake.vt.lines()[23].strip()[:4], current.pilot.game.player.x)))
        play(fake, current, term, 3.1)
        self.assertEqual(seen[0][0], "playing")
        self.assertEqual(seen[1], ("FPS ", seen[0][1]))
        self.assertEqual(current.pilot.game.player.bombs, 1)

    def test_lost_acks_mean_clock_pacing_until_the_terminal_answers_again(self):
        fake, current, term = game("ghostty")
        answering = fake.responder.answer
        fake.at(0.5, lambda: setattr(fake.responder, "answer", lambda *args: b""))  # acks stop coming
        fake.at(4.0, lambda: setattr(fake.responder, "answer", answering))
        seen = []
        fake.at(3.9, lambda: seen.append((term.pacer.disabled, renderer_frames())))
        fake.at(4.5, lambda: seen.append((term.pacer.disabled, renderer_frames())))

        def renderer_frames():
            return sum("Frame " in line for line in logs.output)

        with self.assertLogs(level="DEBUG") as logs:
            play(fake, current, term, 5.0)
        (lost, at_lost), (back, at_back) = seen
        self.assertEqual((lost, back), (True, False))
        self.assertGreater(at_back - at_lost, 25)  # frames kept coming at 60 a second while acks were lost
        notes = [line for line in logs.output if "Acks " in line]
        self.assertEqual(
            [note.split("Acks ")[1] for note in notes], ["lost: clock pacing", "answered again: ack pacing"]
        )

    def test_every_frame_is_logged_with_the_hash_of_its_text(self):
        fake, current, term = game()
        with self.assertLogs(level="DEBUG") as logs:
            renderer, _ = play(fake, current, term, 1.2)
        frames = [line for line in logs.output if "Frame " in line]
        self.assertEqual(len(frames), renderer.frames)
        screen = [line[:80] for line in fake.screen[:24]]
        self.assertTrue(frames[-1].endswith(app.text_hash(screen)))
        (stats,) = [line for line in logs.output if "Loop " in line]
        self.assertRegex(stats, r"Loop 1\.00\ds: 60 ticks (60|61) frames \d+ bytes, longest write 0\.0ms, srtt 0\.001")


class RendererTests(unittest.TestCase):
    def renderer(self, fixture="ghostty", **settings):
        return app.Renderer(options(**settings), capabilities(load(fixture)), (80, 24))

    def test_due_while_playing_every_k_ticks_otherwise_after_a_change(self):
        renderer = self.renderer(fps=20)
        self.assertEqual(renderer.k, 3)
        self.assertTrue(renderer.due(True))  # nothing drawn yet
        renderer.frame(session(), 0.0, True, OFF)
        renderer.ticks_pending = 2
        self.assertFalse(renderer.due(True))
        renderer.ticks_pending = 3
        self.assertTrue(renderer.due(True))
        renderer.ticks_pending, renderer.dirty = 0, False
        self.assertFalse(renderer.due(False))
        renderer.dirty = True
        self.assertTrue(renderer.due(False))
        renderer.ticks_pending, renderer.dirty = 1, False  # the tick that ended play
        self.assertTrue(renderer.due(False))

    def test_an_unchanged_frame_is_sync_and_ack_only(self):
        renderer, current = self.renderer(), session()
        current.scene = "menu"
        self.assertTrue(renderer.frame(current, 0.0, False, OFF))
        self.assertEqual(renderer.frame(current, 0.1, False, OFF), b"\x1b[?2026h\x1b[?2026l\x1b[4;18H\x1b[6n")  # tag 2
        dsr = app.Renderer(options(), replace(capabilities(load("ghostty")), ack="dsr"), (80, 24))
        dsr.frame(current, 0.0, False, OFF)
        self.assertEqual(dsr.frame(current, 0.1, False, OFF), b"\x1b[?2026h\x1b[?2026l\x1b[5n")
        renderer = self.renderer("silent")
        renderer.frame(current, 0.0, False, OFF)
        self.assertEqual(renderer.frame(current, 0.1, False, OFF), b"")

    def test_stress_pads_frames_with_cursor_homes(self):
        renderer = app.Renderer(options(), capabilities(load("silent")), (80, 24), stress=300)
        current = session()
        current.scene = "menu"
        renderer.frame(current, 0.0, False, OFF)
        self.assertEqual(renderer.frame(current, 0.1, False, OFF), b"\x1b[H" * 100)

    def test_fps_is_measured_over_a_second_of_play(self):
        renderer, current = self.renderer(), session()
        for n in range(61):
            renderer.frame(current, n / 60, True, OFF)
        self.assertAlmostEqual(renderer.fps, 60.0, delta=1.5)
        renderer.frame(current, 1.1, False, OFF)
        self.assertEqual(renderer.fps, 0.0)

    def test_the_stats_line(self):
        renderer, current = self.renderer(), session()
        current.routed = 117
        pacer = AckPacer("adaptive")
        self.assertEqual(renderer.stats(current, pacer), "FPS 0.0  RTT -  W 2.0  B/f 0  K17  latched k1 truecolor")
        for n in range(121):
            renderer.frame(current, n / 60, True, pacer)
        pacer.srtt, pacer.window = 0.0424, 3.43
        fps, bpf = renderer.fps, renderer.bpf
        self.assertAlmostEqual(fps, 60.0, delta=1.5)
        self.assertEqual(bpf, 27)  # the mean packet of the last second: nothing moved, so sync and the tagged ack
        line = renderer.stats(current, pacer)
        self.assertEqual(line, f"FPS {fps:.1f}  RTT 42ms  W 3.4  B/f {bpf}  K17  latched k1 truecolor")
        fixed, off, disabled = AckPacer("fixed"), AckPacer("off"), AckPacer("adaptive")
        disabled.disabled = True
        self.assertIn("  W fix  ", renderer.stats(current, fixed))
        for pacer in (off, disabled):
            self.assertIn("RTT -  ACK OFF  B/f", renderer.stats(current, pacer))
        current.routed = 100
        self.assertIn("  K00  ", renderer.stats(current, off))
        mono = app.Renderer(options(), replace(capabilities(load("ghostty")), color="mono"), (80, 24))
        self.assertTrue(mono.stats(current, off).endswith("latched k1 mono"))  # the tier the game draws in

    def test_with_show_fps_each_frame_shows_the_stats_line(self):
        current = session(show_fps=True)
        renderer = app.Renderer(current.options, capabilities(load("ghostty")), (80, 24))
        renderer.frame(current, 0.0, True, OFF)
        self.assertEqual(renderer.buffer.row_text(23)[10:].rstrip(), renderer.stats(current, OFF))  # block at 10
        current.options.show_fps = False  # F1
        current.pilot.game.notes.append(("LAST STAND", 180))  # the bottom row's notices are back
        renderer.frame(current, 0.1, True, OFF)
        self.assertTrue(renderer.buffer.row_text(23)[10:].startswith("LAST STAND"))

    def test_replies_free_frame_slots_and_late_ones_are_logged(self):
        renderer = self.renderer()
        pacer = AckPacer("fixed", tagged=True, clock=lambda: 0.0)
        pacer.sent(0.0, 100, full=False, tag=(4, 2))
        renderer.on_reply(Reply("focus", True, 0.01), pacer)
        self.assertEqual(len(pacer.inflight), 1)
        renderer.on_reply(Reply("cpr", (4, 2), 0.02), pacer)
        self.assertEqual(len(pacer.inflight), 0)
        with self.assertLogs(level="DEBUG") as logs:
            renderer.on_reply(Reply("dsr", 0, 0.03), pacer)  # not this terminal's ack
            renderer.on_reply(Reply("unknown", b"\x1b[?2;2R", 0.04), pacer)
        self.assertEqual(["Late or unknown reply" in line for line in logs.output], [True, True])

    def test_frames_sent_while_clock_pacing_carry_the_first_tag_and_the_sequence_survives_a_restart(self):
        """While the pacer is disabled every frame carries the first tag, which no tracked frame is given; the
        tracked ones then go on with the next tag in the sequence, after a resume too (a new pacer, the same
        renderer)."""
        renderer, current = self.renderer(), session()
        current.scene = "menu"
        pacer = AckPacer("adaptive", tagged=True, clock=lambda: 0.0)
        renderer.frame(current, 0.0, False, pacer)
        self.assertEqual(renderer.last_tag, ack_tag(0, 80, 24))
        pacer.disabled = True
        data = renderer.frame(current, 0.1, False, pacer)
        self.assertEqual((renderer.last_tag, data[-10:]), ((4, 1), b"\x1b[4;1H\x1b[6n"))
        pacer.disabled = False
        renderer.frame(current, 0.2, False, pacer)
        self.assertEqual(renderer.last_tag, ack_tag(1, 80, 24))
        renderer.reconfigure(renderer.caps)
        renderer.frame(current, 0.3, False, AckPacer("adaptive", tagged=True))
        self.assertEqual(renderer.last_tag, ack_tag(2, 80, 24))

    def test_a_new_frame_takes_no_tag_a_reply_still_to_come_may_name(self):
        """L1 of the S15 review: the next tag in sequence is skipped while a frame in flight, given up on or dropped
        by resized() may still be answered at it (AckPacer.reserved()). A screen whose every tag is so taken gets a
        frame with no ack query."""
        renderer, current = self.renderer(), session()
        current.scene = "menu"
        pacer = AckPacer("adaptive", tagged=True, clock=lambda: 0.0)
        pacer.sent(0.0, 100, full=False, tag=ack_tag(0, 80, 24))
        pacer.sent(0.01, 100, full=False, tag=ack_tag(1, 80, 24))
        renderer.frame(current, 0.0, False, pacer)
        self.assertEqual((renderer.last_tag, renderer.tags), (ack_tag(2, 80, 24), 3))
        small = app.Renderer(options(), capabilities(load("ghostty")), (80, 5))
        pacer = AckPacer("adaptive", tagged=True, clock=lambda: 0.0)
        for seq in range(tag_count(80, 5)):
            pacer.sent(seq * 0.001, 100, full=False, tag=ack_tag(seq, 80, 5))
        data = small.frame(current, 0.0, False, pacer)
        self.assertEqual((small.last_tag, b"\x1b[6n" in data), (None, False))

    def test_the_last_free_tag_in_sequence_is_found(self):
        """Every tag the sequence comes to is tried before a frame goes without an ack query."""
        renderer, current = app.Renderer(options(), capabilities(load("ghostty")), (80, 5)), session()
        current.scene = "menu"
        pacer = AckPacer("adaptive", tagged=True, clock=lambda: 0.0)
        count = tag_count(80, 5)
        for seq in range(count - 2):
            pacer.sent(seq * 0.001, 100, full=False, tag=ack_tag(seq, 80, 5))
        renderer.frame(current, 0.0, False, pacer)
        self.assertEqual(renderer.last_tag, ack_tag(count - 2, 80, 5))

    def test_a_screen_under_five_rows_gets_no_ack_query(self):
        """A reply there would be a clamped one (row 4, the last row), land on the probe's rows (2 and 3) or read as F3
        (row 1)."""
        current = session()
        current.scene = "menu"
        for rows in (4, 3, 1):
            renderer = app.Renderer(options(), capabilities(load("ghostty")), (80, rows))
            data = renderer.frame(current, 0.0, False, AckPacer("adaptive", tagged=True))
            self.assertEqual((renderer.last_tag, b"\x1b[6n" in data), (None, False))

    def test_each_fixture_is_drawn_in_its_expected_tier_and_background(self):
        """expect.color and expect.background are what the game draws in: its theme, and the SGR of its frames."""
        for name in names():
            with self.subTest(name):
                expect = load(name)["expect"]
                renderer = app.Renderer(options(), capabilities(load(name)), (240, 70))
                theme = renderer.theme
                self.assertEqual((theme.tier, theme.name), (expect["color"], expect["background"]))
                self.assertEqual(theme.paint_background, expect["color"] in ("truecolor", "256"))
                data = renderer.frame(session(), 0.0, True, OFF)  # the first, full frame: letterbox, HUD and pixels
                self.assertEqual(colour_forms(data), FORMS[expect["color"]])
        self.assertEqual(
            app.overrides(options(color="mono", theme="light")), app.Overrides(color="mono", theme="light")
        )

    def test_text_is_sanitised_as_the_terminal_shows_it(self):
        caps = capabilities(load("ghostty"))
        self.assertEqual(app.text_sanitizer(caps)("a±─"), "a±─")
        self.assertEqual(app.text_sanitizer(replace(caps, ambiguous_wide=True))("a±b"), "a?b")
        self.assertEqual(app.text_sanitizer(replace(caps, utf8=False))("a±b"), "a?b")


class HookTests(unittest.TestCase):
    def test_suspend_pauses_and_resume_repaints_with_the_new_capabilities(self):
        current = session()
        renderer = app.Renderer(current.options, capabilities(load("ghostty")), (80, 24))
        renderer.frame(current, 0.0, True, OFF)
        decoder = Decoder(esc_timeout=0.05, string_timeout=0.5)
        hooks = app.Hooks(current, renderer, decoder)
        hooks.configure(renderer.caps)
        self.assertEqual((current.controls.scheme, current.router.gate.certain, decoder.kitty), ("hold", True, True))
        hooks.before_suspend()
        self.assertEqual((current.scene, current.pause_reason), ("paused", "suspended"))
        current.change_scene("playing")  # a SIGCONT nobody asked for can find the game playing
        remote = capabilities(load("ssh-apple-terminal"))
        hooks.after_resume(remote, "resumed")
        self.assertEqual((current.scene, current.pause_reason), ("paused", "resumed"))
        self.assertIs(renderer.caps, remote)
        self.assertIsNone(renderer.prev)
        self.assertEqual((decoder.esc_timeout, decoder.string_timeout), (remote.esc_timeout, remote.string_timeout))
        gate = current.router.gate
        self.assertEqual(
            (current.controls.scheme, gate.certain, gate.repeat.remote, decoder.kitty), ("latched", False, True, False)
        )
        hooks.after_resume(capabilities(load("ssh-iterm2")), "reprobe")  # a fresh probe keeps the pause and reason
        self.assertEqual((current.scene, current.pause_reason), ("paused", "resumed"))
        self.assertEqual((current.controls.scheme, current.router.gate, gate.certain), ("hold", gate, True))


def entered(fixture="ghostty", environ=None, **settings):
    """A Session and a TerminalSession on a FakeTerminal as app.run makes them (with the Session's keymap), not yet
    entered; environ is added to the fixture's environment."""
    doc = load(fixture)
    fake = FakeTerminal(doc, cols=80, rows=24, rtt=0.001, vt=(200, 80))
    current = session(**settings)
    env = EnvHints.from_env({**doc["env"], **(environ or {})})
    term = TerminalSession(fake.fd, env, app.overrides(current.options), current.keymap, ops=fake.ops())
    return fake, current, term


def wired(current, term):
    """What app.play builds on an entered TerminalSession, short of the loop: (renderer, decoder)."""
    renderer = app.Renderer(current.options, term.caps, term.size())
    decoder = Decoder(esc_timeout=term.caps.esc_timeout, string_timeout=term.caps.string_timeout)
    term.hooks = current.hooks = app.Hooks(current, renderer, decoder, term)
    term.hooks.configure(term.caps)
    return renderer, decoder


class SettingsHookTests(unittest.TestCase):
    """Phase 5 spec 4.5: Hooks.apply(name) after the settings card changes the Session's options."""

    def test_palette_and_background_apply_at_once_and_the_terminal_session_keeps_them(self):
        fake, current, term = entered(environ={"NO_COLOR": "1"})
        natural = load("ghostty")["expect"]["color"]
        with term:
            renderer, _ = wired(current, term)
            self.assertEqual((term.caps.color, term.caps.natural_color), ("mono", natural))  # NO_COLOR, untouched
            renderer.frame(current, 0.0, True, OFF)
            current.options.palette = "cvd"
            current.hooks.apply("palette")
            self.assertEqual((term.overrides.palette, term.overrides.palette_chosen), ("cvd", True))
            self.assertEqual(term.caps.color, natural)  # a chosen palette beats NO_COLOR (spec 3.2)
            self.assertIs(renderer.caps, term.caps)
            self.assertEqual((renderer.theme.palette, renderer.theme.tier), ("cvd", natural))
            self.assertEqual((renderer.prev, renderer.dirty), (None, True))  # one full repaint
            current.options.theme = "light"
            current.hooks.apply("theme")
            self.assertEqual((term.overrides.theme, renderer.theme.name), ("light", "light"))
            for reason in ("reprobe", "resumed"):  # a re-probe, or a SIGCONT: the probe runs again
                term.restart(reason)
                kept = (term.caps.color, renderer.theme.palette, renderer.theme.name)
                self.assertEqual(kept, (natural, "cvd", "light"), reason)
            term.hooks.before_suspend()  # Ctrl-Z, then fg
            term.reenter("suspended")
            kept = (term.caps.color, renderer.theme.palette, renderer.theme.name)
            self.assertEqual(kept, (natural, "cvd", "light"))
            self.assertIs(renderer.caps, term.caps)
            current.options.palette = "default"  # DEFAULT is not saved, so not a choice: NO_COLOR again
            current.hooks.apply("palette")
            self.assertEqual((term.caps.color, renderer.theme.palette), ("mono", "default"))
            current.options.palette = "mono"
            current.hooks.apply("palette")
            self.assertEqual((term.caps.color, renderer.theme.tier), ("mono", "mono"))

    def test_keys_go_to_the_session_its_router_and_the_terminal_session_and_physical_keys_ask_for_a_reprobe(self):
        fake, current, term = entered("kitty")
        fake.responder.push = b"\x1b[?15u\x1b[?62;c"  # this kitty grants flag 4 when asked
        with term:
            renderer, decoder = wired(current, term)
            self.assertEqual(fake.output.count(b"\x1b[>11u"), 1)  # the default keys need no flag 4
            self.assertEqual(fake.output.count(b"\x1b[>15u"), 0)
            current.options.keys = "vim"
            current.hooks.apply("keys")
            self.assertEqual(current.keymap.contexts, Keymap.preset("vim").contexts)
            for holder in (current.router, current.options, term):
                self.assertIs(holder.keymap, current.keymap)
            self.assertFalse(term.reprobe)  # vim binds letters, as the default does
            current.options.keys = "wasd-physical"
            current.hooks.apply("keys")
            self.assertTrue(term.reprobe)  # flag 4 must be pushed: the existing re-probe does it
            term.restart("reprobe")
            self.assertEqual(fake.output.count(b"\x1b[>15u"), 1)  # pushed again from term.keymap, flag 4 included
            self.assertTrue(term.caps.kitty_flags & 4)
            self.assertIs(term.keymap, current.keymap)
            self.assertEqual(current.keymap.contexts, Keymap.preset("wasd-physical").contexts)  # kept on resume
            term.reprobe = False
            current.options.keys = "default"
            current.hooks.apply("keys")
            self.assertTrue(term.reprobe)  # and back without flag 4

    def test_the_settings_card_allows_the_reprobe_keys_asks_for(self):
        """The settings card, a pause and (ST-2) the title menu, which the card closes onto, are idle: the re-probe
        runs there; play waits for one of them."""
        real = TerminalSession.restart
        for scene, expected in (("settings", ["reprobe"]), ("menu", ["reprobe"]), ("paused", ["reprobe"])):
            with self.subTest(scene=scene):
                fake, current, term = game(quickstart=scene != "menu")  # (the stub Painter draws no SETTINGS card)
                current.scene = scene
                fake.at(0.3, lambda term=term: setattr(term, "reprobe", True))
                with patch.object(TerminalSession, "restart", autospec=True, side_effect=real) as restart:
                    play(fake, current, term, 1.0)
                self.assertEqual([call.args[1] for call in restart.call_args_list], expected)

    def keys_row(self, fixture, **settings):
        """A Session on the settings card's KEYS row, entered on the fixture (kitty: one that grants flag 4)."""
        from terminal_invaders.settings import SETTINGS

        fake, current, term = entered(fixture, **settings)
        if fixture == "kitty":
            fake.responder.push = b"\x1b[?15u\x1b[?62;c"
        current.open_settings()
        current.settings_card.cursor = [row.key for row in SETTINGS].index("keys")
        return fake, current, term

    def restarts(self, fake, current, term, until):
        """The loop played to `until`: the re-probes it ran, each (virtual time, reason)."""
        real, seen = TerminalSession.restart, []

        def spy(term, reason):
            seen.append((round(fake.now, 3), reason))
            return real(term, reason)

        with patch.object(TerminalSession, "restart", spy):
            play(fake, current, term, until)
        return seen

    def test_a_held_key_on_the_keys_row_never_reprobes_at_the_key_rate(self):
        """ST-1: Right held on KEYS for a second (31 repeats, ending on WASD) re-probes nowhere on a terminal
        without kitty flags, where it could change nothing, and on kitty once, 0.5 s after the last repeat, as
        PALETTE applies (spec 2.9), instead of at every change to or from WASD."""
        for fixture, expected in (("xterm", 0), ("kitty", 1)):
            with self.subTest(fixture=fixture):
                fake, current, term = self.keys_row(fixture)
                for i in range(31):  # DEFAULT -> ARROWS -> VIM -> WASD -> DEFAULT ...: 31 ends on WASD
                    fake.type(b"\x1b[C", at=0.5 + i / 30)
                seen = self.restarts(fake, current, term, 2.5)
                self.assertEqual((current.options.keys, current.keymap.has_physical), ("wasd-physical", True))
                self.assertEqual(len(seen), expected, seen)
                if seen:
                    self.assertGreaterEqual(seen[0][0], 1.5 + screens.STILL - 0.01)  # after the last repeat at 1.5 s
                    self.assertEqual(fake.output.count(b"\x1b[>15u"), 1)  # flag 4 pushed

    def test_a_keys_change_closed_with_esc_reprobes_in_the_menu(self):
        """ST-2: KEYS to WASD and Esc in one read (kitty over SSH coalesces them): the card applies the change as it
        closes onto the title menu, and the menu runs the re-probe at once, so the first run has flag 4."""
        fake, current, term = self.keys_row("kitty", keys="vim")
        fake.type(b"\x1b[C", at=0.5)  # Right: VIM -> WASD
        fake.type(b"\x1b[27u", at=0.5)  # Esc (kitty's form): save and go back
        seen = self.restarts(fake, current, term, 2.0)
        self.assertEqual((current.scene, current.options.keys, term.reprobe), ("menu", "wasd-physical", False))
        self.assertEqual(seen, [(0.5, "reprobe")])
        self.assertEqual(fake.output.count(b"\x1b[>15u"), 1)
        self.assertTrue(term.caps.kitty_flags & 4)

    def test_a_keymap_file_holds_the_keys_row(self):
        current = session()
        current.options.keymap_file = "keys.json"
        renderer = app.Renderer(current.options, capabilities(load("ghostty")), (80, 24))
        hooks = app.Hooks(current, renderer, Decoder(esc_timeout=0.05, string_timeout=0.5))
        keymap = current.keymap
        current.options.keys = "vim"
        hooks.apply("keys")
        self.assertIs(current.keymap, keymap)

    def test_every_other_setting(self):
        current = session()
        renderer = app.Renderer(current.options, capabilities(load("ghostty")), (240, 70))
        hooks = app.Hooks(current, renderer, Decoder(esc_timeout=0.05, string_timeout=0.5))  # no term: tests, tools
        hooks.configure(renderer.caps)
        renderer.frame(current, 0.0, True, OFF)
        self.assertGreater(renderer.painter.layout.k, 1)
        current.options.scale = "1"
        hooks.apply("scale")
        self.assertEqual((renderer.painter.layout.k, renderer.prev), (1, None))
        for fps, k in ((30, 2), (20, 3), (15, 4), (60, 1)):
            current.options.fps = fps
            hooks.apply("fps")
            self.assertEqual(renderer.k, k)
        self.assertEqual(current.controls.scheme, "hold")
        current.options.controls = "timed"
        hooks.apply("controls")
        self.assertEqual(current.controls.scheme, "timed")
        current.options.no_sound = False  # SOUND ON, with the sound off: on, as M does
        hooks.apply("sound")
        self.assertTrue(current.audio.enabled)
        hooks.apply("sound")
        self.assertTrue(current.audio.enabled)  # already as the setting says
        current.options.no_sound = True
        hooks.apply("sound")
        self.assertFalse(current.audio.enabled)
        painter = renderer.painter
        for name in ("effects", "reduced_motion", "tips", "attract", "name"):
            renderer.dirty = False
            hooks.apply(name)
            self.assertTrue(renderer.dirty, name)  # read from the options when drawn: a frame, nothing rebuilt
            self.assertIs(renderer.painter, painter, name)


class RendererPhase5Tests(unittest.TestCase):
    def test_each_frame_tells_the_painter_its_link(self):
        current = session()
        local = app.Renderer(current.options, capabilities(load("ghostty")), (80, 24))
        local.frame(current, 0.0, True, OFF)
        self.assertEqual(local.painter.link, fx.Link(remote=False, congested=False, clock=False))
        pacer = AckPacer("adaptive", tagged=True, clock=lambda: 0.0)
        pacer.hold_until = 1.0
        local.frame(current, 0.1, True, pacer)
        self.assertEqual(local.painter.link, fx.Link(congested=True))
        pacer.hold_until, pacer.disabled = 0.0, True
        local.frame(current, 0.2, True, pacer)
        self.assertEqual(local.painter.link, fx.Link(clock=True))
        remote = app.Renderer(current.options, capabilities(load("ssh-apple-terminal")), (80, 24))
        remote.frame(current, 0.0, True, OFF)
        self.assertEqual(remote.painter.link, fx.Link(remote=True))
        silent = app.Renderer(current.options, capabilities(load("silent")), (80, 24))
        silent.frame(current, 0.0, True, None)
        self.assertEqual(silent.painter.link, fx.Link())
        # FX-2: no acks at all is clock pacing too (spec 2.8: no kick), as the stats line's ACK OFF says: a terminal
        # that answers no ack query, and --pacing off (resolve() then asks for none)
        silent.frame(current, 0.1, True, AckPacer("off"))
        self.assertEqual(silent.painter.link, fx.Link(clock=True))
        unpaced = app.Renderer(current.options, capabilities(load("ghostty"), app.Overrides(pacing="off")), (80, 24))
        unpaced.frame(current, 0.0, True, AckPacer("off"))
        self.assertEqual((unpaced.caps.ack, unpaced.painter.link), (None, fx.Link(clock=True)))
        stand_in = SimpleNamespace(srtt=None, mode="off", disabled=False, reserved=set)  # as test_arena.py's
        local.frame(current, 0.3, True, stand_in)
        self.assertEqual(local.painter.link, fx.Link())

    def test_the_flash_limiter_survives_a_reconfigure(self):
        current = session()
        renderer = app.Renderer(current.options, capabilities(load("ghostty")), (80, 24))
        limiter = renderer.limiter
        self.assertIsInstance(limiter, fx.FlashLimiter)
        self.assertIs(renderer.painter.limiter, limiter)
        self.assertTrue(limiter.admit("kick", 10))
        renderer.reconfigure(capabilities(load("xterm")))
        renderer.relayout((100, 30))
        current.options.palette = "contrast"
        renderer.reconfigure(renderer.caps)
        self.assertIs(renderer.limiter, limiter)
        self.assertIs(renderer.painter.limiter, limiter)
        self.assertEqual(limiter.admitted, {"kick": 10})

    def test_arcade_frames_are_drawn_with_the_arcade_theme(self):
        """Phase 5 spec 3.1: an Arcade game keeps its 1978 colours under every palette but mono; moving between its
        frames and the cards builds the Painter and Encoder again."""
        current = session(mode="arcade", palette="cvd")
        renderer = app.Renderer(current.options, capabilities(load("ghostty")), (80, 24))
        self.assertEqual((renderer.theme.palette, renderer.arcade_theme.palette), ("cvd", "default"))
        self.assertEqual(renderer.arcade_theme.tier, renderer.theme.tier)
        renderer.frame(current, 0.0, True, OFF)
        self.assertIs(renderer.painter.theme, renderer.arcade_theme)
        painter, encoder = renderer.painter, renderer.encoder
        renderer.frame(current, 0.1, True, OFF)
        self.assertIs(renderer.painter, painter)  # no rebuild while it stays an Arcade frame
        self.assertIs(renderer.encoder, encoder)
        current.scene = "menu"
        renderer.frame(current, 0.2, False, OFF)
        self.assertIs(renderer.painter.theme, renderer.theme)
        self.assertIsNot(renderer.encoder, encoder)  # its pens are the theme's
        self.assertTrue(renderer.last_full)  # every cell painted again in the palette's colours
        self.assertIs(renderer.painter.limiter, renderer.limiter)
        mono = app.Renderer(options(mode="arcade", palette="mono"), capabilities(load("ghostty")), (80, 24))
        self.assertEqual((mono.theme, mono.arcade_theme.palette), (mono.arcade_theme, "mono"))
        default = app.Renderer(options(mode="arcade"), capabilities(load("ghostty")), (80, 24))
        self.assertEqual(default.theme, default.arcade_theme)
        campaign = session(palette="cvd")
        renderer = app.Renderer(campaign.options, capabilities(load("ghostty")), (80, 24))
        renderer.frame(campaign, 0.0, True, OFF)
        self.assertIs(renderer.painter.theme, renderer.theme)

    def test_may_flash_is_unchanged(self):
        renderer = app.Renderer(options(), capabilities(load("ghostty")), (80, 24))
        self.assertEqual(app.FLASH_LIMIT, 3)
        self.assertEqual([renderer.may_flash(t / 10) for t in range(10)], [True] * 3 + [False] * 7)
        self.assertTrue(renderer.may_flash(1.0))
        self.assertFalse(renderer.may_flash(1.05))
        self.assertEqual(renderer.limiter.admitted, {})  # the kick limiter is another thing, on game ticks

    def test_frame_interval(self):
        self.assertEqual([app.frame_interval(fps) for fps in (60, 30, 20, 15, 120, 45)], [1, 2, 3, 4, 1, 1])


class AttractLoopTests(unittest.TestCase):
    """Phase 5 spec 5.3: the loop ticks and draws session.attract, the demo's nested replay Session, in the menu's
    place; the INVADERS_FAULT check reads the shown session's game."""

    def demo(self, current):
        playback = tapes.load_tape("campaign.json")
        return Session(copy.copy(current.options), ReadOnlyProfile(current.profile), SilentAudio(), replay=playback)

    def test_the_loop_ticks_and_draws_the_demo_in_the_menus_place(self):
        fake, current, term = game(quickstart=False)
        self.assertIsNone(current.pilot)  # a first-launch menu: no pilot to tick
        demo = current.attract = self.demo(current)
        fake.at(0.5, lambda: setattr(current, "attract", None))  # the demo ends
        drawn = []
        with watch_frames(lambda renderer: drawn.append(fake.now)):
            with patch.object(Painter, "draw", autospec=True, side_effect=DRAW) as draw:
                play(fake, current, term, 1.0)
        shown = [call.args[1] for call in draw.call_args_list]
        self.assertGreater(len(shown), 20)
        before = [who for who, at in zip(shown, drawn, strict=True) if at < 0.5]
        after = [who for who, at in zip(shown, drawn, strict=True) if at > 0.5]
        self.assertTrue(before and all(who is demo for who in before))
        self.assertTrue(after and all(who is current for who in after))
        self.assertIn(demo.pilot.game.tick, range(25, 35))  # half a second of play
        self.assertEqual((current.scene, current.pilot, demo.profile.writes), ("menu", None, 0))
        self.assertEqual(demo.pilot.game.digest(), self.replayed(demo.pilot.game.tick))

    def replayed(self, ticks):
        (tape,) = tapes.load_tape("campaign.json").tapes
        game = CampaignGame(tape.config)
        for tick in range(ticks):
            game.step(tape.actions_at(tick))
        return game.digest()

    def test_the_fault_check_reads_the_shown_game(self):
        fake, current, term = game(quickstart=False, debug=True)
        current.attract = self.demo(current)
        with self.assertRaisesRegex(RuntimeError, "crash at tick 12"):
            play(fake, current, term, 1.0, crash_at=12)

    def test_the_loop_runs_the_demos_cycle_on_its_clock(self):
        """Session.update_attract(now), when the Session has one, runs each iteration after the countdown; True asks
        for a frame."""
        fake, current, term = game(quickstart=False)
        times = []

        def update_attract(now):
            times.append(now)
            return len(times) == 5

        current.update_attract = update_attract
        with patch.object(Painter, "draw", autospec=True, side_effect=DRAW) as draw:
            play(fake, current, term, 1.0)
        self.assertGreater(len(times), 5)
        self.assertEqual(times, sorted(times))
        self.assertEqual(draw.call_count, 2)  # the first image, and the one update_attract asked for

    def test_play_hands_the_session_its_hooks_and_enables_the_tips(self):
        fake, current, term = game()
        with patch.object(Session, "enable_tips", autospec=True) as enable:
            play(fake, current, term, 0.2)
        enable.assert_called_once_with(current)
        self.assertIsInstance(current.hooks, app.Hooks)
        self.assertIs(term.hooks, current.hooks)
        self.assertIs(current.hooks.term, term)


class FaultTests(unittest.TestCase):
    def test_faults_need_debug_and_a_valid_value(self):
        debug = SimpleNamespace(debug=True)
        for fault, expected in (("raise:120", (120, 0)), ("stress:3000", (None, 3000)), ("raise", (None, 0))):
            with self.subTest(fault=fault), patch.dict(os.environ, {"INVADERS_FAULT": fault}):
                self.assertEqual(app.faults(debug), expected)
                self.assertEqual(app.faults(SimpleNamespace(debug=False)), (None, 0))
        for fault in ("boom:1", "stress:x", ""):
            with patch.dict(os.environ, {"INVADERS_FAULT": fault}):
                self.assertEqual(app.faults(debug), (None, 0))


@pytest.mark.gate  # G3.2
class HeadlessTests(unittest.TestCase):
    def test_a_whole_session_never_imports_curses(self):
        done = subprocess.run([sys.executable, "-c", HEADLESS], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "[]")


class PtyTests(unittest.TestCase):
    """run() and --probe in this process, on a real pty with the terminal stub answering as the fixture."""

    def setUp(self):
        self.master, self.slave = os.openpty()
        self.stub = Stub(self.master, load("ghostty"))
        self.stub.start()
        self.opened = patch.object(app, "open_tty", lambda: os.open(os.ttyname(self.slave), os.O_RDWR | os.O_NOCTTY))
        self.opened.start()
        self.env = patch.dict(os.environ, load("ghostty")["env"], clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.opened.stop()
        self.stub.stop()
        os.close(self.master)
        os.close(self.slave)

    def test_run_plays_quits_and_restores(self):
        before = termios.tcgetattr(self.slave)
        current = session()
        audio = Mock()
        doc = load("ghostty")  # kitty keys, as the stub reports after the game's push
        script = [(0.5, "p"), (0.7, "q"), (0.9, "escape"), (1.1, "escape")]  # pause, menu, QUIT, quit
        timers = [threading.Timer(at, lambda name=name: os.write(self.master, taps(doc, name))) for at, name in script]
        for timer in timers:
            timer.start()
        with patch.object(app, "Session", return_value=current), patch.object(current, "save_replay") as save:
            app.run(current.options, current.profile, audio)
        timers[-1].join()
        self.assertTrue(current.quit)
        save.assert_called_with()  # leaving play saves the partial run, and run() saves on its way out
        audio.close.assert_called_once_with()
        self.assertEqual(termios.tcgetattr(self.slave), before)
        self.assertIsNone(self.stub.error)
        self.assertFalse(self.stub.vt.alt_screen)
        self.assertIn(TITLE, bytes(self.stub.output))

    def probe(self, **settings) -> str:
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.assertEqual(app.run_probe_command(options(**settings)), 0)
        self.assertFalse(self.stub.vt.kitty_depth)
        return out.getvalue()

    def test_probe_prints_what_the_terminal_supports(self):
        text = self.probe()
        self.assertTrue(text.startswith("ghostty 1.2.0  local  truecolor dark  sync  keys kitty 11 release"), text)
        self.assertIn("family           ghostty", text)
        self.assertIn("kitty_flags      11", text)  # pushed and verified, as the game does
        self.assertIn("answered         cpr_echo cpr_width da1", text)
        self.assertIn(b"\x1b[>11u", bytes(self.stub.output))
        self.assertFalse(self.stub.vt.kitty_depth)

    def test_probe_json_prints_every_capability(self):
        fields = json.loads(self.probe(json=True, theme="light"))
        self.assertEqual(list(fields), [item.name for item in dataclasses.fields(Capabilities)])
        self.assertEqual(fields["answered"], sorted(capabilities(load("ghostty")).answered))
        self.assertEqual((fields["color"], fields["background"], fields["theme"]), ("truecolor", "dark", "light"))
        self.assertEqual((fields["kitty_flags"], fields["ambiguous_wide"], fields["ack"]), (11, False, "cpr"))

    def test_probe_raw_captures_a_fixture_that_resolves_to_its_own_expectation(self):
        extra = {"SSH_TTY": "/dev/pts/7", "USER": "alice", "HOSTNAME": "alice-laptop", "TMUX": "/tmp/tmux-1/default,1"}
        with patch.dict(os.environ, extra):  # values that must never be recorded, and presence-only ones
            doc = json.loads(self.probe(raw=True, color="mono", no_kitty=True))  # the defaults, whatever the flags
        expected = load("ghostty")
        self.assertEqual(set(doc), set(expected))
        self.assertEqual(doc["env"], expected["env"] | {"SSH_TTY": "1", "TMUX": "1"})
        self.assertNotIn("alice", json.dumps(doc))
        self.assertEqual((doc["replies"], doc["push_reply"]), (expected["replies"], expected["push_reply"]))
        self.assertRegex(doc["provenance"], r"^captured: ghostty 1\.2\.0, \w+ [\w.-]+, invaders --probe --raw, \d{4}-")
        self.assertEqual((doc["family"], doc["echo"], doc["keys"]["protocol"]), ("ghostty", False, "kitty"))
        self.assertGreater(doc["keys"]["os_repeat"]["delay"], doc["keys"]["os_repeat"]["interval"])
        caps = capabilities(doc)  # the capture replayed as the tests replay every fixture
        scheme = make_controls(caps).scheme
        self.assertEqual(
            {key: scheme if key == "scheme" else getattr(caps, key) for key in doc["expect"]}, doc["expect"]
        )
        self.assertEqual(list(doc["expect"]), list(expected["expect"]))
        self.assertEqual(doc["expect"], expected["expect"] | {"remote": True, "multiplexer": "tmux"})


class CliTests(unittest.TestCase):
    def main(self, *arguments, tty=True, stdout_tty=None):
        # no file descriptor, like any stream that is not a file: 3.14's argparse asks for one to pick its colours
        terminal = Mock(isatty=lambda: tty, fileno=Mock(side_effect=io.UnsupportedOperation))
        output = terminal if stdout_tty is None else Mock(isatty=lambda: stdout_tty, fileno=terminal.fileno)
        with TemporaryDirectory() as directory:
            profile = str(Path(directory) / "p.json")
            with patch("sys.stdin", terminal), patch("sys.stdout", output):
                return cli.main(["--no-sound", "--profile", profile, *arguments])

    def test_the_game_runs_on_its_own_terminal_layer_whatever_the_old_backend_variable_says(self):
        with patch.object(app, "run") as run, patch.dict(os.environ, {"INVADERS_BACKEND": "curses"}):
            self.assertEqual(self.main(), 0)
        run.assert_called_once()

    def test_defaults_are_the_options_of_a_bare_command_line(self):
        with patch.object(app, "run") as run:
            self.assertEqual(self.main(), 0)
        bare, made = vars(run.call_args.args[0]), vars(cli.defaults(no_sound=True))
        self.assertEqual(bare.keys(), made.keys())  # what tools and tests build a Session from lacks nothing
        self.assertEqual(bare.pop("keymap").contexts, made.pop("keymap").contexts)
        for name in ("seed", "profile", "name"):  # a random seed, the test's profile file and the pilot it names
            bare.pop(name), made.pop(name)
        # Phase 5: the flags given, and the title demo, which a launch turns on (auto) and defaults() keeps off
        self.assertEqual((bare.pop("given"), made.pop("given")), (frozenset({"no_sound"}), frozenset()))
        self.assertEqual((bare.pop("attract"), made.pop("attract")), ("auto", "off"))
        self.assertEqual(bare, made)
        self.assertEqual((cli.defaults(seed=7).seed, cli.defaults().keymap.has_physical), (7, False))

    def test_mode_campaign_is_the_classic_mode(self):
        """Spec 16.3: players see CAMPAIGN; tapes, boards and the CLI keep the key classic, and accept the name."""
        for arguments, mode in (
            (("--mode", "campaign"), "classic"),
            (("--mode", "classic"), "classic"),
            ((), "classic"),
        ):
            with self.subTest(arguments=arguments), patch.object(app, "run") as run:
                self.assertEqual(self.main(*arguments), 0)
                self.assertEqual(run.call_args.args[0].mode, mode)
        with patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit) as exited:
            self.main("--mode", "campaign", "--endless")
        self.assertEqual(exited.exception.code, 2)
        self.assertIn("not allowed with argument", err.getvalue())

    def test_replay_refuses_a_campaign_1_tape_before_the_terminal_opens(self):
        """Spec 17: a campaign-1 tape still verifies, but watching one is refused with replay's message, exit 1."""
        legacy = Path(__file__).parent / "fixtures" / "legacy" / "golden-classic-normal.json"
        with patch.object(app, "run") as run, patch.object(cli, "require_terminal") as terminal:
            with patch("sys.stderr", io.StringIO()) as err:
                self.assertEqual(self.main("--replay", str(legacy)), 1)
        run.assert_not_called()
        terminal.assert_not_called()
        self.assertEqual(err.getvalue(), f"invaders: {LEGACY_REFUSAL}\n")
        with patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(cli.main(["--verify-replay", str(legacy)]), 0)
        self.assertTrue(out.getvalue().startswith("VERIFIED"), out.getvalue())
        config = Config(seed=4)
        game, recorder = CampaignGame(config), Recorder(config)
        for tick in range(60):
            actions = ("fire",) if tick % 7 == 0 else ()
            recorder.append(actions)
            game.step(actions)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "new.json"
            recorder.save(path, game)
            with patch.object(app, "run") as run:
                self.assertEqual(self.main("--replay", str(path)), 0)  # a campaign-2 tape is watched
        self.assertEqual(run.call_args.args[3].ruleset, "campaign-2")

    def test_scores_call_the_mode_campaign(self):
        with TemporaryDirectory() as directory:
            profile = str(Path(directory) / "p.json")
            for arguments, header in (
                ((), "CAMPAIGN / NORMAL"),
                (("--mode", "campaign", "--difficulty", "hard"), "CAMPAIGN / HARD"),
                (("--endless",), "ENDLESS / NORMAL"),
                (("--arcade",), "ARCADE"),
            ):
                with self.subTest(arguments=arguments), patch("sys.stdout", io.StringIO()) as out:
                    self.assertEqual(cli.main(["--scores", "--profile", profile, *arguments]), 0)
                empty = "No scores yet. Your next run starts the board."
                self.assertEqual(out.getvalue().splitlines(), [header, empty])

    def test_keys_and_controls_reach_the_game(self):
        with patch.object(app, "run") as run:
            self.assertEqual(self.main("--keys", "vim", "--controls", "timed", "--no-sync", "--no-kitty"), 0)
        options = run.call_args.args[0]
        self.assertEqual(options.keymap.contexts, Keymap.preset("vim").contexts)
        self.assertEqual(options.controls, "timed")
        self.assertEqual(app.overrides(options), app.Overrides(kitty=False, sync=False))  # --controls: make_controls
        with patch.object(app, "run") as run:
            self.main()
        options = run.call_args.args[0]
        self.assertEqual((options.keymap.contexts, options.controls), (Keymap.preset("default").contexts, "auto"))
        self.assertEqual(app.overrides(options), app.Overrides(pacing="auto"))

    def test_pacing_reaches_the_terminal_session(self):
        for flag in ("auto", "adaptive", "fixed", "off"):
            with self.subTest(flag=flag), patch.object(app, "run") as run:
                self.assertEqual(self.main("--pacing", flag), 0)
                self.assertEqual(app.overrides(run.call_args.args[0]).pacing, flag)
        with patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit) as exited:
            self.main("--pacing", "fast")
        self.assertEqual(exited.exception.code, 2)
        self.assertIn("invalid choice", err.getvalue())

    def test_scale_and_glyphs_reach_the_painter(self):
        for arguments, scale, k in (((), "auto", 3), (("--scale", "2"), "2", 2), (("--glyphs",), "1", 1)):
            with self.subTest(arguments=arguments), patch.object(app, "run") as run:
                self.assertEqual(self.main(*arguments), 0)
                parsed = run.call_args.args[0]
                renderer = app.Renderer(parsed, capabilities(load("ghostty")), (240, 70))
                renderer.frame(session(), 0.0, True, OFF)
                self.assertEqual((parsed.scale, renderer.painter.layout.k), (scale, k))
        for arguments, message in ((("--scale", "5"), "invalid choice"), (("--glyphs", "--scale", "2"), "not allowed")):
            with self.subTest(arguments=arguments), patch.object(app, "run") as run:
                with patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit) as exited:
                    self.main(*arguments)
                self.assertEqual(exited.exception.code, 2)
                self.assertIn(message, err.getvalue())
                run.assert_not_called()

    def test_a_keymap_file_is_checked_before_the_terminal_is_touched(self):
        with TemporaryDirectory() as directory:
            good, bad = Path(directory) / "good.json", Path(directory) / "bad.json"
            good.write_text('{"preset": "arrows", "bind": {"play.bomb": ["n"]}}')
            bad.write_text('{"bind": {"play.menu": ["q"]}}')
            with patch.object(app, "run") as run:
                self.assertEqual(self.main("--keymap", str(good)), 0)
            self.assertEqual(run.call_args.args[0].keymap.keys_for("play", "bomb"), ("n",))
            for arguments, message in (
                (("--keymap", str(bad)), "leaving a run is only possible from the pause menu"),
                (("--keymap", str(Path(directory) / "missing.json")), "Cannot read keymap"),
                (("--keymap", str(good), "--keys", "vim"), "not allowed with argument"),
                (("--controls", "sticky"), "invalid choice"),
            ):
                with self.subTest(arguments=arguments), patch.object(app, "run") as run:
                    with patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit) as exited:
                        self.main(*arguments)
                    self.assertEqual(exited.exception.code, 2)
                    self.assertIn(message, err.getvalue())
                    run.assert_not_called()

    def test_a_lost_terminal_exits_129(self):
        with patch.object(app, "run", side_effect=TerminalGone("hangup")):
            self.assertEqual(self.main(), 129)

    def test_probe_needs_a_terminal_to_ask_but_its_report_may_go_to_a_file(self):
        with (
            patch.object(cli, "open_tty", side_effect=ValueError("no terminal")),
            patch.object(app, "run_probe_command"),
        ):
            with patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit) as exited:
                self.main("--probe", tty=False)
        self.assertEqual(exited.exception.code, 2)
        self.assertIn("--probe asks the terminal it runs in", err.getvalue())
        with patch.object(cli, "open_tty", return_value=99), patch.object(cli.os, "close") as closed:
            with patch.object(app, "run_probe_command", return_value=0) as probe:
                self.assertEqual(self.main("--probe"), 0)
                self.assertEqual(self.main("--probe", "--json", stdout_tty=False), 0)  # invaders --probe --json | jq
                self.assertEqual(self.main("--probe", "--raw", tty=False, stdout_tty=False), 0)  # > fixture.json
        self.assertEqual([call.args for call in closed.call_args_list].count((99,)), 3)  # only asked, then closed
        self.assertEqual(
            [(call.args[0].json, call.args[0].raw) for call in probe.call_args_list], [(0, 0), (1, 0), (0, 1)]
        )
        with patch.object(app, "run") as run, patch("sys.stderr", io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                self.main(stdout_tty=False)  # play still needs both ends on the terminal
        self.assertIn("play in an interactive terminal", err.getvalue())
        run.assert_not_called()
        for arguments, message in ((("--json",), "go with --probe"), (("--probe", "--json", "--raw"), "not allowed")):
            with self.subTest(arguments=arguments), patch.object(app, "run_probe_command") as probe:
                with patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit) as exited:
                    self.main(*arguments)
                self.assertEqual(exited.exception.code, 2)
                self.assertIn(message, err.getvalue())
                probe.assert_not_called()

    def test_colour_and_theme_flags_reach_resolve(self):
        for arguments, color, theme in (
            ((), "auto", "auto"),
            (("--mono",), "mono", "auto"),
            (("--color", "256", "--theme", "light"), "256", "light"),
            (("--theme", "terminal"), "auto", "terminal"),
        ):
            with self.subTest(arguments=arguments), patch.object(app, "run") as run:
                self.assertEqual(self.main(*arguments), 0)
                flags = app.overrides(run.call_args.args[0])
                self.assertEqual((flags.color, flags.theme), (color, theme))
        for arguments, message in ((("--mono", "--color", "16"), "not allowed"), (("--theme", "sepia"), "invalid")):
            with self.subTest(arguments=arguments), patch.object(app, "run") as run:
                with patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit) as exited:
                    self.main(*arguments)
                self.assertEqual(exited.exception.code, 2)
                self.assertIn(message, err.getvalue())


@pytest.fixture(autouse=True)
def quiet_logging():
    """assertLogs raises the root level for its block only; keep it where the tests found it."""
    level = logging.root.level
    yield
    logging.root.setLevel(level)
