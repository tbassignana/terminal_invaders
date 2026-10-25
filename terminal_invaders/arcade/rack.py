"""The alien rack: InitRack, DrawAlien, the cursor and the reference alien, the player shot's hit test, scoring."""

from .memory import (
    ADJUST_SCORE,
    ALIEN_CUR_INDEX,
    ALIEN_FRAME,
    ALIEN_IS_EXPLODING,
    ALIEN_POS_LSB,
    ALIEN_ROW,
    EXP_ALIEN_LSB,
    EXP_ALIEN_TIMER,
    EXP_ALIEN_YR,
    GAME_MODE,
    INVADED,
    NUM_ALIENS,
    OBJ1_COOR_XR,
    OBJ1_COOR_YR,
    ONE_ALIEN,
    P1_ALIENS,
    P1_REF_ALIEN_DX,
    P1_SCOR_L,
    P2_ALIENS,
    P2_REF_ALIEN_DX,
    PLAYER_DATA_MSB,
    PLAYER_OK,
    PLYR_SHOT_STATUS,
    RACK_DIRECTION,
    RACK_DOWN_DELTA,
    RAM,
    REF_ALIEN_D_XR,
    REF_ALIEN_D_YR,
    REF_ALIEN_XR,
    REF_ALIEN_YR,
    SAUCER_HIT,
    SCORE_DELTA_LSB,
    SCORE_DELTA_MSB,
    TWO_PLAYERS,
    VRAM,
    WAIT_ON_DRAW,
)
from .program import Jump, bcd_add, rlc


