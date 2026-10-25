"""Connection-bound Insight QA with Worker-only vector retrieval."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from contextvars import copy_context
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import logging
import math
from pathlib import Path
import queue
import re
import secrets
import threading
import time
from typing import Any, Protocol
import uuid

from sqlalchemy import Engine, and_, delete, exists, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.backend_v2.auth.ownership import effective_owner_id, owner_scope
from src.backend_v2.serialization import canonical_json as _json
from src.backend_v2.insight.derived import (
    InsightDerivedRepository,
    InsightVectorStore,
)
from src.backend_v2.insight.repository import (
    InsightConflict,
)
from src.backend_v2.insight.provider_runtime import (
    frozen_chat_config,
    frozen_embedding_config,
    frozen_reranker_config,
)
from src.backend_v2.timestamps import utcnow
from src.backend_v2.redaction import (
    redact_sensitive_value,
    secret_values_from_json,
)
from src.backend_v2.settings.resolver import SettingsResolver
from src.backend_v2.storage.database import (
    SQLITE_BUSY_RETRY_DELAY_SECONDS,
    SQLITE_BUSY_RETRY_LIMIT,
    immediate_transaction,
    is_sqlite_busy_error,
)
from src.backend_v2.storage.platform_repositories import SettingsRepository
from src.backend_v2.storage.schema import (
    analysis_artifacts,
    analysis_layer_result_pages,
    analysis_layer_results,
    analysis_page_results,
    analysis_runs,
    books,
    chapters,
    credential_versions,
    page_assets,
    pages,
    process_epochs,
    transient_requests,
    vector_generations,
)
from src.shared.ai_providers import get_provider_manifest
from src.shared.user_logging import (
    json_details,
    log_model_input,
    log_model_request,
    log_model_response,
    log_result,
    log_retry,
    log_step_failed,
    log_step_finished,
    log_step_started,
    user_log,
    user_log_context,
)


LOGGER = logging.getLogger("saber.worker.insight_qa")
# 客户端断开后多久检查一次取消信号（秒）。只在模型输出静默期（思考中）才会被感知，
# 有分片到达时 on_chunk 会立刻中止。取值兼顾响应速度与轮询开销。
_CANCELLATION_POLL_INTERVAL = 0.2
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]{2,}")
_QUESTION_WORDS = frozenset(
    {
        "什么",
        "为什么",
        "怎么",
        "怎样",
        "如何",
        "是否",
        "谁",
        "哪里",
        "哪",
        "请问",
        "告诉我",
    }
)


def _json_object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise QAConflict(f"stored {field} is missing")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise QAConflict(
            f"stored {field} is invalid"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise QAConflict(f"stored {field} must be an object")
    return dict(parsed)


def _optional_json_object(value: object, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _json_object(value, field)


def _required_string(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise QAConflict(f"{field} must be a non-empty string")
    return value


def _required_integer(
    value: object,
    field: str,
    *,
    minimum: int = 0,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise QAConflict(f"{field} must be an integer >= {minimum}")
    return value


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class QAConflict(InsightConflict):
    pass


class QAFenced(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TransientFence:
    request_id: str
    attempt_id: str
    worker_epoch_id: str
    owner_user_id: str


@dataclass(frozen=True, slots=True)
class QAHandle:
    request_id: str
    connection_token: str
    question: str
    options: dict[str, Any]
    config: dict[str, Any]
    book_id: str = ""


# 通用问答模式：细粒度页面检索与全库概览/压缩上下文合成同一个入口。
# exact/global 作为历史取值继续接受，便于旧客户端与既有测试逐步迁移。
DEFAULT_QA_MODE = "generic"
QA_MODES = frozenset({"generic", "exact", "global"})
# 通用模式为全库材料预留的候选条数（在页面候选配额之外）。
GENERIC_GLOBAL_CANDIDATE_LIMIT = 2


def _validated_request_payload(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    expected = {
        "bookId",
        "sourceRunId",
        "question",
        "mode",
        "keywords",
        "queryVariants",
        "candidateLimit",
        "useParentChild",
        "vectorGeneration",
        "dependencyFingerprint",
        "config",
    }
    if set(value) != expected:
        raise QAConflict("stored QA request fields are invalid")
    for key in ("bookId", "question", "dependencyFingerprint"):
        if not isinstance(value[key], str) or not value[key]:
            raise QAConflict(f"stored QA request {key} is invalid")
    source_run_id = value["sourceRunId"]
    if source_run_id is not None and (
        not isinstance(source_run_id, str) or not source_run_id
    ):
        raise QAConflict("stored QA request sourceRunId is invalid")
    if value["mode"] not in QA_MODES:
        raise QAConflict("stored QA request mode is invalid")
    for key in ("keywords", "queryVariants"):
        items = value[key]
        if (
            not isinstance(items, list)
            or (key == "queryVariants" and not items)
            or any(not isinstance(item, str) or not item for item in items)
        ):
            raise QAConflict(f"stored QA request {key} is invalid")
    for key, minimum in (("candidateLimit", 1), ("vectorGeneration", 0)):
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
            raise QAConflict(f"stored QA request {key} is invalid")
    if not isinstance(value["useParentChild"], bool):
        raise QAConflict("stored QA request useParentChild is invalid")
    if not isinstance(value["config"], Mapping):
        raise QAConflict("stored QA request config is invalid")
    return {
        **dict(value),
        "keywords": list(value["keywords"]),
        "queryVariants": list(value["queryVariants"]),
        "config": dict(value["config"]),
    }


def _optional_boolean(
    value: Mapping[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    if key not in value:
        return default
    result = value[key]
    if not isinstance(result, bool):
        raise ValueError(f"{key} must be a boolean")
    return result


def _finite_number(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise QAConflict(f"{field} must be a finite number")
    return float(value)


def _candidate(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise QAConflict("QA candidate must be an object")
    candidate = dict(value)
    required = {"id", "type", "pageId", "pageNumber", "document", "hybridScore"}
    allowed = required | {"parentContext", "rerankScore", "vectorScore"}
    if not required.issubset(candidate) or not set(candidate).issubset(allowed):
        raise QAConflict("QA candidate fields are invalid")
    if not isinstance(candidate["id"], str) or not candidate["id"]:
        raise QAConflict("QA candidate id is invalid")
    if candidate["type"] not in {"page", "event", "global"}:
        raise QAConflict("QA candidate type is invalid")
    page_id = candidate["pageId"]
    if page_id is not None and (not isinstance(page_id, str) or not page_id):
        raise QAConflict("QA candidate pageId is invalid")
    page_number = candidate["pageNumber"]
    if page_number is not None and (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
    ):
        raise QAConflict("QA candidate pageNumber is invalid")
    if not isinstance(candidate["document"], str):
        raise QAConflict("QA candidate document is invalid")
    candidate["hybridScore"] = _finite_number(
        candidate["hybridScore"],
        "QA candidate hybridScore",
    )
    for field in ("vectorScore", "rerankScore"):
        if field in candidate:
            candidate[field] = _finite_number(
                candidate[field],
                f"QA candidate {field}",
            )
    parent = candidate.get("parentContext")
    if parent is not None:
        if not isinstance(parent, list):
            raise QAConflict("QA candidate parentContext is invalid")
        normalized_parent: list[dict[str, Any]] = []
        for item in parent:
            if not isinstance(item, Mapping) or set(item) != {
                "layerIndex",
                "content",
            }:
                raise QAConflict("QA parent context fields are invalid")
            layer_index = _required_integer(
                item["layerIndex"],
                "QA parent context layerIndex",
                minimum=1,
            )
            content = item["content"]
            if not isinstance(content, Mapping):
                raise QAConflict("QA parent context content is invalid")
            normalized_parent.append(
                {"layerIndex": layer_index, "content": dict(content)}
            )
        candidate["parentContext"] = normalized_parent
    return candidate


def validate_retrieval_candidates(
    value: Mapping[str, Any],
) -> list[dict[str, Any]]:
    allowed = {"mode", "candidates", "retrieval"}
    if not {"mode", "candidates"}.issubset(value) or not set(value).issubset(allowed):
        raise QAConflict("QA retrieval result fields are invalid")
    if value["mode"] not in QA_MODES:
        raise QAConflict("QA retrieval result mode is invalid")
    raw_candidates = value["candidates"]
    if not isinstance(raw_candidates, list):
        raise QAConflict("QA retrieval candidates must be an array")
    diagnostics = value.get("retrieval")
    if diagnostics is not None:
        if not isinstance(diagnostics, Mapping):
            raise QAConflict("QA retrieval diagnostics must be an object")
        if set(diagnostics) != {"degraded", "reason", "mode", "candidateCount"}:
            raise QAConflict("QA retrieval diagnostics fields are invalid")
    return [_candidate(item) for item in raw_candidates]


def retrieval_diagnostics(value: Mapping[str, Any]) -> dict[str, Any]:
    """Read the retrieval degradation record carried alongside QA candidates."""
    raw = value.get("retrieval")
    if not isinstance(raw, Mapping):
        return {"degraded": False, "reason": None, "mode": None, "candidateCount": 0}
    reason = raw.get("reason")
    mode = raw.get("mode")
    count = raw.get("candidateCount")
    return {
        "degraded": bool(raw.get("degraded")),
        "reason": reason if isinstance(reason, str) and reason else None,
        "mode": mode if isinstance(mode, str) and mode else None,
        "candidateCount": count if isinstance(count, int) and not isinstance(count, bool) else 0,
    }


def _clear_chroma_cache() -> None:
    """Drop the process-wide Chroma client/segment cache.

    Without this a worker that outlives an index rebuild keeps querying a HNSW
    reader whose segment files are gone, and every QA request silently degrades
    to keyword retrieval until the process restarts.
    """
    try:
        from chromadb.api.shared_system_client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
    except Exception as exc:  # pragma: no cover - depends on the chromadb version
        LOGGER.debug("Failed to clear the Chroma client cache: %s", exc)


class TransientRequestRepository:
    """Small CAS state machine for non-durable connection work."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create_vector_query(
        self,
        *,
        book_id: str,
        request_payload: Mapping[str, Any],
    ) -> tuple[str, str]:
        _required_string(book_id, "QA bookId")
        payload = _validated_request_payload(request_payload)
        request_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        now = utcnow()
        try:
            with immediate_transaction(self.engine) as connection:
                if connection.execute(
                    select(books.c.id).where(
                        books.c.id == book_id,
                        books.c.owner_user_id == effective_owner_id(),
                    )
                ).scalar_one_or_none() is None:
                    raise QAConflict("book not found")
                connection.execute(
                    insert(transient_requests).values(
                        id=request_id,
                        owner_user_id=effective_owner_id(),
                        kind="vector_query",
                        book_id=book_id,
                        status="pending",
                        connection_token_hash=_token_hash(token),
                        connection_open=True,
                        request_json=_json(payload),
                        created_at=now,
                        updated_at=now,
                    )
                )
        except IntegrityError as exc:
            raise QAConflict(
                "this book already has an active QA request"
            ) from exc
        return request_id, token

    def claim_next(self, *, worker_epoch_id: str) -> TransientFence | None:
        now = utcnow()
        attempt_id = str(uuid.uuid4())
        with immediate_transaction(self.engine) as connection:
            candidate = connection.execute(
                select(
                    transient_requests.c.id,
                    transient_requests.c.owner_user_id,
                )
                .where(
                    transient_requests.c.kind == "vector_query",
                    transient_requests.c.status == "pending",
                    transient_requests.c.connection_open.is_(True),
                )
                .order_by(transient_requests.c.created_at)
                .limit(1)
            ).mappings().one_or_none()
            if candidate is None:
                return None
            request_id = str(candidate["id"])
            changed = connection.execute(
                update(transient_requests)
                .where(
                    transient_requests.c.id == request_id,
                    transient_requests.c.status == "pending",
                    transient_requests.c.connection_open.is_(True),
                )
                .values(
                    status="running",
                    worker_epoch_id=worker_epoch_id,
                    attempt_id=attempt_id,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                return None
        return TransientFence(
            request_id=_required_string(request_id, "QA request id"),
            attempt_id=attempt_id,
            worker_epoch_id=worker_epoch_id,
            owner_user_id=str(candidate["owner_user_id"]),
        )

    def request(self, fence: TransientFence) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    transient_requests.c.request_json,
                    transient_requests.c.connection_open,
                    transient_requests.c.status,
                    transient_requests.c.attempt_id,
                    transient_requests.c.worker_epoch_id,
                ).where(transient_requests.c.id == fence.request_id)
            ).mappings().one_or_none()
        if (
            row is None
            or row["connection_open"] is not True
            or row["status"] != "running"
            or row["attempt_id"] != fence.attempt_id
            or row["worker_epoch_id"] != fence.worker_epoch_id
            or not self._worker_is_active(fence.worker_epoch_id)
        ):
            raise QAFenced("transient vector query is no longer current")
        return _json_object(row["request_json"], "QA request")

    def complete(
        self,
        fence: TransientFence,
        *,
        result: Mapping[str, Any],
    ) -> None:
        now = utcnow()
        with self.engine.begin() as connection:
            secret_values = self._secret_values(
                connection,
                fence.request_id,
            )
            safe_result = redact_sensitive_value(
                dict(result),
                secret_values=secret_values,
            )
            changed = connection.execute(
                update(transient_requests)
                .where(*self._fence_predicates(fence))
                .values(
                    status="completed",
                    result_json=_json(safe_result),
                    completed_at=now,
                    updated_at=now,
                )
            )
        if changed.rowcount != 1:
            raise QAFenced("transient vector query cannot publish")

    def fail(self, fence: TransientFence, *, message: str) -> None:
        now = utcnow()
        with self.engine.begin() as connection:
            secret_values = self._secret_values(
                connection,
                fence.request_id,
            )
            safe_message = redact_sensitive_value(
                message,
                secret_values=secret_values,
            )
            changed = connection.execute(
                update(transient_requests)
                .where(*self._fence_predicates(fence))
                .values(
                    status="failed",
                    result_json=_json(
                        {
                            "error": {
                                "code": "VECTOR_QUERY_FAILED",
                                "message": safe_message,
                            }
                        }
                    ),
                    completed_at=now,
                    updated_at=now,
                )
            )
        if changed.rowcount != 1:
            raise QAFenced("transient vector query failure was fenced")

    @staticmethod
    def _secret_values(
        connection,
        request_id: str,
    ) -> tuple[str, ...]:
        request_json = connection.execute(
            select(transient_requests.c.request_json).where(
                transient_requests.c.id == request_id
            )
        ).scalar_one_or_none()
        request = _validated_request_payload(
            _json_object(request_json, "QA request")
        )
        config = request["config"]
        version_ids: set[str] = set()
        for section in config.values():
            if not isinstance(section, Mapping):
                continue
            version_id = section.get("credentialVersionId")
            if version_id is None:
                continue
            version_ids.add(
                _required_string(version_id, "QA credential version id")
            )
        if not version_ids:
            return ()
        values: set[str] = set()
        for secret_json in connection.execute(
            select(credential_versions.c.secret_json).where(
                credential_versions.c.id.in_(version_ids)
            )
        ).scalars():
            values.update(
                secret_values_from_json(
                    _required_string(secret_json, "credential secret JSON")
                )
            )
        return tuple(sorted(values, key=len, reverse=True))

    def poll(
        self,
        *,
        request_id: str,
        connection_token: str,
    ) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    transient_requests.c.status,
                    transient_requests.c.connection_open,
                    transient_requests.c.result_json,
                ).where(
                    transient_requests.c.id == request_id,
                    transient_requests.c.connection_token_hash
                    == _token_hash(connection_token),
                )
            ).mappings().one_or_none()
        if row is None:
            raise QAFenced("transient request no longer exists")
        status = _required_string(row["status"], "QA request status")
        if status not in {"pending", "running", "completed", "failed", "cancelled"}:
            raise QAConflict("stored QA request status is invalid")
        connection_open = row["connection_open"]
        if not isinstance(connection_open, bool):
            raise QAConflict("stored QA connection state is invalid")
        return {
            "status": status,
            "connectionOpen": connection_open,
            "result": _optional_json_object(row["result_json"], "QA result"),
        }

    def touch_connection(
        self,
        *,
        request_id: str,
        connection_token: str,
    ) -> bool:
        for attempt in range(SQLITE_BUSY_RETRY_LIMIT + 1):
            try:
                with self.engine.begin() as connection:
                    changed = connection.execute(
                        update(transient_requests)
                        .where(
                            transient_requests.c.id == request_id,
                            transient_requests.c.connection_token_hash
                            == _token_hash(connection_token),
                            transient_requests.c.connection_open.is_(True),
                            transient_requests.c.status.in_(("pending", "running")),
                        )
                        .values(updated_at=utcnow())
                    )
                return changed.rowcount == 1
            except Exception as exc:
                if (
                    not is_sqlite_busy_error(exc)
                    or attempt >= SQLITE_BUSY_RETRY_LIMIT
                ):
                    raise
                LOGGER.debug(
                    "Insight QA 连接心跳遇到 SQLite 写锁竞争，将重试："
                    "request=%s retry=%s/%s",
                    request_id[:8],
                    attempt + 1,
                    SQLITE_BUSY_RETRY_LIMIT,
                )
                time.sleep(SQLITE_BUSY_RETRY_DELAY_SECONDS)

    def consume(
        self,
        *,
        request_id: str,
        connection_token: str,
    ) -> dict[str, Any]:
        now = utcnow()
        with immediate_transaction(self.engine) as connection:
            row = connection.execute(
                select(transient_requests.c.result_json).where(
                    transient_requests.c.id == request_id,
                    transient_requests.c.connection_token_hash
                    == _token_hash(connection_token),
                    transient_requests.c.status == "completed",
                    transient_requests.c.connection_open.is_(True),
                )
            ).scalar_one_or_none()
            if row is None:
                raise QAFenced("transient result is not consumable")
            connection.execute(
                update(transient_requests)
                .where(transient_requests.c.id == request_id)
                .values(consumed_at=now, updated_at=now)
            )
        return _json_object(row, "QA result")

    def close(
        self,
        *,
        request_id: str,
        connection_token: str,
    ) -> None:
        token_hash = _token_hash(connection_token)
        with immediate_transaction(self.engine) as connection:
            row = connection.execute(
                select(transient_requests.c.status).where(
                    transient_requests.c.id == request_id,
                    transient_requests.c.connection_token_hash == token_hash,
                )
            ).scalar_one_or_none()
            if row is None:
                return
            status = _required_string(row, "QA request status")
            if status in {"completed", "failed", "cancelled"}:
                connection.execute(
                    delete(transient_requests).where(
                        transient_requests.c.id == request_id,
                        transient_requests.c.connection_token_hash
                        == token_hash,
                    )
                )
                return
            connection.execute(
                update(transient_requests)
                .where(
                    transient_requests.c.id == request_id,
                    transient_requests.c.connection_token_hash == token_hash,
                    transient_requests.c.status.in_(("pending", "running")),
                )
                .values(
                    status="cancelled",
                    connection_open=False,
                    completed_at=utcnow(),
                    updated_at=utcnow(),
                )
            )

    def prune(self, *, older_than_seconds: int = 300) -> int:
        if (
            isinstance(older_than_seconds, bool)
            or not isinstance(older_than_seconds, int)
            or older_than_seconds < 1
        ):
            raise ValueError("older_than_seconds must be a positive integer")
        cutoff = utcnow() - timedelta(seconds=older_than_seconds)
        with self.engine.begin() as connection:
            result = connection.execute(
                delete(transient_requests).where(
                    transient_requests.c.updated_at < cutoff,
                )
            )
        return int(result.rowcount or 0)

    @staticmethod
    def _fence_predicates(
        fence: TransientFence,
    ) -> tuple[Any, ...]:
        return (
            transient_requests.c.id == fence.request_id,
            transient_requests.c.status == "running",
            transient_requests.c.connection_open.is_(True),
            transient_requests.c.worker_epoch_id == fence.worker_epoch_id,
            transient_requests.c.attempt_id == fence.attempt_id,
            exists().where(
                process_epochs.c.id == fence.worker_epoch_id,
                process_epochs.c.role == "worker",
                process_epochs.c.status == "active",
            ),
        )

    def _worker_is_active(self, worker_epoch_id: str) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    select(
                        exists().where(
                            process_epochs.c.id == worker_epoch_id,
                            process_epochs.c.role == "worker",
                            process_epochs.c.status == "active",
                        )
                    )
                ).scalar()
            )


