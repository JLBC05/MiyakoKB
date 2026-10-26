"""Flask application factory for v2-only API routes."""

from __future__ import annotations

from src.version import STORAGE_VERSION
from src.storage_migrator.control import business_ready

from dataclasses import dataclass, field
from copy import deepcopy
import json
import logging
from pathlib import Path
import secrets
import socket
import time
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, Flask, Response, g, jsonify, request
from sqlalchemy import Engine
import yaml

from src.backend_v2.import_guard import assert_api_import_boundary
from src.backend_v2.paths import data_root_fingerprint, project_root
from src.backend_v2.runtime_identity import (
    INTERNAL_HEALTH_TOKEN_HEADER,
    RuntimeIdentity,
)
from src.backend_v2.runtime_profile import (
    RuntimeProfile,
    resolve_runtime_profile,
    validate_profile_bind_host,
)
from src.shared.user_logging import user_log


LOGGER = logging.getLogger("saber.api.http")


@dataclass(frozen=True, slots=True)
class ApiSettings:
    data_root: Path
    identity: RuntimeIdentity
    engine: Engine
    # 默认只绑回环：要对外提供访问必须显式 --host 0.0.0.0，避免"启动即暴露"。
    host: str = "127.0.0.1"
    port: int = 5000
    profile: RuntimeProfile = resolve_runtime_profile("local")
    public_host: str | None = None


@dataclass(slots=True)
class ApiRuntimeServices:
    job_events: Any
    executors: tuple[Any, ...] = ()

    def start(self) -> None:
        self.job_events.start()
        for executor in self.executors:
            executor.start()

    def close(self) -> None:
        for executor in reversed(self.executors):
            executor.close()
        self.job_events.close()


def _load_openapi_document() -> dict[str, Any]:
    spec_path = project_root() / "openapi" / "v2.yaml"
    with spec_path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise RuntimeError("OpenAPI document must be an object")
    return document


def _create_v2_blueprint(settings: ApiSettings) -> Blueprint:
    from src.backend_v2.public_policy import (
        DEFAULT_PUBLIC_USER_POLICY,
        PublicUserPolicyRepository,
    )
    from src.backend_v2.scheduling_policy import SchedulingPolicyRepository

    blueprint = Blueprint("api_v2", __name__, url_prefix="/api/v2")
    if settings.profile.requires_auth:
        from src.backend_v2.auth.repository import AuthRepository

        auth_repository = AuthRepository(settings.engine)
        public_policy = PublicUserPolicyRepository(settings.engine)
        scheduling_policy = SchedulingPolicyRepository(settings.engine)
        registration_requires_invite = auth_repository.registration_requires_invite
        load_public_policy = public_policy.load
        load_scheduling_limit = lambda: scheduling_policy.load()[
            "maxDeepLearningConcurrency"
        ]
    else:
        local_policy = deepcopy(DEFAULT_PUBLIC_USER_POLICY)
        local_policy["settings"]["lamaDisableResize"]["editable"] = True
        local_policy["settings"]["parallel"]["allowed"] = True
        registration_requires_invite = lambda: False
        load_public_policy = lambda: deepcopy(local_policy)
        load_scheduling_limit = lambda: None

    @blueprint.get("/health")
    def health() -> Response:
        payload: dict[str, object] = {
            "status": "ok",
        }
        internal_token = request.headers.get(INTERNAL_HEALTH_TOKEN_HEADER, "")
        include_identity = settings.profile.name != "public" or (
            bool(internal_token)
            and secrets.compare_digest(
                internal_token,
                settings.identity.epoch_token,
            )
        )
        if include_identity:
            payload.update(
                {
                    "storageVersion": STORAGE_VERSION,
                    "role": "api",
                    "epochId": settings.identity.epoch_id,
                    "dataRootFingerprint": data_root_fingerprint(settings.data_root),
                }
            )
        response = jsonify(payload)
        return response

    @blueprint.get("/openapi.json")
    def openapi_document() -> Response:
        return Response(
            json.dumps(_load_openapi_document(), ensure_ascii=False),
            content_type="application/json; charset=utf-8",
        )

    @blueprint.get("/system/capabilities")
    def capabilities() -> Response:
        return jsonify(
            {
                "profile": settings.profile.name,
                "requiresAuth": settings.profile.requires_auth,
                "browserCredentials": settings.profile.browser_credentials,
                "registrationRequiresInvite": (
                    registration_requires_invite()
                ),
                "publicUserPolicy": load_public_policy(),
                "scheduling": {
                    "maxDeepLearningConcurrency": load_scheduling_limit(),
                },
                "features": {
                    "webImport": settings.profile.allow_web_import,
                    "localProviders": settings.profile.allow_local_providers,
                },
            }
        )

    @blueprint.get("/system/server-info")
    def server_info() -> Response:
        if settings.profile.name == "public":
            return jsonify({"error": {"code": "not_found", "message": "not found"}}), 404
        hostname = socket.gethostname()
        lan_url: str | None = None
        if settings.host.lower().rstrip(".") not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            try:
                lan_address = socket.gethostbyname(hostname)
            except OSError:
                lan_address = "127.0.0.1"
            lan_url = f"http://{lan_address}:{settings.port}"
        return jsonify(
            {
                "hostname": hostname,
                "host": settings.host,
                "port": settings.port,
                "lanUrl": lan_url,
            }
        )

    return blueprint


