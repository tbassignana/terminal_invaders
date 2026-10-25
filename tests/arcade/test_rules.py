"""The 1978 program's rules, as observed on ArcadeGame: the research checklist (Phase 3), one test per behaviour.

Each test names the behaviour and cites the ROM routine or address that makes it. The game runs on the shipping image
(build_image(): the program's tables and art.py's pictures, a few of them the ROM's, for play), which is also where
the tests look up pictures (game.rom). Tests drive the game through step() and RAM pokes at frame boundaries, and
read RAM, VRAM, the text layer and the interface.

Frames: frame 0 is the constructor's state (PromptPlayer's wait). One step() is the mid-screen interrupt, then the
end-screen one. `play_start()` stops at frame P, the first whose end-screen half ran the game tasks (0817 enabled
them): the checklist's tick n (end-screen half, then mid-screen) has its end-screen half in frame P + n - 1 and its
mid-screen half in frame P + n. Every expectation here was first confirmed on the original program running on an
emulator, with the same setup, inputs and pokes.
"""

import pytest

from terminal_invaders.arcade import ArcadeGame
from terminal_invaders.model import Config

# RAM (RAMUse.md names)
WAIT_ON_DRAW = 0x2000  # waitOnDraw
ALIEN_IS_EXPLODING = 0x2002  # alienIsExploding
EXP_ALIEN_TIMER = 0x2003  # expAlienTimer
ALIEN_FRAME = 0x2005  # alienFrame
ALIEN_CUR_INDEX = 0x2006  # alienCurIndex
REF_ALIEN_D_YR = 0x2007  # refAlienDYr
REF_ALIEN_D_XR = 0x2008  # refAlienDXr
REF_ALIEN_YR = 0x2009  # refAlienYr
REF_ALIEN_XR = 0x200A  # refAlienXr
RACK_DIRECTION = 0x200D  # rackDirection (0 right, 1 left)
OBJ0_TIMER_MSB = 0x2010  # obj0TimerMSB
OBJ0_TIMER_LSB = 0x2011  # obj0TimerLSB
PLAYER_ALIVE = 0x2015  # playerAlive
PLAYER_XR = 0x201B  # playerXr
PLYR_SHOT_STATUS = 0x2025  # plyrShotStatus
BLOW_UP_TIMER = 0x2026  # blowUpTimer
OBJ1_COOR_YR = 0x2029  # obj1CoorYr
OBJ1_COOR_XR = 0x202A  # obj1CoorXr
FIRE_BOUNCE = 0x202D  # fireBounce
ROL_SHOT = 0x2035  # rolShotStatus: status, steps, track, fire pointer (2), blow count, image (2), Yr, Xr, size
PLU_SHOT = 0x2045  # pluShotStatus
SQU_SHOT = 0x2055  # squShotStatus
SHOT_STEPS, SHOT_CFIR, SHOT_BLOW, SHOT_IMAGE, SHOT_YR, SHOT_XR = 1, 3, 5, 6, 8, 9  # offsets in a shot block
PLAYER_OK = 0x2068  # playerOK
ENABLE_ALIEN_FIRE = 0x2069  # enableAlienFire
ALIEN_FIRE_DELAY = 0x206A  # alienFireDelay
SKIP_PLUNGER = 0x206E  # skipPlunger
ALIEN_SHOT_DELTA = 0x207E  # alienShotDelta
SHOT_SYNC = 0x2080  # shotSync
NUM_ALIENS = 0x2082  # numAliens
SAUCER_START = 0x2083  # saucerStart
SAUCER_ACTIVE = 0x2084  # saucerActive
SAUCER_HIT = 0x2085  # saucerHit
SAUCER_HIT_TIME = 0x2086  # saucerHitTime
SAUCER_XR = 0x208A  # saucerPriPicMSB (the saucer's Xr)
SAUCER_DELTA = 0x208C  # saucerDeltaY (the saucer's Xr step)
SAU_SCORE_LSB = 0x208D  # sauScoreLSB
SHOT_COUNT = 0x208F  # shotCountLSB
TILL_SAUCER = 0x2091  # tillSaucerLSB (word)
EXTRA_HOLD = 0x2099  # extraHold
ISR_DELAY = 0x20C0  # isrDelay
A_SHOT_RELOAD_RATE = 0x20CF  # aShotReloadRate
PLAYER1_EX = 0x20E5  # player1Ex
PLAYER1_ALIVE = 0x20E7  # player1Alive
SUSPEND_PLAY = 0x20E9  # suspendPlay
NUM_COINS = 0x20EB  # numCoins
HI_SCOR_L = 0x20F4  # HiScorL
P1_SCOR_L = 0x20F8  # P1ScorL
P1_ALIENS = 0x2100  # player 1's alien flags, 55 bytes
P1_REF_ALIEN_Y = 0x21FC  # p1RefAlienY
P1_RACK_CNT = 0x21FE  # p1RackCnt
P1_SHIPS_REM = 0x21FF  # p1ShipsRem

# Pictures in the program image (the ROM's addresses; ours in the shipping image)
ALIEN_PICS = (0x1C00, 0x1C10, 0x1C20)  # types A, B, C in frame 0; frame 1 is 0x30 further
PLAYER_PIC = 0x1C60
SHIELD_PIC = 0x1D20  # 22 columns x 2 bytes
SHIELD_XR = (64, 109, 154, 199)
ROLLING_PICS, PLUNGER_PICS, SQUIGGLY_PICS = 0x1CEE, 0x1CE2, 0x1CD0  # alien shots: 4 pictures of 3 columns each

RAM, VRAM = 0x2000, 0x2400


def new_game():
    return ArcadeGame(Config(mode="arcade"))


def peek(game, addr):
    return game.ram[addr - RAM] if addr < VRAM else game.vram[addr - VRAM]


def poke(game, addr, value):
    if addr < VRAM:
        game.ram[addr - RAM] = value & 0xFF
    else:
        game.vram[addr - VRAM] = value & 0xFF


def peek16(game, addr):
    return peek(game, addr) | peek(game, addr + 1) << 8


def poke16(game, addr, value):
    poke(game, addr, value)
    poke(game, addr + 1, value >> 8)


def run(game, frames, actions=(), quiet=False):
    for _ in range(frames):
        if quiet:
            fire_off(game)
        game.step(actions)


def run_until(game, cond, limit, actions=(), quiet=False):
    """Step until cond(game) holds; the number of frames stepped (fails after `limit`)."""
    for n in range(limit + 1):
        if cond(game):
            return n
        if quiet:
            fire_off(game)
        game.step(actions)
    raise AssertionError(f"condition not reached in {limit} frames")


def during(cond, limit):
    """Loop passes while cond() holds (tested before each pass, as `while` does); fails if it still holds after
    `limit` passes, so a broken game fails the test instead of hanging it."""
    for n in range(limit + 1):
        if not cond():
            return
        if n == limit:
            raise AssertionError(f"still going after {limit} passes")
        yield n


def fire_off(game):
    """Hold the aliens' fire off (enableAlienFire 0, its delay never running out)."""
    poke(game, ENABLE_ALIEN_FIRE, 0)
    poke(game, ALIEN_FIRE_DELAY, 0x30)


def obj0_timer(game):
    """The player object's timer word (obj0TimerMSB 2010, obj0TimerLSB 2011)."""
    return peek(game, OBJ0_TIMER_MSB) << 8 | peek(game, OBJ0_TIMER_LSB)


def to_play(game, limit=400, quiet=False):
    """Step to the frame whose end-screen half was the first of a turn to run the game tasks (0817 enabled them):
    the player object's timer has gone from 0080 to 007F and alien 0 has been drawn. The frames that took."""
    return run_until(game, lambda g: peek(g, SUSPEND_PLAY) == 1 and obj0_timer(g) == 0x7F, limit, quiet=quiet)


def play_start(alive=None):
    """A new game at to_play(). `alive`, if given, is the set of alien indices left alive, poked at frame 1
    (before any alien is drawn)."""
    game = new_game()
    game.step()
    if alive is not None:
        for i in range(55):
            poke(game, P1_ALIENS + i, int(i in alive))
    to_play(game)
    return game


def cannon_ready(alive=None, quiet=True):
    """A game at the first frame its cannon can fire (the player object's timer word has run out)."""
    game = play_start(alive)
    run_until(game, lambda g: obj0_timer(g) == 0, 200, quiet=quiet)
    return game


def vaddr(xr, yr):
    """The screen address of pixel (xr, yr) (ConvToScr 1A47); its bit is yr % 8."""
    return RAM + xr * 32 + yr // 8


def column(game, xr, yr, height=8):
    """The `height` pixels of column xr from yr up, as an int (bit 0 = yr)."""
    i = vaddr(xr, yr) - VRAM
    word = int.from_bytes(game.vram[i : i + 3], "little")
    return (word >> (yr % 8)) & ((1 << height) - 1)


def pixel(game, xr, yr):
    return column(game, xr, yr, 1)


def lit(game, xr0, xr1, yr0, yr1):
    """The number of lit pixels in the box Xr xr0..xr1, Yr yr0..yr1."""
    mask, vram, n = (1 << (yr1 - yr0 + 1)) - 1, game.vram, 0
    for x in range(xr0, xr1 + 1):
        i = vaddr(x, 0) - VRAM
        n += bin(int.from_bytes(vram[i : i + 32], "little") >> yr0 & mask).count("1")
    return n


def shows(game, pic, xr, yr, cols):
    """Whether the picture at program address `pic` is drawn at (xr, yr) in the given columns (8 rows each)."""
    return all(column(game, xr + c, yr) == game.rom[pic + c] for c in cols)


CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<> "


def cell_xy(addr):
    """The (Xr, Yr) of the screen address where a character or picture starts."""
    return (addr - RAM) >> 5, (addr & 0x1F) * 8


def text(game, addr, n):
    """The n characters the text layer holds from screen address addr on (each cell is 100h further on)."""
    out = []
    for i in range(n):
        code = game.text.get(addr + 0x100 * i)
        out.append(CHARS[code] if code is not None and code < len(CHARS) else "?")
    return "".join(out)


def screen_text(game, addr, n):
    """text(), but only where the cell's 8 screen bytes still show the character's glyph (font 1E00 + code * 8),
    as the view decides; "?" elsewhere."""
    out = []
    for i in range(n):
        a = addr + 0x100 * i
        code = game.text.get(a)
        shown = code is not None and all(
            game.vram[a - VRAM + 32 * j] == game.rom[0x1E00 + 8 * code + j] for j in range(8)
        )
        out.append(CHARS[code] if shown and code < len(CHARS) else "?")
    return "".join(out)


