# Row and column security editor on the user page

Status: decided in conversation 2026-10-07, implementation follows directly.

## Problem

Row and column security is defined only in policy YAML files shipped inside the
dashboard apps. Changing what one person may see needs a code change and a
redeploy. The admin needs to edit, per user, which rows of which dataset the
user sees and how each column is treated, from the user's page in the access
admin, with the effect visible before and after saving.

## Decisions

- Per-user rules are data in the control plane, not code. They live in a new
  table `user_data_grants` next to `user_attributes`.
- A per-user rule is one more grant of the existing policy model. It is
  evaluated by the same compiler as a policy-file grant with
  `to: {users: [<email>]}`: permissive grants union, restrictive grants
  intersect, default deny, column treatment decided per row. Nothing new in the
  semantics; the policy files stay and still apply.
- Datasets, their columns and the policy-file grants are read from the policy
  files. A dataset may declare `columns:` in the YAML. This is the catalogue the
  admin offers (the admin app has no warehouse). `load_policy` refuses a policy
  whose declared columns differ from the warehouse's, so the catalogue cannot
  drift. A dataset without `columns:` is not offered for editing.
- The admin saves the whole set of one user's rules at once (replace), so it is
  validated and written as one unit.
- Dashboards pick the rules up from the same cached ACL snapshot the gate uses
  (`cache_ttl_seconds`), so a saved rule takes effect within that window.
- A stored rule that fails validation at runtime (policy changed underneath it)
  fails closed: a permissive rule is skipped, a restrictive one matches no
  rows. A warning is logged. It never breaks the dashboard.

## Data model

```
user_data_grants(
  grant_id TEXT PRIMARY KEY,      -- 12 hex chars, generated
  email TEXT NOT NULL,            -- a registered user (lowercase)
  dataset TEXT NOT NULL,          -- key of a dataset in the policy files
  name TEXT NOT NULL,             -- label shown in the audit log as "user:<name>"
  kind TEXT NOT NULL,             -- permissive | restrictive
  row_rule TEXT NOT NULL,         -- row rule, same dialect and whitelist as policy files
  column_rules TEXT NOT NULL      -- JSON object {column: treatment}; "{}" when none
)
```

- `name`: 1-64 chars, letters, digits, space, `_`, `.`, `-`, starting with a
  letter or digit; unique per (email, dataset).
- `row_rule` is at most `DATA_GRANT_RULE_MAX_CHARS` (2000) characters and a user has at most
  `DATA_GRANT_MAX_PER_USER` (100) rules; both are named constants in `kbc_access.store`.
  `dataset` and column names are stored lowercase. A supplied `grant_id` must be 12 lowercase hex
  characters and unique within the request.
- `kind = restrictive` takes no `column_rules` (as in policy files).
- `column_rules` treatments: `clear`, `mask_partial`, `mask_hash`, `absent`.
  Columns not listed are `clear`.
- Deleting a user deletes their rules. Changing a user's email moves them.
- Unreadable stored `column_rules` (not a JSON object of strings): a permissive rule is dropped, a
  restrictive one keeps applying, with a logged warning.
- The Keboola store mirrors the table like the others (single-column key
  `grant_id`).

## Store API (`kbc_access.store`, mirrored by `KeboolaAccessStore`)

```python
@dataclass(frozen=True)
class DataGrant:
    grant_id: str
    email: str
    dataset: str
    name: str
    kind: str
    row_rule: str
    column_rules: dict[str, str]

@dataclass(frozen=True)
class DataGrantInput:
    dataset: str
    name: str
    kind: str
    row_rule: str
    column_rules: dict[str, str]
    grant_id: str | None = None      # keep the id of an existing rule; None creates one

AccessStore.list_data_grants(email: str | None = None) -> list[DataGrant]   # sorted by dataset, name
AccessStore.set_data_grants(email: str, grants: list[DataGrantInput]) -> list[DataGrant]  # replace all
```

`set_data_grants` raises `StoreError` for an unregistered user, a bad kind, name,
treatment, an empty rule, column rules on a restrictive grant, a duplicate
(dataset, name), or a `grant_id` that belongs to another user. `AclSnapshot`
gets `data_grants: dict[str, tuple[DataGrant, ...]]` keyed by email.

## Policy API (`kbc_access.policy`)

- `DatasetSpec.columns: list[str] | None`; checked against the warehouse in `load_policy`.
- `load_catalog(files) -> dict[str, CatalogDataset]` reads the policy files
  without a warehouse: `CatalogDataset(name, source, description, columns, grants)`
  with `grants` as `CatalogGrant(name, kind, to, rows, columns)`. Only datasets
  that declare `columns:` are included. Rules are parsed against those columns.
