"""Layer 3c: the rewriter — adversarial suite.

Every attack is executed through DataGuard on the seeded warehouse. Each test
asserts either a clean QueryRejected (error, no result) or that the executed
result contains only rows/columns the policy allows, compared with the
principal's secured relation (itself checked against raw data in
test_compiler.py).

Decisions documented here (current behaviour, asserted below):
* every table function in FROM is refused, including harmless generators such
  as range() / generate_series(); unnest() and VALUES are allowed because they
  read no relation, but a query must still read at least one dataset;
* statements other than SELECT / set operations are refused outright,
  including SUMMARIZE, DESCRIBE, SHOW, PIVOT-statement, EXPLAIN, CALL;
* trailing semicolons and comments are fine; a second statement is not.
"""

from __future__ import annotations

import shutil
import textwrap
from collections import Counter
from pathlib import Path

import pytest

from kbc_access.audit import AuditLog
from kbc_access.backends.duckdb_backend import DuckDBBackend
from kbc_access.compiler import PARTIAL_MASK_PREFIX, compile_dataset
from kbc_access.config import Settings, load_settings
from kbc_access.guard import DataGuard, GuardedResult
from kbc_access.identity import Principal
from kbc_access.policy import PolicySet, load_policy
from kbc_access.rewriter import QueryRejected, rewrite
from kbc_access.security_model import HIDDEN, KIND_SHOW, SUBJECT_EVERYONE, parse_filter
from kbc_access.store import (
    EMPTY_SECURITY,
    AccessStore,
    AclSnapshot,
    ColumnMask,
    DatasetSecurity,
    PrincipalSecurity,
    RowAccessRule,
)
from tests.conftest import settings_in

ROOT = Path(__file__).resolve().parent.parent
TEST_SECRETS = {
    "GATEWAY_SESSION_SECRET": "test-session-secret-0123456789",
    "MASK_SALT": "test-mask-salt-0123456789abcdef",
}
FINANCE = "3f2b8c1e-7d4a-4e5b-9a61-2c8d0f4e1b7a"
SALES_MGR = "8a1d4e2f-5b6c-4d7e-9f80-1a2b3c4d5e6f"

PETR = Principal("petr.sales@pharma-a.example", frozenset({SALES_MGR}), {"company": "PHARMA_A"})
LUCIE = Principal("lucie.finance@pharma-a.example", frozenset({FINANCE}), {"company": "PHARMA_A"})
MARTIN = Principal("martin.noattr@pharma-c.example", frozenset({SALES_MGR}), {})
# Big enough that no comparison is truncated.
MAX_ROWS = 100_000
MAX_QUERY_CHARS = 100_000


@pytest.fixture(scope="module")
def mod_seeded(tmp_path_factory: pytest.TempPathFactory, shared_warehouse: Path, store_template: Path) -> Settings:
    """Module settings over the session's shared, read-only warehouse and a copy of the seeded store.

    These tests only read both: the store carries the pharma example row lines and masks.
    """
    tmp = tmp_path_factory.mktemp("rewriter")
    settings = settings_in(load_settings(ROOT / "config" / "local.toml", TEST_SECRETS), tmp, shared_warehouse)
    shutil.copyfile(store_template, settings.path(settings.store.sqlite_path))
    return settings


@pytest.fixture(scope="module")
def snapshot(mod_seeded: Settings) -> AclSnapshot:
    """One ACL snapshot of the seeded store (row lines, masks, groups) for the whole module."""
    return AccessStore(mod_seeded.path(mod_seeded.store.sqlite_path), mod_seeded.providers).snapshot()


@pytest.fixture(scope="module")
def backend(mod_seeded: Settings) -> DuckDBBackend:
    return DuckDBBackend(mod_seeded.path(mod_seeded.data.duckdb_path), MAX_ROWS)


@pytest.fixture(scope="module")
def policy(mod_seeded: Settings, backend: DuckDBBackend) -> PolicySet:
    return load_policy([mod_seeded.path(f) for f in mod_seeded.policy.files], backend.column_types())


@pytest.fixture(scope="module")
def guard(mod_seeded: Settings, backend: DuckDBBackend, policy: PolicySet, snapshot: AclSnapshot) -> DataGuard:
    audit = AuditLog(mod_seeded.path(mod_seeded.audit.path))
    return DataGuard(
        policy, backend, audit, salt=mod_seeded.secrets.mask_salt, app_id="sales",
        max_query_chars=MAX_QUERY_CHARS, security=snapshot.security_for,
    )


def _rows(result: GuardedResult) -> list[dict[str, object]]:
    assert result.error is None, result.error
    assert result.result is not None and not result.result.truncated
    return [dict(zip(result.result.columns, row)) for row in result.result.rows]


def _truth(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend, who: Principal, dataset: str) -> list[dict[str, object]]:
    """The principal's secured relation for `dataset`, compiled from the store's lines and executed directly."""
    view = snapshot.security_for(who)
    compiled = compile_dataset(
        guard.policy.datasets[dataset], who, view.audience, view.datasets.get(dataset, EMPTY_SECURITY),
        salt=TEST_SECRETS["MASK_SALT"], dialect="duckdb",
    )
    assert compiled is not None
    result = backend.execute(compiled.relation.sql(dialect="duckdb"))
    return [dict(zip(result.columns, row)) for row in result.rows]


