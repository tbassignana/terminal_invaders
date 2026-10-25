"""MiniVT: a whitelist VT parser and screen model for tests; any sequence it does not know fails the test.

It models what the game relies on (CUP, CUF, SGR, ED 2, the modes ENTER and LEAVE set, the alt screen with its saved
cursor, the kitty keyboard and title stacks, synchronized-output frames) and records the queries it sees, so a
terminal stub can answer them. Writing the last column with autowrap off leaves the cursor position undefined here:
real terminals differ on it, so the next character or CUF without a CUP first is an error.
"""

import codecs
import unicodedata
from dataclasses import dataclass, replace

from terminal_invaders.term.cells import A_BOLD, A_DIM, A_REVERSE

PRIVATE_MODES = {25: "cursor_visible", 7: "autowrap", 1004: "focus", 2004: "paste"}
QUERIES = {("", "n", (5,)): "dsr", ("", "n", (6,)): "cpr", ("", "c", ()): "da1", ("", "c", (0,)): "da1"}
QUERIES |= {(">", "q", ()): "xtversion", (">", "q", (0,)): "xtversion", ("?", "u", ()): "kitty"}


class VTError(AssertionError):
    """The byte stream contained something MiniVT does not accept."""


@dataclass(frozen=True)
class Pen:
    """SGR state; colours are None (default), ("16", 0-15), ("256", n) or ("rgb", r, g, b)."""

    bold: bool = False
    dim: bool = False
    reverse: bool = False
    fg: tuple | None = None
    bg: tuple | None = None


def sgr(pen: Pen, params: list[int]) -> Pen:
    """Apply SGR parameters (semicolon form only) to a pen."""
    index = 0
    while index < len(params):
        code = params[index]
        if code == 0:
            pen = Pen()
        elif code in (1, 2, 7):
            pen = replace(pen, **{{1: "bold", 2: "dim", 7: "reverse"}[code]: True})
        elif code == 22:
            pen = replace(pen, bold=False, dim=False)
        elif code == 27:
            pen = replace(pen, reverse=False)
        elif 30 <= code <= 37 or 90 <= code <= 97:
            pen = replace(pen, fg=("16", code % 10 + (8 if code >= 90 else 0)))
        elif 40 <= code <= 47 or 100 <= code <= 107:
            pen = replace(pen, bg=("16", code % 10 + (8 if code >= 100 else 0)))
        elif code in (39, 49):
            pen = replace(pen, **{"fg" if code == 39 else "bg": None})
        elif code in (38, 48):
            kind = params[index + 1] if index + 1 < len(params) else None
            size = {5: 1, 2: 3}.get(kind)
            values = params[index + 2 : index + 2 + (size or 0)]
            if size is None or len(values) != size or not all(0 <= value <= 255 for value in values):
                raise VTError(f"malformed extended colour in SGR {params}")
            colour = ("256", *values) if kind == 5 else ("rgb", *values)
            pen = replace(pen, **{"fg" if code == 38 else "bg": colour})
            index += 1 + size
        else:
            raise VTError(f"unsupported SGR {code} in {params}")
        index += 1
    return pen


class Screen:
    def __init__(self, cols: int, rows: int):
        self.cols, self.rows = cols, rows
        self.clear()

    def clear(self) -> None:
        self.chars = [[" "] * self.cols for _ in range(self.rows)]
        self.pens = [[Pen()] * self.cols for _ in range(self.rows)]

    def scroll(self) -> None:
        del self.chars[0], self.pens[0]
        self.chars.append([" "] * self.cols)
        self.pens.append([Pen()] * self.cols)

    def resize(self, cols: int, rows: int) -> None:
        self.chars = [(row + [" "] * cols)[:cols] for row in self.chars[:rows]]
        self.pens = [(row + [Pen()] * cols)[:cols] for row in self.pens[:rows]]
        self.chars += [[" "] * cols for _ in range(rows - len(self.chars))]
        self.pens += [[Pen()] * cols for _ in range(rows - len(self.pens))]
        self.cols, self.rows = cols, rows


