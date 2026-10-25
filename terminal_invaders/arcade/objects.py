"""The game objects RunGameObjs runs from the interrupts: the player, its shot, the three alien shots and the saucer."""

from .memory import (
    A_SHOT_BLOW_CNT,
    A_SHOT_C_FIR_LSB,
    A_SHOT_IMAGE_LSB,
    A_SHOT_RELOAD_RATE,
    A_SHOT_STATUS,
    A_SHOT_STEP_CNT,
    A_SHOT_TRACK,
    ADJUST_SCORE,
    ALIEN_FIRE_DELAY,
    ALIEN_IS_EXPLODING,
    ALIEN_SHOT_DELTA,
    ALIEN_SHOT_SIZE,
    ALIEN_SHOT_XR,
    ALIEN_SHOT_YR,
    COLLISION,
    ENABLE_ALIEN_FIRE,
    GAME_MODE,
    INVADED,
    ISR_SPLASH_TASK,
    NEXT_DEMO_CMD,
    NUM_ALIENS,
    OBJ0_TIMER_EXTRA,
    OBJ0_TIMER_MSB,
    OBJ1_COOR_XR,
    OBJ1_COOR_YR,
    OBJ1_IMAGE_LSB,
    OBJ2_TIMER_EXTRA,
    OTHER_SHOT1,
    OTHER_SHOT2,
    PLAYER_ALIVE,
    PLAYER_DATA_MSB,
    PLAYER_OK,
    PLAYER_XR,
    PLAYER_YR,
    PLU_SHOT_C_FIR_LSB,
    PLU_SHOT_STATUS,
    PLU_SHOT_STEP_CNT,
    PLYR_SPR_PIC_L,
    RAM,
    ROL_SHOT_C_FIR_LSB,
    ROL_SHOT_STATUS,
    ROL_SHOT_STEP_CNT,
    SAU_SCORE_LSB,
    SAUCER_ACTIVE,
    SAUCER_DELTA_Y,
    SAUCER_HIT,
    SAUCER_HIT_TIME,
    SAUCER_PRI_LOC_LSB,
    SAUCER_PRI_PIC_MSB,
    SAUCER_START,
    SCORE_DELTA_LSB,
    SHOT_COUNT_LSB,
    SHOT_DELTA_X,
    SHOT_PIC_END,
    SHOT_SYNC,
    SKIP_PLUNGER,
    SOUND2,
    SOUND_PORT5,
    SQU_SHOT_C_FIR_LSB,
    SQU_SHOT_STATUS,
    SQU_SHOT_STEP_CNT,
    STACK,
)
from .program import Jump, rlc


