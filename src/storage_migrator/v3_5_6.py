"""Frozen conversion that retires the translation-era job kinds.

v0.4.5 narrows ``jobs.kind`` to the kinds the knowledge-base product still
submits.  The five translation-era values (``translation``, ``remove_text``,
``detect``, ``style_apply``, ``text_import``) no longer have a worker handler,
so keeping them in the CHECK constraint only advertises dead entry points.

SQLite cannot alter a CHECK constraint in place, so ``jobs`` is rebuilt through
the standard new/copy/drop/rename sequence.  The conversion refuses to run while
rows of a retired kind still exist instead of silently dropping evidence.
"""

from contextlib import closing
import logging
import sqlite3

from .contracts import StorageError, contract_sql
from .v3_5_5 import stop_unfinished as stop_unfinished_previous


LOGGER = logging.getLogger(__name__)

REBUILT_TABLES = ('jobs',)
RETIRED_KINDS = ('translation', 'remove_text', 'detect', 'style_apply', 'text_import')


def stop_unfinished(root):
    """3.5.6 only narrows a CHECK constraint and drops one index."""

    return stop_unfinished_previous(root)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _contract(table):
    with closing(sqlite3.connect(':memory:')) as target:
        target.executescript(contract_sql('3.5.6'))
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
        raise StorageError(f'3.5.6 契约缺少表: {table}')
    return row[0], indexes


def _retired_rows(db):
    placeholders = ', '.join('?' for _ in RETIRED_KINDS)
    return list(
        db.execute(
            f'SELECT kind, count(*) FROM jobs WHERE kind IN ({placeholders}) GROUP BY kind',
            RETIRED_KINDS,
        )
    )


def convert(root):
    with closing(sqlite3.connect(root / 'saber.sqlite3')) as db, db:
        db.execute('PRAGMA foreign_keys=OFF')
        present = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if 'jobs' not in present:
            raise StorageError('3.5.6 转换缺少表: jobs')
        leftovers = _retired_rows(db)
        if leftovers:
            detail = ', '.join(f'{kind}={count}' for kind, count in leftovers)
            raise StorageError(
                f'jobs 仍有已退役的任务类型记录（{detail}），请先排查数据来源再升级'
            )
        ddl, indexes = _contract('jobs')
        existing = [row[1] for row in db.execute(f'PRAGMA table_info({_quote("jobs")})')]
        temporary = 'jobs_new'
        db.execute(ddl.replace('CREATE TABLE jobs', f'CREATE TABLE {temporary}', 1))
        target = [row[1] for row in db.execute(f'PRAGMA table_info({_quote(temporary)})')]
        shared = ', '.join(_quote(column) for column in target if column in existing)
        before = db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        db.execute(
            f'INSERT INTO {_quote(temporary)} ({shared}) SELECT {shared} FROM {_quote("jobs")}'
        )
        db.execute(f'DROP TABLE {_quote("jobs")}')
        db.execute(f'ALTER TABLE {_quote(temporary)} RENAME TO {_quote("jobs")}')
        for sql in indexes:
            db.execute(sql)
        after = db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        if after != before:
            raise StorageError('jobs 重建前后行数不一致')
        LOGGER.info('jobs 表按 3.5.6 契约重建完成（%s 行）', after)

        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise StorageError('结构转换后存在无效引用')


def validate(db, *, root=None, files=False):
    from .v3_5_3 import validate as validate_previous

    validate_previous(db, root=root, files=files)
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if row is None:
        raise StorageError('缺少 jobs 表')
    for kind in RETIRED_KINDS:
        if f"'{kind}'" in row[0]:
            raise StorageError(f'jobs 表仍然接受已退役的任务类型: {kind}')
    leftovers = _retired_rows(db)
    if leftovers:
        detail = ', '.join(f'{kind}={count}' for kind, count in leftovers)
        raise StorageError(f'jobs 表仍存有已退役任务类型的记录: {detail}')
    index_names = {
        name
        for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'"
        )
    }
    if 'uq_jobs_one_nonterminal_translation_per_chapter' in index_names:
        raise StorageError('jobs 表仍保留翻译任务的唯一索引')