def alien_xy(game, i):
    """Alien i's cell (Xr, Yr) from the reference alien, with the 8-bit wrap (GetAlienCoords 017A)."""
    return (peek(game, REF_ALIEN_XR) + 16 * (i % 11)) & 0xFF, (peek(game, REF_ALIEN_YR) + 16 * (i // 11)) & 0xFF


def alien_pic(i, frame):
    return ALIEN_PICS[(i // 11) // 2] + 0x30 * frame


def score(game):
    lo, hi = peek(game, P1_SCOR_L), peek(game, P1_SCOR_L + 1)
    return (hi >> 4) * 1000 + (hi & 15) * 100 + (lo >> 4) * 10 + (lo & 15)


def set_score(game, value):
    s = f"{value:04d}"
    poke(game, P1_SCOR_L, int(s[2:], 16))
    poke(game, P1_SCOR_L + 1, int(s[:2], 16))


def shot_active(game, base):
    return bool(peek(game, base) & 0x80)


PLAY = 189  # the first rack's play_start() frame, counted from the constructor


# ---- T: timing and order ----


def test_one_alien_drawn_per_frame_in_index_order():  # DrawAlien 0100 (end-screen), CursorNextAlien 0141 (mid-screen)
    game = play_start()
    for k in range(1, 56):
        drawn = [i for i in range(55) if lit(game, *_box(game, i))]
        assert drawn == list(range(k))
        game.step()


def _box(game, i):
    x, y = alien_xy(game, i)
    return x, x + 15, y, y + 15


def test_rack_steps_once_per_live_alien_count():  # 0154-015D skips dead aliens in the same call; 01A1 MoveRefAlien
    for alive in (range(55), range(10), range(20, 27)):
        game = play_start(set(alive))
        steps = []
        for f in range(1, 3 * len(alive) + 2):
            x = peek(game, REF_ALIEN_XR)
            game.step()
            if peek(game, REF_ALIEN_XR) != x:
                steps.append(f)
        first = len(alive) + (0 not in alive)  # the cursor starts on alien 0, drawn or not
        assert steps == [first, first + len(alive), first + 2 * len(alive)]


def test_animation_frame_flips_per_step_and_ripples_through_the_rack():  # 01B1 alienFrame ^= 1; DrawAlien 0129
    game = play_start()
    run(game, 54)
    assert peek(game, ALIEN_FRAME) == 0
    assert all(shows(game, alien_pic(i, 0), 56 + 16 * (i % 11), 120 + 16 * (i // 11), range(2, 14)) for i in range(55))
    for i in range(55):
        game.step()
        assert peek(game, ALIEN_FRAME) == 1
        cells = [(56 + 16 * (j % 11), 120 + 16 * (j // 11)) for j in range(55)]
        assert all(shows(game, alien_pic(j, 1), cells[j][0] + 2, cells[j][1], range(2, 14)) for j in range(i + 1))
        assert all(shows(game, alien_pic(j, 0), *cells[j], range(2, 14)) for j in range(i + 1, 55))


def test_alien_shots_move_every_third_frame_by_shot_sync():  # 0072 shotSync; 047A, 04BC, 0683; CompYToBeam 1A06
    """Each alien shot moves once in 3 frames, in the interrupt that matches its Xr (end-screen for Xr >= 128).
    The end-screen interrupt copies the rolling shot's timer to shotSync: the plunger moves after it copied 1, the
    squiggly after 2; the rolling shot (every third object call) right as it copies 0 at Xr >= 128, and at the
    mid-screen interrupt after it copied 1 at Xr < 128, the same interrupt as a plunger shot there. (The checklist's
    "no tick moves two alien shots" does not hold: the rolling and plunger shots left of Xr 128 move together.)"""
    game = play_start()
    names = {ROL_SHOT: "rolling", PLU_SHOT: "plunger", SQU_SHOT: "squiggly"}
    want = {("rolling", True): 0, ("rolling", False): 1, ("plunger", True): 1, ("plunger", False): 1}
    want |= {("squiggly", True): 2, ("squiggly", False): 2}
    seen, last, together = set(), {}, 0
    sync = peek(game, SHOT_SYNC)
    for f in range(1, 1600):
        set_score(game, 3100)  # reload rate 7: all three shots in flight
        before = {b: (peek(game, b), peek(game, b + SHOT_YR)) for b in names}
        game.step(["right"] if 700 < f < 900 else ())
        moved = []
        for b, (status, yr) in before.items():
            if status != 0x80 or peek(game, b) != 0x80:
                last.pop(b, None)  # not flying: the next move starts a new flight
            elif peek(game, b + SHOT_YR) != yr:
                right = peek(game, b + SHOT_XR) >= 128
                assert (peek(game, SHOT_SYNC) if right else sync) == want[names[b], right], (names[b], f)
                assert f - last.get(b, f - 3) == 3, (names[b], f)
                seen.add((names[b], right))
                last[b] = f
                moved.append(right)
        together += moved.count(False) > 1
        sync = peek(game, SHOT_SYNC)
    assert seen == set(want)
    assert together


# ---- S: start of game, turn and rack ----


def test_game_start_takes_the_credit_and_blinks_the_score_for_176_frames():  # NewGame 07A1; PromptPlayer 088D-08B9
    game = new_game()
    assert peek(game, NUM_COINS) == 0
    assert text(game, 0x2B11, 14) == "PLAY PLAYER<1>"
    assert peek(game, ISR_DELAY) == 0xB0
    for f in range(1, 176):
        game.step()
        blank = (0xB0 - f) & 0x04 != 0  # isrDelay & 4: ClearSmallSprite, else DrawScore
        assert (lit(game, 56, 87, 224, 231) == 0) == blank, f
    run(game, 20)
    assert lit(game, 56, 87, 224, 231) and text(game, 0x271C, 4) == "0000"


def test_play_starts_189_frames_after_the_prompt_appears():  # 07F9-0817: prompt, ClearPlayField, rack, tasks on
    game = new_game()
    assert to_play(game) == PLAY
    xr, yr = cell_xy(0x2B11)
    assert not lit(game, xr, xr + 8 * 14 - 1, yr, yr + 7)  # PLAY PLAYER<1> wiped


def test_three_ships_with_two_in_reserve():  # GetShipsPerCred 08D1; RemoveShip 1A7F at 07F6; DrawNumShips 19E6
    game = play_start()
    assert peek(game, P1_SHIPS_REM) == 2
    assert game.player.lives == 3 and text(game, 0x2501, 1) == "3"
    assert shows(game, PLAYER_PIC, 56, 8, range(16)) and shows(game, PLAYER_PIC, 72, 8, range(16))
    assert not lit(game, 88, 167, 8, 15)


def test_rack_one_starts_at_56_120_moving_right():  # NewGame 07EA; InitRack 00B1-00D3; InitAliens 01C0
    game = play_start()
    assert (peek(game, REF_ALIEN_XR), peek(game, REF_ALIEN_YR)) == (56, 120)
    assert (peek(game, REF_ALIEN_D_XR), peek(game, RACK_DIRECTION), peek(game, ALIEN_FRAME)) == (2, 0, 0)
    assert [peek(game, P1_ALIENS + i) for i in range(55)] == [1] * 55
    assert peek(game, NUM_ALIENS) == 55


def test_cannon_appears_128_frames_after_play_start():  # obj0 timer word 0080 (mirror 1B10), run only at 007B
    for actions, xr in (((), 48), (["right"], 49), (["left"], 48)):
        game = play_start()
        for _ in range(127):
            game.step(actions)
            assert not lit(game, 32, 255, 32, 39)
        assert obj0_timer(game) == 0
        game.step(actions)
        assert peek(game, PLAYER_XR) == xr and shows(game, PLAYER_PIC, xr, 32, range(16))
    assert pixel(game, 56, 39)  # the turret, column 8 of the cannon


def test_fire_is_taken_one_frame_before_the_cannon_appears():  # PlrFireOrDemo 161E-1625; InitPlyShot 03FA
    game = play_start()
    run(game, 127, ["fire"])
    assert peek(game, PLYR_SHOT_STATUS) == 1 and not lit(game, 32, 255, 32, 39)
    game.step(["fire"])
    assert peek(game, PLYR_SHOT_STATUS) == 2
    assert (peek(game, OBJ1_COOR_XR), peek(game, OBJ1_COOR_YR)) == (56, 40)
    assert shows(game, PLAYER_PIC, 48, 32, range(16))


def test_alien_fire_starts_after_48_cannon_frames_with_the_rolling_shot():  # 033B-0343, 03B0-03B8; 1B6A = 30
    game = play_start()
    for _ in range(174):
        game.step()
        assert peek(game, ENABLE_ALIEN_FIRE) == 0
        assert not any(shot_active(game, b) for b in (ROL_SHOT, PLU_SHOT, SQU_SHOT))
    game.step()
    assert peek(game, ENABLE_ALIEN_FIRE) == 1
    run_until(game, lambda g: any(shot_active(g, b) for b in (ROL_SHOT, PLU_SHOT, SQU_SHOT)), 2)
    assert shot_active(game, ROL_SHOT) and not shot_active(game, PLU_SHOT) and not shot_active(game, SQU_SHOT)


def clear_rack(game, quiet=True):
    """Kill every alien left (a poke at this frame boundary) and step to the next rack's play_start() frame; the
    frames that took."""
    for i in range(55):
        poke(game, P1_ALIENS + i, 0)
    run_until(game, lambda g: peek(g, SUSPEND_PLAY) == 0, 100, quiet=quiet)
    return to_play(game, 100)


def test_new_rack_restores_the_start_state_without_a_prompt():  # 09EF-0A39: CopyRAMMirror 09FD, shields 0A2A
    game = cannon_ready()
    run(game, 30, ["right"], quiet=True)
    for x in (100, 120):  # damage shield 2 and erode the bottom line
        for c in range(4):
            poke(game, vaddr(x + c, 48), 0)
            poke(game, vaddr(x + c, 16), 0)
    k = game.tick
    for i in range(55):
        poke(game, P1_ALIENS + i, 0)
    off = run_until(game, lambda g: peek(g, SUSPEND_PLAY) == 0, 100, quiet=True)
    assert off == 48  # 0A3C: isrDelay 30h, then the playfield is cleared
    run_until(game, lambda g: not lit(g, 32, 255, 64, 207), 20)  # ClearPlayField, over some 11 frames
    for _ in during(lambda: not (peek(game, SUSPEND_PLAY) == 1 and obj0_timer(game) == 0x7F), 30):
        assert not lit(game, 88, 199, 136, 143)  # no PLAY PLAYER<1> prompt
        game.step()
    assert game.tick - k == 62
    assert [peek(game, P1_ALIENS + i) for i in range(55)] == [1] * 55
    assert (peek(game, REF_ALIEN_XR), peek(game, REF_ALIEN_YR), peek(game, P1_RACK_CNT)) == (56, 96, 1)
    assert peek(game, PLYR_SHOT_STATUS) == 0 and peek(game, PLAYER_XR) == 48
    assert _shields_fresh(game)
    assert all(column(game, x, 16, 1) for x in range(32, 256))  # DrawBottomLine 01CF
    run(game, 127)
    assert obj0_timer(game) == 0 and not lit(game, 32, 255, 32, 39)
    game.step()
    assert shows(game, PLAYER_PIC, 48, 32, range(16))
    run(game, 46)
    assert peek(game, ENABLE_ALIEN_FIRE) == 0
    game.step()
    assert peek(game, ENABLE_ALIEN_FIRE) == 1


def _shields_fresh(game):
    pic = game.rom[SHIELD_PIC : SHIELD_PIC + 44]
    return all(column(game, x + c, 48, 16) == pic[2 * c] | pic[2 * c + 1] << 8 for x in SHIELD_XR for c in range(22))


def test_every_rack_of_a_one_player_game_starts_moving_right():  # InitRack 00C0 reads 21FB, set to 2 only at 00D7
    game = play_start()
    run_until(game, lambda g: peek(g, RACK_DIRECTION) == 1, 1000, quiet=True)
    clear_rack(game)
    assert (peek(game, REF_ALIEN_D_XR), peek(game, RACK_DIRECTION)) == (2, 0)


# ---- P: the player's cannon ----


def test_cannon_moves_1_per_frame_between_48_and_217():  # MovePlayerRight 0381 (CP D9), MovePlayerLeft 038E (CP 30)
    game = play_start()
    xs = []
    for _ in range(320):
        fire_off(game)
        game.step(["right"])
        xs.append(peek(game, PLAYER_XR))
    assert xs[127] == 49 and xs[295] == 217 and xs[294] == 216 and set(xs[295:]) == {217}
    assert all(b - a in (0, 1) for a, b in zip(xs, xs[1:]))
    assert shows(game, PLAYER_PIC, 217, 32, range(16))
    for f in range(180):
        fire_off(game)
        game.step(["left"])
        assert peek(game, PLAYER_XR) == max(48, 216 - f)


def test_right_wins_when_both_are_held():  # 0366-036C: RLCA; JP C right before left
    game = cannon_ready()
    run(game, 20, ["left", "right"], quiet=True)
    assert peek(game, PLAYER_XR) == 68


def test_cannon_ignores_the_controls_while_it_blows_up():  # GameObj0 0290: the blow-up branch reads no input
    game = cannon_ready()
    run(game, 30, ["right"], quiet=True)
    poke(game, PLAYER_ALIVE, 0)  # hit, as an alien shot's 060F does
    x = peek(game, PLAYER_XR)
    for _ in range(55):
        fire_off(game)
        game.step(["left"])
        assert peek(game, PLAYER_XR) == x


# ---- PS: the player's shot ----


def fire_once(game, actions=(), quiet=True):
    """Press fire for one frame after a frame without it."""
    if quiet:
        fire_off(game)
    game.step(actions)
    if quiet:
        fire_off(game)
    game.step([*actions, "fire"])


def test_one_player_shot_at_a_time():  # PlrFireOrDemo 1618-1647: plyrShotStatus must be 0
    game = cannon_ready()
    status, starts = [peek(game, PLYR_SHOT_STATUS)], 0
    for f in range(400):
        fire_off(game)
        game.step(["fire"] if f % 2 else ())  # tapped every other frame
        status.append(peek(game, PLYR_SHOT_STATUS))
        if status[-1] == 1:
            starts += 1
            assert status[-2] == 0  # a new shot only once the last one is over
    assert starts >= 4 and 2 in status and 3 in status


def test_no_fire_while_the_cannon_blows_up():  # PlrFireOrDemo 1618: playerAlive must be FF
    game = cannon_ready()
    poke(game, PLAYER_ALIVE, 0)
    for _ in range(30):
        fire_once(game)
        assert peek(game, PLYR_SHOT_STATUS) == 0


def test_holding_fire_never_fires_again():  # 1632-1651: fireBounce clears only on a release seen with no shot out
    game = cannon_ready()
    fires = 0
    for f in range(260):
        fire_off(game)
        game.step(["fire"])
        fires += "fire" in game.events
    assert fires == 1 and peek(game, PLYR_SHOT_STATUS) == 0 and peek(game, FIRE_BOUNCE) == 1
    run(game, 10, quiet=True)
    assert peek(game, FIRE_BOUNCE) == 0
    fire_off(game)
    game.step(["fire"])
    assert peek(game, PLYR_SHOT_STATUS) == 1


def test_a_release_during_a_shot_does_not_rearm_fire():  # 1632: fireBounce is only cleared with plyrShotStatus 0
    game = cannon_ready()
    fire_once(game)
    run(game, 3, quiet=True)  # released while the shot flies
    assert peek(game, FIRE_BOUNCE) == 1
    run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) == 0, 100, ["fire"], quiet=True)
    run(game, 20, ["fire"], quiet=True)  # held since before the shot ended: no new shot
    assert peek(game, PLYR_SHOT_STATUS) == 0


def test_shot_launches_at_cannon_plus_8_and_climbs_4_a_frame():  # InitPlyShot 03FA-0407; MovePlyShot 0416 (+4)
    game = cannon_ready()
    run(game, 52, ["right"], quiet=True)
    assert peek(game, PLAYER_XR) == 100
    fire_once(game)
    assert peek(game, PLYR_SHOT_STATUS) == 1
    fire_off(game)
    game.step()
    assert peek(game, PLYR_SHOT_STATUS) == 2
    assert (peek(game, OBJ1_COOR_XR), peek(game, OBJ1_COOR_YR)) == (108, 40)
    assert column(game, 108, 40) == game.rom[0x1C90]  # the 1 x 4 shot at Yr 40-43
    for yr in range(44, 120, 4):
        fire_off(game)
        game.step()
        assert peek(game, OBJ1_COOR_YR) == yr and column(game, 108, yr, 4) == game.rom[0x1C90]


def test_shot_misses_at_the_top_and_explodes_there():  # PlayerShotHit 14E4 (Yr >= D8); 03D7-03F7; EndOfBlowup 0436
    game = cannon_ready()
    fire_once(game)
    fire_off(game)
    game.step()
    assert (peek(game, PLYR_SHOT_STATUS), peek(game, OBJ1_COOR_XR), peek(game, OBJ1_COOR_YR)) == (2, 56, 40)
    moves = run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) != 2, 60, quiet=True)
    assert moves == 44 and peek(game, PLYR_SHOT_STATUS) == 3 and peek(game, OBJ1_COOR_YR) == 216
    fire_off(game)
    game.step()
    assert (peek(game, OBJ1_COOR_XR), peek(game, OBJ1_COOR_YR)) == (53, 214)
    assert all(column(game, 53 + c, 214) == game.rom[0x1C91 + c] for c in range(8))
    assert run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) == 0, 30, quiet=True) == 15
    assert not lit(game, 53, 60, 214, 221)


def pattern_pixels(game, pic, width, xr, yr):
    """The pixels a picture of `width` 1-byte columns covers when drawn at (xr, yr)."""
    return {(xr + c, yr + b) for c in range(width) for b in range(8) if game.rom[pic + c] >> b & 1}


def box_pixels(game, xr0, xr1, yr0, yr1):
    return {(x, y) for x in range(xr0, xr1 + 1) for y in range(yr0, yr1 + 1) if pixel(game, x, y)}


def test_shot_explodes_on_a_shield_and_erodes_it():  # PlayerShotHit 14F5-1501 -> 1530; 03E3 EraseShifted; 0439
    game = cannon_ready()
    run(game, 14, ["right"], quiet=True)
    before = box_pixels(game, 64, 85, 48, 63)
    fire_once(game)
    run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) == 3, 20, quiet=True)
    assert (peek(game, OBJ1_COOR_XR), peek(game, OBJ1_COOR_YR)) == (70, 48)  # shield column 6 is lit from Yr 51
    assert peek(game, P1_SCOR_L) == 0
    fire_off(game)
    game.step()
    assert (peek(game, OBJ1_COOR_XR), peek(game, OBJ1_COOR_YR)) == (67, 46)
    blast = pattern_pixels(game, 0x1C91, 8, 67, 46)
    assert all(pixel(game, x, y) for x, y in blast)
    assert run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) == 0, 30, quiet=True) == 15
    shot = pattern_pixels(game, 0x1C90, 1, 70, 48)
    assert box_pixels(game, 64, 85, 48, 63) == before - shot - blast


