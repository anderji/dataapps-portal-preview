"""Demo data for the local suite: a fake pharma warehouse and a pre-filled access store.

Used by scripts/seed.py and by the test suite. Every warehouse row has a
`company` and a `visibility` ('public' | 'private') — the row model from the
2026-07-29 voice note.
"""

from __future__ import annotations

import csv
import random
import tempfile
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import yaml

from kbc_access.config import SeedSettings, Settings
from kbc_access.policy import load_catalog, resolve_examples
from kbc_access.store import AccessStore, App, StoreError

COMPANIES = [
    ("PHARMA_A", "Pharma A s.r.o."),
    ("PHARMA_B", "Pharma B a.s."),
    ("PHARMA_C", "Pharma C GmbH"),
]
CITIES = ["Praha", "Brno", "Ostrava", "Plzeň", "Bratislava", "Košice", "Wien", "Graz"]
REGIONS = {"Praha": "CZ-West", "Plzeň": "CZ-West", "Brno": "CZ-East", "Ostrava": "CZ-East",
           "Bratislava": "SK", "Košice": "SK", "Wien": "AT", "Graz": "AT"}
DRUGS = [
    ("Paralen 500", "Analgesics"), ("Ibalgin 400", "Analgesics"), ("Aspirin C", "Analgesics"),
    ("Amoksiklav", "Antibiotics"), ("Azitromycin", "Antibiotics"),
    ("Euthyrox", "Hormones"), ("Metformin", "Diabetes"), ("Atoris", "Cardio"), ("Prestarium", "Cardio"),
    ("Zyrtec", "Allergy"), ("Claritine", "Allergy"),
]
SUPPLIERS = ["Zentiva", "Teva", "Sandoz", "Krka", "Bayer", "Servier"]
STATUSES = ["delivered", "delivered", "delivered", "in_transit", "delayed"]
STORES_PER_COMPANY = 6


DEMO_TABLES = ("companies", "stores", "drug_sales", "supply_chain")


def _bulk_insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple[object, ...]]) -> None:
    """Load rows through a CSV and COPY: orders of magnitude faster than row-by-row inserts."""
    with tempfile.NamedTemporaryFile("w", suffix=".csv", newline="", encoding="utf-8", delete=False) as fh:
        csv.writer(fh).writerows(rows)
        name = fh.name
    try:
        con.execute(f"COPY {table} FROM '{Path(name).as_posix()}' (HEADER false, DELIMITER ',')")  # noqa: S608 — fixed names
    finally:
        Path(name).unlink(missing_ok=True)


def build_warehouse(settings: Settings, path: Path) -> None:
    if settings.seed is None:
        raise ValueError("Building the demo warehouse needs the [seed] section in the config")
    build_warehouse_from_seed(settings.seed, path)


