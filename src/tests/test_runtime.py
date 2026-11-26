import asyncio
import os
import tempfile
import unittest
from dataclasses import replace

from mpd_jukebox import jukebox as core
from mpd_jukebox.config import Settings, fingerprint, parse_config
from mpd_jukebox.connection import Connection
from mpd_jukebox.model import Entry, Mode
from mpd_jukebox.mpd_io import FatalMpdError, MpdIO
from mpd_jukebox.runtime import Refiller, Runtime
from mpd_jukebox.store import Store, load_migrations

from .fake_mpd import FakeMPD, error, track

TIMEOUT = 5.0


def library(n: int, prefix: str = 's') -> list[dict]:
    return [
        track(f'{prefix}{i}.mp3', artist=f'Artist {prefix}{i}')
        for i in range(n)
    ]


def albums(n: int, tracks: int = 3) -> list[dict]:
    return [
        track(
            f'a{a}/{t}.mp3',
            artist=f'Artist {a}',
            album=f'Album {a}',
            track=str(t),
        )
        for a in range(n)
        for t in range(tracks, 0, -1)
    ]


async def wait_for(predicate, timeout=TIMEOUT):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


class RuntimeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Registered first, so it runs after the runtimes have stopped.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, 'state.sqlite')

    async def store(self) -> Store:
        store = await Store.open(self.path)
        await store.upgrade(load_migrations())
        self.addAsyncCleanup(store.close)
        return store

    async def start(
        self,
        fakes,
        raw=None,
        connection=None,
        store=None,
    ) -> tuple[Runtime, asyncio.Task]:
        """Run a Runtime against FakeMPD instances, one per connect."""
        queue = list(fakes) if isinstance(fakes, list) else [fakes]
        self.connects: list[Connection] = []

        async def connect(conn):
            self.connects.append(conn)
            fake = queue.pop(0) if len(queue) > 1 else queue[0]
            fake.connected = True
            return fake

        runtime = Runtime(
            parse_config(raw or {}),
            connection or Connection(),
            store or await self.store(),
            connect=connect,
            seed=lambda: 1,
            backoff_initial=0.01,
        )
        task = asyncio.create_task(runtime.run())

        async def stop():
            runtime.stop()
            await asyncio.wait_for(task, TIMEOUT)

        self.addAsyncCleanup(stop)
        return runtime, task

    async def test_fills_empty_queue_and_plays(self):
        fake = FakeMPD(library(150))
        runtime, _ = await self.start(fake)
        await wait_for(lambda: fake.state == 'play')
        self.assertEqual(len(fake.queue), 1)
        await wait_for(lambda: len(runtime.state.jukebox_queue) == 100)

        # Consume: the song ends, the jukebox adds the next one.
        fake.finish_song()
        await wait_for(lambda: len(fake.queue) == 1)
        await wait_for(lambda: fake.state == 'play')
        await wait_for(lambda: len(runtime.state.jukebox_queue) == 99)
        artists = {s['artist'] for s in fake.queue}
        self.assertEqual(len(artists), 1)

    async def test_saved_queue_is_reused(self):
        store = await self.store()
        fake = FakeMPD(library(150))
        runtime, task = await self.start(fake, store=store)
        await wait_for(lambda: len(runtime.state.jukebox_queue) == 100)
        runtime.stop()
        await asyncio.wait_for(task, TIMEOUT)
        saved = await store.queue()
        self.assertEqual(saved, runtime.state.jukebox_queue)

        fake2 = FakeMPD(library(150))
        await self.start(fake2, store=store)
        await wait_for(lambda: len(fake2.queue) == 1)
        self.assertEqual(fake2.queue[0]['file'], saved[0].key)
        self.assertNotIn('search', fake2.commands)

    async def test_refill_failure_halts_until_restart(self):
        fake = FakeMPD(library(5))
        runtime, _ = await self.start(fake)
        await wait_for(lambda: runtime.state.halted)
        self.assertIn('found 5 of 101', runtime.state.last_error)
        self.assertEqual(fake.queue, [])

        fake.library = library(150)
        runtime.request_restart()
        await wait_for(lambda: fake.state == 'play')
        self.assertFalse(runtime.state.halted)

    async def test_password_and_reconnect(self):
        first = FakeMPD(library(150), password='pw')
        second = FakeMPD(library(150), password='pw')
        await self.start([first, second], connection=Connection(password='pw'))
        await wait_for(lambda: first.state == 'play')
        self.assertEqual(first.commands[0], 'password')

        first.lose_connection()
        await wait_for(lambda: second.commands[:1] == ['password'])
        self.assertEqual(len(self.connects), 2)
        await wait_for(lambda: second.state == 'play')

    async def test_wrong_password_is_fatal(self):
        fake = FakeMPD(library(10), password='pw')
        runtime = Runtime(
            parse_config({}),
            Connection(password='nope'),
            await self.store(),
            connect=self._connect_to(fake),
        )
        with self.assertRaises(FatalMpdError):
            await asyncio.wait_for(runtime.run(), TIMEOUT)
        self.assertFalse(fake.connected)

    async def test_missing_password_is_fatal(self):
        fake = FakeMPD(library(10), password='pw')
        runtime = Runtime(
            parse_config({}),
            Connection(),
            await self.store(),
            connect=self._connect_to(fake),
        )
        with self.assertRaises(FatalMpdError):
            await asyncio.wait_for(runtime.run(), TIMEOUT)

    async def test_connection_refused_retries(self):
        attempts = []
        fake = FakeMPD(library(150))

        async def connect(conn):
            attempts.append(conn)
            if len(attempts) < 3:
                raise ConnectionRefusedError('refused')
            return fake

        runtime = Runtime(
            parse_config({}),
            Connection(),
            await self.store(),
            connect=connect,
            backoff_initial=0.01,
        )
        task = asyncio.create_task(runtime.run())
        await wait_for(lambda: fake.state == 'play')
        runtime.stop()
        await asyncio.wait_for(task, TIMEOUT)
        self.assertEqual(len(attempts), 3)

    def _connect_to(self, fake):
        async def connect(conn):
            return fake

        return connect

    async def test_album_mode(self):
        fake = FakeMPD(albums(30))
        raw = {'jukebox': {'mode': 'album'}}
        runtime, _ = await self.start(fake, raw)
        await wait_for(lambda: fake.state == 'play')
        uris = fake.uris()
        self.assertEqual(len(uris), 3)
        prefix = uris[0].split('/')[0]
        self.assertEqual(
            uris, [f'{prefix}/1.mp3', f'{prefix}/2.mp3', f'{prefix}/3.mp3']
        )
        await wait_for(lambda: len(runtime.state.jukebox_queue) == 25)

    async def test_album_enqueue_after_restart_rebuilds_index(self):
        store = await self.store()
        raw = {'jukebox': {'mode': 'album'}}
        fake = FakeMPD(albums(30))
        runtime, task = await self.start(fake, raw, store=store)
        await wait_for(lambda: len(runtime.state.jukebox_queue) == 25)
        runtime.stop()
        await asyncio.wait_for(task, TIMEOUT)

        fake2 = FakeMPD(albums(30))
        await self.start(fake2, raw, store=store)
        await wait_for(lambda: len(fake2.queue) == 3)

    async def test_adds_that_keep_failing_halt(self):
        fake = FakeMPD(library(150))
        fake.fail_add = {s['file'] for s in fake.library}
        runtime, _ = await self.start(fake)
        await wait_for(lambda: runtime.state.halted)
        self.assertIn('100 times in a row', runtime.state.last_error)
        self.assertEqual(fake.queue, [])
        self.assertEqual(fake.commands.count('add'), 100)

    async def test_stale_saved_entries_are_skipped_and_dropped(self):
        store = await self.store()
        await store.load_queue(fingerprint(parse_config({}).config))
        saved = (
            Entry('gone0.mp3', 1, 'G0'),
            Entry('s1.mp3', 2, 'Artist s1'),
            Entry('gone1.mp3', 3, 'G1'),
        ) + tuple(Entry(f's{i}.mp3', i, f'Artist s{i}') for i in range(2, 30))
        await store.save_queue(saved)

        fake = FakeMPD(library(150))
        runtime, _ = await self.start(fake, store=store)
        # gone0 fails and is replaced by s1, as in myMPD.
        await wait_for(lambda: fake.uris() == ['s1.mp3'])
        await wait_for(lambda: fake.state == 'play')
        # The check on connect drops gone1 before it is reached.
        await wait_for(
            lambda: all(
                not e.key.startswith('gone')
                for e in runtime.state.jukebox_queue
            )
        )
        self.assertEqual(len(runtime.state.jukebox_queue), 28)
        self.assertEqual(fake.commands.count('add'), 2)

    async def test_database_change_drops_removed_songs(self):
        fake = FakeMPD(library(150))
        runtime, _ = await self.start(fake)
        await wait_for(lambda: len(runtime.state.jukebox_queue) == 100)
        await wait_for(lambda: not runtime.state.validating)
        removed = {e.key for e in runtime.state.jukebox_queue[:5]}
        fake.library = [s for s in fake.library if s['file'] not in removed]
        fake.notify('database')
        await wait_for(lambda: len(runtime.state.jukebox_queue) == 95)
        self.assertFalse(
            removed & {e.key for e in runtime.state.jukebox_queue}
        )
        self.assertEqual(
            {e.key for e in await runtime.store.queue()},
            {e.key for e in runtime.state.jukebox_queue},
        )

    async def test_database_change_in_album_mode(self):
        fake = FakeMPD(albums(30))
        raw = {'jukebox': {'mode': 'album'}}
        runtime, _ = await self.start(fake, raw)
        await wait_for(lambda: len(runtime.state.jukebox_queue) == 25)
        await wait_for(lambda: not runtime.state.validating)
        first = runtime.state.jukebox_queue[0]
        index = runtime.refiller.albums
        assert index is not None
        album = index[first.key]
        fake.library = [s for s in fake.library if s['file'] not in album.uris]
        fake.notify('database')
        await wait_for(
            lambda: (
                first.key not in {e.key for e in runtime.state.jukebox_queue}
            )
        )

    async def test_include_filter_and_playlist_sources(self):
        lib = library(150) + [
            track(f'r{i}.mp3', artist=f'R{i}', genre='Rock')
            for i in range(110)
        ]
        fake = FakeMPD(lib)
        raw = {'jukebox': {'filter_include': "(Genre == 'Rock')"}}
        runtime, _ = await self.start(fake, raw)
        await wait_for(lambda: len(runtime.state.jukebox_queue) == 100)
        self.assertTrue(fake.uris()[0].startswith('r'))

    async def test_playlist_source_old_mpd(self):
        fake = FakeMPD(library(150), version='0.23.5')
        fake.playlists['Favs'] = [f's{i}.mp3' for i in range(120)]
        raw = {'jukebox': {'playlist': 'Favs'}}
        runtime, _ = await self.start(fake, raw)
        await wait_for(lambda: len(runtime.state.jukebox_queue) == 100)

    async def test_mpd_rejecting_the_refill_query_halts(self):
        class Rejecting(FakeMPD):
            async def search(self, *args):
                raise error(2, 'search', 'Unknown filter type')

        fake = Rejecting(library(150))
        runtime, _ = await self.start(fake)
        await wait_for(lambda: runtime.state.halted)
        self.assertIn('Unknown filter type', runtime.state.last_error)

    async def test_mpd_error_in_session_reconnects(self):
        class Flaky(FakeMPD):
            async def status(self):
                raise error(52, 'status', 'system error')

        good = FakeMPD(library(150))
        await self.start([Flaky([]), good])
        await wait_for(lambda: good.state == 'play')
        self.assertEqual(len(self.connects), 2)

    async def test_mpd_error_while_connecting_retries(self):
        class Flaky(FakeMPD):
            async def password(self, password):
                raise error(52, 'password', 'system error')

        good = FakeMPD(library(150), password='pw')
        await self.start(
            [Flaky([]), good], connection=Connection(password='pw')
        )
        await wait_for(lambda: good.state == 'play')

    async def test_playlist_source_windowed(self):
        fake = FakeMPD(library(150))
        fake.playlists['Favs'] = [f's{i}.mp3' for i in range(120)]
        raw = {'jukebox': {'playlist': 'Favs'}}
        runtime, _ = await self.start(fake, raw)
        await wait_for(lambda: len(runtime.state.jukebox_queue) == 100)
        self.assertTrue(
            all(
                e.key in fake.playlists['Favs']
                for e in runtime.state.jukebox_queue
            )
        )


