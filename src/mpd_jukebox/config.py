"""Configuration file parsing (§5.4).

`parse_config` is pure: it takes the decoded TOML document and returns a
validated `Settings` or raises `ConfigError`. Only `read_config_file`
touches the file system.
"""

import hashlib
import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from . import filters
from .model import AlbumSettings, Config, ConfigError, Mode
from .tags import canonical_tag

INT_MAX = 2**31 - 1

_TOP_KEYS = frozenset({'state_file', 'mpd', 'jukebox', 'albums'})
_MPD_KEYS = frozenset({'host', 'port', 'password', 'password_file'})
_JUKEBOX_KEYS = frozenset(
    {
        'mode',
        'playlist',
        'queue_length',
        'last_played',
        'uniq_tag',
        'ignore_hated',
        'filter_include',
        'filter_exclude',
        'min_song_duration',
        'max_song_duration',
        'advanced',
    }
)
_ADVANCED_KEYS = frozenset(
    {
        'queue_length_song',
        'queue_length_song_min',
        'queue_length_album',
        'queue_length_album_min',
        'last_played_count',
        'write_stickers',
    }
)
_ALBUM_KEYS = frozenset(
    {
        'group_tag',
        'use_mbid',
        'unknown',
        'split_by_directory',
        'compilations',
        'merge_discs',
    }
)


@dataclass(frozen=True, slots=True)
class Settings:
    config: Config = field(default_factory=Config)
    include: filters.Filter | None = None
    exclude: filters.Filter | None = None
    mpd: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    state_file: str | None = None
    warnings: tuple[str, ...] = ()


def _table(
    raw: Mapping[str, Any], name: str, allowed: frozenset[str]
) -> Mapping[str, Any]:
    value = raw.get(name, {}) if name else raw
    if not isinstance(value, Mapping):
        raise ConfigError(f'[{name}] must be a table')
    unknown = sorted(set(value) - allowed)
    if unknown:
        where = f'[{name}]' if name else 'top level'
        raise ConfigError(f'unknown key(s) in {where}: {", ".join(unknown)}')
    return value


def _int(
    table: Mapping[str, Any],
    key: str,
    default: int,
    low: int,
    high: int,
    where: str,
) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f'{where}.{key} must be an integer')
    if not low <= value <= high:
        raise ConfigError(
            f'{where}.{key} must be between {low} and {high}, got {value}'
        )
    return value


def _bool(
    table: Mapping[str, Any], key: str, default: bool, where: str
) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f'{where}.{key} must be true or false')
    return value


def _str(table: Mapping[str, Any], key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f'{where}.{key} must be a string')
    return value


def _tag(name: str, where: str) -> str:
    tag = canonical_tag(name)
    if tag is None:
        raise ConfigError(f'{where}: unknown MPD tag {name!r}')
    return tag


def _filter(
    expression: str, where: str
) -> tuple[filters.Filter | None, tuple[str, ...]]:
    if not expression:
        return None, ()
    result = filters.parse(expression)
    match result:
        case filters.Unsupported(reason):
            raise ConfigError(f'{where}: {reason}')
        case filters.Invalid(reason):
            return None, (f'{where} is invalid and ignored: {reason}',)
    return filters.usable(result), ()


