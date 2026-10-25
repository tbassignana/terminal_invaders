"""Input semantics: the Router from keys to commands and controls, the control schemes, and simulated players.

The engine still sees one tuple of actions per tick. On kitty terminals keys report press and release, so
HoldControls moves while a key is down; elsewhere LatchedControls moves from a tap until a tap the other way or a
stop. With press_fire (an Arcade game, spec decision 15) fire follows presses only: tap schemes give a press one tick
of fire and never re-arm it from repeats, by the repeat model's press-only verdict (over SSH too); hold gives the
key's real state, and one tick for a press released inside a tick. On the campaign's upgrade card and continue prompt
the Session calls unlatch() after each tick that held a direction, so every tap of a direction is its own edge.
VirtualInput and IntentDriver turn a bot's intents into the bytes a terminal would send, for tests, balance runs and
an attract mode.
"""

from __future__ import annotations

import math
import signal

from .keymap import CONTROL, PULSE, SIGNAL
from .model import TICK_RATE
from .term import simkeys
from .term.keys import PRESS, RELEASE, REPEAT
from .term.repeat import RepeatModel

FIRE_BUFFER_TICKS = 15  # a tap made while no shot is free fires if one frees within this many ticks
MOVES = {"left": -1, "right": 1}
CHOICES = ("upgrade", "continue")  # ui.CHOICES: the campaign's card and prompt (input.py doesn't import ui)
DIRECTIONS = {-1: "left", 0: None, 1: "right"}
# local, remote: Space events this close are a held Space's OS repeats. Locally they come every 0.09 s or faster, and
# a person's taps rarely come this close; a slower OS repeat is not read as a hold. Over SSH the gap stays 0.25 s, as
# in Phase 1: real links jitter more than the emulated one (which already brings 0.09 s repeats up to 0.13 s apart),
# and a hold over SSH must not be missed. The cost: there, quick taps then a steer can still read as a held Space.
HELD_FIRE_GAP = (0.12, 0.25)
SIGNALS = {"sig_int": signal.SIGINT, "sig_tstp": signal.SIGTSTP, "sig_quit": signal.SIGQUIT}
HOLD_NOTE = "Hold needs a kitty-protocol terminal"
PHYSICAL_NOTE = "Physical keys need kitty flag 4"


class Router:
    """Key -> keymap action in the session's context -> KeyGate -> the Controls or a Session command."""

    def __init__(self, keymap, gate):
        self.keymap, self.gate = keymap, gate

    def route(self, session, key, now: float) -> None:
        hit = self.keymap.lookup(session.context, key)
        if hit and hit[1] == SIGNAL:  # kitty ctrl+c / ctrl+z / ctrl+\: never gated
            if key.kind == PRESS:
                signal.raise_signal(SIGNALS[hit[0]])
            return
        action, kind = hit or (None, None)
        admitted, edge = self.gate.admit(key, kind, now)  # unbound keys still count as held or repeating
        if not admitted:
            return
        if kind == CONTROL:
            press_only = session.controls.press_fire and action == "fire" and key.kind == PRESS
            if press_only and not self.gate.certain:
                edge = self.gate.repeat.press  # every tap, never a repeat; None: maybe a held Space's first repeat
            elif action == "fire" and key.kind == PRESS and self.gate.repeat.remote:
                edge = True  # SSH slotting and jitter make repeat classification unreliable (6.6)
            if action in MOVES and key.kind == PRESS and not self.gate.certain and self.gate.repeat.retap:
                edge = True  # a re-tap reverses, though it came at the learned delay: a first repeat only reverses
            if action in MOVES and key.kind == PRESS and not self.gate.certain and _choosing(session):
                edge = self.gate.repeat.press is True  # on the card every tap moves (16.2), quick ones too
            session.controls.feed(action, key, edge, now)
            if edge is None:
                session.controls.defer_fire(now + self.gate.repeat.hold_gap)
        elif kind == PULSE:
            session.controls.pulse(action)
        else:
            session.command(action, now)


