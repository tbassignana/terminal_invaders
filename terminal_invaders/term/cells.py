"""A grid of single-width cells with packed styles, with the window calls the Painter scenes make."""

from array import array

from .sanitize import sanitize

# A style packs the foreground slot (bits 0-7), background slot (8-15) and attributes; slot 0 is the default colour.
A_BOLD, A_DIM, A_REVERSE = 1 << 16, 1 << 17, 1 << 18
ATTRS = A_BOLD | A_DIM | A_REVERSE


def style(fg: int = 0, bg: int = 0, attrs: int = 0) -> int:
    return fg | bg << 8 | attrs


class CellBuffer:
    """Row-major cells: chars[y * cols + x] holds one character and styles[y * cols + x] its style."""

    def __init__(self, cols: int, rows: int, *, sanitizer=sanitize):
        self.sanitizer = sanitizer
        self.resize(cols, rows)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = max(0, cols), max(0, rows)
        self.blank = ([" "] * (self.cols * self.rows), array("I", bytes(4 * self.cols * self.rows)))  # for erase()
        self.chars, self.styles = self.blank[0].copy(), self.blank[1][:]

    def copy(self) -> "CellBuffer":
        """An independent buffer with the same cells (the Renderer's previous frame)."""
        other = CellBuffer(0, 0, sanitizer=self.sanitizer)
        other.cols, other.rows, other.chars, other.styles = self.cols, self.rows, self.chars.copy(), self.styles[:]
        other.blank = self.blank  # never written: erase() copies it in
        return other

    def getmaxyx(self) -> tuple[int, int]:
        return self.rows, self.cols

    def erase(self) -> None:
        self.chars[:], self.styles[:] = self.blank

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        """Sanitise, then clip to the grid like Painter.text; never raises for any position."""
        if not 0 <= y < self.rows or x >= self.cols:
            return
        text = self.sanitizer(str(text))
        if x < 0:
            text, x = text[-x:], 0
        text = text[: self.cols - x]
        if text:
            start = y * self.cols + x
            self.chars[start : start + len(text)] = text
            self.styles[start : start + len(text)] = array("I", (attr,)) * len(text)

    def put(self, y: int, x: int, ch: str, st: int) -> None:
        """One trusted character; positions outside the grid are ignored. The packer writes whole rows itself, so
        this is for tests and tools that place a single cell."""
        if 0 <= y < self.rows and 0 <= x < self.cols:
            self.chars[y * self.cols + x] = ch
            self.styles[y * self.cols + x] = st

    def fill(self, y: int, x: int, w: int, h: int, ch: str = " ", st: int = 0) -> None:
        x0, x1 = max(0, x), min(self.cols, x + w)
        if x0 >= x1:
            return
        chars, styles = [ch] * (x1 - x0), array("I", (st,)) * (x1 - x0)
        for row in range(max(0, y), min(self.rows, y + h)):
            start = row * self.cols
            self.chars[start + x0 : start + x1] = chars
            self.styles[start + x0 : start + x1] = styles

    def noutrefresh(self) -> None:
        """Painter compatibility: the backend presents the buffer."""

    def row_text(self, y: int) -> str:
        return "".join(self.chars[y * self.cols : (y + 1) * self.cols])

    def snapshot(self) -> tuple[tuple[str, ...], tuple[int, ...]]:
        return tuple(self.row_text(y) for y in range(self.rows)), tuple(self.styles)
