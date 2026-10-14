"""Demo dashboards (sales, supply): gate outcomes, row/column security as rendered, ad-hoc SQL, audit."""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable

import duckdb
import pytest
from fastapi.testclient import TestClient

from apps.dashboard_common.core import CellKind, ColumnState, column_state, format_cell, numeric
from apps.sales.main import EXAMPLES as SALES_EXAMPLES
from apps.sales.main import create_app as create_sales
from apps.supply.main import EXAMPLES as SUPPLY_EXAMPLES
from apps.supply.main import create_app as create_supply
from kbc_access.gate import Gate
from kbc_access.identity import Principal, principal_from_headers
from kbc_access.security_model import MASK_HASH, MASK_PARTIAL, SUBJECT_USER
from kbc_access.store import ColumnMaskInput, RowAccessRuleInput
from kbc_access.web import Runtime, build_guard

PETR = "petr.sales@pharma-a.example"
LUCIE = "lucie.finance@pharma-a.example"
JAN = "jan.partner@pharma-b.example"
MARTIN = "martin.noattr@pharma-c.example"
TOMAS = "tomas.nogrant@pharma-a.example"
OLGA = "olga.left@pharma-a.example"
EVA = "eva.supply@pharma-a.example"
ANNA = "anna.admin@pharma-a.example"

Headers = Callable[[str], dict[str, str]]


@pytest.fixture
def sales(runtime: Runtime) -> TestClient:
    return TestClient(create_sales(runtime))


@pytest.fixture
def supply(runtime: Runtime) -> TestClient:
    return TestClient(create_supply(runtime))


def principal(runtime: Runtime, headers_for: Headers, email: str) -> Principal:
    p = principal_from_headers(headers_for(email), runtime.settings.identity).principal
    return p.with_attributes(runtime.store.attributes_for(p.email))


def audit_events(runtime: Runtime) -> list[dict]:
    path = runtime.settings.path(runtime.settings.audit.path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def company_visibility_rows(html: str) -> set[tuple[str, str]]:
    """(company, visibility) pairs in the sales 'Revenue by company' fallback table."""
    table = re.search(r'<table id="by-company">(.*?)</table>', html, re.S)
    assert table, "by-company table missing"
    return set(re.findall(r"<tr><td>(\w+)</td><td>(\w+)</td>", table.group(1)))


def warehouse(runtime: Runtime) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(runtime.settings.path(runtime.settings.data.duckdb_path)), read_only=True)


def section(html: str, marker: str) -> str:
    """The HTML from `marker` to the end of its <section>."""
    return html.split(marker, 1)[1].split("</section>", 1)[0]


def margin_tile(html: str) -> str:
    """The 'Avg margin' KPI tile of the sales dashboard."""
    return html.split("Avg margin", 1)[1].split("</div>\n  </div>", 1)[0]


def fresh(runtime: Runtime) -> Runtime:
    """The runtime with a gate that never caches, so a change in the store applies on the next request."""
    return dataclasses.replace(runtime, gate=Gate(runtime.store, 0))


def add_masks(runtime: Runtime, dataset: str, extra: list[ColumnMaskInput]) -> None:
    """Put `extra` masking lines ahead of the dataset's stored ones (first applying line wins)."""
    security = runtime.store.dataset_security(dataset)
    rows = [RowAccessRuleInput(r.kind, r.subject_type, r.subject_id, r.filter.to_json() if r.filter else {}, r.rule_id)
            for r in security.rows]
    masks = [ColumnMaskInput(m.column_name, m.subject_type, m.subject_id, m.treatment, m.mask_id) for m in security.masks]
    runtime.store.set_dataset_security(dataset, rows, [*extra, *masks])


# --- gate --------------------------------------------------------------------

