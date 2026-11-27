"""Album index: songs grouped by album key (§1.8)."""

import itertools
import posixpath
import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from .model import Album, AlbumSettings, Song
from .tags import album_key, first, merge_tags, normalize, number


def _order(song: Song) -> tuple[int, int, str]:
    return (
        number(first(song.tags, 'disc')),
        number(first(song.tags, 'track')),
        song.uri,
    )


def _order_by_directory(song: Song) -> tuple[int, str, int, str]:
    # The CUE sheet, not its folder, so that `CD 1.cue` and `CD 2.cue` in
    # one folder do not interleave.
    return (
        number(first(song.tags, 'disc')),
        posixpath.dirname(song.uri),
        number(first(song.tags, 'track')),
        song.uri,
    )


_CUE_TRACK = re.compile(r'track\d{4}')


def folder(uri: str) -> str:
    """The folder a song's file is in. MPD lists the tracks of a CUE sheet
    (or of an embedded one) as `dir/image.cue/track0001`; their folder is
    `dir`, the one holding the sheet."""
    head, name = posixpath.split(uri)
    if _CUE_TRACK.fullmatch(name):
        return posixpath.dirname(head)
    return head


def _titles(songs: Iterable[Song]) -> set[str]:
    return {normalize(t) for s in songs if (t := first(s.tags, 'title'))}


def _discs(songs: Iterable[Song]) -> set[int]:
    return {number(first(s.tags, 'disc')) for s in songs}


def _distinct(a: list[Song], b: list[Song]) -> bool:
    """Whether two sets of songs of a key are parts of one album (discs, or
    an album and its bonus disc) rather than copies of it: no title in
    common, or Disc tags on every song that tell them apart."""
    if not _titles(a) & _titles(b):
        return True
    discs_a, discs_b = _discs(a), _discs(b)
    return 0 not in discs_a and 0 not in discs_b and not discs_a & discs_b


def _depth(home: str) -> int:
    return home.count('/') + 1 if home else 0


def album_parts(
    songs: Iterable[Song],
) -> tuple[tuple[str, tuple[Song, ...]], ...]:
    """The songs of one album key, split into candidate albums (§7.1), as
    (album directory, songs) pairs ordered by directory.

    1. Songs are grouped by folder (`folder`).
    2. Sibling folders below a common parent, other than the library root,
       are discs of one album in the parent if every two of them are
       `_distinct`; otherwise (copies of an album side by side) each stays
       a candidate of its own.
    3. Deepest first, a candidate joins the closest candidate in a
       directory above it (or in the same one) if the two are `_distinct`:
       a bonus disc in a subfolder of the album, say. The library root
       never takes others in.
    """
    folders: dict[str, list[Song]] = {}
    for song in songs:
        folders.setdefault(folder(song.uri), []).append(song)
    siblings: dict[str, list[str]] = {}
    for name in folders:
        parent = posixpath.dirname(name)
        if parent:
            siblings.setdefault(parent, []).append(name)
    disc_parents = {
        parent
        for parent, names in siblings.items()
        if len(names) > 1
        and all(
            _distinct(folders[a], folders[b])
            for a, b in itertools.combinations(names, 2)
        )
    }
    merged: dict[str, list[Song]] = {}
    parts: list[tuple[str, list[Song]]] = []
    for name in sorted(folders):
        parent = posixpath.dirname(name)
        if parent in disc_parents:
            if parent not in merged:
                merged[parent] = []
                parts.append((parent, merged[parent]))
            merged[parent].extend(folders[name])
        else:
            parts.append((name, list(folders[name])))

    alive = sorted(parts, key=lambda p: (-_depth(p[0]), p[0]))
    for part in list(alive):
        home, part_songs = part
        above = [
            other
            for other in alive
            if other is not part
            and other[0]
            and (other[0] == home or home.startswith(other[0] + '/'))
        ]
        if not above:
            continue
        target = max(above, key=lambda p: _depth(p[0]))
        if _distinct(part_songs, target[1]):
            target[1].extend(part_songs)
            alive.remove(part)
    return tuple(
        (home, tuple(part_songs)) for home, part_songs in sorted(alive)
    )


def build_albums(
    songs: Iterable[Song], settings: AlbumSettings
) -> Mapping[str, Album]:
    """Group songs into albums, keyed and ordered by album key.

    Songs without an album key are skipped. An album's songs are ordered by
    disc, track and URI; its tags are the merged tags of its songs,
    `last_modified` is the newest and `added` the oldest of its songs.

    With `split_by_directory`, an album is the largest candidate album of
    its key (`album_parts`): one folder, possibly with disc or bonus-disc
    subfolders. Songs of the key elsewhere, such as stray copies in a
    download folder or another edition, are left out. Songs are then ordered
    by disc, directory, track and URI, so disc folders without Disc tags
    play in order.

    The grouping uses local dicts for speed; nothing outside is mutated.
    """
    groups: dict[str, list[Song]] = {}
    for song in songs:
        key = album_key(song.tags, settings)
        if key is not None:
            groups.setdefault(key, []).append(song)
    if settings.split_by_directory:
        return MappingProxyType(
            {
                key: _album(key, _main_part(groups[key]), by_directory=True)
                for key in sorted(groups)
            }
        )
    return MappingProxyType(
        {key: _album(key, groups[key]) for key in sorted(groups)}
    )


def _main_part(songs: list[Song]) -> list[Song]:
    """The candidate album (`album_parts`) with the most songs; a tie goes
    to the first by directory."""
    parts = album_parts(songs)
    _, best = min(parts, key=lambda p: (-len(p[1]), p[0]))
    return list(best)


def _album(key: str, songs: list[Song], by_directory: bool = False) -> Album:
    if by_directory:
        ordered = sorted(songs, key=_order_by_directory)
    else:
        ordered = sorted(songs, key=_order)
    return Album(
        key=key,
        first_uri=ordered[0].uri,
        uris=tuple(s.uri for s in ordered),
        tags=merge_tags(s.tags for s in ordered),
        last_modified=max(s.last_modified for s in ordered),
        added=min(s.added for s in ordered),
    )
