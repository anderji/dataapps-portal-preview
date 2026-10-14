"""Discovery of the current project's Data Apps with the app's own Storage token.

The fake API mirrors what was observed live on 2026-09-30 with a token that can
only write one bucket: `/apps` answers, the Data Apps configs answer 403.
"""

from __future__ import annotations

import httpx
import pytest

from kbc_access.discovery import (
    AUTH_PARTIAL,
    AUTH_PUBLIC,
    AUTH_REQUIRED,
    AUTH_UNKNOWN,
    NAME_FROM_CONFIG,
    NAME_FROM_SLUG,
    NAME_UNKNOWN,
    AuthProvider,
    DiscoveryError,
    authorization_of,
    data_science_url,
    discover,
    name_from_slug,
    slug_from_url,
)

STACK = "https://connection.europe-west3.gcp.keboola.com"
DS = "https://data-science.europe-west3.gcp.keboola.com"
TOKEN = "1234-storage-token-value"
COMPONENT = "keboola.data-apps"
APPS = [
    {"id": "10000001", "configId": "c-admin", "projectId": "1234", "state": "running",
     "url": "https://access-admin-10000001.hub.europe-west3.gcp.keboola.com"},
    {"id": "10000002", "configId": "c-launch", "projectId": "1234", "state": "stopped",
     "url": "https://app-launcher-10000002.hub.europe-west3.gcp.keboola.com"},
    {"id": "10000099", "configId": "c-new", "projectId": "1234", "state": "created", "url": None},
]
ISSUER = "https://login.microsoftonline.com/tenant-1/v2.0"
# Login blocks shaped like live Data App configurations (2026-10-05), secrets included to prove they are dropped.
OIDC_LOGIN = {"app_proxy": {
    "auth_providers": [{"id": "oidc-1", "type": "oidc", "client_id": "cid", "#client_secret": "KBC::ProjectSecure::x",
                        "issuer_url": ISSUER, "logout_url": ""}],
    "auth_rules": [{"type": "pathPrefix", "value": "/", "auth_required": True, "auth": ["oidc-1"]}],
}}
PUBLIC_LOGIN = {"app_proxy": {"auth_providers": [], "auth_rules": [{"type": "pathPrefix", "value": "/", "auth_required": False}]}}
CONFIGS = [
    {"id": "c-admin", "name": "Access admin", "configuration": {"authorization": OIDC_LOGIN}},
    {"id": "c-new", "name": "Sales cockpit", "configuration": {"authorization": PUBLIC_LOGIN}},
]


