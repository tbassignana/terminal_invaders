"""Phase 5's first-run tips: the Coach that teaches a run as it is played, one tip at a time (spec 7).

The Coach reads the game (player, shots, pickups, aliens, hazards, bunkers, shelter_rev, bomb_charge, wave, phase,
notes, powerups, tick) and the actions the controls produced, after each step, and never writes anything; it counts
game ticks, never a clock, so a tip comes at the same game moment at any frame rate and after a pause. It keeps no
reference to a game: a new run is told by the game's id.

The Painter asks it for the tip to show on the block's bottom row, which only takes the row while the game's own
notices and timers leave it free and the tip fits beside the upgrade codes (notices > timers > tip > codes, spec
7.2). While a tip cannot be drawn its time stands still.

How a tip is scheduled (spec 7.2):

* Candidates are the tips not yet taught whose trigger is current; the most urgent (TIPS order) goes first.
* A tip shows at least 2 s (MIN_TICKS), then until it is done or its time runs out, then the row stays empty 1 s.
  One done sooner (three shots in its first second) stays until its 2 s are up.
* A tip picked while the row is busy waits, its time stopped; if its trigger ends before it is ever drawn (a tell
  over while a pickup timer had the row), it is not shown late: it re-arms for the next time.
* A tip about something on screen (SUBJECT) shows only while that thing is drawn: when it goes, a tip that had its
  2 s is done, and one that had less re-arms for the next time. So a k = 1 text names a glyph only while that glyph
  is drawn (spec 15.2 R7).
* A showing Move or Fire tip yields, once it had its 2 s, to any tip ranked above HUD, and comes back after.
* A tip whose done condition was met before it ever showed (the ship already moved both ways, P already pressed)
  is done without showing.

The Telegraph tip's tell is a shooter's tell pose (sprites.TELLS). A diver's or a mite's wobble before it dives is
the same game state, but they never fire. Pellets come with no tell at all, so in Campaign the tip first shows at
wave 5's gunners.
"""

from __future__ import annotations

from typing import NamedTuple

from .arena import marks
from .campaign.observe import charge_needed
from .keymap import Keymap, label
from .model import HEIGHT, TICK_RATE
from .sprites import TELLS, cell

# The tips by name, most urgent first (spec 7.2): Telegraph, Marks, Shield, Ships, Bomb, then Shelters, Pickup,
# Cargo, then HUD, Meter, Pause, then Move and Fire.
TIPS = ("telegraph", "marks", "shield", "ships", "bomb", "shelters", "pickup", "cargo", "hud", "meter", "pause")
TIPS += ("move", "fire")
WIDTH = 58  # the most a tip's line may take, TIP and its two spaces included
PREFIX = "TIP  "
MIN_TICKS = 2 * TICK_RATE  # a tip shows at least this long
GAP_TICKS = TICK_RATE  # the empty row after a tip
PAUSE_AFTER = 45 * TICK_RATE  # play before the Pause tip, if no wave cleared first
STRUCK = TICK_RATE  # a shelter struck this recently makes the Shelters tip current
BUSY = 4  # enemy shots in the air for the Bomb tip
CAMPAIGN_RULES = ("classic", "endless", "daily")
SECONDS = {  # each tip's longest showing (spec 7.3's figures; Pickup, caught or gone, is capped at 6 s)
    "move": 6,
    "fire": 6,
    "hud": 5,
    "meter": 5,
    "shield": 4,
    "telegraph": 4,
    "marks": 4,
    "shelters": 4,
    "ships": 4,
    "bomb": 6,
    "pause": 5,
    "pickup": 6,
    "cargo": 5,
}
# Tips about something on screen: each shows only while it is drawn (the rest, once begun, run to their end).
SUBJECT = frozenset({"marks", "shield", "ships", "bomb", "shelters", "pickup", "cargo", "meter"})
YIELDING = ("move", "fire")  # they give way to any tip ranked above HUD once they had their 2 s
URGENT = TIPS[: TIPS.index("hud")]
DEFAULT_KEYS = Keymap.preset("default").contexts
# The texts for the default keys (spec 7.3); other keymaps build them from their first bindings.
MOVES = {
    "hold": "Hold A or D (or the arrows) to move",
    "latched": "Tap A or D to start; tap back or S to stop",
    "timed": "Press A or D to move; S stops",
}
TEXTS = {
    "hud": "GUN: shots ready to fire. BOMBS: bombs you hold",
    "meter": "BOMB %: hits in a row fill it; 100% earns a bomb",
    "telegraph": "An invader that changes shape is about to fire",
    "ships": "^ next to LIVES are your ships. Lose them all: over",
    "pickup": "Catch the falling letter for a power-up",
}
GLYPH_TEXTS = {  # k = 1 (the glyph arena) first, then k >= 2 (pixel art, which has no such glyphs)
    "shield": (
        "The { } around your ship is a shield: one hit",
        "The cap over your ship is a shield: it takes one hit",
    ),
    "beam": ("' and ^ show where a beam will strike: move away", "Dots and chevrons show where a beam will strike"),
    "gap": ("Stand between [ and ] to stay safe", "Stand between the ground brackets to stay safe"),
    "shelters": ("Shelters # stop shots, theirs and yours", "The blocks above you stop shots, theirs and yours"),
    "cargo": ("$ ships carry a power-up: shoot them", "Marked ships carry a power-up: shoot them"),
}


