# Dataset security editor: row access, column masking, test as a user

Status: decided in conversation 2026-10-07. Supersedes
`2026-10-07-data-policies-design.md` and the per-user parts of
`2026-10-07-user-data-rules-design.md`. Data policies are removed.

## Problem

Setting up row and column security is hard to read and easy to get wrong:

- rules live in two places (policy YAML, invisible in the admin, and data policies);
- one rule couples rows with masking, so a masking-only rule with `TRUE` widens rows;
- permissive/restrictive vocabulary and union/intersection are not intuitive;
- a column can be clear on some rows and masked on others, which is hard to explain
  and broke SQL (`CASE` mixing types, `SUM` over text);
- row rules are free SQL with attribute values nobody checks against the data
  (`Pharma A s.r.o.` vs `PHARMA_A`);
- there is no way to see what a user will actually get.

## Decisions

- One editor per dataset with two blocks, modelled on Snowflake row access and
  masking policies:
  - **Who sees which rows**: an ordered list of lines `who -> which rows`. Lines
    union. "Always hide rows where ..." lines remove rows for their subject.
  - **Column masking**: per column an ordered list `who -> treatment`; the first
    matching line wins; no matching line means clear.
- Masking never adds rows and is decided **per user for the whole column** (no
  per-row masking). A clear numeric column stays numeric.
- Row filters are structured conditions built from dropdowns, never free SQL.
- `who` is a subject: `everyone` (every signed-in user), `user` (email),
  `local_group` (local_group_id), `group` (IdP group, internal group_id). Bulk
  setup works through groups, as before.
- "Test as": pick a user (plus IdP groups the admin cannot know) and see which
  lines apply and why, rows visible of total, column treatments, warnings, and a
  preview of real rows.
- The admin app reads the demo data read-only (Direct Storage Access, pulled into
  DuckDB like the dashboards) for the preview, counts, value suggestions and
  warnings.