def put_alien_shot(game, base, xr, yr, steps=1):
    """Poke an alien shot into flight at (xr, yr): its block from the RAM mirror, marked active."""
    mirror = base - 5 - RAM + 0x1B00
    for i in range(16):
        poke(game, base - 5 + i, game.rom[mirror + i])
    poke(game, base, 0x80)
    poke(game, base + SHOT_STEPS, steps)
    poke(game, base + SHOT_YR, yr)
    poke(game, base + SHOT_XR, xr)


def test_player_shot_and_alien_shot_destroy_each_other():  # DrawSprCollision 1491; 05FB-0617; PlayerShotHit 1530
    game = cannon_ready()
    run(game, 44, ["right"], quiet=True)
    assert peek(game, PLAYER_XR) == 92  # shot Xr 100, in the gap between shields 1 and 2
    fire_once(game)
    run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) == 2, 3, quiet=True)
    put_alien_shot(game, ROL_SHOT, 99, 104)  # its lit centre column is Xr 100
    run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) != 2, 20, quiet=True)
    assert peek(game, PLYR_SHOT_STATUS) == 3 and peek(game, OBJ1_COOR_YR) > 64
    assert peek(game, ROL_SHOT) == 0x80 and peek(game, ROL_SHOT + SHOT_YR) > 64
    run_until(game, lambda g: peek(g, ROL_SHOT) != 0x80, 3, quiet=True)
    assert peek(game, ROL_SHOT) == 0x81 and peek(game, ROL_SHOT + SHOT_YR) > 64  # blowing up in mid-air
    assert score(game) == 0


def test_shot_count_advances_when_each_shot_ends_and_resets_with_the_rack():  # EndOfBlowup 0456-045A; 09FD
    game = cannon_ready()
    for n in range(1, 4):
        fire_once(game)
        assert peek(game, SHOT_COUNT) == n - 1
        run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) == 0, 80, quiet=True)
        assert peek(game, SHOT_COUNT) == n
    clear_rack(game)
    assert peek(game, SHOT_COUNT) == 0


# ---- AH: hitting aliens, scoring ----


def aim(game, xr, quiet=True):
    """Move the cannon until a shot would leave at Xr xr (the cannon's Xr + 8)."""
    for _ in during(lambda: peek(game, PLAYER_XR) + 8 != xr, 200):
        if quiet:
            fire_off(game)
        game.step(["right"] if peek(game, PLAYER_XR) + 8 < xr else ["left"])


def shoot(game, i, limit=60):
    """Aim at the middle of alien i's cell, fire, and step until the shot is over or has hit something; the frame
    count from the press to then."""
    aim(game, alien_xy(game, i)[0] + 8)
    fire_once(game)
    return run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) in (0, 3, 4, 5), limit, quiet=True)


def test_a_hit_kills_the_alien_and_scores_in_the_same_frame():  # 041E-0429 -> 2002; PlayerShotHit 14EA-152F; 0988
    game = cannon_ready()
    aim(game, alien_xy(game, 2)[0] + 8)
    fire_once(game)
    for _ in during(lambda: peek(game, P1_ALIENS + 2), 40):
        assert peek(game, ALIEN_IS_EXPLODING) == 0 and score(game) == 0
        fire_off(game)
        game.step()
    assert peek(game, PLYR_SHOT_STATUS) == 5 and peek(game, ALIEN_IS_EXPLODING) == 1
    assert score(game) == 10 and text(game, 0x271C, 4) == "0010" and game.events.count("kill") == 1
    assert peek(game, EXP_ALIEN_TIMER) in (15, 16)
    x, y = alien_xy(game, 2)
    assert shows(game, 0x1CC0, x, y, range(16))  # the explosion picture over the cell
    assert sum(peek(game, P1_ALIENS + i) for i in range(55)) == 54


def test_alien_points_by_row_10_10_20_20_30():  # AlienScoreValue 097C, table 1DA0 = 10 20 30
    game = cannon_ready()
    total = 0
    for row, points in enumerate((10, 10, 20, 20, 30)):
        shoot(game, 11 * row + 2)  # column 2 is over the gap between shields 1 and 2
        assert peek(game, P1_ALIENS + 11 * row + 2) == 0
        total += points
        assert score(game) == total
        run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) == 0, 30, quiet=True)
    assert game.score == total


def test_alien_explosion_freezes_the_sweep_for_16_end_screen_halves():  # 152A expAlienTimer 10; AExplodeTime 1538
    """The explosion is timed by DrawAlien in the end-screen half: a kill there (Xr >= 128) is followed by 16 frames
    of explosion, a kill in the mid-screen half (Xr < 128) by 15, as that frame's end-screen half already counts.
    Meanwhile no alien is drawn (waitOnDraw stays set, so the cursor stops after at most one more step) and the rack
    does not move; the cannon does. The frame after it, the alien under the cursor is drawn; the next, the cursor
    moves on."""
    for target, frozen in ((2, 15), (8, 16)):
        game = cannon_ready()
        aim(game, alien_xy(game, target)[0] + 8)
        fire_once(game)
        run_until(game, lambda g: not peek(g, P1_ALIENS + target), 40, quiet=True)
        assert (peek(game, OBJ1_COOR_XR) >= 128) == (target == 8)
        ref, x, cell = peek16(game, REF_ALIEN_YR), peek(game, PLAYER_XR), alien_xy(game, target)
        cursors, frames = set(), 0
        for _ in during(lambda: peek(game, ALIEN_IS_EXPLODING), 30):
            assert peek16(game, REF_ALIEN_YR) == ref
            assert lit(game, cell[0], cell[0] + 15, cell[1], cell[1] + 15)
            fire_off(game)
            game.step(["right"])
            frames += 1
            cursors.add(peek(game, ALIEN_CUR_INDEX))
            assert peek(game, PLAYER_XR) == x + frames
        assert frames == frozen and len(cursors) == 1
        assert not lit(game, cell[0], cell[0] + 15, cell[1], cell[1] + 15)
        run(game, 1, quiet=True)
        assert {peek(game, ALIEN_CUR_INDEX)} == cursors and peek(game, WAIT_ON_DRAW) == 0
        run(game, 1, quiet=True)
        assert {peek(game, ALIEN_CUR_INDEX)} != cursors


def test_no_fire_until_the_alien_explosion_is_over():  # status 5 (1512) -> 4 (1545) -> EndOfBlowup 042D
    game = cannon_ready()
    shoot(game, 2)
    assert peek(game, PLYR_SHOT_STATUS) == 5
    frames = 0
    for _ in during(lambda: peek(game, PLYR_SHOT_STATUS), 20):
        fire_once(game)
        frames += 2
        assert peek(game, PLYR_SHOT_STATUS) in (0, 1, 4, 5)
        if peek(game, PLYR_SHOT_STATUS) == 1:
            break
    assert 15 <= frames <= 18 and peek(game, ALIEN_IS_EXPLODING) == 0


def test_a_hit_in_the_rack_on_a_dead_aliens_cell_scores_nothing():  # 1504-151C: alien dead -> 1530 (status 3)
    game = play_start(set(range(55)) - {2})
    run_until(game, lambda g: obj0_timer(g) == 0, 200, quiet=True)
    aim(game, alien_xy(game, 2)[0] + 8)
    xr, yr = peek(game, PLAYER_XR) + 8, peek(game, REF_ALIEN_YR) + 4
    poke(game, vaddr(xr, yr), peek(game, vaddr(xr, yr)) | 1 << yr % 8)  # debris in alien 2's cell
    fire_once(game)
    run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) != 2 and peek(g, PLYR_SHOT_STATUS) != 1, 40, quiet=True)
    assert peek(game, PLYR_SHOT_STATUS) == 3 and peek(game, OBJ1_COOR_YR) + 6 > peek(game, REF_ALIEN_YR)
    assert score(game) == 0 and sum(peek(game, P1_ALIENS + i) for i in range(55)) == 54


# ---- R: rack movement, bumps, invasion ----


def test_full_rack_bumps_the_right_edge_then_drops_and_steps_left():  # RackBump 159E-15B6; MoveRefAlien 01A5; 01D9
    game = play_start()
    refs = {0: (56, 120)}
    for f in range(1, 2400):
        fire_off(game)
        game.step()
        ref = (peek(game, REF_ALIEN_XR), peek(game, REF_ALIEN_YR))
        if ref not in refs.values():
            refs[f] = ref
        if f == 449:
            assert (peek(game, RACK_DIRECTION), peek(game, REF_ALIEN_D_XR)) == (0, 2)
        if f == 450:  # alien 10 is drawn with its last lit column at Xr 245: the bump latches
            deltas = peek(game, RACK_DIRECTION), peek(game, REF_ALIEN_D_XR), peek(game, REF_ALIEN_D_YR)
            assert deltas == (1, 0xFE, 0xF8) and ref == (72, 120)
    steps = sorted(refs.items())
    assert steps[:10] == [(55 * k, (56 + 2 * k, 120)) for k in range(9)] + [(495, (70, 112))]
    assert refs[1430] == (40, 104) and refs[1375] == (38, 112)  # left bump at 1375, drop step at 1430
    assert refs[2365] == (70, 96)


