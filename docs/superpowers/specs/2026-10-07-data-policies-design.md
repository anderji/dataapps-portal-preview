# Data policies: reusable row and column security, assigned in bulk

Status: decided in conversation 2026-10-07. Supersedes the per-user storage, API
and SPA parts of `2026-10-07-user-data-rules-design.md` (that spec's catalogue,
rule validation, fail-closed behaviour, `row_filter` and effective-access
preview stay as built).

## Problem

Per-user rules do not scale: the same masking has to be re-entered for every
person. The admin wants a named policy ("PII masked", "Own company only") defined
once and assigned to many users, local groups and IdP groups at a time.

## Decisions

- A **data policy** is a named, reusable set of rules. Each rule is what a
  per-user rule was: a dataset, a kind (permissive or restrictive), a row rule
  and column treatments. Rule validation is unchanged (`validate_data_grant`).
- A policy is **assigned** to subjects: a user, a local group or an IdP group
  (the same three subject types as app grants). A principal gets the rules of
  every policy assigned to the user, to any local group the user is in, or to any
  IdP group the user carries. Evaluation is the existing model: permissive rules
  union, restrictive rules intersect, default deny, column treatment decided per
  row. The policy YAML files stay and still apply.
- Per-user rules are **removed**: table `user_data_grants`, its store API, its
  admin routes and its SPA card go away. (Nothing was stored in the test project:
  the table is only created on a first write.)
- The same fail-closed behaviour as before for a stored rule that no longer
  validates (permissive skipped, restrictive matches nothing, warning logged).
- Audit: a fired rule is named `policy:<policy_id>` in `grants_fired`.

## Data model (three new tables)

```
data_policies(
  policy_id TEXT PRIMARY KEY,     -- slug, same pattern as local_group_id: [a-z0-9_-]{1,64}
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT ''
)
data_policy_rules(
  rule_id TEXT PRIMARY KEY,       -- 12 lowercase hex chars, generated
  policy_id TEXT NOT NULL,
  dataset TEXT NOT NULL,          -- lowercase key of a dataset in the policy files
  kind TEXT NOT NULL,             -- permissive | restrictive
  row_rule TEXT NOT NULL,
  column_rules TEXT NOT NULL      -- JSON {column: treatment}, "{}" when none
)
data_policy_assignments(
  policy_id TEXT NOT NULL,
  subject_type TEXT NOT NULL,     -- user | group | local_group
  subject_id TEXT NOT NULL,       -- user email | IdP group_id | local_group_id (as in app_grants)
  PRIMARY KEY (policy_id, subject_type, subject_id)
)
```

- Limits are named constants in `kbc_access.store`: rule text
  `DATA_GRANT_RULE_MAX_CHARS` (2000, reuse), rules per policy
  `DATA_POLICY_MAX_RULES` (50), policies `DATA_POLICY_MAX_COUNT` (200).
  `kind = restrictive` takes no `column_rules`. Treatments as before.
- Cascades: deleting a policy deletes its rules and assignments; deleting a user,
  a local group or an IdP group deletes the assignments naming it; changing a
  user's email moves their assignments. An assignment to an unknown subject or
  policy is refused with `StoreError`.
- Keboola store: mirrored like the other tables. `data_policy_assignments` has a
  composite key (`row_key`, as `app_grants`); the other two have single keys. Old
  buckets lack the tables: they read as empty and are created on the first write.
- The installer's control-plane grants cover `TABLE_COLUMNS`, so the new tables
  are included automatically; the `user_data_grants` entry disappears.

## Store API (`kbc_access.store`, mirrored by `KeboolaAccessStore`)

