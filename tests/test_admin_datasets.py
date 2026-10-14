"""Admin dataset security API (apps/admin/datasets.py): catalogue, editor, examples, values and "Test as".

Every route is exercised in both modes the admin can run in:

* with the warehouse (`[data]` configured, the shared read-only demo DuckDB): counts,
  value suggestions, zero-row warnings and the preview work;
* without it (`settings.data` is None): everything works from the policy catalogue,
  `data_available` is false, `/values` answers 503 and `/test` still explains every
  line and column, without counts or preview.

The seeded store already holds the pharma example lines (row_access_rules and
column_masks loaded from config/policies/pharma.yaml).
"""

from __future__ import annotations

import dataclasses
import json
import re

import pytest
from fastapi.testclient import TestClient

from apps.admin.datasets import PREVIEW_ROW_LIMIT, VALUES_LIMIT, WARNING_VALUES_LIMIT
from apps.admin.main import create_app
from kbc_access.backends.duckdb_backend import DuckDBBackend
from kbc_access.gate import Gate
from kbc_access.identity import Principal
from kbc_access.security_model import (
    CLEAR,
    HIDDEN,
    KIND_HIDE,
    KIND_SHOW,
    MASK_HASH,
    MASK_PARTIAL,
    SUBJECT_EVERYONE,
    SUBJECT_GROUP,
    SUBJECT_LOCAL_GROUP,
    SUBJECT_USER,
)
from kbc_access.store import SECURITY_ID
from kbc_access.web import CSRF_HEADER, Runtime, build_guard

ADMIN = "anna.admin@pharma-a.example"
NOGRANT = "tomas.nogrant@pharma-a.example"
PETR = "petr.sales@pharma-a.example"
LUCIE = "lucie.finance@pharma-a.example"
NOATTR = "martin.noattr@pharma-c.example"
EMEA_SALES = "emea-sales"
FINANCE_EXTERNAL = "3f2b8c1e-7d4a-4e5b-9a61-2c8d0f4e1b7a"
DRUG_SALES = "drug_sales"
SUPPLY_CHAIN = "supply_chain"
DATASET_NAMES = ["drug_sales", "stores", "supply_chain"]
COMPANIES = ["PHARMA_A", "PHARMA_B", "PHARMA_C"]
# The guarded app used to compare "Test as" counts with what the dashboards enforce.
GUARDED_APP = "sales"
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
PARTIAL_PREFIX = "****"
WITH_DATA = "with_data"
WITHOUT_DATA = "without_data"
EVERYONE = {"type": SUBJECT_EVERYONE, "id": ""}
ALL_ROWS = {"all": True}

# (method, path, json body) of every dataset route.
ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    ("GET", "/api/datasets", None),
    ("GET", f"/api/datasets/{DRUG_SALES}", None),
    ("PUT", f"/api/datasets/{DRUG_SALES}", {"rows": [], "masks": {}}),
    ("POST", f"/api/datasets/{DRUG_SALES}/load-examples", None),
    ("GET", f"/api/datasets/{DRUG_SALES}/values/company", None),
    ("POST", f"/api/datasets/{DRUG_SALES}/test", {"email": PETR}),
]
MUTATING_ROUTES = [r for r in ROUTES if r[0] != "GET"]


# ---- fixtures and helpers ----------------------------------------------------------------


def _without_data(runtime: Runtime) -> Runtime:
    return dataclasses.replace(runtime, settings=runtime.settings.model_copy(update={"data": None}))


@pytest.fixture
def client(runtime, frontend_dist) -> TestClient:
    return TestClient(create_app(runtime, frontend_dist=frontend_dist), headers={CSRF_HEADER: "1"})


@pytest.fixture
def nodata_runtime(runtime) -> Runtime:
    return _without_data(runtime)


@pytest.fixture
def nodata_client(nodata_runtime, frontend_dist) -> TestClient:
    return TestClient(create_app(nodata_runtime, frontend_dist=frontend_dist), headers={CSRF_HEADER: "1"})


@pytest.fixture(params=[WITH_DATA, WITHOUT_DATA])
def mode(request) -> str:
    return request.param


@pytest.fixture
def any_client(mode, runtime, frontend_dist) -> TestClient:
    """The admin in either mode; the store, audit log and settings otherwise identical."""
    chosen = runtime if mode == WITH_DATA else _without_data(runtime)
    return TestClient(create_app(chosen, frontend_dist=frontend_dist), headers={CSRF_HEADER: "1"})


@pytest.fixture
def admin(headers_for) -> dict[str, str]:
    return headers_for(ADMIN)


@pytest.fixture
def warehouse(runtime) -> DuckDBBackend:
    """An independent read-only connection to the demo warehouse, for expected counts."""
    data = runtime.settings.data
    assert data is not None
    return DuckDBBackend(runtime.settings.path(data.duckdb_path), data.max_result_rows)


def _count(warehouse: DuckDBBackend, sql: str) -> int:
    return int(warehouse.execute(sql).rows[0][0])


def _admin_events(runtime: Runtime) -> list[tuple[str, str]]:
    path = runtime.settings.path(runtime.settings.audit.path)
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [(e["action"], e["target"]) for e in lines if e["event"] == "admin"]


def _group_id(runtime: Runtime, external_id: str) -> str:
    return next(g.group_id for g in runtime.store.list_groups() if g.external_id == external_id)


def _security_state(runtime: Runtime, dataset: str) -> tuple[object, object]:
    security = runtime.store.dataset_security(dataset)
    return security.rows, security.masks


def _show(subject: dict[str, str], row_filter: dict[str, object], rule_id: str | None = None) -> dict[str, object]:
    line: dict[str, object] = {"kind": KIND_SHOW, "subject": subject, "filter": row_filter}
    if rule_id is not None:
        line["rule_id"] = rule_id
    return line


def _hide(subject: dict[str, str], row_filter: dict[str, object]) -> dict[str, object]:
    return {"kind": KIND_HIDE, "subject": subject, "filter": row_filter}


def _is(column: str, value: str) -> dict[str, object]:
    return {"column": column, "op": "is", "value": value}


