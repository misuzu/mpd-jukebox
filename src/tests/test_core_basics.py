import unittest

from mpd_jukebox import rng
from mpd_jukebox.albums import (
    VARIOUS_ARTISTS,
    album_of,
    album_parts,
    build_albums,
    compilation_keys,
    folder,
)
from mpd_jukebox.model import AlbumSettings, Song
from mpd_jukebox.tags import (
    album_key,
    basename,
    canonical_tag,
    disc_number,
    merge_tags,
    normalize,
    number,
    split_disc,
    tag_value,
)

from .helpers import song

MBID = '01234567-89ab-cdef-0123-456789abcdef'


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


class SplitByDirectoryTestCase(unittest.TestCase):
    SPLIT = AlbumSettings(split_by_directory=True)

    def titled(self, uri: str, title: str, disc: str = '') -> Song:
        if disc:
            return song(uri, artist='A', album='Album', title=title, disc=disc)
        return song(uri, artist='A', album='Album', title=title)

    def parts(self, *songs: Song) -> list[tuple[str, list[str]]]:
        return [
            (home, sorted(s.uri for s in part))
            for home, part in album_parts(songs)
        ]

    def test_one_folder(self):
        self.assertEqual(
            self.parts(
                self.titled('A/Album/01.mp3', 'One'),
                self.titled('A/Album/02.mp3', 'Two'),
            ),
            [('A/Album', ['A/Album/01.mp3', 'A/Album/02.mp3'])],
        )

    def test_disc_folders(self):
        self.assertEqual(
            self.parts(
                self.titled('A/Double/CD1/01.mp3', 'One'),
                self.titled('A/Double/CD2/01.mp3', 'Two'),
            ),
            [('A/Double', ['A/Double/CD1/01.mp3', 'A/Double/CD2/01.mp3'])],
        )

    def test_copies_side_by_side_are_not_discs(self):
        self.assertEqual(
            self.parts(
                self.titled('A/Album (EU)/01.mp3', 'One'),
                self.titled('A/Album (EU)/02.mp3', 'Two'),
                self.titled('A/Album (JP)/01.mp3', 'one'),
                self.titled('A/Album (JP)/02.mp3', 'Two'),
                self.titled('A/Album (JP)/03.mp3', 'Bonus'),
            ),
            [
                (
                    'A/Album (EU)',
                    ['A/Album (EU)/01.mp3', 'A/Album (EU)/02.mp3'],
                ),
                (
                    'A/Album (JP)',
                    [
                        'A/Album (JP)/01.mp3',
                        'A/Album (JP)/02.mp3',
                        'A/Album (JP)/03.mp3',
                    ],
                ),
            ],
        )

    def test_disc_tags_tell_discs_with_shared_titles_apart(self):
        parts = self.parts(
            self.titled('A/Live/CD1/01.mp3', 'Intro', '1'),
            self.titled('A/Live/CD2/01.mp3', 'Intro', '2'),
        )
        self.assertEqual([home for home, _ in parts], ['A/Live'])
        # Without Disc tags on every song, shared titles mean copies.
        parts = self.parts(
            self.titled('A/Live/CD1/01.mp3', 'Intro', '1'),
            self.titled('A/Live/CD2/01.mp3', 'Intro'),
        )
        self.assertEqual(
            [home for home, _ in parts], ['A/Live/CD1', 'A/Live/CD2']
        )

    def test_top_level_folders_are_never_discs(self):
        parts = self.parts(
            self.titled('Inbox/x.mp3', 'One'),
            self.titled('Inbox 2/y.mp3', 'Two'),
        )
        self.assertEqual([home for home, _ in parts], ['Inbox', 'Inbox 2'])

    def test_bonus_disc_in_a_subfolder_joins_the_album(self):
        parts = self.parts(
            self.titled('A/Album/01.mp3', 'One'),
            self.titled('A/Album/Bonus/01.mp3', 'One (Instrumental)'),
            self.titled('A/Album/Extras/Mixes/01.mp3', 'One (Extended)'),
            self.titled('A/Album/Extras/Tracks/01.mp3', 'One (Backing)'),
        )
        self.assertEqual(
            parts,
            [
                (
                    'A/Album',
                    [
                        'A/Album/01.mp3',
                        'A/Album/Bonus/01.mp3',
                        'A/Album/Extras/Mixes/01.mp3',
                        'A/Album/Extras/Tracks/01.mp3',
                    ],
                )
            ],
        )

    def test_copy_in_a_parent_folder_stays_apart(self):
        parts = self.parts(
            self.titled('A/single.mp3', 'One'),
            self.titled('A/Album/01.mp3', 'One'),
            self.titled('A/Album/02.mp3', 'Two'),
        )
        self.assertEqual([home for home, _ in parts], ['A', 'A/Album'])

    def test_library_root_takes_nothing_in(self):
        parts = self.parts(
            self.titled('loose.mp3', 'Other'),
            self.titled('A/Album/01.mp3', 'One'),
        )
        self.assertEqual([home for home, _ in parts], ['', 'A/Album'])

    def tracks(self, *uris: str) -> list[Song]:
        # The track number is the file name.
        return [
            song(uri, artist='A', album='Album', track=uri[-6:-4])
            for uri in uris
        ]

    def test_stray_copies_are_left_out(self):
        songs = self.tracks(
            'Inbox/01.mp3',
            'A/Album/01.mp3',
            'A/Album/02.mp3',
            'Inbox 2/03.mp3',
            'A/Album/04.mp3',
        ) + [song('B/Other/01.mp3', artist='B', album='Other')]
        merged = build_albums(songs, AlbumSettings())
        albums = build_albums(songs, self.SPLIT)
        self.assertEqual(list(albums), list(merged))
        self.assertEqual(
            sorted(a.uris for a in albums.values()),
            [
                ('A/Album/01.mp3', 'A/Album/02.mp3', 'A/Album/04.mp3'),
                ('B/Other/01.mp3',),
            ],
        )

    def test_disc_folders_count_together(self):
        songs = self.tracks(
            'A/Double/CD2/01.mp3',
            'A/Double/CD1/02.mp3',
            'A/Double/CD1/01.mp3',
            'Inbox/01.mp3',
            'Inbox/02.mp3',
        )
        (album,) = build_albums(songs, self.SPLIT).values()
        # Ordered by folder, so discs without Disc tags play in order.
        self.assertEqual(
            album.uris,
            (
                'A/Double/CD1/01.mp3',
                'A/Double/CD1/02.mp3',
                'A/Double/CD2/01.mp3',
            ),
        )

    def test_folder(self):
        self.assertEqual(folder('A/Album/01.mp3'), 'A/Album')
        self.assertEqual(folder('A/Album/track01.mp3'), 'A/Album')
        self.assertEqual(folder('A/Album/image.cue/track0001'), 'A/Album')
        self.assertEqual(folder('A/Album/image.flac/track0012'), 'A/Album')
        self.assertEqual(folder('image.cue/track0001'), '')
        self.assertEqual(folder('loose.mp3'), '')

    def cue(self, *tracks: tuple[str, int]) -> list[Song]:
        return [
            song(
                f'{sheet}/track{n:04}', artist='A', album='Album', track=str(n)
            )
            for sheet, n in tracks
        ]

    def test_cue_sheets_in_disc_folders(self):
        songs = self.cue(
            ('A/Double/Disc 2/image.cue', 1),
            ('A/Double/Disc 1/image.cue', 2),
            ('A/Double/Disc 1/image.cue', 1),
            ('Inbox/image.cue', 1),
            ('Inbox/image.cue', 2),
        )
        (album,) = build_albums(songs, self.SPLIT).values()
        self.assertEqual(
            album.uris,
            (
                'A/Double/Disc 1/image.cue/track0001',
                'A/Double/Disc 1/image.cue/track0002',
                'A/Double/Disc 2/image.cue/track0001',
            ),
        )

    def test_cue_sheets_in_one_folder(self):
        songs = self.cue(
            ('A/Double/CD 2.cue', 1),
            ('A/Double/CD 1.cue', 2),
            ('A/Double/CD 1.cue', 1),
            ('Inbox/x.cue', 1),
        )
        (album,) = build_albums(songs, self.SPLIT).values()
        # One folder, but ordered by sheet, so the discs do not interleave.
        self.assertEqual(
            album.uris,
            (
                'A/Double/CD 1.cue/track0001',
                'A/Double/CD 1.cue/track0002',
                'A/Double/CD 2.cue/track0001',
            ),
        )

    def test_tie_goes_to_the_first_directory(self):
        songs = self.tracks('B/Album/01.mp3', 'A/Album/01.mp3')
        (album,) = build_albums(songs, self.SPLIT).values()
        self.assertEqual(album.uris, ('A/Album/01.mp3',))