```python
@dataclass(frozen=True)
class DataPolicy:  policy_id: str; name: str; description: str
@dataclass(frozen=True)
class DataPolicyRule:  rule_id: str; policy_id: str; dataset: str; kind: str; row_rule: str; column_rules: dict[str, str]
@dataclass(frozen=True)
class DataPolicyRuleInput:  dataset: str; kind: str; row_rule: str; column_rules: dict[str, str]; rule_id: str | None = None
@dataclass(frozen=True)
class DataPolicyAssignment:  policy_id: str; subject_type: str; subject_id: str

create_data_policy(policy_id, name, description="") -> DataPolicy        # StoreError if exists / bad slug
update_data_policy(policy_id, *, name, description="") -> DataPolicy
delete_data_policy(policy_id) -> None                                     # cascades
list_data_policies() -> list[DataPolicy]                                  # sorted by name
set_data_policy_rules(policy_id, rules: list[DataPolicyRuleInput]) -> list[DataPolicyRule]   # replace all
list_data_policy_rules(policy_id: str | None = None) -> list[DataPolicyRule]
set_policy_assignments(subject_type, subject_id, policy_ids: set[str]) -> tuple[set[str], set[str]]  # replace one subject's policies; returns (added, removed)
set_subject_assignments_for_policy(policy_id, subjects: set[tuple[str, str]]) -> tuple[set, set]     # replace one policy's subjects
list_data_policy_assignments() -> list[DataPolicyAssignment]
```

`AclSnapshot` gets `data_policies: dict[str, DataPolicy]`,
`data_policy_rules: dict[str, tuple[DataPolicyRule, ...]]` (by policy id) and
`data_policy_assignments: tuple[DataPolicyAssignment, ...]`, plus a method
`policies_of(principal) -> frozenset[str]`: ids assigned to the principal's email,
to its local groups, or to the internal ids of the IdP groups its external ids
map to (`groups_by_external_id`), as the gate does for app grants.

## Policy API (`kbc_access.policy`)

- Keep `load_catalog`, `validate_data_grant` (accept a `DataPolicyRuleInput` or
  `DataPolicyRule`; messages unchanged).
- Replace `user_grants` by
  `policy_grants(policy: PolicySet, policy_ids: Iterable[str], snapshot_rules: Mapping[str, Sequence[DataPolicyRule]]) -> dict[str, list[Grant]]`:
  one `Grant` per rule, name `policy:<policy_id>`, target `authenticated=True`
  (assignment was already resolved for this principal), fail-closed as before.
- `DataGuard` takes `policy_grants: Callable[[Principal], Mapping[str, Sequence[Grant]]]`
  (the existing `user_grants` parameter is renamed); `web.build_guard` supplies it
  from `runtime.gate.snapshot()` and `snapshot.policies_of(principal)`. The
  principal passed in already carries `local_groups`; IdP groups are in
  `principal.groups`.

## Admin API (under `/api`, same gate + CSRF + `audit.admin`)

- `GET /data-catalog` unchanged.
- `GET /data-policies` -> `{"policies": [{"policy_id","name","description","rule_count","assignments": [{"subject_type","subject_id"}]}]}`
- `POST /data-policies` body `{policy_id, name, description?}` -> 201 `{message, policy_id}`; 409 exists; 400 bad slug.
- `GET /data-policies/{id}` -> `{"policy": {policy_id,name,description}, "rules": [{"rule_id","dataset","kind","row_rule","column_rules"}], "assignments": [{subject_type, subject_id, label}]}`; 404 unknown. `label` is the user's display name or email, the group's name, the local group's name.
- `PUT /data-policies/{id}` body `{name, description?, rules: [{rule_id?, dataset, kind, row_rule, column_rules}], assignments: [{subject_type, subject_id}]}`
  saves name, rules and assignments as one unit -> `{message, ...same shape as GET}`.
  404 unknown policy, 400 with a readable `detail` (`Rule 2 on 'drug_sales': ...`,
  `Unknown user 'x'`) and nothing written. Audit `data_policy_update`.
