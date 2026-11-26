"""The jukebox state machine (§1.3–§1.5, §1.10, §4.2).

`update(cfg, state, event)` is the single entry point. It returns the new
state and a tuple of effects for the shell to carry out. It performs no
I/O and reads no clock: the time comes in with every event.
"""

import logging
from dataclasses import dataclass, replace

from .model import Config, Entry, Mode, PlayState, Status

JUKEBOX_TIMER = 'jukebox'
SCROBBLE_TIMER = 'scrobble'
VALIDATE_TIMER = 'validate'

SCROBBLE_TIME_MAX = 240
SCROBBLE_TIME_TOTAL = 480


# --------------------------------------------------------------------------
# State


@dataclass(frozen=True, slots=True)
class State:
    jukebox_queue: tuple[Entry, ...] = ()
    filling: bool = False
    # An Enqueue is being carried out; no other run may take entries.
    enqueuing: bool = False
    # Failed adds since the last successful one.
    add_failures: int = 0
    # A check of the jukebox queue against MPD's database is running, or
    # another one is due once it (or a refill) finishes.
    validating: bool = False
    revalidate: bool = False
    halted: bool = False
    status: Status | None = None


# --------------------------------------------------------------------------
# Events


@dataclass(frozen=True, slots=True)
class Connected:
    status: Status
    now: int


@dataclass(frozen=True, slots=True)
class QueueChanged:
    status: Status
    now: int


@dataclass(frozen=True, slots=True)
class PlayerChanged:
    status: Status
    now: int


@dataclass(frozen=True, slots=True)
class TimerFired:
    name: str
    status: Status
    now: int


@dataclass(frozen=True, slots=True)
class RefillDone:
    entries: tuple[Entry, ...]
    add: int
    now: int


@dataclass(frozen=True, slots=True)
class RefillFailed:
    error: str
    now: int


@dataclass(frozen=True, slots=True)
class Enqueued:
    """Result of an Enqueue: `added` of the `attempted` entries made it
    into MPD's queue; `wanted` is the Enqueue's."""

    attempted: int
    added: int
    wanted: int
    status: Status
    now: int


@dataclass(frozen=True, slots=True)
class DatabaseChanged:
    """MPD's database changed: picked entries may no longer exist."""

    now: int


@dataclass(frozen=True, slots=True)
class Validated:
    """Result of a Validate: the keys that are not in MPD's database."""

    missing: frozenset[str]
    now: int


@dataclass(frozen=True, slots=True)
class Restart:
    now: int


Event = (
    Connected
    | QueueChanged
    | PlayerChanged
    | TimerFired
    | RefillDone
    | RefillFailed
    | Enqueued
    | DatabaseChanged
    | Validated
    | Restart
)


# --------------------------------------------------------------------------
# Effects


@dataclass(frozen=True, slots=True)
class StartRefill:
    """Fill the jukebox queue to `expected` entries, keeping `existing`;
    then move `add` entries to MPD."""

    mode: Mode
    existing: tuple[Entry, ...]
    expected: int
    add: int


@dataclass(frozen=True, slots=True)
class Enqueue:
    """Add the entries to MPD's queue; `wanted` of them are needed (more
    entries follow if some fail, as in myMPD's jukebox_add_to_queue)."""

    mode: Mode
    entries: tuple[Entry, ...]
    wanted: int


@dataclass(frozen=True, slots=True)
class Validate:
    """Check which entries no longer exist in MPD's database."""

    mode: Mode
    entries: tuple[Entry, ...]


@dataclass(frozen=True, slots=True)
class Play:
    pass


@dataclass(frozen=True, slots=True)
class SetTimer:
    name: str
    seconds: int


@dataclass(frozen=True, slots=True)
class CancelTimer:
    name: str


@dataclass(frozen=True, slots=True)
class RecordPlay:
    uri: str
    played_at: int
    started_at: int


@dataclass(frozen=True, slots=True)
class Persist:
    entries: tuple[Entry, ...]


@dataclass(frozen=True, slots=True)
class Log:
    level: int
    message: str


Effect = (
    StartRefill
    | Enqueue
    | Validate
    | Play
    | SetTimer
    | CancelTimer
    | RecordPlay
    | Persist
    | Log
)

Step = tuple[State, tuple[Effect, ...]]


# --------------------------------------------------------------------------
# Pure helpers


def add_count(cfg: Config, mpd_queue_length: int) -> int | None:
    """How many entries to move to MPD (§1.4); None if the queue is long
    enough. At exactly the target length one entry is added."""
    if mpd_queue_length > cfg.queue_length:
        return None
    missing = cfg.queue_length - mpd_queue_length
    return min(max(missing, 1), cfg.add_max)


