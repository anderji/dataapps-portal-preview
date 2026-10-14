"""Drug sales dashboard (app_id "sales").

Sits behind the app gate and reads every number through the data guard, so
two viewers see different rows (public vs. own-company private sales) and
different columns (margin, customer_id) on the same page. The page never
builds SQL against a physical table: it writes plain SQL against the dataset
names `drug_sales` and `stores`, and the guard rewrites it per viewer.
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
APP_ID = "sales"
SALES = "drug_sales"

# Presentation choices, not tunables: how many sample rows and top stores the
# page shows. The sample size is additionally capped by data.max_result_rows.
SAMPLE_PAGE_SIZE = 25
TOP_STORES = 10

EXAMPLES = [
    Example("Rows per company and visibility",
            "SELECT company, visibility, COUNT(*) AS sales, SUM(revenue) AS revenue\nFROM drug_sales\nGROUP BY company, visibility\nORDER BY company, visibility",
            "legit"),
    Example("Margin and customer per sale",
            "SELECT sale_id, company, visibility, margin, customer_id\nFROM drug_sales\nORDER BY sale_id\nLIMIT 20",
            "legit"),
    Example("Revenue per region (join of two secured datasets)",
            "SELECT s.region, SUM(d.revenue) AS revenue\nFROM drug_sales d JOIN stores s ON d.store_id = s.store_id\n"
            "GROUP BY s.region\nORDER BY revenue DESC",
            "legit"),
    Example("Attack: read the masking salt", "SELECT current_query() FROM drug_sales LIMIT 1", "refused"),
    Example("Attack: read a file", "SELECT * FROM read_csv('/etc/passwd')", "refused"),
    Example("Attack: list the catalog", "SELECT * FROM information_schema.tables", "refused"),
    Example("Attack: stacked DROP", "SELECT 1; DROP TABLE drug_sales", "refused"),
    Example("Attack: CTE shadows the dataset",
            "WITH drug_sales AS (SELECT * FROM drug_sales)\nSELECT * FROM drug_sales",
            "neutralised"),
]


def _margin_kpi(state: ColumnState, kpis: dict[str, object]) -> dict[str, object]:
    """Average margin tile that never shows a number computed over hidden rows as if it were complete."""
    if state is ColumnState.ABSENT:
        return {"value": "hidden", "state": "deny", "note": "margin is not part of your view of drug_sales"}
    if state is ColumnState.MASKED:
        return {"value": "masked", "state": "mask", "note": "margin reaches you masked, so it cannot be averaged"}
    sales = kpis.get("sales") or 0
    with_margin = kpis.get("margin_rows") or 0
    if not with_margin:
        return {"value": "hidden", "state": "deny", "note": f"margin is hidden on all {sales} sales you can see"}
    rate = kpis.get("margin_rate")
    value = f"{rate * 100:.1f} %" if isinstance(rate, float) else "–"
    if with_margin < sales:
        return {"value": value, "state": "mask", "partly": True,
                "note": f"partly hidden: computed over {with_margin} of {sales} sales where margin is visible to you"}
    return {"value": value, "state": None, "note": f"over all {sales} sales"}


def create_app(runtime: Runtime) -> FastAPI:
    app = FastAPI(title=runtime.settings.apps[APP_ID].name)
    access = install_dashboard(app, runtime, app_id=APP_ID, templates_dir=HERE / "templates")
    guard = build_guard(runtime, APP_ID)
    dialect = runtime.settings.data.dialect
    sample_size = min(SAMPLE_PAGE_SIZE, runtime.settings.data.max_result_rows)
    register_query_page(app, access, guard, EXAMPLES)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, principal: Principal = Depends(access.require_with_attributes)) -> HTMLResponse:
        visibility = visibility_panel(guard, principal)
        secured = guard.secured(principal)
        if SALES not in secured:
            return access.render(request, "dashboard.html", principal, page="home", visibility=visibility, no_data=True)

        def q(sql: str):  # noqa: ANN202 - local shorthand
            return run_view(guard, principal, sql, dialect)

        margin_state = column_state(secured[SALES], "margin")
        # Revenue and units are read through `numeric`: they may reach this viewer masked (text) or not at all.
        revenue = numeric("revenue", column_state(secured[SALES], "revenue"))
        units = numeric("units", column_state(secured[SALES], "units"))
        margin_sql = ""
        if margin_state is ColumnState.CLEAR:
            # Margin rate over exactly the rows whose margin is visible, and how many those are.
            margin_sql = (", COUNT(margin) AS margin_rows, "
                          f"SUM(margin) / SUM(CASE WHEN margin IS NOT NULL THEN {revenue} END) AS margin_rate")
        kpi_view = q(f"SELECT SUM({revenue}) AS revenue, SUM({units}) AS units, COUNT(*) AS sales{margin_sql} FROM drug_sales")
        kpis = kpi_view.first()

        by_company = q(f"SELECT company, visibility, SUM({revenue}) AS revenue, COUNT(*) AS sales FROM drug_sales "
                       "GROUP BY company, visibility ORDER BY company, visibility")
        companies = sorted({r["company"] for r in by_company.records()})
        company_chart = chart_spec(
            "bar", companies,
            {vis: [next((r["revenue"] for r in by_company.records() if r["company"] == c and r["visibility"] == vis), 0)
                   for c in companies]
             for vis in sorted({r["visibility"] for r in by_company.records()})},
            stacked=True,
        )

        by_category = q(f"SELECT category, SUM({revenue}) AS revenue, SUM({units}) AS units FROM drug_sales "
                        "GROUP BY category ORDER BY revenue DESC")
        category_chart = chart_spec("bar", [r["category"] for r in by_category.records()],
                                    {"revenue": [r["revenue"] for r in by_category.records()]}, horizontal=True)

        monthly = q(f"SELECT strftime(sale_date, '%Y-%m') AS month, SUM({revenue}) AS revenue, COUNT(*) AS sales "
                    "FROM drug_sales GROUP BY month ORDER BY month")
        monthly_chart = chart_spec("line", [r["month"] for r in monthly.records()],
                                   {"revenue": [r["revenue"] for r in monthly.records()]})

        # A join of two secured relations. The CTEs rename columns so the join
        # needs no table aliases on dataset references.
        top_stores = q(
            "WITH s AS (SELECT store_id AS s_store_id, name, city, company AS store_company FROM stores), "
            f"d AS (SELECT store_id, {revenue} AS revenue, {units} AS units FROM drug_sales) "
            "SELECT s.s_store_id AS store_id, s.name, s.city, s.store_company AS company, "
            "SUM(d.revenue) AS revenue, SUM(d.units) AS units, COUNT(*) AS sales "
            "FROM d JOIN s ON d.store_id = s.s_store_id "
            "GROUP BY s.s_store_id, s.name, s.city, s.store_company "
            f"ORDER BY revenue DESC LIMIT {TOP_STORES}"
        )

        sample = q(f"SELECT * FROM drug_sales ORDER BY sale_date DESC, sale_id DESC LIMIT {sample_size}")

        return access.render(
            request, "dashboard.html", principal,
            page="home", visibility=visibility, no_data=False,
            kpi_view=kpi_view, kpis=kpis, margin=_margin_kpi(margin_state, kpis),
            by_company=by_company, company_chart=company_chart,
            by_category=by_category, category_chart=category_chart,
            monthly=monthly, monthly_chart=monthly_chart,
            top_stores=top_stores, sample=sample, sample_size=sample_size,
        )

    return app


def create_default() -> FastAPI:
    """uvicorn --factory target (config/local.toml [apps.sales].factory)."""
    return create_app(build_runtime())