class View(NamedTuple):
    """What the Painter's latest frame told the Coach: the scheme in use, the arena's scale and the bottom row's
    free columns."""

    scheme: str
    k: int
    room: int


def wanted(options, profile, pilots: int, config) -> bool:
    """Whether a run gets the Coach (spec 7.1): TIPS `always`, or `auto` while the profile's settings hold no
    tips_done: true (a profile without a settings attribute, such as a test stub or tools/balance.py's Offline,
    never under `auto`); `off` never; and only a single-pilot run (pilots == 1) on Campaign rules (config.mode
    classic, endless or daily), never a duel, Arcade, a replay or the demo (the Session never asks for those).
    Options without the attribute read off."""
    tips = getattr(options, "tips", "off")
    if tips not in ("auto", "always") or pilots != 1 or getattr(config, "mode", None) not in CAMPAIGN_RULES:
        return False
    if tips == "always":
        return True
    saved = getattr(profile, "settings", None)
    return isinstance(saved, dict) and saved.get("tips_done") is not True


def row_free(game, reduced_motion: bool) -> bool:
    """Whether the bottom row has room for a tip: no notice of the game's (nor SAFE under reduced motion) and no
    pickup timer (spec 7.2). The upgrade codes come after a tip, so they leave it room; the Painter checks the fit."""
    if any(ticks > 0 for _, ticks in game.notes):
        return False
    if reduced_motion and game.player.invulnerable > 0:
        return False
    return not any(ticks > 0 for ticks in game.powerups.values())


def on_field(sprite) -> bool:
    """Whether a sprite's row is one of the field's (0 to HEIGHT - 1), where the arena draws it: an enemy entering
    from above the field is in the game before it is on screen."""
    return 0 <= cell(sprite.y) < HEIGHT


def fitted(*choices: str) -> str:
    """The first of the texts that fits WIDTH with its TIP prefix, else the last cut to it at a word."""
    for text in choices:
        line = PREFIX + text
        if len(line) <= WIDTH:
            return line
    line = PREFIX + choices[-1]
    cut = line[: WIDTH + 1]
    return cut.rsplit(" ", 1)[0] if " " in cut[len(PREFIX) :] else line[:WIDTH]


