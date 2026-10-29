"""``scope="derived"`` 重建知识库：复用已发布页分析，只重跑派生阶段。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import insert, select

from src.backend_v2.content.repository import ContentRepository
from src.backend_v2.insight.commands import InsightAnalysisCommandService
from src.backend_v2.insight.errors import InsightPrecondition
from src.backend_v2.insight.repository import ANALYSIS_RUN_SCOPES
from src.backend_v2.storage.database import create_sqlite_engine
from src.backend_v2.storage.schema import (
    analysis_heads,
    analysis_page_results,
    analysis_runs,
    assets,
    metadata,
    page_assets,
    pages,
)
from src.backend_v2.storage.seeding import seed_system_records


def _options(temperature=None) -> dict[str, object]:
    return {
        "request": {
            "force_json_output": False,
            "temperature": temperature,
            "extra_body": {},
        },
        "execution": {
            "use_stream": False,
            "rpm_limit": 0,
            "transport_retries": 1,
            "business_retries": 0,
        },
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def derived_platform(tmp_path: Path):
    data_root = tmp_path / "data-v2"
    data_root.mkdir()
    engine = create_sqlite_engine(data_root / "saber.sqlite3")
    metadata.create_all(engine)
    seed_system_records(engine)

    # 让 insight 五段配置可用（ollama 不需要 API Key），否则准入校验会拒绝建任务
    from sqlalchemy import update

    from src.backend_v2.storage.schema import app_settings, provider_settings

    with engine.begin() as connection:
        insight_row = connection.execute(
            select(app_settings.c.payload_json).where(app_settings.c.domain == "insight")
        ).scalar_one()
        insight_payload = json.loads(insight_row)
        insight_payload["vlm"] = {"provider": "ollama"}
        insight_payload["chat"] = {"provider": "ollama", "useSameAsVlm": False}
        insight_payload["embedding"] = {"provider": "ollama"}
        connection.execute(
            update(app_settings)
            .where(app_settings.c.domain == "insight")
            .values(payload_json=json.dumps(insight_payload))
        )
        connection.execute(
            insert(provider_settings),
            [
                {
                    "domain": "insight_vlm",
                    "provider": "ollama",
                    "payload_json": json.dumps(
                        {
                            "modelName": "fake-vlm",
                            "customBaseUrl": "",
                            "openaiOptions": _options(0.3),
                            "imageMaxSize": 0,
                        }
                    ),
                },
                {
                    "domain": "insight_chat",
                    "provider": "ollama",
                    "payload_json": json.dumps(
                        {
                            "modelName": "fake-chat",
                            "customBaseUrl": "",
                            "openaiOptions": _options(None),
                        }
                    ),
                },
                {
                    "domain": "insight_embedding",
                    "provider": "ollama",
                    "payload_json": json.dumps(
                        {
                            "modelName": "fake-embedding",
                            "customBaseUrl": "",
                            "rpmLimit": 0,
                            "transportRetries": 1,
                            "businessRetries": 0,
                            "timeoutSeconds": 0,
                        }
                    ),
                },
            ],
        )

    content = ContentRepository(engine)
    book = content.create_book(title="Derived Book")
    chapter = content.create_chapter(book_id=str(book["id"]), title="Chapter")
    chapter_id = str(chapter["id"])
    book_id = str(book["id"])

    page_ids: list[str] = []
    checksums: dict[str, str] = {}
    with engine.begin() as connection:
        for ordinal in (1, 2):
            page_id = str(uuid.uuid4())
            asset_id = str(uuid.uuid4())
            checksum = f"{ordinal:064x}"
            checksums[page_id] = checksum
            connection.execute(
                insert(assets).values(
                    id=asset_id,
                    relative_path=f"objects/{page_id}.png",
                    mime_type="image/png",
                    checksum=checksum,
                    byte_size=128,
                )
            )
            connection.execute(
                insert(pages).values(
                    id=page_id,
                    chapter_id=chapter_id,
                    ordinal=ordinal,
                    logical_source_path=f"page-{ordinal}.png",
                )
            )
            connection.execute(
                insert(page_assets).values(
                    page_id=page_id, role="source", asset_id=asset_id
                )
            )
            page_ids.append(page_id)

    try:
        yield {
            "data_root": data_root,
            "engine": engine,
            "book": book,
            "chapter": chapter,
            "book_id": book_id,
            "chapter_id": chapter_id,
            "page_ids": page_ids,
            "checksums": checksums,
        }
    finally:
        engine.dispose()


def _publish_page_result(platform, page_id: str, *, checksum: str | None = None) -> str:
    """插入一条已完成的页分析结果，并把它挂成该书页的 active head。"""

    engine = platform["engine"]
    run_id = str(uuid.uuid4())
    result_id = str(uuid.uuid4())
    checksum = checksum or platform["checksums"][page_id]
    with engine.begin() as connection:
        asset_id = connection.execute(
            select(page_assets.c.asset_id).where(page_assets.c.page_id == page_id)
        ).scalar_one()
        connection.execute(
            insert(analysis_runs).values(
                id=run_id,
                book_id=platform["book_id"],
                scope="full",
                status="completed",
                config_json=json.dumps({"analysis": {"slugMode": "pinyin"}}),
                target_count=2,
                success_count=1,
                published_at=_utcnow(),
            )
        )
        connection.execute(
            insert(analysis_page_results).values(
                id=result_id,
                run_id=run_id,
                page_id=page_id,
                source_asset_id=asset_id,
                source_checksum=checksum,
                page_id_snapshot=page_id,
                page_number_snapshot=1,
                payload_json=json.dumps(
                    {
                        "page_id": page_id,
                        "source_asset_id": asset_id,
                        "source_checksum": checksum,
                        "page_number_snapshot": 1,
                        "page_summary": "第一页概要",
                        "continuity_notes": "",
                        "key_events": [],
                        "warnings": [],
                        "characters": [],
                        "locations": [],
                        "abilities": [],
                        "items": [],
                        "concepts": [],
                    },
                    ensure_ascii=False,
                ),
                status="published",
            )
        )
        connection.execute(
            insert(analysis_heads).values(
                id=str(uuid.uuid4()),
                book_id=platform["book_id"],
                page_id=page_id,
                active_run_id=run_id,
                active_result_id=result_id,
            )
        )
    return run_id


def test_derived_scope_is_a_supported_run_scope() -> None:
    assert "derived" in ANALYSIS_RUN_SCOPES


def test_derived_targets_reuse_only_pages_with_matching_active_results(
    derived_platform,
) -> None:
    platform = derived_platform
    page_one, page_two = platform["page_ids"]
    _publish_page_result(platform, page_one)

    service = InsightAnalysisCommandService(platform["engine"])
    with platform["engine"].connect() as connection:
        book, targets, copies = service._derived_targets(connection, platform["book_id"])

    assert book["id"] == platform["book_id"]
    assert [target.page_id for target in targets] == [page_one]
    assert len(copies) == 1
    copy = copies[0]
    assert copy["page_id"] == page_one
    assert copy["source_checksum"] == platform["checksums"][page_one]
    assert copy["page_number"] == 1
    assert copy["payload"]["page_number_snapshot"] == 1
    assert copy["payload"]["page_summary"] == "第一页概要"
    assert page_two not in [target.page_id for target in targets]


def test_derived_targets_skips_pages_whose_source_image_changed(
    derived_platform,
) -> None:
    platform = derived_platform
    page_one, page_two = platform["page_ids"]
    # 结果记录的校验值与当前源图不一致 → 视为源图已变，必须重新分析
    _publish_page_result(platform, page_one, checksum="f" * 64)
    _publish_page_result(platform, page_two)

    service = InsightAnalysisCommandService(platform["engine"])
    with platform["engine"].connect() as connection:
        _, targets, copies = service._derived_targets(connection, platform["book_id"])

    assert [target.page_id for target in targets] == [page_two]
    assert [item["page_id"] for item in copies] == [page_two]


def test_derived_targets_require_at_least_one_reusable_page(derived_platform) -> None:
    platform = derived_platform
    service = InsightAnalysisCommandService(platform["engine"])
    with platform["engine"].connect() as connection:
        with pytest.raises(InsightPrecondition) as raised:
            service._derived_targets(connection, platform["book_id"])
    assert "先完成一次全书分析" in str(raised.value)


def test_derived_job_skips_page_analysis_steps_and_queues_only_derived_work(
    derived_platform,
) -> None:
    platform = derived_platform
    page_one, _ = platform["page_ids"]
    _publish_page_result(platform, page_one)

    from src.backend_v2.storage.schema import job_items, job_steps

    service = InsightAnalysisCommandService(platform["engine"])
    accepted = service.create_analysis_job(
        command={"bookId": platform["book_id"], "scope": "derived"},
        idempotency_key=str(uuid.uuid4()),
    )
    job_id = accepted["jobIds"][0]

    with platform["engine"].connect() as connection:
        kinds = [
            row[0]
            for row in connection.execute(
                select(job_steps.c.kind)
                .select_from(
                    job_steps.join(job_items, job_items.c.id == job_steps.c.job_item_id)
                )
                .where(job_items.c.job_id == job_id)
            )
        ]
        run = (
            connection.execute(
                select(analysis_runs.c.scope, analysis_runs.c.target_count).where(
                    analysis_runs.c.id == accepted["runId"]
                )
            )
            .mappings()
            .one()
        )
        result_ids = connection.execute(
            select(analysis_page_results.c.id).where(
                analysis_page_results.c.run_id == accepted["runId"]
            )
        ).all()

    assert "insight_analyze_batch" not in kinds
    assert "insight_stage_entity_alignment" in kinds
    assert "insight_publish_run" in kinds
    assert run["scope"] == "derived"
    assert run["target_count"] == 1
    assert len(result_ids) == 1


def test_derived_scope_is_accepted_by_the_worker_grouping_rules() -> None:
    from src.backend_v2.insight.worker import _analysis_batch_grouping

    # 派生重建不产生页分析批次；这里只保证 scope 不会被判为非法
    assert _analysis_batch_grouping(scope="derived", analysis_config={}) == "global"
