"""The arcade game program: construction at the start of the first prompt, the round start, the controls, the result's
counters, the extra ship, the end of a rack, game over, determinism, the sound events (the rule, the repeats, the rack
end's pulse, the invasion game over's race), main-thread time outside the game loop (paced routines, interrupt costs,
instruction boundaries, the soundPort3 race, stack resets, the arrival in the game loop, wait loops), the game loop on
cycle time (an interrupt between the accesses of a pass, several in one pass, a stack reset inside one, the frame
ending inside one and its pass run again), the routines a flow runs counted (the same three cases inside
PlayerShotHit in the invasion's 16EE loop, DsableGameTasks' store), the program gone astray (a lone invader wrapped
round into RAM, CountAliens through the address mirror, IN2's controls, the stack, the object list, demo play
and the player switch), BCD arithmetic and speed.

The program image is build_image(): the ROM's tables and art.py's pictures (some of them the ROM's, for play); the
ROM image file is never read."""

import time

import pytest

from terminal_invaders.arcade import RULESET, ArcadeGame, machine
from terminal_invaders.arcade.art import CHARSET
from terminal_invaders.arcade.memory import (
    ADJUST_SCORE,
    ALIEN_IS_EXPLODING,
    EXTRA_HOLD,
    FIRE_BOUNCE,
    GAME_MODE,
    ISR_DELAY,
    NEVER,
    NUM_ALIENS,
    OBJ0_TIMER_LSB,
    OBJ0_TIMER_MSB,
    OBJ1_COOR_XR,
    OBJ1_COOR_YR,
    P1_ALIENS,
    P1_RACK_CNT,
    P1_REF_ALIEN_Y,
    P1_SCOR_L,
    P1_SCOR_M,
    PLAYER_ALIVE,
    PLAYER_DATA_MSB,
    PLAYER_XR,
    PLYR_SHOT_STATUS,
    RAM,
    REF_ALIEN_XR,
    REF_ALIEN_YR,
    SCORE_DELTA_LSB,
    SOUND1,
    SOUND2,
    SOUND_PORT3,
    STACK,
    SUSPEND_PLAY,
    TWO_PLAYERS,
    VRAM,
)
from terminal_invaders.arcade.program import Jump, bcd_add
from terminal_invaders.model import Config

ROUND_START = 190  # the first rack is running by then (PromptPlayer blinks for 176 frames, then the wipe)


def new_game(**kw):
    return ArcadeGame(Config(mode="arcade"), **kw)


def run(game, frames, actions=()):
    for _ in range(frames):
        game.step(actions)


def printed(game, addr, n):
    """The n characters DrawChar printed from screen address addr on (one cell is 100h further)."""
    return "".join(CHARSET.get(game.text.get(addr + 0x100 * i), "?") for i in range(n))


@pytest.fixture
def started():
    """A game whose player can move and fire (the ship appears a while after the rack); a fresh one per test."""
    game = new_game()
    run(game, ROUND_START)
    while game.rd(OBJ0_TIMER_MSB) | game.rd(OBJ0_TIMER_LSB):
        game.step()
    return game


def test_construction_is_the_first_prompt():
    game = new_game()
    assert RULESET == "arcade-1978-1"
    assert (game.tick, game.phase, game.events) == (0, "playing", [])
    assert (game.player.lives, game.score, game.wave, game.kills, game.shots_fired, game.hits) == (3, 0, 1, 0, 0, 0)
    assert game.hi_score_addr == 0x2F1C
    assert printed(game, 0x2B11, 14) == "PLAY PLAYER<1>"
    assert game.rd(ISR_DELAY) == 0xB0
    assert game.sound_ports == (0, 0)
    assert game.trail is None


def test_only_arcade_mode():
    with pytest.raises(ValueError):
        ArcadeGame(Config(mode="classic"))


def test_round_start_wipes_the_prompt_and_starts_the_rack():
    game = new_game()
    run(game, ROUND_START)
    assert game.rd(SUSPEND_PLAY) == 1 and game.rd(NUM_ALIENS) == 55
    assert game.sound_ports[0] & 0x20  # the amplifier is on
    assert not any(game.vram[0x2B11 - VRAM + 0x20 * i] for i in range(14 * 8))  # PLAY PLAYER<1> wiped
    assert all(game.vram[0x02 + 0x20 * i] == 0x01 for i in range(224))  # the line under the player
    assert any(game.rd(0x2806 + 0x20 * row) for row in range(22))  # the first shield


def test_the_wipe_takes_several_frames():
    game = new_game()
    run(game, 181)
    lit = [c for c in range(224) if any(game.vram[c * 32 + 2 : c * 32 + 0x1C])]
    assert lit and min(lit) > 100 and max(lit) > 150  # ClearPlayField is part way through PLAY PLAYER<1>
    run(game, 4)
    assert not any(game.vram[c * 32 + 2 : c * 32 + 0x1C].strip(b"\0") for c in range(224))


def test_left_right_and_fire(started):
    game = started
    x = game.rd(PLAYER_XR)
    run(game, 20, ["right"])
    assert game.rd(PLAYER_XR) == x + 20
    run(game, 20, ["left"])
    assert game.rd(PLAYER_XR) == x
    shots = game.shots_fired
    game.step(["fire"])
    assert game.shots_fired == shots + 1 and "fire" in game.events
    run(game, 100, ["fire"])  # holding fire does not fire again
    assert game.shots_fired == shots + 1
    game.step()
    game.step(["fire"])
    assert game.shots_fired == shots + 2


def test_the_result_counts_shots_hits_and_kills():
    """One shot from Xr 58, between the first two shields, kills the bottom invader of the rack's third column."""
    game = new_game()
    run(game, ROUND_START)
    while game.rd(OBJ0_TIMER_MSB) | game.rd(OBJ0_TIMER_LSB):
        game.step()
    run(game, 0x28, ["right"])
    game.step(["fire"])
    run(game, 60)
    result = game.result()
    assert (result["shots"], result["hits"], result["kills"], result["score"]) == (1, 1, 1, 10)
    assert game.rd(NUM_ALIENS) == 54