def parse_config(raw: Mapping[str, Any]) -> Settings:
    _table(raw, '', _TOP_KEYS)
    mpd = _table(raw, 'mpd', _MPD_KEYS)
    jukebox = _table(raw, 'jukebox', _JUKEBOX_KEYS)
    advanced = _table(jukebox, 'advanced', _ADVANCED_KEYS)
    album = _table(raw, 'albums', _ALBUM_KEYS)
    j, a, al = 'jukebox', 'jukebox.advanced', 'albums'
    defaults = Config()

    mode_name = _str(jukebox, 'mode', defaults.mode.value, j)
    try:
        mode = Mode(mode_name)
    except ValueError:
        raise ConfigError(
            f'jukebox.mode must be "song" or "album", got {mode_name!r}'
        ) from None

    playlist = _str(jukebox, 'playlist', defaults.playlist, j)
    if not playlist:
        raise ConfigError('jukebox.playlist must not be empty')

    group_tag = _str(album, 'group_tag', '', al)
    include_text = _str(jukebox, 'filter_include', '', j)
    exclude_text = _str(jukebox, 'filter_exclude', '', j)
    include, warn_include = _filter(include_text, 'jukebox.filter_include')
    exclude, warn_exclude = _filter(exclude_text, 'jukebox.filter_exclude')

    config = Config(
        mode=mode,
        playlist=playlist,
        queue_length=_int(jukebox, 'queue_length', 1, 1, 999, j),
        last_played_hours=_int(jukebox, 'last_played', 24, 0, 5000, j),
        uniq_tag=_tag(_str(jukebox, 'uniq_tag', 'Artist', j), 'uniq_tag'),
        ignore_hated=_bool(jukebox, 'ignore_hated', False, j),
        filter_include=include_text,
        filter_exclude=exclude_text,
        min_song_duration=_int(jukebox, 'min_song_duration', 0, 0, INT_MAX, j),
        max_song_duration=_int(jukebox, 'max_song_duration', 0, 0, INT_MAX, j),
        queue_length_song=_int(
            advanced, 'queue_length_song', 100, 10, 1000, a
        ),
        queue_length_song_min=_int(
            advanced, 'queue_length_song_min', 10, 10, 500, a
        ),
        queue_length_album=_int(advanced, 'queue_length_album', 25, 5, 250, a),
        queue_length_album_min=_int(
            advanced, 'queue_length_album_min', 5, 5, 125, a
        ),
        last_played_count=_int(
            advanced, 'last_played_count', 2000, 0, 100_000, a
        ),
        write_stickers=_bool(advanced, 'write_stickers', False, a),
        albums=AlbumSettings(
            group_tag=_tag(group_tag, 'albums.group_tag')
            if group_tag
            else None,
            use_mbid=_bool(album, 'use_mbid', True, al),
            unknown=_bool(album, 'unknown', False, al),
            split_by_directory=_bool(album, 'split_by_directory', False, al),
            compilations=_bool(album, 'compilations', False, al),
            merge_discs=_bool(album, 'merge_discs', False, al),
        ),
    )

    state_file = raw.get('state_file')
    if state_file is not None and not isinstance(state_file, str):
        raise ConfigError('state_file must be a string')

    return Settings(
        config=config,
        include=include,
        exclude=exclude,
        mpd=MappingProxyType(dict(mpd)),
        state_file=state_file or None,
        warnings=warn_include + warn_exclude,
    )


def read_config_file(path: str | None) -> Settings:
    if path is None:
        return parse_config({})
    try:
        with open(path, 'rb') as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'{path}: {e}') from e
    return parse_config(raw)


def fingerprint(config: Config) -> str:
    """A hash of the settings that make a saved jukebox queue stale
    (myMPD clears the queue when any of them changes, §1.10)."""
    fields = {
        'mode': config.mode.value,
        'playlist': config.playlist,
        'uniq_tag': config.uniq_tag,
        'last_played': config.last_played_hours,
        'ignore_hated': config.ignore_hated,
        'filter_include': config.filter_include,
        'filter_exclude': config.filter_exclude,
        'min_song_duration': config.min_song_duration,
        'max_song_duration': config.max_song_duration,
        'album_group_tag': config.albums.group_tag,
        'album_use_mbid': config.albums.use_mbid,
        'album_unknown': config.albums.unknown,
        'album_split_by_directory': config.albums.split_by_directory,
        'album_compilations': config.albums.compilations,
        'album_merge_discs': config.albums.merge_discs,
    }
    encoded = json.dumps(fields, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
