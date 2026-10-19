"""Frozen conversion that retires the browser, bubble, render and translation domains."""

from contextlib import closing
import logging
import re
import sqlite3

from .contracts import StorageError, contract_sql
from .v3_5_0 import stop_unfinished as stop_unfinished_previous
from .v3_5_1 import validate as validate_previous


LOGGER = logging.getLogger(__name__)

# The whole group disappears together; children are listed before their parents.
RETIRED_TABLES = (
    'browser_session_credentials',
    'browser_session_pages',
    'browser_sessions',
    'bubbles',
    'job_font_snapshots',
    'operation_font_snapshots',
    'render_requests',
    'translation_constraints',
)
REBUILT_TABLES = ('operations', 'page_assets', 'prompts')
PROMPT_TYPES = re.compile(r'CHECK\s*\(\s*type\s+IN\s*(\([^)]*\))', re.IGNORECASE)


def stop_unfinished(root):
    """Stop unfinished work before a real conversion out of 3.5.3.

    3.5.3 already retired render_requests, so the frozen statements that touch
    that table must be skipped.
    """
    return stop_unfinished_previous(root, skip=('render_requests',))


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _contract(table):
    """Return the frozen 3.5.3 table DDL and its index DDL."""
    with closing(sqlite3.connect(':memory:')) as target:
        target.executescript(contract_sql('3.5.3'))
        row = target.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        indexes = [sql for (sql,) in target.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
    if row is None:
        raise StorageError(f'3.5.3 契约缺少表: {table}')
    return row[0], indexes


def _rebuild(db, table, ddl, indexes, where=None):
    """Rebuild one table through the standard new/copy/drop/rename sequence."""
    existing = [row[1] for row in db.execute(f'PRAGMA table_info({_quote(table)})')]
    if not existing:
        raise StorageError(f'3.5.3 转换缺少表: {table}')
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
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in RETIRED_TABLES:
            if table in present:
                db.execute(f'DROP TABLE {_quote(table)}')
        for table in REBUILT_TABLES[:-1]:
            ddl, indexes = _contract(table)
            _rebuild(db, table, ddl, indexes)
        ddl, indexes = _contract('prompts')
        match = PROMPT_TYPES.search(ddl)
        if match is None:
            raise StorageError('3.5.3 契约缺少 prompts.type 取值约束')
        legal = match.group(1)
        before = db.execute('SELECT count(*) FROM prompts').fetchone()[0]
        removed = db.execute(f'SELECT count(*) FROM prompts WHERE type NOT IN {legal}').fetchone()[0]
        _rebuild(db, 'prompts', ddl, indexes, where=f'type IN {legal}')
        after = db.execute('SELECT count(*) FROM prompts').fetchone()[0]
        if after != before - removed:
            raise StorageError('提示词取值收窄结果与预期不符')
        if removed:
            LOGGER.warning('删除 %s 条已废弃的提示词记录（type 不在 3.5.3 契约取值集合内）', removed)
        LOGGER.info('提示词记录 %s -> %s 条（删除已废弃取值 %s 条）', before, after, removed)
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise StorageError('结构转换后存在无效引用')


def validate(db, *, root=None, files=False):
    validate_previous(db, root=root, files=files)