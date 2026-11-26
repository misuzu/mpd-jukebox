import asyncio
import os
import tempfile
import unittest
from unittest import mock

from click.testing import CliRunner

from mpd_jukebox.cli import EX_CONFIG, main
from mpd_jukebox.model import Entry
from mpd_jukebox.store import Store, load_migrations

from .fake_mpd import FakeMPD, track


def library(n):
    return [track(f's{i}.mp3', artist=f'A{i}') for i in range(n)]


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self._tmp.name, 'state.sqlite')
        self.env = {'MPD_HOST': '', 'MPD_PORT': '', 'XDG_STATE_HOME': ''}

    def tearDown(self):
        self._tmp.cleanup()

    def invoke(self, args, fake=None, **kwargs):
        connects = []

        async def connect(conn):
            connects.append(conn)
            return fake

        result = CliRunner().invoke(
            main,
            ['--state', self.state, *args],
            obj={'connect': connect},
            env=self.env,
            **kwargs,
        )
        self.connects = connects
        return result

    def config(self, text):
        path = os.path.join(self._tmp.name, 'config.toml')
        with open(path, 'w') as f:
            f.write(text)
        return path

    def test_help(self):
        result = CliRunner().invoke(main, ['--help'])
        self.assertEqual(result.exit_code, 0)
        for command in ('run', 'list', 'select'):
            self.assertIn(command, result.output)

    def test_config_error(self):
        path = self.config('[jukebox]\nmode = "off"\n')
        result = self.invoke(['--config', path, 'list'])
        self.assertEqual(result.exit_code, EX_CONFIG)
        self.assertIn('jukebox.mode', result.output)

    def test_fuzzy_filter_is_rejected(self):
        path = self.config('[jukebox]\nfilter_include = "(Artist ~~ \'x\')"\n')
        result = self.invoke(['--config', path, 'list'])
        self.assertEqual(result.exit_code, EX_CONFIG)
        self.assertIn('fuzzy', result.output)

    def test_check(self):
        path = self.config(
            '[mpd]\nhost = "/run/mpd/socket"\n'
            'password_file = "/nonexistent/secret"\n'
        )
        result = self.invoke(['--config', path, 'check'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('song mode, MPD at /run/mpd/socket', result.output)

    def test_check_rejects_bad_config(self):
        path = self.config('[mpd]\npassword = "a"\npassword_file = "/p"\n')
        result = self.invoke(['--config', path, 'check'])
        self.assertEqual(result.exit_code, EX_CONFIG)
        self.assertIn('mutually exclusive', result.output)

    def test_run_reads_password_file(self):
        secret = os.path.join(self._tmp.name, 'secret')
        with open(secret, 'w') as f:
            f.write('pw\n')
        path = self.config(f'[mpd]\npassword_file = "{secret}"\n')
        fake = FakeMPD(library(150), password='pw')

        def install(runtime):
            asyncio.get_running_loop().call_later(0.3, runtime.stop)

        with mock.patch('mpd_jukebox.cli._install_signals', install):
            result = self.invoke(['--config', path, 'run'], fake)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(fake.commands[0], 'password')

    def test_run_missing_password_file(self):
        path = self.config('[mpd]\npassword_file = "/nonexistent"\n')
        result = self.invoke(['--config', path, 'run'])
        self.assertEqual(result.exit_code, EX_CONFIG)
        self.assertIn('cannot read password file', result.output)

    def test_list_without_state(self):
        result = self.invoke(['list'])
        self.assertEqual(result.exit_code, 1)
        self.assertIn('does not exist', result.output)

    def test_list(self):
        async def prepare():
            store = await Store.open(self.state)
            await store.upgrade(load_migrations())
            await store.save_queue((Entry('a.mp3', 1, 'Artist'),))
            await store.close()

        asyncio.run(prepare())
        result = self.invoke(['list'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output, '0\ta.mp3\tArtist\n')
        # The password file is not needed (nor read) for listing.
        path = self.config('[mpd]\npassword_file = "/nonexistent"\n')
        result = self.invoke(['--config', path, 'list'])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_print_lines_survives_a_closed_pipe(self):
        from mpd_jukebox import cli

        stdout = mock.Mock(fileno=mock.Mock(return_value=17))
        with (
            mock.patch('click.echo', side_effect=BrokenPipeError),
            mock.patch.object(cli.sys, 'stdout', stdout),
            mock.patch.object(cli.os, 'open', return_value=42) as open_,
            mock.patch.object(cli.os, 'dup2') as dup2,
        ):
            cli.print_lines(['a', 'b'])
        open_.assert_called_once_with(os.devnull, os.O_WRONLY)
        dup2.assert_called_once_with(42, 17)

    def test_print_lines_writes_once(self):
        from mpd_jukebox import cli

        with mock.patch('click.echo') as echo:
            cli.print_lines(['a', 'b'])
        echo.assert_called_once_with('a\nb\n', nl=False)

    def test_list_old_schema(self):
        async def prepare():
            store = await Store.open(self.state)
            await store.close()

        asyncio.run(prepare())
        result = self.invoke(['list'])
        self.assertEqual(result.exit_code, EX_CONFIG)
        self.assertIn('run the daemon', result.output)

    def test_select(self):
        fake = FakeMPD(library(20), password='pw')
        self.env['MPD_HOST'] = 'pw@music'
        result = self.invoke(['select', '-n', '3', '--seed', '7'], fake)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(result.output.splitlines()), 3)
        self.assertEqual(self.connects[0].host, 'music')
        self.assertEqual(fake.commands[0], 'password')
        self.assertEqual(fake.queue, [])
        again = self.invoke(
            ['select', '-n', '3', '--seed', '7'],
            FakeMPD(library(20), password='pw'),
        )
        self.assertEqual(again.output, result.output)

    def test_select_with_state_and_failure(self):
        async def prepare():
            store = await Store.open(self.state)
            await store.upgrade(load_migrations())
            await store.close()

        asyncio.run(prepare())
        result = self.invoke(['select', '-n', '50'], FakeMPD(library(5)))
        self.assertEqual(result.exit_code, 1)
        self.assertIn('found 5 of 50', result.output)

    def test_select_ignores_old_state(self):
        async def prepare():
            store = await Store.open(self.state)
            await store.close()

        asyncio.run(prepare())
        result = self.invoke(['select'], FakeMPD(library(200)))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(result.output.splitlines()), 100)

    def test_run_stops_on_signal(self):
        fake = FakeMPD(library(150))

        def install(runtime):
            loop = asyncio.get_running_loop()
            loop.call_later(0.3, runtime.stop)

        with mock.patch('mpd_jukebox.cli._install_signals', install):
            result = self.invoke(['--host', 'h', '--port', '1', 'run'], fake)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.connects[0].port, 1)
        self.assertEqual(fake.state, 'play')

    def test_run_wrong_password(self):
        fake = FakeMPD(library(5), password='pw')
        with mock.patch('mpd_jukebox.cli._install_signals'):
            result = self.invoke(['--password', 'nope', 'run'], fake)
        self.assertEqual(result.exit_code, EX_CONFIG)
        self.assertIn('incorrect password', result.output)

    def test_install_signals(self):
        from mpd_jukebox import cli

        class Fake:
            def request_restart(self):
                pass

            def stop(self):
                pass

        async def check():
            loop = asyncio.get_running_loop()
            with mock.patch.object(loop, 'add_signal_handler') as add:
                cli._install_signals(Fake())  # type: ignore[arg-type]
            self.assertEqual(add.call_count, 3)

        asyncio.run(check())
