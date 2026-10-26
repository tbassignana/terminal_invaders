#!/usr/bin/env python3
"""Make the README's screenshots and animations from the real game: python3 tools/make_media.py [--out docs/media]

Every image is what the game draws in a good terminal: truecolor, dark background, the default palette and effects
`full`, with the controls a terminal that reports key releases gets (HOLD) and sound on. Nothing is drawn by hand:
each frame is a real game state (a golden replay in tests/fixtures stepped tick by tick, or a game tests/support/
frames.py snapshots from one, or a title card) drawn by the real Painter (terminal_invaders/ui.py) into a CellBuffer
as the Renderer draws it before encoding, with one flash limiter for a whole animation. This tool only rasterises
that grid of cells: each cell's background and foreground in the colours its theme gives the style's slots, the half
blocks (U+2580 and U+2584, the only block elements the game prints; the others are drawn the same way) as exact
filled rectangles so the pixel art has no seams, every other character in DejaVu Sans Mono (bold in its Bold face,
dim blended halfway toward the background, reverse with its colours swapped). The 120 x 44 screens are drawn in
8 x 16 pixel cells (a half block is then an 8 x 8 square), the cards in 12 x 24 cells, the 224 x 128 Arcade screen
and palettes.png's panels in 4 x 8 cells, each with an even 16 pixel border in the colour of its top-left cell (the
field's background).

What each file shows and where it comes from:

  hero.gif      Campaign gameplay at 120 x 44 (pixel art at k = 2): wave 15 of golden-classic-normal.json (Normal,
                seed 1978), ticks 68,520 to 69,120 (10 s): the formation under fire, kill bursts, debris and score
                popups, the UFO shot down for +150, the starfield.
  boss.gif      The last boss at 120 x 44: the MOTHERSHIP's final phase in wave 24 of golden-daily.json (the Daily run
                that clears all 24 waves), ticks 116,620 to 117,340 (12 s, to the tape's last tick): its health bar,
                the PHASE BREAK that opens the phase (burst and field kick), its bullet streams and beam, its
                destruction (+40,000) and the WAVE 24 CLEAR panel.
  arcade.gif    Arcade mode (the 1978 program, in its own colours) at 224 x 128, where every 1978 pixel has its own
                half block: golden-arcade.json, round 2, ticks 5,370 to 5,910 (9 s): the rack marching and the saucer
                shot down. At this size the program's text is one terminal character per 8 x 8 character cell, so it
                is small, as it is in a terminal that size.
  title.png     The title menu.
  upgrade.png   The first upgrade card, after wave 3 of golden-classic-normal, at 120 x 44 over the arena (the `card`
                scene of tests/support/frames.py: the first tick its keys are armed).
  results.png   The results card of golden-classic-normal's run (the tape, then no input until the run is lost: the
                `end` snapshot of tests/support/frames.py), wave 16 of 24.
  settings.png  SETTINGS with a new player's values.
  palettes.png  One moment of golden-classic-normal (tick 33,430, wave 8: the UFO, a diver, both sides' shots, a burst
                and score popups) in the four palettes, 2 x 2, each at 120 x 44, labelled with the names SETTINGS gives
                them: DEFAULT, COLOUR-BLIND SAFE and HIGH CONTRAST in truecolor, and MONO (the mono tier: no colour at
                all, so it shows in the terminal's own colours, here light grey on near-black).

The three cards (title, results, settings) are the 60 x 24 card the game draws, cut from a 120 x 44 screen like the
animations'. An 80 x 24 terminal draws the same card between two 10-column letterbox bands, but its SETTINGS card
names the scale it plays at, AUTO (TEXT), where this one says AUTO (2X).

The animations are 30 frames a second: every 2nd tick of the game's 60 (ffmpeg writes delays of 30, 30 and 40 ms,
so they run at the game's speed), assembled by ffmpeg in two passes (palettegen over every frame, then paletteuse
without dithering), looping forever. Nothing reads a clock or a random source, so two runs draw the same frames and
write the same bytes (ffmpeg's GIF muxer embeds no time or version; the PNGs carry no metadata); another FreeType,
Pillow or ffmpeg version may rasterise text or build a palette a little differently. The tool writes nothing but
--out (an animation's frames go to a temporary folder in it, removed when the file is made) and --frames DIR, and
no bytecode: running it leaves the checkout as it was.

Needs Pillow, ffmpeg and the DejaVu Sans Mono font (DejaVuSansMono.ttf and DejaVuSansMono-Bold.ttf, where Pillow
finds fonts); it names what is missing and stops. --only NAME ... makes just those files; --frames DIR also keeps
each animation's frames as PNG files (DIR/hero/00000.png, ...).
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

sys.dont_write_bytecode = True  # no __pycache__ in the checkout: the tool writes only --out (and --frames)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # main() names it and stops
    Image = ImageDraw = ImageFont = None

from terminal_invaders import fx  # noqa: E402
from terminal_invaders.input import HoldControls  # noqa: E402
from terminal_invaders.replay import Playback  # noqa: E402
from terminal_invaders.rules import game_class  # noqa: E402
from terminal_invaders.term.cells import A_BOLD, A_DIM, A_REVERSE, CellBuffer  # noqa: E402
from terminal_invaders.term.color import xterm_rgb  # noqa: E402
from terminal_invaders.theme import PALETTE_LABELS, SLOT, resolve_theme  # noqa: E402
from terminal_invaders.ui import Painter  # noqa: E402
from tests.support import frames  # noqa: E402

OUT = ROOT / "docs" / "media"
FIXTURES = ROOT / "tests" / "fixtures"
FONTS = ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf")
FPS, EVERY = 30, 2  # frames a second, and game ticks (60 a second) per frame
PAD = 16  # the border around each screen, in pixels
GAP = 24  # palettes.png: between its panels, and the room above each for its label
DIM = 0.5  # how far dim text goes toward its background
# The terminal's own colours, which only the mono tier shows (truecolor paints every cell itself).
TERMINAL_FG, TERMINAL_BG = (0xD0, 0xD0, 0xD0), (0x12, 0x12, 0x12)
# xterm's 16 colours, for a 16-colour or 256-colour theme's indices 0-15 (no file here uses one).
ANSI = (
    (0x00, 0x00, 0x00),
    (0xCD, 0x00, 0x00),
    (0x00, 0xCD, 0x00),
    (0xCD, 0xCD, 0x00),
    (0x00, 0x00, 0xEE),
    (0xCD, 0x00, 0xCD),
    (0x00, 0xCD, 0xCD),
    (0xE5, 0xE5, 0xE5),
    (0x7F, 0x7F, 0x7F),
    (0xFF, 0x00, 0x00),
    (0x00, 0xFF, 0x00),
    (0xFF, 0xFF, 0x00),
    (0x5C, 0x5C, 0xFF),
    (0xFF, 0x00, 0xFF),
    (0x00, 0xFF, 0xFF),
    (0xFF, 0xFF, 0xFF),
)
# Block elements as filled rectangles (x0, y0, x1, y1) in eighths of the cell: the game prints only the two half
# blocks, the rest are here so any block a future frame uses still has no seams.
BLOCKS = {
    "▀": ((0, 0, 8, 4),),  # upper half
    "▄": ((0, 4, 8, 8),),  # lower half
    "█": ((0, 0, 8, 8),),  # full block
    "▌": ((0, 0, 4, 8),),  # left half
    "▐": ((4, 0, 8, 8),),  # right half
    "▔": ((0, 0, 8, 1),),  # upper eighth
    "▕": ((7, 0, 8, 8),),  # right eighth
    "▖": ((0, 4, 4, 8),),  # quadrants
    "▗": ((4, 4, 8, 8),),
    "▘": ((0, 0, 4, 4),),
    "▝": ((4, 0, 8, 4),),
    "▙": ((0, 0, 4, 8), (4, 4, 8, 8)),
    "▚": ((0, 0, 4, 4), (4, 4, 8, 8)),
    "▛": ((0, 0, 8, 4), (0, 4, 4, 8)),
    "▜": ((0, 0, 8, 4), (4, 4, 8, 8)),
    "▞": ((4, 0, 8, 4), (0, 4, 4, 8)),
    "▟": ((4, 0, 8, 4), (0, 4, 8, 8)),
}
BLOCKS |= {chr(0x2580 + n): ((0, 8 - n, 8, 8),) for n in range(1, 8)}  # lower eighths, U+2581-2587
BLOCKS |= {chr(0x2588 + n): ((0, 0, 8 - n, 8),) for n in range(1, 8)}  # left eighths, U+2589-258F


# --- cells to pixels -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    """A cell size in pixels, the font size that fills it, and the baseline's row in the cell."""

    w: int
    h: int
    size: int
    baseline: int


