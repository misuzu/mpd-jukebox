"""Command line interface (§5.6)."""

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any

import click

from . import jukebox as core
from .config import Settings, read_config_file
from .connection import (
    Connection,
    read_password_file,
    resolve_connection,
    with_password_file,
)
from .model import ConfigError
from .mpd_io import FatalMpdError, MpdIO
from .runtime import (
    Connect,
    NoHistory,
    Refiller,
    Runtime,
    connect_client,
    random_seed,
)
from .store import Store, StoreError, default_path, load_migrations
from .systemd import notify

log = logging.getLogger(__name__)


# sysexits' EX_CONFIG: the configuration is wrong, so restarting cannot help.
# The NixOS module tells systemd not to restart on it.
EX_CONFIG = 78


class ConfigurationError(click.ClickException):
    """A problem only a configuration change can fix."""

    exit_code = EX_CONFIG


@dataclass(frozen=True)
class Options:
    config_path: str | None
    host: str | None
    port: int | None
    password: str | None
    state_file: str | None
    # Replaced in tests.
    connect: Connect = connect_client


@dataclass(frozen=True)
class Loaded:
    settings: Settings
    connection: Connection
    state_file: str


def load(options: Options, *, read_secrets: bool = True) -> Loaded:
    try:
        settings = read_config_file(options.config_path)
        reader = read_password_file if read_secrets else (lambda path: '***')
        connection = resolve_connection(
            {
                'host': options.host,
                'port': options.port,
                'password': options.password,
            },
            with_password_file(settings.mpd, reader),
            os.environ,
        )
    except ConfigError as e:
        raise ConfigurationError(str(e)) from e
    for warning in settings.warnings:
        log.warning('%s', warning)
    state_file = options.state_file or settings.state_file or default_path()
    return Loaded(settings, connection, state_file)


def print_lines(lines: Iterable[str]) -> None:
    """Write all lines at once, so `mpd-jukebox list | head -n 1` does not
    fail half-way; a reader that went away anyway is not an error."""
    text = ''.join(f'{line}\n' for line in lines)
    try:
        click.echo(text, nl=False)
    except BrokenPipeError:
        # Python would complain again when flushing stdout at exit.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())


def run_async(coro: Coroutine[Any, Any, None]) -> None:
    try:
        asyncio.run(coro)
    except (StoreError, FatalMpdError) as e:
        # A wrong password or missing permission, or a database written by
        # a newer version.
        raise ConfigurationError(str(e)) from e


@click.group()
@click.option(
    '--config',
    'config_path',
    envvar='MPD_JUKEBOX_CONFIG',
    show_envvar=True,
    type=click.Path(exists=True, dir_okay=False),
    help='Configuration file (TOML).',
)
@click.option(
    '--host', help='MPD host, socket path or "password@host" [MPD_HOST].'
)
@click.option('--port', type=int, help='MPD port [MPD_PORT].')
@click.option(
    '--password',
    help='MPD password. Visible in the process list; prefer MPD_HOST or '
    'the config file.',
)
@click.option(
    '--state',
    'state_file',
    type=click.Path(dir_okay=False),
    help='State database (default: $XDG_STATE_HOME/mpd-jukebox/state.sqlite).',
)
@click.option('--debug/--no-debug', help='Log debug messages.')
@click.pass_context
def main(
    ctx: click.Context,
    config_path: str | None,
    host: str | None,
    port: int | None,
    password: str | None,
    state_file: str | None,
    debug: bool,
) -> None:
    """Keep MPD's queue filled with random songs or albums, like myMPD's
    jukebox mode."""
    logging.basicConfig(
        level=logging.INFO, format='%(levelname)s: %(message)s'
    )
    if debug:
        # Only our own and python-mpd2's debug output, not aiosqlite's.
        for name in ('mpd_jukebox', 'mpd'):
            logging.getLogger(name).setLevel(logging.DEBUG)
    # Tests may pass {'connect': ...} as the context object.
    connect = (ctx.obj or {}).get('connect', connect_client)
    ctx.obj = Options(config_path, host, port, password, state_file, connect)


