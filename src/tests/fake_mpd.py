"""An in-memory stand-in for python-mpd2's asyncio MPDClient."""

import asyncio
from typing import Any

from mpd.base import CommandError
from mpd.base import ConnectionError as MpdConnectionError

from mpd_jukebox import filters
from mpd_jukebox.mpd_io import song_from_mpd


def error(code: int, command: str, message: str) -> CommandError:
    return CommandError(f'[{code}@0] {{{command}}} {message}')


def track(uri: str, **tags: Any) -> dict[str, Any]:
    return {'file': uri, 'duration': '200.000', **tags}


class FakeMPD:
    def __init__(
        self,
        library: list[dict[str, Any]],
        *,
        password: str | None = None,
        stickers: bool = True,
        version: str = '0.24.0',
    ) -> None:
        self.library = library
        self.playlists: dict[str, list[str]] = {}
        self.queue: list[dict[str, Any]] = []
        self.state = 'stop'
        self.pos: int | None = None
        self.elapsed = 0.0
        self.next_id = 1
        self.required_password = password
        self.authenticated = password is None
        self.stickers_enabled = stickers
        self.stickers: dict[tuple[str, str], str] = {}
        self.mpd_version = version
        self.commands: list[str] = []
        self.fail_add: set[str] = set()
        self.connected = True
        self._idle: asyncio.Queue[Any] = asyncio.Queue()

    # -- helpers for tests -----------------------------------------------------

    def notify(self, *subsystems: str) -> None:
        self._idle.put_nowait(list(subsystems))

    def finish_song(self) -> None:
        """Consume mode: the current song ends and leaves the queue."""
        assert self.pos is not None
        del self.queue[self.pos]
        if self.pos >= len(self.queue):
            self.state, self.pos = 'stop', None
        self.elapsed = 0.0
        self.notify('playlist', 'player')

    def lose_connection(self) -> None:
        self._idle.put_nowait(MpdConnectionError('Connection lost'))

    def uris(self) -> list[str]:
        return [s['file'] for s in self.queue]

    # -- MPD commands -----------------------------------------------------------

    def _command(self, name: str) -> None:
        if not self.connected:
            raise MpdConnectionError('not connected')
        self.commands.append(name)
        if not self.authenticated and name != 'password':
            raise error(4, name, f'you don\'t have permission for "{name}"')

    async def password(self, password: str) -> None:
        self._command('password')
        if password != self.required_password:
            raise error(3, 'password', 'incorrect password')
        self.authenticated = True

    async def status(self) -> dict[str, Any]:
        self._command('status')
        status: dict[str, Any] = {
            'state': self.state,
            'playlistlength': str(len(self.queue)),
            'xfade': '0',
        }
        if self.pos is not None:
            status.update(
                songid=str(self.queue[self.pos]['id']),
                song=str(self.pos),
                elapsed=str(self.elapsed),
                duration=self.queue[self.pos].get('duration', '0'),
            )
        return status

    async def currentsong(self) -> dict[str, Any]:
        self._command('currentsong')
        return {} if self.pos is None else dict(self.queue[self.pos])

    async def playlistinfo(self) -> list[dict[str, Any]]:
        self._command('playlistinfo')
        return [dict(s) for s in self.queue]

    async def lsinfo(self, uri: str) -> list[dict[str, Any]]:
        self._command('lsinfo')
        for s in self.library:
            if s['file'] == uri:
                return [dict(s)]
        raise error(50, 'lsinfo', 'No such directory')

    def _window(self, items: list[Any], args: tuple[str, ...]) -> list[Any]:
        if len(args) >= 2 and args[-2] == 'window':
            start, end = (int(x) for x in args[-1].split(':'))
            return items[start:end]
        return items

    async def search(self, *args: str) -> list[dict[str, Any]]:
        self._command('search')
        if args[:2] == ('file', ''):
            found = self.library
        else:
            parsed = filters.parse(args[0])
            if not isinstance(parsed, filters.Filter):
                raise error(2, 'search', 'invalid filter')
            found = []
            for obj in self.library:
                song = song_from_mpd(obj)
                assert song is not None
                subject = filters.Subject(song.tags, song.uri, song.prio)
                if filters.evaluate(parsed, subject):
                    found.append(obj)
        return [dict(s) for s in self._window(found, args)]

    async def listplaylistinfo(self, name: str, *args: str):
        self._command('listplaylistinfo')
        if name not in self.playlists:
            raise error(50, 'listplaylistinfo', 'No such playlist')
        by_uri = {s['file']: s for s in self.library}
        songs = [dict(by_uri[u]) for u in self.playlists[name]]
        if args:
            start, end = (int(x) for x in args[0].split(':'))
            return songs[start:end]
        return songs

    async def sticker_find(self, kind: str, base: str, name: str):
        self._command('sticker_find')
        if not self.stickers_enabled:
            raise error(5, 'sticker', 'unknown command "sticker"')
        return [
            {'file': uri, 'sticker': f'{n}={v}'}
            for (uri, n), v in self.stickers.items()
            if n == name
        ]

    async def sticker_get(self, kind: str, uri: str, name: str) -> str:
        self._command('sticker_get')
        if (uri, name) not in self.stickers:
            raise error(50, 'sticker', 'no such sticker')
        return self.stickers[uri, name]

    async def sticker_set(self, kind: str, uri: str, name: str, value: str):
        self._command('sticker_set')
        self.stickers[uri, name] = value

    async def add(self, uri: str) -> None:
        self._command('add')
        if uri in self.fail_add:
            raise error(50, 'add', 'No such song')
        for s in self.library:
            if s['file'] == uri:
                self.queue.append({**s, 'id': self.next_id})
                self.next_id += 1
                self.notify('playlist')
                return
        raise error(50, 'add', 'No such song')

    async def play(self) -> None:
        self._command('play')
        if self.queue:
            self.state = 'play'
            if self.pos is None:
                self.pos = 0
            self.notify('player')

    async def idle(self, subsystems: list[str]):
        while True:
            item = await self._idle.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    def disconnect(self) -> None:
        self.connected = False