class HandleTestCase(unittest.IsolatedAsyncioTestCase):
    """Effects that are hard to reach through real timers."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = await Store.open(
            os.path.join(self._tmp.name, 'state.sqlite')
        )
        await self.store.upgrade(load_migrations())

    async def asyncTearDown(self):
        await self.store.close()
        self._tmp.cleanup()

    def runtime(self, raw=None) -> Runtime:
        return Runtime(parse_config(raw or {}), Connection(), self.store)

    async def test_scrobble_records_history_and_stickers(self):
        fake = FakeMPD(library(3))
        fake.queue = [{**fake.library[0], 'id': 1}]
        fake.state, fake.pos, fake.elapsed = 'play', 0, 100.0
        runtime = self.runtime(
            {'jukebox': {'advanced': {'write_stickers': True}}}
        )
        io = MpdIO(fake)
        event = core.TimerFired(core.SCROBBLE_TIMER, await io.status(), 1000)
        await runtime.handle(io, event)
        self.assertEqual(await self.store.recent(5), ('s0.mp3',))
        self.assertEqual(fake.stickers['s0.mp3', 'playCount'], '1')
        self.assertEqual(fake.stickers['s0.mp3', 'lastPlayed'], '900')
        await runtime.handle(io, event)
        self.assertEqual(fake.stickers['s0.mp3', 'playCount'], '2')

    async def test_timers(self):
        fake = FakeMPD(library(3))
        runtime = self.runtime()
        io = MpdIO(fake)
        await runtime._apply(io, core.SetTimer('jukebox', 100))
        await runtime._apply(io, core.SetTimer('jukebox', 100))
        self.assertEqual(len(runtime._timers), 1)
        await runtime._apply(io, core.CancelTimer('jukebox'))
        self.assertEqual(runtime._timers, {})
        await runtime._apply(io, core.CancelTimer('jukebox'))

        await runtime._apply(io, core.SetTimer('jukebox', 0))
        await wait_for(lambda: not runtime._events.empty())
        event = runtime._events.get_nowait()
        self.assertIsInstance(event, core.TimerFired)

    async def test_drain_keeps_restarts(self):
        runtime = self.runtime()
        runtime._events.put_nowait(core.QueueChanged(core.Status(), 1))
        runtime._events.put_nowait(core.Restart(2))
        runtime._drain()
        self.assertEqual(runtime._events.get_nowait(), core.Restart(2))
        self.assertTrue(runtime._events.empty())

    async def test_unknown_album_is_skipped(self):
        runtime = self.runtime({'jukebox': {'mode': 'album'}})
        io = MpdIO(FakeMPD(albums(2)))
        index = await runtime.refiller.album_index(io)
        good = next(iter(index))
        effect = core.Enqueue(
            Mode.ALBUM,
            (Entry('missing', 1, 'x'), Entry(good, 2, 'Artist')),
            2,
        )
        with self.assertLogs('mpd_jukebox.runtime', 'WARNING'):
            self.assertEqual(await runtime._enqueue(io, effect), 1)

    async def test_log_effect(self):
        runtime = self.runtime()
        with self.assertLogs('mpd_jukebox.runtime', 'ERROR'):
            await runtime._apply(MpdIO(FakeMPD([])), core.Log(40, 'boom'))


class RefillerTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_history_and_stickers_shape_the_picks(self):
        fake = FakeMPD(library(4))
        fake.queue = [{**fake.library[0], 'id': 1}]
        fake.stickers['s1.mp3', 'lastPlayed'] = '990'
        fake.stickers['s2.mp3', 'like'] = '0'

        class History:
            async def recent(self, limit):
                return ('s3.mp3', 'gone.mp3')

            async def history(self):
                raise AssertionError('stickers are available')

        settings = parse_config({'jukebox': {'ignore_hated': True}})
        refiller = Refiller(settings, History(), seed=lambda: 1)
        effect = core.StartRefill(settings.config.mode, (), 1, 0)
        result = await refiller.refill(MpdIO(fake), effect, 1000)
        # s0 is queued, s1 played recently, s2 hated, s3 in history.
        self.assertIsInstance(result, core.RefillFailed)

    async def test_history_fallback_without_stickers(self):
        fake = FakeMPD(library(3), stickers=False)

        class History:
            async def recent(self, limit):
                return ()

            async def history(self):
                return (('s0.mp3', 999), ('s1.mp3', 999))

        settings = parse_config({})
        refiller = Refiller(settings, History(), seed=lambda: 1)
        effect = core.StartRefill(settings.config.mode, (), 1, 0)
        result = await refiller.refill(MpdIO(fake), effect, 1000)
        assert isinstance(result, core.RefillDone)
        self.assertEqual([e.key for e in result.entries], ['s2.mp3'])

    async def test_windows(self):
        from mpd_jukebox import runtime as runtime_module

        fake = FakeMPD(library(25))
        settings = Settings(config=replace(parse_config({}).config))
        refiller = Refiller(settings, _NoHistory(), seed=lambda: 3)
        old = runtime_module.WINDOW
        runtime_module.WINDOW = 10
        try:
            effect = core.StartRefill(settings.config.mode, (), 20, 0)
            result = await refiller.refill(MpdIO(fake), effect, 1000)
            index = await refiller.album_index(MpdIO(FakeMPD(albums(5))))
        finally:
            runtime_module.WINDOW = old
        assert isinstance(result, core.RefillDone)
        self.assertEqual(len(result.entries), 20)
        self.assertEqual(fake.commands.count('search'), 3)
        self.assertEqual(len(index), 5)


class _NoHistory:
    async def recent(self, limit):
        return ()

    async def history(self):
        return ()