@pytest.mark.parametrize(
    ("email", "sales_status", "supply_status"),
    [
        (PETR, 200, 403),
        (LUCIE, 200, 200),
        (JAN, 200, 403),
        (MARTIN, 200, 403),
        (TOMAS, 403, 403),
        (OLGA, 403, 403),
        (EVA, 403, 200),
        (ANNA, 403, 200),
    ],
)
def test_gate_outcomes(sales: TestClient, supply: TestClient, headers_for: Headers, email: str, sales_status: int, supply_status: int) -> None:
    for client, expected in ((sales, sales_status), (supply, supply_status)):
        for path in ("/", "/query"):
            assert client.get(path, headers=headers_for(email)).status_code == expected, (email, path)
        assert client.post("/query", data={"sql": "SELECT 1"}, headers=headers_for(email)).status_code == expected


def test_deactivated_user_reason(sales: TestClient, headers_for: Headers) -> None:
    response = sales.get("/", headers=headers_for(OLGA))
    assert response.status_code == 403
    assert "user_deactivated" in response.text


def test_no_headers_is_401(sales: TestClient, supply: TestClient) -> None:
    root = sales.get("/")  # root answers the platform probe: sign-in page, no data
    assert root.status_code == 200 and "Not signed in" in root.text and "drug_sales" not in root.text
    assert supply.get("/query").status_code == 401


def test_health_check_still_answers(sales: TestClient, supply: TestClient) -> None:
    assert sales.post("/").json() == {"status": "ok"}
    assert supply.post("/").json() == {"status": "ok"}


# --- sales: rows -----------------------------------------------------------

def test_petr_sees_no_foreign_private_rows(runtime: Runtime, sales: TestClient, headers_for: Headers) -> None:
    html = sales.get("/", headers=headers_for(PETR)).text
    pairs = company_visibility_rows(html)
    assert ("PHARMA_A", "private") in pairs
    assert ("PHARMA_B", "public") in pairs and ("PHARMA_C", "public") in pairs
    assert ("PHARMA_B", "private") not in pairs and ("PHARMA_C", "private") not in pairs

    guard = build_guard(runtime, "sales")
    result = guard.run(principal(runtime, headers_for, PETR), "SELECT DISTINCT company, visibility FROM drug_sales")
    assert set(result.result.rows) == {("PHARMA_A", "private"), ("PHARMA_A", "public"), ("PHARMA_B", "public"), ("PHARMA_C", "public")}


def test_petr_customer_masked_and_margin_hidden(runtime: Runtime, headers_for: Headers) -> None:
    guard = build_guard(runtime, "sales")
    viewer = principal(runtime, headers_for, PETR)
    result = guard.run(viewer, "SELECT company, visibility, customer_id FROM drug_sales")
    assert result.result is not None and result.result.rows
    for company, visibility, customer in result.result.rows:
        if visibility == "private":  # own company only
            assert company == "PHARMA_A"
        # Masking is decided for the whole column: partial on every row, own company or not.
        assert format_cell(customer).kind is CellKind.PARTIAL
    margin = guard.run(viewer, "SELECT margin FROM drug_sales")
    assert margin.result is None and margin.error is not None and margin.error.startswith("Query failed:")


def test_petr_margin_kpi_is_hidden(sales: TestClient, headers_for: Headers) -> None:
    html = sales.get("/", headers=headers_for(PETR)).text
    tile = margin_tile(html)
    assert '<span class="pill deny kpi-pill">hidden</span>' in tile
    assert "margin is not part of your view of drug_sales" in tile
    assert "partly hidden" not in html
    latest = html.split("Latest", 1)[1]
    assert "<th>customer_id</th>" in latest and "<th>margin</th>" not in latest
    assert "Query failed" not in html


def test_lucie_sees_three_companies_and_full_margin(runtime: Runtime, sales: TestClient, headers_for: Headers) -> None:
    html = sales.get("/", headers=headers_for(LUCIE)).text
    pairs = company_visibility_rows(html)
    assert {c for c, _ in pairs} == {"PHARMA_A", "PHARMA_B", "PHARMA_C"}
    assert ("PHARMA_B", "private") in pairs
    assert "partly hidden" not in html
    tile = margin_tile(html)
    assert "kpi-pill" not in tile and "%" in tile and "over all" in tile
    assert "<th>margin</th>" in html.split("Latest", 1)[1]

    guard = build_guard(runtime, "sales")
    result = guard.run(principal(runtime, headers_for, LUCIE), "SELECT company, visibility, margin, customer_id FROM drug_sales")
    assert result.result is not None and result.result.rows
    for _company, _visibility, margin, customer in result.result.rows:
        assert isinstance(margin, (int, float))
        assert format_cell(customer).kind is CellKind.HASH