def queue_size(cfg: Config, mode: Mode) -> int:
    if mode is Mode.SONG:
        return cfg.queue_length_song
    return cfg.queue_length_album


def needs_topup(cfg: Config, length: int) -> bool:
    if cfg.mode is Mode.SONG:
        return length < cfg.queue_length_song_min
    return length < cfg.queue_length_album_min


def take(
    entries: tuple[Entry, ...], n: int
) -> tuple[tuple[Entry, ...], tuple[Entry, ...]]:
    return entries[:n], entries[n:]


def is_stream(uri: str) -> bool:
    return '://' in uri


def jukebox_timer(cfg: Config, state: State, status: Status) -> Effect:
    """Fire `add_offset` seconds plus crossfade before the song ends."""
    if state.halted or status.state is not PlayState.PLAY:
        return CancelTimer(JUKEBOX_TIMER)
    offset = int(status.duration) - (
        int(status.elapsed) + status.crossfade + cfg.add_offset
    )
    if offset > 0:
        return SetTimer(JUKEBOX_TIMER, offset)
    return CancelTimer(JUKEBOX_TIMER)


def scrobble_timer(status: Status) -> Effect:
    """Half the song, but at most 240 s for songs over 480 s."""
    if status.state is not PlayState.PLAY:
        return CancelTimer(SCROBBLE_TIMER)
    duration = int(status.duration)
    point = (
        SCROBBLE_TIME_MAX if duration > SCROBBLE_TIME_TOTAL else duration // 2
    )
    offset = point - int(status.elapsed)
    if offset > 0:
        return SetTimer(SCROBBLE_TIMER, offset)
    return CancelTimer(SCROBBLE_TIMER)


def _observe(cfg: Config, state: State, status: Status) -> Step:
    state = replace(state, status=status)
    return state, (jukebox_timer(cfg, state, status), scrobble_timer(status))


def _refill(
    cfg: Config, state: State, existing: tuple[Entry, ...], add: int
) -> tuple[State, StartRefill]:
    return replace(state, filling=True), StartRefill(
        cfg.mode, existing, queue_size(cfg, cfg.mode) + add, add
    )


def _enqueue(cfg: Config, state: State, wanted: int) -> Step:
    """Move up to `wanted` entries from the jukebox queue to MPD."""
    taken, rest = take(state.jukebox_queue, wanted)
    state = replace(state, jukebox_queue=rest, enqueuing=True)
    return state, (Enqueue(cfg.mode, taken, wanted), Persist(rest))


def run(cfg: Config, state: State) -> Step:
    """myMPD's jukebox_run (§1.4)."""
    if (
        state.filling
        or state.enqueuing
        or state.halted
        or state.status is None
    ):
        return state, ()
    add = add_count(cfg, state.status.queue_length)
    if add is None:
        return state, ()
    if add > len(state.jukebox_queue):
        state, effect = _refill(cfg, state, state.jukebox_queue, add)
        return state, (effect,)
    return _enqueue(cfg, state, add)


def _validate(cfg: Config, state: State) -> Step:
    """Check the jukebox queue against MPD's database, or note that a check
    is due once the running check or refill is done."""
    if state.filling or state.validating:
        return replace(state, revalidate=True), ()
    state = replace(state, revalidate=False)
    if not state.jukebox_queue:
        return state, ()
    state = replace(state, validating=True)
    return state, (Validate(cfg.mode, state.jukebox_queue),)


def _on_database_changed(cfg: Config, state: State) -> Step:
    """Song mode checks right away. Album mode waits until the database has
    been quiet for `album_check_delay` seconds: its check rescans the whole
    library, and every further change pushes the timer back."""
    if cfg.mode is Mode.ALBUM:
        return state, (SetTimer(VALIDATE_TIMER, cfg.album_check_delay),)
    return _validate(cfg, state)


def _topup(cfg: Config, state: State) -> Step:
    """Refill in the background when the jukebox queue runs low."""
    if (
        state.filling
        or state.enqueuing
        or state.halted
        or not needs_topup(cfg, len(state.jukebox_queue))
    ):
        return state, ()
    state, refill = _refill(cfg, state, state.jukebox_queue, 0)
    return state, (refill,)


# --------------------------------------------------------------------------
# Event handlers


def _on_status(
    cfg: Config, state: State, status: Status, *, trigger: bool
) -> Step:
    state, effects = _observe(cfg, state, status)
    if trigger and status.queue_length < cfg.queue_length:
        state, more = run(cfg, state)
        effects += more
    return state, effects


