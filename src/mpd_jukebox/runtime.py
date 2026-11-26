"""The imperative shell (§5.1): an asyncio loop around `jukebox.update`."""

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any, Protocol

from mpd.asyncio import MPDClient
from mpd.base import CommandError, ProtocolError
from mpd.base import ConnectionError as MpdConnectionError

from . import jukebox as core
from .albums import build_albums
from .config import Settings, fingerprint
from .connection import Connection
from .constraints import (
    SelectContext,
    album_candidate,
    hated_uris,
    history_wanted,
    last_played_map,
    make_context,
    song_candidate,
    uniq_context,
)
from .model import DATABASE, Album, Config, Mode, Song
from .mpd_io import WINDOW, FatalMpdError, MpdIO, check_fatal
from .select import (
    Filled,
    Reservoir,
    more_windows,
    select_init,
    select_result,
    select_step,
)
from .store import Store

log = logging.getLogger(__name__)

IDLE_SUBSYSTEMS = ['playlist', 'player', 'database']
BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 60.0

# Errors that mean the connection is gone; the runtime reconnects.
CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    MpdConnectionError,
    ProtocolError,
    OSError,
    TimeoutError,
)

# Errors a refill does not turn into RefillFailed.
PASS_THROUGH: tuple[type[BaseException], ...] = (
    *CONNECTION_ERRORS,
    FatalMpdError,
)

Connect = Callable[[Connection], Awaitable[Any]]


async def connect_client(connection: Connection) -> Any:
    client = MPDClient()
    await client.connect(connection.host, connection.port)
    return client


def random_seed() -> int:
    return secrets.randbits(64)


class History(Protocol):
    """The part of `Store` a refill reads."""

    async def recent(self, limit: int) -> tuple[str, ...]: ...

    async def history(self) -> tuple[tuple[str, int], ...]: ...


class NoHistory:
    """Used when there is no state database (`select` without one)."""

    async def recent(self, limit: int) -> tuple[str, ...]:
        return ()

    async def history(self) -> tuple[tuple[str, int], ...]:
        return ()


class Refiller:
    """Gathers the inputs for a refill from MPD and the store, then runs the
    pure selection fold over the song or album source (§1.6, §1.7)."""

    def __init__(
        self,
        settings: Settings,
        store: History,
        seed: Callable[[], int] = random_seed,
    ) -> None:
        self.settings = settings
        self.store = store
        self.seed = seed
        self.albums: Mapping[str, Album] | None = None

    async def refill(
        self, io: MpdIO, effect: core.StartRefill, now: int
    ) -> core.RefillDone | core.RefillFailed:
        cfg = self.settings.config
        queue_songs = await io.queue_songs()
        wanted = history_wanted(len(queue_songs), cfg.uniq_range)
        recent = await self.store.recent(wanted) if wanted else ()
        history_songs: list[Song] = []
        for uri in recent:
            song = await io.song(uri)
            if song is not None:
                history_songs.append(song)
        uniq = uniq_context(
            effect.mode,
            cfg.uniq_tag,
            queue_songs + tuple(history_songs),
            cfg.albums,
        )

        stickers = await io.stickers('lastPlayed')
        history = await self.store.history() if stickers is None else ()
        likes = await io.stickers('like') if cfg.ignore_hated else None
        ctx = make_context(
            cfg,
            self.settings.include,
            self.settings.exclude,
            now,
            last_played_map(stickers, history),
            hated_uris(likes, cfg.ignore_hated),
        )
        acc = select_init(effect.existing, effect.expected, self.seed(), uniq)
        if effect.mode is Mode.SONG:
            acc = await self._songs(io, acc, ctx)
        else:
            albums = await self.album_index(io)
            for album in albums.values():
                acc = select_step(acc, album_candidate(ctx, album))

        result = select_result(acc)
        if isinstance(result, Filled):
            return core.RefillDone(result.entries, effect.add, now)
        return core.RefillFailed(
            'Filling jukebox queue failed: found '
            f'{result.found} of {result.expected} entries',
            now,
        )

    async def _songs(
        self, io: MpdIO, acc: Reservoir, ctx: SelectContext
    ) -> Reservoir:
        cfg = self.settings.config
        if cfg.playlist == DATABASE:
            include = self.settings.include

            async def fetch(start: int, end: int) -> tuple[Any, ...]:
                if include is not None:
                    return await io.search(cfg.filter_include, start, end)
                return await io.search_all(start, end)

            windowed = True
        else:
            windowed = io.playlist_window_supported()

            async def fetch(start: int, end: int) -> tuple[Any, ...]:
                if windowed:
                    return await io.playlist(cfg.playlist, start, end)
                return await io.playlist(cfg.playlist)

        start = 0
        while True:
            for song in await fetch(start, start + WINDOW):
                acc = select_step(acc, song_candidate(ctx, song))
            start += WINDOW
            if not windowed or not more_windows(acc, start):
                return acc

    async def album_index(self, io: MpdIO) -> Mapping[str, Album]:
        songs: list[Song] = []
        start = 0
        while True:
            batch = await io.search_all(start, start + WINDOW)
            songs.extend(batch)
            if len(batch) < WINDOW:
                break
            start += WINDOW
        self.albums = build_albums(songs, self.settings.config.albums)
        return self.albums


