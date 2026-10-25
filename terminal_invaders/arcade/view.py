"""Arcade mode's screen: the 224 x 256 display the 1978 program draws in VRAM, pooled p x p into half blocks in the
colours of the cabinet's overlay, with the characters it prints shown as terminal text.

VRAM byte (Xr - 32) * 32 + Yr // 8 holds the pixels (Xr, Yr) to (Xr, Yr + 7), bit 0 lowest; the display shows Xr - 32
left to right and Yr from the bottom up, so each VRAM column of 32 bytes is one display column of 256 pixels.
"""

from dataclasses import dataclass

from ..term.pixels import HalfBlockPacker, PixelLayer
from ..theme import SLOT
from .art import CHARSET

W, H = 224, 256  # the display: x = Xr - 32, y = 255 - Yr
SCALES = (1, 2, 3, 4)  # display pixels per pooled pixel, largest block first
FONT = 0x1E00  # in the ROM image: character code c is the 8 column bytes at FONT + 8 * c
SPACE = 0x26
# The overlay (MAME's CC0 invaders.lay): white, red for y 32-61, green for y 184-239 and for x 16-133 below that.
WHITE, RED, GREEN = SLOT["arcade_white"], SLOT["arcade_red"], SLOT["arcade_green"]
# Each byte's 8 pixels from the top of the display down: bit 7 first.
COLUMN_BITS = tuple(bytes((value >> bit) & 1 for bit in range(7, -1, -1)) for value in range(256))
ROLE = {role: bytes((0, role)) + bytes(254) for role in (WHITE, RED, GREEN)}  # 0/1 pixels -> background/role


def band(x: int, y: int) -> int:
    """The overlay's role at display pixel (x, y)."""
    if 32 <= y < 62:
        return RED
    if 184 <= y < 240 or y >= 240 and 16 <= x < 134:
        return GREEN
    return WHITE


def columns(value: int, p: int, span: int) -> bytes:
    """The OR of each group of p adjacent pixels of a row held as a big-endian integer of 224 bytes; span is 223 + p,
    so the group ending at byte i of the shifted copies is byte p - 1 + i of the result."""
    merged = value
    for shift in range(8, 8 * p, 8):
        merged |= value << shift
    return merged.to_bytes(span)[p - 1 :: p]


@dataclass(frozen=True)
class ArcadeLayout:
    cols: int  # terminal
    rows: int
    p: int  # display pixels pooled into one in each direction; a cell holds two pooled rows
    left: int  # block origin (letterboxed, centred)
    top: int
    width: int  # ceil(224 / p) x ceil(128 / p)
    height: int
    fits: bool  # the terminal holds the p = 4 block


