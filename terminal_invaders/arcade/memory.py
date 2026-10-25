"""The board's memory and the ROM's drawing, printing, shield, score and status routines, ported byte for byte.

Addresses are the ROM's: 0000-1FFF the program image (`rom`), 2000-23FF work RAM (`ram`), 2400-3FFF the bitmap
(`vram`, one bit per pixel, 32 bytes per Xr column, bit 0 lowest). The 8080 wraps addresses at 16 bits and the board
decodes the low 14 address lines, so 4000-FFFF repeat 0000-3FFF. Routine names are the ROM labels in snake_case
(`sub_xxxx` for an unnamed entry point); arguments are the input registers in the order A, B, C, D, E, H, L (a pair,
DE or HL, as one argument) and results are the output registers the ROM's callers use, in the same order.
"""

RAM = 0x2000
# 23D0-23FF: where the 8080's stack lives (SP starts at 2400; the deepest it goes, interrupts included, is 23DA in
# the original's play), which the port does not model. The rest of 2300-23FF is RAM nothing uses.
STACK = 0x23D0
VRAM = 0x2400
VRAM_SIZE = 0x1C00
ROM_SIZE = 0x2000
NEVER = 1 << 62  # `_due` while no interrupt can be taken where the main thread counts its cycles

# ClearPlayField 09D6: byte j (0-19h) of column r is stored CLEAR_START + CLEAR_COLUMN * r + CLEAR_BYTE * j cycles in,
# and the routine runs CLEAR_TIME cycles.
CLEAR_START, CLEAR_COLUMN, CLEAR_BYTE = 10, 1736, 66
CLEAR_TIME = 20 + 224 * CLEAR_COLUMN

# ConvToScr 1A47's instructions: PUSH BC; LD B,03; three times LD A,H; RRA; LD H,A; LD A,L; RRA; LD L,A; DEC B;
# JP NZ; then LD A,H; AND 3F; OR 20; LD H,A; POP BC; RET (191 cycles)
_CONV_TO_SCR = (11, 7) + (5, 4, 5, 5, 4, 5, 5, 10) * 3 + (5, 7, 7, 5, 10, 10)

