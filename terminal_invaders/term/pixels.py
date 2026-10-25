"""Pixel layer of role ids, pooled downsampling, and the half-block packer that turns two pixel rows into one cell row.

Pixel values are theme slots (0 is the background). The packer works a row pair at a time: two bytes.translate calls
turn the rows into the high and low nibbles of one pair code per column, and precomputed tables map each code to its
character and style. A row is written blank first, then the span from its first to its last sprite pixel is looked up
in one operator.itemgetter call. A row pair whose pixels are as they were when it was last packed gets those cells
again.
"""

from array import array
from collections.abc import Sequence
from operator import itemgetter
from typing import NamedTuple

from ..theme import SLOT
from .cells import A_REVERSE, CellBuffer, style

UPPER, LOWER = "\u2580", "\u2584"  # the only non-ASCII characters the game prints
SPACES = (" ",) * 256  # a pooled cell is always a space
# Pooling keeps the highest: shots never vanish when the layer is pooled. Telegraph marks rank above the backdrop
# and below every entity, so a mark never hides what it warns of.
POOL_ORDER = ("player_shot", "enemy_shot", "player", "pickup", "diver", "alien", "ufo", "boss", "shelter")
POOL_ORDER += ("telegraph", "star")
POOL_ORDER += ("arcade_white", "arcade_red", "arcade_green")  # Arcade mode's bands, never in a campaign layer
# Sorted by slot, so telegraph (appended to the roles) takes the last nibble of Phase 4's table and the Arcade bands
# keep theirs.
PIXEL_SLOTS = (0, *sorted(SLOT[role] for role in POOL_ORDER))
# Phase 5's spark (bursts and debris, appended to the roles) takes the 16th and last nibble, after every slot above,
# and pools between telegraph and star: where a pooled cell holds debris and a shot or a mark, the shot or mark wins
# (Phase 5 spec 2.10). POOL_ORDER and PIXEL_SLOTS keep Phase 4's values, which tests/term/ pins; the packer and the
# pool use these. At most 16 slots, so a pair code fits in one byte.
RANKED = (*POOL_ORDER[: POOL_ORDER.index("star")], "spark", *POOL_ORDER[POOL_ORDER.index("star") :])
PACKED_SLOTS = (*PIXEL_SLOTS, SLOT["spark"])


def ranks(order: Sequence[int]) -> bytes:
    """A rank for every byte value, as a permutation of 0-255: order[0] ranks highest and unlisted values rank below
    the listed ones in ascending order, so 0 (the background) ranks lowest unless listed."""
    rest = [value for value in range(256) if value not in order]
    rank = bytearray(256)
    for position, value in enumerate([*rest, *reversed(order)]):
        rank[value] = position
    return bytes(rank)


PRIORITY = ranks([SLOT[role] for role in RANKED])


class Art:
    """A w x h sprite of role ids; 0 is transparent. Opaque runs are precomputed so blitting is slice assignment."""

    __slots__ = ("w", "h", "px", "runs", "spans")

    def __init__(self, w: int, h: int, px: bytes):
        if len(px) != w * h:
            raise ValueError("art size does not match its pixels")
        self.w, self.h, self.px = w, h, bytes(px)
        self.runs = tuple(_runs(self.px[y * w : (y + 1) * w]) for y in range(h))
        self.spans = {}  # layer width -> every run as (start, end, pixels), offsets from the top-left pixel

    def spans_in(self, w: int) -> tuple[tuple[int, int, bytes], ...]:
        """The runs as offsets into a layer w pixels wide, worked out once for each width."""
        spans = self.spans.get(w)
        if spans is None:
            spans = self.spans[w] = tuple(
                (y * w + dx, y * w + dx + len(run), run) for y, runs in enumerate(self.runs) for dx, run in runs
            )
        return spans

    @classmethod
    def from_rows(cls, rows: Sequence[str], role: int) -> "Art":
        """Rows of text where "." is transparent and any other character is the role."""
        w = max(map(len, rows), default=0)
        px = b"".join(bytes(0 if ch == "." else role for ch in row.ljust(w, ".")) for row in rows)
        return cls(w, len(rows), px)


def _runs(row: bytes) -> tuple[tuple[int, bytes], ...]:
    runs, x = [], 0
    while x < len(row):
        if row[x]:
            end = x
            while end < len(row) and row[end]:
                end += 1
            runs.append((x, row[x:end]))
            x = end
        else:
            x += 1
    return tuple(runs)


class PixelLayer:
    """w x h role ids, row-major; 0 = background."""

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.px = bytearray(w * h)

    def clear(self) -> None:
        self.px[:] = bytes(len(self.px))

    def dot(self, x: int, y: int, role: int) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[y * self.w + x] = role

    def blit(self, x: int, y: int, art: Art) -> None:
        """Draw art's opaque pixels with its top-left at (x, y), clipped to the layer."""
        px, w = self.px, self.w
        if 0 <= x and x + art.w <= w and 0 <= y and y + art.h <= self.h:  # inside: no clipping per run
            base = y * w + x
            for start, end, run in art.spans_in(w):
                px[base + start : base + end] = run
            return
        for row in range(max(0, -y), min(art.h, self.h - y)):
            base = (y + row) * self.w
            for dx, run in art.runs[row]:
                left, right = max(0, -(x + dx)), min(len(run), self.w - x - dx)
                if left < right:
                    self.px[base + x + dx + left : base + x + dx + right] = run[left:right]