class Rack:
    """Mixin for ArcadeGame: the routines that move, draw, hit and count the 55 aliens."""

    def init_rack(self) -> None:  # InitRack 00B1
        """Copy the player's reference alien and its delta into the game's rack variables. In, out: nothing."""
        self.cycles += 17
        hl = self.get_al_ref_ptr()
        ref = self.rd16(hl)
        self.cycles += 35
        self.wr16(REF_ALIEN_YR, ref)
        self.cycles += 16
        self.wr16(ALIEN_POS_LSB, ref)
        self.cycles += 55
        a = self.rd(hl - 1)
        if a == 0x03:
            a = 0x02
            self.cycles += 5
        self.wr(REF_ALIEN_D_XR, a)
        self.cycles += 37
        if a == 0xFE:
            self.cycles += 5
        self.wr(RACK_DIRECTION, 1 if a == 0xFE else 0)
        self.cycles += 23

    def sub_00d7(self) -> None:  # 00D7
        """Start both players' racks moving right (delta 2), then blank the absent player 2's score."""
        self.wr(P1_REF_ALIEN_DX, 0x02)
        self.wr(P2_REF_ALIEN_DX, 0x02)
        self.sub_08e4()

    def sub_08e4(self) -> None:  # 08E4
        """In a one-player game, clear player 2's score digits. In, out: nothing."""
        if self.rd(TWO_PLAYERS):
            return
        self.clear_small_sprite(0x20, 0x391C)

    def get_al_ref_ptr(self) -> int:  # GetAlRefPtr 0886
        """Point at the active player's saved reference alien. In: nothing. Out: HL (21FC or 22FC)."""
        self.cycles += 35
        return (self.rd(PLAYER_DATA_MSB) << 8) | 0xFC

    def draw_alien(self) -> None:  # DrawAlien 0100
        """Draw the alien under the cursor (or time down an explosion) and let the cursor move on. In, out: nothing."""
        self.cycles += 31
        if self.rd(ALIEN_IS_EXPLODING):
            self.a_explode_time()
            return
        self.cycles += 78
        if self.rd((self.rd(PLAYER_DATA_MSB) << 8) | self.rd(ALIEN_CUR_INDEX)):
            e = rlc(rlc(rlc(self.rd(ALIEN_ROW) & 0xFE)))
            de = 0x1C00 + e
            self.cycles += 93
            if self.rd(ALIEN_FRAME):
                de += 0x30  # 013B: the second animation frame
                self.cycles += 51
            else:
                self.cycles += 11
            self.cycles += 40
            self.draw_sprite(0x10, de, self.rd16(ALIEN_POS_LSB))
        self.wr(WAIT_ON_DRAW, 0)
        self.cycles += 27

    def cursor_next_alien(self) -> None:  # CursorNextAlien 0141
        """Move the cursor to the next live alien, moving the reference alien after the last one; an alien below
        Yr 28 ends the game (1971). In, out: nothing."""
        self.cycles += 17
        if not self.rd(PLAYER_OK):
            self.cycles += 11
            return
        self.cycles += 22
        if self.rd(WAIT_ON_DRAW):
            self.cycles += 11
            return
        self.cycles += 43
        h = self.rd(PLAYER_DATA_MSB)
        a = self.rd(ALIEN_CUR_INDEX)
        d = 0x02
        while True:
            a = (a + 1) & 0xFF
            self.cycles += 12
            if a == 0x37:
                self.cycles += 17
                d, h = self.move_ref_alien(d)
                if h is None:
                    return
                a = 0
            else:
                self.cycles += 11
            self.cycles += 27
            if self.rd((h << 8) | a) == 1:
                break
        self.wr(ALIEN_CUR_INDEX, a)
        self.cycles += 30
        c, d, yr = self.get_alien_coords(a)
        self.wr16(ALIEN_POS_LSB, (c << 8) | yr)
        self.cycles += 43
        if yr < 0x28:
            self.sub_1971()
        self.wr(ALIEN_ROW, d)
        self.wr(WAIT_ON_DRAW, 1)
        self.cycles += 48

    def get_alien_coords(self, index: int) -> tuple[int, int, int]:  # GetAlienCoords 017A
        """Pixel position and row of an alien. In: L (the alien's index). Out: C (Xr), D (row), L (Yr)."""
        d = 0
        a = index
        b = self.rd(REF_ALIEN_YR)
        c = self.rd(REF_ALIEN_XR)
        while not (a - 0x0B) & 0x80:  # JP M: the carry CP leaves for SBC is 0 on this path
            a = (a - 0x0B) & 0xFF
            b = (b + 0x10) & 0xFF
            d += 1
        self.cycles += 78 + 66 * d + 51 * a
        while a:
            c = (c + 0x10) & 0xFF
            a -= 1
        return c, d, b

    def move_ref_alien(self, d: int) -> tuple[int, int | None]:  # MoveRefAlien 01A1
        """Move the reference alien by its deltas, flip the animation frame and restart the cursor at 0.

        In: D (the cursor's pass count). Out: D, H (the player data MSB, None for ReturnTwo 01CD: no live alien
        was found in a whole pass, so the caller returns too)."""
        d -= 1
        if d == 0:
            self.cycles += 35
            return d, None
        self.wr(ALIEN_CUR_INDEX, 0)
        c = self.rd(REF_ALIEN_D_YR)
        self.wr(REF_ALIEN_D_YR, 0)
        self.cycles += 74
        self.add_delta(c, REF_ALIEN_D_YR)
        self.wr(ALIEN_FRAME, (self.rd(ALIEN_FRAME) + 1) & 0x01)
        self.cycles += 67
        return d, self.rd(PLAYER_DATA_MSB)

    def add_delta(self, c: int, hl: int) -> None:  # AddDelta 01D9
        """Add delta C to the coordinate at HL+2 and the delta at HL+1 to the one at HL+3. In: C, HL. Out: nothing."""
        b = self.rd(hl + 1)
        self.wr(hl + 2, c + self.rd(hl + 2))
        self.wr(hl + 3, b + self.rd(hl + 3))
        self.cycles += 70

    def init_aliens(self) -> None:  # InitAliens 01C0
        """Bring player 1's 55 aliens to life. In, out: nothing."""
        self.cycles += 10
        self.sub_01c3(P1_ALIENS)

    def init_aliens_p2(self) -> None:  # InitAliensP2 1904
        """Bring player 2's 55 aliens to life. In, out: nothing."""
        self.cycles += 20
        self.sub_01c3(P2_ALIENS)

    def sub_01c3(self, hl: int) -> None:  # 01C3
        """Set the 55 alien flags from HL to 1. In: HL. Out: nothing."""
        t = self.cycles + 7
        self.cycles = end = t + 30 * 0x37 + 10
        if self._trace is None:
            i = hl - RAM
            self.ram[i : i + 0x37] = b"\x01" * 0x37
            return
        for k in range(0x37):
            self.cycles = t + 30 * k  # LD (HL),01
            self.wr(hl + k, 0x01)
        self.cycles = end

    def sub_1971(self) -> None:  # 1971
        """The rack reached the bottom: flag the invasion and enter the end of the game (stack reset, 16E6)."""
        self.cycles += 7
        self.wr(INVADED, 1)
        self.cycles += 23
        raise Jump(self.sub_16e6())

    def a_explode_time(self) -> None:  # AExplodeTime 1538
        """Time down the alien explosion; at 0 erase it and retire the player's shot. In, out: nothing."""
        t = (self.rd(EXP_ALIEN_TIMER) - 1) & 0xFF
        self.wr(EXP_ALIEN_TIMER, t)
        self.cycles += 20
        if t:
            self.cycles += 11
            return
        self.cycles += 45
        self.erase_simple_sprite(0x10, self.rd16(EXP_ALIEN_YR))
        self.sub_1545()

    def player_shot_hit(self) -> None:  # PlayerShotHit 14D8
        """Decide what a moving player shot hit: the top of the screen, the saucer, an alien or something else.

        In, out: nothing."""
        a = self.rd(PLYR_SHOT_STATUS)
        if a == 0x05:
            self._tick(13, 7, 11)  # LD A,(2025); CP 05; RET Z
            return
        if a != 0x02:
            self._tick(13, 7, 5, 7, 11)  # ...; RET Z; CP 02; RET NZ
            return
        self._tick(13, 7, 5, 7, 5)
        self.sub_14e1()

    def sub_14e1(self) -> None:  # 14E1
        """PlayerShotHit's tests of a moving shot's position, and their outcome. In, out: nothing."""
        b = self.rd(OBJ1_COOR_YR)
        self._tick(13, 7, 5, 10)  # LD A,(2029); CP D8; LD B,A; JP NC,1530
        if b >= 0xD8:
            self.sub_1530()
            return
        if not self.rd(ALIEN_IS_EXPLODING):
            self._tick(13, 4, 11)  # LD A,(2002); AND A; RET Z
            return
        self._tick(13, 4, 5, 5, 7, 10)  # ...; RET Z; LD A,B; CP CE; JP NC,1579
        if b >= 0xCE:
            self.sub_1579()
            return
        b = (b + 0x06) & 0xFF
        self._tick(7, 5)  # ADD A,06; LD B,A
        a = self.rd(REF_ALIEN_YR)
        self._tick(13, 7, 10)  # LD A,(2009); CP 90; JP NC,1504
        if a < 0x90:
            self._tick(4, 10)  # CP B; JP NC,1530
            if a >= b:
                self.sub_1530()
                return
        self.code_bug1(b)

    def code_bug1(self, b: int) -> None:  # CodeBug1 1504
        """Find the rack slot the shot is in; kill the alien there (explosion, sound, score) or, with none alive,
        end the shot as a miss. In: B (the shot's Yr + 6). Out: nothing."""
        self._tick(5, 17)  # LD L,B; CALL 1562
        b, yr = self.find_row(b)
        h = self.rd(OBJ1_COOR_XR)
        self._tick(13, 5, 17)  # LD A,(202A); LD H,A; CALL 156F
        c, h = self.find_column(h)
        self.wr16(EXP_ALIEN_YR, (h << 8) | yr)
        self._tick(16, 7)  # LD (2064),HL; LD A,05
        self.wr(PLYR_SHOT_STATUS, 0x05)
        self._tick(13, 17)  # LD (2025),A; CALL 1581
        hl = self.get_alien_stat_ptr(b, c)
        alive = self.rd(hl)
        self._tick(7, 4, 10)  # LD A,(HL); AND A; JP Z,1530
        if not alive:
            self.sub_1530()
            return
        self.wr(hl, 0)
        self.kills += 1
        self.hits += 1
        self._tick(10, 17)  # LD (HL),00; CALL 0A5F
        hl = self.score_for_alien(b)
        self._tick(17)  # CALL 1A3B
        b, de, hl = self.read_desc(hl)
        self._tick(17)  # CALL 15D3
        self.draw_sprite(b, de, hl)
        self._tick(7)  # LD A,10
        self.wr(EXP_ALIEN_TIMER, 0x10)
        self._tick(13, 10)  # LD (2003),A; RET

    def sub_1530(self) -> None:  # 1530
        """The shot hit something that is not an alien (or left the screen): blow it up. In, out: nothing."""
        self._tick(7)  # LD A,03
        self.wr(PLYR_SHOT_STATUS, 0x03)
        self._tick(13, 10)  # LD (2025),A; JP 154A
        self.sub_154a()

    def sub_1545(self) -> None:  # 1545
        """Retire the player's shot (status 4). In, out: nothing."""
        self._tick(7)  # LD A,04
        self.wr(PLYR_SHOT_STATUS, 0x04)
        self._tick(13)  # LD (2025),A
        self.sub_154a()

    def sub_154a(self) -> None:  # 154A
        """Clear the alien-exploding flag and its sound. In, out: nothing."""
        self._tick(4)  # XOR A
        self.wr(ALIEN_IS_EXPLODING, 0)
        self._tick(13, 7, 10)  # LD (2002),A; LD B,F7; JP 19DC
        self.sound_bits3_off(0xF7)

    def sub_1579(self) -> None:  # 1579
        """The shot hit the saucer. In, out: nothing."""
        self._tick(7)  # LD A,01
        self.wr(SAUCER_HIT, 1)
        self._tick(13, 10)  # LD (2085),A; JP 1545
        self.sub_1545()

    def cnt16s(self, a: int, h: int) -> tuple[int, int]:  # Cnt16s 1554
        """Count the 16s that bring reference A up to target H. In: A, H. Out: A, C (the count)."""
        c = 0
        if a >= h:
            self._tick(7, 4, 17)  # LD C,00; CP H; CALL NC,1590
            a, c = self.wrap_ref(a, c)
        else:
            self._tick(7, 4, 11)
        while a < h:
            a = (a + 0x10) & 0xFF
            c = (c + 1) & 0xFF
            self._tick(4, 5, 7, 5, 10)  # CP H; RET NC; ADD A,10; INC C; JP 155A
        self._tick(4, 11)  # CP H; RET NC
        return a, c

    def wrap_ref(self, a: int, c: int) -> tuple[int, int]:  # WrapRef 1590
        """Add 16s to A (counting them in C) until its sign bit is clear. In: A, C. Out: A, C."""
        while True:
            c = (c + 1) & 0xFF
            a = (a + 0x10) & 0xFF
            if not a & 0x80:
                self._tick(5, 7, 10, 10)  # INC C; ADD A,10; JP M,1590; RET
                return a, c
            self._tick(5, 7, 10)

    def find_row(self, yr: int) -> tuple[int, int]:  # FindRow 1562
        """Rack row of a Yr coordinate. In: L (the Yr). Out: B (row from 0), L (the row's Yr)."""
        a = self.rd(REF_ALIEN_YR)
        self._tick(13, 5, 17)  # LD A,(2009); LD H,L; CALL 1554
        a, c = self.cnt16s(a, yr)
        self._tick(5, 5, 7, 5, 10)  # LD B,C; DEC B; SBC A,10; LD L,A; RET
        return (c - 1) & 0xFF, (a - 0x10) & 0xFF

    def find_column(self, h: int) -> tuple[int, int]:  # FindColumn 156F
        """Rack column of Xr coordinate H. In: H. Out: C (column from 1), H (the column's Xr)."""
        a = self.rd(REF_ALIEN_XR)
        self._tick(13, 17)  # LD A,(200A); CALL 1554
        a, c = self.cnt16s(a, h)
        self._tick(7, 5, 10)  # SBC A,10; LD H,A; RET
        return c, (a - 0x10) & 0xFF

    def get_alien_stat_ptr(self, b: int, c: int) -> int:  # GetAlienStatPtr 1581
        """Point at the alien flag of row B, column C (from 1), all in 8 bits. In: B, C. Out: HL."""
        a = (rlc(rlc(rlc(b))) + 3 * b + c - 1) & 0xFF
        self._tick(5, 4, 4, 4, 4, 4, 4, 4, 5, 5)  # LD A,B; RLCA x3; ADD A,B x3; ADD A,C; DEC A; LD L,A
        h = self.rd(PLAYER_DATA_MSB)
        self._tick(13, 5, 10)  # LD A,(2067); LD H,A; RET
        return (h << 8) | a

    def score_for_alien(self, b: int) -> int:  # ScoreForAlien 0A5F
        """Start the alien-hit sound and queue row B's score. In: B (row). Out: HL (2062, the explosion descriptor)."""
        if not self.rd(GAME_MODE):
            self._tick(13, 4, 10, 10, 10)  # LD A,(20EF); AND A; JP Z,0A7C; LD HL,2062; RET
            return EXP_ALIEN_LSB
        self._tick(13, 4, 10, 5, 7, 17)  # ...; JP Z; LD C,B; LD B,08; CALL 18FA
        self.sound_bits3_on(0x08)
        self._tick(5, 5, 17)  # LD B,C; LD A,B; CALL 097C
        a = self.rd(self.alien_score_value(b))
        self._tick(7, 10)  # LD A,(HL); LD HL,20F3
        self.wr(SCORE_DELTA_MSB, 0)
        self._tick(10, 5)  # LD (HL),00; DEC HL
        self.wr(SCORE_DELTA_LSB, a)
        self._tick(7, 5)  # LD (HL),A; DEC HL
        self.wr(ADJUST_SCORE, 1)
        self._tick(10, 10, 10)  # LD (HL),01; LD HL,2062; RET
        return EXP_ALIEN_LSB

    def alien_score_value(self, a: int) -> int:  # AlienScoreValue 097C
        """Point at the score of an alien in row A. In: A. Out: HL (1DA0 10, 1DA1 20, 1DA2 30 points)."""
        if a < 0x02:
            self._tick(10, 7, 11)  # LD HL,1DA0; CP 02; RET C
            return 0x1DA0
        if a < 0x04:
            self._tick(10, 7, 5, 5, 7, 11)  # ...; INC HL; CP 04; RET C
            return 0x1DA1
        self._tick(10, 7, 5, 5, 7, 5, 5, 10)  # ...; INC HL; RET
        return 0x1DA2

    def adjust_score(self) -> None:  # AdjustScore 0988
        """Add a queued score delta to the active player's BCD score and print it. In, out: nothing."""
        self._tick(17)  # CALL 09CA
        hl = self.sub_09ca()
        if not self.rd(ADJUST_SCORE):
            self._tick(13, 4, 11)  # LD A,(20F1); AND A; RET Z
            return
        self._tick(13, 4, 5, 4)  # ...; RET Z; XOR A
        self.wr(ADJUST_SCORE, 0)
        self._tick(13, 11)  # LD (20F1),A; PUSH HL
        de = self.rd16(SCORE_DELTA_LSB)
        self._tick(16, 4, 10)  # LD HL,(20F2); EX DE,HL; POP HL
        e, cy = bcd_add(self.rd(hl), de & 0xFF)
        self._tick(7, 4, 4)  # LD A,(HL); ADD A,E; DAA
        self.wr(hl, e)
        self._tick(7, 5, 5)  # LD (HL),A; LD E,A; INC HL
        d, cy = bcd_add(self.rd(hl + 1), de >> 8, cy)
        self._tick(7, 4, 4)  # LD A,(HL); ADC A,D; DAA
        self.wr(hl + 1, d)
        self._tick(7, 5, 5)  # LD (HL),A; LD D,A; INC HL
        lo = self.rd(hl + 2)
        self._tick(7, 5)  # LD A,(HL); INC HL
        hi = self.rd(hl + 3)
        self._tick(7, 5, 10)  # LD H,(HL); LD L,A; JP 09AD
        if cy and hl == P1_SCOR_L:
            self.score_wraps += 1
        self.print4_digits((d << 8) | e, lo | (hi << 8))

    def count_aliens(self) -> None:  # CountAliens 15F3
        """Count the active player's live aliens into numAliens (and flag a lone one). In, out: nothing."""
        self._tick(17)  # CALL 1611
        hl = self.get_player_data_ptr()
        self._tick(10)  # LD BC,3700
        # 15F9-1601 per alien: LD A,(HL); AND A; JP Z; (INC C if alive); INC HL; DEC B; JP NZ: 41 cycles, 46 if alive.
        # The flags left are counted at once up to the alien in whose loop the next interrupt falls due, which runs
        # instruction by instruction; the rest are read again after the interrupt.
        live = k = 0
        while k < 0x37:
            flags = self._flags(hl + k, 0x37 - k)
            n = len(flags) - flags.count(0)
            run = 41 * len(flags) + 5 * n
            if self.cycles + run < self._due:
                self.cycles += run
                live += n
                break
            t = self.cycles
            for v in flags:
                c = 46 if v else 41
                if t + c >= self._due:
                    break
                t += c
                live += v != 0
                k += 1
            self.cycles = t
            if self.rd(hl + k):
                live += 1
                self._tick(7, 4, 10, 5, 5, 5, 10)
            else:
                self._tick(7, 4, 10, 5, 5, 10)
            k += 1
        self._tick(5)  # LD A,C
        self.wr(NUM_ALIENS, live)
        if live != 1:
            self._tick(13, 7, 11)  # LD (2082),A; CP 01; RET NZ
            return
        self._tick(13, 7, 5, 10)  # ...; RET NZ; LD HL,206B
        self.wr(ONE_ALIEN, 1)
        self._tick(10, 10)  # LD (HL),01; RET

    def _flags(self, addr: int, n: int) -> bytes | bytearray:
        """The n bytes from addr, a span inside one 256-byte page, as the bus reads them. The player's alien flags are
        in RAM, but a lone alien drawn over RAM can overwrite playerDataMSB, and CountAliens then reads wherever it
        points: the ROM, or VRAM through the address mirror."""
        a = addr & 0x3FFF
        if a >= VRAM:
            return self.vram[a - VRAM : a - VRAM + n]
        if a >= RAM:
            return self.ram[a - RAM : a - RAM + n]
        return self.rom[a : a + n]

    def plyr_shot_and_bump(self) -> None:  # PlyrShotAndBump 190A
        """The game loop's shot classification and rack bump. In, out: nothing."""
        self._tick(17)  # CALL 14D8
        self.player_shot_hit()
        self._tick(10)  # JP 1597
        self.rack_bump()

    def rack_bump(self) -> None:  # RackBump 1597
        """Turn the rack round (and drop it) when anything is lit at the edge it is moving to. In, out: nothing."""
        left = self.rd(RACK_DIRECTION)
        self._tick(13, 4, 10, 10, 17)  # LD A,(200D); AND A; JP NZ,15B7; LD HL,3EA4 (2524); CALL 15C5
        if not self.sub_15c5(0x2524 if left else 0x3EA4):
            self._tick(11)  # RET NC
            return
        if not left:
            self._tick(5, 7, 7)  # RET NC; LD B,FE; LD A,01
            b, a = 0xFE, 1
        else:
            self._tick(5, 17)  # RET NC; CALL 18F1
            b = self.sub_18f1()
            self._tick(4, 10)  # XOR A; JP 15A9
            a = 0
        self.wr(RACK_DIRECTION, a)
        self._tick(13, 5)  # LD (200D),A; LD A,B
        self.wr(REF_ALIEN_D_XR, b)
        self._tick(13)  # LD (2008),A
        dy = self.rd(RACK_DOWN_DELTA)
        self._tick(13)  # LD A,(200E)
        self.wr(REF_ALIEN_D_YR, dy)
        self._tick(13, 10)  # LD (2007),A; RET

    def sub_15c5(self, hl: int) -> bool:  # 15C5
        """Whether any of the 23 screen bytes from HL is lit. In: HL. Out: carry."""
        self._tick(7)  # LD B,17
        # Per byte LD A,(HL); AND A; JP NZ,166B; INC HL; DEC B; JP NZ (41), then RET (10), or SCF; RET (14) on a hit.
        # The bytes tested before the next interrupt falls due are counted at once; the one in whose test it falls
        # due runs instruction by instruction, and the rest are read again after the interrupt.
        i = hl - VRAM
        k = 0
        while True:
            rest = self.vram[i + k : i + 0x17]
            n = len(rest) - len(rest.lstrip(b"\0"))  # unlit bytes before the first lit one
            hit = n < len(rest)
            run = 41 * n + (21 + 14 if hit else 10)
            if self.cycles + run < self._due:
                self.cycles += run
                return hit
            m = max(0, min(n, (self._due - self.cycles - 1) // 41))
            self.cycles += 41 * m
            k += m
            if k == 0x17:
                self._tick(10)  # RET
                return False
            if self.rd(hl + k):
                self._tick(7, 4, 10, 4, 10)  # LD A,(HL); AND A; JP NZ,166B; SCF; RET
                return True
            self._tick(7, 4, 10, 5, 5, 10)
            k += 1

    def sub_18f1(self) -> int:  # 18F1
        """The rack's rightward delta: 2, or 3 for a lone alien. In: nothing. Out: B."""
        self._tick(7)  # LD B,02
        if self.rd(NUM_ALIENS) != 1:
            self._tick(13, 5, 11)  # LD A,(2082); DEC A; RET NZ
            return 0x02
        self._tick(13, 5, 5, 5, 10)  # ...; RET NZ; INC B; RET
        return 0x03
