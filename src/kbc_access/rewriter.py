"""Layer 3c — rewriter: the chokepoint every ad-hoc query passes through.

A user query is parsed, checked and rewritten so that every reference to a
dataset — at any depth: CTE bodies, subqueries, UNION branches, joins — reads
the principal's secured relation instead of the physical table.

The checks are whitelists, not blacklists of syntax:

* exactly one statement, and it must be a SELECT or a set operation of SELECTs;
* every table reference must be an unqualified dataset name the principal may
  see, or a CTE that is *in scope at that point* — a non-recursive CTE is
  visible to later CTEs and to the main query, never to its own body or to
  earlier CTEs, exactly as the engine resolves it. Every CTE is renamed to a
  unique internal name before execution, so after the rewrite any remaining
  table name can only mean a secured dataset; a CTE called `drug_sales`
  cannot stand in for (or unmask) the real one;
* table functions (`read_csv(...)`) and a denylist of scalar functions that
  reach outside the database are refused;
* query parameters are refused — the only literals are the ones the user typed.

A dataset the principal may not see produces the same error as a dataset that
does not exist, so the error message does not confirm its existence.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from kbc_access.compiler import CompiledDataset

# Scalar and table functions that read files, env or settings. The DuckDB
# backend also disables external access; this is the first line of defence.
DENIED_FUNCTIONS = frozenset(
    {
        "getenv",
        "current_setting",
        "read_text",
        "read_blob",
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "parquet_scan",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "glob",
        "load_extension",
        "pg_read_file",
        "system",
        "query",
        "query_table",
        "sql_auto_complete",
        # Return the executing SQL text, which contains the masking salt.
        "current_query",
        "current_setting",
    }
)

_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)
# Statement kinds that must never appear anywhere in the tree.
_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Set,
    exp.Use,
    exp.Copy,
    exp.Pragma,
    exp.Attach,
    exp.Detach,
    exp.Install,
    exp.LoadData,
    exp.Placeholder,
    exp.Parameter,
    exp.Into,
)


class QueryRejected(ValueError):
    """The query is not allowed. The message is safe to show to the user."""


@dataclass(frozen=True)
class RewrittenQuery:
    sql: str
    datasets: tuple[str, ...]
    query_hash: str


def query_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def _function_name(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    if isinstance(node, exp.Func):
        return node.sql_name().lower()
    return None


def _cte_scope(table: exp.Table, ctes: list[exp.CTE]) -> int | None:
    """Index of the top-level CTE whose body contains `table`, or None for the main query."""
    node: exp.Expression | None = table
    while node is not None:
        parent = node.parent
        if isinstance(node, exp.CTE) and node in ctes:
            return ctes.index(node)
        node = parent
    return None


def _visible_ctes(scope: int | None, ctes: list[exp.CTE], recursive: bool) -> dict[str, int]:
    """CTE name → index for the CTEs a reference in `scope` resolves to."""
    upto = len(ctes) if scope is None else scope + (1 if recursive else 0)
    visible: dict[str, int] = {}
    for i in range(upto):
        visible[ctes[i].alias_or_name.lower()] = i
    return visible


def _replace_table(table: exp.Table, replacement: exp.Expression) -> None:
    # `FROM (drug_sales)` parses as an unaliased Subquery around the Table; replace
    # that wrapper too, or the engine sees `((SELECT ...) AS x)` and refuses it.
    parent = table.parent
    wrapped = isinstance(parent, (exp.Paren, exp.Subquery)) and parent.this is table and not parent.args.get("alias")
    (parent if wrapped else table).replace(replacement)


def rewrite(
    sql: str,
    secured: dict[str, CompiledDataset],
    *,
    dialect: str,
    max_chars: int,
) -> RewrittenQuery:
    """Rewrite `sql` against the principal's secured relations.

    `secured` maps dataset name → compiled relation for exactly the datasets
    the principal may see. A hidden dataset gets the same "Unknown dataset"
    error as a name that does not exist, so errors do not reveal it.
    """
    if len(sql) > max_chars:
        raise QueryRejected(f"Query is too long (limit {max_chars} characters)")
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError as exc:
        errors = getattr(exc, "errors", None)
        raise QueryRejected(f"Query does not parse: {errors[0]['description'] if errors else exc}") from exc
    except RecursionError as exc:
        raise QueryRejected("Query is nested too deeply") from exc
    if len(statements) != 1:
        raise QueryRejected("Exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, _ALLOWED_ROOTS):
        raise QueryRejected("Only SELECT queries are allowed")

    try:
        return _rewrite_tree(tree, secured, dialect=dialect, original=sql)
    except RecursionError as exc:
        raise QueryRejected("Query is nested too deeply") from exc


def _rewrite_tree(tree: exp.Expression, secured: dict[str, CompiledDataset], *, dialect: str, original: str) -> RewrittenQuery:
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise QueryRejected(f"{type(node).__name__} is not allowed")
        name = _function_name(node)
        if name is not None and name in DENIED_FUNCTIONS:
            raise QueryRejected(f"Function '{name}' is not allowed")
        # CTEs only at the top: a WITH inside a subquery has its own scope rules;
        # allowing only one level keeps scope resolution simple and exact.
        if isinstance(node, exp.With) and node.parent is not tree:
            raise QueryRejected("WITH is allowed only at the top of the query")

    with_ = tree.args.get("with_") or tree.args.get("with")
    ctes: list[exp.CTE] = list(with_.expressions) if with_ is not None else []
    recursive = bool(with_ is not None and with_.args.get("recursive"))
    names = [c.alias_or_name.lower() for c in ctes]
    if len(set(names)) != len(names):
        raise QueryRejected("Two CTEs have the same name")
    internal = [f"__kbc_cte_{i}_{uuid.uuid4().hex[:8]}" for i in range(len(ctes))]

    used: set[str] = set()
    for table in list(tree.find_all(exp.Table)):
        if not isinstance(table.this, exp.Identifier):
            raise QueryRejected("Table functions are not allowed in FROM")
        if table.args.get("db") or table.args.get("catalog"):
            raise QueryRejected(f"Qualified names are not allowed ('{table.sql(dialect=dialect)}')")
        name = table.name.lower()
        alias = table.args.get("alias")
        visible = _visible_ctes(_cte_scope(table, ctes), ctes, recursive)
        if name in visible:
            # A CTE reference: rename it in place to the CTE's internal name (keeping
            # PIVOT/SAMPLE etc., which only touch the user's own relation) and alias
            # it with the user-facing name so `a.col` still resolves.
            if not isinstance(alias, exp.TableAlias):
                table.set("alias", exp.TableAlias(this=table.this.copy()))
            table.set("this", exp.to_identifier(internal[visible[name]]))
            continue
        for arg in ("pivots", "sample", "version", "when", "joins", "laterals"):
            if table.args.get(arg):
                raise QueryRejected(f"{arg.upper()} on a dataset is not supported; wrap the dataset in a CTE first")
        if name not in secured:
            raise QueryRejected(f"Unknown dataset '{table.name}'")
        used.add(name)
        # Keep the name the query uses for this relation (its alias with any column
        # list, or the dataset name itself) so `d.col` and `drug_sales.col` resolve.
        new_alias = alias.copy() if isinstance(alias, exp.TableAlias) and alias.this else exp.TableAlias(this=exp.to_identifier(table.name))
        if isinstance(alias, exp.TableAlias) and not alias.this and alias.columns:
            new_alias.set("columns", [c.copy() for c in alias.columns])
        _replace_table(table, exp.Subquery(this=secured[name].relation.copy(), alias=new_alias))

    for cte, internal_name in zip(ctes, internal, strict=True):
        cte_alias = cte.args.get("alias")
        columns = [c.copy() for c in cte_alias.columns] if isinstance(cte_alias, exp.TableAlias) else []
        cte.set("alias", exp.TableAlias(this=exp.to_identifier(internal_name), columns=columns or None))

    if not used:
        raise QueryRejected("The query must read at least one dataset")
    return RewrittenQuery(sql=tree.sql(dialect=dialect), datasets=tuple(sorted(used)), query_hash=query_hash(original))