def _where(*conditions: dict[str, object]) -> dict[str, object]:
    return {"conditions": list(conditions)}


def _put(client: TestClient, headers: dict[str, str], dataset: str, rows: list, masks: dict | None = None):
    return client.put(f"/api/datasets/{dataset}", headers=headers, json={"rows": rows, "masks": masks or {}})


def _test(client: TestClient, headers: dict[str, str], dataset: str, email: str, groups: list[str] | None = None):
    body: dict[str, object] = {"email": email}
    if groups is not None:
        body["groups"] = groups
    response = client.post(f"/api/datasets/{dataset}/test", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _guard_visible(runtime: Runtime, principal: Principal, dataset: str) -> int | None:
    """Rows the dashboards' data guard lets `principal` see (None when the dataset is invisible)."""
    fresh = dataclasses.replace(runtime, gate=Gate(runtime.store, 0))
    guard = build_guard(fresh, GUARDED_APP)
    principal = principal.with_attributes(runtime.store.attributes_for(principal.email))
    compiled = guard.secured(principal).get(dataset)
    if compiled is None:
        return None
    data = runtime.settings.data
    assert data is not None
    backend = DuckDBBackend(runtime.settings.path(data.duckdb_path), data.max_result_rows)
    relation = compiled.relation.sql(dialect=backend.dialect)
    return _count(backend, f"SELECT COUNT(*) FROM ({relation}) AS _g")  # noqa: S608 - compiled by the library


def _strip(line: dict) -> dict[str, object]:
    """A line from a GET answer as a PUT body line (no subject label)."""
    return {k: line[k] for k in ("rule_id", "kind", "filter")} | {
        "subject": {"type": line["subject"]["type"], "id": line["subject"]["id"]}
    }


def _lines_by_id(body: dict) -> dict[str, dict]:
    return {line["rule_id"]: line for line in body["rows"]["lines"]}


def _columns_by_name(body: dict) -> dict[str, dict]:
    return {column["name"]: column for column in body["columns"]}


# ---- access control ------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_route_is_admin_only(any_client, headers_for, runtime, method, path, body):
    before = _security_state(runtime, DRUG_SALES)
    response = any_client.request(method, path, headers=headers_for(NOGRANT), json=body)
    assert response.status_code == 403
    assert _security_state(runtime, DRUG_SALES) == before
    assert not [e for e in _admin_events(runtime) if e[0].startswith("dataset_security")]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_route_needs_identity(any_client, method, path, body):
    response = any_client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.json()["reason"] == "not_authenticated"


@pytest.mark.parametrize(("method", "path", "body"), MUTATING_ROUTES)
def test_mutations_need_the_csrf_header(mode, runtime, frontend_dist, admin, method, path, body):
    chosen = runtime if mode == WITH_DATA else _without_data(runtime)
    bare = TestClient(create_app(chosen, frontend_dist=frontend_dist))
    before = _security_state(runtime, DRUG_SALES)
    response = bare.request(method, path, headers=admin, json=body)
    assert response.status_code == 403 and response.json()["reason"] == "csrf"
    assert _security_state(runtime, DRUG_SALES) == before
    assert not [e for e in _admin_events(runtime) if e[0].startswith("dataset_security")]


@pytest.mark.parametrize(
    "path",
    [
        "/api/data-catalog",
        "/api/data-policies",
        "/api/data-policy-assignments/user/x",
        f"/api/users/{PETR}/data-access",
    ],
)
def test_removed_data_policy_routes_are_gone(any_client, admin, path):
    assert any_client.get(path, headers=admin).status_code == 404


# ---- GET /datasets --------------------------------------------------------------------------


def test_catalogue_lists_datasets_with_kinds_and_line_counts(any_client, admin, mode):
    response = any_client.get("/api/datasets", headers=admin)
    assert response.status_code == 200
    body = response.json()
    assert body["data_available"] is (mode == WITH_DATA)
    by_name = {d["name"]: d for d in body["datasets"]}
    assert sorted(by_name) == DATASET_NAMES

    sales = by_name[DRUG_SALES]
    assert sales["description"] == "Sales of drugs per store and day, one row per sale."
    kinds = {c["name"]: c["type"] for c in sales["columns"]}
    assert [c["name"] for c in sales["columns"]][:3] == ["sale_id", "sale_date", "company"]
    assert kinds == {
        "sale_id": "numeric", "sale_date": "date", "company": "text", "store_id": "text", "drug": "text",
        "category": "text", "units": "numeric", "revenue": "numeric", "margin": "numeric",
        "customer_id": "text", "visibility": "text", "is_test": "boolean",
    }
    assert (sales["show_lines"], sales["hide_lines"]) == (3, 1)
    assert sales["masked_columns"] == ["customer_id", "margin"]
    assert (by_name["stores"]["show_lines"], by_name["stores"]["hide_lines"]) == (3, 0)
    assert by_name["stores"]["masked_columns"] == ["manager_email"]
    assert (by_name[SUPPLY_CHAIN]["show_lines"], by_name[SUPPLY_CHAIN]["hide_lines"]) == (4, 0)
    assert by_name[SUPPLY_CHAIN]["masked_columns"] == ["contract_ref", "unit_cost"]
    assert all(d["has_examples"] for d in body["datasets"])


def test_catalogue_counts_follow_the_store(any_client, admin):
    assert _put(any_client, admin, DRUG_SALES, [_hide(EVERYONE, ALL_ROWS)]).status_code == 200
    sales = next(d for d in any_client.get("/api/datasets", headers=admin).json()["datasets"] if d["name"] == DRUG_SALES)
    assert (sales["show_lines"], sales["hide_lines"], sales["masked_columns"]) == (0, 1, [])


# ---- GET /datasets/{name} ---------------------------------------------------------------------


def test_detail_lists_lines_with_subject_labels_masks_and_attributes(any_client, admin, runtime, mode):
    finance = _group_id(runtime, FINANCE_EXTERNAL)
    response = any_client.get(f"/api/datasets/{DRUG_SALES}", headers=admin)
    assert response.status_code == 200
    body = response.json()
    assert body["dataset"]["name"] == DRUG_SALES
    assert {c["name"]: c["type"] for c in body["dataset"]["columns"]}["is_test"] == "boolean"
    assert body["has_examples"] is True
    assert body["data_available"] is (mode == WITH_DATA)
    assert body["attributes"] == ["company"]

    rows = body["rows"]
    assert [r["kind"] for r in rows] == [KIND_SHOW, KIND_SHOW, KIND_SHOW, KIND_HIDE]
    assert all(SECURITY_ID.fullmatch(r["rule_id"]) for r in rows)
    assert rows[0]["subject"] == {"type": SUBJECT_EVERYONE, "id": "", "label": "Everyone"}
    assert rows[0]["filter"] == _where(_is("visibility", "public"))
    assert rows[1]["filter"] == _where({"column": "company", "op": "is", "source": "attribute:company"})
    assert rows[2]["subject"] == {"type": SUBJECT_GROUP, "id": finance, "label": "finance"}
    assert rows[2]["filter"] == ALL_ROWS
    assert rows[3]["filter"] == _where(_is("is_test", "true"))

    assert sorted(body["masks"]) == ["customer_id", "margin"]
    assert [(m["subject"]["label"], m["treatment"]) for m in body["masks"]["margin"]] == [
        ("finance", CLEAR), ("Everyone", HIDDEN),
    ]
    assert [(m["subject"]["label"], m["treatment"]) for m in body["masks"]["customer_id"]] == [
        ("finance", MASK_HASH), ("Everyone", MASK_PARTIAL),
    ]
    assert all(SECURITY_ID.fullmatch(m["mask_id"]) for ms in body["masks"].values() for m in ms)


def test_detail_labels_users_and_local_groups(any_client, admin):
    rows = [
        _show({"type": SUBJECT_USER, "id": PETR}, ALL_ROWS),
        _show({"type": SUBJECT_LOCAL_GROUP, "id": EMEA_SALES}, ALL_ROWS),
    ]
    masks = {"margin": [{"subject": {"type": SUBJECT_USER, "id": LUCIE}, "treatment": CLEAR}]}
    assert _put(any_client, admin, DRUG_SALES, rows, masks).status_code == 200
    body = any_client.get(f"/api/datasets/{DRUG_SALES}", headers=admin).json()
    assert [r["subject"] for r in body["rows"]] == [
        {"type": SUBJECT_USER, "id": PETR, "label": "Petr Sales"},
        {"type": SUBJECT_LOCAL_GROUP, "id": EMEA_SALES, "label": "EMEA sales"},
    ]
    assert body["masks"]["margin"][0]["subject"] == {"type": SUBJECT_USER, "id": LUCIE, "label": "Lucie Finance"}


def test_detail_name_is_case_insensitive(any_client, admin):
    assert any_client.get("/api/datasets/DRUG_SALES", headers=admin).json()["dataset"]["name"] == DRUG_SALES


def test_detail_of_unknown_dataset_is_404(any_client, admin):
    response = any_client.get("/api/datasets/nope", headers=admin)
    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown dataset 'nope'."


# ---- PUT /datasets/{name} ---------------------------------------------------------------------


def test_put_replaces_lines_keeps_ids_and_orders_by_body(any_client, admin, runtime):
    current = any_client.get(f"/api/datasets/{DRUG_SALES}", headers=admin).json()
    public, own, finance_line, hide = current["rows"]
    margin = current["masks"]["margin"]
    new_masks = {
        # Reversed order: "Everyone hidden" first now wins for everybody.
        "margin": [
            {"mask_id": margin[1]["mask_id"], "subject": EVERYONE, "treatment": HIDDEN},
            {"mask_id": margin[0]["mask_id"], "subject": margin[0]["subject"], "treatment": CLEAR},
        ],
        "revenue": [{"subject": EVERYONE, "treatment": MASK_PARTIAL}],
    }
    # A hide line first, then the show lines swapped, then a brand new show line; finance line dropped.
    rows = [_strip(hide), _strip(own), _strip(public), _show({"type": SUBJECT_USER, "id": PETR.upper()}, ALL_ROWS)]
    response = _put(any_client, admin, DRUG_SALES, rows, new_masks)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["message"] == "Security of 'drug_sales' saved."

    ids = [r["rule_id"] for r in body["rows"]]
    assert ids[:2] == [own["rule_id"], public["rule_id"]]
    assert ids[3] == hide["rule_id"]
    assert SECURITY_ID.fullmatch(ids[2]) and ids[2] not in {public["rule_id"], own["rule_id"], finance_line["rule_id"]}
    assert body["rows"][2]["subject"] == {"type": SUBJECT_USER, "id": PETR, "label": "Petr Sales"}
    assert [m["mask_id"] for m in body["masks"]["margin"]] == [margin[1]["mask_id"], margin[0]["mask_id"]]
    assert "customer_id" not in body["masks"]
    assert body["masks"]["revenue"][0]["treatment"] == MASK_PARTIAL

    stored = runtime.store.list_row_access_rules(DRUG_SALES)
    assert [(r.rule_id, r.kind, r.position) for r in stored] == [
        (own["rule_id"], KIND_SHOW, 1), (public["rule_id"], KIND_SHOW, 2), (ids[2], KIND_SHOW, 3),
        (hide["rule_id"], KIND_HIDE, 1),
    ]
    masks = runtime.store.list_column_masks(DRUG_SALES)
    assert [(m.column_name, m.position, m.treatment) for m in masks] == [
        ("margin", 1, HIDDEN), ("margin", 2, CLEAR), ("revenue", 1, MASK_PARTIAL),
    ]
    assert ("dataset_security_update", DRUG_SALES) in _admin_events(runtime)
    # The GET answer matches the PUT answer (minus the message).
    assert any_client.get(f"/api/datasets/{DRUG_SALES}", headers=admin).json() == {
        k: v for k, v in body.items() if k != "message"
    }


def test_put_with_empty_lists_clears_the_dataset(any_client, admin, runtime):
    response = _put(any_client, admin, DRUG_SALES, [])
    assert response.status_code == 200
    assert response.json()["rows"] == [] and response.json()["masks"] == {}
    assert _security_state(runtime, DRUG_SALES) == ((), ())
    # Other datasets are untouched.
    assert runtime.store.dataset_security(SUPPLY_CHAIN).rows


def test_put_unknown_dataset_is_404(any_client, admin, runtime):
    assert _put(any_client, admin, "nope", []).status_code == 404
    assert ("dataset_security_update", "nope") not in _admin_events(runtime)


_GOOD = _show(EVERYONE, ALL_ROWS)
_BAD_PUTS: list[tuple[str, list, dict, str]] = [
    ("malformed filter", [_GOOD, _show(EVERYONE, {"conditions": []})], {},
     'Row line 2: "conditions" must be a non-empty list (use {"all": true} for all rows)'),
    ("unknown filter field", [_show(EVERYONE, {"all": True, "x": 1})], {},
     'Row line 1: an all-rows filter is exactly {"all": true}'),
    ("unknown operator", [_show(EVERYONE, _where({"column": "company", "op": "like", "value": "P%"}))], {},
     "Row line 1: operator must be one of"),
    ("unknown column", [_GOOD, _GOOD, _show(EVERYONE, _where(_is("nope", "x")))], {},
     "Row line 3: unknown column 'nope'"),
    ("wrong numeric literal", [_show(EVERYONE, _where(_is("units", "abc")))], {},
     "Row line 1: 'abc' is not a number (units is a numeric column)"),
    ("wrong boolean literal", [_hide(EVERYONE, _where(_is("is_test", "maybe")))], {},
     "Row line 1: 'maybe' is not true or false (is_test is a boolean column)"),
    ("wrong date literal", [_show(EVERYONE, _where(_is("sale_date", "yesterday")))], {},
     "Row line 1: 'yesterday' is not a date (YYYY-MM-DD) (sale_date is a date column)"),
    ("wrong literal in a list", [_show(EVERYONE, _where({"column": "units", "op": "one_of", "values": ["1", "x"]}))],
     {}, "Row line 1: 'x' is not a number (units is a numeric column)"),
    ("order operator on text", [_show(EVERYONE, _where({"column": "company", "op": "gt", "value": "A"}))], {},
     "Row line 1: 'gt' needs a numeric or date column; 'company' is text"),
    ("bad source", [_show(EVERYONE, _where({"column": "company", "op": "is", "source": "header:x"}))], {},
     "Row line 1: source must be 'email' or 'attribute:<key>'"),
    ("unknown kind", [{"kind": "maybe", "subject": EVERYONE, "filter": ALL_ROWS}], {},
     "Row line 1: kind must be one of show, hide"),
    ("unknown user", [_GOOD, _show({"type": SUBJECT_USER, "id": "ghost@nowhere.example"}, ALL_ROWS)], {},
     "Row line 2: unknown user 'ghost@nowhere.example'"),
    ("unknown group", [_show({"type": SUBJECT_GROUP, "id": "000000000000"}, ALL_ROWS)], {},
     "Row line 1: unknown group '000000000000'"),
    ("unknown local group", [_show({"type": SUBJECT_LOCAL_GROUP, "id": "nope"}, ALL_ROWS)], {},
     "Row line 1: unknown local group 'nope'"),
    ("unknown subject type", [_show({"type": "role", "id": "x"}, ALL_ROWS)], {},
     "Row line 1: subject type must be one of everyone, user, local_group, group"),
    ("everyone with an id", [_show({"type": SUBJECT_EVERYONE, "id": PETR}, ALL_ROWS)], {},
     "Row line 1: 'everyone' takes no subject id"),
    ("invalid line id", [_show(EVERYONE, ALL_ROWS, rule_id="NOT-AN-ID")], {},
     "Row line 1: invalid line id 'NOT-AN-ID'"),
    ("bad treatment", [], {"margin": [{"subject": EVERYONE, "treatment": CLEAR},
                                      {"subject": EVERYONE, "treatment": "blur"}]},
     "Mask on margin, line 2: treatment must be one of clear, mask_partial, mask_hash, hidden"),
    ("unknown mask column", [], {"Nope": [{"subject": EVERYONE, "treatment": HIDDEN}]},
     "Mask on nope, line 1: unknown column 'Nope'"),
    ("unknown mask subject", [], {"margin": [{"subject": {"type": SUBJECT_USER, "id": "ghost@nowhere.example"},
                                              "treatment": HIDDEN}]},
     "Mask on margin, line 1: unknown user 'ghost@nowhere.example'"),
]


@pytest.mark.parametrize(("rows", "masks", "detail"), [c[1:] for c in _BAD_PUTS], ids=[c[0] for c in _BAD_PUTS])
def test_put_refuses_bad_lines_naming_the_line_and_writes_nothing(any_client, admin, runtime, rows, masks, detail):
    before = _security_state(runtime, DRUG_SALES)
    response = _put(any_client, admin, DRUG_SALES, rows, masks)
    assert response.status_code == 400, response.text
    assert response.json()["detail"].startswith(detail)
    assert _security_state(runtime, DRUG_SALES) == before
    assert ("dataset_security_update", DRUG_SALES) not in _admin_events(runtime)


def test_put_refuses_a_line_id_of_another_dataset(any_client, admin, runtime):
    foreign = runtime.store.list_row_access_rules(SUPPLY_CHAIN)[0].rule_id
    before = _security_state(runtime, DRUG_SALES)
    response = _put(any_client, admin, DRUG_SALES, [_show(EVERYONE, ALL_ROWS, rule_id=foreign)])
    assert response.status_code == 400
    assert response.json()["detail"] == f"Row line 1: line id '{foreign}' belongs to another dataset"
    assert _security_state(runtime, DRUG_SALES) == before


def test_put_keeps_attribute_values_as_data(any_client, admin, runtime):
    """A literal with quotes is stored and returned as given; it never becomes SQL text."""
    value = "PHARMA_A' OR '1'='1"
    response = _put(any_client, admin, DRUG_SALES, [_show(EVERYONE, _where(_is("company", value)))])
    assert response.status_code == 200
    assert response.json()["rows"][0]["filter"] == _where(_is("company", value))


# ---- POST /datasets/{name}/load-examples ------------------------------------------------------


def test_load_examples_refuses_a_dataset_with_lines(any_client, admin, runtime):
    before = _security_state(runtime, DRUG_SALES)
    response = any_client.post(f"/api/datasets/{DRUG_SALES}/load-examples", headers=admin)
    assert response.status_code == 409
    assert "already has security lines" in response.json()["detail"]
    assert _security_state(runtime, DRUG_SALES) == before
    assert ("dataset_security_load_examples", DRUG_SALES) not in _admin_events(runtime)


def test_load_examples_refuses_a_dataset_with_only_masks(any_client, admin):
    masks = {"margin": [{"subject": EVERYONE, "treatment": HIDDEN}]}
    assert _put(any_client, admin, DRUG_SALES, [], masks).status_code == 200
    assert any_client.post(f"/api/datasets/{DRUG_SALES}/load-examples", headers=admin).status_code == 409


def test_load_examples_fills_an_empty_dataset(any_client, admin, runtime, mode):
    finance = _group_id(runtime, FINANCE_EXTERNAL)
    assert _put(any_client, admin, DRUG_SALES, []).status_code == 200
    response = any_client.post(f"/api/datasets/{DRUG_SALES}/load-examples", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["message"] == "Loaded 8 example line(s) into 'drug_sales'."
    assert body["skipped"] == []
    assert body["data_available"] is (mode == WITH_DATA)
    assert [(r["kind"], r["subject"]["type"], r["subject"]["id"]) for r in body["rows"]] == [
        (KIND_SHOW, SUBJECT_EVERYONE, ""), (KIND_SHOW, SUBJECT_EVERYONE, ""), (KIND_SHOW, SUBJECT_GROUP, finance),
        (KIND_HIDE, SUBJECT_EVERYONE, ""),
    ]
    assert [m["treatment"] for m in body["masks"]["margin"]] == [CLEAR, HIDDEN]
    assert [m["treatment"] for m in body["masks"]["customer_id"]] == [MASK_HASH, MASK_PARTIAL]
    assert len(runtime.store.list_row_access_rules(DRUG_SALES)) == 4
    assert len(runtime.store.list_column_masks(DRUG_SALES)) == 4
    assert ("dataset_security_load_examples", DRUG_SALES) in _admin_events(runtime)


def test_load_examples_skips_lines_whose_group_is_not_registered(any_client, admin, runtime):
    runtime.store.delete_group(_group_id(runtime, FINANCE_EXTERNAL))
    assert _put(any_client, admin, DRUG_SALES, []).status_code == 200
    response = any_client.post(f"/api/datasets/{DRUG_SALES}/load-examples", headers=admin)
    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] == [
        f"Row line 3: IdP group '{FINANCE_EXTERNAL}' is not registered",
        f"Mask on margin, line 1: IdP group '{FINANCE_EXTERNAL}' is not registered",
        f"Mask on customer_id, line 1: IdP group '{FINANCE_EXTERNAL}' is not registered",
    ]
    assert body["message"] == "Loaded 5 example line(s) into 'drug_sales'; 3 skipped."
    assert [r["subject"]["type"] for r in body["rows"]] == [SUBJECT_EVERYONE] * 3
    assert {c: [m["treatment"] for m in ms] for c, ms in body["masks"].items()} == {
        "customer_id": [MASK_PARTIAL], "margin": [HIDDEN],
    }


def test_load_examples_for_unknown_dataset_is_404(any_client, admin):
    assert any_client.post("/api/datasets/nope/load-examples", headers=admin).status_code == 404


# Every dataset of the pharma catalogue declares examples, so the "no examples" 400 has no
# dataset to exercise it through the real policy files.


# ---- GET /datasets/{name}/values/{column} -----------------------------------------------------


def test_values_lists_distinct_values(client, admin):
    response = client.get(f"/api/datasets/{DRUG_SALES}/values/company", headers=admin)
    assert response.status_code == 200
    assert response.json() == {"values": COMPANIES, "truncated": False}


def test_values_filter_is_case_insensitive(client, admin):
    url = f"/api/datasets/{DRUG_SALES}/values/company"
    assert client.get(url, headers=admin, params={"q": "pharma_b"}).json() == {"values": ["PHARMA_B"], "truncated": False}
    assert client.get(url, headers=admin, params={"q": "ArMa_C"}).json()["values"] == ["PHARMA_C"]
    assert client.get(url, headers=admin, params={"q": "zzz"}).json() == {"values": [], "truncated": False}


def test_values_filter_is_data_not_sql(client, admin):
    url = f"/api/datasets/{DRUG_SALES}/values/company"
    response = client.get(url, headers=admin, params={"q": "x' OR '1'='1"})
    assert response.status_code == 200
    assert response.json() == {"values": [], "truncated": False}


def test_values_are_capped_and_flag_truncation(client, admin, warehouse):
    assert _count(warehouse, "SELECT COUNT(DISTINCT sale_id) FROM drug_sales") > VALUES_LIMIT
    body = client.get(f"/api/datasets/{DRUG_SALES}/values/sale_id", headers=admin).json()
    assert len(body["values"]) == VALUES_LIMIT and body["truncated"] is True
    assert all(isinstance(v, str) for v in body["values"])
    assert body["values"] == sorted(body["values"])


def test_values_column_is_case_insensitive(client, admin):
    assert client.get(f"/api/datasets/{DRUG_SALES}/values/COMPANY", headers=admin).json()["values"] == COMPANIES


def test_values_unknown_column_or_dataset_is_404(any_client, admin):
    response = any_client.get(f"/api/datasets/{DRUG_SALES}/values/nope", headers=admin)
    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown column 'nope' in 'drug_sales'."
    assert any_client.get("/api/datasets/nope/values/company", headers=admin).status_code == 404


def test_values_without_data_is_503(nodata_client, admin):
    response = nodata_client.get(f"/api/datasets/{DRUG_SALES}/values/company", headers=admin)
    assert response.status_code == 503
    assert "not available" in response.json()["detail"]


# ---- POST /datasets/{name}/test: with the warehouse --------------------------------------------


def test_test_as_explains_lines_counts_columns_and_preview(client, admin, runtime, warehouse):
    security = runtime.store.dataset_security(DRUG_SALES)
    public, own, finance_line, hide = security.rows
    finance = _group_id(runtime, FINANCE_EXTERNAL)
    body = _test(client, admin, DRUG_SALES, PETR.upper())

    assert body["principal"] == {
        "email": PETR, "local_groups": [EMEA_SALES], "idp_groups": [], "attributes": {"company": "PHARMA_A"},
    }
    assert body["accessible"] is True and body["data_available"] is True

    total = _count(warehouse, "SELECT COUNT(*) FROM drug_sales")
    public_rows = _count(warehouse, "SELECT COUNT(*) FROM drug_sales WHERE visibility = 'public'")
    own_rows = _count(warehouse, "SELECT COUNT(*) FROM drug_sales WHERE company = 'PHARMA_A'")
    test_rows = _count(warehouse, "SELECT COUNT(*) FROM drug_sales WHERE is_test")
    expected_visible = _count(
        warehouse,
        "SELECT COUNT(*) FROM drug_sales WHERE (visibility = 'public' OR company = 'PHARMA_A') "
        "AND NOT COALESCE(is_test, FALSE)",
    )
    assert body["rows"]["total"] == total
    assert body["rows"]["visible"] == expected_visible
    assert body["rows"]["visible"] == _guard_visible(runtime, Principal(PETR, frozenset()), DRUG_SALES)

    lines = body["rows"]["lines"]
    assert [line["rule_id"] for line in lines] == [r.rule_id for r in security.rows]
    by_id = _lines_by_id(body)
    assert by_id[public.rule_id] == {
        "rule_id": public.rule_id, "kind": KIND_SHOW, "applies": True, "reason": "applies: every signed-in user",
        "matched_rows": public_rows, "warning": None,
    }
    assert by_id[own.rule_id]["applies"] is True and by_id[own.rule_id]["matched_rows"] == own_rows
    assert by_id[finance_line.rule_id] == {
        "rule_id": finance_line.rule_id, "kind": KIND_SHOW, "applies": False,
        "reason": f"does not apply: does not carry IdP group finance", "matched_rows": None, "warning": None,
    }
    assert by_id[hide.rule_id]["kind"] == KIND_HIDE and by_id[hide.rule_id]["applies"] is True
    assert by_id[hide.rule_id]["matched_rows"] == test_rows

    masks = {(m.column_name, m.position): m for m in security.masks}
    columns = _columns_by_name(body)
    assert [c["name"] for c in body["columns"]] == [c["name"] for c in client.get(
        f"/api/datasets/{DRUG_SALES}", headers=admin).json()["dataset"]["columns"]]
    assert columns["margin"] == {
        "name": "margin", "treatment": HIDDEN, "mask_id": masks[("margin", 2)].mask_id,
        "reason": "line 2 applies: every signed-in user",
    }
    assert columns["customer_id"]["treatment"] == MASK_PARTIAL
    assert columns["customer_id"]["mask_id"] == masks[("customer_id", 2)].mask_id
    assert columns["revenue"] == {
        "name": "revenue", "treatment": CLEAR, "mask_id": None, "reason": "no masking line applies: clear",
    }

    preview = body["preview"]
    assert expected_visible > PREVIEW_ROW_LIMIT
    assert len(preview["rows"]) == PREVIEW_ROW_LIMIT and preview["truncated"] is True
    assert "margin" not in preview["columns"]
    assert preview["columns"] == [c["name"] for c in body["columns"] if c["treatment"] != HIDDEN]
    customer = preview["columns"].index("customer_id")
    company = preview["columns"].index("company")
    is_test = preview["columns"].index("is_test")
    visibility = preview["columns"].index("visibility")
    for row in preview["rows"]:
        assert len(row) == len(preview["columns"])
        assert row[customer].startswith(PARTIAL_PREFIX) and "CUST-" not in row[customer]
        assert row[is_test] is False
        assert row[visibility] == "public" or row[company] == "PHARMA_A"
    assert isinstance(body["row_filter"], str) and "PHARMA_A" in body["row_filter"]
    assert runtime.settings.secrets.mask_salt not in json.dumps(body)


def test_test_as_with_idp_groups_applies_group_lines(client, admin, runtime, warehouse):
    security = runtime.store.dataset_security(DRUG_SALES)
    finance_line = security.rows[2]
    finance = _group_id(runtime, FINANCE_EXTERNAL)
    masks = {(m.column_name, m.position): m for m in security.masks}

    # The admin cannot know IdP groups: without them, Lucie is just another signed-in user.
    plain = _test(client, admin, DRUG_SALES, LUCIE)
    assert _lines_by_id(plain)[finance_line.rule_id]["applies"] is False
    assert _columns_by_name(plain)["margin"]["treatment"] == HIDDEN

    body = _test(client, admin, DRUG_SALES, LUCIE, groups=[f" {FINANCE_EXTERNAL} ", ""])
    assert body["principal"]["idp_groups"] == [FINANCE_EXTERNAL]
    assert body["principal"]["local_groups"] == [EMEA_SALES]
    line = _lines_by_id(body)[finance_line.rule_id]
    assert line["applies"] is True and line["reason"] == f"applies: carries IdP group finance"
    assert line["matched_rows"] == body["rows"]["total"]

    test_rows = _count(warehouse, "SELECT COUNT(*) FROM drug_sales WHERE is_test")
    assert body["rows"]["visible"] == body["rows"]["total"] - test_rows
    assert body["rows"]["visible"] == _guard_visible(
        runtime, Principal(LUCIE, frozenset({FINANCE_EXTERNAL})), DRUG_SALES
    )

    columns = _columns_by_name(body)
    assert columns["margin"] == {
        "name": "margin", "treatment": CLEAR, "mask_id": masks[("margin", 1)].mask_id,
        "reason": f"line 1 applies: carries IdP group finance",
    }
    assert columns["customer_id"]["treatment"] == MASK_HASH
    preview = body["preview"]
    assert "margin" in preview["columns"]
    margin = preview["columns"].index("margin")
    customer = preview["columns"].index("customer_id")
    for row in preview["rows"]:
        assert isinstance(row[margin], int | float)
        assert HEX_SHA256.fullmatch(row[customer])
    # Hashes use the salt but never show it.
    assert runtime.settings.secrets.mask_salt not in json.dumps(body)


def test_test_as_explains_a_missing_attribute(client, admin, runtime):
    security = runtime.store.dataset_security(DRUG_SALES)
    public, own = security.rows[0], security.rows[1]
    body = _test(client, admin, DRUG_SALES, NOATTR)
    assert body["principal"]["attributes"] == {}
    assert _lines_by_id(body)[own.rule_id] == {
        "rule_id": own.rule_id, "kind": KIND_SHOW, "applies": False,
        "reason": "does not apply: the user has no attribute 'company'", "matched_rows": None, "warning": None,
    }
    assert _lines_by_id(body)[public.rule_id]["applies"] is True
    assert body["accessible"] is True
    assert body["rows"]["visible"] == _guard_visible(runtime, Principal(NOATTR, frozenset()), DRUG_SALES)


def test_test_as_hide_line_with_missing_attribute_hides_every_row(client, admin, runtime):
    rows = [_show(EVERYONE, ALL_ROWS),
            _hide(EVERYONE, _where({"column": "company", "op": "is_not", "source": "attribute:company"}))]
    assert _put(client, admin, DRUG_SALES, rows).status_code == 200
    body = _test(client, admin, DRUG_SALES, NOATTR)
    hide = body["rows"]["lines"][1]
    assert hide["applies"] is True
    assert hide["reason"] == "hides every row: the user has no attribute 'company' (fail closed)"
    assert hide["matched_rows"] == body["rows"]["total"]
    assert body["accessible"] is True and body["rows"]["visible"] == 0
    assert body["preview"]["rows"] == [] and body["preview"]["truncated"] is False
    assert _guard_visible(runtime, Principal(NOATTR, frozenset()), DRUG_SALES) == 0


def test_test_as_warns_about_a_line_that_matches_no_row(client, admin, runtime):
    rows = [_show(EVERYONE, _where(_is("company", "Pharma A s.r.o.")))]
    assert _put(client, admin, DRUG_SALES, rows).status_code == 200
    body = _test(client, admin, DRUG_SALES, PETR)
    line = body["rows"]["lines"][0]
    assert line["applies"] is True and line["matched_rows"] == 0
    assert line["warning"] == "No row has company = 'Pharma A s.r.o.' (values: PHARMA_A, PHARMA_B, PHARMA_C)"
    assert body["rows"]["visible"] == 0 and body["accessible"] is True


def test_zero_row_warning_lists_at_most_the_capped_values(client, admin, warehouse):
    assert _count(warehouse, "SELECT COUNT(DISTINCT customer_id) FROM drug_sales") > WARNING_VALUES_LIMIT
    rows = [_show(EVERYONE, _where(_is("customer_id", "nobody")))]
    assert _put(client, admin, DRUG_SALES, rows).status_code == 200
    warning = _test(client, admin, DRUG_SALES, PETR)["rows"]["lines"][0]["warning"]
    assert warning.startswith("No row has customer_id = 'nobody' (values: ") and warning.endswith(", ...)")
    listed = warning.removeprefix("No row has customer_id = 'nobody' (values: ").removesuffix(", ...)").split(", ")
    assert len(listed) == WARNING_VALUES_LIMIT


def test_zero_row_warning_for_numeric_and_combined_conditions(client, admin):
    rows = [
        _show(EVERYONE, _where({"column": "units", "op": "gt", "value": "100000"})),
        # Store B001 belongs to PHARMA_B: each condition matches rows, together none.
        _show(EVERYONE, _where(_is("company", "PHARMA_A"), _is("store_id", "B001"))),
        _show(EVERYONE, _where(_is("visibility", "public"))),
    ]
    assert _put(client, admin, DRUG_SALES, rows).status_code == 200
    lines = _test(client, admin, DRUG_SALES, PETR)["rows"]["lines"]
    assert lines[0]["warning"] == "No row has units > 100000"
    assert lines[1]["matched_rows"] == 0
    assert lines[1]["warning"] == "No row matches all conditions of this line together."
    assert lines[2]["warning"] is None and lines[2]["matched_rows"] > 0


def test_test_as_user_and_local_group_subjects(client, admin, runtime):
    rows = [
        _show({"type": SUBJECT_LOCAL_GROUP, "id": EMEA_SALES}, _where(_is("visibility", "public"))),
        _show({"type": SUBJECT_USER, "id": PETR}, _where(_is("company", "PHARMA_B"))),
    ]
    masks = {"revenue": [{"subject": {"type": SUBJECT_USER, "id": PETR}, "treatment": MASK_PARTIAL}]}
    assert _put(client, admin, DRUG_SALES, rows, masks).status_code == 200
    petr = _test(client, admin, DRUG_SALES, PETR)
    assert [line["reason"] for line in petr["rows"]["lines"]] == [
        f"applies: member of local group EMEA sales", "applies: this user",
    ]
    assert _columns_by_name(petr)["revenue"]["treatment"] == MASK_PARTIAL
    assert _columns_by_name(petr)["revenue"]["reason"] == "line 1 applies: this user"
    assert petr["rows"]["visible"] == _guard_visible(runtime, Principal(PETR, frozenset()), DRUG_SALES)

    other = _test(client, admin, DRUG_SALES, NOGRANT)
    assert [line["reason"] for line in other["rows"]["lines"]] == [
        f"does not apply: not a member of local group EMEA sales", "does not apply: only for user Petr Sales",
    ]
    assert other["accessible"] is False
    assert other["rows"]["visible"] == 0 and other["rows"]["total"] == petr["rows"]["total"]
    assert other["preview"] is None and other["row_filter"] is None
    assert _columns_by_name(other)["revenue"]["treatment"] == CLEAR
    assert _guard_visible(runtime, Principal(NOGRANT, frozenset()), DRUG_SALES) is None


def test_test_as_dataset_without_applying_show_line_is_not_accessible(client, admin):
    body = _test(client, admin, SUPPLY_CHAIN, NOGRANT)
    assert body["accessible"] is False
    assert body["rows"]["visible"] == 0 and body["rows"]["total"] > 0
    assert all(line["applies"] is False and line["matched_rows"] is None for line in body["rows"]["lines"])
    assert body["preview"] is None and body["row_filter"] is None
    catalogue = client.get(f"/api/datasets/{SUPPLY_CHAIN}", headers=admin).json()["dataset"]["columns"]
    assert [c["name"] for c in body["columns"]] == [c["name"] for c in catalogue]  # every column explained
    assert _columns_by_name(body)["contract_ref"]["treatment"] == HIDDEN


def test_test_as_attribute_values_never_become_sql(client, admin, runtime, warehouse):
    """An attribute value with quotes is a literal: it matches no row and widens nothing."""
    runtime.store.set_attribute(NOGRANT, "company", "PHARMA_A' OR '1'='1")
    security = runtime.store.dataset_security(DRUG_SALES)
    body = _test(client, admin, DRUG_SALES, NOGRANT)
    own = _lines_by_id(body)[security.rows[1].rule_id]
    assert own["applies"] is True and own["matched_rows"] == 0
    assert own["warning"].startswith("No row has company = 'PHARMA_A'' OR ''1''=''1'")
    expected = _count(
        warehouse, "SELECT COUNT(*) FROM drug_sales WHERE visibility = 'public' AND NOT COALESCE(is_test, FALSE)"
    )
    assert body["rows"]["visible"] == expected
    assert body["rows"]["visible"] == _guard_visible(runtime, Principal(NOGRANT, frozenset()), DRUG_SALES)


def test_test_as_unregistered_email_is_explained(client, admin):
    body = _test(client, admin, DRUG_SALES, "someone@else.example")
    assert body["principal"] == {
        "email": "someone@else.example", "local_groups": [], "idp_groups": [], "attributes": {},
    }
    assert body["accessible"] is True  # the public "everyone" line still applies


def test_test_as_is_audited(any_client, admin, runtime):
    _test(any_client, admin, DRUG_SALES, PETR.upper())
    assert ("dataset_security_test", f"{DRUG_SALES}:{PETR}") in _admin_events(runtime)


@pytest.mark.parametrize("email", ["not-an-email", "", "a b@c.d", "x@y"])
def test_test_as_with_bad_email_is_400(any_client, admin, runtime, email):
    response = any_client.post(f"/api/datasets/{DRUG_SALES}/test", headers=admin, json={"email": email})
    assert response.status_code == 400
    assert "is not a valid email" in response.json()["detail"]
    assert not [e for e in _admin_events(runtime) if e[0] == "dataset_security_test"]


def test_test_as_unknown_dataset_is_404(any_client, admin):
    assert any_client.post("/api/datasets/nope/test", headers=admin, json={"email": PETR}).status_code == 404


# ---- POST /datasets/{name}/test: without the warehouse -----------------------------------------


def test_test_as_without_data_explains_lines_and_columns_without_counts(client, nodata_client, admin, runtime):
    with_data = _test(client, admin, DRUG_SALES, PETR)
    body = _test(nodata_client, admin, DRUG_SALES, PETR)
    assert body["data_available"] is False
    assert body["principal"] == with_data["principal"]
    assert body["accessible"] is True
    assert body["rows"]["visible"] is None and body["rows"]["total"] is None
    assert body["preview"] is None
    assert body["columns"] == with_data["columns"]
    assert body["row_filter"] == with_data["row_filter"]
    assert [(line["rule_id"], line["kind"], line["applies"], line["reason"]) for line in body["rows"]["lines"]] == [
        (line["rule_id"], line["kind"], line["applies"], line["reason"]) for line in with_data["rows"]["lines"]
    ]
    assert all(line["matched_rows"] is None and line["warning"] is None for line in body["rows"]["lines"])


def test_test_as_without_data_gives_no_zero_row_warning(nodata_client, admin):
    rows = [_show(EVERYONE, _where(_is("company", "Pharma A s.r.o.")))]
    assert _put(nodata_client, admin, DRUG_SALES, rows).status_code == 200
    line = _test(nodata_client, admin, DRUG_SALES, PETR)["rows"]["lines"][0]
    assert line["applies"] is True and line["matched_rows"] is None and line["warning"] is None


def test_test_as_without_data_with_groups_and_inaccessible_dataset(nodata_client, admin, runtime):
    finance = _group_id(runtime, FINANCE_EXTERNAL)
    body = _test(nodata_client, admin, DRUG_SALES, LUCIE, groups=[FINANCE_EXTERNAL])
    assert body["rows"]["lines"][2]["reason"] == f"applies: carries IdP group finance"
    assert _columns_by_name(body)["margin"]["treatment"] == CLEAR
    hidden = _test(nodata_client, admin, SUPPLY_CHAIN, NOGRANT)
    assert hidden["accessible"] is False and hidden["row_filter"] is None
    assert hidden["rows"]["visible"] is None and hidden["preview"] is None


def test_test_reasons_name_groups_by_their_display_name(client, admin, runtime):
    """The Test as reasons must read like the editor: an IdP group by its name, not its internal id."""
    store = runtime.store
    group = next(g for g in store.list_groups())
    detail = client.get("/api/datasets/drug_sales", headers=admin).json()
    rows = [{k: v for k, v in line.items() if k != "subject"} | {"subject": {"type": line["subject"]["type"], "id": line["subject"]["id"]}}
            for line in detail["rows"]]
    rows.append({"kind": "show", "subject": {"type": "group", "id": group.group_id}, "filter": {"all": True}})
    saved = client.put("/api/datasets/drug_sales", headers=admin, json={"rows": rows, "masks": {}})
    assert saved.status_code == 200, saved.text
    result = client.post("/api/datasets/drug_sales/test", headers=admin,
                         json={"email": "tomas.nogrant@pharma-a.example", "groups": []}).json()
    reasons = [line["reason"] for line in result["rows"]["lines"]]
    assert any(group.name in r for r in reasons)
    assert not any(group.group_id in r for r in reasons)
