"""Layer 3: DataGuard — run() never raises on user errors, and the audit trail carries no data.

Personas follow the example security of config/policies/pharma.yaml, which the seeded store
carries as row_access_rules / column_masks. Expected `grants` are built from the stored line ids
(`show:<rule_id>`, `hide:<rule_id>`, `mask:<column>:<mask_id>`), looked up by position.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from kbc_access.gate import Gate
from kbc_access.guard import SALT_REDACTION, DataGuard
from kbc_access.identity import Principal
from kbc_access.rewriter import query_hash
from kbc_access.security_model import (
    ALL_ROWS,
    CLEAR,
    HIDDEN,
    KIND_HIDE,
    KIND_SHOW,
    MASK_HASH,
    SUBJECT_EVERYONE,
    SUBJECT_GROUP,
    SUBJECT_LOCAL_GROUP,
    SUBJECT_USER,
)
from kbc_access.store import (
    ColumnMask,
    ColumnMaskInput,
    DatasetSecurity,
    PrincipalSecurity,
    RowAccessRule,
    RowAccessRuleInput,
)
from kbc_access.web import Runtime, build_guard

FINANCE = "3f2b8c1e-7d4a-4e5b-9a61-2c8d0f4e1b7a"
SUPPLY = "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9"
PARTNER = "00g7pharmab0partner1"
SALES_MGR = "8a1d4e2f-5b6c-4d7e-9f80-1a2b3c4d5e6f"

PETR = Principal("petr.sales@pharma-a.example", frozenset({SALES_MGR}), {"company": "PHARMA_A"})
LUCIE = Principal("lucie.finance@pharma-a.example", frozenset({FINANCE}), {"company": "PHARMA_A"})
JAN = Principal("jan.partner@pharma-b.example", frozenset({PARTNER}), {"company": "PHARMA_B"})
MARTIN = Principal("martin.noattr@pharma-c.example", frozenset({SALES_MGR}), {})
EVA_EMAIL = "eva.supply@pharma-a.example"
# Local group from config/fake_users.yaml; Petr is a member.
EMEA_SALES = "emea-sales"

# Positions of the pharma.yaml example lines, as the seed stored them (1-based within kind / column).
DRUG_SALES_SHOW_PUBLIC = 1
DRUG_SALES_SHOW_OWN_COMPANY = 2
DRUG_SALES_HIDE_TEST_ROWS = 1
SUPPLY_SHOW_PARTNER = 2
MASK_FIRST = 1  # finance (or partner) line
MASK_SECOND = 2  # everyone else (or anna)
CONTRACT_REF_EVERYONE = 3


def _rule_id(runtime: Runtime, dataset: str, kind: str, position: int) -> str:
    [rule] = [r for r in runtime.store.list_row_access_rules(dataset) if r.kind == kind and r.position == position]
    return rule.rule_id


def _mask_id(runtime: Runtime, dataset: str, column: str, position: int) -> str:
    [mask] = [m for m in runtime.store.list_column_masks(dataset) if m.column_name == column and m.position == position]
    return mask.mask_id


def _group_id(runtime: Runtime, external_id: str) -> str:
    [group] = [g for g in runtime.store.list_groups() if g.external_id == external_id]
    return group.group_id


def _fresh(runtime: Runtime) -> Runtime:
    """The runtime with a gate that never caches, so a change in the store applies on the next call."""
    return dataclasses.replace(runtime, gate=Gate(runtime.store, 0))


def _current_inputs(runtime: Runtime, dataset: str) -> tuple[list[RowAccessRuleInput], list[ColumnMaskInput]]:
    """The dataset's stored lines as admin inputs, so a test can add to them."""
    security = runtime.store.dataset_security(dataset)
    rows = [
        RowAccessRuleInput(r.kind, r.subject_type, r.subject_id, r.filter.to_json() if r.filter else {}, r.rule_id)
        for r in security.rows
    ]
    masks = [ColumnMaskInput(m.column_name, m.subject_type, m.subject_id, m.treatment, m.mask_id) for m in security.masks]
    return rows, masks


@pytest.fixture
def guard(runtime: Runtime) -> DataGuard:
    return build_guard(runtime, "sales")


