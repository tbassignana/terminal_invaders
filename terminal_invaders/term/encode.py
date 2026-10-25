"""Diff two CellBuffers into ANSI bytes, and wrap a frame body in its synchronized-output packet.

Every frame starts its first SGR with a full reset and never uses ED, so neither pen drift nor background colour
erase (BCE) can leak between frames; a full repaint writes every cell explicitly.
"""

from itertools import groupby
from typing import Protocol

from .cells import A_BOLD, A_DIM, A_REVERSE, ATTRS, CellBuffer

BSU, ESU = b"\x1b[?2026h", b"\x1b[?2026l"  # the exact forms; Alacritty recognises nothing else
ACKS = {"dsr": b"\x1b[5n", "da1": b"\x1b[c"}
# A tagged ack is CUP to the frame's tag, then CPR, whose reply repeats the position: so every reply names its frame.
# Tags sit on rows TAG_FIRST to TAG_LAST and columns 1 to TAG_COLS, inside the smallest screen the game plays on
# (60x24) and below the probe's CPRs on rows 2 and 3; a smaller screen's tags avoid the edges where the terminal clamps
# the others (tag_area). The first tag is kept for frames sent while ack pacing is off. No tag takes columns 2 to 16
# (TAG_SKIP): a terminal shrunk to one row clamps every reply onto row 1, and there CSI 1;m R with m from 2 to 16 is
# also F3 with modifiers (decode), which while replies were owed then read as replies.
TAG_FIRST, TAG_LAST, TAG_COLS = 4, 24, 60
TAG_SKIP = range(2, 17)  # the modifier parameters of a legacy F3 key, CSI 1;m R
SYNC_LIMIT = 1 << 20  # larger frames go out unsynchronized; Alacritty's sync buffer is 2 MiB
MAX_GAP = 4  # unchanged cells in the pen's own style are re-sent rather than skipped with CUF
BOLD_DIM = A_BOLD | A_DIM


class FrameEncoder(Protocol):
    """What the Renderer needs; a Phase 3 PlanarEncoder implements the same protocol."""

    last_full: bool

    def encode(self, prev: CellBuffer | None, cur: CellBuffer) -> bytes: ...

    def invalidate(self) -> None: ...


class Encoder:
    """Row diff with run merging, CUP/CUF positioning and minimal SGR deltas."""

    def __init__(self, theme, caps):
        self.charset = "utf-8" if caps.utf8 else "ascii"  # "?" stands in for anything a non-UTF-8 terminal lacks
        self.pens, self.deltas = _Pens(theme), _Deltas()
        self.repaint, self.last_full = False, False
        # row -> (its chars, its styles, and their planes) as the last frame that changed the row left it: the next
        # change to it diffs against these planes instead of building them again, once its cells are checked equal
        self.known = {}

    def invalidate(self) -> None:
        """The next frame paints every cell explicitly (after resize, resume or a cut frame)."""
        self.repaint = True

    def encode(self, prev: CellBuffer | None, cur: CellBuffer) -> bytes:
        """Bytes that turn a screen showing prev into cur; prev None means the screen is unknown."""
        full = self.repaint or prev is None or (prev.cols, prev.rows) != (cur.cols, cur.rows)
        self.repaint, self.last_full = False, full
        cols, chars, styles = cur.cols, cur.chars, cur.styles
        out, pen = [], None
        for y in range(cur.rows):
            start, end = y * cols, y * cols + cols
            if full:
                spans = [[0, cols]] if cols else []
            else:
                old_chars, new_chars = prev.chars[start:end], chars[start:end]
                old_styles, new_styles = prev.styles[start:end], styles[start:end]
                if old_chars == new_chars and old_styles == new_styles:
                    continue
                spans = self._changed(y, old_chars, new_chars, old_styles, new_styles)
            pen = self._row(out, y, start, cols, spans, chars, styles, pen)
        return "".join(out).encode(self.charset, "replace")

    def _changed(self, y, old_chars, new_chars, old_styles, new_styles) -> list[list[int]]:
        """_spans() of row y. A row that changes keeps changing (a sprite moving across it), and its previous cells
        are then most often the ones the last frame encoded: their planes are taken from `known` when those cells are
        equal, which costs a fraction of building them."""
        known = self.known.get(y)
        if known is not None and known[0] == old_chars and known[1] == old_styles:
            old_plane, old_style_plane = known[2], known[3]
        else:
            old_plane, old_style_plane = _plane(old_chars), _style_plane(old_styles)
        new_plane, new_style_plane = _plane(new_chars), _style_plane(new_styles)
        self.known[y] = (new_chars, new_styles, new_plane, new_style_plane)
        return _runs((old_plane ^ new_plane) | (old_style_plane ^ new_style_plane), len(new_chars))

    def _row(self, out, y, base, cols, spans, chars, styles, pen):
        pens, deltas, emit = self.pens, self.deltas, out.append
        cursor = None  # column the next character lands in; None after the last column (autowrap is off)
        for start, end in spans:
            if cursor is None:
                emit(f"\x1b[{y + 1};{start + 1}H")
            elif start - cursor <= MAX_GAP and all(pens[st] == pen for st in styles[base + cursor : base + start]):
                emit("".join(chars[base + cursor : base + start]))
            else:
                emit(f"\x1b[{start - cursor}C")
            x = base + start
            if end - start == 1:  # half of all spans: one cell, one style
                new = pens[styles[x]]
                if new != pen:
                    emit(deltas[pen, new])
                    pen = new
                emit(chars[x])
            else:
                for st, run in groupby(styles[base + start : base + end]):
                    n = len(list(run))
                    new = pens[st]
                    if new != pen:
                        emit(deltas[pen, new])
                        pen = new
                    emit("".join(chars[x : x + n]))
                    x += n
            cursor = end if end < cols else None
        return pen


