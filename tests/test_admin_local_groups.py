"""Local groups: admin JSON API, the request principal built by `install`, and audit lines."""

from __future__ import annotations

import json

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from apps.admin.main import create_app
from kbc_access.identity import Principal
from kbc_access.web import CSRF_HEADER, install

ADMIN = "anna.admin@pharma-a.example"
NOGRANT = "tomas.nogrant@pharma-a.example"
PETR = "petr.sales@pharma-a.example"
LUCIE = "lucie.finance@pharma-a.example"


@pytest.fixture
def client(runtime, frontend_dist) -> TestClient:
    return TestClient(create_app(runtime, frontend_dist=frontend_dist), headers={CSRF_HEADER: "1"})


@pytest.fixture
def admin(headers_for) -> dict[str, str]:
    return headers_for(ADMIN)


def _admin_events(runtime) -> list[tuple[str, str]]:
    path = runtime.settings.path(runtime.settings.audit.path)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [(e["action"], e["target"]) for e in lines if e["event"] == "admin"]


# ---- list / get ------------------------------------------------------------


def test_list_returns_seeded_group_with_members(client, admin):
    response = client.get("/api/local-groups", headers=admin)
    assert response.status_code == 200
    assert response.json() == [
        {
            "local_group_id": "emea-sales",
            "name": "EMEA sales",
            "description": "Sales and finance people covering the EMEA region",
            "members": [LUCIE, PETR],
            "member_count": 2,
        }
    ]


def test_get_one_group_with_members_and_grants(client, admin, runtime):
    runtime.store.grant("supply", "local_group", "emea-sales", "test")
    body = client.get("/api/local-groups/emea-sales", headers=admin).json()
    assert body["members"] == [LUCIE, PETR] and body["member_count"] == 2
    assert body["granted"] == ["supply"]


def test_get_unknown_group_is_404(client, admin):
    assert client.get("/api/local-groups/nope", headers=admin).status_code == 404


def test_every_route_is_admin_only(client, headers_for):
    other = headers_for(NOGRANT)
    assert client.get("/api/local-groups", headers=other).status_code == 403
    assert client.post("/api/local-groups", headers=other, json={"local_group_id": "x", "name": "X"}).status_code == 403
    assert client.put(f"/api/local-groups/emea-sales/members/{NOGRANT}", headers=other).status_code == 403
    assert client.delete("/api/local-groups/emea-sales", headers=other).status_code == 403


def test_mutations_need_the_csrf_header(runtime, frontend_dist, admin):
    bare = TestClient(create_app(runtime, frontend_dist=frontend_dist))
    response = bare.post("/api/local-groups", headers=admin, json={"local_group_id": "x", "name": "X"})
    assert response.status_code == 403 and response.json()["reason"] == "csrf"
    assert "x" not in {g.local_group_id for g in runtime.store.list_local_groups()}


# ---- create / edit / delete --------------------------------------------------


def test_create_returns_201_and_audits(client, admin, runtime):
    response = client.post(
        "/api/local-groups", headers=admin, json={"local_group_id": "new-team", "name": "New team", "description": "d"}
    )
    assert response.status_code == 201
    assert response.json() == {
        "local_group_id": "new-team", "name": "New team", "description": "d", "members": [], "member_count": 0,
    }
    assert ("local_group_create", "new-team") in _admin_events(runtime)


def test_create_existing_is_409(client, admin, runtime):
    response = client.post("/api/local-groups", headers=admin, json={"local_group_id": "emea-sales", "name": "Dup"})
    assert response.status_code == 409
    assert runtime.store.list_local_groups()[0].name == "EMEA sales"


@pytest.mark.parametrize("slug", ["", "Bad Slug", "UPPER", "a/b", "x" * 65])
def test_create_with_bad_slug_is_422(client, admin, slug):
    response = client.post("/api/local-groups", headers=admin, json={"local_group_id": slug, "name": "X"})
    assert response.status_code == 422
    assert "Local group ID" in response.json()["detail"]


