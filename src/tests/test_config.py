import os
import tempfile
import unittest
from typing import Any

from mpd_jukebox.config import fingerprint, parse_config, read_config_file
from mpd_jukebox.filters import Filter
from mpd_jukebox.model import ConfigError, Mode


class ParseConfigTestCase(unittest.TestCase):
    def test_defaults(self):
        settings = parse_config({})
        cfg = settings.config
        self.assertEqual(cfg.mode, Mode.SONG)
        self.assertEqual(cfg.playlist, 'Database')
        self.assertEqual(cfg.queue_length, 1)
        self.assertEqual(cfg.uniq_tag, 'artist')
        self.assertEqual(cfg.last_played_hours, 24)
        self.assertIsNone(settings.include)
        self.assertEqual(settings.warnings, ())

    def test_full(self):
        settings = parse_config(
            {
                'state_file': '/tmp/x.sqlite',
                'mpd': {'host': 'h', 'port': 1, 'password': 'p'},
                'jukebox': {
                    'mode': 'album',
                    'playlist': 'Favs',
                    'queue_length': 5,
                    'last_played': 0,
                    'uniq_tag': 'Album',
                    'ignore_hated': True,
                    'filter_include': "(Genre == 'Rock')",
                    'filter_exclude': "(Artist == 'X')",
                    'min_song_duration': 30,
                    'max_song_duration': 600,
                    'advanced': {
                        'queue_length_song': 50,
                        'queue_length_song_min': 20,
                        'queue_length_album': 10,
                        'queue_length_album_min': 6,
                        'last_played_count': 0,
                        'write_stickers': True,
                    },
                },
                'albums': {
                    'group_tag': 'Date',
                    'use_mbid': False,
                    'unknown': True,
                },
            }
        )
        cfg = settings.config
        self.assertEqual(cfg.mode, Mode.ALBUM)
        self.assertEqual(cfg.uniq_tag, 'album')
        self.assertEqual(cfg.albums.group_tag, 'date')
        self.assertFalse(cfg.albums.use_mbid)
        self.assertTrue(cfg.write_stickers)
        self.assertIsInstance(settings.include, Filter)
        self.assertIsInstance(settings.exclude, Filter)
        self.assertEqual(settings.mpd['host'], 'h')
        self.assertEqual(settings.state_file, '/tmp/x.sqlite')

    def test_errors(self):
        cases: list[dict[str, Any]] = [
            {'nope': 1},
            {'mpd': 'x'},
            {'jukebox': {'mode': 'off'}},
            {'jukebox': {'mode': 1}},
            {'jukebox': {'queue_length': 0}},
            {'jukebox': {'queue_length': 1000}},
            {'jukebox': {'queue_length': True}},
            {'jukebox': {'last_played': 5001}},
            {'jukebox': {'uniq_tag': 'nope'}},
            {'jukebox': {'ignore_hated': 'yes'}},
            {'jukebox': {'playlist': ''}},
            {'jukebox': {'advanced': {'queue_length_song': 5}}},
            {'jukebox': {'advanced': {'unknown': 1}}},
            {'jukebox': {'filter_include': "(Artist ~~ 'x')"}},
            {'jukebox': {'filter_exclude': "(Artist ~~ 'x')"}},
            {'albums': {'group_tag': 'nope'}},
            {'state_file': 5},
        ]
        for raw in cases:
            with self.subTest(raw), self.assertRaises(ConfigError):
                parse_config(raw)

    def test_invalid_filter_is_a_warning(self):
        settings = parse_config({'jukebox': {'filter_include': '(broken'}})
        self.assertIsNone(settings.include)
        self.assertEqual(len(settings.warnings), 1)
        self.assertIn('filter_include', settings.warnings[0])

    def test_fingerprint(self):
        a = parse_config({}).config
        b = parse_config({'jukebox': {'queue_length': 9}}).config
        c = parse_config({'jukebox': {'uniq_tag': 'Album'}}).config
        self.assertEqual(fingerprint(a), fingerprint(b))
        self.assertNotEqual(fingerprint(a), fingerprint(c))


class ReadConfigFileTestCase(unittest.TestCase):
    def test_no_file(self):
        self.assertEqual(read_config_file(None).config.mode, Mode.SONG)

    def test_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'c.toml')
            with open(path, 'w') as f:
                f.write('[jukebox]\nmode = "album"\n')
            self.assertEqual(read_config_file(path).config.mode, Mode.ALBUM)
            with open(path, 'w') as f:
                f.write('[jukebox\n')
            with self.assertRaises(ConfigError):
                read_config_file(path)
