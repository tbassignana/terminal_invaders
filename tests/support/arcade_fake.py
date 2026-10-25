"""FakeArcade: the ArcadeGame members the view and Session read, over a scripted screen, for view and UI tests that
need an Arcade game without the ported program.

It draws in the 1978 program's memory layout: VRAM bytes at (Xr - 32) * 32 + Yr // 8, the score header and scores
printed through a DrawChar of its own (so game.text holds them and their VRAM bytes equal glyphs of a fake font in
its ROM image), a full rack of 55 invaders that steps one invader a frame, four shields, a cannon that moves and
fires, and a ground line. It ends its game after `length` frames.
"""

import hashlib
from types import SimpleNamespace

from terminal_invaders.model import Config

RULESET = "arcade-1978-1"
FONT = 0x1E00
# The character codes the program prints, for the fake font; the view takes the shipped map, art.CHARSET.
FAKE_CHARSET = {code: chr(ord("A") + code) for code in range(26)} | {0x1A + n: str(n) for n in range(10)}
FAKE_CHARSET |= {0x24: "<", 0x25: ">", 0x26: " ", 0x27: "=", 0x28: "*", 0x38: "?", 0x3F: "-"}
CODES = {char: code for code, char in FAKE_CHARSET.items()}
HEADER, P1_SCORE, HI_SCORE, P2_SCORE = 0x241E, 0x271C, 0x2F1C, 0x391C  # where the program prints them
SHIPS, CREDIT = 0x2501, 0x3501
INVADER = bytes((0x18, 0x3C, 0x66, 0xFF, 0xDB, 0x3E, 0x3E, 0xDB, 0xFF, 0x66, 0x3C, 0x18))  # 12 columns, 8 rows
CANNON = bytes((0x70, 0x70, 0x78, 0x7C, 0x7C, 0x7E, 0x7F, 0x7F, 0x7E, 0x7C, 0x7C, 0x78, 0x70, 0x70))
SHIELD_XR = (0x40, 0x6D, 0x9A, 0xC7)  # 22 columns each, Yr 48 to 63


def glyph(code: int) -> bytes:
    """The fake font's 8 column bytes for a code: blank for the space, otherwise distinct and never blank."""
    return bytes(8) if code == 0x26 else bytes((code * 37 + n * 11) % 254 + 1 for n in range(8))


def fake_rom() -> bytes:
    """An 8 KB image that holds only the fake font."""
    rom = bytearray(0x2000)
    for code in FAKE_CHARSET:
        rom[FONT + 8 * code : FONT + 8 * code + 8] = glyph(code)
    return bytes(rom)


ROM = fake_rom()