class MiniVT:
    def __init__(self, cols: int = 80, rows: int = 24, *, on_esu=None):
        self.cols, self.rows = cols, rows
        self.main, self.alt = Screen(cols, rows), None
        self.screen = self.main
        self.y = self.x = 0
        self.pen = Pen()
        self.saved = None
        self.wrap_pending = self.cursor_lost = False
        self.cursor_visible, self.autowrap, self.focus, self.paste, self.sync = True, True, False, False, False
        self.kitty = {"main": [], "alt": []}
        self.titles, self.title = [], ""
        self.queries = []
        self.cprs = []  # (row, col), 1-based, where the cursor was at each CPR query
        self.frames = 0
        self.on_esu = on_esu  # called with this MiniVT at every ESU that closes a synchronized frame
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._state, self._seq = "ground", ""

    # Inspection

    @property
    def alt_screen(self) -> bool:
        return self.screen is self.alt

    @property
    def kitty_depth(self) -> int:
        return len(self.kitty["main"]) + len(self.kitty["alt"])

    def lines(self) -> list[str]:
        return ["".join(row) for row in self.screen.chars]

    def cell(self, y: int, x: int) -> tuple[str, Pen]:
        return self.screen.chars[y][x], self.screen.pens[y][x]

    def snapshot(self) -> tuple[tuple[str, ...], tuple[Pen, ...]]:
        return tuple(self.lines()), tuple(pen for row in self.screen.pens for pen in row)

    def resize(self, cols: int, rows: int) -> None:
        for screen in (self.main, self.alt):
            if screen:
                screen.resize(cols, rows)
        self.cols, self.rows = cols, rows
        self.y, self.x = min(self.y, rows - 1), min(self.x, cols - 1)
        self.wrap_pending = False

    # Parsing

    def feed(self, data: bytes) -> None:
        for n, part in enumerate(bytes(data).split(b"\x18")):
            if n:
                self._cancel()
            try:
                text = self._decoder.decode(part)
            except UnicodeDecodeError as exc:
                raise VTError(f"invalid UTF-8: {exc}") from None
            for char in text:
                getattr(self, "_" + self._state)(char)

    def _cancel(self) -> None:
        """CAN aborts any sequence, even right after ESC, and a UTF-8 character it cuts short shows as U+FFFD, as in
        xterm (a flush or a restart can cut the stream anywhere); outside a sequence it does nothing."""
        if self._decoder.getstate()[0]:
            self._decoder.reset()
            if self._state == "ground":
                self._print("\ufffd")
        self._state = "ground"

    def _ground(self, char: str) -> None:
        if char == "\x1b":
            self._state = "escape"
        elif char == "\n":
            self._linefeed()
        elif char == "\r":
            self.x, self.wrap_pending, self.cursor_lost = 0, False, False
        elif char == "\b":
            self.x, self.wrap_pending = max(0, self.x - 1), False
        elif char == "\t":
            self.x, self.wrap_pending = min(self.cols - 1, (self.x // 8 + 1) * 8), False
        elif char == "\a":
            pass  # BEL rings
        else:
            self._print(char)

    def _escape(self, char: str) -> None:
        self._state, self._seq = {"[": "csi", "]": "osc", "(": "charset"}.get(char, "ground"), ""
        if self._state == "ground":
            raise VTError(f"unsupported escape ESC {char!r}")

    def _charset(self, char: str) -> None:
        if char != "B":
            raise VTError(f"unsupported charset ESC ( {char!r}")
        self._state = "ground"

    def _csi(self, char: str) -> None:
        if "\x20" <= char <= "\x3f":
            self._seq += char
        elif "\x40" <= char <= "\x7e":
            self._state = "ground"
            self._dispatch(self._seq, char)
        else:
            raise VTError(f"control {char!r} inside CSI {self._seq!r}")

    def _osc(self, char: str) -> None:
        if char == "\a":
            self._state = "ground"
            self._osc_dispatch(self._seq)
        elif char == "\x1b":
            self._state = "osc_escape"
        else:
            self._seq += char

    def _osc_escape(self, char: str) -> None:
        if char != "\\":
            raise VTError(f"OSC {self._seq!r} not ended by ST")
        self._state = "ground"
        self._osc_dispatch(self._seq)

    def _osc_dispatch(self, body: str) -> None:
        number, _, value = body.partition(";")
        if number == "2":
            self.title = value
        elif body == "11;?":
            self.queries.append("osc11")
        else:
            raise VTError(f"unsupported OSC {body!r}")

    def _dispatch(self, seq: str, final: str) -> None:
        prefix = seq[0] if seq[:1] in ("<", "=", ">", "?") else ""
        body, intermediate = seq[len(prefix) :], ""
        while body and body[-1] in " !\"#$%&'()*+,-./":
            body, intermediate = body[:-1], body[-1] + intermediate
        if any(char not in "0123456789;" for char in body):
            raise VTError(f"unsupported parameters in CSI {seq!r} {final!r}")
        params = tuple(int(part) if part else None for part in body.split(";")) if body else ()
        query = QUERIES.get((prefix, final, params)) if not intermediate else None
        if query:
            self.queries.append(query)
            if query == "cpr":
                self.cprs.append((self.y + 1, self.x + 1))
        elif (prefix, intermediate, final) == ("?", "$", "p") and len(params) == 1:
            self.queries.append(f"decrqm:{params[0]}")
        elif intermediate:
            raise VTError(f"unsupported CSI {seq!r} {final!r}")
        elif prefix == "" and final == "m":
            if None in params:
                raise VTError(f"empty SGR parameter in {seq!r}")
            self.pen = sgr(self.pen, list(params) or [0])
        elif prefix == "" and final == "H" and len(params) <= 2:
            row, col = (params + (None, None))[:2]
            self.y, self.x = min(self.rows, row or 1) - 1, min(self.cols, col or 1) - 1
            self.wrap_pending = self.cursor_lost = False
        elif prefix == "" and final == "C" and len(params) <= 1:
            if self.cursor_lost:
                raise VTError("CUF while the cursor position is undefined (after a last-column write)")
            self.x, self.wrap_pending = min(self.cols - 1, self.x + ((params or (1,))[0] or 1)), False
        elif prefix == "" and final == "J" and params == (2,):
            if self.pen != Pen():
                raise VTError("ED with a non-default pen (background colour erase differs between terminals)")
            self.screen.clear()
        elif final in "hl" and prefix in ("", "?") and params and None not in params:
            for mode in params:
                self._mode(prefix, mode, final == "h")
        elif prefix == "" and final == "r" and not params:
            self.y = self.x = 0  # DECSTBM homes the cursor
        elif prefix == "" and final == "t" and params in ((22, 0, 0), (23, 0, 0)):
            if params[0] == 22:
                self.titles.append(self.title)
            elif self.titles:
                self.title = self.titles.pop()
        elif prefix == ">" and final == "u" and len(params) == 1 and params[0] is not None:
            self.kitty["alt" if self.alt_screen else "main"].append(params[0])
        elif prefix == "<" and final == "u" and len(params) <= 1:
            stack = self.kitty["alt" if self.alt_screen else "main"]
            del stack[len(stack) - min(len(stack), (params or (1,))[0] or 1) :]
        else:
            raise VTError(f"unsupported CSI {seq!r} {final!r}")

    def _mode(self, prefix: str, mode: int, on: bool) -> None:
        if prefix == "" and mode == 4 and not on:
            return  # IRM off: replace mode, the only mode modelled
        if prefix == "?" and mode == 6 and not on:
            return  # DECOM off
        if prefix == "?" and mode in PRIVATE_MODES:
            setattr(self, PRIVATE_MODES[mode], on)
            self.wrap_pending = self.wrap_pending and self.autowrap
        elif prefix == "?" and mode == 1049:
            self._alternate(on)
        elif prefix == "?" and mode == 2026:
            if on and self.sync:
                raise VTError("BSU inside a synchronized frame")
            if not on and self.sync:
                self.frames += 1
                self.sync = False
                if self.on_esu:
                    self.on_esu(self)
            self.sync = on
        else:
            raise VTError(f"unsupported mode {prefix}{mode} {'h' if on else 'l'}")

    def _alternate(self, on: bool) -> None:
        if on:
            self.saved = (self.y, self.x, self.pen)
            self.alt = Screen(self.cols, self.rows)
            self.screen = self.alt
        else:
            self.screen = self.main
            if self.saved:
                self.y, self.x, self.pen = self.saved
        self.wrap_pending = self.cursor_lost = False

    # Screen updates

    def _linefeed(self) -> None:
        self.wrap_pending = False
        if self.y == self.rows - 1:
            self.screen.scroll()
        else:
            self.y += 1

    def _print(self, char: str) -> None:
        category, width = unicodedata.category(char), unicodedata.east_asian_width(char)
        if category[0] in "CM" or category in ("Zl", "Zp") or width in ("W", "F"):
            raise VTError(f"unprintable or wide character {char!r} (U+{ord(char):04X})")
        if self.cursor_lost:
            raise VTError(f"character {char!r} while the cursor position is undefined (after a last-column write)")
        if self.wrap_pending:
            self.x = 0
            self._linefeed()
        self.screen.chars[self.y][self.x] = char
        self.screen.pens[self.y][self.x] = self.pen
        if self.x < self.cols - 1:
            self.x += 1
        elif self.autowrap:
            self.wrap_pending = True
        else:
            self.cursor_lost = True


def expected_pen(theme, st: int) -> Pen:
    """The pen a cell of style st must show with: its attributes and its theme colours from a reset pen."""
    parts = ["0", *(code for bit, code in ((A_BOLD, "1"), (A_DIM, "2"), (A_REVERSE, "7")) if st & bit)]
    parts += [theme.fg[st & 255], theme.bg[st >> 8 & 255]]
    return sgr(Pen(), [int(part) for part in ";".join(part for part in parts if part).split(";")])


def assert_shows(vt: MiniVT, buf, theme) -> None:
    """The screen holds exactly the CellBuffer: every character, and every cell's pen."""
    assert vt.lines() == [buf.row_text(y) for y in range(buf.rows)]
    pens = {st: expected_pen(theme, st) for st in set(buf.styles)}
    assert vt.snapshot()[1] == tuple(pens[st] for st in buf.styles)
