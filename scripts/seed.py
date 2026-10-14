"""Build the local demo: a fake pharma warehouse (DuckDB) and a pre-filled access store.

    .venv/bin/python scripts/seed.py --config config/local.toml [--reset]

Warehouse tables: companies, stores, drug_sales, supply_chain. Every row has a
`company` and a `visibility` ('public' | 'private') — the row model from the
2026-07-29 voice note. Access store: the apps from [apps] (except the
launcher), the users, groups, attributes and grants from the fake users file.
`--reset` deletes both files first; without it an existing store is left as
is, so changes made in the admin app survive a re-run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kbc_access.config import load_settings  # noqa: E402
from kbc_access.demo_seed import build_warehouse, fill_store  # noqa: E402
from kbc_access.store import AccessStore  # noqa: E402

SECRETS_FILE = ROOT / "secrets.local.env"


def env_for_settings() -> dict[str, str]:
    """Process env plus secrets.local.env (the process env wins)."""
    values = {k: v for k, v in dotenv_values(SECRETS_FILE).items() if v is not None}
    return {**values, **os.environ}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--reset", action="store_true", help="delete the warehouse and access store first")
    args = parser.parse_args()
    settings = load_settings(args.config, env_for_settings())

    if settings.store.sqlite_path is None:
        raise ValueError("scripts/seed.py fills the local SQLite store; the config has no [store] sqlite_path")
    data, _ = settings.require_data()
    store_path = settings.path(settings.store.sqlite_path)
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{store_path}{suffix}").unlink(missing_ok=True)
    build_warehouse(settings, settings.path(data.duckdb_path))
    fill_store(settings, AccessStore(store_path, settings.providers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