def downsample(layer: PixelLayer, fx: int, fy: int, priority: bytes) -> PixelLayer:
    """Pool fx x fy blocks into one pixel, keeping the role of highest rank (priority is a ranks() permutation)."""
    out = PixelLayer(-(-layer.w // fx), -(-layer.h // fy))
    inverse = bytes.maketrans(priority, bytes(range(256)))
    w, pad = layer.w, bytes(out.w * fx - layer.w)
    for oy in range(out.h):
        block = range(oy * fy, min(layer.h, (oy + 1) * fy))  # the last block may be short
        ranked = [layer.px[y * w : (y + 1) * w].translate(priority) + pad for y in block]
        column = bytes(map(max, *ranked)) if len(ranked) > 1 else bytes(ranked[0])
        pooled = bytes(map(max, *(column[i::fx] for i in range(fx)))) if fx > 1 else column
        out.px[oy * out.w : (oy + 1) * out.w] = pooled.translate(inverse)
    return out


class RowMaps(NamedTuple):
    """Packing tables for one theme: role id -> nibble of the pair code, and pair code -> cell."""

    top: bytes  # bytes.translate table: role id -> high nibble (unknown roles pack as background)
    bottom: bytes  # role id -> low nibble
    chars: tuple[str, ...]  # pair code -> character
    styles: tuple[int, ...]  # pair code -> style
    pooled: tuple[int, ...]  # pair code -> style of the pooled full cell: the pair's higher PRIORITY role (2:1 pool)


def half_block(theme, top: int, bottom: int, *, mono: bool) -> tuple[str, int]:
    """The packer's cell for a top and a bottom pixel (the slow path that row_maps tabulates)."""
    if mono:
        if top and bottom:
            return " ", A_REVERSE
        return (UPPER if top else LOWER if bottom else " "), 0
    if top == bottom:
        return " ", style(bg=top)
    if top:
        return UPPER, style(top, bottom, theme.attrs[top])
    return LOWER, style(bottom, 0, theme.attrs[bottom])


def row_maps(theme, *, mono: bool = False) -> RowMaps:
    nibble = bytearray(256)
    for index, slot in enumerate(PACKED_SLOTS):
        nibble[slot] = index
    top, bottom = bytes(n << 4 for n in nibble), bytes(nibble)
    chars, styles, pooled = [" "] * 256, [0] * 256, [0] * 256
    for t, top_slot in enumerate(PACKED_SLOTS):
        for b, bottom_slot in enumerate(PACKED_SLOTS):
            chars[t << 4 | b], styles[t << 4 | b] = half_block(theme, top_slot, bottom_slot, mono=mono)
            role = max(top_slot, bottom_slot, key=PRIORITY.__getitem__)  # what downsample(layer, 1, 2) keeps
            pooled[t << 4 | b] = (A_REVERSE if mono else style(bg=role)) if role else 0
    return RowMaps(top, bottom, tuple(chars), tuple(styles), tuple(pooled))


class HalfBlockPacker:
    """Two pixel rows per cell row with U+2580/U+2584, or 2:1 pooled spaces where those blocks are unsafe.

    ambiguous_wide is true whenever half blocks cannot be trusted to be one cell: an ambiguous-wide or non-UTF-8
    terminal.
    """

    def __init__(self, theme, *, mono: bool, ambiguous_wide: bool):
        self.maps = row_maps(theme, mono=mono)
        self.pooled = ambiguous_wide
        # layer row pair -> (its two pixel rows, the cells packed from them) as last packed. A cell row depends on
        # nothing but its two pixel rows, and most of them are as they were a frame before: those are copied back.
        self.known = {}

    def pack(self, layer: PixelLayer, buf: CellBuffer, top: int, left: int) -> None:
        """Write the layer into buf with its top-left pixel pair at cell (top, left), clipped to buf. Pooled, each
        cell is a space in the colour of the pair's higher-priority pixel, so shots never vanish."""
        x0, x1 = max(0, -left), min(layer.w, buf.cols - left)
        if x0 >= x1:
            return
        top_map, bottom_map, chars, styles, pooled = self.maps
        if self.pooled:
            chars, styles = SPACES, pooled
        w, n, px, cells, cell_styles = layer.w, x1 - x0, layer.px, buf.chars, buf.styles
        blank_chars, blank_styles = [" "] * n, array("I", bytes(4 * n))  # pair code 0 is a background space
        known = self.known
        for row in range(max(0, -top), min((layer.h + 1) // 2, buf.rows - top)):
            cell = (top + row) * buf.cols + left + x0
            upper, lower = 2 * row * w + x0, (2 * row + 1) * w + x0
            above, below = px[upper : upper + n], px[lower : lower + n]  # below is empty past an odd last row
            last = known.get(row)
            if last is not None and last[0] == above and last[1] == below:
                cells[cell : cell + n], cell_styles[cell : cell + n] = last[2], last[3]
                continue
            high, low = int.from_bytes(above.translate(top_map)), int.from_bytes(below.translate(bottom_map))
            cells[cell : cell + n], cell_styles[cell : cell + n] = blank_chars, blank_styles
            if high | low:
                code = (high | low).to_bytes(n)
                start, end = n - len(code.lstrip(b"\0")), len(code.rstrip(b"\0"))
                if end - start == 1:  # itemgetter of one index returns the item, not a tuple
                    cells[cell + start], cell_styles[cell + start] = chars[code[start]], styles[code[start]]
                else:
                    pick = itemgetter(*code[start:end])
                    cells[cell + start : cell + end] = pick(chars)
                    cell_styles[cell + start : cell + end] = array("I", pick(styles))
            known[row] = (above, below, cells[cell : cell + n], cell_styles[cell : cell + n])