def _audit_lines(runtime: Runtime) -> list[dict[str, object]]:
    path = runtime.settings.path(runtime.settings.audit.path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _query_lines(runtime: Runtime) -> list[dict[str, object]]:
    return [line for line in _audit_lines(runtime) if line["event"] == "query"]


def test_secured_datasets_per_persona(guard: DataGuard) -> None:
    assert set(guard.secured(PETR)) == {"drug_sales", "stores"}
    assert set(guard.secured(LUCIE)) == {"drug_sales", "stores", "supply_chain"}
    assert set(guard.secured(JAN)) == {"drug_sales", "stores", "supply_chain"}
    assert set(guard.secured(MARTIN)) == {"drug_sales", "stores"}
    # The supply-chain line reads the company attribute: without it, it does not apply.
    assert set(guard.secured(Principal("nobody@x.example", frozenset({SUPPLY}), {}))) == {"drug_sales", "stores"}
    assert guard.policy.fingerprint


def test_column_treatments_per_persona(guard: DataGuard) -> None:
    petr = guard.secured(PETR)["drug_sales"]
    assert petr.column_treatments["margin"] == HIDDEN and petr.absent_columns == ("margin",)
    assert petr.masked_columns == ("customer_id",)
    lucie = guard.secured(LUCIE)["drug_sales"]
    assert lucie.column_treatments["margin"] == CLEAR and lucie.absent_columns == ()
    assert lucie.column_treatments["customer_id"] == MASK_HASH
    jan = guard.secured(JAN)["supply_chain"]
    assert jan.absent_columns == ("unit_cost", "contract_ref")
    assert "unit_cost" not in dict(jan.visible_columns)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "not sql at all",
        "SELECT",
        "SELECT * FROM",
        "SELECT * FROM supply_chain",
        "DROP TABLE drug_sales",
        "SELECT * FROM drug_sales; SELECT 1",
        "SELECT no_such_col FROM drug_sales",
        "SELECT margin FROM drug_sales",
        "SELECT SUM(customer_id) FROM drug_sales",
        "SELECT 1/0 FROM drug_sales",
        "SELECT CAST(company AS INT) FROM drug_sales",
        "SELECT '\x00' FROM drug_sales",
        "SELECT " + "(" * 20 + "1" + ")" * 20 + " FROM drug_sales",
        "SELECT * FROM drug_sales WHERE sale_id = ?",
        "SELECT '" + "x" * 10_000 + "' FROM drug_sales LIMIT 1",
        "SELECT 'ž🙂' AS u FROM drug_sales LIMIT 1",
    ],
)
def test_run_never_raises(guard: DataGuard, sql: str) -> None:
    result = guard.run(PETR, sql)
    assert (result.error is None) != (result.result is None)


def test_run_never_raises_on_deep_nesting(guard: DataGuard) -> None:
    result = guard.run(PETR, "SELECT " + "(" * 1000 + "1" + ")" * 1000 + " FROM drug_sales")
    assert result.result is None and result.error is not None


def test_ok_result_and_audit_line(guard: DataGuard, runtime: Runtime) -> None:
    sql = "SELECT company, count(*) AS n FROM drug_sales WHERE visibility = 'private' GROUP BY company"
    result = guard.run(PETR, sql)
    assert result.error is None and result.result is not None
    assert result.result.rows == [("PHARMA_A", result.result.rows[0][1])]
    assert result.datasets == ("drug_sales",)
    assert result.display_sql is not None and "drug_sales" in result.display_sql
    [line] = _query_lines(runtime)
    assert line["allowed"] is True and line["reason"] == "ok"
    assert line["app_id"] == "sales"
    assert line["email"] == PETR.email
    assert line["datasets"] == ["drug_sales"]
    assert line["grants"] == {
        "drug_sales": [
            f"show:{_rule_id(runtime, 'drug_sales', KIND_SHOW, DRUG_SALES_SHOW_PUBLIC)}",
            f"show:{_rule_id(runtime, 'drug_sales', KIND_SHOW, DRUG_SALES_SHOW_OWN_COMPANY)}",
            f"hide:{_rule_id(runtime, 'drug_sales', KIND_HIDE, DRUG_SALES_HIDE_TEST_ROWS)}",
            f"mask:margin:{_mask_id(runtime, 'drug_sales', 'margin', MASK_SECOND)}",
            f"mask:customer_id:{_mask_id(runtime, 'drug_sales', 'customer_id', MASK_SECOND)}",
        ]
    }
    assert line["masked_columns"] == {"drug_sales": ["customer_id"]}
    assert line["absent_columns"] == {"drug_sales": ["margin"]}
    assert line["policy_fingerprint"] == guard.policy.fingerprint
    assert line["query_hash"] == query_hash(sql)
    assert line["row_count"] == 1
    assert "ts" in line


