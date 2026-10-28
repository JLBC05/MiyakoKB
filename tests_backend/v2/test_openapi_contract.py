from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml

from src.backend_v2.api.app import ApiSettings, create_api_app
from src.backend_v2.runtime_identity import RuntimeIdentity
from src.backend_v2.runtime_profile import resolve_runtime_profile
from src.backend_v2.storage.database import create_sqlite_engine
from src.backend_v2.storage.schema import metadata
from src.backend_v2.storage.seeding import seed_system_records


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = PROJECT_ROOT / "openapi" / "v2.yaml"
GENERATED_TYPES = PROJECT_ROOT / "vue-frontend" / "src" / "api" / "generated" / "v2.ts"
V2_API_DIRECTORY = PROJECT_ROOT / "vue-frontend" / "src" / "api" / "v2"
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
UNKEYED_STATE_COMMANDS = frozenset(
    {
        "batchDeleteBooks",
        "batchUpdateBookTags",
        "cancelJob",
        "cancelQueuedJobBatchMembers",
        "cancelQueuedJobs",
        "clearChapterPages",
        "clearJobHistory",
        "confirmLocalImport",
        "continueInterruptedJob",
        "continueJobBatchMembers",
        "createBook",
        "createChapter",
        "createTag",
        "deleteBook",
        "deleteChapter",
        "deletePage",
        "deleteTag",
        "importVeneraComics",
        "pauseRunningJob",
        "pauseJobQueue",
        "prioritizeQueuedJobBatchMembers",
        "reorderChapterPages",
        "reorderChapters",
        "reorderJobs",
        "resumePausedJob",
        "resumeJobQueue",
        "updateBook",
        "updateChapter",
        "updateTag",
    }
)


def _document() -> dict[str, Any]:
    value = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _normalize_runtime_path(path: str) -> str:
    without_prefix = path.removeprefix("/api/v2")
    return re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", without_prefix)


def test_openapi_operations_match_the_runtime_route_set(tmp_path: Path) -> None:
    runtime_operations: set[tuple[str, str]] = set()
    runtimes = []
    try:
        for profile_name in ("local", "public"):
            data_root = tmp_path / f"{profile_name}-data"
            data_root.mkdir()
            engine = create_sqlite_engine(data_root / "saber.sqlite3")
            metadata.create_all(engine)
            seed_system_records(engine, profile_name=profile_name)
            app = create_api_app(
                ApiSettings(
                    data_root=data_root,
                    identity=RuntimeIdentity(
                        epoch_id=f"openapi-contract-{profile_name}",
                        epoch_token="test-token",
                        test_mode=True,
                    ),
                    engine=engine,
                    host=("127.0.0.1" if profile_name == "public" else "0.0.0.0"),
                    profile=resolve_runtime_profile(profile_name),
                    public_host=(
                        "public.example.test" if profile_name == "public" else None
                    ),
                )
            )
            runtimes.append((app, engine))
            runtime_operations.update(
                (method.lower(), _normalize_runtime_path(rule.rule))
                for rule in app.url_map.iter_rules()
                if rule.rule.startswith("/api/v2/")
                and rule.rule != "/api/v2/openapi.json"
                for method in rule.methods - {"HEAD", "OPTIONS"}
            )

        document = _document()
        contract_operations = {
            (method, path)
            for path, path_item in document["paths"].items()
            for method in HTTP_METHODS
            if method in path_item
        }
        assert contract_operations == runtime_operations
    finally:
        for app, engine in runtimes:
            app.extensions["saber_v2_runtime"].close()
            engine.dispose()


