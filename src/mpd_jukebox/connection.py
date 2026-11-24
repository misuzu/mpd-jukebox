"""MPD address and password from CLI, config file and environment (§5.5).

`MPD_HOST` is parsed like libmpdclient's `mpd_parse_host_password`: a value
starting with `@` is an abstract socket; otherwise everything before the
first `@` is the password.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .model import ConfigError

DEFAULT_HOST = 'localhost'
DEFAULT_PORT = 6600


@dataclass(frozen=True, slots=True)
class Connection:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    password: str | None = None

    def __repr__(self) -> str:
        password = None if self.password is None else '***'
        return (
            f'Connection(host={self.host!r}, port={self.port!r}, '
            f'password={password!r})'
        )

    @property
    def is_socket(self) -> bool:
        return self.host.startswith(('/', '@'))

    def describe(self) -> str:
        """Address for log messages; never includes the password."""
        return self.host if self.is_socket else f'{self.host}:{self.port}'


def parse_mpd_host(value: str) -> tuple[str, str | None]:
    """Split `password@host` into (host, password)."""
    if value.startswith('@'):
        return value, None
    password, sep, host = value.partition('@')
    if not sep:
        return value, None
    if not host:
        raise ConfigError('MPD host is empty after "password@"')
    return host, password


def parse_port(value: object, source: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f'{source}: invalid port {value!r}')
    try:
        port = int(str(value))
    except (TypeError, ValueError):
        raise ConfigError(f'{source}: invalid port {value!r}') from None
    if not 1 <= port <= 65535:
        raise ConfigError(f'{source}: port out of range: {port}')
    return port


def _text(value: object, source: str) -> str | None:
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ConfigError(f'{source} must be a string')
    return value


def with_password_file(
    config: Mapping[str, Any], read: Callable[[str], str]
) -> Mapping[str, Any]:
    """Replace `[mpd].password_file` with the `password` it contains.

    `read` returns the file's contents; the shell passes a real reader, and
    `check` passes one that returns a placeholder so no secret is read.
    """
    path = config.get('password_file')
    if path is None:
        return config
    if not isinstance(path, str) or not path:
        raise ConfigError('[mpd].password_file must be a non-empty string')
    if config.get('password') is not None:
        raise ConfigError(
            '[mpd].password and [mpd].password_file are mutually exclusive'
        )
    password = read(path).rstrip('\r\n')
    if not password:
        raise ConfigError(f'{path}: the password file is empty')
    rest = {k: v for k, v in config.items() if k != 'password_file'}
    return {**rest, 'password': password}


def read_password_file(path: str) -> str:
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except OSError as e:
        raise ConfigError(f'cannot read password file: {e}') from e


def resolve_connection(
    cli: Mapping[str, Any],
    config: Mapping[str, Any],
    env: Mapping[str, str],
) -> Connection:
    """Pick host, port and password.

    Host (and a password embedded in it): `--host`, then `[mpd].host`, then
    `MPD_HOST`, then localhost. A password embedded in one source is never
    sent to a host from another source.

    Password: `--password`, then the one embedded in the chosen host, then
    `[mpd].password`. Giving a password twice in one source is an error.

    Port: `--port`, then `[mpd].port`, then `MPD_PORT`, then 6600.
    """
    cli_password = _text(cli.get('password'), '--password')
    config_password = _text(config.get('password'), '[mpd].password')
    sources = (
        ('cli', _text(cli.get('host'), '--host')),
        ('config', _text(config.get('host'), '[mpd].host')),
        ('env', _text(env.get('MPD_HOST'), 'MPD_HOST')),
    )
    source, raw_host = next(
        ((name, host) for name, host in sources if host is not None),
        ('default', DEFAULT_HOST),
    )
    host, embedded = parse_mpd_host(raw_host)
    if embedded is not None and (
        (source == 'cli' and cli_password is not None)
        or (source == 'config' and config_password is not None)
    ):
        raise ConfigError(
            'password given twice: in the host and as a separate option'
        )
    password = cli_password or embedded or config_password

    if cli.get('port') is not None:
        port = parse_port(cli['port'], '--port')
    elif config.get('port') is not None:
        port = parse_port(config['port'], '[mpd].port')
    elif env.get('MPD_PORT'):
        port = parse_port(env['MPD_PORT'], 'MPD_PORT')
    else:
        port = DEFAULT_PORT
    return Connection(host, port, password)
