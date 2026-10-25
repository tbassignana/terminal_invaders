"""The game on its terminal: the main loop, the Renderer and the --probe command, on a TerminalSession.

Rendering is locked to the 60 Hz ticks: while playing, a frame is due every k ticks; otherwise only when something
changed. A frame is built only when the output queue is empty and the ack pacer allows it, so a slow terminal
drops frames instead of queueing them. Keys go through the Session's Router with the control scheme that the
terminal's capabilities allow (hold with kitty key reports, tap elsewhere).
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import logging
import os
import platform
import sys
from collections import deque

from . import fx
from .input import LatchedControls, make_controls
from .keymap import Keymap
from .model import TICK_RATE
from .term.caps import EnvHints, Overrides, resolve, user_env
from .term.cells import CellBuffer
from .term.decode import Decoder
from .term.encode import Encoder, ack_tag, packet, tag_count
from .term.gate import KeyGate
from .term.keys import RELEASE, Reply
from .term.pacing import TICK
from .term.repeat import RepeatModel
from .term.sanitize import sanitize
from .term.simkeys import PROFILES
from .term.tty import TerminalGone, TerminalSession, open_tty
from .theme import PALETTE_NAMES, resolve_theme
from .ui import Fits, FixedClock, Painter, Session, arcade_frame

# Variables a fixture records (5.8): these by value, the presence-only ones as "1"; never anything else
FIXTURE_ENV = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "LC_TERMINAL", "VTE_VERSION")
FIXTURE_ENV += ("KONSOLE_VERSION", "LANG", "LC_ALL", "LC_CTYPE", "NO_COLOR", "COLORFGBG")
FIXTURE_PRESENCE = ("WT_SESSION", "TMUX", "STY", "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
FIXTURE_EXPECT = ("family", "remote", "multiplexer", "color", "background", "sync", "release_events", "scheme")
FIXTURE_EXPECT += ("utf8", "ambiguous_wide", "unicode_blocks", "echoes_queries", "ack")
FLASH_LIMIT = 3  # full-screen flashes in any one second (WCAG 2.3.1), for Phase 5's effects
READ_LIMIT = 64  # events handled per iteration; the tty is not read while this many wait, so the kernel buffers
READ_SIZE = 4096  # bytes per read; a full read may have left more in the tty
DOWNGRADE_NOTE = "Keyboard mode changed: tap to move"


def overrides(options) -> Overrides:
    """What resolve() takes from the options: --color (--mono), --theme (the BACKGROUND setting), --pacing,
    --no-kitty, --no-sync, and the palette in force, which gives the tier with them (Phase 5 spec 3.2): mono is the
    mono tier, and one the player chose (--palette, or a palette other than default, as only those are saved) beats
    NO_COLOR. --controls goes to make_controls() and --scale to the layout."""
    palette = getattr(options, "palette", "default")
    palette = palette if palette in PALETTE_NAMES else "default"
    chosen = "palette" in getattr(options, "given", ()) or palette != "default"
    return Overrides(
        color=options.color,
        theme=options.theme,
        pacing=options.pacing,  # auto is the adaptive law (deviation D1, accepted)
        kitty=False if options.no_kitty else None,
        sync=False if options.no_sync else None,
        palette=palette,
        palette_chosen=chosen,
    )


def frame_interval(fps: int) -> int:
    """Ticks per frame: 60, 30, 20 or 15 frames per second (any --fps from 15 to 120 rounds to one of them)."""
    return max(1, round(TICK_RATE / fps))


def text_sanitizer(caps):
    """Text as this terminal can show it: '?' for ambiguous-width characters where they are wide, and for all
    non-ASCII without UTF-8 (the encoder would send '?' anyway, and the buffer must hold what the screen shows)."""
    narrow = not caps.ambiguous_wide

    def clean(text: str) -> str:
        text = sanitize(text, ambiguous_narrow=narrow)
        return text if caps.utf8 or text.isascii() else text.encode("ascii", "replace").decode()

    return clean


def text_hash(rows) -> str:
    """Hash of a screen's text rows, logged with every frame under --debug; the pty tests compare MiniVT with it."""
    return hashlib.sha1("\n".join(rows).encode()).hexdigest()[:16]


