"""Access admin — the dataset security editor API (row access lines, column masks, "Test as").

Routes (all under the admin's gated, CSRF-protected /api router):

* `GET  /datasets`                         the catalogue with line counts
* `GET  /datasets/{name}`                  one dataset's lines and masks
* `PUT  /datasets/{name}`                  replace its lines and masks in one unit
* `POST /datasets/{name}/load-examples`    copy the policy file's examples into an empty dataset
* `GET  /datasets/{name}/values/{column}`  distinct values from the data (for the value pickers)
* `POST /datasets/{name}/test`             what a user would get: lines, columns, counts, preview

The catalogue (datasets, columns, declared column types, examples) comes from the
policy files, which the admin always has. The warehouse is optional: the deployed
admin gets `[data]` only once the demo data exists. Without it everything works
from the catalogue, `data_available` is false, `/values` answers 503 and `/test`
explains lines and columns without counts or preview. Column types are the
declared catalogue types in both modes; `load_policy` (dashboards) refuses a
catalogue whose types differ from the warehouse's, so they cannot drift.

Rule text never reaches SQL: filters are structured, checked against the catalogue
and compiled through sqlglot's AST like in the dashboards.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlglot import exp

from kbc_access.backends.duckdb_backend import DuckDBBackend
from kbc_access.compiler import DatasetPlan, LinePlan, Unresolvable, bind_condition, compile_plan, hides, plan_dataset
from kbc_access.identity import Principal
from kbc_access.policy import Dataset, PolicyError, load_catalog, resolve_examples, validate_filter, validate_mask
from kbc_access.security_model import (
    KIND_HIDE,
    KIND_SHOW,
    KIND_TEXT,
    OP_IS,
    OP_ONE_OF,
    SUBJECT_EVERYONE,
    FilterError,
    parse_filter,
)
from kbc_access.store import (
    EMAIL_PATTERN,
    FILTER_VALUE_MAX_CHARS,
    ColumnMask,
    ColumnMaskInput,
    DatasetSecurity,
    RowAccessRule,
    RowAccessRuleInput,
    StoreError,
)
from kbc_access.web import Access, Runtime, build_backend

logger = logging.getLogger(__name__)

# How many distinct values `/values` returns at most.
VALUES_LIMIT = 50
# How many rows the "Test as" preview shows at most.
PREVIEW_ROW_LIMIT = 20
# How many existing values a zero-row warning lists at most.
WARNING_VALUES_LIMIT = 10
# The admin compiles in the dialect its own warehouse copy speaks (DuckDB, read-only).
ADMIN_DIALECT = DuckDBBackend.dialect


class SubjectIn(BaseModel):
    type: str
    id: str = ""


class RowLineIn(BaseModel):
    rule_id: str | None = None
    kind: str
    subject: SubjectIn
    filter: dict[str, object]


class MaskLineIn(BaseModel):
    mask_id: str | None = None
    subject: SubjectIn
    treatment: str


class DatasetSecurityIn(BaseModel):
    rows: list[RowLineIn] = []
    masks: dict[str, list[MaskLineIn]] = {}


class TestIn(BaseModel):
    email: str
    groups: list[str] = []


def _fail(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _json_value(value: object) -> object:
    """A warehouse value as JSON: dates and times in ISO form, decimals and anything else as text."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, date | datetime | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


@dataclass
class _Warehouse:
    """The admin's read-only warehouse copy, opened on first use (None while it does not exist)."""

    runtime: Runtime
    backend: DuckDBBackend | None = None

    def get(self) -> DuckDBBackend | None:
        if self.runtime.settings.data is None:
            return None
        if self.backend is None:
            try:
                self.backend = build_backend(self.runtime)
            except FileNotFoundError as exc:
                logger.warning("admin warehouse not available: %s", exc)
                return None
        return self.backend


