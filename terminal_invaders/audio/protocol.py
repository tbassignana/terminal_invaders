"""What the game and the audio helper agree on: the sample format, the cue table and the one-byte messages.

Imports nothing, so the helper (python -I -S) and the tests can load it alone.
"""

RATE = 48000  # Hz; the usual output device rate
CHANNELS = 2
BLOCK_FRAMES = 480  # 10 ms
BLOCK_SAMPLES = BLOCK_FRAMES * CHANNELS
BLOCK_BYTES = BLOCK_SAMPLES * 2  # s16le; 1920 < PIPE_BUF, so a block is one atomic pipe write
VOICES = 4
FX_PEAK = 5000  # effects' peak sample, so 4 voices at once cannot clip
STALE = 0.100  # s: a cue byte older than this when the helper reads it is dropped (a late cue is worse than none)
IDLE_AFTER = 2.0  # s of silence after which the helper stops feeding the sink
STATS_EVERY = 2.0  # s between "stats" lines while feeding

# Rank order, highest priority first: (name, start Hz, end Hz, length ms, minimum repeat gap ms).
# The Phase 4 campaign's cues (tell: a telegraph starts; beam: a beam goes live; clear: the wave is clear; phase: a boss
# phase breaks; siren: a boss arrives; pick: an upgrade is taken) sit after ufo and before Arcade's saucer, so only
# saucer and fire move rank, by name and on both sides of the pipe.
# damage and game_over always play: each one's gap is at least its length, so one of each sounds at a time at most,
# and with 4 voices there is always one they outrank.
# Arcade mode's cues approximate the 1978 board's circuits (MAME's mw8080bw_a.cpp) with the same square chirp, and
# play nothing the cabinet mutes (the amplifier bit off, a bit set and cleared within a frame; machine._sound_events):
# - the march notes are its 555 astable charging toward 4.4 V (invaders_thump_555), high (R1 + 75k) x C x
#   ln(2.733 / 1.067) and low 75k x C x ln 2 with C = 0.1 uF and R1 = 40k, 68k, 82k, 100k: 62, 54, 50, 46 Hz, each
#   held for 4 frames;
# - the saucer is the SN76477's VCO (8.2k, 0.1 uF) swept by its 7.2 Hz SLF (120k, 1 uF) from 780 Hz (its floor) to
#   5.3 kHz and mostly below 2 kHz: 780 to 2000 Hz here, sent again every 8 frames (7.5 Hz) while the bit is on and
#   as long, its gap well under that so a late tick never drops a repeat;
# - the cannon's blast is noise through a 2.2 uF one-shot, full for 0.87 s and silent by 1.73 s: 1.5 s here, its gap
#   as long, so the invasion game over's second rise 59 frames later (a faint blip on the cabinet) is dropped, while
#   real blasts come at least 187 frames apart;
# - the saucer's hit is an op-amp VCO warbling at 6.1 Hz between 106 and 1488 Hz while the bit is on (93 frames),
#   then a 0.7 s release: one fall across that range here, sent again every 10 frames (6 Hz) while the bit is on and
#   as long, so it warbles for as long as the bit; the release is left out.
CUES = (
    ("game_over", 330, 35, 400, 500),
    ("damage", 220, 45, 240, 250),
    ("bomb", 95, 25, 320, 400),
    ("victory", 330, 1320, 400, 500),
    ("boss", 90, 330, 300, 400),
    ("blast", 160, 30, 1500, 1500),
    ("march1", 62, 62, 70, 50),
    ("march2", 54, 54, 70, 50),
    ("march3", 50, 50, 70, 50),
    ("march4", 46, 46, 70, 50),
    ("saucer_hit", 1488, 106, 166, 50),
    ("life", 440, 1760, 250, 300),
    ("wave", 220, 880, 220, 400),
    ("pickup", 660, 1320, 140, 150),
    ("shield", 1320, 660, 100, 120),
    ("kill", 320, 65, 100, 80),
    ("hit", 420, 220, 40, 120),
    ("ufo", 180, 480, 200, 400),
    ("tell", 1200, 1500, 40, 250),
    ("beam", 110, 90, 300, 400),
    ("clear", 523, 1046, 260, 400),
    ("phase", 160, 640, 350, 400),
    ("siren", 440, 220, 500, 600),
    ("pick", 880, 1320, 150, 200),
    ("saucer", 780, 2000, 133, 50),
    ("fire", 1040, 260, 60, 120),
)
RANK = {name: rank for rank, (name, *_) in enumerate(CUES)}

# Messages, game to helper: one byte each.
CUE_BASE = 0x40  # CUE_BASE + rank starts that cue
CUT = b"!"  # silence every voice at once
QUIT = b"q"


def cue_byte(name: str) -> bytes:
    return bytes((CUE_BASE + RANK[name],))
