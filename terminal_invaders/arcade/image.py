"""The 8 KB program image the port reads its data from: zeros, the ROM's tables (tables.py) and art.py's pictures
(our own, but for the few art.py marks as the ROM's because they decide play). The ROM image file is never read."""

from .art import SHIP_ART
from .memory import ROM_SIZE
from .tables import TABLES


def build_image(art: dict[int, bytes] | None = None) -> bytes:
    """Zeros except TABLES and the art (default art.SHIP_ART), each at its ROM address.

    Raises ValueError if an entry runs outside 0000-1FFF or two entries share a byte."""
    if art is None:
        art = SHIP_ART
    entries = sorted(
        [(addr, len(data), "TABLES", data) for addr, data in TABLES.items()]
        + [(addr, len(data), "art", data) for addr, data in art.items()]
    )
    image = bytearray(ROM_SIZE)
    last_end, last = 0, (0, "")
    for addr, size, owner, data in entries:
        if addr < 0 or addr + size > ROM_SIZE:
            raise ValueError(f"{owner} entry {addr:04X} (+{size:X}) runs outside the image")
        if size and addr < last_end:
            raise ValueError(f"{owner} entry {addr:04X} (+{size:X}) overlaps {last[1]} entry {last[0]:04X}")
        if addr + size > last_end:
            last_end, last = addr + size, (addr, owner)
        image[addr : addr + size] = data
    return bytes(image)
