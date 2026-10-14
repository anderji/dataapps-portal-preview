"""DuckDB backend for the data guard.

Every call opens a fresh read-only connection with external access disabled
and the configuration locked, so even a query that slipped past the rewriter
cannot read files, load extensions or change settings. This is defence in
depth; the rewriter is still the primary control.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple[object, ...]]
    truncated: bool


class DuckDBBackend:
    dialect = "duckdb"

    def __init__(self, path: Path, max_rows: int) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"Warehouse file not found: {path} (run scripts/seed.py)")
        self._path = path
        self._max_rows = max_rows

    def _connect(self) -> duckdb.DuckDBPyConnection:
        conn = duckdb.connect(
            str(self._path),
            read_only=True,
            config={"enable_external_access": False, "autoload_known_extensions": False},
        )
        # Connections to one file in one process share a database instance, so a concurrent request
        # finds the configuration already locked; setting the lock again would raise. Lock it only once
        # and refuse a connection whose instance is not locked after that.
        if not self._configuration_locked(conn):
            try:
                conn.execute("SET lock_configuration = true")
            except duckdb.InvalidInputException:
                pass  # another thread locked it between the check and the SET; verified just below
        if not self._configuration_locked(conn):
            conn.close()
            raise RuntimeError("DuckDB configuration could not be locked")
        return conn

    @staticmethod
    def _configuration_locked(conn: duckdb.DuckDBPyConnection) -> bool:
        row = conn.execute("SELECT current_setting('lock_configuration')").fetchone()
        return bool(row and row[0])

    def schemas(self) -> dict[str, list[str]]:
        """Relation name → column names, for every table in the main schema."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
            ).fetchall()
        out: dict[str, list[str]] = {}
        for table, column in rows:
            out.setdefault(table, []).append(column)
        return out

    def column_types(self) -> dict[str, dict[str, str]]:
        """Relation name → {column name: type}, for every table in the main schema, in column order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
            ).fetchall()
        out: dict[str, dict[str, str]] = {}
        for table, column, data_type in rows:
            out.setdefault(table, {})[column] = data_type
        return out

    def execute(self, sql: str) -> QueryResult:
        with self._connect() as conn:
            cursor = conn.execute(sql)
            columns = [d[0] for d in (cursor.description or [])]
            rows = cursor.fetchmany(self._max_rows + 1)
        return QueryResult(columns=columns, rows=rows[: self._max_rows], truncated=len(rows) > self._max_rows)
