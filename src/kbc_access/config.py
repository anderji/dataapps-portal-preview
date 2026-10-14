"""Settings loader.

Non-secret settings come from a TOML file whose path is given by the
KBC_ACCESS_CONFIG environment variable. Secrets come from environment
variables. Nothing has a default: a missing key fails at startup with the
key's name.

One code base runs in two modes, chosen by the config file:

* local: `[store] backend = "sqlite"` plus a `[gateway]` section (fake
  apps-proxy and IdP), secrets GATEWAY_SESSION_SECRET and MASK_SALT;
* deployed in Keboola: `[store] backend = "keboola"`, no `[gateway]`, each
  app with a `public_url`, secrets ACCESS_STORAGE_TOKEN and MASK_SALT.

The Storage token is ACCESS_STORAGE_TOKEN, not KBC_TOKEN: the Data Apps runtime
sets KBC_TOKEN itself (seen live: an app secret #KBC_TOKEN was skipped as
"already set"), so a secret under that name would silently not apply.

Which secrets are required follows from the file, so a deployed app never
needs the dev gateway's secret and a local run never needs a Storage token.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONFIG_ENV = "KBC_ACCESS_CONFIG"
# Env var (from the Data App secret #ACCESS_STORAGE_TOKEN) with the app's own Storage token.
STORAGE_TOKEN_ENV = "ACCESS_STORAGE_TOKEN"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IdentitySettings(_Strict):
    email_header: str
    roles_header: str
    roles_separator: str
    trusted_prefix: str
    hostile_headers: list[str]


class ProviderSettings(_Strict):
    label: str
    group_id_pattern: str
    example: str


class KeboolaStoreSettings(_Strict):
    stack_url: str = Field(pattern=r"^https://")
    # Full bucket id, e.g. "in.c-apps-control-plane" (the name is chosen at install time).
    bucket_id: str = Field(pattern=r"^(in|out)\.c-[A-Za-z0-9_-]+$")
    http_timeout_seconds: float = Field(gt=0)
    job_timeout_seconds: float = Field(gt=0)
    job_poll_seconds: float = Field(gt=0)
    # Storage's synchronous data-preview returns at most 1000 rows.
    preview_row_limit: int = Field(gt=0, le=1000)
    # How long list pages may reuse the last read of the tables (writes always read fresh).
    read_cache_seconds: float = Field(ge=0)


class StoreSettings(_Strict):
    backend: Literal["sqlite", "keboola"]
    sqlite_path: Path | None = None
    keboola: KeboolaStoreSettings | None = None

    @model_validator(mode="after")
    def _backend_settings_present(self) -> StoreSettings:
        if self.backend == "sqlite" and self.sqlite_path is None:
            raise ValueError("[store] backend = 'sqlite' requires sqlite_path")
        if self.backend == "keboola" and self.keboola is None:
            raise ValueError("[store] backend = 'keboola' requires a [store.keboola] section")
        return self


class UiSettings(_Strict):
    # Where "Switch user" / "Sign in" links go: the fake IdP locally, the proxy's sign-out in Keboola.
    switch_user_path: str


class DataSettings(_Strict):
    duckdb_path: Path
    dialect: str
    max_result_rows: int = Field(gt=0)
    max_query_chars: int = Field(gt=0)
    # Deployed dashboards: Storage tables pulled into duckdb_path through Direct Storage
    # Access, refreshed every refresh_seconds. Locally both are absent (seeded file).
    storage_access_tables: list[str] | None = None
    refresh_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _storage_access_complete(self) -> DataSettings:
        if (self.storage_access_tables is None) != (self.refresh_seconds is None):
            raise ValueError("[data] storage_access_tables and refresh_seconds go together")
        return self


class GateSettings(_Strict):
    cache_ttl_seconds: float = Field(ge=0)


class AuditSettings(_Strict):
    path: Path
    tail_lines: int = Field(gt=0)


class LoggingSettings(_Strict):
    path: Path
    level: str


class PolicySettings(_Strict):
    files: list[Path]


class GatewaySettings(_Strict):
    host: str
    login_path: str
    logout_path: str
    session_cookie: str
    fake_users_file: Path
    upstream_timeout_seconds: float = Field(gt=0)


class DiscoverySettings(_Strict):
    http_timeout_seconds: float = Field(gt=0)
    data_apps_component_id: str


class SeedSettings(_Strict):
    random_seed: int
    drug_sales_rows: int = Field(gt=0)
    supply_chain_rows: int = Field(gt=0)
    public_share: float = Field(ge=0, le=1)
    test_share: float = Field(ge=0, le=1)
    start_date: str
    days: int = Field(gt=0)


class AppSettings(_Strict):
    name: str
    port: int
    factory: str
    protected: bool
    # Exactly one: the dev gateway port (local) or the Data App URL (deployed).
    public_port: int | None = None
    public_url: str | None = None

    @model_validator(mode="after")
    def _one_public_address(self) -> AppSettings:
        if (self.public_port is None) == (self.public_url is None):
            raise ValueError(f"app '{self.name}' needs exactly one of public_port or public_url")
        return self


class Secrets(_Strict):
    gateway_session_secret: str | None = Field(default=None, min_length=16)
    kbc_token: str | None = Field(default=None, min_length=16)
    # URL-safe characters only, so the salt renders as a SQL literal verbatim and redaction is exact.
    mask_salt: str = Field(min_length=16, pattern=r"^[A-Za-z0-9_-]+$")


class Settings(_Strict):
    root: Path
    identity: IdentitySettings
    providers: dict[str, ProviderSettings]
    store: StoreSettings
    ui: UiSettings
    gate: GateSettings
    audit: AuditSettings
    logging: LoggingSettings
    discovery: DiscoverySettings
    apps: dict[str, AppSettings]
    secrets: Secrets
    # Only apps that read warehouse data need [data] and [policy].
    data: DataSettings | None = None
    policy: PolicySettings | None = None
    # Local development only.
    gateway: GatewaySettings | None = None
    seed: SeedSettings | None = None

    def path(self, relative: Path) -> Path:
        """Resolve a config-relative path against the project root."""
        return relative if relative.is_absolute() else self.root / relative

    def app_url(self, app_id: str) -> str:
        """Public URL of an app: its Data App URL when deployed, its gateway origin locally."""
        app = self.apps[app_id]
        if app.public_url is not None:
            return app.public_url
        if self.gateway is None:
            raise ValueError(f"app '{app_id}' has a public_port but the config has no [gateway]")
        return f"http://{self.gateway.host}:{app.public_port}/"

    def require_gateway(self) -> GatewaySettings:
        if self.gateway is None or self.secrets.gateway_session_secret is None:
            raise ValueError("This command needs the local [gateway] section and GATEWAY_SESSION_SECRET")
        return self.gateway

    def require_data(self) -> tuple[DataSettings, PolicySettings]:
        if self.data is None or self.policy is None:
            raise ValueError("This app reads warehouse data but the config has no [data] / [policy] section")
        return self.data, self.policy


def required_secrets(raw: dict) -> list[str]:
    """Secret env vars this config needs, derived from what it contains."""
    names = ["MASK_SALT"]
    if "gateway" in raw:
        names.append("GATEWAY_SESSION_SECRET")
    if raw.get("store", {}).get("backend") == "keboola":
        names.append(STORAGE_TOKEN_ENV)
    return names


def load_settings(config_path: Path, env: dict[str, str]) -> Settings:
    """Build Settings from a TOML file plus secrets taken from `env`."""
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    names = required_secrets(raw)
    missing = [name for name in names if not env.get(name)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    raw["root"] = config_path.resolve().parent.parent
    raw["secrets"] = {
        "mask_salt": env["MASK_SALT"],
        "gateway_session_secret": env.get("GATEWAY_SESSION_SECRET") if "GATEWAY_SESSION_SECRET" in names else None,
        "kbc_token": env.get(STORAGE_TOKEN_ENV) if STORAGE_TOKEN_ENV in names else None,
    }
    return Settings.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings; requires KBC_ACCESS_CONFIG to be set."""
    config = os.environ.get(CONFIG_ENV)
    if not config:
        raise ValueError(f"Missing required environment variable: {CONFIG_ENV}")
    return load_settings(Path(config), dict(os.environ))
