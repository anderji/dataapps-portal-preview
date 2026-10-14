"""Dataset security in the access store: row access lines and column masks.

Covers the SQLite `AccessStore` (validation, positions, ordering, cascades, snapshot,
raw table round trip), the `KeboolaAccessStore` on the fake Storage API (what reaches
Storage, and only that) and the demo seed that copies the pharma.yaml examples once.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kbc_access.config import Settings
from kbc_access.demo_seed import fill_store, load_example_security
from kbc_access.identity import Principal
from kbc_access.keboola_store import KeboolaAccessStore, storage_columns, storage_primary_key
from kbc_access.security_model import (
    ALL_ROWS,
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
    FilterError,
    parse_filter,
)
from kbc_access.store import (
    EMPTY_SECURITY,
    FILTER_CONDITIONS_MAX,
    FILTER_VALUE_MAX_CHARS,
    FILTER_VALUES_MAX,
    MASK_LINES_MAX_PER_DATASET,
    ROW_LINES_MAX_PER_DATASET,
    TABLE_COLUMNS,
    TABLE_KEYS,
    AccessStore,
    ColumnMaskInput,
    DatasetSecurity,
    RowAccessRuleInput,
    StoreError,
)
from tests.fake_storage_api import FakeStorageApi

BUCKET = "in.c-apps-control-plane"
FINANCE = "3f2b8c1e-7d4a-4e5b-9a61-2c8d0f4e1b7a"
SUPPLY = "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9"
PARTNER = "00g7pharmab0partner1"
UNKNOWN_EXTERNAL = "99999999-9999-4999-8999-999999999999"
PETR = "petr.sales@pharma-a.example"
EVA = "eva.supply@pharma-a.example"
ANNA = "anna.admin@pharma-a.example"
TEAM = "team"
OTHER_TEAM = "other-team"
DRUG_SALES = "drug_sales"
STORES = "stores"
SUPPLY_CHAIN = "supply_chain"
SECURITY_TABLES = ("row_access_rules", "column_masks")

PUBLIC = {"conditions": [{"column": "visibility", "op": "is", "value": "public"}]}
OWN_COMPANY = {"conditions": [{"column": "company", "op": "is", "source": "attribute:company"}]}
TEST_ROWS = {"conditions": [{"column": "is_test", "op": "is", "value": "true"}]}
ALL = {"all": True}

# Line ids are 12 lowercase hex characters (store.SECURITY_ID).
RULE_ID_A = "0123456789ab"
RULE_ID_B = "abcdef012345"
RULE_ID_C = "fedcba987654"
MASK_ID_A = "111111111111"
MASK_ID_B = "222222222222"

# What the pharma.yaml examples load per dataset (all subjects are registered by the seed).
EXAMPLE_COUNTS = {
    DRUG_SALES: {"show": 3, "hide": 1, "masks": {"margin": 2, "customer_id": 2}},
    STORES: {"show": 3, "hide": 0, "masks": {"manager_email": 2}},
    SUPPLY_CHAIN: {"show": 4, "hide": 0, "masks": {"contract_ref": 3, "unit_cost": 2}},
}


# ---- helpers / fixtures ---------------------------------------------------------------------------


def show(subject_type: str = SUBJECT_EVERYONE, subject_id: str = "", row_filter: dict | None = None,
         rule_id: str | None = None) -> RowAccessRuleInput:
    return RowAccessRuleInput(KIND_SHOW, subject_type, subject_id, PUBLIC if row_filter is None else row_filter, rule_id)


def hide(subject_type: str = SUBJECT_EVERYONE, subject_id: str = "", row_filter: dict | None = None,
         rule_id: str | None = None) -> RowAccessRuleInput:
    return RowAccessRuleInput(KIND_HIDE, subject_type, subject_id, TEST_ROWS if row_filter is None else row_filter, rule_id)


def mask(column: str, treatment: str = HIDDEN, subject_type: str = SUBJECT_EVERYONE, subject_id: str = "",
         mask_id: str | None = None) -> ColumnMaskInput:
    return ColumnMaskInput(column, subject_type, subject_id, treatment, mask_id)


def condition_filter(count: int) -> dict:
    return {"conditions": [{"column": f"c{i}", "op": "is", "value": "x"} for i in range(count)]}


def everything(store: AccessStore | KeboolaAccessStore) -> tuple[list, list]:
    return store.list_row_access_rules(), store.list_column_masks()


class Subjects:
    """Ids of the subjects registered by `register_subjects`."""

    def __init__(self, finance: str, supply: str) -> None:
        self.finance = finance
        self.supply = supply


def register_subjects(store: AccessStore | KeboolaAccessStore) -> Subjects:
    store.upsert_user(PETR, "Petr", True)
    store.upsert_user(EVA, "Eva", True)
    store.create_local_group(TEAM, "Team")
    store.create_local_group(OTHER_TEAM, "Other team")
    store.add_local_group_member(TEAM, PETR)
    finance = store.create_group("entra", FINANCE, "finance").group_id
    supply = store.create_group("entra", SUPPLY, "supply-chain").group_id
    return Subjects(finance, supply)


@pytest.fixture
def store(settings: Settings, tmp_path: Path) -> AccessStore:
    return AccessStore(tmp_path / "store.sqlite", settings.providers)


@pytest.fixture
def subjects(store: AccessStore) -> Subjects:
    return register_subjects(store)


# ---- table definitions -------------------------------------------------------------------------------


def test_table_columns_and_keys_for_the_security_tables(store: AccessStore) -> None:
    assert TABLE_COLUMNS["row_access_rules"] == [
        "rule_id", "dataset", "position", "kind", "subject_type", "subject_id", "filter",
    ]
    assert TABLE_COLUMNS["column_masks"] == [
        "mask_id", "dataset", "column_name", "position", "subject_type", "subject_id", "treatment",
    ]
    assert TABLE_KEYS["row_access_rules"] == ["rule_id"]
    assert TABLE_KEYS["column_masks"] == ["mask_id"]
    assert TABLE_COLUMNS.keys() == TABLE_KEYS.keys()
    for table in SECURITY_TABLES:
        assert store.dump_rows(table) == []


def test_data_policy_tables_are_gone(store: AccessStore, tmp_path: Path) -> None:
    assert not [t for t in (*TABLE_COLUMNS, *TABLE_KEYS) if t.startswith("data_polic")]
    with sqlite3.connect(tmp_path / "store.sqlite") as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(SECURITY_TABLES) <= names
    assert not [n for n in names if n.startswith("data_polic")]


# ---- set_dataset_security: replace as one unit -----------------------------------------------------------


def test_set_replaces_lines_and_masks_as_one_unit(store: AccessStore, subjects: Subjects) -> None:
    first = store.set_dataset_security(
        DRUG_SALES,
        [show(), show(SUBJECT_GROUP, subjects.finance, ALL), hide()],
        [mask("margin", CLEAR, SUBJECT_GROUP, subjects.finance), mask("margin", HIDDEN)],
    )
    assert len(first.rows) == 3 and len(first.masks) == 2
    assert store.dataset_security(DRUG_SALES) == first

    second = store.set_dataset_security(DRUG_SALES, [show(SUBJECT_USER, PETR, OWN_COMPANY)], [])
    assert store.dataset_security(DRUG_SALES) == second
    assert [(r.kind, r.subject_type, r.subject_id) for r in second.rows] == [(KIND_SHOW, SUBJECT_USER, PETR)]
    assert second.masks == ()
    assert store.list_column_masks(DRUG_SALES) == []
    # Ids from the first save are gone: nothing is left over from it.
    assert not {r.rule_id for r in first.rows} & {r.rule_id for r in store.list_row_access_rules()}


def test_set_returns_what_is_stored_with_parsed_filters(store: AccessStore, subjects: Subjects) -> None:
    saved = store.set_dataset_security(DRUG_SALES, [show(row_filter=ALL), show(row_filter=OWN_COMPANY)], [])
    assert saved == store.dataset_security(DRUG_SALES)
    assert [r.filter for r in saved.rows] == [ALL_ROWS, parse_filter(OWN_COMPANY)]
    assert all(r.dataset == DRUG_SALES for r in saved.rows)
    stored = {r["rule_id"]: r for r in store.dump_rows("row_access_rules")}
    for rule in saved.rows:
        assert json.loads(stored[rule.rule_id]["filter"]) == rule.filter.to_json()


def test_new_lines_get_fresh_ids_and_given_ids_are_kept(store: AccessStore, subjects: Subjects) -> None:
    saved = store.set_dataset_security(
        DRUG_SALES,
        [show(rule_id=RULE_ID_A), show(row_filter=ALL), hide(rule_id=f"  {RULE_ID_B} ")],
        [mask("margin", mask_id=MASK_ID_A), mask("customer_id")],
    )
    ids = {r.rule_id for r in saved.rows}
    assert {RULE_ID_A, RULE_ID_B} <= ids and len(ids) == 3
    fresh = (ids - {RULE_ID_A, RULE_ID_B}).pop()
    assert len(fresh) == 12 and all(c in "0123456789abcdef" for c in fresh)
    assert {m.mask_id for m in saved.masks} >= {MASK_ID_A}

    # A re-save that hands the ids back keeps every one of them.
    again = store.set_dataset_security(
        DRUG_SALES,
        [show(rule_id=RULE_ID_A), show(row_filter=ALL, rule_id=fresh), hide(rule_id=RULE_ID_B)],
        [mask("margin", mask_id=MASK_ID_A)],
    )
    assert {r.rule_id for r in again.rows} == {RULE_ID_A, RULE_ID_B, fresh}
    assert [m.mask_id for m in again.masks] == [MASK_ID_A]


def test_positions_per_kind_and_per_column_follow_list_order(store: AccessStore, subjects: Subjects) -> None:
    saved = store.set_dataset_security(
        DRUG_SALES,
        [
            hide(rule_id=RULE_ID_A),
            show(SUBJECT_USER, PETR, rule_id=RULE_ID_B),
            hide(SUBJECT_LOCAL_GROUP, TEAM, rule_id=RULE_ID_C),
            show(SUBJECT_GROUP, subjects.finance, ALL),
        ],
        [
            mask("revenue", MASK_PARTIAL, SUBJECT_USER, PETR, mask_id=MASK_ID_A),
            mask("customer_id", MASK_HASH, SUBJECT_GROUP, subjects.finance),
            mask("revenue", HIDDEN, mask_id=MASK_ID_B),
            mask("customer_id", MASK_PARTIAL),
        ],
    )
    by_id = {r.rule_id: r for r in saved.rows}
    assert (by_id[RULE_ID_A].kind, by_id[RULE_ID_A].position) == (KIND_HIDE, 1)
    assert (by_id[RULE_ID_C].kind, by_id[RULE_ID_C].position) == (KIND_HIDE, 2)
    assert (by_id[RULE_ID_B].kind, by_id[RULE_ID_B].position) == (KIND_SHOW, 1)
    finance_line = next(r for r in saved.rows if r.subject_type == SUBJECT_GROUP)
    assert (finance_line.kind, finance_line.position) == (KIND_SHOW, 2)

    masks = {m.mask_id: m for m in saved.masks}
    assert (masks[MASK_ID_A].column_name, masks[MASK_ID_A].position) == ("revenue", 1)
    assert (masks[MASK_ID_B].column_name, masks[MASK_ID_B].position) == ("revenue", 2)
    assert [(m.column_name, m.position, m.treatment) for m in saved.masks if m.column_name == "customer_id"] == [
        ("customer_id", 1, MASK_HASH), ("customer_id", 2, MASK_PARTIAL),
    ]
    # Stored as text in the table (Storage tables are all strings).
    assert {r["position"] for r in store.dump_rows("row_access_rules")} == {"1", "2"}


def test_rows_are_listed_show_before_hide_by_position(store: AccessStore, subjects: Subjects) -> None:
    store.set_dataset_security(
        DRUG_SALES,
        [
            hide(rule_id=RULE_ID_A),
            show(SUBJECT_USER, PETR, rule_id=RULE_ID_B),
            hide(SUBJECT_LOCAL_GROUP, TEAM, rule_id=RULE_ID_C),
            show(SUBJECT_GROUP, subjects.finance, ALL),
        ],
        [],
    )
    listed = store.list_row_access_rules(DRUG_SALES)
    assert [(r.kind, r.position) for r in listed] == [(KIND_SHOW, 1), (KIND_SHOW, 2), (KIND_HIDE, 1), (KIND_HIDE, 2)]
    assert listed[0].rule_id == RULE_ID_B
    assert [r.rule_id for r in listed[2:]] == [RULE_ID_A, RULE_ID_C]
    assert store.dataset_security(DRUG_SALES).rows == tuple(listed)


def test_lists_across_datasets_are_grouped_by_dataset(store: AccessStore, subjects: Subjects) -> None:
    store.set_dataset_security(STORES, [hide(), show()], [mask("manager_email", MASK_PARTIAL)])
    store.set_dataset_security(DRUG_SALES, [show(), hide()], [mask("margin"), mask("customer_id", MASK_HASH)])
    rules = store.list_row_access_rules()
    assert [(r.dataset, r.kind) for r in rules] == [
        (DRUG_SALES, KIND_SHOW), (DRUG_SALES, KIND_HIDE), (STORES, KIND_SHOW), (STORES, KIND_HIDE),
    ]
    masks = store.list_column_masks()
    assert [(m.dataset, m.column_name) for m in masks] == [
        (DRUG_SALES, "customer_id"), (DRUG_SALES, "margin"), (STORES, "manager_email"),
    ]


def test_masks_are_listed_by_column_then_position(store: AccessStore, subjects: Subjects) -> None:
    store.set_dataset_security(
        DRUG_SALES,
        [],
        [
            mask("revenue", MASK_PARTIAL, SUBJECT_USER, PETR),
            mask("customer_id", MASK_HASH, SUBJECT_GROUP, subjects.finance),
            mask("revenue", HIDDEN),
            mask("customer_id", MASK_PARTIAL),
            mask("revenue", CLEAR, SUBJECT_LOCAL_GROUP, TEAM),
        ],
    )
    listed = store.list_column_masks(DRUG_SALES)
    assert [(m.column_name, m.position, m.treatment) for m in listed] == [
        ("customer_id", 1, MASK_HASH),
        ("customer_id", 2, MASK_PARTIAL),
        ("revenue", 1, MASK_PARTIAL),
        ("revenue", 2, HIDDEN),
        ("revenue", 3, CLEAR),
    ]
    assert store.dataset_security(DRUG_SALES).masks == tuple(listed)


def test_mask_column_names_are_normalised(store: AccessStore, subjects: Subjects) -> None:
    saved = store.set_dataset_security(DRUG_SALES, [], [mask(" Revenue "), mask("REVENUE", CLEAR)])
    assert [(m.column_name, m.position) for m in saved.masks] == [("revenue", 1), ("revenue", 2)]


def test_dataset_name_is_normalised(store: AccessStore, subjects: Subjects) -> None:
    saved = store.set_dataset_security(" Drug_Sales ", [show()], [mask("margin")])
    assert saved.rows[0].dataset == DRUG_SALES and saved.masks[0].dataset == DRUG_SALES
    assert store.list_row_access_rules(" DRUG_SALES ") == list(saved.rows)
    assert store.list_column_masks("Drug_Sales") == list(saved.masks)
    assert store.dataset_security("DRUG_SALES") == saved


@pytest.mark.parametrize("dataset", ["", "   ", "a b", "-x", ".x", "a/b", "x" * 65, "drug;sales", "ž"])
def test_invalid_dataset_name_refused(store: AccessStore, dataset: str) -> None:
    with pytest.raises(StoreError, match="Dataset name"):
        store.set_dataset_security(dataset, [show()], [])
    assert everything(store) == ([], [])


def test_empty_save_clears_one_dataset_and_leaves_others(store: AccessStore, subjects: Subjects) -> None:
    stores = store.set_dataset_security(STORES, [show(), show(SUBJECT_GROUP, subjects.finance, ALL)],
                                        [mask("manager_email", MASK_PARTIAL)])
    store.set_dataset_security(DRUG_SALES, [show(), hide()], [mask("margin")])
    store.set_dataset_security(DRUG_SALES, [], [])
    assert store.dataset_security(DRUG_SALES) == EMPTY_SECURITY
    assert store.dataset_security(STORES) == stores
    assert store.dataset_security("never_configured") == EMPTY_SECURITY


def test_saving_one_dataset_does_not_touch_another(store: AccessStore, subjects: Subjects) -> None:
    stores = store.set_dataset_security(STORES, [show(rule_id=RULE_ID_A)], [mask("manager_email", mask_id=MASK_ID_A)])
    store.set_dataset_security(DRUG_SALES, [show(), hide(), show(row_filter=ALL)], [mask("margin")])
    store.set_dataset_security(DRUG_SALES, [show(row_filter=OWN_COMPANY)], [mask("customer_id", MASK_HASH)])
    assert store.dataset_security(STORES) == stores


def test_values_from_json_are_kept_as_text(store: AccessStore) -> None:
    row_filter = {"conditions": [
        {"column": "Units", "op": "gt", "value": 5},
        {"column": "is_test", "op": "is", "value": False},
        {"column": "store_id", "op": "one_of", "values": ["A001", 7]},
    ]}
    saved = store.set_dataset_security(DRUG_SALES, [show(row_filter=row_filter)], [])
    conditions = saved.rows[0].filter.conditions
    assert [(c.column, c.op, c.value, c.values) for c in conditions] == [
        ("units", "gt", "5", None), ("is_test", "is", "false", None), ("store_id", "one_of", None, ("A001", "7")),
    ]
    assert store.list_row_access_rules(DRUG_SALES)[0].filter == saved.rows[0].filter


# ---- subjects -------------------------------------------------------------------------------------------


def test_every_subject_type_is_accepted(store: AccessStore, subjects: Subjects) -> None:
    saved = store.set_dataset_security(
        DRUG_SALES,
        [show(), show(SUBJECT_USER, PETR), show(SUBJECT_LOCAL_GROUP, TEAM), show(SUBJECT_GROUP, subjects.finance)],
        [mask("margin", CLEAR, SUBJECT_GROUP, subjects.finance), mask("margin", MASK_HASH, SUBJECT_LOCAL_GROUP, TEAM),
         mask("margin", MASK_PARTIAL, SUBJECT_USER, PETR), mask("margin", HIDDEN)],
    )
    assert [(r.subject_type, r.subject_id) for r in saved.rows] == [
        (SUBJECT_EVERYONE, ""), (SUBJECT_USER, PETR), (SUBJECT_LOCAL_GROUP, TEAM), (SUBJECT_GROUP, subjects.finance),
    ]
    assert [(m.subject_type, m.subject_id) for m in saved.masks] == [
        (SUBJECT_GROUP, subjects.finance), (SUBJECT_LOCAL_GROUP, TEAM), (SUBJECT_USER, PETR), (SUBJECT_EVERYONE, ""),
    ]


def test_user_subject_ids_are_lowercased(store: AccessStore, subjects: Subjects) -> None:
    saved = store.set_dataset_security(
        DRUG_SALES, [show(SUBJECT_USER, "  Petr.Sales@Pharma-A.Example ")], [mask("margin", CLEAR, SUBJECT_USER, PETR.upper())]
    )
    assert saved.rows[0].subject_id == PETR and saved.masks[0].subject_id == PETR


def test_everyone_takes_no_id(store: AccessStore, subjects: Subjects) -> None:
    saved = store.set_dataset_security(DRUG_SALES, [show(SUBJECT_EVERYONE, "   ")], [])
    assert saved.rows[0].subject_id == ""
    with pytest.raises(StoreError, match=r"^Row line 2: 'everyone' takes no subject id$"):
        store.set_dataset_security(DRUG_SALES, [show(), show(SUBJECT_EVERYONE, PETR)], [])
    with pytest.raises(StoreError, match=r"^Mask on margin, line 1: 'everyone' takes no subject id$"):
        store.set_dataset_security(DRUG_SALES, [], [mask("margin", subject_id=TEAM)])


@pytest.mark.parametrize(
    ("subject_type", "subject_id", "message"),
    [
        (SUBJECT_USER, "ghost@pharma-a.example", "unknown user 'ghost@pharma-a.example'"),
        (SUBJECT_USER, "Ghost@Pharma-A.example", "unknown user 'ghost@pharma-a.example'"),
        (SUBJECT_LOCAL_GROUP, "nope", "unknown local group 'nope'"),
        (SUBJECT_GROUP, FINANCE, f"unknown group '{FINANCE}'"),  # an external id is not a group id
        (SUBJECT_GROUP, "000000000000", "unknown group '000000000000'"),
    ],
)
def test_unknown_subjects_are_refused_naming_the_line(
    store: AccessStore, subjects: Subjects, subject_type: str, subject_id: str, message: str
) -> None:
    with pytest.raises(StoreError) as row_error:
        store.set_dataset_security(DRUG_SALES, [show(), show(subject_type, subject_id)], [])
    assert str(row_error.value) == f"Row line 2: {message}"
    with pytest.raises(StoreError) as mask_error:
        store.set_dataset_security(
            DRUG_SALES, [], [mask("margin"), mask("revenue", CLEAR, subject_type, subject_id)]
        )
    assert str(mask_error.value) == f"Mask on revenue, line 1: {message}"
    assert everything(store) == ([], [])


@pytest.mark.parametrize("subject_type", ["role", "", "users", "EVERYONE"])
def test_unknown_subject_type_refused(store: AccessStore, subjects: Subjects, subject_type: str) -> None:
    with pytest.raises(StoreError, match=r"^Row line 1: subject type must be one of"):
        store.set_dataset_security(DRUG_SALES, [show(subject_type, PETR)], [])
    with pytest.raises(StoreError, match=r"^Mask on margin, line 1: subject type must be one of"):
        store.set_dataset_security(DRUG_SALES, [], [mask("margin", subject_type=subject_type, subject_id=PETR)])


def test_bad_kind_treatment_and_column_refused(store: AccessStore, subjects: Subjects) -> None:
    with pytest.raises(StoreError, match=r"^Row line 1: kind must be one of show, hide$"):
        store.set_dataset_security(DRUG_SALES, [RowAccessRuleInput("permissive", SUBJECT_EVERYONE, "", PUBLIC)], [])
    with pytest.raises(StoreError, match=r"^Mask on margin, line 2: treatment must be one of"):
        store.set_dataset_security(DRUG_SALES, [], [mask("margin"), mask("margin", "redact")])
    with pytest.raises(StoreError, match="the column is required"):
        store.set_dataset_security(DRUG_SALES, [], [mask("   ")])
    assert everything(store) == ([], [])


# ---- filters ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"all": False},
        {"all": True, "conditions": []},
        {"conditions": []},
        {"conditions": "visibility = 'public'"},
        {"where": "1=1"},
        {"conditions": [{"column": "visibility", "op": "like", "value": "p%"}]},
        {"conditions": [{"column": "", "op": "is", "value": "x"}]},
        {"conditions": [{"column": "company", "op": "is", "value": "A", "source": "email"}]},
        {"conditions": [{"column": "company", "op": "is"}]},
        {"conditions": [{"column": "company", "op": "one_of", "value": "A"}]},
        {"conditions": [{"column": "company", "op": "one_of", "values": []}]},
        {"conditions": [{"column": "company", "op": "is_empty", "value": "x"}]},
        {"conditions": [{"column": "company", "op": "is", "source": "attribute:Company"}]},
        {"conditions": [{"column": "company", "op": "is", "source": "header:x-user"}]},
        {"conditions": [{"column": "company", "op": "is", "value": {"nested": 1}}]},
        {"conditions": [{"column": "company", "op": "is", "value": "A", "sql": "OR 1=1"}]},
        {"conditions": ["company = 'A'"]},
    ],
)
def test_malformed_filters_are_refused_with_the_parser_message(
    store: AccessStore, subjects: Subjects, raw: dict
) -> None:
    with pytest.raises(FilterError) as parsed:
        parse_filter(raw)
    with pytest.raises(StoreError) as refused:
        store.set_dataset_security(DRUG_SALES, [show(), show(row_filter=raw)], [])
    assert str(refused.value) == f"Row line 2: {parsed.value}"
    assert everything(store) == ([], [])


# ---- limits -----------------------------------------------------------------------------------------------


def test_row_line_limit(store: AccessStore) -> None:
    saved = store.set_dataset_security(DRUG_SALES, [show() for _ in range(ROW_LINES_MAX_PER_DATASET)], [])
    assert len(saved.rows) == ROW_LINES_MAX_PER_DATASET
    with pytest.raises(StoreError, match=f"At most {ROW_LINES_MAX_PER_DATASET} row lines"):
        store.set_dataset_security(DRUG_SALES, [show() for _ in range(ROW_LINES_MAX_PER_DATASET + 1)], [])
    assert store.dataset_security(DRUG_SALES) == saved


def test_mask_line_limit(store: AccessStore) -> None:
    saved = store.set_dataset_security(DRUG_SALES, [], [mask(f"c{i}") for i in range(MASK_LINES_MAX_PER_DATASET)])
    assert len(saved.masks) == MASK_LINES_MAX_PER_DATASET
    with pytest.raises(StoreError, match=f"At most {MASK_LINES_MAX_PER_DATASET} masking lines"):
        store.set_dataset_security(DRUG_SALES, [], [mask(f"c{i}") for i in range(MASK_LINES_MAX_PER_DATASET + 1)])
    assert store.dataset_security(DRUG_SALES) == saved


def test_condition_limit(store: AccessStore) -> None:
    store.set_dataset_security(DRUG_SALES, [show(row_filter=condition_filter(FILTER_CONDITIONS_MAX))], [])
    with pytest.raises(StoreError, match=rf"^Row line 2: at most {FILTER_CONDITIONS_MAX} conditions per line$"):
        store.set_dataset_security(
            DRUG_SALES, [show(), show(row_filter=condition_filter(FILTER_CONDITIONS_MAX + 1))], []
        )
    assert len(store.list_row_access_rules(DRUG_SALES)[0].filter.conditions) == FILTER_CONDITIONS_MAX


def test_values_limit(store: AccessStore) -> None:
    def one_of(count: int) -> dict:
        return {"conditions": [{"column": "store_id", "op": "one_of", "values": [f"S{i}" for i in range(count)]}]}

    store.set_dataset_security(DRUG_SALES, [show(row_filter=one_of(FILTER_VALUES_MAX))], [])
    with pytest.raises(StoreError, match=rf"^Row line 1: at most {FILTER_VALUES_MAX} values in one list$"):
        store.set_dataset_security(DRUG_SALES, [show(row_filter=one_of(FILTER_VALUES_MAX + 1))], [])
    assert len(store.list_row_access_rules(DRUG_SALES)[0].filter.conditions[0].values) == FILTER_VALUES_MAX


def test_value_length_limit(store: AccessStore) -> None:
    longest = "x" * FILTER_VALUE_MAX_CHARS
    too_long = longest + "x"
    store.set_dataset_security(DRUG_SALES, [show(row_filter={"conditions": [
        {"column": "company", "op": "is", "value": longest},
        {"column": "store_id", "op": "one_of", "values": ["a", longest]},
    ]})], [])
    message = rf"^Row line 1: a value is longer than {FILTER_VALUE_MAX_CHARS} characters$"
    with pytest.raises(StoreError, match=message):
        store.set_dataset_security(
            DRUG_SALES, [show(row_filter={"conditions": [{"column": "company", "op": "is", "value": too_long}]})], []
        )
    with pytest.raises(StoreError, match=message):
        store.set_dataset_security(DRUG_SALES, [show(row_filter={"conditions": [
            {"column": "store_id", "op": "one_of", "values": ["a", too_long]},
        ]})], [])
    assert store.list_row_access_rules(DRUG_SALES)[0].filter.conditions[0].value == longest


# ---- ids ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["ABCDEF012345", "0123456789a", "0123456789abc", "g123456789ab", "x' OR '1'='1"])
def test_invalid_ids_refused(store: AccessStore, bad_id: str) -> None:
    with pytest.raises(StoreError, match=r"^Row line 1: invalid line id"):
        store.set_dataset_security(DRUG_SALES, [show(rule_id=bad_id)], [])
    with pytest.raises(StoreError, match=r"^Mask on margin, line 1: invalid line id"):
        store.set_dataset_security(DRUG_SALES, [], [mask("margin", mask_id=bad_id)])
    assert everything(store) == ([], [])


def test_duplicate_ids_refused(store: AccessStore) -> None:
    with pytest.raises(StoreError, match=rf"^Row line 2: line id '{RULE_ID_A}' is used twice$"):
        store.set_dataset_security(DRUG_SALES, [show(rule_id=RULE_ID_A), hide(rule_id=RULE_ID_A)], [])
    with pytest.raises(StoreError, match=rf"^Mask on revenue, line 1: line id '{MASK_ID_A}' is used twice$"):
        store.set_dataset_security(DRUG_SALES, [], [mask("margin", mask_id=MASK_ID_A), mask("revenue", mask_id=MASK_ID_A)])
    assert everything(store) == ([], [])


def test_ids_of_another_dataset_refused(store: AccessStore) -> None:
    stores = store.set_dataset_security(STORES, [show(rule_id=RULE_ID_A)], [mask("manager_email", mask_id=MASK_ID_A)])
    with pytest.raises(StoreError, match=rf"^Row line 1: line id '{RULE_ID_A}' belongs to another dataset$"):
        store.set_dataset_security(DRUG_SALES, [show(rule_id=RULE_ID_A)], [])
    with pytest.raises(StoreError, match=rf"^Mask on margin, line 1: line id '{MASK_ID_A}' belongs to another dataset$"):
        store.set_dataset_security(DRUG_SALES, [], [mask("margin", mask_id=MASK_ID_A)])
    assert store.dataset_security(STORES) == stores
    assert store.dataset_security(DRUG_SALES) == EMPTY_SECURITY


# ---- nothing written on error -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "masks"),
    [
        ([show(), show(SUBJECT_USER, "ghost@x.example")], [mask("margin")]),
        ([show(), show()], [mask("margin"), mask("customer_id", "redact")]),
        ([show(), show()], [mask("margin"), mask("customer_id", CLEAR, SUBJECT_LOCAL_GROUP, "nope")]),
        ([show(), show(row_filter={"conditions": []})], []),
        ([show(rule_id=RULE_ID_C)], [mask("margin", mask_id=MASK_ID_B), mask("margin", mask_id=MASK_ID_B)]),
        ([show(row_filter=condition_filter(FILTER_CONDITIONS_MAX + 1))], [mask("margin")]),
    ],
)
def test_nothing_is_written_on_any_error(
    store: AccessStore, subjects: Subjects, rows: list[RowAccessRuleInput], masks: list[ColumnMaskInput]
) -> None:
    store.set_dataset_security(DRUG_SALES, [show(rule_id=RULE_ID_A), hide()], [mask("margin", CLEAR, mask_id=MASK_ID_A)])
    store.set_dataset_security(STORES, [show(rule_id=RULE_ID_B)], [])
    before = {t: store.dump_rows(t) for t in SECURITY_TABLES}
    with pytest.raises(StoreError):
        store.set_dataset_security(DRUG_SALES, rows, masks)
    assert {t: store.dump_rows(t) for t in SECURITY_TABLES} == before


# ---- cascades ------------------------------------------------------------------------------------------------


def _lines_for_every_subject(store: AccessStore | KeboolaAccessStore, subjects: Subjects) -> None:
    store.set_dataset_security(
        DRUG_SALES,
        [show(), show(SUBJECT_USER, PETR), show(SUBJECT_USER, EVA), show(SUBJECT_LOCAL_GROUP, TEAM),
         show(SUBJECT_LOCAL_GROUP, OTHER_TEAM), show(SUBJECT_GROUP, subjects.finance), hide(SUBJECT_GROUP, subjects.supply)],
        [mask("margin", CLEAR, SUBJECT_USER, PETR), mask("margin", CLEAR, SUBJECT_LOCAL_GROUP, TEAM),
         mask("margin", CLEAR, SUBJECT_GROUP, subjects.finance), mask("margin", HIDDEN)],
    )
    store.set_dataset_security(
        STORES, [show(SUBJECT_USER, PETR, ALL), show(SUBJECT_GROUP, subjects.supply)],
        [mask("manager_email", MASK_PARTIAL, SUBJECT_USER, PETR), mask("manager_email", HIDDEN, SUBJECT_GROUP, subjects.finance)],
    )


def _subjects_named(store: AccessStore | KeboolaAccessStore) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    rules, masks = everything(store)
    return {(r.subject_type, r.subject_id) for r in rules}, {(m.subject_type, m.subject_id) for m in masks}


def test_deleting_a_user_deletes_lines_naming_them(store: AccessStore, subjects: Subjects) -> None:
    _lines_for_every_subject(store, subjects)
    rules_before, masks_before = everything(store)
    store.delete_user(" Petr.Sales@Pharma-A.example ")
    rule_subjects, mask_subjects = _subjects_named(store)
    assert (SUBJECT_USER, PETR) not in rule_subjects | mask_subjects
    assert (SUBJECT_USER, EVA) in rule_subjects
    rules_after, masks_after = everything(store)
    assert len(rules_after) == len(rules_before) - 2
    assert len(masks_after) == len(masks_before) - 2


def test_deleting_a_local_group_deletes_lines_naming_it(store: AccessStore, subjects: Subjects) -> None:
    _lines_for_every_subject(store, subjects)
    store.delete_local_group(TEAM)
    rule_subjects, mask_subjects = _subjects_named(store)
    assert (SUBJECT_LOCAL_GROUP, TEAM) not in rule_subjects | mask_subjects
    assert (SUBJECT_LOCAL_GROUP, OTHER_TEAM) in rule_subjects
    assert (SUBJECT_USER, PETR) in rule_subjects  # the member's own lines stay


def test_deleting_an_idp_group_deletes_lines_naming_it(store: AccessStore, subjects: Subjects) -> None:
    _lines_for_every_subject(store, subjects)
    store.delete_group(subjects.finance)
    rule_subjects, mask_subjects = _subjects_named(store)
    assert (SUBJECT_GROUP, subjects.finance) not in rule_subjects | mask_subjects
    assert (SUBJECT_GROUP, subjects.supply) in rule_subjects
    store.delete_group(subjects.supply)
    rule_subjects, _ = _subjects_named(store)
    assert not [s for s in rule_subjects if s[0] == SUBJECT_GROUP]
    # The hide line of the supply group went with it.
    assert [r.kind for r in store.list_row_access_rules(DRUG_SALES)].count(KIND_HIDE) == 0


def test_renaming_a_user_moves_their_lines(store: AccessStore, subjects: Subjects) -> None:
    _lines_for_every_subject(store, subjects)
    before = everything(store)
    renamed = store.update_user(PETR, new_email="Petr.New@Pharma-A.example", display_name="Petr", active=True)
    rule_subjects, mask_subjects = _subjects_named(store)
    assert (SUBJECT_USER, PETR) not in rule_subjects | mask_subjects
    assert (SUBJECT_USER, renamed) in rule_subjects and (SUBJECT_USER, renamed) in mask_subjects
    after = everything(store)
    # Same lines (ids, positions, filters), only the subject id changed.
    assert [(r.rule_id, r.position, r.filter) for r in after[0]] == [(r.rule_id, r.position, r.filter) for r in before[0]]
    assert [m.mask_id for m in after[1]] == [m.mask_id for m in before[1]]


def test_editing_a_user_without_renaming_keeps_their_lines(store: AccessStore, subjects: Subjects) -> None:
    _lines_for_every_subject(store, subjects)
    before = everything(store)
    store.update_user(PETR, new_email=PETR.upper(), display_name="Petr S", active=False)
    assert everything(store) == before


# ---- snapshot ------------------------------------------------------------------------------------------------


def test_snapshot_security_by_dataset(store: AccessStore, subjects: Subjects) -> None:
    drug = store.set_dataset_security(DRUG_SALES, [show(), hide(), show(SUBJECT_GROUP, subjects.finance, ALL)],
                                      [mask("margin", CLEAR, SUBJECT_GROUP, subjects.finance), mask("margin")])
    only_masks = store.set_dataset_security(STORES, [], [mask("manager_email", MASK_PARTIAL)])
    only_rows = store.set_dataset_security(SUPPLY_CHAIN, [show(SUBJECT_USER, PETR, OWN_COMPANY)], [])
    store.set_dataset_security("emptied", [show()], [])
    store.set_dataset_security("emptied", [], [])
    snapshot = store.snapshot()
    assert snapshot.security == {DRUG_SALES: drug, STORES: only_masks, SUPPLY_CHAIN: only_rows}
    assert isinstance(snapshot.security[DRUG_SALES], DatasetSecurity)
    assert [r.kind for r in snapshot.security[DRUG_SALES].rows] == [KIND_SHOW, KIND_SHOW, KIND_HIDE]


def test_snapshot_of_an_empty_store_has_no_security(store: AccessStore) -> None:
    assert store.snapshot().security == {}


def test_audience_of(store: AccessStore, subjects: Subjects) -> None:
    store.add_local_group_member(OTHER_TEAM, EVA)
    snapshot = store.snapshot()
    principal = Principal(
        "  Petr.Sales@Pharma-A.Example ",
        frozenset({FINANCE, UNKNOWN_EXTERNAL}),
        local_groups=frozenset({"carried"}),
    )
    audience = snapshot.audience_of(principal)
    assert audience.email == PETR
    # Snapshot membership (team) united with what the principal already carries.
    assert audience.local_groups == frozenset({TEAM, "carried"})
    # External ids map to internal group ids; unregistered external ids map to nothing.
    assert audience.group_ids == frozenset({subjects.finance})
    assert audience.matches(SUBJECT_USER, PETR)
    assert audience.matches(SUBJECT_GROUP, subjects.finance)
    assert not audience.matches(SUBJECT_GROUP, FINANCE)
    assert not audience.matches(SUBJECT_LOCAL_GROUP, OTHER_TEAM)


def test_audience_of_a_stranger(store: AccessStore, subjects: Subjects) -> None:
    audience = store.snapshot().audience_of(Principal("stranger@nowhere.example", frozenset()))
    assert (audience.email, audience.local_groups, audience.group_ids) == (
        "stranger@nowhere.example", frozenset(), frozenset(),
    )
    assert audience.matches(SUBJECT_EVERYONE, "")


def test_security_for(store: AccessStore, subjects: Subjects) -> None:
    drug = store.set_dataset_security(DRUG_SALES, [show()], [mask("margin")])
    snapshot = store.snapshot()
    principal = Principal(EVA, frozenset({SUPPLY}))
    security = snapshot.security_for(principal)
    assert security.audience == snapshot.audience_of(principal)
    assert security.audience.group_ids == frozenset({subjects.supply})
    assert dict(security.datasets) == {DRUG_SALES: drug}


# ---- unreadable stored rows -----------------------------------------------------------------------------------


def _raw_rule(rule_id: str, *, filter_text: str, position: str = "1", kind: str = KIND_SHOW) -> dict[str, str]:
    return {"rule_id": rule_id, "dataset": DRUG_SALES, "position": position, "kind": kind,
            "subject_type": SUBJECT_EVERYONE, "subject_id": "", "filter": filter_text}


@pytest.mark.parametrize(
    "filter_text",
    ["not json", "", "[1, 2]", "null", '{"where": "1=1"}', '{"conditions": []}', '{"all": false}'],
)
def test_unreadable_stored_filter_reads_as_none(store: AccessStore, filter_text: str) -> None:
    store.load_rows("row_access_rules", [
        _raw_rule(RULE_ID_A, filter_text=filter_text, kind=KIND_HIDE),
        _raw_rule(RULE_ID_B, filter_text=json.dumps(PUBLIC)),
    ])
    rules = {r.rule_id: r for r in store.list_row_access_rules(DRUG_SALES)}
    assert rules[RULE_ID_A].filter is None
    assert rules[RULE_ID_B].filter == parse_filter(PUBLIC)
    assert {r.rule_id: r.filter for r in store.snapshot().security[DRUG_SALES].rows}[RULE_ID_A] is None


def test_unreadable_stored_position_reads_as_zero(store: AccessStore) -> None:
    store.load_rows("row_access_rules", [
        _raw_rule(RULE_ID_A, filter_text=json.dumps(PUBLIC), position="2"),
        _raw_rule(RULE_ID_B, filter_text=json.dumps(PUBLIC), position="first"),
    ])
    store.load_rows("column_masks", [
        {"mask_id": MASK_ID_A, "dataset": DRUG_SALES, "column_name": "margin", "position": "",
         "subject_type": SUBJECT_EVERYONE, "subject_id": "", "treatment": HIDDEN},
    ])
    rules = store.list_row_access_rules(DRUG_SALES)
    assert [(r.rule_id, r.position) for r in rules] == [(RULE_ID_B, 0), (RULE_ID_A, 2)]
    assert [(m.mask_id, m.position) for m in store.list_column_masks(DRUG_SALES)] == [(MASK_ID_A, 0)]


# ---- raw table round trip ---------------------------------------------------------------------------------------


def test_dump_and_load_rows_round_trip(store: AccessStore, subjects: Subjects, settings: Settings, tmp_path: Path) -> None:
    _lines_for_every_subject(store, subjects)
    copy = AccessStore(tmp_path / "copy.sqlite", settings.providers)
    for table in TABLE_COLUMNS:
        dumped = store.dump_rows(table)
        assert all(list(row) == TABLE_COLUMNS[table] for row in dumped)
        assert all(isinstance(v, str) for row in dumped for v in row.values())
        copy.load_rows(table, dumped)
    assert everything(copy) == everything(store)
    assert copy.snapshot().security == store.snapshot().security
    for table in SECURITY_TABLES:
        assert sorted(copy.dump_rows(table), key=str) == sorted(store.dump_rows(table), key=str)


# ---- KeboolaAccessStore -------------------------------------------------------------------------------------------


@pytest.fixture
def api() -> FakeStorageApi:
    fake = FakeStorageApi()
    fake.add_bucket(BUCKET)
    return fake


@pytest.fixture
def kstore(api: FakeStorageApi, settings: Settings) -> KeboolaAccessStore:
    created = KeboolaAccessStore(api.client(), BUCKET, settings.providers, read_cache_seconds=0)
    created.ensure_schema()
    return created


@pytest.fixture
def ksubjects(kstore: KeboolaAccessStore) -> Subjects:
    return register_subjects(kstore)


def storage_rows(api: FakeStorageApi, table: str) -> list[dict[str, str]]:
    return api.rows(f"{BUCKET}.{table}")


def reset_counters(api: FakeStorageApi) -> None:
    api.imports.clear()
    api.deletes.clear()


def test_storage_tables_have_single_keys_and_no_row_key(api: FakeStorageApi, kstore: KeboolaAccessStore) -> None:
    assert api.tables[f"{BUCKET}.row_access_rules"].columns == TABLE_COLUMNS["row_access_rules"]
    assert api.tables[f"{BUCKET}.row_access_rules"].primary_key == ["rule_id"]
    assert api.tables[f"{BUCKET}.column_masks"].columns == TABLE_COLUMNS["column_masks"]
    assert api.tables[f"{BUCKET}.column_masks"].primary_key == ["mask_id"]
    for table in SECURITY_TABLES:
        assert "row_key" not in storage_columns(table)
        assert storage_primary_key(table) == TABLE_KEYS[table][0]
    assert not [t for t in api.tables if t.split(".")[-1].startswith("data_polic")]


def test_keboola_set_reaches_storage(api: FakeStorageApi, kstore: KeboolaAccessStore, ksubjects: Subjects) -> None:
    saved = kstore.set_dataset_security(
        " Drug_Sales ", [show(rule_id=RULE_ID_A), show(SUBJECT_GROUP, ksubjects.finance, ALL), hide()],
        [mask("margin", CLEAR, SUBJECT_GROUP, ksubjects.finance, mask_id=MASK_ID_A), mask("margin")],
    )
    assert kstore.dataset_security(DRUG_SALES) == saved
    assert kstore.snapshot().security == {DRUG_SALES: saved}
    stored = {r["rule_id"]: r for r in storage_rows(api, "row_access_rules")}
    assert stored.keys() == {r.rule_id for r in saved.rows}
    assert stored[RULE_ID_A] == {
        "rule_id": RULE_ID_A, "dataset": DRUG_SALES, "position": "1", "kind": KIND_SHOW,
        "subject_type": SUBJECT_EVERYONE, "subject_id": "", "filter": json.dumps(PUBLIC, sort_keys=True),
    }
    masks = {r["mask_id"]: r for r in storage_rows(api, "column_masks")}
    assert masks[MASK_ID_A] == {
        "mask_id": MASK_ID_A, "dataset": DRUG_SALES, "column_name": "margin", "position": "1",
        "subject_type": SUBJECT_GROUP, "subject_id": ksubjects.finance, "treatment": CLEAR,
    }


def test_keboola_set_pushes_only_changed_rows_and_deletes_removed_ones(
    api: FakeStorageApi, kstore: KeboolaAccessStore, ksubjects: Subjects
) -> None:
    kstore.set_dataset_security(
        DRUG_SALES,
        [show(rule_id=RULE_ID_A), show(row_filter=OWN_COMPANY, rule_id=RULE_ID_B), hide(rule_id=RULE_ID_C)],
        [mask("margin", mask_id=MASK_ID_A), mask("customer_id", MASK_HASH, mask_id=MASK_ID_B)],
    )
    kstore.set_dataset_security(STORES, [show()], [mask("manager_email", MASK_PARTIAL)])
    stores_rows = [r for r in storage_rows(api, "row_access_rules") if r["dataset"] == STORES]
    reset_counters(api)

    # Line A unchanged, line B gets a new filter, line C goes, a new line comes; masks: A kept, B goes.
    kstore.set_dataset_security(
        DRUG_SALES,
        [show(rule_id=RULE_ID_A), show(row_filter=ALL, rule_id=RULE_ID_B), show(SUBJECT_USER, PETR)],
        [mask("margin", mask_id=MASK_ID_A)],
    )
    assert sorted(api.imports) == [(f"{BUCKET}.row_access_rules", 2)]
    assert sorted(api.deletes) == [
        (f"{BUCKET}.column_masks", [MASK_ID_B]),
        (f"{BUCKET}.row_access_rules", [RULE_ID_C]),
    ]
    assert [r for r in storage_rows(api, "row_access_rules") if r["dataset"] == STORES] == stores_rows
    stored = {r["rule_id"]: r for r in storage_rows(api, "row_access_rules") if r["dataset"] == DRUG_SALES}
    assert RULE_ID_C not in stored and json.loads(stored[RULE_ID_B]["filter"]) == ALL

    reset_counters(api)
    kstore.set_dataset_security(  # the same content again: nothing to send
        DRUG_SALES,
        [show(rule_id=RULE_ID_A), show(row_filter=ALL, rule_id=RULE_ID_B),
         show(SUBJECT_USER, PETR, rule_id=next(k for k in stored if k not in (RULE_ID_A, RULE_ID_B)))],
        [mask("margin", mask_id=MASK_ID_A)],
    )
    assert api.imports == [] and api.deletes == []


@pytest.mark.parametrize(
    ("rows", "masks"),
    [
        ([show(SUBJECT_USER, "ghost@x.example")], []),
        ([show(row_filter={"conditions": [{"column": "x", "op": "drop table", "value": "1"}]})], []),
        ([show()], [mask("margin", "redact")]),
        ([show(rule_id="NOT-AN-ID")], []),
        ([show() for _ in range(ROW_LINES_MAX_PER_DATASET + 1)], []),
    ],
)
def test_keboola_refusals_send_nothing(
    api: FakeStorageApi, kstore: KeboolaAccessStore, ksubjects: Subjects,
    rows: list[RowAccessRuleInput], masks: list[ColumnMaskInput],
) -> None:
    kstore.set_dataset_security(DRUG_SALES, [show(rule_id=RULE_ID_A)], [mask("margin", mask_id=MASK_ID_A)])
    before = {t: storage_rows(api, t) for t in SECURITY_TABLES}
    reset_counters(api)
    with pytest.raises(StoreError):
        kstore.set_dataset_security(DRUG_SALES, rows, masks)
    assert api.imports == [] and api.deletes == []
    assert {t: storage_rows(api, t) for t in SECURITY_TABLES} == before


def test_keboola_id_of_another_dataset_refused(api: FakeStorageApi, kstore: KeboolaAccessStore) -> None:
    kstore.set_dataset_security(STORES, [show(rule_id=RULE_ID_A)], [])
    reset_counters(api)
    with pytest.raises(StoreError, match="belongs to another dataset"):
        kstore.set_dataset_security(DRUG_SALES, [show(rule_id=RULE_ID_A)], [])
    assert api.imports == [] and api.deletes == []
    assert [r["dataset"] for r in storage_rows(api, "row_access_rules")] == [STORES]


def test_keboola_cascades_reach_storage(api: FakeStorageApi, kstore: KeboolaAccessStore, ksubjects: Subjects) -> None:
    _lines_for_every_subject(kstore, ksubjects)

    def named(table: str) -> set[tuple[str, str]]:
        return {(r["subject_type"], r["subject_id"]) for r in storage_rows(api, table)}

    renamed = kstore.update_user(PETR, new_email="petr.new@pharma-a.example", display_name="Petr", active=True)
    for table in SECURITY_TABLES:
        assert (SUBJECT_USER, PETR) not in named(table)
        assert (SUBJECT_USER, renamed) in named(table)

    kstore.update_user(renamed, new_email=renamed, display_name="Petr N", active=True)
    assert (SUBJECT_USER, renamed) in named("row_access_rules")

    kstore.delete_user(renamed)
    kstore.delete_local_group(TEAM)
    kstore.delete_group(ksubjects.finance)
    gone = {(SUBJECT_USER, renamed), (SUBJECT_LOCAL_GROUP, TEAM), (SUBJECT_GROUP, ksubjects.finance)}
    for table in SECURITY_TABLES:
        assert not named(table) & gone
    assert named("row_access_rules") == {
        (SUBJECT_EVERYONE, ""), (SUBJECT_USER, EVA), (SUBJECT_LOCAL_GROUP, OTHER_TEAM), (SUBJECT_GROUP, ksubjects.supply),
    }
    assert named("column_masks") == {(SUBJECT_EVERYONE, "")}
    # Storage and the store agree.
    assert {(r.subject_type, r.subject_id) for r in kstore.list_row_access_rules()} == named("row_access_rules")


HOSTILE_VALUES = [
    "x' OR '1'='1",
    'say "hello", world',
    "line one\nline two\r\nline three",
    "back\\slash; DROP TABLE users; --",
    "ž, \"quoted\", 'single', |pipe|",
]


def test_keboola_hostile_filter_values_round_trip(api: FakeStorageApi, kstore: KeboolaAccessStore) -> None:
    row_filter = {"conditions": [
        {"column": "company", "op": "is", "value": HOSTILE_VALUES[0]},
        {"column": "store_id", "op": "one_of", "values": HOSTILE_VALUES},
    ]}
    saved = kstore.set_dataset_security(DRUG_SALES, [show(row_filter=row_filter)], [])
    expected = parse_filter(row_filter)
    assert saved.rows[0].filter == expected
    assert kstore.list_row_access_rules(DRUG_SALES)[0].filter == expected
    assert kstore.snapshot().security[DRUG_SALES].rows[0].filter == expected
    stored = storage_rows(api, "row_access_rules")
    assert len(stored) == 1 and parse_filter(json.loads(stored[0]["filter"])) == expected


def test_bucket_without_security_tables_reads_empty_and_creates_them_on_first_write(
    api: FakeStorageApi, settings: Settings
) -> None:
    created = KeboolaAccessStore(api.client(), BUCKET, settings.providers, read_cache_seconds=0)
    created.ensure_schema()
    for table in SECURITY_TABLES:
        del api.tables[f"{BUCKET}.{table}"]  # a bucket created before dataset security existed
    assert created.list_row_access_rules() == [] and created.list_column_masks() == []
    assert created.dataset_security(DRUG_SALES) == EMPTY_SECURITY
    assert created.snapshot().security == {}
    for table in SECURITY_TABLES:
        assert f"{BUCKET}.{table}" not in api.tables  # reads never create tables
    saved = created.set_dataset_security(DRUG_SALES, [show()], [mask("margin")])
    for table in SECURITY_TABLES:
        assert api.tables[f"{BUCKET}.{table}"].columns == TABLE_COLUMNS[table]
        assert api.tables[f"{BUCKET}.{table}"].primary_key == TABLE_KEYS[table]
    assert [r["rule_id"] for r in storage_rows(api, "row_access_rules")] == [saved.rows[0].rule_id]
    assert [r["mask_id"] for r in storage_rows(api, "column_masks")] == [saved.masks[0].mask_id]
    assert created.missing_tables() == []


# ---- demo seed ------------------------------------------------------------------------------------------------------


def _counts(store: AccessStore | KeboolaAccessStore, dataset: str) -> dict[str, object]:
    security = store.dataset_security(dataset)
    columns: dict[str, int] = {}
    for line in security.masks:
        columns[line.column_name] = columns.get(line.column_name, 0) + 1
    return {
        "show": sum(r.kind == KIND_SHOW for r in security.rows),
        "hide": sum(r.kind == KIND_HIDE for r in security.rows),
        "masks": columns,
    }


@pytest.fixture
def seeded_store(seeded: Settings) -> AccessStore:
    return AccessStore(seeded.path(seeded.store.sqlite_path), seeded.providers)


def test_seed_loads_the_pharma_examples(seeded_store: AccessStore) -> None:
    for dataset, expected in EXAMPLE_COUNTS.items():
        assert _counts(seeded_store, dataset) == expected, dataset
    assert set(seeded_store.snapshot().security) == set(EXAMPLE_COUNTS)


def test_seed_resolves_idp_groups_to_internal_ids(seeded_store: AccessStore) -> None:
    internal = {g.external_id: g.group_id for g in seeded_store.list_groups()}
    rules, masks = everything(seeded_store)
    group_ids = {line.subject_id for line in (*rules, *masks) if line.subject_type == SUBJECT_GROUP}
    assert group_ids == {internal[FINANCE], internal[SUPPLY], internal[PARTNER]}
    assert not group_ids & {FINANCE, SUPPLY, PARTNER}
    finance_all = [r for r in seeded_store.list_row_access_rules(DRUG_SALES) if r.subject_type == SUBJECT_GROUP]
    assert [(r.kind, r.subject_id, r.filter) for r in finance_all] == [(KIND_SHOW, internal[FINANCE], ALL_ROWS)]
    margin = [m for m in seeded_store.list_column_masks(DRUG_SALES) if m.column_name == "margin"]
    assert [(m.subject_type, m.subject_id, m.treatment) for m in margin] == [
        (SUBJECT_GROUP, internal[FINANCE], CLEAR), (SUBJECT_EVERYONE, "", HIDDEN),
    ]


def test_seed_loads_the_user_subject(seeded_store: AccessStore) -> None:
    anna_rows = [r for r in seeded_store.list_row_access_rules(SUPPLY_CHAIN) if r.subject_type == SUBJECT_USER]
    assert [(r.subject_id, r.kind, r.position, r.filter) for r in anna_rows] == [
        (ANNA, KIND_SHOW, 4, parse_filter(OWN_COMPANY)),
    ]
    unit_cost = [m for m in seeded_store.list_column_masks(SUPPLY_CHAIN) if m.column_name == "unit_cost"]
    assert [(m.subject_type, m.position, m.treatment) for m in unit_cost] == [
        (SUBJECT_GROUP, 1, HIDDEN), (SUBJECT_USER, 2, MASK_PARTIAL),
    ]
    assert unit_cost[1].subject_id == ANNA


def test_seed_rerun_does_not_duplicate_or_overwrite_edited_lines(seeded: Settings, seeded_store: AccessStore) -> None:
    edited = seeded_store.set_dataset_security(DRUG_SALES, [show(SUBJECT_USER, PETR, ALL)], [mask("revenue", MASK_HASH)])
    before = {t: sorted(seeded_store.dump_rows(t), key=str) for t in SECURITY_TABLES}
    fill_store(seeded, seeded_store)
    assert seeded_store.dataset_security(DRUG_SALES) == edited
    for dataset in (STORES, SUPPLY_CHAIN):
        assert _counts(seeded_store, dataset) == EXAMPLE_COUNTS[dataset]
    assert {t: sorted(seeded_store.dump_rows(t), key=str) for t in SECURITY_TABLES} == before


def test_seed_reloads_examples_only_for_an_emptied_dataset(seeded: Settings, seeded_store: AccessStore) -> None:
    seeded_store.set_dataset_security(STORES, [], [])
    load_example_security(seeded, seeded_store)
    assert _counts(seeded_store, STORES) == EXAMPLE_COUNTS[STORES]


def test_examples_naming_unregistered_subjects_are_skipped(
    settings: Settings, store: AccessStore, capsys: pytest.CaptureFixture[str]
) -> None:
    # Only the finance IdP group is registered: no supply/partner groups and no user anna.
    finance = store.create_group("entra", FINANCE, "finance").group_id
    load_example_security(settings, store)
    assert _counts(store, DRUG_SALES) == EXAMPLE_COUNTS[DRUG_SALES]
    supply_rows = store.list_row_access_rules(SUPPLY_CHAIN)
    assert [(r.subject_type, r.subject_id, r.position) for r in supply_rows] == [(SUBJECT_GROUP, finance, 1)]
    supply_masks = store.list_column_masks(SUPPLY_CHAIN)
    assert [(m.column_name, m.subject_type, m.position) for m in supply_masks] == [
        ("contract_ref", SUBJECT_GROUP, 1), ("contract_ref", SUBJECT_EVERYONE, 2),
    ]
    err = capsys.readouterr().err
    assert f"IdP group '{SUPPLY}' is not registered" in err
    assert f"IdP group '{PARTNER}' is not registered" in err
    assert f"user '{ANNA}' is not registered" in err


def test_keboola_seed_loads_the_examples_into_storage(api: FakeStorageApi, kstore: KeboolaAccessStore, settings: Settings) -> None:
    fill_store(settings, kstore)
    for dataset, expected in EXAMPLE_COUNTS.items():
        assert _counts(kstore, dataset) == expected, dataset
    expected_rows = sum(c["show"] + c["hide"] for c in EXAMPLE_COUNTS.values())
    expected_masks = sum(sum(c["masks"].values()) for c in EXAMPLE_COUNTS.values())
    assert len(storage_rows(api, "row_access_rules")) == expected_rows
    assert len(storage_rows(api, "column_masks")) == expected_masks
    reset_counters(api)
    fill_store(settings, kstore)
    assert not [t for t, _ in api.imports if t.split(".")[-1] in SECURITY_TABLES]
    assert api.deletes == []
