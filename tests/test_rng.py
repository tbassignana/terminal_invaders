"""Known-answer vectors pin the replay random stream; any change to it must be deliberate."""

import unittest
from collections import Counter

from terminal_invaders.rng import Rng, splitmix64


class Scripted(Rng):
    """Feeds chosen 32-bit outputs to the derived methods."""

    def __init__(self, outputs):
        self.outputs = iter(outputs)

    def next32(self):
        return next(self.outputs)


class ReferenceTests(unittest.TestCase):
    def test_pcg32_matches_the_reference_demo_stream(self):
        rng = Rng()
        rng._seed(42, 54)  # pcg32-demo: pcg32_srandom_r(&rng, 42u, 54u)
        expected = [0xA15C02B7, 0x7B47F409, 0xBA1D3330, 0x83D2F293, 0xBFA4784B, 0xCBED606E]
        self.assertEqual([rng.next32() for _ in expected], expected)

    def test_splitmix64_matches_the_reference_stream(self):
        state, outputs = 0, []
        for _ in range(3):
            output, state = splitmix64(state)
            outputs.append(output)
        self.assertEqual(outputs, [0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4, 0x06C45D188009454F])

    def test_seeded_known_answers(self):
        vectors = {
            0: ([2422489633, 1176037471, 2405161421, 2938897158], 0.9640662344639521, [3, 6, 4, 2, 3, 5]),
            1: ([1380566492, 26580016, 3885459305, 1473386567], 0.9220211617351939, [3, 2, 6, 6, 2, 4]),
            42: ([3508393247, 2846903365, 3050928809, 2850731726], 0.9619113252720054, [2, 1, 4, 2, 1, 6]),
            10**15: ([1928300832, 2063518300, 1018180097, 370712865], 0.9477823465327997, [5, 2, 1, 3, 3, 3]),
        }
        for seed, (words, real, dice) in vectors.items():
            with self.subTest(seed=seed):
                rng = Rng(seed)
                self.assertEqual([rng.next32() for _ in words], words)
                self.assertEqual(rng.random(), real)
                self.assertEqual([rng.randint(1, 6) for _ in dice], dice)
        self.assertEqual(Rng(-1).getstate(), Rng(2**64 - 1).getstate())


class DerivedValueTests(unittest.TestCase):
    def test_random_uses_53_bits_from_two_outputs(self):
        self.assertEqual(Scripted([0, 0]).random(), 0.0)
        self.assertEqual(Scripted([2**32 - 1, 2**32 - 1]).random(), 1 - 2**-53)
        self.assertEqual(Scripted([1 << 31, 0]).random(), 0.5)

    def test_randint_rejects_the_incomplete_top_block(self):
        self.assertEqual(Scripted([2**32 - 1, 7]).randint(0, 2), 1)
        self.assertEqual(Scripted([2**32 - 2]).randint(0, 2), 2)
        self.assertEqual(Scripted([2**32 - 1]).randint(0, 2**32 - 1), 2**32 - 1)
        self.assertEqual(Scripted([5]).randint(-3, -3), -3)
        for low, high in ((1, 0), (0, 2**32)):
            with self.assertRaises(ValueError):
                Rng().randint(low, high)

    def test_choice_covers_the_sequence_and_rejects_empty(self):
        rng = Rng(3)
        self.assertEqual({rng.choice((150, 250, 400)) for _ in range(200)}, {150, 250, 400})
        with self.assertRaises(IndexError):
            rng.choice(())

    def test_state_is_two_plain_integers_and_round_trips(self):
        rng = Rng(99)
        state = rng.getstate()
        self.assertEqual([type(value) for value in state], [int, int])
        first = [rng.randint(0, 1000) for _ in range(20)]
        rng.setstate(state)
        self.assertEqual([rng.randint(0, 1000) for _ in range(20)], first)
        for bad in ((0, 2), (-1, 1), (2**64, 1), (True, 1), (0, 1.0), (0,)):
            with self.subTest(state=bad), self.assertRaises((ValueError, TypeError)):
                rng.setstate(bad)

    def test_uniformity_sanity(self):
        rng = Rng(7)
        draws = 100_000
        counts = Counter(rng.randint(0, 9) for _ in range(draws))
        chi_square = sum((counts[face] - draws / 10) ** 2 / (draws / 10) for face in range(10))
        self.assertLess(chi_square, 27.9)  # p = 0.001 with nine degrees of freedom
        reals = [rng.random() for _ in range(draws)]
        self.assertTrue(all(0 <= value < 1 for value in reals))
        self.assertAlmostEqual(sum(reals) / draws, 0.5, delta=0.005)
        deciles = Counter(int(value * 10) for value in reals)
        self.assertLess(max(abs(deciles[d] - draws / 10) for d in range(10)), draws / 100)


if __name__ == "__main__":
    unittest.main()
