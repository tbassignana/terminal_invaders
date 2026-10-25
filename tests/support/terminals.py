"""The terminal fixtures of spec 5.8: load one, probe it on a FakeTerminal and resolve its Capabilities.

The real run_probe() and push_kitty() read the fixture's replies (5.3, 5.4), and what the probe read is then
checked strictly: every byte decodes to a reply the probe routes, none to a key, and no sequence is left open.
"""

import json
from pathlib import Path

from terminal_invaders.term.caps import Capabilities, EnvHints, Overrides, ProbeReplies, resolve
from terminal_invaders.term.decode import Decoder
from terminal_invaders.term.encode import TAG_FIRST
from terminal_invaders.term.keys import RELEASE, Key, Reply
from terminal_invaders.term.probe import ROUTED, push_kitty, run_probe
from terminal_invaders.term.simkeys import encode
from tests.support.faketerm import FakeTerminal

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "terminals"


def names() -> list[str]:
    return sorted(path.stem for path in FIXTURES.glob("*.json"))


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def taps(doc: dict, *names: str) -> bytes:
    """A press and a release of each key, as the fixture's terminal sends them (legacy terminals send no release)."""
    kitty = doc["keys"]["protocol"] == "kitty"
    return b"".join(encode(Key(name), kitty=kitty) + encode(Key(name, kind=RELEASE), kitty=kitty) for name in names)


def strict(data: bytes) -> None:
    """Fail unless data is nothing but replies the probe routes."""
    decoder = Decoder(esc_timeout=0.05, string_timeout=0.5)
    decoder.feed(data, 0.0)
    if decoder.deadline() is not None:
        raise AssertionError(f"replies end inside a sequence: {data!r}")
    for event in decoder.events(len(data)):
        if not isinstance(event, Reply):
            raise AssertionError(f"reply decoded as a key: {event}")
        cpr = event.kind == "cpr" and event.value[0] < TAG_FIRST  # on a tag's row it would be an ack, not the probe's
        routed = event.kind in ROUTED or cpr or (event.kind, event.value) == ("dsr", 0)
        if not routed and not (event.kind == "decrpm" and event.value[0] == 2026):
            raise AssertionError(f"reply the probe does not route: {event}")


def probe(doc: dict, *, rtt: float = 0.001, keymap=None, overrides=Overrides()) -> tuple[ProbeReplies, int]:
    """The fixture's terminal probed and its kitty push verified, as TerminalSession.enter does: (replies, flags)."""
    fake = FakeTerminal(doc, rtt=rtt)
    io = fake.ops().io()
    replies = run_probe(fake.fd, EnvHints.from_env(doc.get("env", {})), **io)
    strict(replies.raw)
    assert replies.raw == doc["replies"].encode(), "the probe stopped reading before the last reply"
    _, flags, _ = push_kitty(fake.fd, replies, keymap, overrides, 0.25, **io)
    return replies, flags


def parse_replies(raw: str, rtt: float = 0.001) -> ProbeReplies:
    """A reply string as the probe reads it, one round trip after sending."""
    return probe({"replies": raw, "push_reply": None}, rtt=rtt)[0]


def capabilities(doc: dict, overrides: Overrides = Overrides()) -> Capabilities:
    replies, flags = probe(doc, overrides=overrides)
    return resolve(EnvHints.from_env(doc["env"]), replies, overrides, kitty_verified=flags)
