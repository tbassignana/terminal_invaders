"""ROM data the game program reads that is not a picture: the RAM mirror, lookup tables and message strings.

Each entry is the bytes at a ROM address, copied from the annotated disassembly's hex columns (except 0800, which
keeps only bit 0 of code bytes); build_image() places them in the program image at those addresses. Messages are in
the font's character codes (00-19 A-Z, 1A-23 0-9, 24 '<', 25 '>', 26 space, 3F '-'). Bitmaps (sprites, shields, the
font) are art.py's; attract-only data is left out.
"""

# Saucer start sides: bit 0 of each byte at 0800-08FF, which EndOfBlowup 0462 reads through the shot-count pointer
# 208F/2090 (0800 + shots ended, low byte only). The bytes there are program code; only the derived bit is kept here,
# as 1 (L: enter at Xr 29 moving right) or 0 (R: enter at Xr E0 moving left).
SAUCER_SIDES = (
    "LRLRLLLRLRLRRRLLRLLLLLRLLLRRLRRLRRLRLLLLLRLRRRLRLLLRLLLLLRRLRLLL"
    "RRLRRRLRRLLLLRLRRLLRRRRLRRLLRRRLLLLRLRLRRLLLLRLLLLLRRLRRRRRLRLRL"
    "LRRRRRRLRLRRLLLLLRLRRLLRRLRLRRLLLRLRRRRRRRRRLRRRRRRLRLLLLLLRRRLR"
    "LRLRLRLRLRLLLRLLRLRRLRLLRRRRLRRLRRRLRRRLRLRLRRLLRRLRLLLRLLLRLRLL"
)

TABLES: dict[int, bytes] = {
    # bit 0 of each byte at 0800-08FF (code), read by EndOfBlowup 0462 as the saucer's start side
    0x0800: bytes(side == "L" for side in SAUCER_SIDES),
    # Alien delay lists 1A11/1A21: alien counts, then fleet-sound delays (FleetDelayExShip 1775)
    0x1A11: bytes.fromhex(
        "32 2B 24 1C 16 11 0D 0A 08 07 06 05 04 03 02 01 34 2E 27 22 1C 18 15 13 10 0E 0D 0C 0B 09 07 05 FF"
    ),
    # ShotReloadRate 1AA1: alien reload rates by score band (AShotReloadRate 170E)
    0x1AA1: bytes.fromhex("30 10 0B 08 07"),
    # MessageGOver 1AA6: "GAME OVER PLAYER< >"
    0x1AA6: bytes.fromhex("06 00 0C 04 26 0E 15 04 11 26 26 0F 0B 00 18 04 11 24 26 25"),
    # Message1Only 1ACF: "ONLY 1PLAYER  BUTTON "
    0x1ACF: bytes.fromhex("0E 0D 0B 18 26 1B 0F 0B 00 18 04 11 26 26 01 14 13 13 0E 0D 26"),
    # MessageScore 1AE4: " SCORE<1> HI-SCORE SCORE<2> "
    0x1AE4: bytes.fromhex("26 12 02 0E 11 04 24 1B 25 26 07 08 3F 12 02 0E 11 04 26 12 02 0E 11 04 24 1C 25 26"),
    # RAM mirror 1B00-1B9F (CopyRAMMirror 01E4): the game objects, MesssageP1 "PLAY PLAYER<1>" at 1B70, then the
    # saucer and sound state from 1B80
    0x1B00: bytes.fromhex(
        "01 00 00 10 00 00 00 00 02 78 38 78 38 00 F8 00 00 80 00 8E 02 FF 05 0C 60 1C 20 30 10 01 00 00 "
        "00 00 00 BB 03 00 10 90 1C 28 30 01 04 00 FF FF 00 00 02 76 04 00 00 00 00 00 04 EE 1C 00 00 03 "
        "00 00 00 B6 04 00 00 01 00 1D 04 E2 1C 00 00 03 00 00 00 82 06 00 00 01 06 1D 04 D0 1C 00 00 03 "
        "FF 00 C0 1C 00 00 10 21 01 00 30 00 12 00 00 00 0F 0B 00 18 26 0F 0B 00 18 04 11 24 1B 25 FC 00 "
        "01 FF FF 00 00 00 20 64 1D D0 29 18 02 54 1D 00 08 00 06 00 00 01 40 00 01 00 00 10 9E 00 20 1C"
    ),
    # RAM mirror 1BB0-1BBF, the end of CopyRAMMirror's C0 bytes (1BA0-1BAF is a splash bitmap, left out)
    0x1BB0: bytes.fromhex("00 00 01 B8 98 A0 1B 10 FF 00 A0 1B 00 00 00 00"),
    # power-on copy 18D9 (256 bytes from 1B00): 1BC0-1BCF to 20C0 (isrDelay ... aShotReloadRate)
    0x1BC0: bytes.fromhex("00 10 00 0E 05 00 00 00 00 00 07 D0 1C C8 9B 03"),
    # power-on copy 18D9: 1BE0-1BFF to 20E0 (suspendPlay ... score descriptors; 1BD0-1BDF is a splash bitmap, left out)
    0x1BE0: bytes.fromhex(
        "00 00 00 00 00 00 00 00 00 01 00 00 01 74 1F 00 80 00 00 00 00 00 1C 2F 00 00 1C 27 00 00 1C 39"
    ),
    # AReloadScoreTab 1CB8: score MSB bands for ShotReloadRate
    0x1CB8: bytes.fromhex("02 10 20 30"),
    # ColFireTable 1D00: shot columns (plunger 00-0F, squiggly 06-14) and the unused tail to 1D1F
    0x1D00: bytes.fromhex(
        "01 07 01 01 01 04 0B 01 06 03 01 01 0B 09 02 08 02 0B 04 07 0A 05 02 05 04 06 07 08 0A 06 0A 03"
    ),
    # 1D4C: possible saucer scores (BCD tens)
    0x1D4C: bytes.fromhex("05 10 15 30"),
    # 1D50: LSB of each score's string at 1D94
    0x1D50: bytes.fromhex("94 97 9A 9D"),
    # SaucerScrTab 1D54: saucer score per shot count (sauScore pointer, wraps at 1D63)
    0x1D54: bytes.fromhex("10 05 05 10 15 10 10 05 30 10 10 10 05 15 10 05"),
    # SaucSoreStr 1D94: " 50", "100", "150", "300"
    0x1D94: bytes.fromhex("26 1F 1A 1B 1A 1A 1B 1F 1A 1D 1A 1A"),
    # AlienScores 1DA0: 10, 20, 30 points (BCD)
    0x1DA0: bytes.fromhex("10 20 30"),
    # AlienStartTable 1DA3: rack start Yr for rounds 2-9
    0x1DA3: bytes.fromhex("60 50 48 48 48 40 40 40"),
    # MessageCredit 1FA9: "CREDIT "
    0x1FA9: bytes.fromhex("02 11 04 03 08 13 26"),
    # MessagePush 1FF3: "PUSH " (the wait screen prints 4)
    0x1FF3: bytes.fromhex("0F 14 12 07 26"),
}