def _choosing(session) -> bool:
    """The active game is on the campaign's upgrade card or continue prompt (an engine phase, not the scene: Arcade
    play has press_fire too)."""
    pilot = getattr(session, "pilot", None)
    return getattr(pilot and pilot.game, "phase", None) in CHOICES


class Controls:
    """What every scheme shares: pulses go out once, and the fire buffer, which a shot empties. press_fire is the
    active game's fire rule, set by the Session."""

    press_fire = False

    def pulse(self, action: str) -> None:
        self.pending.add(action)

    def fired(self) -> None:
        self.fire_buffer = 0

    def unlatch(self) -> None:
        """Forget the direction a tap set, so the next tap of either way is a new edge (the Session calls it on the
        upgrade card and the continue prompt, spec 16.2). Fire is untouched. Hold controls have nothing to forget: a
        key moves only while it is down, and its release ends it."""

    def actions(self, move: str | None, fire: bool) -> tuple[str, ...]:
        """This tick's actions; the fire buffer counts down."""
        out = self.pending | ({move} if move else set()) | ({"fire"} if fire else set())
        self.fire_buffer = max(0, self.fire_buffer - 1)
        self.pending = set()
        return tuple(sorted(out))


class HoldControls(Controls):
    """Kitty: true key-down and key-up. With both directions down the latest press wins, and a press and release
    inside one tick still moves once and fires once."""

    scheme, fire_repeats, note = "hold", True, ""

    def __init__(self):
        self.clear()

    def clear(self) -> None:
        self.held: dict[str, tuple[str, int]] = {}  # key name -> (action, press number)
        self.press_seq: dict[str, int] = {}
        self.edges: set[str] = set()
        self.pending: set[str] = set()
        self.fire_buffer, self.seq = 0, 0

    def feed(self, action: str, key, edge: bool, now: float) -> None:
        if key.kind == RELEASE:
            self.held.pop(key.name, None)
            return
        if key.kind == REPEAT and key.name in self.held:
            return
        if action == "stop":
            self.held = {name: entry for name, entry in self.held.items() if entry[0] not in MOVES}
            self.edges -= MOVES.keys()
            return
        self.seq += 1  # a press, or a repeat of a key held since before tracking began
        self.held[key.name] = (action, self.seq)
        self.press_seq[action] = self.seq
        self.edges.add(action)
        if action == "fire":
            self.fire_buffer = FIRE_BUFFER_TICKS

    def sample(self, now: float) -> tuple[str, ...]:
        moves = {action for action, _ in self.held.values() if action in MOVES} | (self.edges & MOVES.keys())
        move = max(moves, key=self.press_seq.__getitem__) if moves else None
        fire = ("fire" in self.edges if self.press_fire else self.fire_buffer > 0) or any(
            action == "fire" for action, _ in self.held.values()
        )
        self.edges.clear()
        return self.actions(move, fire)


