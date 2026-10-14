"""Row access and column masking: the vocabulary shared by the store, the policy files and the compiler.

A dataset's security is two ordered lists, modelled on Snowflake row access and
masking policies:

* row lines `who -> which rows`: `show` lines union, `hide` lines remove rows for
  their subject;
* column mask lines per column `who -> treatment`: the first line whose subject
  matches wins; no matching line means clear.

A row filter is structured data (`{"all": true}` or an AND of conditions), never
free SQL. This module only knows its shape; the compiler turns it into a sqlglot
AST with literals typed from the warehouse column types.

Nothing here imports the rest of the package, so every layer can use it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# ---- subjects ------------------------------------------------------------------

SUBJECT_EVERYONE = "everyone"
SUBJECT_USER = "user"
SUBJECT_LOCAL_GROUP = "local_group"
SUBJECT_GROUP = "group"
RULE_SUBJECT_TYPES = (SUBJECT_EVERYONE, SUBJECT_USER, SUBJECT_LOCAL_GROUP, SUBJECT_GROUP)

# ---- row lines -----------------------------------------------------------------

KIND_SHOW = "show"
KIND_HIDE = "hide"
ROW_KINDS = (KIND_SHOW, KIND_HIDE)

# ---- column treatments, from most to least revealing -----------------------------

CLEAR = "clear"
MASK_PARTIAL = "mask_partial"
MASK_HASH = "mask_hash"
HIDDEN = "hidden"
TREATMENTS = (CLEAR, MASK_PARTIAL, MASK_HASH, HIDDEN)
MASKED_TREATMENTS = (MASK_PARTIAL, MASK_HASH)

# ---- operators -----------------------------------------------------------------

OP_IS = "is"
OP_IS_NOT = "is_not"
OP_ONE_OF = "one_of"
OP_NOT_ONE_OF = "not_one_of"
OP_GT = "gt"
OP_GTE = "gte"
OP_LT = "lt"
OP_LTE = "lte"
OP_IS_EMPTY = "is_empty"
OP_IS_NOT_EMPTY = "is_not_empty"
OPERATORS = (
    OP_IS, OP_IS_NOT, OP_ONE_OF, OP_NOT_ONE_OF, OP_GT, OP_GTE, OP_LT, OP_LTE, OP_IS_EMPTY, OP_IS_NOT_EMPTY,
)
LIST_OPERATORS = (OP_ONE_OF, OP_NOT_ONE_OF)
EMPTY_OPERATORS = (OP_IS_EMPTY, OP_IS_NOT_EMPTY)
ORDER_OPERATORS = (OP_GT, OP_GTE, OP_LT, OP_LTE)

# ---- value sources ---------------------------------------------------------------

SOURCE_EMAIL = "email"
SOURCE_ATTRIBUTE_PREFIX = "attribute:"
# Same shape as attribute keys in the access store.
ATTRIBUTE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# ---- column kinds (logical types the editor and the literal typing use) ----------

KIND_NUMERIC = "numeric"
KIND_BOOLEAN = "boolean"
KIND_DATE = "date"
KIND_TEXT = "text"
COLUMN_KINDS = (KIND_NUMERIC, KIND_BOOLEAN, KIND_DATE, KIND_TEXT)
BOOLEAN_TRUE = "true"
BOOLEAN_FALSE = "false"


class FilterError(ValueError):
    """A row filter that does not have the expected shape (message has no location)."""


@dataclass(frozen=True)
class Condition:
    """`column op value`; exactly one of value / values / source, none for the emptiness operators."""

    column: str
    op: str
    value: str | None = None
    values: tuple[str, ...] | None = None
    source: str | None = None

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {"column": self.column, "op": self.op}
        if self.value is not None:
            out["value"] = self.value
        if self.values is not None:
            out["values"] = list(self.values)
        if self.source is not None:
            out["source"] = self.source
        return out

    @property
    def attribute(self) -> str | None:
        """The attribute key this condition reads, if its source is an attribute."""
        if self.source and self.source.startswith(SOURCE_ATTRIBUTE_PREFIX):
            return self.source[len(SOURCE_ATTRIBUTE_PREFIX):]
        return None


@dataclass(frozen=True)
class RowFilter:
    """All rows, or the AND of `conditions`."""

    all_rows: bool
    conditions: tuple[Condition, ...] = ()

    def to_json(self) -> dict[str, object]:
        if self.all_rows:
            return {"all": True}
        return {"conditions": [c.to_json() for c in self.conditions]}


ALL_ROWS = RowFilter(all_rows=True)


def _scalar_text(value: object, what: str) -> str:
    """A literal from JSON as text: strings as they are, numbers in their JSON form, booleans lowercase."""
    if isinstance(value, bool):
        return BOOLEAN_TRUE if value else BOOLEAN_FALSE
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return value
    raise FilterError(f"{what} must be a text, number or boolean value")


def _parse_source(raw: object) -> str:
    if not isinstance(raw, str):
        raise FilterError("source must be 'email' or 'attribute:<key>'")
    source = raw.strip()
    if source == SOURCE_EMAIL:
        return source
    if source.startswith(SOURCE_ATTRIBUTE_PREFIX) and ATTRIBUTE_KEY.fullmatch(source[len(SOURCE_ATTRIBUTE_PREFIX):]):
        return source
    raise FilterError(f"source must be 'email' or 'attribute:<key>' (lowercase snake_case key), got '{source}'")


def parse_condition(raw: object) -> Condition:
    if not isinstance(raw, Mapping):
        raise FilterError("each condition must be an object")
    unknown = set(raw) - {"column", "op", "value", "values", "source"}
    if unknown:
        raise FilterError(f"unknown condition field(s): {', '.join(sorted(map(str, unknown)))}")
    column = raw.get("column")
    if not isinstance(column, str) or not column.strip():
        raise FilterError("each condition needs a column")
    column = column.strip().lower()
    op = raw.get("op")
    if op not in OPERATORS:
        raise FilterError(f"operator must be one of {', '.join(OPERATORS)}, got '{op}'")
    given = [k for k in ("value", "values", "source") if raw.get(k) is not None]
    if op in EMPTY_OPERATORS:
        if given:
            raise FilterError(f"'{op}' on '{column}' takes no value")
        return Condition(column, op)
    if op in LIST_OPERATORS:
        if given != ["values"]:
            raise FilterError(f"'{op}' on '{column}' takes a list of values (and nothing else)")
        values = raw["values"]
        if not isinstance(values, list) or not values:
            raise FilterError(f"'{op}' on '{column}' needs a non-empty list of values")
        return Condition(column, op, values=tuple(_scalar_text(v, f"a value for '{column}'") for v in values))
    if len(given) != 1 or given[0] == "values":
        raise FilterError(f"'{op}' on '{column}' needs exactly one of a value or a source")
    if given[0] == "source":
        return Condition(column, op, source=_parse_source(raw["source"]))
    return Condition(column, op, value=_scalar_text(raw["value"], f"the value for '{column}'"))


def parse_filter(raw: object) -> RowFilter:
    """Parse filter JSON (`{"all": true}` or `{"conditions": [...]}`); raise FilterError when malformed."""
    if not isinstance(raw, Mapping):
        raise FilterError("the filter must be an object")
    if "all" in raw:
        if dict(raw) != {"all": True}:
            raise FilterError('an all-rows filter is exactly {"all": true}')
        return ALL_ROWS
    if set(raw) != {"conditions"}:
        raise FilterError('the filter needs "conditions" (or {"all": true})')
    conditions = raw["conditions"]
    if not isinstance(conditions, list) or not conditions:
        raise FilterError('"conditions" must be a non-empty list (use {"all": true} for all rows)')
    return RowFilter(all_rows=False, conditions=tuple(parse_condition(c) for c in conditions))


@dataclass(frozen=True)
class Audience:
    """Who a principal is for subject matching: email, local groups and internal IdP group ids."""

    email: str
    local_groups: frozenset[str]
    group_ids: frozenset[str]
    # Display names by (subject type, subject id), used only in `explain` sentences.
    labels: Mapping[tuple[str, str], str] = field(default_factory=dict, compare=False, hash=False)

    def matches(self, subject_type: str, subject_id: str) -> bool:
        if subject_type == SUBJECT_EVERYONE:
            return True
        if subject_type == SUBJECT_USER:
            return subject_id == self.email
        if subject_type == SUBJECT_LOCAL_GROUP:
            return subject_id in self.local_groups
        if subject_type == SUBJECT_GROUP:
            return subject_id in self.group_ids
        return False

    def explain(self, subject_type: str, subject_id: str) -> str:
        """Why a subject does or does not match, in a sentence for the admin."""
        name = self.labels.get((subject_type, subject_id), subject_id)
        if subject_type == SUBJECT_EVERYONE:
            return "every signed-in user"
        if subject_type == SUBJECT_USER:
            return "this user" if self.matches(subject_type, subject_id) else f"only for user {name}"
        if subject_type == SUBJECT_LOCAL_GROUP:
            return (f"member of local group {name}" if self.matches(subject_type, subject_id)
                    else f"not a member of local group {name}")
        if subject_type == SUBJECT_GROUP:
            return (f"carries IdP group {name}" if self.matches(subject_type, subject_id)
                    else f"does not carry IdP group {name}")
        return f"unknown subject type '{subject_type}'"
