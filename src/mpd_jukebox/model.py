"""Immutable data shared by the functional core and the shell."""

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

Tags = Mapping[str, tuple[str, ...]]

EMPTY_TAGS: Tags = MappingProxyType({})


def _no_tags() -> Tags:
    # A default_factory, not a plain default: Python 3.11's dataclasses
    # reject unhashable defaults such as a MappingProxyType.
    return EMPTY_TAGS


def freeze_tags(tags: Mapping[str, tuple[str, ...]]) -> Tags:
    return MappingProxyType(dict(tags))


@dataclass(frozen=True, slots=True)
class Song:
    """A song as reported by MPD. Tag names are lower case."""

    uri: str
    tags: Tags = field(default_factory=_no_tags)
    duration: float = 0.0
    prio: int = 0
    last_modified: int = 0
    added: int = 0


@dataclass(frozen=True, slots=True)
class Album:
    """An album: songs grouped by album key (§1.8)."""

    key: str
    first_uri: str
    uris: tuple[str, ...]
    tags: Tags = field(default_factory=_no_tags)
    last_modified: int = 0
    added: int = 0


@dataclass(frozen=True, slots=True)
class Entry:
    """One jukebox-queue item: a song URI or an album key."""

    key: str
    lineno: int
    tag_value: str


class Mode(enum.Enum):
    SONG = 'song'
    ALBUM = 'album'


class PlayState(enum.Enum):
    PLAY = 'play'
    PAUSE = 'pause'
    STOP = 'stop'


@dataclass(frozen=True, slots=True)
class Status:
    """The parts of MPD's `status` the jukebox needs."""

    state: PlayState = PlayState.STOP
    queue_length: int = 0
    song_id: int | None = None
    song_uri: str | None = None
    elapsed: float = 0.0
    duration: float = 0.0
    crossfade: int = 0


@dataclass(frozen=True, slots=True)
class AlbumSettings:
    group_tag: str | None = None
    use_mbid: bool = True
    unknown: bool = False
    split_by_directory: bool = False
    compilations: bool = False


@dataclass(frozen=True, slots=True)
class Config:
    """Jukebox settings (§1.1) plus myMPD's internal sizes."""

    mode: Mode = Mode.SONG
    playlist: str = 'Database'
    queue_length: int = 1
    last_played_hours: int = 24
    uniq_tag: str = 'artist'
    ignore_hated: bool = False
    filter_include: str = ''
    filter_exclude: str = ''
    min_song_duration: int = 0
    max_song_duration: int = 0
    queue_length_song: int = 100
    queue_length_song_min: int = 10
    queue_length_album: int = 25
    queue_length_album_min: int = 5
    add_max: int = 99
    uniq_range: int = 50
    add_offset: int = 10
    last_played_count: int = 2000
    write_stickers: bool = False
    albums: AlbumSettings = field(default_factory=AlbumSettings)


DATABASE = 'Database'


class ConfigError(ValueError):
    """An invalid configuration value."""