class CompilationTestCase(unittest.TestCase):
    COMP = AlbumSettings(compilations=True)

    def track(self, uri: str, artist: str, track: str, **tags: str) -> Song:
        return song(
            uri, 200.0, artist=artist, album='Mix', track=track, **tags
        )

    def test_untagged_compilation_is_one_album(self):
        songs = [
            self.track('C/Mix/01.mp3', 'X', '1'),
            self.track('C/Mix/02.mp3', 'Y feat. Z', '2'),
            self.track('C/Mix/03.mp3', 'X', '3'),
        ]
        keys = compilation_keys(songs, self.COMP)
        various = song('v', albumartist=VARIOUS_ARTISTS, album='Mix')
        self.assertEqual(
            dict(keys),
            dict.fromkeys(
                (s.uri for s in songs), album_key(various.tags, self.COMP)
            ),
        )
        merged = build_albums(songs, AlbumSettings())
        self.assertEqual(len(merged), 2)
        (album,) = build_albums(songs, self.COMP).values()
        self.assertEqual(len(album.uris), 3)
        self.assertEqual(dict(album_of({album.key: album})), dict(keys))

    def test_not_a_compilation(self):
        cases = {
            'one artist': [
                self.track('C/Mix/01.mp3', 'X', '1'),
                self.track('C/Mix/02.mp3', 'X', '2'),
            ],
            'different folders': [
                self.track('C/Mix/01.mp3', 'X', '1'),
                self.track('D/Mix/02.mp3', 'Y', '2'),
            ],
            'a track number missing': [
                self.track('C/Mix/01.mp3', 'X', '1'),
                self.track('C/Mix/02.mp3', 'Y', ''),
            ],
            'a track number twice': [
                self.track('C/Single/01.mp3', 'X', '1'),
                self.track('C/Single/02.mp3', 'Y', '1'),
            ],
            'tagged with AlbumArtist': [
                self.track('C/Mix/01.mp3', 'X', '1', albumartist='VA'),
                self.track('C/Mix/02.mp3', 'Y', '2', albumartist='VA'),
            ],
            'MusicBrainz album IDs': [
                self.track('C/Mix/01.mp3', 'X', '1', musicbrainz_albumid=MBID),
                self.track('C/Mix/02.mp3', 'Y', '2', musicbrainz_albumid=MBID),
            ],
        }
        for name, songs in cases.items():
            with self.subTest(name):
                self.assertEqual(dict(compilation_keys(songs, self.COMP)), {})

    def test_discs_and_group_tag(self):
        songs = [
            self.track('C/Mix/01.mp3', 'X', '1', disc='1', date='2001'),
            self.track('C/Mix/02.mp3', 'Y', '1', disc='2', date='2001'),
            self.track('C/Mix/03.mp3', 'Z', '1', disc='1', date='2002'),
            self.track('C/Mix/04.mp3', 'W', '2', disc='1', date='2002'),
        ]
        settings = AlbumSettings(compilations=True, group_tag='date')
        keys = compilation_keys(songs, settings)
        self.assertEqual(len(keys), 4)
        self.assertEqual(len(set(keys.values())), 2)
        # Without MusicBrainz IDs in use, an ID does not stop detection.
        tagged = [
            self.track('C/Mix/01.mp3', 'X', '1', musicbrainz_albumid=MBID),
            self.track('C/Mix/02.mp3', 'Y', '2'),
        ]
        no_mbid = AlbumSettings(compilations=True, use_mbid=False)
        self.assertEqual(len(compilation_keys(tagged, no_mbid)), 2)
        self.assertEqual(len(compilation_keys(tagged, self.COMP)), 0)

    def test_without_artist_or_album(self):
        songs = [
            song('C/Mix/01.mp3', album='Mix', track='1'),
            song('C/Mix/02.mp3', artist='Y', track='2'),
        ]
        self.assertEqual(dict(compilation_keys(songs, self.COMP)), {})


