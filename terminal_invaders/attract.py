"""Phase 5's title demo: after 30 s idle on the title, a recorded run plays, then the high scores (spec 5).

The demo is a nested replay Session of a packaged tape (tapes.attract_tapes()), made with a copy of the options, a
ReadOnlyProfile and SilentAudio, and held as the menu Session's `attract`, which the app loop ticks and draws in
the menu's place. A replay Session never records; the read-only profile and the silent audio make sure nothing the
demo does writes the profile or makes a sound. Any key ends it and does nothing else. Its timers run on the loop's
`now`, outside any game.

The cycle (spec 5.2), held by the menu Session's Attract:

1. the title menu has had no key for `idle` seconds (30, or INVADERS_ATTRACT_IDLE);
2. the outer Session's scene becomes `attract` and the nested Session plays a tape: the Campaign one, and on
   alternate cycles the Arcade one when the window holds the Arcade block;
3. at the tape's end (the nested Session's records card, never its results) or after DEMO_LENGTH seconds, the
   nested Session goes and the outer one draws the high scores (the records card's page 0, scene `attract`) for
   SCORES seconds;
4. the menu again, with the cursor where it was; after CYCLES cycles no demo starts until the next key.

A tape that fails its end-of-tape check is dropped for the session, silently (logged under --debug).
"""

from __future__ import annotations

import copy
import logging
import os

IDLE_ENV = "INVADERS_ATTRACT_IDLE"  # seconds, or off: a test-only override read once at launch (spec 1.3)
IDLE = 30.0  # seconds without a key on the title menu before the demo starts (spec 5.2)
DEMO_LENGTH = 90.0  # seconds a tape may play before the high scores
SCORES = 10.0  # seconds the high scores show
CYCLES = 10  # demo cycles before it stops until the next key
BANNER = "DEMO  Press any key"  # the banner row of every demo frame (spec 5.3)


def idle_wait(environ=None) -> float | None:
    """The idle wait: INVADERS_ATTRACT_IDLE's seconds (a number >= 0), None for `off`, else IDLE."""
    value = (os.environ if environ is None else environ).get(IDLE_ENV, "").strip().lower()
    if value == "off":
        return None
    try:
        seconds = float(value)
    except ValueError:
        return IDLE
    return seconds if 0 <= seconds < float("inf") else IDLE


def enabled(options) -> bool:
    """Whether the TITLE DEMO setting lets a demo start: on, or auto unless reduced motion is on; off, and options
    without the attribute, never (spec 5.2)."""
    setting = getattr(options, "attract", "off")
    return setting == "on" or setting == "auto" and not getattr(options, "reduced_motion", False)


class ReadOnlyProfile:
    """A profile the demo can read and never write (spec 5.3): best(), data and alert come from the real profile;
    record(), save(), rename() and update_settings() do nothing, return what a read-only profile returns ([] or
    False) and count the calls in `writes`, which every demo test asserts is 0. A stray call is never a crash."""

    read_only = True
    last_placing = None

    def __init__(self, profile):
        self._profile = profile
        self.writes = 0

    @property
    def data(self):
        return self._profile.data

    @property
    def alert(self):
        return self._profile.alert

    def best(self, config) -> int:
        return self._profile.best(config)

    def old_boards(self):
        boards = getattr(self._profile, "old_boards", None)
        return boards() if callable(boards) else {}

    def record(self, *args, **kwargs) -> list[str]:
        self.writes += 1
        return []

    def save(self) -> bool:
        self.writes += 1
        return False

    def rename(self, *args, **kwargs) -> bool:
        self.writes += 1
        return False

    def update_settings(self, *args, **kwargs) -> bool:
        self.writes += 1
        return False


class SilentAudio:
    """The demo's audio (spec 5.3): every event is dropped; it never sounds. shown, when given, is the menu's audio,
    whose on/off and availability the demo's own cards show in their footer (they never change it)."""

    def __init__(self, shown=None):
        self.shown = shown

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.shown, "enabled", False))

    @property
    def available(self) -> bool:
        return bool(getattr(self.shown, "available", True))

    def update(self, events=()) -> None:
        pass

    def toggle(self) -> bool:
        return False

    def poll_status(self) -> None:
        pass

    def close(self) -> None:
        pass