def test_extra_ship_at_1500():
    game = new_game()
    run(game, ROUND_START)
    game.wr(P1_SCOR_M, 0x15)
    game.step()
    assert game.player.lives == 4 and "life" in game.events
    run(game, 5)
    assert game.player.lives == 4


def test_score_wraps_past_9999():
    game = new_game()
    game.wr16(P1_SCOR_L, 0x9990)
    game.wr16(SCORE_DELTA_LSB, 0x0020)
    game.wr(ADJUST_SCORE, 1)
    game.adjust_score()
    assert (game.rd(P1_SCOR_M), game.rd(P1_SCOR_L), game.score_wraps, game.score) == (0x00, 0x10, 1, 10010)


def test_a_cleared_rack_starts_wave_two():
    game = new_game()
    run(game, ROUND_START)
    game.ram[P1_ALIENS - 0x2000 : P1_ALIENS - 0x2000 + 55] = bytes(55)
    for _ in range(200):
        game.step()
        if game.wave == 2 and game.rd(SUSPEND_PLAY):
            break
    assert game.wave == 2 and game.rd(SUSPEND_PLAY) == 1
    assert game.rd(NUM_ALIENS) == 55 and game.rd(P1_RACK_CNT) == 1
    assert game.rd(P1_REF_ALIEN_Y) == game.rom[0x1DA3]  # the second rack's start row


def test_no_input_ends_in_game_over():
    game = new_game()
    lives = [game.player.lives]
    while game.phase == "playing" and game.tick < 4000:
        game.step()
        if game.player.lives != lives[-1]:
            lives.append(game.player.lives)
    assert game.phase == "game_over" and lives == [3, 2, 1, 0]
    assert game.tick == 2345  # the original's game over with no input ends at the same frame
    assert printed(game, 0x2D18, 9) == "GAME OVER"  # typed, then wiped with the playfield
    assert not any(game.vram[0x2D18 - VRAM + 0x20 * i] for i in range(9 * 8))
    result = game.result()
    assert set(result) == {"score", "wave", "kills", "shots", "hits", "best_combo", "ticks", "victory"}
    assert result["ticks"] == game.tick and result["score"] == game.score and result["victory"] is False
    tick, digest = game.tick, game.digest()
    game.step(["fire"])
    assert (game.tick, game.digest()) == (tick, digest)