def transport(*, configs_status: int = 403, apps_body: object = APPS, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.headers["X-StorageApi-Token"] == TOKEN
        assert "X-KBC-ManageApiToken" not in request.headers
        if str(request.url) == f"{DS}/apps":
            return httpx.Response(200, json=apps_body)
        if str(request.url) == f"{STACK}/v2/storage/components/{COMPONENT}/configs":
            if configs_status == 200:
                return httpx.Response(200, json=CONFIGS)
            return httpx.Response(configs_status, json={"error": "You don't have access to the resource."})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def run(**kwargs: object):
    return discover(STACK, TOKEN, component_id=COMPONENT, timeout_seconds=5, transport=transport(**kwargs))  # type: ignore[arg-type]


def test_data_science_url():
    assert data_science_url(STACK) == DS
    for bad in ("https://example.com", "http://connection.keboola.com"):
        with pytest.raises(DiscoveryError):
            data_science_url(bad)


@pytest.mark.parametrize(("url", "app_id", "slug"), [
    ("https://access-admin-10000001.hub.europe-west3.gcp.keboola.com", "10000001", "access-admin"),
    ("https://x-1.hub.keboola.com/", "1", "x"),
    ("https://odd-host.hub.keboola.com", "5", "odd-host"),
])
def test_slug_from_url(url, app_id, slug):
    assert slug_from_url(url, app_id) == slug


def test_name_from_slug():
    assert name_from_slug("access-admin") == "Access admin"
    assert name_from_slug("sales_q3-report") == "Sales q3 report"
    assert name_from_slug("") == ""


def test_bucket_only_token_names_apps_from_their_slug():
    found = {a.app_id: a for a in run()}
    assert found["10000001"].name == "Access admin" and found["10000001"].name_source == NAME_FROM_SLUG
    assert found["10000001"].url == "https://access-admin-10000001.hub.europe-west3.gcp.keboola.com/"
    assert found["10000001"].project_id == "1234" and found["10000001"].config_id == "c-admin"


def test_token_that_can_read_configs_gets_real_names():
    found = {a.app_id: a for a in run(configs_status=200)}
    assert found["10000001"].name_source == NAME_FROM_CONFIG
    assert found["10000099"].name == "Sales cockpit"  # real name even before its first deploy


def test_undeployed_app_is_listed_without_url():
    new = {a.app_id: a for a in run()}["10000099"]
    assert new.url == "" and new.slug == "" and new.name == "Data App 10000099" and new.name_source == NAME_UNKNOWN


def test_results_are_sorted_by_name():
    assert [a.name for a in run()] == sorted((a.name for a in run()), key=str.lower)


def test_only_this_project_and_only_the_storage_token_are_used():
    seen: list[httpx.Request] = []
    discover(STACK, TOKEN, component_id=COMPONENT, timeout_seconds=5, transport=transport(seen=seen))
    assert {str(r.url) for r in seen} == {f"{DS}/apps", f"{STACK}/v2/storage/components/{COMPONENT}/configs"}
    assert all(r.method == "GET" for r in seen)  # read-only: no tokens created, nothing written


def test_wrapped_list_response_is_accepted():
    assert len(run(apps_body={"data": APPS})) == 3


def test_config_errors_other_than_403_fail_loudly():
    with pytest.raises(DiscoveryError, match="HTTP 500"):
        run(configs_status=500)


def test_apps_endpoint_failure_is_a_discovery_error():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid token")

    with pytest.raises(DiscoveryError, match="HTTP 401"):
        discover(STACK, TOKEN, component_id=COMPONENT, timeout_seconds=5, transport=httpx.MockTransport(handle))


def test_token_that_can_read_configs_gets_each_apps_login():
    found = {a.app_id: a for a in run(configs_status=200)}
    assert found["10000001"].auth_mode == AUTH_REQUIRED
    assert found["10000001"].auth_providers == (AuthProvider("oidc-1", "oidc", ISSUER),)
    assert found["10000099"].auth_mode == AUTH_PUBLIC and found["10000099"].auth_providers == ()
    # c-launch has no readable configuration in the listing.
    assert found["10000002"].auth_mode == AUTH_UNKNOWN


def test_bucket_only_token_cannot_tell_the_login():
    assert {a.auth_mode for a in run()} == {AUTH_UNKNOWN}
    assert all(a.auth_providers == () for a in run())


def test_password_login():
    login = {"app_proxy": {"auth_providers": [{"id": "simpleAuth", "type": "password"}],
                           "auth_rules": [{"type": "pathPrefix", "value": "/", "auth_required": True, "auth": ["simpleAuth"]}]}}
    assert authorization_of({"authorization": login}) == (AUTH_REQUIRED, (AuthProvider("simpleAuth", "password", ""),))


def test_login_on_some_paths_only_is_partial_and_lists_only_the_used_providers():
    login = {"app_proxy": {
        "auth_providers": [{"id": "simpleAuth", "type": "password"}, {"id": "gh", "type": "github"}],
        "auth_rules": [{"type": "pathPrefix", "value": "/", "auth_required": False},
                       {"type": "pathPrefix", "value": "/admin", "auth_required": True, "auth": ["gh"]}],
    }}
    assert authorization_of({"authorization": login}) == (AUTH_PARTIAL, (AuthProvider("gh", "github", ""),))


def test_configured_providers_without_a_required_rule_are_public():
    login = {"app_proxy": {"auth_providers": [{"id": "simpleAuth", "type": "password"}], "auth_rules": []}}
    assert authorization_of({"authorization": login}) == (AUTH_PUBLIC, ())


def test_configuration_without_authorization_is_public():
    assert authorization_of({}) == (AUTH_PUBLIC, ())


def test_login_never_carries_secrets():
    _, providers = authorization_of({"authorization": OIDC_LOGIN})
    assert "KBC::" not in repr(providers) and "cid" not in repr(providers)
