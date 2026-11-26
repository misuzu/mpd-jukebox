import unittest
from dataclasses import replace

from mpd_jukebox.constraints import (
    Candidate,
    UniqContext,
    album_candidate,
    duration_ok,
    hated_uris,
    history_wanted,
    last_played_map,
    make_context,
    needed_tags,
    played_before,
    song_candidate,
    uniq_context,
)
from mpd_jukebox.filters import Filter, parse
from mpd_jukebox.model import Album, AlbumSettings, Config, Entry, Mode
from mpd_jukebox.select import (
    Filled,
    Short,
    more_windows,
    select_init,
    select_result,
    select_step,
)
from mpd_jukebox.tags import album_key

from .helpers import song

NOW = 1_000_000


def ctx(cfg=None, include=None, exclude=None, last_played=None, hated=()):
    return make_context(
        cfg or Config(),
        include,
        exclude,
        NOW,
        last_played or {},
        frozenset(hated),
    )


def flt(expression: str) -> Filter:
    result = parse(expression)
    assert isinstance(result, Filter)
    return result


def fold(acc, candidates):
    for cand in candidates:
        acc = select_step(acc, cand)
    return acc


class ConstraintTestCase(unittest.TestCase):
    def test_duration_is_strict(self):
        self.assertTrue(duration_ok(100, 0, 0))
        self.assertFalse(duration_ok(100, 100, 0))
        self.assertTrue(duration_ok(101, 100, 0))
        self.assertFalse(duration_ok(100, 0, 100))
        self.assertTrue(duration_ok(99, 0, 100))

    def test_played_before(self):
        self.assertTrue(played_before({}, 'a', 10))
        self.assertTrue(played_before({'a': 9}, 'a', 10))
        self.assertFalse(played_before({'a': 10}, 'a', 10))

    def test_song_candidate(self):
        s = song('a.mp3', 200, artist='A', genre='Rock')
        self.assertEqual(song_candidate(ctx(), s), Candidate('a.mp3', 'A'))
        cases = {
            'too short': ctx(Config(min_song_duration=300)),
            'too long': ctx(Config(max_song_duration=100)),
            'recent': ctx(last_played={'a.mp3': NOW - 3600}),
            'hated': ctx(hated={'a.mp3'}),
            'excluded': ctx(exclude=flt("(Genre == 'rock')")),
            'not included': ctx(include=flt("(Genre == 'pop')")),
        }
        for name, c in cases.items():
            with self.subTest(name):
                self.assertIsNone(song_candidate(c, s))

    def test_last_played_window(self):
        c = ctx(Config(last_played_hours=1), last_played={'a.mp3': NOW - 3601})
        self.assertIsNotNone(song_candidate(c, song('a.mp3')))

    def test_album_candidate(self):
        album = Album('k', 'a/1.mp3', ('a/1.mp3',), song('', artist='R').tags)
        self.assertEqual(album_candidate(ctx(), album), Candidate('k', 'R'))
        self.assertIsNone(
            album_candidate(ctx(last_played={'a/1.mp3': NOW}), album)
        )
        self.assertIsNone(
            album_candidate(ctx(exclude=flt("(Artist == 'r')")), album)
        )
        # Durations do not apply to albums.
        self.assertIsNotNone(
            album_candidate(ctx(Config(min_song_duration=9999)), album)
        )

    def test_last_played_map(self):
        self.assertEqual(
            dict(last_played_map({'a': '5', 'b': 'x'}, [('a', 9)])), {'a': 5}
        )
        self.assertEqual(
            dict(last_played_map(None, [('a', 9), ('a', 4), ('b', 1)])),
            {'a': 9, 'b': 1},
        )

    def test_hated(self):
        likes = {'a': '0', 'b': '1', 'c': '2'}
        self.assertEqual(hated_uris(likes, True), {'a'})
        self.assertEqual(hated_uris(likes, False), frozenset())
        self.assertEqual(hated_uris(None, True), frozenset())

    def test_history_wanted(self):
        self.assertEqual(history_wanted(10, 50), 50)
        self.assertEqual(history_wanted(50, 50), 0)

    def test_uniq_context_songs(self):
        songs = [song('a', artist='A'), song('b')]
        result = uniq_context(Mode.SONG, 'artist', songs, AlbumSettings())
        self.assertEqual(result.keys, {'a', 'b'})
        self.assertEqual(result.values, {'A', ''})

    def test_uniq_context_title_is_empty(self):
        songs = [song('a', title='T')]
        result = uniq_context(Mode.SONG, 'title', songs, AlbumSettings())
        self.assertEqual(result.values, {''})

    def test_uniq_context_albums(self):
        s = song('a', artist='A', album='X')
        result = uniq_context(
            Mode.ALBUM, 'artist', [s, song('b')], AlbumSettings()
        )
        self.assertEqual(result.keys, {album_key(s.tags, AlbumSettings()), ''})


