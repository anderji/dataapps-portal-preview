"""Layer 3a — the policy files: the dataset catalogue and example security lines.

Since policy file version 2 the YAML no longer carries the security itself. It
declares, per dataset, the physical `source`, a description, the `columns` (and
the logical `types` of the non-text ones) and optional *example* row lines and
column masks. The security that is enforced lives in the control-plane tables
`row_access_rules` and `column_masks` (see kbc_access.store); the examples are
loaded into them once on request (the admin's "Load example rules", or the local
demo seed).

`load_policy` checks the catalogue against the warehouse: the declared columns
must be exactly the warehouse's (Storage system columns aside) and each declared
type must match the warehouse type, so the admin, which may run without the
warehouse, types literals the same way the dashboards do.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kbc_access.security_model import (
    BOOLEAN_FALSE,
    BOOLEAN_TRUE,
    COLUMN_KINDS,
    KIND_BOOLEAN,
    KIND_DATE,
    KIND_NUMERIC,
    KIND_TEXT,
    LIST_OPERATORS,
    ORDER_OPERATORS,
    SUBJECT_EVERYONE,
    SUBJECT_GROUP,
    SUBJECT_LOCAL_GROUP,
    SUBJECT_USER,
    TREATMENTS,
    Condition,
    FilterError,
    RowFilter,
    parse_filter,
)
from kbc_access.store import ColumnMaskInput, RowAccessRuleInput

logger = logging.getLogger(__name__)

POLICY_FILE_VERSION = 2
# Columns Storage adds to every table it unloads; they are not data the policy author declares.
STORAGE_SYSTEM_COLUMNS = frozenset({"_timestamp"})
# A number literal the compiler may put into SQL verbatim: digits, optional fraction and exponent.
NUMBER_LITERAL = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
# Warehouse type names (upper-case prefixes, as DuckDB, Snowflake and BigQuery report them) per kind.
_NUMERIC_TYPES = (
    "TINYINT", "SMALLINT", "INTEGER", "INT", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
    "UBIGINT", "UHUGEINT", "DECIMAL", "NUMERIC", "NUMBER", "DOUBLE", "FLOAT", "REAL", "FLOAT64", "INT64",
    "BIGNUMERIC",
)
_BOOLEAN_TYPES = ("BOOLEAN", "BOOL")
_DATE_TYPES = ("DATE", "TIMESTAMP", "DATETIME", "TIME")
TIMESTAMP_TYPE_PREFIX = "TIMESTAMP_"
# Example `who` keys and the subject type they become.
EXAMPLE_WHO_KEYS = {"user": SUBJECT_USER, "local_group": SUBJECT_LOCAL_GROUP, "group_external_id": SUBJECT_GROUP}


class PolicyError(ValueError):
    """The policy document, or a security line checked against it, is invalid."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys (a second block must not silently win)."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key '{key}'", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_who(value: str | dict[str, str]) -> str | dict[str, str]:
    if value == SUBJECT_EVERYONE:
        return value
    if isinstance(value, dict) and len(value) == 1 and next(iter(value)) in EXAMPLE_WHO_KEYS:
        if not str(next(iter(value.values()))).strip():
            raise ValueError("who needs a non-empty id")
        return value
    raise ValueError(f"who must be 'everyone' or one of {{{', '.join(EXAMPLE_WHO_KEYS)}: <id>}}")


class ExampleRow(_Strict):
    kind: str = Field(pattern="^(show|hide)$")
    who: str | dict[str, str]
    filter: dict[str, object]

    @field_validator("who")
    @classmethod
    def _who_valid(cls, value: str | dict[str, str]) -> str | dict[str, str]:
        return _check_who(value)

    @field_validator("filter")
    @classmethod
    def _filter_shape(cls, value: dict[str, object]) -> dict[str, object]:
        parse_filter(value)  # FilterError is a ValueError: pydantic reports it
        return value


class ExampleMask(_Strict):
    who: str | dict[str, str]
    treatment: str

    @field_validator("who")
    @classmethod
    def _who_valid(cls, value: str | dict[str, str]) -> str | dict[str, str]:
        return _check_who(value)

    @field_validator("treatment")
    @classmethod
    def _known_treatment(cls, value: str) -> str:
        if value not in TREATMENTS:
            raise ValueError(f"treatment must be one of {', '.join(TREATMENTS)}")
        return value


class Examples(_Strict):
    rows: list[ExampleRow] = Field(default_factory=list)
    masks: dict[str, list[ExampleMask]] = Field(default_factory=dict)


class DatasetSpec(_Strict):
    source: str
    description: str
    columns: list[str]
    # Logical type of each non-text column; an undeclared column is text.
    types: dict[str, str] = Field(default_factory=dict)
    examples: Examples = Field(default_factory=Examples)

    @field_validator("columns")
    @classmethod
    def _columns_valid(cls, value: list[str]) -> list[str]:
        lowered = [c.strip().lower() for c in value]
        if not lowered or any(not c for c in lowered):
            raise ValueError("columns must be a non-empty list of column names")
        if len(set(lowered)) != len(lowered):
            raise ValueError("columns lists a column twice")
        return lowered

    @model_validator(mode="after")
    def _types_valid(self) -> DatasetSpec:
        for column, kind in self.types.items():
            if column.lower() not in self.columns:
                raise ValueError(f"types names unknown column '{column}'")
            if kind not in COLUMN_KINDS:
                raise ValueError(f"type of '{column}' must be one of {', '.join(COLUMN_KINDS)}")
        return self