@pytest.mark.parametrize("yr", [31, 32, 100, 215, 216])
def test_bump_is_a_pixel_test_on_columns_245_and_41(yr):  # 15C5-15D1: 23 bytes from Yr 32; 159E 3EA4, 15B7 2524
    bump = 32 <= yr <= 215
    game = play_start()
    run(game, 10, quiet=True)
    poke(game, vaddr(245, yr), 1 << yr % 8)  # moving right: the right column
    fire_off(game)
    game.step()
    assert (peek(game, RACK_DIRECTION) == 1) == bump
    if bump:
        assert (peek(game, REF_ALIEN_D_XR), peek(game, REF_ALIEN_D_YR)) == (0xFE, 0xF8)
    game = play_start()
    run(game, 10, quiet=True)
    poke(game, vaddr(245, 100), 1 << 4)
    run(game, 2, quiet=True)
    assert peek(game, RACK_DIRECTION) == 1
    poke(game, vaddr(245, 100), 0)
    poke(game, vaddr(41, yr), 1 << yr % 8)  # moving left: the left column
    fire_off(game)
    game.step()
    assert (peek(game, RACK_DIRECTION) == 0) == bump
    if bump:
        assert (peek(game, REF_ALIEN_D_XR), peek(game, REF_ALIEN_D_YR)) == (2, 0xF8)


def test_lone_alien_steps_every_frame_and_moves_right_at_3_after_a_left_bump():  # 18F1-18F8 from 15BE only
    game = play_start({22})
    run(game, 1, quiet=True)  # the cursor starts on alien 0: the first step comes a frame late
    dxs, last = [], peek(game, REF_ALIEN_XR)
    for _ in range(400):
        fire_off(game)
        game.step()
        x = peek(game, REF_ALIEN_XR)
        assert x != last  # one step a frame
        dxs.append((x - last) & 0xFF)
        last = x
    runs = [dxs[0]] + [b for a, b in zip(dxs, dxs[1:]) if a != b]
    assert runs[:4] == [2, 0xFE, 3, 0xFE]


def test_a_stored_dx_of_3_starts_the_next_rack_at_2():  # InitRack 00C2-00C7
    game = play_start()
    poke(game, 0x21FB, 3)  # p1RefAlienDX
    clear_rack(game)
    assert (peek(game, REF_ALIEN_D_XR), peek(game, RACK_DIRECTION)) == (2, 0)


def test_invasion_ends_the_game_whatever_ships_are_left():  # CursorNextAlien 016A-016D -> 1971 -> 16E6
    game = new_game()
    game.step()
    poke(game, P1_REF_ALIEN_Y, 48)  # the rack starts at Yr 48, two drops above the cannon row
    to_play(game)
    run_until(game, lambda g: peek(g, 0x206D), 2000, quiet=True)  # invaded
    x, y = alien_xy(game, peek(game, ALIEN_CUR_INDEX))
    assert y < 40 and peek(game, REF_ALIEN_YR) == 32 and peek(game, PLAYER_ALIVE) != 0xFF
    assert peek(game, P1_SHIPS_REM) == 2
    assert run_until(game, lambda g: g.phase == "game_over", 400, quiet=True) == 259
    assert game.player.lives == 0 and not lit(game, 56, 167, 8, 15)
    assert text(game, 0x2D18, 9) == "GAME OVER"


def test_invasion_with_only_the_top_row_comes_at_ref_yr_minus_32():  # GetAlienCoords 8-bit wrap; 016B CP 28
    game = new_game()
    game.step()
    for i in range(55):
        poke(game, P1_ALIENS + i, int(i >= 44))
    poke(game, P1_REF_ALIEN_Y, 0xF0)  # row 4 at Yr 48
    to_play(game)
    run_until(game, lambda g: peek(g, 0x206D), 3000, quiet=True)
    assert peek(game, REF_ALIEN_YR) == 0xE0 and alien_xy(game, peek(game, ALIEN_CUR_INDEX))[1] == 32


def test_aliens_and_the_cannon_overwrite_their_whole_box():  # DrawSprite 15D3 (16 x 16 with the clear byte); 1439
    game = play_start()
    run(game, 60, quiet=True)
    x, y = alien_xy(game, 0)
    poke(game, vaddr(x + 8, y + 12), 1 << 4)  # in the clear half of alien 0's box
    run(game, 49, quiet=True)
    assert pixel(game, x + 8, y + 12) and lit(game, x + 2, x + 3, y, y + 7)  # the old picture's columns 2-3
    fire_off(game)
    game.step()  # the rack steps 2 to the right and alien 0 is redrawn: its blank columns 0-1 land there
    assert alien_xy(game, 0) == (x + 2, y)
    assert not pixel(game, x + 8, y + 12) and not lit(game, x + 2, x + 3, y, y + 7)
    game = cannon_ready()
    run(game, 2, quiet=True)
    poke(game, vaddr(48, 33), 1 << 1)  # in the cannon's blank first column
    fire_off(game)
    game.step()
    assert not pixel(game, 48, 33)


def test_rack_start_heights_by_round():  # 0A09-0A1E: rack count AND 7, + 1, table 1DA3 = 60 50 48 48 48 40 40 40
    game = play_start()
    heights = []
    for _ in range(11):
        clear_rack(game)
        heights.append((peek(game, REF_ALIEN_XR), peek(game, REF_ALIEN_YR)))
    assert heights == [(56, y) for y in (96, 80, 72, 72, 72, 64, 64, 64, 96, 80, 72)]
    assert peek(game, P1_RACK_CNT) == 3 and game.wave == 12  # the count runs 1-8 (AND 7, + 1)


def test_objects_run_on_through_the_48_frame_rack_wait():  # 0A3C-0A58: isrDelay 30h, tasks still on
    game = cannon_ready()
    put_alien_shot(game, SQU_SHOT, 100, 150)
    for i in range(55):
        poke(game, P1_ALIENS + i, 0)
    ys = []
    for _ in during(lambda: peek(game, SUSPEND_PLAY), 60):
        fire_off(game)
        game.step()
        ys.append(peek(game, SQU_SHOT + SHOT_YR))
    assert len(ys) == 48 and ys[-1] < 150 - 4 * 14  # it kept falling until the screen was cleared


def test_a_death_in_the_rack_wait_costs_a_ship_then_the_next_rack_starts():  # 0A52; 02D0 -> 0817 -> 082F -> 09EF
    game = cannon_ready()
    for i in range(55):
        poke(game, P1_ALIENS + i, 0)
    run(game, 10, quiet=True)
    poke(game, PLAYER_ALIVE, 0)
    run_until(game, lambda g: peek(g, P1_SHIPS_REM) == 1, 100, quiet=True)
    assert game.player.lives == 2
    run_until(game, lambda g: not peek(g, SUSPEND_PLAY), 100, quiet=True)
    to_play(game, 100)
    assert peek(game, P1_RACK_CNT) == 1 and peek(game, REF_ALIEN_YR) == 96 and peek(game, NUM_ALIENS) == 55


# ---- AS: alien shots ----


def immortal(game):
    """Undo a hit on the cannon at this frame boundary, so long runs keep their alien fire."""
    if peek(game, PLAYER_ALIVE) != 0xFF:
        poke(game, PLAYER_ALIVE, 0xFF)


def launches(game, frames, base, hold_score=None, actions=()):
    """Step `frames` frames; the (frame, Xr, Yr, ref Xr) of every launch of the alien shot whose block is at
    `base`."""
    out = []
    for f in range(frames):
        if hold_score is not None:
            set_score(game, hold_score)
        immortal(game)
        was = shot_active(game, base)
        game.step(actions)
        if shot_active(game, base) and not was:
            out.append((f, peek(game, base + SHOT_XR), peek(game, base + SHOT_YR), peek(game, REF_ALIEN_XR)))
    return out


def column_of(xr, ref_xr):
    """The 1-based rack column an alien shot launched at Xr came from."""
    return round(((xr - 7 - ref_xr) & 0xFF) / 16) + 1


def test_alien_shot_spawns_10_below_its_alien_and_shows_after_its_first_move():  # 05A9-05B4; 05B7; 05E5-05F0
    game = play_start()
    run_until(game, lambda g: shot_active(g, ROL_SHOT), 200)
    x, y = alien_xy(game, 0)  # the lowest alien of the column over the cannon
    assert (peek(game, ROL_SHOT + SHOT_XR), peek(game, ROL_SHOT + SHOT_YR)) == (x + 7, y - 10)
    assert not lit(game, x + 7, x + 9, y - 14, y - 3)  # nothing drawn yet
    run_until(game, lambda g: peek(g, ROL_SHOT + SHOT_YR) != y - 10, 4)
    assert peek(game, ROL_SHOT + SHOT_YR) == y - 14
    pic = peek16(game, ROL_SHOT + SHOT_IMAGE)
    assert pic == ROLLING_PICS + 3  # the move steps the picture before drawing it
    assert all(column(game, x + 7 + c, y - 14) == game.rom[pic + c] for c in range(3))


def test_alien_shot_falls_4_every_third_frame_and_ends_below_yr_21():  # 05E5-05ED (207E = FC); 05F3-05F8 (CP 15)
    game = cannon_ready()
    put_alien_shot(game, SQU_SHOT, 95, 100)  # in the gap between shields 1 and 2
    ys, frames = [100], []
    for f in range(100):
        fire_off(game)
        game.step()
        if peek(game, SQU_SHOT + SHOT_YR) != ys[-1]:
            ys.append(peek(game, SQU_SHOT + SHOT_YR))
            frames.append(f)
        if peek(game, SQU_SHOT) != 0x80:
            break
    assert ys == list(range(100, 19, -4))
    assert all(b - a == 3 for a, b in zip(frames, frames[1:]))
    assert peek(game, SQU_SHOT) == 0x81 and f == frames[-1]  # blowing up after the move to Yr 20


@pytest.mark.parametrize(("start", "last"), [(101, 16), (100, 20)])
def test_alien_shot_ends_after_the_move_below_yr_21_not_at_it(start, last):  # 05F3-05F8: CP 15; JP C
    """At 5 px a move (8 aliens left) a shot from Yr 101 passes through 21 and ends at 16."""
    game = cannon_ready(set(range(8)))
    run(game, 2, quiet=True)
    assert peek(game, ALIEN_SHOT_DELTA) == 0xFB
    put_alien_shot(game, SQU_SHOT, 236, start)
    ys = [start]
    for _ in during(lambda: peek(game, SQU_SHOT) == 0x80, 80):
        fire_off(game)
        game.step()
        if peek(game, SQU_SHOT + SHOT_YR) != ys[-1]:
            ys.append(peek(game, SQU_SHOT + SHOT_YR))
    assert ys == list(range(start, last - 1, -5)) and peek(game, SQU_SHOT) == 0x81


@pytest.mark.parametrize(
    ("base", "pics"),
    [(ROL_SHOT, ROLLING_PICS), (PLU_SHOT, PLUNGER_PICS), (SQU_SHOT, SQUIGGLY_PICS)],
    ids=["rolling", "plunger", "squiggly"],
)
def test_alien_shot_pictures_cycle_through_four(base, pics):  # 05D4-05E2: +3 a move, back 0Ch at the set's end (207F)
    game = cannon_ready()
    put_alien_shot(game, base, 236, 150)  # the mirror's block: the set's first picture
    assert peek16(game, base + SHOT_IMAGE) == pics
    seen = []
    for _ in during(lambda: peek(game, base) == 0x80, 120):
        yr = peek(game, base + SHOT_YR)
        calm(game)  # the rack held clear of Xr 236
        if peek(game, base) == 0x80 and peek(game, base + SHOT_YR) != yr:
            pic, yr = peek16(game, base + SHOT_IMAGE), peek(game, base + SHOT_YR)
            seen.append(pic)
            assert all(column(game, 236 + c, yr) == game.rom[pic + c] for c in range(3))
    assert len(seen) >= 10 and seen == [pics + 3 * ((k + 1) % 4) for k in range(len(seen))]


def test_a_column_with_only_its_top_alien_alive_still_fires():  # FindInColumn 062F-0643: 5 rows from the bottom
    game = play_start(set(range(44, 55)))  # only row 4
    shots = []
    for _ in range(600):
        immortal(game)
        was = [shot_active(game, b) for b in (ROL_SHOT, PLU_SHOT, SQU_SHOT)]
        game.step()
        for b, active in zip((ROL_SHOT, PLU_SHOT, SQU_SHOT), was):
            if shot_active(game, b) and not active:
                shots.append((peek(game, b + SHOT_XR) - peek(game, REF_ALIEN_XR), peek(game, b + SHOT_YR)))
                assert shots[-1][1] == peek(game, REF_ALIEN_YR) + 64 - 10  # 10 below its row-4 alien
    assert len(shots) == 5 and all((dx - 7) % 16 == 0 for dx, _ in shots)


