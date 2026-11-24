"""SQLite persistence (§5.3): migrations, jukebox queue, play history."""

import contextlib
import importlib.resources
import os
import urllib.parse
from collections.abc import AsyncIterator

import aiosqlite

from .migrate import Migration, UnsupportedDatabase, latest_version, pending
from .migrate import plan as plan_migrations
from .model import Entry


class StoreError(Exception):
    pass


def load_migrations() -> tuple[Migration, ...]:
    """Read the SQL files shipped in the package and plan them."""
    directory = importlib.resources.files('mpd_jukebox') / 'migrations'
    files = {
        item.name: item.read_text(encoding='utf-8')
        for item in directory.iterdir()
        if item.name.endswith('.sql')
    }
    return plan_migrations(files)


def default_path() -> str:
    base = os.environ.get('XDG_STATE_HOME') or os.path.join(
        os.path.expanduser('~'), '.local', 'state'
    )
    return os.path.join(base, 'mpd-jukebox', 'state.sqlite')


class Store:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    @classmethod
    async def open(cls, path: str, *, readonly: bool = False) -> 'Store':
        if readonly:
            quoted = urllib.parse.quote(os.path.abspath(path))
            uri = f'file:{quoted}?mode=ro'
            db = await aiosqlite.connect(uri, uri=True, isolation_level=None)
        else:
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
            db = await aiosqlite.connect(path, isolation_level=None)
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    @contextlib.asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        await self._db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            await self._db.execute('ROLLBACK')
            raise
        await self._db.execute('COMMIT')

    async def version(self) -> int:
        async with self._db.execute('PRAGMA user_version') as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    async def upgrade(self, migrations: tuple[Migration, ...]) -> None:
        """Apply all pending migrations in one transaction."""
        async with self._transaction():
            todo = pending(await self.version(), migrations)
            if isinstance(todo, UnsupportedDatabase):
                raise StoreError(
                    f'database schema version {todo.version} is newer than '
                    f'this program supports ({todo.latest})'
                )
            for migration in todo:
                for statement in migration.statements:
                    await self._db.execute(statement)
                await self._db.execute(
                    f'PRAGMA user_version = {migration.version:d}'
                )

    async def check_version(self, migrations: tuple[Migration, ...]) -> None:
        """For read-only users: the schema must be exactly current."""
        version, latest = await self.version(), latest_version(migrations)
        if version != latest:
            raise StoreError(
                f'database schema version is {version}, expected {latest}; '
                'run the daemon once to upgrade it'
            )

    async def _meta(self, key: str) -> str | None:
        async with self._db.execute(
            'SELECT value FROM meta WHERE key = ?', (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else str(row[0])

    async def load_queue(self, fingerprint: str) -> tuple[Entry, ...]:
        """The saved jukebox queue, or nothing if the settings changed
        since it was saved. Records the current fingerprint."""
        async with self._transaction():
            if await self._meta('constraint_fingerprint') != fingerprint:
                await self._db.execute('DELETE FROM jukebox_queue')
                await self._db.execute(
                    'INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)',
                    ('constraint_fingerprint', fingerprint),
                )
                return ()
        return await self.queue()

    async def queue(self) -> tuple[Entry, ...]:
        async with self._db.execute(
            'SELECT key, lineno, tag_value FROM jukebox_queue ORDER BY pos'
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(Entry(str(k), int(n), str(v)) for k, n, v in rows)

    async def save_queue(self, entries: tuple[Entry, ...]) -> None:
        async with self._transaction():
            await self._db.execute('DELETE FROM jukebox_queue')
            await self._db.executemany(
                'INSERT INTO jukebox_queue (pos, key, lineno, tag_value) '
                'VALUES (?, ?, ?, ?)',
                [
                    (pos, e.key, e.lineno, e.tag_value)
                    for pos, e in enumerate(entries)
                ],
            )

    async def record_play(self, uri: str, ts: int, keep: int) -> None:
        """Append to the history, keeping the newest `keep` rows."""
        if keep <= 0:
            return
        async with self._transaction():
            await self._db.execute(
                'INSERT INTO history (uri, ts) VALUES (?, ?)', (uri, ts)
            )
            await self._db.execute(
                'DELETE FROM history WHERE id <= '
                '(SELECT MAX(id) FROM history) - ?',
                (keep,),
            )

    async def recent(self, limit: int) -> tuple[str, ...]:
        async with self._db.execute(
            'SELECT uri FROM history ORDER BY id DESC LIMIT ?', (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(str(r[0]) for r in rows)

    async def history(self) -> tuple[tuple[str, int], ...]:
        async with self._db.execute(
            'SELECT uri, ts FROM history ORDER BY id DESC'
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple((str(u), int(t)) for u, t in rows)