def test_create_without_name_is_422(client, admin):
    response = client.post("/api/local-groups", headers=admin, json={"local_group_id": "ok", "name": " "})
    assert response.status_code == 422


def test_edit_renames_and_audits(client, admin, runtime):
    response = client.put("/api/local-groups/emea-sales", headers=admin, json={"name": "EMEA", "description": ""})
    assert response.status_code == 200
    assert response.json()["name"] == "EMEA" and response.json()["member_count"] == 2
    assert ("local_group_update", "emea-sales") in _admin_events(runtime)


def test_edit_unknown_is_404(client, admin):
    assert client.put("/api/local-groups/nope", headers=admin, json={"name": "X"}).status_code == 404


def test_delete_removes_memberships_and_grants(client, admin, runtime):
    runtime.store.grant("supply", "local_group", "emea-sales", "test")
    response = client.delete("/api/local-groups/emea-sales", headers=admin)
    assert response.status_code == 200
    assert runtime.store.list_local_groups() == []
    assert runtime.store.list_local_group_members() == {}
    assert not [g for g in runtime.store.list_grants() if g.subject_type == "local_group"]
    assert ("local_group_delete", "emea-sales") in _admin_events(runtime)


def test_delete_unknown_is_404(client, admin):
    assert client.delete("/api/local-groups/nope", headers=admin).status_code == 404


# ---- members ---------------------------------------------------------------


def test_add_member_is_idempotent_and_audited(client, admin, runtime):
    url = f"/api/local-groups/emea-sales/members/{NOGRANT.upper()}"
    assert client.put(url, headers=admin).status_code == 200
    assert client.put(url, headers=admin).status_code == 200
    assert runtime.store.list_local_group_members()["emea-sales"].count(NOGRANT) == 1
    assert ("local_group_member_add", f"emea-sales:{NOGRANT}") in _admin_events(runtime)


def test_add_unregistered_member_is_422(client, admin, runtime):
    response = client.put("/api/local-groups/emea-sales/members/ghost@nowhere.example", headers=admin)
    assert response.status_code == 422
    assert response.json()["detail"] == "user must be registered first"
    assert "ghost@nowhere.example" not in runtime.store.list_local_group_members()["emea-sales"]


def test_add_member_to_unknown_group_is_404(client, admin):
    assert client.put(f"/api/local-groups/nope/members/{PETR}", headers=admin).status_code == 404


def test_remove_member(client, admin, runtime):
    response = client.delete(f"/api/local-groups/emea-sales/members/{PETR}", headers=admin)
    assert response.status_code == 200
    assert runtime.store.list_local_group_members()["emea-sales"] == [LUCIE]
    assert ("local_group_member_remove", f"emea-sales:{PETR}") in _admin_events(runtime)


def test_remove_member_from_unknown_group_is_404(client, admin):
    assert client.delete(f"/api/local-groups/nope/members/{PETR}", headers=admin).status_code == 404


# ---- grants ------------------------------------------------------------------


def test_grant_and_revoke_with_local_group_subject(client, admin, runtime):
    body = {"app_id": "supply", "subject_type": "local_group", "subject_id": "emea-sales"}
    assert client.post("/api/grants", headers=admin, json=body).status_code == 200
    assert ("supply", "local_group", "emea-sales") in {
        (g.app_id, g.subject_type, g.subject_id) for g in runtime.store.list_grants()
    }
    assert ("grant", "supply:local_group:emea-sales") in _admin_events(runtime)
    assert client.post("/api/grants/revoke", headers=admin, json=body).status_code == 200
    assert not [g for g in runtime.store.list_grants() if g.subject_type == "local_group"]
    assert ("revoke", "supply:local_group:emea-sales") in _admin_events(runtime)


def test_grant_to_unknown_local_group_is_400(client, admin):
    body = {"app_id": "supply", "subject_type": "local_group", "subject_id": "nope"}
    assert client.post("/api/grants", headers=admin, json=body).status_code == 400


def test_grants_listing_includes_local_groups(client, admin):
    body = client.get("/api/grants", headers=admin).json()
    assert [g["local_group_id"] for g in body["local_groups"]] == ["emea-sales"]


