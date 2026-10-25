"""ArcadeGame: the 1978 program's game part, ported routine by routine onto a model of its own memory.

The port runs on the original's CPU-cycle time. The interrupts' routines count the cycles of the instructions they
execute, and the main thread gets the rest of each gap between two interrupts (128 lines from the mid-screen one to
the end-screen one, 134 back). Outside the game loop (the round's start and end, the player's blow-up, the invasion,
game over) the main thread is a generator that yields where an interrupt arrives: its flows spend their
instructions' cycles one by one, or, with the game tasks off, a routine's whole running time with each byte it writes
landing when its writing instruction starts (`_paced`). In the game loop, and in the routines a flow runs while the
interrupts run the game objects (`_counted`), every routine counts its cycles as it goes (`_tick`) and makes each
memory and port access at the start of the instruction that makes it, so an interrupt is taken exactly where it falls
due inside them. One step() is one video frame: it ends right before the mid-screen interrupt is vectored. When that
point falls inside a game-loop pass or a counted routine (it nearly always does), the run is cut there, and the next
step() runs it again from its start up to the same point, where the frame's controls and any changes made to memory
between the frames (by a test or a comparison tool) come in, and on.

Two things are not timed instruction by instruction; both are exact all the same:
* A paced routine reads memory at its start (its writes land at their times), and `_spend` takes an interrupt at its
  due time rather than at the end of the instruction then running. The flows run routines paced only with the game
  tasks off, when the interrupts write nothing a paced routine reads (vblankStatus, isrDelay and coinSwitch only) and
  never reset the stack, so neither shows. A change made to memory between frames reaches a paced routine's reads
  only if it is made before the routine starts.
* PromptPlayer's blink runs one pass of its wait loop after each interrupt, and the main thread's time from the
  interrupt that ends it to the next CALL is a constant measured on the original (`_PROMPT_END`): nothing in the
  prompt varies from game to game.
"""

import hashlib
from bisect import bisect_left
from collections import deque
from operator import attrgetter

from .image import build_image
from .memory import (
    A_SHOT_RELOAD_RATE,
    ALIEN_SHOT_DELTA,
    CHANGE_FLEET_SND,
    CLEAR_BYTE,
    CLEAR_COLUMN,
    CLEAR_START,
    CLEAR_TIME,
    COIN_SWITCH,
    EXTRA_HOLD,
    FIRE_BOUNCE,
    FLEET_SND_CNT,
    FLEET_SND_HOLD,
    FLEET_SND_RELOAD,
    GAME_MODE,
    HI_SCOR_L,
    HI_SCOR_LO_L,
    HI_SCOR_M,
    INP1,
    INP2,
    ISR_DELAY,
    ISR_SPLASH_TASK,
    NEVER,
    NUM_ALIENS,
    NUM_COINS,
    OBJ0_TIMER_LSB,
    OBJ0_TIMER_MSB,
    OBJ1_TIMER_MSB,
    OBJ2_TIMER_EXTRA,
    P1_RACK_CNT,
    P1_REF_ALIEN_Y,
    P1_SCOR_L,
    P1_SCOR_M,
    P1_SHIPS_REM,
    P2_RACK_CNT,
    P2_REF_ALIEN_YR,
    P2_SCOR_L,
    P2_SHIPS_REM,
    PLAYER1_ALIVE,
    PLAYER1_EX,
    PLAYER2_ALIVE,
    PLAYER_ALIVE,
    PLAYER_DATA_MSB,
    PLAYER_OK,
    PLYR_SHOT_STATUS,
    REF_ALIEN_YR,
    SAUCER_ACTIVE,
    SAUCER_HIT,
    SAUCER_START,
    SHOT_SYNC,
    SOUND1,
    SOUND2,
    SOUND_PORT5,
    SUSPEND_PLAY,
    TILL_SAUCER_LSB,
    TWO_PLAYERS,
    VBLANK_STATUS,
    WAIT_START_LOOP,
    Memory,
)
from .objects import Objects
from .program import Jump, bcd_add, rlc
from .rack import Rack

RULESET = "arcade-1978-1"

_INPUT_BITS = {"fire": 0x10, "left": 0x20, "right": 0x40}  # IN1 for player 1; 0x09 (no coin, bit 3) is always set
_PORT3_EVENTS = ((0x01, "saucer"), (0x02, "fire"), (0x04, "blast"), (0x08, "kill"), (0x10, "life"))
_PORT5_EVENTS = ((0x01, "march1"), (0x02, "march2"), (0x04, "march3"), (0x08, "march4"), (0x10, "saucer_hit"))
_AMPLIFIER = 0x20  # port 3 bit 5: while it is off the cabinet is silent, both ports' sounds included
# Sounds the cabinet holds while their bit stays on, sent again every so many frames after the rise: the saucer's
# SN76477 warbles at 7.2 Hz (its SLF), the saucer hit's op-amp VCO at 6.1 Hz.
_REPEATS = {"saucer": 8, "saucer_hit": 10}

# CPU cycles from one interrupt's due time to the next: 128 lines of 128 cycles from the mid-screen one (line 96) to
# the end-screen one (line 224), 134 lines from there round to line 96 of the next frame.
_MID_GAP, _END_GAP = 128 * 128, 134 * 128
# Main-thread time from the due time of the end-screen interrupt that ends PromptPlayer's wait to the CALL after it,
# measured on the original (the same in every game: the prompt starts the same way and nothing in it varies): the
# 2 cycles of the wait loop's instruction that the interrupt waits for, then 1078 after the interrupt.
_PROMPT_END = 2 + 1078


class _Boundary(Exception):
    """The frame ends inside a counted run: the mid-screen interrupt is due before its next instruction."""


class _Abandon(Exception):
    """An interrupt taken inside a counted run reset the stack: the run ends and `flow` replaces the main thread."""

    def __init__(self, flow):
        super().__init__("run abandoned")
        self.flow = flow


class _Player:
    """`player.lives`: the ships digit the ROM prints at 2501, which counts the ship in play."""

    __slots__ = ("_game",)

    def __init__(self, game):
        self._game = game

    @property
    def lives(self) -> int:
        return self._game.text.get(0x2501, 0x1A) - 0x1A


def _label(name: str) -> str:
    """The ROM label of a ported routine's method name: sub_xxxx -> sub_XXXX, snake_case -> CamelCase."""
    if name.startswith("sub_"):
        return "sub_" + name[4:].upper()
    return _LABELS.get(name) or "".join(w[:1].upper() + w[1:] for w in name.split("_"))


_LABELS = {
    "copy_ram_mirror": "CopyRAMMirror",
    "game_loop": "sub_081F",
    "scan_line96": "ScanLine96",
    "scan_line224": "ScanLine224",
}
# The attributes besides memory, the text layer and the trail that a counted run can change (_save, _restore).
_SAVED = (
    "cycles",
    "_due",
    "_room",
    "_gaps",
    "_mid_next",
    "_in1",
    "shift_reg",
    "shift_amount",
    "sound_port3",
    "sound_port5",
    "kills",
    "hits",
    "shots_fired",
    "score_wraps",
)
_saved_values = attrgetter(*_SAVED)
_NOT_ROUTINES = frozenset({"init_memory", "rd", "wr", "rd16", "wr16", "out", "inp", "step", "result", "digest"})