def register_dataset_routes(api: APIRouter, runtime: Runtime, access: Access) -> None:
    settings = runtime.settings
    store = runtime.store
    audit = runtime.audit
    catalog: dict[str, Dataset] = (
        load_catalog([settings.path(f) for f in settings.policy.files]) if settings.policy else {}
    )
    warehouse = _Warehouse(runtime)

    # ---- helpers ---------------------------------------------------------------------

    def _known_dataset(name: str) -> Dataset:
        dataset = catalog.get(name.strip().lower())
        if dataset is None:
            raise _fail(404, f"Unknown dataset '{name}'.")
        return dataset

    def _labels() -> Callable[[str, str], str]:
        users = {u.email: u.display_name or u.email for u in store.list_users()}
        groups = {g.group_id: g.name for g in store.list_groups()}
        local_groups = {g.local_group_id: g.name for g in store.list_local_groups()}

        def label(subject_type: str, subject_id: str) -> str:
            if subject_type == SUBJECT_EVERYONE:
                return "Everyone"
            table = {"user": users, "group": groups, "local_group": local_groups}.get(subject_type, {})
            return table.get(subject_id, subject_id)

        return label

    def _subject(subject_type: str, subject_id: str, label: Callable[[str, str], str]) -> dict[str, str]:
        return {"type": subject_type, "id": subject_id, "label": label(subject_type, subject_id)}

    def _line(rule: RowAccessRule, label: Callable[[str, str], str]) -> dict[str, object]:
        return {
            "rule_id": rule.rule_id,
            "kind": rule.kind,
            "subject": _subject(rule.subject_type, rule.subject_id, label),
            "filter": rule.filter.to_json() if rule.filter is not None else None,
        }

    def _mask(mask: ColumnMask, label: Callable[[str, str], str]) -> dict[str, object]:
        return {"mask_id": mask.mask_id, "subject": _subject(mask.subject_type, mask.subject_id, label),
                "treatment": mask.treatment}

    def _columns(dataset: Dataset) -> list[dict[str, str]]:
        return [{"name": c, "type": dataset.column_kinds[c]} for c in dataset.columns]

    def _detail(dataset: Dataset, security: DatasetSecurity) -> dict[str, object]:
        label = _labels()
        masks: dict[str, list[dict[str, object]]] = {}
        for mask in security.masks:
            masks.setdefault(mask.column_name, []).append(_mask(mask, label))
        return {
            "dataset": {"name": dataset.name, "description": dataset.description, "columns": _columns(dataset)},
            "rows": [_line(rule, label) for rule in security.rows],
            "masks": masks,
            "attributes": sorted({key for _, key, _ in store.list_attributes()}),
            "has_examples": dataset.has_examples,
            "data_available": warehouse.get() is not None,
        }

    def _count(backend: DuckDBBackend, relation: exp.Expression) -> int:
        query = exp.select(exp.Count(this=exp.Star())).from_(exp.paren(relation.copy(), copy=False).as_("_c"))
        return int(backend.execute(query.sql(dialect=ADMIN_DIALECT)).rows[0][0])

    def _count_where(backend: DuckDBBackend, dataset: Dataset, predicate: exp.Expression) -> int:
        query = exp.select(exp.Count(this=exp.Star())).from_(exp.to_table(dataset.source)).where(predicate.copy())
        return int(backend.execute(query.sql(dialect=ADMIN_DIALECT)).rows[0][0])

    def _distinct(backend: DuckDBBackend, dataset: Dataset, column: str, limit: int, contains: str = "") -> tuple[list[str], bool]:
        as_text = exp.cast(exp.column(column), exp.DataType.build("VARCHAR"))
        query = exp.select(as_text.as_("v")).distinct().from_(exp.to_table(dataset.source)).where(
            exp.Is(this=exp.column(column), expression=exp.Null()).not_()
        )
        if contains:
            needle = exp.Lower(this=exp.Literal.string(contains))
            query = query.where(exp.GT(this=exp.StrPosition(this=exp.Lower(this=as_text.copy()), substr=needle),
                                       expression=exp.Literal.number(0)))
        query = query.order_by("v").limit(limit + 1)
        rows = backend.execute(query.sql(dialect=ADMIN_DIALECT)).rows
        return [str(r[0]) for r in rows[:limit]], len(rows) > limit

    def _zero_row_warning(backend: DuckDBBackend, dataset: Dataset, line: LinePlan, principal: Principal) -> str:
        row_filter = line.rule.filter
        if row_filter is None or row_filter.all_rows:
            return "The dataset has no rows."
        for condition in row_filter.conditions:
            try:
                predicate = bind_condition(dataset, condition, principal)
            except Unresolvable:
                continue
            if _count_where(backend, dataset, predicate) == 0:
                text = predicate.sql(dialect=ADMIN_DIALECT)
                if condition.op in (OP_IS, OP_ONE_OF) and dataset.column_kinds.get(condition.column) == KIND_TEXT:
                    values, more = _distinct(backend, dataset, condition.column, WARNING_VALUES_LIMIT)
                    listed = ", ".join(values) + (", ..." if more else "")
                    return f"No row has {text} (values: {listed})"
                return f"No row has {text}"
        return "No row matches all conditions of this line together."

    # ---- routes ------------------------------------------------------------------------

    @api.get("/datasets")
    def list_datasets() -> dict[str, object]:
        rules = store.list_row_access_rules()
        masks = store.list_column_masks()
        return {
            "datasets": [
                {
                    "name": d.name,
                    "description": d.description,
                    "columns": _columns(d),
                    "show_lines": sum(1 for r in rules if r.dataset == d.name and r.kind == KIND_SHOW),
                    "hide_lines": sum(1 for r in rules if r.dataset == d.name and r.kind == KIND_HIDE),
                    "masked_columns": sorted({m.column_name for m in masks if m.dataset == d.name}),
                    "has_examples": d.has_examples,
                }
                for d in catalog.values()
            ],
            "data_available": warehouse.get() is not None,
        }

    @api.get("/datasets/{name}")
    def get_dataset(name: str) -> dict[str, object]:
        dataset = _known_dataset(name)
        return _detail(dataset, store.dataset_security(dataset.name))

    @api.put("/datasets/{name}")
    def save_dataset(name: str, body: DatasetSecurityIn, principal: Principal = Depends(access.require)) -> dict[str, object]:
        dataset = _known_dataset(name)
        rows: list[RowAccessRuleInput] = []
        for number, line in enumerate(body.rows, start=1):
            where = f"Row line {number}"
            try:
                validate_filter(dataset, parse_filter(line.filter))
            except (FilterError, PolicyError) as exc:
                raise _fail(400, f"{where}: {exc}") from exc
            rows.append(RowAccessRuleInput(line.kind, line.subject.type, line.subject.id, line.filter, line.rule_id))
        masks: list[ColumnMaskInput] = []
        for column, lines in body.masks.items():
            for number, mask in enumerate(lines, start=1):
                where = f"Mask on {column.strip().lower()}, line {number}"
                try:
                    validate_mask(dataset, column, mask.treatment)
                except PolicyError as exc:
                    raise _fail(400, f"{where}: {exc}") from exc
                masks.append(ColumnMaskInput(column, mask.subject.type, mask.subject.id, mask.treatment, mask.mask_id))
        try:
            saved = store.set_dataset_security(dataset.name, rows, masks)
        except StoreError as exc:
            raise _fail(400, str(exc)) from exc
        audit.admin(email=principal.email, action="dataset_security_update", target=dataset.name)
        return {"message": f"Security of '{dataset.name}' saved.", **_detail(dataset, saved)}

    @api.post("/datasets/{name}/load-examples")
    def load_examples(name: str, principal: Principal = Depends(access.require)) -> dict[str, object]:
        dataset = _known_dataset(name)
        current = store.dataset_security(dataset.name)
        if current.rows or current.masks:
            raise _fail(409, f"'{dataset.name}' already has security lines; examples load only into an empty dataset.")
        if not dataset.has_examples:
            raise _fail(400, f"The policy files declare no example rules for '{dataset.name}'.")
        resolved = resolve_examples(
            dataset,
            users={u.email for u in store.list_users()},
            local_groups={g.local_group_id for g in store.list_local_groups()},
            group_ids_by_external_id={g.external_id: g.group_id for g in store.list_groups()},
        )
        try:
            saved = store.set_dataset_security(dataset.name, resolved.rows, resolved.masks)
        except StoreError as exc:
            raise _fail(400, str(exc)) from exc
        audit.admin(email=principal.email, action="dataset_security_load_examples", target=dataset.name)
        loaded = len(resolved.rows) + len(resolved.masks)
        return {
            "message": f"Loaded {loaded} example line(s) into '{dataset.name}'"
                       + (f"; {len(resolved.skipped)} skipped." if resolved.skipped else "."),
            "skipped": resolved.skipped,
            **_detail(dataset, saved),
        }

    @api.get("/datasets/{name}/values/{column}")
    def column_values(name: str, column: str, q: str = "") -> dict[str, object]:
        dataset = _known_dataset(name)
        column = column.strip().lower()
        if column not in dataset.column_kinds:
            raise _fail(404, f"Unknown column '{column}' in '{dataset.name}'.")
        backend = warehouse.get()
        if backend is None:
            raise _fail(503, "The data is not available to the admin yet, so no values can be suggested.")
        values, truncated = _distinct(backend, dataset, column, VALUES_LIMIT, q.strip()[:FILTER_VALUE_MAX_CHARS])
        return {"values": values, "truncated": truncated}

    @api.post("/datasets/{name}/test")
    def test_dataset(name: str, body: TestIn, principal: Principal = Depends(access.require)) -> dict[str, object]:
        dataset = _known_dataset(name)
        email = body.email.strip().lower()
        if not EMAIL_PATTERN.fullmatch(email):
            raise _fail(400, f"'{email}' is not a valid email.")
        snapshot = store.snapshot()
        groups = frozenset(g.strip() for g in body.groups if g.strip())
        tested = Principal(
            email=email, groups=groups, attributes=store.attributes_for(email),
            local_groups=snapshot.local_groups_of(email),
        )
        label = _labels()
        security = snapshot.security.get(dataset.name, DatasetSecurity())
        named = {(line.subject_type, line.subject_id) for line in (*security.rows, *security.masks)}
        # Reasons name groups and users the way the editor does, not by internal id.
        audience = replace(snapshot.audience_of(tested), labels={key: label(*key) for key in named})
        plan = plan_dataset(dataset, tested, audience, security)
        result = explain(plan, tested, warehouse.get())
        audit.admin(email=principal.email, action="dataset_security_test", target=f"{dataset.name}:{email}")
        return {
            "principal": {
                "email": email,
                "local_groups": sorted(tested.local_groups),
                "idp_groups": sorted(groups),
                "attributes": dict(sorted(tested.attributes.items())),
            },
            **result,
        }

    def explain(plan: DatasetPlan, tested: Principal, backend: DuckDBBackend | None) -> dict[str, object]:
        dataset = plan.dataset
        compiled = compile_plan(plan, salt=settings.secrets.mask_salt, dialect=ADMIN_DIALECT)
        total = visible = None
        preview = None
        lines: list[dict[str, object]] = []
        if backend is not None:
            total = _count_where(backend, dataset, exp.true())
            visible = _count(backend, compiled.relation) if compiled is not None else 0
        for line in plan.lines:
            matched = None
            warning = None
            if backend is not None and line.predicate is not None:
                # A hide line's count includes the NULL rows it removes, like the compiled relation.
                counted = hides(line.predicate) if line.rule.kind == KIND_HIDE else line.predicate
                matched = _count_where(backend, dataset, counted)
                if line.applies and matched == 0:
                    warning = _zero_row_warning(backend, dataset, line, tested)
            lines.append({"rule_id": line.rule.rule_id, "kind": line.rule.kind, "applies": line.applies,
                          "reason": line.reason, "matched_rows": matched, "warning": warning})
        if backend is not None and compiled is not None:
            result = backend.execute(compiled.relation.limit(PREVIEW_ROW_LIMIT + 1).sql(dialect=ADMIN_DIALECT))
            preview = {
                "columns": list(result.columns),
                "rows": [[_json_value(v) for v in row] for row in result.rows[:PREVIEW_ROW_LIMIT]],
                "truncated": len(result.rows) > PREVIEW_ROW_LIMIT,
            }
        return {
            "accessible": compiled is not None,
            "rows": {"visible": visible, "total": total, "lines": lines},
            "columns": [{"name": c.name, "treatment": c.treatment, "mask_id": c.mask_id, "reason": c.reason}
                        for c in plan.columns],
            "preview": preview,
            "row_filter": compiled.row_filter if compiled is not None else None,
            "data_available": backend is not None,
        }
