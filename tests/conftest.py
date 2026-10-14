"""Shared fixtures: an isolated copy of the local suite per test, built fast.

* The demo warehouse (DuckDB) is built ONCE per test session and shared by
  every test that reads it. Its file and folder are read-only, so a test that
  tries to write to it — or to rebuild it — fails loudly instead of corrupting
  data for the others. Tests that need their own warehouse build one in
  their own tmp_path.
* The access store (SQLite) is seeded once into a template; `seeded` gives each
  test a fresh copy, because tests change it.
* Audit and app logs live in each test's tmp_path.

`headers_for(email)` returns the proxy headers a fake-IdP user would arrive with.
`frontend_dist` is a minimal built single-page frontend (index.html + one asset)
for apps that serve one, so tests never depend on an npm build.
"""

from __future__ import annotations

import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from kbc_access.config import Settings, load_settings
from kbc_access.demo_seed import build_warehouse, fill_store
from kbc_access.store import AccessStore
from kbc_access.web import Runtime, build_runtime

ROOT = Path(__file__).resolve().parent.parent
TEST_SECRETS = {
    "GATEWAY_SESSION_SECRET": "test-session-secret-0123456789",
    "MASK_SALT": "test-mask-salt-0123456789abcdef",
}
READ_ONLY_FILE = stat.S_IRUSR | stat.S_IRGRP
READ_ONLY_DIR = stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP


def base_settings() -> Settings:
    return load_settings(ROOT / "config" / "local.toml", TEST_SECRETS)


def settings_in(base: Settings, folder: Path, warehouse: Path) -> Settings:
    """`base` with the store, logs and audit in `folder` and the warehouse at `warehouse`."""
    return base.model_copy(
        update={
            "store": base.store.model_copy(update={"sqlite_path": folder / "access.sqlite"}),
            "data": base.data.model_copy(update={"duckdb_path": warehouse}),
            "audit": base.audit.model_copy(update={"path": folder / "audit.jsonl"}),
            "logging": base.logging.model_copy(update={"path": folder / "app.log"}),
        }
    )


@pytest.fixture(scope="session")
def shared_warehouse(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The demo warehouse, built once and read-only for the whole session."""
    folder = tmp_path_factory.mktemp("shared-warehouse")
    path = folder / "warehouse.duckdb"
    build_warehouse(base_settings(), path)
    path.chmod(READ_ONLY_FILE)
    folder.chmod(READ_ONLY_DIR)
    yield path
    folder.chmod(stat.S_IRWXU)  # let pytest clean up
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


@pytest.fixture(scope="session")
def store_template(tmp_path_factory: pytest.TempPathFactory, shared_warehouse: Path) -> Path:
    """The seeded access store, built once; tests get copies."""
    folder = tmp_path_factory.mktemp("store-template")
    settings = settings_in(base_settings(), folder, shared_warehouse)
    path = settings.path(settings.store.sqlite_path)
    fill_store(settings, AccessStore(path, settings.providers))
    # Fold the WAL into the main file so a plain copy carries every row.
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path


@pytest.fixture
def settings(tmp_path: Path, shared_warehouse: Path) -> Settings:
    return settings_in(base_settings(), tmp_path, shared_warehouse)


@pytest.fixture
def seeded(settings: Settings, store_template: Path) -> Settings:
    shutil.copyfile(store_template, settings.path(settings.store.sqlite_path))
    return settings


@pytest.fixture
def runtime(seeded: Settings) -> Runtime:
    return build_runtime(seeded)


@pytest.fixture
def fake_users(settings: Settings) -> dict:
    return yaml.safe_load(settings.path(settings.require_gateway().fake_users_file).read_text(encoding="utf-8"))


@pytest.fixture
def headers_for(settings: Settings, fake_users: dict) -> Callable[[str], dict[str, str]]:
    """Proxy headers for a fake user, as the gateway would send them."""
    group_ids = {key: gid for groups in fake_users["groups"].values() for key, gid in groups.items()}

    def make(email: str) -> dict[str, str]:
        user = next(u for u in fake_users["users"] if u["email"] == email)
        roles = settings.identity.roles_separator.join(group_ids[g] for g in user["groups"])
        return {settings.identity.email_header: email, settings.identity.roles_header: roles}

    return make


SPA_INDEX_MARKER = "kbc-access-test-spa-shell"
SPA_ASSET_NAME = "index-test.js"
SPA_ASSET_BODY = "console.log('test asset');"


@pytest.fixture
def frontend_dist(tmp_path: Path) -> Path:
    """A minimal `npm run build` output: dist/index.html and dist/assets/<one file>."""
    dist = tmp_path / "frontend-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        f'<!doctype html><html><head><script type="module" src="/assets/{SPA_ASSET_NAME}"></script></head>'
        f'<body><div id="root" data-marker="{SPA_INDEX_MARKER}"></div></body></html>',
        encoding="utf-8",
    )
    (dist / "assets" / SPA_ASSET_NAME).write_text(SPA_ASSET_BODY, encoding="utf-8")
    return dist
