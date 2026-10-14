"""FastAPI integration: how an app protects itself in one call.

    runtime = build_runtime()
    app = FastAPI()
    access = install(app, runtime, app_id="sales", templates_dir=HERE / "templates")

    @app.get("/")
    def home(request: Request, principal: Principal = Depends(access.require)):
        ...

`install` wires the Keboola health check (POST /), JSON request logging to
logs/, and the 401/403 responses: HTML pages for browser routes, JSON for
paths under /api/. `access.require` builds the principal from the proxy
headers and runs the app gate; `access.identify` only builds the principal
(for the launcher, which lists apps but protects none).

Apps with a single-page frontend (the access admin) additionally use:

* `require_csrf_header` on their /api routes — every request other than
  GET/HEAD/OPTIONS must carry the `CSRF_HEADER`. A browser sends a custom
  header cross-origin only after a CORS preflight, which these apps never
  allow, so a forged form or cross-site fetch cannot mutate anything.
* `mount_spa(app, dist_dir)` — serves the built frontend: `/assets/*` from
  `dist/assets` and `dist/index.html` for every other GET outside /api/, without
  identity (the shell carries no data; all data comes from the gated API).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from kbc_access.audit import AuditLog
from kbc_access.config import Settings, get_settings
from kbc_access.backends.duckdb_backend import DuckDBBackend
from kbc_access.gate import Gate, GateDecision
from kbc_access.guard import DataGuard
from kbc_access.identity import IdentityError, IdentityResult, Principal, principal_from_headers
from kbc_access.policy import load_policy
from kbc_access.keboola_storage import StorageClient, StorageSettings
from kbc_access.keboola_store import KeboolaAccessStore
from kbc_access.storage_access import WarehouseRefresher
from kbc_access.store import AccessStore, PrincipalSecurity

SHARED_TEMPLATES = Path(__file__).parent / "templates"
# Protocol constants shared with single-page frontends (see the module docstring).
API_PREFIX = "/api/"
ASSETS_PREFIX = "/assets/"
# Deliberately outside the trusted `x-kbc-*` prefix: the proxy (and the dev gateway)
# strips every client-sent header with that prefix, which would drop this one too.
CSRF_HEADER = "X-Access-Csrf"
CSRF_EXEMPT_METHODS = ("GET", "HEAD", "OPTIONS")


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    store: AccessStore | KeboolaAccessStore
    gate: Gate
    audit: AuditLog
    logger: logging.Logger


def _json_logger(settings: Settings) -> logging.Logger:
    path = settings.path(settings.logging.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # One logger per log file, so runtimes with different settings never share a handler.
    logger = logging.getLogger(f"kbc_access.requests.{path.resolve()}")
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(settings.logging.level)
    return logger


def build_store(settings: Settings) -> AccessStore | KeboolaAccessStore:
    """The access store the config selects: local SQLite or Keboola Storage tables."""
    if settings.store.backend == "keboola":
        kbc = settings.store.keboola
        assert kbc is not None and settings.secrets.kbc_token is not None  # guaranteed by config validation
        client = StorageClient(
            StorageSettings(
                stack_url=kbc.stack_url,
                token=settings.secrets.kbc_token,
                http_timeout_seconds=kbc.http_timeout_seconds,
                job_timeout_seconds=kbc.job_timeout_seconds,
                job_poll_seconds=kbc.job_poll_seconds,
                preview_row_limit=kbc.preview_row_limit,
            )
        )
        return KeboolaAccessStore(client, kbc.bucket_id, settings.providers, read_cache_seconds=kbc.read_cache_seconds)
    assert settings.store.sqlite_path is not None  # guaranteed by config validation
    return AccessStore(settings.path(settings.store.sqlite_path), settings.providers)


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    store = build_store(settings)
    return Runtime(
        settings=settings,
        store=store,
        gate=Gate(store, settings.gate.cache_ttl_seconds),
        audit=AuditLog(settings.path(settings.audit.path)),
        logger=_json_logger(settings),
    )


_refreshers: dict[Path, WarehouseRefresher] = {}


def build_backend(runtime: Runtime) -> DuckDBBackend:
    """The warehouse backend of an app with `[data]`.

    With `[data] storage_access_tables` (deployed apps) the warehouse is first pulled
    through Direct Storage Access and then kept fresh in the background. Raises
    FileNotFoundError when the warehouse file does not exist (yet).
    """
    settings = runtime.settings
    data, _ = settings.require_data()
    if data.storage_access_tables is not None and data.refresh_seconds is not None:
        path = settings.path(data.duckdb_path)
        if path not in _refreshers:
            refresher = WarehouseRefresher(data.storage_access_tables, path, data.refresh_seconds)
            runtime.logger.info(json.dumps({"event": "warehouse_sync", "tables": refresher.start()}))
            _refreshers[path] = refresher
    return DuckDBBackend(settings.path(data.duckdb_path), data.max_result_rows)


def build_guard(runtime: Runtime, app_id: str) -> DataGuard:
    """Data guard for an app: warehouse backend + policy catalogue validated against its schemas.

    Row lines and column masks come from the same cached ACL snapshot as the gate, so a
    saved change applies within `[gate] cache_ttl_seconds`.
    """
    settings = runtime.settings
    data, policy_settings = settings.require_data()
    backend = build_backend(runtime)
    policy = load_policy([settings.path(f) for f in policy_settings.files], backend.column_types())

    def security(principal: Principal) -> PrincipalSecurity:
        return runtime.gate.snapshot().security_for(principal)

    return DataGuard(
        policy, backend, runtime.audit, salt=settings.secrets.mask_salt, app_id=app_id,
        max_query_chars=data.max_query_chars, security=security,
    )


class NotAuthenticated(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


class AccessDenied(Exception):
    def __init__(self, principal: Principal, decision: GateDecision) -> None:
        self.principal = principal
        self.decision = decision


class MissingCsrfHeader(Exception):
    """A state-changing /api request without the CSRF header."""


def is_api_path(request: Request) -> bool:
    return request.url.path.startswith(API_PREFIX)


def require_csrf_header(request: Request) -> None:
    """Dependency for /api routes: reject non-safe methods that lack `CSRF_HEADER`."""
    if request.method not in CSRF_EXEMPT_METHODS and not request.headers.get(CSRF_HEADER):
        raise MissingCsrfHeader()


DENY_MESSAGES = {
    "no_grant": "Neither you nor any of your groups (IdP or local) has been granted access to this app.",
    "unknown_app": "This app is not registered in the access admin.",
    "app_inactive": "This app is switched off in the access admin.",
    "user_deactivated": "Your account is deactivated in the access admin.",
}


class Access:
    def __init__(self, runtime: Runtime, app_id: str, templates: Jinja2Templates) -> None:
        self.runtime = runtime
        self.app_id = app_id
        self.templates = templates

    def _identity(self, request: Request) -> IdentityResult:
        try:
            result = principal_from_headers(request.headers, self.runtime.settings.identity)
        except IdentityError as exc:
            self.runtime.audit.gate(
                app_id=self.app_id, email=None, allowed=False, reason="not_authenticated",
                matched=(), hostile_headers=(), path=request.url.path,
            )
            raise NotAuthenticated(str(exc)) from exc
        # Local groups come from the ACL snapshot (same cache as the gate), never from headers.
        local_groups = self.runtime.gate.snapshot().local_groups_of(result.principal.email)
        return IdentityResult(
            principal=result.principal.with_local_groups(local_groups), hostile_headers=result.hostile_headers
        )

    def identify(self, request: Request) -> Principal:
        """Principal only; no app gate. Hostile headers are still audited."""
        result = self._identity(request)
        if result.hostile_headers:
            self.runtime.audit.gate(
                app_id=self.app_id, email=result.principal.email, allowed=True, reason="identified_only",
                matched=(), hostile_headers=result.hostile_headers, path=request.url.path,
            )
        return result.principal

    def require(self, request: Request) -> Principal:
        """Principal that passed the app gate; raises AccessDenied otherwise."""
        result = self._identity(request)
        decision = self.runtime.gate.check(result.principal, self.app_id)
        self.runtime.audit.gate(
            app_id=self.app_id, email=result.principal.email, allowed=decision.allowed, reason=decision.reason,
            matched=decision.matched, hostile_headers=result.hostile_headers, path=request.url.path,
        )
        if not decision.allowed:
            raise AccessDenied(result.principal, decision)
        return result.principal

    def require_with_attributes(self, request: Request) -> Principal:
        """Gate-checked principal enriched with its user_attributes (for the data guard)."""
        principal = self.require(request)
        return principal.with_attributes(self.runtime.store.attributes_for(principal.email))

    def navigation(self) -> dict[str, str]:
        """Where a signed-out or refused user can go next (the SPA's 401/403 screens link to these)."""
        settings = self.runtime.settings
        return {"launcher_url": settings.app_url("launcher"), "switch_user_path": settings.ui.switch_user_path}

    def render(self, request: Request, template: str, principal: Principal | None, status_code: int = 200, **context: object) -> HTMLResponse:
        settings = self.runtime.settings
        return self.templates.TemplateResponse(
            request,
            template,
            {
                "principal": principal,
                "app_id": self.app_id,
                "app_name": settings.apps[self.app_id].name,
                "launcher_url": settings.app_url("launcher"),
                "switch_user_path": settings.ui.switch_user_path,
                **context,
            },
            status_code=status_code,
        )


def install(app: FastAPI, runtime: Runtime, *, app_id: str, templates_dir: Path | None) -> Access:
    if app_id not in runtime.settings.apps:
        raise ValueError(f"App '{app_id}' is not configured in [apps]")
    dirs = [str(templates_dir)] if templates_dir else []
    env = Environment(loader=ChoiceLoader([FileSystemLoader(d) for d in [*dirs, str(SHARED_TEMPLATES)]]), autoescape=True)
    templates = Jinja2Templates(env=env)
    access = Access(runtime, app_id, templates)

    @app.post("/", include_in_schema=False)
    def keboola_health_check() -> JSONResponse:
        # The Keboola platform probes Data Apps with POST /.
        return JSONResponse({"status": "ok"})

    @app.middleware("http")
    async def log_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        response: Response = await call_next(request)
        runtime.logger.info(
            json.dumps(
                {
                    "ts": time.time(),
                    "app_id": app_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "email": request.headers.get(runtime.settings.identity.email_header),
                }
            )
        )
        return response

    @app.exception_handler(NotAuthenticated)
    def not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
        if is_api_path(request):
            return JSONResponse(
                {"detail": exc.reason, "reason": "not_authenticated", **access.navigation()}, status_code=401
            )
        # The Keboola platform's startup probe calls GET / straight on the container,
        # without the proxy and so without identity headers, and only marks the app
        # running on a 2xx (seen live: a 401 here kept the app "starting" until it was
        # killed). The root therefore answers the sign-in page with 200; it carries no
        # data, and every other path still refuses with 401.
        probe_path = request.url.path == "/" and request.method in ("GET", "HEAD")
        return access.render(
            request, "not_authenticated.html", None, status_code=200 if probe_path else 401, reason=exc.reason
        )

    @app.exception_handler(AccessDenied)
    def denied(request: Request, exc: AccessDenied) -> Response:
        message = DENY_MESSAGES.get(exc.decision.reason, exc.decision.reason)
        if is_api_path(request):
            return JSONResponse(
                {
                    "detail": message,
                    "reason": exc.decision.reason,
                    "user": {"email": exc.principal.email, "groups": sorted(exc.principal.groups)},
                    **access.navigation(),
                },
                status_code=403,
            )
        return access.render(
            request, "denied.html", exc.principal, status_code=403, reason=exc.decision.reason, message=message,
        )

    @app.exception_handler(MissingCsrfHeader)
    def missing_csrf(request: Request, exc: MissingCsrfHeader) -> JSONResponse:
        return JSONResponse({"detail": "Missing CSRF header.", "reason": "csrf"}, status_code=403)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        if not is_api_path(request):
            return await request_validation_exception_handler(request, exc)
        # API clients get one readable sentence in `detail`, like every other /api error.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'] if part != 'body') or 'body'}: {error['msg']}"
            for error in exc.errors()
        )
        return JSONResponse({"detail": f"Invalid request: {problems}"}, status_code=400)

    return access