- The policy YAML keeps only the dataset catalogue plus *example rules* in the
  new format. They are loaded into the tables once on request ("Load example
  rules" on a dataset with no rules; the local demo seed loads them directly).
  After that the tables are the only source of truth.
- The old tables `data_policies`, `data_policy_rules`, `data_policy_assignments`
  are dropped from the code (the test project's two test policies are discarded).

## Semantics (compiler)

For principal P and dataset D:

1. A line applies to P when its subject matches: `everyone`; `user` = P.email;
   `local_group` in P's local groups; `group` = an internal group_id whose
   external id P carries (`snapshot.groups_by_external_id`).
2. Rows: predicate = `OR(show filters of applying show lines) AND NOT(hide
   filter)` for every applying hide line. No applying show line -> D is invisible.
3. A filter is `all rows` or an AND of conditions. A condition is
   `column op value` where value is a literal (or list) or a source
   (`attribute:<key>` or `email`).
4. Missing or unparseable attribute: a show line does not apply; a hide line
   hides every row (fail closed).
5. Columns: for each column the first applying mask line (by position) gives
   the treatment: `clear`, `mask_partial`, `mask_hash`, `hidden` (absent from the
   relation). No applying line -> `clear`. Masked values are text.
6. Literals are typed from the warehouse column type: numeric -> number literal,
   boolean -> TRUE/FALSE, everything else a string literal. Everything is built
   as a sqlglot AST; nothing is string-concatenated.
7. Audit `grants_fired` lists the applying lines as `show:<rule_id>`,
   `hide:<rule_id>`, `mask:<column>:<mask_id>`.

Operators: `is`, `is_not`, `one_of`, `not_one_of`, `gt`, `gte`, `lt`, `lte`,
`is_empty`, `is_not_empty` (NULL or empty string). `one_of` / `not_one_of` take a
list of literals (not a source). `gt..lte` need a numeric or date column.

## Data model (control-plane tables)

```
row_access_rules(
  rule_id TEXT PRIMARY KEY,          -- 12 lowercase hex
  dataset TEXT NOT NULL,
  position TEXT NOT NULL,            -- integer as text, order within (dataset, kind)
  kind TEXT NOT NULL,                -- show | hide
  subject_type TEXT NOT NULL,        -- everyone | user | local_group | group
  subject_id TEXT NOT NULL,          -- '' for everyone
  filter TEXT NOT NULL               -- JSON, see below
)
column_masks(
  mask_id TEXT PRIMARY KEY,          -- 12 lowercase hex
  dataset TEXT NOT NULL,
  column_name TEXT NOT NULL,
  position TEXT NOT NULL,            -- order within (dataset, column_name)
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  treatment TEXT NOT NULL            -- clear | mask_partial | mask_hash | hidden
)
```

Filter JSON: `{"all": true}` or
`{"conditions": [{"column": "company", "op": "is", "value": "PHARMA_A"}, {"column": "company", "op": "is", "source": "attribute:company"}, {"column": "drug", "op": "one_of", "values": ["Zyrtec", "Euthyrox"]}, {"column": "margin", "op": "is_empty"}]}`.
Exactly one of `value` / `values` / `source` per condition, none for
`is_empty` / `is_not_empty`.

Limits are named constants in `kbc_access.store` (lines per dataset, conditions
per line, values per list, value length). Cascades: deleting a user, local group
or IdP group deletes the lines naming it; a user's email change moves them. A
subject that does not exist is refused. Keboola store mirrors both tables
(single keys); old buckets read them as empty and create them on first write.

## Policy file (catalogue + examples)

```yaml
version: 2
datasets:
  drug_sales:
    source: drug_sales
    description: ...
    columns: [...]
    examples:
      rows:
        - {kind: show, who: everyone, filter: {conditions: [{column: visibility, op: is, value: public}]}}
        - {kind: show, who: {group_external_id: "<entra guid>"}, filter: {all: true}}
        - {kind: hide, who: everyone, filter: {conditions: [{column: is_test, op: is, value: "true"}]}}
      masks:
        margin: [{who: {group_external_id: "..."}, treatment: clear}, {who: everyone, treatment: hidden}]
```

`who` in examples: `everyone`, `{user: email}`, `{local_group: id}`,
`{group_external_id: id}` (resolved to the registered IdP group with that
external id when loading; a line whose group is not registered is skipped and
reported). `load_policy` keeps checking declared columns against the warehouse
(ignoring Storage system columns).

## Admin API (under `/api`, same gate + CSRF + `audit.admin`)

- `GET /datasets` -> `{"datasets": [{"name", "description", "columns": [{"name", "type"}], "show_lines", "hide_lines", "masked_columns", "has_examples"}], "data_available": bool}`
- `GET /datasets/{name}` -> `{"dataset": {"name","description","columns":[{"name","type"}]}, "rows": [Line], "masks": {"<column>": [Mask]}, "attributes": ["company", ...], "has_examples": bool, "data_available": bool}`
  - Line: `{"rule_id", "kind", "subject": {"type","id","label"}, "filter": Filter}`
  - Mask: `{"mask_id", "subject": {"type","id","label"}, "treatment"}`
- `PUT /datasets/{name}` body `{"rows": [Line without labels, rule_id optional], "masks": {...}}`
  replaces the dataset's lines and masks in one unit; answers like GET plus
  `message`. 400 with a readable `detail` naming the line (`Row line 2: ...`,
  `Mask on revenue, line 1: ...`); nothing written on error. Audit `dataset_security_update`.
- `POST /datasets/{name}/load-examples` -> loads the YAML examples when the
  dataset has no lines and no masks; 409 otherwise; answers like GET plus
  `message` and `skipped: [..]`.
- `GET /datasets/{name}/values/{column}?q=` -> `{"values": [..], "truncated": bool}`
  distinct values from the data (cap is a named constant); 503 when data is unavailable.
- `POST /datasets/{name}/test` body `{"email", "groups": [external ids]}` ->
  `{"principal": {"email","local_groups","idp_groups","attributes"},
    "accessible": bool,
    "rows": {"visible": int|null, "total": int|null, "lines": [{"rule_id","kind","applies": bool,"reason": str,"matched_rows": int|null,"warning": str|null}]},
    "columns": [{"name","treatment","mask_id": str|null,"reason": str}],
    "preview": {"columns": [..], "rows": [[..]], "truncated": bool}|null,
    "row_filter": str|null, "data_available": bool}`
  `warning` is set e.g. when an applying line matches 0 rows: "No row has
  company = 'Pharma A s.r.o.' (values: PHARMA_A, PHARMA_B, PHARMA_C)". Preview
  rows are the compiled relation for the principal, limited by a named constant;
  hashes use the admin's own salt (display only). Audit `dataset_security_test`.
- Remove: `/data-catalog`, `/data-policies*`, `/data-policy-assignments*`,
  `/users/{email}/data-access`.

## Admin SPA

- Nav "Datasets" replaces "Data policies". `/datasets` lists datasets with line
  counts. `/datasets/:name` is the editor:
  - "Who sees which rows": ordered show lines (subject picker, "All rows" or
    condition rows with column / operator / value; the value field offers
    distinct values from `/values` and a "user's <attribute>" / "user's email"
    choice), then "Always hide rows where" lines. Drag or up/down to reorder.
  - "Column masking": one row per column; columns without lines show "clear for
    everyone"; adding an exception opens the ordered line list with subject and
    treatment; the last line can be "everyone else".
  - One "Save" button (PUT). "Load example rules" when the dataset is empty and
    has examples.
  - "Test as" panel: user picker plus optional IdP group ids, then counts, the
    per-line explanation with warnings, column treatments with reasons, and the
    preview table. Re-runs after a save.
- User page: the "Data policies" card and "Effective access" panel are replaced
  by "What this user sees": the Test as result for each dataset, read-only, with
  links to the editors. Local group and IdP group pages lose the data policies
  card and list the datasets/lines that name them (read-only, links).

## Dashboards

`column_state` reduces to CLEAR / MASKED / ABSENT (no per-row state); `numeric()`
keeps aggregates safe. They compile with the new compiler from the same snapshot.

## Deployment

- The admin app reads data: `reads_data = true` in `deploy/suite.toml`, so its
  bundle gets `[data]` + `[policy]` and its config gets Direct Storage Access to
  the demo tables (in addition to its control-plane grants) and the runtime
  workspace flag.
- `install.py redeploy` syncs each app's `storage.output.tables` (control-plane
  grants for the admin, demo-table grants for every app that reads data) and the
  workspace flag before pushing, without touching secrets, tokens or login.

## Tests

compiler (subjects, union, hide, missing attribute fail-closed, typed literals,
operators, first-match masks, hidden, no per-row), store (tables, validation,
cascades, rename, mirror), example loading, admin API (every route incl. 400,
404, 409, 503, audit, CSRF), test endpoint (counts, reasons, zero-row warning,
preview), dashboards on the new model, installer storage sync, frontend editor,
test panel and user page.

## Out of scope

OR inside one line (use two lines), per-row masking, conditions over other
datasets, time-bounded rules.

## Implementation decisions (backend)

Details the spec left open, as built:

- **Column types without the warehouse.** The catalogue declares the logical type of every
  non-text column: `types: {sale_id: numeric, sale_date: date, is_test: boolean, ...}` (kinds
  `numeric`, `boolean`, `date`, `text`; an undeclared column is `text`). The admin always types
  literals and validates operators from these declared kinds, with or without `[data]`, so both
  modes behave the same. `load_policy` (dashboards, and anything with a warehouse) refuses a
  catalogue whose declared kinds differ from the warehouse types (`column_kind` maps DuckDB,
  Snowflake and BigQuery type names), so the declaration cannot drift. The API's column `type`
  is this logical kind.
- **Module layout.** The filter model, subjects, operators, treatments and `Audience` live in
  `kbc_access.security_model` (no package imports). The store has `RowAccessRule`,
  `ColumnMask`, their `*Input` forms, `DatasetSecurity` and `PrincipalSecurity`; API
  `list_row_access_rules(dataset=None)`, `list_column_masks(dataset=None)`,
  `dataset_security(dataset)`, `set_dataset_security(dataset, rows, masks)` (replace, one
  transaction). `AclSnapshot.security` is keyed by dataset; `audience_of(principal)` and
  `security_for(principal)` resolve subjects (local groups: snapshot membership plus any the
  principal carries; IdP external ids to internal group ids).
- **Store limits** (`kbc_access.store`): `ROW_LINES_MAX_PER_DATASET` 100,
  `MASK_LINES_MAX_PER_DATASET` 200, `FILTER_CONDITIONS_MAX` 20, `FILTER_VALUES_MAX` 100,
  `FILTER_VALUE_MAX_CHARS` 500. Filter values are stored as text (JSON numbers and booleans are
  normalised to their text form); positions are 1-based per kind (rows) and per column (masks)
  in submitted order. A supplied id must be 12 lowercase hex characters, unique in the request
  and not belong to another dataset.
- **SQL shapes.** `is_not` is `IS DISTINCT FROM` (a NULL counts as "not X"); `not_one_of` is
  `col IS NULL OR NOT col IN (...)`; `is_empty` is `IS NULL` plus `= ''` for text columns; a hide
  line contributes `NOT COALESCE(filter, TRUE)`, so a row its filter cannot decide (NULL) is hidden
  too (fail closed; decided 2026-10-07). The Test as count of a hide line includes those rows. Number literals are emitted only after matching `^-?\d+(\.\d+)?([eE][-+]?\d+)?$`;
  dates are ISO string literals. A stored filter that is unreadable, names a column that no longer
  exists, or does not fit the column type fails closed like a missing attribute; an unknown stored
  treatment is treated as `hidden`. When every column is hidden the relation keeps its rows with one
  NULL column `_no_visible_columns`.
- **Policy file.** Version 2 only (version 1 files are refused). `columns` is required. Example
  `who` also accepts `{user: ...}` and `{local_group: ...}`; unregistered users and missing local
  groups are skipped and reported like unregistered IdP groups. The local demo seed loads examples
  only into datasets with no lines and no masks, so re-running it never overwrites admin edits.
- **Admin API.** Routes live in `apps/admin/datasets.py`. `masked_columns` in `GET /datasets` is
  the list of column names that have at least one masking line. `POST /load-examples` answers 400
  when the dataset declares no examples. `POST /test` accepts any valid email (a user that is not
  registered is tested as a signed-in user without attributes or local groups); `reason` for IdP
  group lines names the internal group id. `matched_rows` is the number of rows the line's own
  filter matches (for a hide line, the rows it hides), computed whenever the filter could be bound
  for the tested user; `warning` (only for applying lines with 0 rows) names the first condition
  that matches nothing and, for `is` / `one_of` on a text column, lists up to
  `WARNING_VALUES_LIMIT` (10) existing values. Limits: `VALUES_LIMIT` 50, `PREVIEW_ROW_LIMIT` 20.
  Audit actions: `dataset_security_update`, `dataset_security_load_examples`,
  `dataset_security_test` (target `<dataset>:<email>`).
- **Admin warehouse.** The admin opens its warehouse copy lazily (`web.build_backend`, which also
  starts the Direct Storage Access refresher when configured); without `[data]`, or while the file
  does not exist yet, `data_available` is false.
- **Dashboards.** `CompiledDataset.column_treatments` maps every column to its single treatment;
  `column_state` is CLEAR / MASKED / ABSENT.
