"""SGR colour fragments per tier, and the xterm 256-colour cube."""

CUBE_LEVELS = (0, 95, 135, 175, 215, 255)


def fragment(tier: str, value: int | None, *, bg: bool = False) -> str:
    """SGR parameters for one colour; None is the terminal's default colour.

    value is 0xRRGGBB in truecolor, an index in 256, and a foreground code (30-37, 90-97) in 16. Mono has no colour.
    """
    if tier == "mono":
        return ""
    if value is None:
        return "49" if bg else "39"
    if tier == "truecolor":
        return f"{48 if bg else 38};2;{value >> 16 & 255};{value >> 8 & 255};{value & 255}"
    if tier == "256":
        return f"{48 if bg else 38};5;{value}"
    return str(value + 10 if bg else value)


def xterm_rgb(index: int) -> int:
    """0xRRGGBB of a 256-colour index from 16 up (0-15 are the user's own palette)."""
    if index >= 232:
        return (8 + 10 * (index - 232)) * 0x010101
    index -= 16
    r, g, b = CUBE_LEVELS[index // 36], CUBE_LEVELS[index // 6 % 6], CUBE_LEVELS[index % 6]
    return r << 16 | g << 8 | b


def nearest256(rgb: int) -> int:
    """Closest index in 16-255 by RGB distance: the best cube colour or the best grey, whichever is nearer."""
    r, g, b = rgb >> 16 & 255, rgb >> 8 & 255, rgb & 255

    def level(v):
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40

    cube = 16 + 36 * level(r) + 6 * level(g) + level(b)
    grey = 232 + min(23, max(0, (r + g + b) // 3 - 3) // 10)
    return min((cube, grey), key=lambda index: _distance(xterm_rgb(index), r, g, b))


def _distance(rgb: int, r: int, g: int, b: int) -> int:
    return ((rgb >> 16) - r) ** 2 + ((rgb >> 8 & 255) - g) ** 2 + ((rgb & 255) - b) ** 2