def test_rejected_audit_line(guard: DataGuard, runtime: Runtime) -> None:
    sql = "SELECT * FROM supply_chain"
    result = guard.run(PETR, sql)
    assert result.result is None and result.error and result.display_sql is None and result.datasets == ()
    [line] = _query_lines(runtime)
    assert (line["allowed"], line["reason"]) == (False, "rejected")
    assert line["datasets"] == [] and line["grants"] == {} and line["row_count"] is None
    assert line["query_hash"] == query_hash(sql)
    assert line["policy_fingerprint"] == guard.policy.fingerprint


def test_execution_error_audit_line(guard: DataGuard, runtime: Runtime) -> None:
    sql = "SELECT margin, customer_id FROM drug_sales"
    result = guard.run(MARTIN, sql)
    assert result.result is None
    assert result.error is not None and result.error.startswith("Query failed:")
    assert result.datasets == ("drug_sales",)
    assert result.display_sql is not None
    [line] = _query_lines(runtime)
    assert (line["allowed"], line["reason"]) == (False, "execution_error")
    assert line["datasets"] == ["drug_sales"]
    # Martin has no company attribute: the own-company show line does not apply to him.
    assert line["grants"] == {
        "drug_sales": [
            f"show:{_rule_id(runtime, 'drug_sales', KIND_SHOW, DRUG_SALES_SHOW_PUBLIC)}",
            f"hide:{_rule_id(runtime, 'drug_sales', KIND_HIDE, DRUG_SALES_HIDE_TEST_ROWS)}",
            f"mask:margin:{_mask_id(runtime, 'drug_sales', 'margin', MASK_SECOND)}",
            f"mask:customer_id:{_mask_id(runtime, 'drug_sales', 'customer_id', MASK_SECOND)}",
        ]
    }
    assert line["absent_columns"] == {"drug_sales": ["margin"]}
    assert line["masked_columns"] == {"drug_sales": ["customer_id"]}
    assert line["row_count"] is None


def test_multi_dataset_audit_line(guard: DataGuard, runtime: Runtime) -> None:
    result = guard.run(JAN, "SELECT count(*) FROM supply_chain JOIN stores USING (company)")
    assert result.error is None
    [line] = _query_lines(runtime)
    assert line["datasets"] == ["stores", "supply_chain"]
    assert line["grants"]["supply_chain"] == [  # type: ignore[index]
        f"show:{_rule_id(runtime, 'supply_chain', KIND_SHOW, SUPPLY_SHOW_PARTNER)}",
        f"mask:unit_cost:{_mask_id(runtime, 'supply_chain', 'unit_cost', MASK_FIRST)}",
        f"mask:contract_ref:{_mask_id(runtime, 'supply_chain', 'contract_ref', CONTRACT_REF_EVERYONE)}",
    ]
    assert line["absent_columns"]["supply_chain"] == ["unit_cost", "contract_ref"]  # type: ignore[index]
    assert line["masked_columns"]["supply_chain"] == []  # type: ignore[index]


def test_jan_sees_only_public_pharma_b_shipments(guard: DataGuard) -> None:
    result = guard.run(JAN, "SELECT DISTINCT company, visibility FROM supply_chain")
    assert result.result is not None and result.result.rows == [("PHARMA_B", "public")]
    for column in ("unit_cost", "contract_ref"):
        failed = guard.run(JAN, f"SELECT {column} FROM supply_chain")
        assert failed.result is None and failed.error is not None and failed.error.startswith("Query failed:")


def test_result_truncated_at_max_rows(guard: DataGuard, runtime: Runtime) -> None:
    result = guard.run(LUCIE, "SELECT * FROM drug_sales")
    assert result.result is not None
    assert result.result.truncated
    assert len(result.result.rows) == runtime.settings.data.max_result_rows
    small = guard.run(LUCIE, "SELECT * FROM drug_sales LIMIT 3")
    assert small.result is not None and not small.result.truncated and len(small.result.rows) == 3


def test_salt_redacted_in_display_sql_and_errors(guard: DataGuard, runtime: Runtime) -> None:
    salt = runtime.settings.secrets.mask_salt
    ok = guard.run(LUCIE, "SELECT customer_id FROM drug_sales LIMIT 1")  # mask_hash for finance
    assert ok.display_sql is not None and salt not in ok.display_sql and SALT_REDACTION in ok.display_sql
    assert ok.result is not None and all(salt not in str(v) for row in ok.result.rows for v in row)
    bad = guard.run(LUCIE, "SELECT customer_id + 1 FROM drug_sales")
    assert bad.error is not None and salt not in bad.error
    assert bad.display_sql is not None and salt not in bad.display_sql