- `validate_data_grant(dataset: str, columns: Collection[str], grant: DataGrantInput | DataGrant) -> None`
  raises `PolicyError` with a readable message (unknown column, forbidden node,
  unknown placeholder syntax, SQL comments, a row rule that is not a condition such as a bare
  literal, restrictive with columns, ...). The message carries no location; callers prefix the
  rule and dataset. Used by the admin
  on save and by the guard at runtime.
- `user_grants(policy: PolicySet, records: Iterable[DataGrant]) -> dict[str, list[Grant]]`
  converts stored rules into `Grant`s (target `users=[email]`, name
  `user:<name>`), fail-closed as above; unknown datasets are ignored with a warning.
- `DataGuard` takes `user_grants: Callable[[Principal], Mapping[str, Sequence[Grant]]]`
  and appends them to the dataset's policy grants when compiling. `web.build_guard`
  supplies it from `runtime.gate.snapshot().data_grants`.
- `CompiledDataset` gets `row_filter: str`, the bound row predicate as SQL
  (no salt appears in it), for the admin's effective-access preview.

## Admin API (all under `/api`, same `access.require` + CSRF + `audit.admin`)

- `GET /data-catalog` ->
  `{"datasets": [{"name", "source", "description", "columns": [..],
  "policy_grants": [{"name", "kind", "to": {"authenticated": bool, "users": [..], "groups": [..], "local_groups": [..]}, "rows", "columns": {col: treatment}}]}],
  "treatments": ["clear", "mask_partial", "mask_hash", "absent"]}`
  Empty `datasets` when the app has no `[policy]`.
- `GET /users/{email}/data-grants` -> `{"grants": [{"grant_id", "dataset", "name", "kind", "row_rule", "column_rules"}]}`; 404 unknown user.
- `PUT /users/{email}/data-grants` body `{"grants": [{"grant_id"?: str|null, "dataset", "name", "kind", "row_rule", "column_rules"}]}`
  -> `{"message", "grants": [...]}`. 404 unknown user, 400 with a readable
  `detail` naming the rule (`Rule 'x' on 'drug_sales': ...`) for anything invalid;
  nothing is written on error. Audit action `user_data_grants_set`, target the email.
- `GET /users/{email}/data-access` -> effective access of the user's data
  grants plus policy-file grants that apply to the user (IdP groups are unknown
  to the admin, so only user, local-group and `authenticated` targets are
  evaluated, the same limit as the user matrix):
  `{"datasets": [{"name", "accessible": bool, "grants_fired": [..], "row_filter": str|null,
  "columns": [{"name", "treatment"}]}]}` where `treatment` is `clear`,
  `mask_partial`, `mask_hash`, `absent` or e.g. `clear (per row)`.
  Absent columns are listed with treatment `absent`. Inaccessible datasets have
  `row_filter` null and an empty `columns`.

## Admin SPA

On `/users/:email`, a new card "Data access (row and column security)" under
Attributes:

- One section per dataset the user has rules for, plus an "Add rule" control
  that picks a dataset. Each rule: name, kind (permissive/restrictive), row
  rule (text field with the dataset's columns and the user's attributes plus
  `:email` offered as insertable chips, and an "All rows" shortcut), and a
  column table (one select per column with the four treatments; hidden for
  restrictive rules).
- Read-only list of the policy-file grants of that dataset ("also applies from
  the policy files") so the admin sees the union.
- One "Save data access" button saves all rules (PUT). Server errors are shown
  in the card.
- "Effective access" panel below the rules: for each dataset, whether the user
  can see it, which grants fire, the row filter and the column treatments; it
  refreshes after a save.

## Deployment

The admin app bundle ships `config/policies` and a `[policy]` section so it can
read the catalogue. The dashboards need the new library code (guard) and are
redeployed with it. The new table is created in the control-plane bucket by the
installer's schema step and read by the dashboards' read token with the other
tables.

## Tests

store (CRUD, replace, validation, cascades, rename, snapshot), keboola_store
(mirror and push of the new table), policy (columns declared and checked,
catalog, validate_data_grants, user_grants fail-closed), compiler (`row_filter`),
guard (a user rule widens / narrows / masks, unknown dataset ignored), admin API
(every route: 200, 400, 404, audit, CSRF), frontend (card renders, add / edit /
remove rule, column treatments, save payload, error display, preview).

## Out of scope

Rules for local groups or IdP groups, a visual row-rule builder, editing the
policy-file grants, per-rule effective dates.
