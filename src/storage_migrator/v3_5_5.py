"""Frozen conversion that lets ``analysis_runs.scope`` record a derived rebuild.

v0.4.4 adds the ``derived`` scope: re-run only the whole-book post-processing
(layer aggregates, overviews, timeline, entity alignment, vectors) while reusing
the already published page analyses. SQLite cannot alter a CHECK constraint in
place, so ``analysis_runs`` is rebuilt through the standard new/copy/drop/rename
sequence; no other table changes and no rows are dropped.
"""

from contextlib import closing
import logging
import sqlite3

from .contracts import StorageError, contract_sql
from .v3_5_4 import stop_unfinished as stop_unfinished_previous


LOGGER = logging.getLogger(__name__)

REBUILT_TABLES = ('analysis_runs',)


def stop_unfinished(root):
    """3.5.5 only widens a CHECK constraint; it cannot lose work."""

    return stop_unfinished_previous(root)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _contract(table):
    with closing(sqlite3.connect(':memory:')) as target:
        target.executescript(contract_sql('3.5.5'))
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
        raise StorageError(f'3.5.5 契约缺少表: {table}')
    return row[0], indexes


def _rebuild(db, table, ddl, indexes):
    existing = [row[1] for row in db.execute(f'PRAGMA table_info({_quote(table)})')]
    if not existing:
        raise StorageError(f'3.5.5 转换缺少表: {table}')
    temporary = f'{table}_new'
    db.execute(ddl.replace(f'CREATE TABLE {table}', f'CREATE TABLE {temporary}', 1))
    target = [row[1] for row in db.execute(f'PRAGMA table_info({_quote(temporary)})')]
    shared = ', '.join(_quote(column) for column in target if column in existing)
    db.execute(
        f'INSERT INTO {_quote(temporary)} ({shared}) SELECT {shared} FROM {_quote(table)}'
    )
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
        if 'analysis_runs' not in present:
            raise StorageError('3.5.5 转换缺少表: analysis_runs')
        ddl, indexes = _contract('analysis_runs')
        before = db.execute('SELECT count(*) FROM analysis_runs').fetchone()[0]
        _rebuild(db, 'analysis_runs', ddl, indexes)
        after = db.execute('SELECT count(*) FROM analysis_runs').fetchone()[0]
        if after != before:
            raise StorageError('analysis_runs 重建前后行数不一致')
        LOGGER.info('analysis_runs 表按 3.5.5 契约重建完成（%s 行）', after)

        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise StorageError('结构转换后存在无效引用')


def validate(db, *, root=None, files=False):
    from .v3_5_3 import validate as validate_previous

    validate_previous(db, root=root, files=files)
    contract, _ = _contract('analysis_runs')
    if "'derived'" not in contract:
        raise StorageError('3.5.5 契约缺少 analysis_runs.scope 的 derived 取值')
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='analysis_runs'"
    ).fetchone()
    if row is None or "'derived'" not in row[0]:
        raise StorageError('analysis_runs 未按 3.5.5 契约记录 derived 取值')