def export_csvs(warehouse: Path, out_dir: Path) -> dict[str, Path]:
    """Write every demo table as a CSV with a header (the shape Storage imports and exports)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    with duckdb.connect(str(warehouse), read_only=True) as con:
        for table in DEMO_TABLES:
            target = out_dir / f"{table}.csv"
            con.execute(f"COPY {table} TO '{target.as_posix()}' (HEADER, DELIMITER ',')")  # noqa: S608 — fixed names
            files[table] = target
    return files


def build_warehouse_from_seed(cfg: SeedSettings, path: Path) -> None:
    rnd = random.Random(cfg.random_seed)
    start = date.fromisoformat(cfg.start_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE companies (company VARCHAR, name VARCHAR)")
    _bulk_insert(con, "companies", COMPANIES)

    stores: list[tuple[object, ...]] = []
    for company, _ in COMPANIES:
        for i in range(STORES_PER_COMPANY):
            city = rnd.choice(CITIES)
            store_id = f"{company[-1]}{i + 1:03d}"
            visibility = "public" if rnd.random() < cfg.public_share * 2 else "private"
            manager = f"manager.{store_id.lower()}@{company.lower().replace('_', '-')}.example"
            stores.append((store_id, company, f"Lékárna {city} {i + 1}", city, REGIONS[city], manager, visibility))
    con.execute(
        "CREATE TABLE stores (store_id VARCHAR, company VARCHAR, name VARCHAR, city VARCHAR, "
        "region VARCHAR, manager_email VARCHAR, visibility VARCHAR)"
    )
    _bulk_insert(con, "stores", stores)

    sales: list[tuple[object, ...]] = []
    for n in range(cfg.drug_sales_rows):
        store = rnd.choice(stores)
        drug, category = rnd.choice(DRUGS)
        units = rnd.randint(1, 40)
        price = round(rnd.uniform(49, 890), 2)
        revenue = round(units * price, 2)
        sales.append(
            (
                n + 1,
                start + timedelta(days=rnd.randrange(cfg.days)),
                store[1],
                store[0],
                drug,
                category,
                units,
                revenue,
                round(revenue * rnd.uniform(0.08, 0.35), 2),
                f"CUST-{rnd.randint(100000, 999999)}",
                "public" if rnd.random() < cfg.public_share else "private",
                rnd.random() < cfg.test_share,
            )
        )
    con.execute(
        "CREATE TABLE drug_sales (sale_id INTEGER, sale_date DATE, company VARCHAR, store_id VARCHAR, "
        "drug VARCHAR, category VARCHAR, units INTEGER, revenue DOUBLE, margin DOUBLE, customer_id VARCHAR, "
        "visibility VARCHAR, is_test BOOLEAN)"
    )
    _bulk_insert(con, "drug_sales", sales)

    shipments: list[tuple[object, ...]] = []
    for n in range(cfg.supply_chain_rows):
        company = rnd.choice(COMPANIES)[0]
        drug, _ = rnd.choice(DRUGS)
        shipments.append(
            (
                n + 1,
                start + timedelta(days=rnd.randrange(cfg.days)),
                company,
                rnd.choice(SUPPLIERS),
                drug,
                rnd.randint(50, 5000),
                round(rnd.uniform(12, 420), 2),
                rnd.choice(STATUSES),
                rnd.randint(2, 45),
                f"CTR-{company[-1]}-{rnd.randint(1000, 9999)}",
                "public" if rnd.random() < cfg.public_share * 2 else "private",
            )
        )
    con.execute(
        "CREATE TABLE supply_chain (shipment_id INTEGER, ship_date DATE, company VARCHAR, supplier VARCHAR, "
        "drug VARCHAR, quantity INTEGER, unit_cost DOUBLE, status VARCHAR, lead_time_days INTEGER, "
        "contract_ref VARCHAR, visibility VARCHAR)"
    )
    _bulk_insert(con, "supply_chain", shipments)
    con.close()
    print(f"warehouse: {len(stores)} stores, {len(sales)} sales, {len(shipments)} shipments -> {path}", file=sys.stderr)


def fill_store(settings: Settings, store: AccessStore) -> None:
    fake = yaml.safe_load(settings.path(settings.require_gateway().fake_users_file).read_text(encoding="utf-8"))
    for app_id, app in settings.apps.items():
        if app_id == "launcher":
            continue  # the launcher lists apps; it is not itself gated
        store.upsert_app(App(app_id, app.name, settings.app_url(app_id), "local", app_id, "manual", True))

    group_ids: dict[str, str] = {}
    existing = {(g.provider, g.external_id): g.group_id for g in store.list_groups()}
    for provider, groups in fake["groups"].items():
        for key, external_id in groups.items():
            try:
                group_ids[key] = store.create_group(provider, external_id, key).group_id
            except StoreError:
                group_ids[key] = existing[(provider, external_id)]

    for user in fake["users"]:
        store.upsert_user(user["email"], user["display_name"], user.get("active", True))
        for key, value in user.get("attributes", {}).items():
            store.set_attribute(user["email"], key, value)

    # Suite-defined groups: members must be registered above, so this runs after the users.
    for local_group_id, spec in fake.get("local_groups", {}).items():
        try:
            store.create_local_group(local_group_id, spec["name"], spec.get("description", ""))
        except StoreError:
            pass  # re-run on an existing store: keep the group, still add missing members
        for member in spec.get("members", []):
            store.add_local_group_member(local_group_id, member)

    for app_id, grants in fake["grants"].items():
        for grant in grants:
            if "group" in grant:
                store.grant(app_id, "group", group_ids[grant["group"]], "seed")
            elif "local_group" in grant:
                store.grant(app_id, "local_group", grant["local_group"], "seed")
            else:
                store.grant(app_id, "user", grant["user"], "seed")
    load_example_security(settings, store)
    print(
        f"access store: {len(store.list_apps())} apps, {len(store.list_users())} users, "
        f"{len(store.list_groups())} groups, {len(store.list_local_groups())} local groups, "
        f"{len(store.list_grants())} grants",
        file=sys.stderr,
    )


def load_example_security(settings: Settings, store: AccessStore) -> None:
    """Copy each catalogue dataset's example row lines and masks into the store, once.

    A dataset that already has lines or masks is left alone, so a re-run never overwrites what
    an administrator changed. Example lines naming an unregistered subject are skipped and printed.
    """
    if settings.policy is None:
        return
    catalog = load_catalog([settings.path(f) for f in settings.policy.files])
    users = {u.email for u in store.list_users()}
    local_groups = {g.local_group_id for g in store.list_local_groups()}
    group_ids = {g.external_id: g.group_id for g in store.list_groups()}
    for name, dataset in catalog.items():
        current = store.dataset_security(name)
        if current.rows or current.masks or not dataset.has_examples:
            continue
        resolved = resolve_examples(dataset, users=users, local_groups=local_groups, group_ids_by_external_id=group_ids)
        store.set_dataset_security(name, resolved.rows, resolved.masks)
        for problem in resolved.skipped:
            print(f"dataset {name}: example skipped: {problem}", file=sys.stderr)