class TapFire(Controls):
    """Fire without release events. Each fire event arms the fire buffer, so holding Space fires through its OS
    repeats. The OS repeats only the latest key, so a key pressed while Space repeats takes the repeats but not the
    hold: fire stays on until Space comes again (a tap then fires once) or clear() (a pause, a scene change).
    Space was repeating if its last two events, and then the other key, each came within held_gap (HELD_FIRE_GAP,
    wider over SSH): a slower OS repeat, or a key pressed before Space's first repeat, lets the fire stop.

    With press_fire only presses fire, one tick each; a Space that may be a held key's first repeat waits for
    defer_fire's time, and fires then unless a repeat came first."""

    def __init__(self, *, remote: bool = False, note: str = ""):
        self.held_gap = HELD_FIRE_GAP[int(remote)]
        self.note = note
        self.clear()

    def clear(self) -> None:
        self.fire_buffer, self.pending, self.now = 0, set(), 0.0
        self.fire_last, self.streaming, self.held_fire = -math.inf, False, False
        self.fire_due: float | None = None

    def pulse(self, action: str) -> None:
        super().pulse(action)
        self.took_repeat(self.now)  # B or F takes the repeats too; now is at most a tick old

    def feed_fire(self, edge: bool | None, now: float) -> None:
        self.streaming = now - self.fire_last <= self.held_gap
        self.fire_last, self.held_fire, self.now = now, False, now
        if self.press_fire:
            self.settle(now)
            self.fire_due = None  # a Space this soon after a deferred one: that one was a held key's first repeat
            if edge:
                self.fire_buffer = 1  # one tick of fire per press
        elif edge or self.fire_repeats:
            self.fire_buffer = FIRE_BUFFER_TICKS

    def defer_fire(self, due: float) -> None:
        """The Space just fed may be a held key's first repeat: it fires at due if no repeat comes before."""
        self.fire_due = due

    def settle(self, now: float) -> None:
        if self.fire_due is not None and now >= self.fire_due:
            self.fire_due, self.fire_buffer = None, 1

    def took_repeat(self, now: float) -> None:
        """Another key's event: a Space that was repeating is still down (which press_fire never acts on)."""
        repeating = self.streaming and now - self.fire_last <= self.held_gap
        self.held_fire = self.fire_repeats and not self.press_fire and (self.held_fire or repeating)
        self.streaming, self.now = False, now

    def actions(self, move: str | None, fire: bool) -> tuple[str, ...]:
        return super().actions(move, fire or self.held_fire)


class LatchedControls(TapFire):
    """No release events: a tap latches a direction until a tap the other way or a stop. Fire events re-arm the
    fire buffer, so holding Space fires at the cooldown rate through OS repeats and each tap fires once."""

    scheme, fire_repeats = "latched", True

    def __init__(self, note: str = "", *, remote: bool = False):
        super().__init__(remote=remote, note=note)

    def clear(self) -> None:
        super().clear()
        self.direction = 0

    def feed(self, action: str, key, edge: bool, now: float) -> None:
        if key.kind == RELEASE:
            return
        if action == "fire":
            self.feed_fire(edge, now)
            return
        self.took_repeat(now)
        if action in MOVES:
            step = MOVES[action]
            if not edge:
                return
            if self.direction == -step:
                self.direction = 0  # tap the opposite way: stop
            elif self.direction == 0:
                self.direction = step
        elif action == "stop":
            self.direction = 0

    def unlatch(self) -> None:
        """Drop the latched direction as a stop would, without touching fire."""
        self.direction = 0

    def sample(self, now: float) -> tuple[str, ...]:
        self.now = now
        self.settle(now)
        return self.actions(DIRECTIONS[self.direction], self.fire_buffer > 0)


