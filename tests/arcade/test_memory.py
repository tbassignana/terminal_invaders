"""The arcade memory model: the bus, the hardware shifter, sprite drawing with collision, DrawChar's text layer, BCD
printing, the shield and ship routines, and the program image's overlap checks.

The image here is TABLES (the ROM's tables) plus small made-up pictures placed where the routines read them; the ROM
image file is never read."""

import pytest

from terminal_invaders.arcade import memory as mem
from terminal_invaders.arcade.image import build_image
from terminal_invaders.arcade.memory import Memory
from terminal_invaders.arcade.tables import TABLES

SPRITE = 0x0100  # a made-up 3-row picture for the shifted-sprite routines
SPRITE_BYTES = bytes((0b10000001, 0b11111111, 0b00111100))
SHIP = bytes(range(0x10, 0x20))  # 16 rows at 1C60, the player picture DrawNumShips copies
SHIELD = bytes((i * 7 + 3) & 0xFF for i in range(44))  # at 1D20


def glyph(code):
    """A distinct made-up 8-byte glyph for each character code."""
    return bytes(((code * 37 + row * 11) & 0xFF) | 1 for row in range(8))


def synthetic_image():
    rom = bytearray(build_image(art={}))
    for code in (*range(0x29), 0x38, 0x3F):  # the slots the game prints; the others hold message strings
        rom[0x1E00 + code * 8 : 0x1E08 + code * 8] = glyph(code)
    rom[SPRITE : SPRITE + len(SPRITE_BYTES)] = SPRITE_BYTES
    rom[0x1C60:0x1C70] = SHIP
    rom[0x1D20:0x1D4C] = SHIELD
    return bytes(rom)


IMAGE = synthetic_image()


class Board(Memory):
    def __init__(self, rom=IMAGE):
        self.init_memory(rom)
        self.dirty = set()


