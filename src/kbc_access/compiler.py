"""Layer 3b — compiler: dataset × principal × security lines → secured relation.

For principal P and dataset D (see docs/superpowers/specs/2026-10-07-dataset-security-editor-design.md):

* a line applies when its subject matches P (`everyone`, P's email, one of P's
  local groups, an IdP group P carries);
* rows: `OR(show filters of the applying show lines) AND NOT(hide filter)` for
  every applying hide line; no applying show line → D does not exist for P;
* a filter reading an attribute P lacks (or one that does not fit the column's
  type) makes a show line not apply and a hide line hide every row (fail closed);
* columns: the first applying mask line of each column gives its treatment for
  the whole column (clear, mask_partial, mask_hash, hidden); none → clear.
  Masking never adds rows and is never decided per row, so a clear numeric
  column stays numeric.

Literals are typed from the column kind (number, boolean, string) and every
value, attribute values included, enters the SQL through sqlglot's AST, never by
string concatenation, so a quote inside an attribute value cannot end a literal.
Query Service has no parameter binding, which is why this matters.

`plan_dataset` explains every line and column decision (the admin's "Test as");
`compile_plan` turns a plan into SQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlglot import exp

from kbc_access.identity import Principal
from kbc_access.policy import Dataset, literal_problem
from kbc_access.security_model import (
    BOOLEAN_TRUE,
    CLEAR,
    HIDDEN,
    KIND_BOOLEAN,
    KIND_HIDE,
    KIND_NUMERIC,
    KIND_SHOW,
    KIND_TEXT,
    MASK_HASH,
    MASK_PARTIAL,
    MASKED_TREATMENTS,
    OP_GT,
    OP_GTE,
    OP_IS,
    OP_IS_EMPTY,
    OP_IS_NOT,
    OP_IS_NOT_EMPTY,
    OP_LT,
    OP_LTE,
    OP_NOT_ONE_OF,
    OP_ONE_OF,
    SOURCE_EMAIL,
    TREATMENTS,
    Audience,
    Condition,
    RowFilter,
)
from kbc_access.store import ColumnMask, DatasetSecurity, RowAccessRule

# Visible suffix length for mask_partial (e.g. "****1234").
PARTIAL_VISIBLE_CHARS = 4
PARTIAL_MASK_PREFIX = "****"
TEXT_TYPE = "VARCHAR"


class Unresolvable(ValueError):
    """A filter that cannot be bound for this principal (missing attribute, value of the wrong type, ...)."""


@dataclass(frozen=True)
class LinePlan:
    rule: RowAccessRule
    applies: bool
    # Why the line applies or not, in a sentence for the admin.
    reason: str
    # The bound filter of an applying line (TRUE for a hide line that fails closed); None otherwise.
    predicate: exp.Expression | None


@dataclass(frozen=True)
class ColumnPlan:
    name: str
    treatment: str
    mask_id: str | None
    reason: str


@dataclass(frozen=True)
class DatasetPlan:
    dataset: Dataset
    lines: tuple[LinePlan, ...]
    columns: tuple[ColumnPlan, ...]
    # The full row predicate; None when no show line applies (the dataset is invisible).
    predicate: exp.Expression | None

    @property
    def accessible(self) -> bool:
        return self.predicate is not None

    @property
    def grants_fired(self) -> tuple[str, ...]:
        fired = [f"{line.rule.kind}:{line.rule.rule_id}" for line in self.lines if line.applies]
        fired += [f"mask:{c.name}:{c.mask_id}" for c in self.columns if c.mask_id is not None]
        return tuple(fired)


@dataclass(frozen=True)
class CompiledDataset:
    dataset: str
    relation: exp.Select
    # `show:<rule_id>`, `hide:<rule_id>` and `mask:<column>:<mask_id>` of the lines that applied.
    grants_fired: tuple[str, ...]
    # Columns in the relation, in order, with their treatment.
    visible_columns: tuple[tuple[str, str], ...]
    masked_columns: tuple[str, ...]
    # Hidden columns (not in the relation).
    absent_columns: tuple[str, ...]
    # The bound row predicate as SQL in the backend's dialect (no masking salt appears in it).
    row_filter: str
    # Treatment of every column of the dataset, hidden ones included.
    column_treatments: dict[str, str]


# ---- binding filters ------------------------------------------------------------------------


def _source_value(condition: Condition, principal: Principal) -> str:
    if condition.source == SOURCE_EMAIL:
        return principal.email
    key = condition.attribute
    values = {k.lower(): v for k, v in principal.attributes.items()}
    if key is None or key not in values or not str(values[key]).strip():
        raise Unresolvable(f"the user has no attribute '{key}'")
    return str(values[key])


def _literal(kind: str, value: str, column: str) -> exp.Expression:
    problem = literal_problem(kind, value)
    if problem:
        raise Unresolvable(f"{problem} ({column} is a {kind} column)")
    if kind == KIND_NUMERIC:
        # literal_problem accepted it as digits, fraction and exponent only: safe to emit verbatim.
        return exp.Literal.number(value.strip())
    if kind == KIND_BOOLEAN:
        return exp.true() if value.strip().lower() == BOOLEAN_TRUE else exp.false()
    return exp.Literal.string(value)


def _is_empty(column: exp.Expression, kind: str) -> exp.Expression:
    missing = exp.Is(this=column.copy(), expression=exp.Null())
    if kind != KIND_TEXT:
        return missing
    return exp.paren(exp.or_(missing, exp.EQ(this=column.copy(), expression=exp.Literal.string(""))), copy=False)


def bind_condition(dataset: Dataset, condition: Condition, principal: Principal) -> exp.Expression:
    """One condition as a predicate; raises Unresolvable when it cannot be bound for `principal`."""
    if condition.column not in dataset.column_kinds:
        raise Unresolvable(f"column '{condition.column}' is not in the dataset")
    kind = dataset.column_kinds[condition.column]
    column = exp.column(condition.column)
    if condition.op == OP_IS_EMPTY:
        return _is_empty(column, kind)
    if condition.op == OP_IS_NOT_EMPTY:
        return exp.Not(this=_is_empty(column, kind))
    if condition.op in (OP_ONE_OF, OP_NOT_ONE_OF):
        literals = [_literal(kind, v, condition.column) for v in condition.values or ()]
        inside = exp.In(this=column, expressions=literals)
        if condition.op == OP_ONE_OF:
            return inside
        return exp.paren(exp.or_(exp.Is(this=column.copy(), expression=exp.Null()), exp.Not(this=inside)), copy=False)
    raw = condition.value if condition.value is not None else _source_value(condition, principal)
    literal = _literal(kind, raw, condition.column)
    builders = {
        OP_IS: exp.EQ,
        OP_IS_NOT: exp.NullSafeNEQ,  # NULL counts as "not X"
        OP_GT: exp.GT,
        OP_GTE: exp.GTE,
        OP_LT: exp.LT,
        OP_LTE: exp.LTE,
    }
    if condition.op not in builders:
        raise Unresolvable(f"unknown operator '{condition.op}'")
    return builders[condition.op](this=column, expression=literal)


def bind_filter(dataset: Dataset, row_filter: RowFilter | None, principal: Principal) -> exp.Expression:
    if row_filter is None:
        raise Unresolvable("the stored filter is unreadable")
    if row_filter.all_rows:
        return exp.true()
    parts = [bind_condition(dataset, c, principal) for c in row_filter.conditions]
    return exp.and_(*parts, copy=False) if len(parts) > 1 else parts[0]


# ---- planning --------------------------------------------------------------------------------------


def _plan_line(dataset: Dataset, rule: RowAccessRule, principal: Principal, audience: Audience) -> LinePlan:
    who = audience.explain(rule.subject_type, rule.subject_id)
    if rule.kind not in (KIND_SHOW, KIND_HIDE):
        return LinePlan(rule, False, f"unknown line kind '{rule.kind}'", None)
    if not audience.matches(rule.subject_type, rule.subject_id):
        return LinePlan(rule, False, f"does not apply: {who}", None)
    try:
        predicate = bind_filter(dataset, rule.filter, principal)
    except Unresolvable as exc:
        if rule.kind == KIND_SHOW:
            return LinePlan(rule, False, f"does not apply: {exc}", None)
        return LinePlan(rule, True, f"hides every row: {exc} (fail closed)", exp.true())
    return LinePlan(rule, True, f"applies: {who}", predicate)


def _plan_column(column: str, masks: Sequence[ColumnMask], audience: Audience) -> ColumnPlan:
    for mask in masks:
        if audience.matches(mask.subject_type, mask.subject_id):
            who = audience.explain(mask.subject_type, mask.subject_id)
            if mask.treatment not in TREATMENTS:
                return ColumnPlan(column, HIDDEN, mask.mask_id,
                                  f"line {mask.position}: unknown treatment '{mask.treatment}', hidden (fail closed)")
            return ColumnPlan(column, mask.treatment, mask.mask_id, f"line {mask.position} applies: {who}")
    return ColumnPlan(column, CLEAR, None, "no masking line applies: clear")


def _or(conditions: list[exp.Expression]) -> exp.Expression:
    if len(conditions) == 1:
        return conditions[0]
    return exp.or_(*(exp.paren(c.copy(), copy=False) for c in conditions), copy=False)


def hides(predicate: exp.Expression) -> exp.Expression:
    """The rows a hide line removes: those its filter matches, and those it cannot decide (NULL).

    Fail closed: "always hide rows where is_test is true" also hides a row whose is_test is missing.
    """
    return exp.Coalesce(this=exp.paren(predicate.copy(), copy=False), expressions=[exp.true()])


def plan_dataset(
    dataset: Dataset, principal: Principal, audience: Audience, security: DatasetSecurity
) -> DatasetPlan:
    lines = tuple(_plan_line(dataset, rule, principal, audience) for rule in security.rows)
    shows = [line.predicate for line in lines if line.applies and line.rule.kind == KIND_SHOW and line.predicate]
    predicate: exp.Expression | None = None
    if shows:
        predicate = exp.paren(_or(shows), copy=False)
        for line in lines:
            if line.applies and line.rule.kind == KIND_HIDE and line.predicate is not None:
                predicate = exp.and_(predicate, exp.Not(this=hides(line.predicate)), copy=False)
    by_column: dict[str, list[ColumnMask]] = {}
    for mask in security.masks:
        by_column.setdefault(mask.column_name, []).append(mask)
    columns = tuple(
        _plan_column(c, sorted(by_column.get(c, []), key=lambda m: (m.position, m.mask_id)), audience)
        for c in dataset.columns
    )
    return DatasetPlan(dataset, lines, columns, predicate)


# ---- SQL -------------------------------------------------------------------------------------------


def _masked(column: str, treatment: str, salt: str, dialect: str) -> exp.Expression:
    as_text = exp.cast(exp.column(column), exp.DataType.build(TEXT_TYPE))
    if treatment == MASK_PARTIAL:
        right = exp.Right(this=as_text, expression=exp.Literal.number(PARTIAL_VISIBLE_CHARS))
        return exp.Concat(expressions=[exp.Literal.string(PARTIAL_MASK_PREFIX), right])
    if treatment == MASK_HASH:
        salted = exp.Concat(expressions=[exp.Literal.string(salt), as_text])
        digest = exp.SHA2(this=salted, length=exp.Literal.number(256))
        # BigQuery's SHA256 returns BYTES; the other dialects return hex text.
        return exp.Hex(this=digest) if dialect == "bigquery" else digest
    return exp.column(column)


def compile_plan(plan: DatasetPlan, *, salt: str, dialect: str) -> CompiledDataset | None:
    """The secured relation of a plan, or None when the principal may not see the dataset."""
    if plan.predicate is None:
        return None
    dataset = plan.dataset
    inner = exp.select("*").from_(exp.to_table(dataset.source)).where(plan.predicate.copy(), copy=False)
    projections: list[exp.Expression] = []
    visible: list[tuple[str, str]] = []
    for column in plan.columns:
        if column.treatment == HIDDEN:
            continue
        projections.append(exp.alias_(_masked(column.name, column.treatment, salt, dialect), column.name, copy=False))
        visible.append((column.name, column.treatment))
    if not projections:
        # Every column hidden: the rows still exist (counts work), but carry no values.
        projections.append(exp.alias_(exp.null(), "_no_visible_columns", copy=False))
    relation = exp.select(*projections, copy=False).from_(inner.subquery("_secured", copy=False))
    return CompiledDataset(
        dataset=dataset.name,
        relation=relation,
        grants_fired=plan.grants_fired,
        visible_columns=tuple(visible),
        masked_columns=tuple(c.name for c in plan.columns if c.treatment in MASKED_TREATMENTS),
        absent_columns=tuple(c.name for c in plan.columns if c.treatment == HIDDEN),
        row_filter=plan.predicate.sql(dialect=dialect),
        column_treatments={c.name: c.treatment for c in plan.columns},
    )


def compile_dataset(
    dataset: Dataset,
    principal: Principal,
    audience: Audience,
    security: DatasetSecurity,
    *,
    salt: str,
    dialect: str,
) -> CompiledDataset | None:
    """Return the secured relation, or None when the principal may not see the dataset."""
    return compile_plan(plan_dataset(dataset, principal, audience, security), salt=salt, dialect=dialect)
