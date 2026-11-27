"""Selection constraints (§1.6, §1.7) as small pure functions."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .filters import Filter, Subject, predicate, tags_used
from .model import Album, AlbumSettings, Config, Mode, Song
from .tags import album_key, tag_value


@dataclass(frozen=True, slots=True)
class Candidate:
    """A song or album that passed every constraint except uniqueness."""

    key: str
    tag_value: str


@dataclass(frozen=True, slots=True)
class UniqContext:
    """Keys and tag values from the MPD queue and recent history."""

    keys: frozenset[str] = frozenset()
    values: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SelectContext:
    uniq_tag: str
    min_duration: int
    max_duration: int
    since: int
    last_played: Mapping[str, int]
    hated: frozenset[str]
    matches: Callable[[Subject], bool]


def make_context(
    cfg: Config,
    include: Filter | None,
    exclude: Filter | None,
    now: int,
    last_played: Mapping[str, int],
    hated: frozenset[str],
) -> SelectContext:
    return SelectContext(
        uniq_tag=cfg.uniq_tag,
        min_duration=cfg.min_song_duration,
        max_duration=cfg.max_song_duration,
        since=now - cfg.last_played_hours * 3600,
        last_played=last_played,
        hated=hated,
        matches=predicate(include, exclude),
    )


def needed_tags(
    cfg: Config, include: Filter | None, exclude: Filter | None
) -> frozenset[str] | None:
    """The tags the jukebox reads from MPD with this configuration, or None
    for all of them (a filter using `any`). Asking MPD for only these
    (`tagtypes`) makes library reads much smaller."""
    tags = {cfg.uniq_tag}
    if cfg.uniq_tag == 'title':
        tags.add('name')  # the title falls back to it (tag_value)
    if cfg.mode is Mode.ALBUM:
        # Album keys (album_key) and the order of an album's songs.
        tags |= {'albumartist', 'artist', 'album', 'disc', 'track'}
        if cfg.albums.use_mbid:
            tags.add('musicbrainz_albumid')
        if cfg.albums.group_tag is not None:
            tags.add(cfg.albums.group_tag)
        if cfg.albums.split_by_directory:
            tags.add('title')  # tells disc folders from copies (albums.py)
    for flt in (include, exclude):
        if flt is not None:
            used = tags_used(flt)
            if used is None:
                return None
            tags |= used
    return frozenset(tags)


def duration_ok(duration: float, min_s: int, max_s: int) -> bool:
    """Strict bounds, 0 meaning no limit (myMPD's check_*_duration)."""
    return (min_s == 0 or duration > min_s) and (
        max_s == 0 or duration < max_s
    )


def played_before(
    last_played: Mapping[str, int], uri: str, since: int
) -> bool:
    """True if the URI was not played at or after `since`. URIs never
    played pass."""
    played = last_played.get(uri)
    return played is None or played < since


def song_candidate(ctx: SelectContext, song: Song) -> Candidate | None:
    ok = (
        duration_ok(song.duration, ctx.min_duration, ctx.max_duration)
        and played_before(ctx.last_played, song.uri, ctx.since)
        and song.uri not in ctx.hated
        and ctx.matches(
            Subject(
                song.tags,
                song.uri,
                song.prio,
                song.last_modified,
                song.added,
            )
        )
    )
    if not ok:
        return None
    return Candidate(song.uri, tag_value(song.tags, ctx.uniq_tag, song.uri))


def album_candidate(ctx: SelectContext, album: Album) -> Candidate | None:
    """Album mode checks last played on the first song, and the
    expression on album tags; durations and hated songs are not checked."""
    ok = played_before(
        ctx.last_played, album.first_uri, ctx.since
    ) and ctx.matches(
        Subject(
            album.tags,
            last_modified=album.last_modified,
            added=album.added,
        )
    )
    if not ok:
        return None
    return Candidate(
        album.key, tag_value(album.tags, ctx.uniq_tag, album.first_uri)
    )


def last_played_map(
    stickers: Mapping[str, str] | None,
    history: Iterable[tuple[str, int]],
) -> Mapping[str, int]:
    """Last-played timestamps: from `lastPlayed` stickers when MPD has a
    sticker database, otherwise from our own history (§8)."""
    result: dict[str, int] = {}
    if stickers is not None:
        for uri, value in stickers.items():
            try:
                result[uri] = int(value)
            except ValueError:
                pass
    else:
        for uri, ts in history:
            result[uri] = max(ts, result.get(uri, ts))
    return MappingProxyType(result)


def hated_uris(
    likes: Mapping[str, str] | None, ignore_hated: bool
) -> frozenset[str]:
    """URIs with the `like` sticker set to 0, if `ignore_hated` is on."""
    if not ignore_hated or likes is None:
        return frozenset()
    return frozenset(uri for uri, v in likes.items() if v.startswith('0'))


def history_wanted(queue_length: int, uniq_range: int) -> int:
    """How many recent history songs to add to the uniqueness context."""
    return uniq_range if queue_length < uniq_range else 0


def uniq_context(
    mode: Mode,
    uniq_tag: str,
    songs: Iterable[Song],
    albums: AlbumSettings,
    album_of: Mapping[str, str] | None = None,
) -> UniqContext:
    """myMPD's jukebox_get_last_played: one (key, tag value) per song of the
    MPD queue and recent history. With uniq tag `title` the value is
    empty. In album mode, `album_of` (the album key of each URI in the
    album index) takes precedence over the song's own album key, which
    differs for songs of untagged compilations (§7.1)."""
    keys: set[str] = set()
    values: set[str] = set()
    for song in songs:
        if mode is Mode.SONG:
            keys.add(song.uri)
        else:
            key = album_of.get(song.uri) if album_of is not None else None
            keys.add(key or album_key(song.tags, albums) or '')
        values.add(
            '' if uniq_tag == 'title' else tag_value(song.tags, uniq_tag, '')
        )
    return UniqContext(frozenset(keys), frozenset(values))