def _install_request_logging(app: Flask) -> None:
    """Record useful API timings without logging bodies, queries, or secrets."""

    @app.before_request
    def start_request_timer() -> None:
        g.saber_request_started_at = time.perf_counter()

    @app.after_request
    def log_request(response: Response) -> Response:
        started_at = getattr(g, "saber_request_started_at", None)
        duration_ms = (
            (time.perf_counter() - started_at) * 1000
            if isinstance(started_at, float)
            else 0.0
        )
        path = request.path
        successful_health_check = (
            request.method == "GET"
            and path == "/api/v2/health"
            and response.status_code < 400
            and duration_ms < 1000
        )
        if successful_health_check:
            response.headers["X-Response-Time"] = f"{duration_ms / 1000:.3f}s"
            return response
        if response.status_code >= 500 and path != "/api/v2/health":
            user_log(
                "error",
                f"接口请求失败｜{request.method} {path}｜"
                f"状态码 {response.status_code}｜耗时 {duration_ms:.1f} 毫秒",
            )
        LOGGER.debug(
            "HTTP %s %s -> %s (%.1f ms, client=%s)",
            request.method,
            path,
            response.status_code,
            duration_ms,
            request.remote_addr or "-",
        )
        response.headers["X-Response-Time"] = f"{duration_ms / 1000:.3f}s"
        return response

    @app.teardown_request
    def log_unhandled_request_error(error: BaseException | None) -> None:
        # Flask closes streaming response generators with GeneratorExit when an
        # SSE client reloads, navigates away, or otherwise disconnects.  That is
        # normal transport lifecycle, not an application failure.
        if isinstance(error, GeneratorExit):
            return
        if error is not None:
            LOGGER.error(
                "HTTP %s %s raised an unhandled exception",
                request.method,
                request.path,
                exc_info=(type(error), error, error.__traceback__),
            )


