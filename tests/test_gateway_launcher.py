"""Gateway (fake apps-proxy) and launcher, wired together in-process.

The gateway forwards to the real launcher app through httpx.ASGITransport, so
these tests exercise the same header handling the browser path uses.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.gateway.main import _safe_next, create_gateway
from apps.launcher.main import create_app as create_launcher
from kbc_access.web import CSRF_HEADER

FINANCE = "3f2b8c1e-7d4a-4e5b-9a61-2c8d0f4e1b7a"
SALES_MANAGERS = "8a1d4e2f-5b6c-4d7e-9f80-1a2b3c4d5e6f"


def _echo_app() -> FastAPI:
    """Upstream that reports exactly which headers reached it."""
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def echo(request: Request, path: str) -> dict:
        return {"path": path, "headers": dict(request.headers), "body": (await request.body()).decode()}

    return app


@pytest.fixture
def echo_gateway(runtime):
    gateway = create_gateway(runtime.settings, "sales", transport=httpx.ASGITransport(app=_echo_app()))
    return TestClient(gateway, follow_redirects=False)


def _login(client: TestClient, settings, email: str, groups: list[str]) -> None:
    response = client.post(
        settings.gateway.login_path, data={"email": email, "groups_selected": groups, "next": "/"}
    )
    assert response.status_code == 303


def test_no_session_redirects_to_login_with_next(echo_gateway, runtime):
    response = echo_gateway.get("/query?x=1")
    assert response.status_code == 303
    assert response.headers["location"].startswith(runtime.settings.gateway.login_path + "?next=")
    assert "%2Fquery%3Fx%3D1" in response.headers["location"]


def test_login_page_lists_personas(echo_gateway, runtime):
    page = echo_gateway.get(runtime.settings.gateway.login_path)
    assert page.status_code == 200
    assert "petr.sales@pharma-a.example" in page.text
    assert "fake identity provider" in page.text


def test_session_becomes_proxy_headers(echo_gateway, runtime):
    ident = runtime.settings.identity
    _login(echo_gateway, runtime.settings, "petr.sales@pharma-a.example", [SALES_MANAGERS])
    seen = echo_gateway.get("/anything").json()["headers"]
    assert seen[ident.email_header.lower()] == "petr.sales@pharma-a.example"
    assert seen[ident.roles_header.lower()] == SALES_MANAGERS


def test_client_sent_kbc_headers_are_stripped(echo_gateway, runtime):
    ident = runtime.settings.identity
    _login(echo_gateway, runtime.settings, "petr.sales@pharma-a.example", [SALES_MANAGERS])
    seen = echo_gateway.get(
        "/",
        headers={
            ident.email_header: "lucie.finance@pharma-a.example",
            ident.roles_header: FINANCE,
            "X-Kbc-Anything-Else": "x",
        },
    ).json()["headers"]
    assert seen[ident.email_header.lower()] == "petr.sales@pharma-a.example"
    assert seen[ident.roles_header.lower()] == SALES_MANAGERS
    assert "x-kbc-anything-else" not in seen


def test_forwarded_headers_pass_through_like_the_real_proxy(echo_gateway, runtime):
    _login(echo_gateway, runtime.settings, "petr.sales@pharma-a.example", [SALES_MANAGERS])
    seen = echo_gateway.get("/", headers={"X-Forwarded-Email": "lucie.finance@pharma-a.example"}).json()["headers"]
    assert seen["x-forwarded-email"] == "lucie.finance@pharma-a.example"


def test_spa_csrf_header_survives_the_gateway(echo_gateway, runtime):
    # The admin SPA's CSRF header must not use the stripped `x-kbc-*` prefix,
    # or every mutation would arrive without it (seen live: "Missing CSRF header.").
    _login(echo_gateway, runtime.settings, "petr.sales@pharma-a.example", [SALES_MANAGERS])
    seen = echo_gateway.post("/api/users", headers={CSRF_HEADER: "1"}).json()["headers"]
    assert seen[CSRF_HEADER.lower()] == "1"


def test_session_cookie_is_not_forwarded_but_other_cookies_are(echo_gateway, runtime):
    _login(echo_gateway, runtime.settings, "petr.sales@pharma-a.example", [SALES_MANAGERS])
    echo_gateway.cookies.set("app_pref", "dark")
    seen = echo_gateway.get("/").json()["headers"]
    assert runtime.settings.gateway.session_cookie not in seen.get("cookie", "")
    assert "app_pref=dark" in seen.get("cookie", "")


def test_forged_session_cookie_is_rejected(echo_gateway, runtime):
    echo_gateway.cookies.set(runtime.settings.gateway.session_cookie, "eyJlbWFpbCI6ImV2aWxAeC5jb20ifQ.forged")
    assert echo_gateway.get("/").status_code == 303


def test_post_body_is_forwarded(echo_gateway, runtime):
    _login(echo_gateway, runtime.settings, "petr.sales@pharma-a.example", [SALES_MANAGERS])
    assert echo_gateway.post("/query", data={"sql": "select 1"}).json()["body"] == "sql=select+1"


@pytest.mark.parametrize(
    ("target", "expected"),
    [("/", "/"), ("/query?x=1", "/query?x=1"), ("https://evil.example/", "/"), ("//evil.example", "/"), ("query", "/"), (None, "/")],
)
def test_next_is_never_an_open_redirect(target, expected):
    assert _safe_next(target) == expected


def test_logout_clears_session(echo_gateway, runtime):
    _login(echo_gateway, runtime.settings, "petr.sales@pharma-a.example", [SALES_MANAGERS])
    echo_gateway.get(runtime.settings.gateway.logout_path)
    assert echo_gateway.get("/").status_code == 303


def test_unreachable_upstream_is_a_clean_502(runtime):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    gateway = TestClient(create_gateway(runtime.settings, "sales", transport=httpx.MockTransport(refuse)), follow_redirects=False)
    _login(gateway, runtime.settings, "petr.sales@pharma-a.example", [SALES_MANAGERS])
    response = gateway.get("/")
    assert response.status_code == 502


# ---- launcher behind the gateway -------------------------------------------------


@pytest.fixture
def launcher_via_gateway(runtime):
    upstream = httpx.ASGITransport(app=create_launcher(runtime))
    return TestClient(create_gateway(runtime.settings, "launcher", transport=upstream), follow_redirects=False)


def test_launcher_lists_only_granted_apps(launcher_via_gateway, runtime):
    _login(launcher_via_gateway, runtime.settings, "petr.sales@pharma-a.example", [SALES_MANAGERS])
    page = launcher_via_gateway.get("/").text
    assert "Drug sales dashboard" in page
    assert "Supply chain dashboard" not in page
    assert "Access admin" not in page.split("<main>")[1]


def test_launcher_for_finance_lists_both_dashboards(launcher_via_gateway, runtime):
    _login(launcher_via_gateway, runtime.settings, "lucie.finance@pharma-a.example", [FINANCE])
    page = launcher_via_gateway.get("/").text
    assert "Drug sales dashboard" in page and "Supply chain dashboard" in page


def test_launcher_with_no_grants_says_so(launcher_via_gateway, runtime):
    _login(launcher_via_gateway, runtime.settings, "nobody@elsewhere.example", ["not-a-known-group"])
    page = launcher_via_gateway.get("/").text
    assert "don't have access to any app" in page
    assert "unknown to access admin" in page
    assert "not in the users table" in page


def test_launcher_flags_deactivated_user(launcher_via_gateway, runtime):
    _login(launcher_via_gateway, runtime.settings, "olga.left@pharma-a.example", [SALES_MANAGERS])
    page = launcher_via_gateway.get("/").text
    assert "deactivated" in page
    assert "Drug sales dashboard" not in page.split("<main>")[1]


def test_root_without_proxy_headers_answers_the_probe_with_the_sign_in_page(runtime):
    # The Keboola startup probe calls GET / without identity headers and needs a 2xx.
    response = TestClient(create_launcher(runtime)).get("/")
    assert response.status_code == 200
    assert "Not signed in" in response.text and "Your apps" not in response.text


def test_other_paths_without_proxy_headers_are_401(runtime):
    assert TestClient(create_launcher(runtime)).get("/anything").status_code in (401, 404)


def test_hostile_header_through_gateway_is_audited(launcher_via_gateway, runtime):
    _login(launcher_via_gateway, runtime.settings, "petr.sales@pharma-a.example", [SALES_MANAGERS])
    launcher_via_gateway.get("/", headers={"X-Forwarded-Email": "lucie.finance@pharma-a.example"})
    lines = [json.loads(line) for line in runtime.settings.path(runtime.settings.audit.path).read_text().splitlines()]
    hostile = [e for e in lines if e.get("hostile_headers")]
    assert hostile and hostile[-1]["email"] == "petr.sales@pharma-a.example"
    assert hostile[-1]["hostile_headers"] == ["x-forwarded-email"]


def test_health_check_post_root(runtime):
    assert TestClient(create_launcher(runtime)).post("/").json() == {"status": "ok"}


def test_launcher_lists_the_users_local_groups(launcher_via_gateway, runtime):
    _login(launcher_via_gateway, runtime.settings, "petr.sales@pharma-a.example", [SALES_MANAGERS])
    page = launcher_via_gateway.get("/").text
    assert "Local groups" in page and "emea-sales" in page and "EMEA sales" in page


def test_launcher_shows_no_local_groups_for_non_members(launcher_via_gateway, runtime):
    _login(launcher_via_gateway, runtime.settings, "tomas.nogrant@pharma-a.example", [])
    page = launcher_via_gateway.get("/").text.split("Local groups")[1]
    assert "emea-sales" not in page and "none" in page


def test_launcher_lists_apps_granted_through_a_local_group(launcher_via_gateway, runtime):
    runtime.store.grant("supply", "local_group", "emea-sales", "test")
    _login(launcher_via_gateway, runtime.settings, "petr.sales@pharma-a.example", [SALES_MANAGERS])
    runtime.gate._snapshot = None  # drop the cached ACL snapshot instead of waiting out the TTL
    assert "Supply chain dashboard" in launcher_via_gateway.get("/").text
