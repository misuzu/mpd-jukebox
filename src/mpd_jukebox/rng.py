"""A pure SplitMix64 pseudo-random number generator (§4.4).

The generator state is a plain integer that callers thread through, so
functions that need randomness stay deterministic for a given seed.
"""

MASK64 = (1 << 64) - 1
GOLDEN_GAMMA = 0x9E3779B97F4A7C15


def next_u64(state: int) -> tuple[int, int]:
    """Return a 64-bit value and the next state."""
    state = (state + GOLDEN_GAMMA) & MASK64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return z ^ (z >> 31), state


def randrange(state: int, n: int) -> tuple[int, int]:
    """Return a value in `[0, n)` and the next state, like myMPD's
    `randrange(0, n)`."""
    if n <= 0:
        raise ValueError('n must be positive')
    value, state = next_u64(state)
    return (value * n) >> 64, state
