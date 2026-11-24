"""Reservoir selection, as in myMPD's random_select_songs/albums (§1.7).

The selection is a fold: `select_init` builds the accumulator,
`select_step` consumes one candidate (or None for one that failed the
constraints), and `select_result` checks the outcome.
"""

from dataclasses import dataclass, replace

from . import rng
from .constraints import Candidate, UniqContext
from .model import Entry


@dataclass(frozen=True, slots=True)
class Reservoir:
    entries: tuple[Entry, ...]
    initial: int
    expected: int
    lineno: int
    skipno: int
    rng: int
    uniq: UniqContext
    keys: frozenset[str]
    values: frozenset[str]

    @property
    def need(self) -> int:
        return self.expected - self.initial

    @property
    def seen(self) -> int:
        """Candidates consumed so far, plus one (myMPD's lineno+skipno)."""
        return self.lineno + self.skipno


def _index(entries: tuple[Entry, ...]) -> tuple[frozenset[str], ...]:
    return (
        frozenset(e.key for e in entries),
        frozenset(e.tag_value for e in entries),
    )


def select_init(
    existing: tuple[Entry, ...],
    expected: int,
    seed: int,
    uniq: UniqContext,
) -> Reservoir:
    keys, values = _index(existing)
    return Reservoir(
        entries=existing,
        initial=len(existing),
        expected=expected,
        lineno=1,
        skipno=0,
        rng=seed,
        uniq=uniq,
        keys=keys,
        values=values,
    )


def is_uniq(acc: Reservoir, cand: Candidate) -> bool:
    """myMPD's check_uniq_tag: the key and the tag value must not appear in
    the uniqueness context or in the list being built. Empty values are
    compared like any other value."""
    return not (
        cand.key in acc.uniq.keys
        or cand.tag_value in acc.uniq.values
        or cand.key in acc.keys
        or cand.tag_value in acc.values
    )


def select_step(acc: Reservoir, cand: Candidate | None) -> Reservoir:
    if acc.need <= 0:
        return acc
    if cand is None or not is_uniq(acc, cand):
        return replace(acc, skipno=acc.skipno + 1)

    draw, state = rng.randrange(acc.rng, acc.lineno)
    entries = acc.entries
    if draw < acc.need:
        entry = Entry(cand.key, acc.lineno, cand.tag_value)
        if len(entries) < acc.expected:
            entries = entries + (entry,)
        else:
            # Replace a random new entry; pre-existing ones stay untouched.
            pos = acc.initial
            if acc.need > 1:
                offset, state = rng.randrange(state, acc.need)
                pos += offset
            entries = entries[:pos] + (entry,) + entries[pos + 1 :]
    if entries is acc.entries:
        return replace(acc, lineno=acc.lineno + 1, rng=state)
    keys, values = _index(entries)
    return replace(
        acc,
        entries=entries,
        lineno=acc.lineno + 1,
        rng=state,
        keys=keys,
        values=values,
    )


def more_windows(acc: Reservoir, next_start: int) -> bool:
    """Whether to request the next result window: myMPD stops once a
    window came back short (`lineno + skipno > start`)."""
    return acc.need > 0 and acc.seen > next_start


@dataclass(frozen=True, slots=True)
class Filled:
    entries: tuple[Entry, ...]


@dataclass(frozen=True, slots=True)
class Short:
    found: int
    expected: int


def select_result(acc: Reservoir) -> Filled | Short:
    """A short list is a failure, as in myMPD."""
    if len(acc.entries) < acc.expected:
        return Short(len(acc.entries), acc.expected)
    return Filled(acc.entries)
