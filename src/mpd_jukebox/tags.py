"""Tag helpers: names, values, normalisation and album keys (§1.8)."""

import hashlib
import unicodedata
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from .model import AlbumSettings, Tags

# MPD tag names, lower case, as python-mpd2 reports them.
MPD_TAGS: frozenset[str] = frozenset(
    {
        'artist',
        'artistsort',
        'album',
        'albumsort',
        'albumartist',
        'albumartistsort',
        'title',
        'titlesort',
        'track',
        'name',
        'genre',
        'mood',
        'date',
        'originaldate',
        'composer',
        'composersort',
        'performer',
        'conductor',
        'work',
        'ensemble',
        'movement',
        'movementnumber',
        'showmovement',
        'location',
        'grouping',
        'comment',
        'disc',
        'label',
        'musicbrainz_artistid',
        'musicbrainz_albumid',
        'musicbrainz_albumartistid',
        'musicbrainz_trackid',
        'musicbrainz_releasegroupid',
        'musicbrainz_releasetrackid',
        'musicbrainz_workid',
    }
)

MBID_LENGTH = 36
UNKNOWN_ALBUM = 'Unknown Album'

_DROPPED_CATEGORIES = frozenset({'Mn', 'Me', 'Cc', 'Cf'})


def canonical_tag(name: str) -> str | None:
    """Return the lower-case MPD tag name, or None if it is not a tag."""
    lowered = name.lower()
    return lowered if lowered in MPD_TAGS else None


def normalize(text: str) -> str:
    """Approximate myMPD's utf8proc normalisation: compatibility
    decomposition, case folding, and removal of combining marks and control
    characters."""
    decomposed = unicodedata.normalize('NFKD', text.casefold())
    kept = ''.join(
        c
        for c in decomposed
        if unicodedata.category(c) not in _DROPPED_CATEGORIES
    )
    return unicodedata.normalize('NFC', kept)


def values(tags: Tags, tag: str) -> tuple[str, ...]:
    return tags.get(tag, ())


def first(tags: Tags, tag: str) -> str | None:
    found = values(tags, tag)
    return found[0] if found else None


def joined(tags: Tags, tag: str) -> str:
    """All values of a tag joined with ', ', like myMPD."""
    return ', '.join(values(tags, tag))


def basename(uri: str) -> str:
    return uri.rstrip('/').rsplit('/', 1)[-1]


def tag_value(tags: Tags, tag: str, uri: str) -> str:
    """The value used for the uniq-tag constraint.

    As in myMPD, a missing title falls back to the name tag and then to the
    file name.
    """
    value = joined(tags, tag)
    if value or tag != 'title':
        return value
    return joined(tags, 'name') or basename(uri)


def album_key(tags: Tags, settings: AlbumSettings) -> str | None:
    """The album key of a song or album, or None if it has none."""
    if settings.use_mbid:
        mbid = first(tags, 'musicbrainz_albumid')
        if mbid is not None and len(mbid) == MBID_LENGTH:
            return mbid
    artist = joined(tags, 'albumartist') or joined(tags, 'artist')
    if not artist:
        return None
    album = first(tags, 'album')
    if album is None:
        if not settings.unknown:
            return None
        album = UNKNOWN_ALBUM
    key = f'{artist}::{album}'
    if settings.group_tag is not None:
        group = first(tags, settings.group_tag)
        if group is not None:
            key = f'{key}::{group}'
    return hashlib.sha1(key.encode()).hexdigest()


def merge_tags(tag_sets: Iterable[Tags]) -> Tags:
    """Merge tags of several songs: each tag keeps its distinct values in
    order of first appearance."""
    merged: dict[str, list[str]] = {}
    for tags in tag_sets:
        for name, vals in tags.items():
            known = merged.setdefault(name, [])
            known.extend(v for v in vals if v not in known)
    return _freeze(merged)


def _freeze(tags: Mapping[str, list[str]]) -> Tags:
    return MappingProxyType({k: tuple(v) for k, v in tags.items()})


def number(value: str | None) -> int:
    """Parse '3' or '3/12' to 3; anything else is 0."""
    if not value:
        return 0
    head = value.split('/', 1)[0].strip()
    return int(head) if head.isdigit() else 0
