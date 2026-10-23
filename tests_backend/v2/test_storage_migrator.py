from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import psutil
import pytest

from src.version import STORAGE_VERSION, parse_version
from src.backend_v2.storage.lifecycle import initialize_database, schema_smoke_test
from src.backend_v2.storage.schema import (
    OPERATION_KINDS, PAGE_ASSET_ROLES, PROMPT_TYPES,
    assets, books, chapters, operations, page_assets, pages, prompts, schema_metadata,
)
from src.storage_migrator.contracts import StorageError, contract_sql, read_identity, validate_schema
from src.storage_migrator.control import DataRootLock, DataRootAlreadyLocked, business_ready, atomic_json, control_root, registered_process, wait_for_children
from src.storage_migrator.registry import Migration, migration_chain
from src.storage_migrator.runner import StorageManager, OWNER_FILE
from src.storage_migrator.paths import filesystem_path


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    root = tmp_path_factory.mktemp("version-baseline") / "data"
    initialize_database(root)
    # The migration protocol fixtures need existing files, not real font payloads.
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        # The fixtures only need a schema matching the stamped version. Now that
        # the 3.5.2 contract differs from the current schema, that source is the
        # current version itself; protocol tests step out of it synthetically.
        db.execute("UPDATE schema_metadata SET storage_version=?", (STORAGE_VERSION,))
        for (relative,) in db.execute("SELECT relative_path FROM fonts"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"font fixture")
    return root


@pytest.fixture
def root(tmp_path, baseline):
    result = tmp_path / "data"
    shutil.copytree(baseline, result)
    (result / "objects").mkdir(exist_ok=True)
    (result / "objects" / "saved.txt").write_text("用户已保存的成果", encoding="utf-8")
    return result


def convert(root):
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        db.execute("ALTER TABLE app_settings ADD COLUMN migration_test TEXT")
    (root / "objects" / "saved.txt").write_text("已转换的成果", encoding="utf-8")


def sql_for(version):
    sql = contract_sql(STORAGE_VERSION)
    if version != STORAGE_VERSION:
        sql += "\nALTER TABLE app_settings ADD COLUMN migration_test TEXT;"
    return sql


def manager(root, **kwargs):
    return StorageManager(root, "local", target="3.6.0", steps=(Migration(STORAGE_VERSION, "3.6.0", convert),), sql_for=sql_for, **kwargs)


def test_version_numeric_and_registry():
    assert STORAGE_VERSION == STORAGE_VERSION
    assert parse_version("3.5.10") > parse_version("3.5.9")
    for value in ("3.5", "3.05.0", "3.6.0-rc1", "v3.5.1"):
        with pytest.raises(ValueError):
            parse_version(value)
    with pytest.raises(StorageError, match="降级"):
        migration_chain("3.6.0", STORAGE_VERSION)
    with pytest.raises(StorageError, match="缺少"):
        migration_chain(STORAGE_VERSION, "3.6.1", [Migration(STORAGE_VERSION, "3.6.0")])


