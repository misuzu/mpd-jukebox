"""Pure planning of schema migrations (§5.3.1).

Migrations are `NNNN_description.sql` files in the `migrations` directory
of this package. This module only works on their names and contents;
`store.py` reads and applies them.
"""

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

_NAME = re.compile(r'^(\d{4})_[A-Za-z0-9_]+\.sql$')


class MigrationError(Exception):
    """The shipped migration files are inconsistent (a packaging bug)."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnsupportedDatabase:
    """The database was written by a newer version of this program."""

    version: int
    latest: int


def parse_name(filename: str) -> int:
    match = _NAME.match(filename)
    if match is None:
        raise MigrationError(f'invalid migration file name: {filename!r}')
    return int(match.group(1))


def split_statements(sql: str) -> tuple[str, ...]:
    """Split SQL text into statements using SQLite's own tokenizer, so
    semicolons in strings, comments and trigger bodies are handled."""
    statements = []
    pending = ''
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if _has_code(statement):
                statements.append(statement)
            pending = ''
    if _has_code(pending):
        raise MigrationError(f'incomplete statement: {pending.strip()!r}')
    return tuple(statements)


def _has_code(text: str) -> bool:
    """True if the text has anything besides comments, whitespace and
    semicolons."""
    without_block = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    without_line = re.sub(r'--[^\n]*', '', without_block)
    return bool(without_line.replace(';', '').strip())


def plan(files: Mapping[str, str]) -> tuple[Migration, ...]:
    """Validate the migration files (name -> text) and order them."""
    migrations = sorted(
        (
            Migration(parse_name(name), name, split_statements(text))
            for name, text in files.items()
        ),
        key=lambda m: m.version,
    )
    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise MigrationError(
                f'expected migration {expected:04d}, found {migration.name}'
            )
        if not migration.statements:
            raise MigrationError(f'{migration.name} has no statements')
    if not migrations:
        raise MigrationError('no migrations found')
    return tuple(migrations)


def pending(
    current: int, migrations: tuple[Migration, ...]
) -> tuple[Migration, ...] | UnsupportedDatabase:
    latest = migrations[-1].version if migrations else 0
    if current > latest:
        return UnsupportedDatabase(current, latest)
    return tuple(m for m in migrations if m.version > current)


def latest_version(migrations: tuple[Migration, ...]) -> int:
    return migrations[-1].version if migrations else 0
