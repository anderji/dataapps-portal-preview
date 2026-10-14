"""Config modes: local (sqlite + [gateway]) vs deployed in Keboola (keboola store, public_url)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kbc_access.config import Settings, load_settings, required_secrets
from kbc_access.keboola_store import KeboolaAccessStore
from kbc_access.store import AccessStore
from kbc_access.web import build_store

MASK_SALT = "test-mask-salt-0123456789abcdef"
KBC_TOKEN = "test-kbc-token-0123456789abcdef"
DEPLOYED_SECRETS = {"MASK_SALT": MASK_SALT, "ACCESS_STORAGE_TOKEN": KBC_TOKEN}

BASE = """
[identity]
email_header = "X-Kbc-User-Email"
roles_header = "X-Kbc-User-Roles"
roles_separator = ","
trusted_prefix = "x-kbc-"
hostile_headers = ["x-forwarded-email"]

[providers.entra]
label = "Microsoft Entra ID"
group_id_pattern = "^[0-9a-fA-F-]{36}$"
example = "3f2b8c1e-7d4a-4e5b-9a61-2c8d0f4e1b7a"

[ui]
switch_user_path = "/_proxy/sign_out"

[gate]
cache_ttl_seconds = 5

[audit]
path = "logs/audit.jsonl"
tail_lines = 200

[logging]
path = "logs/app.log"
level = "INFO"

[discovery]
http_timeout_seconds = 30
data_apps_component_id = "keboola.data-apps"
"""

KEBOOLA_STORE = """
[store]
backend = "keboola"

[store.keboola]
stack_url = "https://connection.keboola.com"
bucket_id = "{bucket_id}"
http_timeout_seconds = 30
job_timeout_seconds = 120
job_poll_seconds = 1
preview_row_limit = 1000
read_cache_seconds = 0
"""

DEPLOYED_APPS = """
[apps.launcher]
name = "App launcher"
port = 8701
factory = "apps.launcher.main:create_default"
protected = false
public_url = "https://launcher-123.hub.keboola.com/"