def _as_bag(rows: list[dict[str, object]]) -> Counter[tuple[tuple[str, object], ...]]:
    return Counter(tuple(sorted(r.items())) for r in rows)


def _rejected(guard: DataGuard, who: Principal, sql: str) -> str:
    """Assert a clean rejection by both the rewriter and the guard; return the message."""
    with pytest.raises(QueryRejected) as info:
        rewrite(sql, guard.secured(who), max_chars=MAX_QUERY_CHARS, dialect="duckdb")
    result = guard.run(who, sql)
    assert result.result is None and result.error == str(info.value)
    assert result.display_sql is None and result.datasets == ()
    return str(info.value)


# ---- plain reads ------------------------------------------------------------


def test_select_star_equals_secured_relation(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "SELECT * FROM drug_sales"))
    assert _as_bag(rows) == _as_bag(_truth(snapshot, guard, backend, PETR, "drug_sales"))


def test_select_star_exposes_only_allowed_columns(guard: DataGuard) -> None:
    # Seeded masks: margin is hidden for everyone outside finance, customer_id partially masked.
    rows = _rows(guard.run(MARTIN, "SELECT * FROM drug_sales"))
    assert rows and all("margin" not in r for r in rows)
    customer_ids = [r["customer_id"] for r in rows if r["customer_id"] is not None]
    assert customer_ids and all(str(c).startswith(PARTIAL_MASK_PREFIX) for c in customer_ids)
    cols = guard.run(MARTIN, "SELECT COLUMNS(*) FROM drug_sales LIMIT 1").result
    assert cols is not None and "margin" not in cols.columns


@pytest.mark.parametrize("sql", ["SELECT margin FROM drug_sales", "SELECT count(*) FROM drug_sales WHERE margin IS NOT NULL", "SELECT sum(margin) FROM drug_sales"])
def test_selecting_absent_column_fails_at_execution(guard: DataGuard, sql: str) -> None:
    result = guard.run(MARTIN, sql)
    assert result.result is None
    assert result.error is not None and result.error.startswith("Query failed:")
    assert result.datasets == ("drug_sales",)


def test_hidden_dataset_same_error_as_nonexistent(guard: DataGuard) -> None:
    hidden = _rejected(guard, PETR, "SELECT * FROM supply_chain")
    missing = _rejected(guard, PETR, "SELECT * FROM no_such_thing")
    assert hidden.replace("supply_chain", "X") == missing.replace("no_such_thing", "X")
    # A physical table outside the policy is no different.
    assert _rejected(guard, PETR, "SELECT * FROM companies").replace("companies", "X") == missing.replace("no_such_thing", "X")


@pytest.mark.parametrize("name", ['"drug_sales"', '"DRUG_SALES"', "DRUG_SALES", "Drug_Sales", '"Drug_Sales"'])
def test_quoted_and_case_variations(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend, name: str) -> None:
    rows = _rows(guard.run(PETR, f"SELECT sale_id FROM {name}"))
    assert sorted(r["sale_id"] for r in rows) == sorted(r["sale_id"] for r in _truth(snapshot, guard, backend, PETR, "drug_sales"))  # type: ignore[type-var]


def test_nested_subqueries(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "SELECT * FROM (SELECT * FROM (SELECT * FROM (SELECT * FROM drug_sales) a) b) c"))
    assert _as_bag(rows) == _as_bag(_truth(snapshot, guard, backend, PETR, "drug_sales"))


def test_parenthesised_table(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "SELECT * FROM (drug_sales)"))
    assert _as_bag(rows) == _as_bag(_truth(snapshot, guard, backend, PETR, "drug_sales"))


def test_from_first_syntax(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    assert _as_bag(_rows(guard.run(PETR, "FROM drug_sales"))) == _as_bag(_truth(snapshot, guard, backend, PETR, "drug_sales"))
    # customer_id is masked for PETR: the projection must carry the masked values, never the raw ones.
    masked = _rows(guard.run(PETR, "FROM drug_sales SELECT sale_id, customer_id"))
    truth = {r["sale_id"]: r["customer_id"] for r in _truth(snapshot, guard, backend, PETR, "drug_sales")}
    assert {r["sale_id"]: r["customer_id"] for r in masked} == truth


# ---- CTEs -------------------------------------------------------------------


def test_cte_body_is_rewritten(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "WITH x AS (SELECT * FROM drug_sales) SELECT * FROM x"))
    assert _as_bag(rows) == _as_bag(_truth(snapshot, guard, backend, PETR, "drug_sales"))


def test_cte_referencing_cte(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "WITH a AS (SELECT * FROM drug_sales), b AS (SELECT * FROM a WHERE units > 0) SELECT * FROM b"))
    assert _as_bag(rows) == _as_bag(_truth(snapshot, guard, backend, PETR, "drug_sales"))


def test_materialized_cte(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "WITH x AS MATERIALIZED (SELECT * FROM drug_sales) SELECT count(*) AS n FROM x"))
    assert rows == [{"n": len(_truth(snapshot, guard, backend, PETR, "drug_sales"))}]


