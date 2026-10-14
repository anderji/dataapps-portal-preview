"""Discover the Data Apps of the project the admin app runs in.

Uses only the Storage token the app already has (secret #ACCESS_STORAGE_TOKEN) — no Manage
token, no other projects, no tokens created:

1. `GET https://data-science.<stack suffix>/apps` lists the project's Data Apps
   (id, configId, url, state). Verified live 2026-09-30: a token with nothing
   but write access to one bucket can call it.
2. Names live in the apps' Storage configurations
   (`GET {stack}/v2/storage/components/<data apps component>/configs`), which
   such a token may NOT read (403). If the token can, the real names are used;
   otherwise a name is derived from the URL slug and the admin can edit it
   before importing. A 403 there is expected, not an error.
3. The same configurations hold each app's login (`authorization.app_proxy`):
   which providers it has (password, OIDC, ...) and which paths require them.
   The Data Science API does not return it (checked live 2026-10-05), so when
   the configs are not readable the login is reported as unknown.

Apps that were never deployed have no URL yet; they are returned with an empty
URL so the admin sees them, and cannot be imported until they are deployed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

STORAGE_TOKEN_HEADER = "X-StorageApi-Token"
NAME_FROM_CONFIG = "configuration"
NAME_FROM_SLUG = "url slug"
NAME_UNKNOWN = "unknown"
# How an app's login is set up (DiscoveredApp.auth_mode).
AUTH_REQUIRED = "required"  # the whole app ("/") needs a login
AUTH_PARTIAL = "partial"  # some paths need a login, "/" does not
AUTH_PUBLIC = "public"  # no path needs a login: anyone with the URL gets in
AUTH_UNKNOWN = "unknown"  # the configuration could not be read


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthProvider:
    """One login provider of an app, without its secrets."""

    provider_id: str
    # The Keboola provider type as configured: "password", "oidc", "github", ...
    type: str
    # The OIDC issuer, empty for providers that have none (e.g. password).
    issuer_url: str


@dataclass(frozen=True)
class DiscoveredApp:
    app_id: str
    config_id: str
    project_id: str
    name: str
    # Where the name came from: NAME_FROM_CONFIG, NAME_FROM_SLUG or NAME_UNKNOWN.
    name_source: str
    slug: str
    url: str
    state: str
    auth_mode: str = AUTH_UNKNOWN
    # The providers the login rules use (all configured ones when no rule names any).
    auth_providers: tuple[AuthProvider, ...] = ()


def authorization_of(configuration: dict) -> tuple[str, tuple[AuthProvider, ...]]:
    """(auth mode, providers) from a Data App configuration's `authorization.app_proxy`.

    Secret fields ("#client_secret") are never copied. A rule protects its path
    when `auth_required` is true; the app is fully protected when the "/" rule is.
    """
    proxy = (configuration.get("authorization") or {}).get("app_proxy") or {}
    rules = [r for r in proxy.get("auth_rules") or [] if isinstance(r, dict)]
    required = [r for r in rules if r.get("auth_required")]
    if not required:
        mode = AUTH_PUBLIC
    elif any(r.get("type") == "pathPrefix" and r.get("value") == "/" for r in required):
        mode = AUTH_REQUIRED
    else:
        mode = AUTH_PARTIAL
    used = {str(i) for r in required for i in r.get("auth") or []}
    providers = tuple(
        AuthProvider(str(p.get("id") or ""), str(p.get("type") or ""), str(p.get("issuer_url") or ""))
        for p in proxy.get("auth_providers") or []
        if isinstance(p, dict) and (not used or str(p.get("id")) in used)
    )
    return mode, providers if mode != AUTH_PUBLIC else ()


def data_science_url(stack_url: str) -> str:
    """`https://connection.X` → `https://data-science.X`."""
    parsed = urlparse(stack_url.rstrip("/"))
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.startswith("connection."):
        raise DiscoveryError(f"Stack URL must be https://connection.… (got {stack_url})")
    return f"https://data-science.{host.removeprefix('connection.')}"


def slug_from_url(url: str, app_id: str) -> str:
    """`https://access-admin-10000001.hub.…` → `access-admin` (empty when there is no URL)."""
    first_label = (urlparse(url).hostname or "").split(".")[0]
    suffix = f"-{app_id}"
    return first_label[: -len(suffix)] if first_label.endswith(suffix) else first_label


def name_from_slug(slug: str) -> str:
    words = re.sub(r"[-_]+", " ", slug).strip()
    return words[:1].upper() + words[1:] if words else ""


def _check(response: httpx.Response, what: str) -> httpx.Response:
    if response.status_code >= 400:
        raise DiscoveryError(f"{what} failed: HTTP {response.status_code} {response.text[:200]}")
    return response


def discover(
    stack_url: str,
    storage_token: str,
    *,
    component_id: str,
    timeout_seconds: float,
    transport: httpx.BaseTransport | None = None,
) -> list[DiscoveredApp]:
    """All Data Apps of the token's project, sorted by name."""
    stack = stack_url.rstrip("/")
    headers = {STORAGE_TOKEN_HEADER: storage_token}
    with httpx.Client(timeout=timeout_seconds, transport=transport, headers=headers) as client:
        body = _check(client.get(f"{data_science_url(stack)}/apps"), "Listing the project's Data Apps").json()
        deployed = (body.get("data") or body.get("apps") or []) if isinstance(body, dict) else body
        if not isinstance(deployed, list):
            raise DiscoveryError("Unexpected response from the Data Science API")
        names: dict[str, str] = {}
        auth: dict[str, tuple[str, tuple[AuthProvider, ...]]] = {}
        configs = client.get(f"{stack}/v2/storage/components/{component_id}/configs")
        if configs.status_code == 200:
            for c in configs.json():
                names[str(c["id"])] = str(c.get("name") or "")
                auth[str(c["id"])] = authorization_of(c.get("configuration") or {})
        elif configs.status_code != 403:
            _check(configs, "Reading Data App names")
    found: list[DiscoveredApp] = []
    for app in deployed:
        app_id, config_id = str(app["id"]), str(app.get("configId") or "")
        url = str(app.get("url") or "")
        slug = slug_from_url(url, app_id) if url else ""
        if names.get(config_id):
            name, source = names[config_id], NAME_FROM_CONFIG
        elif slug:
            name, source = name_from_slug(slug), NAME_FROM_SLUG
        else:
            name, source = f"Data App {app_id}", NAME_UNKNOWN
        auth_mode, auth_providers = auth.get(config_id, (AUTH_UNKNOWN, ()))
        found.append(
            DiscoveredApp(
                app_id=app_id, config_id=config_id, project_id=str(app.get("projectId") or ""), name=name,
                name_source=source, slug=slug, url=url.rstrip("/") + "/" if url else "", state=str(app.get("state") or ""),
                auth_mode=auth_mode, auth_providers=auth_providers,
            )
        )
    return sorted(found, key=lambda a: a.name.lower())
