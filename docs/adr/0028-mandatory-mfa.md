# 0028 — Mandatory MFA: per-user flag, global policy, forced enrollment

Status: Accepted (2026-10-17)

## Context

Local accounts could set up TOTP, security keys (WebAuthn) and recovery codes on their profile page,
but MFA was optional: an administrator could not require it. The user asked for a way to make a second
factor mandatory, for single users and for every local account. A review then found that the first
implementation's enrollment token could be replayed (see "Replay protection" below).

## Decision

### Who must have a second factor
- **Per user:** `users.mfa_required`, set in the Users & Roles dialog (`POST /users/{id}/mfa-requirement`)
  or on creation (`mfa_required` in `POST /users`). Saved immediately, audited.
- **Global:** `instance_settings.require_mfa_all_local_users` (`PUT /mfa-policy`, `platform_admin` only,
  audited). The Users page shows it as one slim row with a "Change…" button and a confirmation.
- `mfa_required_for(db, user)` (`instance_settings.py`) is the single check: own flag OR global policy,
  and only for local accounts. **Break-glass and SSO users are always exempt**: break-glass is the
  emergency path (below), and SSO users do MFA at their identity provider. The API refuses the flag and
  Reset MFA for them (`mfa_not_applicable`).
- Enforcement is at the next sign-in. Running sessions are not ended.

### Forced enrollment with a restricted token
`POST /auth/local-login` with a correct password, MFA required and no confirmed factor answers
`mfa_enrollment_required: true` plus an `enrollment_token` (15 minutes, `MFA_ENROLLMENT_TTL_SECONDS`)
and no session. The token carries `purpose: "mfa_enrollment"`, so `verify_session_token` rejects it like
every other purpose token: it reaches no business endpoint, not `whoami`, not factor removal or password
change. Only `get_session_or_enrollment_user` accepts it, and only these endpoints use that dependency:
`GET /mfa/status`, `POST /mfa/totp/enroll`, `POST /mfa/totp/confirm`, `POST /mfa/webauthn/register-options`,
`POST /mfa/webauthn/register`, `POST /mfa/recovery-codes/generate`. Once a factor is proven,
`POST /auth/mfa/enrollment/complete` exchanges the token for a normal session. The login page walks the
user through it (`MfaEnrollmentStep`), shows the recovery codes once (copy, download as .txt, a required
"I have saved these codes" checkbox) and then signs in.

### Replay protection
The token is a stateless JWT. Without more, it could be replayed within its 15 minutes after the
enrollment it belongs to: exchanged for a second session without a second factor, used to replace the
user's recovery codes, or to register an attacker's security key. A token issued *before* someone else's
enrollment would also still work afterwards.

Each enrollment token now has a unique `jti`. The bindings are kept in the append-only `audit_events`
table (immutable at the database level), so no new table or migration is needed
(`auth/mfa_enrollment.py`):

| Audit action | Written when | `detail` |
| --- | --- | --- |
| `mfa_enrollment_factor_added` | a TOTP is confirmed or a key registered with an enrollment token | `enrollment_jti`, `factor`, `credential_id` |
| `mfa_enrollment_recovery_codes` | recovery codes generated with an enrollment token | `enrollment_jti` |
| `local_login` (`mfa: enrolled_at_login`) | the token was exchanged for a session | `enrollment_jti` |

A token is **usable** only while (1) it has not been exchanged and (2) every confirmed factor the account
currently has was added with this very token. So:
- no factor yet → usable (equivalent to knowing the password, which can fetch a new token anyway);
- the same token finishing its own enrollment (second factor, recovery codes, exchange) → usable;
- a factor from another enrollment, from the profile page, or from before an admin reset → 401
  `invalid_enrollment_token`;
- after the exchange → 401: `/auth/mfa/enrollment/complete` is single-use.

Recovery codes can be generated with an enrollment token **once** per enrollment (after its first
factor); afterwards 403 `recovery_codes_need_session`. The exchange and the recovery-code call lock the
user row (`SELECT … FOR UPDATE`), so two concurrent calls with the same token cannot both succeed.
Tokens without a `jti` (issued before this change) are rejected.

### Last-factor guard
While MFA is required, the last confirmed TOTP or security key cannot be removed (409
`mfa_required_last_factor`). The profile page disables the Remove button in that case and says why
("Add a security key first — your administrator requires a second factor."). Recovery codes do not count
as a factor: they only back one up.

### Reset MFA
`POST /users/{id}/reset-mfa` (`platform_admin`) deletes the account's TOTP (also an unconfirmed one),
security keys and recovery codes, audited as `user_mfa_reset`. `UserOut.mfa_reset_possible` tells the UI
whether there is anything to remove. An administrator **cannot reset their own MFA** (409
`cannot_reset_own_mfa`); they manage it on their profile, so a stolen admin session cannot strip its own
second factor in one step.

### Configuration export
The export carries the per-user `mfa_required` flag and the global `require_mfa_all_local_users` switch
(in the `instance_settings` section); an import of an older file without the switch leaves it unchanged.
Factor material (TOTP secrets, security keys, recovery codes) and password hashes are never exported,
so after an import onto a new appliance every local user needs a password set again (Users & Roles) and,
with MFA required, sets up a new factor at the next sign-in.

### Lockout recovery
In this order:
1. **Recovery code:** the user signs in with one of their recovery codes and sets up a new factor on the
   profile page.
2. **Another administrator** opens Users & Roles → the user → **Reset MFA**. The user sets up a new factor
   at the next sign-in.
3. **No other administrator** (or the only admin is locked out): break-glass on the appliance console.
   ```
   cd /opt/resurgam/appliance
   docker compose exec backend python -c "import bcrypt;print(bcrypt.hashpw(b'NEW',bcrypt.gensalt()).decode())"
   # in .env (single quotes, so Compose does not expand the $ signs in the hash):
   #   BREAKGLASS_ENABLED=true
   #   BREAKGLASS_PASSWORD_HASH='$2b$12$…'
   docker compose up -d
   ```
   Sign in via **Emergency access (break-glass)** on the login page (username `BREAKGLASS_USERNAME`,
   default `admin`), open Users & Roles → the locked-out user → **Reset MFA**. Then set
   `BREAKGLASS_ENABLED=false` again and `docker compose up -d`.

   The break-glass user always holds global `platform_admin`: it is granted at every break-glass
   sign-in (`auth/breakglass.py`), also if someone removed it -- an emergency account without a role
   could sign in but not fix the lockout.

## Consequences

- An administrator can enforce MFA without touching each account, and the forced enrollment cannot be
  skipped: no session exists until a factor is proven.
- The enrollment token stays stateless; the only server-side state is audit rows, which are written
  anyway and cannot be altered afterwards. The price is one or two audit queries per
  enrollment-token request; enrollment is rare, so this does not matter.
- Audit rows double as the evidence of when and how an account enrolled.
- Break-glass remains the way out of a total lockout, which is why it must stay exempt from MFA and why
  enabling it requires console access to the appliance.