@pytest.mark.parametrize("name", ["drug_sales", "DRUG_SALES", '"Drug_Sales"', "stores"])
def test_cte_named_like_a_dataset_only_shadows_with_its_own_rows(guard: DataGuard, name: str) -> None:
    # CTEs are renamed internally, so a CTE called like a dataset is just the user's own
    # relation: it yields the literal rows it defines and never the physical table.
    # Read some *other* dataset too; inside this query `{name}` means the CTE everywhere.
    other = "stores" if name.strip('"').lower() == "drug_sales" else "drug_sales"
    rows = _rows(guard.run(PETR, f"WITH {name} AS (SELECT 'PHARMA_B' AS company) SELECT c.company FROM {name} c, {other} LIMIT 1"))
    assert rows == [{"company": "PHARMA_B"}]
    # Without any real dataset read, the query is refused rather than run.
    assert "at least one dataset" in _rejected(guard, PETR, f"WITH {name} AS (SELECT 1 AS x) SELECT * FROM {name}")


@pytest.mark.parametrize("name", ["drug_sales", "stores"])
def test_cte_wrapping_the_dataset_of_the_same_name_reads_the_secured_relation(
    snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend, name: str
) -> None:
    rows = _rows(guard.run(PETR, f"WITH {name} AS (SELECT * FROM {name}) SELECT count(*) AS n FROM {name}"))
    assert rows == [{"n": len(_truth(snapshot, guard, backend, PETR, name))}]


def test_cte_named_like_hidden_dataset_is_harmless(guard: DataGuard) -> None:
    rows = _rows(guard.run(PETR, "WITH supply_chain AS (SELECT 1 AS x) SELECT count(*) AS n FROM supply_chain, drug_sales WHERE drug_sales.visibility = 'none'"))
    assert rows == [{"n": 0}]


def test_cte_named_like_hidden_dataset_indistinguishable_from_unknown_name(guard: DataGuard) -> None:
    hidden = guard.run(PETR, "WITH supply_chain AS (SELECT 1 AS x) SELECT count(*) FROM drug_sales")
    unknown = guard.run(PETR, "WITH totally_unknown AS (SELECT 1 AS x) SELECT count(*) FROM drug_sales")
    assert (hidden.error is None) == (unknown.error is None)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM drug_sales WHERE sale_id IN (WITH x AS (SELECT 1) SELECT * FROM x)",
        "SELECT * FROM (WITH drug_sales AS (SELECT 1 AS x) SELECT * FROM drug_sales) t, drug_sales",
        "WITH a AS (WITH b AS (SELECT * FROM drug_sales) SELECT * FROM b) SELECT * FROM a",
        "WITH x AS (SELECT 1) (SELECT * FROM x) UNION (WITH y AS (SELECT 2) SELECT * FROM y)",
        "SELECT (WITH c AS (SELECT 1 AS v) SELECT v FROM c) FROM drug_sales",
    ],
)
def test_nested_with_rejected(guard: DataGuard, sql: str) -> None:
    assert "WITH" in _rejected(guard, PETR, sql)