class Objects:
    """Mixin for ArcadeGame: RunGameObjs and the five object handlers with their helpers."""

    def _handler_table(self) -> dict:
        """Handler address (the object's xx03/xx04 bytes) -> method, as RunGameObjs dispatches them."""
        return {
            0x028E: self.game_obj0,
            0x03BB: self.game_obj1,
            0x0476: self.game_obj2,
            0x04B6: self.game_obj3,
            0x0682: self.game_obj4,
        }

    def run_game_objs(self) -> None:  # RunGameObjs 0248
        """Run the object list from the player's object (2010). In, out: nothing."""
        self.cycles += 10
        self.sub_024b(OBJ0_TIMER_MSB)

    def sub_024b(self, hl: int) -> None:  # 024B
        """Run the object list from HL: count each object's timers down and call its handler when they are all 0.

        In: HL. Out: nothing."""
        ram = self.ram
        while True:
            if hl >= STACK:  # the list's end mark (2060) overwritten: the walk would go on into the stack
                self._lost = True
                return
            i = hl - RAM
            a = ram[i]
            if a == 0xFF:
                self.cycles += 25
                return
            if a != 0xFE:
                b = ram[i + 1]
                if a | b:
                    if b == 0:
                        a = (a - 1) & 0xFF
                        self.cycles += 5
                    ram[i + 1] = (b - 1) & 0xFF
                    ram[i] = a
                    self.cycles += 146
                elif ram[i + 2]:
                    ram[i + 2] -= 1
                    self.cycles += 158
                else:
                    self.cycles += 192
                    handler = self._handlers.get(ram[i + 3] | (ram[i + 4] << 8))
                    if handler is None:  # overwritten: the program would jump outside its code
                        self._lost = True
                    else:
                        handler(hl + 4)
                    self.cycles += 40
            else:
                self.cycles += 66
            hl += 0x10

    # ---- object 0: the player ----

    def game_obj0(self, hl: int) -> None:  # GameObj0 028E
        """Move and draw the player, or run its blow-up; the end of a blow-up ends the turn (02D0).

        In: HL (2014, pushed by RunGameObjs). Out: nothing."""
        hl += 1
        a = self.rd(hl)
        self.cycles += 39
        if a == 0xFF:
            self.sub_033b()
            return
        hl += 1
        t = (self.rd(hl) - 1) & 0xFF
        self.wr(hl, t)
        if t:
            self.cycles += 26
            return
        self.wr(PLAYER_OK, 0)
        self.wr(ENABLE_ALIEN_FIRE, 0)
        self.wr(ALIEN_FIRE_DELAY, 0x30)
        self.wr(hl, 0x05)
        hl += 1
        t = (self.rd(hl) - 1) & 0xFF
        self.wr(hl, t)
        self.cycles += 115
        if t:
            self.draw_player_die(a)
            return
        self.cycles += 40
        self.erase_simple_sprite(0x10, self.rd16(PLAYER_YR))
        self.cycles += 44
        self.block_copy(0x10, 0x1B10, OBJ0_TIMER_MSB)
        self.cycles += 24
        self.sound_bits3_off(0x00)
        self.cycles += 17
        if self.rd(INVADED):
            self.cycles += 11
            return
        self.cycles += 22
        if not self.rd(GAME_MODE):
            self.cycles += 11
            return
        self.cycles += 5
        raise Jump(self.sub_02d0())

    def sub_033b(self) -> None:  # 033B
        """The player is fine: count down the alien-fire delay, then move and draw the player. In, out: nothing."""
        self.wr(PLAYER_OK, 1)
        self.cycles += 56
        if not self.rd(ENABLE_ALIEN_FIRE):
            t = (self.rd(ALIEN_FIRE_DELAY) - 1) & 0xFF
            self.wr(ALIEN_FIRE_DELAY, t)
            self.cycles += 25
            if t == 0:
                self.wr(ENABLE_ALIEN_FIRE, 1)
                self.cycles += 29
        self.sub_034a()

    def sub_034a(self) -> None:  # 034A
        """Move the player one pixel by the controls (right wins) and draw it. In, out: nothing."""
        b = self.rd(PLAYER_XR)
        self.cycles += 45
        if self.rd(GAME_MODE):
            self.cycles += 17
            a = self.read_inputs()
            right, left = a & 0x40, a & 0x20
            self.cycles += 18
        else:
            a = self.rd(NEXT_DEMO_CMD)
            right, left = a & 0x01, a & 0x02
            self.cycles += 27
        if right:
            self.move_player_right(b)
        elif left:
            self.cycles += 14
            self.move_player_left(b)
        else:
            self.cycles += 14 if self.rd(GAME_MODE) else 24
            self.sub_036f()

    def move_player_right(self, b: int) -> None:  # MovePlayerRight 0381
        """Step right unless at Xr D9, then draw. In: B (the player's Xr). Out: nothing."""
        self.cycles += 22
        if b != 0xD9:
            self.wr(PLAYER_XR, b + 1)
            self.cycles += 28
        self.sub_036f()

    def move_player_left(self, b: int) -> None:  # MovePlayerLeft 038E
        """Step left unless at Xr 30, then draw. In: B (the player's Xr). Out: nothing."""
        self.cycles += 22
        if b != 0x30:
            self.wr(PLAYER_XR, b - 1)
            self.cycles += 28
        self.sub_036f()

    def sub_036f(self) -> None:  # 036F
        """Draw the player's sprite from its descriptor at 2018. In, out: nothing."""
        self.cycles += 27
        b, de, hl = self.read_desc(PLYR_SPR_PIC_L)
        self.cycles += 17
        hl = self.conv_to_scr(hl)
        self.cycles += 17
        self.draw_simp_sprite(b, de, hl)
        self.wr(OBJ0_TIMER_EXTRA, 0)
        self.cycles += 30

    def draw_player_die(self, a: int) -> None:  # DrawPlayerDie 039B
        """Flip the blow-up picture (1C70/1C80) and draw it. In: A (the picture number). Out: nothing."""
        a = (a + 1) & 0x01
        self.wr(PLAYER_ALIVE, a)
        self.wr16(PLYR_SPR_PIC_L, 0x1C00 | ((0x70 + rlc(rlc(rlc(rlc(a))))) & 0xFF))
        self.cycles += 86
        self.sub_036f()

    # ---- object 1: the player's shot ----

    def game_obj1(self, hl: int) -> None:  # GameObj1 03BB
        """Start, move or blow up the player's shot, in the interrupt matching its screen half.

        In: HL (2024). Out: nothing."""
        self.cycles += 27
        if not self.comp_y_to_beam(OBJ1_COOR_XR):
            self.cycles += 21
            return
        hl += 1
        a = self.rd(hl)
        self.cycles += 31
        if a == 0:
            self.cycles += 11
            return
        self.cycles += 22
        if a == 0x01:
            self.init_ply_shot(a, hl)
            return
        self.cycles += 17
        if a == 0x02:
            self.move_ply_shot()
            return
        hl += 1
        self.cycles += 22
        if a != 0x03:
            self.cycles += 7
            if a != 0x05:
                self.cycles += 15
                self.end_of_blowup()
            else:
                self.cycles += 11
            return
        t = (self.rd(hl) - 1) & 0xFF
        self.wr(hl, t)
        self.cycles += 20
        if t == 0:
            self.end_of_blowup()
            return
        self.cycles += 14
        if t != 0x0F:
            self.cycles += 11
            return
        self.cycles += 33
        b, de, pos = self.read_ply_shot()
        self.cycles += 17
        self.erase_shifted(b, de, pos)
        self.wr(hl + 1, self.rd(hl + 1) + 1)
        self.wr(hl + 3, self.rd(hl + 3) - 2)
        self.wr(hl + 4, self.rd(hl + 4) - 3)
        self.wr(hl + 5, 0x08)
        self.cycles += 122
        b, de, pos = self.read_ply_shot()
        self.cycles += 10
        self.draw_shifted_sprite(b, de, pos)

    def init_ply_shot(self, a: int, hl: int) -> None:  # InitPlyShot 03FA
        """Put a new shot above the centre of the player. In: A (1), HL (2025). Out: nothing."""
        self.wr(hl, a + 1)
        self.wr(OBJ1_COOR_XR, self.rd(PLAYER_XR) + 0x08)
        self.cycles += 62
        b, de, pos = self.read_ply_shot()
        self.cycles += 10
        self.draw_shifted_sprite(b, de, pos)

    def move_ply_shot(self) -> None:  # MovePlyShot 040A
        """Move the shot up by shotDeltaX and redraw it; a collision sets alienIsExploding. In, out: nothing."""
        self.cycles += 17
        b, de, hl = self.read_ply_shot()
        self.cycles += 50
        self.erase_shifted(b, de, hl)
        a = (self.rd(SHOT_DELTA_X) + hl) & 0xFF
        hl = (hl & 0xFF00) | a
        self.wr(OBJ1_COOR_YR, a)
        self.cycles += 82
        self.draw_spr_collision(b, de, hl)
        a = self.rd(COLLISION)
        if a:
            self.wr(ALIEN_IS_EXPLODING, a)
            self.cycles += 45
        else:
            self.cycles += 28

    def read_ply_shot(self) -> tuple[int, int, int]:  # ReadPlyShot 0430
        """Read the player shot's descriptor at 2027. In: nothing. Out: B, DE, HL."""
        self.cycles += 20
        return self.read_desc(OBJ1_IMAGE_LSB)

    def end_of_blowup(self) -> None:  # EndOfBlowup 0436
        """Erase the shot, reset its object, advance the saucer-score and shot-count pointers and, with no saucer
        flying, pick the next saucer's side. In, out: nothing."""
        self.cycles += 17
        b, de, hl = self.read_ply_shot()
        self.cycles += 17
        self.erase_shifted(b, de, hl)
        self.cycles += 44
        self.block_copy(0x07, 0x1B25, 0x2025)
        hl = self.rd16(SAU_SCORE_LSB)
        lo = (hl + 1) & 0xFF
        self.cycles += 43
        if lo >= 0x63:
            lo = 0x54
            self.cycles += 7
        self.wr16(SAU_SCORE_LSB, (hl & 0xFF00) | lo)
        hl = self.rd16(SHOT_COUNT_LSB)
        hl = (hl & 0xFF00) | ((hl + 1) & 0xFF)
        self.wr16(SHOT_COUNT_LSB, hl)
        # 0453-0461: LD (208D),HL; LD HL,(208F); INC L; LD (208F),HL; LD A,(2084); AND A; RET NZ (81 taken, 75 not)
        if self.rd(SAUCER_ACTIVE):
            self.cycles += 81
            return
        bc = 0x0229 if self.rd(hl) & 0x01 else 0xFEE0
        self.wr(SAUCER_PRI_PIC_MSB, bc & 0xFF)
        self.wr(SAUCER_DELTA_Y, bc >> 8)
        # 0462-046B: LD A,(HL); AND 01; LD BC,0229; JP NZ (34), and LD BC,FEE0 (10) when bit 0 is clear;
        # 046E-0475: LD HL,208A; LD (HL),C; INC HL; INC HL; LD (HL),B; RET (44)
        self.cycles += 75 + 34 + 44 + (0 if bc == 0x0229 else 10)

    # ---- objects 2-4: the alien shots ----

    def game_obj2(self, hl: int) -> None:  # GameObj2 0476
        """The rolling shot, aimed at the player; it also restarts the shot phase counter 2032.

        In: HL (2034, unused). Out: nothing."""
        self.wr(OBJ2_TIMER_EXTRA, self.rd(0x1B32))
        hl = self.rd16(ROL_SHOT_C_FIR_LSB)
        self.cycles += 71
        if hl == 0:
            self.wr16(ROL_SHOT_C_FIR_LSB, 0xFFFF)
            self.cycles += 31
            return
        self.cycles += 34
        self.to_shot_struct(0xF9, ROL_SHOT_STATUS)
        self.wr(OTHER_SHOT1, self.rd(PLU_SHOT_STEP_CNT))
        self.wr(OTHER_SHOT2, self.rd(SQU_SHOT_STEP_CNT))
        self.cycles += 69
        self.handle_alien_shot()
        self.cycles += 37
        if self.rd(A_SHOT_BLOW_CNT):
            self.from_shot_struct(ROL_SHOT_STATUS)
            return
        self.reset_shot()

    def reset_shot(self) -> None:  # ResetShot 04AB
        """Reload the rolling shot's object from the ROM mirror. In, out: nothing."""
        self.cycles += 37
        self.block_copy(0x10, 0x1B30, 0x2030)

    def game_obj3(self, hl: int) -> None:  # GameObj3 04B6
        """The plunger shot, fired down the columns of ColFireTable 1D00; off when one alien is left.

        In: HL (2044, unused). Out: nothing."""
        self.cycles += 27
        if self.rd(SKIP_PLUNGER):
            self.cycles += 11
            return
        self.cycles += 25
        if self.rd(SHOT_SYNC) != 0x01:
            self.cycles += 11
            return
        self.cycles += 39
        self.to_shot_struct(0xED, PLU_SHOT_STATUS)
        self.wr(OTHER_SHOT1, self.rd(ROL_SHOT_STEP_CNT))
        self.wr(OTHER_SHOT2, self.rd(SQU_SHOT_STEP_CNT))
        self.cycles += 69
        self.handle_alien_shot()
        self.cycles += 30
        if self.rd(A_SHOT_C_FIR_LSB) >= 0x10:
            self.wr(A_SHOT_C_FIR_LSB, self.rd(0x1B48))
            self.cycles += 26
        self.cycles += 37
        if self.rd(A_SHOT_BLOW_CNT):
            self.from_shot_struct(PLU_SHOT_STATUS)
            return
        self.cycles += 44
        self.block_copy(0x10, 0x1B40, 0x2040)
        self.cycles += 28
        if self.rd(NUM_ALIENS) == 1:
            self.wr(SKIP_PLUNGER, 1)
            self.cycles += 20
        self.wr16(PLU_SHOT_C_FIR_LSB, self.rd16(A_SHOT_C_FIR_LSB))  # 067E
        self.cycles += 52

    def game_obj4(self, hl: int) -> None:  # GameObj4 0682
        """The saucer when it is due and no squiggly shot is out, otherwise the squiggly shot.

        In: HL (2054, unused). Out: nothing."""
        self.cycles += 30
        if self.rd(SHOT_SYNC) != 0x02:
            self.cycles += 11
            return
        self.cycles += 36
        if not self.rd(SAUCER_START):
            self.sub_050f()
            return
        self.cycles += 27
        if self.rd(SQU_SHOT_STEP_CNT):
            self.sub_050f()
            return
        self.cycles += 26
        if not self.rd(SAUCER_ACTIVE):
            self.cycles += 30
            if self.rd(NUM_ALIENS) < 0x08:
                self.sub_050f()
                return
            self.wr(SAUCER_ACTIVE, 1)
            self.cycles += 27
            self.sub_073c()
        self.cycles += 27
        if not self.comp_y_to_beam(SAUCER_PRI_PIC_MSB):
            self.cycles += 11
            return
        self.cycles += 36
        if self.rd(SAUCER_HIT):
            self.sub_06d6()
            return
        self.wr(SAUCER_PRI_PIC_MSB, self.rd(SAUCER_PRI_PIC_MSB) + self.rd(SAUCER_DELTA_Y))
        self.cycles += 64
        self.sub_073c()
        a = self.rd(SAUCER_PRI_PIC_MSB)
        self.cycles += 34
        if a < 0x28:
            self.sub_06f9()
            return
        self.cycles += 17
        if a >= 0xE1:
            self.sub_06f9()
            return
        self.cycles += 10

    def sub_050f(self) -> None:  # 050F
        """The squiggly shot, fired down the columns of ColFireTable 1D06-1D14. In, out: nothing."""
        self.cycles += 34
        self.to_shot_struct(0xDB, SQU_SHOT_STATUS)
        self.wr(OTHER_SHOT1, self.rd(PLU_SHOT_STEP_CNT))
        self.wr(OTHER_SHOT2, self.rd(ROL_SHOT_STEP_CNT))
        self.cycles += 69
        self.handle_alien_shot()
        self.cycles += 30
        if self.rd(A_SHOT_C_FIR_LSB) >= 0x15:
            self.wr(A_SHOT_C_FIR_LSB, self.rd(0x1B58))
            self.cycles += 26
        self.cycles += 37
        if self.rd(A_SHOT_BLOW_CNT):
            self.from_shot_struct(SQU_SHOT_STATUS)
            return
        self.cycles += 44
        self.block_copy(0x10, 0x1B50, 0x2050)
        self.wr16(SQU_SHOT_C_FIR_LSB, self.rd16(A_SHOT_C_FIR_LSB))
        self.cycles += 42

    def to_shot_struct(self, a: int, de: int) -> None:  # ToShotStruct 0550
        """Copy a shot's 11 bytes from DE into the active-shot structure 2073; A is its last picture's LSB.

        In: A, DE. Out: nothing."""
        self.wr(SHOT_PIC_END, a)
        self.cycles += 40
        self.block_copy(0x0B, de, A_SHOT_STATUS)

    def from_shot_struct(self, hl: int) -> None:  # FromShotStruct 055B
        """Copy the active-shot structure back to the shot at HL. In: HL. Out: nothing."""
        self.cycles += 27
        self.block_copy(0x0B, A_SHOT_STATUS, hl)

    def handle_alien_shot(self) -> None:  # HandleAlienShot 0563
        """Move the active shot, or start it if alien fire is on and the other shots are far enough along.

        In, out: nothing."""
        self.cycles += 34
        if self.rd(A_SHOT_STATUS) & 0x80:
            self.sub_05c1()
            return
        a = self.rd(ENABLE_ALIEN_FIRE)
        self.cycles += 43
        if self.rd(ISR_SPLASH_TASK) == 0x04:
            self.sub_05b7()
            return
        self.cycles += 4
        if not a:
            self.cycles += 11
            return
        self.cycles += 10
        self.wr(A_SHOT_STEP_CNT, 0)
        self.cycles += 10
        for other in (OTHER_SHOT1, OTHER_SHOT2):
            b = self.rd(other)
            self.cycles += 27
            if b:
                self.cycles += 22
                if self.rd(A_SHOT_RELOAD_RATE) >= b:
                    self.cycles += 11
                    return
                self.cycles += 5
        self.cycles += 26
        if not self.rd(A_SHOT_TRACK):
            self.sub_061b()
            return
        hl = self.rd16(A_SHOT_C_FIR_LSB)
        c = self.rd(hl)
        self.wr16(A_SHOT_C_FIR_LSB, hl + 1)
        self.cycles += 48
        self.sub_05a5(c)

    def sub_05a5(self, c: int) -> None:  # 05A5
        """Fire from the lowest live alien of column C (from 1), if there is one. In: C. Out: nothing."""
        self.cycles += 17
        found, hl = self.find_in_column(c)
        if not found:
            self.cycles += 11
            return
        self.cycles += 22
        c, _, yr = self.get_alien_coords(hl & 0xFF)
        self.wr16(ALIEN_SHOT_YR, (((c + 0x07) & 0xFF) << 8) | ((yr - 0x0A) & 0xFF))
        self.cycles += 50
        self.sub_05b7()

    def sub_05b7(self) -> None:  # 05B7
        """Mark the active shot as running with one step made. In, out: nothing."""
        self.wr(A_SHOT_STATUS, self.rd(A_SHOT_STATUS) | 0x80)
        self.wr(A_SHOT_STEP_CNT, self.rd(A_SHOT_STEP_CNT) + 1)
        self.cycles += 56

    def sub_05c1(self) -> None:  # 05C1
        """Step the active shot down (in the interrupt matching its half), animate it and handle what it hit.

        In, out: nothing."""
        self.cycles += 27
        if not self.comp_y_to_beam(ALIEN_SHOT_XR):
            self.cycles += 11
            return
        self.cycles += 34
        if self.rd(A_SHOT_STATUS) & 0x01:  # the ROM reaches 2073 by INC HL from CompYToBeam's 2072
            self.shot_blowing_up()
            return
        self.wr(A_SHOT_STEP_CNT, self.rd(A_SHOT_STEP_CNT) + 1)
        self.cycles += 32
        self.sub_0675()
        a = (self.rd(A_SHOT_IMAGE_LSB) + 0x03) & 0xFF
        self.cycles += 47
        if a >= self.rd(SHOT_PIC_END):
            a = (a - 0x0C) & 0xFF
            self.cycles += 7
        self.wr(A_SHOT_IMAGE_LSB, a)
        self.wr(ALIEN_SHOT_YR, self.rd(ALIEN_SHOT_DELTA) + self.rd(ALIEN_SHOT_YR))
        self.cycles += 78
        self.sub_066c()
        a = self.rd(ALIEN_SHOT_YR)
        self.cycles += 30
        if a < 0x15:
            self.sub_0612()
            return
        self.cycles += 17
        if not self.rd(COLLISION):
            self.cycles += 11
            return
        self.cycles += 35
        if a < 0x1E:
            self.sub_0612()
            return
        self.cycles += 21
        if a >= 0x27:
            self.sub_0612()
            return
        self.wr(PLAYER_ALIVE, 0)
        self.cycles += 17
        self.sub_0612()

    def sub_0612(self) -> None:  # 0612
        """Start the active shot's blow-up. In, out: nothing."""
        self.wr(A_SHOT_STATUS, self.rd(A_SHOT_STATUS) | 0x01)
        self.cycles += 43

    def sub_061b(self) -> None:  # 061B
        """Aim at the player: fire from the column above it (11 at most). In, out: nothing."""
        self.cycles += 42
        c, _ = self.find_column((self.rd(PLAYER_XR) + 0x08) & 0xFF)
        self.cycles += 22
        if c >= 0x0C:
            c = 0x0B
            self.cycles += 17
        self.sub_05a5(c)

    def find_in_column(self, c: int) -> tuple[bool, int]:  # FindInColumn 062F
        """Find the lowest live alien in column C (from 1). In: C. Out: carry (found), HL (its flag)."""
        h = self.rd(PLAYER_DATA_MSB) << 8
        lo = (c - 1) & 0xFF
        cy = False
        self.cycles += 35
        for _ in range(5):
            if self.rd(h | lo):
                self.cycles += 26
                return True, h | lo
            self.cycles += 52
            lo += 0x0B
            cy = lo > 0xFF
            lo &= 0xFF
        self.cycles += 10
        return cy, h | lo

    def shot_blowing_up(self) -> None:  # ShotBlowingUp 0644
        """Time the active shot's explosion: draw it at the first tick, erase it at the last. In, out: nothing."""
        t = (self.rd(A_SHOT_BLOW_CNT) - 1) & 0xFF
        self.wr(A_SHOT_BLOW_CNT, t)
        self.cycles += 44
        if t == 0x03:
            self.cycles += 17
            self.sub_0675()
            self.wr16(A_SHOT_IMAGE_LSB, 0x1CDC)
            self.wr(ALIEN_SHOT_XR, self.rd(ALIEN_SHOT_XR) - 2)
            self.wr(ALIEN_SHOT_YR, self.rd(ALIEN_SHOT_YR) - 2)
            self.wr(ALIEN_SHOT_SIZE, 0x06)
            self.cycles += 111
            self.sub_066c()
            return
        if t == 0:
            self.cycles += 19
            self.sub_0675()
        else:
            self.cycles += 15

    def sub_066c(self) -> None:  # 066C
        """Draw the active shot with collision detection. In, out: nothing."""
        self.cycles += 27
        b, de, hl = self.read_desc(A_SHOT_IMAGE_LSB)
        self.cycles += 10
        self.draw_spr_collision(b, de, hl)

    def sub_0675(self) -> None:  # 0675
        """Erase the active shot. In, out: nothing."""
        self.cycles += 27
        b, de, hl = self.read_desc(A_SHOT_IMAGE_LSB)
        self.cycles += 10
        self.erase_shifted(b, de, hl)

    # ---- the saucer ----

    def sub_06d6(self) -> None:  # 06D6
        """The hit saucer: explosion, then its score, then removal, on the show-hit timer. In, out: nothing."""
        self.cycles += 24
        self.sound_bits3_off(0xFE)
        t = (self.rd(SAUCER_HIT_TIME) - 1) & 0xFF
        self.wr(SAUCER_HIT_TIME, t)
        self.cycles += 39
        if t == 0x1F:
            self.sub_074b()
            return
        self.cycles += 17
        if t == 0x18:
            self.sub_070c()
            return
        self.cycles += 4
        if t:
            self.cycles += 11
            return
        a = self.rd(SOUND_PORT5) & 0xEF
        self.wr(SOUND_PORT5, a)
        self.out(SOUND2, a & 0x20)
        self.cycles += 69
        self.sub_06f9()

    def sub_06f9(self) -> None:  # 06F9
        """Remove the saucer from the screen and reset its structure. In, out: nothing."""
        self.cycles += 17
        b, _, hl = self.sub_0742()
        self.cycles += 17
        self.clear_small_sprite(b, hl)
        self.cycles += 54
        self.block_copy(0x0A, 0x1B83, 0x2083)  # 075F
        self.sub_0707()

    def sub_0707(self) -> None:  # 0707
        """Turn the saucer sound off. In, out: nothing."""
        self._tick(7, 10)  # LD B,FE; JP 19DC
        self.sound_bits3_off(0xFE)

    def sub_070c(self) -> None:  # 070C
        """Score the saucer by the shot-count table and print its value where it was. In, out: nothing."""
        self.wr(ADJUST_SCORE, 1)
        b = self.rd(self.rd16(SAU_SCORE_LSB))
        hl, de = 0x1D50, 0x1D4C
        self.cycles += 70
        for _ in range(4):
            self.cycles += 21
            if self.rd(de) == b:
                break
            self.cycles += 25
            hl += 1
            de += 1
        self.wr(SAUCER_PRI_LOC_LSB, self.rd(hl))
        self.wr16(SCORE_DELTA_LSB, b << 4)
        self.kills += 1
        self.hits += 1
        self.cycles += 105
        _, de, hl = self.sub_0742()
        self.cycles += 10
        self.sub_08f1(de, hl)

    def sub_073c(self) -> None:  # 073C
        """Draw the saucer (or what its descriptor holds). In, out: nothing."""
        self.cycles += 17
        b, de, hl = self.sub_0742()
        self.cycles += 10
        self.draw_simp_sprite(b, de, hl)

    def sub_0742(self) -> tuple[int, int, int]:  # 0742
        """Read the saucer's descriptor at 2087, with its position as a screen address. In: nothing. Out: B, DE, HL."""
        self.cycles += 27
        b, de, hl = self.read_desc(SAUCER_PRI_LOC_LSB)
        self.cycles += 10
        return b, de, self.conv_to_scr(hl)

    def sub_074b(self) -> None:  # 074B
        """Start the saucer-hit sound and draw the saucer's explosion. In, out: nothing."""
        a = self.rd(SOUND_PORT5) | 0x10
        self.wr(SOUND_PORT5, a)
        self.cycles += 52
        self.sub_1770(a)
        self.wr16(SAUCER_PRI_LOC_LSB, 0x1D7C)
        self.cycles += 36
        self.sub_073c()

    def sub_176d(self) -> None:  # 176D
        """Write soundPort5 to port 5 without the march notes. In, out: nothing."""
        self.cycles += 13
        self.sub_1770(self.rd(SOUND_PORT5))

    def sub_1770(self, a: int) -> None:  # 1770
        """Write A to port 5 without the march notes. In: A. Out: nothing."""
        self.out(SOUND2, a & 0x30)
        self.cycles += 27