def test_attribute_value_cannot_inject_sql(guard: DataGuard) -> None:
    """A quote in an attribute value stays inside the literal: no private row of another company leaks."""
    hostile = Principal(PETR.email, PETR.groups, {"company": "PHARMA_A' OR '1'='1"})
    result = guard.run(hostile, "SELECT DISTINCT visibility FROM drug_sales")
    assert result.error is None and result.result is not None
    assert result.result.rows == [("public",)]
    compiled = guard.secured(hostile)["drug_sales"]
    assert "'PHARMA_A'' OR ''1''=''1'" in compiled.row_filter


def _all_warehouse_values(runtime: Runtime) -> set[str]:
    """Every distinct text value of the columns an attacker cares about."""
    import duckdb

    path = runtime.settings.path(runtime.settings.data.duckdb_path)
    con = duckdb.connect(str(path), read_only=True, config={"enable_external_access": False, "autoload_known_extensions": False})
    try:
        values: set[str] = set()
        for sql in [
            "SELECT customer_id FROM drug_sales",
            "SELECT CAST(revenue AS VARCHAR) FROM drug_sales",
            "SELECT CAST(margin AS VARCHAR) FROM drug_sales",
            "SELECT name FROM stores",
            "SELECT manager_email FROM stores",
            "SELECT contract_ref FROM supply_chain",
            "SELECT name FROM companies",
        ]:
            values |= {str(r[0]) for r in con.execute(sql).fetchall()}
    finally:
        con.close()
    return values


def test_audit_contains_no_row_or_attribute_values(guard: DataGuard, runtime: Runtime) -> None:
    salt = runtime.settings.secrets.mask_salt
    queries = [
        (PETR, "SELECT * FROM drug_sales WHERE company = 'PHARMA_B'"),
        (PETR, "SELECT customer_id, margin FROM drug_sales LIMIT 5"),
        (LUCIE, "SELECT * FROM drug_sales WHERE customer_id LIKE 'CUST-%'"),
        (LUCIE, "SELECT * FROM supply_chain"),
        (JAN, "SELECT * FROM supply_chain WHERE contract_ref = 'CTR-B-1234'"),
        (JAN, "SELECT unit_cost FROM supply_chain"),
        (MARTIN, "SELECT margin FROM drug_sales WHERE company = 'PHARMA_C'"),
        (PETR, "SELECT * FROM supply_chain WHERE company = 'PHARMA_A'"),
        (PETR, "SELECT CAST(name AS INT) FROM stores"),
        (PETR, "SELECT name FROM stores WHERE name = 'Lékárna Praha 1'"),
    ]
    for who, sql in queries:
        guard.run(who, sql)
    text = runtime.settings.path(runtime.settings.audit.path).read_text(encoding="utf-8")
    assert len(text.splitlines()) == len(queries)
    # Attribute values (the principals' company) and literals from the SQL text.
    for forbidden in ["PHARMA_A", "PHARMA_B", "PHARMA_C", "CUST-", "CTR-", "Lékárna", "SELECT", "visibility", salt]:
        assert forbidden not in text, forbidden
    # Every row value of the sensitive columns.
    lines = _audit_lines(runtime)
    leaves: set[str] = set()

    def collect(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                leaves.add(str(k))
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)
        else:
            leaves.add(str(node))

    for line in lines:
        collect(line)
    leaked = leaves & _all_warehouse_values(runtime)
    assert not leaked, sorted(leaked)[:5]


def test_audit_contract_fields(guard: DataGuard, runtime: Runtime) -> None:
    guard.run(PETR, "SELECT count(*) FROM drug_sales")
    [line] = _query_lines(runtime)
    assert set(line) == {
        "ts", "event", "app_id", "email", "allowed", "reason", "datasets", "grants", "masked_columns",
        "absent_columns", "policy_fingerprint", "query_hash", "row_count",
    }


def test_user_error_message_is_returned_not_raised(guard: DataGuard) -> None:
    result = guard.run(PETR, "SELECT * FROM drug_sales WHERE")
    assert result.error is not None and result.error.startswith("Query does not parse")


