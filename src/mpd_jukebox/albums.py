"""Album index: songs grouped by album key (§1.8)."""

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from .model import Album, AlbumSettings, Song
from .tags import album_key, first, merge_tags, number


def _order(song: Song) -> tuple[int, int, str]:
    return (
        number(first(song.tags, 'disc')),
        number(first(song.tags, 'track')),
        song.uri,
    )


def build_albums(
    songs: Iterable[Song], settings: AlbumSettings
) -> Mapping[str, Album]:
    """Group songs into albums, keyed and ordered by album key.

    Songs without an album key are skipped. An album's songs are ordered by
    disc, track and URI; its tags are the merged tags of its songs,
    `last_modified` is the newest and `added` the oldest of its songs.

    The grouping uses a local dict for speed; nothing outside is mutated.
    """
    groups: dict[str, list[Song]] = {}
    for song in songs:
        key = album_key(song.tags, settings)
        if key is not None:
            groups.setdefault(key, []).append(song)
    return MappingProxyType(
        {key: _album(key, groups[key]) for key in sorted(groups)}
    )


def _album(key: str, songs: list[Song]) -> Album:
    ordered = sorted(songs, key=_order)
    return Album(
        key=key,
        first_uri=ordered[0].uri,
        uris=tuple(s.uri for s in ordered),
        tags=merge_tags(s.tags for s in ordered),
        last_modified=max(s.last_modified for s in ordered),
        added=min(s.added for s in ordered),
    )
