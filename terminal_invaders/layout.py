"""Where the play block sits in the terminal, and how far it scales up."""

from dataclasses import dataclass

from .model import HEIGHT, WIDTH

SCALES = (6, 4, 3, 2)  # pixel arena scales, largest first; 1 is the glyph arena
CHROME = 4  # two HUD rows above the arena, two footer rows below


@dataclass(frozen=True)
class Layout:
    cols: int  # terminal
    rows: int
    k: int  # 1 = glyph arena; 2, 3, 4, 6 = pixel arena
    left: int  # block origin (letterboxed, centred)
    top: int
    width: int  # 60k x (20k + 4)
    height: int
    fits: bool  # the terminal holds the k = 1 block


def compute_layout(cols: int, rows: int, *, max_k: int | None) -> Layout:
    """Largest scale whose block fits; the block is centred, never off-screen left. Every terminal can show the pixel
    arena: where half blocks are unsafe (ambiguous-wide or not UTF-8) the packer pools pixels 2:1 into spaces."""
    scales = [k for k in SCALES if (max_k is None or k <= max_k) and WIDTH * k <= cols and HEIGHT * k + CHROME <= rows]
    k = scales[0] if scales else 1
    width, height = WIDTH * k, HEIGHT * k + CHROME
    fits = cols >= WIDTH and rows >= HEIGHT + CHROME
    return Layout(cols, rows, k, max(0, (cols - width) // 2), max(0, (rows - height) // 2), width, height, fits)
