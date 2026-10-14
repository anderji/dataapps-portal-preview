"""Editing users and groups in the admin app's JSON API (and the store methods behind it)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.admin.main import create_app
from kbc_access.web import CSRF_HEADER
from kbc_access.gate import decide
from kbc_access.identity import Principal
from kbc_access.store import StoreError

ADMIN = "anna.admin@pharma-a.example"
PETR = "petr.sales@pharma-a.example"
SALES_MANAGERS = "8a1d4e2f-5b6c-4d7e-9f80-1a2b3c4d5e6f"
NEW_ENTRA_ID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def client(runtime, frontend_dist) -> TestClient:
    return TestClient(create_app(runtime, frontend_dist=frontend_dist), headers={CSRF_HEADER: "1"})


@pytest.fixture
def admin(headers_for) -> dict[str, str]:
    return headers_for(ADMIN)


def _group(runtime, name: str):
    return next(g for g in runtime.store.list_groups() if g.name == name)


def _allowed(runtime, email: str, groups: set[str], app_id: str) -> bool:
    return decide(runtime.store.snapshot(), Principal(email, frozenset(groups)), app_id).allowed


# ---- store ------------------------------------------------------------------


def test_rename_user_moves_grants_and_attributes(runtime):
    store = runtime.store
    store.grant("supply", "user", PETR, "test")
    saved = store.update_user(PETR, new_email="Petr.New@Pharma-A.example", display_name="Petr N", active=True)
    assert saved == "petr.new@pharma-a.example"
    assert PETR not in {u.email for u in store.list_users()}
    assert store.attributes_for(saved) == {"company": "PHARMA_A"}
    assert store.attributes_for(PETR) == {}
    assert ("supply", "user", saved) in {(g.app_id, g.subject_type, g.subject_id) for g in store.list_grants()}


def test_rename_user_onto_existing_email_is_refused(runtime):
    with pytest.raises(StoreError, match="already exists"):
        runtime.store.update_user(PETR, new_email=ADMIN, display_name="x", active=True)


def test_update_unknown_user_is_refused(runtime):
    with pytest.raises(StoreError, match="Unknown user"):
        runtime.store.update_user("ghost@x.example", new_email="ghost@x.example", display_name="", active=True)


def test_update_group_keeps_grants_and_validates_id(runtime):
    store = runtime.store
    group = _group(runtime, "sales-managers")
    with pytest.raises(StoreError, match="not a valid"):
        store.update_group(group.group_id, provider="entra", external_id="not-a-guid", name="x")
    updated = store.update_group(group.group_id, provider="entra", external_id=NEW_ENTRA_ID, name="Sales managers")
    assert updated.name == "Sales managers"
    # Grants reference the internal group id, so they follow the new external id.
    assert _allowed(runtime, PETR, {NEW_ENTRA_ID}, "sales")
    assert not _allowed(runtime, PETR, {SALES_MANAGERS}, "sales")


def test_update_group_to_duplicate_external_id_is_refused(runtime):
    finance = _group(runtime, "finance")
    with pytest.raises(StoreError, match="already exists"):
        runtime.store.update_group(
            _group(runtime, "sales-managers").group_id, provider="entra", external_id=finance.external_id, name="x"
        )


def test_set_app_grants_is_exact(runtime):
    group = _group(runtime, "sales-managers")
    added, removed = runtime.store.set_app_grants("group", group.group_id, {"supply"}, "test")
    assert (added, removed) == ({"supply"}, {"sales"})
    added, removed = runtime.store.set_app_grants("group", group.group_id, {"supply"}, "test")
    assert (added, removed) == (set(), set())


# ---- admin API ----------------------------------------------------------------


def test_user_list_carries_the_email_to_edit(client, admin):
    assert PETR in {u["email"] for u in client.get("/api/users", headers=admin).json()["users"]}


def test_get_user_shows_identity_attributes_and_apps(client, admin, runtime):
    runtime.store.grant("supply", "user", PETR, "test")
    response = client.get(f"/api/users/{PETR.upper()}", headers=admin)
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == PETR and body["user"]["active"] is True
    assert body["attributes"] == {"company": "PHARMA_A"}
    assert "Drug sales dashboard" in {a["name"] for a in body["apps"]}
    assert body["granted"] == ["supply"]


def test_get_unknown_user_is_404(client, admin):
    response = client.get("/api/users/ghost@x.example", headers=admin)
    assert response.status_code == 404 and response.json()["detail"] == "Unknown user 'ghost@x.example'."


def test_edit_user_saves_name_active_and_direct_apps(client, admin, runtime):
    response = client.put(
        f"/api/users/{PETR}",
        json={"new_email": PETR, "display_name": "Petr Edited", "active": False, "app_ids": ["supply", "no-such-app"]},
        headers=admin,
    )
    assert response.status_code == 200
    assert response.json() == {"message": f"User '{PETR}' saved; 1 app(s) granted, 0 revoked.", "email": PETR}
    user = {u.email: u for u in runtime.store.list_users()}[PETR]
    assert user.display_name == "Petr Edited"
    assert user.active is False
    grants = {(g.app_id, g.subject_id) for g in runtime.store.list_grants() if g.subject_type == "user"}
    assert ("supply", PETR) in grants
    assert ("no-such-app", PETR) not in grants
    # Deactivation overrides the new grant.
    assert not _allowed(runtime, PETR, set(), "supply")


def test_edit_user_rename(client, admin, runtime):
    response = client.put(
        f"/api/users/{PETR}", json={"new_email": "petr2@pharma-a.example", "display_name": "P", "active": True},
        headers=admin,
    )
    assert response.json()["email"] == "petr2@pharma-a.example"
    emails = {u.email for u in runtime.store.list_users()}
    assert "petr2@pharma-a.example" in emails and PETR not in emails
    events = [line for line in runtime.settings.path(runtime.settings.audit.path).read_text().splitlines() if "user_rename" in line]
    assert events


def test_edit_user_invalid_email_is_400_and_changes_nothing(client, admin, runtime):
    response = client.put(f"/api/users/{PETR}", json={"new_email": "nope", "active": True}, headers=admin)
    assert response.status_code == 400 and "nope" in response.json()["detail"]
    assert PETR in {u.email for u in runtime.store.list_users()}


def test_edit_unknown_user_is_404(client, admin):
    response = client.put("/api/users/ghost@x.example", json={"new_email": "ghost@x.example"}, headers=admin)
    assert response.status_code == 404


def test_set_attribute_overwrites(client, admin, runtime):
    response = client.put(f"/api/users/{PETR}/attributes/company", json={"value": "PHARMA_B"}, headers=admin)
    assert response.status_code == 200
    assert runtime.store.attributes_for(PETR) == {"company": "PHARMA_B"}


def test_group_list_carries_the_group_id_to_edit(client, admin, runtime):
    group = _group(runtime, "finance")
    assert group.group_id in {g["group_id"] for g in client.get("/api/groups", headers=admin).json()["groups"]}


def test_get_and_save_group(client, admin, runtime):
    group = _group(runtime, "sales-managers")
    body = client.get(f"/api/groups/{group.group_id}", headers=admin).json()
    assert body["group"]["external_id"] == SALES_MANAGERS
    assert body["granted"] == ["sales"]
    assert set(body["providers"]) == set(runtime.store.providers)
    assert {a["app_id"] for a in body["apps"]} >= {"sales", "supply"}

    response = client.put(
        f"/api/groups/{group.group_id}",
        json={"provider": "entra", "external_id": SALES_MANAGERS, "name": "Sales managers CZ", "app_ids": ["sales", "supply"]},
        headers=admin,
    )
    assert response.json() == {"message": "Group 'Sales managers CZ' saved; 1 app(s) granted, 0 revoked."}
    assert _group(runtime, "Sales managers CZ").group_id == group.group_id
    assert _allowed(runtime, PETR, {SALES_MANAGERS}, "supply")


def test_get_unknown_group_is_404(client, admin):
    response = client.get("/api/groups/no-such-group", headers=admin)
    assert response.status_code == 404 and response.json()["detail"] == "Unknown group 'no-such-group'."
    response = client.put(
        "/api/groups/no-such-group", json={"provider": "entra", "external_id": NEW_ENTRA_ID, "name": "x"}, headers=admin
    )
    assert response.status_code == 404


def test_edit_group_invalid_id_is_400_and_changes_nothing(client, admin, runtime):
    group = _group(runtime, "sales-managers")
    response = client.put(
        f"/api/groups/{group.group_id}",
        json={"provider": "okta", "external_id": "bad", "name": "Typed name"},
        headers=admin,
    )
    assert response.status_code == 400 and "not a valid" in response.json()["detail"]
    # Nothing changed in the store (the SPA keeps what the admin typed in its own form state).
    assert _group(runtime, "sales-managers").external_id == SALES_MANAGERS
    assert _group(runtime, "sales-managers").name == "sales-managers"


def test_edit_endpoints_are_admin_only(client, headers_for, runtime):
    petr = headers_for(PETR)
    assert client.get(f"/api/users/{PETR}", headers=petr).status_code == 403
    group = _group(runtime, "finance")
    assert client.put(
        f"/api/groups/{group.group_id}",
        json={"provider": "entra", "external_id": group.external_id, "name": "hijack"},
        headers=petr,
    ).status_code == 403
    assert _group(runtime, "finance").name == "finance"