def test_lan_writes_are_not_restricted_by_origin_without_wildcard_cors(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data-v2"
    data_root.mkdir()
    engine = create_sqlite_engine(data_root / "saber.sqlite3")
    metadata.create_all(engine)
    seed_system_records(engine)
    app = create_api_app(
        ApiSettings(
            data_root=data_root,
            identity=RuntimeIdentity(
                epoch_id="origin-contract",
                epoch_token="test-token",
                test_mode=True,
            ),
            engine=engine,
        )
    )
    client = app.test_client()
    try:
        same_origin = client.post(
            "/api/v2/books",
            json={"title": "same origin"},
            headers={
                "Host": "192.168.1.8:5000",
                "Origin": "http://192.168.1.8:5000",
                "Idempotency-Key": "same-origin-book",
            },
        )
        assert same_origin.status_code == 201
        assert "Access-Control-Allow-Origin" not in same_origin.headers

        local_alias = client.post(
            "/api/v2/books",
            json={"title": "local alias"},
            headers={
                "Host": "127.0.0.1:5000",
                "Origin": "http://localhost:5000",
                "Idempotency-Key": "local-alias-book",
            },
        )
        assert local_alias.status_code == 201
        assert "Access-Control-Allow-Origin" not in local_alias.headers

        cross_origin = client.post(
            "/api/v2/books",
            json={"title": "cross origin"},
            headers={
                "Host": "192.168.1.8:5000",
                "Origin": "http://malicious.example:5000",
                "Idempotency-Key": "cross-origin-book",
            },
        )
        assert cross_origin.status_code == 201
        assert "Access-Control-Allow-Origin" not in cross_origin.headers

        command_line_client = client.post(
            "/api/v2/books",
            json={"title": "trusted LAN client"},
            headers={
                "Host": "192.168.1.8:5000",
                "Idempotency-Key": "trusted-lan-book",
            },
        )
        assert command_line_client.status_code == 201
        assert "Access-Control-Allow-Origin" not in command_line_client.headers
    finally:
        app.extensions["saber_v2_runtime"].close()
        engine.dispose()


def test_openapi_contract_is_closed_and_all_local_refs_resolve() -> None:
    document = _document()
    assert document["openapi"].startswith("3.1.")
    for node in _walk(document):
        reference = node.get("$ref")
        if not isinstance(reference, str) or not reference.startswith("#/"):
            continue
        resolved: Any = document
        for token in reference[2:].split("/"):
            resolved = resolved[token.replace("~1", "/").replace("~0", "~")]
        assert resolved is not None


def test_every_operation_has_a_unique_operation_id_and_responses() -> None:
    document = _document()
    operation_ids: list[str] = []
    for path, path_item in document["paths"].items():
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if operation is None:
                continue
            operation_id = operation.get("operationId")
            assert isinstance(operation_id, str) and operation_id, (
                f"{method.upper()} {path} has no operationId"
            )
            assert operation.get("responses"), (
                f"{method.upper()} {path} has no responses"
            )
            operation_ids.append(operation_id)
    assert len(operation_ids) == len(set(operation_ids))


def test_public_operations_do_not_use_an_untyped_generic_object() -> None:
    document = _document()
    assert "GenericObject" not in document["components"]["schemas"]
    assert "GenericSuccess" not in document["components"]["responses"]
    serialized = yaml.safe_dump(document, sort_keys=False)
    assert "#/components/schemas/GenericObject" not in serialized


def test_required_backend_first_commands_are_explicit() -> None:
    document = _document()
    paths = document["paths"]
    # The operations/repairs, studio chat and page-document domains were removed
    # from src/** by the cleanup commits (src/backend_v2/operations/* is gone and
    # the runtime exposes none of those route sets), so they are no longer
    # backend-first commands that the contract must pin.
    assert {
        "/pages/{page_id}",
        "/jobs/{job_id}/resume",
        "/jobs/{job_id}/continue",
    } <= set(paths)
    assert "/jobs/{job_id}/download" not in paths
    assert "/job-batches/{batch_id}" not in paths

    schemas = document["components"]["schemas"]
    assert schemas["JobStatus"]["enum"]
    assert schemas["JobCommandResult"]["required"] == ["jobId", "status"]
    assert schemas["BatchContinueResult"]["required"] == ["continued"]
    list_scope = paths["/jobs"]["get"]["parameters"][0]["schema"]["enum"]
    assert list_scope == ["queue", "history", "all"]


def test_idempotency_keys_are_reserved_for_replayable_commands() -> None:
    document = _document()
    found_unkeyed_state_commands: set[str] = set()
    for path, path_item in document["paths"].items():
        for method in ("post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if operation is None:
                continue
            parameters = [*path_item.get("parameters", []), *operation.get("parameters", [])]
            refs = {parameter.get("$ref") for parameter in parameters}
            has_key = "#/components/parameters/IdempotencyKey" in refs
            operation_id = operation["operationId"]
            if operation.get("x-command-mode") == "transient":
                assert not has_key, f"{operation_id} is transient but requires Idempotency-Key"
            elif operation_id in UNKEYED_STATE_COMMANDS:
                found_unkeyed_state_commands.add(operation_id)
                assert not has_key, f"{operation_id} is a simple state command"
            else:
                assert has_key, f"{method.upper()} {path} has no Idempotency-Key"
    assert found_unkeyed_state_commands == UNKEYED_STATE_COMMANDS


def test_typescript_client_is_generated_from_the_contract() -> None:
    generated = GENERATED_TYPES.read_text(encoding="utf-8")
    assert "This file was auto-generated by openapi-typescript." in generated
    assert '"/jobs/{job_id}/resume"' in generated


def test_v2_api_modules_do_not_declare_handwritten_v2_interfaces() -> None:
    declarations: list[str] = []
    pattern = re.compile(r"^\s*(?:export\s+)?interface\s+V2\w+", re.MULTILINE)
    for path in V2_API_DIRECTORY.glob("*.ts"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            declarations.append(f"{path.name}:{match.group(0).strip()}")
    assert declarations == []


# 后端会用三种写法抛出业务错误码，三种都要扫，否则会漏登记（历史上就漏了 22 个）。
_ERROR_CODE_PATTERNS = (
    re.compile(r'_error\(\s*["\']([a-z0-9_]+)["\']'),
    re.compile(r'(?<![\w])code\s*=\s*["\']([a-z0-9_]+)["\']'),
    re.compile(r'["\']code["\']\s*:\s*["\']([a-z0-9_]+)["\']'),
)


def _backend_error_codes() -> dict[str, str]:
    """扫描 src/backend_v2 下全部 Python 文件，返回 {错误码: 首个出处}。"""
    codes: dict[str, str] = {}
    for path in (PROJECT_ROOT / "src" / "backend_v2").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in _ERROR_CODE_PATTERNS:
            for match in pattern.finditer(text):
                code = match.group(1)
                if code not in codes:
                    line = text.count("\n", 0, match.start()) + 1
                    codes[code] = f"{path.relative_to(PROJECT_ROOT).as_posix()}:{line}"
    return codes


def test_every_backend_error_code_is_declared_in_the_schema() -> None:
    """任何后端会返回的错误码都必须在 ErrorDetail.code 里登记。

    漏登记会让前端类型收窄失效、客户端分支写不出来；这条测试是唯一守门人，
    新增 code 忘记登记时会在这里失败并指出出处。
    """
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    declared = set(
        spec["components"]["schemas"]["ErrorDetail"]["properties"]["code"]["enum"]
    )
    used = _backend_error_codes()
    missing = {code: where for code, where in used.items() if code not in declared}
    assert missing == {}, (
        "以下错误码后端在用但未在 openapi ErrorDetail.code 登记：\n"
        + "\n".join(f"  {code} @ {where}" for code, where in sorted(missing.items()))
    )


def test_error_code_scan_finds_known_codes() -> None:
    """自检：扫描器本身不能悄悄失效（正则写错会让上一条测试永远通过）。"""
    used = _backend_error_codes()
    assert "not_found" in used
    assert "validation_error" in used
    assert "revision_conflict" in used
    assert len(used) >= 25


def test_insight_precondition_and_lock_conflict_use_different_codes(
    tmp_path: Path,
) -> None:
    """前置条件不满足与乐观锁冲突必须给前端不同的 code（U-1）。

    两者语义不同：前者重试无用（要先完成某个前置动作），后者重试有用
    （别人先改了数据）。此前都映射成 ``revision_conflict``，前端只会刷新快照
    后无限重试。这条测试锁住两个分支的 code 与状态码。
    """
    from src.backend_v2.insight.errors import InsightPrecondition
    from src.backend_v2.insight.repository import InsightConflict, InsightLocked

    root = tmp_path / "data-v2"
    root.mkdir()
    engine = create_sqlite_engine(root / "saber.sqlite3")
    metadata.create_all(engine)
    seed_system_records(engine)
    try:
        app = create_api_app(
            ApiSettings(
                data_root=root,
                identity=RuntimeIdentity(
                    epoch_id="test-api", epoch_token="test-token", test_mode=True
                ),
                engine=engine,
            )
        )
        blueprint = app.blueprints[
            next(name for name in app.blueprints if name.startswith("insight_v2"))
        ]
        handlers = blueprint.error_handler_spec[None][None]

        def call(exc: Exception) -> tuple[int, str]:
            with app.test_request_context("/probe"):
                response = handlers[type(exc)](exc)
            status = response[1] if isinstance(response, tuple) else response.status_code
            return status, response[0].get_json()["error"]["code"]

        assert call(InsightPrecondition("该书还没有实体知识网络，请先完成一次知识库构建")) == (
            409,
            "conflict",
        )
        assert call(InsightConflict("revision mismatch")) == (409, "revision_conflict")
        assert call(InsightLocked("locked")) == (423, "resource_locked")
        # 分层不是重命名：前置条件仍继承父类，旧 except InsightConflict 照样接得住。
        assert isinstance(InsightPrecondition("x"), InsightConflict)
    finally:
        engine.dispose()


def test_character_skill_reports_missing_alignment_as_a_precondition() -> None:
    """真实调用点：前置条件类错误的调用点必须抛 InsightPrecondition，不能回退成纯冲突。"""
    source = (
        PROJECT_ROOT / "src" / "backend_v2" / "insight" / "character_skill.py"
    ).read_text(encoding="utf-8")
    assert 'raise InsightPrecondition("该书还没有实体知识网络' in source
    assert 'raise InsightPrecondition("该书还没有已发布的页面分析结果")' in source
    assert "from src.backend_v2.insight.errors import InsightPrecondition" in source