def script(k):
    return [("fire",), ("left",), ("right", "fire"), ()][(k // 7) % 4]


def test_same_inputs_same_game():
    a, b = new_game(), new_game()
    assert a.digest() == b.digest()
    for k in range(600):
        a.step(script(k))
        b.step(script(k))
        if k % 100 == 99:
            assert a.digest() == b.digest()
    b.step(["right"])
    a.step()
    assert a.digest() != b.digest()


def test_trail_records_rom_labels():
    game = new_game(trail=200)
    run(game, ROUND_START + 2)
    assert {"ScanLine96", "ScanLine224", "PlrFireOrDemo", "CountAliens"} <= set(game.trail)
    assert len(game.trail) == 200


# ---- sound events ----


@pytest.mark.parametrize(
    ("before", "writes", "events"),
    [
        ((0x20, 0x00), [(SOUND2, 0x02)], ["march2"]),
        ((0x20, 0x00), [(SOUND2, 0x02), (SOUND2, 0x00)], []),  # the rack end's 57-cycle note
        ((0x2A, 0x00), [(SOUND1, 0x22), (SOUND1, 0x2A), (SOUND1, 0x20)], []),  # a race's stale bit, cleared again
        ((0x2A, 0x00), [(SOUND1, 0x22), (SOUND1, 0x2A)], ["kill"]),  # cleared and set again: the one-shot fires again
        ((0x24, 0x00), [(SOUND1, 0x00), (SOUND1, 0x04)], []),  # risen with the amplifier off
        ((0x00, 0x00), [(SOUND1, 0x04), (SOUND1, 0x24)], ["blast"]),  # the amplifier on while the bit is still set
        ((0x00, 0x00), [(SOUND2, 0x01)], []),  # the amplifier mutes port 5 too
        ((0x20, 0x02), [(SOUND1, 0x28), (SOUND2, 0x14)], ["kill", "march3", "saucer_hit"]),  # port 3's first
    ],
)
def test_a_sound_is_a_bit_that_rose_in_the_frame_and_is_still_on_at_its_end_with_the_amplifier(before, writes, events):
    game = new_game()
    game._port3, game._port5 = before
    game.port_writes[:] = writes
    assert game._sound_events() == events
    last = dict([(SOUND1, before[0]), (SOUND2, before[1]), *writes])
    assert (game._port3, game._port5) == (last[SOUND1], last[SOUND2])  # where the next frame's rises start from


def test_the_saucer_and_its_hit_repeat_every_8_and_10_frames_while_their_bits_stay_on():
    """The saucer's SN76477 warbles at 7.2 Hz and the saucer hit's op-amp VCO at 6.1 Hz for as long as their bits are
    on: each is sent at its rise and again every 8 or 10 frames while its bit stays on, and not while the amplifier is
    off."""
    game = new_game()
    game._port3, game._port5 = 0x20, 0x00
    script = {3: [(SOUND1, 0x21)], 30: [(SOUND2, 0x10)], 33: [(SOUND1, 0x20)], 50: [(SOUND1, 0x00)]}
    script |= {60: [(SOUND1, 0x20)], 75: [(SOUND2, 0x00)]}
    sent = {"saucer": [], "saucer_hit": []}
    for tick in range(100):
        game.tick = tick
        game.port_writes[:] = script.get(tick, [])
        for name in game._sound_events():
            sent[name].append(tick)
    assert sent == {"saucer": [3, 11, 19, 27], "saucer_hit": [30, 40, 60, 70]}


def _sound_writes(game):
    """Log each frame's sound-port writes by the tick its step() ends on."""
    log, cls = {}, type(game)

    def events():
        log[game.tick + 1] = list(game.port_writes)
        return cls._sound_events(game)

    game._sound_events = events
    return log


def test_the_rack_end_s_march_pulse_is_silent():
    """In the wait after a rack's last kill, a note that falls due is set and cleared 57 cycles later (TimeFleetSound
    1757 and 1772 with numAliens 0), which the cabinet never sounds: the march stays silent from the rack's end to the
    next rack's first note, 64 frames after 0817 enables the game tasks again."""
    game = new_game()
    run(game, ROUND_START + 230)
    writes = _sound_writes(game)
    game.ram[P1_ALIENS - RAM : P1_ALIENS - RAM + 55] = bytes(55)  # the rack's last kill
    marches, suspended = [], []
    for _ in range(140):
        game.step()
        if any(name.startswith("march") for name in game.events):
            marches.append(game.tick)
        if not game.rd(SUSPEND_PLAY):
            suspended.append(game.tick)
    notes = {t: [v & 0x0F for port, v in w if port == SOUND2] for t, w in writes.items()}
    pulses = [t for t, n in notes.items() if any(n) and n[-1] == 0]  # a note set and cleared within the frame
    restart = suspended[-1] + 1  # 0817: suspendPlay 1 again
    assert len(pulses) == 1 and pulses[0] < suspended[0] and suspended == list(range(suspended[0], restart))
    assert marches == [restart + 64]


def test_the_invasion_game_over_s_second_explosion_rise_is_silent():
    """When the invasion's blow-up ends, 02C1 in the end-screen interrupt clears port 3, the amplifier too, while the
    main thread is in 16EE's SoundBits3On(04), which then writes 04 back: the explosion bit rises again 59 frames after
    the cannon's blast, with the amplifier off. The cabinet stays silent (MAME mutes everything), and so does the
    game."""
    game = new_game()
    game.step()
    game.ram[P1_ALIENS - RAM : P1_ALIENS - RAM + 55] = bytes([1] + [0] * 54)  # alien 0 alone plunges to the cannon
    writes = _sound_writes(game)
    blasts = []
    while game.phase == "playing":
        game.step()
        if "blast" in game.events:
            blasts.append(game.tick)
        assert game.tick < 1000
    race = [t for t, w in writes.items() if [v for port, v in w if port == SOUND1][-2:] == [0x00, 0x04]]
    assert len(race) == 1 and blasts == [race[0] - 59]


def _vram_pattern(game):
    game.vram[:] = bytes((i * 7 + 1) & 0xFF for i in range(len(game.vram)))


PACED = [
    ("clear_play_field", ()),
    ("draw_bottom_line", ()),
    ("restore_shields1", ()),
    ("restore_shields2", ()),
    ("copy_ram_mirror", ()),
    ("draw_shield_pl1", ()),
    ("draw_shield_pl2", ()),
    ("init_aliens", ()),
    ("init_aliens_p2", ()),
    ("init_rack", ()),
    ("remove_ship", ()),
    ("draw_char", (0x1A, 0x2D18)),
    ("print_hi_score", ()),
    ("sub_19fa", (0x2701,)),
    ("enable_game_tasks", ()),
    ("sub_1a8b", (0,)),
]


@pytest.mark.parametrize(("routine", "args"), PACED, ids=[p[0] for p in PACED])
def test_paced_routines_land_their_writes_when_their_instructions_start(routine, args):
    """A routine run paced counts the cycles it counts run plainly and ends in the same state; the cycles it logs
    for its writes rise and lie inside its running time; at each interrupt it spans, exactly the writes logged
    before that point have landed (and the sound-port writes among them have been made)."""
    plain, traced, paced = new_game(), new_game(), new_game()
    for g in (plain, traced, paced):
        _vram_pattern(g)
    before = bytes(plain.ram) + bytes(plain.vram)
    c0 = plain.cycles
    getattr(plain, routine)(*args)
    total = plain.cycles - c0
    after = bytes(plain.ram) + bytes(plain.vram)
    traced._trace, t0 = [], traced.cycles
    getattr(traced, routine)(*args)
    trace, traced._trace = traced._trace, None
    times = [t - t0 for t, _, _, _ in trace]
    assert trace and times == sorted(times) and 0 <= times[0] and times[-1] < total

    def expected(elapsed):
        state = bytearray(before)
        for t, addr, old, new in trace:
            if old is not None and t - t0 < elapsed:
                state[addr - 0x2000] = new
        return bytes(state)

    rooms = (min(total // 3, 900) or 1, 16218)
    paced._busy, paced._room = 0, rooms[0]
    paced.port_writes.clear()
    elapsed = gaps = 0
    for _ in paced._paced(getattr(paced, routine), *args):
        elapsed += paced._room - (paced._busy if gaps == 0 else 0)
        assert bytes(paced.ram) + bytes(paced.vram) == expected(elapsed)
        assert len(paced.port_writes) == sum(1 for t, _, old, _ in trace if old is None and t - t0 < elapsed)
        gaps += 1
        paced._busy, paced._carry, paced._room = paced._carry, 0, rooms[gaps % 2]
    assert bytes(paced.ram) + bytes(paced.vram) == after
    assert paced.port_writes == plain.port_writes
    used = splits = 0
    while total - used > rooms[splits % 2]:
        used += rooms[splits % 2]
        splits += 1
    assert (gaps, elapsed, paced._busy) == (splits, used, total - used)


def test_clear_play_field_on_the_main_thread_lands_its_stores_as_paced():
    """The flows' ClearPlayField, which does not log its stores, leaves at every interrupt it spans what the paced
    routine would, and spends the same time."""
    runs = []
    for paced in (True, False):
        game = new_game()
        _vram_pattern(game)
        game._busy, game._room = 0, 977
        flow = game._paced(game.clear_play_field) if paced else game._clear_play_field()
        states = []
        for _ in flow:
            states.append((bytes(game.vram), game._carry))
            game._busy, game._carry, game._room = game._carry, 0, 16218
        runs.append((states, bytes(game.vram), game._busy))
    assert runs[0] == runs[1] and len(runs[0][0]) == 24


def test_idle_interrupts_cost_what_they_cost_on_the_original():
    """With game tasks off (the prompt), the mid-screen interrupt runs 163 cycles and the end-screen one 288, RST
    included (measured on the original program, where every interrupt of every game costs what the port counts); the
    main thread gets the rest of the gap."""
    game = new_game()
    for mid, gap, cost in ((True, machine._MID_GAP, 163), (False, machine._END_GAP, 288)):
        c0 = game.cycles
        game._mid_next = mid
        game._interrupt()
        assert game.cycles - c0 == cost and game._room == gap - cost and game._mid_next is not mid


def test_an_instruction_started_before_the_due_time_runs_before_the_interrupt():
    """The interrupt is taken at the first instruction boundary at or after its due time: an instruction that starts
    before it runs first, and the cycles it runs past the due time are the main thread's first after the interrupt."""
    game = new_game()
    game._busy, game._room = 0, 20
    flow = game._step(13, 13, 4)
    assert next(flow) == "busy" and game._busy == 26 and game._carry == 6
    game._busy, game._carry, game._room = game._carry, 0, 100
    assert list(flow) == [] and game._busy == 10


@pytest.mark.parametrize(
    ("room", "ram", "port"), [(0, 0x04, 0x04), (10, 0x25, 0x25), (17, 0x25, 0x25), (18, 0x00, 0x25), (31, 0x00, 0x25)]
)
def test_an_interrupt_inside_sound_bits3_on_loses_its_change_to_sound_port3(room, ram, port):
    """SoundBits3On, which the flows run counted (0817, 16EE's loop), reads soundPort3 (LD A at 18FA, 13 cycles), ORs
    B in (4), stores it (LD at 18FE, 13) and writes it to port 3 (10): an interrupt that clears soundPort3
    (GameObj0's SoundBits3Off(00) at the end of the player's blow-up, with the main thread in 16EE's loop) taken
    after the read and before the store has its change overwritten, as on the original. Taken before the read, the
    bit lands on the cleared value; taken after the store, the clearing stands in RAM and the port gets the value
    read before it."""
    game = new_game()
    game.wr(SOUND_PORT3, 0x21)
    game._busy, game._room, game._mid_next = 0, room, False
    game._isr = _stub_isr(game, 50, [], change=lambda g: g.wr(SOUND_PORT3, 0x00))
    try:
        for _ in game._counted(game.sound_bits3_on, 0x04):
            game._interrupt()
            game._busy, game._carry = game._carry, 0
    finally:
        del game._isr
    assert (game.rd(SOUND_PORT3), game.sound_port3) == (ram, port)


def test_a_stack_reset_starts_its_flow_after_the_interrupt_and_the_waited_instruction():
    """An interrupt that resets the stack (GameObj0 into 02D0, 1971 into 16E6) gives the flow it enters the gap less
    the interrupt's cycles up to the reset and less the part of the abandoned instruction that ran past the due
    time."""
    game = new_game()

    def isr():
        game.cycles += 1000
        raise Jump(game.sub_16e6())

    game.scan_line96 = isr
    game._carry, game._mid_next = 7, True
    game._interrupt()
    assert game._room == machine._MID_GAP - 1011 - 7 and game._carry == 0


def test_an_interrupt_due_on_the_arrival_in_the_game_loop_comes_before_the_first_pass():
    """0817 runs 111 cycles up to SoundBits3On's RET (10 cycles): with the interrupt due at 115 the RET runs first and
    the interrupt is taken on the arrival at 081F, before the first pass starts, which counts the 6 cycles the RET
    ran past the due time as its own."""
    game = new_game()
    run(game, ROUND_START)
    game._main = game._run(game.sub_0817())
    game._carry, game._room, game._mid_next = 0, 115, False
    starts = []
    inner = game._pass

    def watched():
        starts.append((game._gaps, game._room - (game._due - game.cycles)))
        return inner()

    game._pass = watched
    gaps = game._gaps
    game._main_run()
    assert starts[0] == (gaps + 1, 6) and game._pos == "cut"


def test_a_wait_loop_ends_where_it_would_with_every_pass_run():
    """_poll skips the passes of a wait loop that cannot change anything before the next interrupt: WaitOnDelay
    ends at the same main-thread cycle, after the same interrupts, as with every pass run."""

    def every_pass(iteration):
        while True:
            done = yield from iteration()
            if done:
                return done

    ends = []
    for skip in (True, False):
        game = new_game()
        if not skip:
            game._poll = every_pass
        game._busy, game._room, game._mid_next = 0, 5000, False
        isrs = 0
        for _ in game.wait_on_delay(3):
            game._interrupt()
            game._busy, game._carry = game._carry, 0
            isrs += 1
        ends.append((isrs, game._busy, game.rd(ISR_DELAY)))
    assert ends[0] == ends[1] and ends[0][0] == 5 and ends[0][2] == 0


# ---- the game loop on cycle time ----


def _hook(game, log):
    """Log the game's shared accesses as (cycle, kind, address or port, value): R and W for RAM and VRAM (not the
    ROM), I and O for the ports."""
    cls = type(game)

    def rd(a):
        v = cls.rd(game, a)
        if a & 0x3FFF >= RAM:
            log.append((game.cycles, "R", a & 0x3FFF, v))
        return v

    def wr(a, v):
        cls.wr(game, a, v)
        if a & 0x3FFF >= RAM:
            log.append((game.cycles, "W", a & 0x3FFF, v & 0xFF))

    def inp(p):
        v = cls.inp(game, p)
        log.append((game.cycles, "I", p, v))
        return v

    def out(p, v):
        log.append((game.cycles, "O", p, v & 0xFF))
        cls.out(game, p, v)

    game.rd, game.wr, game.inp, game.out = rd, wr, inp, out


def _unhook(game):
    del game.rd, game.wr, game.inp, game.out


def _state(game):
    return bytes(game.ram), bytes(game.vram), game.shift_reg, game.shift_amount, game.sound_port3, game.sound_port5


def _effects_until(start, log, t):
    """The state `start` (as _state gives it) with the writes and OUTs of `log` made before cycle t applied."""
    ram, vram, reg, amount, p3, p5 = start
    ram, vram = bytearray(ram), bytearray(vram)
    for c, kind, a, v in log:
        if c >= t:
            break
        if kind == "W":
            if a >= VRAM:
                vram[a - VRAM] = v
            else:
                ram[a - RAM] = v
        elif kind == "O":
            if a == 2:
                amount = v & 7
            elif a == 4:
                reg = (v << 8) | (reg >> 8)
            elif a == 3:
                p3 = v
            elif a == 5:
                p5 = v
    return bytes(ram), bytes(vram), reg, amount, p3, p5


def _fire_ready(game):
    """PlrFireOrDemo reads the controls (IN 1); a queued score is printed (VRAM)."""
    game.wr(PLAYER_ALIVE, 0xFF)
    game.wr(OBJ0_TIMER_MSB, 0)
    game.wr(OBJ0_TIMER_LSB, 0)
    game.wr(PLYR_SHOT_STATUS, 0)
    game.wr(FIRE_BOUNCE, 0)
    game.wr16(SCORE_DELTA_LSB, 0x0010)
    game.wr(ADJUST_SCORE, 1)


def _shot_hits_alien(game):
    """The player's shot is in the rack's lowest row over its first column: CodeBug1 kills the alien there and draws
    the explosion through the shifter (OUT 2, OUT 4, IN 3)."""
    game.wr(PLYR_SHOT_STATUS, 2)
    game.wr(OBJ1_COOR_YR, game.rd(REF_ALIEN_YR) + 2)
    game.wr(OBJ1_COOR_XR, game.rd(REF_ALIEN_XR) + 0x14)
    game.wr(ALIEN_IS_EXPLODING, 1)


@pytest.fixture(scope="module")
def pass_starts():
    """Saved states (ArcadeGame._save) of a game in its first rack, set up for a pass of each kind of access."""
    game = new_game()
    run(game, ROUND_START + 20)
    game._split = game._pokes = None
    saved = {}
    for name, setup in (("fire", _fire_ready), ("hit", _shot_hits_alien)):
        base = game._save()
        setup(game)
        saved[name] = game._save()
        game._restore(base)
    return game, saved


def _reference(game, saved):
    """Run the pass from `saved` with every instruction counted and no interrupt: (start state, cycle at the start,
    instruction boundaries, accesses, state after, cycles it ran)."""
    game._restore(saved)
    game._mid_next, game._pokes = False, None
    start, c0 = _state(game), game.cycles
    log, bounds = [], []

    def late(lengths):
        t = game.cycles - sum(lengths)
        for n in lengths:
            bounds.append(t)
            t += n

    _hook(game, log)
    game._late, game._due = late, 0
    try:
        game._pass()
    finally:
        _unhook(game)
        del game._late
    bounds.append(game.cycles)
    return start, c0, bounds, log, _state(game), game.cycles - c0


def _stub_isr(game, cost, seen, flow=None, toggle=False, change=None):
    """An interrupt routine of `cost` cycles that records the state it finds, then makes `change` (a function of the
    game); returns `flow` (a stack reset)."""

    def isr(mid):
        seen.append((mid, _state(game)))
        game.cycles += cost
        if change is not None:
            change(game)
        if toggle:
            game._mid_next = not mid
        return flow

    return isr


def _flip_ram(a):
    def change(game):
        game.ram[a - RAM] ^= 0xFF

    return change


def _load_shifter(game):
    game.shift_reg ^= 0xFFFF


KINDS = [
    ("RAM read", "fire", lambda e: e[1] == "R" and e[2] < VRAM),
    ("RAM write", "fire", lambda e: e[1] == "W" and e[2] < VRAM),
    ("VRAM write", "fire", lambda e: e[1] == "W" and e[2] >= VRAM),
    ("sound OUT 3", "fire", lambda e: e[1:3] == ("O", 3)),
    ("controls IN 1", "fire", lambda e: e[1:3] == ("I", 1)),
    ("shift amount OUT 2", "hit", lambda e: e[1:3] == ("O", 2)),
    ("shift data OUT 4", "hit", lambda e: e[1:3] == ("O", 4)),
    ("shifter IN 3", "hit", lambda e: e[1:3] == ("I", 3)),
]


@pytest.mark.parametrize(("kind", "setup", "match"), KINDS, ids=[k[0] for k in KINDS])
def test_an_interrupt_inside_a_pass_comes_between_the_accesses_of_its_instructions(pass_starts, kind, setup, match):
    """Due at the start of the instruction that makes an access, the interrupt is taken before it: it finds memory,
    shifter and sound latches as the instructions before left them, and the access comes after it (a read sees what
    the interrupt changed). Due one cycle later, it is taken at the next boundary, after the access. The rest of the
    pass moves on by the interrupt's cycles and ends as it would have."""
    game, saved = pass_starts
    start, c0, bounds, log, after, ran = _reference(game, saved[setup])
    s, _, where, value = next(e for e in log if match(e) and e[0] > c0)
    assert s in bounds
    for due, t in ((s, s), (s + 1, bounds[bounds.index(s) + 1])):
        at = s + 50 if t == s else s
        for change in (None, {"RAM read": _flip_ram(where), "shifter IN 3": _load_shifter}.get(kind)):
            game._restore(saved[setup])
            game._mid_next, game._pokes, game._due = False, None, due
            seen, got = [], []
            game._isr = _stub_isr(game, 50, seen, change=change)
            _hook(game, got)
            try:
                game._pass()
            finally:
                _unhook(game)
                del game._isr
            assert [m for m, _ in seen] == [False] and seen[0][1] == _effects_until(start, log, t)
            made = [e for e in got if e[0] == at and match(e)]
            if change is None:
                assert game.cycles - c0 == ran + 50 and _state(game) == after
                assert made and made[0][2:] == (where, value)
            elif t == s:
                assert made and made[0][3] != value  # the read sees the interrupt's change
            else:
                assert made and made[0][3] == value


def test_the_frame_boundary_inside_a_pass_brings_the_next_frame_s_controls():
    """IN 1 reads the next frame's controls once the mid-screen interrupt that ends the frame is taken, and the
    current frame's before it: due at the start of the IN, the press is seen and the shot starts."""
    game = new_game()
    run(game, ROUND_START + 20)
    game._split = game._pokes = None
    _fire_ready(game)
    saved = game._save()
    start, c0, bounds, log, after, ran = _reference(game, saved)
    s = next(e[0] for e in log if e[1:3] == ("I", 1))
    for due, fired in ((s, True), (s + 1, False)):
        game._restore(saved)
        game._mid_next, game._pokes, game._due = True, (), due
        game._in1, game._in1_next = 0x09, 0x19
        seen = []
        game._isr = _stub_isr(game, 50, seen, toggle=True)
        try:
            game._pass()
        finally:
            del game._isr
        assert [m for m, _ in seen] == [True] and game._pokes is None and game._in1 == 0x19
        assert (game.rd(PLYR_SHOT_STATUS) == 1) is fired


def test_several_interrupts_in_one_pass_each_at_the_first_boundary_after_its_due_time(pass_starts, monkeypatch):
    """With gaps shorter than a pass, interrupts come one after another inside it: each at the first instruction
    boundary at or after its due time, the gap counted from the due time before it, whatever the interrupts before it
    cost. The second mid-screen one ends the frame: the pass is cut there."""
    game, saved = pass_starts
    start, c0, bounds, log, after, ran = _reference(game, saved["hit"])
    monkeypatch.setattr(machine, "_MID_GAP", 900)
    monkeypatch.setattr(machine, "_END_GAP", 700)
    game._restore(saved["hit"])
    game._mid_next, game._pokes, game._due = False, (), c0 + 333
    seen = []
    game._isr = _stub_isr(game, 120, seen, toggle=True)
    try:
        with pytest.raises(machine._Boundary):
            game._pass()
    finally:
        del game._isr
    due, lost, mid, expect = c0 + 333, 0, False, []
    for _ in range(4):
        t = next(b for b in bounds if b + lost >= due)
        expect.append((mid, _effects_until(start, log, t)))
        if len(expect) == 4:
            break
        lost += 120
        due += 900 if mid else 700
        mid = not mid
    assert ran > 4 * 900 and seen == expect[:3]
    assert _state(game) == expect[3][1]


def test_interrupts_due_inside_one_count_of_instructions_come_each_at_its_own_boundary(monkeypatch):
    """One count of several instructions (a loop's whole run) can hold more than one due time: the interrupts come
    one after another, each at the first boundary at or after its due time with the ones before it run, and one that
    resets the stack ends the count there."""
    monkeypatch.setattr(machine, "_MID_GAP", 37)
    monkeypatch.setattr(machine, "_END_GAP", 29)
    lengths = (4, 7, 10, 5, 13, 4, 11, 17, 7, 10, 5, 4) * 3
    game = new_game()
    c0, flow, seen = game.cycles, iter(()), []

    def isr(mid):
        seen.append(mid)
        game.cycles += 6
        game._mid_next, game._pokes = not mid, ()
        return flow if len(seen) == 5 else None

    game._mid_next, game._pokes, game._due, game._isr = False, (), c0 + 9, isr
    try:
        with pytest.raises(machine._Abandon) as abandon:
            game._tick(*lengths)
    finally:
        del game._isr
    t, due, kinds = c0, c0 + 9, []
    for n in lengths:
        t += n
        if t >= due:
            kinds.append(len(kinds) % 2 == 1)
            if len(kinds) == 5:
                break
            t, due = t + 6, due + (37 if kinds[-1] else 29)
    assert seen == kinds == [False, True, False, True, False] and abandon.value.flow is flow
    assert game._room == 29 - 6 - (t - due) and t - due > 0


def test_a_stack_reset_inside_a_pass_ends_the_game_loop_where_the_interrupt_comes(pass_starts):
    """An interrupt taken inside a pass that resets the stack ends the pass there, with the instructions before it
    done; the flow it enters replaces the game loop and has the gap less the interrupt's cycles and less the part of
    the instruction it waited for that ran past its due time."""
    game, saved = pass_starts
    start, c0, bounds, log, after, ran = _reference(game, saved["hit"])
    i = next(i for i, b in enumerate(bounds) if b - c0 > 1000)
    t, due = bounds[i], bounds[i] - 3  # inside the instruction before: every instruction takes 4 cycles or more
    assert bounds[i - 1] < due

    def landed():
        yield "landed"

    flow = landed()
    game._restore(saved["hit"])
    game._mid_next, game._pokes, game._busy, game._room = False, None, 0, due - game.cycles
    game._main = game._run(game.game_loop())
    seen = []
    game._isr = _stub_isr(game, 400, seen, flow=flow)
    try:
        assert next(game._main) == "landed"
    finally:
        del game._isr
    assert seen[0][1] == _effects_until(start, log, t)
    assert game._room == machine._END_GAP - 400 - 3 and (game._busy, game._carry, game._due) == (0, 0, NEVER)


def test_a_frame_that_ends_inside_a_pass_runs_it_again_from_its_start():
    """step() returns where the mid-screen interrupt is due, inside a pass, with memory as the pass left it there.
    The next step() runs that pass again from its start: it repeats exactly up to that point, where the changes made
    to memory between the frames and the new frame's controls come in, then the interrupt. The sound writes the pass
    made before the cut belong to the frame that ended there, and its reads of the controls before the cut see that
    frame's controls again."""
    game = new_game()
    run(game, ROUND_START + 140, ["right"])  # the player's start delay is over: each pass reads the controls
    replayed, reads = [], []
    cls = type(game)

    def out(p, v):
        if p in (3, 5) and game._replay:
            replayed.append((p, v))
        cls.out(game, p, v)

    def inp(p):
        v = cls.inp(game, p)
        if p == 1:
            reads.append((game._replay, v))
        return v

    def isr(mid):
        if mid:
            seen.append((bytes(game.ram), bytes(game.vram), game._in1, game._replay, list(game.port_writes)))
            reads.append(("mid", game._in1))
        return cls._isr(game, mid)

    game.out, game.inp, game._isr = out, inp, isr
    before, again = 0x49, 0
    for k in range(30):
        assert game._split is not None and game._pos == "cut"
        ram, vram = game._split
        assert (bytes(game.ram), bytes(game.vram)) == (ram, vram)
        poked = bytearray(ram)
        poked[EXTRA_HOLD - RAM] ^= 0x5A
        game.ram[:] = poked
        seen, reads[:] = [], []
        now = 0x29 if k % 2 == 0 else 0x49
        game.step(["left"] if now == 0x29 else ["right"])
        assert seen[0] == (bytes(poked), vram, now, False, []) and game._pokes is None
        mid = reads.index(("mid", now))
        assert all(v == before for _, v in reads[:mid]) and all(r is True for r, _ in reads[:mid])
        assert all(v == now for r, v in reads[mid + 1 :] if r != "mid")
        before, again = now, again + mid
    assert replayed  # sound writes made again before a cut, and not reported again
    assert again  # the controls read again before a cut: that frame's own


# ---- routines a flow runs counted ----


def _shot_over_the_rack():
    """A game in its first rack whose player's shot moves at Yr CC with no alien exploding: PlayerShotHit reads the
    shot's Yr (LD A,(2029), 37 cycles in), then alienIsExploding (LD A,(2002), 72 cycles in, 13 long), which ends it
    (RET Z) unless an interrupt has set it; then CodeBug1 takes the shot (status 5, or 3 for an empty slot)."""
    game = new_game()
    run(game, ROUND_START + 20)
    game._split = game._pokes = None
    game._mid_next, game._due, game._replay = False, NEVER, False
    game.wr(PLYR_SHOT_STATUS, 2)
    game.wr(OBJ1_COOR_YR, 0xCC)
    game.wr(ALIEN_IS_EXPLODING, 0)
    return game


def _sets_exploding(game):
    game.wr(ALIEN_IS_EXPLODING, 1)


@pytest.mark.parametrize(("room", "seen"), [(72, True), (73, False)])
def test_an_interrupt_inside_a_counted_routine_comes_between_its_accesses(room, seen):
    """A flow runs PlayerShotHit counted (the invasion's 16EE loop, where the interrupts move the shot and may set
    alienIsExploding): an interrupt due at the start of the read of alienIsExploding is taken before it and the read
    sees what it changed (the shot goes on into CodeBug1); due a cycle later, it comes after the read (RET Z). After
    the routine the flow's time since the interrupt's due time is the routine's cycles less those before it."""
    game = _shot_over_the_rack()
    game._busy, game._room = 0, room
    isrs = []
    game._isr = _stub_isr(game, 50, isrs, change=_sets_exploding)
    c0 = game.cycles
    try:
        assert list(game._counted(game.player_shot_hit)) == []
    finally:
        del game._isr
    assert [m for m, _ in isrs] == [False] and isrs[0][1][0][ALIEN_IS_EXPLODING - RAM] == 0
    assert (game.rd(PLYR_SHOT_STATUS) != 2) is seen
    assert game._due == NEVER and game._busy == game.cycles - c0 - 50 - room


@pytest.mark.parametrize(("room", "suspended", "overrun"), [(14, 1, 0), (15, 0, 12)])
def test_dsable_game_tasks_counted_stores_at_its_instruction(room, suspended, overrun):
    """DsableGameTasks (XOR A; JP 19D3; LD (20E9),A; RET: the store 14 cycles in, 13 long) runs counted at 02D0 and
    16E6, where the interrupts run the game objects until its store and may reset the stack: one due at the store's
    start comes before it and finds the game tasks on; due a cycle later, it waits for the store, finds them off, and
    the flow it enters loses the 12 cycles the store ran past the due time."""
    game = new_game()
    run(game, ROUND_START + 20)
    game._split = game._pokes = None
    game._mid_next, game._due, game._replay = False, NEVER, False
    game._busy, game._room = 0, room

    def landed():
        yield "landed"

    def flow():
        yield from game._counted(game.dsable_game_tasks)
        yield "not abandoned"

    game._main = game._run(flow())
    isrs = []

    def isr(mid):
        isrs.append(game.rd(SUSPEND_PLAY))
        game.cycles += 50
        return landed()

    game._isr = isr
    try:
        assert next(game._main) == "landed"
    finally:
        del game._isr
    assert isrs == [suspended] and game.rd(SUSPEND_PLAY) == suspended
    assert game._room == machine._END_GAP - 50 - overrun


def test_a_stack_reset_inside_a_counted_routine_gives_its_flow_the_overrun():
    """An interrupt that resets the stack inside a counted routine (16E6 re-entered while PlayerShotHit runs in the
    16EE loop) ends the routine and its flow there; the flow it enters has the gap less the interrupt's cycles and
    less the part of the instruction it waited for that ran past its due time (8 of LD A,(2002)'s 13 here)."""
    game = _shot_over_the_rack()

    def landed():
        yield "landed"

    def flow():
        yield from game._counted(game.player_shot_hit)
        yield "not abandoned"

    game._busy, game._room = 0, 77
    game._main = game._run(flow())
    isrs = []
    game._isr = _stub_isr(game, 400, isrs, flow=landed(), change=_sets_exploding)
    try:
        assert next(game._main) == "landed"
    finally:
        del game._isr
    assert game._room == machine._END_GAP - 400 - 8 and (game._busy, game._carry, game._due) == (0, 0, NEVER)
    assert game.rd(PLYR_SHOT_STATUS) == 2  # the routine stopped at the interrupt


@pytest.mark.parametrize("poke", [False, True])
def test_a_frame_that_ends_inside_a_counted_routine_runs_it_again_from_its_start(poke):
    """The mid-screen interrupt due inside a counted routine ends the frame there: the flow yields with memory as the
    routine left it. The next step() runs the routine again from its start; at the cut the changes made to memory
    between the frames and the new frame's controls come in, then the interrupt, and the routine's later reads see
    them."""
    game = _shot_over_the_rack()
    game._busy, game._room, game._mid_next = 0, 72, True
    game._in1, game._in1_next = 0x09, 0x19
    flow = game._counted(game.player_shot_hit)
    assert next(flow) == "cut" and game._split == (bytes(game.ram), bytes(game.vram))
    if poke:
        game.ram[ALIEN_IS_EXPLODING - RAM] = 1
    isrs = []
    game._isr = _stub_isr(game, 50, isrs, toggle=True)
    try:
        assert list(flow) == []
    finally:
        del game._isr
    assert [m for m, _ in isrs] == [True] and isrs[0][1][0][ALIEN_IS_EXPLODING - RAM] == poke
    assert (game.rd(PLYR_SHOT_STATUS) != 2) is poke
    assert (game._in1, game._split, game._pokes, game._replay) == (0x19, None, None, False)


def test_a_lone_invader_that_wraps_round_into_ram_ends_the_game():
    """Invader 11 alone, no input: its trail lights the left edge column, so the rack's edge test misses it and it
    creeps off the right edge, wraps round to Xr 0 and is drawn over RAM. In frame 612 it reaches the stack, and the
    1978 program goes astray (five frames later it runs off its code): the port ends the game in that frame."""
    game = new_game()
    game.ram[P1_ALIENS - RAM : P1_ALIENS - RAM + 0x37] = bytes(int(i == 11) for i in range(0x37))
    run(game, 611)
    assert game.phase == "playing" and not game._lost
    game.step()
    assert game.phase == "game_over" and game._lost
    game.step()
    assert game.tick == 612


@pytest.mark.parametrize("fast", [True, False])
def test_count_aliens_reads_where_player_data_msb_points(fast):
    """With playerDataMSB overwritten (a lone invader drawn over RAM), CountAliens counts the flags of the page it
    names: 7A is 3A through the 14-bit address decode, a VRAM page. Counted at once or alien by alien around an
    interrupt, the count is the same."""
    game = new_game()
    run(game, ROUND_START)
    game.wr(PLAYER_DATA_MSB, 0x7A)
    page = bytes((i * 37) & 0x03 for i in range(0x37))
    game.vram[0x3A00 - VRAM : 0x3A37 - VRAM] = page
    if not fast:
        game._due, game._mid_next, game._pokes = game.cycles + 500, False, ()
        game._isr = _stub_isr(game, 50, [])
    try:
        game.count_aliens()
    finally:
        game.__dict__.pop("_isr", None)
    assert game.rd(NUM_ALIENS) == sum(1 for v in page if v)


def test_the_second_player_s_controls_are_the_first_player_s_on_the_upright_cabinet():
    """ReadInputs reads IN2 when playerDataMSB is even, which a one-player game reaches only with it overwritten:
    the upright cabinet wires the same controls to IN1 and IN2."""
    game = new_game()
    game._in1 = 0x09 | 0x40
    for msb in (0x21, 0x22, 0x7A):
        game.wr(PLAYER_DATA_MSB, msb)
        assert game.read_inputs() & 0x70 == 0x40


def test_a_write_to_the_stack_page_ends_the_game_with_the_frame(started):
    """The port does not model the stack (23D0-23FF): the program writes there only once it has gone astray, and the
    game ends with that frame."""
    game = started
    game.wr(STACK - 1, 0x55)
    assert not game._lost
    game.wr(STACK, 0x55)
    tick = game.tick
    game.step()
    game.step()
    assert game.phase == "game_over" and game.tick == tick + 1


@pytest.mark.parametrize("where", ["handler", "end mark"])
def test_an_object_list_gone_astray_ends_the_game(where):
    """RunGameObjs calls a handler that is none of the five, or walks past the list's end mark (2060) into the
    stack: the 1978 program would leave its code or overwrite its stack."""
    game = new_game()
    if where == "handler":
        game.ram[0x60:0x66] = bytes((0, 0, 0, 0x34, 0x12, 0))
    else:
        game.ram[0x60 : STACK - RAM : 0x10] = b"\xfe" * ((STACK - 0x2060) // 0x10)
    game.sub_024b(0x2060)
    assert game._lost


def test_demo_play_and_the_player_switch_end_the_game(started):
    """A credited one-player game reaches demo play (gameMode 0 in the game loop) or the player switch (twoPlayers
    set at the end of a blow-up) only with their bytes overwritten; neither is ported."""
    game = started
    game.wr(GAME_MODE, 0)
    game.plr_fire_or_demo()
    assert game._lost
    game = new_game()
    run(game, ROUND_START)
    game.wr(TWO_PLAYERS, 1)
    game._main = game._run(game.sub_02d0())
    game._busy, game._room, game._mid_next = 0, 1 << 30, False
    game._main_run()
    assert game._lost and game.phase == "game_over"


def test_bcd_add_is_add_then_daa():
    assert bcd_add(0x99, 0x01) == (0x00, 1)
    assert bcd_add(0x01, 0x99) == (0x00, 1)  # one credit taken
    assert bcd_add(0x00, 0x99) == (0x99, 0)
    assert bcd_add(0x45, 0x55) == (0x00, 1)
    assert bcd_add(0x09, 0x00, 1) == (0x10, 0)
    for a in range(100):
        for b in range(100):
            r, cy = bcd_add(int(str(a), 16), int(str(b), 16))
            assert (r, cy) == (int(str((a + b) % 100), 16), int(a + b >= 100))


def test_step_is_fast(started):
    game = started
    times = []
    for k in range(300):
        t = time.perf_counter()
        game.step(script(k))
        times.append(time.perf_counter() - t)
    times.sort()
    assert times[len(times) // 2] < 0.001