class MergeDiscsTestCase(unittest.TestCase):
    MERGE = AlbumSettings(merge_discs=True)

    def test_split_disc(self):
        cases = {
            'Album - Disc 2': ('Album', 2),
            'Album -Disc2-': ('Album', 2),
            'Album [Disc 3]': ('Album', 3),
            'Album (CD 2)': ('Album', 2),
            'Album (cd2)': ('Album', 2),
            'Album (Disk 2 of 3)': ('Album', 2),
            'Album CD2': ('Album', 2),
            'Album: CD 2': ('Album', 2),
            'Album DISC 02': ('Album', 2),
            'Album (Deluxe)(Disc 1)': ('Album (Deluxe)', 1),
            'Album Vol. 3 CD2 (Mixed by X)': ('Album Vol. 3 (Mixed by X)', 2),
            'Album CD1 Subtitle (Mixed by X)': ('Album', 1),
            'Album CD2 Bonus Disc': ('Album', 2),
            'Album Disc 01／Side A': ('Album', 1),
            'Album : CD 1 Subtitle': ('Album', 1),
            'Album (CD1 - Subtitle)': ('Album', 1),
            'Album (Deluxe Edition: Disc 2)': ('Album (Deluxe Edition)', 2),
            # Left alone.
            'Album': ('Album', 0),
            'Album Vol. 2': ('Album Vol. 2', 0),
            'Album Part 2': ('Album Part 2', 0),
            'Album (single CD1)': ('Album (single CD1)', 0),
            'Album [ABC CD8]': ('Album [ABC CD8]', 0),
            'Album [ABCD 19]': ('Album [ABCD 19]', 0),
            'CD 1': ('CD 1', 0),
            'Discovery': ('Discovery', 0),
            'Disco 2000': ('Disco 2000', 0),
        }
        for album, expected in cases.items():
            with self.subTest(album):
                self.assertEqual(split_disc(album), expected)

    def test_disc_number(self):
        self.assertEqual(disc_number(song('a', album='A (CD 2)').tags), 2)
        self.assertEqual(
            disc_number(song('a', album='A (CD 2)', disc='1').tags), 2
        )
        self.assertEqual(disc_number(song('a', album='A', disc='3').tags), 3)
        self.assertEqual(disc_number(song('a', disc='3').tags), 3)
        self.assertEqual(disc_number(song('a').tags), 0)

    def test_album_key(self):
        one = song('a', artist='R', album='A (CD 1)').tags
        two = song('b', artist='R', album='A - Disc 2').tags
        self.assertNotEqual(
            album_key(one, AlbumSettings()), album_key(two, AlbumSettings())
        )
        self.assertEqual(
            album_key(one, self.MERGE), album_key(two, self.MERGE)
        )
        self.assertEqual(
            album_key(one, self.MERGE),
            album_key(song('c', artist='R', album='A').tags, AlbumSettings()),
        )

    def test_discs_in_one_folder_play_in_order(self):
        songs = [
            song('A/2-01.mp3', artist='R', album='A (CD 2)', track='1'),
            song('A/1-02.mp3', artist='R', album='A (CD 1)', track='2'),
            song('A/1-01.mp3', artist='R', album='A (CD 1)', track='1'),
        ]
        for settings in (
            self.MERGE,
            AlbumSettings(merge_discs=True, split_by_directory=True),
        ):
            with self.subTest(settings):
                (album,) = build_albums(songs, settings).values()
                self.assertEqual(
                    album.uris, ('A/1-01.mp3', 'A/1-02.mp3', 'A/2-01.mp3')
                )

    def test_compilation_on_discs_in_one_folder(self):
        songs = [
            song('C/1-01.mp3', artist='X', album='Mix CD1', track='1'),
            song('C/1-02.mp3', artist='Y', album='Mix CD1', track='2'),
            song('C/2-01.mp3', artist='Z', album='Mix CD2', track='1'),
        ]
        settings = AlbumSettings(compilations=True, merge_discs=True)
        keys = compilation_keys(songs, settings)
        self.assertEqual(len(set(keys.values())), 1)
        self.assertEqual(len(keys), 3)


class ModelTestCase(unittest.TestCase):
    def test_default_tags_are_empty(self):
        from mpd_jukebox.model import EMPTY_TAGS, Album, Song

        self.assertIs(Song('a.mp3').tags, EMPTY_TAGS)
        self.assertIs(Album('k', 'a.mp3', ('a.mp3',)).tags, EMPTY_TAGS)