def arcade_layout(cols: int, rows: int) -> ArcadeLayout:
    """The largest scale (the smallest p) whose block fits, centred and never off-screen left; p = 4 when none does."""
    for p in SCALES:
        width, height = -(-W // p), -(-H // (2 * p))
        if width <= cols and height <= rows:
            break
    fits = width <= cols and height <= rows
    return ArcadeLayout(cols, rows, p, max(0, (cols - width) // 2), max(0, (rows - height) // 2), width, height, fits)


class ArcadeView:
    """The display as a 224 x 256 layer of 0/1, rebuilt column by column from VRAM where the game wrote since the
    last frame, then per frame: the shown characters' cells cleared, pooled p x p by OR (a lit pixel never vanishes),
    coloured by the overlay band of each pooled pixel's top-left source pixel and packed into half blocks.

    A character the program printed (game.text) is shown as terminal text only while its 8 VRAM bytes still equal
    its glyph in the game's ROM image; otherwise its pixels are drawn. HI-SCORE shows the better of the profile's
    best and the current score, compared in four digits as the program compares them, so it never shows less than
    SCORE<1>."""

    def __init__(self, theme, caps=None, charset: dict[int, str] = CHARSET):
        self.charset = charset
        self.theme = theme
        blocks = caps is None or caps.unicode_blocks
        self.packer = HalfBlockPacker(theme, mono=theme.tier == "mono", ambiguous_wide=not blocks)
        self.pixels = bytearray(W * H)  # row-major, 0 or 1
        self.game = None  # whose VRAM self.pixels holds
        self.scales: dict[int, tuple[PixelLayer, bytearray, tuple]] = {}

    def draw(self, painter, session) -> None:
        """The block at the Painter's frame (the arcade layout's), then the text over it."""
        game = session.pilot.game
        self.update(game)
        shown = self.shown(game)
        best = max(session.profile.best(game.config) % 10000, game.score % 10000)  # as the field's four digits
        hi = {game.hi_score_addr + 0x100 * n: "%04d" % best for n in range(4)}
        p = painter.arcade.p
        layer = self.pool(p, shown)
        self.packer.pack(layer, painter.screen, painter.top, painter.left)
        for addr, char in shown:
            char = hi[addr][(addr - game.hi_score_addr) // 0x100] if addr in hi else char
            if char != " ":
                index = addr - 0x2400
                x, y = index // 32, 248 - 8 * (index % 32)
                painter.text((y + 4) // (2 * p), x // p, char, band(x, y))

    def update(self, game) -> None:
        """Unpack the VRAM columns written since the last frame (every column for a game new to this view)."""
        dirty = range(W) if game is not self.game or game.dirty is None else game.dirty
        vram, pixels, bits = game.vram, self.pixels, COLUMN_BITS
        for x in dirty:
            if 0 <= x < W:
                pixels[x::W] = b"".join(map(bits.__getitem__, vram[32 * x : 32 * x + 32][::-1]))
        self.game, game.dirty = game, set()

    def shown(self, game) -> list[tuple[int, str]]:
        """(VRAM address, character) of every printed character still intact on screen."""
        font, vram, charset, found = bytes(game.rom[FONT : FONT + 0x200]), game.vram, self.charset, []
        for addr, code in game.text.items():
            index = addr - 0x2400
            char = charset.get(code) if code < 64 else None
            if char is None or not 0 <= index <= len(vram) - 225:
                continue
            if vram[index : index + 225 : 32] == font[8 * code : 8 * code + 8]:
                found.append((addr, char))
        return found

    def pool(self, p: int, shown: list[tuple[int, str]]) -> PixelLayer:
        """The pooled, coloured layer for this frame."""
        if p not in self.scales:
            self.scales[p] = self.prepare(p)
        layer, pooled, segments = self.scales[p]
        pixels = bytearray(self.pixels)
        blank = bytes(8)
        for addr, _ in shown:
            index = addr - 0x2400
            x, y = index // 32, 248 - 8 * (index % 32)
            end = min(8, W - x)
            for row in range(y * W + x, (y + 8) * W + x, W):
                pixels[row : row + end] = blank[:end]
        if p == 1:
            pooled = pixels
        else:
            w, span = layer.w, W + p - 1
            for oy in range(layer.h):
                rows = range(oy * p * W, min(H, (oy + 1) * p) * W, W)
                value = 0
                for start in rows:
                    value |= int.from_bytes(pixels[start : start + W])
                pooled[oy * w : (oy + 1) * w] = columns(value, p, span)
        px = layer.px
        for start, end, table in segments:
            px[start:end] = pooled[start:end].translate(table)
        return layer

    def prepare(self, p: int) -> tuple[PixelLayer, bytearray, tuple]:
        """The pooled layer for p, its 0/1 buffer, and the (start, end, translate table) runs that colour it."""
        layer = PixelLayer(-(-W // p), -(-H // p))
        segments = []
        for oy in range(layer.h):
            row = [band(ox * p, oy * p) for ox in range(layer.w)]
            ox = 0
            while ox < layer.w:
                end = ox
                while end < layer.w and row[end] == row[ox]:
                    end += 1
                segments.append([oy * layer.w + ox, oy * layer.w + end, ROLE[row[ox]]])
                ox = end
        merged = [segments[0]]
        for start, end, table in segments[1:]:
            if table is merged[-1][2] and start == merged[-1][1]:
                merged[-1][1] = end
            else:
                merged.append([start, end, table])
        return layer, bytearray(layer.w * layer.h), tuple(map(tuple, merged))
