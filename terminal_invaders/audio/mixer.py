"""The helper's mixer: effects synthesized at start in integers, and four voices taken by rank (spec §5)."""

from __future__ import annotations

from array import array
from operator import add, itemgetter

from .protocol import BLOCK_SAMPLES, CUES, FX_PEAK, RATE, VOICES

ATTACK = 144  # frames (3 ms) of rising level, so every cue starts at 0 with no click
SILENCE = array("h", bytes(2 * BLOCK_SAMPLES))  # the shared zero block; sinks only read it


def synth(f0: int, f1: int, ms: int) -> array:
    """A square chirp from f0 to f1 Hz lasting ms, its level decaying quadratically from FX_PEAK after a 3 ms attack.

    Integers only (a 32-bit phase accumulator, its increment swept linearly), so every interpreter gives the same
    samples. Returns array("h"), stereo interleaved with both channels equal."""
    n = RATE * ms // 1000
    nn = n * n
    inc = (f0 << 32) // RATE
    step = ((f1 - f0) << 32) // (RATE * n)
    mono = array("h", bytes(2 * n))
    phase = 0
    for i in range(n):
        level = FX_PEAK * (n - i) ** 2 // nn
        if i < ATTACK:
            level = level * i // ATTACK
        mono[i] = -level if phase & 0x80000000 else level
        phase = (phase + inc) & 0xFFFFFFFF
        inc += step
    stereo = array("h", bytes(4 * n))
    stereo[0::2] = stereo[1::2] = mono
    return stereo


class Mixer:
    """Builds each 10 ms block from the voices, one synthesized array per cue rank, over the test tone if there is one.

    Polyphony: VOICES voices; a new cue takes a free one, else replaces the lowest-ranked voice (the oldest among
    equals) when it outranks it, else is dropped."""

    def __init__(self, cues: list[array], tone=None) -> None:
        self.cues = cues
        self.tone = tone  # a steady test signal under the cues (INVADERS_AUDIO_TONE, the dropout check), or None
        self.voices: list[list[int]] = []  # [rank, next sample], oldest first
        self.started: list[int] = []  # ranks of the voices whose first sample is in the last block (the file sink)

    def cue(self, rank: int) -> str | None:
        """Start the cue of this rank; returns its name, or None when every voice outranks it."""
        voices = self.voices
        if len(voices) >= VOICES:
            worst = max(voices, key=itemgetter(0))  # max keeps the first of equals: the oldest of the lowest rank
            if worst[0] <= rank:
                return None
            voices.remove(worst)
        voices.append([rank, 0])
        return CUES[rank][0]

    def cut(self) -> None:
        """Silence every voice at once ("!")."""
        self.voices.clear()

    @property
    def audible(self) -> bool:
        """A voice is playing, or the test tone is."""
        return bool(self.voices) or self.tone is not None

    def block(self) -> array:
        """The next 10 ms: array("h") of BLOCK_SAMPLES samples, the parts summed and clamped only if they overflow."""
        parts = [] if self.tone is None else [self.tone.read()]
        started, kept = [], []
        for voice in self.voices:
            rank, pos = voice
            sound = self.cues[rank]
            part = sound[pos : pos + BLOCK_SAMPLES]
            if len(part) < BLOCK_SAMPLES:
                part += SILENCE[len(part) :]
            if pos == 0:
                started.append(rank)
            voice[1] = pos + BLOCK_SAMPLES
            if voice[1] < len(sound):
                kept.append(voice)
            parts.append(part)
        self.voices, self.started = kept, started
        if not parts:
            return SILENCE
        if len(parts) == 1:
            return parts[0]
        mixed = parts[0]
        for part in parts[1:]:
            mixed = map(add, mixed, part)
        mixed = list(mixed)
        if max(mixed) > 32767 or min(mixed) < -32768:  # a guard only: 4 voices and the test tone peak at 28 000
            mixed = [32767 if x > 32767 else -32768 if x < -32768 else x for x in mixed]
        return array("h", mixed)