def faults(options) -> tuple[int | None, int]:
    """INVADERS_FAULT, honoured only with --debug: raise:<tick> crashes at that tick, stress:<bytes> pads frames."""
    fault = os.environ.get("INVADERS_FAULT", "") if options.debug else ""
    kind, _, value = fault.partition(":")
    if kind not in ("raise", "stress") or not value.isdigit():
        return None, 0
    return (int(value), 0) if kind == "raise" else (None, int(value))


class Renderer:
    """Painter scenes on a CellBuffer, diffed against the previous frame into one packet.

    Two themes: the palette in force (options.palette), and arcade_theme, the default palette's in the same tier and
    background, which every frame of an Arcade game is drawn with (its 1978 colours under every palette but mono,
    Phase 5 spec 3.1). Moving between them builds the Painter and Encoder again and paints every cell, as a scene
    change does. The flash limiter (spec 2.9) is the Renderer's, handed to each Painter it builds, so it survives a
    reconfigure; each frame tells the Painter the link it goes out on (painter.link, spec 2.8)."""

    def __init__(self, options, caps, size: tuple[int, int], *, stress: int = 0):
        self.options, self.stress = options, stress
        self.k = frame_interval(options.fps)
        self.buffer = CellBuffer(*size)
        self.ticks_pending, self.frames, self.last_full = 0, 0, False
        self.tags, self.last_tag = 0, None  # the place in the tag sequence, on across restarts (whose new pacer
        # still reserves the tags the old frames' replies may name: AckPacer.inherit)
        self.fps, self.bpf, self.was_ticking = 0.0, 0, None  # the stats line's frames per second and bytes per frame
        self.fps_since, self.fps_frames, self.fps_bytes = 0.0, 0, 0
        self.flashes: deque[float] = deque(maxlen=FLASH_LIMIT)
        self.limiter = fx.FlashLimiter()  # kicks admitted (spec 2.9), fed game ticks, across reconfigures
        self.reconfigure(caps)

    def reconfigure(self, caps) -> None:
        """Startup, resume or a setting: themes, text rules and encoder follow caps and the options (the palette is
        resolved here every time, so a re-probe, a Ctrl-Z / fg or a resize keeps the one chosen in SETTINGS), and
        the next frame paints every cell."""
        self.caps = caps
        palette = getattr(self.options, "palette", "default")
        self.theme = resolve_theme(caps, palette)
        self.arcade_theme = resolve_theme(caps, "mono" if palette == "mono" else "default")
        self.buffer.sanitizer = text_sanitizer(caps)
        self.build(self.theme)

    def build(self, theme) -> None:
        """A Painter and an Encoder for theme; the next frame paints every cell."""
        self.painter = Painter(self.buffer, self.options, theme, self.caps)
        self.painter.limiter, self.painter.link = self.limiter, fx.Link()
        self.encoder = Encoder(theme, self.caps)
        self.prev, self.dirty = None, True

    def may_flash(self, now: float) -> bool:
        """The Phase 5 hook for effects that flash the screen: True, and counted, at most FLASH_LIMIT times in any
        second (WCAG 2.3.1). Phase 1 has no such effect, so nothing asks yet."""
        if len(self.flashes) == FLASH_LIMIT and now - self.flashes[0] < 1.0:
            return False
        self.flashes.append(now)
        return True

    @property
    def fits(self) -> Fits:
        """Whether the window holds the campaign's block and the Arcade block."""
        return Fits(self.painter.layout.fits, self.painter.arcade.fits)

    def relayout(self, size: tuple[int, int]) -> bool:
        """Follow the terminal's size; False when it did not change."""
        if size == (self.buffer.cols, self.buffer.rows):
            return False
        self.buffer.resize(*size)
        self.painter.resize()
        self.invalidate()
        return True

    def invalidate(self) -> None:
        """The next frame paints every cell explicitly, never with ED (resize, ctrl+l, a screen of unknown state)."""
        self.encoder.invalidate()
        self.dirty = True

    def due(self, ticking: bool) -> bool:
        """Playing: every k ticks. Otherwise only after a change: input, a resize, or a tick that ended play."""
        if self.prev is None:
            return True
        return self.ticks_pending >= self.k if ticking else self.dirty or self.ticks_pending > 0

    def frame(self, session, now: float, ticking: bool, pacer) -> bytes:
        """Draw and diff one frame. An unchanged frame still goes out (sync and ack only), so the ack clock runs and
        every frame carries its own ticks; without sync and acks its packet is empty."""
        theme = self.arcade_theme if arcade_frame(session) else self.theme
        if theme != self.painter.theme:  # into or out of an Arcade game under a palette of its own
            self.build(theme)
        # the link this frame goes out on (spec 2.8); a pacer stand-in without congestion (tests, tools) has none.
        # Clock pacing: the acks stopped coming, or the terminal has none at all (--pacing off, or no reply to the
        # probe's ack queries), so nothing measures the link and the kick stays off
        congested = getattr(pacer, "congested", False)
        clock = getattr(pacer, "disabled", False) or (pacer is not None and not self.caps.ack)
        self.painter.link = fx.Link(remote=self.caps.remote, congested=congested, clock=clock)
        self.painter.draw(session, self.stats(session, pacer) if self.options.show_fps else "")
        body = self.encoder.encode(self.prev, self.buffer)
        self.last_full = self.encoder.last_full
        self.prev = self.buffer.copy()
        ticks, self.ticks_pending, self.dirty = self.ticks_pending, 0, False
        if self.stress:
            body += b"\x1b[H" * (self.stress // 3)  # cursor moves only: the screen stays the same
        self.last_tag = None
        if self.caps.ack == "cpr":
            self.last_tag = ack_tag(None, *self.size) if pacer.disabled else self._next_tag(pacer)
        data = packet(body, self.caps, self.last_tag)
        self.frames += 1
        self._count(now, ticking, len(data))
        if logging.root.isEnabledFor(logging.DEBUG):
            buf = self.buffer
            rows = [buf.row_text(y) for y in range(buf.rows)]
            logging.debug(
                "Frame %d %dx%d ticks %d bytes %d full %d text %s",
                *(self.frames, buf.cols, buf.rows, ticks, len(data), self.last_full, text_hash(rows)),
            )
        return data

    @property
    def size(self) -> tuple[int, int]:
        return self.buffer.cols, self.buffer.rows

    def _next_tag(self, pacer) -> tuple[int, int] | None:
        """The next tag in sequence that no reply still to come may name (AckPacer.reserved()): a reply to an older
        frame then never settles this one. None (no ack query) when there is none, as on a screen under 5 rows."""
        reserved = pacer.reserved()
        for _ in range(max(1, tag_count(*self.size) - 1)):
            tag = ack_tag(self.tags, *self.size)
            self.tags += 1
            if tag is None or tag not in reserved:
                return tag
        return None

    def _count(self, now: float, ticking: bool, nbytes: int) -> None:
        """Frames per second and bytes per frame while playing, measured over at least a second."""
        if ticking != self.was_ticking:
            self.fps, self.bpf, self.was_ticking = 0.0, 0, ticking
            self.fps_since, self.fps_frames, self.fps_bytes = now, 0, 0
        if ticking:
            self.fps_frames, self.fps_bytes = self.fps_frames + 1, self.fps_bytes + nbytes
            if now - self.fps_since >= 1:
                self.fps, self.bpf = self.fps_frames / (now - self.fps_since), round(self.fps_bytes / self.fps_frames)
                self.fps_since, self.fps_frames, self.fps_bytes = now, 0, 0

    def stats(self, session, pacer) -> str:
        """The stats line (F1, --show-fps): frame rate, ack round trip, pacing window, bytes per frame, keys routed
        (mod 100: gate G4 times key to photon by it), control scheme, scale (the arena's k, or p for an Arcade game's
        block) and colour tier."""
        rtt = "-" if pacer.srtt is None else f"{1000 * pacer.srtt:.0f}ms"
        if pacer.mode == "off" or pacer.disabled:
            window = "ACK OFF"
        else:
            window = "W fix" if pacer.mode == "fixed" else f"W {pacer.window:.1f}"
        scale = f"p{self.painter.arcade.p}" if arcade_frame(session) else f"k{self.painter.layout.k}"
        return (
            f"FPS {self.fps:.1f}  RTT {rtt}  {window}  B/f {self.bpf}  K{session.routed % 100:02d}"
            f"  {session.controls.scheme} {scale} {self.theme.tier}"
        )

    def on_reply(self, reply: Reply, pacer) -> None:
        """An ack frees a frame slot (a CPR names its frame by its position); focus and paste change nothing yet;
        anything else came late or is unknown."""
        if reply.kind == self.caps.ack:
            pacer.acked(reply.time, reply.value if reply.kind == "cpr" else None)
        elif reply.kind not in ("focus", "paste"):
            logging.debug("Late or unknown reply: %r", reply)


def configure(session, decoder: Decoder, caps) -> None:
    """Kitty decoding, the control scheme and the KeyGate's mode for these capabilities."""
    decoder.kitty = bool(caps.kitty_flags)
    decoder.esc_timeout, decoder.string_timeout = caps.esc_timeout, caps.string_timeout
    if session.router.gate.repeat.remote != caps.remote:  # remote timings: wider guards, campaign fire presses as edges
        session.router.gate = KeyGate(RepeatModel(remote=caps.remote), certain=caps.release_events)
    session.set_scheme(make_controls(caps, session.options.controls, session.keymap), caps.release_events)
    logging.debug("Controls: %s, keys %s", session.controls.scheme, "kitty" if decoder.kitty else "legacy")


class Hooks:
    """The game's side of the terminal: keys as this terminal reports them, and what a suspend or a resume does
    (pause play, then repaint and re-key with the new capabilities)."""

    def __init__(self, session, renderer: Renderer, decoder: Decoder, term=None):
        self.session, self.renderer, self.decoder = session, renderer, decoder
        self.term = term  # the TerminalSession served, which keeps overrides and keymap for a re-probe (Phase 5 4.5)

    def configure(self, caps) -> None:
        configure(self.session, self.decoder, caps)

    def apply(self, name: str) -> None:
        """A setting the settings card changed in the Session's options takes effect at once (Phase 5 spec 4.5): name
        is its settings key. What the terminal session must remember goes there too (term.overrides, term.keymap), so
        a later re-probe, Ctrl-Z / fg or reconnect keeps it. effects, reduced_motion, tips, attract and name are read
        from the options (or the profile) when they are needed, so they only ask for a frame."""
        session, renderer, term = self.session, self.renderer, self.term
        options = session.options
        if name in ("palette", "theme"):  # the tier and background again (spec 3.2), then one full repaint
            caps = renderer.caps
            if term is not None:
                term.overrides = overrides(options)
                if term.caps is not None and term.replies is not None:
                    flags = term.caps.kitty_flags
                    caps = term.caps = resolve(term.env, term.replies, term.overrides, kitty_verified=flags)
            renderer.reconfigure(caps)
        elif name == "scale":
            renderer.reconfigure(renderer.caps)  # the Painter lays out again at the new largest scale
        elif name == "fps":
            renderer.k = frame_interval(options.fps)
        elif name == "controls":
            self.configure(renderer.caps)  # the scheme and the KeyGate, as at launch
        elif name == "keys" and not options.keymap_file:  # a --keymap FILE holds the row
            keymap = Keymap.preset(options.keys)
            physical = keymap.has_physical != session.keymap.has_physical
            options.keymap = session.keymap = session.router.keymap = keymap
            if term is not None:
                term.keymap = keymap
                # to or from wasd-physical: push the kitty flags again, flag 4 included; only a terminal that took
                # kitty flags can gain or lose flag 4, so elsewhere a re-probe would be pure cost (ST-1)
                term.reprobe |= physical and bool(renderer.caps.kitty_flags)
            self.configure(renderer.caps)  # the controls read their keys from the keymap
        elif name == "sound" and session.audio.enabled == options.no_sound:
            session.audio.toggle()  # as M does
        renderer.dirty = True

    def before_suspend(self) -> None:
        self.session.pause("suspended")  # which forgets held keys too

    def after_resume(self, caps, reason: str) -> None:
        """reason: suspended, resumed (a SIGCONT nobody asked for, which can find the game playing) or reprobe."""
        if reason != "reprobe":
            self.session.pause(reason)
        self.renderer.reconfigure(caps)
        self.configure(caps)
        logging.debug("Terminal back (%s)", reason)


def on_reply(session, term, renderer: Renderer, reply: Reply) -> None:
    """Focus loss pauses, a paste is dropped, a downgrade means tap controls; acks and late replies are the
    Renderer's."""
    if reply.kind == "focus":
        session.on_focus(reply.value)
    elif reply.kind == "paste":
        logging.debug("Paste of %d bytes ignored", reply.value)
    elif reply.kind == "downgrade":
        downgrade(session, term)
    else:
        renderer.on_reply(reply, term.pacer)
        return
    renderer.dirty = True


def downgrade(session, term) -> None:
    """A bare key in kitty mode: the terminal dropped the protocol (a reset, or tmux reattached elsewhere). Tap
    controls from now on, and a fresh probe at the next pause. kitty_depth stays: the flags may still be pushed (a
    split sequence reads as a bare key too), and popping an empty stack does nothing."""
    term.reprobe = True
    session.set_scheme(LatchedControls(DOWNGRADE_NOTE, remote=session.router.gate.repeat.remote), certain=False)
    logging.debug("Kitty key reports stopped: tap controls until the next probe")


class Meter:
    """Loop statistics and the stats line, logged once a second under --debug (the pty tests read ticks per second
    from them), and every change between ack and clock pacing."""

    def __init__(self, now: float):
        self.since, self.ticks, self.frames, self.bytes = now, 0, 0, 0
        self.acks_off = False

    def update(self, now: float, term, stats) -> None:
        """stats() gives the stats line; it is built only when logged."""
        if term.pacer.disabled != self.acks_off:
            self.acks_off = term.pacer.disabled
            logging.debug("Acks %s", "lost: clock pacing" if self.acks_off else "answered again: ack pacing")
        if now - self.since < 1.0:
            return
        logging.debug(
            "Loop %.3fs: %d ticks %d frames %d bytes, longest write %.1fms, srtt %s",
            *(now - self.since, self.ticks, self.frames, self.bytes, 1000 * term.out.longest, term.pacer.srtt),
        )
        logging.debug("Stats: %s", stats())
        term.out.longest = 0.0
        self.since, self.ticks, self.frames, self.bytes = now, 0, 0, 0


def loop(session, term, renderer: Renderer, decoder: Decoder, clock: FixedClock, *, crash_at=None) -> None:
    """One select() per iteration over the tty (reads, and writes while output waits) and the signal pipe."""
    ops, meter = term.ops, Meter(term.ops.clock())
    # Since when replies may have waited unread: the last read that drained the tty (from just before it), or the last
    # select() that polled it (less any time it overran its timeout, when it may have stopped polling). The game's
    # waits bound each round trip from below (pacing.py): the time to a read since then, and the time the newest
    # frame's bytes (built at frame_at) sat in the queue on the game since they were built or last offered to the tty
    # (offered). That is counted whenever the tty can take them when the game looks again, so a stall the terminal
    # made room in counts whole, as the game cannot see when it did (the safe side: a lower bound only lower), and
    # never the time inside write(), which waits on the terminal. Both are measured for one pacer (tracked).
    awake = offered = tracked = frame_at = None
    showing = session  # whose image the screen holds: the Session, or the title demo's nested one
    while not session.quit:
        term.check()  # signals; may suspend and resume, or raise
        small = term.small_probe and (session.scene != "playing" or not renderer.frames)  # off play, or before it
        if term.reprobe and (session.scene in ("paused", "settings", "menu") or small):
            # kitty reports stopped during play, the last probe ran on one row, or KEYS moved to or from
            # wasd-physical on the settings card (applied as the card closes, too: the title menu is as idle as a
            # pause, ST-2): ask again
            term.restart("reprobe")
        while term.early_keys:
            key = term.early_keys.pop(0)
            session.handle(key, key.time)
            if session.controls.scheme == "hold":  # a legacy key from before the push: no release will follow
                session.handle(dataclasses.replace(key, kind=RELEASE), key.time)
            renderer.dirty = True
        if term.resized:
            term.resized = False
            size = term.size()
            term.pacer.resized(*size)  # frames tagged off this size's tags leave the window
            if renderer.relayout(size):
                session.on_resize(renderer.fits)
        if session.redraw:
            session.redraw = False
            renderer.invalidate()
        now = ops.clock()
        if session.update_countdown(now):  # first, so play starts on the instant the countdown ends
            renderer.dirty = True
        attract = getattr(session, "update_attract", None)  # the title demo's cycle (spec 5.2), on the loop's clock
        if attract is not None and attract(now):
            renderer.dirty = True
        shown = session.attract or session  # the demo's nested replay Session plays and draws in the menu's place
        if shown is not showing:  # the demo began or ended: the other's image is due at once
            showing, renderer.dirty = shown, True
        for _ in range(clock.advance(now, shown.ticking)):
            if not shown.ticking:
                break
            shown.step(now)
            renderer.ticks_pending += 1
            meter.ticks += 1
            if crash_at is not None and shown.pilot.game.tick >= crash_at:
                raise RuntimeError(f"INVADERS_FAULT: crash at tick {crash_at}")
        pacer = term.pacer
        if pacer is not tracked:  # a new one, from enter() after a suspend or a reprobe: nothing waited on it yet
            awake = offered = None
            tracked = pacer
        meter.update(now, term, lambda: renderer.stats(shown, pacer))  # here: a second's ticks are those due in it
        ticking = shown.ticking
        session.audio.update()  # reads the sound helper's status now and then, in menus too
        queued, built = not term.out.empty(), None
        if renderer.due(ticking) and not queued and pacer.ready(now):
            data = renderer.frame(shown, now, ticking, pacer)
            if data:
                term.out.push(data, frame=True)
                built = frame_at = ops.clock()  # once built: its round trip starts now
                pacer.sent(built, len(data), full=renderer.last_full, tag=renderer.last_tag)
                meter.frames, meter.bytes = meter.frames + 1, meter.bytes + len(data)
        wait = clock.next_tick_at() - now + 0.0003 if ticking else 0.100
        if not ticking and pacer.inflight:
            wait = TICK  # replies awaited: a stall inside a select() goes uncounted for its timeout, so a tick at most
        for deadline in (decoder.deadline(), pacer.deadline(), session.countdown_deadline()):
            if deadline is not None:
                wait = min(wait, deadline - now)
        backlog = len(decoder.queue)
        readable = [term.fd, term.wakeup] if backlog < READ_LIMIT else [term.wakeup]
        writable = [term.fd] if term.out.wants_write() else []
        timeout = 0.0 if backlog else max(0.0, wait)
        since = built if built is not None else offered if queued else None
        # Only the game fills the tty, so one found full was full all the time since the game last wrote: bytes left
        # from an earlier iteration ask it at `before`, a frame built in this one goes by the select()'s answer.
        before = ops.clock()
        sendable = queued and bool(ops.select([], [term.fd], [], 0)[1])
        r, w, _ = ops.select(readable, writable, [], timeout)
        after = ops.clock()
        overrun = max(0.0, after - before - timeout)  # the game descheduled in select(), or in the poll before it
        if since is not None:
            sendable = sendable or (not queued and term.fd in w)
            waited = (before - since if sendable else 0.0) + (overrun if sendable or term.fd in w else 0.0)
            pacer.held(waited, frame_at)  # the newest frame's bytes may have waited on the game: at most this
        if term.wakeup in r:
            term.drain_wakeup()
        if term.fd in w:
            term.out.flush_some(unread=pacer.unread(term.out.pending()))
            offered = ops.clock()
            if since is not None:  # sendable since `after`: outside write() itself, they waited on the game
                pacer.held(offered - after - term.out.writing, frame_at)
        else:
            offered = after - overrun  # the tty took nothing: from when select() would have returned
        if term.fd in r:
            looked = ops.clock()
            chunk, read_at = term.read(READ_SIZE), ops.clock()
            unread = (0.0 if awake is None else before - awake) + overrun + read_at - after
            pacer.read(read_at, unread)
            if unread >= 2 * TICK:
                logging.debug("Read %.0fms after the last look: its replies may have waited", 1000 * unread)
            if len(chunk) < READ_SIZE:
                awake = looked  # the tty is drained as of the read: from then on a reply waits only on the game
            if term.size_changed():  # signalled before these bytes were read: replies among them may be clamped to it
                pacer.resized(*term.size())
            # Each reply owed may come clamped onto row 1, before the game hears of the size (none without CPR acks)
            decoder.clamped = pacer.owed if term.caps.ack == "cpr" else 0
            decoder.feed(chunk, read_at)
        elif term.fd in readable:
            awake = after - overrun  # polled: nothing had come by the end of its timeout
        now = ops.clock()
        decoder.expire(now)
        for event in decoder.events(READ_LIMIT):
            if isinstance(event, Reply):
                on_reply(session, term, renderer, event)
            else:
                session.handle(event, event.time)
                renderer.dirty = True
        if awake is not None and not (pacer.overdue(awake) and acks_queued(decoder, term.caps.ack)):
            pacer.expire(awake)  # a frame is lost only if its reply had not come when the game last looked
        if term.out.dead:
            raise TerminalGone("the terminal stopped accepting output")


def acks_queued(decoder: Decoder, kind: str | None) -> bool:
    """Whether an ack reply the game has read still waits behind READ_LIMIT events, unhandled."""
    return any(isinstance(event, Reply) and event.kind == kind for event in decoder.queue)


def play(session, term, *, crash_at=None, stress: int = 0) -> Renderer:
    """The game on an entered TerminalSession, until it quits."""
    caps = term.caps
    renderer = Renderer(session.options, caps, term.size(), stress=stress)
    decoder = Decoder(esc_timeout=caps.esc_timeout, string_timeout=caps.string_timeout)
    term.hooks = session.hooks = Hooks(session, renderer, decoder, term)  # the settings card applies through it
    term.hooks.configure(caps)
    session.enable_tips()  # the real terminal path: the first-run tips may teach this Session's runs (spec 7.1)
    session.room = renderer.fits
    if not session.fits:
        session.pause("small")  # too small from the start: play waits, paused, for a bigger window
    loop(session, term, renderer, decoder, FixedClock(term.ops.clock()), crash_at=crash_at)
    return renderer


def run(options, profile, audio, replay=None) -> None:
    """Play on the terminal; it is restored first, then the replay is saved and audio closed, on every exit path."""
    session = Session(options, profile, audio, replay)
    crash_at, stress = faults(options)
    try:
        fd = open_tty()
        try:
            env = EnvHints.from_env(user_env(os.environ, sys.flags.utf8_mode))
            with TerminalSession(fd, env, overrides(options), session.keymap) as term:
                play(session, term, crash_at=crash_at, stress=stress)
        finally:
            os.close(fd)
    finally:
        session.save_replay()
        audio.close()


def run_probe_command(options) -> int:
    """invaders --probe: ENTER, the probe, the kitty push and its verification, LEAVE; then print the result: a
    summary and one line per field, the fields as JSON (--json), or a fixture document (--raw). A fixture records
    the terminal with the default settings, so the flags that change what is asked (--no-kitty, a keymap) or
    resolved (--color, --theme) are left out of it."""
    raw = options.raw
    keymap = Keymap.preset("default") if raw else options.keymap
    environ = user_env(os.environ, sys.flags.utf8_mode)
    fd = open_tty()
    try:
        env, flags = EnvHints.from_env(environ), Overrides() if raw else overrides(options)
        with TerminalSession(fd, env, flags, keymap) as term:
            term.check()  # a ^C, SIGTERM or SIGHUP during the probe exits as it would in play (4.8)
            caps = term.caps
    finally:
        os.close(fd)
    fields = {item.name: getattr(caps, item.name) for item in dataclasses.fields(caps)}
    if raw:
        print(json.dumps(fixture(caps, term.replies.raw, term.push_reply, environ), indent=2))
    elif options.json:
        print(json.dumps({name: sorted(v) if isinstance(v, frozenset) else v for name, v in fields.items()}, indent=2))
    else:
        print(caps.row())
        for name, value in fields.items():
            print(f"{name:<16} {' '.join(sorted(value)) if isinstance(value, frozenset) else value}")
    return 0


def fixture(caps, replies: bytes, push_reply: bytes | None, env, *, today: str | None = None) -> dict:
    """A terminal fixture (spec 5.8) from a probe: the whitelisted variables (presence-only ones as "1"), the
    replies as read, and what resolve() made of them as its expectation. The OS key repeat cannot be probed, so
    keys.os_repeat is this platform's default profile."""
    mac = sys.platform == "darwin"
    system = f"macOS {platform.mac_ver()[0]}" if mac else f"{platform.system()} {platform.release()}"
    origin = f"captured: {caps.family} {caps.version}".rstrip()
    repeat = PROFILES["macos-default" if mac else "gnome"]
    scheme = make_controls(caps).scheme
    recorded = {name: env[name] for name in FIXTURE_ENV if name in env}
    return {
        "family": caps.family,
        "provenance": f"{origin}, {system}, invaders --probe --raw, {today or datetime.date.today().isoformat()}",
        "env": recorded | {name: "1" for name in FIXTURE_PRESENCE if name in env},
        "replies": replies.decode("utf-8", "replace"),
        "push_reply": None if push_reply is None else push_reply.decode("utf-8", "replace"),
        "echo": caps.echoes_queries,
        "keys": {
            "protocol": "kitty" if caps.kitty_flags else "legacy",
            "os_repeat": {"delay": repeat.delay, "interval": repeat.interval},
        },
        "expect": {name: scheme if name == "scheme" else getattr(caps, name) for name in FIXTURE_EXPECT},
    }