class PolicyDocument(_Strict):
    version: int = Field(ge=POLICY_FILE_VERSION, le=POLICY_FILE_VERSION)
    datasets: dict[str, DatasetSpec]


@dataclass(frozen=True)
class Dataset:
    name: str
    source: str
    description: str
    # Declared columns, in catalogue order (what a secured relation projects).
    columns: tuple[str, ...]
    # Logical kind per column (numeric, boolean, date, text).
    column_kinds: Mapping[str, str]
    examples: Examples = field(default_factory=Examples)

    @property
    def has_examples(self) -> bool:
        return bool(self.examples.rows or self.examples.masks)


@dataclass(frozen=True)
class PolicySet:
    datasets: dict[str, Dataset]
    # sha256 over the policy files' bytes; written into every query audit line.
    fingerprint: str


def column_kind(sql_type: str) -> str:
    """The logical kind of a warehouse column type (DECIMAL(10,2) -> numeric, TIMESTAMP WITH TIME ZONE -> date)."""
    base = sql_type.strip().upper().split("(")[0].split(" ")[0]
    if base in _NUMERIC_TYPES:
        return KIND_NUMERIC
    if base in _BOOLEAN_TYPES:
        return KIND_BOOLEAN
    # TIMESTAMP_NTZ / _LTZ / _TZ (Snowflake), TIMESTAMP_NS / _MS / _S (DuckDB) are timestamps too.
    if base in _DATE_TYPES or base.startswith(TIMESTAMP_TYPE_PREFIX):
        return KIND_DATE
    return KIND_TEXT


def _read(path: Path) -> tuple[bytes, PolicyDocument]:
    raw_bytes = path.read_bytes()
    try:
        return raw_bytes, PolicyDocument.model_validate(yaml.load(raw_bytes, Loader=_UniqueKeyLoader))
    except Exception as exc:  # pydantic.ValidationError or yaml.YAMLError
        raise PolicyError(f"{path}: {exc}") from exc


def _dataset_from_spec(key: str, spec: DatasetSpec) -> Dataset:
    kinds = {c: spec.types.get(c, KIND_TEXT) for c in spec.columns}
    kinds.update({c.lower(): k for c, k in spec.types.items()})
    dataset = Dataset(key, spec.source, spec.description, tuple(spec.columns), kinds, spec.examples)
    for number, row in enumerate(spec.examples.rows, start=1):
        try:
            validate_filter(dataset, parse_filter(row.filter))
        except (PolicyError, FilterError) as exc:
            raise PolicyError(f"dataset '{key}', example row line {number}: {exc}") from exc
    for column in spec.examples.masks:
        if column.lower() not in dataset.columns:
            raise PolicyError(f"dataset '{key}', example masks: unknown column '{column}'")
    return dataset


def _documents(files: list[Path]) -> tuple[str, list[tuple[str, DatasetSpec]]]:
    """(fingerprint, datasets) of the policy files; the fingerprint is sha256 over their bytes."""
    digest = hashlib.sha256()
    out: list[tuple[str, DatasetSpec]] = []
    seen: set[str] = set()
    for path in files:
        raw_bytes, doc = _read(path)
        digest.update(raw_bytes)
        for name, spec in doc.datasets.items():
            key = name.lower()
            if key in seen:
                raise PolicyError(f"dataset '{name}' is defined twice")
            seen.add(key)
            out.append((key, spec))
    return digest.hexdigest()[:16], out


def load_catalog(files: list[Path]) -> dict[str, Dataset]:
    """The catalogue without a warehouse: datasets, columns, declared types and examples."""
    _, specs = _documents(files)
    return {key: _dataset_from_spec(key, spec) for key, spec in specs}


def load_policy(files: list[Path], schemas: Mapping[str, Mapping[str, str]]) -> PolicySet:
    """Load the catalogue and check it against the warehouse.

    `schemas` maps a relation to {column: warehouse type}, as the backend reports it. A
    dataset whose source is missing, whose declared columns differ from the warehouse's
    (Storage system columns aside) or whose declared types disagree with the warehouse
    types is refused.
    """
    digest, specs = _documents(files)
    datasets: dict[str, Dataset] = {}
    lowered = {relation: {c.lower(): t for c, t in columns.items()} for relation, columns in schemas.items()}
    for key, spec in specs:
        if spec.source not in lowered:
            raise PolicyError(f"dataset '{key}': source '{spec.source}' not found in the warehouse")
        warehouse = {c: t for c, t in lowered[spec.source].items() if c not in STORAGE_SYSTEM_COLUMNS}
        declared = set(spec.columns)
        if declared != set(warehouse):
            raise PolicyError(
                f"dataset '{key}': declared columns differ from the warehouse "
                f"(missing in the policy: {sorted(set(warehouse) - declared)}, "
                f"not in the warehouse: {sorted(declared - set(warehouse))})"
            )
        dataset = _dataset_from_spec(key, spec)
        wrong = {
            c: (dataset.column_kinds[c], column_kind(t))
            for c, t in warehouse.items()
            if dataset.column_kinds[c] != column_kind(t)
        }
        if wrong:
            details = ", ".join(f"{c}: declared {d}, warehouse {w}" for c, (d, w) in sorted(wrong.items()))
            raise PolicyError(f"dataset '{key}': declared types differ from the warehouse ({details})")
        datasets[key] = dataset
    return PolicySet(datasets=datasets, fingerprint=digest)