def test_martin_without_attribute_sees_only_public_rows(runtime: Runtime, sales: TestClient, headers_for: Headers) -> None:
    html = sales.get("/", headers=headers_for(MARTIN)).text
    assert {v for _, v in company_visibility_rows(html)} == {"public"}
    assert '<span class="pill deny kpi-pill">hidden</span>' in margin_tile(html)
    guard = build_guard(runtime, "sales")
    result = guard.run(principal(runtime, headers_for, MARTIN), "SELECT DISTINCT visibility FROM drug_sales")
    assert result.result.rows == [("public",)]


def test_jan_partner_sees_pharma_b_private(sales: TestClient, headers_for: Headers) -> None:
    pairs = company_visibility_rows(sales.get("/", headers=headers_for(JAN)).text)
    assert ("PHARMA_B", "private") in pairs
    assert ("PHARMA_A", "private") not in pairs


def test_top_stores_join_runs(sales: TestClient, headers_for: Headers) -> None:
    html = sales.get("/", headers=headers_for(PETR)).text
    top = html.split("Top stores", 1)[1].split("</section>", 1)[0]
    assert "flash err" not in top
    assert "Lékárna" in top


# --- "What you can see" ------------------------------------------------------

def test_panel_lists_absent_columns_and_hidden_datasets(runtime: Runtime, sales: TestClient, supply: TestClient, headers_for: Headers) -> None:
    html = sales.get("/", headers=headers_for(MARTIN)).text
    panel = html.split('id="what-you-can-see"', 1)[1]
    assert '<span class="pill deny" title="hidden">margin</span>' in panel
    assert '<span class="pill mask" title="mask_partial">customer_id · mask_partial</span>' in panel
    assert '<span class="pill deny">supply_chain</span>' in panel  # dataset martin cannot see at all
    assert '<span class="pill deny">none</span>' in panel  # no attributes
    own_company = [r.rule_id for r in runtime.store.list_row_access_rules("drug_sales")
                   if r.filter and any(c.attribute == "company" for c in r.filter.conditions)]
    assert own_company and all(f"show:{rule_id}" not in panel for rule_id in own_company)

    panel = supply.get("/", headers=headers_for(ANNA)).text.split('id="what-you-can-see"', 1)[1]
    assert '<span class="pill deny" title="hidden">contract_ref</span>' in panel
    assert "unit_cost · mask_partial" in panel
    [anna_line] = [r.rule_id for r in runtime.store.list_row_access_rules("supply_chain")
                   if r.subject_type == SUBJECT_USER and r.subject_id == ANNA]
    assert f'<span class="pill allow">show:{anna_line}</span>' in panel
    assert "<code>company</code> = <b>PHARMA_A</b>" in panel


# --- column state and numeric() -------------------------------------------------

def test_column_state_follows_column_treatments(runtime: Runtime, headers_for: Headers) -> None:
    guard = build_guard(runtime, "sales")
    petr = guard.secured(principal(runtime, headers_for, PETR))["drug_sales"]
    assert column_state(petr, "margin") is ColumnState.ABSENT
    assert column_state(petr, "customer_id") is ColumnState.MASKED
    assert column_state(petr, "revenue") is ColumnState.CLEAR
    lucie = guard.secured(principal(runtime, headers_for, LUCIE))["drug_sales"]
    assert column_state(lucie, "margin") is ColumnState.CLEAR  # an explicit "clear" line
    assert column_state(lucie, "customer_id") is ColumnState.MASKED  # mask_hash
    supply = build_guard(runtime, "supply")
    jan = supply.secured(principal(runtime, headers_for, JAN))["supply_chain"]
    assert column_state(jan, "unit_cost") is ColumnState.ABSENT
    assert column_state(jan, "contract_ref") is ColumnState.ABSENT
    assert column_state(jan, "quantity") is ColumnState.CLEAR
    anna = supply.secured(principal(runtime, headers_for, ANNA))["supply_chain"]
    assert column_state(anna, "unit_cost") is ColumnState.MASKED


