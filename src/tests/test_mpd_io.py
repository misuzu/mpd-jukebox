import unittest

from mpd.base import CommandError

from mpd_jukebox.model import PlayState
from mpd_jukebox.mpd_io import (
    FatalMpdError,
    MpdIO,
    parse_time,
    song_from_mpd,
    status_from_mpd,
    stickers_from_mpd,
    supports_playlist_window,
)

from .fake_mpd import FakeMPD, error, track


class ParseTestCase(unittest.TestCase):
    def test_song(self):
        s = song_from_mpd(
            {
                'file': 'a.mp3',
                'artist': ['A', 'B'],
                'title': 'T',
                'duration': '187.5',
                'prio': '3',
                'last-modified': '2024-01-02T03:04:05Z',
                'added': '2024-01-02T03:04:05Z',
                'format': '44100:16:2',
            }
        )
        assert s is not None
        self.assertEqual(s.tags['artist'], ('A', 'B'))
        self.assertNotIn('format', s.tags)
        self.assertEqual(s.duration, 187.5)
        self.assertEqual(s.prio, 3)
        self.assertEqual(s.last_modified, 1704164645)

    def test_song_old_time_and_missing_file(self):
        s = song_from_mpd({'file': 'a', 'time': '120'})
        assert s is not None
        self.assertEqual(s.duration, 120)
        self.assertIsNone(song_from_mpd({'directory': 'x'}))

    def test_parse_time(self):
        self.assertEqual(parse_time('1970-01-01T00:01:00Z'), 60)
        self.assertEqual(parse_time('1970-01-01T00:01:00'), 60)
        self.assertEqual(parse_time('garbage'), 0)
        self.assertEqual(parse_time(['1970-01-01T00:00:01Z']), 1)

    def test_status(self):
        status = status_from_mpd(
            {
                'state': 'play',
                'playlistlength': '4',
                'songid': '7',
                'elapsed': '12.5',
                'duration': '200.1',
                'xfade': '3',
            },
            {'file': 'x.mp3'},
        )
        self.assertEqual(status.state, PlayState.PLAY)
        self.assertEqual(
            (status.queue_length, status.song_id, status.song_uri),
            (4, 7, 'x.mp3'),
        )
        self.assertEqual((status.duration, status.crossfade), (200.1, 3))

    def test_status_old_and_odd(self):
        status = status_from_mpd({'state': '??', 'time': '5:300'}, None)
        self.assertEqual(status.state, PlayState.STOP)
        self.assertEqual(status.duration, 300)
        self.assertIsNone(status.song_uri)

    def test_stickers(self):
        found = stickers_from_mpd(
            [
                {'file': 'a', 'sticker': 'like=0'},
                {'file': 'b', 'sticker': 'other=1'},
                {'file': 'c', 'sticker': 'broken'},
            ],
            'like',
        )
        self.assertEqual(dict(found), {'a': '0'})

    def test_playlist_window_support(self):
        self.assertTrue(supports_playlist_window('0.24.0'))
        self.assertTrue(supports_playlist_window('1.0'))
        self.assertFalse(supports_playlist_window('0.23.16'))
        self.assertFalse(supports_playlist_window(None))
        self.assertFalse(supports_playlist_window('x.y'))


class MpdIOTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_song_lookup(self):
        io = MpdIO(FakeMPD([track('a.mp3')]))
        song = await io.song('a.mp3')
        self.assertIsNotNone(song)
        self.assertIsNone(await io.song('missing.mp3'))

    async def test_exists(self):
        io = MpdIO(FakeMPD([track('a.cue/track0001')]))
        self.assertTrue(await io.exists('a.cue/track0001'))
        self.assertFalse(await io.exists('a.cue/track0002'))

    async def test_exists_errors(self):
        class Broken(FakeMPD):
            async def find(self, tag, value):
                raise error(52, 'find', 'system error')

        with self.assertLogs('mpd_jukebox.mpd_io', 'WARNING'):
            self.assertTrue(await MpdIO(Broken([])).exists('a'))
        with self.assertRaises(FatalMpdError):
            await MpdIO(FakeMPD([], password='pw')).exists('a')

    async def test_stickers_unavailable(self):
        io = MpdIO(FakeMPD([], stickers=False))
        self.assertIsNone(await io.stickers('like'))

    async def test_fatal_errors(self):
        fake = FakeMPD([track('a.mp3')], password='pw')
        io = MpdIO(fake)
        with self.assertRaises(FatalMpdError):
            await io.add('a.mp3')
        with self.assertRaises(FatalMpdError):
            await io.password('wrong')
        with self.assertRaises(FatalMpdError):
            await io.stickers('like')

    async def test_play_error_is_logged(self):
        class Broken(FakeMPD):
            async def play(self):
                raise error(2, 'play', 'bad')

        with self.assertLogs('mpd_jukebox.mpd_io', 'ERROR'):
            await MpdIO(Broken([])).play()

    async def test_sticker_errors(self):
        class Broken(FakeMPD):
            async def sticker_get(self, *args):
                return 'not a number'

            async def sticker_set(self, *args):
                raise error(52, 'sticker', 'disk full')

        with self.assertLogs('mpd_jukebox.mpd_io', 'WARNING'):
            await MpdIO(Broken([])).record_play_stickers('a', 1)

        class Unknown(FakeMPD):
            async def sticker_get(self, *args):
                raise error(5, 'sticker', 'unknown command')

        with self.assertLogs('mpd_jukebox.mpd_io', 'WARNING'):
            await MpdIO(Unknown([])).record_play_stickers('a', 1)

    async def test_other_password_error_propagates(self):
        class Broken(FakeMPD):
            async def password(self, password):
                raise CommandError('weird')

        with self.assertRaises(CommandError):
            await MpdIO(Broken([])).password('x')