class QARetrievalAlgorithms(Protocol):
    def embed_queries(
        self,
        queries: Sequence[str],
        *,
        config: Mapping[str, Any],
    ) -> Sequence[Sequence[float]]: ...


class DefaultQARetrievalAlgorithms:
    def embed_queries(
        self,
        queries: Sequence[str],
        *,
        config: Mapping[str, Any],
    ) -> Sequence[Sequence[float]]:
        from src.core.manga_insight.embedding_client import EmbeddingClient

        client = EmbeddingClient(frozen_embedding_config(config))

        async def execute() -> Sequence[Sequence[float]]:
            output: list[Sequence[float]] = []
            # Query variants stay independent so one malformed provider
            # response cannot invalidate every retrieval query at once.
            for query in queries:
                output.append(await client.embed(query))
            return output

        return asyncio.run(execute())


class InsightQAWorkerService:
    """Runs embedding and Chroma reads only at the Worker safe point."""

    def __init__(
        self,
        *,
        data_root: Path,
        engine: Engine,
        worker_epoch_id: str,
        repository: TransientRequestRepository | None = None,
        algorithms: QARetrievalAlgorithms | None = None,
        derived: InsightDerivedRepository | None = None,
    ) -> None:
        self.engine = engine
        self.worker_epoch_id = worker_epoch_id
        self.repository = repository or TransientRequestRepository(engine)
        self.algorithms = algorithms or DefaultQARetrievalAlgorithms()
        self.vector_store = InsightVectorStore(data_root)
        self.credentials = SettingsRepository(engine)
        self.derived = derived or InsightDerivedRepository(engine)

    def run_one(self) -> bool:
        fence = self.repository.claim_next(
            worker_epoch_id=self.worker_epoch_id
        )
        if fence is None:
            return False
        started_at = time.monotonic()
        with user_log_context(
            operation_id=fence.request_id,
            step_kind="insight_qa_retrieve",
        ):
            log_step_started()
            with owner_scope(fence.owner_user_id):
                try:
                    request_payload = self.repository.request(fence)
                    result = self._retrieve(request_payload)
                    self.repository.complete(fence, result=result)
                except QAFenced:
                    LOGGER.debug(
                        "Insight QA 检索已不再属于当前 Worker：request=%s",
                        fence.request_id[:8],
                    )
                    user_log(
                        "system",
                        "问答请求已取消或被新请求替代，本次检索停止",
                    )
                    return True
                except Exception as exc:
                    log_step_failed(
                        exc,
                        duration=time.monotonic() - started_at,
                    )
                    try:
                        self.repository.fail(fence, message=str(exc))
                    except QAFenced:
                        pass
                else:
                    log_result(
                        "问答资料检索结果",
                        json_details(result),
                    )
                    log_step_finished(
                        duration=time.monotonic() - started_at,
                    )
        return True

    def _fallback_page_candidates(
        self,
        *,
        book_id: str,
        keywords: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        pages_data: list[dict[str, Any]] = []
        try:
            snapshot = self.derived.snapshot(book_id=book_id)
            pages_data = list(snapshot.pages)
        except Exception as exc:
            LOGGER.warning("Failed to get snapshot for fallback candidates: %s", exc)

        if not pages_data:
            try:
                with self.engine.connect() as connection:
                    rows = list(
                        connection.execute(
                            select(
                                analysis_page_results.c.page_id_snapshot,
                                analysis_page_results.c.page_number_snapshot,
                                analysis_page_results.c.payload_json,
                            )
                            .join(pages, pages.c.id == analysis_page_results.c.page_id_snapshot)
                            .join(chapters, chapters.c.id == pages.c.chapter_id)
                            .where(
                                chapters.c.book_id == book_id,
                                analysis_page_results.c.status.in_(("staging", "published")),
                            )
                            .order_by(analysis_page_results.c.page_number_snapshot)
                        ).mappings()
                    )
                for row in rows:
                    p_id = row["page_id_snapshot"]
                    p_num = row["page_number_snapshot"]
                    raw_payload = row["payload_json"]
                    payload: dict[str, Any] = {}
                    if isinstance(raw_payload, str):
                        try:
                            payload = json.loads(raw_payload)
                        except Exception:
                            pass
                    elif isinstance(raw_payload, Mapping):
                        payload = dict(raw_payload)
                    pages_data.append({
                        "pageId": p_id,
                        "pageNumber": p_num,
                        "analysis": payload,
                    })
            except Exception as exc:
                LOGGER.warning("Direct DB query for fallback candidates failed: %s", exc)

        candidates: list[dict[str, Any]] = []
        for item in pages_data:
            page_id = item.get("pageId")
            page_number = item.get("pageNumber")
            analysis = item.get("analysis") or {}
            if not isinstance(page_id, str) or not page_id:
                continue
            if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
                continue
            if not isinstance(analysis, Mapping):
                analysis = {}

            document = _page_document(page_number, analysis)
            # 关键词兜底只保留真正命中的页面：没有命中就返回空，交由调用方降级。
            # 之前按页码升序截断会退化成「全书最前面几页」，产生看似有据的错误引用。
            lexical = _lexical_score(document, keywords or ())
            if lexical <= 0:
                continue

            candidates.append({
                "id": f"page_{page_id}",
                "type": "page",
                "pageId": page_id,
                "pageNumber": page_number,
                "document": document,
                "hybridScore": round(0.5 + 0.5 * lexical, 4),
            })

        candidates.sort(key=lambda c: (-c["hybridScore"], c["pageNumber"]))
        return candidates[:limit]

    def _retrieve(self, request_payload: Mapping[str, Any]) -> dict[str, Any]:
        request_payload = _validated_request_payload(request_payload)
        mode = request_payload["mode"]
        book_id = request_payload["bookId"]
        if mode == "global":
            artifacts = self._global_artifact_candidates(
                book_id=book_id,
                dependency_fingerprint=request_payload["dependencyFingerprint"],
            )
            if artifacts:
                global_candidates = artifacts
                global_reason: str | None = None
            else:
                # 没有全局概览资料时只能退到关键词检索；命中不了就不编造「全书开头几页」。
                global_candidates = self._fallback_page_candidates(
                    book_id=book_id,
                    keywords=request_payload["keywords"],
                    limit=20,
                )
                global_reason = (
                    "该书没有可用的全局概览资料，仅以关键词命中的页面作为依据。"
                    if global_candidates
                    else "该书没有可用的全局概览资料，关键词检索也未命中，本次回答缺少可用依据。"
                )
            return {
                "mode": "global",
                "candidates": global_candidates,
                "retrieval": {
                    "degraded": global_reason is not None,
                    "reason": global_reason,
                    "mode": "global",
                    "candidateCount": len(global_candidates),
                },
            }
        candidates, retrieval = self._page_candidates(request_payload)
        if mode == "exact":
            return {"mode": "exact", "candidates": candidates, "retrieval": retrieval}
        # 通用模式：页面/事件候选之后追加全库材料，使单次问答同时具备细粒度证据与全局脉络。
        merged = self._global_artifact_candidates(
            book_id=book_id,
            dependency_fingerprint=request_payload["dependencyFingerprint"],
        )[:GENERIC_GLOBAL_CANDIDATE_LIMIT] + candidates
        return {"mode": "generic", "candidates": merged, "retrieval": retrieval}

    def _page_candidates(
        self,
        request_payload: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        book_id = request_payload["bookId"]
        generation = request_payload["vectorGeneration"]
        candidates: list[dict[str, Any]] | None = None
        vector_issue: str | None = None
        if generation > 0:
            with self.engine.connect() as connection:
                active = connection.execute(
                    select(
                        vector_generations.c.generation,
                        vector_generations.c.status,
                        vector_generations.c.dependency_fingerprint,
                    ).where(
                        vector_generations.c.book_id == book_id,
                        vector_generations.c.is_active.is_(True),
                    )
                ).mappings().one_or_none()
            if (
                active is not None
                and active["generation"] == generation
                and active["status"] in {"ready", "degraded"}
                and active["dependency_fingerprint"] == request_payload["dependencyFingerprint"]
            ):
                try:
                    variants = request_payload["queryVariants"]
                    config = self._with_credentials(request_payload["config"])
                    embeddings = list(
                        self.algorithms.embed_queries(
                            variants,
                            config=config,
                        )
                    )
                    if len(embeddings) == len(variants):
                        dimensions: int | None = None
                        normalized_embeddings: list[list[float]] = []
                        valid_embeddings = True
                        for embedding in embeddings:
                            if not isinstance(embedding, Sequence) or isinstance(
                                embedding,
                                (str, bytes, bytearray),
                            ):
                                valid_embeddings = False
                                break
                            normalized = [
                                _finite_number(value, "query embedding value")
                                for value in embedding
                            ]
                            if not normalized:
                                valid_embeddings = False
                                break
                            if dimensions is None:
                                dimensions = len(normalized)
                            elif len(normalized) != dimensions:
                                valid_embeddings = False
                                break
                            normalized_embeddings.append(normalized)
                        if valid_embeddings:
                            candidates = self._query_chroma(
                                book_id=book_id,
                                generation=generation,
                                embeddings=normalized_embeddings,
                                limit=request_payload["candidateLimit"],
                                keywords=request_payload["keywords"],
                            )
                        else:
                            vector_issue = "查询向量无效"
                    else:
                        vector_issue = "查询向量数量与分析请求不一致"
                except Exception as exc:
                    LOGGER.warning(
                        "Vector retrieval failed for book %s generation %s: %s; falling back to page analysis",
                        book_id,
                        generation,
                        exc,
                    )
                    candidates = None
                    vector_issue = "语义索引读取失败"
            else:
                vector_issue = "语义索引代次不可用（未发布、已过期或与当前分析不一致）"
        else:
            vector_issue = "该书尚未建立语义索引"

        if candidates:
            search_mode = "vector"
        else:
            candidates = self._fallback_page_candidates(
                book_id=book_id,
                keywords=request_payload["keywords"],
                limit=request_payload["candidateLimit"],
            )
            search_mode = "lexical" if candidates else "none"

        if search_mode == "vector":
            retrieval: dict[str, Any] = {
                "degraded": False,
                "reason": None,
                "mode": "vector",
                "candidateCount": len(candidates),
            }
        else:
            completion = (
                "关键词检索也未命中，本次回答缺少页面级证据。"
                if search_mode == "none"
                else "仅以关键词命中的页面作为页面级证据。"
            )
            reason = f"{vector_issue or '语义索引不可用'}，{completion}"
            retrieval = {
                "degraded": True,
                "reason": reason,
                "mode": search_mode,
                "candidateCount": len(candidates),
            }
            user_log("warning", f"漫画问答检索降级：{reason}")

        if request_payload["useParentChild"] and candidates:
            self._attach_parent_context(
                candidates,
                run_id=request_payload["sourceRunId"],
            )
        self._monitor_character_extraction_rate(
            run_id=request_payload["sourceRunId"],
            candidates=candidates,
        )
        return candidates, retrieval

    def _monitor_character_extraction_rate(
        self,
        *,
        run_id: str | None,
        candidates: Sequence[Mapping[str, Any]],
    ) -> None:
        """Monitor chapter character extraction rate; flag warning if < 30%."""
        if not run_id:
            return
        try:
            with self.engine.connect() as connection:
                # Query all page results for this run along with their chapter_id
                stmt = (
                    select(
                        pages.c.chapter_id,
                        analysis_page_results.c.payload_json,
                    )
                    .join(
                        pages,
                        pages.c.id == analysis_page_results.c.page_id_snapshot,
                    )
                    .where(
                        analysis_page_results.c.run_id == run_id,
                        analysis_page_results.c.status.in_(("staging", "published")),
                    )
                )
                rows = list(connection.execute(stmt).fetchall())
            if not rows:
                return

            chapter_stats: dict[str, dict[str, int]] = {}
            for chapter_id, payload_raw in rows:
                if not chapter_id:
                    chapter_id = "default_chapter"
                stats = chapter_stats.setdefault(chapter_id, {"total": 0, "extracted": 0})
                stats["total"] += 1

                payload: dict[str, Any] = {}
                if isinstance(payload_raw, str):
                    try:
                        payload = json.loads(payload_raw)
                    except Exception:
                        pass
                elif isinstance(payload_raw, Mapping):
                    payload = dict(payload_raw)

                chars = payload.get("characters")
                if isinstance(chars, list) and len(chars) > 0:
                    stats["extracted"] += 1

            for chapter_id, stats in chapter_stats.items():
                if stats["total"] > 0:
                    rate = stats["extracted"] / stats["total"]
                    if rate < 0.30:
                        warning_msg = (
                            f"[QA检索层监控] 话 {chapter_id} 角色抽取率仅为 {rate:.1%} "
                            f"({stats['extracted']}/{stats['total']})，低于 30%，标记为：可能抽取失败"
                        )
                        LOGGER.warning(warning_msg)
                        user_log("warning", warning_msg)
        except Exception as exc:
            LOGGER.debug("character extraction rate monitor failed: %s", exc)

    def _query_chroma(
        self,
        *,
        book_id: str,
        generation: int,
        embeddings: Sequence[Sequence[float]],
        limit: int,
        keywords: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Query the vector store, self-healing a broken in-process segment handle.

        Chroma keeps segment readers in a process-wide cache. When the index is
        rebuilt underneath a running worker the cached HNSW reader points at
        deleted files and every query fails with「Nothing found on disk」even
        though the storage on disk is healthy. Dropping the cache and retrying
        once re-opens the segments, so one stale handle does not degrade QA into
        keyword-only retrieval for the rest of the process lifetime.
        """
        try:
            return self._query_chroma_once(
                book_id=book_id,
                generation=generation,
                embeddings=embeddings,
                limit=limit,
                keywords=keywords,
            )
        except QAConflict:
            raise
        except Exception as exc:
            LOGGER.warning(
                "Chroma query failed for book %s generation %s: %s; clearing client cache and retrying once",
                book_id,
                generation,
                exc,
            )
            _clear_chroma_cache()
            return self._query_chroma_once(
                book_id=book_id,
                generation=generation,
                embeddings=embeddings,
                limit=limit,
                keywords=keywords,
            )

    def _query_chroma_once(
        self,
        *,
        book_id: str,
        generation: int,
        embeddings: Sequence[Sequence[float]],
        limit: int,
        keywords: Sequence[str],
    ) -> list[dict[str, Any]]:
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError as exc:
            raise QAConflict("ChromaDB is not installed") from exc
        client = chromadb.PersistentClient(
            path=str(self.vector_store.path),
            settings=Settings(anonymized_telemetry=False),
        )
        names = InsightVectorStore.names(book_id, generation)
        merged: dict[str, dict[str, Any]] = {}
        for collection_name in names:
            collection = client.get_collection(collection_name)
            count = _required_integer(
                collection.count(),
                "Chroma collection count",
            )
            if count == 0:
                continue
            response = collection.query(
                query_embeddings=[list(value) for value in embeddings],
                n_results=min(limit, count),
                include=["documents", "metadatas", "distances"],
            )
            if not isinstance(response, Mapping):
                raise QAConflict("Chroma query response must be an object")
            rows = []
            for field in ("ids", "documents", "metadatas", "distances"):
                value = response.get(field)
                if not isinstance(value, list):
                    raise QAConflict(f"Chroma query {field} must be an array")
                rows.append(value)
            ids_rows, document_rows, metadata_rows, distance_rows = rows
            if not (
                len(ids_rows)
                == len(document_rows)
                == len(metadata_rows)
                == len(distance_rows)
                == len(embeddings)
            ):
                raise QAConflict("Chroma query result row counts do not match")
            for query_index, ids in enumerate(ids_rows):
                documents = document_rows[query_index]
                metadatas = metadata_rows[query_index]
                distances = distance_rows[query_index]
                if not all(
                    isinstance(value, list)
                    for value in (ids, documents, metadatas, distances)
                ):
                    raise QAConflict("Chroma query result rows must be arrays")
                for record_id, document, metadata, distance in zip(
                    ids,
                    documents,
                    metadatas,
                    distances,
                    strict=True,
                ):
                    if not isinstance(record_id, str) or not record_id:
                        raise QAConflict("Chroma record id is invalid")
                    if not isinstance(document, str):
                        raise QAConflict("Chroma record document is invalid")
                    if not isinstance(metadata, Mapping):
                        raise QAConflict("Chroma record metadata is invalid")
                    record_type = metadata.get("type")
                    if record_type not in {"page", "event"}:
                        raise QAConflict("Chroma record type is invalid")
                    raw_page_id = metadata.get("page_id")
                    raw_page_number = metadata.get("page_number")
                    if raw_page_id in {None, ""}:
                        page_id = None
                        page_number = None
                    else:
                        if not isinstance(raw_page_id, str):
                            raise QAConflict("Chroma record page_id is invalid")
                        if (
                            isinstance(raw_page_number, bool)
                            or not isinstance(raw_page_number, int)
                            or raw_page_number < 1
                        ):
                            raise QAConflict("Chroma record page_number is invalid")
                        page_id = raw_page_id
                        page_number = raw_page_number
                    vector_score = max(
                        0.0,
                        1.0 - _finite_number(distance, "Chroma distance"),
                    )
                    lexical_score = _lexical_score(document, keywords)
                    hybrid_score = vector_score * 0.8 + lexical_score * 0.2
                    existing = merged.get(record_id)
                    if (
                        existing is not None
                        and existing["hybridScore"] >= hybrid_score
                    ):
                        continue
                    merged[record_id] = {
                        "id": record_id,
                        "type": record_type,
                        "pageId": page_id,
                        "pageNumber": page_number,
                        "document": document,
                        "vectorScore": vector_score,
                        "hybridScore": hybrid_score,
                    }
        return sorted(
            merged.values(),
            key=lambda row: (-row["hybridScore"], row["id"]),
        )[:limit]

    def _attach_parent_context(
        self,
        candidates: list[dict[str, Any]],
        *,
        run_id: str | None,
    ) -> None:
        page_ids = {
            row["pageId"]
            for row in candidates
            if row.get("pageId")
        }
        if not run_id or not page_ids:
            return
        with self.engine.connect() as connection:
            rows = list(
                connection.execute(
                    select(
                        analysis_layer_result_pages.c.page_id_snapshot,
                        analysis_layer_results.c.layer_index,
                        analysis_layer_results.c.content_json,
                    )
                    .join(
                        analysis_layer_results,
                        analysis_layer_results.c.id
                        == analysis_layer_result_pages.c.layer_result_id,
                    )
                    .where(
                        analysis_layer_results.c.run_id == run_id,
                        analysis_layer_results.c.status == "published",
                        analysis_layer_results.c.layer_index > 0,
                        analysis_layer_result_pages.c.page_id_snapshot.in_(
                            tuple(page_ids)
                        ),
                    )
                    .order_by(analysis_layer_results.c.layer_index)
                )
            )
        parents: dict[str, list[dict[str, Any]]] = {}
        for page_id, layer_index, content_json in rows:
            page_id = _required_string(page_id, "analysis layer page id")
            parents.setdefault(page_id, []).append(
                {
                    "layerIndex": _required_integer(
                        layer_index,
                        "analysis layer index",
                        minimum=1,
                    ),
                    "content": _json_object(
                        content_json,
                        "analysis layer content",
                    ),
                }
            )
        for candidate in candidates:
            candidate["parentContext"] = parents.get(
                candidate.get("pageId"),
                [],
            )

    def _global_artifact_candidates(
        self,
        *,
        book_id: str,
        dependency_fingerprint: str,
    ) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = list(
                connection.execute(
                    select(
                        analysis_artifacts.c.kind,
                        analysis_artifacts.c.template,
                        analysis_artifacts.c.payload_json,
                        analysis_artifacts.c.dependency_fingerprint,
                    ).where(
                        analysis_artifacts.c.book_id == book_id,
                        analysis_artifacts.c.is_active.is_(True),
                        analysis_artifacts.c.status.in_(("ready", "degraded")),
                        or_(
                            and_(
                                analysis_artifacts.c.kind == "overview",
                                analysis_artifacts.c.template == "story_summary",
                            ),
                            and_(
                                analysis_artifacts.c.kind == "compressed_context",
                                analysis_artifacts.c.template == "default",
                            ),
                        ),
                    )
                    .order_by(
                        analysis_artifacts.c.kind,
                        analysis_artifacts.c.template,
                    )
                ).mappings()
            )
        required = {
            ("overview", "story_summary"),
            ("compressed_context", "default"),
        }
        output: list[dict[str, Any]] = []
        found: set[tuple[str, str]] = set()
        for row in rows:
            kind = _required_string(row["kind"], "analysis artifact kind")
            template = _required_string(
                row["template"],
                "analysis artifact template",
            )
            key = (kind, template)
            if key in found:
                raise QAConflict("global QA context contains duplicate artifacts")
            found.add(key)
            if _required_string(
                row["dependency_fingerprint"],
                "analysis artifact dependency fingerprint",
            ) != dependency_fingerprint:
                continue
            output.append(
                {
                    "id": f"{kind}:{template}",
                    "type": "global",
                    "pageId": None,
                    "pageNumber": None,
                    "document": _json(
                        _json_object(
                            row["payload_json"],
                            "analysis artifact payload",
                        )
                    ),
                    "hybridScore": 1.0,
                }
            )
        return output

    def _with_credentials(
        self,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            return self.credentials.resolve_credential_sections(
                config,
                ("embedding",),
            )
        except LookupError as exc:
            raise QAConflict("frozen embedding credential is missing") from exc


class QAApiAlgorithms(Protocol):
    def rerank(
        self,
        *,
        question: str,
        candidates: Sequence[Mapping[str, Any]],
        top_k: int,
        config: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]: ...

    def stream_answer(
        self,
        *,
        question: str,
        candidates: Sequence[Mapping[str, Any]],
        config: Mapping[str, Any],
        cancelled: threading.Event,
        resolved_entities: Mapping[str, Any] | None = None,
        locations: Mapping[int, Mapping[str, Any]] | None = None,
    ) -> Iterator[str]: ...


class DefaultQAApiAlgorithms:
    def rerank(
        self,
        *,
        question: str,
        candidates: Sequence[Mapping[str, Any]],
        top_k: int,
        config: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]:
        from src.shared.ai_transport import (
            AsyncOpenAICompatibleTransport,
            UnifiedRerankRequest,
        )

        if not candidates:
            return []
        reranker_config = frozen_reranker_config(config)
        documents: list[str] = []
        for index, candidate in enumerate(candidates):
            document = candidate.get("document")
            if not isinstance(document, str) or not document:
                raise QAConflict(
                    f"reranker candidate {index} document is invalid"
                )
            documents.append(document)
        request = UnifiedRerankRequest(
            provider=reranker_config.provider,
            api_key=reranker_config.api_key,
            model=reranker_config.model,
            credential_version_id=reranker_config.credential_version_id,
            rpm_limit=0,
            query=question,
            documents=documents,
            top_n=min(top_k, len(documents)),
            base_url=reranker_config.base_url or None,
            timeout=(
                None
                if reranker_config.timeout_seconds == 0
                else reranker_config.timeout_seconds
            ),
        )
        log_model_input(
            "重排模型",
            (
                f"查询：{question}",
                *(
                    f"候选 {index:02d}：{document}"
                    for index, document in enumerate(documents, start=1)
                ),
            ),
        )

        def validate_result(result: object) -> list[dict[str, Any]]:
            if not isinstance(result, Mapping) or "results" not in result:
                raise QAConflict("reranker response fields are invalid")
            items = result["results"]
            if not isinstance(items, list):
                raise QAConflict("reranker results must be an array")
            output: list[dict[str, Any]] = []
            seen: set[int] = set()
            for item in items:
                if not isinstance(item, Mapping):
                    raise QAConflict("reranker result item must be an object")
                index = item.get("index")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < len(candidates)
                ):
                    raise QAConflict("reranker result index is invalid")
                if index in seen:
                    raise QAConflict("reranker result indices must be unique")
                seen.add(index)
                score = item.get("relevance_score")
                if (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(score)
                ):
                    raise QAConflict("reranker relevance score is invalid")
                candidate = dict(candidates[index])
                candidate["rerankScore"] = float(score)
                output.append(candidate)
            if not output:
                raise QAConflict("reranker returned no usable results")
            if len(output) != request.top_n:
                raise QAConflict("reranker result count does not match top_n")
            return output

        async def execute() -> Sequence[Mapping[str, Any]]:
            transport = AsyncOpenAICompatibleTransport(
                max_retries=reranker_config.transport_retries
            )
            total_attempts = reranker_config.business_retries + 1
            for attempt in range(total_attempts):
                log_model_request(
                    provider=get_provider_manifest(
                        reranker_config.provider
                    ).display_name,
                    model=reranker_config.model,
                    stream=False,
                    attempt=attempt + 1,
                    total_attempts=total_attempts,
                )
                try:
                    raw_result = await transport.rerank(request)
                    log_model_response(
                        "重排模型",
                        json.dumps(raw_result, ensure_ascii=False),
                    )
                    return validate_result(raw_result)
                except QAConflict as exc:
                    if attempt >= reranker_config.business_retries:
                        raise
                    LOGGER.debug("Insight 重排结果校验失败：%s", exc)
                    log_retry(
                        "重排模型",
                        attempt + 2,
                        total_attempts,
                        exc,
                    )
                    await asyncio.sleep(1)
            raise RuntimeError("reranker request completed without a result")

        return asyncio.run(execute())

    def stream_answer(
        self,
        *,
        question: str,
        candidates: Sequence[Mapping[str, Any]],
        config: Mapping[str, Any],
        cancelled: threading.Event,
        resolved_entities: Mapping[str, Any] | None = None,
        locations: Mapping[int, Mapping[str, Any]] | None = None,
    ) -> Iterator[str]:
        from src.shared.ai_transport import UnifiedChatRequest
        from src.shared.openai_execution import (
            OpenAICompatibleAsyncExecutor,
            build_openai_compatible_runtime_options,
        )
        from src.shared.openai_options import OpenAICompatibleOptions

        chat_config = frozen_chat_config(config)
        context = _answer_context(
            candidates,
            resolved_entities=resolved_entities,
            locations=locations,
        )
        prompts = config.get("prompts")
        if not isinstance(prompts, Mapping):
            raise QAConflict("frozen Insight prompts must be an object")
        qa_prompt = prompts.get("qa_response")
        system_prompt = prompts.get("analysis_system")
        if not isinstance(qa_prompt, Mapping) or not isinstance(
            system_prompt,
            Mapping,
        ):
            raise QAConflict("frozen Insight QA prompts are invalid")
        configured = qa_prompt.get("content")
        system = system_prompt.get("content")
        if not isinstance(configured, str) or not configured.strip():
            raise QAConflict("frozen Insight qa_response prompt is invalid")
        if not isinstance(system, str) or not system.strip():
            raise QAConflict("frozen Insight analysis_system prompt is invalid")
        prompt = (
            f"{configured.strip()}\n\n"
            f"问题：{question}\n\n资料：\n{context}"
        )
        options = OpenAICompatibleOptions.from_dict(
            chat_config.openai_options.to_dict()
        )
        options.execution.use_stream = True
        options.request.force_json_output = False

        chunks: queue.Queue[object] = queue.Queue(maxsize=128)
        done = object()

        class ConnectionClosed(RuntimeError):
            pass

        def publish_chunk(value: object) -> bool:
            while not cancelled.is_set():
                try:
                    chunks.put(value, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        def publish_final(value: object) -> None:
            """终态标记（``done`` 或异常）必须入队，否则消费方会一直等下去。

            取消之后 ``publish_chunk`` 会立即返回 False，用它发 ``done`` 会让
            ``stream_answer`` 的消费循环永远拿不到终止信号（实测挂死）。这里改为
            跳过队列长度限制，并在必要时挤掉一个待发分片。
            """
            while True:
                try:
                    chunks.put(value, timeout=0.1)
                    return
                except queue.Full:
                    try:
                        chunks.get_nowait()
                    except queue.Empty:
                        pass

        def on_chunk(chunk: str, _full_text: str) -> None:
            if not publish_chunk(chunk):
                raise ConnectionClosed("QA connection closed")

        def before_request() -> None:
            if cancelled.is_set():
                raise ConnectionClosed("QA connection closed")

        request = UnifiedChatRequest(
            provider=chat_config.provider,
            api_key=chat_config.api_key,
            model=chat_config.model,
            credential_version_id=chat_config.credential_version_id,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            base_url=chat_config.base_url or None,
            openai_options=options,
            runtime_options=build_openai_compatible_runtime_options(
                on_stream_chunk=on_chunk,
            ),
        )

        async def complete() -> None:
            async def check_connection() -> None:
                before_request()

            # 审计 P2-5：``cancelled`` 是 threading.Event，过去只在「下一次模型分片
            # 到达」或请求开始前被看到。用户关页面后没人再读它，上游请求会一直跑到
            # 300s 上限，白烧一整次回答的 token（推理模型尤甚）。
            #
            # 这里用一个纯 asyncio 的轮询协程与上游请求赛跑：
            # * 不能用 ``asyncio.to_thread(cancelled.wait)``——流式响应里上游协程在
            #   分片之间是纯 Python 等待，永远不会让出控制权去跑 ``call_soon`` 回调，
            #   取消因此传不进去（实测会挂死）；
            # * 分片到达时 ``on_chunk`` 会立刻发现自己被取消，所以最坏只多等一个
            #   ``_CANCELLATION_POLL_INTERVAL`` 的模型静默期，也就是思考期。
            async def wait_cancelled() -> None:
                while not cancelled.is_set():
                    await asyncio.sleep(_CANCELLATION_POLL_INTERVAL)
                raise ConnectionClosed("QA connection closed")

            upstream = asyncio.ensure_future(
                OpenAICompatibleAsyncExecutor().execute(
                    request,
                    capability=request.capability,
                    before_request=check_connection,
                )
            )
            watcher = asyncio.ensure_future(wait_cancelled())
            try:
                # 不能直接 ``await gather(watcher, upstream)``：watcher 只有在被取消
                # 时才结束，正常回答时它永远 pending，gather 就永远不返回，于是
                # ``done`` 永远不会入队、消费循环无限等待（实测挂死 100s+）。
                # 改为「谁先结束就先处理」：上游先完成就收起 watcher 并返回结果，
                # 取消先到就取消上游并抛 ConnectionClosed。
                pending: set[asyncio.Future[object]] = {upstream, watcher}
                while pending:
                    finished, pending = await asyncio.wait(
                        pending,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for future in finished:
                        if future is upstream:
                            upstream.result()  # 把上游异常原样抛出
                            return
                        future.result()  # watcher 只在被取消时结束，不会走到这里
            except BaseException:
                for pending_future in (upstream, watcher):
                    if not pending_future.done():
                        pending_future.cancel()
                # 等被取消的协程真正收尾，再关掉 httpx 连接，避免响应流悬挂。
                for pending_future in (upstream, watcher):
                    try:
                        await pending_future
                    except BaseException:
                        pass
                raise
            finally:
                if not watcher.done():
                    watcher.cancel()

        def run() -> None:
            try:
                # The async transport applies a wall-clock limit to the whole
                # streaming attempt, including provider keep-alive frames.  QA
                # still runs it in this producer thread so Flask can forward
                # chunks and heartbeats without blocking the request iterator.
                asyncio.run(complete())
            except ConnectionClosed:
                pass
            except BaseException as exc:
                publish_final(exc)
            finally:
                publish_final(done)

        producer_context = copy_context()
        thread = threading.Thread(
            target=lambda: producer_context.run(run),
            name="insight-qa-stream",
            daemon=True,
        )
        thread.start()
        try:
            while True:
                try:
                    value = chunks.get(timeout=15)
                except queue.Empty:
                    yield ""
                    continue
                if value is done:
                    return
                if isinstance(value, BaseException):
                    raise value
                if not isinstance(value, str):
                    raise QAConflict("QA stream chunk must be a string")
                yield value
        finally:
            cancelled.set()
            thread.join(timeout=2)


class InsightQACommandService:
    def __init__(
        self,
        engine: Engine,
        *,
        repository: TransientRequestRepository | None = None,
    ) -> None:
        self.engine = engine
        self.repository = repository or TransientRequestRepository(engine)
        self.settings = SettingsResolver(engine)
        self.credentials = SettingsRepository(engine)
        self.derived = InsightDerivedRepository(engine)

    def create(
        self,
        *,
        book_id: str,
        command: Mapping[str, Any],
    ) -> QAHandle:
        allowed = {
            "mode",
            "question",
            "threshold",
            "topK",
            "useParentChild",
            "useReasoning",
            "useReranker",
        }
        unknown = set(command) - allowed
        if unknown:
            raise ValueError(
                f"unknown QA command fields: {', '.join(sorted(unknown))}"
            )
        raw_question = command.get("question")
        if not isinstance(raw_question, str):
            raise ValueError("question must be a string")
        question = raw_question.strip()
        if not question:
            raise ValueError("question must not be empty")
        mode = command.get("mode", DEFAULT_QA_MODE)
        if not isinstance(mode, str):
            raise ValueError("mode must be generic, exact or global")
        if mode not in QA_MODES:
            raise ValueError("mode must be generic, exact or global")
        top_k = command.get("topK", 5)
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise ValueError("topK must be an integer")
        if top_k < 1:
            raise ValueError("topK must be a positive integer")
        threshold = command.get("threshold", 0)
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
        ):
            raise ValueError("threshold must be a finite number")
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be between 0 and 1")
        options = {
            "mode": mode,
            "useParentChild": _optional_boolean(
                command,
                "useParentChild",
                default=False,
            ),
            "useReasoning": _optional_boolean(
                command,
                "useReasoning",
                default=False,
            ),
            "useReranker": _optional_boolean(
                command,
                "useReranker",
                default=True,
            ),
            "topK": top_k,
            "threshold": threshold,
        }
        processed = preprocess_question(question)
        config = self.settings.resolve_insight(
            book_id=book_id,
            scope="qa",
        )
        snapshot = self.derived.snapshot(book_id=book_id)
        vector_generation = 0
        if mode in {"exact", "generic"}:
            # 通用模式复用页面检索的可用性信息：向量代次可用则走向量检索，否则由
            # _page_candidates 内部回退到页面分析；只有旧的 exact 模式才硬性要求可用。
            status = self.derived.qa_status(book_id=book_id, mode="exact")
            available = status.get("available")
            if not isinstance(available, bool):
                raise QAConflict("exact QA status is invalid")
            if not available and mode == "exact":
                raise QAConflict(
                    f"exact QA is unavailable: {status.get('reason')}"
                )
            raw_generation = status.get("generation")
            if isinstance(raw_generation, int) and raw_generation >= 0:
                vector_generation = raw_generation
            else:
                vector_generation = 0
        else:
            self._validate_global_context(
                book_id=book_id,
                dependency_fingerprint=snapshot.fingerprint,
            )
        variants = [processed["cleanQuery"] or question]
        if options["useReasoning"]:
            variants.extend(processed["reasoningQueries"])
        payload = {
            "bookId": book_id,
            # A valid snapshot may be assembled from page-scoped runs and then
            # receive freshly rebuilt derived artifacts. In that case there is
            # deliberately no book head; parent-layer enrichment simply has no
            # single canonical run to attach.
            "sourceRunId": snapshot.source_run_id,
            "question": question,
            "mode": mode,
            "keywords": processed["keywords"],
            "queryVariants": list(dict.fromkeys(variants)),
            "candidateLimit": top_k * 6,
            "useParentChild": options["useParentChild"],
            "vectorGeneration": vector_generation,
            "dependencyFingerprint": snapshot.fingerprint,
            "config": config,
        }
        request_id, token = self.repository.create_vector_query(
            book_id=book_id,
            request_payload=payload,
        )
        return QAHandle(
            request_id=request_id,
            connection_token=token,
            question=question,
            options=options,
            config=config,
            book_id=book_id,
        )

    def materialize_api_config(
        self,
        config: Mapping[str, Any],
        *,
        include_reranker: bool,
    ) -> dict[str, Any]:
        section_names = ["chat"]
        if include_reranker:
            section_names.append("reranker")
        try:
            return self.credentials.resolve_credential_sections(
                config,
                section_names,
            )
        except LookupError as exc:
            raise QAConflict("frozen QA credential is missing") from exc

    def _validate_global_context(
        self,
        *,
        book_id: str,
        dependency_fingerprint: str,
    ) -> None:
        """Validate global QA context if present; graceful degradation allows fallback."""
        return


def select_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    threshold: float,
    top_k: int,
) -> list[dict[str, Any]]:
    validated = [_candidate(row) for row in candidates]
    return [
        row for row in validated if row["hybridScore"] >= threshold
    ][:top_k]


def citations_for(
    candidates: Sequence[Mapping[str, Any]],
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    page_ids: list[str] = []
    for raw in candidates:
        row = _candidate(raw)
        page_id = row["pageId"]
        if page_id is None or page_id in seen:
            continue
        seen.add(page_id)
        page_ids.append(page_id)

    page_metadata: dict[str, dict[str, Any]] = {}
    metadata_failed = False
    if engine is not None and page_ids:
        try:
            with engine.connect() as conn:
                source_assets = (
                    select(page_assets.c.page_id, page_assets.c.asset_id)
                    .where(
                        page_assets.c.page_id.in_(page_ids),
                        page_assets.c.role == "source",
                    )
                    .subquery()
                )
                query = (
                    select(
                        pages.c.id,
                        pages.c.ordinal.label("page_ordinal"),
                        chapters.c.ordinal.label("chapter_ordinal"),
                        chapters.c.title.label("chapter_title"),
                        source_assets.c.asset_id.label("source_asset_id"),
                    )
                    .join(chapters, chapters.c.id == pages.c.chapter_id)
                    .outerjoin(source_assets, source_assets.c.page_id == pages.c.id)
                    .where(pages.c.id.in_(page_ids))
                )
                for r in conn.execute(query).mappings():
                    page_metadata[r["id"]] = dict(r)
        except Exception as exc:
            metadata_failed = True
            LOGGER.warning("Failed to enrich citation chapter/page metadata: %s", exc)

    processed: set[str] = set()
    for raw in candidates:
        row = _candidate(raw)
        page_id = row["pageId"]
        if page_id is None or page_id in processed:
            continue
        processed.add(page_id)

        meta = page_metadata.get(page_id, {})
        page_ordinal = meta.get("page_ordinal")
        # 审计 P2-4：过去这里用「章内序号命中就替换、失败就回退全书页码」的单字段，
        # 同一字段会有两种语义。现在两个语义各占一个显式字段：
        #   pageInChapter     —— 章内第几页（真实可点击位置，元数据缺失时为 None）
        #   globalPageNumber  —— 该页在本 run 范围内的全书页码（模型看到的就是它）
        # pageNumber 保留为「给人看的页码」，等于章内页码，元数据缺失时才退回全书页码，
        # 同时用 pageNumberSource 明确告诉调用方它到底是哪一种。
        page_in_chapter = page_ordinal if page_ordinal is not None else None
        global_page_number = row["pageNumber"]
        if page_in_chapter is not None:
            page_num = page_in_chapter
            page_number_source = "chapter"
        else:
            page_num = global_page_number
            page_number_source = "run_global"
        chapter_ordinal = (
            meta.get("chapter_ordinal")
            if meta.get("chapter_ordinal") is not None
            else (row.get("chapterOrdinal") or row.get("chapter_ordinal"))
        )
        chapter_title = (
            meta.get("chapter_title")
            or row.get("chapterTitle")
            or row.get("chapter_title")
        )
        source_asset_id = meta.get("source_asset_id")
        source_url = f"/api/v2/assets/{source_asset_id}" if source_asset_id else None

        citations.append(
            {
                "pageId": page_id,
                "page_id": page_id,
                "pageNumber": page_num,
                "page_number": page_num,
                "pageNumberSource": page_number_source,
                "page_number_source": page_number_source,
                "pageInChapter": page_in_chapter,
                "page_in_chapter": page_in_chapter,
                "globalPageNumber": global_page_number,
                "global_page_number": global_page_number,
                "chapterOrdinal": chapter_ordinal,
                "chapter_ordinal": chapter_ordinal,
                "chapterTitle": chapter_title,
                "chapter_title": chapter_title,
                "sourceUrl": source_url,
                "source_url": source_url,
                "source": "page",
                "excerpt": row["document"],
                "score": row.get("rerankScore", row["hybridScore"]),
            }
        )
    if metadata_failed:
        LOGGER.warning(
            "Citation chapter metadata unavailable; %d citation(s) fall back to run-global page numbers",
            len(citations),
        )
    return citations


def page_locations(
    snapshot_pages: Sequence[Mapping[str, Any]],
    *,
    engine: Engine | None = None,
) -> dict[int, dict[str, Any]]:
    """Map run-scoped page numbers to real chapter identity.

    Entity knowledge records evidence as run-scoped page numbers, while citations
    render pages with the chapter they belong to. This index is the bridge between
    the two so answers can state「第 X 话 第 Y 页」instead of a bare global number.
    """
    numbered: dict[str, int] = {}
    for item in snapshot_pages:
        if not isinstance(item, Mapping):
            continue
        page_id = item.get("pageId")
        number = item.get("pageNumber")
        if (
            isinstance(page_id, str)
            and page_id
            and isinstance(number, int)
            and not isinstance(number, bool)
            and number >= 1
        ):
            numbered.setdefault(page_id, number)
    locations: dict[int, dict[str, Any]] = {}
    if not numbered:
        return locations

    metadata: dict[str, Mapping[str, Any]] = {}
    if engine is not None:
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    select(
                        pages.c.id,
                        pages.c.ordinal.label("page_in_chapter"),
                        chapters.c.ordinal.label("chapter_ordinal"),
                        chapters.c.title.label("chapter_title"),
                    )
                    .join(chapters, chapters.c.id == pages.c.chapter_id)
                    .where(pages.c.id.in_(list(numbered)))
                ).mappings()
                for row in rows:
                    metadata[row["id"]] = row
        except Exception as exc:
            LOGGER.warning("Failed to map QA page numbers to chapters: %s", exc)

    for page_id, number in numbered.items():
        meta = metadata.get(page_id) or {}
        locations[number] = {
            "pageId": page_id,
            "pageNumber": number,
            "chapterOrdinal": meta.get("chapter_ordinal"),
            "chapterTitle": meta.get("chapter_title"),
            "pageInChapter": meta.get("page_in_chapter"),
        }
    return locations


def entity_page_candidates(
    resolved_entities: Mapping[str, Any] | None,
    *,
    snapshot_pages: Sequence[Mapping[str, Any]],
    locations: Mapping[int, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build candidates for the pages an entity knowledge entry points at.

    The entity knowledge base records「出现页码」but those pages were never part of
    the retrieved material, so the model could only answer page numbers without
    any page evidence behind them. These candidates make that evidence citable.
    """
    if not isinstance(resolved_entities, Mapping) or not locations:
        return []
    by_page_id: dict[str, Mapping[str, Any]] = {}
    for item in snapshot_pages:
        if not isinstance(item, Mapping):
            continue
        page_id = item.get("pageId")
        if isinstance(page_id, str) and page_id:
            by_page_id.setdefault(page_id, item)

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    matched = resolved_entities.get("matched_entities")
    if not isinstance(matched, list):
        return []
    for entity in matched:
        if not isinstance(entity, Mapping):
            continue
        for value in entity.get("pages") or []:
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            info = locations.get(value)
            if not isinstance(info, Mapping):
                continue
            page_id = info.get("pageId")
            if not isinstance(page_id, str) or page_id in seen:
                continue
            item = by_page_id.get(page_id)
            if item is None:
                continue
            analysis = item.get("analysis")
            if not isinstance(analysis, Mapping):
                analysis = {}
            seen.add(page_id)
            candidates.append(
                {
                    "id": f"entity_page_{page_id}",
                    "type": "page",
                    "pageId": page_id,
                    "pageNumber": value,
                    "document": _page_document(value, analysis),
                    "hybridScore": 1.0,
                }
            )
    return candidates


def preprocess_question(question: str) -> dict[str, Any]:
    clean = question.strip()
    for word in _QUESTION_WORDS:
        clean = clean.replace(word, " ")
    clean = re.sub(r"[？?！!。,.，；;：:\s]+", " ", clean).strip()
    tokens = [
        token.lower()
        for token in _TOKEN_PATTERN.findall(clean or question)
        if token.lower() not in _QUESTION_WORDS
    ]
    keywords = list(dict.fromkeys(tokens))
    reasoning = [
        part.strip()
        for part in re.split(r"[，,；;。！？!?]|以及|并且|然后|与", question)
        if len(part.strip()) >= 2
    ]
    return {
        "cleanQuery": clean,
        "keywords": keywords,
        "reasoningQueries": reasoning,
    }


def _lexical_score(document: str, keywords: Sequence[str]) -> float:
    if not keywords:
        return 0.0
    lowered = document.lower()
    hits = sum(1 for keyword in keywords if keyword.lower() in lowered)
    return hits / len(keywords)


def _page_document(page_number: int, analysis: Mapping[str, Any]) -> str:
    """Render one page analysis payload as the retrieval document for that page."""
    summary = analysis.get("page_summary") or ""
    events = analysis.get("key_events") or []
    event_strs: list[str] = []
    if isinstance(events, list):
        for event in events:
            if isinstance(event, Mapping) and event.get("summary"):
                event_strs.append(str(event["summary"]))
            elif isinstance(event, str) and event:
                event_strs.append(event)

    parts: list[str] = []
    if summary:
        parts.append(str(summary))
    if event_strs:
        parts.append("关键事件：" + "；".join(event_strs))
    document = "\n".join(parts).strip()
    return document or f"第 {page_number} 页"


_CATEGORY_CN = {
    "characters": "角色",
    "abilities": "能力",
    "items": "物品",
    "locations": "地点",
    "concepts": "概念",
}


def _format_pages_list(pages: Sequence[Any]) -> str:
    if not pages:
        return "无"
    sorted_pages = sorted(
        set(int(p) for p in pages if isinstance(p, (int, str)) and str(p).isdigit())
    )
    if not sorted_pages:
        return "无"
    # 若完全连续且数量 >= 3，显示为 1-46
    if len(sorted_pages) >= 3 and sorted_pages[-1] - sorted_pages[0] + 1 == len(sorted_pages):
        return f"{sorted_pages[0]}-{sorted_pages[-1]}"
    if len(sorted_pages) <= 25:
        return ", ".join(str(p) for p in sorted_pages)
    return ", ".join(str(p) for p in sorted_pages[:25]) + f"... 等共 {len(sorted_pages)} 页"


def _location_label(
    number: Any,
    locations: Mapping[int, Mapping[str, Any]] | None,
) -> str:
    """Render one run-scoped page number together with its chapter position."""
    info = None
    if locations and isinstance(number, int) and not isinstance(number, bool):
        info = locations.get(number)
    if isinstance(info, Mapping):
        chapter = info.get("chapterOrdinal")
        page_in_chapter = info.get("pageInChapter")
        if chapter is not None and page_in_chapter is not None:
            return f"第 {chapter} 话 第 {page_in_chapter} 页（全书第 {number} 页）"
    return f"第 {number} 页"


def _page_positions(
    pages: Sequence[Any],
    locations: Mapping[int, Mapping[str, Any]] | None,
    *,
    limit: int = 12,
) -> list[str]:
    if not locations:
        return []
    positions: list[str] = []
    for value in pages:
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        info = locations.get(value)
        if not isinstance(info, Mapping):
            continue
        chapter = info.get("chapterOrdinal")
        page_in_chapter = info.get("pageInChapter")
        if chapter is None or page_in_chapter is None:
            continue
        positions.append(f"第 {chapter} 话 第 {page_in_chapter} 页（全书第 {value} 页）")
    return positions[:limit]


def _answer_context(
    candidates: Sequence[Mapping[str, Any]],
    resolved_entities: Mapping[str, Any] | None = None,
    *,
    locations: Mapping[int, Mapping[str, Any]] | None = None,
) -> str:
    sections = []

    matched = []
    if isinstance(resolved_entities, Mapping):
        matched = resolved_entities.get("matched_entities", [])

    if matched:
        entity_lines = ["[实体知识库数据]"]
        for ent in matched:
            name = ent.get("canonical_name") or ent.get("entity_id", "")
            cat = _CATEGORY_CN.get(str(ent.get("category", "")), ent.get("category", "实体"))
            aliases = ent.get("aliases") or []
            alias_str = "、".join(str(a) for a in aliases) if aliases else "无"
            pages = ent.get("pages") or []
            pages_str = _format_pages_list(pages)
            freq = ent.get("frequency", len(pages))

            if "-" in pages_str and "（共" not in pages_str:
                pages_str = f"{pages_str}（共 {len(pages)} 页）"

            entity_lines.append(
                f"- 实体：{name}（{cat}）\n"
                f"  别名：{alias_str}\n"
                f"  出现页码：{pages_str}\n"
                f"  出现频次：{freq}"
            )
            positions = _page_positions(pages, locations)
            if positions:
                entity_lines.append("  页话对应：" + "；".join(positions))
        sections.append("\n".join(entity_lines))

    if candidates:
        blocks = []
        for index, row in enumerate(candidates, start=1):
            location = (
                f"page_id={row.get('pageId')}, {_location_label(row.get('pageNumber'), locations)}"
                if row.get("pageId")
                else str(row.get("id", "全局资料"))
            )
            parent = row.get("parentContext", [])
            parent_text = (
                f"\n父级摘要：{_json(parent)}"
                if isinstance(parent, list) and parent
                else ""
            )
            blocks.append(
                f"[资料 {index} | {location}]\n"
                f"{row.get('document', '')}{parent_text}"
            )
        sections.append("[页面资料]\n" + "\n\n".join(blocks))
    elif not matched:
        sections.append("无相关漫画分析资料。")

    if matched or candidates:
        sections.append(
            "[回答要求]\n"
            "- 引用证据时写明「第 X 话 第 Y 页」；资料只给出全书页码时，按「页话对应」换算后再回答。\n"
            "- 资料中没有依据的内容必须回答「知识库中没有相关依据」，不得凭常识补全或猜测页码。"
        )

    return "\n\n".join(sections)