# Work RAM (RAMUse.md names)
WAIT_ON_DRAW = 0x2000  # waitOnDraw
ALIEN_IS_EXPLODING = 0x2002  # alienIsExploding
EXP_ALIEN_TIMER = 0x2003  # expAlienTimer
ALIEN_ROW = 0x2004  # alienRow
ALIEN_FRAME = 0x2005  # alienFrame
ALIEN_CUR_INDEX = 0x2006  # alienCurIndex
REF_ALIEN_D_YR = 0x2007  # refAlienDYr
REF_ALIEN_D_XR = 0x2008  # refAlienDXr
REF_ALIEN_YR = 0x2009  # refAlienYr
REF_ALIEN_XR = 0x200A  # refAlienXr
ALIEN_POS_LSB = 0x200B  # alienPosLSB
ALIEN_POS_MSB = 0x200C  # alienPosMSB
RACK_DIRECTION = 0x200D  # rackDirection
RACK_DOWN_DELTA = 0x200E  # rackDownDelta
OBJ0_TIMER_MSB = 0x2010  # obj0TimerMSB
OBJ0_TIMER_LSB = 0x2011  # obj0TimerLSB
OBJ0_TIMER_EXTRA = 0x2012  # obj0TimerExtra
OBJ0_HANLDER_LSB = 0x2013  # obj0HanlderLSB
OBJ0_HANLDER_MSB = 0x2014  # oBJ0HanlderMSB
PLAYER_ALIVE = 0x2015  # playerAlive
EXP_ANIMATE_TIMER = 0x2016  # expAnimateTimer
EXP_ANIMATE_CNT = 0x2017  # expAnimateCnt
PLYR_SPR_PIC_L = 0x2018  # plyrSprPicL
PLYR_SPR_PIC_M = 0x2019  # plyrSprPicM
PLAYER_YR = 0x201A  # playerYr
PLAYER_XR = 0x201B  # playerXr
PLYR_SPR_SIZ = 0x201C  # plyrSprSiz
NEXT_DEMO_CMD = 0x201D  # nextDemoCmd
HID_MESS_SEQ = 0x201E  # hidMessSeq
OBJ1_TIMER_MSB = 0x2020  # obj1TimerMSB
OBJ1_TIMER_LSB = 0x2021  # obj1TimerLSB
OBJ1_TIMER_EXTRA = 0x2022  # obj1TimerExtra
OBJ1_HANDLER_LSB = 0x2023  # obj1HandlerLSB
OBJ1_HANDLER_MSB = 0x2024  # obj1HandlerMSB
PLYR_SHOT_STATUS = 0x2025  # plyrShotStatus
BLOW_UP_TIMER = 0x2026  # blowUpTimer
OBJ1_IMAGE_LSB = 0x2027  # obj1ImageLSB
OBJ1_IMAGE_MSB = 0x2028  # obj1ImageMSB
OBJ1_COOR_YR = 0x2029  # obj1CoorYr
OBJ1_COOR_XR = 0x202A  # obj1CoorXr
OBJ1_IMAGE_SIZE = 0x202B  # obj1ImageSize
SHOT_DELTA_X = 0x202C  # shotDeltaX
FIRE_BOUNCE = 0x202D  # fireBounce
OBJ2_TIMER_MSB = 0x2030  # obj2TimerMSB
OBJ2_TIMER_LSB = 0x2031  # obj2TimerLSB
OBJ2_TIMER_EXTRA = 0x2032  # obj2TimerExtra
OBJ2_HANDLER_LSB = 0x2033  # obj2HandlerLSB
OBJ2_HANDLER_MSB = 0x2034  # obj2HandlerMSB
ROL_SHOT_STATUS = 0x2035  # rolShotStatus
ROL_SHOT_STEP_CNT = 0x2036  # rolShotStepCnt
ROL_SHOT_TRACK = 0x2037  # rolShotTrack
ROL_SHOT_C_FIR_LSB = 0x2038  # rolShotCFirLSB
ROL_SHOT_C_FIR_MSB = 0x2039  # rolShotCFirMSB
ROL_SHOT_BLOW_CNT = 0x203A  # rolShotBlowCnt
ROL_SHOT_IMAGE_LSB = 0x203B  # rolShotImageLSB
ROL_SHOT_IMAGE_MSB = 0x203C  # rolShotImageMSB
ROL_SHOT_YR = 0x203D  # rolShotYr
ROL_SHOT_XR = 0x203E  # rolShotXr
ROL_SHOT_SIZE = 0x203F  # rolShotSize
OBJ3_TIMER_MSB = 0x2040  # obj3TimerMSB
OBJ3_TIMER_LSB = 0x2041  # obj3TimerLSB
OBJ3_TIMER_EXTRA = 0x2042  # obj3TimerExtra
OBJ3_HANDLER_LSB = 0x2043  # obj3HandlerLSB
OBJ3_HANDLER_MSB = 0x2044  # obj3HandlerMSB
PLU_SHOT_STATUS = 0x2045  # pluShotStatus
PLU_SHOT_STEP_CNT = 0x2046  # pluShotStepCnt
PLU_SHOT_TRACK = 0x2047  # pluShotTrack
PLU_SHOT_C_FIR_LSB = 0x2048  # pluShotCFirLSB
PLU_SHOT_C_FIR_MSB = 0x2049  # pluShotCFirMSB
PLU_SHOT_BLOW_CNT = 0x204A  # pluShotBlowCnt
PLU_SHOT_IMAGE_LSB = 0x204B  # pluShotImageLSB
PLU_SHOT_IMAGE_MSB = 0x204C  # pluShotImageMSB
PLU_SHOT_YR = 0x204D  # pluShotYr
PLU_SHOT_XR = 0x204E  # pluSHotXr
PLU_SHOT_SIZE = 0x204F  # pluShotSize
OBJ4_TIMER_MSB = 0x2050  # obj4TimerMSB
OBJ4_TIMER_LSB = 0x2051  # obj4TimerLSB
OBJ4_TIMER_EXTRA = 0x2052  # obj4TimerExtra
OBJ4_HANDLER_LSB = 0x2053  # obj4HandlerLSB
OBJ4_HANDLER_MSB = 0x2054  # obj4HandlerMSB
SQU_SHOT_STATUS = 0x2055  # squShotStatus
SQU_SHOT_STEP_CNT = 0x2056  # squShotStepCnt
SQU_SHOT_TRACK = 0x2057  # squShotTrack
SQU_SHOT_C_FIR_LSB = 0x2058  # squShotCFirLSB
SQU_SHOT_C_FIR_MSB = 0x2059  # squShotCFirMSB
SQU_SHOT_BLOW_CNT = 0x205A  # squSHotBlowCnt
SQU_SHOT_IMAGE_LSB = 0x205B  # squShotImageLSB
SQU_SHOT_IMAGE_MSB = 0x205C  # squShotImageMSB
SQU_SHOT_YR = 0x205D  # squShotYr
SQU_SHOT_XR = 0x205E  # squShotXr
SQU_SHOT_SIZE = 0x205F  # squShotSize
END_OF_TASKS = 0x2060  # endOfTasks
COLLISION = 0x2061  # collision
EXP_ALIEN_LSB = 0x2062  # expAlienLSB
EXP_ALIEN_MSB = 0x2063  # expAlienMSB
EXP_ALIEN_YR = 0x2064  # expAlienYr
EXP_ALIEN_XR = 0x2065  # expAlienXr
EXP_ALIEN_SIZE = 0x2066  # expAlienSize
PLAYER_DATA_MSB = 0x2067  # playerDataMSB
PLAYER_OK = 0x2068  # playerOK
ENABLE_ALIEN_FIRE = 0x2069  # enableAlienFire
ALIEN_FIRE_DELAY = 0x206A  # alienFireDelay
ONE_ALIEN = 0x206B  # oneAlien
TEMP206C = 0x206C  # temp206C
INVADED = 0x206D  # invaded
SKIP_PLUNGER = 0x206E  # skipPlunger
OTHER_SHOT1 = 0x2070  # otherShot1
OTHER_SHOT2 = 0x2071  # otherShot2
VBLANK_STATUS = 0x2072  # vblankStatus
A_SHOT_STATUS = 0x2073  # aShotStatus
A_SHOT_STEP_CNT = 0x2074  # aShotStepCnt
A_SHOT_TRACK = 0x2075  # aShotTrack
A_SHOT_C_FIR_LSB = 0x2076  # aShotCFirLSB
A_SHOT_C_FIR_MSB = 0x2077  # aShotCFirMSB
A_SHOT_BLOW_CNT = 0x2078  # aShotBlowCnt
A_SHOT_IMAGE_LSB = 0x2079  # aShotImageLSB
A_SHOT_IMAGE_MSB = 0x207A  # aShotImageMSB
ALIEN_SHOT_YR = 0x207B  # alienShotYr
ALIEN_SHOT_XR = 0x207C  # alienShotXr
ALIEN_SHOT_SIZE = 0x207D  # alienShotSize
ALIEN_SHOT_DELTA = 0x207E  # alienShotDelta
SHOT_PIC_END = 0x207F  # shotPicEnd
SHOT_SYNC = 0x2080  # shotSync
TMP2081 = 0x2081  # tmp2081
NUM_ALIENS = 0x2082  # numAliens
SAUCER_START = 0x2083  # saucerStart
SAUCER_ACTIVE = 0x2084  # saucerActive
SAUCER_HIT = 0x2085  # saucerHit
SAUCER_HIT_TIME = 0x2086  # saucerHitTime
# The saucer's descriptor, which ReadDesc reads from 2087, under RAMUse's names: 2087-2088 hold the picture pointer,
# 2089 the saucer's Yr and 208A its Xr, 208B the picture's size; 208C holds the Xr step of each move.
SAUCER_PRI_LOC_LSB = 0x2087  # saucerPriLocLSB
SAUCER_PRI_LOC_MSB = 0x2088  # saucerPriLocMSB
SAUCER_PRI_PIC_LSB = 0x2089  # saucerPriPicLSB
SAUCER_PRI_PIC_MSB = 0x208A  # saucerPriPicMSB
SAUCER_PRI_SIZE = 0x208B  # saucerPriSize
SAUCER_DELTA_Y = 0x208C  # saucerDeltaY
SAU_SCORE_LSB = 0x208D  # sauScoreLSB
SAU_SCORE_MSB = 0x208E  # sauScoreMSB
SHOT_COUNT_LSB = 0x208F  # shotCountLSB
SHOT_COUNT_MSB = 0x2090  # shotCountMSB
TILL_SAUCER_LSB = 0x2091  # tillSaucerLSB
TILL_SAUCER_MSB = 0x2092  # tillSaucerMSB
WAIT_START_LOOP = 0x2093  # waitStartLoop
SOUND_PORT3 = 0x2094  # soundPort3
CHANGE_FLEET_SND = 0x2095  # changeFleetSnd
FLEET_SND_CNT = 0x2096  # fleetSndCnt
FLEET_SND_RELOAD = 0x2097  # fleetSndReload
SOUND_PORT5 = 0x2098  # soundPort5
EXTRA_HOLD = 0x2099  # extraHold
TILT = 0x209A  # tilt
FLEET_SND_HOLD = 0x209B  # fleetSndHold
ISR_DELAY = 0x20C0  # isrDelay
ISR_SPLASH_TASK = 0x20C1  # isrSplashTask
SPLASH_AN_FORM = 0x20C2  # splashAnForm
SPLASH_DELTA_X = 0x20C3  # splashDeltaX
SPLASH_DELTA_Y = 0x20C4  # splashDeltaY
SPLASH_YR = 0x20C5  # splashYr
SPLASH_XR = 0x20C6  # splashXr
SPLASH_IMAGE_LSB = 0x20C7  # splashImageLSB
SPLASH_IMAGE_MSB = 0x20C8  # splashImageMSB
SPLASH_IMAGE_SIZE = 0x20C9  # splashImageSize
SPLASH_TARGET_Y = 0x20CA  # splashTargetY
SPLASH_REACHED = 0x20CB  # splashReached
SPLASH_IM_REST_LSB = 0x20CC  # splashImRestLSB
SPLASH_IM_REST_MSB = 0x20CD  # splashImRestMSB
TWO_PLAYERS = 0x20CE  # twoPlayers
A_SHOT_RELOAD_RATE = 0x20CF  # aShotReloadRate
PLAYER1_EX = 0x20E5  # player1Ex
PLAYER2_EX = 0x20E6  # player2Ex
PLAYER1_ALIVE = 0x20E7  # player1Alive
PLAYER2_ALIVE = 0x20E8  # player2Alive
SUSPEND_PLAY = 0x20E9  # suspendPlay
COIN_SWITCH = 0x20EA  # coinSwitch
NUM_COINS = 0x20EB  # numCoins
SPLASH_ANIMATE = 0x20EC  # splashAnimate
DEMO_CMD_PTR_LSB = 0x20ED  # demoCmdPtrLSB
DEMO_CMD_PTR_MSB = 0x20EE  # demoCmdPtrMSB
GAME_MODE = 0x20EF  # gameMode
ADJUST_SCORE = 0x20F1  # adjustScore
SCORE_DELTA_LSB = 0x20F2  # scoreDeltaLSB
SCORE_DELTA_MSB = 0x20F3  # scoreDeltaMSB
HI_SCOR_L = 0x20F4  # HiScorL
HI_SCOR_M = 0x20F5  # HiScorM
HI_SCOR_LO_L = 0x20F6  # HiScorLoL
HI_SCOR_LO_M = 0x20F7  # HiScorLoM
P1_SCOR_L = 0x20F8  # P1ScorL
P1_SCOR_M = 0x20F9  # P1ScorM
P1_SCOR_LO_L = 0x20FA  # P1ScorLoL
P1_SCOR_LO_M = 0x20FB  # P1ScorLoM
P2_SCOR_L = 0x20FC  # P2ScorL
P2_SCOR_M = 0x20FD  # P2ScorM
P2_SCOR_LO_L = 0x20FE  # P2ScorLoL
P2_SCOR_LO_M = 0x20FF  # P2ScorLoM
P1_REF_ALIEN_DX = 0x21FB  # p1RefAlienDX
P1_REF_ALIEN_Y = 0x21FC  # p1RefAlienY
P1_REF_ALIEN_X = 0x21FD  # p1RefAlienX
P1_RACK_CNT = 0x21FE  # p1RackCnt
P1_SHIPS_REM = 0x21FF  # p1ShipsRem
P2_REF_ALIEN_DX = 0x22FB  # p2RefAlienDX
P2_REF_ALIEN_YR = 0x22FC  # p2RefAlienYr
P2_REF_ALIEN_XR = 0x22FD  # p2RefAlienXr
P2_RACK_CNT = 0x22FE  # p2RackCnt
P2_SHIPS_REM = 0x22FF  # p2ShipsRem

