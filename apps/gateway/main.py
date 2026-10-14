"""Fake Keboola apps-proxy with a fake OIDC login — LOCAL DEVELOPMENT ONLY.

One gateway instance sits in front of each app, on the app's public port,
the way the Keboola apps-proxy fronts every Data App on its own URL. It does
what the real proxy does for identity, and nothing more:

* no session → redirect to the login page (stands in for the IdP redirect);
* strip every client-sent header with the trusted prefix (`x-kbc-*`), then
  set `X-Kbc-User-Email` and `X-Kbc-User-Roles` from the session;
* pass `X-Forwarded-*` and other headers through untouched, like the real
  proxy — so the apps' hostile-header handling is exercised for real.

The "IdP" is config/fake_users.yaml plus a free-form identity form. The session
is a signed cookie shared by all gateway ports (browsers do not isolate
cookies by port), so one login covers the whole suite.
"""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from kbc_access.config import Settings, get_settings

HERE = Path(__file__).parent
HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
     "transfer-encoding", "upgrade", "host", "content-length"}
)
SESSION_SALT = "kbc-dev-gateway-session"


def _load_fake_idp(settings: Settings) -> tuple[list[dict], dict[str, dict[str, str]]]:
    raw = yaml.safe_load(settings.path(settings.require_gateway().fake_users_file).read_text(encoding="utf-8"))
    return raw["users"], raw["groups"]


def _safe_next(target: str | None) -> str:
    """Only same-origin relative paths; anything else goes to the app root (no open redirect)."""
    if not target:
        return "/"
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def create_gateway(settings: Settings, app_id: str, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    if app_id not in settings.apps:
        raise ValueError(f"App '{app_id}' is not configured in [apps]")
    app_cfg = settings.apps[app_id]
    gw = settings.require_gateway()
    upstream = f"http://{gw.host}:{app_cfg.port}"
    ident = settings.identity
    assert settings.secrets.gateway_session_secret is not None  # require_gateway checked it
    serializer = URLSafeSerializer(settings.secrets.gateway_session_secret, salt=SESSION_SALT)
    users, provider_groups = _load_fake_idp(settings)
    # key -> (provider, external id), for rendering and for resolving persona groups.
    groups = {key: (provider, gid) for provider, entries in provider_groups.items() for key, gid in entries.items()}
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    client = httpx.AsyncClient(base_url=upstream, timeout=gw.upstream_timeout_seconds, transport=transport)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await client.aclose()

    app = FastAPI(title=f"dev gateway for {app_id}", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def session(request: Request) -> dict | None:
        raw = request.cookies.get(gw.session_cookie)
        if not raw:
            return None
        try:
            value = serializer.loads(raw)
        except BadSignature:
            return None
        return value if isinstance(value, dict) and value.get("email") else None

    @app.get(gw.login_path, response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/") -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "users": [
                    {**u, "resolved": [(k, *groups[k]) for k in u["groups"]], "active": u.get("active", True)}
                    for u in users
                ],
                "groups": sorted((k, p, gid) for k, (p, gid) in groups.items()),
                "current": session(request),
                "next": _safe_next(next),
                "app_name": app_cfg.name,
                "app_id": app_id,
                "login_path": gw.login_path,
                "logout_path": gw.logout_path,
                "email_header": ident.email_header,
                "roles_header": ident.roles_header,
            },
        )

    @app.post(gw.login_path)
    def login(
        email: str = Form(...),
        groups_selected: list[str] = Form(default_factory=list),
        extra_groups: str = Form(""),
        next: str = Form("/"),
    ) -> RedirectResponse:
        extra = [g.strip() for g in extra_groups.replace(",", "\n").splitlines() if g.strip()]
        token = serializer.dumps({"email": email.strip(), "groups": sorted(set(groups_selected) | set(extra))})
        response = RedirectResponse(_safe_next(next), status_code=303)
        response.set_cookie(gw.session_cookie, token, httponly=True, samesite="lax", path="/")
        return response

    @app.get(gw.logout_path)
    def logout() -> RedirectResponse:
        response = RedirectResponse(gw.login_path, status_code=303)
        response.delete_cookie(gw.session_cookie, path="/")
        return response

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def proxy(request: Request, path: str) -> Response:
        current = session(request)
        if current is None:
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"{gw.login_path}?next={quote(target, safe='')}", status_code=303)

        prefix = ident.trusted_prefix.lower()
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP and not k.lower().startswith(prefix) and k.lower() != "cookie"
        }
        # Forward other cookies but never the gateway session itself.
        other_cookies = "; ".join(f"{k}={v}" for k, v in request.cookies.items() if k != gw.session_cookie)
        if other_cookies:
            headers["cookie"] = other_cookies
        headers[ident.email_header] = current["email"]
        headers[ident.roles_header] = ident.roles_separator.join(current["groups"])

        upstream_request = client.build_request(
            request.method, "/" + path, params=request.query_params, headers=headers, content=await request.body()
        )
        try:
            upstream_response = await client.send(upstream_request)
        except httpx.ConnectError:
            return HTMLResponse(f"<h1>502</h1><p>App '{app_id}' is not running on {upstream}.</p>", status_code=502)
        out_headers = {
            k: v for k, v in upstream_response.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != "content-encoding"
        }
        return Response(upstream_response.content, status_code=upstream_response.status_code, headers=out_headers)

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the dev gateway in front of one app.")
    parser.add_argument("--app", required=True, help="app id from [apps] in the config")
    args = parser.parse_args()
    settings = get_settings()
    gateway = settings.require_gateway()
    port = settings.apps[args.app].public_port
    if port is None:
        raise ValueError(f"app '{args.app}' has no public_port for the dev gateway")
    uvicorn.run(create_gateway(settings, args.app), host=gateway.host, port=port)


if __name__ == "__main__":
    main()
