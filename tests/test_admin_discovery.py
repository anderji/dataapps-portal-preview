"""Admin app discovery API: scan this project, classify, import with edited name and app ID."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.admin.main import create_app, suggested_app_id
from kbc_access.store import App
from kbc_access.web import CSRF_HEADER
from tests.test_discovery import APPS, COMPONENT, DS, ISSUER, STACK, TOKEN, transport

ADMIN = "anna.admin@pharma-a.example"
LAUNCHER_URL = "https://app-launcher-10000002.hub.europe-west3.gcp.keboola.com/"


def _api(apps: list[dict]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{DS}/apps":
            return httpx.Response(200, json=apps)
        if str(request.url).endswith(f"/components/{COMPONENT}/configs"):
            return httpx.Response(403, json={"error": "no access"})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


@pytest.fixture
def deployed_runtime(runtime):
    # The launcher's public URL as it would be in a deployed config.
    apps = dict(runtime.settings.apps)
    apps["launcher"] = apps["launcher"].model_copy(update={"public_port": None, "public_url": LAUNCHER_URL})
    return type(runtime)(**{**runtime.__dict__, "settings": runtime.settings.model_copy(update={"apps": apps})})


@pytest.fixture
def client_for(frontend_dist):
    def make(runtime, apps: list[dict] = APPS, transport: httpx.BaseTransport | None = None) -> TestClient:
        app = create_app(
            runtime, discovery_transport=transport or _api(apps), discovery_source=(STACK, TOKEN),
            frontend_dist=frontend_dist,
        )
        return TestClient(app, headers={CSRF_HEADER: "1"})

    return make


@pytest.fixture
def admin(headers_for) -> dict[str, str]:
    return headers_for(ADMIN)


def _app_ids(runtime) -> set[str]:
    return {a.app_id for a in runtime.store.list_apps()}


def test_local_admin_says_discovery_needs_keboola(runtime, admin, frontend_dist):
    client = TestClient(create_app(runtime, frontend_dist=frontend_dist), headers={CSRF_HEADER: "1"})
    assert client.get("/api/discovery", headers=admin).json() == {"available": False}
    for path, body in (("/api/discovery/scan", None), ("/api/discovery/import", {"items": [{"ds_app_id": "1"}]})):
        response = client.post(path, json=body, headers=admin)
        assert response.status_code == 400, path
        assert "only when the admin app runs in Keboola" in response.json()["detail"]


def test_deployed_admin_says_discovery_is_available(deployed_runtime, admin, client_for):
    assert client_for(deployed_runtime).get("/api/discovery", headers=admin).json() == {"available": True}


def test_scan_classifies_new_registered_launcher_and_undeployed(deployed_runtime, admin, client_for):
    deployed_runtime.store.upsert_app(App("admin-live", "Access admin", APPS[0]["url"] + "/", "1234", "c-admin", "manual", True))
    response = client_for(deployed_runtime).post("/api/discovery/scan", headers=admin)
    assert response.status_code == 200
    results = response.json()["results"]
    statuses = {r["status"] for r in results}
    assert {"registered", "launcher", "not_deployed"} <= statuses
    registered = next(r for r in results if r["status"] == "registered")  # matched by config id
    assert registered["local_id"] == "admin-live"
    assert set(results[0]) == {
        "ds_app_id", "config_id", "project_id", "name", "name_source", "slug", "url", "state", "status", "local_id",
        "auth_mode", "auth_providers",
    }
    assert {r["auth_mode"] for r in results} == {"unknown"}  # this token cannot read the configs
    assert TOKEN not in response.text
    events = [json.loads(l) for l in deployed_runtime.settings.path(deployed_runtime.settings.audit.path).read_text().splitlines()]
    assert any(e.get("action") == "discovery_run" for e in events)


def test_scan_reports_each_apps_login_when_configs_are_readable(deployed_runtime, admin, client_for):
    client = client_for(deployed_runtime, transport=transport(configs_status=200))
    results = {r["ds_app_id"]: r for r in client.post("/api/discovery/scan", headers=admin).json()["results"]}
    assert results["10000001"]["auth_mode"] == "required"
    assert results["10000001"]["auth_providers"] == [{"id": "oidc-1", "type": "oidc", "issuer_url": ISSUER}]
    assert results["10000099"]["auth_mode"] == "public"


def test_import_uses_the_edited_name_and_id_and_grants_nobody(deployed_runtime, admin, client_for):
    apps = [*APPS, {"id": "10000100", "configId": "c-sales", "projectId": "1234", "state": "running",
                    "url": "https://sales-cockpit-10000100.hub.europe-west3.gcp.keboola.com"}]
    client = client_for(deployed_runtime, apps)
    response = client.post("/api/discovery/import", headers=admin, json={
        "items": [{"ds_app_id": "10000100", "name": "Sales cockpit (CZ)", "local_id": "sales-cockpit"}],
    })
    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["message"] == "Imported 1 app(s): sales-cockpit. Nobody can open them until you grant access."
    app = {a.app_id: a for a in deployed_runtime.store.list_apps()}["sales-cockpit"]
    assert (app.name, app.source, app.config_id, app.project_id) == ("Sales cockpit (CZ)", "discovered", "c-sales", "1234")
    assert app.url == "https://sales-cockpit-10000100.hub.europe-west3.gcp.keboola.com/"
    assert not [g for g in deployed_runtime.store.list_grants() if g.app_id == "sales-cockpit"]
    audit = deployed_runtime.settings.path(deployed_runtime.settings.audit.path).read_text()
    assert any(json.loads(l).get("action") == "app_import" for l in audit.splitlines())


def test_import_falls_back_to_the_scanned_name_and_suggested_id(deployed_runtime, admin, client_for):
    client = client_for(deployed_runtime)
    new = next(r for r in client.post("/api/discovery/scan", headers=admin).json()["results"] if r["status"] == "new")
    body = client.post("/api/discovery/import", headers=admin, json={"items": [{"ds_app_id": new["ds_app_id"]}]}).json()
    assert body["error"] is None and new["local_id"] in body["message"]
    assert {a.app_id: a for a in deployed_runtime.store.list_apps()}[new["local_id"]].name == new["name"]


def test_import_does_not_trust_the_request(deployed_runtime, admin, client_for):
    client = client_for(deployed_runtime)
    # An id that is not in the project, the launcher, and an undeployed app: none may be imported.
    response = client.post("/api/discovery/import", headers=admin, json={
        "items": [{"ds_app_id": "99999999"}, {"ds_app_id": "10000002"}, {"ds_app_id": "10000099"}],
    })
    body = response.json()
    assert response.status_code == 200 and body["message"] is None
    assert body["error"].startswith("Not imported: ") and "99999999: no longer in this project" in body["error"]
    assert _app_ids(deployed_runtime) == {"admin", "sales", "supply"}


def test_import_needs_at_least_one_item(deployed_runtime, admin, client_for):
    response = client_for(deployed_runtime).post("/api/discovery/import", headers=admin, json={"items": []})
    assert response.status_code == 400
    assert response.json()["detail"] == "Select at least one discovered app to import."


def test_import_rejects_an_invalid_app_id(deployed_runtime, admin, client_for):
    client = client_for(deployed_runtime)
    response = client.post("/api/discovery/import", headers=admin, json={
        "items": [{"ds_app_id": "10000001", "local_id": "Bad Id"}],
    })
    body = response.json()
    assert body["message"] is None and "App ID must be lowercase" in body["error"]
    assert "Bad Id" not in _app_ids(deployed_runtime)


def test_scan_failure_is_reported_without_the_token(deployed_runtime, admin, client_for):
    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"boom {TOKEN}")

    client = client_for(deployed_runtime, transport=httpx.MockTransport(fail))
    for path, body in (("/api/discovery/scan", None), ("/api/discovery/import", {"items": [{"ds_app_id": "10000001"}]})):
        response = client.post(path, json=body, headers=admin)
        assert response.status_code == 502, path
        assert response.json()["detail"].startswith("Discovery failed") and TOKEN not in response.text
    assert _app_ids(deployed_runtime) == {"admin", "sales", "supply"}


def test_discovery_is_admin_only(deployed_runtime, headers_for, client_for):
    petr = headers_for("petr.sales@pharma-a.example")
    assert client_for(deployed_runtime).post("/api/discovery/scan", headers=petr).status_code == 403


@pytest.mark.parametrize(("slug", "taken", "expected"), [
    ("access-admin", set(), "access-admin"),
    ("access-admin", {"access-admin"}, "access-admin-42"),
    ("", set(), "app-42"),
    ("Weird_Slug", set(), "weird_slug"),
])
def test_suggested_app_id(slug, taken, expected):
    assert suggested_app_id(slug, "42", taken) == expected