P1_ALIENS = 0x2100  # player 1 alien ship indicators, 55 bytes (0 = dead)
P1_SHIELDS = 0x2142  # player 1 shields remembered between rounds, 4 x 44 bytes
P2_ALIENS = 0x2200  # player 2 alien ship indicators
P2_SHIELDS = 0x2242  # player 2 shields remembered between rounds

# I/O ports (Hardware.md names)
INP0 = 0x00
INP1 = 0x01  # coins, starts, player 1 fire/left/right
INP2 = 0x02  # DIP switches, tilt, player 2 fire/left/right
SHFT_IN = 0x03  # read: shifter result
SHFTAMNT = 0x02  # write: shift amount (3 bits)
SOUND1 = 0x03  # write: sound bits (port 3)
SHFT_DATA = 0x04  # write: shift data
SOUND2 = 0x05  # write: sound bits (port 5)
WATCHDOG = 0x06  # write: watchdog


class Memory:
    """The board's memory and I/O, mixed into ArcadeGame: RAM, VRAM, the ROM image, the shifter and the sound latches.

    `text` maps the VRAM address where DrawChar drew a character to its code; `dirty` collects the display columns
    (Xr - 32) written since the view last cleared it, None meaning all of them; `port_writes` lists this frame's
    (port, value) sound writes.

    `cycles` counts the CPU cycles of the ROM instructions the ported routines stand for, along the path each call
    takes (8080 timings, a routine's own RET included and its caller's CALL not). The routines the main thread runs
    count them as they go (`_tick`, with every instruction's length) and make each memory and port access when
    `cycles` is the start of the instruction that makes it; in a counted run (a game-loop pass, or a routine a flow
    runs counted) `_tick` takes an interrupt where it comes due (machine.py). A routine may instead count its whole
    running time at once when it ends before `_due`, the time the next interrupt is due (never, outside counted
    runs). While `_trace` is a list, every RAM or VRAM write is made and logged in it as (cycle, address, old value,
    new value), and every sound-port write is logged as (cycle, port, None, value) and not made: `_paced` undoes the
    writes and makes them all again at their times.
    """

    def init_memory(self, rom) -> None:
        """Power-on state: zeroed RAM and VRAM, an empty text layer, the shifter and sound latches at 0."""
        if len(rom) != ROM_SIZE:
            raise ValueError(f"the ROM image must be {ROM_SIZE} bytes, not {len(rom)}")
        self.rom = rom
        self.ram = bytearray(VRAM - RAM)
        self.vram = bytearray(VRAM_SIZE)
        self.text: dict[int, int] = {}
        self.dirty: set[int] | None = None
        self.shift_reg = 0  # the 16-bit shift register: a write to port 4 enters at the top
        self.shift_amount = 0
        self.sound_port3 = 0
        self.sound_port5 = 0
        self.port_writes: list[tuple[int, int]] = []
        self.cycles = 0
        self._due = NEVER
        self._replay = False  # a counted run is being run again up to the frame boundary: its sound writes are known
        self._lost = False  # the program has gone where the port cannot follow it (ArcadeGame.step)
        self._trace: list[tuple[int, int, int | None, int]] | None = None

    def _tick(self, *lengths: int) -> None:
        """Run main-thread instructions of these lengths (the first may be the one that made the access just made;
        the others make none): the clock moves on, and an interrupt due by one of their boundaries is taken there."""
        self.cycles += sum(lengths)
        if self.cycles >= self._due:
            self._late(lengths)

    # ---- bus ----

    def rd(self, addr: int) -> int:
        """Read one byte anywhere in the address space (ROM below 2000)."""
        addr &= 0x3FFF
        if addr >= VRAM:
            return self.vram[addr - VRAM]
        if addr >= RAM:
            return self.ram[addr - RAM]
        return self.rom[addr]

    def wr(self, addr: int, v: int) -> None:
        """Write one byte; writes to the ROM are lost, writes to VRAM mark their column dirty. The game writes where
        the stack lives only when it draws over RAM (a lone invader wrapped round the screen): that write sets
        `_lost`."""
        addr &= 0x3FFF
        if self._trace is not None and addr >= RAM:
            self._trace.append((self.cycles, addr, self.rd(addr), v & 0xFF))
        if addr >= VRAM:
            i = addr - VRAM
            self.vram[i] = v & 0xFF
            if self.dirty is not None:
                self.dirty.add(i >> 5)
        elif addr >= RAM:
            self.ram[addr - RAM] = v & 0xFF
            if addr >= STACK:
                self._lost = True

    def rd16(self, addr: int) -> int:
        """Read a little-endian word (LD HL,(nn))."""
        return self.rd(addr) | (self.rd((addr + 1) & 0xFFFF) << 8)

    def wr16(self, addr: int, v: int) -> None:
        """Write a little-endian word (LD (nn),HL)."""
        self.wr(addr, v & 0xFF)
        self.wr((addr + 1) & 0xFFFF, (v >> 8) & 0xFF)

    def out(self, port: int, value: int) -> None:
        """OUT: 2 shift amount, 4 shift data, 3 and 5 sound (latched and recorded), 6 watchdog (ignored)."""
        value &= 0xFF
        if self._trace is not None and port in (SOUND1, SOUND2):
            self._trace.append((self.cycles, port, None, value))
            return
        if port == SHFTAMNT:
            self.shift_amount = value & 7
        elif port == SHFT_DATA:
            self.shift_reg = (value << 8) | (self.shift_reg >> 8)
        elif port == SOUND1:
            self.sound_port3 = value
            if not self._replay:
                self.port_writes.append((port, value))
        elif port == SOUND2:
            self.sound_port5 = value
            if not self._replay:
                self.port_writes.append((port, value))

    def inp(self, port: int) -> int:
        """IN for port 3, the shifter's result; the controls and DIP ports belong to the game."""
        if port != SHFT_IN:
            raise ValueError(f"port {port} is read by the game, not the memory model")
        return (self.shift_reg >> (8 - self.shift_amount)) & 0xFF

    def _all_dirty(self) -> None:
        if self.dirty is not None:
            self.dirty.update(range(VRAM_SIZE // 32))

    def _span(self, scr: int, rows: int, width: int) -> int:
        """VRAM index of screen address scr if `rows` rows of `width` bytes from it stay inside VRAM (their
        columns are then marked dirty), else -1: the fast paths below write VRAM directly only in that case."""
        i = scr - VRAM
        last = i + 0x20 * (rows - 1) + width - 1
        if i < 0 or last >= VRAM_SIZE:
            return -1
        if self.dirty is not None:
            self.dirty.update(range(i >> 5, (last >> 5) + 1))
        return i

    # ---- RAM mirror, shields ----

    def draw_bottom_line(self) -> None:  # DrawBottomLine 01CF
        """Draw the 1 px line under the player across the screen. In: nothing. Out: nothing."""
        self.cycles += 34
        self.sub_14cc(0x01, 0xE0, 0x2402)

    def copy_ram_mirror(self) -> None:  # CopyRAMMirror 01E4
        """Copy the ROM's RAM mirror 1B00-1BBF over 2000-20BF. In: nothing. Out: nothing."""
        self.cycles += 7
        self.sub_01e6(0xC0)

    def sub_01e6(self, b: int) -> None:  # 01E6
        """Copy B bytes (0 = 256) of the RAM mirror at 1B00 to 2000 (power-on uses B = 0). In: B. Out: nothing."""
        self.cycles += 30
        self.block_copy(b, 0x1B00, RAM)

    def draw_shield_pl1(self) -> None:  # DrawShieldPl1 01EF
        """Fill player 1's shield buffer with four fresh shields. In: nothing. Out: nothing."""
        self.cycles += 20
        self.sub_01f8(P1_SHIELDS)

    def draw_shield_pl2(self) -> None:  # DrawShieldPl2 01F5
        """Fill player 2's shield buffer with four fresh shields. In: nothing. Out: nothing."""
        self.cycles += 10
        self.sub_01f8(P2_SHIELDS)

    def sub_01f8(self, hl: int) -> None:  # 01F8
        """Copy the 44-byte shield picture at 1D20 four times to HL. In: HL. Out: nothing."""
        self.cycles += 17
        for _ in range(4):
            self.cycles += 35
            _, hl = self.block_copy(0x2C, 0x1D20, hl)
            self.cycles += 25
        self.cycles += 10

    # Only the player switch (02ED), which a one-player game never reaches, remembers the shields: RememberShields1/2
    # and CopyShields' remember branch are ported with the routine they belong to.

    def remember_shields1(self) -> None:  # RememberShields1 0209
        """Copy the four shields on screen to player 1's buffer. In: nothing. Out: nothing."""
        self.cycles += 27
        self.copy_shields(1, P1_SHIELDS)

    def remember_shields2(self) -> None:  # RememberShields2 020E
        """Copy the four shields on screen to player 2's buffer. In: nothing. Out: nothing."""
        self.cycles += 37
        self.copy_shields(1, P2_SHIELDS)

    def restore_shields2(self) -> None:  # RestoreShields2 0213
        """OR player 2's shield buffer onto the screen. In: nothing. Out: nothing."""
        self.cycles += 24
        self.copy_shields(0, P2_SHIELDS)

    def restore_shields1(self) -> None:  # RestoreShields1 021A
        """OR player 1's shield buffer onto the screen. In: nothing. Out: nothing."""
        self.cycles += 14
        self.copy_shields(0, P1_SHIELDS)

    def copy_shields(self, a: int, de: int) -> None:  # CopyShields 021E
        """Move four shields of 22 rows x 2 bytes between the buffer and the screen at 2806 (2E0 bytes between them).

        In: A (not 0: screen to buffer, 0: buffer ORed onto screen), DE buffer. Out: nothing."""
        self.wr(TMP2081, a)
        self.cycles += 40
        hl = 0x2806
        for count in range(4, 0, -1):
            self.cycles += 66
            if self.rd(TMP2081):
                de, hl = self.remember_shields(0x16, 0x02, de, hl)
                self.cycles += 10
            else:
                de, hl = self.restore_shields(0x16, 0x02, de, hl)
            if count > 1:
                hl = (hl + 0x02E0) & 0xFFFF
                self.cycles += 81
            else:
                self.cycles += 36

    # ---- characters and numbers ----

    def sub_08f1(self, de: int, hl: int) -> None:  # 08F1
        """Print the 3-character saucer score at DE to screen HL. In: DE, HL. Out: nothing."""
        self.cycles += 7
        self.print_message(0x03, de, hl)

    def print_message(self, c: int, de: int, hl: int) -> None:  # PrintMessage 08F3
        """Print C characters (0 = 256) from DE at screen HL, one character every 100h. In: C, DE, HL. Out: nothing."""
        for _ in range(c or 256):
            self.cycles += 35
            hl = self.draw_char(self.rd(de), hl)
            self.cycles += 30
            de = (de + 1) & 0xFFFF
        self.cycles += 10

    def draw_char(self, a: int, hl: int) -> int:  # DrawChar 08FF
        """Draw character A's 8 font bytes (1E00 + A*8) down from screen HL; record it in the text layer if HL is on
        the screen (the game never prints elsewhere).

        In: A, HL. Out: HL (100h further: the next character cell)."""
        if VRAM <= hl <= 0x3FFF:
            self.text[hl] = a
        # LD DE,1E00; PUSH HL; LD H,00; LD L,A; ADD HL,HL x3; ADD HL,DE; EX DE,HL; POP HL; LD B,08; OUT (06),A; JP 1439
        self._tick(10, 11, 7, 5, 10, 10, 10, 10, 4, 10, 7, 10, 10)
        return self.draw_simp_sprite(0x08, (0x1E00 + a * 8) & 0xFFFF, hl)

    def print4_digits(self, de: int, hl: int) -> None:  # Print4Digits 09AD
        """Print DE as four hex/BCD digits at screen HL, D first. In: DE, HL. Out: nothing."""
        self._tick(5, 17)  # LD A,D; CALL 09B2
        hl = self.draw_hex_byte(de >> 8, hl)
        self._tick(5)  # LD A,E (into 09B2)
        self.draw_hex_byte(de & 0xFF, hl)

    def draw_hex_byte(self, a: int, hl: int) -> int:  # DrawHexByte 09B2
        """Print A as two hex/BCD digits at screen HL, high digit first. In: A, HL. Out: HL (after both digits)."""
        self._tick(11, 11, 4, 4, 4, 4, 7, 17)  # PUSH DE; PUSH AF; RRCA x4; AND 0F; CALL 09C5
        hl = self.sub_09c5((a >> 4) & 0x0F, hl)
        self._tick(10, 7, 17)  # POP AF; AND 0F; CALL 09C5
        hl = self.sub_09c5(a & 0x0F, hl)
        self._tick(10, 10)  # POP DE; RET
        return hl

    def sub_09c5(self, a: int, hl: int) -> int:  # 09C5
        """Draw digit A (code A + 1A) at screen HL. In: A, HL. Out: HL (the next cell)."""
        self._tick(7, 10)  # ADD A,1A; JP 08FF
        return self.draw_char((a + 0x1A) & 0xFF, hl)

    def sub_09ca(self) -> int:  # 09CA
        """Point at the active player's score descriptor. In: nothing. Out: HL (20F8 player 1, 20FC player 2)."""
        if self.rd(PLAYER_DATA_MSB) & 1:
            self._tick(13, 4, 10, 11)  # LD A,(2067); RRCA; LD HL,20F8; RET C
            return P1_SCOR_L
        self._tick(13, 4, 10, 5, 10, 10)
        return P2_SCOR_L

    def clear_play_field(self) -> None:  # ClearPlayField 09D6
        """Clear bytes 02-1B of every screen column: all but the score rows and the bottom.

        In: nothing. Out: nothing."""
        t = self.cycles
        self.cycles = end = t + CLEAR_TIME
        if self._trace is None:
            vram = self.vram
            zeros = bytes(0x1A)
            for col in range(0, VRAM_SIZE, 32):
                vram[col + 2 : col + 0x1C] = zeros
            self._all_dirty()
            return
        wr = self.wr
        for r in range(224):
            for j in range(0x1A):
                self.cycles = t + CLEAR_START + CLEAR_COLUMN * r + CLEAR_BYTE * j  # LD (HL),00
                wr(VRAM + 0x20 * r + 2 + j, 0)
        self.cycles = end

    # ---- sprites ----

    def draw_shifted_sprite(self, b: int, de: int, hl: int) -> None:  # DrawShiftedSprite 1400
        """OR B rows of the picture at DE onto the screen at pixel position HL through the shifter. Run only inside
        the interrupts (the game objects), so it counts its time at once: no interrupt falls in it.

        In: B, DE, HL. Out: nothing."""
        self.cycles += 21
        hl = self.cnvt_pix_number(hl)
        n = b or 256
        self.cycles += 14 + 166 * n
        amt = 8 - self.shift_amount
        reg = self.shift_reg
        i = self._span(hl, n, 2)
        if i >= 0 and de + n <= ROM_SIZE:
            vram = self.vram
            for v in self.rom[de : de + n]:
                reg = (v << 8) | (reg >> 8)
                vram[i] |= (reg >> amt) & 0xFF
                reg >>= 8
                vram[i + 1] |= reg >> amt
                i += 0x20
        else:
            rd, wr = self.rd, self.wr
            for _ in range(n):
                reg = (rd(de) << 8) | (reg >> 8)
                wr(hl, ((reg >> amt) & 0xFF) | rd(hl))
                nxt = (hl + 1) & 0xFFFF
                de = (de + 1) & 0xFFFF
                reg >>= 8
                wr(nxt, (reg >> amt) | rd(nxt))
                hl = (hl + 0x20) & 0xFFFF
        self.shift_reg = reg

    def erase_simple_sprite(self, b: int, hl: int) -> None:  # EraseSimpleSprite 1424
        """Clear two bytes per row for B rows at pixel position HL (sets the shift amount). Run only inside the
        interrupts (GameObj0's blow-up, AExplodeTime from DrawAlien), so it counts its time at once.

        In: B, HL. Out: nothing."""
        self.cycles += 17
        hl = self.cnvt_pix_number(hl)
        n = b or 256
        self.cycles += 10 + 105 * n
        i = self._span(hl, n, 2)
        if i >= 0:
            zeros = bytes(n)
            self.vram[i : i + 0x20 * n : 0x20] = zeros
            self.vram[i + 1 : i + 1 + 0x20 * n : 0x20] = zeros
            return
        wr = self.wr
        for _ in range(n):
            wr(hl, 0)
            wr((hl + 1) & 0xFFFF, 0)
            hl = (hl + 0x20) & 0xFFFF

    def draw_simp_sprite(self, b: int, de: int, hl: int) -> int:  # DrawSimpSprite 1439
        """Store B bytes (0 = 256) from DE at screen HL, one per row, unshifted.

        In: B, DE, HL. Out: HL (after the last row)."""
        n = b or 256
        end = self.cycles + 75 * n + 10
        if end < self._due and self._trace is None and de + n <= ROM_SIZE:
            i = self._span(hl, n, 1)
            if i >= 0:
                self.cycles = end
                self.vram[i : i + 0x20 * n : 0x20] = self.rom[de : de + n]
                return hl + 0x20 * n
        rd, wr, tick = self.rd, self.wr, self._tick
        for _ in range(n):
            tick(11)  # PUSH BC
            v = rd(de)
            tick(7)  # LD A,(DE)
            wr(hl, v)
            tick(7, 5, 10, 10, 10, 5, 10)  # LD (HL),A; INC DE; LD BC,0020; ADD HL,BC; POP BC; DEC B; JP NZ
            de = (de + 1) & 0xFFFF
            hl = (hl + 0x20) & 0xFFFF
        tick(10)  # RET
        return hl

    def erase_shifted(self, b: int, de: int, hl: int) -> None:  # EraseShifted 1452
        """Clear the pixels of B rows of the picture at DE from pixel position HL through the shifter. Run only
        inside the interrupts (the game objects), so it counts its time at once: no interrupt falls in it.

        In: B, DE, HL. Out: nothing."""
        self.cycles += 17
        hl = self.cnvt_pix_number(hl)
        n = b or 256
        self.cycles += 10 + 174 * n
        amt = 8 - self.shift_amount
        reg = self.shift_reg
        i = self._span(hl, n, 2)
        if i >= 0 and de + n <= ROM_SIZE:
            vram = self.vram
            for v in self.rom[de : de + n]:
                reg = (v << 8) | (reg >> 8)
                vram[i] &= ~(reg >> amt)
                reg >>= 8
                vram[i + 1] &= ~(reg >> amt)
                i += 0x20
        else:
            rd, wr = self.rd, self.wr
            for _ in range(n):
                reg = (rd(de) << 8) | (reg >> 8)
                wr(hl, ~(reg >> amt) & rd(hl))
                nxt = (hl + 1) & 0xFFFF
                de = (de + 1) & 0xFFFF
                reg >>= 8
                wr(nxt, ~(reg >> amt) & rd(nxt))
                hl = (hl + 0x20) & 0xFFFF
        self.shift_reg = reg

    def cnvt_pix_number(self, hl: int) -> int:  # CnvtPixNumber 1474
        """Set the shift amount from pixel position HL and convert it to a screen address. In: HL. Out: HL."""
        if self.cycles + 32 + 191 < self._due:
            self.shift_amount = hl & 7
            self.cycles += 32 + 191
            return 0x2000 | ((hl >> 3) & 0x1FFF)
        self._tick(5, 7)  # LD A,L; AND 07
        self.out(SHFTAMNT, hl & 7)
        self._tick(10, 10)  # OUT (02),A; JP 1A47
        return self.conv_to_scr(hl)

    def remember_shields(self, b: int, c: int, de: int, hl: int) -> tuple[int, int]:  # RememberShields 147C
        """Copy B rows of C bytes from screen HL to the buffer at DE.

        In: B, C, DE, HL. Out: DE, HL (both past the block)."""
        rows, width = b or 256, c or 256
        t = self.cycles
        row = 77 + 39 * width
        rd, wr = self.rd, self.wr
        for r in range(rows):
            start = hl
            for j in range(width):
                self.cycles = t + row * r + 29 + 39 * j  # LD (DE),A
                wr(de, rd(hl))
                de = (de + 1) & 0xFFFF
                hl = (hl + 1) & 0xFFFF
            hl = (start + 0x20) & 0xFFFF
        self.cycles = t + row * rows + 10
        return de, hl

    def draw_spr_collision(self, b: int, de: int, hl: int) -> None:  # DrawSprCollision 1491
        """OR B rows of the picture at DE at pixel position HL; collision (2061) = 1 if any lit pixel was already on.
        Run only inside the interrupts (the game objects), so it counts its time at once.

        In: B, DE, HL. Out: nothing (the flag is in RAM)."""
        self.cycles += 17
        hl = self.cnvt_pix_number(hl)
        n = b or 256
        self.cycles += 27 + 242 * n
        amt = 8 - self.shift_amount
        reg = self.shift_reg
        i = self._span(hl, n, 2)
        hits = 0  # bytes whose pixels met lit ones: 20 cycles each (setting the flag)
        if i >= 0 and de + n <= ROM_SIZE:
            vram = self.vram
            for v in self.rom[de : de + n]:
                reg = (v << 8) | (reg >> 8)
                x = (reg >> amt) & 0xFF
                if x & vram[i]:
                    hits += 1
                vram[i] |= x
                reg >>= 8
                x = reg >> amt
                if x & vram[i + 1]:
                    hits += 1
                vram[i + 1] |= x
                i += 0x20
            self.ram[COLLISION - RAM] = 1 if hits else 0
        else:
            rd, wr = self.rd, self.wr
            wr(COLLISION, 0)
            for _ in range(n):
                reg = (rd(de) << 8) | (reg >> 8)
                v = (reg >> amt) & 0xFF
                if v & rd(hl):
                    hits += 1
                    wr(COLLISION, 1)
                wr(hl, v | rd(hl))
                nxt = (hl + 1) & 0xFFFF
                de = (de + 1) & 0xFFFF
                reg >>= 8
                v = (reg >> amt) & 0xFF
                if v & rd(nxt):
                    hits += 1
                    wr(COLLISION, 1)
                wr(nxt, v | rd(nxt))
                hl = (hl + 0x20) & 0xFFFF
        self.shift_reg = reg
        self.cycles += 20 * hits

    def clear_small_sprite(self, b: int, hl: int) -> int:  # ClearSmallSprite 14CB
        """Clear one byte per row for B rows (0 = 256) at screen HL. In: B, HL. Out: HL (after the last row)."""
        self.cycles += 4
        return self.sub_14cc(0, b, hl)

    def sub_14cc(self, a: int, b: int, hl: int) -> int:  # 14CC
        """Store A in one byte per row for B rows (0 = 256) at screen HL. In: A, B, HL. Out: HL (after the last row)."""
        n = b or 256
        t = self.cycles
        self.cycles = end = t + 63 * n + 10
        i = self._span(hl, n, 1)
        if i >= 0 and self._trace is None:
            self.vram[i : i + 0x20 * n : 0x20] = bytes((a,)) * n
            return hl + 0x20 * n
        wr = self.wr
        for k in range(n):
            self.cycles = t + 11 + 63 * k  # row k's LD (HL),A
            wr(hl, a)
            hl = (hl + 0x20) & 0xFFFF
        self.cycles = end
        return hl

    def draw_sprite(self, b: int, de: int, hl: int) -> None:  # DrawSprite 15D3
        """Store B rows of the picture at DE at pixel position HL through the shifter, overwriting both bytes.

        In: B, DE, HL. Out: nothing."""
        self._tick(17)  # CALL 1474
        hl = self.cnvt_pix_number(hl)
        n = b or 256
        end = self.cycles + 11 + 152 * n + 20
        if end < self._due and self._trace is None and de + n <= ROM_SIZE:
            i = self._span(hl, n, 2)
            if i >= 0:
                self.cycles = end
                amt = 8 - self.shift_amount
                reg = self.shift_reg
                vram = self.vram
                for v in self.rom[de : de + n]:
                    reg = (v << 8) | (reg >> 8)
                    vram[i] = (reg >> amt) & 0xFF
                    reg >>= 8
                    vram[i + 1] = reg >> amt
                    i += 0x20
                self.shift_reg = reg
                return
        rd, wr, out, inp, tick = self.rd, self.wr, self.out, self.inp, self._tick
        tick(11)  # PUSH HL
        for _ in range(n):
            tick(11, 11)  # PUSH BC; PUSH HL
            v = rd(de)
            tick(7)  # LD A,(DE)
            out(SHFT_DATA, v)
            tick(10)  # OUT (04),A
            v = inp(SHFT_IN)
            tick(10)  # IN A,(03)
            wr(hl, v)
            tick(7, 5, 5, 4)  # LD (HL),A; INC HL; INC DE; XOR A
            out(SHFT_DATA, 0)
            tick(10)  # OUT (04),A
            v = inp(SHFT_IN)
            tick(10)  # IN A,(03)
            wr((hl + 1) & 0xFFFF, v)
            tick(7, 10, 10, 10, 10, 5, 10)  # LD (HL),A; POP HL; LD BC,0020; ADD HL,BC; POP BC; DEC B; JP NZ
            de = (de + 1) & 0xFFFF
            hl = (hl + 0x20) & 0xFFFF
        tick(10, 10)  # POP HL; RET

    def get_player_data_ptr(self) -> int:  # GetPlayerDataPtr 1611
        """Point at the active player's data page. In: nothing. Out: HL (2100 or 2200)."""
        self._tick(7)  # LD L,00
        h = self.rd(PLAYER_DATA_MSB)
        self._tick(13, 5, 10)  # LD A,(2067); LD H,A; RET
        return h << 8

    def cur_ply_alive(self) -> int:  # CurPlyAlive 1910
        """Point at the active player's alive flag. In: nothing. Out: HL (20E7 player 1, 20E8 player 2)."""
        self._tick(10)  # LD HL,20E7
        if self.rd(PLAYER_DATA_MSB) & 1:
            self._tick(13, 4, 11)  # LD A,(2067); RRCA; RET C
            return PLAYER1_ALIVE
        self._tick(13, 4, 5, 5, 10)  # ...; RET C; INC HL; RET
        return PLAYER2_ALIVE

    def sub_092e(self) -> tuple[int, int]:  # 092E
        """Get the active player's ships remaining. In: nothing. Out: A (the count), HL (21FF or 22FF)."""
        self._tick(17)  # CALL 1611
        hl = self.get_player_data_ptr() | 0xFF
        self._tick(7)  # LD L,FF
        a = self.rd(hl)
        self._tick(7, 10)  # LD A,(HL); RET
        return a, hl

    # ---- sound ----

    def sound_bits3_on(self, b: int) -> None:  # SoundBits3On 18FA
        """OR B into soundPort3 and write it to port 3. In: B. Out: nothing."""
        a = self.rd(SOUND_PORT3) | b
        self._tick(13, 4)  # LD A,(2094); OR B
        self.wr(SOUND_PORT3, a)
        self._tick(13)  # LD (2094),A
        self.out(SOUND1, a)
        self._tick(10, 10)  # OUT (03),A; RET

    def sound_bits3_off(self, b: int) -> None:  # SoundBits3Off 19DC
        """AND soundPort3 with mask B and write it to port 3. In: B. Out: nothing."""
        a = self.rd(SOUND_PORT3) & b
        self._tick(13, 4)  # LD A,(2094); AND B
        self.wr(SOUND_PORT3, a)
        self._tick(13)  # LD (2094),A
        self.out(SOUND1, a)
        self._tick(10, 10)  # OUT (03),A; RET

    # ---- scores and status ----

    def draw_score_head(self) -> None:  # DrawScoreHead 191A
        """Print " SCORE<1> HI-SCORE SCORE<2> " across the top. In: nothing. Out: nothing."""
        self.cycles += 37
        self.print_message(0x1C, 0x1AE4, 0x241E)

    def sub_1925(self) -> None:  # 1925
        """Print player 1's score. In: nothing. Out: nothing."""
        self.cycles += 20
        self.draw_score(P1_SCOR_L)

    def sub_192b(self) -> None:  # 192B
        """Print player 2's score. In: nothing. Out: nothing."""
        self.cycles += 20
        self.draw_score(P2_SCOR_L)

    def draw_score(self, hl: int) -> None:  # DrawScore 1931
        """Print the score of descriptor HL (value LSB, MSB, screen address LSB, MSB). In: HL. Out: nothing."""
        de = self.rd16(hl)
        self.cycles += 58
        self.print4_digits(de, self.rd16((hl + 2) & 0xFFFF))

    def sub_193c(self) -> None:  # 193C
        """Print "CREDIT " at 3501. In: nothing. Out: nothing."""
        self.cycles += 37
        self.print_message(0x07, 0x1FA9, 0x3501)

    def draw_num_credits(self) -> None:  # DrawNumCredits 1947
        """Print numCoins as two digits at 3C01. In: nothing. Out: nothing."""
        self.cycles += 33
        self.draw_hex_byte(self.rd(NUM_COINS), 0x3C01)

    def print_hi_score(self) -> None:  # PrintHiScore 1950
        """Print the high score. In: nothing. Out: nothing."""
        self.cycles += 20
        self.draw_score(HI_SCOR_L)

    def draw_status(self) -> None:  # DrawStatus 1956
        """Clear the screen, then print the score header, both scores, the high score and the credits.

        In: nothing. Out: nothing."""
        self.cycles += 17
        self.clear_screen()
        for routine in (self.draw_score_head, self.sub_1925, self.sub_192b, self.print_hi_score, self.sub_193c):
            self.cycles += 17
            routine()
        self.cycles += 10
        self.draw_num_credits()

    def enable_game_tasks(self) -> None:  # EnableGameTasks 19D1
        """suspendPlay = 1: the interrupts run the game objects. In: nothing. Out: nothing."""
        self.cycles += 7
        self.wr(SUSPEND_PLAY, 1)
        self.cycles += 23

    def dsable_game_tasks(self) -> None:  # DsableGameTasks 19D7
        """suspendPlay = 0. In: nothing. Out: nothing."""
        self._tick(4, 10)  # XOR A; JP 19D3
        self.wr(SUSPEND_PLAY, 0)
        self._tick(13, 10)  # LD (20E9),A; RET

    def draw_num_ships(self, a: int) -> None:  # DrawNumShips 19E6
        """Draw A reserve ships from 2701 (one every 200h), then clear the rest of the line up to 3501.

        In: A (the ROM tests the Z flag, which its one caller sets from A). Out: nothing."""
        hl = 0x2701
        self.cycles += 20
        while a:
            self.cycles += 39
            hl = self.draw_simp_sprite(0x10, 0x1C60, hl)
            self.cycles += 20
            a = (a - 1) & 0xFF
        self.sub_19fa(hl)

    def sub_19fa(self, hl: int) -> None:  # 19FA
        """Clear 16-row ship cells from screen HL until H reaches 35. In: HL. Out: nothing."""
        while True:
            self.cycles += 24
            hl = self.clear_small_sprite(0x10, hl)
            self.cycles += 22
            if hl >> 8 == 0x35:
                self.cycles += 10
                return

    def comp_y_to_beam(self, de: int) -> bool:  # CompYToBeam 1A06
        """True (carry) when bit 7 of the coordinate at DE matches vblankStatus: this interrupt runs the object.

        In: DE. Out: carry (HL is left at 2072, vblankStatus)."""
        if (self.rd(de) & 0x80) ^ self.rd(VBLANK_STATUS):
            self.cycles += 46
            return False
        self.cycles += 54
        return True

    def block_copy(self, b: int, de: int, hl: int) -> tuple[int, int]:  # BlockCopy 1A32
        """Copy B bytes (0 = 256) from DE to HL. In: B, DE, HL. Out: DE, HL (both past the block)."""
        n = b or 256
        t = self.cycles
        self.cycles = end = t + 39 * n + 10
        if RAM <= hl and hl + n <= VRAM and self._trace is None:
            if de + n <= ROM_SIZE:
                self.ram[hl - RAM : hl - RAM + n] = self.rom[de : de + n]
                return de + n, hl + n
            if RAM <= de and de + n <= VRAM and (hl >= de + n or hl + n <= de):
                self.ram[hl - RAM : hl - RAM + n] = self.ram[de - RAM : de - RAM + n]
                return de + n, hl + n
        rd, wr = self.rd, self.wr
        for k in range(n):
            self.cycles = t + 7 + 39 * k  # LD (HL),A
            wr(hl, rd(de))
            hl = (hl + 1) & 0xFFFF
            de = (de + 1) & 0xFFFF
        self.cycles = end
        return de, hl

    def read_desc(self, hl: int) -> tuple[int, int, int]:  # ReadDesc 1A3B
        """Load a 5-byte sprite descriptor (picture LSB, MSB, position LSB, MSB, size) from HL.

        In: HL. Out: B (size), DE (picture), HL (position)."""
        rd = self.rd
        if self.cycles + 75 < self._due:
            self.cycles += 75
            de = rd(hl) | (rd((hl + 1) & 0xFFFF) << 8)
            pos = rd((hl + 2) & 0xFFFF) | (rd((hl + 3) & 0xFFFF) << 8)
            return rd((hl + 4) & 0xFFFF), de, pos
        tick = self._tick
        e = rd(hl)
        tick(7, 5)  # LD E,(HL); INC HL
        d = rd((hl + 1) & 0xFFFF)
        tick(7, 5)  # LD D,(HL); INC HL
        lo = rd((hl + 2) & 0xFFFF)
        tick(7, 5)  # LD A,(HL); INC HL
        hi = rd((hl + 3) & 0xFFFF)
        tick(7, 5)  # LD C,(HL); INC HL
        b = rd((hl + 4) & 0xFFFF)
        tick(7, 5, 5, 10)  # LD B,(HL); LD H,C; LD L,A; RET
        return b, e | (d << 8), lo | (hi << 8)

    def conv_to_scr(self, hl: int) -> int:  # ConvToScr 1A47
        """Convert pixel position HL (Xr*256 + Yr) to its screen byte: 2000 + HL/8. In: HL. Out: HL."""
        if self.cycles + 191 < self._due:
            self.cycles += 191
        else:
            self._tick(*_CONV_TO_SCR)
        return 0x2000 | ((hl >> 3) & 0x1FFF)

    def clear_screen(self) -> None:  # ClearScreen 1A5C
        """Clear all of VRAM (2400-3FFF). Run only by DrawStatus at power-on and in NewGame, which are not timed.

        In: nothing. Out: nothing."""
        self.vram[:] = bytes(VRAM_SIZE)
        self._all_dirty()
        self.cycles += 20 + 37 * VRAM_SIZE

    def restore_shields(self, b: int, c: int, de: int, hl: int) -> tuple[int, int]:  # RestoreShields 1A69
        """OR B rows of C bytes from the buffer at DE onto screen HL.

        In: B, C, DE, HL. Out: DE, HL (both past the block)."""
        rows, width = b or 256, c or 256
        t = self.cycles
        row = 77 + 46 * width
        rd, wr = self.rd, self.wr
        for r in range(rows):
            start = hl
            for j in range(width):
                self.cycles = t + row * r + 36 + 46 * j  # LD (HL),A
                wr(hl, rd(de) | rd(hl))
                de = (de + 1) & 0xFFFF
                hl = (hl + 1) & 0xFFFF
            hl = (start + 0x20) & 0xFFFF
        self.cycles = t + row * rows + 10
        return de, hl

    def remove_ship(self) -> None:  # RemoveShip 1A7F
        """Take a ship from the active player's stash, redraw the reserve and print the count it had.

        In: nothing. Out: nothing."""
        self.cycles += 17
        a, hl = self.sub_092e()
        if a == 0:
            self.cycles += 15
            return
        self.cycles += 25
        self.wr(hl, a - 1)
        self.cycles += 24
        self.draw_num_ships(a - 1)
        self.cycles += 10
        self.sub_1a8b(a)

    def sub_1a8b(self, a: int) -> None:  # 1A8B
        """Print the low digit of A as the ships count at 2501. In: A. Out: nothing."""
        self._tick(10, 7, 10)  # LD HL,2501; AND 0F; JP 09C5
        self.sub_09c5(a & 0x0F, 0x2501)