def test_guard_for_other_app_id(runtime: Runtime) -> None:
    supply = build_guard(runtime, "supply")
    supply.run(LUCIE, "SELECT count(*) FROM supply_chain")
    [line] = _query_lines(runtime)
    assert line["app_id"] == "supply"


def test_guard_fails_fast_without_warehouse(runtime: Runtime, tmp_path: Path) -> None:
    from kbc_access.backends.duckdb_backend import DuckDBBackend

    with pytest.raises(FileNotFoundError):
        DuckDBBackend(tmp_path / "missing.duckdb", 10)


def test_backend_is_read_only_and_locked(runtime: Runtime) -> None:
    from kbc_access.backends.duckdb_backend import DuckDBBackend

    backend = DuckDBBackend(runtime.settings.path(runtime.settings.data.duckdb_path), 10)
    import duckdb

    with pytest.raises(duckdb.Error):
        backend.execute("CREATE TABLE x AS SELECT 1")
    with pytest.raises(duckdb.Error):
        backend.execute("SET enable_external_access = true")
    with pytest.raises(duckdb.Error):
        backend.execute("SELECT * FROM read_text('/etc/hosts')")


# --- the store is the source of truth ----------------------------------------------------------


def test_store_change_applies_to_guard(runtime: Runtime) -> None:
    """A masking line saved in the store reaches a guard built afterwards (and one built before, with no cache)."""
    fresh = _fresh(runtime)
    before = build_guard(fresh, "sales")
    assert before.secured(PETR)["drug_sales"].column_treatments["margin"] == HIDDEN
    assert before.run(PETR, "SELECT margin FROM drug_sales LIMIT 1").error is not None

    rows, masks = _current_inputs(runtime, "drug_sales")
    # First line of the margin column wins: clear for Petr ahead of the "everyone: hidden" line.
    runtime.store.set_dataset_security(
        "drug_sales", rows, [ColumnMaskInput("margin", SUBJECT_USER, PETR.email, CLEAR), *masks]
    )

    after = build_guard(fresh, "sales")
    for guard in (after, before):
        compiled = guard.secured(PETR)["drug_sales"]
        assert compiled.column_treatments["margin"] == CLEAR and "margin" not in compiled.absent_columns
        result = guard.run(PETR, "SELECT SUM(margin) AS m FROM drug_sales")
        assert result.error is None and result.result is not None and result.result.rows[0][0] is not None


def test_removing_every_show_line_hides_the_dataset(runtime: Runtime) -> None:
    fresh = _fresh(runtime)
    guard = build_guard(fresh, "sales")
    assert "stores" in guard.secured(PETR)
    runtime.store.set_dataset_security("stores", [], [])
    assert "stores" not in guard.secured(PETR) and "stores" not in guard.secured(LUCIE)
    rejected = guard.run(LUCIE, "SELECT * FROM stores")
    assert rejected.result is None and rejected.error is not None and rejected.datasets == ()


def _subject(runtime: Runtime, kind: str) -> tuple[str, str]:
    """(subject_type, subject_id) for a subject that includes Petr."""
    return {
        "user": (SUBJECT_USER, PETR.email),
        "local_group": (SUBJECT_LOCAL_GROUP, EMEA_SALES),
        "idp_group": (SUBJECT_GROUP, _group_id(runtime, SALES_MGR)),
    }[kind]


def _other_subject(runtime: Runtime, kind: str) -> tuple[str, str]:
    """(subject_type, subject_id) for an existing subject that does not include Petr."""
    if kind == "user":
        return SUBJECT_USER, EVA_EMAIL
    if kind == "local_group":
        runtime.store.create_local_group("apac-sales", "APAC sales")
        runtime.store.add_local_group_member("apac-sales", EVA_EMAIL)
        return SUBJECT_LOCAL_GROUP, "apac-sales"
    return SUBJECT_GROUP, _group_id(runtime, FINANCE)


def _assign_supply_lines(runtime: Runtime, subject_type: str, subject_id: str) -> None:
    """Add a show-all-rows line and a hash mask on unit_cost for the subject to supply_chain."""
    rows, masks = _current_inputs(runtime, "supply_chain")
    runtime.store.set_dataset_security(
        "supply_chain",
        [*rows, RowAccessRuleInput(KIND_SHOW, subject_type, subject_id, {"all": True})],
        [ColumnMaskInput("unit_cost", subject_type, subject_id, MASK_HASH), *masks],
    )


