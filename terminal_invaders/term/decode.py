"""Incremental tty input decoder: legacy keys, kitty key events, terminal replies, focus and paste.

Bytes are processed one at a time, so every state resumes across feed() calls at any byte boundary. No input
raises, and what is held stays bounded: CSI parameters 128 bytes, an incomplete CSI 1024, strings 4096, and
paste content is only counted.
"""

from __future__ import annotations

from collections import deque
from string import hexdigits

from .encode import TAG_SKIP
from .keys import ALT, CTRL, LOCKS, MODS, PRESS, RELEASE, REPEAT, SHIFT, Key, Reply, kitty_name

GROUND, ESC, CSI, CSI_SWALLOW, SS3, CONSOLE, STRING, STRING_ESC, PASTE = range(9)
MAX_CSI, MAX_PENDING, MAX_STRING, MAX_PASTE = 128, 1024, 4096, 1 << 20
STRINGS = {0x5D: "osc", 0x50: "dcs", 0x5F: "apc", 0x5E: "pm", 0x58: "sos"}
PASTE_END = b"\x1b[201~"
CURSOR = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "P": "f1", "Q": "f2", "S": "f4"}
SS3_KEYS = CURSOR | {"R": "f3", "M": "enter"}
CONSOLE_KEYS = {"A": "f1", "B": "f2", "C": "f3", "D": "f4", "E": "f5"}  # the Linux console's ESC [ [ A..E
TILDE = {1: "home", 2: "insert", 3: "delete", 4: "end", 5: "page_up", 6: "page_down", 7: "home", 8: "end"}
TILDE |= dict(zip((11, 12, 13, 14, 15, 17, 18, 19, 20, 21, 23, 24), (f"f{n}" for n in range(1, 13))))
KINDS = {"": PRESS, "1": PRESS, "2": REPEAT, "3": RELEASE}
INTERMEDIATE = " !\"#$%&'()*+,-./"


def _ints(fields: list[str]) -> tuple[int, ...]:
    return tuple(int(field) for field in fields if field)


def _modifiers(field: str) -> tuple[int, str, bool]:
    """'m[:e]' -> (mods without lock bits, kind, whether the event type was reported)."""
    mods, _, event = field.partition(":")
    if event not in KINDS:
        raise ValueError(event)
    return max(0, int(mods or 1) - 1) & ~LOCKS & MODS, KINDS[event], bool(event)


def _rgb(text: bytes) -> tuple[int, int, int] | None:
    """'rgb:r/g/b' with 1 to 4 hex digits per channel, scaled to 8 bits."""
    if not text.startswith(b"rgb:"):
        return None
    parts = text[4:].decode("ascii", "replace").split("/")
    if len(parts) != 3 or not all(0 < len(p) <= 4 and all(c in hexdigits for c in p) for p in parts):
        return None
    r, g, b = (round(int(p, 16) * 255 / (16 ** len(p) - 1)) for p in parts)
    return r, g, b


def _ascii(byte: int) -> tuple[str, int] | None:
    """A single legacy byte as (name, mods); None for bytes that are no key."""
    if byte in (0x0D, 0x0A):
        return "enter", 0
    if byte == 0x09:
        return "tab", 0
    if byte in (0x7F, 0x08):
        return "backspace", 0
    if byte == 0x20:
        return "space", 0
    if 0x01 <= byte <= 0x1A:
        return chr(byte + 0x60), CTRL
    if 0x21 <= byte <= 0x7E:
        char = chr(byte)
        return char.lower(), SHIFT if char.isupper() else 0
    return None


def _unshift(name: str, mods: int) -> tuple[str, int]:
    """Letters are reported lower case with SHIFT, whichever form the terminal sent."""
    if len(name) == 1 and name.isupper() and len(name.lower()) == 1:
        return name.lower(), mods | SHIFT
    return name, mods