def create_api_app(settings: ApiSettings) -> Flask:
    app = Flask("saber_translator_v2", static_folder=None)
    @app.before_request
    def storage_admission():
        if request.path != "/api/v2/health" and not business_ready(settings.data_root):
            return jsonify({"error": {"code": "storage_upgrade_pending", "message": "存储升级正在等待服务就绪"}}), 503
        return None

    app.config.update(
        JSON_SORT_KEYS=False,
        SABER_V2_API_EPOCH_ID=settings.identity.epoch_id,
        SABER_V2_PROFILE=settings.profile.name,
    )
    from src.backend_v2.public_policy import PublicPolicyDenied
    from src.backend_v2.storage.assets import AssetQuotaExceeded

    @app.errorhandler(PublicPolicyDenied)
    def public_policy_denied(error: PublicPolicyDenied):
        return jsonify(
            {"error": {"code": error.code, "message": str(error)}}
        ), 403

    @app.errorhandler(AssetQuotaExceeded)
    def asset_quota_exceeded(error: AssetQuotaExceeded):
        return jsonify(
            {
                "error": {
                    "code": "asset_quota_exceeded",
                    "message": str(error),
                    "usedBytes": error.used_bytes,
                    "quotaBytes": error.quota_bytes,
                    "incomingBytes": error.incoming_bytes,
                }
            }
        ), 413

    @app.errorhandler(PermissionError)
    def permission_denied(error: PermissionError):
        return jsonify(
            {"error": {"code": "forbidden", "message": str(error)}}
        ), 403

    @app.errorhandler(404)
    def not_found(error: object):
        if request.path == "/api/v2" or request.path.startswith("/api/v2/"):
            return jsonify(
                {"error": {"code": "not_found", "message": "not found"}}
            ), 404
        return error

    if settings.profile.name == "public":
        validate_profile_bind_host(settings.profile, settings.host)
        if settings.public_host is None:
            raise ValueError("public_host is required for the public profile")
        allowed_hosts = {
            "127.0.0.1",
            "localhost",
            "::1",
            settings.public_host,
        }

        @app.before_request
        def validate_public_host():
            host = (urlsplit(request.host_url).hostname or "").lower().rstrip(".")
            if host not in allowed_hosts:
                return jsonify(
                    {"error": {"code": "invalid_host", "message": "invalid host"}}
                ), 400
            return None

        @app.after_request
        def add_public_security_headers(response: Response) -> Response:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "font-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Permissions-Policy"] = (
                "camera=(), microphone=(), geolocation=(), payment=()"
            )
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
            if request.path.startswith("/api/") or request.path == "/":
                response.headers["Cache-Control"] = "no-store"
            elif request.path.startswith(("/js/", "/assets/")):
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                )
            return response

    _install_request_logging(app)
    if settings.profile.requires_auth:
        from src.backend_v2.auth.authorization import install_route_ownership
        from src.backend_v2.auth.http import install_authentication
        from src.backend_v2.auth.repository import AuthRepository

        auth_repository = AuthRepository(settings.engine)
        install_authentication(
            app,
            repository=auth_repository,
            profile=settings.profile,
        )
        install_route_ownership(
            app,
            engine=settings.engine,
            profile=settings.profile,
        )
    app.register_blueprint(_create_v2_blueprint(settings))
    if settings.profile.requires_auth:
        from src.backend_v2.auth.routes import create_auth_blueprint

        app.register_blueprint(
            create_auth_blueprint(engine=settings.engine, profile=settings.profile)
        )
    from src.backend_v2.content.routes import create_content_blueprint
    from src.backend_v2.jobs.events import JobEventBroadcaster
    from src.backend_v2.jobs.repository import JobQueueRepository
    from src.backend_v2.jobs.routes import create_jobs_blueprint
    from src.backend_v2.insight.routes import create_insight_blueprint
    from src.backend_v2.settings.routes import create_settings_blueprint
    from src.backend_v2.transfer.routes import create_transfer_blueprint
    from src.backend_v2.web_import.routes import (
        create_web_import_blueprint,
    )
    from src.backend_v2.api.web import create_web_blueprint
    from src.backend_v2.storage.platform_repositories import ProviderRateLimiter
    from src.backend_v2.scheduling_policy import (
        SchedulingPolicyCache,
        SchedulingPolicyRepository,
    )
    from src.shared.openai_rate_limits import configure_provider_rate_limit_store

    engine = settings.engine
    configure_provider_rate_limit_store(ProviderRateLimiter(engine))
    broadcaster = JobEventBroadcaster(
        JobQueueRepository(engine),
    )
    scheduling_policy = (
        SchedulingPolicyCache(SchedulingPolicyRepository(engine))
        if settings.profile.name == "public"
        else None
    )
    app.extensions["saber_v2_runtime"] = ApiRuntimeServices(job_events=broadcaster)
    app.register_blueprint(
        create_content_blueprint(
            data_root=settings.data_root,
            engine=engine,
            profile=settings.profile,
        )
    )
    app.register_blueprint(
        create_jobs_blueprint(
            engine=engine,
            broadcaster=broadcaster,
            profile=settings.profile,
        )
    )
    app.register_blueprint(
        create_insight_blueprint(
            engine=engine,
            data_root=settings.data_root,
            profile=settings.profile,
            url_prefix="/api/v2/insight",
        )
    )
    app.register_blueprint(
        create_settings_blueprint(
            data_root=settings.data_root,
            engine=engine,
            profile=settings.profile,
        )
    )
    app.register_blueprint(
        create_transfer_blueprint(data_root=settings.data_root, engine=engine)
    )
    from src.backend_v2.manga_import.routes import create_manga_import_blueprint
    app.register_blueprint(
        create_manga_import_blueprint(data_root=settings.data_root, engine=engine)
    )
    if settings.profile.allow_web_import:
        app.register_blueprint(
            create_web_import_blueprint(
                data_root=settings.data_root,
                engine=engine,
            )
        )
    app.register_blueprint(create_web_blueprint())

    # Unit tests may share a pytest process with suites that import Torch and
    # model interfaces. Production roles still enforce the
    # process-wide guard; the isolated API probe verifies test-mode import graphs.
    if not settings.identity.test_mode:
        assert_api_import_boundary()
    return app