SMALL = Cell(4, 8, 7, 6)  # the 224 x 128 Arcade screen and palettes.png's panels
PLAY = Cell(8, 16, 13, 12)  # 120 x 44 (DejaVu Sans Mono's advance at 13 px is 7.8)
CARD = Cell(12, 24, 20, 18)  # 80 x 24


def colour(fragment: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """The RGB a terminal shows for an SGR colour fragment (term/color.py): 38;2;R;G;B, 38;5;N, a 16-colour code,
    or the terminal's own colour for '', 39 and 49."""
    if fragment in ("", "39", "49"):
        return default
    parts = [int(part) for part in fragment.split(";")]
    if parts[0] in (38, 48) and parts[1] == 2:
        return tuple(parts[2:5])
    if parts[0] in (38, 48) and parts[1] == 5:
        index = parts[2]
        rgb = xterm_rgb(index) if index >= 16 else None
        return ANSI[index] if rgb is None else (rgb >> 16 & 255, rgb >> 8 & 255, rgb & 255)
    code = parts[0]
    index = code % 10 + (8 if code >= 90 else 0)
    return ANSI[index]


class Raster:
    """Draws CellBuffers into an image, redrawing only the cells that changed since the last one (as a terminal
    does), so a clip of hundreds of frames stays quick."""

    def __init__(self, cell: Cell, theme, cols: int, rows: int):
        self.cell, self.cols, self.rows = cell, cols, rows
        self.fonts = {bold: ImageFont.truetype(FONTS[bold], cell.size) for bold in (False, True)}
        self.fg = [colour(part, TERMINAL_FG) for part in theme.fg]
        self.bg = [colour(part, TERMINAL_BG) for part in theme.bg]
        self.image = Image.new("RGB", (cols * cell.w, rows * cell.h), TERMINAL_BG)
        self.draw = ImageDraw.Draw(self.image)
        self.glyphs: dict[tuple[str, bool], Image.Image] = {}
        self.chars: list[str | None] = [None] * (cols * rows)
        self.styles: list[int | None] = [None] * (cols * rows)

    def pens(self, st: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """A style's foreground and background as the terminal shows them."""
        fg, bg = self.fg[st & 255], self.bg[st >> 8 & 255]
        if st & A_DIM:
            fg = tuple(round(f + (b - f) * DIM) for f, b in zip(fg, bg))
        return (bg, fg) if st & A_REVERSE else (fg, bg)

    def glyph(self, char: str, bold: bool):
        mask = self.glyphs.get((char, bold))
        if mask is None:
            mask = Image.new("L", (self.cell.w, self.cell.h), 0)
            ImageDraw.Draw(mask).text((0, self.cell.baseline), char, fill=255, font=self.fonts[bold], anchor="ls")
            self.glyphs[char, bold] = mask
        return mask

    def render(self, buf: CellBuffer) -> "Image.Image":
        """The image of buf (the same object every call: copy it to keep it)."""
        w, h, draw = self.cell.w, self.cell.h, self.draw
        for index, (char, st) in enumerate(zip(buf.chars, buf.styles)):
            if char == self.chars[index] and st == self.styles[index]:
                continue
            self.chars[index], self.styles[index] = char, st
            x0, y0 = (index % self.cols) * w, (index // self.cols) * h
            fg, bg = self.pens(st)
            draw.rectangle((x0, y0, x0 + w - 1, y0 + h - 1), fill=bg)
            if char == " ":
                continue
            rects = BLOCKS.get(char)
            if rects:
                for left, top, right, bottom in rects:
                    box = (x0 + left * w // 8, y0 + top * h // 8, x0 + right * w // 8 - 1, y0 + bottom * h // 8 - 1)
                    draw.rectangle(box, fill=fg)
            else:
                self.image.paste(fg, (x0, y0), self.glyph(char, bool(st & A_BOLD)))
        return self.image

    def edge(self, buf: CellBuffer) -> tuple[int, int, int]:
        """The background of the screen's top-left cell (the letterbox round a card, else the field's): the border's
        colour."""
        return self.pens(buf.styles[0])[1]


def framed(image, background, pad: int = PAD):
    """image with an even border of pad pixels in background."""
    out = Image.new("RGB", (image.width + 2 * pad, image.height + 2 * pad), background)
    out.paste(image, (pad, pad))
    return out


# --- what the game draws -------------------------------------------------------------------------------------------


def truecolor(palette: str = "default"):
    """The theme a truecolor terminal on a dark background resolves (it paints its own background); the mono
    palette is the mono tier, which paints nothing."""
    color = "mono" if palette == "mono" else "truecolor"
    caps = SimpleNamespace(color=color, theme="dark", paint_background=color == "truecolor")
    return resolve_theme(caps, palette)


def session(game=None, **overrides):
    """A Session as a new player has it on a terminal that reports key releases (HOLD controls), with the options
    tests/support/frames.py gives its goldens but effects at their default, full; playing game when given."""
    current = frames.session(**dict(effects="full") | overrides)
    current.set_scheme(HoldControls(), True)
    current.audio.enabled = True  # sound is on for a new player (the SOUND setting's default)
    if game is not None:
        frames.playing(current, game)
    return current


class Screen:
    """A Painter drawing one session into a CellBuffer as the Renderer does: one Painter and one flash limiter for
    the whole clip, on a link with nothing in the way (spec 2.8: local, not congested, acks)."""

    def __init__(self, cols: int, rows: int, theme, options):
        self.buf = CellBuffer(cols, rows)
        self.painter = Painter(self.buf, options, theme, None)
        self.painter.limiter, self.painter.link = fx.FlashLimiter(), fx.Link()

    def draw(self, current) -> CellBuffer:
        self.painter.draw(current)
        return self.buf

    def frame(self) -> tuple[int, int, int, int]:
        """Where the last scene drew: left, top, columns and rows (a card's 60 x 24, or the arena's whole block)."""
        painter = self.painter
        return painter.left, painter.top, painter.width, painter.height


def replay(name: str):
    """A golden replay's tape and a new game of its ruleset."""
    playback = Playback(FIXTURES / name)
    (tape,) = playback.tapes
    return tape, game_class(playback.ruleset)(tape.config)


def fly(game, tape, until: int) -> None:
    while game.tick < until:
        game.step(tape.actions_at(game.tick))


@dataclass(frozen=True)
class Clip:
    """An animation: ticks [start, end) of a golden replay at cols x rows, a frame every EVERY ticks."""

    name: str
    replay: str
    start: int
    end: int
    cols: int
    rows: int
    cell: Cell
    options: dict = field(default_factory=dict)

    def frames(self):
        """(CellBuffer, theme) for each frame, the buffer reused."""
        tape, game = replay(self.replay)
        fly(game, tape, self.start)
        current = session(game, **self.options)
        theme = truecolor()  # an Arcade game keeps the default palette's 1978 colours (Renderer.arcade_theme)
        screen = Screen(self.cols, self.rows, theme, current.options)
        while game.tick < self.end:
            yield screen.draw(current), theme
            for _ in range(EVERY):
                game.step(tape.actions_at(game.tick))


CLIPS = (
    Clip("hero.gif", "golden-classic-normal.json", 68520, 69120, 120, 44, PLAY),
    Clip("boss.gif", "golden-daily.json", 116620, 117340, 120, 44, PLAY, dict(mode="daily")),
    Clip("arcade.gif", "golden-arcade.json", 5370, 5910, 224, 128, SMALL, dict(mode="arcade")),
)
PALETTE_TICK = 33430  # palettes.png: wave 8 of golden-classic-normal, with every sprite colour on the field


def title():
    return session(quickstart=False)


def upgrade():
    return session(frames.snapshot("card"))


def results():
    """golden-classic-normal's run to its end (frames.snapshot("end"): the tape, then no input until the run is
    lost) and the Session's step that shows its results card."""
    current = session(frames.snapshot("end"))
    current.step(0)
    return current


def settings():
    current = session(quickstart=False, attract="auto")
    current.open_settings()
    return current


COLS, ROWS = 120, 44  # the screen of everything but the Arcade
# (file, the cell, the session, whether it is a card): a card is drawn on the 120 x 44 screen of the animations and
# shown alone, the 60 x 24 the game draws it in, so SETTINGS names the scale that screen plays at (AUTO (2X)); an
# 80 x 24 terminal shows the same card between letterbox bands, but AUTO (TEXT) there.
STILLS = (
    ("title.png", CARD, title, True),
    ("upgrade.png", PLAY, upgrade, False),
    ("results.png", CARD, results, True),
    ("settings.png", CARD, settings, True),
)


# --- files ---------------------------------------------------------------------------------------------------------


def crop(buf: CellBuffer, left: int, top: int, cols: int, rows: int) -> CellBuffer:
    """The cols x rows of buf from column left and row top."""
    out = CellBuffer(cols, rows)
    for y in range(rows):
        start = (top + y) * buf.cols + left
        out.chars[y * cols : (y + 1) * cols] = buf.chars[start : start + cols]
        out.styles[y * cols : (y + 1) * cols] = buf.styles[start : start + cols]
    return out


def still(current, cell: Cell, theme=None, card: bool = False):
    """A session's 120 x 44 screen (only the card's frame when card) as an image with its border."""
    theme = theme or truecolor()
    screen = Screen(COLS, ROWS, theme, current.options)
    buf = screen.draw(current)
    if card:
        buf = crop(buf, *screen.frame())
    raster = Raster(cell, theme, buf.cols, buf.rows)
    return framed(raster.render(buf), raster.edge(buf))


def save_png(image, path: Path) -> None:
    image.save(path, optimize=True)


def palettes(path: Path) -> None:
    """The same moment in the four palettes, 2 x 2, each with its name above it."""
    tape, game = replay("golden-classic-normal.json")
    fly(game, tape, PALETTE_TICK)
    panels = []
    for name in ("default", "cvd", "contrast", "mono"):
        current = session(game, palette=name)
        panels.append((PALETTE_LABELS[name], still(current, SMALL, truecolor(name))))
    width, height = panels[0][1].size
    background = truecolor().bg[0]
    sheet = Image.new("RGB", (2 * width + GAP, 2 * (height + GAP)), colour(background, TERMINAL_BG))
    font = ImageFont.truetype(FONTS[True], 14)
    label = colour(truecolor().fg[SLOT["dim"]], TERMINAL_FG)
    for index, (name, image) in enumerate(panels):
        x, y = (index % 2) * (width + GAP), (index // 2) * (height + GAP) + GAP
        sheet.paste(image, (x, y))
        ImageDraw.Draw(sheet).text((x + PAD, y - 4), name, fill=label, font=font, anchor="ls")
    save_png(sheet, path)


def gif(clip: Clip, path: Path, keep: Path | None) -> int:
    """Draw a clip's frames to PNG files (in a temporary folder beside path unless keep), then two ffmpeg passes;
    returns the frame count."""
    with tempfile.TemporaryDirectory(prefix=".make_media-", dir=path.parent) as temp:
        folder = Path(temp) if keep is None else keep / Path(clip.name).stem
        folder.mkdir(parents=True, exist_ok=True)
        raster, count = None, 0
        for count, (buf, theme) in enumerate(clip.frames(), 1):
            if raster is None:  # the first frame's: its border stays for the whole clip
                raster = Raster(clip.cell, theme, buf.cols, buf.rows)
                border = raster.edge(buf)
            framed(raster.render(buf), border).save(folder / f"{count - 1:05d}.png", compress_level=1)
        frames_in = ["-framerate", str(FPS), "-i", str(folder / "%05d.png")]
        palette = Path(temp) / "palette.png"
        ffmpeg = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
        stats = "palettegen=max_colors=256:stats_mode=full:reserve_transparent=0"
        subprocess.run([*ffmpeg, *frames_in, "-vf", stats, str(palette)], check=True)
        use = "[0:v][1:v]paletteuse=dither=none:diff_mode=rectangle"
        subprocess.run([*ffmpeg, *frames_in, "-i", str(palette), "-lavfi", use, "-loop", "0", str(path)], check=True)
    return count


def missing() -> list[str]:
    """What the tool needs and cannot find."""
    needs = []
    if Image is None:
        needs.append("Pillow (pip install Pillow)")
    if shutil.which("ffmpeg") is None:
        needs.append("ffmpeg")
    if Image is not None:
        for name in FONTS:
            try:
                ImageFont.truetype(name, 12)
            except OSError:
                needs.append(f"the font {name}")
    return needs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT, help="the folder to write to (default docs/media)")
    names = [clip.name for clip in CLIPS] + [name for name, *_ in STILLS] + ["palettes.png"]
    parser.add_argument("--only", nargs="+", choices=names, metavar="NAME", help="just these files")
    parser.add_argument("--frames", type=Path, metavar="DIR", help="also keep each animation's frames in DIR")
    args = parser.parse_args(argv)
    needs = missing()
    if needs:
        print(f"make_media.py needs {', '.join(needs)}", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    wanted = set(args.only or names)
    for clip in CLIPS:
        if clip.name in wanted:
            count = gif(clip, args.out / clip.name, args.frames)
            print(f"{clip.name}: {count} frames at {FPS} fps, {(args.out / clip.name).stat().st_size:,} bytes")
    for name, cell, build, card in STILLS:
        if name in wanted:
            save_png(still(build(), cell, card=card), args.out / name)
            print(f"{name}: {(args.out / name).stat().st_size:,} bytes")
    if "palettes.png" in wanted:
        palettes(args.out / "palettes.png")
        print(f"palettes.png: {(args.out / 'palettes.png').stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