@main.command()
@click.pass_obj
def run(options: Options) -> None:
    """Run the jukebox daemon."""
    loaded = load(options)
    run_async(_run(loaded, options.connect))


async def _run(loaded: Loaded, connect: Connect) -> None:
    store = await Store.open(loaded.state_file)
    try:
        await store.upgrade(load_migrations())
        runtime = Runtime(
            loaded.settings, loaded.connection, store, connect=connect
        )
        _install_signals(runtime)
        # Only now can SIGHUP (`systemctl reload`) be handled.
        notify('READY=1')
        await runtime.run()
    finally:
        await store.close()


def _install_signals(runtime: Runtime) -> None:
    loop = asyncio.get_running_loop()
    handlers: dict[signal.Signals, Callable[[], None]] = {
        signal.SIGHUP: runtime.request_restart,
        signal.SIGINT: runtime.stop,
        signal.SIGTERM: runtime.stop,
    }
    for signum, handler in handlers.items():
        loop.add_signal_handler(signum, handler)


@main.command()
@click.pass_obj
def check(options: Options) -> None:
    """Check the configuration and exit. Connects to nothing and reads no
    password file."""
    loaded = load(options, read_secrets=False)
    cfg = loaded.settings.config
    click.echo(
        f'OK: {cfg.mode.value} mode, MPD at {loaded.connection.describe()}, '
        f'state in {loaded.state_file}'
    )


@main.command('list')
@click.pass_obj
def list_(options: Options) -> None:
    """Print the saved jukebox queue."""
    # Needs no MPD connection, so no password file either: it may only
    # exist while the service runs (a systemd credential).
    loaded = load(options, read_secrets=False)
    if not os.path.exists(loaded.state_file):
        raise click.ClickException(f'{loaded.state_file} does not exist')
    run_async(_list(loaded.state_file))


async def _list(path: str) -> None:
    store = await Store.open(path, readonly=True)
    try:
        await store.check_version(load_migrations())
        entries = await store.queue()
    finally:
        await store.close()
    print_lines(
        f'{pos}\t{entry.key}\t{entry.tag_value}'
        for pos, entry in enumerate(entries)
    )


@main.command()
@click.option(
    '-n',
    'count',
    type=int,
    help='How many entries to pick (default: the '
    'jukebox queue size for the mode).',
)
@click.option('--seed', type=int, help='Random seed, for repeatable picks.')
@click.pass_obj
def select(options: Options, count: int | None, seed: int | None) -> None:
    """Pick entries like a refill would and print them, without changing
    MPD or the saved state."""
    loaded = load(options)
    run_async(_select(loaded, options.connect, count, seed))


async def _select(
    loaded: Loaded, connect: Connect, count: int | None, seed: int | None
) -> None:
    cfg = loaded.settings.config
    expected = count if count is not None else core.queue_size(cfg, cfg.mode)
    store = await _optional_store(loaded.state_file)
    client = await connect(loaded.connection)
    try:
        io = MpdIO(client)
        if loaded.connection.password is not None:
            await io.password(loaded.connection.password)
        refiller = Refiller(
            loaded.settings,
            store if store is not None else NoHistory(),
            random_seed if seed is None else (lambda: seed),
        )
        effect = core.StartRefill(cfg.mode, (), expected, 0)
        result = await refiller.refill(io, effect, int(time.time()))
    finally:
        client.disconnect()
        if store is not None:
            await store.close()
    if isinstance(result, core.RefillFailed):
        raise click.ClickException(result.error)
    print_lines(f'{entry.key}\t{entry.tag_value}' for entry in result.entries)


async def _optional_store(path: str) -> Store | None:
    """The history makes picks match the daemon's; it is optional here."""
    if not os.path.exists(path):
        return None
    store = await Store.open(path, readonly=True)
    try:
        await store.check_version(load_migrations())
    except StoreError:
        await store.close()
        return None
    return store
