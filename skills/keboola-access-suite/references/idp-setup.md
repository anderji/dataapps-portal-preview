# Login provider setup for the access suite

The provider details (client ID, secret, issuer) are entered by the user in the
Keboola UI: Data App → **Authentication**. This page covers the provider side.

The Keboola apps-proxy logs users in with OIDC and forwards three claims to the
app as headers: `name` → `X-Kbc-User-Name`, `email` → `X-Kbc-User-Email`,
`groups` → `X-Kbc-User-Roles`. The suite decides access from **email and
groups**, so the IdP must put both into the ID token.

One IdP app can serve every app of the suite: each Data App only needs its own
redirect URI registered on it.

## Redirect URIs

For every app: `https://<app host>/_proxy/callback` — the installer prints the
exact list (`redirect_uris_to_register_at_idp`). Add them after the install,
when the app URLs exist.

## Microsoft Entra ID

1. **App registrations → New registration.** Platform *Web*. Add the redirect
   URIs later (see above).
2. **Certificates & secrets → New client secret.** Paste the *value* into the
   app's Authentication settings in Keboola; do not share it anywhere else.
3. **Token configuration → Add groups claim →** *Security groups*; for the
   *ID* token choose *Group ID*. The claim then carries group **object IDs**
   (GUIDs) — exactly what the access admin stores.
4. **Nothing to configure for the email.** The proxy requests the `email`
   scope, and Entra then puts the user's **mail** attribute into the `email`
   claim (the UPN is not used). Every user who should sign in needs a mail
   attribute (Users → user → Properties → Contact information → Email); users
   without one get no `email` claim and cannot sign in. Grants in the access
   admin use that email.
5. Issuer URL: `https://login.microsoftonline.com/<tenant id>/v2.0`;
   logout URL: `https://login.microsoftonline.com/<tenant id>/oauth2/logout`.

Watch out: a user in more than ~200 groups gets no `groups` claim (Entra
"overage"). The suite then sees no groups and falls back to direct user grants
only — assign such users directly, or limit the claim to groups assigned to the
application (*Groups assigned to the application* in step 3).

## GitHub / GitLab

Create an OAuth app at the provider with the redirect URI above and enter its
client ID and secret in the Keboola UI. The proxy forwards the user's email;
what it sends as groups (organisations, teams) is not verified yet — check the
launcher's "What the launcher knows about you" table after the first login.

## Okta

1. **Applications → Create App Integration → OIDC → Web Application.**
2. Sign-in redirect URIs: add later (see above). Grant type: Authorization Code.
3. **Sign On → OpenID Connect ID Token → Groups claim type** *Filter*, claim
   name `groups`, filter e.g. *Matches regex* `.*`.
4. Issuer URL: `https://<your okta domain>/oauth2/default` (or the org
   authorization server `https://<your okta domain>`).

Watch out: Okta's groups claim carries group **names**, not the `00g…` IDs
that the access admin's Okta template expects. Until this is verified on a live
tenant, prefer Entra, or check the launcher's "What the launcher knows about
you" table after the first login and tell the maintainer what arrived.