def test_numeric_reads_every_state_as_a_number() -> None:
    assert numeric("revenue", ColumnState.CLEAR) == "revenue"
    assert numeric("revenue", ColumnState.MASKED) == "TRY_CAST(revenue AS DOUBLE)"
    assert numeric("revenue", ColumnState.ABSENT) == "CAST(NULL AS DOUBLE)"


def test_aggregate_over_masked_numeric_column_works_through_numeric(runtime: Runtime, headers_for: Headers) -> None:
    viewer = principal(runtime, headers_for, PETR)
    guard = build_guard(fresh(runtime), "sales")
    add_masks(runtime, "drug_sales", [ColumnMaskInput("revenue", SUBJECT_USER, PETR, MASK_PARTIAL)])
    compiled = guard.secured(viewer)["drug_sales"]
    state = column_state(compiled, "revenue")
    assert state is ColumnState.MASKED
    plain = guard.run(viewer, "SELECT SUM(revenue) FROM drug_sales")
    assert plain.result is None and plain.error is not None  # SUM over text fails
    safe = guard.run(viewer, f"SELECT SUM({numeric('revenue', state)}) AS s, COUNT(*) AS n FROM drug_sales")
    assert safe.error is None and safe.result is not None
    total, count = safe.result.rows[0]
    assert total is None and count > 0  # every masked value reads as NULL, the rows still count
    absent = guard.run(viewer, f"SELECT SUM({numeric('margin', column_state(compiled, 'margin'))}) FROM drug_sales")
    assert absent.error is None and absent.result is not None and absent.result.rows == [(None,)]


# --- supply --------------------------------------------------------------------

def test_anna_unit_cost_masked_not_averaged(supply: TestClient, headers_for: Headers) -> None:
    html = supply.get("/", headers=headers_for(ANNA)).text
    section = html.split('id="unit-cost"', 1)[1].split("</section>", 1)[0]
    assert "pill mask" in section and "no average is computed" in section
    assert "avg_unit_cost" not in section
    assert "****" in section
    recent = html.split("Recent", 1)[1]
    assert "<th>contract_ref</th>" not in recent


def test_eva_supply_own_company_hashed_contracts(runtime: Runtime, supply: TestClient, headers_for: Headers) -> None:
    html = supply.get("/", headers=headers_for(EVA)).text
    assert "chart-cost" in html  # unit_cost clear for planners → average charted
    guard = build_guard(runtime, "supply")
    result = guard.run(principal(runtime, headers_for, EVA), "SELECT DISTINCT company FROM supply_chain")
    assert result.result.rows == [("PHARMA_A",)]
    refs = guard.run(principal(runtime, headers_for, EVA), "SELECT contract_ref FROM supply_chain LIMIT 5")
    assert all(format_cell(r[0]).kind is CellKind.HASH for r in refs.result.rows)


@pytest.mark.parametrize(
    ("email", "margin_hidden"),
    [(PETR, True), (LUCIE, False), (JAN, True), (MARTIN, True)],
)
def test_sales_page_renders_for_each_persona(sales: TestClient, headers_for: Headers, email: str, margin_hidden: bool) -> None:
    response = sales.get("/", headers=headers_for(email))
    assert response.status_code == 200
    html = response.text
    assert "Query failed" not in html and "flash err" not in html
    assert ('<span class="pill deny kpi-pill">hidden</span>' in margin_tile(html)) is margin_hidden
    latest = html.split("Latest", 1)[1]
    assert ("<th>margin</th>" in latest) is not margin_hidden
    assert "<th>customer_id</th>" in latest  # masked for everyone, never absent