[apps.admin]
name = "Access admin"
port = 8702
factory = "apps.admin.main:create_default"
protected = true
public_url = "https://admin-456.hub.keboola.com/"
"""

GATEWAY = """
[gateway]
host = "127.0.0.1"
login_path = "/_dev/login"
logout_path = "/_dev/logout"
session_cookie = "kbc_dev_session"
fake_users_file = "config/fake_users.yaml"
upstream_timeout_seconds = 30
"""


def _write(tmp_path: Path, text: str) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "deployed.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _deployed(tmp_path: Path, *, bucket_id: str = "in.c-apps-control-plane", apps: str = DEPLOYED_APPS,
              store: str | None = None, extra: str = "") -> Path:
    store_text = store if store is not None else KEBOOLA_STORE.format(bucket_id=bucket_id)
    return _write(tmp_path, BASE + store_text + apps + extra)


# ---- deployed mode -----------------------------------------------------------------


def test_deployed_config_loads_with_only_mask_salt_and_token(tmp_path: Path) -> None:
    settings = load_settings(_deployed(tmp_path), DEPLOYED_SECRETS)
    assert settings.store.backend == "keboola"
    assert settings.store.keboola is not None and settings.store.keboola.bucket_id == "in.c-apps-control-plane"
    assert settings.secrets.kbc_token == KBC_TOKEN
    assert settings.secrets.gateway_session_secret is None
    assert settings.gateway is None and settings.data is None and settings.policy is None and settings.seed is None
    assert settings.root == tmp_path


def test_deployed_config_needs_exactly_mask_salt_and_kbc_token(tmp_path: Path) -> None:
    import tomllib

    raw = tomllib.loads(_deployed(tmp_path).read_text(encoding="utf-8"))
    assert required_secrets(raw) == ["MASK_SALT", "ACCESS_STORAGE_TOKEN"]


def test_missing_kbc_token_is_named(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ACCESS_STORAGE_TOKEN") as info:
        load_settings(_deployed(tmp_path), {"MASK_SALT": MASK_SALT})
    assert "GATEWAY_SESSION_SECRET" not in str(info.value)


def test_empty_kbc_token_counts_as_missing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ACCESS_STORAGE_TOKEN"):
        load_settings(_deployed(tmp_path), {**DEPLOYED_SECRETS, "ACCESS_STORAGE_TOKEN": ""})


def test_short_kbc_token_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_settings(_deployed(tmp_path), {**DEPLOYED_SECRETS, "ACCESS_STORAGE_TOKEN": "short"})


def test_gateway_secret_ignored_without_gateway_section(tmp_path: Path) -> None:
    settings = load_settings(_deployed(tmp_path), {**DEPLOYED_SECRETS, "GATEWAY_SESSION_SECRET": "x" * 32})
    assert settings.secrets.gateway_session_secret is None


def test_gateway_section_makes_its_secret_required(tmp_path: Path) -> None:
    path = _deployed(tmp_path, extra=GATEWAY)
    with pytest.raises(ValueError, match="GATEWAY_SESSION_SECRET"):
        load_settings(path, DEPLOYED_SECRETS)
    settings = load_settings(path, {**DEPLOYED_SECRETS, "GATEWAY_SESSION_SECRET": "s" * 32})
    assert settings.require_gateway().host == "127.0.0.1"


def test_sqlite_backend_does_not_need_kbc_token(tmp_path: Path) -> None:
    store = '[store]\nbackend = "sqlite"\nsqlite_path = "data/access.sqlite"\n'
    settings = load_settings(_deployed(tmp_path, store=store), {"MASK_SALT": MASK_SALT})
    assert settings.secrets.kbc_token is None
    assert settings.store.sqlite_path == Path("data/access.sqlite")


# ---- store section validation ---------------------------------------------------------


def test_sqlite_backend_without_path_fails(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="sqlite_path"):
        load_settings(_deployed(tmp_path, store='[store]\nbackend = "sqlite"\n'), {"MASK_SALT": MASK_SALT})


def test_keboola_backend_without_section_fails(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=r"\[store.keboola\]"):
        load_settings(_deployed(tmp_path, store='[store]\nbackend = "keboola"\n'), DEPLOYED_SECRETS)


def test_unknown_backend_fails(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="backend"):
        load_settings(_deployed(tmp_path, store='[store]\nbackend = "postgres"\n'), {"MASK_SALT": MASK_SALT})


@pytest.mark.parametrize(
    "bucket_id",
    ["apps-control-plane", "c-apps", "sys.c-apps", "in.apps", "in.c-", "in.c-apps.extra", "in.c-a b", "IN.c-apps"],
)
def test_bad_bucket_id_fails(tmp_path: Path, bucket_id: str) -> None:
    with pytest.raises(ValidationError, match="bucket_id"):
        load_settings(_deployed(tmp_path, bucket_id=bucket_id), DEPLOYED_SECRETS)


@pytest.mark.parametrize("bucket_id", ["in.c-apps-control-plane", "out.c-acl_2"])
def test_good_bucket_ids(tmp_path: Path, bucket_id: str) -> None:
    assert load_settings(_deployed(tmp_path, bucket_id=bucket_id), DEPLOYED_SECRETS).store.keboola.bucket_id == bucket_id


def test_non_https_stack_url_fails(tmp_path: Path) -> None:
    text = KEBOOLA_STORE.format(bucket_id="in.c-acl").replace("https://", "http://")
    with pytest.raises(ValidationError, match="stack_url"):
        load_settings(_deployed(tmp_path, store=text), DEPLOYED_SECRETS)


# ---- apps -----------------------------------------------------------------------------------


APP_BOTH = """
[apps.admin]
name = "Access admin"
port = 8702
factory = "apps.admin.main:create_default"
protected = true
public_port = 8802
public_url = "https://admin-456.hub.keboola.com/"
"""

APP_NEITHER = """
[apps.admin]
name = "Access admin"
port = 8702
factory = "apps.admin.main:create_default"
protected = true
"""


@pytest.mark.parametrize("apps", [APP_BOTH, APP_NEITHER], ids=["both", "neither"])
def test_app_needs_exactly_one_public_address(tmp_path: Path, apps: str) -> None:
    with pytest.raises(ValidationError, match="exactly one of public_port or public_url"):
        load_settings(_deployed(tmp_path, apps=apps), DEPLOYED_SECRETS)


def test_app_url_returns_public_url(tmp_path: Path) -> None:
    settings = load_settings(_deployed(tmp_path), DEPLOYED_SECRETS)
    assert settings.app_url("admin") == "https://admin-456.hub.keboola.com/"
    assert settings.app_url("launcher") == "https://launcher-123.hub.keboola.com/"


def test_app_url_with_public_port_but_no_gateway_fails(tmp_path: Path) -> None:
    apps = APP_NEITHER + "public_port = 8802\n"
    settings = load_settings(_deployed(tmp_path, apps=apps), DEPLOYED_SECRETS)
    with pytest.raises(ValueError, match=r"no \[gateway\]"):
        settings.app_url("admin")


# ---- optional sections ------------------------------------------------------------------------


def test_require_gateway_and_data_raise_clear_errors_when_absent(tmp_path: Path) -> None:
    settings = load_settings(_deployed(tmp_path), DEPLOYED_SECRETS)
    with pytest.raises(ValueError, match=r"\[gateway\].*GATEWAY_SESSION_SECRET"):
        settings.require_gateway()
    with pytest.raises(ValueError, match=r"\[data\] / \[policy\]"):
        settings.require_data()


def test_require_data_needs_both_data_and_policy(tmp_path: Path) -> None:
    data = '\n[data]\nduckdb_path = "data/w.duckdb"\ndialect = "duckdb"\nmax_result_rows = 5\nmax_query_chars = 100\n'
    settings = load_settings(_deployed(tmp_path, extra=data), DEPLOYED_SECRETS)
    with pytest.raises(ValueError, match="policy"):
        settings.require_data()
    policy = '\n[policy]\nfiles = ["config/policies/x.yaml"]\n'
    settings = load_settings(_deployed(tmp_path, extra=data + policy), DEPLOYED_SECRETS)
    data_settings, policy_settings = settings.require_data()
    assert data_settings.max_result_rows == 5 and policy_settings.files == [Path("config/policies/x.yaml")]


def test_local_config_still_requires_gateway_secret(settings: Settings) -> None:
    # The shipped local config keeps working through the conftest fixture.
    assert settings.store.backend == "sqlite"
    assert settings.require_gateway().host == "127.0.0.1"


# ---- build_store ------------------------------------------------------------------------------------


def test_build_store_returns_keboola_store_for_deployed_config(tmp_path: Path) -> None:
    settings = load_settings(_deployed(tmp_path), DEPLOYED_SECRETS)
    store = build_store(settings)  # constructing it makes no network call
    assert isinstance(store, KeboolaAccessStore)
    assert store.table_id("apps") == "in.c-apps-control-plane.apps"
    assert set(store.providers) == {"entra"}


def test_build_store_returns_sqlite_store_for_sqlite_config(tmp_path: Path) -> None:
    store_text = '[store]\nbackend = "sqlite"\nsqlite_path = "data/access.sqlite"\n'
    settings = load_settings(_deployed(tmp_path, store=store_text), {"MASK_SALT": MASK_SALT})
    store = build_store(settings)
    assert isinstance(store, AccessStore)
    assert (tmp_path / "data" / "access.sqlite").is_file()