- `DELETE /data-policies/{id}` -> audit `data_policy_delete`; 404 unknown.
- `GET /data-policy-assignments/{subject_type}/{subject_id}` -> `{"policy_ids": [..], "policies": [{policy_id, name, rule_count}]}` where `policies` lists all existing policies (so a page can show a checkbox list); 404 unknown subject.
- `PUT /data-policy-assignments/{subject_type}/{subject_id}` body `{"policy_ids": [..]}` -> `{message}`; audit `data_policy_assign`, target `<subject_type>:<subject_id>`.
- `GET /users/{email}/data-access` stays (effective access), now including
  rules from policies assigned to the user directly or via local groups (IdP
  groups are unknown to the admin: say so in the response field
  `"note": str|null`, set when some assigned policies come from IdP groups the
  admin cannot resolve for this user - leave null otherwise).
- Remove `GET/PUT /users/{email}/data-grants`.

## Admin SPA

- Nav item "Data policies" (next to Local groups): `/data-policies` lists
  policies with rule and assignment counts and a create form (id, name,
  description); `/data-policies/:policyId` edits a policy: name and description,
  the rule editor (the existing per-dataset rule UI, reused: kind, row rule with
  column and `:attribute` chips, "All rows", column treatments, read-only
  policy-file grants of the dataset), and an "Assigned to" section with pickers
  for users, local groups and IdP groups. One "Save policy" button (PUT).
- User page, local group page and IdP group page get a card "Data policies":
  a checkbox list of all policies for that subject, saved with its own button via
  `PUT /data-policy-assignments/...`. The user page keeps the "Effective access"
  panel and drops the per-user rule editor.
- Reuse and move code from `DataAccessCard.tsx` instead of duplicating it.

## Deployment

Same as before: admin and both dashboards are redeployed with `install.py
redeploy`. The three new tables are created on the admin's first write.

## Tests

store (CRUD, replace semantics, validation, cascades on policy / user / local
group / IdP group delete, email rename, snapshot, `policies_of` across user, local
group and IdP group), keboola_store (mirror and push of the three tables, old
bucket without them), policy (`policy_grants` fail-closed), guard (a policy
assigned via each subject type widens / narrows / masks; unassigned policy does
not apply), admin API (every route: 200, 400, 404, 409, audit, CSRF), frontend
(policies list and create, policy editor, assignments, subject cards, effective
access).

## Out of scope

Policy inheritance or nesting, per-assignment overrides, rule effective dates,
editing the policy-file grants.

## Implementation decisions (backend)

Details the spec left open, as built:

- `save_data_policy(policy_id, *, name, description, rules, subjects)` exists on both stores (not in
  the list above): name, rules and assignments of one policy are written in one transaction, so
  `PUT /data-policies/{id}` is all-or-nothing. It returns `(policy, rules, (added, removed))`.
- A rule error from the store is numbered like the admin's: `Rule 2 on 'drug_sales': ...` (1-based
  position in the submitted list). Rules have no name.
- Rule ids that exist nowhere are accepted if they are 12 lowercase hex characters; an id that belongs
  to another policy, or is used twice in one request, is refused.
- `set_policy_assignments` / `set_subject_assignments_for_policy` refuse unknown subjects and unknown
  policies with `StoreError` and write nothing; a user id is lowercased. `list_data_policy_rules` is sorted
  by policy, dataset, kind, rule id.
- `AclSnapshot.policies_of(principal)` resolves local groups from the snapshot membership and from
  `principal.local_groups` (union), and IdP groups through `groups_by_external_id`.
- Snapshot only carries rules and assignments of policies that exist. An unreadable `column_rules`
  drops a permissive rule and keeps a restrictive one (with a warning).
- `policy_grants` applies policies in sorted id order, so `grants_fired` is deterministic; a policy id
  without stored rules adds nothing.
- `GET /users/{email}/data-access` adds `"note"`: set when at least one policy is assigned to an IdP
  group and the user does not already get that policy another way (directly or via a local group);
  null otherwise. `GET /data-policy-assignments/...` answers 404 for an unknown subject, including an
  unknown subject type; `PUT` answers 400 for an unknown policy id.
- Limits: `DATA_POLICY_MAX_RULES` and `DATA_POLICY_MAX_COUNT` as specified; the old per-user limit and
  the rule `name` are gone.
