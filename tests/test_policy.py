"""Layer 3a: the policy file (version 2 catalogue + examples) and the row filter vocabulary.

Covers `kbc_access.policy` (loading the catalogue with and without the warehouse,
checking declared columns and types, validating filters, masks and literals,
resolving examples into store input) and `kbc_access.security_model`
(`parse_filter` and `Audience`).
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping
from pathlib import Path

import pytest

from kbc_access.backends.duckdb_backend import DuckDBBackend
from kbc_access.config import Settings
from kbc_access.policy import (
    POLICY_FILE_VERSION,
    STORAGE_SYSTEM_COLUMNS,
    Dataset,
    PolicyError,
    column_kind,
    literal_problem,
    load_catalog,
    load_policy,
    resolve_examples,
    validate_filter,
    validate_mask,
)
from kbc_access.security_model import (
    ALL_ROWS,
    CLEAR,
    HIDDEN,
    KIND_BOOLEAN,
    KIND_DATE,
    KIND_HIDE,
    KIND_NUMERIC,
    KIND_SHOW,
    KIND_TEXT,
    MASK_HASH,
    MASK_PARTIAL,
    OP_GT,
    OP_IS,
    OP_IS_EMPTY,
    OP_ONE_OF,
    SUBJECT_EVERYONE,
    SUBJECT_GROUP,
    SUBJECT_LOCAL_GROUP,
    SUBJECT_USER,
    Audience,
    Condition,
    FilterError,
    RowFilter,
    parse_filter,
)
from kbc_access.store import ColumnMaskInput, RowAccessRuleInput

FINGERPRINT_LENGTH = 16
FINANCE_EXTERNAL_ID = "3f2b8c1e-7d4a-4e5b-9a61-2c8d0f4e1b7a"
SUPPLY_EXTERNAL_ID = "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9"
PARTNER_EXTERNAL_ID = "00g7pharmab0partner1"
ANNA = "anna.admin@pharma-a.example"
PHARMA_DATASETS = {"drug_sales", "stores", "supply_chain"}
MAX_ATTRIBUTE_KEY_LENGTH = 64

# A small warehouse as DuckDB reports it: relation -> {column: type}.
SALES_TYPES: dict[str, str] = {
    "sale_id": "INTEGER",
    "sale_date": "DATE",
    "company": "VARCHAR",
    "units": "BIGINT",
    "revenue": "DECIMAL(10,2)",
    "is_test": "BOOLEAN",
    "visibility": "VARCHAR",
}
SCHEMAS: dict[str, dict[str, str]] = {
    "drug_sales": SALES_TYPES,
    "stores": {"store_id": "VARCHAR", "company": "VARCHAR"},
}
SALES_COLUMNS = "[sale_id, sale_date, company, units, revenue, is_test, visibility]"
SALES_TYPES_YAML = "{sale_id: numeric, sale_date: date, units: numeric, revenue: numeric, is_test: boolean}"


def _write(tmp_path: Path, body: str, name: str = "p.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _doc(
    *,
    columns: str = SALES_COLUMNS,
    types: str = SALES_TYPES_YAML,
    examples: str = "{}",
    version: int = POLICY_FILE_VERSION,
    dataset: str = "drug_sales",
    source: str = "drug_sales",
) -> str:
    return f"""
    version: {version}
    datasets:
      {dataset}:
        source: {source}
        description: d
        columns: {columns}
        types: {types}
        examples: {examples}
    """


def _rows_example(*rows: str) -> str:
    return "{rows: [" + ", ".join(rows) + "]}"


def _sales_dataset(tmp_path: Path, examples: str = "{}") -> Dataset:
    return load_policy([_write(tmp_path, _doc(examples=examples))], SCHEMAS).datasets["drug_sales"]


def _pharma_files(settings: Settings) -> list[Path]:
    return [settings.path(f) for f in settings.policy.files]


# ---- loading the real catalogue -------------------------------------------------------------


def test_loads_pharma_against_seeded_warehouse(settings: Settings) -> None:
    backend = DuckDBBackend(settings.path(settings.data.duckdb_path), 10)
    policy = load_policy(_pharma_files(settings), backend.column_types())
    assert set(policy.datasets) == PHARMA_DATASETS
    # `companies` exists in the warehouse but is not catalogued, so it is not a dataset.
    assert "companies" not in policy.datasets
    sales = policy.datasets["drug_sales"]
    assert sales.source == "drug_sales"
    assert sales.columns[:3] == ("sale_id", "sale_date", "company")
    assert sales.column_kinds["sale_id"] == KIND_NUMERIC
    assert sales.column_kinds["sale_date"] == KIND_DATE
    assert sales.column_kinds["revenue"] == KIND_NUMERIC
    assert sales.column_kinds["is_test"] == KIND_BOOLEAN
    assert sales.column_kinds["company"] == KIND_TEXT
    assert set(sales.column_kinds) == set(sales.columns)
    assert sales.has_examples
    assert [r.kind for r in sales.examples.rows] == [KIND_SHOW, KIND_SHOW, KIND_SHOW, KIND_HIDE]
    assert list(sales.examples.masks) == ["margin", "customer_id"]
    assert all(kind == KIND_TEXT for kind in policy.datasets["stores"].column_kinds.values())
    assert len(policy.fingerprint) == FINGERPRINT_LENGTH


def test_load_catalog_needs_no_warehouse(settings: Settings) -> None:
    catalog = load_catalog(_pharma_files(settings))
    assert set(catalog) == PHARMA_DATASETS
    supply = catalog["supply_chain"]
    assert supply.column_kinds["ship_date"] == KIND_DATE
    assert supply.column_kinds["unit_cost"] == KIND_NUMERIC
    assert supply.column_kinds["contract_ref"] == KIND_TEXT
    assert [m.treatment for m in supply.examples.masks["contract_ref"]] == [CLEAR, MASK_HASH, HIDDEN]


def test_catalog_and_policy_agree(settings: Settings) -> None:
    backend = DuckDBBackend(settings.path(settings.data.duckdb_path), 10)
    policy = load_policy(_pharma_files(settings), backend.column_types())
    assert load_catalog(_pharma_files(settings)) == policy.datasets


# ---- document shape ---------------------------------------------------------------------------


def test_minimal_document(tmp_path: Path) -> None:
    sales = _sales_dataset(tmp_path)
    assert sales.columns == ("sale_id", "sale_date", "company", "units", "revenue", "is_test", "visibility")
    assert not sales.has_examples


@pytest.mark.parametrize("version", [1, 3])
def test_other_versions_refused(tmp_path: Path, version: int) -> None:
    with pytest.raises(PolicyError, match="version"):
        load_policy([_write(tmp_path, _doc(version=version))], SCHEMAS)


def test_missing_version_refused(tmp_path: Path) -> None:
    body = _doc().replace(f"version: {POLICY_FILE_VERSION}", "")
    with pytest.raises(PolicyError, match="version"):
        load_catalog([_write(tmp_path, body)])


def test_empty_file_refused(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_catalog([_write(tmp_path, "")])


def test_extra_keys_refused(tmp_path: Path) -> None:
    body = _doc().replace("description: d", "description: d\n        grants: []")
    with pytest.raises(PolicyError, match="grants"):
        load_catalog([_write(tmp_path, body)])


def test_duplicate_yaml_key_refused(tmp_path: Path) -> None:
    body = f"""
    version: {POLICY_FILE_VERSION}
    datasets:
      drug_sales:
        source: drug_sales
        description: strict
        columns: {SALES_COLUMNS}
      drug_sales:
        source: drug_sales
        description: wide open
        columns: {SALES_COLUMNS}
    """
    with pytest.raises(PolicyError, match="duplicate key 'drug_sales'"):
        load_catalog([_write(tmp_path, body)])


def test_duplicate_nested_yaml_key_refused(tmp_path: Path) -> None:
    body = _doc().replace("description: d", "description: d\n        description: e")
    with pytest.raises(PolicyError, match="duplicate key 'description'"):
        load_catalog([_write(tmp_path, body)])


def test_duplicate_dataset_across_files_refused(tmp_path: Path) -> None:
    a = _write(tmp_path, _doc(), "a.yaml")
    b = _write(tmp_path, _doc(dataset="DRUG_SALES"), "b.yaml")
    with pytest.raises(PolicyError, match="defined twice"):
        load_catalog([a, b])
    with pytest.raises(PolicyError, match="defined twice"):
        load_policy([a, b], SCHEMAS)


def test_dataset_names_are_lowercased(tmp_path: Path) -> None:
    assert set(load_catalog([_write(tmp_path, _doc(dataset="Drug_Sales"))])) == {"drug_sales"}


# ---- columns ----------------------------------------------------------------------------------


def test_columns_required(tmp_path: Path) -> None:
    body = _doc().replace(f"columns: {SALES_COLUMNS}", "").replace(f"types: {SALES_TYPES_YAML}", "")
    with pytest.raises(PolicyError, match="columns"):
        load_catalog([_write(tmp_path, body)])


@pytest.mark.parametrize("columns", ["[]", "[sale_id, '']", "[sale_id, '  ']"])
def test_columns_must_be_non_empty(tmp_path: Path, columns: str) -> None:
    with pytest.raises(PolicyError, match="non-empty list of column names"):
        load_catalog([_write(tmp_path, _doc(columns=columns, types="{}"))])


def test_columns_must_be_unique_ignoring_case(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="lists a column twice"):
        load_catalog([_write(tmp_path, _doc(columns="[company, Company]", types="{}"))])


def test_declared_column_names_are_lowercased(tmp_path: Path) -> None:
    columns = "[Sale_ID, sale_date, COMPANY, units, revenue, is_test, visibility]"
    sales = load_policy([_write(tmp_path, _doc(columns=columns))], SCHEMAS).datasets["drug_sales"]
    assert sales.columns[:3] == ("sale_id", "sale_date", "company")


def test_warehouse_column_names_are_lowercased(tmp_path: Path) -> None:
    upper = {"drug_sales": {c.upper(): t for c, t in SALES_TYPES.items()}}
    sales = load_policy([_write(tmp_path, _doc())], upper).datasets["drug_sales"]
    assert sales.column_kinds["revenue"] == KIND_NUMERIC


def test_unknown_source_refused(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="source 'nope' not found"):
        load_policy([_write(tmp_path, _doc(source="nope"))], SCHEMAS)


def test_column_missing_in_policy_refused(tmp_path: Path) -> None:
    schemas = {"drug_sales": {**SALES_TYPES, "margin": "DOUBLE"}}
    with pytest.raises(PolicyError, match=r"missing in the policy: \['margin'\]"):
        load_policy([_write(tmp_path, _doc())], schemas)


def test_column_not_in_warehouse_refused(tmp_path: Path) -> None:
    columns = SALES_COLUMNS.replace("]", ", salary]")
    with pytest.raises(PolicyError, match=r"not in the warehouse: \['salary'\]"):
        load_policy([_write(tmp_path, _doc(columns=columns))], SCHEMAS)


def test_storage_system_columns_are_ignored(tmp_path: Path) -> None:
    assert "_timestamp" in STORAGE_SYSTEM_COLUMNS
    schemas = {"drug_sales": {**SALES_TYPES, "_timestamp": "TIMESTAMP"}}
    sales = load_policy([_write(tmp_path, _doc())], schemas).datasets["drug_sales"]
    assert "_timestamp" not in sales.columns
    assert "_timestamp" not in sales.column_kinds


# ---- types ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql_type", "kind"),
    [
        # DuckDB
        ("INTEGER", KIND_NUMERIC),
        ("BIGINT", KIND_NUMERIC),
        ("HUGEINT", KIND_NUMERIC),
        ("UBIGINT", KIND_NUMERIC),
        ("DOUBLE", KIND_NUMERIC),
        ("DECIMAL(10,2)", KIND_NUMERIC),
        ("decimal(18, 3)", KIND_NUMERIC),
        ("BOOLEAN", KIND_BOOLEAN),
        ("DATE", KIND_DATE),
        ("TIMESTAMP", KIND_DATE),
        ("TIMESTAMP WITH TIME ZONE", KIND_DATE),
        ("TIME", KIND_DATE),
        ("VARCHAR", KIND_TEXT),
        ("UUID", KIND_TEXT),
        # Snowflake
        ("NUMBER(38,0)", KIND_NUMERIC),
        ("FLOAT", KIND_NUMERIC),
        ("TEXT", KIND_TEXT),
        ("VARCHAR(16777216)", KIND_TEXT),
        ("TIMESTAMP_NTZ", KIND_DATE),
        ("TIMESTAMP_LTZ", KIND_DATE),
        ("TIMESTAMP_TZ", KIND_DATE),
        # BigQuery
        ("INT64", KIND_NUMERIC),
        ("FLOAT64", KIND_NUMERIC),
        ("NUMERIC", KIND_NUMERIC),
        ("BIGNUMERIC", KIND_NUMERIC),
        ("BOOL", KIND_BOOLEAN),
        ("DATETIME", KIND_DATE),
        ("STRING", KIND_TEXT),
        ("  real  ", KIND_NUMERIC),
    ],
)
def test_column_kind(sql_type: str, kind: str) -> None:
    assert column_kind(sql_type) == kind


def test_undeclared_columns_are_text(tmp_path: Path) -> None:
    schemas = {"drug_sales": {**SALES_TYPES, "units": "VARCHAR"}}
    types = "{sale_id: numeric, sale_date: date, revenue: numeric, is_test: boolean}"
    sales = load_policy([_write(tmp_path, _doc(types=types))], schemas).datasets["drug_sales"]
    assert sales.column_kinds["units"] == KIND_TEXT
    assert sales.column_kinds["company"] == KIND_TEXT


def test_type_keys_are_case_insensitive(tmp_path: Path) -> None:
    types = "{SALE_ID: numeric, sale_date: date, Units: numeric, revenue: numeric, is_test: boolean}"
    sales = load_policy([_write(tmp_path, _doc(types=types))], SCHEMAS).datasets["drug_sales"]
    assert sales.column_kinds["sale_id"] == KIND_NUMERIC
    assert sales.column_kinds["units"] == KIND_NUMERIC


def test_declared_type_differs_from_warehouse_refused(tmp_path: Path) -> None:
    types = SALES_TYPES_YAML.replace("is_test: boolean", "is_test: numeric")
    with pytest.raises(PolicyError, match="is_test: declared numeric, warehouse boolean"):
        load_policy([_write(tmp_path, _doc(types=types))], SCHEMAS)


def test_missing_type_declaration_refused(tmp_path: Path) -> None:
    types = SALES_TYPES_YAML.replace(", sale_date: date", "")
    with pytest.raises(PolicyError, match="sale_date: declared text, warehouse date"):
        load_policy([_write(tmp_path, _doc(types=types))], SCHEMAS)


def test_type_of_unknown_column_refused(tmp_path: Path) -> None:
    types = SALES_TYPES_YAML.replace("}", ", salary: numeric}")
    with pytest.raises(PolicyError, match="types names unknown column 'salary'"):
        load_catalog([_write(tmp_path, _doc(types=types))])


def test_unknown_type_kind_refused(tmp_path: Path) -> None:
    types = SALES_TYPES_YAML.replace("units: numeric", "units: integer")
    with pytest.raises(PolicyError, match="type of 'units' must be one of"):
        load_catalog([_write(tmp_path, _doc(types=types))])


# ---- examples ---------------------------------------------------------------------------------


def test_valid_examples_parse(tmp_path: Path) -> None:
    rows = [
        "{kind: show, who: everyone, filter: {all: true}}",
        "{kind: show, who: {user: a@b.example}, filter: {conditions: [{column: units, op: gt, value: 5}]}}",
        "{kind: show, who: {local_group: emea-sales}, filter: {conditions: [{column: company, op: is, source: email}]}}",
        f'{{kind: hide, who: {{group_external_id: "{FINANCE_EXTERNAL_ID}"}}, '
        "filter: {conditions: [{column: is_test, op: is, value: true}]}}",
        "{kind: show, who: everyone, filter: {conditions: [{column: sale_date, op: gte, value: '2026-01-01'}]}}",
        "{kind: show, who: everyone, filter: {conditions: [{column: visibility, op: one_of, values: [public, partner]}]}}",
    ]
    masks = (
        "{revenue: [{who: {local_group: emea-sales}, treatment: clear}, {who: everyone, treatment: mask_partial}], "
        "company: [{who: everyone, treatment: mask_hash}], visibility: [{who: everyone, treatment: hidden}]}"
    )
    sales = _sales_dataset(tmp_path, f"{{rows: [{', '.join(rows)}], masks: {masks}}}")
    assert sales.has_examples
    assert [r.kind for r in sales.examples.rows] == [KIND_SHOW] * 3 + [KIND_HIDE] + [KIND_SHOW] * 2
    assert [m.treatment for m in sales.examples.masks["revenue"]] == [CLEAR, MASK_PARTIAL]
    assert [m.treatment for m in sales.examples.masks["visibility"]] == [HIDDEN]


@pytest.mark.parametrize(
    ("who", "message"),
    [
        ("somebody", "who must be 'everyone' or one of"),
        ("{email: a@b.example}", "who must be 'everyone' or one of"),
        ("{user: a@b.example, local_group: x}", "who must be 'everyone' or one of"),
        ("{user: ''}", "who needs a non-empty id"),
    ],
)
def test_bad_who_refused(tmp_path: Path, who: str, message: str) -> None:
    examples = _rows_example(f"{{kind: show, who: {who}, filter: {{all: true}}}}")
    with pytest.raises(PolicyError, match=message):
        load_catalog([_write(tmp_path, _doc(examples=examples))])


def test_bad_who_on_mask_refused(tmp_path: Path) -> None:
    examples = "{masks: {revenue: [{who: nobody, treatment: hidden}]}}"
    with pytest.raises(PolicyError, match="who must be 'everyone'"):
        load_catalog([_write(tmp_path, _doc(examples=examples))])


def test_bad_row_kind_refused(tmp_path: Path) -> None:
    examples = _rows_example("{kind: deny, who: everyone, filter: {all: true}}")
    with pytest.raises(PolicyError, match="kind"):
        load_catalog([_write(tmp_path, _doc(examples=examples))])


def test_bad_treatment_refused(tmp_path: Path) -> None:
    examples = "{masks: {revenue: [{who: everyone, treatment: redact}]}}"
    with pytest.raises(PolicyError, match="treatment must be one of clear, mask_partial, mask_hash, hidden"):
        load_catalog([_write(tmp_path, _doc(examples=examples))])


@pytest.mark.parametrize(
    ("filter_yaml", "message"),
    [
        ("{all: false}", "exactly"),
        ("{conditions: []}", "non-empty list"),
        ("{conditions: [{column: company, op: like, value: a}]}", "operator must be one of"),
        ("{conditions: [{column: company, op: is}]}", "needs exactly one of a value or a source"),
        ("{sql: 'TRUE'}", 'needs "conditions"'),
    ],
)
def test_malformed_example_filter_refused(tmp_path: Path, filter_yaml: str, message: str) -> None:
    examples = _rows_example(f"{{kind: show, who: everyone, filter: {filter_yaml}}}")
    with pytest.raises(PolicyError, match=message):
        load_catalog([_write(tmp_path, _doc(examples=examples))])


def test_example_filter_unknown_column_refused_with_line(tmp_path: Path) -> None:
    examples = _rows_example(
        "{kind: show, who: everyone, filter: {all: true}}",
        "{kind: show, who: everyone, filter: {conditions: [{column: salary, op: is, value: '1'}]}}",
    )
    with pytest.raises(PolicyError, match="dataset 'drug_sales', example row line 2: unknown column 'salary'"):
        load_catalog([_write(tmp_path, _doc(examples=examples))])


def test_example_filter_literal_of_wrong_type_refused(tmp_path: Path) -> None:
    examples = _rows_example("{kind: show, who: everyone, filter: {conditions: [{column: units, op: is, value: many}]}}")
    with pytest.raises(PolicyError, match=r"example row line 1: 'many' is not a number \(units is a numeric column\)"):
        load_catalog([_write(tmp_path, _doc(examples=examples))])


def test_example_mask_unknown_column_refused(tmp_path: Path) -> None:
    examples = "{masks: {salary: [{who: everyone, treatment: hidden}]}}"
    with pytest.raises(PolicyError, match="dataset 'drug_sales', example masks: unknown column 'salary'"):
        load_catalog([_write(tmp_path, _doc(examples=examples))])


def test_extra_keys_in_example_refused(tmp_path: Path) -> None:
    examples = _rows_example("{kind: show, who: everyone, filter: {all: true}, sql: 'TRUE'}")
    with pytest.raises(PolicyError, match="sql"):
        load_catalog([_write(tmp_path, _doc(examples=examples))])


# ---- validate_filter / validate_mask ----------------------------------------------------------


def _conditions(*conditions: dict[str, object]) -> RowFilter:
    return parse_filter({"conditions": list(conditions)})


def test_validate_filter_accepts_all_rows_and_typed_literals(tmp_path: Path) -> None:
    sales = _sales_dataset(tmp_path)
    validate_filter(sales, ALL_ROWS)
    validate_filter(
        sales,
        _conditions(
            {"column": "units", "op": "gte", "value": 3},
            {"column": "revenue", "op": "lt", "value": "12.50"},
            {"column": "sale_date", "op": "gt", "value": "2026-01-01"},
            {"column": "sale_date", "op": "lte", "value": "2026-01-01T10:00:00"},
            {"column": "is_test", "op": "is", "value": False},
            {"column": "is_test", "op": "is_not", "value": "TRUE"},
            {"column": "company", "op": "is", "source": "attribute:company"},
            {"column": "units", "op": "is", "source": "attribute:limit"},
            {"column": "visibility", "op": "is_empty"},
            {"column": "units", "op": "one_of", "values": [1, 2, "3.5"]},
        ),
    )


def test_validate_filter_unknown_column(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="unknown column 'salary'"):
        validate_filter(_sales_dataset(tmp_path), _conditions({"column": "salary", "op": "is", "value": "1"}))


@pytest.mark.parametrize("op", ["gt", "gte", "lt", "lte"])
def test_validate_filter_order_operator_on_text_column(tmp_path: Path, op: str) -> None:
    with pytest.raises(PolicyError, match=f"'{op}' needs a numeric or date column; 'company' is text"):
        validate_filter(_sales_dataset(tmp_path), _conditions({"column": "company", "op": op, "value": "a"}))


def test_validate_filter_order_operator_on_boolean_column(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="'is_test' is boolean"):
        validate_filter(_sales_dataset(tmp_path), _conditions({"column": "is_test", "op": OP_GT, "value": True}))


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ({"column": "units", "op": "is", "value": "five"}, r"'five' is not a number \(units is a numeric column\)"),
        ({"column": "units", "op": "one_of", "values": [1, "2;DROP"]}, "'2;DROP' is not a number"),
        ({"column": "is_test", "op": "is", "value": "yes"}, r"'yes' is not true or false \(is_test is a boolean"),
        ({"column": "is_test", "op": "not_one_of", "values": [True, "1"]}, "'1' is not true or false"),
        ({"column": "sale_date", "op": "is", "value": "2026-13-01"}, r"is not a date \(YYYY-MM-DD\)"),
        ({"column": "sale_date", "op": "one_of", "values": ["2026-01-01", "yesterday"]}, "'yesterday' is not a date"),
    ],
)
def test_validate_filter_literal_types(tmp_path: Path, condition: dict[str, object], message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        validate_filter(_sales_dataset(tmp_path), _conditions(condition))


def test_validate_filter_text_column_takes_any_literal(tmp_path: Path) -> None:
    validate_filter(_sales_dataset(tmp_path), _conditions({"column": "company", "op": "is", "value": "' OR 1=1 --"}))


def test_validate_mask(tmp_path: Path) -> None:
    sales = _sales_dataset(tmp_path)
    for treatment in (CLEAR, MASK_PARTIAL, MASK_HASH, HIDDEN):
        validate_mask(sales, "revenue", treatment)
    validate_mask(sales, " Revenue ", HIDDEN)
    with pytest.raises(PolicyError, match="unknown column 'salary'"):
        validate_mask(sales, "salary", HIDDEN)
    with pytest.raises(PolicyError, match="treatment must be one of"):
        validate_mask(sales, "revenue", "absent")


# ---- literal_problem --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "42", "-7", "3.14", " 12 ", "1e5", "2.5E-3", "-1e+20"])
def test_number_literals_accepted(value: str) -> None:
    assert literal_problem(KIND_NUMERIC, value) is None


@pytest.mark.parametrize("value", ["", "abc", "1,5", ".5", "+1", "0x10", "1 OR 1=1", "nan", "inf", "1.2.3"])
def test_number_literals_refused(value: str) -> None:
    assert literal_problem(KIND_NUMERIC, value) == f"'{value}' is not a number"


@pytest.mark.parametrize("value", ["true", "false", "TRUE", " False "])
def test_boolean_literals_accepted(value: str) -> None:
    assert literal_problem(KIND_BOOLEAN, value) is None


@pytest.mark.parametrize("value", ["yes", "1", "t", ""])
def test_boolean_literals_refused(value: str) -> None:
    assert literal_problem(KIND_BOOLEAN, value) == f"'{value}' is not true or false"


@pytest.mark.parametrize("value", ["2026-10-07", "2026-10-07T08:30:00", "2026-10-07 08:30:00"])
def test_date_literals_accepted(value: str) -> None:
    assert literal_problem(KIND_DATE, value) is None


@pytest.mark.parametrize("value", ["2026-02-30", "07.10.2026", "today", ""])
def test_date_literals_refused(value: str) -> None:
    assert literal_problem(KIND_DATE, value) == f"'{value}' is not a date (YYYY-MM-DD)"


def test_text_literals_always_accepted() -> None:
    assert literal_problem(KIND_TEXT, "anything at all; DROP TABLE x") is None


# ---- resolve_examples -------------------------------------------------------------------------


def test_resolve_pharma_examples(settings: Settings) -> None:
    catalog = load_catalog(_pharma_files(settings))
    groups = {FINANCE_EXTERNAL_ID: "g-finance", SUPPLY_EXTERNAL_ID: "g-supply", PARTNER_EXTERNAL_ID: "g-partner"}
    resolved = resolve_examples(catalog["supply_chain"], users={ANNA}, local_groups=set(), group_ids_by_external_id=groups)
    assert resolved.skipped == []
    company_from_attribute = {"conditions": [{"column": "company", "op": "is", "source": "attribute:company"}]}
    assert resolved.rows[0] == RowAccessRuleInput(KIND_SHOW, SUBJECT_GROUP, "g-supply", company_from_attribute)
    assert resolved.rows[2] == RowAccessRuleInput(KIND_SHOW, SUBJECT_GROUP, "g-finance", {"all": True})
    assert resolved.rows[3] == RowAccessRuleInput(KIND_SHOW, SUBJECT_USER, ANNA, company_from_attribute)
    assert resolved.masks == [
        ColumnMaskInput("contract_ref", SUBJECT_GROUP, "g-finance", CLEAR),
        ColumnMaskInput("contract_ref", SUBJECT_GROUP, "g-supply", MASK_HASH),
        ColumnMaskInput("contract_ref", SUBJECT_EVERYONE, "", HIDDEN),
        ColumnMaskInput("unit_cost", SUBJECT_GROUP, "g-partner", HIDDEN),
        ColumnMaskInput("unit_cost", SUBJECT_USER, ANNA, MASK_PARTIAL),
    ]


def test_resolve_skips_unregistered_subjects_and_names_the_line(settings: Settings) -> None:
    catalog = load_catalog(_pharma_files(settings))
    resolved = resolve_examples(
        catalog["supply_chain"], users=set(), local_groups=set(), group_ids_by_external_id={FINANCE_EXTERNAL_ID: "g-f"}
    )
    assert [r.subject_id for r in resolved.rows] == ["g-f"]
    assert resolved.skipped == [
        f"Row line 1: IdP group '{SUPPLY_EXTERNAL_ID}' is not registered",
        f"Row line 2: IdP group '{PARTNER_EXTERNAL_ID}' is not registered",
        f"Row line 4: user '{ANNA}' is not registered",
        f"Mask on contract_ref, line 2: IdP group '{SUPPLY_EXTERNAL_ID}' is not registered",
        f"Mask on unit_cost, line 1: IdP group '{PARTNER_EXTERNAL_ID}' is not registered",
        f"Mask on unit_cost, line 2: user '{ANNA}' is not registered",
    ]
    assert [(m.column_name, m.subject_type) for m in resolved.masks] == [
        ("contract_ref", SUBJECT_GROUP),
        ("contract_ref", SUBJECT_EVERYONE),
    ]


def test_resolve_users_local_groups_and_lowercased_columns(tmp_path: Path) -> None:
    rows = [
        "{kind: show, who: {user: ' Ann@B.Example '}, filter: {all: true}}",
        "{kind: hide, who: {local_group: emea-sales}, filter: {conditions: [{column: units, op: is, value: 0}]}}",
        "{kind: show, who: {local_group: ghosts}, filter: {all: true}}",
        "{kind: show, who: everyone, filter: {all: true}}",
    ]
    masks = "{Revenue: [{who: {local_group: emea-sales}, treatment: mask_partial}]}"
    examples = f"{{rows: [{', '.join(rows)}], masks: {masks}}}"
    sales = _sales_dataset(tmp_path, examples)
    resolved = resolve_examples(
        sales, users={"ann@b.example"}, local_groups={"emea-sales"}, group_ids_by_external_id={}
    )
    assert [(r.kind, r.subject_type, r.subject_id) for r in resolved.rows] == [
        (KIND_SHOW, SUBJECT_USER, "ann@b.example"),
        (KIND_HIDE, SUBJECT_LOCAL_GROUP, "emea-sales"),
        (KIND_SHOW, SUBJECT_EVERYONE, ""),
    ]
    assert resolved.rows[1].filter == {"conditions": [{"column": "units", "op": "is", "value": 0}]}
    assert resolved.masks == [ColumnMaskInput("revenue", SUBJECT_LOCAL_GROUP, "emea-sales", MASK_PARTIAL)]
    assert resolved.skipped == ["Row line 3: local group 'ghosts' does not exist"]


def test_resolve_dataset_without_examples(tmp_path: Path) -> None:
    resolved = resolve_examples(_sales_dataset(tmp_path), users=set(), local_groups=set(), group_ids_by_external_id={})
    assert (resolved.rows, resolved.masks, resolved.skipped) == ([], [], [])


# ---- fingerprint ------------------------------------------------------------------------------


def test_fingerprint_stable_and_changes_with_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path, _doc())
    first = load_policy([path], SCHEMAS).fingerprint
    assert len(first) == FINGERPRINT_LENGTH
    assert load_policy([path], SCHEMAS).fingerprint == first
    path.write_text(path.read_text(encoding="utf-8") + "\n# comment only\n", encoding="utf-8")
    assert load_policy([path], SCHEMAS).fingerprint != first


def test_fingerprint_covers_every_file(tmp_path: Path) -> None:
    a = _write(tmp_path, _doc(), "a.yaml")
    b = _write(tmp_path, _doc(dataset="stores", source="stores", columns="[store_id, company]", types="{}"), "b.yaml")
    only_a = load_policy([a], SCHEMAS).fingerprint
    both = load_policy([a, b], SCHEMAS)
    assert set(both.datasets) == {"drug_sales", "stores"}
    assert both.fingerprint != only_a


# ---- security_model.parse_filter --------------------------------------------------------------


def test_parse_all_rows() -> None:
    assert parse_filter({"all": True}) is ALL_ROWS
    assert ALL_ROWS.all_rows and ALL_ROWS.conditions == ()


@pytest.mark.parametrize("raw", [{"all": False}, {"all": "true"}, {"all": True, "conditions": []}])
def test_all_rows_form_is_exact(raw: dict[str, object]) -> None:
    with pytest.raises(FilterError, match=r'exactly \{"all": true\}'):
        parse_filter(raw)


@pytest.mark.parametrize("raw", [None, [], "TRUE", 1])
def test_filter_must_be_an_object(raw: object) -> None:
    with pytest.raises(FilterError, match="the filter must be an object"):
        parse_filter(raw)


@pytest.mark.parametrize("raw", [{}, {"conditions": [], "extra": 1}, {"where": "1=1"}])
def test_filter_needs_conditions(raw: dict[str, object]) -> None:
    with pytest.raises(FilterError, match='needs "conditions"'):
        parse_filter(raw)


@pytest.mark.parametrize("conditions", [[], None, "company = 'a'", {"column": "a"}])
def test_conditions_must_be_a_non_empty_list(conditions: object) -> None:
    with pytest.raises(FilterError, match="must be a non-empty list"):
        parse_filter({"conditions": conditions})


def test_parse_conditions() -> None:
    parsed = parse_filter(
        {
            "conditions": [
                {"column": " Company ", "op": "is", "value": "Pharma A"},
                {"column": "units", "op": "one_of", "values": ["1", "2"]},
                {"column": "company", "op": "is_not", "source": "attribute:company"},
                {"column": "owner", "op": "is", "source": " email "},
                {"column": "region", "op": "is_not_empty"},
            ]
        }
    )
    assert not parsed.all_rows
    assert parsed.conditions == (
        Condition("company", OP_IS, value="Pharma A"),
        Condition("units", OP_ONE_OF, values=("1", "2")),
        Condition("company", "is_not", source="attribute:company"),
        Condition("owner", OP_IS, source="email"),
        Condition("region", "is_not_empty"),
    )
    assert parsed.conditions[2].attribute == "company"
    assert parsed.conditions[3].attribute is None
    assert parsed.conditions[0].attribute is None


@pytest.mark.parametrize(
    "condition",
    [
        {"column": "c", "op": "is"},
        {"column": "c", "op": "is", "value": "a", "source": "email"},
        {"column": "c", "op": "is", "values": ["a"]},
        {"column": "c", "op": "gt", "value": "1", "values": ["1"]},
    ],
)
def test_scalar_operators_need_exactly_one_of_value_or_source(condition: dict[str, object]) -> None:
    with pytest.raises(FilterError, match="needs exactly one of a value or a source"):
        parse_filter({"conditions": [condition]})


@pytest.mark.parametrize("extra", [{"value": "a"}, {"values": ["a"]}, {"source": "email"}])
def test_emptiness_operators_take_no_value(extra: dict[str, object]) -> None:
    with pytest.raises(FilterError, match="'is_empty' on 'c' takes no value"):
        parse_filter({"conditions": [{"column": "c", "op": OP_IS_EMPTY, **extra}]})


def test_emptiness_operator_ignores_null_fields() -> None:
    parsed = parse_filter({"conditions": [{"column": "c", "op": OP_IS_EMPTY, "value": None}]})
    assert parsed.conditions == (Condition("c", OP_IS_EMPTY),)


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ({"column": "c", "op": "one_of", "value": "a"}, "takes a list of values"),
        ({"column": "c", "op": "one_of", "values": ["a"], "source": "email"}, "takes a list of values"),
        ({"column": "c", "op": "not_one_of", "source": "email"}, "takes a list of values"),
        ({"column": "c", "op": "one_of", "values": []}, "needs a non-empty list of values"),
        ({"column": "c", "op": "one_of", "values": "a,b"}, "needs a non-empty list of values"),
        ({"column": "c", "op": "one_of", "values": [["nested"]]}, "must be a text, number or boolean value"),
        ({"column": "c", "op": "one_of", "values": [None]}, "must be a text, number or boolean value"),
    ],
)
def test_list_operators_need_a_non_empty_values_list(condition: dict[str, object], message: str) -> None:
    with pytest.raises(FilterError, match=message):
        parse_filter({"conditions": [condition]})


@pytest.mark.parametrize("source", ["email", "attribute:company", "attribute:cost_center_2", "attribute:" + "a" * MAX_ATTRIBUTE_KEY_LENGTH])
def test_valid_sources(source: str) -> None:
    assert parse_filter({"conditions": [{"column": "c", "op": "is", "source": source}]}).conditions[0].source == source


@pytest.mark.parametrize(
    "source",
    [
        "Email",
        "user",
        "attribute:",
        "attribute:Company",
        "attribute:1company",
        "attribute:company-id",
        "attribute:company;drop",
        "attribute:" + "a" * (MAX_ATTRIBUTE_KEY_LENGTH + 1),
        "attr:company",
        42,
        ["email"],
    ],
)
def test_bad_sources_refused(source: object) -> None:
    with pytest.raises(FilterError, match="source must be 'email' or 'attribute:<key>'"):
        parse_filter({"conditions": [{"column": "c", "op": "is", "source": source}]})


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ({"column": "c", "op": "is", "value": "a", "sql": "1=1"}, r"unknown condition field\(s\): sql"),
        ({"column": "c", "op": "is", "value": "a", "b": 1, "a": 2}, r"unknown condition field\(s\): a, b"),
        ({"op": "is", "value": "a"}, "each condition needs a column"),
        ({"column": "  ", "op": "is", "value": "a"}, "each condition needs a column"),
        ({"column": 7, "op": "is", "value": "a"}, "each condition needs a column"),
        ({"column": "c", "op": "eq", "value": "a"}, "operator must be one of"),
        ({"column": "c", "value": "a"}, "operator must be one of"),
        ({"column": "c", "op": "is", "value": {"x": 1}}, "must be a text, number or boolean value"),
    ],
)
def test_malformed_conditions_refused(condition: dict[str, object], message: str) -> None:
    with pytest.raises(FilterError, match=message):
        parse_filter({"conditions": [condition]})


def test_condition_must_be_an_object() -> None:
    with pytest.raises(FilterError, match="each condition must be an object"):
        parse_filter({"conditions": ["company = 'a'"]})


@pytest.mark.parametrize(
    ("value", "text"),
    [(True, "true"), (False, "false"), (5, "5"), (-3, "-3"), (2.5, "2.5"), (1e20, "1e+20"), ("Pharma A", "Pharma A")],
)
def test_scalars_normalised_to_text(value: object, text: str) -> None:
    parsed = parse_filter({"conditions": [{"column": "c", "op": "is", "value": value}]})
    assert parsed.conditions[0].value == text
    listed = parse_filter({"conditions": [{"column": "c", "op": "one_of", "values": [value, "x"]}]})
    assert listed.conditions[0].values == (text, "x")


def test_filter_error_is_a_value_error() -> None:
    assert issubclass(FilterError, ValueError)


@pytest.mark.parametrize(
    "raw",
    [
        {"all": True},
        {
            "conditions": [
                {"column": "company", "op": "is", "value": "A"},
                {"column": "units", "op": "not_one_of", "values": ["1", "2"]},
                {"column": "company", "op": "is", "source": "attribute:company"},
                {"column": "owner", "op": "is_not", "source": "email"},
                {"column": "region", "op": "is_empty"},
            ]
        },
    ],
)
def test_to_json_round_trip(raw: Mapping[str, object]) -> None:
    parsed = parse_filter(raw)
    assert parsed.to_json() == raw
    assert parse_filter(parsed.to_json()) == parsed


def test_to_json_normalises_input() -> None:
    parsed = parse_filter({"conditions": [{"column": " Units ", "op": "one_of", "values": [1, True]}]})
    assert parsed.to_json() == {"conditions": [{"column": "units", "op": "one_of", "values": ["1", "true"]}]}


# ---- security_model.Audience ------------------------------------------------------------------

AUDIENCE = Audience("ann@b.example", frozenset({"emea-sales"}), frozenset({"g-finance"}))


@pytest.mark.parametrize(
    ("subject_type", "subject_id", "expected"),
    [
        (SUBJECT_EVERYONE, "", True),
        (SUBJECT_USER, "ann@b.example", True),
        (SUBJECT_USER, "bob@b.example", False),
        (SUBJECT_LOCAL_GROUP, "emea-sales", True),
        (SUBJECT_LOCAL_GROUP, "g-finance", False),  # an IdP group id is not a local group
        (SUBJECT_GROUP, "g-finance", True),
        (SUBJECT_GROUP, "emea-sales", False),  # a local group is not an IdP group
        ("role", "ann@b.example", False),
    ],
)
def test_audience_matches(subject_type: str, subject_id: str, expected: bool) -> None:
    assert AUDIENCE.matches(subject_type, subject_id) is expected


@pytest.mark.parametrize(
    ("subject_type", "subject_id", "sentence"),
    [
        (SUBJECT_EVERYONE, "", "every signed-in user"),
        (SUBJECT_USER, "ann@b.example", "this user"),
        (SUBJECT_USER, "bob@b.example", "only for user bob@b.example"),
        (SUBJECT_LOCAL_GROUP, "emea-sales", "member of local group emea-sales"),
        (SUBJECT_LOCAL_GROUP, "other", "not a member of local group other"),
        (SUBJECT_GROUP, "g-finance", "carries IdP group g-finance"),
        (SUBJECT_GROUP, "g-other", "does not carry IdP group g-other"),
        ("role", "x", "unknown subject type 'role'"),
    ],
)
def test_audience_explain(subject_type: str, subject_id: str, sentence: str) -> None:
    assert AUDIENCE.explain(subject_type, subject_id) == sentence
