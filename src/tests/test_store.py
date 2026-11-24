import os
import sqlite3
import tempfile
import unittest

from mpd_jukebox.migrate import (
    Migration,
    MigrationError,
    UnsupportedDatabase,
    latest_version,
    parse_name,
    pending,
    plan,
    split_statements,
)
from mpd_jukebox.model import Entry
from mpd_jukebox.store import Store, StoreError, default_path, load_migrations


def schema(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(
            conn.execute(
                'SELECT type, name, sql FROM sqlite_master '
                "WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )
    finally:
        conn.close()


class MigrateTestCase(unittest.TestCase):
    def test_parse_name(self):
        self.assertEqual(parse_name('0002_add_index.sql'), 2)
        for bad in ('2_x.sql', '0002-x.sql', '0002_x.txt', '0002_.sql'):
            with self.subTest(bad), self.assertRaises(MigrationError):
                parse_name(bad)

    def test_split_statements(self):
        sql = """
            -- comment; with a semicolon
            CREATE TABLE t (v TEXT DEFAULT 'a;b');
            /* block; comment */
            CREATE TRIGGER tr AFTER INSERT ON t BEGIN
                UPDATE t SET v = 'x;y';
            END;
            INSERT INTO t VALUES ('z'); -- trailing
        """
        statements = split_statements(sql)
        self.assertEqual(len(statements), 3)
        self.assertIn("'a;b'", statements[0])
        self.assertTrue(statements[1].rstrip().endswith('END;'))
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        for statement in statements:
            conn.execute(statement)
        self.assertEqual(
            conn.execute('SELECT v FROM t').fetchall(), [('x;y',)]
        )

    def test_split_incomplete(self):
        with self.assertRaises(MigrationError):
            split_statements('CREATE TABLE t (v TEXT')

    def test_plan(self):
        migrations = plan(
            {'0002_b.sql': 'SELECT 2;', '0001_a.sql': 'SELECT 1;'}
        )
        self.assertEqual([m.version for m in migrations], [1, 2])
        self.assertEqual(latest_version(migrations), 2)
        self.assertEqual(latest_version(()), 0)

    def test_plan_errors(self):
        cases = [
            {},
            {'0002_b.sql': 'SELECT 1;'},
            {'0001_a.sql': 'SELECT 1;', '0001_b.sql': 'SELECT 1;'},
            {'0001_a.sql': 'SELECT 1;', '0003_c.sql': 'SELECT 1;'},
            {'0001_a.sql': '-- nothing'},
        ]
        for files in cases:
            with self.subTest(files), self.assertRaises(MigrationError):
                plan(files)

    def test_pending(self):
        migrations = (Migration(1, 'a', ('x',)), Migration(2, 'b', ('y',)))
        self.assertEqual(pending(0, migrations), migrations)
        self.assertEqual(pending(1, migrations), migrations[1:])
        self.assertEqual(pending(2, migrations), ())
        self.assertEqual(pending(3, migrations), UnsupportedDatabase(3, 2))

    def test_shipped_migrations(self):
        migrations = load_migrations()
        self.assertGreaterEqual(len(migrations), 1)
        for migration in migrations:
            self.assertTrue(migration.statements)


class StoreTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'sub', 'state.sqlite')

    def tearDown(self):
        self._tmp.cleanup()

    async def open(self, readonly=False):
        store = await Store.open(self.path, readonly=readonly)
        self.addAsyncCleanup(store.close)
        return store

    async def test_upgrade_is_idempotent(self):
        store = await self.open()
        migrations = load_migrations()
        await store.upgrade(migrations)
        self.assertEqual(await store.version(), latest_version(migrations))
        before = schema(self.path)
        await store.upgrade(migrations)
        self.assertEqual(schema(self.path), before)

    async def test_schema_equivalence(self):
        extra = Migration(
            2, '0002_x.sql', ('CREATE INDEX h ON history (uri)',)
        )
        migrations = load_migrations()[:1] + (extra,)
        store = await self.open()
        await store.upgrade(migrations[:1])
        await store.upgrade(migrations)
        stepwise = schema(self.path)

        fresh = os.path.join(self._tmp.name, 'fresh.sqlite')
        other = await Store.open(fresh)
        await other.upgrade(migrations)
        await other.close()
        self.assertEqual(schema(fresh), stepwise)

    async def test_failed_migration_rolls_back(self):
        store = await self.open()
        migrations = load_migrations()
        await store.upgrade(migrations)
        version = await store.version()
        before = schema(self.path)
        broken = Migration(
            version + 1,
            'broken',
            ('CREATE TABLE extra (x)', 'SELECT * FROM no_such_table'),
        )
        with self.assertRaises(sqlite3.OperationalError):
            await store.upgrade(migrations + (broken,))
        self.assertEqual(await store.version(), version)
        self.assertEqual(schema(self.path), before)

    async def test_newer_database_is_refused(self):
        store = await self.open()
        await store.upgrade(load_migrations())
        with self.assertRaises(StoreError):
            await store.upgrade(())

    async def test_queue(self):
        store = await self.open()
        await store.upgrade(load_migrations())
        self.assertEqual(await store.load_queue('fp1'), ())
        entries = (Entry('a', 1, 'x'), Entry('b', 2, 'y'))
        await store.save_queue(entries)
        self.assertEqual(await store.load_queue('fp1'), entries)
        await store.save_queue(entries[1:])
        self.assertEqual(await store.queue(), entries[1:])
        # Changed settings discard the saved queue.
        self.assertEqual(await store.load_queue('fp2'), ())
        self.assertEqual(await store.queue(), ())

    async def test_history(self):
        store = await self.open()
        await store.upgrade(load_migrations())
        for i in range(5):
            await store.record_play(f'u{i}', 100 + i, keep=3)
        self.assertEqual(await store.recent(10), ('u4', 'u3', 'u2'))
        self.assertEqual(await store.recent(1), ('u4',))
        self.assertEqual(
            await store.history(), (('u4', 104), ('u3', 103), ('u2', 102))
        )
        await store.record_play('ignored', 1, keep=0)
        self.assertEqual(len(await store.history()), 3)

    async def test_readonly(self):
        store = await self.open()
        await store.upgrade(load_migrations())
        await store.save_queue((Entry('a', 1, 'x'),))
        reader = await self.open(readonly=True)
        await reader.check_version(load_migrations())
        self.assertEqual(len(await reader.queue()), 1)
        with self.assertRaises(sqlite3.OperationalError):
            await reader.save_queue(())

    async def test_readonly_version_mismatch(self):
        store = await self.open()
        await store.upgrade(load_migrations())
        reader = await self.open(readonly=True)
        extra = Migration(99, 'x', ('SELECT 1',))
        with self.assertRaises(StoreError):
            await reader.check_version(load_migrations() + (extra,))


class DefaultPathTestCase(unittest.TestCase):
    def test_xdg(self):
        old = os.environ.get('XDG_STATE_HOME')
        try:
            os.environ['XDG_STATE_HOME'] = '/state'
            self.assertEqual(default_path(), '/state/mpd-jukebox/state.sqlite')
            del os.environ['XDG_STATE_HOME']
            self.assertTrue(
                default_path().endswith(
                    '.local/state/mpd-jukebox/state.sqlite'
                )
            )
        finally:
            if old is not None:
                os.environ['XDG_STATE_HOME'] = old
