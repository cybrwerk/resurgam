# 0004 — Break-glass auth as the first (and permanent) auth path

Status: Accepted (2026-09-12)

## Context

Manifest §6.3 requires OIDC via authentik as the primary login path, but also requires a local "break-glass" login that is "explicit, gehärtet und vollständig auditiert" (explicit, hardened, and fully audited) for emergencies. No authentik instance is available in this development environment yet, and the user confirmed OIDC should be stubbed behind an interface for now rather than blocked on standing up authentik.

## Decision

Build the break-glass login path first, as a real (not throwaway) implementation: bcrypt-checked static credentials from configuration, gated by `BREAKGLASS_ENABLED` (default `false`), issuing a short-lived signed session token, with an `AuditEvent` written on both successful and failed attempts. `get_current_user` and `AuthenticatedUser` (`backend/src/resurgam_api/auth/`) are designed as the stable interface: they don't know or care whether the underlying login was break-glass or (later) OIDC — they only consume a validated session identifying a `User` row and that user's `UserRoleAssignment`s.

## Consequences

- Every business endpoint written from now on protects itself via `get_current_user`/`require_role` (`backend/src/resurgam_api/auth/dependency.py`) and will not need to change when real OIDC is added — only the *token issuance* side changes (a new `/api/v1/auth/oidc/*` flow populating the same `User`/session mechanism, or a parallel token-verification path feeding the same `AuthenticatedUser` shape).
- Break-glass is not a temporary shortcut to delete later; it stays as the permanent emergency-access path required by §6.3, so its hardening (audit-on-every-attempt, disabled by default, bcrypt hashing, short TTL) was built to the real bar from the start rather than being revisited.
- Session tokens today are self-issued JWTs signed with a single symmetric `SESSION_SECRET`. Once OIDC lands, real user sessions will likely follow the same short-lived local session token shape (so `get_current_user` doesn't change), with the OIDC flow only responsible for establishing that a `User` row exists/is updated from IdP claims and then issuing the same kind of session token — this needs revisiting once authentik is actually available, but the interface boundary here is designed to make that a contained change.