@pytest.mark.parametrize("kind", ["user", "local_group", "idp_group"])
def test_lines_assigned_to_a_subject_apply_to_its_members(runtime: Runtime, kind: str) -> None:
    guard = build_guard(_fresh(runtime), "sales")
    assert "supply_chain" not in guard.secured(PETR)
    subject_type, subject_id = _subject(runtime, kind)
    _assign_supply_lines(runtime, subject_type, subject_id)

    compiled = guard.secured(PETR).get("supply_chain")
    assert compiled is not None
    assert compiled.column_treatments["unit_cost"] == MASK_HASH
    assert compiled.column_treatments["contract_ref"] == HIDDEN  # the "everyone" line still applies
    [show_id] = [r.rule_id for r in runtime.store.list_row_access_rules("supply_chain")
                 if r.subject_type == subject_type and r.subject_id == subject_id]
    assert f"show:{show_id}" in compiled.grants_fired
    result = guard.run(PETR, "SELECT COUNT(DISTINCT company) AS n FROM supply_chain")
    assert result.error is None and result.result is not None and result.result.rows[0][0] > 1


@pytest.mark.parametrize("kind", ["user", "local_group", "idp_group"])
def test_lines_assigned_to_another_subject_do_not_apply(runtime: Runtime, kind: str) -> None:
    guard = build_guard(_fresh(runtime), "sales")
    subject_type, subject_id = _other_subject(runtime, kind)
    _assign_supply_lines(runtime, subject_type, subject_id)
    assert "supply_chain" not in guard.secured(PETR)
    # The masking line for the other subject does not touch a column Petr can see elsewhere either.
    assert guard.secured(PETR)["drug_sales"].column_treatments["revenue"] == CLEAR


def test_guard_with_explicit_security_callable(runtime: Runtime, tmp_path: Path) -> None:
    """DataGuard built by hand: a catalogue-only policy file and a security callable naming a local group."""
    from kbc_access.backends.duckdb_backend import DuckDBBackend
    from kbc_access.policy import load_policy

    policy_file = tmp_path / "local.yaml"
    policy_file.write_text(
        "version: 2\n"
        "datasets:\n"
        "  stores:\n"
        "    source: stores\n"
        "    description: d\n"
        "    columns: [store_id, company, name, city, region, manager_email, visibility]\n",
        encoding="utf-8",
    )
    settings = runtime.settings
    backend = DuckDBBackend(settings.path(settings.data.duckdb_path), settings.data.max_result_rows)
    policy = load_policy([policy_file], backend.column_types())
    lines = DatasetSecurity(
        rows=(RowAccessRule("0123456789ab", "stores", 1, KIND_SHOW, SUBJECT_LOCAL_GROUP, "team", ALL_ROWS),),
        masks=(ColumnMask("ba9876543210", "stores", "manager_email", 1, SUBJECT_EVERYONE, "", HIDDEN),),
    )
    snapshot = runtime.gate.snapshot()

    def security(principal: Principal) -> PrincipalSecurity:
        return PrincipalSecurity(audience=snapshot.audience_of(principal), datasets={"stores": lines})

    guard = DataGuard(
        policy, backend, runtime.audit, salt=settings.secrets.mask_salt, app_id="sales",
        max_query_chars=settings.data.max_query_chars, security=security,
    )
    member = Principal(PETR.email, PETR.groups, dict(PETR.attributes), frozenset({"team"}))
    assert set(guard.secured(member)) == {"stores"}
    assert guard.secured(member)["stores"].absent_columns == ("manager_email",)
    outcome = guard.run(member, "SELECT count(*) AS n FROM stores")
    assert outcome.error is None and outcome.result is not None and outcome.result.rows[0][0] > 0
    assert not guard.secured(PETR)


def test_guard_refuses_unsafe_salt(runtime: Runtime) -> None:
    from kbc_access.backends.duckdb_backend import DuckDBBackend
    from kbc_access.policy import load_policy

    settings = runtime.settings
    backend = DuckDBBackend(settings.path(settings.data.duckdb_path), settings.data.max_result_rows)
    policy = load_policy([settings.path(f) for f in settings.require_data()[1].files], backend.column_types())
    with pytest.raises(ValueError, match="salt"):
        DataGuard(
            policy, backend, runtime.audit, salt="it's'quoted-salt-value", app_id="sales",
            max_query_chars=settings.data.max_query_chars, security=runtime.gate.snapshot().security_for,
        )
