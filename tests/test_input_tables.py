"""Building the dashboards' DuckDB from input-mapped CSVs."""

from __future__ import annotations

import duckdb
import pytest

from kbc_access.demo_seed import DEMO_TABLES, build_warehouse, export_csvs
from kbc_access.input_tables import build, main


@pytest.fixture
def csv_dir(settings, tmp_path):
    build_warehouse(settings, tmp_path / "orig.duckdb")
    export_csvs(tmp_path / "orig.duckdb", tmp_path / "in")
    return tmp_path / "in"


def test_round_trip_keeps_rows_and_types(csv_dir, tmp_path, settings):
    counts = build(csv_dir, tmp_path / "out.duckdb", list(DEMO_TABLES))
    assert counts["drug_sales"] == settings.seed.drug_sales_rows
    with duckdb.connect(str(tmp_path / "out.duckdb"), read_only=True) as con:
        types = dict(con.execute("select column_name, data_type from information_schema.columns where table_name='drug_sales'").fetchall())
    assert types["sale_date"] == "DATE" and types["is_test"] == "BOOLEAN" and types["revenue"] == "DOUBLE"


def test_missing_table_fails_the_start(csv_dir, tmp_path):
    (csv_dir / "stores.csv").unlink()
    with pytest.raises(FileNotFoundError, match="stores"):
        build(csv_dir, tmp_path / "out.duckdb", list(DEMO_TABLES))
    assert not (tmp_path / "out.duckdb").exists()


def test_invalid_table_name_is_refused(csv_dir, tmp_path):
    with pytest.raises(ValueError):
        build(csv_dir, tmp_path / "out.duckdb", ["drug_sales; drop"])


def test_cli_exit_codes(csv_dir, tmp_path):
    args = ["--input", str(csv_dir), "--out", str(tmp_path / "o.duckdb")]
    assert main([*args, "--table", "companies"]) == 0
    assert main([*args, "--table", "ghost"]) == 2