class ArcadeGame(Objects, Rack, Memory):
    """A one-player credited game of the 1978 program, behind the interface the game modes share (step, result,
    digest, score, phase, events)."""

    def __init__(self, config, *, rom=None, trail=None):
        if config.mode != "arcade":
            raise ValueError(f"ArcadeGame plays mode 'arcade', not {config.mode!r}")
        self.config = config
        self.init_memory(build_image() if rom is None else rom)
        self.tick = 0
        self.phase = "playing"
        self.events: list[str] = []
        self.wave = 1
        self.kills = self.shots_fired = self.hits = 0
        self.score_wraps = 0
        self.player = _Player(self)
        self.trail = deque(maxlen=trail) if trail is not None else None
        if self.trail is not None:
            self._install_trail()
        self._handlers = self._handler_table()
        self._in1 = 0x09  # what IN 1 reads
        self._in1_next = 0x09  # the controls of the frame step() runs, read from its mid-screen interrupt on
        self._room = _MID_GAP  # main-thread cycles from the last interrupt to the next one
        self._busy = 0  # main-thread cycles run since the last interrupt
        self._carry = 0  # cycles of the instruction an interrupt waited for that ran past its due time
        self._gaps = 0  # interrupts taken
        self._mid_next = True  # the next interrupt is the mid-screen one
        self._split = None  # (RAM, VRAM) where the last step() ended, inside a counted run
        self._pokes = None  # while a cut run runs again up to its cut: what memory holds there, or () if unchanged
        self._power_on()
        self._wait_for_start()
        self._main = self._run(self.new_game())
        self._pos = next(self._main)
        # The interrupts taken while NewGame ran (game tasks off) leave these; the last one was the end-screen one.
        self.wr(VBLANK_STATUS, 0x80)
        self.wr(COIN_SWITCH, 1)
        self.port_writes.clear()
        self._port3, self._port5 = self.sound_port3, self.sound_port5
        self._since = dict.fromkeys(_REPEATS, 0)  # the tick of each repeated sound's last rise
        self.dirty = None

    # ---- the shared interface ----

    @property
    def score(self) -> int:
        """Player 1's score, with 10000 for every time the four BCD digits wrapped."""
        lo, hi = self.rd(P1_SCOR_L), self.rd(P1_SCOR_M)
        digits = (hi >> 4) * 1000 + (hi & 0x0F) * 100 + (lo >> 4) * 10 + (lo & 0x0F)
        return digits + 10000 * self.score_wraps

    @property
    def hi_score_addr(self) -> int:
        """The VRAM address where the HI-SCORE digits start."""
        return self.rd16(HI_SCOR_LO_L)

    @property
    def sound_ports(self) -> tuple[int, int]:
        """The values last written to sound ports 3 and 5."""
        return self.sound_port3, self.sound_port5

    def step(self, actions=()) -> None:
        """Run one frame with the controls in `actions` held ("left", "right", "fire"; others are ignored).

        The game also ends with a frame in which the program goes where the port does not follow it: a write where
        the stack lives (23D0-23FF), an object list or handler gone astray, demo play or the player switch reached,
        a counted run longer than a frame. A one-player game gets there only with its RAM overwritten, which the 1978
        program does itself once a lone invader's trail has lit the left edge column: the rack then turns back each
        time it moves left, creeps right past the right edge and wraps round into RAM, where the invader overwrites
        the program's variables and, as it reaches the stack, the return addresses there."""
        if self.phase != "playing":
            return
        bits = 0x09
        for name in actions:
            bits |= _INPUT_BITS.get(name, 0)
        self._in1_next = bits
        if self._split is None:
            self._in1 = bits
            self._interrupt()
        while True:
            self._main_run()
            if self.phase != "playing" or self._split is not None or self._mid_next:
                break
            self._interrupt()
        self.events = self._sound_events()
        self.port_writes.clear()
        self.tick += 1
        if self._lost:
            self.phase = "game_over"

    def result(self) -> dict[str, int | bool]:
        return {
            "score": self.score,
            "wave": self.wave,
            "kills": self.kills,
            "shots": self.shots_fired,
            "hits": self.hits,
            "best_combo": 0,
            "ticks": self.tick,
            "victory": False,
        }

    def digest(self) -> str:
        """sha256 over the ruleset, tick, RAM below the stack page, VRAM, the text layer, the main thread's
        position and the score wraps."""
        h = hashlib.sha256()
        h.update(RULESET.encode())
        h.update(self.tick.to_bytes(8, "little"))
        h.update(self.ram[:0x300])
        h.update(self.vram)
        h.update(repr(sorted(self.text.items())).encode())
        h.update(str(self._pos).encode())
        h.update(self.score_wraps.to_bytes(8, "little"))
        return h.hexdigest()

    # ---- machinery: interrupts, main-thread time, sound events, the trail ----

    def inp(self, port: int) -> int:
        """IN: port 1 the player's controls; port 2 the DIP switches (3 ships, extra ship at 1500, no tilt) and, on the
        upright cabinet, the same controls (read only when playerDataMSB is overwritten: the game has one player)."""
        if port == INP1:
            return self._in1
        if port == INP2:
            return self._in1 & 0x70
        return Memory.inp(self, port)

    def _isr(self, mid: bool):
        """Run the RST (11 cycles) and the mid-screen (`mid`) or end-screen interrupt's routine. Returns the flow it
        enters if it resets the stack, else None."""
        self._gaps += 1
        self._mid_next = not mid
        self.cycles += 11
        try:
            if mid:
                self.scan_line96()
            else:
                self.scan_line224()
        except Jump as jump:
            return jump.flow
        return None

    def _interrupt(self) -> None:
        """Take the interrupt the main thread yielded for. The main thread loses the routine's cycles from the gap to
        the next one. If the routine resets the stack, the flow it enters replaces the main thread and starts after
        it; the main-thread instruction the interrupt waited for then also cost the new flow the part of it that ran
        past the due time."""
        mid = self._mid_next
        start = self.cycles
        flow = self._isr(mid)
        cost = self.cycles - start
        gap = _MID_GAP if mid else _END_GAP
        if flow is None:
            self._room = gap - cost
            return
        self._main = self._run(flow)
        self._room = gap - cost - self._carry
        self._carry = 0

    def _late(self, lengths) -> None:
        """`_tick` ran past the due time: take the interrupt at the first boundary of these instructions at or after
        it, and any due after it (the later instructions move on by each one's cycles).

        * The mid-screen interrupt ends the frame. The first time a counted run gets there it is cut (`_Boundary`);
          when it runs again, the changes made to memory between the frames and the next frame's controls come in
          there.
        * One that resets the stack ends the run (`_Abandon`): the flow it enters gets the gap less the interrupt's
          cycles and less the part of the waited instruction that ran past the due time."""
        b = self.cycles - sum(lengths)
        lost = i = 0
        while self.cycles >= self._due:
            due = self._due
            while b + lost < due:
                b += lengths[i]
                i += 1
            mid = self._mid_next
            if mid:
                if self._pokes is None:
                    raise _Boundary
                if self._pokes:
                    self.ram[:], self.vram[:] = self._pokes
                    self._all_dirty()
                self._pokes = None
                self._replay = False
                self._in1 = self._in1_next
            start = self.cycles
            self._due = NEVER
            flow = self._isr(mid)
            cost = self.cycles - start
            gap = _MID_GAP if mid else _END_GAP
            if flow is not None:
                self._room = gap - cost - (b + lost - due)
                self._busy = self._carry = 0
                raise _Abandon(flow)
            self._room = gap - cost
            lost += cost
            self._due = due + gap

    def _run(self, flow):
        """Drive the main thread: each flow returns the flow it jumps to, None where the ROM goes to 0B89; an
        interrupt that resets the stack inside a counted run (`_Abandon`) replaces the flow there."""
        while flow is not None:
            try:
                flow = yield from flow
            except _Abandon as abandon:
                flow = abandon.flow

    def _main_run(self) -> None:
        """Run the main thread up to the next interrupt it yields for, or to the end of the frame in a counted run."""
        self._busy, self._carry = self._carry, 0
        try:
            self._pos = next(self._main)
        except StopIteration:
            self.phase = "game_over"

    def _save(self):
        """Everything a counted run can change, at its start."""
        trail = self.trail
        return (
            bytes(self.ram),
            bytes(self.vram),
            self.text.copy(),
            None if trail is None else tuple(trail),
            _saved_values(self),
        )

    def _restore(self, saved) -> None:
        """Go back to a counted run's start (`_save`); the dirty columns stay marked."""
        ram, vram, text, trail, values = saved
        self.ram[:] = ram
        self.vram[:] = vram
        self.text.clear()
        self.text.update(text)
        if trail is not None:
            self.trail.clear()
            self.trail.extend(trail)
        for name, value in zip(_SAVED, values):
            setattr(self, name, value)

    def _step(self, *cycles: int):
        """Run main-thread instructions of these lengths in turn: an interrupt that is due is taken before the next
        instruction starts. The caller makes an instruction's effect after the call that starts it. A length of 0
        marks the start of an instruction that is not run here."""
        for c in cycles:
            while self._busy >= self._room:
                self._carry = self._busy - self._room
                yield "busy"
            self._busy += c

    def _spend(self, cycles: int, progress=None):
        """Let a main-thread routine run for `cycles` CPU cycles, yielding at every interrupt it spans.
        progress(elapsed) applies the routine's effect so far, at each interrupt and at the end."""
        used = 0
        while self._busy + cycles - used > self._room:
            ran = max(0, self._room - self._busy)
            used += ran
            if progress is not None:
                progress(used)
            self._carry = self._busy + ran - self._room
            yield "busy"
        self._busy += cycles - used
        if progress is not None:
            progress(cycles)

    def _paced(self, routine, *args):
        """Run a ported routine on the main thread at the original's pace: for the cycles it counts, each byte and
        sound-port value it writes landing when the writing instruction starts. Returns the routine's result."""
        trace = self._trace = []
        c0 = self.cycles
        try:
            result = routine(*args)
        finally:
            self._trace = None
        total = self.cycles - c0
        if not trace:
            yield from self._spend(total)
            return result
        for _, addr, old, _ in reversed(trace):
            if old is not None:
                self.wr(addr, old)
        times = [t - c0 for t, _, _, _ in trace]
        done = 0

        def progress(elapsed):
            nonlocal done
            n = bisect_left(times, elapsed)
            for _, addr, old, new in trace[done:n]:
                if old is None:
                    self.out(addr, new)
                else:
                    self.wr(addr, new)
            done = max(done, n)

        yield from self._spend(total, progress)
        return result

    def _counted(self, routine, *args):
        """Run a routine that counts its instructions (`_tick`) on the main thread, as a game-loop pass runs:

        * an interrupt is taken at the first instruction boundary at or after its due time, and the routine's later
          reads see what it changed;
        * one that resets the stack ends the routine and its flow there (`_Abandon`);
        * the frame's end inside it cuts it, and the next step() runs it again from its start (`_resume`).

        The flows run this way the routines they run while the interrupts run the game objects. Returns the
        routine's result."""
        yield from self._step(0)  # an interrupt due by now comes before the routine's first instruction
        self._due = self.cycles + self._room - self._busy
        saved = self._save()
        try:
            result = routine(*args)
        except _Boundary:
            result = yield from self._resume(saved, routine, *args)
        self._hand_back()
        return result

    def _hand_back(self) -> None:
        """A counted run ends: the main thread's time since the last interrupt is `_busy` again."""
        self._busy = self._room - (self._due - self.cycles)
        self._due = NEVER

    def _clear_play_field(self):  # ClearPlayField 09D6 on the main thread
        """ClearPlayField at the original's pace, as `_paced` would run it but without logging its 5824 stores: each
        lands at its time in memory.py's schedule (CLEAR_START, CLEAR_COLUMN, CLEAR_BYTE)."""
        self._note("ClearPlayField")
        vram, zeros = self.vram, bytes(0x1A)
        done = 0

        def progress(elapsed):
            nonlocal done
            col, rest = divmod(max(elapsed - CLEAR_START, 0), CLEAR_COLUMN)
            n = min(0x1A * col + min(0x1A, -(-rest // CLEAR_BYTE)), 0x1A * 224)
            while done < n:
                r, j = divmod(done, 0x1A)
                k = min(n - done, 0x1A - j)
                vram[0x20 * r + 2 + j : 0x20 * r + 2 + j + k] = zeros[:k]
                if self.dirty is not None:
                    self.dirty.add(r)
                done += k

        yield from self._spend(CLEAR_TIME, progress)

    def _poll(self, iteration):
        """Run a wait loop: `iteration` is a generator function for one pass of it, returning a true value when the
        loop ends. A pass that no interrupt split and that changed no byte is repeated as many times as it fits
        before the next interrupt without being run (it would read and do the same). Returns the final value."""
        while True:
            gaps, busy = self._gaps, self._busy
            ram, vram = bytes(self.ram), bytes(self.vram)
            done = yield from iteration()
            if done:
                return done
            if self._gaps == gaps and self.ram == ram and self.vram == vram:
                period = self._busy - busy
                k = (self._room - self._busy - 1) // period
                if k > 0:
                    self._busy += k * period

    def _note(self, label: str) -> None:
        """Record a routine the main thread runs instruction by instruction in the trail."""
        if self.trail is not None:
            self.trail.append(label)

    def _sound_events(self) -> list[str]:
        """This frame's sounds: each bit that rose during the frame and is still set in the frame's last value of its
        port, and each held sound of _REPEATS whose repeat falls due; none while the frame's last value of port 3 has
        the amplifier off. A bit set and cleared within a frame (the rack end's 57-cycle march pulse, a race between
        the two threads) is a click on the cabinet at most."""
        p3, p5 = self._port3, self._port5
        rose3 = rose5 = 0
        for port, v in self.port_writes:
            if port == SOUND1:
                rose3 |= v & ~p3
                p3 = v
            else:
                rose5 |= v & ~p5
                p5 = v
        self._port3, self._port5 = p3, p5
        events = []
        if not p3 & _AMPLIFIER:
            return events
        for value, rose, names in ((p3, rose3, _PORT3_EVENTS), (p5, rose5, _PORT5_EVENTS)):
            for bit, name in names:
                if not value & bit:
                    continue
                repeat = _REPEATS.get(name)
                if rose & bit:
                    events.append(name)
                    if repeat:
                        self._since[name] = self.tick
                elif repeat and (self.tick - self._since[name]) % repeat == 0:
                    events.append(name)
        return events

    def _install_trail(self) -> None:
        """Wrap every ported routine on this instance so that calling it appends its ROM label to the trail."""
        trail = self.trail
        seen = set()
        for cls in type(self).__mro__:
            for name, fn in vars(cls).items():
                if name in seen or name.startswith("_") or name in _NOT_ROUTINES or not callable(fn):
                    continue
                seen.add(name)
                label, bound = _label(name), getattr(self, name)

                def traced(*args, _fn=bound, _label=label):
                    trail.append(_label)
                    return _fn(*args)

                setattr(self, name, traced)

    # ---- power-on and the credit ----

    def _power_on(self) -> None:  # init 18D4-18E4, then 0AEA-0AEF and the coin's credit (0035-003B)
        """What power-on and one coin leave that the game uses: RAM from the mirror, the status lines, sound off,
        one credit."""
        self.sub_01e6(0x00)
        self.draw_status()
        self.wr(A_SHOT_RELOAD_RATE, 0x08)
        self.out(SOUND1, 0)
        self.out(SOUND2, 0)
        self.wr(ISR_SPLASH_TASK, 0)
        self.wr(NUM_COINS, bcd_add(self.rd(NUM_COINS), 0x01)[0])
        self.draw_num_credits()

    def _wait_for_start(self) -> None:  # WaitForStart 0765-078E
        """The one-credit wait screen, up to the 1P start press that enters NewGame."""
        self.wr(WAIT_START_LOOP, 1)
        self.sub_1979()
        self.clear_play_field()
        self.print_message(0x04, 0x1FF3, 0x3013)
        self.print_message(0x14, 0x1ACF, 0x2810)

    def sub_1979(self) -> None:  # 1979
        """Suspend game tasks and print the credits with their label. In, out: nothing."""
        self.dsable_game_tasks()
        self.draw_num_credits()
        self.sub_193c()

    # ---- main-thread flows ----

    def new_game(self):  # NewGame 0798
        """Take the credit, set up both players' data and the screen, then prompt and start the first rack."""
        self.wr(TWO_PLAYERS, 0)
        self.wr(NUM_COINS, bcd_add(self.rd(NUM_COINS), 0x99)[0])
        self.draw_num_credits()
        self.wr16(P1_SCOR_L, 0)
        self.wr16(P2_SCOR_L, 0)
        self.sub_1925()
        self.sub_192b()
        self.dsable_game_tasks()
        self.wr(GAME_MODE, 1)
        self.wr16(PLAYER1_ALIVE, 0x0101)
        self.wr16(PLAYER1_EX, 0x0101)
        self.draw_status()
        self.draw_shield_pl1()
        self.draw_shield_pl2()
        a = self.get_ships_per_cred()
        self.wr(P1_SHIPS_REM, a)
        self.wr(P2_SHIPS_REM, a)
        self.sub_00d7()
        self.wr(P1_RACK_CNT, 0)
        self.wr(P2_RACK_CNT, 0)
        self.init_aliens()
        self.init_aliens_p2()
        self.wr16(P1_REF_ALIEN_Y, 0x3878)
        self.wr16(P2_REF_ALIEN_YR, 0x3878)
        self.copy_ram_mirror()
        self.remove_ship()
        return (yield from self.sub_07f9())

    def sub_07f9(self):  # 07F9
        """Prompt the player, clear the playfield and start the round."""
        yield from self.prompt_player()
        self._busy += _PROMPT_END
        yield from self._step(17)  # CALL 09D6
        yield from self._clear_play_field()
        yield from self._step(4, 4, 13)  # NOP; XOR A; LD (20C1),A
        self.wr(ISR_SPLASH_TASK, 0)
        return (yield from self.sub_0804())

    def sub_0804(self):  # 0804
        """Top of a round: the bottom line, the player's shields, the rack; then the game loop."""
        yield from self._step(17)  # CALL 01CF
        yield from self._paced(self.draw_bottom_line)
        yield from self._step(13)  # LD A,(2067)
        player1 = self.rd(PLAYER_DATA_MSB) & 0x01
        yield from self._step(4, 10)  # RRCA; JP C,0872
        if player1:
            yield from self._step(17)  # 0872: CALL 021A
            yield from self._paced(self.restore_shields1)
            yield from self._step(10)  # JP 0814
        else:
            yield from self._step(17)  # CALL 0213
            yield from self._paced(self.restore_shields2)
            yield from self._step(17)  # CALL 01CF
            yield from self._paced(self.draw_bottom_line)
        yield from self._step(17)  # 0814: CALL 00B1
        yield from self._paced(self.init_rack)
        return (yield from self.sub_0817())

    def sub_0817(self):  # 0817
        """Enable the interrupts' game tasks and the amplifier, and run the game loop. Instruction by instruction:
        from the write to suspendPlay on, an interrupt runs the game objects and may reset the stack."""
        self._note("EnableGameTasks")
        yield from self._step(17, 7, 13)  # CALL 19D1; LD A,01; LD (20E9),A
        self.wr(SUSPEND_PLAY, 1)
        yield from self._step(10, 7, 17)  # RET; LD B,20; CALL 18FA
        yield from self._counted(self.sound_bits3_on, 0x20)
        yield from self._step(0)  # 081F: an interrupt due by the arrival is taken before the first pass
        return (yield from self.game_loop())

    def game_loop(self):  # 081F (the game loop)
        """The game loop on cycle time, a pass at a time; JP 09EF when the rack is cleared. An interrupt that resets
        the stack inside a pass ends the loop there (`_run`)."""
        self._due = self.cycles + self._room - self._busy
        while True:
            saved = self._save()
            try:
                flow = self._pass()
            except _Boundary:
                flow = yield from self._resume(saved, self._pass)
            if flow is not None:
                return flow

    def _resume(self, saved, routine, *args):
        """The frame ended inside the counted run of `routine` that started at `saved`: yield with memory as it is
        there, then run it again from its start, which repeats exactly up to the cut, and on from there."""
        ram, vram = bytes(self.ram), bytes(self.vram)
        self._split = ram, vram
        self._due = NEVER
        yield "cut"
        self._split = None
        if self.ram == ram and self.vram == vram:
            self._pokes = ()
        else:
            self._pokes = bytes(self.ram), bytes(self.vram)
        self._restore(saved)
        self._replay = True
        try:
            return routine(*args)
        except _Boundary:  # a run longer than a frame, which only a program gone astray makes
            self._lost = True
            yield "cut"

    def _pass(self):
        """One pass of the game loop, 081F-0851. Returns the flow it leaves the loop for (09EF), or None."""
        tick = self._tick
        tick(17)  # CALL 1618
        self.plr_fire_or_demo()
        tick(17)  # CALL 190A
        self.plyr_shot_and_bump()
        tick(17)  # CALL 15F3
        self.count_aliens()
        tick(17)  # CALL 0988
        self.adjust_score()
        n = self.rd(NUM_ALIENS)
        tick(13, 4, 10)  # LD A,(2082); AND A; JP Z,09EF
        if not n:
            self._hand_back()
            return self.sub_09ef()
        tick(17)  # CALL 170E
        self.a_shot_reload_rate()
        tick(17)  # CALL 0935
        self.sub_0935()
        tick(17)  # CALL 08D8
        self.speed_shots()
        tick(17)  # CALL 172C
        self.shot_sound()
        tick(17)  # CALL 0A59
        if self.sub_0a59():
            tick(10, 17)  # JP Z,0849; CALL 1775
        else:
            tick(10, 7, 17)  # JP Z,0849; LD B,04; CALL 18FA
            self.sound_bits3_on(0x04)
            tick(17)  # CALL 1775
        self.fleet_delay_ex_ship()
        tick(10, 17)  # OUT (06),A; CALL 1804
        self.ctrl_saucer_sound()
        tick(10)  # JP 081F
        return None

    def sub_09ef(self):  # 09EF
        """End of a rack: wait, clear the playfield, reset the game objects, set up the next rack, start it."""
        yield from self._step(17)  # CALL 0A3C
        yield from self.sub_0a3c()
        yield from self._step(4, 13)  # XOR A; LD (20E9),A
        self.wr(SUSPEND_PLAY, 0)
        yield from self._step(17)
        yield from self._clear_play_field()
        yield from self._step(13)  # LD A,(2067)
        a = self.rd(PLAYER_DATA_MSB)
        yield from self._step(11, 17)  # PUSH AF; CALL 01E4
        yield from self._paced(self.copy_ram_mirror)
        yield from self._step(10, 13)  # POP AF; LD (2067),A
        self.wr(PLAYER_DATA_MSB, a)
        yield from self._step(13, 5, 11, 7, 7)  # LD A,(2067); LD H,A; PUSH HL; LD L,FE; LD A,(HL)
        hl = (a << 8) | 0xFE
        n = (self.rd(hl) & 0x07) + 1
        yield from self._step(7, 5, 7)  # AND 07; INC A; LD (HL),A
        self.wr(hl, n)
        # LD HL,1DA2; the walk to the rack's entry; LD A,(HL); POP HL; LD L,FC; LD (HL),A
        yield from self._step(10, *(5, 5, 10) * n, 7, 10, 7, 7)
        self.wr((a << 8) | 0xFC, self.rd(0x1DA2 + n))
        yield from self._step(5, 10)  # INC HL; LD (HL),38
        self.wr((a << 8) | 0xFD, 0x38)
        yield from self._step(5, 4, 10)  # LD A,H; RRCA; JP C,0A33
        self.wave += 1
        if a & 0x01:
            yield from self._step(17)
            yield from self._paced(self.draw_shield_pl1)
            yield from self._step(17)
            yield from self._paced(self.init_aliens)
        else:
            yield from self._step(7, 13)  # LD A,21; LD (2098),A
            self.wr(SOUND_PORT5, 0x21)
            yield from self._step(17)
            yield from self._paced(self.draw_shield_pl2)
            yield from self._step(17)
            yield from self._paced(self.init_aliens_p2)
        yield from self._step(10)  # JP 0804
        return (yield from self.sub_0804())

    def sub_0a3c(self):  # 0A3C
        """Give a cleared rack half a second, or (if the player is hit) wait out the blow-up. Instruction by
        instruction: the interrupts run the game objects meanwhile and may reset the stack."""
        yield from self._step(17)  # CALL 0A59
        ok = yield from self._counted(self.sub_0a59)
        yield from self._step(10)  # JP NZ,0A52
        if ok:
            yield from self._step(7, 13)  # LD A,30; LD (20C0),A
            self.wr(ISR_DELAY, 0x30)
            if (yield from self._poll(self._wait_0a47)) == "delay":
                return
        yield from self._poll(self._wait_0a52)
        yield from self._step(10)  # RET

    def _wait_0a47(self):  # 0A47-0A4F, one pass
        yield from self._step(13)  # LD A,(20C0)
        if not self.rd(ISR_DELAY):
            yield from self._step(4, 11)  # AND A; RET Z
            return "delay"
        yield from self._step(4, 5, 17)  # AND A; RET Z; CALL 0A59
        ok = yield from self._counted(self.sub_0a59)
        yield from self._step(10)  # JP Z,0A47
        return None if ok else "hit"

    def _wait_0a52(self):  # 0A52-0A55, one pass
        yield from self._step(17)  # CALL 0A59
        ok = yield from self._counted(self.sub_0a59)
        yield from self._step(10)  # JP NZ,0A52
        return ok

    def sub_0a59(self) -> bool:  # 0A59
        """Whether the player is unhurt (playerAlive FF). In: nothing. Out: Z."""
        ok = self.rd(PLAYER_ALIVE) == 0xFF
        self._tick(13, 7, 10)  # LD A,(2015); CP FF; RET
        return ok

    def sub_02d0(self):  # 02D0 (GameObj0 after a blow-up, stack reset)
        """The player's blow-up is over: take a ship and resume the game loop, or end the game."""
        yield from self._step(10, 4, 17)  # LD SP,2400; EI; CALL 19D7
        yield from self._counted(self.dsable_game_tasks)
        yield from self._step(17)  # CALL 092E
        a, _ = yield from self._paced(self.sub_092e)
        yield from self._step(4, 10)  # AND A; JP Z,166D
        if a == 0:
            return (yield from self.sub_166d())
        # 02DE: 18E7 points at the other player's alive flag; with one player either test leads to 032C.
        self._note("sub_18E7")
        yield from self._step(17, 13)  # CALL 18E7; LD A,(2067)
        player1 = self.rd(PLAYER_DATA_MSB) & 0x01
        yield from self._step(10, 4)  # LD HL,20E7; RRCA
        if player1:
            yield from self._step(5, 5, 10)  # RET NC; INC HL; RET
            hl = PLAYER2_ALIVE
        else:
            yield from self._step(11)  # RET NC
            hl = PLAYER1_ALIVE
        yield from self._step(7)  # LD A,(HL)
        other = self.rd(hl)
        yield from self._step(4, 10)  # AND A; JP Z,032C
        if other:
            yield from self._step(13)  # LD A,(20CE)
            two = self.rd(TWO_PLAYERS)
            yield from self._step(4, 10)  # AND A; JP Z,032C
            if two:  # 02ED: the player switch, which a one-player game reaches only with twoPlayers overwritten
                self._lost = True
                return None
        yield from self._step(17)  # 032C: CALL 1A7F
        yield from self._paced(self.remove_ship)
        yield from self._step(10)  # JP 0817
        return (yield from self.sub_0817())

    def sub_166d(self):  # 166D
        """Game over with no ships left: print 0 ships, then the game-over sequence."""
        yield from self._step(4, 17)  # XOR A; CALL 1A8B
        yield from self._paced(self.sub_1a8b, 0)
        return (yield from self.sub_1671())

    def sub_1671(self):  # 1671
        """Game over: the player is dead, the high score is updated, GAME OVER is spelled out; then 0B89."""
        yield from self._step(17)
        hl = yield from self._paced(self.cur_ply_alive)
        yield from self._step(10)  # LD (HL),00
        self.wr(hl, 0)
        yield from self._step(17)
        hl = yield from self._paced(self.sub_09ca)
        yield from self._step(5, 10, 7)  # INC HL; LD DE,20F5; LD A,(DE)
        hi_m = self.rd(HI_SCOR_M)
        yield from self._step(7)  # CP (HL)
        p_m = self.rd(hl + 1)
        yield from self._step(5, 5, 7)  # DEC DE; DEC HL; LD A,(DE)
        hi_l = self.rd(HI_SCOR_L)
        yield from self._step(10)  # JP Z,168B
        if hi_m == p_m:
            yield from self._step(7)  # 168B: CP (HL)
            higher = hi_l < self.rd(hl)
            yield from self._step(10)  # JP NC,1698
        else:
            yield from self._step(10)  # JP NC,1698
            higher = hi_m < p_m
            if higher:
                yield from self._step(10)  # JP 168F
        if higher:
            yield from self._step(7)  # 168F: LD A,(HL)
            a = self.rd(hl)
            yield from self._step(7)  # LD (DE),A
            self.wr(HI_SCOR_L, a)
            yield from self._step(5, 5, 7)  # INC DE; INC HL; LD A,(HL)
            a = self.rd(hl + 1)
            yield from self._step(7)  # LD (DE),A
            self.wr(HI_SCOR_M, a)
            yield from self._step(17)
            yield from self._paced(self.print_hi_score)
        yield from self._step(13, 4, 10)  # 1698: one player, so the short message (16C9)
        yield from self._step(10, 10, 7, 17)  # LD HL,2D18; LD DE,1AA6; LD C,0A; CALL 0A93
        yield from self.print_message_del(0x0A, 0x1AA6, 0x2D18)
        yield from self._step(17)  # CALL 0AB6
        yield from self.two_sec_delay()
        yield from self._step(17)  # CALL 09D6
        yield from self._clear_play_field()
        yield from self._step(4, 13)  # XOR A; LD (20EF),A
        self.wr(GAME_MODE, 0)
        yield from self._step(10)  # OUT (05),A
        self.out(SOUND2, 0)
        yield from self._step(17)  # CALL 19D1
        yield from self._paced(self.enable_game_tasks)
        yield from self._step(0)  # JP 0B89 at 16E3: an interrupt due by then is taken first
        return None

    def sub_16e6(self):  # 16E6 (from 1971, stack reset)
        """The rack has landed: the player blows up, the reserve is wiped, the game ends whatever ships remain."""
        yield from self._step(10, 4, 4, 13)  # LD SP,2400; EI; XOR A; LD (2015),A
        self.wr(PLAYER_ALIVE, 0)
        yield from self._poll(self._wait_16ee)
        yield from self._step(17)  # CALL 19D7
        yield from self._counted(self.dsable_game_tasks)
        yield from self._step(10, 17)  # LD HL,2701; CALL 19FA
        yield from self._paced(self.sub_19fa, 0x2701)
        yield from self._step(4, 17)  # XOR A; CALL 1A8B
        yield from self._paced(self.sub_1a8b, 0)
        yield from self._step(7, 10, 17)  # LD B,FB; JP 196B; CALL 19DC
        yield from self._counted(self.sound_bits3_off, 0xFB)
        yield from self._step(10)  # JP 1671
        return (yield from self.sub_1671())

    def _wait_16ee(self):  # 16EE-16F9, one pass
        yield from self._step(17)  # CALL 14D8
        yield from self._counted(self.player_shot_hit)
        yield from self._step(7, 17)  # LD B,04; CALL 18FA
        yield from self._counted(self.sound_bits3_on, 0x04)
        yield from self._step(17)  # CALL 0A59
        ok = yield from self._counted(self.sub_0a59)
        yield from self._step(10)  # JP NZ,16EE
        return ok

    def prompt_player(self):  # PromptPlayer 088D
        """Print PLAY PLAYER<1> and blink the player's score for 176 frames (end-screen interrupts): one pass of the
        wait loop after each interrupt (the others repeat it)."""
        self.print_message(0x0E, 0x1B70, 0x2B11)
        if not self.rd(PLAYER_DATA_MSB) & 0x01:
            self.draw_char(0x1C, 0x3711)
        self.wr(ISR_DELAY, 0xB0)
        while True:
            a = self.rd(ISR_DELAY)
            if a == 0:
                return
            if a & 0x04:
                self.clear_small_sprite(0x20, 0x271C if self.rd(PLAYER_DATA_MSB) & 0x01 else 0x391C)
            else:
                self.draw_score(self.sub_09ca())
            yield "08A9"

    def print_message_del(self, c: int, de: int, hl: int):  # PrintMessageDel 0A93
        """Print C characters from DE at screen HL, one every 6 frames (end-screen interrupts). Out: HL (the next
        cell)."""
        while True:
            yield from self._step(11, 7, 17)  # PUSH DE; LD A,(DE); CALL 08FF
            hl = yield from self._paced(self.draw_char, self.rd(de), hl)
            yield from self._step(10, 7, 13)  # POP DE; LD A,07; LD (20C0),A
            self.wr(ISR_DELAY, 0x07)
            yield from self._poll(self._wait_0a9e)
            yield from self._step(5, 5, 10)  # INC DE; DEC C; JP NZ,0A93
            de = (de + 1) & 0xFFFF
            c = (c - 1) & 0xFF
            if c == 0:
                yield from self._step(10)  # RET
                return hl

    def _wait_0a9e(self):  # 0A9E-0AA2, one pass
        yield from self._step(13)  # LD A,(20C0)
        done = self.rd(ISR_DELAY) == 0x01
        yield from self._step(5, 10)  # DEC A; JP NZ,0A9E
        return done

    def two_sec_delay(self):  # TwoSecDelay 0AB6
        """Wait 128 frames (end-screen interrupts)."""
        yield from self._step(7, 10)  # LD A,80; JP 0AD7
        yield from self.wait_on_delay(0x80)

    def wait_on_delay(self, a: int):  # WaitOnDelay 0AD7
        """Wait until the end-screen interrupt has counted isrDelay down from A to 0."""
        yield from self._step(13)  # LD (20C0),A
        self.wr(ISR_DELAY, a)
        yield from self._poll(self._wait_0ada)
        yield from self._step(10)  # RET

    def _wait_0ada(self):  # 0ADA-0ADE, one pass
        yield from self._step(13)  # LD A,(20C0)
        done = not self.rd(ISR_DELAY)
        yield from self._step(4, 10)  # AND A; JP NZ,0ADA
        return done

    # ---- the interrupts ----

    def scan_line96(self) -> None:  # ScanLine96 0008 (continues at 008C)
        """Mid-screen interrupt: the objects from obj1 on (those with bit 7 of their Xr clear act, CompYToBeam: the
        half of the screen the beam has passed), then the alien cursor."""
        self.wr(VBLANK_STATUS, 0)
        self.cycles += 98
        if not self.rd(SUSPEND_PLAY):
            self.cycles += 54
            return
        self.cycles += 27
        if not self.rd(GAME_MODE):
            self.cycles += 27
            if not self.rd(ISR_SPLASH_TASK) & 0x01:
                self.cycles += 54
                return
        self.cycles += 27
        self.sub_024b(OBJ1_TIMER_MSB)
        self.cycles += 17
        self.cursor_next_alien()
        self.cycles += 64

    def scan_line224(self) -> None:  # ScanLine224 0010
        """End-screen interrupt: isrDelay, then with game tasks on the fleet sound, the shot phase, the alien under
        the cursor, every object from the player's, and the saucer timer."""
        self.wr(VBLANK_STATUS, 0x80)
        self.wr(ISR_DELAY, self.rd(ISR_DELAY) - 1)
        # 001D-0041: CheckHandleTilt returns at once (IN2 bit 2 is 0) and IN1 bit 0 is 1 (no coin): 0067.
        self.wr(COIN_SWITCH, 1)
        self.cycles += 223
        if not self.rd(SUSPEND_PLAY):
            self.cycles += 54
            return
        self.cycles += 27
        if not self.rd(GAME_MODE):
            return  # 0050-0064: attract-screen tasks, which a credited game never reaches
        self.cycles += 17
        self.time_fleet_sound()
        self.sub_0072()
        self.cycles += 58

    def sub_0072(self) -> None:  # 0072
        """The end-screen interrupt's game tasks. In, out: nothing."""
        self.wr(SHOT_SYNC, self.rd(OBJ2_TIMER_EXTRA))
        self.cycles += 43
        self.draw_alien()
        self.cycles += 17
        self.run_game_objs()
        self.cycles += 17
        self.time_to_saucer()

    # ---- timers, sounds and controls ----

    def time_to_saucer(self) -> None:  # TimeToSaucer 0913
        """Once the rack is below its first row, count down to the next saucer (every 600h frames)."""
        self.cycles += 20
        if self.rd(REF_ALIEN_YR) >= 0x78:
            self.cycles += 11
            return
        self.cycles += 40
        hl = self.rd16(TILL_SAUCER_LSB)
        if hl == 0:
            hl = 0x0600
            self.cycles += 30
            self.wr(SAUCER_START, 1)
        self.wr16(TILL_SAUCER_LSB, hl - 1)
        self.cycles += 31

    def time_fleet_sound(self) -> None:  # TimeFleetSound 1740
        """Time the march note's hold and the fleet delay; flag a new note when the delay runs out."""
        self.cycles += 20
        t = (self.rd(FLEET_SND_HOLD) - 1) & 0xFF
        self.wr(FLEET_SND_HOLD, t)
        if t == 0:
            self.cycles += 17
            self.sub_176d()
        else:
            self.cycles += 11
        self.cycles += 27
        if not self.rd(PLAYER_OK):
            self.sub_176d()
            return
        self.cycles += 20
        t = (self.rd(FLEET_SND_CNT) - 1) & 0xFF
        self.wr(FLEET_SND_CNT, t)
        if t:
            self.cycles += 11
            return
        self.cycles += 32
        self.out(SOUND2, self.rd(SOUND_PORT5))
        self.cycles += 27
        if not self.rd(NUM_ALIENS):
            self.sub_176d()
            return
        self.wr(FLEET_SND_CNT, self.rd(FLEET_SND_RELOAD))
        self.wr(CHANGE_FLEET_SND, 1)
        self.wr(FLEET_SND_HOLD, 0x04)
        self.cycles += 69

    def fleet_delay_ex_ship(self) -> None:  # FleetDelayExShip 1775
        """Pick the fleet delay for the number of aliens and move to the next march note when flagged; time out the
        extra-ship sound."""
        tick = self._tick
        if self.rd(CHANGE_FLEET_SND):
            tick(13, 4, 10, 10, 10)  # LD A,(2095); AND A; JP Z,17AA; LD HL,1A11; LD DE,1A21
            n = self.rd(NUM_ALIENS)
            lengths = [13]  # LD A,(2082)
            hl, de = 0x1A11, 0x1A21
            while n < self.rd(hl):
                lengths += (7, 10, 5, 5, 10)  # CP (HL); JP NC,178E; INC HL; INC DE; JP 1785
                hl += 1
                de += 1
            tick(*lengths, 7, 10, 7)  # CP (HL); JP NC,178E; LD A,(DE)
            self.wr(FLEET_SND_RELOAD, self.rd(de))
            tick(13, 10)  # LD (2097),A; LD HL,2098
            b = self.rd(SOUND_PORT5) & 0x30
            tick(7, 7, 5)  # LD A,(HL); AND 30; LD B,A
            a = rlc(self.rd(SOUND_PORT5) & 0x0F)
            if a == 0x10:
                tick(7, 7, 4, 7, 10, 7, 4)  # LD A,(HL); AND 0F; RLCA; CP 10; JP NZ,17A4; LD A,01; OR B
                a = 0x01
            else:
                tick(7, 7, 4, 7, 10, 4)
            self.wr(SOUND_PORT5, a | b)
            tick(7, 4)  # LD (HL),A; XOR A
            self.wr(CHANGE_FLEET_SND, 0)
            tick(13, 10)  # LD (2095),A; LD HL,2099
        else:
            tick(13, 4, 10, 10)  # LD A,(2095); AND A; JP Z,17AA; LD HL,2099
        t = (self.rd(EXTRA_HOLD) - 1) & 0xFF
        self.wr(EXTRA_HOLD, t)
        if t:
            tick(10, 11)  # DEC (HL); RET NZ
            return
        tick(10, 5, 7, 10)  # DEC (HL); RET NZ; LD B,EF; JP 19DC
        self.sound_bits3_off(0xEF)

    def shot_sound(self) -> None:  # ShotSound 172C
        """The shot sound is on while the player's shot is active."""
        on = self.rd(PLYR_SHOT_STATUS)
        self._tick(13, 7, 10, 7, 10)  # LD A,(2025); CP 00; JP NZ,1739; LD B,FD or 02; JP 19DC or 18FA
        if on:
            self.sound_bits3_on(0x02)
        else:
            self.sound_bits3_off(0xFD)

    def ctrl_saucer_sound(self) -> None:  # CtrlSaucerSound 1804
        """Retrigger the saucer sound while the saucer flies unhit; off without a saucer."""
        self._tick(10)  # LD HL,2084
        if not self.rd(SAUCER_ACTIVE):
            self._tick(7, 4, 10)  # LD A,(HL); AND A; JP Z,0707
            self.sub_0707()
            return
        self._tick(7, 4, 10, 5)  # ...; INC HL
        if self.rd(SAUCER_HIT):
            self._tick(7, 4, 11)  # LD A,(HL); AND A; RET NZ
            return
        self._tick(7, 4, 5, 7, 10)  # ...; RET NZ; LD B,01; JP 18FA
        self.sound_bits3_on(0x01)

    def a_shot_reload_rate(self) -> None:  # AShotReloadRate 170E
        """Set the alien shots' reload rate from the score's high byte (tables 1CB8 and 1AA1)."""
        self._tick(17)  # CALL 09CA
        hl = self.sub_09ca()
        self._tick(5)  # INC HL
        a = self.rd(hl + 1)
        lengths = [7, 10, 10, 7, 5]  # LD A,(HL); LD DE,1CB8; LD HL,1AA1; LD C,04; LD B,A
        de, hl = 0x1CB8, 0x1AA1
        for _ in range(4):
            if self.rd(de) >= a:
                lengths += (7, 4, 10)  # LD A,(DE); CP B; JP NC,1727
                break
            lengths += (7, 4, 10, 5, 5, 5, 10)  # ...; INC HL; INC DE; DEC C; JP NZ,171C
            hl += 1
            de += 1
        self._tick(*lengths, 7)  # LD A,(HL)
        self.wr(A_SHOT_RELOAD_RATE, self.rd(hl))
        self._tick(13, 10)  # LD (20CF),A; RET

    def speed_shots(self) -> None:  # SpeedShots 08D8
        """With fewer than 9 aliens the alien shots move 5 pixels a step instead of 4."""
        if self.rd(NUM_ALIENS) >= 0x09:
            self._tick(13, 7, 11)  # LD A,(2082); CP 09; RET NC
            return
        self._tick(13, 7, 5, 7)  # ...; RET NC; LD A,FB
        self.wr(ALIEN_SHOT_DELTA, 0xFB)
        self._tick(13, 10)  # LD (207E),A; RET

    def sub_0935(self) -> None:  # 0935
        """Award the extra ship once the score reaches 1500: a ship icon, the digit, the sound."""
        tick = self._tick
        tick(17)  # CALL 1910
        hl = self.cur_ply_alive() - 2
        tick(5, 5)  # DEC HL; DEC HL
        if not self.rd(hl):
            tick(7, 4, 11)  # LD A,(HL); AND A; RET Z
            return
        tick(7, 4, 5, 7)  # ...; RET Z; LD B,15
        if self.inp(INP2) & 0x08:
            tick(10, 7, 10, 7, 17)  # IN A,(02); AND 08; JP Z,0948; LD B,10; CALL 09CA
            b = 0x10
        else:
            tick(10, 7, 10, 17)
            b = 0x15
        hl = self.sub_09ca()
        tick(5)  # INC HL
        if self.rd(hl + 1) < b:
            tick(7, 4, 11)  # LD A,(HL); CP B; RET C
            return
        tick(7, 4, 5, 17)  # ...; RET C; CALL 092E
        _, hl = self.sub_092e()
        a = (self.rd(hl) + 1) & 0xFF
        self.wr(hl, a)
        tick(10)  # INC (HL)
        a = self.rd(hl)
        tick(7, 11, 10)  # LD A,(HL); PUSH AF; LD HL,2501
        h, x = 0x25, a
        while True:
            h = (h + 2) & 0xFF
            x = (x - 1) & 0xFF
            tick(5, 5, 5, 10)  # INC H; INC H; DEC A; JP NZ,0958
            if x == 0:
                break
        tick(7, 10, 17)  # LD B,10; LD DE,1C60; CALL 1439
        self.draw_simp_sprite(0x10, 0x1C60, (h << 8) | 0x01)
        tick(10, 5, 17)  # POP AF; INC A; CALL 1A8B
        self.sub_1a8b(a + 1)
        tick(17)  # CALL 1910
        hl = self.cur_ply_alive() - 2
        tick(5, 5)  # DEC HL; DEC HL
        self.wr(hl, 0)
        tick(10, 7)  # LD (HL),00; LD A,FF
        self.wr(EXTRA_HOLD, 0xFF)
        tick(13, 7, 10)  # LD (2099),A; LD B,10; JP 18FA
        self.sound_bits3_on(0x10)

    def get_ships_per_cred(self) -> int:  # GetShipsPerCred 08D1
        """Ships per game from the DIP switches (3-6). Out: A."""
        return (self.inp(INP2) & 0x03) + 0x03

    def read_inputs(self) -> int:  # ReadInputs 17C0
        """The active player's controls (IN1 for player 1, IN2 for player 2). Out: A."""
        port = INP1 if self.rd(PLAYER_DATA_MSB) & 0x01 else INP2
        self._tick(13, 4, 10)  # LD A,(2067); RRCA; JP NC,17CA
        a = self.inp(port)
        self._tick(10, 10)  # IN A,(01) or (02); RET
        return a

    def plr_fire_or_demo(self) -> None:  # PlrFireOrDemo 1618
        """Start a shot on a fresh press of fire, when the player is alive and moving and no shot is out."""
        tick = self._tick
        if self.rd(PLAYER_ALIVE) != 0xFF:
            tick(13, 7, 11)  # LD A,(2015); CP FF; RET NZ
            return
        tick(13, 7, 5, 10)  # ...; RET NZ; LD HL,2010
        a = self.rd(OBJ0_TIMER_MSB)
        tick(7, 5)  # LD A,(HL); INC HL
        if a | self.rd(OBJ0_TIMER_LSB):
            tick(7, 4, 11)  # LD B,(HL); OR B; RET NZ
            return
        tick(7, 4, 5)
        if self.rd(PLYR_SHOT_STATUS):
            tick(13, 4, 11)  # LD A,(2025); AND A; RET NZ
            return
        tick(13, 4, 5)
        demo = not self.rd(GAME_MODE)
        tick(13, 4, 10)  # LD A,(20EF); AND A; JP Z,1652
        if demo:  # 1652: demo play, which a credited game reaches only with gameMode overwritten
            self._lost = True
            return
        bounce = self.rd(FIRE_BOUNCE)
        tick(13, 4, 10, 17)  # LD A,(202D); AND A; JP NZ,1648; CALL 17C0
        fire = self.read_inputs() & 0x10
        if bounce:
            if fire:
                tick(7, 11)  # AND 10; RET NZ
                return
            tick(7, 5)
            self.wr(FIRE_BOUNCE, 0)
            tick(13, 10)  # LD (202D),A; RET
            return
        if not fire:
            tick(7, 11)  # AND 10; RET Z
            return
        tick(7, 5, 7)  # ...; RET Z; LD A,01
        self.wr(PLYR_SHOT_STATUS, 1)
        tick(13)  # LD (2025),A
        self.wr(FIRE_BOUNCE, 1)
        tick(13, 10)  # LD (202D),A; RET
        self.shots_fired += 1