class SelectTestCase(unittest.TestCase):
    def candidates(self, n):
        return [Candidate(f'u{i}', f'v{i}') for i in range(n)]

    def test_fills_and_is_deterministic(self):
        acc = fold(select_init((), 5, 42, UniqContext()), self.candidates(100))
        result = select_result(acc)
        assert isinstance(result, Filled)
        self.assertEqual(len(result.entries), 5)
        self.assertEqual(len({e.key for e in result.entries}), 5)
        again = fold(
            select_init((), 5, 42, UniqContext()), self.candidates(100)
        )
        self.assertEqual(again.entries, acc.entries)
        other = fold(
            select_init((), 5, 43, UniqContext()), self.candidates(100)
        )
        self.assertNotEqual(other.entries, acc.entries)

    def test_counts(self):
        acc = fold(
            select_init((), 3, 1, UniqContext()),
            [
                Candidate('a', 'x'),
                None,
                Candidate('b', 'x'),
                Candidate('c', 'y'),
            ],
        )
        self.assertEqual((acc.lineno, acc.skipno), (3, 2))
        self.assertEqual([e.key for e in acc.entries], ['a', 'c'])
        self.assertEqual([e.lineno for e in acc.entries], [1, 2])

    def test_short(self):
        acc = fold(select_init((), 5, 1, UniqContext()), self.candidates(3))
        self.assertEqual(select_result(acc), Short(3, 5))

    def test_existing_entries_untouched(self):
        existing = tuple(Entry(f'old{i}', i, f'ov{i}') for i in range(3))
        for seed in range(50):
            acc = fold(
                select_init(existing, 6, seed, UniqContext()),
                self.candidates(200),
            )
            self.assertEqual(acc.entries[:3], existing)
            self.assertEqual(len(acc.entries), 6)

    def test_uniqueness_against_everything(self):
        existing = (Entry('u0', 1, 'old'),)
        uniq = UniqContext(frozenset({'u1'}), frozenset({'v2'}))
        cands = self.candidates(5) + [Candidate('u9', 'old')]
        acc = fold(select_init(existing, 10, 3, uniq), cands)
        self.assertEqual(
            sorted(e.key for e in acc.entries), ['u0', 'u3', 'u4']
        )

    def test_never_duplicates_over_many_seeds(self):
        cands = [Candidate(f'u{i}', f'v{i % 7}') for i in range(100)]
        for seed in range(100):
            acc = fold(select_init((), 7, seed, UniqContext()), cands)
            self.assertEqual(len({e.tag_value for e in acc.entries}), 7)

    def test_already_full(self):
        existing = (Entry('a', 1, 'x'),)
        acc = select_init(existing, 1, 1, UniqContext())
        self.assertIs(select_step(acc, Candidate('b', 'y')), acc)
        self.assertFalse(more_windows(acc, 0))
        self.assertEqual(select_result(acc), Filled(existing))

    def test_single_slot_replacement(self):
        acc = fold(select_init((), 1, 5, UniqContext()), self.candidates(50))
        self.assertEqual(len(acc.entries), 1)

    def test_more_windows(self):
        acc = select_init((), 5, 1, UniqContext())
        full = replace(acc, lineno=10_001)
        self.assertTrue(more_windows(full, 10_000))
        short = replace(acc, lineno=9_000)
        self.assertFalse(more_windows(short, 10_000))


class NeededTagsTestCase(unittest.TestCase):
    def test_song_mode(self):
        self.assertEqual(needed_tags(Config(), None, None), {'artist'})

    def test_title_falls_back_to_name(self):
        cfg = Config(uniq_tag='title')
        self.assertEqual(needed_tags(cfg, None, None), {'title', 'name'})

    def test_album_mode(self):
        cfg = Config(mode=Mode.ALBUM, uniq_tag='genre')
        self.assertEqual(
            needed_tags(cfg, None, None),
            {
                'genre',
                'albumartist',
                'artist',
                'album',
                'disc',
                'track',
                'musicbrainz_albumid',
            },
        )
        cfg = Config(
            mode=Mode.ALBUM,
            albums=AlbumSettings(group_tag='date', use_mbid=False),
        )
        tags = needed_tags(cfg, None, None)
        assert tags is not None
        self.assertIn('date', tags)
        self.assertNotIn('musicbrainz_albumid', tags)

    def test_filters(self):
        tags = needed_tags(
            Config(), flt("(Genre == 'x')"), flt("(Composer == 'y')")
        )
        self.assertEqual(tags, {'artist', 'genre', 'composer'})
        self.assertIsNone(needed_tags(Config(), None, flt("(any == 'y')")))
