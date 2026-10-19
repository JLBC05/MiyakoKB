"""Minimal v2 Worker process lifecycle."""

from __future__ import annotations

from src.backend_v2.storage.startup import current_storage_process
from src.storage_migrator.control import business_ready
from src.version import STORAGE_VERSION

import json
import logging
import os
from pathlib import Path
import signal
import threading
from typing import Any

from src.backend_v2.logging_config import configure_backend_logging
from src.backend_v2.paths import data_root_fingerprint, ensure_data_root, resolve_data_root
from src.backend_v2.runtime_identity import (
    LAUNCHER_PARENT_LOST_EXIT_CODE,
    LauncherParentMonitor,
    RuntimeIdentity,
    WORKER_RECYCLE_EXIT_CODE,
    start_launcher_parent_monitor,
)
from src.backend_v2.runtime_profile import PROFILE_ENV, resolve_runtime_profile
from src.backend_v2.storage.database import (
    create_sqlite_engine,
    database_path_for,
    is_sqlite_busy_error,
)
from src.backend_v2.storage.epochs import ProcessEpochRepository
from src.shared.user_logging import user_log


LOGGER = logging.getLogger("saber.worker")


def _insight_layer_handler(step_kind: str, service: Any):
    prefix = "insight_build_layer_"
    suffix = step_kind.removeprefix(prefix)
    if (
        step_kind.startswith(prefix)
        and suffix.isascii()
        and suffix.isdigit()
        and str(int(suffix)) == suffix
    ):
        return service.handle
    return None


