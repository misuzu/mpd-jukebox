import unittest

from mpd_jukebox import rng
from mpd_jukebox.albums import build_albums
from mpd_jukebox.model import AlbumSettings
from mpd_jukebox.tags import (
    album_key,
    basename,
    canonical_tag,
    merge_tags,
    normalize,
    number,
    tag_value,
)

from .helpers import song


class RngTestCase(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(rng.next_u64(42), rng.next_u64(42))
        self.assertNotEqual(rng.next_u64(42)[0], rng.next_u64(43)[0])

    def test_known_value(self):
        # Reference SplitMix64 output for seed 0.
        self.assertEqual(rng.next_u64(0)[0], 0xE220A8397B1DCDAF)

    def test_randrange_bounds(self):
        state = 7
        seen = set()
        for _ in range(1000):
            value, state = rng.randrange(state, 5)
            seen.add(value)
        self.assertEqual(seen, {0, 1, 2, 3, 4})

    def test_randrange_one(self):
        self.assertEqual(rng.randrange(123, 1)[0], 0)

    def test_randrange_rejects_empty_range(self):
        with self.assertRaises(ValueError):
            rng.randrange(1, 0)


class TagsTestCase(unittest.TestCase):
    def test_canonical_tag(self):
        self.assertEqual(canonical_tag('AlbumArtist'), 'albumartist')
        self.assertIsNone(canonical_tag('nope'))

    def test_normalize(self):
        self.assertEqual(normalize('Björk'), 'bjork')
        self.assertEqual(normalize('ＡＢＣ'), 'abc')
        self.assertEqual(normalize('Straße'), 'strasse')

    def test_tag_value_joins(self):
        s = song('a.mp3', artist=['A', 'B'])
        self.assertEqual(tag_value(s.tags, 'artist', s.uri), 'A, B')
        self.assertEqual(tag_value(s.tags, 'genre', s.uri), '')

    def test_title_fallback(self):
        self.assertEqual(
            tag_value(song('d/x.mp3', title='T').tags, 'title', 'd/x.mp3'),
            'T',
        )
        self.assertEqual(
            tag_value(song('d/x.mp3', name='N').tags, 'title', 'd/x.mp3'),
            'N',
        )
        self.assertEqual(
            tag_value(song('d/x.mp3').tags, 'title', 'd/x.mp3'), 'x.mp3'
        )

    def test_basename(self):
        self.assertEqual(basename('a/b/c.flac'), 'c.flac')
        self.assertEqual(basename('http://x/y/'), 'y')

    def test_number(self):
        self.assertEqual(number('3/12'), 3)
        self.assertEqual(number(' 7 '), 7)
        self.assertEqual(number(None), 0)
        self.assertEqual(number('x'), 0)

    def test_merge_tags(self):
        merged = merge_tags(
            [song('1', genre=['a', 'b']).tags, song('2', genre='b').tags]
        )
        self.assertEqual(merged['genre'], ('a', 'b'))


class AlbumKeyTestCase(unittest.TestCase):
    MBID = '01234567-89ab-cdef-0123-456789abcdef'

    def test_mbid(self):
        tags = song('x', musicbrainz_albumid=self.MBID, album='A').tags
        self.assertEqual(album_key(tags, AlbumSettings()), self.MBID)

    def test_mbid_wrong_length_falls_back(self):
        tags = song('x', musicbrainz_albumid='short', artist='R', album='A')
        key = album_key(tags.tags, AlbumSettings())
        self.assertEqual(
            key,
            album_key(song('y', artist='R', album='A').tags, AlbumSettings()),
        )

    def test_mbid_disabled(self):
        tags = song('x', musicbrainz_albumid=self.MBID, artist='R', album='A')
        self.assertNotEqual(
            album_key(tags.tags, AlbumSettings(use_mbid=False)), self.MBID
        )

    def test_albumartist_preferred(self):
        settings = AlbumSettings()
        a = album_key(
            song('x', albumartist='V', artist='R', album='A').tags, settings
        )
        b = album_key(song('y', artist='V', album='A').tags, settings)
        self.assertEqual(a, b)

    def test_missing_artist_or_album(self):
        settings = AlbumSettings()
        self.assertIsNone(album_key(song('x', album='A').tags, settings))
        self.assertIsNone(album_key(song('x', artist='R').tags, settings))

    def test_unknown_album(self):
        settings = AlbumSettings(unknown=True)
        self.assertIsNotNone(album_key(song('x', artist='R').tags, settings))

    def test_group_tag(self):
        settings = AlbumSettings(group_tag='date')
        a = album_key(
            song('x', artist='R', album='A', date='1').tags, settings
        )
        b = album_key(
            song('y', artist='R', album='A', date='2').tags, settings
        )
        c = album_key(song('z', artist='R', album='A').tags, settings)
        self.assertNotEqual(a, b)
        self.assertEqual(
            c,
            album_key(song('z', artist='R', album='A').tags, AlbumSettings()),
        )


class BuildAlbumsTestCase(unittest.TestCase):
    def test_groups_and_orders(self):
        songs = [
            song('a/2.mp3', artist='R', album='A', track='2', genre='x'),
            song('a/1.mp3', artist='R', album='A', track='1/9', genre='y'),
            song('b/1.mp3', artist='S', album='B', disc='2', track='1'),
            song('b/2.mp3', artist='S', album='B', disc='1', track='5'),
            song('loose.mp3'),
        ]
        albums = build_albums(songs, AlbumSettings())
        self.assertEqual(len(albums), 2)
        self.assertEqual(list(albums), sorted(albums))
        by_first = {a.first_uri: a for a in albums.values()}
        self.assertEqual(by_first['a/1.mp3'].uris, ('a/1.mp3', 'a/2.mp3'))
        self.assertEqual(by_first['a/1.mp3'].tags['genre'], ('y', 'x'))
        self.assertEqual(by_first['b/2.mp3'].uris, ('b/2.mp3', 'b/1.mp3'))

    def test_dates(self):
        from mpd_jukebox.model import Song

        base = song('a', artist='R', album='A')
        songs = [
            Song('a', base.tags, last_modified=5, added=9),
            Song('b', base.tags, last_modified=7, added=3),
        ]
        (album,) = build_albums(songs, AlbumSettings()).values()
        self.assertEqual((album.last_modified, album.added), (7, 3))
