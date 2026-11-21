# 0007 — Real OIDC via authentik (Authlib, PKCE, group-based role mapping)

Status: Accepted (2026-09-13)

## Context

ADR 0004 deferred real OIDC because no authentik instance existed to integrate against. The user stood one up locally (`http://localhost:9000`, application slug `resurgam`) and this was verified end-to-end against it — not just implemented against assumptions, unlike most of this project's PBS/PVE integration work.

## Decision

- **Authlib** (`authlib.integrations.starlette_client.OAuth`) implements the Authorization Code + PKCE flow, configured via `server_metadata_url` (OIDC discovery) so Authlib fetches endpoints and validates the ID token's issuer, audience, signature, expiry, and nonce itself — the exact checklist manifest §6.3 requires, without hand-rolled JWT validation.
- **PKCE must be requested explicitly** — Authlib does not enable it by default even for a registered OAuth2 client; `client_kwargs={"code_challenge_method": "S256"}` is required. Confirmed via a real redirect to authentik that `code_challenge`/`code_challenge_method` were absent before this was added and present after.
- **PKCE state (verifier, nonce) is held in a signed session cookie** (Starlette's `SessionMiddleware`, `main.py`), reusing `SESSION_SECRET` as the signing key — the same trust boundary as the JWT sessions it already signs, not new key-management surface.
- **Group→role mapping**: an authentik group named `resurgam-<role-key>` grants that role at **global** scope on login. Role keys are underscore_separated (`platform_admin`) but a real authentik group is naturally typed kebab-case (`resurgam-platform-admin`) — confirmed live that an exact-match comparison silently granted nothing until hyphens were normalized to underscores before matching (`auth/oidc.py::_role_keys_from_groups`). This was a real bug caught by live testing, not a hypothetical.
- **Deprovisioning**: `user_role_assignments.source` (`manual`/`oidc`) lets each login reconcile only OIDC-derived global assignments against the caller's current groups — granting newly-present ones, revoking ones whose group disappeared — without ever touching manually-granted assignments (break-glass or otherwise).
- **Claims are read from `userinfo`, falling back to whatever the ID token itself carried** (`token["userinfo"]`, Authlib's parsed-ID-token claims) if the userinfo call fails. This was validated live rather than left as a permanent guess — see "Verified" below.
- The callback returns the same `{access_token, token_type, expires_in}` JSON shape `POST /auth/breakglass-login` does, since there is no frontend yet to hand a cookie or redirect to.

## Verified live (not just assumed)

Against the user's real authentik instance:
- Discovery, authorization redirect (with correct `client_id`/`redirect_uri`/PKCE params), token exchange, ID token validation, and session issuance all worked on the first real login.
- `userinfo`/claims correctly carried `sub`, `email`, `name`, and `groups` — group-based role grant confirmed working once the hyphen/underscore normalization fix landed (`resurgam-platform-admin` → `platform_admin` at global scope, visible via `GET /auth/whoami`).
- Revocation (removing a group and re-logging in) shares the identical reconciliation code path as the grant path just proven live, and is covered by unit tests (`test_oidc_provisioning.py::test_revokes_role_when_group_removed_on_later_login`) — not separately re-verified live, a reasonable stopping point rather than a gap.

## Consequences

- Manifest §17's "OIDC und RBAC" Milestone 1 pairing is now fully done — this was the last open item.
- Customer-scoped role grants via OIDC groups are still not expressible (a group name alone can't carry a customer) — remains a manual action, same limitation stated when this was planned.
- No server-side session revocation/logout exists for either break-glass or OIDC sessions — both are stateless JWTs. Pre-existing, orthogonal gap, not introduced or worsened here.
- `get_oauth_client()` reconstructs the Authlib `OAuth` registration (and re-fetches discovery metadata) on every login/callback call rather than caching it — simplest correct thing for now given login isn't a high-frequency path; worth revisiting only if that changes.
