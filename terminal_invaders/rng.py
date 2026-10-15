"""PCG32 random stream, fully specified so replays never depend on the interpreter's own generator.

The generator is O'Neill's PCG32 (pcg32_random_r: 64-bit LCG state, XSH-RR output, 32-bit results).
Rng(seed) takes the seed modulo 2**64 and feeds two SplitMix64 outputs to pcg32_srandom_r as
initstate and initseq. Every derived value below is defined in terms of next32() alone.
"""

MASK32, MASK64 = 2**32 - 1, 2**64 - 1
MULTIPLIER = 6364136223846793005


def splitmix64(state: int) -> tuple[int, int]:
    """Return (output, next state) of Vigna's SplitMix64."""
    state = (state + 0x9E3779B97F4A7C15) & MASK64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return z ^ (z >> 31), state


class Rng:
    """Small deterministic generator; the state is two plain integers (state, increment)."""

    __slots__ = ("_state", "_inc")

    def __init__(self, seed: int = 0):
        initstate, mixer = splitmix64(seed & MASK64)
        initseq, _ = splitmix64(mixer)
        self._seed(initstate, initseq)

    def _seed(self, initstate: int, initseq: int) -> None:
        """pcg32_srandom_r: select stream initseq, then advance past initstate."""
        self._state, self._inc = 0, ((initseq << 1) | 1) & MASK64
        self.next32()
        self._state = (self._state + initstate) & MASK64
        self.next32()

    def next32(self) -> int:
        old = self._state
        self._state = (old * MULTIPLIER + self._inc) & MASK64
        shifted, rotation = (((old >> 18) ^ old) >> 27) & MASK32, old >> 59
        return ((shifted >> rotation) | (shifted << (-rotation & 31))) & MASK32

    def random(self) -> float:
        """53-bit float in [0, 1): 27 high bits from one output, then 26 from the next."""
        high, low = self.next32() >> 5, self.next32() >> 6
        return (high * 67108864 + low) / 9007199254740992

    def randint(self, a: int, b: int) -> int:
        """Unbiased integer in [a, b]: reject outputs from the incomplete top block, then reduce."""
        span = b - a + 1
        if not 1 <= span <= 2**32:
            raise ValueError("randint span must be between 1 and 2**32")
        limit = 2**32 - 2**32 % span
        value = self.next32()
        while value >= limit:
            value = self.next32()
        return a + value % span

    def choice(self, seq):
        if not seq:
            raise IndexError("cannot choose from an empty sequence")
        return seq[self.randint(0, len(seq) - 1)]

    def getstate(self) -> tuple[int, int]:
        return self._state, self._inc

    def setstate(self, state: tuple[int, int]) -> None:
        state, inc = state
        if any(type(value) is not int or not 0 <= value <= MASK64 for value in (state, inc)) or not inc & 1:
            raise ValueError("invalid generator state")
        self._state, self._inc = state, inc