class FakeArcade:
    """A deterministic stand-in for ArcadeGame: same members, a screen that changes every frame, a game over."""

    length = 3600  # frames until the game is over

    def __init__(self, config: Config = Config(mode="arcade"), *, rom=None):
        if config.mode != "arcade":
            raise ValueError("FakeArcade plays the arcade mode")
        self.config, self.rom = config, ROM if rom is None else rom
        self.ram, self.vram, self.text = bytearray(0x400), bytearray(0x1C00), {}
        self.dirty: set[int] | None = None
        self.tick, self.phase, self.events = 0, "playing", []
        self.player = SimpleNamespace(lives=3)
        self.score = self.score_wraps = self.kills = self.shots_fired = self.hits = 0
        self.wave, self.hi_score_addr, self.sound_ports, self.trail = 1, HI_SCORE, (0, 0), None
        self.cannon, self.shot, self.ref, self.direction, self.cursor = 80, None, 0x30, 2, 0
        self.print(HEADER, " SCORE<1> HI-SCORE SCORE<2> ")
        for addr in (P1_SCORE, HI_SCORE, P2_SCORE):
            self.print(addr, "0000")
        self.print(SHIPS, "3")
        self.print(CREDIT, "CREDIT 01")
        for x in range(224):
            self.vram[32 * x + 2] = 0x01  # the ground line at Yr 16
        for xr in SHIELD_XR:
            for x in range(xr - 32, xr - 10):
                self.vram[32 * x + 6 : 32 * x + 8] = b"\xff\xff"
        for n in range(55):
            self.sprite(*self.invader(n), INVADER)
        self.sprite(self.cannon, 3, CANNON)

    def print(self, addr: int, message: str) -> None:
        """DrawChar for each character: its glyph down the 8 columns from addr, and the text layer's entry."""
        for char in message:
            code = CODES[char]
            for n, byte in enumerate(glyph(code)):
                self.poke(addr + 32 * n, byte)
            self.text[addr] = code
            addr += 0x100

    def poke(self, addr: int, value: int) -> None:
        index = addr - 0x2400
        self.vram[index] = value
        if self.dirty is not None:
            self.dirty.add(index // 32)

    def sprite(self, xr: int, row: int, image: bytes, erase: bool = False) -> None:
        """A byte-aligned sprite: image column n at Xr xr + n, VRAM byte row `row` (Yr 8 * row to 8 * row + 7)."""
        for n, byte in enumerate(image):
            self.poke(0x2000 + 32 * (xr + n) + row, 0 if erase else byte)

    def invader(self, n: int) -> tuple[int, int]:
        """Invader n's Xr and byte row: 11 to a row, 16 apart, rows 2 bytes apart from Yr 120 up."""
        return self.ref + 16 * (n % 11), 15 + 2 * (n // 11)

    def plot(self, xr: int, yr: int, on: bool) -> None:
        index = (xr - 32) * 32 + yr // 8
        value = self.vram[index] | 1 << yr % 8 if on else self.vram[index] & ~(1 << yr % 8)
        self.poke(0x2400 + index, value)

    def step(self, actions=()) -> None:
        if self.phase == "game_over":
            return
        self.events = []
        actions = set(actions)
        move = ("right" in actions) - ("left" in actions)
        if move and 32 <= self.cannon + move <= 255 - len(CANNON):
            self.sprite(self.cannon, 3, CANNON, erase=True)
            self.cannon += move
            self.sprite(self.cannon, 3, CANNON)
        if "fire" in actions and self.shot is None:
            self.shot, self.shots_fired = [self.cannon + 7, 32], self.shots_fired + 1
            self.events.append("fire")
        if self.shot:
            xr, yr = self.shot
            for y in range(yr, yr + 4):
                self.plot(xr, y, False)
            self.shot[1] += 4
            if self.shot[1] >= 216:
                self.shot = None
                self.kills, self.hits, self.score = self.kills + 1, self.hits + 1, self.score + 10
                self.print(P1_SCORE, "%04d" % (self.score % 10000))
                self.events.append("kill")
            else:
                for y in range(self.shot[1], self.shot[1] + 4):
                    self.plot(xr, y, True)
        xr, row = self.invader(self.cursor)
        self.sprite(xr, row, INVADER, erase=True)
        self.sprite(xr + self.direction, row, INVADER)
        self.cursor += 1
        if self.cursor == 55:
            self.cursor, self.ref = 0, self.ref + self.direction
            if not 0x22 <= self.ref <= 0x50:  # the rack spans 172 columns of Xr 32-255
                self.direction = -self.direction
        if self.tick % 30 == 0:
            self.events.append(f"march{self.tick // 30 % 4 + 1}")
        self.ram[self.tick % 0x300] = self.tick & 0xFF
        self.tick += 1
        if self.tick == self.length // 2:
            self.player.lives -= 1
            self.events.append("blast")
            self.print(SHIPS, str(self.player.lives))
        if self.tick >= self.length:
            self.phase = "game_over"

    def result(self) -> dict:
        return dict(
            score=self.score, wave=self.wave, kills=self.kills, shots=self.shots_fired, hits=self.hits,
            best_combo=0, ticks=self.tick, victory=False,
        )  # fmt: skip

    def digest(self) -> str:
        state = hashlib.sha256(f"{RULESET} {self.tick} {self.score_wraps}".encode())
        state.update(self.ram[:0x300] + self.vram)
        state.update(repr(sorted(self.text.items())).encode())
        return state.hexdigest()
