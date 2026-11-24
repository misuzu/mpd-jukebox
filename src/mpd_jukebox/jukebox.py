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

SCROBBLE_TIME_MAX = 240
SCROBBLE_TIME_TOTAL = 480

ADD_FAILED = 'Adding songs from jukebox to queue failed'


# --------------------------------------------------------------------------
# State


@dataclass(frozen=True, slots=True)
class State:
    jukebox_queue: tuple[Entry, ...] = ()
    filling: bool = False
    halted: bool = False
    last_error: str = ''
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
    added: int
    status: Status
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


def run(cfg: Config, state: State) -> Step:
    """myMPD's jukebox_run (§1.4)."""
    if state.filling or state.halted or state.status is None:
        return state, ()
    add = add_count(cfg, state.status.queue_length)
    if add is None:
        return state, ()
    if add > len(state.jukebox_queue):
        state, effect = _refill(cfg, state, state.jukebox_queue, add)
        return state, (effect,)
    taken, rest = take(state.jukebox_queue, add)
    state = replace(state, jukebox_queue=rest)
    return state, (Enqueue(cfg.mode, taken), Persist(rest))


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
    status = event.status
    uri = status.song_uri
    if status.state is PlayState.PLAY and uri and not is_stream(uri):
        started = event.now - int(status.elapsed)
        effects += (RecordPlay(uri, event.now, started),)
    return state, effects


def _on_refill_done(cfg: Config, state: State, event: RefillDone) -> Step:
    state = replace(
        state, filling=False, last_error='', jukebox_queue=event.entries
    )
    if event.add == 0:
        return state, (Persist(event.entries),)
    taken, rest = take(event.entries, event.add)
    state = replace(state, jukebox_queue=rest)
    return state, (Enqueue(cfg.mode, taken), Persist(rest))


def _on_refill_failed(state: State, event: RefillFailed) -> Step:
    state = replace(state, filling=False, halted=True, last_error=event.error)
    return state, (
        Log(
            logging.ERROR,
            f'Jukebox halted: {event.error}. Send SIGHUP to restart it.',
        ),
        CancelTimer(JUKEBOX_TIMER),
    )


def _on_enqueued(cfg: Config, state: State, event: Enqueued) -> Step:
    state, effects = _observe(cfg, state, event.status)
    if event.added == 0:
        state = replace(state, last_error=ADD_FAILED)
        return state, effects + (Log(logging.ERROR, ADD_FAILED),)
    if event.status.state is not PlayState.PLAY:
        effects += (Play(),)
    if (
        not state.filling
        and not state.halted
        and needs_topup(cfg, len(state.jukebox_queue))
    ):
        state, refill = _refill(cfg, state, state.jukebox_queue, 0)
        effects += (refill,)
    return state, effects


def _on_restart(cfg: Config, state: State) -> Step:
    state = replace(state, halted=False, last_error='', jukebox_queue=())
    state, effects = run(cfg, state)
    return state, (Persist(()),) + effects


def update(cfg: Config, state: State, event: Event) -> Step:
    match event:
        case Connected():
            state = replace(state, filling=False)
            return _on_status(cfg, state, event.status, trigger=True)
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
        case Restart():
            return _on_restart(cfg, state)
    raise AssertionError(event)  # pragma: no cover
