"""Layer 3b: compiler — dataset x principal x security lines -> secured relation, verified on real DuckDB.

Datasets come from the real catalogue (config/policies/pharma.yaml) checked against the
shared demo warehouse; security lines are built directly as RowAccessRule / ColumnMask, or
read from the seeded store where a test is about the snapshot. Every row-level claim is
checked by running the compiled relation and an equivalent hand-written query on DuckDB.

The demo data has no NULLs, so the NULL-handling tests compile a copy of `drug_sales`
whose source is a CTE that blanks some values (see NULLS_CTE).
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from kbc_access.backends.duckdb_backend import DuckDBBackend
from kbc_access.compiler import (
    PARTIAL_MASK_PREFIX,
    PARTIAL_VISIBLE_CHARS,
    CompiledDataset,
    Unresolvable,
    bind_condition,
    bind_filter,
    compile_dataset,
    compile_plan,
    plan_dataset,
)
from kbc_access.guard import SALT_REDACTION, DataGuard
from kbc_access.identity import Principal
from kbc_access.policy import Dataset, PolicySet, load_policy
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
    Audience,
    Condition,
    RowFilter,
    parse_filter,
)
from kbc_access.store import EMPTY_SECURITY, ColumnMask, DatasetSecurity, RowAccessRule
from kbc_access.web import Runtime, build_guard
from tests.conftest import TEST_SECRETS

ROOT = Path(__file__).resolve().parent.parent
PHARMA_POLICY = ROOT / "config" / "policies" / "pharma.yaml"
MAX_ROWS = 1_000_000
SALT = TEST_SECRETS["MASK_SALT"]
OTHER_SALT = SALT + "-rotated"
DIALECTS = ("duckdb", "bigquery", "snowflake")
SHA256_HEX_CHARS = 64

DRUG_SALES = "drug_sales"
STORES = "stores"
SUPPLY_CHAIN = "supply_chain"

# IdP group external ids, as in X-Kbc-User-Roles (config/fake_users.yaml).
FINANCE_EXTERNAL = "3f2b8c1e-7d4a-4e5b-9a61-2c8d0f4e1b7a"
SUPPLY_EXTERNAL = "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9"
PARTNER_EXTERNAL = "00g7pharmab0partner1"
SALES_MGR_EXTERNAL = "8a1d4e2f-5b6c-4d7e-9f80-1a2b3c4d5e6f"
# Internal group id used by the hand-built audiences (lines store internal ids, never external ones).
FINANCE_GROUP_ID = "f1a2b3c4d5e6"
EMEA = "emea-sales"

PETR_EMAIL = "petr.sales@pharma-a.example"
LUCIE_EMAIL = "lucie.finance@pharma-a.example"
MARTIN_EMAIL = "martin.noattr@pharma-c.example"
JAN_EMAIL = "jan.partner@pharma-b.example"
EVA_EMAIL = "eva.supply@pharma-a.example"
STRANGER_EMAIL = "x@y.example"

COMPANY_A = "PHARMA_A"
COMPANY_B = "PHARMA_B"
COMPANY_C = "PHARMA_C"

PETR = Principal(PETR_EMAIL, frozenset({SALES_MGR_EXTERNAL}), {"company": COMPANY_A})
LUCIE = Principal(LUCIE_EMAIL, frozenset({FINANCE_EXTERNAL}), {"company": COMPANY_A})
MARTIN = Principal(MARTIN_EMAIL, frozenset({SALES_MGR_EXTERNAL}), {})
JAN = Principal(JAN_EMAIL, frozenset({PARTNER_EXTERNAL}), {"company": COMPANY_B})
EVA = Principal(EVA_EMAIL, frozenset({SUPPLY_EXTERNAL}), {"company": COMPANY_A})

AUD_PETR = Audience(PETR_EMAIL, frozenset({EMEA}), frozenset())
AUD_LUCIE = Audience(LUCIE_EMAIL, frozenset({EMEA}), frozenset({FINANCE_GROUP_ID}))
AUD_MARTIN = Audience(MARTIN_EMAIL, frozenset(), frozenset())
AUD_STRANGER = Audience(STRANGER_EMAIL, frozenset(), frozenset())

# Line ids (12 lowercase hex, as the store generates them).
SHOW_1 = "5a0000000001"
SHOW_2 = "5a0000000002"
SHOW_3 = "5a0000000003"
HIDE_1 = "4d0000000001"
HIDE_2 = "4d0000000002"
MASK_1 = "3a0000000001"
MASK_2 = "3a0000000002"
MASK_3 = "3a0000000003"

PUBLIC = {"column": "visibility", "op": "is", "value": "public"}
OWN_COMPANY = {"column": "company", "op": "is", "source": "attribute:company"}
IS_TEST = {"column": "is_test", "op": "is", "value": "true"}
ALL = {"all": True}

# A copy of drug_sales with NULLs and empty strings, used as the source of NULLS dataset.
NULLS_SOURCE = "drug_sales_with_nulls"
NULLS_CTE = (
    "SELECT * REPLACE ("
    "CASE WHEN sale_id % 7 = 0 THEN NULL ELSE company END AS company, "
    "CASE WHEN sale_id % 5 = 0 THEN NULL WHEN sale_id % 5 = 1 THEN '' ELSE customer_id END AS customer_id, "
    "CASE WHEN sale_id % 3 = 0 THEN NULL ELSE margin END AS margin, "
    "CASE WHEN sale_id % 11 = 0 THEN NULL ELSE is_test END AS is_test, "
    "CASE WHEN sale_id % 13 = 0 THEN NULL ELSE drug END AS drug"
    ") FROM drug_sales"
)


# ---- fixtures and helpers ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend(shared_warehouse: Path) -> DuckDBBackend:
    return DuckDBBackend(shared_warehouse, MAX_ROWS)


@pytest.fixture(scope="module")
def policy(backend: DuckDBBackend) -> PolicySet:
    return load_policy([PHARMA_POLICY], backend.column_types())


@pytest.fixture(scope="module")
def sales(policy: PolicySet) -> Dataset:
    return policy.datasets[DRUG_SALES]


@pytest.fixture(scope="module")
def nulls(sales: Dataset) -> Dataset:
    return dataclasses.replace(sales, source=NULLS_SOURCE)


def _filter(*conditions: dict[str, object]) -> RowFilter:
    return parse_filter({"conditions": list(conditions)})


def _line(
    kind: str,
    rule_id: str,
    row_filter: RowFilter | dict[str, object] | None,
    *,
    position: int = 1,
    subject_type: str = SUBJECT_EVERYONE,
    subject_id: str = "",
    dataset: str = DRUG_SALES,
) -> RowAccessRule:
    parsed = parse_filter(row_filter) if isinstance(row_filter, dict) else row_filter
    return RowAccessRule(rule_id, dataset, position, kind, subject_type, subject_id, parsed)


def _show(rule_id: str, row_filter: RowFilter | dict[str, object] | None, **kw: object) -> RowAccessRule:
    return _line(KIND_SHOW, rule_id, row_filter, **kw)  # type: ignore[arg-type]


def _hide(rule_id: str, row_filter: RowFilter | dict[str, object] | None, **kw: object) -> RowAccessRule:
    return _line(KIND_HIDE, rule_id, row_filter, **kw)  # type: ignore[arg-type]


def _mask(
    mask_id: str,
    column: str,
    treatment: str,
    *,
    position: int = 1,
    subject_type: str = SUBJECT_EVERYONE,
    subject_id: str = "",
    dataset: str = DRUG_SALES,
) -> ColumnMask:
    return ColumnMask(mask_id, dataset, column, position, subject_type, subject_id, treatment)


def _security(*rows: RowAccessRule, masks: tuple[ColumnMask, ...] = ()) -> DatasetSecurity:
    return DatasetSecurity(rows=rows, masks=masks)


def _compile(
    dataset: Dataset,
    principal: Principal,
    audience: Audience,
    security: DatasetSecurity,
    *,
    salt: str = SALT,
    dialect: str = "duckdb",
) -> CompiledDataset | None:
    return compile_dataset(dataset, principal, audience, security, salt=salt, dialect=dialect)


def _must(compiled: CompiledDataset | None) -> CompiledDataset:
    assert compiled is not None, "expected the dataset to be visible"
    return compiled


def _with_source(dataset: Dataset, sql: str) -> str:
    return f"WITH {NULLS_SOURCE} AS ({NULLS_CTE}) {sql}" if dataset.source == NULLS_SOURCE else sql


def _run(backend: DuckDBBackend, dataset: Dataset, compiled: CompiledDataset) -> list[dict[str, object]]:
    result = backend.execute(_with_source(dataset, compiled.relation.sql(dialect="duckdb")))
    return [dict(zip(result.columns, row, strict=True)) for row in result.rows]


def _columns(backend: DuckDBBackend, dataset: Dataset, compiled: CompiledDataset) -> list[str]:
    return backend.execute(_with_source(dataset, compiled.relation.sql(dialect="duckdb"))).columns


def _ids(rows: list[dict[str, object]], key: str = "sale_id") -> list[object]:
    return sorted(r[key] for r in rows)  # type: ignore[type-var]


def _raw_ids(backend: DuckDBBackend, dataset: Dataset, where: str, key: str = "sale_id") -> list[object]:
    sql = f"SELECT {key} FROM {dataset.source} WHERE {where} ORDER BY {key}"
    return [r[0] for r in backend.execute(_with_source(dataset, sql)).rows]


def _scalar(backend: DuckDBBackend, sql: str) -> object:
    return backend.execute(sql).rows[0][0]


def _raw_by_id(backend: DuckDBBackend, table: str, key: str) -> dict[object, dict[str, object]]:
    result = backend.execute(f"SELECT * FROM {table}")
    index = result.columns.index(key)
    return {row[index]: dict(zip(result.columns, row, strict=True)) for row in result.rows}


def _partial(value: str) -> str:
    return PARTIAL_MASK_PREFIX + value[-PARTIAL_VISIBLE_CHARS:]


def _hash(salt: str, value: str) -> str:
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()


# ---- subject matching -------------------------------------------------------------------------


def test_everyone_line_applies_to_any_signed_in_user(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(_show(SHOW_1, ALL))
    compiled = _must(_compile(sales, Principal(STRANGER_EMAIL, frozenset()), AUD_STRANGER, security))
    assert len(_run(backend, sales, compiled)) == _scalar(backend, "SELECT count(*) FROM drug_sales")
    assert compiled.grants_fired == (f"show:{SHOW_1}",)


def test_user_line_applies_to_that_email_only(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(_show(SHOW_1, ALL, subject_type=SUBJECT_USER, subject_id=PETR_EMAIL))
    assert _compile(sales, PETR, AUD_PETR, security) is not None
    assert _compile(sales, LUCIE, AUD_LUCIE, security) is None
    # Subject matching uses the audience (built by the snapshot, which lowercases the email).
    shouting = Audience(PETR_EMAIL.upper(), frozenset(), frozenset())
    assert _compile(sales, PETR, shouting, security) is None


def test_local_group_line_applies_to_members_only(sales: Dataset) -> None:
    security = _security(_show(SHOW_1, ALL, subject_type=SUBJECT_LOCAL_GROUP, subject_id=EMEA))
    assert _compile(sales, PETR, AUD_PETR, security) is not None
    assert _compile(sales, MARTIN, AUD_MARTIN, security) is None


def test_group_line_matches_internal_group_id_only(sales: Dataset) -> None:
    by_internal = _security(_show(SHOW_1, ALL, subject_type=SUBJECT_GROUP, subject_id=FINANCE_GROUP_ID))
    assert _compile(sales, LUCIE, AUD_LUCIE, by_internal) is not None
    assert _compile(sales, PETR, AUD_PETR, by_internal) is None
    # A line naming the external id never matches: lines store internal group ids.
    by_external = _security(_show(SHOW_1, ALL, subject_type=SUBJECT_GROUP, subject_id=FINANCE_EXTERNAL))
    assert _compile(sales, LUCIE, AUD_LUCIE, by_external) is None


def test_unknown_subject_type_never_matches(sales: Dataset) -> None:
    security = _security(_show(SHOW_1, ALL, subject_type="role", subject_id=PETR_EMAIL))
    assert _compile(sales, PETR, AUD_PETR, security) is None


def test_idp_group_resolved_through_snapshot(runtime: Runtime, sales: Dataset) -> None:
    snapshot = runtime.store.snapshot()
    finance_id = snapshot.groups_by_external_id[FINANCE_EXTERNAL][0].group_id
    security = _security(_show(SHOW_1, ALL, subject_type=SUBJECT_GROUP, subject_id=finance_id))
    lucie = snapshot.audience_of(LUCIE)
    assert finance_id in lucie.group_ids
    assert _compile(sales, LUCIE, lucie, security) is not None
    # Same person without the IdP group in the header: no match.
    without = Principal(LUCIE_EMAIL, frozenset(), {"company": COMPANY_A})
    assert _compile(sales, without, snapshot.audience_of(without), security) is None
    # A header carrying the internal id itself is not an IdP group the snapshot knows.
    spoof = Principal(PETR_EMAIL, frozenset({finance_id}), {"company": COMPANY_A})
    assert _compile(sales, spoof, snapshot.audience_of(spoof), security) is None


def test_local_group_resolved_through_snapshot(runtime: Runtime, sales: Dataset) -> None:
    snapshot = runtime.store.snapshot()
    security = _security(_show(SHOW_1, ALL, subject_type=SUBJECT_LOCAL_GROUP, subject_id=EMEA))
    assert _compile(sales, PETR, snapshot.audience_of(PETR), security) is not None
    assert _compile(sales, MARTIN, snapshot.audience_of(MARTIN), security) is None


# ---- row lines --------------------------------------------------------------------------------


def test_show_lines_union(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(_show(SHOW_1, _filter(PUBLIC)), _show(SHOW_2, _filter(OWN_COMPANY), position=2))
    compiled = _must(_compile(sales, PETR, AUD_PETR, security))
    rows = _run(backend, sales, compiled)
    assert _ids(rows) == _raw_ids(backend, sales, "visibility = 'public' OR company = 'PHARMA_A'")
    assert {r["company"] for r in rows if r["visibility"] == "private"} == {COMPANY_A}
    assert {r["company"] for r in rows if r["visibility"] == "public"} == {COMPANY_A, COMPANY_B, COMPANY_C}
    assert compiled.grants_fired == (f"show:{SHOW_1}", f"show:{SHOW_2}")


def test_show_line_of_other_subject_does_not_widen(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(
        _show(SHOW_1, _filter(PUBLIC)),
        _show(SHOW_2, ALL, position=2, subject_type=SUBJECT_GROUP, subject_id=FINANCE_GROUP_ID),
    )
    petr = _run(backend, sales, _must(_compile(sales, PETR, AUD_PETR, security)))
    lucie = _run(backend, sales, _must(_compile(sales, LUCIE, AUD_LUCIE, security)))
    assert _ids(petr) == _raw_ids(backend, sales, "visibility = 'public'")
    assert len(lucie) == _scalar(backend, "SELECT count(*) FROM drug_sales")


def test_hide_line_removes_rows(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(_show(SHOW_1, ALL), _hide(HIDE_1, _filter(IS_TEST)))
    compiled = _must(_compile(sales, PETR, AUD_PETR, security))
    rows = _run(backend, sales, compiled)
    tests = _scalar(backend, "SELECT count(*) FROM drug_sales WHERE is_test")
    assert tests, "the seed must contain test rows for this check to mean anything"
    assert _ids(rows) == _raw_ids(backend, sales, "NOT is_test")
    assert not any(r["is_test"] for r in rows)
    assert compiled.grants_fired == (f"show:{SHOW_1}", f"hide:{HIDE_1}")


def test_hide_line_applies_to_its_subject_only(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(
        _show(SHOW_1, ALL),
        _hide(HIDE_1, _filter(PUBLIC), subject_type=SUBJECT_LOCAL_GROUP, subject_id=EMEA),
    )
    member = _run(backend, sales, _must(_compile(sales, PETR, AUD_PETR, security)))
    other = _must(_compile(sales, MARTIN, AUD_MARTIN, security))
    assert _ids(member) == _raw_ids(backend, sales, "visibility <> 'public'")
    assert len(_run(backend, sales, other)) == _scalar(backend, "SELECT count(*) FROM drug_sales")
    assert other.grants_fired == (f"show:{SHOW_1}",)


def test_hide_line_cannot_add_rows(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(_show(SHOW_1, _filter(PUBLIC)), _hide(HIDE_1, _filter({"column": "company", "op": "is", "value": "NOPE"})))
    rows = _run(backend, sales, _must(_compile(sales, PETR, AUD_PETR, security)))
    assert _ids(rows) == _raw_ids(backend, sales, "visibility = 'public'")


def test_several_hide_lines_all_apply(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(
        _show(SHOW_1, ALL),
        _hide(HIDE_1, _filter(IS_TEST)),
        _hide(HIDE_2, _filter({"column": "company", "op": "is", "value": COMPANY_C}), position=2),
    )
    rows = _run(backend, sales, _must(_compile(sales, PETR, AUD_PETR, security)))
    assert _ids(rows) == _raw_ids(backend, sales, "NOT is_test AND company <> 'PHARMA_C'")


def test_hide_line_also_hides_rows_it_cannot_decide(backend: DuckDBBackend, nulls: Dataset) -> None:
    """Fail closed: a row the hide filter cannot decide (company IS NULL) is hidden too."""
    security = _security(_show(SHOW_1, ALL), _hide(HIDE_1, _filter({"column": "company", "op": "is", "value": COMPANY_A})))
    compiled = _must(_compile(nulls, PETR, AUD_PETR, security))
    rows = _run(backend, nulls, compiled)
    null_rows = _raw_ids(backend, nulls, "company IS NULL")
    assert null_rows, "the NULL copy must contain NULL companies"
    assert _ids(rows) == _raw_ids(backend, nulls, "company IS NOT NULL AND company <> 'PHARMA_A'")
    assert not set(null_rows) & set(_ids(rows))
    assert "COALESCE" in compiled.row_filter


def test_no_applying_show_line_means_invisible(sales: Dataset) -> None:
    assert _compile(sales, PETR, AUD_PETR, EMPTY_SECURITY) is None
    only_hide = _security(_hide(HIDE_1, _filter(IS_TEST)))
    assert _compile(sales, PETR, AUD_PETR, only_hide) is None
    other_user = _security(_show(SHOW_1, ALL, subject_type=SUBJECT_USER, subject_id=LUCIE_EMAIL))
    assert _compile(sales, PETR, AUD_PETR, other_user) is None
    plan = plan_dataset(sales, PETR, AUD_PETR, only_hide)
    assert not plan.accessible
    assert compile_plan(plan, salt=SALT, dialect="duckdb") is None


def test_show_line_matching_no_rows_is_visible_but_empty(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(_show(SHOW_1, _filter({"column": "company", "op": "is", "value": "Pharma A s.r.o."})))
    compiled = _must(_compile(sales, PETR, AUD_PETR, security))
    assert _run(backend, sales, compiled) == []


# ---- missing or unparseable attributes fail closed ---------------------------------------------


def test_missing_attribute_skips_show_line(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(_show(SHOW_1, _filter(PUBLIC)), _show(SHOW_2, _filter(OWN_COMPANY), position=2))
    compiled = _must(_compile(sales, MARTIN, AUD_MARTIN, security))
    assert compiled.grants_fired == (f"show:{SHOW_1}",)
    assert _ids(_run(backend, sales, compiled)) == _raw_ids(backend, sales, "visibility = 'public'")


def test_missing_attribute_with_only_attribute_show_line_is_invisible(sales: Dataset) -> None:
    security = _security(_show(SHOW_1, _filter(OWN_COMPANY)))
    assert _compile(sales, MARTIN, AUD_MARTIN, security) is None
    blank = Principal(MARTIN_EMAIL, frozenset(), {"company": "   "})
    assert _compile(sales, blank, AUD_MARTIN, security) is None


def test_attribute_keys_match_case_insensitively(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(_show(SHOW_1, _filter(OWN_COMPANY)))
    upper = Principal(PETR_EMAIL, frozenset(), {"Company": COMPANY_B})
    rows = _run(backend, sales, _must(_compile(sales, upper, AUD_PETR, security)))
    assert {r["company"] for r in rows} == {COMPANY_B}


def test_missing_attribute_in_hide_line_hides_every_row(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(_show(SHOW_1, ALL), _hide(HIDE_1, _filter({"column": "company", "op": "is_not", "source": "attribute:company"})))
    with_attr = _must(_compile(sales, PETR, AUD_PETR, security))
    without = _must(_compile(sales, MARTIN, AUD_MARTIN, security))
    assert {r["company"] for r in _run(backend, sales, with_attr)} == {COMPANY_A}
    assert _run(backend, sales, without) == []
    assert without.grants_fired == (f"show:{SHOW_1}", f"hide:{HIDE_1}")


def test_unparseable_attribute_for_numeric_column(backend: DuckDBBackend, sales: Dataset) -> None:
    max_units = {"column": "units", "op": "lte", "source": "attribute:max_units"}
    show = _security(_show(SHOW_1, _filter(max_units)))
    good = Principal(PETR_EMAIL, frozenset(), {"max_units": "10"})
    bad = Principal(PETR_EMAIL, frozenset(), {"max_units": "ten"})
    rows = _run(backend, sales, _must(_compile(sales, good, AUD_PETR, show)))
    assert _ids(rows) == _raw_ids(backend, sales, "units <= 10")
    assert _compile(sales, bad, AUD_PETR, show) is None
    reason = plan_dataset(sales, bad, AUD_PETR, show).lines[0].reason
    assert reason.startswith("does not apply:") and "not a number" in reason
    hide = _security(_show(SHOW_1, ALL), _hide(HIDE_1, _filter({**max_units, "op": "gt"})))
    assert _run(backend, sales, _must(_compile(sales, bad, AUD_PETR, hide))) == []
    assert _ids(_run(backend, sales, _must(_compile(sales, good, AUD_PETR, hide)))) == _raw_ids(backend, sales, "units <= 10")


def test_unreadable_stored_filter_fails_closed(backend: DuckDBBackend, sales: Dataset) -> None:
    show_none = _security(_show(SHOW_1, None), _show(SHOW_2, _filter(PUBLIC), position=2))
    compiled = _must(_compile(sales, PETR, AUD_PETR, show_none))
    assert compiled.grants_fired == (f"show:{SHOW_2}",)
    assert _ids(_run(backend, sales, compiled)) == _raw_ids(backend, sales, "visibility = 'public'")
    assert "unreadable" in plan_dataset(sales, PETR, AUD_PETR, show_none).lines[0].reason
    assert _compile(sales, PETR, AUD_PETR, _security(_show(SHOW_1, None))) is None
    hide_none = _security(_show(SHOW_1, ALL), _hide(HIDE_1, None))
    hidden = _must(_compile(sales, PETR, AUD_PETR, hide_none))
    assert _run(backend, sales, hidden) == []
    with pytest.raises(Unresolvable):
        bind_filter(sales, None, PETR)


def test_unknown_line_kind_does_not_apply(sales: Dataset) -> None:
    security = _security(_line("allow", SHOW_1, ALL))
    plan = plan_dataset(sales, PETR, AUD_PETR, security)
    assert not plan.lines[0].applies
    assert "unknown line kind" in plan.lines[0].reason
    assert _compile(sales, PETR, AUD_PETR, security) is None


def test_condition_on_unknown_column_is_unresolvable(sales: Dataset) -> None:
    with pytest.raises(Unresolvable, match="not in the dataset"):
        bind_condition(sales, Condition("no_such_column", "is", value="x"), PETR)


# ---- typed literals ---------------------------------------------------------------------------


def _bound(dataset: Dataset, condition: dict[str, object], principal: Principal = PETR) -> exp.Expression:
    return bind_condition(dataset, _filter(condition).conditions[0], principal)


def test_numeric_column_gets_number_literal(sales: Dataset) -> None:
    bound = _bound(sales, {"column": "units", "op": "is", "value": "12"})
    literal = bound.expression
    assert isinstance(literal, exp.Literal) and not literal.is_string
    assert bound.sql(dialect="duckdb") == "units = 12"
    # A JSON number is the same literal.
    assert _bound(sales, {"column": "revenue", "op": "gt", "value": 5000.5}).sql(dialect="duckdb") == "revenue > 5000.5"


def test_boolean_column_gets_true_false(sales: Dataset) -> None:
    assert isinstance(_bound(sales, {"column": "is_test", "op": "is", "value": "TRUE"}).expression, exp.Boolean)
    assert _bound(sales, {"column": "is_test", "op": "is", "value": True}).sql(dialect="duckdb") == "is_test = TRUE"
    assert _bound(sales, {"column": "is_test", "op": "is", "value": "false"}).sql(dialect="duckdb") == "is_test = FALSE"


def test_date_and_text_columns_get_string_literals(sales: Dataset) -> None:
    date_literal = _bound(sales, {"column": "sale_date", "op": "gte", "value": "2026-06-01"}).expression
    assert isinstance(date_literal, exp.Literal) and date_literal.is_string
    # A number-looking value on a text column stays text.
    text_literal = _bound(sales, {"column": "store_id", "op": "is", "value": 123}).expression
    assert isinstance(text_literal, exp.Literal) and text_literal.is_string and text_literal.this == "123"


@pytest.mark.parametrize(
    ("condition", "problem"),
    [
        ({"column": "units", "op": "is", "value": "12 units"}, "not a number"),
        ({"column": "is_test", "op": "is", "value": "yes"}, "not true or false"),
        ({"column": "sale_date", "op": "is", "value": "June 1st"}, "not a date"),
        ({"column": "units", "op": "one_of", "values": ["1", "two"]}, "not a number"),
    ],
)
def test_literal_of_the_wrong_kind_is_unresolvable(sales: Dataset, condition: dict[str, object], problem: str) -> None:
    with pytest.raises(Unresolvable, match=problem):
        _bound(sales, condition)


def test_typed_literals_select_the_right_rows(backend: DuckDBBackend, sales: Dataset) -> None:
    cases = [
        ({"column": "units", "op": "is", "value": "12"}, "units = 12"),
        ({"column": "is_test", "op": "is", "value": "TRUE"}, "is_test"),
        ({"column": "sale_date", "op": "gte", "value": "2026-06-01"}, "sale_date >= DATE '2026-06-01'"),
        ({"column": "store_id", "op": "is", "value": "A005"}, "store_id = 'A005'"),
    ]
    for condition, where in cases:
        compiled = _must(_compile(sales, PETR, AUD_PETR, _security(_show(SHOW_1, _filter(condition)))))
        expected = _raw_ids(backend, sales, where)
        assert expected, f"no rows for {where}: the check would mean nothing"
        assert _ids(_run(backend, sales, compiled)) == expected, condition


# ---- operators --------------------------------------------------------------------------------

# (condition, equivalent hand-written WHERE over the NULL copy of drug_sales).
OPERATOR_CASES = [
    ({"column": "company", "op": "is", "value": COMPANY_A}, "company = 'PHARMA_A'"),
    ({"column": "company", "op": "is_not", "value": COMPANY_A}, "company IS NULL OR company <> 'PHARMA_A'"),
    ({"column": "is_test", "op": "is", "value": "true"}, "is_test = TRUE"),
    ({"column": "is_test", "op": "is_not", "value": "true"}, "is_test IS NULL OR is_test = FALSE"),
    ({"column": "units", "op": "is_not", "value": "12"}, "units <> 12"),
    ({"column": "drug", "op": "one_of", "values": ["Zyrtec", "Euthyrox"]}, "drug IN ('Zyrtec', 'Euthyrox')"),
    ({"column": "units", "op": "one_of", "values": [1, "2", 3]}, "units IN (1, 2, 3)"),
    ({"column": "company", "op": "not_one_of", "values": [COMPANY_A, COMPANY_B]},
     "company IS NULL OR company NOT IN ('PHARMA_A', 'PHARMA_B')"),
    ({"column": "drug", "op": "not_one_of", "values": ["Zyrtec"]}, "drug IS NULL OR drug <> 'Zyrtec'"),
    ({"column": "units", "op": "gt", "value": "20"}, "units > 20"),
    ({"column": "units", "op": "gte", "value": "20"}, "units >= 20"),
    ({"column": "units", "op": "lt", "value": "20"}, "units < 20"),
    ({"column": "units", "op": "lte", "value": "20"}, "units <= 20"),
    ({"column": "revenue", "op": "gt", "value": "5000.5"}, "revenue > 5000.5"),
    ({"column": "margin", "op": "lte", "value": "1000"}, "margin <= 1000"),
    ({"column": "sale_date", "op": "gt", "value": "2026-06-01"}, "sale_date > DATE '2026-06-01'"),
    ({"column": "sale_date", "op": "gte", "value": "2026-06-01"}, "sale_date >= DATE '2026-06-01'"),
    ({"column": "sale_date", "op": "lt", "value": "2026-03-01"}, "sale_date < DATE '2026-03-01'"),
    ({"column": "sale_date", "op": "lte", "value": "2026-03-01"}, "sale_date <= DATE '2026-03-01'"),
    ({"column": "customer_id", "op": "is_empty"}, "customer_id IS NULL OR customer_id = ''"),
    ({"column": "customer_id", "op": "is_not_empty"}, "customer_id IS NOT NULL AND customer_id <> ''"),
    ({"column": "margin", "op": "is_empty"}, "margin IS NULL"),
    ({"column": "margin", "op": "is_not_empty"}, "margin IS NOT NULL"),
    ({"column": "is_test", "op": "is_empty"}, "is_test IS NULL"),
]


@pytest.mark.parametrize(("condition", "where"), OPERATOR_CASES, ids=lambda v: str(v.get("op")) if isinstance(v, dict) else None)
def test_operator_selects_expected_rows(
    backend: DuckDBBackend, nulls: Dataset, condition: dict[str, object], where: str
) -> None:
    compiled = _must(_compile(nulls, PETR, AUD_PETR, _security(_show(SHOW_1, _filter(condition)))))
    expected = _raw_ids(backend, nulls, where)
    assert expected, f"no rows for {where}: the check would mean nothing"
    assert _ids(_run(backend, nulls, compiled)) == expected


def test_is_not_and_not_one_of_keep_null_rows(backend: DuckDBBackend, nulls: Dataset) -> None:
    null_companies = set(_raw_ids(backend, nulls, "company IS NULL"))
    assert null_companies
    for condition in (
        {"column": "company", "op": "is_not", "value": COMPANY_A},
        {"column": "company", "op": "not_one_of", "values": [COMPANY_A, COMPANY_B, COMPANY_C]},
    ):
        compiled = _must(_compile(nulls, PETR, AUD_PETR, _security(_show(SHOW_1, _filter(condition)))))
        assert null_companies <= set(_ids(_run(backend, nulls, compiled))), condition
    # not_one_of every company leaves exactly the NULL rows.
    every = {"column": "company", "op": "not_one_of", "values": [COMPANY_A, COMPANY_B, COMPANY_C]}
    compiled = _must(_compile(nulls, PETR, AUD_PETR, _security(_show(SHOW_1, _filter(every)))))
    assert set(_ids(_run(backend, nulls, compiled))) == null_companies


def test_conditions_of_one_line_are_anded(backend: DuckDBBackend, sales: Dataset) -> None:
    row_filter = _filter(PUBLIC, {"column": "company", "op": "is", "value": COMPANY_B}, {"column": "units", "op": "gte", "value": 30})
    rows = _run(backend, sales, _must(_compile(sales, PETR, AUD_PETR, _security(_show(SHOW_1, row_filter)))))
    assert _ids(rows) == _raw_ids(backend, sales, "visibility = 'public' AND company = 'PHARMA_B' AND units >= 30")


def test_email_source_binds_principal_email(sales: Dataset) -> None:
    bound = _bound(sales, {"column": "customer_id", "op": "is_not", "source": "email"})
    literal = bound.expression
    assert isinstance(literal, exp.Literal) and literal.is_string and literal.this == PETR_EMAIL


def test_unknown_operator_is_unresolvable(sales: Dataset) -> None:
    with pytest.raises(Unresolvable, match="unknown operator"):
        bind_condition(sales, Condition("units", "between", value="1"), PETR)


# ---- column masks -----------------------------------------------------------------------------


def test_first_applying_mask_line_wins_per_column(sales: Dataset) -> None:
    masks = (
        # Listed out of order on purpose: position decides, not tuple order.
        _mask(MASK_2, "margin", HIDDEN, position=2),
        _mask(MASK_1, "margin", CLEAR, position=1, subject_type=SUBJECT_GROUP, subject_id=FINANCE_GROUP_ID),
        _mask(MASK_3, "customer_id", MASK_PARTIAL, position=1, subject_type=SUBJECT_USER, subject_id=LUCIE_EMAIL),
    )
    security = _security(_show(SHOW_1, ALL), masks=masks)
    lucie = _must(_compile(sales, LUCIE, AUD_LUCIE, security))
    petr = _must(_compile(sales, PETR, AUD_PETR, security))
    assert lucie.column_treatments["margin"] == CLEAR
    assert lucie.column_treatments["customer_id"] == MASK_PARTIAL
    assert petr.column_treatments["margin"] == HIDDEN
    assert petr.column_treatments["customer_id"] == CLEAR  # no applying line: clear
    assert all(t == CLEAR for c, t in petr.column_treatments.items() if c not in {"margin", "customer_id"})
    assert set(petr.column_treatments) == set(sales.columns)
    assert lucie.grants_fired == (f"show:{SHOW_1}", f"mask:margin:{MASK_1}", f"mask:customer_id:{MASK_3}")
    assert petr.grants_fired == (f"show:{SHOW_1}", f"mask:margin:{MASK_2}")


def test_hidden_columns_absent_from_relation(backend: DuckDBBackend, sales: Dataset) -> None:
    masks = (_mask(MASK_1, "margin", HIDDEN), _mask(MASK_2, "customer_id", HIDDEN))
    compiled = _must(_compile(sales, PETR, AUD_PETR, _security(_show(SHOW_1, ALL), masks=masks)))
    columns = _columns(backend, sales, compiled)
    assert "margin" not in columns and "customer_id" not in columns
    assert [c for c, _ in compiled.visible_columns] == columns
    assert columns == [c for c in sales.columns if c not in {"margin", "customer_id"}]
    assert set(compiled.absent_columns) == {"margin", "customer_id"}
    assert compiled.masked_columns == ()
    with pytest.raises(Exception, match="margin"):
        backend.execute(f"SELECT margin FROM ({compiled.relation.sql(dialect='duckdb')})")


def test_every_column_hidden_keeps_row_count(backend: DuckDBBackend, sales: Dataset) -> None:
    masks = tuple(_mask(f"3b{i:010x}", c, HIDDEN) for i, c in enumerate(sales.columns))
    compiled = _must(_compile(sales, PETR, AUD_PETR, _security(_show(SHOW_1, _filter(PUBLIC)), masks=masks)))
    assert compiled.visible_columns == ()
    rows = _run(backend, sales, compiled)
    assert len(rows) == len(_raw_ids(backend, sales, "visibility = 'public'"))
    assert not any(sales.columns[0] in r for r in rows)


def test_row_filter_may_use_hidden_column(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(
        _show(SHOW_1, _filter({"column": "margin", "op": "gt", "value": "1000"})),
        masks=(_mask(MASK_1, "margin", HIDDEN),),
    )
    compiled = _must(_compile(sales, PETR, AUD_PETR, security))
    rows = _run(backend, sales, compiled)
    assert "margin" not in rows[0]
    assert _ids(rows) == _raw_ids(backend, sales, "margin > 1000")


def test_masked_values_are_text_with_expected_format(backend: DuckDBBackend, sales: Dataset) -> None:
    masks = (_mask(MASK_1, "customer_id", MASK_PARTIAL), _mask(MASK_2, "units", MASK_HASH))
    compiled = _must(_compile(sales, PETR, AUD_PETR, _security(_show(SHOW_1, ALL), masks=masks)))
    assert set(compiled.masked_columns) == {"customer_id", "units"}
    assert dict(compiled.visible_columns)["units"] == MASK_HASH
    raw = _raw_by_id(backend, DRUG_SALES, "sale_id")
    for row in _run(backend, sales, compiled):
        source = raw[row["sale_id"]]
        assert row["customer_id"] == _partial(str(source["customer_id"]))
        assert len(str(row["customer_id"])) == len(PARTIAL_MASK_PREFIX) + PARTIAL_VISIBLE_CHARS
        assert isinstance(row["units"], str) and len(row["units"]) == SHA256_HEX_CHARS
        assert row["units"] == _hash(SALT, str(source["units"]))
        assert row["revenue"] == source["revenue"]


def test_clear_numeric_column_stays_numeric_and_aggregates(backend: DuckDBBackend, sales: Dataset) -> None:
    masks = (_mask(MASK_1, "customer_id", MASK_HASH), _mask(MASK_2, "units", MASK_PARTIAL))
    compiled = _must(_compile(sales, PETR, AUD_PETR, _security(_show(SHOW_1, _filter(PUBLIC)), masks=masks)))
    relation = compiled.relation.sql(dialect="duckdb")
    total = backend.execute(f"SELECT sum(margin), sum(revenue) FROM ({relation})").rows[0]
    expected = backend.execute("SELECT sum(margin), sum(revenue) FROM drug_sales WHERE visibility = 'public'").rows[0]
    assert total[0] == pytest.approx(expected[0])
    assert total[1] == pytest.approx(expected[1])
    types = dict(backend.execute(f"SELECT column_name, column_type FROM (DESCRIBE {relation})").rows)
    assert types["margin"] == "DOUBLE" and types["sale_id"] == "INTEGER"
    assert types["units"] == "VARCHAR" and types["customer_id"] == "VARCHAR"


def test_no_per_row_masking(backend: DuckDBBackend, sales: Dataset) -> None:
    """The treatment is per user for the whole column: own private rows are masked like public ones."""
    security = _security(
        _show(SHOW_1, _filter(PUBLIC)),
        _show(SHOW_2, _filter(OWN_COMPANY), position=2),
        masks=(_mask(MASK_1, "customer_id", MASK_PARTIAL),),
    )
    compiled = _must(_compile(sales, PETR, AUD_PETR, security))
    assert not list(compiled.relation.find_all(exp.Case))
    raw = _raw_by_id(backend, DRUG_SALES, "sale_id")
    rows = _run(backend, sales, compiled)
    assert {r["visibility"] for r in rows} == {"public", "private"}
    for row in rows:
        assert row["customer_id"] == _partial(str(raw[row["sale_id"]]["customer_id"]))


def test_masks_never_add_rows(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(_show(SHOW_1, _filter(PUBLIC)), masks=(_mask(MASK_1, "margin", CLEAR),))
    rows = _run(backend, sales, _must(_compile(sales, PETR, AUD_PETR, security)))
    assert _ids(rows) == _raw_ids(backend, sales, "visibility = 'public'")
    assert _compile(sales, PETR, AUD_PETR, _security(masks=(_mask(MASK_1, "margin", CLEAR),))) is None


def test_unknown_treatment_is_hidden(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(_show(SHOW_1, ALL), masks=(_mask(MASK_1, "margin", "reveal"),))
    plan = plan_dataset(sales, PETR, AUD_PETR, security)
    margin = next(c for c in plan.columns if c.name == "margin")
    assert margin.treatment == HIDDEN and margin.mask_id == MASK_1
    assert "unknown treatment" in margin.reason and "fail closed" in margin.reason
    compiled = _must(compile_plan(plan, salt=SALT, dialect="duckdb"))
    assert "margin" in compiled.absent_columns
    assert "margin" not in _columns(backend, sales, compiled)


# ---- audit, explanation and display -----------------------------------------------------------


def test_grants_fired_format(sales: Dataset) -> None:
    security = _security(
        _show(SHOW_1, _filter(PUBLIC)),
        _show(SHOW_2, _filter(OWN_COMPANY), position=2),
        _show(SHOW_3, ALL, position=3, subject_type=SUBJECT_USER, subject_id=LUCIE_EMAIL),
        _hide(HIDE_1, _filter(IS_TEST)),
        masks=(
            _mask(MASK_1, "customer_id", MASK_HASH),
            _mask(MASK_2, "margin", HIDDEN),
            _mask(MASK_3, "revenue", CLEAR, subject_type=SUBJECT_LOCAL_GROUP, subject_id="other-group"),
        ),
    )
    compiled = _must(_compile(sales, PETR, AUD_PETR, security))
    # Row lines in order, then masks in the dataset's column order (margin before customer_id).
    assert compiled.grants_fired == (
        f"show:{SHOW_1}",
        f"show:{SHOW_2}",
        f"hide:{HIDE_1}",
        f"mask:margin:{MASK_2}",
        f"mask:customer_id:{MASK_1}",
    )


def test_row_filter_text(sales: Dataset) -> None:
    security = _security(_show(SHOW_1, _filter(PUBLIC)), _show(SHOW_2, _filter(OWN_COMPANY), position=2), _hide(HIDE_1, _filter(IS_TEST)))
    compiled = _must(_compile(sales, PETR, AUD_PETR, security))
    assert compiled.row_filter == (
        "((visibility = 'public') OR (company = 'PHARMA_A')) AND NOT COALESCE((is_test = TRUE), TRUE)"
    )
    only_all = _must(_compile(sales, PETR, AUD_PETR, _security(_show(SHOW_1, ALL))))
    assert only_all.row_filter == "(TRUE)"
    plan = plan_dataset(sales, PETR, AUD_PETR, security)
    assert plan.predicate is not None and compiled.row_filter == plan.predicate.sql(dialect="duckdb")


def test_plan_dataset_reasons(sales: Dataset) -> None:
    security = _security(
        _show(SHOW_1, _filter(PUBLIC)),
        _show(SHOW_2, _filter(OWN_COMPANY), position=2),
        _show(SHOW_3, ALL, position=3, subject_type=SUBJECT_USER, subject_id=LUCIE_EMAIL),
        _hide(HIDE_1, _filter({"column": "company", "op": "is_not", "source": "attribute:company"})),
        _hide(HIDE_2, ALL, position=2, subject_type=SUBJECT_GROUP, subject_id=FINANCE_GROUP_ID),
        masks=(
            _mask(MASK_1, "margin", CLEAR, subject_type=SUBJECT_LOCAL_GROUP, subject_id=EMEA),
            _mask(MASK_2, "customer_id", HIDDEN, position=1),
        ),
    )
    plan = plan_dataset(sales, MARTIN, AUD_MARTIN, security)
    reasons = {line.rule.rule_id: (line.applies, line.reason) for line in plan.lines}
    assert reasons[SHOW_1] == (True, "applies: every signed-in user")
    assert reasons[SHOW_2] == (False, "does not apply: the user has no attribute 'company'")
    assert reasons[SHOW_3] == (False, f"does not apply: only for user {LUCIE_EMAIL}")
    assert reasons[HIDE_1] == (True, "hides every row: the user has no attribute 'company' (fail closed)")
    assert reasons[HIDE_2] == (False, f"does not apply: does not carry IdP group {FINANCE_GROUP_ID}")
    hide_line = next(line for line in plan.lines if line.rule.rule_id == HIDE_1)
    assert isinstance(hide_line.predicate, exp.Boolean) and hide_line.predicate.this is True
    assert all(line.predicate is None for line in plan.lines if not line.applies)
    columns = {c.name: c for c in plan.columns}
    assert columns["margin"].reason == "no masking line applies: clear" and columns["margin"].mask_id is None
    assert columns["customer_id"].reason == "line 1 applies: every signed-in user"
    assert plan.accessible

    member = plan_dataset(sales, PETR, AUD_PETR, security)
    assert {c.name: c.reason for c in member.columns}["margin"] == f"line 1 applies: member of local group {EMEA}"
    finance = plan_dataset(sales, LUCIE, AUD_LUCIE, security)
    finance_reasons = {line.rule.rule_id: line.reason for line in finance.lines}
    assert finance_reasons[SHOW_3] == "applies: this user"
    assert finance_reasons[HIDE_2] == f"applies: carries IdP group {FINANCE_GROUP_ID}"


# ---- the seeded pharma examples end to end ----------------------------------------------------


@pytest.mark.parametrize(
    ("principal", "dataset", "where", "absent"),
    [
        (PETR, DRUG_SALES, "(visibility = 'public' OR company = 'PHARMA_A') AND NOT is_test", {"margin"}),
        (LUCIE, DRUG_SALES, "NOT is_test", set()),
        (MARTIN, DRUG_SALES, "visibility = 'public' AND NOT is_test", {"margin"}),
        (JAN, SUPPLY_CHAIN, "company = 'PHARMA_B' AND visibility = 'public'", {"unit_cost", "contract_ref"}),
        (EVA, SUPPLY_CHAIN, "company = 'PHARMA_A'", set()),
        (PETR, STORES, "visibility = 'public' OR company = 'PHARMA_A'", set()),
    ],
    ids=["petr-sales", "lucie-sales", "martin-sales", "jan-supply", "eva-supply", "petr-stores"],
)
def test_seeded_examples(
    runtime: Runtime, backend: DuckDBBackend, policy: PolicySet,
    principal: Principal, dataset: str, where: str, absent: set[str],
) -> None:
    snapshot = runtime.store.snapshot()
    ds = policy.datasets[dataset]
    key = ds.columns[0]
    compiled = _must(_compile(ds, principal, snapshot.audience_of(principal), snapshot.security[dataset]))
    assert _ids(_run(backend, ds, compiled), key) == _raw_ids(backend, ds, where, key)
    assert set(compiled.absent_columns) == absent


def test_seeded_supply_chain_invisible_without_matching_line(runtime: Runtime, policy: PolicySet) -> None:
    snapshot = runtime.store.snapshot()
    ds = policy.datasets[SUPPLY_CHAIN]
    assert _compile(ds, PETR, snapshot.audience_of(PETR), snapshot.security[SUPPLY_CHAIN]) is None
    partner_without_company = Principal(STRANGER_EMAIL, frozenset({PARTNER_EXTERNAL}), {})
    audience = snapshot.audience_of(partner_without_company)
    assert _compile(ds, partner_without_company, audience, snapshot.security[SUPPLY_CHAIN]) is None


# ---- other dialects ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("principal", [PETR, LUCIE, MARTIN, JAN, EVA], ids=lambda p: p.email.split("@")[0])
def test_generated_sql_parses_back(runtime: Runtime, policy: PolicySet, dialect: str, principal: Principal) -> None:
    snapshot = runtime.store.snapshot()
    audience = snapshot.audience_of(principal)
    for name, ds in policy.datasets.items():
        compiled = _compile(ds, principal, audience, snapshot.security.get(name, EMPTY_SECURITY), dialect=dialect)
        if compiled is None:
            continue
        text = compiled.relation.sql(dialect=dialect)
        parsed = sqlglot.parse_one(text, read=dialect)
        assert isinstance(parsed, exp.Select)
        assert parsed.sql(dialect=dialect) == text
        assert sqlglot.parse_one(compiled.row_filter, read=dialect) is not None
        assert {t.name for t in parsed.find_all(exp.Table)} == {ds.source}


def test_hash_function_per_dialect(sales: Dataset) -> None:
    security = _security(_show(SHOW_1, ALL), masks=(_mask(MASK_1, "customer_id", MASK_HASH),))
    bigquery = _must(_compile(sales, PETR, AUD_PETR, security, dialect="bigquery")).relation.sql(dialect="bigquery")
    snowflake = _must(_compile(sales, PETR, AUD_PETR, security, dialect="snowflake")).relation.sql(dialect="snowflake")
    duckdb = _must(_compile(sales, PETR, AUD_PETR, security, dialect="duckdb")).relation.sql(dialect="duckdb")
    assert "TO_HEX(SHA256(" in bigquery
    assert "SHA2(" in snowflake
    assert "SHA256(" in duckdb


def test_typed_literals_in_every_dialect(sales: Dataset) -> None:
    row_filter = _filter(
        {"column": "units", "op": "gte", "value": "5"},
        {"column": "is_test", "op": "is", "value": "false"},
        {"column": "sale_date", "op": "lt", "value": "2026-06-01"},
    )
    for dialect in DIALECTS:
        compiled = _must(_compile(sales, PETR, AUD_PETR, _security(_show(SHOW_1, row_filter)), dialect=dialect))
        parsed = sqlglot.parse_one(compiled.row_filter, read=dialect)
        numbers = [lit.this for lit in parsed.find_all(exp.Literal) if not lit.is_string]
        strings = [lit.this for lit in parsed.find_all(exp.Literal) if lit.is_string]
        assert "5" in numbers and "2026-06-01" in strings, dialect
        assert any(b.this is False for b in parsed.find_all(exp.Boolean)), dialect


def test_inner_relation_is_aliased(sales: Dataset) -> None:
    compiled = _must(_compile(sales, PETR, AUD_PETR, _security(_show(SHOW_1, ALL))))
    assert "AS _secured" in compiled.relation.sql(dialect="duckdb")


# ---- attribute values and literals cannot widen access ----------------------------------------

HOSTILE_VALUES = [
    "' OR '1'='1",
    "PHARMA_B' OR 'a'='a",
    "PHARMA_A' OR visibility = 'private' --",
    "PHARMA_B\\",
    "x\\' OR 1=1 --",
    "x'); SELECT * FROM companies; --",
    "PHARMA_B\n",
    "ＰＨＡＲＭＡ＿Ｂ",
    "$$ OR TRUE $$",
    "x' /*",
    "PHARMA_A'||'",
    "%",
    "\" OR \"1\"=\"1",
]

PUBLIC_PLUS_OWN = _security(_show(SHOW_1, _filter(PUBLIC)), _show(SHOW_2, _filter(OWN_COMPANY), position=2))


@pytest.mark.parametrize("value", HOSTILE_VALUES)
def test_attribute_value_cannot_widen_rows(backend: DuckDBBackend, sales: Dataset, value: str) -> None:
    nobody = Principal(STRANGER_EMAIL, frozenset(), {"company": "NO_SUCH_COMPANY"})
    baseline = _run(backend, sales, _must(_compile(sales, nobody, AUD_STRANGER, PUBLIC_PLUS_OWN)))
    hostile = Principal(STRANGER_EMAIL, frozenset(), {"company": value})
    rows = _run(backend, sales, _must(_compile(sales, hostile, AUD_STRANGER, PUBLIC_PLUS_OWN)))
    assert _ids(rows) == _ids(baseline)
    assert {r["visibility"] for r in rows} == {"public"}


@pytest.mark.parametrize("value", HOSTILE_VALUES)
def test_attribute_value_in_hide_line_cannot_unhide(backend: DuckDBBackend, sales: Dataset, value: str) -> None:
    # Hide every row not of the user's company: a hostile value must hide everything, never nothing.
    security = _security(_show(SHOW_1, ALL), _hide(HIDE_1, _filter({"column": "company", "op": "is_not", "source": "attribute:company"})))
    hostile = Principal(STRANGER_EMAIL, frozenset(), {"company": value})
    assert _run(backend, sales, _must(_compile(sales, hostile, AUD_STRANGER, security))) == []


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("value", HOSTILE_VALUES)
def test_attribute_value_round_trips_as_one_literal(sales: Dataset, dialect: str, value: str) -> None:
    hostile = Principal(STRANGER_EMAIL, frozenset(), {"company": value})
    compiled = _must(_compile(sales, hostile, AUD_STRANGER, PUBLIC_PLUS_OWN, dialect=dialect))
    parsed = sqlglot.parse_one(compiled.relation.sql(dialect=dialect), read=dialect)
    literals = [lit.this for lit in parsed.find_all(exp.Literal) if lit.is_string]
    assert value in literals
    assert {t.name for t in parsed.find_all(exp.Table)} == {DRUG_SALES}
    filter_literals = [lit.this for lit in sqlglot.parse_one(compiled.row_filter, read=dialect).find_all(exp.Literal)]
    assert sorted(filter_literals) == sorted(["public", value])


NUMERIC_INJECTIONS = ["1 OR 1=1", "0) OR (1=1", "1; DROP TABLE drug_sales", "1--", "0x10", "1e", "NaN", "Infinity", "1 ", "--1"]


@pytest.mark.parametrize("value", NUMERIC_INJECTIONS)
def test_numeric_literal_injection_is_refused(backend: DuckDBBackend, sales: Dataset, value: str) -> None:
    source_condition = Condition("units", "gte", source="attribute:min_units")
    hostile = Principal(STRANGER_EMAIL, frozenset(), {"min_units": value})
    if value.strip() == "1":
        # Surrounding whitespace is trimmed, the number itself is fine.
        assert bind_condition(sales, source_condition, hostile).sql(dialect="duckdb") == "units >= 1"
        return
    with pytest.raises(Unresolvable):
        bind_condition(sales, source_condition, hostile)
    with pytest.raises(Unresolvable):
        bind_condition(sales, Condition("units", "is", value=value), PETR)
    with pytest.raises(Unresolvable):
        bind_condition(sales, Condition("units", "one_of", values=("1", value)), PETR)
    # As a show line it does not apply; as a hide line it hides every row.
    show = _security(_show(SHOW_1, RowFilter(False, (source_condition,))))
    assert _compile(sales, hostile, AUD_STRANGER, show) is None
    hide = _security(_show(SHOW_1, ALL), _hide(HIDE_1, RowFilter(False, (source_condition,))))
    assert _run(backend, sales, _must(_compile(sales, hostile, AUD_STRANGER, hide))) == []


def test_salt_never_in_row_filter_and_hashes_follow_the_salt(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(
        _show(SHOW_1, _filter(PUBLIC)),
        _hide(HIDE_1, _filter(IS_TEST)),
        masks=(_mask(MASK_1, "customer_id", MASK_HASH), _mask(MASK_2, "store_id", MASK_HASH)),
    )
    first = _must(_compile(sales, PETR, AUD_PETR, security))
    again = _must(_compile(sales, PETR, AUD_PETR, security))
    rotated = _must(_compile(sales, PETR, AUD_PETR, security, salt=OTHER_SALT))
    for dialect in DIALECTS:
        compiled = _must(_compile(sales, PETR, AUD_PETR, security, dialect=dialect))
        assert SALT not in compiled.row_filter
    assert SALT not in first.row_filter and OTHER_SALT not in rotated.row_filter
    a1 = {r["sale_id"]: r["customer_id"] for r in _run(backend, sales, first)}
    a2 = {r["sale_id"]: r["customer_id"] for r in _run(backend, sales, again)}
    b = {r["sale_id"]: r["customer_id"] for r in _run(backend, sales, rotated)}
    assert a1 and a1 == a2
    assert all(a1[k] != b[k] for k in a1)
    raw = _raw_by_id(backend, DRUG_SALES, "sale_id")
    for k, hashed in a1.items():
        assert hashed == _hash(SALT, str(raw[k]["customer_id"]))
        # An unsalted hash must not match (no rainbow-table shortcut).
        assert hashed != hashlib.sha256(str(raw[k]["customer_id"]).encode("utf-8")).hexdigest()


def test_mask_hash_is_joinable_across_rows(backend: DuckDBBackend, sales: Dataset) -> None:
    security = _security(_show(SHOW_1, ALL), masks=(_mask(MASK_1, "store_id", MASK_HASH),))
    rows = _run(backend, sales, _must(_compile(sales, PETR, AUD_PETR, security)))
    raw = _raw_by_id(backend, DRUG_SALES, "sale_id")
    by_raw: dict[object, set[object]] = {}
    for row in rows:
        by_raw.setdefault(raw[row["sale_id"]]["store_id"], set()).add(row["store_id"])
    assert len(by_raw) > 1
    assert all(len(v) == 1 for v in by_raw.values())
    assert len({next(iter(v)) for v in by_raw.values()}) == len(by_raw)


def test_salt_never_in_display_sql(runtime: Runtime) -> None:
    guard = build_guard(runtime, "sales")
    salt = runtime.settings.secrets.mask_salt
    # Lucie is in finance: the seeded examples hash her customer_id with the salt.
    result = guard.run(LUCIE, "SELECT customer_id FROM drug_sales")
    assert result.error is None
    assert result.display_sql is not None
    assert salt not in result.display_sql
    assert SALT_REDACTION in result.display_sql
    failing = guard.run(LUCIE, "SELECT customer_id, no_such_column FROM drug_sales")
    assert failing.error is not None
    assert salt not in failing.error
    assert failing.display_sql is not None and salt not in failing.display_sql


def test_salt_with_quote_is_refused(runtime: Runtime) -> None:
    # A salt that would need escaping as a SQL literal is refused up front, so
    # redaction by exact text match is always complete.
    base = build_guard(runtime, "sales")
    with pytest.raises(ValueError, match="salt"):
        DataGuard(
            base.policy, base._backend, runtime.audit,
            salt="o'brien-salt-0123456789", app_id="sales", max_query_chars=100_000,
            security=lambda p: runtime.gate.snapshot().security_for(p),
        )


def test_nul_byte_in_attribute_fails_closed(runtime: Runtime) -> None:
    guard = build_guard(runtime, "sales")
    result = guard.run(Principal(STRANGER_EMAIL, frozenset(), {"company": "x\x00y"}), "SELECT count(*) FROM drug_sales")
    # Either a clean error or baseline rows; never more.
    if result.error is None:
        assert result.result is not None
        baseline = guard.run(Principal(STRANGER_EMAIL, frozenset(), {"company": "NOPE"}), "SELECT count(*) FROM drug_sales")
        assert baseline.result is not None
        assert result.result.rows == baseline.result.rows
