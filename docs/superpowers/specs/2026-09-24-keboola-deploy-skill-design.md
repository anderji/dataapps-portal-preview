# Skill: deploy the access-control suite into a Keboola project

- **Status:** approved 2026-09-24; phase 1 implemented (`skills/keboola-access-suite/`, `src/kbc_access/keboola_store.py`, `scripts/build_app_bundle.py`), not yet run live
- **Date:** 2026-09-24
- **Target project for the first run:** a dedicated test project, reached through its kbagent alias
- **Decisions already made by Vojta:** OIDC login (he has a provider), code in Keboola-managed git (no GitHub), standalone skill uploadable to Agnes, phase 2 = demo dashboards + data.

## 1. What the skill does

**Phase 1 — skeleton.** Into the chosen project:

1. a bucket (name chosen by the user, default `apps-control-plane`, i.e. `in.c-apps-control-plane`) with the five access tables (`apps`, `users`, `groups`, `app_grants`, `user_attributes`);
2. the **Access admin** Data App and the **App launcher** Data App, deployed from Keboola-managed git, behind OIDC;
3. both apps registered in `apps`, and one bootstrap grant so the first administrator can open the admin app.

**Phase 2 — demo.** Into the same project: the pharma demo tables (`companies`, `stores`, `drug_sales`, `supply_chain`), the row/column policy, and the two dashboards (sales, supply) behind the same gate. Phase 2 runs only after phase 1 is healthy.

## 2. Facts this design rests on (verified 2026-09-24)

| Fact | Source |
|---|---|
| The proxy deletes every client `x-kbc-*` header and sets `X-Kbc-User-Name/Email/Roles` from the OIDC `name`/`email`/`groups` claims | `keboola-as-code/internal/pkg/service/appsproxy/proxy/apphandler` (local checkout, Jan 2026) |
| OIDC provider in the Storage config: `{id, type: "oidc", client_id, "#client_secret", issuer_url, logout_url, allowed_roles?}`; rule `{type: pathPrefix, value: "/", auth_required: true, auth: [id]}` | live config of "Auth with OIDC" in project new-playground + `AppProxyDefinition.php` validator |
| OIDC redirect URL to register at the IdP: `https://<app-host>/_proxy/callback` | appsproxy `InternalPrefix = "/_proxy"` |
| `kbagent data-app create` supports only `--auth password|public`; OIDC must be written into the config afterwards | kbagent 0.84 `data_app_service.py` |
| App secrets in `parameters.dataApp.secrets` become env vars (`#ACCESS_STORAGE_TOKEN` → `KBC_TOKEN`) | dataapp-deployment skill |

**To verify on the first live run (not yet proven):** how multiple groups are joined in `X-Kbc-User-Roles` (the separator is config, so this is a one-line fix either way); whether a plain `#client_secret` written by the API is encrypted server-side or must be encrypted first via the Encryption API (the skill encrypts first — always safe).

## 3. New code this needs

1. **`KeboolaStorageAccessStore`** — the same interface as the SQLite `AccessStore`, backed by the Storage API:
   - reads: `GET /v2/storage/tables/{id}/data-preview` (synchronous, up to 1000 rows — fine for ACL tables; the limit is stated and checked);
   - writes: incremental `import-async` with primary keys, and `DELETE /v2/storage/tables/{id}/rows` for removals; the admin app waits for the Storage job before redirecting;
   - selected by config (`[store] backend = "sqlite" | "keboola"`), so the local suite keeps working unchanged.
2. **Runtime config from environment** for deployed apps (bucket id, stack URL, token, salt) — fail fast on anything missing.
3. **Keboola packaging** per app: `keboola-config/` (nginx → uvicorn, supervisord, `setup.sh`), `pyproject.toml`, POST `/` health check (already there).
4. **The skill** `skills/keboola-access-suite/`: `SKILL.md` + `scripts/` (bootstrap steps) + `assets/` (the app source, bundled at build time so the skill is standalone for Agnes).

## 4. Phase 1 step by step (what the skill asks and does)

1. **Pick the project** — `kbagent project list`, match, confirm. Never hardcoded.
2. **Ask for OIDC** — provider (Entra/Okta), issuer URL, client ID, client secret (secret goes straight to the Encryption API, never echoed or logged), and the first admin: email + the admin group ID from the IdP.
3. **Create the bucket and tables** — ask for the bucket name (default `apps-control-plane`), then `kbagent storage create-bucket` / `create-table` with primary keys. The chosen bucket id is written to the apps' config, never assumed.
4. **Create a scoped Storage token** for the apps — write access to the access bucket only (phase 2 adds read on the demo bucket). Shown to nobody; written as the apps' `#ACCESS_STORAGE_TOKEN` secret.
5. **Create both apps** — `kbagent data-app create --use-managed-git-repo --no-deploy`, push the bundled code to each managed repo, set secrets, write the OIDC `authorization` block, deploy, wait.
6. **Register** both apps in `apps` and grant the admin app to the first admin's group and email.
7. **Hand-off** — print both app URLs and the two redirect URLs to add at the IdP (`https://<host>/_proxy/callback`); after the user adds them, open the launcher and check the round trip.

Every outward step (create bucket, token, apps, deploy) is announced and confirmed before it runs; the skill is re-runnable and skips what already exists.

## 5. What this does not do

- Configure the IdP itself (app registration, redirect URLs, group claims) — the skill tells the user exactly what to add.
- Protect existing apps in the project (that is the `protect-data-app` skill).
- Replace SQLite for local development.

## 6. Open questions

- ~~Q1. Bucket name~~ — decided 2026-09-24: user-chosen, default `apps-control-plane`. Phase 2 demo data bucket: proposal `apps-control-plane-demo`, also user-chosen.
- Q2. Can the kbagent project token create Storage tokens (`canManageTokens`)? If not, the skill asks the user to create one in the UI.
- Q3. OIDC details for the target project — issuer URL, client ID, the admin group ID — needed at run time, not now.