class Coach:
    """A run's tips: their triggers, how long each shows and when it is done (spec 7.2-7.3). Session.start() attaches
    the Session's one Coach to a run when wanted() says so; a taught tip is not taught twice in the session.

    done: the names (from TIPS) of the tips taught so far."""

    def __init__(self, keymap, options):
        self.keymap, self.options = keymap, options
        self.done: set[str] = set()
        self.view: View | None = None
        self.run = None  # the id of the game being observed: another one is a new run
        self.fresh_run()

    def fresh_run(self) -> None:
        """What the triggers count, for a new run (the tips taught stay taught)."""
        self.moved: set[str] = set()
        self.fired = 0
        self.played = 0  # ticks of wave play
        self.cleared = self.damaged = self.bombed = self.caught = self.paused = self.empty = False
        self.rev: int | None = None
        self.struck: int | None = None  # the game tick a shelter was last struck
        self.active: str | None = None  # the tip showing, or waiting for room on the row
        self.variant = ""  # the Marks tip's mark: beam or gap
        self.shown = 0  # ticks the active tip has been drawn
        self.gap = 0  # ticks the row stays empty after a tip
        self.visible = False  # whether the active tip could be drawn at the latest tick
        self.yielded: dict[str, int] = {}  # a Move or Fire tip that gave way, and the ticks it had shown

    @property
    def k(self) -> int:
        return self.view.k if self.view else 1

    def observe(self, game, actions, events: tuple) -> None:
        """One game tick seen (spec 7.1): Session.step calls it after game.step, controls.fired() and
        audio.update(), with that tick's actions and a copy of game.events."""
        if id(game) != self.run:
            self.run = id(game)
            self.fresh_run()
        self.track(game, actions, events)
        if game.phase != "playing":  # the intro, the clear, the card, the prompt: tips hold
            self.visible = False
            return
        self.played += 1
        for name in TIPS:
            if name not in self.done and name != self.active and self.finished(name):
                self.done.add(name)  # learned before it was shown: never shown
        current = [name for name in TIPS if name not in self.done and self.current(name, game)]
        if self.active is not None:
            name = self.active
            if self.finished(name) and (self.shown >= MIN_TICKS or self.shown == 0):
                self.end(True)  # done once it had its 2 s (SC-3); done unseen when learned before it was drawn
            elif name in SUBJECT and not self.holds(name, game):
                self.end(self.shown >= MIN_TICKS)
            elif self.shown == 0 and name not in current:
                self.end(False)  # its trigger ended before it was ever drawn: not shown late, it re-arms (SC-2)
            elif name in YIELDING and self.shown >= MIN_TICKS and any(tip in URGENT for tip in current):
                self.yielded[name] = self.shown
                self.end(None)
        if self.active is None and self.gap == 0 and current:
            pick = current[0]
            self.active, self.shown = pick, self.yielded.pop(pick, 0)
            if pick == "marks":
                self.variant = "beam" if self.mark("beam", game) else "gap"
        self.visible = self.active is not None and self.drawable(game)
        if self.visible:
            self.shown += 1
            if self.shown >= SECONDS[self.active] * TICK_RATE:
                self.end(True)
        elif self.active is None and self.gap > 0:
            self.gap -= 1

    def end(self, done: bool | None) -> None:
        """The active tip leaves the row: done (True), re-armed (False) or yielded (None); a tip that showed leaves
        the row empty for GAP_TICKS, except one that yields, whose row the urgent tip takes at once (a tell lasts
        a fraction of a second)."""
        if done:
            self.done.add(self.active)
        if done is not None and (self.shown > 0 or done):
            self.gap = GAP_TICKS
        self.active, self.variant, self.shown, self.visible = None, "", 0, False

    def on_pause(self) -> None:
        """The player paused by hand (Session.pause(MANUAL)): the Pause tip is done."""
        self.paused = True
        if self.active == "pause":
            self.end(True)
        else:
            self.done.add("pause")

    def tip(self, scheme: str, k: int, room: int) -> str | None:
        """The bottom row's tip this frame, `TIP  ...` in at most min(room, WIDTH) columns, with the keys of the
        scheme in use (hold, latched, timed) and the text for the arena's scale k (glyphs at 1, pixel art from 2);
        None when no tip shows. A frame at a scale other than the last one's shows none: the next tick picks again
        by this scale's rules."""
        moved = self.view is not None and self.view.k != k
        self.view = View(scheme, k, room)
        if moved or self.active is None or not self.visible:
            return None
        line = self.text(self.active, scheme, k)
        return line if len(line) <= min(room, WIDTH) else None

    # --- what each tip watches ---------------------------------------------------------------------------------------

    def track(self, game, actions, events: tuple) -> None:
        """The run's counts from this tick: directions asked, shots, a cleared wave, a ship lost, a bomb, a catch,
        a shelter struck, an empty gun."""
        self.moved |= {action for action in actions if action in ("left", "right")}
        self.fired += events.count("fire")
        self.cleared |= "clear" in events
        self.damaged |= "damage" in events
        self.bombed |= "bomb" in events
        self.caught |= "pickup" in events
        rev = game.shelter_rev
        if self.rev is not None and rev != self.rev and game.phase == "playing" and "pickup" not in events:
            self.struck = game.tick  # a shot hit a shelter (a REPAIR pickup and a new wave's rebuild bump it too)
        self.rev = rev
        if game.phase == "playing":
            busy = len({shot.volley for shot in game.shots if shot.friendly})
            self.empty |= busy >= game.player.weapon > 0

    def finished(self, name: str) -> bool:
        """The tip's done condition, other than its time: what it teaches was done."""
        if name == "move":
            return {"left", "right"} <= self.moved
        if name == "fire":
            return self.fired >= 3
        if name == "bomb":
            return self.bombed
        if name == "pause":
            return self.paused
        if name == "pickup":
            return self.caught
        return False

    def current(self, name: str, game) -> bool:
        """Whether the tip's trigger holds this tick (spec 7.3)."""
        player = game.player
        if name == "move":
            return True  # wave play has begun
        if name == "fire":
            return "move" in self.done
        if name == "hud":
            return game.wave == 1 and "fire" in self.done or self.empty
        if name == "meter":
            return "hud" in self.done and self.holds(name, game)
        if name == "telegraph":  # a shooter in its tell pose; a diver or mite wobbling before a dive fires nothing
            return any(alien.state == "telegraph" and alien.kind in TELLS for alien in game.aliens)
        if name == "marks":
            return self.mark("beam", game) or self.mark("gap", game)
        if name == "shelters":
            return self.struck is not None and game.tick - self.struck <= STRUCK and self.holds(name, game)
        if name == "ships":
            return self.damaged and self.holds(name, game)
        if name == "bomb":
            return player.bombs > 0 and sum(1 for shot in game.shots if not shot.friendly) >= BUSY
        if name == "pause":
            return self.cleared or self.played >= PAUSE_AFTER
        if name in ("shield", "pickup", "cargo"):
            return self.holds(name, game)
        raise ValueError(f"Unknown tip: {name}")

    def holds(self, name: str, game) -> bool:
        """Whether what a SUBJECT tip describes is still drawn: at k = 1 the very glyph its text names."""
        player = game.player
        if name == "marks":
            return self.mark(self.variant or "beam", game)
        if name == "shield":  # the shielded ship; an invulnerable ship blinks out unless reduced motion holds it
            return player.guard > 0 and (not player.invulnerable or getattr(self.options, "reduced_motion", False))
        if name == "shelters":  # k = 1 names the solid glyph #; a cracked cell draws :
            return any(hp > 1 for hp in game.bunkers.values()) if self.k == 1 else bool(game.bunkers)
        if name == "ships":
            return player.lives >= 1
        if name == "bomb":
            return player.bombs > 0
        if name == "meter":
            return 0 < game.bomb_charge < charge_needed(game)
        if name == "pickup":
            return bool(game.pickups)
        if name == "cargo":  # a cargo ship still entering from above the field is not drawn yet
            return any(alien.cargo and alien.state != "telegraph" and on_field(alien) for alien in game.aliens)
        return True

    def mark(self, variant: str, game) -> bool:
        """Whether a beam's (or a gap's or a lane's) mark is drawn: at k = 1 both glyphs the text names."""
        if self.k == 1:
            drawn = {key for _, _, key in marks(game.hazards, game.bunkers, 2)}
            needed = {"beam_warn", "lane_mark"} if variant == "beam" else {"gap_open", "gap_close"}
            return needed <= drawn
        kinds = ("beam",) if variant == "beam" else ("gap", "lane")
        return any(hazard.kind in kinds and hazard.state != "live" for hazard in game.hazards)

    def drawable(self, game) -> bool:
        """Whether the active tip can take the row now: a free row and a line that fits the room the latest frame
        left (before any frame, the whole row)."""
        if not row_free(game, getattr(self.options, "reduced_motion", False)):
            return False
        view = self.view or View("latched", 1, WIDTH)
        return len(self.text(self.active, view.scheme, view.k)) <= min(view.room, WIDTH)

    # --- the words -------------------------------------------------------------------------------------------------

    def key(self, context: str, action: str) -> str:
        specs = self.keymap.keys_for(context, action)
        return label(specs[0]) if specs else "?"

    def text(self, name: str, scheme: str, k: int) -> str:
        """The tip's line, TIP included, for the scheme and scale, with this keymap's keys (spec 7.3)."""
        key = self.key
        if name == "move":
            if self.keymap.contexts == DEFAULT_KEYS:
                return fitted(MOVES.get(scheme, MOVES["latched"]))
            left, right, stop = key("play", "left"), key("play", "right"), key("play", "stop")
            built = {
                "hold": f"Hold {left} or {right} to move",
                "timed": f"Press {left} or {right} to move; {stop} stops",
            }
            short = f"{left}/{right} move" + ("" if scheme == "hold" else f", {stop} stops")
            return fitted(built.get(scheme, f"Tap {left} or {right} to start; tap back or {stop} to stop"), short)
        if name == "fire":
            fire = key("play", "fire")
            return fitted(f"{fire} fires. Hold it to keep firing", f"{fire} fires")
        if name == "bomb":
            bomb = key("play", "bomb")
            return fitted(f"{bomb} drops a bomb: it clears enemy fire", f"{bomb} drops a bomb")
        if name == "pause":
            pause, leave = key("play", "pause"), key("pause", "menu")
            return fitted(f"{pause} pauses. {leave} in the pause menu leaves the run", f"{pause} pauses")
        if name == "marks":
            return fitted(GLYPH_TEXTS[self.variant or "beam"][0 if k == 1 else 1])
        if name in GLYPH_TEXTS:
            return fitted(GLYPH_TEXTS[name][0 if k == 1 else 1])
        return fitted(TEXTS[name])