def pixel(board, xr, yr):
    return board.rd(0x2000 + xr * 32 + yr // 8) >> (yr % 8) & 1


def lit(board):
    return {(xr, yr) for xr in range(32, 256) for yr in range(256) if pixel(board, xr, yr)}


def sprite_pixels(xr, yr, data=SPRITE_BYTES):
    return {(xr + row, yr + bit) for row, byte in enumerate(data) for bit in range(8) if byte >> bit & 1}


# ---- bus ----


def test_memory_starts_zeroed_and_rejects_a_short_rom():
    board = Board()
    assert board.ram == bytearray(0x400) and board.vram == bytearray(0x1C00)
    assert board.text == {} and board.port_writes == [] and (board.sound_port3, board.sound_port5) == (0, 0)
    with pytest.raises(ValueError):
        Board(bytes(100))


def test_bus_maps_rom_ram_and_vram_and_drops_rom_writes():
    board = Board()
    board.wr(0x2005, 0x1AB)  # bytes wrap
    board.wr(0x2400, 7)
    board.wr(0x3FFF, 9)
    board.wr(0x1E00, 0xEE)
    assert board.ram[5] == 0xAB and board.vram[0] == 7 and board.vram[-1] == 9
    assert board.rd(0x1E00) == IMAGE[0x1E00] and board.rd(0x0100) == SPRITE_BYTES[0]
    assert board.rd(0x6005) == 0xAB  # the board decodes 14 address lines
    board.wr16(0x20F2, 0x1234)
    assert (board.rd(0x20F2), board.rd(0x20F3), board.rd16(0x20F2)) == (0x34, 0x12, 0x1234)


def test_vram_writes_mark_their_display_column_dirty():
    board = Board()
    board.wr(0x2400 + 5 * 32 + 31, 1)
    board.wr(0x2000 + 255 * 32, 1)
    board.wr(0x2050, 1)  # RAM
    assert board.dirty == {5, 223}
    board.dirty = None
    board.wr(0x2400, 1)
    assert board.dirty is None


# ---- the shifter and the other ports ----


def test_shifter_shifts_bytes_in_from_the_top_and_reads_through_the_offset():
    board = Board()
    board.out(mem.SHFT_DATA, 0xAA)
    assert board.shift_reg == 0xAA00
    board.out(mem.SHFT_DATA, 0xFF)
    assert board.shift_reg == 0xFFAA
    expected = {0: 0xFF, 2: 0xFE, 7: 0xD5}  # Hardware.md: xxxxxxxx, xxxxxxyy, xyyyyyyy
    for amount, value in expected.items():
        board.out(mem.SHFTAMNT, amount | 0xF8)  # only 3 bits count
        assert board.shift_amount == amount and board.inp(mem.SHFT_IN) == value
    board.out(mem.SHFT_DATA, 0x12)
    assert board.shift_reg == 0x12FF


def test_sound_ports_latch_and_record_and_the_watchdog_is_ignored():
    board = Board()
    board.out(mem.SOUND1, 0x22)
    board.out(mem.WATCHDOG, 0x55)
    board.out(mem.SOUND2, 0x101)
    assert (board.sound_port3, board.sound_port5) == (0x22, 0x01)
    assert board.port_writes == [(3, 0x22), (5, 0x01)]
    board.wr(mem.SOUND_PORT3, 0x20)
    board.sound_bits3_on(0x02)
    board.sound_bits3_off(0xDF)
    assert board.rd(mem.SOUND_PORT3) == 0x02 and board.port_writes[2:] == [(3, 0x22), (3, 0x02)]
    with pytest.raises(ValueError):
        board.inp(mem.INP1)


# ---- coordinates and sprites ----


@pytest.mark.parametrize("xr, yr", [(32, 0), (0x48, 0x2B), (0xFF, 0xFF), (0x80, 0x07)])
def test_pixel_numbers_convert_to_screen_bytes_and_a_shift_amount(xr, yr):
    board = Board()
    assert board.conv_to_scr((xr << 8) | yr) == 0x2000 + xr * 32 + yr // 8
    assert board.cnvt_pix_number((xr << 8) | yr) == 0x2000 + xr * 32 + yr // 8
    assert board.shift_amount == yr % 8


@pytest.mark.parametrize("yr", [0x20, 0x23, 0x27])
def test_draw_sprite_places_every_pixel_at_its_coordinates(yr):
    board = Board()
    board.vram[:] = b"\xff" * len(board.vram)
    board.draw_sprite(3, SPRITE, (0x60 << 8) | yr)  # overwrites both bytes of each row
    on = {(x, y) for (x, y) in sprite_pixels(0x60, yr)}
    rows = {(x, y) for x in range(0x60, 0x63) for y in range(yr & ~7, (yr & ~7) + 16)}
    assert {p for p in rows if pixel(board, *p)} == on
    assert board.dirty == {0x60 - 32, 0x61 - 32, 0x62 - 32}


def test_shifted_draw_ors_and_erase_removes_only_the_sprite():
    board = Board()
    other = (0x61, 0x25)
    board.wr(0x2000 + 0x61 * 32 + 4, 1 << 5)  # pixel (61, 25), inside the sprite's bytes but not under it
    board.draw_shifted_sprite(3, SPRITE, 0x6026)
    assert lit(board) == sprite_pixels(0x60, 0x26) | {other}
    board.erase_shifted(3, SPRITE, 0x6026)
    assert lit(board) == {other}


@pytest.mark.parametrize("yr", [0x20, 0x26])
def test_erase_shifted_clears_exactly_the_sprite_pixels(yr):
    board = Board()
    board.vram[:] = b"\xff" * len(board.vram)
    board.erase_shifted(3, SPRITE, 0x6000 | yr)
    area = {(x, y) for x in range(0x60, 0x63) for y in range(32, 48)}
    assert {p for p in area if not pixel(board, *p)} == sprite_pixels(0x60, yr)


def test_collision_is_flagged_only_when_a_lit_pixel_overlaps():
    board = Board()
    board.draw_spr_collision(3, SPRITE, 0x5010)
    assert board.rd(mem.COLLISION) == 0
    board.draw_spr_collision(3, SPRITE, 0x5310)  # three columns further: touching nothing
    assert board.rd(mem.COLLISION) == 0
    board.draw_spr_collision(1, SPRITE, 0x5214)  # its bit 0 lands on (52, 14), lit by the first sprite's row 2
    assert board.rd(mem.COLLISION) == 1
    assert lit(board) == sprite_pixels(0x50, 0x10) | sprite_pixels(0x53, 0x10) | {(0x52, 0x14), (0x52, 0x1B)}
    board.draw_spr_collision(1, SPRITE, 0x7000)
    assert board.rd(mem.COLLISION) == 0  # cleared on every call
    board.wr(0x2000 + 0x74 * 32 + 3, 1 << 5)  # (74, 1D)
    board.draw_spr_collision(1, SPRITE, 0x7416)  # bit 7 lands on it, in the second byte of the shifted row
    assert board.rd(mem.COLLISION) == 1


def test_simple_sprites_draw_unshifted_and_erase_two_bytes_per_row():
    board = Board()
    assert board.draw_simp_sprite(3, SPRITE, 0x2E01) == 0x2E61
    assert [board.rd(0x2E01 + 0x20 * k) for k in range(3)] == list(SPRITE_BYTES)
    board.wr(0x2E02, 0xFF)
    board.wr(0x2E03, 0xFF)
    board.erase_simple_sprite(2, 0x7008)  # pixel position of 2E01
    assert (board.rd(0x2E01), board.rd(0x2E02), board.rd(0x2E21), board.rd(0x2E03)) == (0, 0, 0, 0xFF)
    assert board.rd(0x2E41) == SPRITE_BYTES[2]
    assert board.clear_small_sprite(3, 0x2E41) == 0x2EA1 and board.rd(0x2E41) == 0


def test_sprites_running_off_the_top_of_vram_wrap_like_the_board():
    board = Board()
    board.draw_simp_sprite(3, SPRITE, 0x3FE1)  # rows 2 and 3 land in ROM space and are lost
    assert board.rd(0x3FE1) == SPRITE_BYTES[0] and board.rd(0x0001) == IMAGE[1]


# ---- characters, the text layer and numbers ----


def test_draw_char_draws_the_font_glyph_and_records_the_text_layer():
    board = Board()
    assert board.draw_char(0x07, 0x3501) == 0x3601
    assert bytes(board.rd(0x3501 + 0x20 * row) for row in range(8)) == glyph(0x07)
    assert board.text == {0x3501: 0x07}
    board.draw_char(0x26, 0x3501)
    assert board.text == {0x3501: 0x26}


def test_print_message_prints_each_character_one_cell_apart():
    board = Board()
    board.draw_score_head()  # " SCORE<1> HI-SCORE SCORE<2> " at 241E
    message = TABLES[0x1AE4]
    assert board.text == {0x241E + 0x100 * k: code for k, code in enumerate(message)}
    board.clear_play_field()  # clears pixels below the score rows only; the text layer is never edited
    assert len(board.text) == 28 and board.rd(0x241E) == glyph(message[0])[0]


def test_a_message_length_of_0_prints_256_and_only_screen_cells_enter_the_text_layer():
    board = Board()
    board.print_message(0, 0x1B00, 0x2400)  # 256 characters 100h apart: HL wraps through all 64K
    assert sorted(board.text) == [0x2400 + 0x100 * k for k in range(0x1C)]


def test_bcd_digits_print_as_digit_characters():
    board = Board()
    board.print4_digits(0x1905, 0x271C)
    assert [board.text[0x271C + 0x100 * k] for k in range(4)] == [0x1B, 0x23, 0x1A, 0x1F]  # "1905"
    board.text.clear()
    assert board.draw_hex_byte(0xA7, 0x2601) == 0x2801
    assert board.text == {0x2601: 0x24, 0x2701: 0x21}  # hex digit A runs on past 9


def test_scores_print_from_their_descriptors():
    board = Board()
    board.copy_ram_mirror()
    board.sub_01e6(0)  # the power-on copy: score descriptors at 20F4-20FF
    board.wr16(mem.P1_SCOR_L, 0x0150)
    board.wr16(mem.HI_SCOR_L, 0x9990)
    board.wr(mem.NUM_COINS, 0x01)
    board.draw_status()
    p1, hi, p2 = board.rd16(mem.P1_SCOR_LO_L), board.rd16(mem.HI_SCOR_LO_L), board.rd16(mem.P2_SCOR_LO_L)
    assert [board.text[p1 + 0x100 * k] for k in range(4)] == [0x1A, 0x1B, 0x1F, 0x1A]
    assert [board.text[hi + 0x100 * k] for k in range(4)] == [0x23, 0x23, 0x23, 0x1A]
    assert [board.text[p2 + 0x100 * k] for k in range(4)] == [0x1A] * 4
    assert [board.text[0x3C01], board.text[0x3D01]] == [0x1A, 0x1B]
    assert [board.text[0x3501 + 0x100 * k] for k in range(7)] == list(TABLES[0x1FA9])


# ---- RAM mirror, shields, ships, flags ----


def test_ram_mirror_copies_the_tables_and_leaves_the_bitmap_hole_zero():
    board = Board()
    board.ram[:] = b"\x55" * 0x400
    board.copy_ram_mirror()
    assert board.ram[:0xA0] == TABLES[0x1B00] and board.ram[0xA0:0xB0] == bytes(16)
    assert board.ram[0xB0:0xC0] == TABLES[0x1BB0] and board.ram[0xC0] == 0x55


def test_shields_go_to_the_buffer_onto_the_screen_and_back():
    board = Board()
    board.draw_shield_pl1()
    assert board.ram[0x142 : 0x142 + 176] == SHIELD * 4
    board.restore_shields1()
    for k in range(4):
        base = 0x2806 + 0x5A0 * k  # 22 rows plus the 2E0 gap
        assert bytes(board.rd(base + 0x20 * (i // 2) + i % 2) for i in range(44)) == SHIELD
    board.ram[0x142 : 0x142 + 176] = bytes(176)
    board.remember_shields1()
    assert board.ram[0x142 : 0x142 + 176] == SHIELD * 4 and board.rd(mem.TMP2081) == 1


def test_remove_ship_redraws_the_reserve_and_prints_the_count_it_had():
    board = Board()
    board.wr(mem.PLAYER_DATA_MSB, 0x21)
    board.wr(mem.P1_SHIPS_REM, 3)
    board.sub_19fa(0x2701)
    board.vram[0x3301 - 0x2400] = 0xFF  # a stale ship cell beyond the reserve is cleared
    board.remove_ship()
    assert board.rd(mem.P1_SHIPS_REM) == 2
    assert [bytes(board.rd(c + 0x20 * r) for r in range(16)) == SHIP for c in (0x2701, 0x2901, 0x2B01)] == [
        True,
        True,
        False,
    ]
    assert board.rd(0x3301) == 0 and board.text == {0x2501: 0x1A + 3}
    board.wr(mem.P1_SHIPS_REM, 0)
    board.text.clear()
    board.remove_ship()
    assert board.text == {} and board.rd(mem.P1_SHIPS_REM) == 0


def test_player_pointers_follow_player_data_msb():
    board = Board()
    for msb, data, score, alive in ((0x21, 0x2100, 0x20F8, 0x20E7), (0x22, 0x2200, 0x20FC, 0x20E8)):
        board.wr(mem.PLAYER_DATA_MSB, msb)
        board.wr(data | 0xFF, 4)
        assert (board.get_player_data_ptr(), board.sub_09ca(), board.cur_ply_alive()) == (data, score, alive)
        assert board.sub_092e() == (4, data | 0xFF)


def test_comp_y_to_beam_compares_bit_7_with_the_beam_flag():
    board = Board()
    board.wr(mem.VBLANK_STATUS, 0x80)
    board.wr(0x2029, 0x9C)
    assert board.comp_y_to_beam(0x2029) is True
    board.wr(mem.VBLANK_STATUS, 0x00)
    assert board.comp_y_to_beam(0x2029) is False


def test_block_copy_read_desc_and_game_task_flags():
    board = Board()
    assert board.block_copy(5, 0x1B10, 0x2010) == (0x1B15, 0x2015)
    assert board.ram[0x10:0x15] == TABLES[0x1B00][0x10:0x15]
    board.block_copy(4, 0x2010, 0x2011)  # overlapping copies run forward byte by byte, as the 8080 does
    assert board.ram[0x10:0x15] == bytes([TABLES[0x1B00][0x10]] * 5)
    board.ram[0x79:0x7E] = bytes((0xD0, 0x1C, 0x40, 0x88, 0x03))
    assert board.read_desc(0x2079) == (0x03, 0x1CD0, 0x8840)
    board.enable_game_tasks()
    assert board.rd(mem.SUSPEND_PLAY) == 1
    board.dsable_game_tasks()
    assert board.rd(mem.SUSPEND_PLAY) == 0


def test_clearing_routines_leave_the_borders_they_should():
    board = Board()
    board.vram[:] = b"\xff" * len(board.vram)
    board.dirty = set()
    board.clear_play_field()
    col = board.vram[0x40:0x60]
    assert col == b"\xff\xff" + bytes(0x1A) + b"\xff" * 4 and board.dirty == set(range(224))
    board.draw_bottom_line()
    assert board.vram[2::32] == b"\x01" * 224
    board.clear_screen()
    assert board.vram == bytearray(0x1C00)


def test_ram_names_follow_ramuse():
    assert (mem.REF_ALIEN_XR, mem.PLYR_SHOT_STATUS, mem.NUM_ALIENS, mem.ISR_DELAY) == (0x200A, 0x2025, 0x2082, 0x20C0)
    assert (mem.P1_SCOR_L, mem.P1_SHIPS_REM, mem.P2_REF_ALIEN_XR) == (0x20F8, 0x21FF, 0x22FD)


# ---- the program image ----


def test_build_image_places_tables_and_art_and_zeros_elsewhere():
    image = build_image(art={0x1C00: b"\x01\x02", 0x1E00: b"\x03"})
    assert len(image) == mem.ROM_SIZE
    assert all(image[addr : addr + len(data)] == data for addr, data in TABLES.items())
    assert (image[0x1C00:0x1C02], image[0x1E00], image[0x1C02], image[0x0000]) == (b"\x01\x02", 3, 0, 0)


@pytest.mark.parametrize(
    "art",
    [
        {0x1AA4: b"\x00\x00"},  # runs into ShotReloadRate's last byte and GAME OVER
        {0x1B9F: b"\x00"},  # the last byte of the RAM mirror
        {0x1C00: bytes(16), 0x1C0F: b"\x00"},  # art over art
        {0x1C00: bytes(64), 0x1C20: bytes(2)},  # contained in an earlier entry
        {0x1FFF: b"\x00\x00"},  # past the end
        {-1: b"\x00"},
    ],
)
def test_build_image_rejects_overlaps_and_overruns(art):
    with pytest.raises(ValueError):
        build_image(art=art)


def test_build_image_accepts_entries_that_only_touch():
    build_image(art={0x1BA0: bytes(16), 0x1C00: bytes(16), 0x1C10: bytes(16), 0x1FF8: bytes(8)})


def test_the_shipping_image_builds_without_overlaps():
    image = build_image()
    assert all(image[addr : addr + len(data)] == data for addr, data in TABLES.items())