def _on_timer(cfg: Config, state: State, event: TimerFired) -> Step:
    state, effects = _observe(cfg, state, event.status)
    if event.name == JUKEBOX_TIMER:
        state, more = run(cfg, state)
        return state, effects + more
    if event.name == VALIDATE_TIMER:
        state, more = _validate(cfg, state)
        return state, effects + more
    status = event.status
    uri = status.song_uri
    if status.state is PlayState.PLAY and uri and not is_stream(uri):
        started = event.now - int(status.elapsed)
        effects += (RecordPlay(uri, event.now, started),)
    return state, effects


def _on_refill_done(cfg: Config, state: State, event: RefillDone) -> Step:
    state = replace(state, filling=False, jukebox_queue=event.entries)
    if event.add == 0:
        effects: tuple[Effect, ...] = (Persist(event.entries),)
    else:
        state, effects = _enqueue(cfg, state, event.add)
    if state.revalidate:
        # The database changed while the refill was picking.
        state, more = _validate(cfg, state)
        effects += more
    return state, effects


def _halt(state: State, error: str) -> Step:
    state = replace(state, filling=False, enqueuing=False, halted=True)
    return state, (
        Log(
            logging.ERROR,
            f'Jukebox halted: {error}. Send SIGHUP to restart it.',
        ),
        CancelTimer(JUKEBOX_TIMER),
    )


def _on_refill_failed(state: State, event: RefillFailed) -> Step:
    return _halt(state, event.error)


def _on_enqueued(cfg: Config, state: State, event: Enqueued) -> Step:
    state, effects = _observe(cfg, state, event.status)
    failures = 0 if event.added else state.add_failures + event.attempted
    state = replace(state, enqueuing=False, add_failures=failures)
    if failures >= queue_size(cfg, cfg.mode):
        state, more = _halt(
            state, f"adding to MPD's queue failed {failures} times in a row"
        )
        return state, effects + more
    if event.added and event.status.state is not PlayState.PLAY:
        effects += (Play(),)

    # Entries that failed are replaced by the next ones, as in myMPD.
    missing = event.wanted - event.added
    if missing > 0 and not state.halted and not state.filling:
        if state.jukebox_queue:
            state, more = _enqueue(cfg, state, missing)
        else:
            state, refill = _refill(cfg, state, (), missing)
            more = (refill,)
        return state, effects + more

    state, more = _topup(cfg, state)
    return state, effects + more


def _on_validated(cfg: Config, state: State, event: Validated) -> Step:
    kept = tuple(e for e in state.jukebox_queue if e.key not in event.missing)
    effects: tuple[Effect, ...] = ()
    dropped = len(state.jukebox_queue) - len(kept)
    state = replace(state, validating=False, jukebox_queue=kept)
    if state.filling and event.missing:
        # A refill started during this check works from a snapshot that
        # may still hold the missing entries; RefillDone would bring them
        # back. Check again once it is done.
        state = replace(state, revalidate=True)
    if dropped:
        effects += (
            Log(
                logging.INFO,
                f'Dropped {dropped} picked entries that are no longer in '
                "MPD's database",
            ),
            Persist(kept),
        )
    if state.revalidate:
        state, more = _validate(cfg, state)
        effects += more
    state, more = _topup(cfg, state)
    return state, effects + more


def _on_restart(cfg: Config, state: State) -> Step:
    state = replace(state, halted=False, add_failures=0, jukebox_queue=())
    state, effects = run(cfg, state)
    return state, (Persist(()),) + effects


def update(cfg: Config, state: State, event: Event) -> Step:
    match event:
        case Connected():
            # Work from an earlier connection was abandoned.
            state = replace(
                state, filling=False, enqueuing=False, validating=False
            )
            state, effects = _on_status(cfg, state, event.status, trigger=True)
            # The database may have changed while we were away.
            state, more = _validate(cfg, state)
            return state, effects + more
        case QueueChanged():
            return _on_status(cfg, state, event.status, trigger=True)
        case PlayerChanged():
            return _on_status(cfg, state, event.status, trigger=False)
        case TimerFired():
            return _on_timer(cfg, state, event)
        case RefillDone():
            return _on_refill_done(cfg, state, event)
        case RefillFailed():
            return _on_refill_failed(state, event)
        case Enqueued():
            return _on_enqueued(cfg, state, event)
        case DatabaseChanged():
            return _on_database_changed(cfg, state)
        case Validated():
            return _on_validated(cfg, state, event)
        case Restart():
            return _on_restart(cfg, state)
    raise AssertionError(event)  # pragma: no cover