# ---- user detail, overview, simulator ----------------------------------------


def test_user_detail_lists_local_groups(client, admin):
    body = client.get(f"/api/users/{PETR}", headers=admin).json()
    assert body["local_groups"] == [{"local_group_id": "emea-sales", "name": "EMEA sales"}]
    assert client.get(f"/api/users/{NOGRANT}", headers=admin).json()["local_groups"] == []


def test_overview_counts_and_matrix_include_local_groups(client, admin, runtime):
    runtime.store.grant("supply", "local_group", "emea-sales", "test")
    body = client.get("/api/overview", headers=admin).json()
    assert body["counts"]["local_groups"] == 1
    assert [g["local_group_id"] for g in body["local_groups"]] == ["emea-sales"]
    assert body["local_group_grants"] == {"emea-sales": ["supply"]}
    cell = body["user_matrix"][PETR]["supply"]
    assert cell["allowed"] and cell["matched"] == ["local_group:emea-sales"]
    assert not body["user_matrix"][NOGRANT]["supply"]["allowed"]


def test_simulate_resolves_local_groups_from_the_store(client, admin, runtime):
    runtime.store.grant("supply", "local_group", "emea-sales", "test")
    body = client.post("/api/simulate", headers=admin, json={"email": PETR.upper()}).json()
    assert body["local_groups"] == [{"local_group_id": "emea-sales", "name": "EMEA sales"}]
    supply = next(r for r in body["results"] if r["app"]["app_id"] == "supply")
    assert supply["decision"]["allowed"] and supply["decision"]["matched"] == ["local_group:emea-sales"]
    other = client.post("/api/simulate", headers=admin, json={"email": NOGRANT}).json()
    assert other["local_groups"] == []
    assert not next(r for r in other["results"] if r["app"]["app_id"] == "supply")["decision"]["allowed"]


# ---- the request principal -----------------------------------------------------


def test_request_principal_carries_resolved_local_groups(runtime, headers_for):
    app = FastAPI()
    access = install(app, runtime, app_id="sales", templates_dir=None)

    @app.get("/who")
    def who(principal: Principal = Depends(access.require_with_attributes)) -> dict[str, object]:
        return {"local_groups": sorted(principal.local_groups), "attributes": dict(principal.attributes)}

    @app.get("/who-identify")
    def who_identify(principal: Principal = Depends(access.identify)) -> dict[str, object]:
        return {"local_groups": sorted(principal.local_groups)}

    client = TestClient(app)
    assert client.get("/who", headers=headers_for(PETR)).json() == {
        "local_groups": ["emea-sales"], "attributes": {"company": "PHARMA_A"},
    }
    assert client.get("/who-identify", headers=headers_for(LUCIE)).json() == {"local_groups": ["emea-sales"]}
    assert client.get("/who-identify", headers=headers_for(NOGRANT)).json() == {"local_groups": []}


def test_membership_header_cannot_forge_a_local_group(runtime, headers_for):
    app = FastAPI()
    access = install(app, runtime, app_id="sales", templates_dir=None)

    @app.get("/who")
    def who(principal: Principal = Depends(access.identify)) -> dict[str, object]:
        return {"local_groups": sorted(principal.local_groups)}

    forged = {**headers_for(NOGRANT), "x-kbc-local-groups": "emea-sales"}
    assert TestClient(app).get("/who", headers=forged).json() == {"local_groups": []}


def test_gate_allows_local_group_member_through_require(runtime, headers_for):
    runtime.store.grant("supply", "local_group", "emea-sales", "test")
    app = FastAPI()
    access = install(app, runtime, app_id="supply", templates_dir=None)

    @app.get("/x")
    def x(principal: Principal = Depends(access.require)) -> dict[str, str]:
        return {"email": principal.email}

    client = TestClient(app)
    runtime.gate._snapshot = None
    assert client.get("/x", headers=headers_for(PETR)).status_code == 200
    assert client.get("/x", headers=headers_for(NOGRANT)).status_code == 403
