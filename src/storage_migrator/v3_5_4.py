"""Frozen conversion that drops the three unreferenced operation snapshot tables.

Only the tables nobody reads or writes are removed. ``operations``,
``operation_events`` and ``operation_artifacts`` stay: the job executor and the
epoch convergence paths still select, update and insert into them.

The conversion also narrows ``jobs.kind`` to the values the application can
actually enqueue, which retires the half-deleted ``plugin_agent`` domain.
"""

from contextlib import closing
import logging
import re
import sqlite3

from .contracts import StorageError, contract_sql
from .v3_5_3 import stop_unfinished as stop_unfinished_previous


LOGGER = logging.getLogger(__name__)

# Nothing outside the frozen contracts mentions these tables any more.
RETIRED_TABLES = (
    'operation_credential_snapshots',
    'operation_plugin_snapshots',
    'operation_asset_inputs',
)
REBUILT_TABLES = ('jobs',)
JOB_KIND_VALUES = re.compile(r'\bkind\s+IN\s*(\([^)]*\))', re.IGNORECASE)


def stop_unfinished(root):
    """3.5.4 only removes tables no worker statement touches."""

    return stop_unfinished_previous(root)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _contract(table):
    """Return the frozen 3.5.4 table DDL and its index DDL."""

    with closing(sqlite3.connect(':memory:')) as target:
        target.executescript(contract_sql('3.5.4'))
        row = target.execute(
            'SELECT sql FROM sqlite_master WHERE type=? AND name=?', ('table', table)
        ).fetchone()
        indexes = [
            sql
            for (sql,) in target.execute(
                'SELECT sql FROM sqlite_master WHERE type=? AND tbl_name=? AND sql IS NOT NULL',
                ('index', table),
            )
        ]
    if row is None:
        raise StorageError(f'3.5.4 契约缺少表: {table}')
    return row[0], indexes


def _rebuild(db, table, ddl, indexes, where=None):
    """Rebuild one table through the standard new/copy/drop/rename sequence."""

    existing = [row[1] for row in db.execute(f'PRAGMA table_info({_quote(table)})')]
    if not existing:
        raise StorageError(f'3.5.4 转换缺少表: {table}')
    temporary = f'{table}_new'
    db.execute(ddl.replace(f'CREATE TABLE {table}', f'CREATE TABLE {temporary}', 1))
    target = [row[1] for row in db.execute(f'PRAGMA table_info({_quote(temporary)})')]
    # Only columns that exist on both sides survive, in the contract order.
    shared = ', '.join(_quote(column) for column in target if column in existing)
    statement = f'INSERT INTO {_quote(temporary)} ({shared}) SELECT {shared} FROM {_quote(table)}'
    if where:
        statement += f' WHERE {where}'
    db.execute(statement)
    db.execute(f'DROP TABLE {_quote(table)}')
    db.execute(f'ALTER TABLE {_quote(temporary)} RENAME TO {_quote(table)}')
    for sql in indexes:
        db.execute(sql)


def convert(root):
    with closing(sqlite3.connect(root / 'saber.sqlite3')) as db, db:
        db.execute('PRAGMA foreign_keys=OFF')
        present = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in RETIRED_TABLES:
            if table not in present:
                continue
            rows = db.execute(f'SELECT count(*) FROM {_quote(table)}').fetchone()[0]
            if rows:
                # Losing rows nobody can read back is still losing data; stop instead
                # of guessing whether they matter.
                raise StorageError(
                    f'{table} 仍有 {rows} 条记录，3.5.3 的代码已不再写入该表，请先排查数据来源'
                )
            db.execute(f'DROP TABLE {_quote(table)}')
            LOGGER.info('删除无引用的表 %s', table)

        ddl, indexes = _contract('jobs')
        match = JOB_KIND_VALUES.search(ddl)
        if match is None:
            raise StorageError('3.5.4 契约缺少 jobs.kind 取值约束')
        legal = match.group(1)
        before = db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        removed = db.execute(f'SELECT count(*) FROM jobs WHERE kind NOT IN {legal}').fetchone()[0]
        if removed:
            raise StorageError(
                f'{removed} 条作业的 kind 不在 3.5.4 契约取值集合内，拒绝静默删除，请先导出排查'
            )
        _rebuild(db, 'jobs', ddl, indexes, where=f'kind IN {legal}')
        after = db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        if after != before:
            raise StorageError('jobs.kind 取值收窄前后行数不一致')
        LOGGER.info('jobs 表按 3.5.4 契约重建完成（%s 行）', after)

        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise StorageError('结构转换后存在无效引用')


def validate(db, *, root=None, files=False):
    from .v3_5_3 import validate as validate_previous

    validate_previous(db, root=root, files=files)
    for table in RETIRED_TABLES:
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is not None:
            raise StorageError(f'3.5.4 仍残留已退役的表: {table}')
    contract, _ = _contract('jobs')
    match = JOB_KIND_VALUES.search(contract)
    legal = match.group(1) if match else None
    if legal is None:
        raise StorageError('3.5.4 契约缺少 jobs.kind 取值约束')
    stray = db.execute(f'SELECT count(*) FROM jobs WHERE kind NOT IN {legal}').fetchone()[0]
    if stray:
        raise StorageError(f'{stray} 条作业的 kind 不在 3.5.4 契约取值集合内')