def test_legit_recursive_cte(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    sql = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT count(*) AS c FROM r, drug_sales"
    assert _rows(guard.run(PETR, sql)) == [{"c": 3 * len(_truth(snapshot, guard, backend, PETR, "drug_sales"))}]


# The CTE-scope bypass: rewriter.py skips every table reference whose name is ANY
# CTE name in the query, but SQL scoping says a CTE is not visible inside its own
# (non-recursive) body nor inside earlier CTEs. DuckDB resolves those references
# to the physical relation, which is then read unrewritten.
CTE_SCOPE_BYPASS = [
    "WITH companies AS (SELECT * FROM companies) SELECT c.* FROM companies c, drug_sales",
    "WITH RECURSIVE companies AS (SELECT * FROM companies) SELECT companies.* FROM companies, drug_sales",
    "WITH a AS (SELECT * FROM companies), companies AS (SELECT 1 AS x) SELECT a.* FROM a, drug_sales",
    "WITH a AS (SELECT * FROM sqlite_master), sqlite_master AS (SELECT 1) SELECT a.* FROM a, drug_sales",
    "WITH a AS (SELECT * FROM duckdb_tables), duckdb_tables AS (SELECT 1) SELECT a.* FROM a, drug_sales",
    "WITH a AS (SELECT * FROM pg_tables), pg_tables AS (SELECT 1) SELECT a.* FROM a, drug_sales",
    "WITH a AS (SELECT * FROM pg_settings), pg_settings AS (SELECT 1) SELECT a.* FROM a, drug_sales",
]


@pytest.mark.parametrize("sql", CTE_SCOPE_BYPASS)
def test_cte_scope_bypass_reaches_physical_relations(guard: DataGuard, backend: DuckDBBackend, sql: str) -> None:
    result = guard.run(PETR, sql)
    assert result.error is not None, f"accepted and returned {result.result.rows[:1] if result.result else None}"


RENAMED_DATASET = "sales"
RENAMED_SOURCE = "drug_sales"
LINE_ID_PUBLIC = "aaaaaaaaaaa1"
MASK_ID_MARGIN = "bbbbbbbbbbb1"


def test_cte_scope_bypass_reads_raw_source_of_protected_dataset(
    snapshot: AclSnapshot, backend: DuckDBBackend, tmp_path: Path
) -> None:
    # A catalogue where the dataset name differs from its source table, secured by one
    # show line (public rows for everyone) and one mask line (margin hidden for everyone).
    columns = backend.column_types()[RENAMED_SOURCE]
    path = tmp_path / "renamed.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            version: 2
            datasets:
              {RENAMED_DATASET}:
                source: {RENAMED_SOURCE}
                description: renamed dataset
                columns: [{", ".join(columns)}]
                types: {{sale_id: numeric, sale_date: date, units: numeric, revenue: numeric, margin: numeric, is_test: boolean}}
            """
        ),
        encoding="utf-8",
    )
    renamed = load_policy([path], backend.column_types())
    security = DatasetSecurity(
        rows=(
            RowAccessRule(
                rule_id=LINE_ID_PUBLIC, dataset=RENAMED_DATASET, position=0, kind=KIND_SHOW,
                subject_type=SUBJECT_EVERYONE, subject_id="",
                filter=parse_filter({"conditions": [{"column": "visibility", "op": "is", "value": "public"}]}),
            ),
        ),
        masks=(
            ColumnMask(
                mask_id=MASK_ID_MARGIN, dataset=RENAMED_DATASET, column_name="margin", position=0,
                subject_type=SUBJECT_EVERYONE, subject_id="", treatment=HIDDEN,
            ),
        ),
    )

    def lines(principal: Principal) -> PrincipalSecurity:
        return PrincipalSecurity(audience=snapshot.audience_of(principal), datasets={RENAMED_DATASET: security})

    g = DataGuard(
        renamed, backend, AuditLog(tmp_path / "a.jsonl"), salt=TEST_SECRETS["MASK_SALT"], app_id="sales",
        max_query_chars=MAX_QUERY_CHARS, security=lines,
    )
    ok = _rows(g.run(PETR, f"SELECT DISTINCT visibility FROM {RENAMED_DATASET}"))
    assert ok == [{"visibility": "public"}]
    attack = g.run(
        PETR,
        f"WITH {RENAMED_SOURCE} AS (SELECT * FROM {RENAMED_SOURCE}) "
        f"SELECT DISTINCT d.visibility, d.margin IS NOT NULL AS has_margin FROM {RENAMED_SOURCE} d, "
        f"(SELECT 1 FROM {RENAMED_DATASET} LIMIT 1) s",
    )
    if attack.error is None:
        rows = _rows(attack)
        assert {r["visibility"] for r in rows} == {"public"}
        assert not any(r["has_margin"] for r in rows)


# ---- set operations -----------------------------------------------------------


@pytest.mark.parametrize("op", ["UNION", "UNION ALL", "INTERSECT", "EXCEPT"])
def test_set_operations_rewrite_every_branch(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend, op: str) -> None:
    visible = {r["sale_id"] for r in _truth(snapshot, guard, backend, PETR, "drug_sales")}
    rows = _rows(guard.run(PETR, f"SELECT sale_id FROM drug_sales {op} SELECT sale_id FROM drug_sales WHERE sale_id > 100"))
    got = [r["sale_id"] for r in rows]
    expected = {
        "UNION": visible,
        "UNION ALL": visible,
        "INTERSECT": {s for s in visible if s > 100},  # type: ignore[operator]
        "EXCEPT": {s for s in visible if s <= 100},  # type: ignore[operator]
    }[op]
    assert set(got) == expected


def test_union_branch_with_hidden_dataset_rejected(guard: DataGuard) -> None:
    _rejected(guard, PETR, "SELECT company FROM drug_sales UNION SELECT company FROM supply_chain")
    _rejected(guard, PETR, "SELECT company FROM drug_sales UNION SELECT company FROM companies")
    _rejected(guard, PETR, "(SELECT company FROM drug_sales) EXCEPT (SELECT company FROM (SELECT * FROM supply_chain))")


def test_union_by_name(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "SELECT count(*) AS n FROM drug_sales UNION ALL BY NAME SELECT count(*) AS n FROM stores"))
    assert sorted(r["n"] for r in rows) == sorted([len(_truth(snapshot, guard, backend, PETR, "drug_sales")), len(_truth(snapshot, guard, backend, PETR, "stores"))])  # type: ignore[type-var]


# ---- joins, aliases, correlation --------------------------------------------------


def test_join_with_aliases(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(
        guard.run(PETR, "SELECT d.sale_id, s.name, s.manager_email FROM drug_sales d JOIN stores s ON d.store_id = s.store_id")
    )
    sales = _truth(snapshot, guard, backend, PETR, "drug_sales")
    stores = {r["store_id"]: r for r in _truth(snapshot, guard, backend, PETR, "stores")}
    expected = Counter(
        (d["sale_id"], stores[d["store_id"]]["name"], stores[d["store_id"]]["manager_email"]) for d in sales if d["store_id"] in stores
    )
    assert Counter((r["sale_id"], r["name"], r["manager_email"]) for r in rows) == expected


def test_join_using(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "SELECT sale_id, name FROM drug_sales JOIN stores USING (store_id)"))
    stores = {r["store_id"]: r["name"] for r in _truth(snapshot, guard, backend, PETR, "stores")}
    expected = Counter((d["sale_id"], stores[d["store_id"]]) for d in _truth(snapshot, guard, backend, PETR, "drug_sales") if d["store_id"] in stores)
    assert Counter((r["sale_id"], r["name"]) for r in rows) == expected


def test_self_join(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "SELECT count(*) AS n FROM drug_sales a JOIN drug_sales b ON a.sale_id = b.sale_id"))
    assert rows == [{"n": len(_truth(snapshot, guard, backend, PETR, "drug_sales"))}]


def test_table_alias_preserved(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "SELECT s.company, s.revenue FROM drug_sales s WHERE s.company = 'PHARMA_A' AND s.visibility = 'private'"))
    assert rows and all(r["company"] == "PHARMA_A" for r in rows)
    rows = _rows(guard.run(PETR, "SELECT drug_sales.sale_id FROM drug_sales"))
    assert len(rows) == len(_truth(snapshot, guard, backend, PETR, "drug_sales"))


def test_alias_named_like_other_dataset(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    # `stores` here is an alias for drug_sales; the result must be drug_sales' secured rows.
    rows = _rows(guard.run(PETR, "SELECT stores.* FROM drug_sales AS stores"))
    assert _as_bag(rows) == _as_bag(_truth(snapshot, guard, backend, PETR, "drug_sales"))
    rows = _rows(guard.run(PETR, 'SELECT * FROM drug_sales AS "companies"'))
    assert _as_bag(rows) == _as_bag(_truth(snapshot, guard, backend, PETR, "drug_sales"))


def test_alias_named_like_hidden_dataset_or_physical_table(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "SELECT supply_chain.sale_id FROM drug_sales AS supply_chain"))
    assert len(rows) == len(_truth(snapshot, guard, backend, PETR, "drug_sales"))
    _rejected(guard, PETR, "SELECT * FROM drug_sales, companies AS drug_sales")


def test_correlated_exists(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(
        guard.run(PETR, "SELECT s.store_id FROM stores s WHERE EXISTS (SELECT 1 FROM drug_sales d WHERE d.store_id = s.store_id AND d.visibility = 'private')")
    )
    private_stores = {d["store_id"] for d in _truth(snapshot, guard, backend, PETR, "drug_sales") if d["visibility"] == "private"}
    visible_stores = {s["store_id"] for s in _truth(snapshot, guard, backend, PETR, "stores")}
    assert {r["store_id"] for r in rows} == private_stores & visible_stores


def test_correlated_scalar_subquery_in_select(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(
        guard.run(PETR, "SELECT s.store_id, (SELECT count(*) FROM drug_sales d WHERE d.store_id = s.store_id) AS n FROM stores s")
    )
    counts = Counter(d["store_id"] for d in _truth(snapshot, guard, backend, PETR, "drug_sales"))
    assert {r["store_id"]: r["n"] for r in rows} == {s["store_id"]: counts.get(s["store_id"], 0) for s in _truth(snapshot, guard, backend, PETR, "stores")}


def test_uncorrelated_subqueries(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "SELECT (SELECT count(*) FROM drug_sales) AS n FROM stores LIMIT 1"))
    assert rows == [{"n": len(_truth(snapshot, guard, backend, PETR, "drug_sales"))}]
    rows = _rows(guard.run(PETR, "SELECT store_id FROM stores WHERE store_id IN (SELECT store_id FROM drug_sales WHERE visibility = 'private')"))
    assert {r["store_id"] for r in rows} <= {s["store_id"] for s in _truth(snapshot, guard, backend, PETR, "stores")}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM drug_sales WHERE EXISTS (SELECT 1 FROM supply_chain)",
        "SELECT * FROM drug_sales WHERE company = ANY(SELECT company FROM supply_chain)",
        "SELECT (SELECT max(name) FROM companies) FROM drug_sales",
        "SELECT * FROM drug_sales, LATERAL (SELECT * FROM companies)",
        "SELECT * FROM drug_sales POSITIONAL JOIN companies",
        "SELECT * FROM drug_sales ANTI JOIN supply_chain USING (company)",
        "SELECT * FROM drug_sales LIMIT (SELECT count(*) FROM companies)",
        "SELECT * FROM drug_sales ORDER BY (SELECT 1 FROM companies LIMIT 1)",
        "SELECT sum(revenue) FILTER (WHERE company IN (SELECT company FROM companies)) FROM drug_sales",
        "SELECT * FROM unnest((SELECT list(name) FROM companies)), drug_sales",
        "SELECT * FROM drug_sales, (FROM companies)",
    ],
)
def test_hidden_relation_anywhere_rejected(guard: DataGuard, sql: str) -> None:
    _rejected(guard, PETR, sql)


def test_lateral_and_semi_joins_on_visible_datasets(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "SELECT d.sale_id FROM drug_sales d SEMI JOIN stores s ON d.store_id = s.store_id"))
    stores = {s["store_id"] for s in _truth(snapshot, guard, backend, PETR, "stores")}
    assert {r["sale_id"] for r in rows} == {d["sale_id"] for d in _truth(snapshot, guard, backend, PETR, "drug_sales") if d["store_id"] in stores}
    rows = _rows(guard.run(PETR, "SELECT s.store_id, x.n FROM stores s, LATERAL (SELECT count(*) AS n FROM drug_sales d WHERE d.store_id = s.store_id) x"))
    assert {r["store_id"] for r in rows} == stores


def test_natural_join(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "SELECT count(*) AS n FROM stores NATURAL JOIN drug_sales"))
    stores = {(s["store_id"], s["company"], s["visibility"]) for s in _truth(snapshot, guard, backend, PETR, "stores")}
    expected = sum(1 for d in _truth(snapshot, guard, backend, PETR, "drug_sales") if (d["store_id"], d["company"], d["visibility"]) in stores)
    assert rows == [{"n": expected}]


# ---- other DuckDB syntax ----------------------------------------------------------------


def test_qualify(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    rows = _rows(guard.run(PETR, "SELECT company, sale_id FROM drug_sales QUALIFY row_number() OVER (PARTITION BY company ORDER BY sale_id) = 1"))
    firsts: dict[object, object] = {}
    for r in sorted(_truth(snapshot, guard, backend, PETR, "drug_sales"), key=lambda r: r["sale_id"]):  # type: ignore[arg-type, return-value]
        firsts.setdefault(r["company"], r["sale_id"])
    assert {r["company"]: r["sale_id"] for r in rows} == firsts


def test_values_and_unnest_allowed_next_to_a_dataset(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    n = len(_truth(snapshot, guard, backend, PETR, "drug_sales"))
    assert _rows(guard.run(PETR, "SELECT count(*) AS c FROM (VALUES (1), (2)) v(x), drug_sales")) == [{"c": 2 * n}]
    assert _rows(guard.run(PETR, "SELECT count(*) AS c FROM unnest([1, 2, 3]) t(x), drug_sales")) == [{"c": 3 * n}]


@pytest.mark.parametrize("sql", ["SELECT 1", "SELECT * FROM (VALUES (1)) v(x)", "SELECT * FROM unnest([1, 2])", "VALUES (1)", "SELECT version()"])
def test_query_reading_no_dataset_rejected(guard: DataGuard, sql: str) -> None:
    _rejected(guard, PETR, sql)


def test_pivot_clause_never_widens(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    result = guard.run(PETR, "SELECT * FROM drug_sales PIVOT (sum(revenue) FOR company IN ('PHARMA_A', 'PHARMA_B', 'PHARMA_C'))")
    if result.error is None:
        rows = _rows(result)
        cols = set(rows[0]) if rows else set()
        truth = _truth(snapshot, guard, backend, PETR, "drug_sales")
        if "PHARMA_B" in cols:
            total_b = sum(r["PHARMA_B"] or 0 for r in rows)  # type: ignore[misc]
            assert total_b == pytest.approx(sum(r["revenue"] for r in truth if r["company"] == "PHARMA_B"))  # type: ignore[misc]
        else:
            assert _as_bag(rows) <= _as_bag(truth)


def test_pivot_on_a_dataset_is_refused_not_silently_dropped(guard: DataGuard) -> None:
    msg = _rejected(guard, PETR, "SELECT * FROM drug_sales PIVOT (sum(revenue) FOR company IN ('PHARMA_A', 'PHARMA_B'))")
    assert "CTE" in msg


def test_pivot_via_cte_works(guard: DataGuard) -> None:
    rows = _rows(guard.run(PETR, "WITH d AS (SELECT company, revenue FROM drug_sales) SELECT * FROM d PIVOT (sum(revenue) FOR company IN ('PHARMA_A', 'PHARMA_B'))"))
    assert "PHARMA_A" in rows[0]


def test_column_alias_list_is_preserved(guard: DataGuard) -> None:
    rows = _rows(guard.run(PETR, "SELECT a FROM drug_sales AS d(a) LIMIT 1"))
    assert list(rows[0]) == ["a"]


def test_tablesample_never_widens(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    for sql in ["SELECT * FROM drug_sales TABLESAMPLE 10%", "SELECT * FROM drug_sales USING SAMPLE 10"]:
        result = guard.run(PETR, sql)
        if result.error is None:
            assert _as_bag(_rows(result)) <= _as_bag(_truth(snapshot, guard, backend, PETR, "drug_sales"))


# ---- statements other than SELECT ---------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO drug_sales SELECT * FROM drug_sales",
        "UPDATE drug_sales SET units = 1",
        "DELETE FROM drug_sales",
        "CREATE TABLE x AS SELECT * FROM drug_sales",
        "CREATE VIEW v AS SELECT * FROM drug_sales",
        "CREATE MACRO m(x) AS x",
        "CREATE TEMP TABLE t AS SELECT * FROM companies",
        "DROP TABLE drug_sales",
        "ALTER TABLE drug_sales ADD COLUMN x INT",
        "ATTACH 'x.db' AS x",
        "DETACH x",
        "COPY drug_sales TO 'x.csv'",
        "COPY (SELECT * FROM drug_sales) TO 'x.csv'",
        "PRAGMA table_info(drug_sales)",
        "SET threads = 1",
        "RESET threads",
        "INSTALL httpfs",
        "LOAD httpfs",
        "USE memory",
        "SUMMARIZE drug_sales",
        "DESCRIBE drug_sales",
        "SHOW TABLES",
        "SHOW drug_sales",
        "PIVOT drug_sales ON company USING sum(revenue)",
        "EXPLAIN SELECT * FROM drug_sales",
        "CALL pragma_version()",
        "TABLE drug_sales",
        "VACUUM",
        "CHECKPOINT",
        "EXPORT DATABASE 'x'",
    ],
)
def test_non_select_statements_rejected(guard: DataGuard, sql: str) -> None:
    _rejected(guard, PETR, sql)


def test_select_into_rejected(guard: DataGuard) -> None:
    assert "Into" in _rejected(guard, PETR, "SELECT * INTO x FROM drug_sales")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM drug_sales; DROP TABLE drug_sales",
        "SELECT * FROM drug_sales; SELECT * FROM companies",
        "SELECT 1 FROM drug_sales /* ; */ ; SELECT * FROM companies",
        "SELECT 1 FROM drug_sales;\n-- comment\nSELECT * FROM companies",
        "",
        ";",
    ],
)
def test_multiple_or_zero_statements_rejected(guard: DataGuard, sql: str) -> None:
    _rejected(guard, PETR, sql)


def test_comment_hiding_a_second_statement_is_just_a_comment(snapshot: AclSnapshot, guard: DataGuard, backend: DuckDBBackend) -> None:
    n = len(_truth(snapshot, guard, backend, PETR, "drug_sales"))
    rows = _rows(guard.run(PETR, "SELECT 1 AS one FROM drug_sales -- ; SELECT * FROM companies"))
    assert len(rows) == n and all(r == {"one": 1} for r in rows)
    rows = _rows(guard.run(PETR, "SELECT 1 AS one FROM drug_sales /* ; SELECT * FROM companies */"))
    assert len(rows) == n
    assert len(_rows(guard.run(PETR, "SELECT 1 AS one FROM drug_sales;"))) == n


# ---- functions -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('x.csv')",
        "SELECT * FROM read_csv_auto('x.csv'), drug_sales",
        "SELECT * FROM read_parquet('x.parquet'), drug_sales",
        "SELECT * FROM parquet_scan('x.parquet'), drug_sales",
        "SELECT * FROM read_json('x.json'), drug_sales",
        "SELECT * FROM glob('*'), drug_sales",
        "SELECT * FROM range(10), drug_sales",
        "SELECT * FROM generate_series(1, 2), drug_sales",
        "SELECT * FROM duckdb_tables(), drug_sales",
        "SELECT * FROM duckdb_columns(), drug_sales",
        "SELECT * FROM duckdb_settings(), drug_sales",
        "SELECT * FROM duckdb_secrets(), drug_sales",
        "SELECT * FROM pragma_table_info('companies'), drug_sales",
        "SELECT * FROM query_table('companies'), drug_sales",
        "SELECT * FROM query('SELECT * FROM companies'), drug_sales",
        "SELECT * FROM sniff_csv('x.csv'), drug_sales",
        "SELECT * FROM json_execute_serialized_sql(json_serialize_sql('SELECT * FROM companies')), drug_sales",
        "SELECT * FROM 'x.csv', drug_sales",
        "SELECT * FROM \"data/warehouse.duckdb\", drug_sales",
    ],
)
def test_table_functions_and_file_scans_rejected(guard: DataGuard, sql: str) -> None:
    _rejected(guard, PETR, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT getenv('HOME') FROM drug_sales",
        "SELECT GETENV('HOME') FROM drug_sales",
        "SELECT main.getenv('HOME') FROM drug_sales",
        "SELECT current_setting('threads') FROM drug_sales",
        "SELECT read_text('x') FROM drug_sales",
        "SELECT read_blob('x') FROM drug_sales",
        "SELECT query('SELECT 1') FROM drug_sales",
        "SELECT sale_id FROM drug_sales WHERE getenv('HOME') IS NOT NULL",
        "WITH x AS (SELECT getenv('HOME') AS h) SELECT * FROM x, drug_sales",
    ],
)
def test_denied_scalar_functions_rejected(guard: DataGuard, sql: str) -> None:
    assert "not allowed" in _rejected(guard, PETR, sql)


def test_quoted_denied_function_does_not_execute(guard: DataGuard) -> None:
    result = guard.run(PETR, "SELECT \"getenv\"('HOME') AS h FROM drug_sales LIMIT 1")
    assert result.result is None or all(r[0] is None for r in result.result.rows)


def test_current_query_does_not_reveal_salt(guard: DataGuard) -> None:
    result = guard.run(LUCIE, "SELECT current_query() AS q FROM drug_sales LIMIT 1")
    if result.error is None:
        assert result.result is not None
        assert TEST_SECRETS["MASK_SALT"] not in str(result.result.rows)


# ---- catalogs, qualified names, placeholders ----------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM information_schema.columns, drug_sales",
        "SELECT * FROM pg_catalog.pg_class, drug_sales",
        "SELECT * FROM sqlite_master, drug_sales",
        "SELECT * FROM pg_tables, drug_sales",
        "SELECT * FROM duckdb_tables, drug_sales",
        "SELECT * FROM main.drug_sales",
        "SELECT * FROM memory.main.drug_sales",
        "SELECT * FROM warehouse.main.drug_sales",
        'SELECT * FROM "main"."drug_sales"',
        "SELECT * FROM main.companies, drug_sales",
        "SELECT * FROM system.main.drug_sales",
    ],
)
def test_catalogs_and_qualified_names_rejected(guard: DataGuard, sql: str) -> None:
    _rejected(guard, PETR, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM drug_sales WHERE sale_id = ?",
        "SELECT * FROM drug_sales WHERE sale_id = $1",
        "SELECT * FROM drug_sales WHERE company = :company",
        "SELECT * FROM drug_sales WHERE company = $company",
    ],
)
def test_placeholders_rejected(guard: DataGuard, sql: str) -> None:
    _rejected(guard, PETR, sql)


# ---- error channel -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sale_id FROM drug_sales WHERE CASE WHEN company = 'PHARMA_B' AND visibility = 'private' "
        "THEN error('LEAK ' || company || ' ' || CAST(revenue AS VARCHAR)) ELSE FALSE END",
        "SELECT sale_id FROM drug_sales WHERE CASE WHEN is_test THEN error('LEAK test row ' || CAST(sale_id AS VARCHAR)) ELSE FALSE END",
        "SELECT sale_id FROM drug_sales WHERE company = 'PHARMA_B' AND visibility = 'private' AND CAST('LEAK' || CAST(revenue AS VARCHAR) AS INT) = 1",
        "SELECT sale_id FROM drug_sales WHERE is_test AND CAST('LEAK' || CAST(sale_id AS VARCHAR) AS INT) = 1",
    ],
)
def test_errors_do_not_leak_hidden_rows(guard: DataGuard, sql: str) -> None:
    result = guard.run(PETR, sql)
    assert result.error is None or "LEAK" not in result.error
    assert result.result is None or result.result.rows == []


# ---- the rewritten SQL itself ---------------------------------------------------------------


def test_rewrite_reports_datasets_and_hash(guard: DataGuard) -> None:
    sql = "SELECT d.sale_id, s.name FROM drug_sales d JOIN stores s USING (store_id)"
    rw = rewrite(sql, guard.secured(PETR), max_chars=MAX_QUERY_CHARS, dialect="duckdb")
    assert rw.datasets == ("drug_sales", "stores")
    assert len(rw.query_hash) == 16
    assert rw.query_hash == rewrite(sql, guard.secured(LUCIE), max_chars=MAX_QUERY_CHARS, dialect="duckdb").query_hash
    assert "'PHARMA_A'" in rw.sql  # the principal's rule is inlined


def test_rewrite_does_not_mutate_compiled_relations(guard: DataGuard) -> None:
    secured = guard.secured(PETR)
    before = secured["drug_sales"].relation.sql(dialect="duckdb")
    rewrite("SELECT * FROM drug_sales a, drug_sales b", secured, max_chars=MAX_QUERY_CHARS, dialect="duckdb")
    assert secured["drug_sales"].relation.sql(dialect="duckdb") == before


@pytest.mark.parametrize("dialect", ["snowflake", "bigquery"])
def test_rewrite_for_other_dialects_contains_secured_relation(policy: PolicySet, snapshot: AclSnapshot, dialect: str) -> None:
    import sqlglot
    from sqlglot import exp

    view = snapshot.security_for(PETR)
    secured = {
        name: c
        for name, ds in policy.datasets.items()
        if (
            c := compile_dataset(
                ds, PETR, view.audience, view.datasets.get(name, EMPTY_SECURITY),
                salt=TEST_SECRETS["MASK_SALT"], dialect=dialect,
            )
        ) is not None
    }
    rw = rewrite("SELECT company, count(*) FROM drug_sales GROUP BY company", secured, max_chars=MAX_QUERY_CHARS, dialect=dialect)
    parsed = sqlglot.parse_one(rw.sql, read=dialect)
    tables = list(parsed.find_all(exp.Table))
    assert [t.name for t in tables] == ["drug_sales"]
    # The only physical reference sits under the secured WHERE.
    assert tables[0].find_ancestor(exp.Select).args.get("where") is not None  # type: ignore[union-attr]


def test_concurrent_connections_share_the_locked_configuration(tmp_path: Path) -> None:
    """Two requests at once share one DuckDB instance: the second connection must work and stay locked."""
    import duckdb

    from kbc_access.backends.duckdb_backend import DuckDBBackend

    path = tmp_path / "w.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE t AS SELECT 1 AS x")
    backend = DuckDBBackend(path, 10)
    with backend._connect() as first, backend._connect() as second:
        assert second.execute("SELECT x FROM t").fetchall() == [(1,)]
        for conn in (first, second):
            with pytest.raises(duckdb.Error):
                conn.execute("SET enable_external_access = true")


def test_many_threads_connecting_at_once_all_get_a_locked_connection(tmp_path: Path) -> None:
    """The admin's user page fires one test per dataset in parallel: no connection may fail on the lock."""
    from concurrent.futures import ThreadPoolExecutor

    import duckdb

    from kbc_access.backends.duckdb_backend import DuckDBBackend

    path = tmp_path / "w.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE t AS SELECT 1 AS x")
    backend = DuckDBBackend(path, 10)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: backend.execute("SELECT x FROM t").rows, range(200)))
    assert all(r == [(1,)] for r in results)
