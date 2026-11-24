"""Talking to MPD (§5.2).

The `*_from_mpd` functions are pure parsers of python-mpd2 results. `MpdIO`
is a thin async wrapper around a connected python-mpd2 asyncio client.
"""

import datetime
import logging
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from mpd.base import CommandError, FailureResponseCode

from .model import PlayState, Song, Status, freeze_tags
from .tags import MPD_TAGS

log = logging.getLogger(__name__)

WINDOW = 10_000

# MPD errors that a retry cannot fix.
FATAL_ERRORS = frozenset(
    {FailureResponseCode.PASSWORD, FailureResponseCode.PERMISSION}
)


class FatalMpdError(Exception):
    """Wrong password or missing permission: retrying cannot help."""


def check_fatal(error: CommandError) -> None:
    if error.errno in FATAL_ERRORS:
        raise FatalMpdError(error.msg or str(error)) from error


# --------------------------------------------------------------------------
# Pure parsers


def _text(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ''
    return str(value)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(_text(value).split(':')[0])
    except (TypeError, ValueError):
        return default


def parse_time(value: Any) -> int:
    """ISO 8601 timestamp from MPD (`2024-01-02T03:04:05Z`) to epoch."""
    try:
        text = _text(value).replace('Z', '+00:00')
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return int(parsed.timestamp())


def song_from_mpd(obj: Mapping[str, Any]) -> Song | None:
    uri = obj.get('file')
    if not uri:
        return None
    tags = {
        key: tuple(str(v) for v in value)
        if isinstance(value, list)
        else (str(value),)
        for key, value in obj.items()
        if key in MPD_TAGS
    }
    duration = obj.get('duration', obj.get('time'))
    return Song(
        uri=_text(uri),
        tags=freeze_tags(tags),
        duration=_number(duration),
        prio=int(_number(obj.get('prio'))),
        last_modified=parse_time(obj.get('last-modified', '')),
        added=parse_time(obj.get('added', '')),
    )


def songs_from_mpd(objs: Sequence[Mapping[str, Any]]) -> tuple[Song, ...]:
    return tuple(s for s in map(song_from_mpd, objs) if s is not None)


def status_from_mpd(
    status: Mapping[str, Any], current: Mapping[str, Any] | None
) -> Status:
    try:
        state = PlayState(_text(status.get('state', 'stop')))
    except ValueError:
        state = PlayState.STOP
    song_id = status.get('songid')
    duration = status.get('duration')
    if duration is None and 'time' in status:
        # Old MPD: "time: elapsed:total"
        duration = _text(status['time']).partition(':')[2]
    return Status(
        state=state,
        queue_length=int(_number(status.get('playlistlength'))),
        song_id=None if song_id is None else int(_number(song_id)),
        song_uri=_text(current['file'])
        if current and 'file' in current
        else None,
        elapsed=_number(status.get('elapsed')),
        duration=_number(duration),
        crossfade=int(_number(status.get('xfade'))),
    )


def stickers_from_mpd(
    objs: Sequence[Mapping[str, Any]], name: str
) -> Mapping[str, str]:
    """`sticker find` results to uri -> value."""
    result = {}
    for obj in objs:
        uri = obj.get('file')
        sticker_name, sep, value = _text(obj.get('sticker', '')).partition('=')
        if uri and sep and sticker_name == name:
            result[_text(uri)] = value
    return MappingProxyType(result)


def supports_playlist_window(version: str | None) -> bool:
    """`listplaylistinfo NAME START:END` needs MPD 0.24."""
    if not version:
        return False
    try:
        parts = tuple(int(p) for p in version.split('.')[:2])
    except ValueError:
        return False
    return parts >= (0, 24)


# --------------------------------------------------------------------------
# Async wrapper


class MpdIO:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def password(self, password: str) -> None:
        try:
            await self.client.password(password)
        except CommandError as e:
            check_fatal(e)
            raise

    async def status(self) -> Status:
        status = await self.client.status()
        current = (
            await self.client.currentsong() if 'songid' in status else None
        )
        return status_from_mpd(status, current)

    async def queue_songs(self) -> tuple[Song, ...]:
        return songs_from_mpd(await self.client.playlistinfo())

    async def song(self, uri: str) -> Song | None:
        try:
            found = songs_from_mpd(await self.client.lsinfo(uri))
        except CommandError as e:
            check_fatal(e)
            return None
        return found[0] if found else None

    async def search_all(self, start: int, end: int) -> tuple[Song, ...]:
        """Every song of the database, one window at a time."""
        return songs_from_mpd(
            await self.client.search('file', '', 'window', f'{start}:{end}')
        )

    async def search(
        self, expression: str, start: int, end: int
    ) -> tuple[Song, ...]:
        return songs_from_mpd(
            await self.client.search(expression, 'window', f'{start}:{end}')
        )

    def playlist_window_supported(self) -> bool:
        return supports_playlist_window(self.client.mpd_version)

    async def playlist(
        self, name: str, start: int | None = None, end: int | None = None
    ) -> tuple[Song, ...]:
        if start is None or end is None:
            return songs_from_mpd(await self.client.listplaylistinfo(name))
        return songs_from_mpd(
            await self.client.listplaylistinfo(name, f'{start}:{end}')
        )

    async def stickers(self, name: str) -> Mapping[str, str] | None:
        """All song stickers with this name, or None if MPD has no sticker
        database."""
        try:
            found = await self.client.sticker_find('song', '', name)
        except CommandError as e:
            check_fatal(e)
            log.debug('stickers unavailable: %s', e)
            return None
        return stickers_from_mpd(found, name)

    async def add(self, uri: str) -> bool:
        try:
            await self.client.add(uri)
        except CommandError as e:
            check_fatal(e)
            log.error('Adding %s to the queue failed: %s', uri, e)
            return False
        log.info('Jukebox adding song: %s', uri)
        return True

    async def play(self) -> None:
        try:
            await self.client.play()
        except CommandError as e:
            check_fatal(e)
            log.error('Start playing failed: %s', e)

    async def record_play_stickers(self, uri: str, started_at: int) -> None:
        """Like myMPD: playCount + 1 and lastPlayed = song start time."""
        try:
            try:
                count = int(
                    await self.client.sticker_get('song', uri, 'playCount')
                )
            except CommandError as e:
                if e.errno is not FailureResponseCode.NO_EXIST:
                    raise
                count = 0
            except ValueError:
                count = 0
            await self.client.sticker_set(
                'song', uri, 'playCount', str(count + 1)
            )
            await self.client.sticker_set(
                'song', uri, 'lastPlayed', str(started_at)
            )
        except CommandError as e:
            check_fatal(e)
            log.warning('Setting stickers for %s failed: %s', uri, e)
