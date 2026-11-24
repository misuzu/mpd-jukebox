import os
import tempfile
import unittest
from typing import Any

from mpd_jukebox.connection import (
    Connection,
    parse_mpd_host,
    parse_port,
    read_password_file,
    resolve_connection,
    with_password_file,
)
from mpd_jukebox.model import ConfigError


class ParseMpdHostTestCase(unittest.TestCase):
    def test_table(self):
        cases = {
            'localhost': ('localhost', None),
            'secret@localhost': ('localhost', 'secret'),
            'secret@192.168.1.5': ('192.168.1.5', 'secret'),
            'secret@::1': ('::1', 'secret'),
            'secret@/run/mpd/socket': ('/run/mpd/socket', 'secret'),
            '@mpd': ('@mpd', None),
            'secret@@mpd': ('@mpd', 'secret'),
            'a@b@c': ('b@c', 'a'),
        }
        for value, expected in cases.items():
            with self.subTest(value):
                self.assertEqual(parse_mpd_host(value), expected)

    def test_empty_host(self):
        with self.assertRaises(ConfigError):
            parse_mpd_host('secret@')


class ParsePortTestCase(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_port('6601', 'x'), 6601)
        self.assertEqual(parse_port(6602, 'x'), 6602)

    def test_invalid(self):
        for value in ('abc', '0', '65536', True, None, '6600.5'):
            with self.subTest(value), self.assertRaises(ConfigError):
                parse_port(value, 'MPD_PORT')


class ResolveTestCase(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(
            resolve_connection({}, {}, {}), Connection('localhost', 6600)
        )

    def test_env(self):
        conn = resolve_connection(
            {}, {}, {'MPD_HOST': 'pw@music', 'MPD_PORT': '6601'}
        )
        self.assertEqual(conn, Connection('music', 6601, 'pw'))

    def test_empty_env_is_unset(self):
        conn = resolve_connection({}, {}, {'MPD_HOST': '', 'MPD_PORT': ''})
        self.assertEqual(conn, Connection())

    def test_precedence(self):
        env = {'MPD_HOST': 'envpw@envhost', 'MPD_PORT': '1'}
        config = {'host': 'cfghost', 'port': 2}
        cli = {'host': 'clihost', 'port': 3}
        self.assertEqual(
            resolve_connection(cli, config, env), Connection('clihost', 3)
        )
        self.assertEqual(
            resolve_connection({}, config, env), Connection('cfghost', 2)
        )

    def test_env_password_not_sent_to_other_host(self):
        conn = resolve_connection(
            {}, {'host': 'other'}, {'MPD_HOST': 'secret@home'}
        )
        self.assertIsNone(conn.password)

    def test_explicit_passwords(self):
        env = {'MPD_HOST': 'h'}
        self.assertEqual(
            resolve_connection({}, {'password': 'c'}, env).password, 'c'
        )
        self.assertEqual(
            resolve_connection(
                {'password': 'x'}, {'password': 'c'}, env
            ).password,
            'x',
        )
        # The password embedded in the chosen host beats the config one.
        self.assertEqual(
            resolve_connection(
                {}, {'password': 'c'}, {'MPD_HOST': 'e@h'}
            ).password,
            'e',
        )

    def test_password_twice_in_one_source(self):
        with self.assertRaises(ConfigError):
            resolve_connection({'host': 'a@h', 'password': 'b'}, {}, {})
        with self.assertRaises(ConfigError):
            resolve_connection({}, {'host': 'a@h', 'password': 'b'}, {})

    def test_non_string_host(self):
        with self.assertRaises(ConfigError):
            resolve_connection({}, {'host': 5}, {})

    def test_repr_and_describe_hide_password(self):
        conn = Connection('h', 1, 'secret')
        self.assertNotIn('secret', repr(conn))
        self.assertEqual(conn.describe(), 'h:1')
        self.assertNotIn('secret', repr(Connection()))
        self.assertEqual(
            Connection('/run/mpd.sock').describe(), '/run/mpd.sock'
        )
        self.assertTrue(Connection('@mpd').is_socket)


class PasswordFileTestCase(unittest.TestCase):
    def test_replaces_password_file(self):
        result = with_password_file(
            {'host': 'h', 'password_file': '/p'},
            lambda path: f'secret-from-{path}\n',
        )
        self.assertEqual(
            dict(result), {'host': 'h', 'password': 'secret-from-/p'}
        )

    def test_without_password_file(self):
        config = {'host': 'h'}
        self.assertIs(with_password_file(config, _fail), config)

    def test_errors(self):
        cases: list[dict[str, Any]] = [
            {'password_file': ''},
            {'password_file': 5},
            {'password_file': '/p', 'password': 'x'},
        ]
        for config in cases:
            with self.subTest(config), self.assertRaises(ConfigError):
                with_password_file(config, lambda path: 'pw')
        with self.assertRaises(ConfigError):
            with_password_file({'password_file': '/p'}, lambda path: '\n')

    def test_read_password_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'pw')
            with open(path, 'w') as f:
                f.write('secret\n')
            self.assertEqual(read_password_file(path), 'secret\n')
            with self.assertRaises(ConfigError):
                read_password_file(os.path.join(tmp, 'missing'))


def _fail(path):
    raise AssertionError(path)
