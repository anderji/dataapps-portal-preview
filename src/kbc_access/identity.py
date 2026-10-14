"""Layer 1 — identity.

A Principal is built from the headers the Keboola apps-proxy sets after OIDC
login, and from nothing else. The proxy strips client-sent `x-kbc-*` headers,
so those are trustworthy; `X-Forwarded-*` and similar are passed through
untouched and are therefore one `curl` away from impersonation. Their presence
is reported as hostile so the caller can audit it — they are never read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from kbc_access.config import IdentitySettings
from kbc_access.store import EMAIL_PATTERN


class IdentityError(Exception):
    """The request carries no usable identity. Maps to HTTP 401."""


@dataclass(frozen=True)
class Principal:
    email: str
    groups: frozenset[str]
    # Filled from the user_attributes table by the data guard, never from headers.
    attributes: Mapping[str, str] = field(default_factory=dict)
    # Suite-defined groups, resolved from the ACL snapshot by email (never from headers).
    local_groups: frozenset[str] = frozenset()

    def with_attributes(self, attributes: Mapping[str, str]) -> Principal:
        return Principal(
            email=self.email, groups=self.groups, attributes=dict(attributes), local_groups=self.local_groups
        )

    def with_local_groups(self, local_groups: frozenset[str]) -> Principal:
        return Principal(
            email=self.email, groups=self.groups, attributes=dict(self.attributes), local_groups=local_groups
        )


@dataclass(frozen=True)
class IdentityResult:
    principal: Principal
    # Names of untrusted identity headers present on the request (lowercased).
    hostile_headers: tuple[str, ...]


def principal_from_headers(headers: Mapping[str, str], settings: IdentitySettings) -> IdentityResult:
    """Build the principal from proxy headers; raise IdentityError when absent.

    Header lookup is case-insensitive. The email is lowercased so ACL matching
    does not depend on how the IdP capitalises it. The email header alone proves
    the request came through the proxy (it strips client-sent `x-kbc-*`), so an
    empty or missing roles header means "authenticated, no IdP groups": an IdP
    without a groups claim (Okta by default) still signs users in, and access
    then comes from direct and local-group grants.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    hostile = tuple(sorted(h for h in settings.hostile_headers if h.lower() in lowered))

    email = lowered.get(settings.email_header.lower(), "").strip().lower()
    if not EMAIL_PATTERN.fullmatch(email):
        raise IdentityError("missing or malformed user email header")

    roles = lowered.get(settings.roles_header.lower(), "")
    groups = frozenset(part.strip() for part in roles.split(settings.roles_separator) if part.strip())
    return IdentityResult(principal=Principal(email=email, groups=groups), hostile_headers=hostile)