def test_current_check_and_readonly_cli(root):
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        db.execute("UPDATE schema_metadata SET storage_version=?", (STORAGE_VERSION,))
    assert StorageManager(root, "local").check()["status"] == "current"
    result = subprocess.run([sys.executable, "saber_v2.py", "--role", "storage-migrator", "--action", "check", "--data-dir", str(root)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "current"
    assert not control_root(root).exists()


def test_empty_check_does_not_create_directories(tmp_path):
    root = tmp_path / "new"
    assert StorageManager(root, "local").check()["status"] == "new"
    assert not root.exists() and not control_root(root).exists()
    root.mkdir()
    (root / "launcher-settings.json").write_text("{}")
    with pytest.raises(StorageError, match="旧数据"):
        StorageManager(root, "local").check()
    assert not (root / "saber.sqlite3").exists()


def test_unversioned_database_is_never_stamped(root):
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        db.execute("ALTER TABLE schema_metadata DROP COLUMN storage_version")
    before = (root / "saber.sqlite3").read_bytes()
    with StorageManager(root, "local") as migration:
        with pytest.raises(StorageError, match="无版本"):
            migration.prepare(initialize_database)
    assert (root / "saber.sqlite3").read_bytes() == before


def test_profile_schema_and_json_rejected(root):
    with pytest.raises(StorageError, match="模式"):
        StorageManager(root, "public").check()
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        db.execute("UPDATE app_settings SET payload_json='broken'")
    with pytest.raises(StorageError, match="JSON"):
        StorageManager(root, "local").check()
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        db.execute("DROP INDEX ix_process_epochs_role_status")
    with pytest.raises(StorageError, match="结构不匹配"):
        schema_smoke_test(root / "saber.sqlite3")


def test_351_frozen_contract_upgrade_preserves_user_data(tmp_path):
    assert contract_sql("3.5.1") == contract_sql("3.5.2")
    root = tmp_path / "old"
    root.mkdir()
    (root / "objects").mkdir()
    (root / "objects" / "saved.txt").write_text("用户已保存的成果", encoding="utf-8")
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        db.executescript(contract_sql("3.5.2"))
        insert_row(db, schema_metadata, singleton_id=1, runtime_profile="local", storage_version="3.5.2")
        insert_row(db, books, id="book", title="book")
    saved = (root / "objects" / "saved.txt").read_bytes()
    with StorageManager(root, "local") as migration:
        assert migration.check()["status"] == "upgrade_required"
        assert migration.prepare(initialize_database)["status"] == "awaiting_health"
        assert read_identity(root)[0] == STORAGE_VERSION
        migration.before_start()
        assert migration.confirm_ready() == []
        assert business_ready(root)
        assert migration.check()["status"] == "current"
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        assert db.execute("SELECT id,title FROM books").fetchall() == [("book", "book")]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables.isdisjoint(RETIRED_DOMAINS)
    assert (root / "objects" / "saved.txt").read_bytes() == saved


RETIRED_DOMAINS = (
    "browser_session_credentials",
    "browser_session_pages",
    "browser_sessions",
    "bubbles",
    "job_font_snapshots",
    "operation_font_snapshots",
    "render_requests",
    "translation_constraints",
)


def insert_row(db, table, **values):
    unknown = set(values) - {column.name for column in table.c}
    assert not unknown, f"{table.name} 没有这些字段: {sorted(unknown)}"
    columns = [column for column in table.c if column.name in values]
    placeholders = ", ".join("?" for _ in columns)
    db.execute(
        f'INSERT INTO "{table.name}"({", ".join(column.name for column in columns)}) VALUES ({placeholders})',
        tuple(values[column.name] for column in columns),
    )


def test_352_upgrade_retires_legacy_domains_and_narrows_prompt_types(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    (root / "objects").mkdir()
    (root / "objects" / "saved.txt").write_text("用户已保存的成果", encoding="utf-8")
    (root / "objects" / "asset.png").write_bytes(b"png fixture")
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        db.executescript(contract_sql("3.5.2"))
        insert_row(db, schema_metadata, singleton_id=1, runtime_profile="local", storage_version="3.5.2")
        insert_row(db, books, id="book", title="book")
        insert_row(db, chapters, id="chapter", book_id="book", ordinal=1, title="chapter")
        insert_row(db, pages, id="page", chapter_id="chapter", ordinal=1, logical_source_path="page.png")
        insert_row(db, prompts, id="keep", type=PROMPT_TYPES[0], name="保留", content="保留")
        insert_row(db, prompts, id="drop", type="proofreading", name="废弃", content="废弃")
        # 已废弃域的表只存在于冻结的 3.5.2 契约里，字段名取自该契约的 DDL。
        db.execute("INSERT INTO bubbles(id,page_id,ordinal,payload_json,updated_revision) VALUES ('bubble','page',1,'{}',1)")
        db.execute("INSERT INTO render_requests(id,page_id,requested_revision) VALUES ('render','page',1)")
        insert_row(db, operations, id="op", kind=OPERATION_KINDS[0], executor_role="worker", request_json="{}",
                   page_id="page", bubble_id="bubble")
        insert_row(db, assets, id="asset", relative_path="objects/asset.png", mime_type="image/png",
                   checksum="d5057fbbb9b08de9e05cbef10bcf6d8cee42f33995d9ea2e7245d6b75c3988d1", byte_size=11)
        insert_row(db, page_assets, page_id="page", role=PAGE_ASSET_ROLES[0], asset_id="asset",
                   producer_render_request_id="render")
        assert set(RETIRED_DOMAINS).issubset({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})
    with StorageManager(root, "local") as migration:
        assert migration.check()["status"] == "upgrade_required"
        assert migration.prepare(initialize_database)["status"] == "awaiting_health"
        assert read_identity(root)[0] == STORAGE_VERSION
        migration.before_start()
        assert migration.confirm_ready() == []
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db:
        validate_schema(db, contract_sql(STORAGE_VERSION))
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables.isdisjoint(RETIRED_DOMAINS)
        assert db.execute("SELECT id,page_id,bubble_id FROM operations").fetchall() == [("op", "page", "bubble")]
        assert db.execute("SELECT page_id,role,asset_id,producer_render_request_id FROM page_assets").fetchall() == [
            ("page", PAGE_ASSET_ROLES[0], "asset", "render")]
        assert [foreign_key[2] for foreign_key in db.execute('PRAGMA foreign_key_list("operations")')].count("bubbles") == 0
        assert [foreign_key[2] for foreign_key in db.execute('PRAGMA foreign_key_list("page_assets")')].count("render_requests") == 0
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='ix_page_assets_producer_render_request_id'").fetchall()
        assert [row[0] for row in db.execute("SELECT id FROM prompts ORDER BY id")] == ["keep"]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO prompts(id,type,name,content) VALUES ('bad','proofreading','x','x')")
    assert (root / "objects" / "saved.txt").read_text(encoding="utf-8") == "用户已保存的成果"


def test_noop_chain_preserves_files_and_does_not_backup(root):
    before = (root / "objects" / "saved.txt").read_bytes()
    steps = (Migration(STORAGE_VERSION, "3.6.0"), Migration("3.6.0", "3.6.1"))
    with StorageManager(root, "local", target="3.6.1", steps=steps, sql_for=lambda _: contract_sql(STORAGE_VERSION)) as migration:
        assert migration.prepare(initialize_database)["status"] == "updated"
        assert read_identity(root)[0] == "3.6.1"
        assert not list(migration._operations())
        assert business_ready(root)
    assert (root / "objects" / "saved.txt").read_bytes() == before


def test_noop_failure_keeps_version(root):
    def fail(_):
        raise StorageError("bad target")
    with StorageManager(root, "local", target="3.6.0", steps=(Migration(STORAGE_VERSION, "3.6.0", validate=fail),), sql_for=lambda _: contract_sql(STORAGE_VERSION)) as migration:
        with pytest.raises(StorageError, match="bad target"):
            migration.prepare(initialize_database)
    assert read_identity(root)[0] == STORAGE_VERSION


def test_real_upgrade_commit_cleanup_and_user_backup(root):
    user_backup = root.parent / "my-backup"
    user_backup.mkdir()
    (user_backup / "keep").write_text("user backup")
    with manager(root) as migration:
        assert migration.prepare(initialize_database)["status"] == "awaiting_health"
        op, state = list(migration._operations())[0]
        assert read_identity(op / "backup")[0] == STORAGE_VERSION
        assert read_identity(root)[0] == "3.6.0"
        assert not business_ready(root)
        with pytest.raises(StorageError, match="尚未开始"):
            migration.confirm_ready()
        assert (op / "backup").exists() and not business_ready(root)
        migration.before_start()
        assert not business_ready(root)
        assert migration.confirm_ready() == []
        assert business_ready(root)
        assert not (op / "backup").exists()
        assert not (root / OWNER_FILE).exists()
        assert (root / "objects" / "saved.txt").read_text(encoding="utf-8") == "已转换的成果"
        assert list(migration._operations())[0][1]["stage"] == "cleaned"
    assert (user_backup / "keep").exists()


class PowerLoss(BaseException):
    pass


@pytest.mark.parametrize("stage", ["copying", "converting", "validated", "old_moved", "installed", "starting", "committed"])
def test_recovery_at_persisted_boundaries(root, stage):
    def crash(current):
        if current == stage:
            raise PowerLoss()
    with pytest.raises(PowerLoss), manager(root, failpoint=crash) as migration:
        migration.prepare(initialize_database)
        migration.before_start()
        migration.confirm_ready()
    with manager(root) as recovered:
        if stage in {"installed", "committed"}:
            recovered.prepare(initialize_database)
            recovered.before_start()
            recovered.confirm_ready()
            assert read_identity(root)[0] == "3.6.0"
        else:
            with pytest.raises(StorageError, match="已恢复旧数据"):
                recovered.prepare(initialize_database)
            assert read_identity(root)[0] == STORAGE_VERSION
            assert (root / "objects" / "saved.txt").read_text(encoding="utf-8") == "用户已保存的成果"


def test_startup_failure_rolls_back(root):
    with manager(root) as migration:
        migration.prepare(initialize_database)
        migration.before_start()
        migration.startup_failed()
        assert migration.rolled_back
        assert read_identity(root)[0] == STORAGE_VERSION
        assert business_ready(root)


def test_cleanup_failure_persists_and_next_start_retries(root, monkeypatch):
    with manager(root) as migration:
        migration.prepare(initialize_database)
        migration.before_start()
        with monkeypatch.context() as patch:
            patch.setattr(migration, "_remove", lambda *_: (_ for _ in ()).throw(PermissionError("busy")))
            assert len(migration.confirm_ready()) == 1
        op, state = list(migration._operations())[0]
        assert state["stage"] == "committed" and state["cleanup_error"] == "busy"
        assert business_ready(root)
    with manager(root) as migration:
        migration.prepare(initialize_database)
        migration.before_start()
        assert migration.confirm_ready() == []
        assert not (op / "backup").exists()
        assert read_identity(root)[0] == "3.6.0"


def test_lock_outside_root_and_live_process_protection(root):
    with DataRootLock(root):
        with pytest.raises(DataRootAlreadyLocked):
            with DataRootLock(root):
                pass
    with registered_process(root, "worker"):
        with pytest.raises(StorageError, match="尚未退出"):
            wait_for_children(root, timeout=0)


@pytest.mark.parametrize("state", ["reused", "exited", "alive", "access_denied"])
def test_child_identity_uses_creation_time_without_reading_command_line(tmp_path, monkeypatch, state):
    root = tmp_path / "data"
    marker = control_root(root) / "processes" / "api-123.json"
    marker.parent.mkdir(parents=True)
    atomic_json(marker, {"data_root": str(root.resolve()), "pid": 123, "created_at": 1.0, "role": "api"})

    class Process:
        def __init__(self, pid):
            assert pid == 123
            if state == "exited":
                raise psutil.NoSuchProcess(pid)

        def create_time(self):
            if state == "access_denied":
                raise psutil.AccessDenied(123)
            return 2.0 if state == "reused" else 1.0

        def cmdline(self):
            raise AssertionError("Command-line permissions must not decide process identity")

        def is_running(self):
            return True

    monkeypatch.setattr("src.storage_migrator.control.psutil.Process", Process)
    if state in {"alive", "access_denied"}:
        message = "尚未退出" if state == "alive" else "进程信息访问受限"
        with pytest.raises(StorageError, match=message):
            wait_for_children(root, timeout=0)
        assert marker.exists()
    else:
        wait_for_children(root, timeout=0)
        assert not marker.exists()


def test_api_gate_allows_only_health(root):
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        db.execute("UPDATE schema_metadata SET storage_version=?", (STORAGE_VERSION,))
    from src.backend_v2.api.app import ApiSettings, create_api_app
    from src.backend_v2.runtime_identity import RuntimeIdentity
    from src.backend_v2.storage.database import create_sqlite_engine
    engine = create_sqlite_engine(root / "saber.sqlite3")
    app = create_api_app(ApiSettings(root, RuntimeIdentity("test", "token", True), engine))
    try:
        control_root(root).mkdir()
        atomic_json(control_root(root) / "upgrade-pending.json", {"id": "test"})
        with app.test_client() as client:
            health = client.get("/api/v2/health")
            assert health.status_code == 200 and health.json["storageVersion"] == STORAGE_VERSION
            assert client.get("/api/v2/jobs").status_code == 503
    finally:
        app.extensions["saber_v2_runtime"].close()
        engine.dispose()


def test_real_conversion_ends_unfinished_graph_preserves_completed_items(root):
    progress = {"jobStatus": "queued", "executionMode": "sequential", "pools": [],
                "totalItems": 2, "completedItems": 1, "failedItems": 0, "skippedItems": 0, "cancelledItems": 0}
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        db.execute("INSERT INTO jobs(id,kind,status,config_json,latest_progress_json) VALUES ('job','translation','queued','{}',?)", (json.dumps(progress),))
        db.execute("INSERT INTO job_items(id,job_id,ordinal,status,result_json) VALUES ('saved','job',1,'completed','{\"text\":\"saved translation\"}')")
        db.execute("INSERT INTO job_items(id,job_id,ordinal,status) VALUES ('pending','job',2,'pending')")
        db.execute("INSERT INTO job_steps(id,job_item_id,ordinal,kind,status,checkpoint_json) VALUES ('step','pending',1,'ocr','pending','{}')")
    with manager(root) as migration:
        migration.prepare(initialize_database)
        with closing(sqlite3.connect(root / "saber.sqlite3")) as db:
            status, encoded = db.execute("SELECT status,latest_progress_json FROM jobs WHERE id='job'").fetchone()
            assert status == "failed" and json.loads(encoded)["failedItems"] == 1
            assert db.execute("SELECT result_json FROM job_items WHERE id='saved'").fetchone()[0] == '{"text":"saved translation"}'
            assert db.execute("SELECT status,checkpoint_json FROM job_steps").fetchone() == ("failed", None)
            assert "STORAGE_UPGRADE_INTERRUPTED" in db.execute("SELECT error_json FROM job_steps").fetchone()[0]


@pytest.mark.parametrize("after_install", [False, True])
def test_crash_between_directory_rename_and_journal_write(root, monkeypatch, after_install):
    import src.storage_migrator.runner as runner
    original = runner.os.replace
    def rename(source, target):
        original(source, target)
        if (Path(source).name == "work" if after_install else Path(source) == root):
            raise PowerLoss()
    with pytest.raises(PowerLoss), manager(root) as migration, monkeypatch.context() as patch:
        patch.setattr(runner.os, "replace", rename)
        migration.prepare(initialize_database)
    with manager(root) as migration:
        with pytest.raises(StorageError, match="已恢复旧数据"):
            migration.prepare(initialize_database)
    assert read_identity(root)[0] == STORAGE_VERSION


def test_insufficient_space_and_failed_target_leave_original(root, monkeypatch):
    import src.storage_migrator.runner as runner
    from types import SimpleNamespace
    with manager(root) as migration, monkeypatch.context() as patch:
        patch.setattr(runner.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
        with pytest.raises(StorageError, match="空间不足"):
            migration.prepare(initialize_database)
    assert read_identity(root)[0] == STORAGE_VERSION
    with manager(root) as migration:
        migration.sql_for = lambda _: contract_sql(STORAGE_VERSION)
        with pytest.raises(StorageError, match="结构不匹配"):
            migration.prepare(initialize_database)
    assert read_identity(root)[0] == STORAGE_VERSION


def test_unknown_backup_marker_never_deleted(root):
    with manager(root) as migration:
        migration.prepare(initialize_database)
        op, _ = list(migration._operations())[0]
        (op / "backup" / OWNER_FILE).write_text('{}')
        migration.before_start()
        assert migration.confirm_ready()
        assert (op / "backup" / "saber.sqlite3").exists()
        assert read_identity(root)[0] == "3.6.0"


def test_business_process_imports_no_historical_steps():
    result = subprocess.run([sys.executable, "-c", "import src.backend_v2.storage.startup; import sys; assert 'src.storage_migrator.registry' not in sys.modules; assert 'src.storage_migrator.v3_5_0' not in sys.modules"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_launcher_setup_failure_restores_candidate(root, monkeypatch):
    from src.backend_v2.launcher import entrypoint
    with manager(root) as migration:
        migration.prepare(initialize_database)
        monkeypatch.setattr(entrypoint, "create_sqlite_engine", lambda _: (_ for _ in ()).throw(OSError("open failed")))
        supervisor = entrypoint.LauncherSupervisor(entrypoint.LauncherConfig(root, host="127.0.0.1", port=5000), storage_manager=migration)
        with pytest.raises(OSError, match="open failed"):
            supervisor.run()
        assert migration.rolled_back and read_identity(root)[0] == STORAGE_VERSION


def test_real_launcher_confirms_candidate_and_cleans_backup(root):
    import socket
    import threading
    from src.backend_v2.launcher.entrypoint import LauncherConfig, LauncherState, LauncherSupervisor
    # The synthetic 3.4.9 fixture carries the current schema, so it must use the
    # preparer that tolerates tables a later contract already retired.
    from src.storage_migrator.v3_5_3 import stop_unfinished
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        db.execute("UPDATE schema_metadata SET storage_version='3.4.9'")
        db.execute("ALTER TABLE app_settings ADD COLUMN old_fixture_column TEXT")
    def fixture_sql(version):
        return contract_sql(STORAGE_VERSION) + ("\nALTER TABLE app_settings ADD COLUMN old_fixture_column TEXT;" if version == "3.4.9" else "")
    def fixture_convert(work):
        with closing(sqlite3.connect(work / "saber.sqlite3")) as db, db:
            db.execute("ALTER TABLE app_settings DROP COLUMN old_fixture_column")
    ready = threading.Event()
    errors = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with StorageManager(root, "local", steps=(Migration("3.4.9", STORAGE_VERSION, fixture_convert),), sql_for=fixture_sql, preparers={"3.4.9": stop_unfinished}) as migration:
        supervisor = LauncherSupervisor(LauncherConfig(root, host="127.0.0.1", port=port, open_browser=False), storage_manager=migration,
            status_callback=lambda status: ready.set() if status.state == LauncherState.RUNNING else None)
        def run():
            try:
                supervisor.run()
            except BaseException as error:
                errors.append(error)
                ready.set()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            assert ready.wait(40), "candidate did not become healthy"
            assert not errors, errors
            assert business_ready(root)
            op, state = list(migration._operations())[0]
            assert state["stage"] == "cleaned" and not (op / "backup").exists()
            assert read_identity(root)[0] == STORAGE_VERSION
        finally:
            supervisor.request_stop()
            thread.join(40)
            assert not thread.is_alive()


def test_installed_candidate_corruption_rolls_back_on_next_prepare(root):
    with manager(root) as migration:
        migration.prepare(initialize_database)
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        db.execute("DROP INDEX ix_process_epochs_role_status")
    with manager(root) as migration:
        with pytest.raises(StorageError, match="结构不匹配"):
            migration.prepare(initialize_database)
        assert migration.rolled_back
        assert read_identity(root)[0] == STORAGE_VERSION
        schema_smoke_test(root / "saber.sqlite3")


def test_desktop_setup_error_before_backend_restores_installed_data(root):
    with manager(root) as migration:
        with pytest.raises(ValueError, match="desktop"), migration.startup_guard():
            migration.prepare(initialize_database)
            raise ValueError("desktop settings construction failed")
        assert read_identity(root)[0] == STORAGE_VERSION


@pytest.mark.parametrize("filename", [OWNER_FILE, "upgrade-pending.json"])
def test_post_commit_marker_cleanup_denied_does_not_block_business(root, monkeypatch, filename):
    original = Path.unlink
    def denied(path, *args, **kwargs):
        if path.name == filename:
            raise PermissionError("Windows handle held")
        return original(path, *args, **kwargs)
    with manager(root) as migration:
        migration.prepare(initialize_database)
        migration.before_start()
        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", denied)
            assert migration.confirm_ready()
            assert business_ready(root)
            migration.startup_failed()
            assert read_identity(root)[0] == "3.6.0"
        assert migration.confirm_ready() == []
        assert list(migration._operations())[0][1]["stage"] == "cleaned"


def test_lost_operation_journal_never_initializes_empty_root(root):
    def crash(stage):
        if stage == "old_moved":
            raise PowerLoss()
    with pytest.raises(PowerLoss), manager(root, failpoint=crash) as migration:
        migration.prepare(initialize_database)
    op = next((control_root(root) / "operations").iterdir())
    (op / "state.json").unlink()
    with manager(root) as migration:
        with pytest.raises(StorageError, match="记录缺失"):
            migration.prepare(initialize_database)
    assert not root.exists()
    assert read_identity(op / "backup")[0] == STORAGE_VERSION


def test_orphan_pending_gate_never_initializes_empty_root(tmp_path):
    root = tmp_path / "absent"
    with StorageManager(root, "local") as migration:
        atomic_json(migration.pending, {"id": "unregistered"})
        with pytest.raises(StorageError, match="缺少对应恢复记录"):
            migration.prepare(initialize_database)
    assert not root.exists()


def test_pending_candidate_profile_mismatch_is_readonly(root):
    with manager(root) as migration:
        migration.prepare(initialize_database)
    with StorageManager(root, "public") as migration:
        with pytest.raises(StorageError, match="模式"):
            migration.prepare(initialize_database)
    assert read_identity(root) == ("3.6.0", "local")


def test_release_registers_complete_fixed_contracts():
    from src.storage_migrator.registry import MIGRATIONS, SOURCE_PREPARERS, VERSION_VALIDATORS
    versions = {"3.5.0", STORAGE_VERSION} | {step.source for step in MIGRATIONS} | {step.target for step in MIGRATIONS}
    for version in versions:
        assert parse_version(version) <= parse_version(STORAGE_VERSION)
        migration_chain(version, STORAGE_VERSION, MIGRATIONS)
        assert contract_sql(version)
        assert version in SOURCE_PREPARERS
        assert version in VERSION_VALIDATORS


def test_current_desktop_json_is_validated_before_loading_ui(root):
    path = root / "launcher-settings.json"
    path.write_text('{"server": {}}')
    with pytest.raises(StorageError, match="桌面设置"):
        StorageManager(root, "local").check()
    assert path.read_text() == '{"server": {}}'


def test_missing_ready_vector_collection_is_rejected(root):
    from src.storage_migrator.v3_5_0 import validate
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        book = db.execute("SELECT id FROM books LIMIT 1").fetchone()[0]
        db.execute("INSERT INTO vector_generations(id,book_id,generation,status,dependency_fingerprint) VALUES ('vector',?,1,'ready',?)", (book, '0'*64))
        with pytest.raises(StorageError, match="Chroma"):
            validate(db, root=root, files=True)


def test_real_upgrade_converges_rendering_page_projection(root):
    # 3.5.3 already retired render_requests, so the 3.5.3 preparer is the one
    # that can run against the current fixture schema.
    from src.storage_migrator.v3_5_3 import stop_unfinished
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db, db:
        chapter = db.execute("SELECT id FROM chapters LIMIT 1").fetchone()[0]
        db.execute("INSERT INTO pages(id,chapter_id,ordinal,logical_source_path,render_status) VALUES ('rendering-page',?,1,'render-test.png','rendering')", (chapter,))
    stop_unfinished(root)
    with closing(sqlite3.connect(root / "saber.sqlite3")) as db:
        assert db.execute("SELECT render_status FROM pages WHERE id='rendering-page'").fetchone()[0] == 'render_failed'


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Windows file-sharing behavior')
def test_real_windows_open_backup_file_retries_cleanup(root):
    with manager(root) as migration:
        migration.prepare(initialize_database)
        migration.before_start()
        op, _ = list(migration._operations())[0]
        with (op / 'backup' / 'objects' / 'saved.txt').open('rb'):
            assert migration.confirm_ready()
            assert business_ready(root)
            assert list(migration._operations())[0][1]['stage'] == 'committed'
        assert migration.confirm_ready() == []
        assert not (op / 'backup').exists()


def test_mixed_chain_uses_one_copy_and_orders_version_markers(root):
    def second_step(work):
        assert read_identity(work)[0] == '3.6.0'
        convert(work)
    steps = (Migration(STORAGE_VERSION, '3.6.0'), Migration('3.6.0', '3.6.1', second_step))
    with StorageManager(root, 'local', target='3.6.1', steps=steps,
                        sql_for=lambda v: sql_for(STORAGE_VERSION if v == '3.6.0' else v)) as migration:
        migration.prepare(initialize_database)
        operations = list(migration._operations())
        assert len(operations) == 1
        assert read_identity(operations[0][0] / 'backup')[0] == STORAGE_VERSION
        assert read_identity(root)[0] == '3.6.1'


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows extended filesystem paths')
@pytest.mark.parametrize('rollback', [False, True])
def test_long_work_and_backup_paths_preserve_identity_and_can_be_removed(root, rollback):
    # The source is below MAX_PATH, but the migration's extra directories exceed it.
    name = '字' * (235 - len(str(root / 'objects'))) + '.bin'
    saved = root / 'objects' / name
    saved.write_bytes(b'old long-path content')
    with manager(root) as migration:
        migration.prepare(initialize_database)
        op, state = next(migration._operations())
        assert state['root'] == str(root.resolve())
        assert len(str(op / 'backup' / 'objects' / name)) > 260
        assert (op / 'backup' / 'objects' / name).read_bytes() == saved.read_bytes()
        migration.before_start()
        if rollback:
            migration.startup_failed()
            assert read_identity(root)[0] == STORAGE_VERSION
            assert saved.read_bytes() == b'old long-path content'
            assert not (op / 'discarded').exists()
        else:
            assert migration.confirm_ready() == []
            assert read_identity(root)[0] == '3.6.0'
        assert not (op / 'backup').exists()
        assert not (op / 'work').exists()


@pytest.mark.parametrize('deep', [False, pytest.param(True, marks=pytest.mark.skipif(sys.platform != 'win32', reason='Windows long SQLite path'))])
def test_readonly_database_uri_handles_literal_uri_characters(tmp_path, deep):
    from src.storage_migrator.contracts import read_database
    path = tmp_path / '数据库 #100%.sqlite3'
    if deep:
        path = tmp_path / ('nested-' * 18) / ('nested-' * 18) / path.name
    filesystem_path(path.parent).mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(filesystem_path(path))) as db:
        db.execute('CREATE TABLE saved(value TEXT)')
    with closing(read_database(path)) as db:
        assert db.execute('SELECT count(*) FROM saved').fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            db.execute("INSERT INTO saved VALUES ('must not write')")


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows path spelling')
def test_extended_path_preserves_unc_and_is_idempotent():
    path = Path(r'\\server\share\folder')
    extended = filesystem_path(path)
    assert str(extended) == r'\\?\UNC\server\share\folder'
    assert filesystem_path(extended) == extended


@pytest.mark.parametrize('valid_identity', [True, False])
def test_recovery_of_marker_interrupted_before_atomic_replace(root, valid_identity):
    def crash(stage):
        if stage == 'copying':
            raise PowerLoss()
    with pytest.raises(PowerLoss), manager(root, failpoint=crash) as migration:
        migration.prepare(initialize_database)
    op, state = next(migration._operations())
    marker = op / 'work' / OWNER_FILE
    temporary = marker.with_suffix(marker.suffix + '.tmp')
    marker.replace(temporary)
    if not valid_identity:
        temporary.write_text(json.dumps({'id': state['id'], 'root': 'another-directory'}))
    with manager(root) as recovered:
        with pytest.raises(StorageError, match='已恢复旧数据'):
            recovered.prepare(initialize_database)
        assert read_identity(root)[0] == STORAGE_VERSION
        assert (root / 'objects' / 'saved.txt').read_text(encoding='utf-8') == '用户已保存的成果'
        assert (op / 'work').exists() is not valid_identity
        if not valid_identity:
            assert temporary.exists()
