# 0029 — Personal API tokens with per-token roles, read-only mode and IP allowlist

Status: Accepted (2026-11-11)

## Context

The API accepted only login sessions, which expire after 15 minutes (`session_ttl_seconds`). Scripts,
CI jobs and monitoring had no lasting credential: the Zabbix endpoints (`api/v1/zabbix.py`) noted that
a configured token would need constant rotation, and a session can't be obtained non-interactively
once MFA is required. The manifest asks for "API-Tokens … für Maschinenzugriff" and "API-Tokens mit
minimalen Rechten". The user asked for tokens users can create themselves, with finer permissions than
"read-only or everything", and a maximum lifetime of 18 months.

## Decision

**Token = a user's credential, narrowed.** A token belongs to one user and acts as that user, but its
`AuthenticatedUser.assignments` are replaced by the token's own `grants` (role + scope). All existing
authorization (`require_role`, `visible_customer_ids`, the per-customer checks) works on those
assignments, so no endpoint needed changing and there is no second permission system to keep in sync.

**Grants must be covered by the user, now and on every request.** A grant is allowed when the user holds
that role globally or exactly that role on that scope; a global `platform_admin` may grant any role on
any scope (they can already do everything). `platform_admin` itself is global-only. Coverage is
re-evaluated per request against the user's *current* roles, so removing a role or disabling a user
takes effect on all their tokens immediately — no token can outlive its owner's rights.

**Further limits per token:**

- `read_only` (default on): only GET/HEAD/OPTIONS. Independent of roles, so even a `platform_admin`
  token can be safe for monitoring.
- Optional CIDR allowlist. The client IP comes from the TCP peer, or — only when `trust_proxy_headers`
  is on — from the right-most `X-Forwarded-For` entry. The appliance enables it: its Caddy replaces any
  client-sent `X-Forwarded-For`, and the backend port isn't published. Off by default so a directly
  reachable API can't be fooled with a spoofed header.
- Mandatory expiry, 1–548 days (18 months, the user's limit). No non-expiring tokens.

**Storage.** `rsg_` + 32 random bytes (url-safe). Only the SHA-256 hex digest is stored (a slow KDF adds
nothing for 256-bit random secrets and would prevent lookup by hash); the first 12 characters are kept
as a display prefix. The token is returned once. Revocation sets `revoked_at` (rows are kept so audit
entries can still be resolved). Expired tokens stay listed until deleted. At most 50 active tokens per
user.

**What a token can't do.** Token management (`/api-tokens/*`) and `change-password` require a login
session (`get_session_user`); MFA endpoints already accept only sessions. The break-glass account can't
create tokens — emergency access must not turn into a standing credential.

**Audit.** Creation and revocation are audited. Every audit event written during a token-authenticated
request is labelled "<user> via API token <name> (<prefix>…)": the label is put on the request's DB
session (`Session.info`) by the auth dependency and read by the existing `before_insert` hook in
`models/audit.py`, so no `_record_audit` helper had to change. `last_used_at`/`last_used_ip` are
written at most once a minute per token (or when the address changes).

## Alternatives considered

- **OAuth2 client credentials** (named in the manifest as "später"): needs a client registry and a
  token endpoint, and still needs the same permission model. Personal tokens solve the actual need now;
  client credentials can be added on the same grants later.
- **A separate permission vocabulary for tokens** (e.g. `backups:read`): finer in theory, but a second
  model to map onto every endpoint and keep in sync. Roles × scope × read-only covers the requested
  granularity with the existing checks.
- **Service accounts** (non-human users holding tokens): useful when a token must survive its creator
  leaving. Not built yet; an admin can create a dedicated local user with minimal roles today.

## Consequences

- `get_current_user` now takes the `Request` (method and client IP).
- Read-only also blocks the few POST endpoints that only read (e.g. report generation); such automation
  needs a read & write token, still limited by its roles.
- A read & write `platform_admin` token is as powerful as its owner's session, minus token/password/MFA
  management. The UI defaults to read-only and asks for the narrowest roles.
- Tokens are not in the configuration export (ADR 0024): they are secrets bound to one instance.
- Not yet tried against the appliance's real Caddy: the `X-Forwarded-For` handling is covered by unit
  tests and relies on Caddy ≥ 2.5 replacing the header (the appliance image's version).