@pytest.mark.parametrize(
    ("email", "cost_state"),
    [(LUCIE, ColumnState.CLEAR), (EVA, ColumnState.CLEAR), (ANNA, ColumnState.MASKED)],
)
def test_supply_page_renders_for_each_persona(supply: TestClient, headers_for: Headers, email: str, cost_state: ColumnState) -> None:
    response = supply.get("/", headers=headers_for(email))
    assert response.status_code == 200
    html = response.text
    assert "Query failed" not in html and "flash err" not in html
    cost = section(html, 'id="unit-cost"')
    assert ("chart-cost" in cost) is (cost_state is ColumnState.CLEAR)
    assert ('<span class="pill mask">masked</span>' in cost) is (cost_state is ColumnState.MASKED)


def test_jan_supply_page_shows_unit_cost_hidden(runtime: Runtime, headers_for: Headers) -> None:
    """Jan (partner) gets public PHARMA_B shipments with unit_cost and contract_ref hidden once let into the app."""
    live = fresh(runtime)
    supply = TestClient(create_supply(live))
    assert supply.get("/", headers=headers_for(JAN)).status_code == 403
    runtime.store.grant("supply", SUBJECT_USER, JAN, "test")
    html = supply.get("/", headers=headers_for(JAN)).text
    assert "Query failed" not in html and "flash err" not in html
    cost = section(html, 'id="unit-cost"')
    assert '<span class="pill deny">hidden</span>' in cost and "not part of your view of supply_chain" in cost
    assert "chart-cost" not in cost
    recent = html.split("Recent", 1)[1]
    assert "<th>unit_cost</th>" not in recent and "<th>contract_ref</th>" not in recent
    guard = build_guard(live, "supply")
    rows = guard.run(principal(runtime, headers_for, JAN), "SELECT DISTINCT company, visibility FROM supply_chain")
    assert rows.result is not None and rows.result.rows == [("PHARMA_B", "public")]


def test_absent_column_query_fails_cleanly(runtime: Runtime, supply: TestClient, headers_for: Headers) -> None:
    response = supply.post("/query", data={"sql": "SELECT contract_ref FROM supply_chain"}, headers=headers_for(ANNA))
    assert response.status_code == 200
    assert "Query failed" in response.text and 'role="alert"' in response.text
    last = [e for e in audit_events(runtime) if e["event"] == "query"][-1]
    assert last["reason"] == "execution_error" and last["row_count"] is None


# --- ad-hoc SQL -----------------------------------------------------------------

REFUSED = [
    ("SELECT current_query() FROM drug_sales", "current_query"),
    ("SELECT * FROM read_csv('/etc/passwd')", "not allowed"),
    ("SELECT * FROM information_schema.tables", "Qualified names are not allowed"),
    ("SELECT 1; DROP TABLE drug_sales", "Exactly one statement"),
]


@pytest.mark.parametrize(("sql", "message"), REFUSED)
def test_attacks_are_refused(runtime: Runtime, sales: TestClient, headers_for: Headers, sql: str, message: str) -> None:
    response = sales.post("/query", data={"sql": sql}, headers=headers_for(PETR))
    assert response.status_code == 200
    assert message in response.text
    assert "How this was secured" not in response.text  # rejected before compile: nothing executed
    last = [e for e in audit_events(runtime) if e["event"] == "query"][-1]
    assert (last["allowed"], last["reason"], last["row_count"], last["datasets"]) == (False, "rejected", None, [])
    with warehouse(runtime) as con:
        assert con.execute("SELECT COUNT(*) FROM drug_sales").fetchone()[0] == runtime.settings.seed.drug_sales_rows


def test_example_buttons_cover_attacks() -> None:
    for examples in (SALES_EXAMPLES, SUPPLY_EXAMPLES):
        assert 6 <= len(examples) <= 8
        assert sum(e.expectation == "refused" for e in examples) >= 4


@pytest.mark.parametrize("app_id", ["sales", "supply"])
def test_refused_examples_really_are_refused(runtime: Runtime, headers_for: Headers, app_id: str) -> None:
    examples = SALES_EXAMPLES if app_id == "sales" else SUPPLY_EXAMPLES
    guard = build_guard(runtime, app_id)
    viewer = principal(runtime, headers_for, LUCIE)  # sees every dataset, so a refusal is not a visibility artefact
    for example in examples:
        result = guard.run(viewer, example.sql)
        if example.expectation == "refused":
            assert result.error and result.result is None and result.display_sql is None, example.label
        else:
            assert result.error is None, (example.label, result.error)