class Decoder:
    """Byte state machine; events() hands out decoded Keys and Replies in arrival order."""

    def __init__(self, *, esc_timeout: float, string_timeout: float, kitty: bool = False):
        self.esc_timeout, self.string_timeout = esc_timeout, string_timeout
        self.kitty = kitty
        self.clamped = 0  # CPR replies owed: a terminal shrunk to one row answers them there, where they read like F3
        self.state = GROUND
        self.since = 0.0  # when the pending ESC, sequence or string began
        self.pending = 0  # bytes of the incomplete CSI
        self.buf = bytearray()  # CSI parameters and intermediates, or string content
        self.utf8 = bytearray()
        self.intro, self.overflow = 0, False
        self.pasted, self.match = 0, 0
        self.queue: deque[Key | Reply] = deque()

    def feed(self, data: bytes, now: float) -> None:
        index, end = 0, len(data)
        while index < end:
            state = self.state
            if state == PASTE:
                index = self._paste(data, index, now)
                continue
            byte = data[index]
            index += 1
            if state == GROUND:
                self._ground(byte, now)
            elif state == ESC:
                self._escape(byte, now)
            elif state == STRING:
                self._string(byte, now)
            elif state == STRING_ESC:
                self._string_escape(byte, now)
            elif state == SS3:
                self._ss3(byte, now)
            elif state == CONSOLE:
                self._final(byte, now, CONSOLE_KEYS)
            else:
                self._csi(byte, now)

    def events(self, limit: int = 64) -> list[Key | Reply]:
        """Up to limit events; the rest stays queued for the next call."""
        return [self.queue.popleft() for _ in range(min(limit, len(self.queue)))]

    def deadline(self) -> float | None:
        if self.state == ESC:  # kitty sends Esc as CSI 27 u: an ESC only leads a sequence, which may arrive split
            return self.since + (self.string_timeout if self.kitty else self.esc_timeout)
        if self.state in (GROUND, PASTE):
            return None
        return self.since + self.string_timeout

    def expire(self, now: float) -> None:
        """A lone ESC becomes Escape (dropped in kitty mode); a stalled sequence or string is dropped."""
        deadline = self.deadline()
        if deadline is not None and now >= deadline:
            if self.state == ESC:
                self._lone_escape()
            self.state = GROUND

    # -- states ----------------------------------------------------------------------------------------------
    def _ground(self, byte: int, now: float) -> None:
        if self.utf8:
            if 0x80 <= byte <= 0xBF:
                self.utf8.append(byte)
                if len(self.utf8) == (2 if self.utf8[0] < 0xE0 else 3 if self.utf8[0] < 0xF0 else 4):
                    try:
                        char = self.utf8.decode()
                    except UnicodeDecodeError:  # overlong forms and surrogates
                        char = ""
                    self.utf8.clear()
                    if char and char.isprintable():
                        self._text(char, now)
                return
            self.utf8.clear()  # truncated character: drop it, then read this byte afresh
        if byte == 0x1B:
            self._begin(ESC, now)
        elif 0xC2 <= byte <= 0xF4:
            self.utf8.append(byte)
        elif 0x20 <= byte <= 0x7E:
            self._text(chr(byte), now)
        elif key := _ascii(byte):
            self._key(*key, now)

    def _text(self, char: str, now: float) -> None:
        if self.kitty:  # with flag 8 every key is an escape code: bare text means the protocol was dropped
            self.kitty = False
            self.queue.append(Reply("downgrade", None, now))
        if char == " ":
            self._key("space", 0, now)
        else:
            self._key(*_unshift(char, 0), now)

    def _escape(self, byte: int, now: float) -> None:
        if byte == 0x5B:
            self.state, self.pending = CSI, 0
            self.buf.clear()
        elif byte == 0x4F:
            self.state, self.overflow = SS3, False
            self.buf.clear()
        elif byte in STRINGS:
            self.state, self.intro, self.overflow = STRING, byte, False
            self.buf.clear()
        elif byte == 0x1B:
            self._lone_escape()
            self.since = now
        elif byte < 0x80 and (key := _ascii(byte)):
            self.state = GROUND
            self._key(key[0], key[1] | ALT, now)
        else:
            self._lone_escape()
            self.state = GROUND
            self._ground(byte, now)

    def _csi(self, byte: int, now: float) -> None:
        self.pending += 1
        if self.pending > MAX_PENDING:
            self.state, self.pending = GROUND, 0
        elif byte == 0x5B and self.pending == 1:  # ESC [ [: a Linux console F-key, whose letter is no key of its own
            self.state = CONSOLE
        elif 0x40 <= byte <= 0x7E:
            swallowed, self.state = self.state == CSI_SWALLOW, GROUND
            if not swallowed:
                self._dispatch(bytes(self.buf), chr(byte), now)
        elif byte in (0x18, 0x1A):
            self.state = GROUND
        elif byte == 0x1B:
            self._begin(ESC, now)
        elif 0x20 <= byte <= 0x3F and self.state == CSI:
            if len(self.buf) < MAX_CSI:
                self.buf.append(byte)
            else:
                self.state = CSI_SWALLOW
        # other C0 bytes, DEL and 8-bit bytes inside a CSI are ignored

    def _ss3(self, byte: int, now: float) -> None:
        """ESC O [m] final: a key, with modifiers m as some terminals send modified F1-F4 (ESC O 2 R is Shift+F3), or
        nothing. With a parameter and any other final, the whole sequence is dropped: read as text, ESC O 2 R was a
        Shift+R, which the pause menu takes for restart."""
        if 0x30 <= byte <= 0x39:
            if len(self.buf) < 3:
                self.buf.append(byte)
            else:
                self.overflow = True
        elif not self.buf:
            self._final(byte, now, SS3_KEYS)
        else:
            self.state = GROUND
            if byte == 0x1B:
                self._begin(ESC, now)
            elif not self.overflow and 0x40 <= byte <= 0x7E and (name := SS3_KEYS.get(chr(byte))):
                self.queue.append(self._legacy_key(name, self.buf.decode(), now))

    def _final(self, byte: int, now: float, keys: dict[str, str]) -> None:
        """The one byte that ends ESC O or ESC [ [: a key, or nothing."""
        self.state = GROUND
        if byte == 0x1B:
            self._begin(ESC, now)
        elif name := keys.get(chr(byte)):
            self._key(name, 0, now)

    def _string(self, byte: int, now: float) -> None:
        if byte == 0x1B:
            self.state = STRING_ESC
        elif byte == 0x07 and self.intro == 0x5D:
            self._string_done(now)
        elif self.overflow or len(self.buf) >= MAX_STRING:  # too long for any reply: swallow to the terminator
            self.overflow = True
            self.buf.clear()
        else:
            self.buf.append(byte)

    def _string_escape(self, byte: int, now: float) -> None:
        if byte == 0x5C:
            self._string_done(now)
            return
        self._begin(ESC, now)  # an ESC that is not ST aborts the string and leads a new sequence, never a key
        if byte != 0x1B:
            self._escape(byte, now)

    def _paste(self, data: bytes, index: int, now: float) -> int:
        """Count pasted bytes up to the end marker, which may be split across feeds; returns the next index."""
        while index < len(data):
            if data[index] == PASTE_END[self.match]:
                self.match, index = self.match + 1, index + 1
                if self.match == len(PASTE_END):
                    self._paste_done(now)
                    return index
                continue
            if self.match:  # a broken end marker was content; the byte is read afresh
                self.pasted, self.match = self.pasted + self.match, 0
            else:
                stop = data.find(0x1B, index)
                take = min((len(data) if stop < 0 else stop) - index, MAX_PASTE + 1 - self.pasted)
                self.pasted, index = self.pasted + take, index + take
            if self.pasted > MAX_PASTE:
                self._paste_done(now)
                return index
        return index

    # -- events ----------------------------------------------------------------------------------------------
    def _begin(self, state: int, now: float) -> None:
        self.state, self.since = state, now

    def _key(self, name: str, mods: int, now: float) -> None:
        self.queue.append(Key(name, mods, time=now))

    def _lone_escape(self) -> None:
        if not self.kitty:  # kitty sends Escape as CSI 27 u, so a bare ESC is only a sequence lead
            self._key("escape", 0, self.since)

    def _paste_done(self, now: float) -> None:
        self.queue.append(Reply("paste", min(self.pasted, MAX_PASTE), now))
        self.state, self.pasted, self.match = GROUND, 0, 0

    def _string_done(self, now: float) -> None:
        self.state = GROUND
        if self.overflow:
            return
        content = bytes(self.buf)
        rgb = _rgb(content[3:]) if self.intro == 0x5D and content.startswith(b"11;") else None
        if rgb:
            self.queue.append(Reply("osc11", rgb, now))
        elif self.intro == 0x50 and content.startswith(b">|"):
            self.queue.append(Reply("xtversion", content[2:].decode("utf-8", "replace"), now))
        else:
            self.queue.append(Reply("unknown", bytes((0x1B, self.intro)) + content, now))

    def _dispatch(self, data: bytes, final: str, now: float) -> None:
        text = data.decode("ascii")
        private = text[0] if text and text[0] in "<=>?" else ""
        params = text[len(private) :].rstrip(INTERMEDIATE)
        inter = text[len(private) + len(params) :]
        try:
            event = self._sequence(private, params, inter, final, now)
        except (ValueError, IndexError):
            event = Reply("unknown")
        if isinstance(event, Reply) and event.kind == "unknown":
            event = Reply("unknown", b"\x1b[" + data + final.encode(), now)
        if event is not None:
            self.queue.append(event)

    def _sequence(self, private: str, params: str, inter: str, final: str, now: float) -> Key | Reply | None:
        """One complete CSI; None when it decodes to nothing (a dropped key, or the start of a paste)."""
        fields = params.split(";") if params else []
        if any(c in INTERMEDIATE for c in params):
            return Reply("unknown")
        if private == "?" and inter == "$" and final == "y" and len(fields) == 2:
            return Reply("decrpm", (int(fields[0]), int(fields[1])), now)
        if inter:
            return Reply("unknown")
        if private == "?" and final in "uc":
            return Reply("kitty_flags", int(params), now) if final == "u" else Reply("da1", _ints(fields), now)
        if private == ">" and final == "c":
            return Reply("da2", _ints(fields), now)
        if private:
            return Reply("unknown")
        if final == "n" and params in ("0", "3"):
            return Reply("dsr", int(params), now)
        if final in "IO" and not params:
            return Reply("focus", final == "I", now)
        if final == "R":
            return self._cpr_or_f3(fields, now)
        if final == "~":
            return self._tilde(fields, now)
        if final == "u":
            return self._kitty(fields, now)
        if final in CURSOR and len(fields) <= 2 and (not fields or fields[0] in ("", "1")):
            return self._legacy_key(CURSOR[final], fields[1] if len(fields) == 2 else "", now)
        if final == "Z" and not params:
            return Key("tab", SHIFT, time=now)
        return Reply("unknown")

    def _legacy_key(self, name: str, field: str, now: float) -> Key:
        mods, kind, reported = _modifiers(field)
        return Key(name, mods, kind, None, self.kitty or reported, now)

    def _cpr_or_f3(self, fields: list[str], now: float) -> Key | Reply:
        """CSI row;col R is a CPR from row 2 down. On row 1 it reads like F3 with modifiers (CSI 1;m R, m from 2 to
        16), so no query parks there; but there it is a CPR in kitty mode, where F3 is CSI 13 ~, and while CPR replies
        are owed (clamped, which the loop sets before each feed), as a terminal shrunk to one row clamps them onto it.
        They keep their tag's column, which is never 2 to 16 (encode.TAG_SKIP), so a modified F3 key pressed meanwhile
        is still a key. Only a screen of one row and fewer than 17 columns, too small to play, clamps a reply into
        those columns, where it reads as F3: its frame left the window when the game learned that size, as no loss,
        but its query stays owed until a later reply names a frame, or for LATE_KEEP s."""
        row = int(fields[0]) if len(fields) == 2 else 0
        tag_column = row == 1 and fields[1].isdigit() and int(fields[1]) not in TAG_SKIP
        if row >= 2 or (row == 1 and (self.kitty or (self.clamped and tag_column))):
            self.clamped = max(0, self.clamped - 1)  # one of the replies owed, whichever row it names
            return Reply("cpr", (row, int(fields[1])), now)
        if not fields or (len(fields) == 2 and fields[0] == "1"):
            return self._legacy_key("f3", fields[1] if fields else "", now)
        return Reply("unknown")

    def _tilde(self, fields: list[str], now: float) -> Key | Reply | None:
        if fields == ["200"]:
            self.state, self.pasted, self.match = PASTE, 0, 0
            return None
        if len(fields) == 3 and fields[0] == "27":  # xterm modifyOtherKeys: CSI 27 ; m ; code ~
            name = kitty_name(int(fields[2]))
            if name is None:
                return None
            mods, _, _ = _modifiers(fields[1])
            return Key(*_unshift(name, mods), time=now)
        if len(fields) <= 2 and (name := TILDE.get(int(fields[0]))):
            return self._legacy_key(name, fields[1] if len(fields) == 2 else "", now)
        return Reply("unknown")

    def _kitty(self, fields: list[str], now: float) -> Key | None:
        """CSI code[:shifted[:base]] [; mods[:event]] [; text] u; the text field is never requested."""
        if not 1 <= len(fields) <= 3:
            raise ValueError(fields)
        codes = fields[0].split(":")
        name = kitty_name(int(codes[0]))
        base = kitty_name(int(codes[2])) if len(codes) > 2 and codes[2] else None
        mods, kind, reported = _modifiers(fields[1] if len(fields) > 1 else "")
        if name is None:
            return None
        name, mods = _unshift(name, mods)
        return Key(name, mods, kind, base and _unshift(base, 0)[0], self.kitty or reported, now)