_STOP = object()


class Runtime:
    def __init__(
        self,
        settings: Settings,
        connection: Connection,
        store: Store,
        *,
        connect: Connect = connect_client,
        clock: Callable[[], float] = time.time,
        seed: Callable[[], int] = random_seed,
        backoff_initial: float = BACKOFF_INITIAL,
        backoff_max: float = BACKOFF_MAX,
    ) -> None:
        self.settings = settings
        self.connection = connection
        self.store = store
        self.refiller = Refiller(settings, store, seed)
        self.state = core.State()
        self._connect = connect
        self._clock = clock
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._events: asyncio.Queue[Any] = asyncio.Queue()
        self._stopping = asyncio.Event()
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._refill_task: asyncio.Task[None] | None = None

    @property
    def cfg(self) -> Config:
        return self.settings.config

    def now(self) -> int:
        return int(self._clock())

    # -- control, safe to call from signal handlers --------------------------

    def request_restart(self) -> None:
        log.info('Restarting the jukebox')
        self._events.put_nowait(core.Restart(self.now()))

    def stop(self) -> None:
        self._stopping.set()
        self._events.put_nowait(_STOP)

    # -- main loop ------------------------------------------------------------

    async def run(self) -> None:
        saved = await self.store.load_queue(fingerprint(self.cfg))
        self.state = core.State(jukebox_queue=saved)
        delay = self._backoff_initial
        while not self._stopping.is_set():
            try:
                client = await self._open()
            except CommandError as e:
                check_fatal(e)
                log.error('MPD error while connecting: %s', e)
            except CONNECTION_ERRORS as e:
                log.warning(
                    'Cannot connect to MPD at %s: %s',
                    self.connection.describe(),
                    e or type(e).__name__,
                )
            else:
                delay = self._backoff_initial
                try:
                    await self._session(client)
                except CONNECTION_ERRORS as e:
                    log.warning(
                        'Lost connection to MPD: %s', e or type(e).__name__
                    )
                except CommandError as e:
                    check_fatal(e)
                    log.error('MPD error, reconnecting: %s', e)
                finally:
                    self._teardown()
                    client.disconnect()
            if self._stopping.is_set():
                break
            try:
                await asyncio.wait_for(self._stopping.wait(), delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, self._backoff_max)

    async def _open(self) -> Any:
        client = await self._connect(self.connection)
        if self.connection.password is not None:
            try:
                await MpdIO(client).password(self.connection.password)
            except BaseException:
                client.disconnect()
                raise
        log.info('Connected to MPD at %s', self.connection.describe())
        return client

    def _drain(self) -> None:
        """Drop events from an earlier connection, keeping restarts."""
        kept = []
        while not self._events.empty():
            item = self._events.get_nowait()
            if isinstance(item, core.Restart) or item is _STOP:
                kept.append(item)
        for item in kept:
            self._events.put_nowait(item)

    async def _session(self, client: Any) -> None:
        io = MpdIO(client)
        self._drain()
        self._spawn(self._idle(client, io))
        self._events.put_nowait(core.Connected(await io.status(), self.now()))
        while True:
            item = await self._events.get()
            if item is _STOP:
                return
            if isinstance(item, BaseException):
                raise item
            await self.handle(io, item)

    async def handle(self, io: MpdIO, event: core.Event) -> None:
        self.state, effects = core.update(self.cfg, self.state, event)
        for effect in effects:
            await self._apply(io, effect)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[None]) -> None:
        """Forward failures of background tasks to the main loop, which
        re-raises them."""
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._events.put_nowait(task.exception())

    def _teardown(self) -> None:
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        for task in tuple(self._tasks):
            task.cancel()
        self._refill_task = None

    # -- event sources --------------------------------------------------------

    async def _idle(self, client: Any, io: MpdIO) -> None:
        async for changed in client.idle(IDLE_SUBSYSTEMS):
            status = await io.status()
            now = self.now()
            if 'playlist' in changed:
                self._events.put_nowait(core.QueueChanged(status, now))
            if 'player' in changed:
                self._events.put_nowait(core.PlayerChanged(status, now))
            if 'database' in changed:
                self._events.put_nowait(core.DatabaseChanged(now))

    def _fire(self, name: str, io: MpdIO) -> None:
        self._timers.pop(name, None)
        self._spawn(self._timer_event(name, io))

    async def _timer_event(self, name: str, io: MpdIO) -> None:
        status = await io.status()
        self._events.put_nowait(core.TimerFired(name, status, self.now()))

    async def _validate(self, io: MpdIO, effect: core.Validate) -> None:
        """Find picked entries that left MPD's database."""
        if effect.mode is Mode.SONG:
            missing = [
                e.key for e in effect.entries if not await io.exists(e.key)
            ]
        else:
            albums = await self.refiller.album_index(io)
            missing = [e.key for e in effect.entries if e.key not in albums]
        self._events.put_nowait(core.Validated(frozenset(missing), self.now()))

    async def _refill(self, io: MpdIO, effect: core.StartRefill) -> None:
        try:
            event = await self.refiller.refill(io, effect, self.now())
        except PASS_THROUGH:
            raise
        except Exception as e:
            log.exception('Refill failed')
            event = core.RefillFailed(str(e) or type(e).__name__, self.now())
        self._events.put_nowait(event)

    # -- effects --------------------------------------------------------------

    async def _apply(self, io: MpdIO, effect: core.Effect) -> None:
        match effect:
            case core.StartRefill():
                assert self._refill_task is None or self._refill_task.done()
                log.info('Filling jukebox queue')
                self._refill_task = self._spawn(self._refill(io, effect))
            case core.Enqueue(_, entries, wanted):
                added = await self._enqueue(io, effect)
                status = await io.status()
                self._events.put_nowait(
                    core.Enqueued(
                        len(entries), added, wanted, status, self.now()
                    )
                )
            case core.Validate():
                self._spawn(self._validate(io, effect))
            case core.Play():
                log.debug('Jukebox: start playback')
                await io.play()
            case core.SetTimer(name, seconds):
                self._cancel_timer(name)
                loop = asyncio.get_running_loop()
                self._timers[name] = loop.call_later(
                    seconds, self._fire, name, io
                )
            case core.CancelTimer(name):
                self._cancel_timer(name)
            case core.RecordPlay(uri, played_at, started_at):
                await self.store.record_play(
                    uri, played_at, self.cfg.last_played_count
                )
                if self.cfg.write_stickers:
                    await io.record_play_stickers(uri, started_at)
            case core.Persist(entries):
                await self.store.save_queue(entries)
            case core.Log(level, message):
                log.log(level, message)

    def _cancel_timer(self, name: str) -> None:
        handle = self._timers.pop(name, None)
        if handle is not None:
            handle.cancel()

    async def _enqueue(self, io: MpdIO, effect: core.Enqueue) -> int:
        if effect.mode is Mode.SONG:
            added = 0
            for entry in effect.entries:
                added += await io.add(entry.key)
            return added
        albums = self.refiller.albums
        keys = {e.key for e in effect.entries}
        if albums is None or not keys <= albums.keys():
            albums = await self.refiller.album_index(io)
        added = 0
        for entry in effect.entries:
            album = albums.get(entry.key)
            if album is None:
                log.warning(
                    'Album %s is no longer in the database, skipping it',
                    entry.tag_value or entry.key,
                )
                continue
            results = [await io.add(uri) for uri in album.uris]
            if any(results):
                log.info('Jukebox adding album: %s', entry.tag_value)
                added += 1
        return added