class _Pens(dict):
    """style -> (attributes, foreground fragment, background fragment), filled on first use."""

    def __init__(self, theme):
        super().__init__()
        self.fg, self.bg = theme.fg, theme.bg

    def __missing__(self, st):
        pen = self[st] = (st & ATTRS, self.fg[st & 255], self.bg[st >> 8 & 255])
        return pen


class _Deltas(dict):
    """(pen, new pen) -> SGR, filled on first use."""

    def __missing__(self, key):
        sgr = self[key] = _sgr(*key)
        return sgr


CHANGED = bytes([0] + [1] * 255)  # bytes.translate table: any non-zero byte -> 1


def _spans(old_chars, new_chars, old_styles, new_styles) -> list[list[int]]:
    """[start, end) runs of changed cells, before gap merging.

    Chars (as UTF-32) and styles are 4 bytes per cell, so one big-integer XOR per plane finds every difference; folding
    each cell's 4 bytes into its last byte leaves one flag per cell, and bytes.find walks the runs at C speed.
    """
    chars = _plane(old_chars) ^ _plane(new_chars)
    styles = _style_plane(old_styles) ^ _style_plane(new_styles)
    return _runs(chars | styles, len(new_chars))


def _runs(diff: int, count: int) -> list[list[int]]:
    """The runs of the cells whose 4 bytes are not all zero in diff (the XOR of two rows' planes, `count` cells)."""
    diff |= diff >> 8
    diff |= diff >> 16
    flags = diff.to_bytes(4 * count)[3::4].translate(CHANGED)
    spans, start = [], flags.find(1)
    while start >= 0:
        end = flags.find(0, start)
        end = len(flags) if end < 0 else end
        spans.append([start, end])
        start = flags.find(1, end)
    return spans


def _plane(chars) -> int:
    return int.from_bytes("".join(chars).encode("utf-32-le", "surrogatepass"))


def _style_plane(styles) -> int:
    return int.from_bytes(styles.tobytes())


def _sgr(pen, new) -> str:
    """SGR from pen to new; pen None (a frame's first SGR) gives the full reset form CSI 0;attrs;fg;bg m."""
    attrs, fg, bg = new
    if pen is None:  # the reset already selects the default colours
        parts = ["0", *_attrs_on(attrs), "" if fg == "39" else fg, "" if bg == "49" else bg]
    else:
        old, old_fg, old_bg = pen
        parts = []
        if old & ~attrs & BOLD_DIM:  # 22 clears both bold and dim; whichever stays on is re-added below
            parts.append("22")
            old &= ~BOLD_DIM
        if old & ~attrs & A_REVERSE:
            parts.append("27")
            old &= ~A_REVERSE
        parts += _attrs_on(attrs & ~old)
        parts += [fg] if fg != old_fg else []
        parts += [bg] if bg != old_bg else []
    return "\x1b[" + ";".join(part for part in parts if part) + "m"


def _attrs_on(attrs: int) -> list[str]:
    return [code for bit, code in ((A_BOLD, "1"), (A_DIM, "2"), (A_REVERSE, "7")) if attrs & bit]


def tag_area(cols: int, rows: int) -> tuple[int, int]:
    """The last row and the last column the ack tags of a screen this size use. The terminal clamps the CUP
    of a frame tagged for a larger screen onto the last row of one with fewer rows than the tag area, and onto the
    last column of one with fewer columns: so such a screen's tags avoid that row or column, and a clamped reply
    names no frame tagged for it."""
    return (TAG_LAST if rows >= TAG_LAST else rows - 1), (TAG_COLS if cols >= TAG_COLS else max(0, cols - 1))


def ack_tag(seq: int | None, cols: int, rows: int) -> tuple[int, int] | None:
    """The position the seq-th tagged ack query parks the cursor at (seq None: the first tag, for frames sent while
    ack pacing is off), within tag_area(). None (no ack query) when fewer than two tags fit, as on every screen under
    5 rows: a reply on row 4 would be a clamped one, on rows 2 and 3 it would read as the probe's own, and on row 1
    as F3."""
    count = tag_count(cols, rows)
    if count < 2:
        return None
    across = _across(tag_area(cols, rows)[1])
    index = 0 if seq is None else 1 + seq % (count - 1)
    column = index % across
    return TAG_FIRST + index // across, 1 if column == 0 else TAG_SKIP.stop - 1 + column


def tag_count(cols: int, rows: int) -> int:
    """How many tags a screen this size has, the first (seq None) included."""
    last, width = tag_area(cols, rows)
    return _across(width) * max(0, last - TAG_FIRST + 1)


def _across(width: int) -> int:
    """How many tags a row of the tag area holds: column 1, then those from TAG_SKIP.stop to `width`."""
    return 0 if width < 1 else 1 + max(0, width - TAG_SKIP.stop + 1)


def packet(body: bytes, caps, tag: tuple[int, int] | None = None) -> bytes:
    """One frame packet: BSU body ESU when the terminal syncs (and the body fits its buffer), then the ack query,
    which for CPR acks parks the cursor at the frame's tag first (none without a tag)."""
    sync = caps.sync and len(body) <= SYNC_LIMIT
    if caps.ack == "cpr":
        ack = b"" if tag is None else b"\x1b[%d;%dH\x1b[6n" % tag
    else:
        ack = ACKS.get(caps.ack, b"")
    return (BSU if sync else b"") + body + (ESU if sync else b"") + ack