class TimedControls(TapFire):
    """--controls timed (design A's latch with hold inference): a tap latches a direction, and fast repeats of the
    moving key mark a hold, so the ship stops when they stop, unless another key took over the OS repeat (the
    player may still be holding, so the direction stays latched). It can surprise when you fire while moving."""

    scheme, fire_repeats = "timed", True

    def __init__(self, *, remote: bool = False, note: str = ""):
        self.repeat = RepeatModel(remote=remote)  # learns the burst gap from these keys' own repeats
        self.last: dict[str, float] = {}
        super().__init__(remote=remote, note=note)

    def clear(self) -> None:
        super().clear()
        self.direction, self.held, self.held_key, self.last_move = 0, False, "", 0.0

    def pulse(self, action: str) -> None:
        super().pulse(action)
        self.held = False  # B or F takes the OS repeat from the moving key: keep the direction latched, as Space does

    def feed(self, action: str, key, edge: bool, now: float) -> None:
        if key.kind == RELEASE:
            return
        name = key.name
        gap = now - self.last.get(name, -math.inf)
        self.last[name] = now
        self.repeat.observe(name, now)
        quick = gap <= self.repeat.burst_gap
        if action == "fire":
            self.feed_fire(edge if self.press_fire else True, now)  # every event re-arms, unless fire is press only
        else:
            self.took_repeat(now)
        if action in MOVES:
            step = MOVES[action]
            if step == self.direction:
                if quick:
                    self.held, self.held_key = True, name
                self.last_move = now
            elif self.direction == 0 or self.held:  # a start, or a hold rolled over to the other way
                self.direction, self.held, self.last_move = step, False, now
            else:
                self.direction = 0  # tapped the opposite way: stop
            return
        if self.held and name != self.held_key:
            self.held = False  # the OS now repeats this key instead; keep the direction latched
        if action == "stop":
            self.direction, self.held = 0, False

    def unlatch(self) -> None:
        """End the direction's hold window, as sample() does when a hold's repeats stop; fire is untouched. A key
        still held starts a new latch with its next OS repeat, as a held arrow repeats in the menu."""
        self.direction, self.held = 0, False

    def sample(self, now: float) -> tuple[str, ...]:
        self.now = now
        self.settle(now)
        if self.held and now - self.last_move > self.repeat.burst_gap:
            self.direction, self.held = 0, False  # the hold ended
        return self.actions(DIRECTIONS[self.direction], self.fire_buffer > 0)


def make_controls(caps, wanted: str = "auto", keymap=None):
    """--controls auto: hold where the terminal reports releases, latched elsewhere; hold falls back with a note."""
    note = PHYSICAL_NOTE if keymap is not None and keymap.has_physical and not caps.kitty_flags & 4 else ""
    if wanted == "timed":
        return TimedControls(remote=caps.remote, note=note)
    if wanted == "latched" or not caps.release_events:
        return LatchedControls(HOLD_NOTE if wanted == "hold" else note, remote=caps.remote)
    controls = HoldControls()
    controls.note = note
    return controls


class VirtualInput:
    """Physical keys pressed and released over time -> the timed bytes a terminal sends for them: kitty events, or
    legacy presses with the profile's OS auto-repeat; over ssh, delivered with delay, jitter and keystroke slotting.

    Times only move forward: a press or release before the last bytes_until() happens at its end."""

    def __init__(self, protocol: str, profile: simkeys.RepeatProfile, *, ssh: simkeys.Link | None = None, rng=None):
        if ssh is not None and rng is None:
            raise ValueError("an ssh link needs an rng for its jitter")
        self.kitty, self.profile, self.link, self.rng = protocol == "kitty", profile, ssh, rng
        self.holds: list[list] = []  # [name, down, up]; up is inf while the key is down
        self.sent = -math.inf  # every event before this time has been encoded
        self.last = -math.inf  # the link's latest arrival: delivery keeps the order
        self.queue: list[tuple[float, bytes]] = []  # encoded, still on the way

    def press(self, name: str, t: float) -> None:
        self.holds.append([name, max(t, self.sent), math.inf])

    def release(self, name: str, t: float) -> None:
        """A release before a press still to come cancels that press."""
        t = max(t, self.sent)
        for hold in self.holds:
            if hold[0] == name and hold[2] == math.inf:
                hold[2] = t
        self.holds = [hold for hold in self.holds if hold[2] >= hold[1]]

    def tap(self, name: str, t: float, dur: float = 0.06) -> None:
        self.press(name, t)
        self.release(name, t + dur)

    def down(self, name: str) -> bool:
        return any(hold[0] == name and hold[2] == math.inf for hold in self.holds)

    def bytes_until(self, t: float) -> list[tuple[float, bytes]]:
        """(arrival, bytes) for everything that arrives before t, in order."""
        holds = [(name, down, min(up, t)) for name, down, up in self.holds]
        events = [key for key in simkeys.key_events(holds, self.profile, kitty=self.kitty) if self.sent <= key.time < t]
        sent = [(key.time, data) for key in events if (data := simkeys.encode(key, kitty=self.kitty))]
        self.sent = t
        # the first key pressed after one still down ended its auto-repeat for good: keep it while that key is down
        ordered = sorted(self.holds, key=lambda hold: hold[1])
        keep = {id(hold) for hold in ordered if hold[2] >= t}
        keep |= {id(ordered[n + 1]) for n, hold in enumerate(ordered[:-1]) if hold[2] >= t}
        self.holds = [hold for hold in self.holds if id(hold) in keep]
        for arrival, data in self.link.deliver(sent, self.rng) if self.link else sent:
            self.last = max(self.last, arrival)
            self.queue.append((self.last, data))
        due = [item for item in self.queue if item[0] < t]
        self.queue = self.queue[len(due) :]
        return due