def mount_spa(app: FastAPI, dist_dir: Path) -> None:
    """Serve a built single-page frontend from `dist_dir` (call after every other route).

    `/assets/*` comes from `dist/assets`; every other GET/HEAD outside /api/
    answers `dist/index.html`, so client-side routes survive a reload. The shell
    needs no identity: it carries no data, and the Keboola startup probe (GET /
    without identity headers) gets its 2xx. Unknown /api/ paths, and any other
    method on a path no route handles, stay JSON 404s. POST / remains the health
    check registered by `install`, which must therefore run first.
    """
    index = dist_dir / "index.html"
    assets = dist_dir / "assets"
    if not index.is_file() or not assets.is_dir():
        raise RuntimeError(
            f"Frontend build not found in {dist_dir} (needs index.html and assets/). "
            f"Run `npm ci && npm run build` in {dist_dir.parent}."
        )
    app.mount(ASSETS_PREFIX.rstrip("/"), StaticFiles(directory=assets), name="assets")

    # Always revalidate the shell so a redeploy's new asset hashes are picked up.
    shell_headers = {"Cache-Control": "no-cache"}

    @app.get("/", include_in_schema=False)
    def spa_root() -> FileResponse:
        return FileResponse(index, headers=shell_headers)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def spa_fallback(request: Request, path: str) -> FileResponse:
        served = request.method in ("GET", "HEAD")
        if not served or is_api_path(request) or request.url.path.startswith(ASSETS_PREFIX):
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(index, headers=shell_headers)