def test_alien_shots_speed_up_to_5_with_8_aliens_left_for_the_rest_of_the_rack():  # SpeedShots 08D8-08E3; 1B7E
    game = play_start(set(range(9)))
    run(game, 3, quiet=True)
    assert peek(game, ALIEN_SHOT_DELTA) == 0xFC
    poke(game, P1_ALIENS + 8, 0)
    run(game, 1, quiet=True)
    assert peek(game, ALIEN_SHOT_DELTA) == 0xFB
    poke(game, P1_ALIENS + 8, 1)  # back to 9: still 5
    run(game, 3, quiet=True)
    assert peek(game, NUM_ALIENS) == 9 and peek(game, ALIEN_SHOT_DELTA) == 0xFB
    clear_rack(game)
    assert peek(game, ALIEN_SHOT_DELTA) == 0xFC  # CopyRAMMirror at the next rack
    poke(game, P1_ALIENS + 54, 0)
    poke(game, P1_ALIENS + 53, 0)
    put_alien_shot(game, SQU_SHOT, 240, 100)
    ys = [peek(game, SQU_SHOT + SHOT_YR)]
    for _ in range(10):
        fire_off(game)
        game.step()
        ys.append(peek(game, SQU_SHOT + SHOT_YR))
    assert set(a - b for a, b in zip(ys, ys[1:])) == {0, 4}


def drop_onto(game, base, xr, debris_yr, start_yr=100):
    """Drop an alien shot from (xr, start_yr) onto a 3-pixel bar of debris at debris_yr (far from the cannon);
    the frame count until it collides (its status gets the blow-up bit) and the shot's Yr then."""
    for c in range(3):
        a = vaddr(xr + c, debris_yr)
        poke(game, a, peek(game, a) | 1 << debris_yr % 8)
    put_alien_shot(game, base, xr, start_yr)
    n = run_until(game, lambda g: peek(g, base) != 0x80, 100, quiet=True)
    return n, peek(game, base + SHOT_YR)


@pytest.mark.parametrize("yr", [26, 29, 30, 34, 38, 39, 42, 49])
def test_a_collision_with_yr_30_to_38_kills_the_player_wherever_it_is(yr):  # 0600-060F (CP 1E, CP 27)
    game = cannon_ready()
    _, hit = drop_onto(game, PLU_SHOT, 236, yr + 3, start_yr=yr + 64)  # the cannon is at Xr 48
    assert hit == yr and peek(game, PLU_SHOT) == 0x81
    assert (peek(game, PLAYER_ALIVE) != 0xFF) == (30 <= yr <= 38)


def test_alien_shot_blow_up_shows_at_the_next_move_slot_for_3_slots():  # ShotBlowingUp 0644-0669 (blow count 4)
    game = cannon_ready()
    _, yr = drop_onto(game, PLU_SHOT, 236, 50)
    assert peek(game, PLAYER_ALIVE) == 0xFF
    blast = pattern_pixels(game, 0x1CDC, 6, 234, yr - 2)
    run(game, 2, quiet=True)
    assert not all(pixel(game, x, y) for x, y in blast)  # frozen as it was
    run(game, 1, quiet=True)
    assert all(pixel(game, x, y) for x, y in blast)  # the 6 x 8 explosion at (Xr - 2, Yr - 2)
    run(game, 8, quiet=True)
    assert all(pixel(game, x, y) for x, y in blast) and peek(game, PLU_SHOT) == 0x81
    run(game, 1, quiet=True)
    assert not any(pixel(game, x, y) for x, y in blast) and peek(game, PLU_SHOT) == 0


@pytest.mark.parametrize(
    ("score_value", "rate"),
    [(0, 48), (299, 48), (300, 16), (1099, 16), (1100, 11), (2099, 11), (2100, 8), (3099, 8), (3100, 7), (9990, 7)],
)
def test_alien_reload_rate_by_score(score_value, rate):  # AShotReloadRate 170E; 1CB8 = 02 10 20 30, 1AA1
    game = play_start()
    set_score(game, score_value)
    run(game, 1, quiet=True)
    assert peek(game, A_SHOT_RELOAD_RATE) == rate


def test_no_alien_shot_launches_too_soon_after_another():  # 057C-0595: another shot's steps must be 0 or > rate
    game = play_start()
    checked = 0
    blocks = (ROL_SHOT, PLU_SHOT, SQU_SHOT)
    for _ in range(1500):
        set_score(game, 1000)  # rate 16
        immortal(game)
        before = {b: shot_active(game, b) for b in blocks}
        game.step()
        for b in blocks:
            if shot_active(game, b) and not before[b]:
                others = [peek(game, o + SHOT_STEPS) for o in blocks if o != b]
                assert all(steps == 0 or steps > 16 for steps in others), (hex(b), others)
                checked += any(others)
    assert checked > 5


def test_one_alien_shot_at_a_time_at_score_0():  # a fall is shorter than the 48-step reload
    game = play_start()
    for _ in during(lambda: game.phase == "playing", 6000):
        game.step()
        assert sum(shot_active(game, b) for b in (ROL_SHOT, PLU_SHOT, SQU_SHOT)) <= 1


@pytest.mark.parametrize("cannon", [48, 70, 100, 150, 217])
def test_rolling_shot_targets_the_column_over_the_cannon(cannon):  # 061B-062C: FindColumn(playerXr + 8), C <= 11
    game = cannon_ready()
    poke(game, PLAYER_XR, cannon)
    run_until(game, lambda g: shot_active(g, ROL_SHOT), 60, quiet=False)
    ref = peek(game, REF_ALIEN_XR)
    col = min(11, max(1, -(-(cannon + 8 - ref) // 16)))
    assert peek(game, ROL_SHOT + SHOT_XR) == ref + 16 * (col - 1) + 7


def rolling_launch(alive, cannon, ref_xr):
    """The rack held at (ref_xr, 120) and the cannon at `cannon`: the Xr of the first rolling shot, or None."""
    game = play_start(alive)
    for _ in range(400):
        poke(game, PLAYER_XR, cannon)
        hold_rack(game, 120, ref_xr)
        poke(game, ENABLE_ALIEN_FIRE, 1)
        immortal(game)
        was = shot_active(game, ROL_SHOT)
        game.step()
        if shot_active(game, ROL_SHOT) and not was:
            return peek(game, ROL_SHOT + SHOT_XR)
    return None


@pytest.mark.parametrize(
    ("cannon", "ref_xr", "launch_xr"),
    [
        (48, 56, 63),  # target 56 = ref: Cnt16s counts equal as past (CALL NC), column 1
        (96, 56, 95),  # target 104: column 3
        (97, 56, 111),  # target 105, one past column 3's 16: column 4
        (112, 56, 111),  # target 120: column 4
        (113, 56, 127),  # column 5
        (217, 56, 223),  # target 225: column 11
        (200, 38, 205),  # column 11 (target 208)
        (217, 38, 205),  # target 225: "column 12", capped at 11 (0625-062A); the rack at its left turn
    ],
)
def test_rolling_shot_column_edges_and_the_cap_at_11(cannon, ref_xr, launch_xr):  # 061B-062C; FindColumn 156F
    assert rolling_launch(set(range(11)), cannon, ref_xr) == launch_xr


def test_rolling_shot_skips_one_try_after_each_reset():  # 047D-0489: fire word 0000 -> FFFF, no shot
    game = play_start()
    run_until(game, lambda g: shot_active(g, ROL_SHOT), 300)
    run_until(game, lambda g: not shot_active(g, ROL_SHOT), 300)
    assert peek16(game, ROL_SHOT + SHOT_CFIR) == 0
    run_until(game, lambda g: peek16(g, ROL_SHOT + SHOT_CFIR) != 0, 3)
    assert peek16(game, ROL_SHOT + SHOT_CFIR) == 0xFFFF and not shot_active(game, ROL_SHOT)


PLUNGER_COLUMNS = [1, 7, 1, 1, 1, 4, 11, 1, 6, 3, 1, 1, 11, 9, 2, 8]  # table 1D00
SQUIGGLY_COLUMNS = [11, 1, 6, 3, 1, 1, 11, 9, 2, 8, 2, 11, 4, 7, 10]  # table 1D06


def test_plunger_and_squiggly_fire_by_their_column_tables():  # 059C-05A2, 04D9-04E4 (CP 10); 0526-0531 (CP 15)
    game = play_start()
    plunger, squiggly = [], []
    for f in range(2400):
        set_score(game, 3100)
        immortal(game)
        was = shot_active(game, PLU_SHOT), shot_active(game, SQU_SHOT)
        game.step()
        for base, log, active in ((PLU_SHOT, plunger, was[0]), (SQU_SHOT, squiggly, was[1])):
            if shot_active(game, base) and not active:
                log.append(column_of(peek(game, base + SHOT_XR), peek(game, REF_ALIEN_XR)))
    assert len(plunger) >= 20 and len(squiggly) >= 18
    assert plunger == (PLUNGER_COLUMNS * 2)[: len(plunger)]
    assert squiggly == (SQUIGGLY_COLUMNS * 2)[: len(squiggly)]


def test_plunger_table_moves_on_past_an_empty_column():  # 05A0-05A8: the pointer is stored before FindInColumn
    game = play_start(set(range(55)) - {0, 11, 22, 33, 44})  # column 1 gone
    columns = []
    for f in range(1500):
        set_score(game, 3100)
        immortal(game)
        was = shot_active(game, PLU_SHOT)
        game.step()
        if shot_active(game, PLU_SHOT) and not was:
            columns.append(column_of(peek(game, PLU_SHOT + SHOT_XR), peek(game, REF_ALIEN_XR)))
    without = [c for c in PLUNGER_COLUMNS * 3 if c != 1]
    assert len(columns) >= 8 and columns == without[: len(columns)]


def test_plunger_stops_after_its_shot_ends_with_one_alien_left():  # 04FC-0505 skipPlunger; 04B7-04BB
    game = play_start()
    run_until(game, lambda g: shot_active(g, PLU_SHOT), 600, quiet=False)
    for i in range(1, 55):
        poke(game, P1_ALIENS + i, 0)
    flight = run_until(game, lambda g: not shot_active(g, PLU_SHOT), 200)
    assert flight > 3 and peek(game, SKIP_PLUNGER) == 1
    assert not launches(game, 800, PLU_SHOT, hold_score=3100)


def test_plunger_fires_once_more_when_one_alien_is_left():  # 04FC runs only when a plunger shot has ended
    game = play_start({28})  # a lone alien in column 7: the plunger table's second entry
    shots = launches(game, 700, PLU_SHOT, hold_score=3100)
    assert len(shots) == 1 and peek(game, SKIP_PLUNGER) == 1


def test_alien_shots_erode_shields_under_the_shot_and_its_explosion():  # EraseShifted 1452 at 05D1, 064E, 0669
    game = cannon_ready()
    before = box_pixels(game, 109, 130, 48, 63)
    put_alien_shot(game, SQU_SHOT, 118, 100)
    run_until(game, lambda g: peek(g, SQU_SHOT) != 0x80, 100, quiet=True)
    yr, pic = peek(game, SQU_SHOT + SHOT_YR), peek16(game, SQU_SHOT + SHOT_IMAGE)
    assert 48 <= yr <= 63 and pic == SQUIGGLY_PICS + 3 * ((100 - yr) // 4 % 4)  # the picture of its last move
    shot = pattern_pixels(game, pic, 3, 118, yr)
    run_until(game, lambda g: peek(g, SQU_SHOT) == 0, 20, quiet=True)
    blast = pattern_pixels(game, 0x1CDC, 6, 116, yr - 2)
    assert box_pixels(game, 109, 130, 48, 63) == before - shot - blast


# ---- SC: the saucer ----

LEFT_COLUMNS = {r * 11 + c for r in range(5) for c in (0, 1)}  # 10 aliens, clear of the gaps right of Xr 131


def calm(game, actions=()):
    """One frame with the aliens' fire off and the rack held still (waitOnDraw set, so CursorNextAlien 0146 waits)."""
    fire_off(game)
    poke(game, WAIT_ON_DRAW, 1)
    game.step(actions)


def calm_until(game, cond, limit):
    """calm() until cond(game) holds; the frames that took (fails after `limit`)."""
    for n in range(limit + 1):
        if cond(game):
            return n
        calm(game)
    raise AssertionError(f"condition not reached in {limit} frames")


def die_and_respawn(game):
    """Hit the cannon at this frame boundary and step to the frame its successor appears (189 frames)."""
    poke(game, PLAYER_ALIVE, 0)
    hit = game.tick
    run_until(game, lambda g: peek(g, PLAYER_OK) == 0, 10, quiet=True)
    run_until(game, lambda g: peek(g, PLAYER_OK) == 1, 300, quiet=True)
    assert game.tick - hit == 189


def saucer_game(alive=LEFT_COLUMNS):
    game = play_start(alive)
    for _ in during(lambda: obj0_timer(game), 200):
        calm(game)
    return game


def start_saucer(game, from_right=False):
    """Flag the saucer (saucerStart, as TimeToSaucer 0926 does), from the right if asked (as EndOfBlowup 0462 sets
    it after an odd table bit)."""
    if from_right:
        poke(game, SAUCER_XR, 0xE0)
        poke(game, SAUCER_DELTA, 0xFE)
    poke(game, SAUCER_START, 1)


def saucer_path(game, limit=400):
    """Step until the saucer is gone; [(frame, Xr)] of every frame its Xr changed, the start included."""
    path, f = [], 0
    while not peek(game, SAUCER_ACTIVE):
        calm(game)
        f += 1
        assert f < 10
    path.append((f, peek(game, SAUCER_XR)))
    while peek(game, SAUCER_ACTIVE) and f < limit:
        calm(game)
        f += 1
        if peek(game, SAUCER_XR) != path[-1][1] and peek(game, SAUCER_ACTIVE):
            path.append((f, peek(game, SAUCER_XR)))
    return path


def test_saucer_timer_runs_only_below_the_first_rack_height():  # TimeToSaucer 0913-0918 (CP 78, unsigned)
    game = play_start()
    for f in range(1, 495):
        fire_off(game)
        game.step()
        assert peek16(game, TILL_SAUCER) == 0x0600, f
    fire_off(game)
    game.step()
    assert peek(game, REF_ALIEN_YR) == 112 and peek16(game, TILL_SAUCER) == 0x05FF
    for n in range(2, 50):
        fire_off(game)
        game.step()
        assert peek16(game, TILL_SAUCER) == 0x0600 - n


def test_saucer_flag_every_1536_counting_frames():  # TimeToSaucer 0919-092A: 0600h, saucerStart = 1
    game = play_start()
    run_until(game, lambda g: peek16(g, TILL_SAUCER) != 0x0600, 600, quiet=True)
    assert run_until(game, lambda g: peek(g, SAUCER_START), 2000, quiet=True) == 1536  # counting frame 1537
    first = game.tick
    run_until(game, lambda g: not peek(g, SAUCER_START), 400, quiet=True)  # the flight ends (075F resets it)
    run_until(game, lambda g: peek(g, SAUCER_START), 2000, quiet=True)
    assert game.tick - first == 1536  # the timer ran on through the flight


def test_saucer_needs_8_aliens_and_an_idle_squiggly_shot():  # 0689-06A8 (CP 08); squiggly steps must be 0
    game = saucer_game({r * 11 + c for r in range(4) for c in (0, 1)})
    assert peek(game, NUM_ALIENS) == 8
    start_saucer(game)
    assert run_until(game, lambda g: peek(g, SAUCER_ACTIVE), 3, quiet=True) <= 3
    game = saucer_game(set(range(7)))
    start_saucer(game)
    for _ in range(300):
        calm(game)
        assert not peek(game, SAUCER_ACTIVE) and peek(game, SAUCER_START) == 1
    game = saucer_game()
    put_alien_shot(game, SQU_SHOT, 95, 150)
    start_saucer(game)
    for _ in during(lambda: shot_active(game, SQU_SHOT) or peek(game, SQU_SHOT + SHOT_STEPS), 200):
        assert not peek(game, SAUCER_ACTIVE)
        calm(game)
    assert run_until(game, lambda g: peek(g, SAUCER_ACTIVE), 6, quiet=True) <= 6


def test_no_squiggly_shot_while_the_saucer_flies():  # 0682-0689: the saucer takes object 4's turn
    game = saucer_game(set(range(55)))
    start_saucer(game)
    run_until(game, lambda g: peek(g, SAUCER_ACTIVE), 3, quiet=True)
    for _ in during(lambda: peek(game, SAUCER_ACTIVE), 400):
        set_score(game, 3100)
        immortal(game)
        game.step()  # alien fire on
        assert not shot_active(game, SQU_SHOT)


def test_saucer_from_the_left_moves_2_every_3_frames_and_leaves_at_225():  # 06BA-06C4; 06CB-06D2 (CP E1)
    """It starts at Xr 41 in an end-screen half and moves in mid-screen halves (Xr < 128) until it crosses 128,
    then in end-screen halves: one frame early once, never twice in a frame."""
    game = saucer_game()
    start_saucer(game)
    path = saucer_path(game)
    assert [x for _, x in path] == list(range(41, 224, 2))
    gaps = {x: f1 - f0 for (f0, _), (f1, x) in zip(path, path[1:])}
    assert gaps[43] == 1 and gaps[131] == 2 and all(g == 3 for x, g in gaps.items() if x not in (43, 131))
    assert not peek(game, SAUCER_ACTIVE) and not lit(game, 32, 255, 208, 215)


def test_saucer_from_the_right_double_steps_at_128_and_leaves_below_40():  # CompYToBeam 1A06; 06CB (CP 28)
    """Drawn at Xr 224 and moved to 222 in the end-screen half that starts it. At Xr 128 it moves in the end-screen
    half (Xr >= 128) and again in the next mid-screen half (Xr < 128): two steps a frame apart."""
    game = saucer_game()
    start_saucer(game, from_right=True)
    path = saucer_path(game)
    assert [x for _, x in path] == list(range(222, 39, -2))
    gaps = {x: f1 - f0 for (f0, _), (f1, x) in zip(path, path[1:])}
    assert gaps[124] == 1 and all(g == 3 for x, g in gaps.items() if x != 124)
    assert not peek(game, SAUCER_ACTIVE) and not lit(game, 32, 255, 208, 215)


SAUCER_SIDES = (  # bit 0 of ROM 0800 + shot count: L = from the left (checklist Q5)
    "LRLRLLLRLRLRRRLLRLLLLLRLLLRRLRRLRRLRLLLLLRLRRRLRLLLRLLLLLRRLRLLL"
    "RRLRRRLRRLLLLRLRRLLRRRRLRRLLRRRLLLLRLRLRRLLLLRLLLLLRRLRRRRRLRLRL"
    "LRRRRRRLRLRRLLLLLRLRRLLRRLRLRRLLLRLRRRRRRRRRLRRRRRRLRLLLLLLRRRLR"
    "LRLRLRLRLRLLLRLLRLRRLRLLRRRRLRRLRRRLRRRLRLRLRRLLRRLRLLLRLLLRLRLL"
)


def end_shot(game):
    """End the player's shot as a hit on the saucer does (status 4: EndOfBlowup on its next run)."""
    poke(game, PLYR_SHOT_STATUS, 4)
    calm_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) == 0, 3)


def test_saucer_side_comes_from_a_rom_table_by_shot_count():  # EndOfBlowup 0456-0474: ROM[0800 + shotCount] bit 0
    game = saucer_game()
    assert (peek(game, SAUCER_XR), peek(game, SAUCER_DELTA)) == (41, 2)  # no shot yet: from the left
    sides = ""
    for n in range(1, 257):
        end_shot(game)
        assert peek(game, SHOT_COUNT) == n & 0xFF
        sides += {(41, 2): "L", (224, 0xFE): "R"}[peek(game, SAUCER_XR), peek(game, SAUCER_DELTA)]
    assert sides == SAUCER_SIDES[1:] + SAUCER_SIDES[0]


def test_one_shot_brings_the_saucer_from_the_right_and_a_flight_resets_it():  # 0462; 075F resets 2083-208C
    game = saucer_game()
    fire_once(game)
    run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) == 0, 80, quiet=True)
    start_saucer(game)
    path = saucer_path(game)
    assert path[0][1] == 222 and path[-1][1] == 40
    start_saucer(game)
    path = saucer_path(game)  # no shot ended since: the mirror's side and step, from the left
    assert [x for _, x in path] == list(range(41, 224, 2))


