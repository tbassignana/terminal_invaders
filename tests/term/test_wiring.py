"""Every fixture's Capabilities configure the S0 modules the way the Renderer and Router will (S3, S6).

Output: theme, layout, half-block packer, encoder, frame packet and ack pacer, applied through MiniVT. Input: the
fixture's keyboard protocol and OS repeat timings through Decoder, Keymap and KeyGate.
"""

import re
import unittest

from terminal_invaders.keymap import Keymap
from terminal_invaders.layout import compute_layout
from terminal_invaders.term.cells import A_BOLD, CellBuffer, style
from terminal_invaders.term.decode import Decoder
from terminal_invaders.term.encode import Encoder, ack_tag, packet
from terminal_invaders.term.gate import KeyGate
from terminal_invaders.term.keys import Key
from terminal_invaders.term.pacing import AckPacer
from terminal_invaders.term.pixels import Art, HalfBlockPacker, PixelLayer
from terminal_invaders.term.repeat import RepeatModel
from terminal_invaders.term.simkeys import RepeatProfile, stream
from terminal_invaders.theme import SLOT, resolve_theme
from tests.support.terminals import capabilities, load, names
from tests.support.vt import MiniVT

ALIEN = Art.from_rows([".##.", "####", "#..#"], SLOT["alien"])


def frame(caps, cols=240, rows=70):
    """A letterboxed scene with HUD text and pixel sprites, as the S3 Renderer will build it."""
    theme = resolve_theme(caps)
    layout = compute_layout(cols, rows, max_k=None)
    buf = CellBuffer(cols, rows)
    buf.fill(0, 0, cols, rows, st=style(bg=SLOT["letterbox"]))
    buf.fill(layout.top, layout.left, layout.width, layout.height, st=style(bg=SLOT["bg"]))
    buf.addstr(layout.top, layout.left, "SCORE 000120  WAVE 1", theme.style("accent") | A_BOLD)
    layer = PixelLayer(layout.width, 2 * (layout.height - 4))
    for x in range(0, layer.w - ALIEN.w, 9):
        layer.blit(x, 6, ALIEN)
    layer.dot(layer.w // 2, layer.h - 9, SLOT["player_shot"])
    packer = HalfBlockPacker(theme, mono=theme.tier == "mono", ambiguous_wide=not caps.unicode_blocks)
    packer.pack(layer, buf, layout.top + 2, layout.left)
    return theme, layout, buf


class OutputWiringTests(unittest.TestCase):
    def check_frame(self, caps):
        theme, layout, buf = frame(caps)
        self.assertEqual((theme.tier, theme.name), (caps.color, caps.theme))
        self.assertEqual(theme.paint_background, caps.paint_background)
        self.assertEqual(layout.k, 3)  # pooled 2:1 where half blocks are unsafe
        encoder = Encoder(theme, caps)
        tag = ack_tag(0, buf.cols, buf.rows) if caps.ack == "cpr" else None
        data = packet(encoder.encode(None, buf), caps, tag)
        vt = MiniVT(buf.cols, buf.rows)
        vt.feed(b"\x1b[?7l")  # ENTER turns autowrap off
        vt.feed(data)
        # exact, so half blocks went out only where they are one cell wide and the terminal decodes UTF-8
        self.assertEqual(vt.lines(), [buf.row_text(y) for y in range(buf.rows)])
        self.assertEqual(vt.frames, int(caps.sync))
        self.assertEqual(vt.queries, [caps.ack] if caps.ack else [])
        self.assertEqual(vt.cprs, [tag] if tag else [])  # the reply repeats the frame's tag
        pacer = AckPacer("auto" if caps.ack else "off", tagged=tag is not None)  # no ack: clock pacing (8.3)
        self.assertTrue(pacer.ready(0.0))
        pacer.sent(0.0, len(data), full=encoder.last_full, tag=tag)
        for at in vt.cprs or vt.queries:
            pacer.acked(0.025, at if tag else None)
        self.assertEqual(pacer.srtt, 0.025 if caps.ack else None)
        self.assertFalse(pacer.inflight)

    def test_each_fixture_frame_shows_exactly_and_is_acked_once(self):
        for name in names():
            with self.subTest(name):
                self.check_frame(capabilities(load(name)))

    def test_a_latin1_terminal_that_acks_with_da1(self):
        doc = load("xterm")
        doc["env"]["LANG"] = "en_US.ISO-8859-1"
        doc["replies"] = re.sub("\x1b\\[\\d+;\\d+R|\x1b\\[0n", "", doc["replies"])  # no CPR, no DSR
        caps = capabilities(doc)
        self.assertEqual((caps.utf8, caps.unicode_blocks, caps.ack), (False, False, "da1"))
        self.check_frame(caps)


class InputWiringTests(unittest.TestCase):
    def test_each_fixture_keyboard_moves_then_fires_once(self):
        keymap = Keymap.preset("default")
        holds = [("d", 0.0, 1.5), ("space", 2.0, 2.06)]  # hold right through OS repeats, then tap fire
        for name in names():
            with self.subTest(name):
                doc = load(name)
                caps = capabilities(doc)
                kitty = doc["keys"]["protocol"] == "kitty"
                self.assertEqual(kitty, caps.kitty_flags != 0)  # push_kitty verified flags 1|2|8 (5.4)
                decoder = Decoder(
                    esc_timeout=caps.esc_timeout, string_timeout=caps.string_timeout, kitty=bool(caps.kitty_flags)
                )
                gate = KeyGate(RepeatModel(remote=caps.remote), certain=caps.release_events)
                admitted = []
                for at, data in stream(holds, RepeatProfile(name, **doc["keys"]["os_repeat"]), kitty=kitty):
                    decoder.feed(data, at)
                    for key in decoder.events():
                        self.assertIsInstance(key, Key)  # neither a downgrade nor a stray reply
                        action, kind = keymap.lookup("play", key)
                        ok, edge = gate.admit(key, kind, key.time)
                        if ok:
                            admitted.append((action, key.kind, edge))
                self.assertEqual(admitted[0], ("right", "press", True))
                self.assertEqual(
                    [entry for entry in admitted if entry[0] == "fire" and entry[2]], [("fire", "press", True)]
                )
                if kitty:
                    self.assertEqual(admitted[-1], ("fire", "release", False))
                    self.assertIn(("right", "release", False), admitted)
                else:
                    self.assertGreater(len(admitted), 6)  # the OS repeats of the held key reach the controls