def test_union_attack_only_reads_secured_rows(runtime: Runtime, sales: TestClient, headers_for: Headers) -> None:
    sql = "SELECT * FROM (SELECT * FROM drug_sales) x UNION ALL SELECT * FROM drug_sales"
    response = sales.post("/query", data={"sql": sql}, headers=headers_for(PETR))
    assert "How this was secured" in response.text
    guard = build_guard(runtime, "sales")
    result = guard.run(principal(runtime, headers_for, PETR), f"SELECT DISTINCT company, visibility FROM ({sql}) u")
    assert ("PHARMA_B", "private") not in result.result.rows and ("PHARMA_C", "private") not in result.result.rows


def test_query_page_shows_result_and_secured_sql(sales: TestClient, headers_for: Headers) -> None:
    response = sales.post("/query", data={"sql": "SELECT company, COUNT(*) AS n FROM drug_sales GROUP BY company"}, headers=headers_for(PETR))
    assert "<th>company</th>" in response.text and "PHARMA_A" in response.text
    assert "How this was secured" in response.text
    assert "test-mask-salt" not in response.text


def test_empty_query_is_handled(sales: TestClient, headers_for: Headers) -> None:
    response = sales.post("/query", data={"sql": "   "}, headers=headers_for(PETR))
    assert response.status_code == 200 and "Type a query first" in response.text


# --- audit ------------------------------------------------------------------

def test_audit_has_query_events_without_row_values(runtime: Runtime, sales: TestClient, supply: TestClient, headers_for: Headers) -> None:
    for email in (PETR, LUCIE, MARTIN):
        assert sales.get("/", headers=headers_for(email)).status_code == 200
    for email in (ANNA, EVA, LUCIE):
        assert supply.get("/", headers=headers_for(email)).status_code == 200
    sales.post("/query", data={"sql": SALES_EXAMPLES[1].sql}, headers=headers_for(LUCIE))

    events = audit_events(runtime)
    queries = [e for e in events if e["event"] == "query"]
    assert {e["app_id"] for e in queries} == {"sales", "supply"}
    assert all(e["allowed"] for e in queries)

    with warehouse(runtime) as con:
        customers = [r[0] for r in con.execute("SELECT customer_id FROM drug_sales LIMIT 50").fetchall()]
        contracts = [r[0] for r in con.execute("SELECT contract_ref FROM supply_chain LIMIT 50").fetchall()]
        stores = [r[0] for r in con.execute("SELECT name FROM stores").fetchall()]
    text = runtime.settings.path(runtime.settings.audit.path).read_text(encoding="utf-8")
    for value in (*customers, *contracts, *stores, "PHARMA_A", runtime.settings.secrets.mask_salt):
        assert value not in text, value
    # Partial masks (the last 4 chars) of the customers must not appear either.
    assert not re.search(r"\*\*\*\*\d{4}", text)


def test_sales_dashboard_survives_masked_revenue_and_units(runtime: Runtime, headers_for: Headers) -> None:
    """Masking lines on numeric columns must not break the dashboard's aggregates (they become '–')."""
    live = fresh(runtime)
    sales = TestClient(create_sales(live))
    add_masks(runtime, "drug_sales", [
        ColumnMaskInput("revenue", SUBJECT_USER, PETR, MASK_PARTIAL),
        ColumnMaskInput("units", SUBJECT_USER, PETR, MASK_HASH),
    ])
    response = sales.get("/", headers=headers_for(PETR))
    assert response.status_code == 200
    assert "Query failed" not in response.text and "flash err" not in response.text
    kpis = response.text.split('id="kpis"', 1)[1].split("Avg margin", 1)[0]
    assert kpis.count('<div class="big">–</div>') == 2  # revenue and units
    # Lucie is not named by the new lines: her figures stay numbers.
    lucie = sales.get("/", headers=headers_for(LUCIE)).text.split('id="kpis"', 1)[1].split("Avg margin", 1)[0]
    assert '<div class="big">–</div>' not in lucie
