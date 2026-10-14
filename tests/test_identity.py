"""Layer 1: principal built only from the trusted proxy headers."""

from __future__ import annotations

import pytest

from kbc_access.config import IdentitySettings, Settings
from kbc_access.identity import IdentityError, Principal, principal_from_headers

FINANCE = "3f2b8c1e-7d4a-4e5b-9a61-2c8d0f4e1b7a"
SUPPLY = "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9"
PARTNER = "00g7pharmab0partner1"


@pytest.fixture
def ident(settings: Settings) -> IdentitySettings:
    return settings.identity


def test_basic_principal(ident: IdentitySettings) -> None:
    result = principal_from_headers(
        {"X-Kbc-User-Email": "lucie.finance@pharma-a.example", "X-Kbc-User-Roles": f"{FINANCE},{SUPPLY}"}, ident
    )
    assert result.principal == Principal("lucie.finance@pharma-a.example", frozenset({FINANCE, SUPPLY}), {})
    assert result.hostile_headers == ()


def test_headers_are_case_insensitive(ident: IdentitySettings) -> None:
    result = principal_from_headers({"x-kbc-user-email": "a@b.example", "X-KBC-USER-ROLES": PARTNER}, ident)
    assert result.principal.email == "a@b.example"
    assert result.principal.groups == frozenset({PARTNER})


def test_email_is_lowercased_and_stripped(ident: IdentitySettings) -> None:
    result = principal_from_headers({"X-Kbc-User-Email": "  Petr.Sales@Pharma-A.Example ", "X-Kbc-User-Roles": ""}, ident)
    assert result.principal.email == "petr.sales@pharma-a.example"


def test_roles_are_trimmed_and_empty_parts_dropped(ident: IdentitySettings) -> None:
    result = principal_from_headers({"X-Kbc-User-Email": "a@b.example", "X-Kbc-User-Roles": f" {FINANCE} ,, {SUPPLY},"}, ident)
    assert result.principal.groups == frozenset({FINANCE, SUPPLY})


def test_group_ids_are_not_case_folded(ident: IdentitySettings) -> None:
    # IdP identifiers are passed through verbatim; the gate matches them exactly.
    result = principal_from_headers({"X-Kbc-User-Email": "a@b.example", "X-Kbc-User-Roles": FINANCE.upper()}, ident)
    assert result.principal.groups == frozenset({FINANCE.upper()})


def test_missing_email_header(ident: IdentitySettings) -> None:
    with pytest.raises(IdentityError):
        principal_from_headers({"X-Kbc-User-Roles": FINANCE}, ident)


@pytest.mark.parametrize("email", ["", "   ", "not-an-email", "petr.sales"])
def test_malformed_email(ident: IdentitySettings, email: str) -> None:
    with pytest.raises(IdentityError):
        principal_from_headers({"X-Kbc-User-Email": email, "X-Kbc-User-Roles": ""}, ident)


@pytest.mark.parametrize("email", ["user@", "@", "@example.com"])
def test_email_without_local_part_or_domain_rejected(ident: IdentitySettings, email: str) -> None:
    with pytest.raises(IdentityError):
        principal_from_headers({"X-Kbc-User-Email": email, "X-Kbc-User-Roles": ""}, ident)


def test_missing_roles_header_is_authenticated_without_groups(ident: IdentitySettings) -> None:
    # An IdP without a groups claim (Okta by default): the proxy forwards the email only.
    result = principal_from_headers({"X-Kbc-User-Email": "a@b.example"}, ident)
    assert result.principal.groups == frozenset()
    assert result.principal.email == "a@b.example"


def test_empty_roles_header_is_authenticated_without_groups(ident: IdentitySettings) -> None:
    result = principal_from_headers({"X-Kbc-User-Email": "a@b.example", "X-Kbc-User-Roles": ""}, ident)
    assert result.principal.groups == frozenset()
    assert result.principal.email == "a@b.example"


def test_attributes_never_come_from_headers(ident: IdentitySettings) -> None:
    result = principal_from_headers(
        {
            "X-Kbc-User-Email": "a@b.example",
            "X-Kbc-User-Roles": "",
            "X-Kbc-User-Company": "PHARMA_B",
            "X-Kbc-User-Attributes": "company=PHARMA_B",
        },
        ident,
    )
    assert dict(result.principal.attributes) == {}


@pytest.mark.parametrize("hostile", ["X-Forwarded-Email", "x-forwarded-user", "X-FORWARDED-GROUPS", "X-Auth-Request-Email", "X-User-Email", "Remote-User"])
def test_hostile_headers_reported_never_used(ident: IdentitySettings, hostile: str) -> None:
    result = principal_from_headers(
        {"X-Kbc-User-Email": "petr.sales@pharma-a.example", "X-Kbc-User-Roles": "", hostile: f"lucie.finance@pharma-a.example,{FINANCE}"},
        ident,
    )
    assert result.hostile_headers == (hostile.lower(),)
    assert result.principal.email == "petr.sales@pharma-a.example"
    assert result.principal.groups == frozenset()


def test_hostile_headers_alone_do_not_authenticate(ident: IdentitySettings) -> None:
    with pytest.raises(IdentityError):
        principal_from_headers(
            {"X-Forwarded-Email": "lucie.finance@pharma-a.example", "X-Forwarded-Groups": FINANCE, "X-Kbc-User-Roles": FINANCE},
            ident,
        )


def test_all_hostile_headers_reported_sorted(ident: IdentitySettings) -> None:
    headers = {"X-Kbc-User-Email": "a@b.example", "X-Kbc-User-Roles": ""}
    headers.update({h: "x" for h in ident.hostile_headers})
    result = principal_from_headers(headers, ident)
    assert result.hostile_headers == tuple(sorted(ident.hostile_headers))


def test_with_attributes_copies() -> None:
    p = Principal("a@b.example", frozenset({"g"}))
    attrs = {"company": "PHARMA_A"}
    q = p.with_attributes(attrs)
    attrs["company"] = "PHARMA_B"
    assert q.attributes == {"company": "PHARMA_A"}
    assert q.email == p.email and q.groups == p.groups
    assert dict(p.attributes) == {}


def test_principal_has_no_local_groups_by_default() -> None:
    assert Principal("a@b.example", frozenset()).local_groups == frozenset()


def test_local_groups_never_come_from_headers(ident: IdentitySettings) -> None:
    headers = {ident.email_header: "a@b.example", ident.roles_header: "team", "x-kbc-local-groups": "team"}
    assert principal_from_headers(headers, ident).principal.local_groups == frozenset()


def test_with_attributes_preserves_local_groups_and_vice_versa() -> None:
    p = Principal("a@b.example", frozenset({"g"}), local_groups=frozenset({"team"}))
    q = p.with_attributes({"company": "PHARMA_A"})
    assert q.local_groups == frozenset({"team"}) and q.attributes == {"company": "PHARMA_A"}
    r = q.with_local_groups(frozenset({"other"}))
    assert r.local_groups == frozenset({"other"}) and r.attributes == {"company": "PHARMA_A"} and r.groups == p.groups
