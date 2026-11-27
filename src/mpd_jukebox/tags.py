"""Tag helpers: names, values, normalisation and album keys (§1.8)."""

import functools
import hashlib
import re
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
    elif settings.merge_discs:
        album = split_disc(album)[0]
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


_DISC = (
    r'(?<!\w)(?:cd|dis[ck])\s*\.?\s*(\d{1,2})(?:\s*(?:of|/)\s*\d{1,2})?(?!\w)'
)
_SEP = r'[-–:,／]'
# Tried in order; the first that matches wins.
_DISC_PATTERNS = (
    # "Album (CD 2)", "Album [Disc 3 of 4]": the brackets hold nothing else.
    re.compile(rf'\s*[(\[]\s*{_DISC}\s*[)\]]', re.IGNORECASE),
    # "Album - Disc 2", "Album -Disc1-", "Album CD2 (Mixed by X)": at the
    # end, or just before a final bracket, which is kept.
    re.compile(
        rf'(?:\s*{_SEP})?\s*{_DISC}\s*-?(?=\s*(?:$|[(\[]))', re.IGNORECASE
    ),
    # "Album (CD1 - Subtitle)": a bracket that starts with the disc goes.
    re.compile(rf'\s*[(\[]\s*{_DISC}[^)\]]*[)\]]', re.IGNORECASE),
    # "Album (Edition: Disc 2)": the disc and its subtitle, up to the
    # closing bracket.
    re.compile(rf'\s*{_SEP}\s*{_DISC}[^)\]]*(?=[)\]])', re.IGNORECASE),
    # "Album CD1 Subtitle", "Album Disc 01／Side A": the disc and its
    # subtitle, to the end.
    re.compile(rf'(?:\s*{_SEP})?\s+{_DISC}.*$', re.IGNORECASE),
)


_BRACKET_SPACE = re.compile(r'(?<=[(\[])\s+|\s+(?=[)\]])')


def _balanced(text: str) -> bool:
    return text.count('(') == text.count(')') and text.count(
        '['
    ) == text.count(']')


@functools.lru_cache(maxsize=1 << 16)  # pure; a library has few Album tags
def split_disc(album: str) -> tuple[str, int]:
    """An Album tag without its disc number (and the disc's subtitle), and
    the disc number, 0 for none (§7.1). For example `("Album", 2)` for
    "Album (CD 2)", "Album [Disc 2 of 3]", "Album - Disc 2", "Album CD2
    Bonus Disc" or "Album (CD2 - Live)", and `("Album (Mixed by X)", 2)`
    for "Album CD2 (Mixed by X)". Only CD, Disc and Disk count: volumes
    and parts are separate albums. A disc inside a bracket that does not
    start with it or follow a separator ("[ABC CD8]", a catalogue number,
    or "(single CD1)") is left alone, and so is a tag that is nothing but
    a disc number."""
    for pattern in _DISC_PATTERNS:
        for match in pattern.finditer(album):
            removed = album[match.start() : match.end()]
            rest = album[: match.start()] + ' ' + album[match.end() :]
            base = _BRACKET_SPACE.sub('', ' '.join(rest.split()))
            if base and _balanced(removed) and _balanced(base):
                return base, int(match.group(1))
    return album, 0


def disc_number(tags: Tags) -> int:
    """A song's disc: from a disc number in its Album tag (`split_disc`),
    which is explicit, else from its Disc tag; 0 if neither has one."""
    album = first(tags, 'album')
    disc = split_disc(album)[1] if album is not None else 0
    return disc or number(first(tags, 'disc'))
