"""The capability probe (spec 5.2 to 5.4): one batched write inside the alt screen, read until the DA1 sentinel.

The fd operations are injected (clock, read, write, select), so tests drive the probe on an in-memory terminal.
"""

import logging

from .caps import EnvHints, Overrides, ProbeReplies
from .decode import Decoder
from .encode import TAG_FIRST
from .keys import Key, Reply
from .output import write_all

# (name, bytes) in sending order. The CPRs sit on rows 2 and 3: a CPR on row 1 reads like Shift+F3 (CSI 1;2R), and
# from row TAG_FIRST down it answers a frame's ack query, a late one when a restart probes again. A terminal that
# shrank to 2 or 3 rows clamps such a late ack onto the probe's rows, but it comes first, as its query went out first:
# so the probe's own are the last two CPRs on those rows.
QUERIES = (
    ("home", b"\x1b[2;1H"),
    ("kitty", b"\x1b[?u"),
    ("decrqm", b"\x1b[?2026$p"),
    ("xtversion", b"\x1b[>0q"),
    ("osc11", b"\x1b]11;?\x1b\\"),
    ("echo", b"\x1b[6n"),  # column 1: nothing printed
    ("width", b"\x1b[3;1H\xe2\x96\x80\x1b[6n"),  # U+2580, then column 2 narrow, 3 ambiguous-wide, else not UTF-8
    ("dsr", b"\x1b[5n"),
    ("da1", b"\x1b[c"),
)
SKIPS = {"apple": frozenset({"kitty", "decrqm", "xtversion"}), "linux": frozenset({"decrqm", "xtversion", "osc11"})}
ECHOABLE = {"kitty": "kitty_flags", "decrqm": "decrpm_2026", "xtversion": "xtversion", "osc11": "osc11"}
CLEAR = b"\x1b[0m\x1b[H\x1b[2J"  # after the read phase: erases anything an echoing terminal printed
ROUTED = ("kitty_flags", "xtversion", "osc11", "da1")  # Reply kinds stored under their own ProbeReplies name


def probe_bytes(env: EnvHints, skip: frozenset[str] = frozenset()) -> bytes:
    """The probe batch for these env hints, without the named queries."""
    if env.term_program == "Apple_Terminal":
        skip |= SKIPS["apple"]
    if env.term == "linux":
        skip |= SKIPS["linux"]
    return b"".join(data for name, data in QUERIES if name not in skip)


def echoed(answered: frozenset[str]) -> frozenset[str]:
    """Queries to skip when re-probing a terminal that echoes queries: the ones it left unanswered."""
    return frozenset(name for name, field in ECHOABLE.items() if field not in answered)


def timeout(env: EnvHints) -> float:
    return 1.0 if env.ssh else 0.25


def read_until(fd, seconds: float, decoder: Decoder, done, *, clock, read, select) -> tuple[list, bytes, float | None]:
    """Decoded events until done(events), the timeout or a hangup: (events, raw bytes, when done)."""
    events, raw, end = [], bytearray(), clock() + seconds
    while (left := end - clock()) > 0:
        if not select([fd], [], [], left)[0]:
            continue
        try:
            data = read(fd, 4096)
        except OSError:  # gone: the next write says so
            break
        if not data:
            break
        raw += data
        now = clock()
        decoder.feed(data, now)
        while batch := decoder.events():
            events += batch
        if done(events):
            return events, bytes(raw), now
    return events, bytes(raw), None


def read_replies(fd, seconds: float, decoder: Decoder, **io) -> tuple[list, bytes, float | None]:
    """Decoded events until a DA1 reply or the timeout: (events, raw bytes, when the DA1 arrived)."""
    return read_until(fd, seconds, decoder, lambda events: any(_da1(event) for event in events), **io)


def _da1(event) -> bool:
    return isinstance(event, Reply) and event.kind == "da1"


def run_probe(fd, env: EnvHints, *, clock, read, write, select, skip: frozenset[str] = frozenset()) -> ProbeReplies:
    """Send the batch and route the replies; keys typed meanwhile become early_keys. No DA1 means silent."""
    io = dict(clock=clock, read=read, select=select)
    seconds = timeout(env)
    write_all(fd, probe_bytes(env, skip), clock() + seconds, clock=clock, write=write, select=select)
    sent = clock()
    decoder = Decoder(esc_timeout=0.15 if env.ssh else 0.05, string_timeout=seconds)
    events, raw, da1 = read_replies(fd, seconds, decoder, **io)
    found, cprs, keys = {}, [], []
    for event in events:
        if isinstance(event, Key):
            keys.append(event)
        elif event.kind in ROUTED:
            found[event.kind] = event.value
        elif event.kind == "decrpm" and event.value[0] == 2026:
            found["decrpm_2026"] = event.value[1]
        elif event.kind == "cpr" and event.value[0] < TAG_FIRST:
            cprs.append(event.value)
        elif event.kind == "dsr" and event.value == 0:
            found["dsr"] = True
        else:
            logging.debug("Probe reply not routed: %r", event)
    found.update(zip(("cpr_echo", "cpr_width"), cprs[-2:]))
    return ProbeReplies(**found, rtt=None if da1 is None else da1 - sent, early_keys=tuple(keys), raw=raw)


def push_kitty(
    fd,
    replies: ProbeReplies,
    keymap,
    overrides: Overrides,
    seconds: float,
    *,
    clock,
    read,
    write,
    select,
    pushed=lambda: None,
):
    """Push kitty flags 1|2|8 (plus 4 for physical bindings) and verify them: (kitty_depth, verified flags, the
    bytes the verification read, for --probe --raw; None when nothing was pushed). pushed() runs once the push is
    written, before the verification is read."""
    if overrides.kitty is False or replies.kitty_flags is None:
        return 0, 0, None
    wanted = 15 if keymap is not None and keymap.has_physical else 11
    write_all(fd, b"\x1b[>%du\x1b[?u\x1b[c" % wanted, clock() + seconds, clock=clock, write=write, select=select)
    pushed()
    decoder = Decoder(esc_timeout=0.05, string_timeout=seconds)
    events, raw, _ = read_replies(fd, seconds, decoder, clock=clock, read=read, select=select)
    flags = next((e.value for e in events if isinstance(e, Reply) and e.kind == "kitty_flags"), None)
    if flags is not None and flags & 11 == 11:
        if wanted & 4 and not flags & 4:
            logging.debug("Physical keys need kitty flag 4; this terminal verified %d", flags)
        return 1, flags, raw
    write_all(fd, b"\x1b[<u", clock() + seconds, clock=clock, write=write, select=select)
    return 0, 0, raw