class IntentDriver:
    """A bot's intents per tick, keyed as a person would for the scheme: hold and timed hold keys down; latched
    taps a direction, taps S to stop, and taps the other way twice to reverse. Fire holds Space, in every scheme.
    Without release events a key pressed before Space has begun its OS repeats leaves nothing that says Space is
    down, so the shots stop: then, in latched and timed alike, the bot presses Space again, as a player would."""

    KEYS = {"left": "a", "right": "d", "stop": "s", "fire": "space", "bomb": "b", "autofire": "f"}
    TWICE = 0.12  # the second tap of a reversal: above MIN_RETAP even with 30 ms of jitter
    SETTLE = 0.20  # latched taps closer than this could read as a bounce or an OS repeat

    def __init__(self, scheme: str, vin: VirtualInput):
        self.scheme, self.vin = scheme, vin
        self.direction, self.firing, self.ready = 0, False, -math.inf
        self.fire_down = -math.inf  # when Space was last pressed

    def intend(self, tick: int, held: frozenset[str]) -> None:
        """held: the subset of {left, right, fire} the bot wants this tick."""
        t = tick / TICK_RATE
        if ("fire" in held) != self.firing:  # first, so a tap this tick presses Space again after it
            self.firing = not self.firing
            (self.vin.press if self.firing else self.vin.release)(self.KEYS["fire"], t)
            self.fire_down = t
        direction = ("right" in held) - ("left" in held)
        if direction != self.direction and (self.scheme != "latched" or t >= self.ready):
            self._move(direction, t)

    def pulse(self, action: str, tick: int) -> None:
        """bomb or autofire: one tap."""
        self._taps([self.KEYS[action]], tick / TICK_RATE)

    def _move(self, direction: int, t: float) -> None:
        old, self.direction = self.direction, direction
        names = {-1: self.KEYS["left"], 1: self.KEYS["right"]}
        if self.scheme != "latched":
            if old:
                self.vin.release(names[old], t)
            if direction:
                self.vin.press(names[direction], t)
                self._refire(t, t + 0.01)
            return
        taps = [names[direction]] * (2 if old == -direction else 1) if direction else [self.KEYS["stop"]]
        self.ready = self._taps(taps, t) + self.SETTLE

    def _taps(self, names: list[str], t: float) -> float:
        """Tap the keys TWICE apart; returns when the last tap ends."""
        for n, name in enumerate(names):
            self.vin.tap(name, t + n * self.TWICE)
        end = t + (len(names) - 1) * self.TWICE + 0.06
        self._refire(t, end + 0.01)
        return end

    def _refire(self, pressed: float, again: float) -> None:
        """A key pressed at `pressed` took the OS repeats from Space: press Space again at `again` if they had not
        begun (a legacy terminal then keeps firing while Space stays down)."""
        if self.firing and self.scheme != "hold" and pressed < self.fire_down + self.vin.profile.delay:
            self.vin.release(self.KEYS["fire"], pressed)
            self.vin.press(self.KEYS["fire"], again)
            self.fire_down = again
