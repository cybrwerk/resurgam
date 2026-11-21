# 0010 — Local user accounts and MFA (TOTP, WebAuthn, recovery codes)

Status: Accepted (2026-09-14)

## Context

Requested directly (not in the manifest, which only specifies break-glass and OIDC login — ADR 0004/0007): "For Local-Users there must be MFA with FIDO, TOTP and/or Recovery-Codes. Also there is no profile site to change password." Neither requirement could be met by the existing auth model: `User.is_breakglass` is a single, env-configured, shared credential with no per-user password, and OIDC users have no password at all (authentik owns that). MFA and a self-service "change password" both presuppose an account with its own, individually-settable credential — that didn't exist.

## Decisions

### A real local-user account is `User.password_hash` set, not a new table

Considered a separate `LocalAccount` table joined to `User`. Rejected: `User` already distinguishes break-glass (`is_breakglass`) and OIDC (`external_subject` set) by flags/nullable columns on the same table, and a local account is the same kind of thing — a third way a `User` row can authenticate. `password_hash` nullable, set only for local accounts, keeps `AuthenticatedUser`/role-assignment/scope code completely unaware of *how* a session was established, preserving the existing "every auth backend produces the same `AuthenticatedUser` shape" design (ADR 0004).

### `POST /auth/local-login` is a new endpoint, not a change to break-glass

Break-glass stays exactly as-is — a single emergency credential from `.env`, never subject to MFA, never able to change its own password (there's no `User.password_hash` for it to change; attempting `/auth/change-password` on it is explicitly rejected with `no_local_password`). Local accounts are additive: `POST /users` (admin-created, `platform_admin`-only) issues individually-settable passwords; `POST /auth/local-login` authenticates them. The frontend's login page now shows both forms — local sign-in as the primary form, break-glass tucked behind an "Emergency access" toggle — without touching break-glass's own request/response shape or its existing tests.

### MFA is a second, stateless step — not a session-store dependent one

RESURGAM's sessions are stateless signed JWTs (no server-side session store, ADR-adjacent to the original break-glass design). Adding MFA without a session store meant the gap between "password verified" and "second factor verified" needed to be bridged statelessly too: `POST /auth/local-login` with a password but no confirmed MFA method returns a full session token exactly as before (unchanged for accounts without MFA enabled); with a confirmed method, it returns a short-lived (`mfa_challenge_ttl_seconds`, 5 min) **MFA challenge token** instead — a JWT carrying a `purpose: "mfa_challenge"` claim. `verify_session_token()` explicitly rejects any token carrying a `purpose` claim, so a challenge token can never be used to reach a business endpoint on its own, only the three `/auth/mfa/verify-*` endpoints that consume it.

### WebAuthn ceremonies reuse the same stateless-JWT trick, not a server-side ceremony store

A WebAuthn registration or login ceremony needs the server to remember its own random challenge between the "generate options" and "verify response" calls. Rather than add the first piece of server-side session state RESURGAM has ever needed, the challenge (and, for registration, the chosen nickname) rides back to the client and back again inside another short-lived, purpose-scoped JWT (`create_webauthn_ceremony_token`/`verify_webauthn_ceremony_token`, `action: "register"` or `"authenticate"` so one can't be replayed as the other). One real bug this caused during development: an early version concatenated the nickname onto the raw challenge bytes with a `|` delimiter — the challenge is random bytes that can legitimately contain that exact byte, intermittently corrupting the split and raising a `UnicodeDecodeError`. Fixed by giving the nickname its own JWT claim instead of packing it into the challenge bytes; caught by a real (if initially flaky) test failure, not by inspection.

### Recovery codes require an existing primary method

`POST /mfa/recovery-codes/generate` is rejected (`no_primary_mfa_method`) unless the account already has a confirmed TOTP credential or a registered WebAuthn credential. Recovery codes back up an existing second factor; issuing them with nothing to back up would create an account whose "second factor" is a codes-on-paper list with no first factor behind it, defeating the point of MFA.

### TOTP secrets and WebAuthn public keys are stored the same way PBS/PVE tokens are

A TOTP secret is exactly as sensitive as a PBS API token — `TotpCredential.secret_ref_id` points into the same `SecretReference` + `LocalEncryptedSecretsProvider` (Fernet-encrypted, ADR 0005) rather than a new storage mechanism. `confirmed_at` stays null until the user proves they can generate a valid code once, so an enrolled-but-never-confirmed secret (e.g. a mis-scanned QR code) never silently becomes a login requirement. WebAuthn's `credential_id`/`public_key` are stored as base64url text (the shape the `webauthn` library already produces/expects), not raw bytes — there's nothing secret about a public key, only the private key (which never leaves the authenticator) matters.

### Testing WebAuthn: verify the wiring, not the cryptography

`webauthn.verify_registration_response`/`verify_authentication_response` are monkeypatched at the call site in tests — hand-rolling a fake authenticator's CBOR/COSE attestation encoding would test the third-party `webauthn` library's own correctness, not RESURGAM's. What RESURGAM tests for real: ceremony-token round-tripping, database storage, `sign_count` persistence, and every error path (unknown credential id, a registration-purpose ceremony token rejected by the authenticate endpoint, etc.) it's actually responsible for. Everything else in this ADR's scope — local login, change-password, TOTP enroll/confirm/verify/remove, recovery-code generate/verify/single-use — is tested with real bcrypt hashing, real `pyotp` TOTP generation, and a real Fernet-encrypted secret round-trip; no mocking.

## Consequences

- Role assignment for a newly-created local user is entirely manual (`POST /users/{id}/assign-role`) — a fresh account can log in but reaches nothing until an admin grants a role. No bulk-invite or self-registration flow exists; not requested.
- Account lockout after repeated failed MFA attempts, "remember this device," and rate-limiting on `/auth/local-login` are not implemented — stated simplifications, not silent gaps, worth revisiting before this leaves a development/internal deployment.
- WebAuthn's `rp_id`/`origin` (`webauthn_rp_id`/`webauthn_origin` settings) must match the frontend's actual origin in every environment, same operational requirement as any WebAuthn relying party — misconfiguring it fails registration/authentication with a clear error, not a silent security hole.