def nested(session, playback):
    """The demo's Session for a tape: a replay Session of the menu Session's own class, with a copy of its options,
    a ReadOnlyProfile and SilentAudio, playing from the tape's first tick, its window and the menu's mode and
    difficulty (its records card at the tape's end shows the menu's board)."""
    demo = type(session)(
        copy.copy(session.options), ReadOnlyProfile(session.profile), SilentAudio(session.audio), playback
    )
    demo.demo = True
    demo.room, demo.mode, demo.difficulty, demo.now = session.room, session.mode, session.difficulty, session.now
    return demo


def arcade_tape(playback) -> bool:
    return playback.tapes[0].config.mode == "arcade"


class Attract:
    """The title demo's cycle (spec 5.2), held by the menu Session: after `idle` seconds with no key on the title
    menu, the Campaign tape (the Arcade one on alternate cycles when the Arcade block fits), then the records card's
    page 0 for SCORES seconds, then the menu again; after CYCLES cycles it waits for a key. Only when the TITLE DEMO
    setting allows it (auto: on unless reduced motion), never under --replay or in a window too small.

    tapes: the Playbacks to play, or None to load the packaged ones (tapes.attract_tapes()) when first needed."""

    def __init__(self, options, tapes=None, idle: float | None = IDLE):
        self.options = options
        self.tapes = None if tapes is None else list(tapes)
        self.idle = idle
        self.cycles = 0
        self.menu_since: float | None = None  # when the menu was last arrived at (or this Attract first looked)
        self.since = 0.0  # when the demo, or the scores, began
        self.last_arcade = True  # the kind of tape played last: the first cycle plays the Campaign one

    def load(self) -> list:
        if self.tapes is None:
            from .tapes import attract_tapes

            self.tapes = list(attract_tapes())
        return self.tapes

    def allowed(self, session) -> bool:
        """Whether a demo may start now: the setting, the title menu, no replay, a window that holds the cards, cycles
        left and an idle wait."""
        return (
            self.idle is not None
            and enabled(session.options)
            and session.scene == "menu"
            and not session.replay
            and session.room.campaign
            and self.cycles < CYCLES
        )

    def choose(self, session):
        """The next tape: the Arcade one after a Campaign one when the Arcade block fits, else the Campaign one; None
        when no tape is usable."""
        tapes = self.load()
        arcade = [tape for tape in tapes if arcade_tape(tape)]
        campaign = [tape for tape in tapes if not arcade_tape(tape)]
        if arcade and session.room.arcade and (not self.last_arcade or not campaign):
            return arcade[0]
        return campaign[0] if campaign else None

    def update(self, session, now: float) -> bool:
        """Start, advance or end the demo for the menu Session at the loop's time now (session.attract is the
        demo's nested Session while one plays, else None); True when what is shown changed."""
        if session.scene == "attract":
            demo = session.attract
            if demo is not None:
                if demo.scene == "playing" and now - self.since < DEMO_LENGTH:
                    return False
                self.finish(demo)
                session.attract, self.since = None, now
                return True
            if now - self.since < SCORES:
                return False
            self.cycles += 1
            self.back(session, now)
            return True
        if session.scene != "menu":
            self.menu_since = None
            return False
        if self.menu_since is None:
            self.menu_since = now
        if not self.allowed(session):
            return False
        last_key = session.last_key_at if session.last_key_at is not None else self.menu_since
        if now - max(self.menu_since, last_key) < self.idle:
            return False
        playback = self.choose(session)
        session.change_scene("attract")
        self.since = now
        if playback is not None:
            self.last_arcade = arcade_tape(playback)
            session.attract = nested(session, playback)
        return True

    def finish(self, demo) -> None:
        """The tape played to its end, or long enough: a tape whose check failed is dropped for the session."""
        tape = demo.replay.tapes[0] if demo.replay else None
        if tape is not None and demo.pilot is not None and demo.pilot.verified is False and self.tapes:
            logging.debug("Title demo: the %s tape did not verify; dropped", tape.config.mode)
            self.tapes = [playback for playback in self.tapes if playback is not demo.replay]

    def back(self, session, now: float) -> None:
        """The menu again, with its cursor where it was; the idle wait starts again."""
        session.attract = None
        session.change_scene("menu")
        self.menu_since = now

    def stop(self, session, now: float) -> None:
        """End a running demo (a key, a focus-out, a resize that no longer fits): session.attract becomes None and the
        menu is back with its cursor where it was."""
        if session.scene == "attract" or session.attract is not None:
            self.back(session, now)
