# Local groups

Status: approved in conversation 2026-10-05, implementation follows directly.

## Problem

Groups reach the suite only from the IdP (Entra / Okta) as external ids in the
`X-Kbc-User-Roles` header, and an admin registers a mapping row per IdP group.
The IdP groups are too coarse to express who may open which app or see which
rows and columns. The admin needs groups defined inside the suite, with a flat
many-to-many relation between users and groups.

## Decisions

- Local groups are a flat set of users. No nesting, and a local group never
  references an IdP group as a source of members.
- A user can be in any number of local groups; a local group has any number of
  members.
- IdP groups stay as a parallel, independent grant subject. A grant (app gate)
  or a policy target (data guard) can name a user, an IdP group, or a local
  group. Access is the union, exactly as today.
- Local groups apply to both the app gate and the data guard.
- A member must already be registered in `users`. Adding an unregistered email
  is rejected (HTTP 422). Registration stays a deliberate admin step.
- Login is untouched: the proxy still delivers identity and IdP groups through
  headers. Local groups are resolved from the ACL snapshot by email, never from
  headers.
- The existing "Groups" feature is renamed "IdP groups" in the UI and API docs
  to avoid confusion. Its table, routes and behaviour do not change.

## Data model (additive)

```
local_groups(local_group_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '')
local_group_members(local_group_id TEXT NOT NULL, email TEXT NOT NULL, PRIMARY KEY(local_group_id, email))
```

- `local_group_id` is a slug: lowercase letters, digits, `-` and `_`, 1-64
  chars, unique. It lives in its own namespace, so it cannot collide with IdP
  external ids or internal IdP `group_id`s.
- `app_grants.subject_type` accepts a third value `local_group`; `subject_id` is
  the `local_group_id`. Granting to an unknown local group is rejected.
- Deleting a user removes their memberships. Deleting a local group removes its
  memberships and its grants. Deactivating a user keeps memberships; the
  `user_deactivated` kill switch still overrides every grant.
- Schema creation is `CREATE TABLE IF NOT EXISTS`, so existing stores upgrade in
  place. `keboola_store.py` mirrors and pushes the two new tables like the
  existing ones; the Storage tables are created on first use.

## Evaluation

- `Principal` gains `local_groups: frozenset[str]` (default empty), filled from
  the ACL snapshot by the code that builds the request principal (the same
  snapshot and cache TTL the gate uses). `with_attributes` preserves it.
- `AclSnapshot` exposes `local_groups` and the member map (email to set of
  local group ids).
- Gate: matched grants are the union of `user:<email>`, `group:<group_id>` (via
  IdP external ids) and `local_group:<local_group_id>`. Reason codes are
  unchanged; the `matched` tuple now may contain `local_group:<id>` entries.
- Data guard: `GrantTarget` (`policy.py`) gains `local_groups: list[str]`, valid
  next to `authenticated`, `users`, `groups`. `applies_to` is true when the
  principal's `local_groups` intersect it. Permissive grants still union and
  restrictive grants still intersect. An unknown local group id in a policy file
  does not fail loading (the store fills at runtime); it logs a warning and
  matches nobody.
- The launcher's "What the launcher knows about you" table also lists the
  user's local groups.

## Admin API (all under `/api`, same `access.require` + CSRF header + `audit.admin` as the rest)

- `GET /local-groups` -> `[{local_group_id, name, description, members: [email], member_count}]`
- `POST /local-groups` body `{local_group_id, name, description?}` -> 201; 409 if it exists; 422 on a bad slug
- `GET /local-groups/{id}` -> one group with members; 404 if unknown
- `PUT /local-groups/{id}` body `{name, description?}`
- `DELETE /local-groups/{id}` -> removes memberships and grants
- `PUT /local-groups/{id}/members/{email}` -> adds; 422 `user must be registered first` when the email is not in `users`; idempotent
- `DELETE /local-groups/{id}/members/{email}`
- `POST /grants` and `POST /grants/revoke` accept `subject_type: "local_group"`
- `GET /users/{email}` additionally returns `local_groups: [{local_group_id, name}]`
- `GET /overview` includes a `local_groups` count and the local groups in the access matrix
- `POST /simulate` resolves the local groups of the given email from the store and reports `local_group:<id>` in `matched`

## Admin SPA

- New route `/local-groups` (list + create) and `/local-groups/:localGroupId` (edit name/description, add and remove members via a picker of registered users, show the groups' grants).
- Nav: "Groups" is renamed "IdP groups"; "Local groups" is added next to it.
- User detail shows the user's local groups with links.
- The grants page offers local groups as a subject type.
- Built on the existing Keboola design system components and API client patterns.

## Tests

- store: create/rename/delete group, add/remove member, unregistered email rejected, cascades on user and group delete, grant to unknown local group rejected, slug validation
- gate: grant via local group allows, union with user and IdP grants, kill switch overrides, non-member denied
- policy/compiler/guard: `local_groups` target applies, restrictive intersection, unknown id warns and matches nobody
- keboola_store: new tables mirrored and pushed
- admin API: every route above, including 409, 422, 404 and the audit line
- identity/web: principal carries resolved local groups
- frontend (vitest): new pages, nav rename, grants page subject type

## Out of scope

Bulk import of users or members, nested groups, syncing members from the IdP,
removing the IdP group feature.
