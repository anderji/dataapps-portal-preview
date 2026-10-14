"""Shared dashboard helpers.

Nothing here touches the warehouse directly: every value comes from
`DataGuard.run`, which rewrites the SQL against the viewer's secured relations.
The helpers only decide how to *show* what came back — masked strings, hashes
and NULLs from hidden columns are rendered honestly instead of being passed
off as real numbers.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

import sqlglot
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlglot.errors import SqlglotError

from kbc_access.compiler import PARTIAL_MASK_PREFIX, CompiledDataset
from kbc_access.guard import DataGuard, GuardedResult
from kbc_access.identity import Principal
from kbc_access.security_model import CLEAR, HIDDEN, MASKED_TREATMENTS
from kbc_access.web import Access, Runtime, install

COMMON_TEMPLATES = Path(__file__).parent / "templates"

# Presentation only: how many leading characters of a salted hash are shown
# in a table cell (the full value is in the cell's title attribute).
HASH_DISPLAY_CHARS = 10
# Length of a hex SHA-256 digest, which is what mask_hash produces.
SHA256_HEX_LENGTH = 64
_HEX_DIGITS = frozenset(string.hexdigits)


def install_dashboard(app: FastAPI, runtime: Runtime, *, app_id: str, templates_dir: Path) -> Access:
    """`install()` plus the shared dashboard templates and filters.

    `install()` accepts a single app templates dir; the shared partials live in
    this package, so they are appended to the Jinja loader chain afterwards
    (app dir → kbc_access shared layout → dashboard partials).
    """
    access = install(app, runtime, app_id=app_id, templates_dir=templates_dir)
    env = access.templates.env
    env.loader = ChoiceLoader([env.loader, FileSystemLoader(str(COMMON_TEMPLATES))])  # type: ignore[list-item]
    env.filters["num"] = format_number
    env.filters["pct"] = format_percent
    return access


def format_number(value: object, decimals: int = 0) -> str:
    if value is None:
        return "–"
    if isinstance(value, (int, float)):
        return f"{value:,.{decimals}f}".replace(",", " ")
    return str(value)


def format_percent(value: object, decimals: int = 1) -> str:
    if not isinstance(value, (int, float)):
        return "–"
    return f"{value * 100:.{decimals}f} %"


class CellKind(StrEnum):
    VALUE = "value"
    NUMBER = "number"
    NULL = "null"
    PARTIAL = "partial"
    HASH = "hash"


@dataclass(frozen=True)
class Cell:
    text: str
    kind: CellKind
    # Full value for shortened cells (hashes), shown on hover.
    title: str | None = None


def _is_hash(value: str) -> bool:
    return len(value) == SHA256_HEX_LENGTH and all(c in _HEX_DIGITS for c in value)


def format_cell(value: object) -> Cell:
    """Render one value as it came back from the guard, masked or not."""
    if value is None:
        return Cell("hidden", CellKind.NULL, "NULL: no value is visible to you on this row")
    if isinstance(value, bool):
        return Cell(str(value).lower(), CellKind.VALUE)
    if isinstance(value, int):
        return Cell(format_number(value), CellKind.NUMBER)
    if isinstance(value, float):
        return Cell(format_number(value, 2), CellKind.NUMBER)
    if isinstance(value, (date, datetime)):
        return Cell(value.isoformat(), CellKind.VALUE)
    text = str(value)
    if text.startswith(PARTIAL_MASK_PREFIX):
        return Cell(text, CellKind.PARTIAL, "Partially masked by policy")
    if _is_hash(text):
        return Cell(text[:HASH_DISPLAY_CHARS] + "…", CellKind.HASH, text)
    return Cell(text, CellKind.VALUE)


@dataclass(frozen=True)
class ResultView:
    """A guarded query result prepared for a template."""

    columns: list[str] = field(default_factory=list)
    rows: list[list[Cell]] = field(default_factory=list)
    # Raw values, for charts and KPIs computed from aggregates.
    raw: list[tuple[object, ...]] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None
    display_sql: str | None = None
    datasets: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.error is None

    def first(self) -> dict[str, object]:
        """First row as a dict (for single-row aggregate queries)."""
        return dict(zip(self.columns, self.raw[0])) if self.raw else {}

    def records(self) -> list[dict[str, object]]:
        return [dict(zip(self.columns, row)) for row in self.raw]


def pretty_sql(sql: str, dialect: str) -> str:
    try:
        return sqlglot.transpile(sql, read=dialect, write=dialect, pretty=True)[0]
    except SqlglotError:
        # Display only: the compiled SQL is still correct, just not re-indented.
        return sql


def to_view(guarded: GuardedResult, dialect: str) -> ResultView:
    display = pretty_sql(guarded.display_sql, dialect) if guarded.display_sql else None
    if guarded.result is None:
        return ResultView(error=guarded.error, display_sql=display, datasets=guarded.datasets)
    result = guarded.result
    return ResultView(
        columns=list(result.columns),
        rows=[[format_cell(v) for v in row] for row in result.rows],
        raw=list(result.rows),
        truncated=result.truncated,
        display_sql=display,
        datasets=guarded.datasets,
    )


def run_view(guard: DataGuard, principal: Principal, sql: str, dialect: str) -> ResultView:
    return to_view(guard.run(principal, sql), dialect)


class ColumnState(StrEnum):
    """What a viewer gets for one column. Masking is decided per viewer for the whole column."""

    # Clear on every visible row: safe to aggregate.
    CLEAR = "clear"
    # Masked (partial or hash): values are text, never aggregate.
    MASKED = "masked"
    # Not in the viewer's relation at all: referencing it fails.
    ABSENT = "absent"


def column_state(compiled: CompiledDataset, column: str) -> ColumnState:
    """The state of `column` for the viewer, from the treatment its first applying masking line gave it."""
    treatment = compiled.column_treatments.get(column, CLEAR)
    if treatment == HIDDEN:
        return ColumnState.ABSENT
    if treatment in MASKED_TREATMENTS:
        return ColumnState.MASKED
    return ColumnState.CLEAR


def numeric(column: str, state: ColumnState) -> str:
    """SQL that reads `column` as a number whatever the viewer's treatment of it is.

    A masked value is text (`****1234`, a hash), so a plain SUM or AVG would fail. Masked values
    become NULL here. A column the viewer may not see at all is NULL.
    """
    if state is ColumnState.ABSENT:
        return "CAST(NULL AS DOUBLE)"
    if state is ColumnState.MASKED:
        return f"TRY_CAST({column} AS DOUBLE)"
    return column


def _pill_class(treatment: str) -> str:
    return "allow" if treatment == CLEAR else "mask"


def visibility_panel(guard: DataGuard, principal: Principal) -> dict[str, object]:
    """Context for the "What you can see" partial."""
    secured = guard.secured(principal)
    datasets = [
        {
            "name": name,
            "description": guard.policy.datasets[name].description,
            "grants": list(compiled.grants_fired),
            "columns": [(col, treatment, _pill_class(treatment)) for col, treatment in compiled.visible_columns],
            "absent": list(compiled.absent_columns),
        }
        for name, compiled in secured.items()
    ]
    hidden = [name for name in guard.policy.datasets if name not in secured]
    return {
        "datasets": datasets,
        "hidden": hidden,
        "attributes": sorted(principal.attributes.items()),
    }


@dataclass(frozen=True)
class Example:
    label: str
    sql: str
    # "legit" runs normally; "refused" must be rejected by the rewriter;
    # "neutralised" runs, but only ever against the secured relations.
    expectation: str


def register_query_page(app: FastAPI, access: Access, guard: DataGuard, examples: list[Example]) -> None:
    """GET/POST /query: ad-hoc SQL through the guard, with one-click examples."""
    dialect = access.runtime.settings.data.dialect

    def page(request: Request, principal: Principal, sql: str, view: ResultView | None) -> HTMLResponse:
        return access.render(
            request, "dash_query.html", principal,
            page="query", sql=sql, view=view, examples=examples,
            visibility=visibility_panel(guard, principal),
            max_rows=access.runtime.settings.data.max_result_rows,
        )

    @app.get("/query", response_class=HTMLResponse)
    def query_form(request: Request, principal: Principal = Depends(access.require_with_attributes)) -> HTMLResponse:
        return page(request, principal, examples[0].sql if examples else "", None)

    @app.post("/query", response_class=HTMLResponse)
    def query_run(
        request: Request,
        sql: str = Form(""),
        principal: Principal = Depends(access.require_with_attributes),
    ) -> HTMLResponse:
        if not sql.strip():
            return page(request, principal, sql, ResultView(error="Type a query first."))
        return page(request, principal, sql, run_view(guard, principal, sql, dialect))


def chart_spec(kind: str, labels: list[object], series: dict[str, list[object]], *, horizontal: bool = False, stacked: bool = False) -> dict[str, object]:
    """Data for one Chart.js chart; colors are resolved in the browser from CSS variables."""
    return {
        "type": kind,
        "labels": [str(label) for label in labels],
        "series": [{"label": name, "data": [v if isinstance(v, (int, float)) else None for v in data]} for name, data in series.items()],
        "horizontal": horizontal,
        "stacked": stacked,
    }
