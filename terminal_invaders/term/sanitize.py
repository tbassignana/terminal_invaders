"""Make any text safe to print one code point per cell: no controls, no invisible or combining marks, no wide glyphs."""

import unicodedata
from functools import lru_cache

# C0/C1 controls, bidi and zero-width formats, surrogates, private use, unassigned, line/paragraph separators, marks.
DROPPED = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Mn", "Mc", "Me"})
SPACES = str.maketrans("\t\r\n", "   ")


def sanitize(text: str, *, ambiguous_narrow: bool = True) -> str:
    """Tab, CR and LF become spaces; dropped categories vanish; wide (and, unless narrow, ambiguous) become "?"."""
    if text.isascii() and text.isprintable():
        return text
    return _slow(text, ambiguous_narrow)


@lru_cache(4096)
def _slow(text: str, ambiguous_narrow: bool) -> str:
    out = []
    for char in text.translate(SPACES):
        if unicodedata.category(char) in DROPPED:
            continue
        width = unicodedata.east_asian_width(char)
        out.append("?" if width in ("W", "F") or width == "A" and not ambiguous_narrow else char)
    return "".join(out)