def test_a_shot_ending_while_the_saucer_flies_keeps_its_course():  # 045B-0474 skip with saucerActive
    game = saucer_game()
    start_saucer(game)
    run(game, 30, quiet=True)
    x = peek(game, SAUCER_XR)
    end_shot(game)
    assert peek(game, SAUCER_DELTA) == 2 and abs(peek(game, SAUCER_XR) - x) <= 2


def test_a_death_keeps_the_shot_count_and_the_saucer_score_index():  # 02B6 restores only 2010-201F
    game = saucer_game()
    for _ in range(5):
        end_shot(game)
    die_and_respawn(game)
    assert peek(game, SHOT_COUNT) == 5 and peek(game, SAU_SCORE_LSB) == 0x54 + 5


AIM_XR = 140  # the column the saucer tests fire up: in the gap between shields 2 and 3, clear of LEFT_COLUMNS


def hit_saucer(game):
    """Start the saucer from the left and fire through its path when it is 28 Xr short of the shot's column; the
    frame count from the press to the hit."""
    poke(game, PLAYER_XR, AIM_XR - 8)
    poke(game, SAUCER_XR, 41)
    poke(game, SAUCER_DELTA, 2)
    start_saucer(game)
    calm_until(game, lambda g: peek(g, SAUCER_ACTIVE) and peek(g, SAUCER_XR) >= AIM_XR - 12 - 28, 300)
    calm(game)
    calm(game, ["fire"])
    return calm_until(game, lambda g: peek(g, SAUCER_HIT), 60)


def test_a_shot_reaching_yr_206_hits_the_saucer_without_an_explosion():  # 14EF-14F2 (CP CE) -> 1579; status 4
    game = saucer_game()
    hit_saucer(game)
    assert peek(game, OBJ1_COOR_YR) >= 206 and peek(game, PLYR_SHOT_STATUS) == 4
    x, statuses = peek(game, SAUCER_XR), set()
    for _ in range(20):
        calm(game)
        statuses.add(peek(game, PLYR_SHOT_STATUS))
        assert peek(game, SAUCER_XR) == x
    assert statuses == {0} and peek(game, BLOW_UP_TIMER) == 0x10  # status 4 -> 0: never 3, no shot blast


def test_saucer_hit_sequence_explosion_score_at_24_gone_at_0():  # 06D6-0739, 074B-075C; hit timer 20h
    game = saucer_game()
    hit_saucer(game)
    x = peek(game, SAUCER_XR)
    frames, seen = 0, {}
    for _ in during(lambda: peek(game, SAUCER_ACTIVE), 200):
        calm(game)
        frames += 1
        t = peek(game, SAUCER_HIT_TIME)
        if t not in seen:
            pic = [column(game, x + c, 208) for c in range(24)]
            seen[t] = (frames, score(game), game.sound_ports[1] & 0x10, "saucer_hit" in game.events, pic)
            seen[t] += (text(game, vaddr(x, 208), 3),)
    assert seen[31][2] and seen[31][3]  # the saucer-hit sound from the first slot
    assert seen[31][4] == list(game.rom[0x1D7C : 0x1D7C + 24])  # SpriteSaucerExp over the saucer's box
    assert seen[25][1] == 0 and seen[24][1] == 50 and seen[24][5] == " 50"  # added and shown at timer 24
    assert seen[24][0] - seen[31][0] == 7 * 3
    assert seen[1][5] == " 50" and frames == 96 and not lit(game, x, x + 23, 208, 215)
    assert game.sound_ports[1] & 0x10 == 0


SAUCER_POINTS = [100, 50, 50, 100, 150, 100, 100, 50, 300, 100, 100, 100, 50, 150, 100]  # table 1D54, x 10


def test_saucer_score_follows_the_shot_count_23rd_scores_300():  # 0447-0453 (CP 63: 15 entries); 0711-0733
    """The points and the text printed where the saucer was (its message pointer from 1D50, beside the value)."""
    game = saucer_game()
    shots, got = list(range(1, 17)) + [23, 38], []
    for k in shots:
        for _ in during(lambda: peek(game, SHOT_COUNT) < k - 1, 40):
            end_shot(game)
        before = score(game)
        hit_saucer(game)
        x = peek(game, SAUCER_XR)
        calm_until(game, lambda g: peek(g, SAUCER_HIT_TIME) <= 24, 60)  # added and printed at timer 24
        shown = screen_text(game, vaddr(x, 208), 3)
        calm_until(game, lambda g: not peek(g, SAUCER_ACTIVE), 120)
        got.append((k, score(game) - before, shown))
        set_score(game, 0)
    assert got == [(k, SAUCER_POINTS[k % 15], f"{SAUCER_POINTS[k % 15]:3d}") for k in shots]


# ---- D: death, ships, game over, score ----

BLOW_UP_PICS = (0x1C70, 0x1C80)  # the cannon's two blow-up pictures


def hit_cannon(alive=None, fire=True):
    """A game whose cannon (moved to Xr 68, no alien shot in flight) is hit at this frame boundary, as an alien
    shot's 060F does; with alien fire enabled and due if `fire`."""
    game = cannon_ready(alive)
    run(game, 20, ["right"], quiet=True)
    poke(game, PLAYER_ALIVE, 0)
    if fire:
        poke(game, ENABLE_ALIEN_FIRE, 1)
        poke(game, ALIEN_FIRE_DELAY, 0)
    return game


