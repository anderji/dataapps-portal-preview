"""Build the dashboards' warehouse from input-mapped CSVs (Keboola Data Apps).

A Data App's input mapping copies Storage tables to `/data/in/tables/<name>.csv`
when the app is deployed (a snapshot; refreshed only by a redeploy). The data
guard runs on DuckDB, so `setup.sh` turns those CSVs into one DuckDB file
before the app starts:

    python -m kbc_access.input_tables --input /data/in/tables --out data/warehouse.duckdb \
        --table companies --table stores --table drug_sales --table supply_chain

Every listed table must be present; a missing one fails the start (a dashboard
silently missing a dataset would look like an access rule). The file is built
under a temporary name and renamed, so the app never opens a half-built file.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import duckdb

TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def build(input_dir: Path, out: Path, tables: list[str]) -> dict[str, int]:
    """Load `<input_dir>/<table>.csv` for every table into a fresh DuckDB file. Returns row counts."""
    for table in tables:
        if not TABLE_NAME.fullmatch(table):
            raise ValueError(f"Invalid table name '{table}'")
    missing = [t for t in tables if not (input_dir / f"{t}.csv").is_file()]
    if missing:
        raise FileNotFoundError(f"Input mapping did not deliver: {', '.join(missing)} (looked in {input_dir})")
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    partial.unlink(missing_ok=True)
    counts: dict[str, int] = {}
    with duckdb.connect(str(partial)) as con:
        for table in tables:
            source = (input_dir / f"{table}.csv").as_posix()
            con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv(?, header = true, auto_detect = true)", [source])  # noqa: S608
            counts[table] = int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608
    partial.replace(out)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--table", action="append", required=True, dest="tables")
    args = parser.parse_args(argv)
    try:
        counts = build(args.input, args.out, args.tables)
    except (ValueError, FileNotFoundError, duckdb.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(", ".join(f"{t}={n}" for t, n in counts.items()), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