def _write_ready_marker(data_root: Path, identity: RuntimeIdentity) -> None:
    runtime_dir = data_root / "runtime"
    marker = runtime_dir / "worker-ready.json"
    temporary = runtime_dir / f".worker-ready-{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(
            {
                "storageVersion": STORAGE_VERSION,
                "pid": os.getpid(),
                "epochId": identity.epoch_id,
                "dataRootFingerprint": data_root_fingerprint(data_root),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary.replace(marker)


@current_storage_process("worker")
def run_worker(args: object) -> int:
    profile = resolve_runtime_profile(getattr(args, "profile", "local"))
    if profile.name == "public" and not getattr(args, "data_dir", None):
        raise ValueError("--data-dir is required for the public profile")
    os.environ[PROFILE_ENV] = profile.name
    data_root = ensure_data_root(resolve_data_root(args.data_dir))
    if not args.probe:
        log_path = configure_backend_logging(
            role="worker",
            data_root=data_root,
            console_level=args.log_level,
        )
        LOGGER.debug(
            "Worker 进程启动：pid=%s，data_root=%s，日志=%s",
            os.getpid(),
            data_root,
            log_path,
        )
    identity = RuntimeIdentity.for_worker(test_mode=args.test_mode)
    database_path = database_path_for(data_root)
    # Test mode also backs schema-free probes (see current_storage_process), so
    # the engine is only opened when the data root already carries a database.
    engine = None
    if not identity.test_mode or database_path.exists():
        engine = create_sqlite_engine(database_path)
    if not identity.test_mode:
        repository = ProcessEpochRepository(engine)
        if not repository.validate(
            role="worker",
            epoch_id=identity.epoch_id,
            token=identity.epoch_token,
        ):
            engine.dispose()
            raise RuntimeError("Launcher-issued Worker epoch is missing or invalid")
    elif engine is not None:
        from sqlalchemy import inspect as sqlalchemy_inspect

        from src.backend_v2.storage.database import immediate_transaction
        from src.backend_v2.storage.schema import process_epochs
        from src.backend_v2.timestamps import utcnow

        if sqlalchemy_inspect(engine).has_table(process_epochs.name):
            with immediate_transaction(engine) as conn:
                conn.execute(
                    process_epochs.delete().where(process_epochs.c.id == identity.epoch_id)
                )
                conn.execute(
                    process_epochs.insert().values(
                        id=identity.epoch_id,
                        role="worker",
                        token_hash="",
                        pid=os.getpid(),
                        status="active",
                        heartbeat_at=utcnow(),
                        lease_expires_at=utcnow(),
                    )
                )
        else:
            engine.dispose()
            engine = None
            LOGGER.debug("Worker 测试模式运行在无 schema 的数据根，跳过测试 epoch 登记")
    else:
        LOGGER.debug("Worker 测试模式运行在无数据库的数据根，跳过测试 epoch 登记")

    if args.probe:
        print(
            json.dumps(
                {
                    "role": "worker",
                    "status": "ready",
                    "epochId": identity.epoch_id,
                    "dataRootFingerprint": data_root_fingerprint(data_root),
                },
                sort_keys=True,
            )
        )
        if engine is not None:
            engine.dispose()
        return 0

    stop_event = threading.Event()
    parent_monitor: LauncherParentMonitor | None = None

    def request_stop(_signum: int, _frame: object) -> None:
        LOGGER.debug("Worker 收到终止信号")
        stop_event.set()

    def stop_orphaned_worker() -> None:
        LOGGER.critical("Launcher 进程已退出，Worker 立即终止")
        os._exit(LAUNCHER_PARENT_LOST_EXIT_CODE)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        parent_monitor = start_launcher_parent_monitor(
            stop_orphaned_worker,
            test_mode=identity.test_mode,
        )
    except BaseException:
        if engine is not None:
            engine.dispose()
        raise
    try:
        if engine is None:
            _write_ready_marker(data_root, identity)
            while not stop_event.wait(timeout=0.5):
                pass
        else:
            from src.backend_v2.jobs.repository import JobQueueRepository
            from src.backend_v2.jobs.worker_loop import JobWorkerLoop
            from src.backend_v2.insight.worker import (
                InsightAnalysisWorkerService,
            )
            from src.backend_v2.insight.derived import (
                InsightDerivedWorkerService,
            )
            from src.backend_v2.insight.continuation import (
                ContinuationWorkerService,
            )
            from src.backend_v2.insight.qa import InsightQAWorkerService
            from src.backend_v2.insight.exports import (
                InsightExportWorkerService,
            )
            from src.backend_v2.content.image_import import (
                ImportSafetyLimits,
                PUBLIC_IMPORT_SAFETY_LIMITS,
            )
            from src.backend_v2.transfer.worker import TransferWorkerService
            from src.backend_v2.web_import.worker import (
                WebImportWorkerService,
            )
            from src.backend_v2.worker.maintenance import WorkerMaintenance
            from src.backend_v2.storage.platform_repositories import (
                ProviderRateLimiter,
            )
            from src.backend_v2.scheduling_policy import (
                SchedulingPolicyCache,
                SchedulingPolicyRepository,
                available_memory_mib,
            )
            from src.shared.openai_rate_limits import (
                configure_provider_rate_limit_store,
            )

            configure_provider_rate_limit_store(ProviderRateLimiter(engine))
            scheduling_policy = (
                SchedulingPolicyCache(SchedulingPolicyRepository(engine))
                if profile.name == "public"
                else None
            )
            job_repository = JobQueueRepository(engine)
            transfer = TransferWorkerService(
                data_root=data_root,
                engine=engine,
                jobs_repository=job_repository,
                limits=(
                    PUBLIC_IMPORT_SAFETY_LIMITS
                    if profile.name == "public"
                    else ImportSafetyLimits()
                ),
            )
            job_handlers = {
                step_kind: transfer.handler
                for step_kind in (
                    "container_scan",
                    "container_import_page",
                    "export_package",
                )
            }
            web_import = WebImportWorkerService(
                data_root=data_root,
                engine=engine,
                jobs=job_repository,
            )
            job_handlers.update(
                {
                    step_kind: web_import.handle
                    for step_kind in (
                        "web_extract_scan",
                        "web_extract_page",
                        "web_extract_finalize",
                        "web_extract_auto_commit",
                        "web_import_commit_page",
                        "web_import_commit_finalize",
                    )
                }
            )
            insight = InsightAnalysisWorkerService(
                data_root=data_root,
                engine=engine,
                jobs=job_repository,
            )
            job_handlers.update(
                {
                    "insight_analyze_batch": insight.handle,
                    "insight_validate_run": insight.handle,
                    "insight_publish_run": insight.handle,
                }
            )
            insight_derived = InsightDerivedWorkerService(
                data_root=data_root,
                engine=engine,
                jobs=job_repository,
            )
            job_handlers.update(
                {
                    "insight_build_overview": insight_derived.handle,
                    "insight_build_compressed_context": insight_derived.handle,
                    "insight_build_timeline": insight_derived.handle,
                    "insight_build_vectors": insight_derived.handle,
                    "insight_build_entity_alignment": insight_derived.handle,
                    "insight_stage_compressed_context": insight_derived.handle,
                    "insight_stage_overview_no_spoiler": insight_derived.handle,
                    "insight_stage_overview_story_summary": insight_derived.handle,
                    "insight_stage_timeline": insight_derived.handle,
                    "insight_stage_entity_alignment": insight_derived.handle,
                    "insight_stage_vectors": insight_derived.handle,
                }
            )
            continuation = ContinuationWorkerService(
                data_root=data_root,
                engine=engine,
                jobs=job_repository,
            )
            job_handlers.update(
                {
                    "continuation_generate_script": continuation.handle,
                    "continuation_generate_page": continuation.handle,
                    "continuation_generate_image": continuation.handle,
                    "continuation_generate_character_sheet": (
                        continuation.handle
                    ),
                    "continuation_export": continuation.handle,
                }
            )
            insight_export = InsightExportWorkerService(
                data_root=data_root,
                engine=engine,
                jobs=job_repository,
            )
            job_handlers["insight_export_report"] = insight_export.handle
            qa_runner = InsightQAWorkerService(
                data_root=data_root,
                engine=engine,
                worker_epoch_id=identity.epoch_id,
            )
            maintenance = WorkerMaintenance(
                data_root=data_root,
                engine=engine,
            )

            def memory_admitted() -> bool:
                if scheduling_policy is None:
                    return True
                threshold = int(
                    scheduling_policy.load()["minAvailableMemoryMiB"]
                )
                return threshold == 0 or available_memory_mib() >= threshold

            def run_immediate_work() -> bool:
                try:
                    maintenance.run_browser_cleanup_if_due()
                    if not memory_admitted():
                        return False
                    if qa_runner.run_one():
                        return True
                    return False
                except Exception as exc:
                    if not is_sqlite_busy_error(exc):
                        raise
                    LOGGER.debug(
                        "Worker 即时任务遇到 SQLite 写锁竞争，将在下一轮重试"
                    )
                    return False

            def run_idle_work() -> bool:
                try:
                    cleaned = maintenance.run_browser_cleanup_if_due()
                    return maintenance.run_if_due() or cleaned
                except Exception as exc:
                    if not is_sqlite_busy_error(exc):
                        raise
                    LOGGER.debug(
                        "Worker 空闲维护遇到 SQLite 写锁竞争，将在下一轮重试"
                    )
                    return False

            def abort_stalled_attempt(fence: Any, reason: str) -> None:
                LOGGER.critical(
                    "任务处理器控制超时，立即退出 Worker 交由 Launcher 恢复："
                    "job=%s attempt=%s reason=%s",
                    str(fence.job_id)[:8],
                    str(fence.attempt_id)[:8],
                    reason,
                )
                if not identity.test_mode:
                    os._exit(WORKER_RECYCLE_EXIT_CODE)

            job_loop = JobWorkerLoop(
                job_repository,
                worker_epoch_id=identity.epoch_id,
                handlers=job_handlers,
                batch_handlers={
                    "web_extract_page": web_import.handle_download_batch,
                },
                handler_resolver=lambda step_kind: _insight_layer_handler(
                    step_kind,
                    insight_derived,
                ),
                safe_point=run_immediate_work,
                idle_work=run_idle_work,
                scheduling_policy=(
                    scheduling_policy.load
                    if scheduling_policy is not None
                    else None
                ),
                admission_check=(
                    memory_admitted if scheduling_policy is not None else None
                ),
                on_control_timeout=abort_stalled_attempt,
                control_timeout_seconds=30.0 if identity.test_mode else 1.5,
            )
            _write_ready_marker(data_root, identity)
            LOGGER.debug(
                "Worker 服务初始化完成并写入 ready marker："
                "epoch=%s，任务步骤处理器=%s，批处理器=3",
                identity.epoch_id[:8],
                len(job_handlers),
            )
            user_log("system", "任务执行器已就绪")
            while not business_ready(data_root):
                if stop_event.wait(0.1):
                    break
            if not stop_event.is_set():
                job_loop.run(stop_event)
    finally:
        LOGGER.debug("Worker 正在关闭")
        if parent_monitor is not None:
            parent_monitor.stop()
        if engine is not None:
            engine.dispose()
        LOGGER.debug("Worker 已关闭")

    return 0