# ---- checking security lines against a dataset ----------------------------------------------


def literal_problem(kind: str, value: str) -> str | None:
    """Why `value` is not a valid literal for a column of `kind`, or None when it is."""
    if kind == KIND_NUMERIC and not NUMBER_LITERAL.fullmatch(value.strip()):
        return f"'{value}' is not a number"
    if kind == KIND_BOOLEAN and value.strip().lower() not in (BOOLEAN_TRUE, BOOLEAN_FALSE):
        return f"'{value}' is not true or false"
    if kind == KIND_DATE:
        try:
            (datetime if "T" in value or " " in value.strip() else date).fromisoformat(value.strip())
        except ValueError:
            return f"'{value}' is not a date (YYYY-MM-DD)"
    return None


def validate_condition(dataset: Dataset, condition: Condition) -> None:
    if condition.column not in dataset.column_kinds:
        raise PolicyError(f"unknown column '{condition.column}'")
    kind = dataset.column_kinds[condition.column]
    if condition.op in ORDER_OPERATORS and kind not in (KIND_NUMERIC, KIND_DATE):
        raise PolicyError(f"'{condition.op}' needs a numeric or date column; '{condition.column}' is {kind}")
    literals = list(condition.values or ()) if condition.op in LIST_OPERATORS else (
        [condition.value] if condition.value is not None else []
    )
    for value in literals:
        problem = literal_problem(kind, value)
        if problem:
            raise PolicyError(f"{problem} ({condition.column} is a {kind} column)")


def validate_filter(dataset: Dataset, row_filter: RowFilter) -> None:
    """Check a parsed filter against the dataset's columns and their kinds; PolicyError without location."""
    for condition in row_filter.conditions:
        validate_condition(dataset, condition)


def validate_mask(dataset: Dataset, column: str, treatment: str) -> None:
    if column.strip().lower() not in dataset.column_kinds:
        raise PolicyError(f"unknown column '{column}'")
    if treatment not in TREATMENTS:
        raise PolicyError(f"treatment must be one of {', '.join(TREATMENTS)}")


# ---- examples -> store lines ---------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedExamples:
    rows: list[RowAccessRuleInput]
    masks: list[ColumnMaskInput]
    # One readable sentence per example line that could not be loaded.
    skipped: list[str]


def resolve_examples(
    dataset: Dataset,
    *,
    users: Collection[str],
    local_groups: Collection[str],
    group_ids_by_external_id: Mapping[str, str],
) -> ResolvedExamples:
    """The dataset's example lines as store input; lines naming a subject that is not registered are skipped."""
    skipped: list[str] = []

    def subject(who: str | dict[str, str], label: str) -> tuple[str, str] | None:
        if who == SUBJECT_EVERYONE:
            return SUBJECT_EVERYONE, ""
        assert isinstance(who, dict)  # guaranteed by ExampleRow / ExampleMask validation
        key, value = next(iter(who.items()))
        value = str(value).strip()
        if key == "user":
            if value.lower() in users:
                return SUBJECT_USER, value.lower()
            skipped.append(f"{label}: user '{value}' is not registered")
        elif key == "local_group":
            if value in local_groups:
                return SUBJECT_LOCAL_GROUP, value
            skipped.append(f"{label}: local group '{value}' does not exist")
        elif value in group_ids_by_external_id:
            return SUBJECT_GROUP, group_ids_by_external_id[value]
        else:
            skipped.append(f"{label}: IdP group '{value}' is not registered")
        return None

    rows: list[RowAccessRuleInput] = []
    for number, row in enumerate(dataset.examples.rows, start=1):
        resolved = subject(row.who, f"Row line {number}")
        if resolved is not None:
            rows.append(RowAccessRuleInput(row.kind, resolved[0], resolved[1], row.filter))
    masks: list[ColumnMaskInput] = []
    for column, lines in dataset.examples.masks.items():
        for number, mask in enumerate(lines, start=1):
            resolved = subject(mask.who, f"Mask on {column.lower()}, line {number}")
            if resolved is not None:
                masks.append(ColumnMaskInput(column.lower(), resolved[0], resolved[1], mask.treatment))
    return ResolvedExamples(rows, masks, skipped)

