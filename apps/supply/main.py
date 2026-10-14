"""Supply chain dashboard (app_id "supply").

Behind the app gate; every number comes through the data guard from the
`supply_chain` dataset. Column treatment differs per viewer: a planner sees
`contract_ref` hashed, the named admin sees `unit_cost` only partially
("****2.33", which is text) and no `contract_ref` at all, finance sees
everything. Aggregates over `unit_cost` are only computed when it is clear
for the viewer.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse

from apps.dashboard_common import (
    ColumnState,
    Example,
    chart_spec,
    column_state,
    install_dashboard,
    numeric,
    register_query_page,
    run_view,
    visibility_panel,
)
from kbc_access.identity import Principal
from kbc_access.web import Runtime, build_guard, build_runtime

HERE = Path(__file__).parent
APP_ID = "supply"
SHIPMENTS = "supply_chain"

# Presentation choices, not tunables: rows in the recent-shipments table and
# masked unit_cost samples shown in place of an average. Capped by data.max_result_rows.
RECENT_PAGE_SIZE = 25
MASKED_SAMPLES = 5

EXAMPLES = [
    Example("Shipments per supplier and status",
            "SELECT supplier, status, COUNT(*) AS shipments, SUM(quantity) AS quantity\nFROM supply_chain\n"
            "GROUP BY supplier, status\nORDER BY supplier, status",
            "legit"),
    Example("Cost and contract per shipment",
            "SELECT shipment_id, company, supplier, unit_cost, contract_ref\nFROM supply_chain\nORDER BY shipment_id\nLIMIT 20",
            "legit"),
    Example("Delayed shipments by company",
            "SELECT company, COUNT(*) AS delayed, AVG(lead_time_days) AS avg_lead_days\nFROM supply_chain\n"
            "WHERE status = 'delayed'\nGROUP BY company\nORDER BY company",
            "legit"),
    Example("Attack: read the masking salt", "SELECT current_query() FROM supply_chain LIMIT 1", "refused"),
    Example("Attack: read a file", "SELECT * FROM read_csv('/etc/passwd')", "refused"),
    Example("Attack: list the catalog", "SELECT * FROM information_schema.tables", "refused"),
    Example("Attack: stacked DROP", "SELECT 1; DROP TABLE supply_chain", "refused"),
    Example("Attack: CTE shadows the dataset",
            "WITH supply_chain AS (SELECT * FROM supply_chain)\nSELECT * FROM supply_chain",
            "neutralised"),
]


def _unit_cost_note(state: ColumnState) -> str:
    return {
        ColumnState.CLEAR: "",
        ColumnState.MASKED: "unit_cost reaches you masked (text like ****2.33), so no average is computed.",
        ColumnState.ABSENT: "unit_cost is not part of your view of supply_chain.",
    }[state]


def create_app(runtime: Runtime) -> FastAPI:
    app = FastAPI(title=runtime.settings.apps[APP_ID].name)
    access = install_dashboard(app, runtime, app_id=APP_ID, templates_dir=HERE / "templates")
    guard = build_guard(runtime, APP_ID)
    dialect = runtime.settings.data.dialect
    max_rows = runtime.settings.data.max_result_rows
    recent_size = min(RECENT_PAGE_SIZE, max_rows)
    register_query_page(app, access, guard, EXAMPLES)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, principal: Principal = Depends(access.require_with_attributes)) -> HTMLResponse:
        visibility = visibility_panel(guard, principal)
        secured = guard.secured(principal)
        if SHIPMENTS not in secured:
            return access.render(request, "dashboard.html", principal, page="home", visibility=visibility, no_data=True)

        def q(sql: str):  # noqa: ANN202 - local shorthand
            return run_view(guard, principal, sql, dialect)

        # Quantity and lead time are read through `numeric`: they may reach this viewer masked (text) or not at all.
        quantity = numeric("quantity", column_state(secured[SHIPMENTS], "quantity"))
        lead_time = numeric("lead_time_days", column_state(secured[SHIPMENTS], "lead_time_days"))
        kpi_view = q(
            f"SELECT COUNT(*) AS shipments, SUM({quantity}) AS quantity, "
            "AVG(CASE WHEN status = 'delayed' THEN 1.0 ELSE 0.0 END) AS delayed_share, "
            f"AVG({lead_time}) AS avg_lead_days FROM supply_chain"
        )

        by_supplier = q(f"SELECT supplier, COUNT(*) AS shipments, SUM({quantity}) AS quantity FROM supply_chain "
                        "GROUP BY supplier ORDER BY shipments DESC, supplier")
        supplier_chart = chart_spec("bar", [r["supplier"] for r in by_supplier.records()],
                                    {"shipments": [r["shipments"] for r in by_supplier.records()]})

        by_status = q(f"SELECT status, COUNT(*) AS shipments, SUM({quantity}) AS quantity FROM supply_chain "
                      "GROUP BY status ORDER BY shipments DESC, status")
        status_chart = chart_spec("doughnut", [r["status"] for r in by_status.records()],
                                  {"shipments": [r["shipments"] for r in by_status.records()]})

        cost_state = column_state(secured[SHIPMENTS], "unit_cost")
        cost_view = None
        cost_samples = None
        if cost_state is ColumnState.CLEAR:
            cost_view = q("SELECT supplier, AVG(unit_cost) AS avg_unit_cost, MIN(unit_cost) AS min_unit_cost, "
                          "MAX(unit_cost) AS max_unit_cost, COUNT(unit_cost) AS rows_with_cost, COUNT(*) AS shipments "
                          "FROM supply_chain GROUP BY supplier ORDER BY avg_unit_cost DESC")
        elif cost_state is ColumnState.MASKED:
            cost_samples = q(f"SELECT shipment_id, supplier, unit_cost FROM supply_chain ORDER BY shipment_id "
                             f"LIMIT {min(MASKED_SAMPLES, max_rows)}")
        cost_chart = None
        if cost_view is not None and cost_view.ok:
            cost_chart = chart_spec("bar", [r["supplier"] for r in cost_view.records()],
                                    {"avg unit_cost": [r["avg_unit_cost"] for r in cost_view.records()]})

        monthly = q(f"SELECT strftime(ship_date, '%Y-%m') AS month, COUNT(*) AS shipments, SUM({quantity}) AS quantity "
                    "FROM supply_chain GROUP BY month ORDER BY month")
        monthly_chart = chart_spec("line", [r["month"] for r in monthly.records()],
                                   {"quantity": [r["quantity"] for r in monthly.records()]})

        recent = q(f"SELECT * FROM supply_chain ORDER BY ship_date DESC, shipment_id DESC LIMIT {recent_size}")

        return access.render(
            request, "dashboard.html", principal,
            page="home", visibility=visibility, no_data=False,
            kpi_view=kpi_view, kpis=kpi_view.first(),
            by_supplier=by_supplier, supplier_chart=supplier_chart,
            by_status=by_status, status_chart=status_chart,
            cost_state=cost_state.value, cost_note=_unit_cost_note(cost_state),
            cost_view=cost_view, cost_chart=cost_chart, cost_samples=cost_samples,
            monthly=monthly, monthly_chart=monthly_chart,
            recent=recent, recent_size=recent_size,
        )

    return app


def create_default() -> FastAPI:
    """uvicorn --factory target (config/local.toml [apps.supply].factory)."""
    return create_app(build_runtime())