def test_death_freezes_the_rack_after_5_frames_and_blows_up_for_60():  # 0290-02AB, 039B-03AD, 02AE-02C3
    game = hit_cannon()
    cursors, pics = [peek(game, ALIEN_CUR_INDEX)], []
    for f in range(1, 61):
        game.step()
        cursors.append(peek(game, ALIEN_CUR_INDEX))
        pics.append([p for p in (PLAYER_PIC, *BLOW_UP_PICS) if shows(game, p, 68, 32, range(16))])
        assert ("blast" in game.events) == (f == 1)
        if f < 60:
            assert game.sound_ports[0] & 0x04 and peek(game, P1_SHIPS_REM) == 2 and game.player.lives == 3
        assert (peek(game, PLAYER_OK) == 1) == (f < 5)
        if f >= 5:
            assert (peek(game, ENABLE_ALIEN_FIRE), peek(game, ALIEN_FIRE_DELAY)) == (0, 48)
    assert cursors[:6] == list(range(cursors[0], cursors[0] + 6)) and set(cursors[5:]) == {cursors[5]}
    assert pics[:4] == [[PLAYER_PIC]] * 4
    assert pics[4:59] == [[BLOW_UP_PICS[1 - k // 5 % 2]] for k in range(55)]  # 11 pictures, 5 frames each
    assert pics[59] == [] and not lit(game, 68, 83, 32, 47)  # the 16 x 16 box erased
    assert game.sound_ports[0] & 0x1F == 0 and peek(game, P1_SHIPS_REM) == 1 and game.player.lives == 2
    assert text(game, 0x2501, 1) == "2" and not lit(game, 72, 87, 8, 15)  # RemoveShip: the second icon goes


def test_respawn_at_48_comes_129_frames_after_the_blow_up_and_fire_47_after_that():  # 02B6-02BE; 0817; 033B
    game = hit_cannon(fire=False)
    run(game, 60)
    assert obj0_timer(game) == 0x80
    for f in range(61, 189):
        if f == 100:
            put_alien_shot(game, PLU_SHOT, 59, 60)  # falls through the empty cannon spot
        game.step(["right", "fire"] if f % 2 else ["left"])
        assert not lit(game, 32, 255, 32, 39) or shot_active(game, PLU_SHOT)
        assert peek(game, PLAYER_ALIVE) == 0xFF and peek(game, PLAYER_OK) == 0 and peek(game, PLYR_SHOT_STATUS) == 0
    assert peek(game, PLU_SHOT) == 0 and peek(game, P1_SHIPS_REM) == 1  # it ended at the bottom
    cursor = peek(game, ALIEN_CUR_INDEX)
    game.step()
    assert shows(game, PLAYER_PIC, 48, 32, range(16)) and peek(game, PLAYER_OK) == 1
    assert peek(game, ALIEN_CUR_INDEX) == cursor
    game.step()
    assert peek(game, ALIEN_CUR_INDEX) == cursor + 1
    run(game, 45)
    assert peek(game, ENABLE_ALIEN_FIRE) == 0
    game.step()
    assert peek(game, ENABLE_ALIEN_FIRE) == 1


def test_the_world_goes_on_while_the_cannon_blows_up():  # tasks off only at 02D4; 0563-0569 before 0577
    game = cannon_ready(LEFT_COLUMNS | {14})
    for _ in during(lambda: peek(game, PLAYER_XR) != alien_xy(game, 14)[0], 200):  # under alien 14's middle, a gap
        calm(game, ["right"])
    fire_once(game)
    poke(game, PLAYER_ALIVE, 0)
    put_alien_shot(game, SQU_SHOT, 118, 90)  # over shield 2
    shield = box_pixels(game, 109, 130, 48, 63)
    run_until(game, lambda g: not peek(g, P1_ALIENS + 14), 30)  # the shot in flight still scores
    assert score(game) == 10 and peek(game, PLAYER_ALIVE) != 0xFF
    run_until(game, lambda g: peek(g, SQU_SHOT) == 0, 60)
    damaged = box_pixels(game, 109, 130, 48, 63)
    assert damaged < shield
    start_saucer(game)
    run(game, 4)
    xs = set()
    for _ in during(lambda: peek(game, PLAYER_ALIVE) != 0xFF or peek(game, PLAYER_OK) == 0, 300):
        xs.add(peek(game, SAUCER_XR))
        game.step()
    assert len(xs) > 30  # the saucer flew on
    assert box_pixels(game, 109, 130, 48, 63) == damaged and not peek(game, P1_ALIENS + 14)
    assert sum(peek(game, P1_ALIENS + i) for i in range(55)) == peek(game, NUM_ALIENS) == 10


def test_a_death_with_no_ships_in_reserve_ends_the_game():  # 02D7-02DB -> 166D-16E3
    game = hit_cannon()
    poke(game, P1_SHIPS_REM, 0)
    set_score(game, 1240)
    run(game, 59)
    assert game.player.lives == 3 and peek(game, PLAYER1_ALIVE) == 1 and peek16(game, HI_SCOR_L) == 0
    game.step()
    assert game.player.lives == 0 and text(game, 0x2501, 1) == "0" and peek(game, PLAYER1_ALIVE) == 0
    assert peek16(game, HI_SCOR_L) == 0x1240
    frames = run_until(game, lambda g: g.phase == "game_over", 400)
    assert text(game, 0x2D18, 9) == "GAME OVER"
    assert frames == 200 and game.score == 1240  # GAME OVER at 6 frames a letter, then 128 frames


def test_the_game_ends_when_the_third_ship_is_lost():  # 02D0-02DB: 21FF 0 at the blow-up's end -> 166D
    """Three alien-shot hits through real play: the first two cost a reserve ship each and the cannon comes back;
    the third, with none in reserve, ends the game."""
    game = cannon_ready()

    def shot_down():
        put_alien_shot(game, PLU_SHOT, peek(game, PLAYER_XR) + 6, 50)  # onto the turret, in the Yr 30-38 band
        run_until(game, lambda g: peek(g, PLAYER_ALIVE) != 0xFF, 20, quiet=True)

    for reserve in (1, 0):
        shot_down()
        assert run_until(game, lambda g: peek(g, P1_SHIPS_REM) == reserve, 70, quiet=True) == 59  # the blow-up's end
        assert game.player.lives == reserve + 1
        assert text(game, 0x2501, 1) == str(reserve + 1) and peek(game, PLAYER1_ALIVE) == 1
        run_until(game, lambda g: peek(g, PLAYER_OK) == 1, 200, quiet=True)
        assert game.phase == "playing" and shows(game, PLAYER_PIC, 48, 32, range(16))
    shot_down()
    assert run_until(game, lambda g: peek(g, PLAYER1_ALIVE) == 0, 70, quiet=True) == 59  # no ship to take
    assert game.player.lives == 0 and text(game, 0x2501, 1) == "0"
    assert run_until(game, lambda g: g.phase == "game_over", 300, quiet=True) == 200
    assert text(game, 0x2D18, 9) == "GAME OVER"


@pytest.mark.parametrize(("hi", "final", "after"), [(1230, 1230, 1230), (1230, 1240, 1240), (1230, 1220, 1230)])
def test_high_score_is_taken_only_when_strictly_beaten_at_game_over(hi, final, after):  # 1676-1695
    game = hit_cannon()
    poke(game, P1_SHIPS_REM, 0)
    poke16(game, HI_SCOR_L, int(str(hi), 16))
    poke(game, PLAYER1_EX, 0)  # no extra ship to save the game
    set_score(game, final + 2000)  # beating it during play changes nothing
    run(game, 30)
    assert peek16(game, HI_SCOR_L) == int(str(hi), 16)
    set_score(game, final)
    run(game, 30)
    assert peek16(game, HI_SCOR_L) == int(str(after), 16)
    shown = "0000" if after == hi else str(after)  # printed only when taken (PrintHiScore 1950 at 1695)
    assert text(game, peek16(game, 0x20F6), 4) == shown


def test_extra_ship_at_1500_in_the_scoring_frame_and_only_once():  # 0935-0979; DIP 1500 (093F-0946)
    game = cannon_ready()
    set_score(game, 1490)
    aim(game, alien_xy(game, 2)[0] + 8)
    fire_once(game)
    for _ in during(lambda: peek(game, P1_ALIENS + 2), 40):
        assert peek(game, P1_SHIPS_REM) == 2 and peek(game, PLAYER1_EX) == 1
        fire_off(game)
        game.step()
    assert score(game) == 1500 and peek(game, P1_SHIPS_REM) == 3 and peek(game, PLAYER1_EX) == 0
    assert "life" in game.events and game.sound_ports[0] & 0x10
    assert game.player.lives == 4 and text(game, 0x2501, 1) == "4"
    assert shows(game, PLAYER_PIC, 88, 8, range(16))  # the third icon
    run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) == 0, 30, quiet=True)
    set_score(game, 2990)
    shoot(game, 13)
    run(game, 60, quiet=True)
    assert score(game) == 3000 and peek(game, P1_SHIPS_REM) == 3 and game.player.lives == 4


@pytest.mark.parametrize(("value", "awarded"), [(1490, False), (1650, True), (1790, True), (9990, True)])
def test_extra_ship_comes_at_any_score_from_1500(value, awarded):  # 094B-094E: score MSB CP 15h; RET C
    """The test runs every main-loop pass (0835) and is "MSB >= 15h", so a score that jumps past 15xx (a 300-point
    saucer from 1490 gives 1790) still earns the ship, in the frame the score gets there."""
    game = cannon_ready()
    set_score(game, value)
    run(game, 1, quiet=True)
    assert peek(game, P1_SHIPS_REM) == 2 + awarded and peek(game, PLAYER1_EX) == int(not awarded)
    assert game.player.lives == 3 + awarded and ("life" in game.events) == awarded


def test_extra_ship_sound_lasts_255_main_loop_passes():  # 0972 extraHold FF; FleetDelayExShip 17AA-17B1
    """extraHold is set to FF in the scoring frame and counted down once a main-loop pass; the sound goes off at the
    pass that takes it to 0. In frames that is 53 on the original's own timing (some 4.8 passes a frame) and 56 at
    4.5 passes a frame (the port's and the emulator's atomic timing)."""
    game = cannon_ready()
    set_score(game, 1490)
    shoot(game, 2)
    assert game.sound_ports[0] & 0x10 and 0xF8 <= peek(game, EXTRA_HOLD)  # FF less the frame's later passes
    passes, frames = 0xFF - peek(game, EXTRA_HOLD), 0
    for _ in during(lambda: game.sound_ports[0] & 0x10, 80):
        assert passes < 255
        hold = peek(game, EXTRA_HOLD)
        fire_off(game)
        game.step()
        frames += 1
        passes += (hold - peek(game, EXTRA_HOLD)) & 0xFF
    assert passes >= 255 and 50 <= frames <= 60


def test_score_wraps_from_9990_to_0000():  # AdjustScore 09A1-09A3 drops the carry
    game = cannon_ready()
    set_score(game, 9990)
    shoot(game, 2)
    assert score(game) == 0 and text(game, 0x271C, 4) == "0000" and game.score == 10000


def test_a_second_hit_on_the_blowing_up_cannon_changes_nothing():  # 060F rewrites 2015; 2016/2017 time it
    game = hit_cannon()
    hit = game.tick
    run(game, 12)
    put_alien_shot(game, PLU_SHOT, 68 + 6, 50)
    run_until(game, lambda g: peek(g, PLU_SHOT) != 0x80, 30)
    assert 30 <= peek(game, PLU_SHOT + SHOT_YR) <= 38  # it struck the explosion
    run_until(game, lambda g: peek(g, PLAYER_OK) == 1, 300)
    assert game.tick - hit == 189 and peek(game, P1_SHIPS_REM) == 1 and game.player.lives == 2


# ---- SH: shields, the bottom line ----


def test_four_shields_of_22_by_16_at_play_start():  # DrawShieldPl1 01EF-0208 (Xr 64 + 45n, Yr 48); 1D20
    game = play_start()
    assert _shields_fresh(game)
    shields = {x for s in SHIELD_XR for x in range(s, s + 22)}
    assert all(not lit(game, x, x, 48, 63) for x in range(32, 256) if x not in shields)
    assert lit(game, 32, 255, 48, 63) == sum(bin(b).count("1") for b in game.rom[SHIELD_PIC : SHIELD_PIC + 44]) * 4


def test_shield_damage_survives_a_death_but_not_a_new_rack():  # no shield code on the death path; 0A2A, 080E
    game = cannon_ready()
    put_alien_shot(game, SQU_SHOT, 118, 90)
    run_until(game, lambda g: peek(g, SQU_SHOT) == 0, 100, quiet=True)
    damaged = box_pixels(game, 109, 130, 48, 63)
    assert not _shields_fresh(game)
    die_and_respawn(game)
    assert box_pixels(game, 109, 130, 48, 63) == damaged
    clear_rack(game)
    assert _shields_fresh(game)


def test_shot_explosions_erode_the_bottom_line_and_a_death_does_not_redraw_it():  # 05F3 (CP 15); 0669; 0804
    game = cannon_ready()
    assert lit(game, 32, 255, 16, 16) == 224
    put_alien_shot(game, SQU_SHOT, 236, 98)  # its last move is to Yr 18: the blast covers Yr 16-23
    run_until(game, lambda g: peek(g, SQU_SHOT) == 0, 100, quiet=True)
    gone = {234 + c for c in range(6) if game.rom[0x1CDC + c] & 1}
    assert gone and {x for x in range(32, 256) if not pixel(game, x, 16)} == gone
    die_and_respawn(game)
    assert lit(game, 32, 255, 16, 16) == 224 - len(gone)


def test_aliens_at_yr_56_overwrite_the_shield_tops_in_their_boxes():  # DrawSprite 15D3 at Yr 56 (R7)
    game = new_game()
    game.step()
    for i in range(55):
        poke(game, P1_ALIENS + i, int(i < 4))
    poke(game, P1_REF_ALIEN_Y, 56)
    to_play(game)
    run(game, 3, quiet=True)  # aliens 0-3 drawn once, at Xr 56 + 16i
    for i in range(4):
        assert all(column(game, 56 + 16 * i + c, 56) == game.rom[alien_pic(i, 0) + c] for c in range(16))
    assert _shield_tops_outside(game, range(56, 120))  # shield 2 from Xr 120 on, shields 3 and 4: intact


def _shield_tops_outside(game, xs):
    """Whether the shields' top halves (Yr 56-63) are intact at every Xr not in xs."""
    pic = game.rom[SHIELD_PIC : SHIELD_PIC + 44]
    return all(column(game, s + c, 56) == pic[2 * c + 1] for s in SHIELD_XR for c in range(22) if s + c not in xs)


# ---- N: sound events ----


def test_fleet_notes_cycle_through_four_and_last_4_frames():  # TimeFleetSound 1740-176C; FleetDelayExShip 1775
    game = play_start()
    notes, held = [], []
    for f in range(1, 400):
        fire_off(game)
        game.step()
        marches = [e for e in game.events if e.startswith("march")]
        if marches:
            notes.append((f, marches))
        if game.sound_ports[1] & 0x0F:
            held.append(f)
    assert notes == [(63 + 52 * k, [f"march{(k + 1) % 4 + 1}"]) for k in range(7)]
    assert held == [f for n, _ in notes for f in range(n, n + 4)]


FLEET_COUNTS = (55, 50, 49, 43, 42, 36, 35, 28, 27, 22, 21, 17, 16, 13, 12, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1)
FLEET_DELAYS = (52, 52, 46, 46, 39, 39, 34, 34, 28, 28, 24, 24, 21, 21, 19, 19, 16, 16, 14, 13, 12, 11, 9, 7, 5)


@pytest.mark.parametrize(("alive", "interval"), list(zip(FLEET_COUNTS, FLEET_DELAYS)))
def test_fleet_note_interval_by_alien_count(alive, interval):  # 177C-178F: tables 1A11 (counts), 1A21 (delays)
    game = play_start(set(range(alive)))
    starts = []
    for _ in during(lambda: len(starts) < 4, 400):
        fire_off(game)
        game.step()
        if any(e.startswith("march") for e in game.events):
            starts.append(game.tick)
    assert [b - a for a, b in zip(starts, starts[1:])] == [interval] * 3


@pytest.mark.parametrize("extra", [0, 20])
def test_fleet_notes_pause_while_the_cannon_is_down(extra):  # 1747-174B: no notes with playerOK 0
    game = cannon_ready()
    run(game, 20 + extra, ["right"], quiet=True)
    poke(game, PLAYER_ALIVE, 0)
    hit = game.tick
    due = next(n for n in range(PLAY + 63, 2000, 52) if n > hit + 5) - hit  # the next note, frames after the hit
    notes = []
    for f in range(1, 260):
        fire_off(game)
        game.step()
        if any(e.startswith("march") for e in game.events):
            notes.append(f)
    assert notes and notes[0] == 189 + due - 5 and all(b - a == 52 for a, b in zip(notes, notes[1:]))


def test_shot_alien_hit_and_saucer_sounds_follow_their_objects():  # ShotSound 172C; 0A67/154E; 1804
    game = saucer_game()
    poke(game, PLAYER_XR, 52)  # shots at Xr 60: clear of shield 1, into column 0
    seen = set()
    for f in range(400):
        if f == 60:
            start_saucer(game)
        calm(game, ["fire"] if f % 7 == 0 else [])
        p3 = game.sound_ports[0]
        state = (peek(game, PLYR_SHOT_STATUS) != 0, peek(game, ALIEN_IS_EXPLODING) != 0)
        state += (bool(peek(game, SAUCER_ACTIVE) and not peek(game, SAUCER_HIT)),)
        assert (bool(p3 & 0x02), bool(p3 & 0x08), bool(p3 & 0x01)) == state, f
        seen.add(state)
    assert {(True, True, False), (True, False, True), (False, False, False)} <= seen


# ---- Q: quirks ----


def test_lone_type_a_alien_leaves_a_residue_at_xr_41_and_plunges():  # DrawSprite 15D3 with DX 3 (18F8); RackBump
    game = play_start({0})
    ys = []
    while not peek(game, 0x206D):  # invaded
        fire_off(game)
        game.step()
        ys.append(peek(game, REF_ALIEN_YR))
        assert len(ys) < 700
    assert len(ys) == 419 and lit(game, 41, 41, 32, 215)  # a column left behind at Xr 41
    assert ys[-8:] == [88, 80, 72, 64, 56, 48, 40, 32]  # 8 px a frame at the right edge: an invasion


def test_saucer_blown_up_at_xr_40_turns_a_left_moving_rack():  # 0756 explosion column 1 at Xr 41; RackBump 15B7
    game = saucer_game({r * 11 + c for r in range(5) for c in range(6, 11)})
    poke(game, RACK_DIRECTION, 1)
    poke(game, REF_ALIEN_D_XR, 0xFE)
    start_saucer(game, from_right=True)
    calm_until(game, lambda g: peek(g, SAUCER_XR) == 40, 400)
    poke(game, PLAYER_XR, 48)
    poke(game, PLYR_SHOT_STATUS, 2)
    poke(game, OBJ1_COOR_XR, 56)
    poke(game, OBJ1_COOR_YR, 204)
    calm_until(game, lambda g: peek(g, SAUCER_HIT), 3)
    assert peek(game, SAUCER_XR) == 40 and peek(game, RACK_DIRECTION) == 1
    calm_until(game, lambda g: lit(g, 41, 41, 208, 215), 5)
    calm(game)
    assert (peek(game, RACK_DIRECTION), peek(game, REF_ALIEN_D_XR), peek(game, REF_ALIEN_D_YR)) == (0, 2, 0xF8)
    assert peek(game, REF_ALIEN_XR) + 96 > 140  # its leftmost column is far from the edge
    y = peek(game, REF_ALIEN_YR)
    run_until(game, lambda g: peek(g, REF_ALIEN_YR) != y, 30, quiet=True)
    assert peek(game, REF_ALIEN_YR) == y - 8


def hold_rack(game, yr, xr):
    """Hold the reference alien at (xr, yr) with no step pending, and the aliens' fire off."""
    fire_off(game)
    for addr, value in ((REF_ALIEN_D_YR, 0), (REF_ALIEN_D_XR, 0), (REF_ALIEN_YR, yr), (REF_ALIEN_XR, xr)):
        poke(game, addr, value)


def fire_from(game, xr, yr_ref, xr_ref):
    """With the rack held, move the cannon so that its shot leaves at Xr xr, fire, and step until the shot is
    over or has hit something."""
    for _ in during(lambda: peek(game, PLAYER_XR) + 8 != xr or obj0_timer(game), 400):
        hold_rack(game, yr_ref, xr_ref)
        game.step(["right"] if peek(game, PLAYER_XR) + 8 < xr else ["left"] if peek(game, PLAYER_XR) + 8 > xr else [])
    hold_rack(game, yr_ref, xr_ref)
    game.step()
    hold_rack(game, yr_ref, xr_ref)
    game.step(["fire"])
    for _ in during(lambda: peek(game, PLYR_SHOT_STATUS) in (1, 2), 60):
        hold_rack(game, yr_ref, xr_ref)
        game.step()


@pytest.mark.parametrize("ref_yr", [48, 53, 54])
def test_a_shield_hit_left_of_a_low_rack_kills_its_column_0_alien(ref_yr):  # Cnt16s 1554, WrapRef 1590; 14F5-1501
    game = play_start({0, 4, 5})
    fire_from(game, 70, ref_yr, 100)  # into shield 1 at Yr 48, left of the rack: inside it while ref Yr < 48 + 6
    hold_rack(game, ref_yr, 100)
    game.step()
    if ref_yr < 54:
        assert peek(game, PLYR_SHOT_STATUS) == 5 and not peek(game, P1_ALIENS + 0) and score(game) == 10
        assert (peek(game, 0x2065), peek(game, 0x2064)) == (100, ref_yr)  # the explosion at alien 0's cell
    else:
        assert peek(game, PLYR_SHOT_STATUS) == 3 and peek(game, P1_ALIENS + 0) and score(game) == 0
    assert peek(game, OBJ1_COOR_YR) in (48, 46) and peek(game, P1_ALIENS + 4) and peek(game, P1_ALIENS + 5)


def test_a_hit_right_of_the_rack_can_kill_an_alien_a_row_up():  # CodeBug1 1504-150F; GetAlienStatPtr 1581
    game = play_start({44})
    poke(game, vaddr(222, 49), 1 << 1)  # debris right of shield 4
    fire_from(game, 222, 0xF8, 40)  # row 3 (Yr 40-55) and "column 12": 11 * 3 + 12 - 1 = 44
    assert peek(game, PLYR_SHOT_STATUS) == 5 and not peek(game, P1_ALIENS + 44)
    assert (peek(game, 0x2065), peek(game, 0x2064)) == (216, 40)  # the explosion goes to row 3's "column 12"
    hold_rack(game, 0xF8, 40)
    game.step()
    assert score(game) == 20  # row 3's value, not the top row's 30


def test_a_shot_at_exactly_the_ref_xr_is_in_column_1():  # Cnt16s 1556-1557: CP H; CALL NC WrapRef (equal is past)
    """Debris at (ref Xr, ref Yr + 4), in dead alien 0's cell: FindColumn counts a target equal to the ref as column
    1, so the hit finds alien 0 dead and is a plain miss. (A "column 0" would be index FF of the alien flags,
    21FF: the reserve ships.)"""
    game = play_start({1, 2, 3})
    run_until(game, lambda g: obj0_timer(g) == 0, 200, quiet=True)
    poke(game, vaddr(100, 124), 1 << 124 % 8)
    fire_from(game, 100, 120, 100)
    hold_rack(game, 120, 100)
    game.step()
    assert peek(game, PLYR_SHOT_STATUS) == 3 and peek(game, OBJ1_COOR_YR) == 122 and score(game) == 0
    assert peek(game, P1_SHIPS_REM) == 2 and game.player.lives == 3
    assert [peek(game, P1_ALIENS + i) for i in range(4)] == [0, 1, 1, 1]


def test_saucer_timer_stops_while_the_rack_is_wrapped_below_zero():  # TimeToSaucer 0916 (CP 78, unsigned)
    game = new_game()
    game.step()
    for i in range(55):
        poke(game, P1_ALIENS + i, int(i >= 44))
    poke(game, P1_REF_ALIEN_Y, 0xF8)  # row 4 at Yr 56
    to_play(game)
    for _ in range(250):
        fire_off(game)
        game.step()
        assert peek(game, REF_ALIEN_YR) >= 0xE8 and peek16(game, TILL_SAUCER) == 0x0600


@pytest.mark.parametrize(
    ("alive", "cannon", "launch_xr"),
    [
        ({0, 10}, 48, (150 + 160 + 7) & 0xFF),  # WrapRef counts 7, then 4 more: column 11
        ({0, 1}, 48, None),  # column 11 dead: no shot
        ({0, 10, 13}, 100, (150 + 160 + 7) & 0xFF),  # "column 14" (alien 13's), capped at 11 (0625-062A)
    ],
)
def test_rolling_shot_takes_column_11_when_the_ref_is_past_xr_128(alive, cannon, launch_xr):  # WrapRef 1590; 061B
    assert rolling_launch(alive, cannon, 150) == launch_xr


def test_the_extra_ship_is_given_once_a_game_even_after_the_score_wraps():  # 093A-093C, 0970 player1Ex
    game = cannon_ready()
    set_score(game, 1490)
    shoot(game, 2)
    assert peek(game, P1_SHIPS_REM) == 3
    run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) == 0, 30, quiet=True)
    set_score(game, 9990)
    shoot(game, 13)
    assert score(game) == 0 and game.score == 10000
    run_until(game, lambda g: peek(g, PLYR_SHOT_STATUS) == 0, 30, quiet=True)
    set_score(game, 1490)
    shoot(game, 24)
    run(game, 5, quiet=True)
    assert score(game) == 1510 and peek(game, P1_SHIPS_REM) == 3 and peek(game, PLAYER1_EX) == 0
