"""P1：服务端内部不变量错误必须映射 500 internal_error（而不是 409）。

背景：``InsightConflict`` 曾同时承载三类语义（v0.4.2/v0.4.3 已分出
``InsightPrecondition``）。本批把「已存数据/算法约束处于不可能状态」这一类
（``stored …`` / ``frozen … is not published``）分出为 ``InsightInvariant``，
映射 ``500 internal_error``；否则前端会把它们当成「快照过期」而无限刷新重试。
"""

from __future__ import annotations

from pathlib import Path
import re

import yaml

from src.backend_v2.api.app import ApiSettings, create_api_app
from src.backend_v2.runtime_identity import RuntimeIdentity
from src.backend_v2.storage.database import create_sqlite_engine
from src.backend_v2.storage.schema import metadata
from src.backend_v2.storage.seeding import seed_system_records


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSIGHT_DIR = PROJECT_ROOT / "src" / "backend_v2" / "insight"
SPEC_PATH = PROJECT_ROOT / "openapi" / "v2.yaml"


def _build_app(tmp_path: Path):
    root = tmp_path / "data-v2"
    root.mkdir()
    engine = create_sqlite_engine(root / "saber.sqlite3")
    metadata.create_all(engine)
    seed_system_records(engine)
    app = create_api_app(
        ApiSettings(
            data_root=root,
            identity=RuntimeIdentity(
                epoch_id="test-api", epoch_token="test-token", test_mode=True
            ),
            engine=engine,
        )
    )
    return app, engine


def test_insight_invariant_maps_to_internal_error(tmp_path: Path) -> None:
    """``InsightInvariant`` → 500 ``internal_error``，并且仍然是一种 InsightConflict。"""
    from src.backend_v2.insight.errors import InsightInvariant
    from src.backend_v2.insight.repository import InsightConflict

    app, engine = _build_app(tmp_path)
    try:
        blueprint = app.blueprints[
            next(name for name in app.blueprints if name.startswith("insight_v2"))
        ]
        handlers = blueprint.error_handler_spec[None][None]
        with app.test_request_context("/probe"):
            response = handlers[InsightInvariant](
                InsightInvariant("stored active analysis runs are duplicated")
            )
        status = response[1] if isinstance(response, tuple) else response.status_code
        code = response[0].get_json()["error"]["code"]
        assert (status, code) == (500, "internal_error")
        # 兼容阶梯：继承父类，旧 except / pytest.raises 继续接得住。
        assert isinstance(InsightInvariant("x"), InsightConflict)
        # 更具体的处理器必须胜过 InsightConflict 的那一个。
        assert handlers[InsightInvariant] is not handlers[InsightConflict]
    finally:
        engine.dispose()


def test_invariant_handler_wins_over_conflict_handler(tmp_path: Path) -> None:
    """Flask 按异常的 MRO 找最具体的处理器：必须是 Invariant 那个，不是 Conflict。

    ``InsightInvariant`` 为兼容旧 ``except InsightConflict`` 而继承了父类，
    所以这条验证是必要的——否则 500 会静默退化成 409。
    """
    from src.backend_v2.insight.repository import InsightConflict, InsightInvariant

    app, engine = _build_app(tmp_path)
    try:
        blueprint = app.blueprints[
            next(name for name in app.blueprints if name.startswith("insight_v2"))
        ]
        handlers = blueprint.error_handler_spec[None][None]
        resolved = app._find_error_handler(
            InsightInvariant("stored x is invalid"), [blueprint.name]
        )
        assert resolved is handlers[InsightInvariant]
        assert resolved is not handlers[InsightConflict]
    finally:
        engine.dispose()


def test_unhandled_exception_returns_json_internal_error(tmp_path: Path) -> None:
    """任何未捕获异常都要回 JSON ``internal_error`` + ``X-Request-Id``，不能吐 HTML。"""
    app, engine = _build_app(tmp_path)
    try:
        handler = app.error_handler_spec[None][None][Exception]
        with app.test_request_context(
            "/api/v2/probe", method="GET", headers={"X-Request-Id": "rid-abc123"}
        ):
            response = handler(RuntimeError("boom"))
        assert response.status_code == 500
        body = response.get_json()
        assert body["error"]["code"] == "internal_error"
        assert "rid-abc123" in body["error"]["message"]
        assert response.headers["X-Request-Id"] == "rid-abc123"
    finally:
        engine.dispose()


def test_http_exceptions_pass_through_untouched(tmp_path: Path) -> None:
    """404/405 这类 HTTPException 不能被改写成 500。"""
    from werkzeug.exceptions import NotFound

    app, engine = _build_app(tmp_path)
    try:
        handler = app.error_handler_spec[None][None][Exception]
        with app.test_request_context("/api/v2/missing"):
            result = handler(NotFound())
        assert isinstance(result, NotFound)
    finally:
        engine.dispose()


def test_no_stored_or_frozen_not_published_raise_plain_conflict() -> None:
    """回退守卫：已迁移的文案不得再抛裸 ``InsightConflict``。"""
    patterns = (
        re.compile(r'raise InsightConflict\(\s*f?"stored [^"]*"'),
        re.compile(r'raise InsightConflict\("frozen [^"]*is not published"'),
    )
    offenders: list[str] = []
    for path in sorted(INSIGHT_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.name}:{line}: {match.group(0)}")
    assert offenders == [], "仍有内部不变量文案抛裸 InsightConflict：\n" + "\n".join(offenders)


def test_openapi_declares_internal_error() -> None:
    """契约必须声明 ``internal_error``，否则前端生成的 code 联合类型不含它。"""
    document = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    enum = document["components"]["schemas"]["ErrorDetail"]["properties"]["code"]["enum"]
    assert "internal_error" in enum
