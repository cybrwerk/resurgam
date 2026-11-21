# 0005 — Interim local encrypted secrets provider

Status: Accepted (2026-09-12)

## Context

PBS and PVE connections (manifest §6.1/§6.2) need an API token to actually be usable — not just stored as an opaque reference. The manifest names OpenBao as the target secrets provider (§4, §14: "Secrets nur zur Laufzeit beziehen"), but integrating a real OpenBao instance is out of scope for this slice, and a transient-only approach (never persisting the token at all) would mean connections could never be re-tested or used again after creation — a real functional gap, not an acceptable interim state, since manifest §9.1 explicitly requires ongoing "testen" (test) as a first-class action.

## Decision

Implement a small interim `SecretsProvider` (`backend/src/resurgam_api/secrets/provider.py`, a `Protocol`) with one real implementation, `LocalEncryptedSecretsProvider` (`local_provider.py`): Fernet (symmetric AES-based) encryption with a key from server configuration (`SECRETS_LOCAL_ENCRYPTION_KEY`), ciphertext stored in a new `local_secret_values` table keyed by `secret_references.reference_key`. `secret_references` itself stays exactly what the manifest describes — an opaque, provider-agnostic pointer — so nothing that references a `SecretReference` (e.g. `pbs_instances.api_token_secret_ref_id`) needs to change when the provider changes.

## Consequences

- PBS/PVE connections created now can actually be tested repeatedly (`POST .../test-connection`) using their stored token, not just at creation time.
- The encryption key lives in the same server configuration as the database credentials (`.env`/environment), not a separate trust boundary — this is explicitly *not* equivalent to a real secrets provider (OpenBao, HashiCorp Vault, a cloud KMS). It protects against someone reading the database directly (e.g. a backup dump, a read-only DB replica) but not against full server/host compromise. This must not be presented as production-grade secret custody.
- Swapping to OpenBao later means writing a new class implementing `SecretsProvider` and changing which one gets constructed — `secret_references`, `pbs_instances`, `pve_clusters`, and all their callers stay unchanged. `local_secret_values` would eventually be dropped once nothing writes to it anymore.
- No secret material (token plaintext, ciphertext, or the encryption key) ever appears in an API response, and never should — this was validated in tests (`test_create_list_get_pbs_instance` asserts `"api_token" not in body`).

## Addendum (2026-09-24): the blast radius grew beyond what this ADR originally scoped

At acceptance, `secret_references` held only PBS/PVE API tokens. It now also holds every local user's TOTP MFA seed (`mfa.py`), the OIDC client secret (`integrations.py`), and the SMTP password (`mail_settings.py`) — all through the exact same table and, until this addendum, the exact same one Fernet key. A leak of that one key decrypted all four categories at once; a confused-deputy bug (code resolving the wrong `SecretReference` for a given operation) could silently hand back the wrong kind of secret.

Migration `0047_secret_reference_purpose_and_subkeys` adds `SecretReference.purpose` (`infra_credential`/`mfa_totp`/`oidc_client_secret`/`smtp_password`, DB-enforced via a `CHECK` constraint, set explicitly by every creation call site — never inferred from `reference_key`'s own string shape) and re-encrypts every existing `local_secret_values` row under an HKDF-SHA256-derived, purpose-specific subkey (`secrets/key_derivation.py`) instead of the one shared master key. `LocalEncryptedSecretsProvider.store`/`retrieve`/`delete` keep their existing signatures unchanged — the provider resolves `purpose` itself via a `SecretReference` lookup, so none of this provider's ~20 call sites across the codebase needed to change.

**What this does and does not buy**: `purpose` values are public (literal strings in this project's own source), so anyone holding the master key can already derive every subkey — this is explicitly *not* protection against a leak of `SECRETS_LOCAL_ENCRYPTION_KEY` itself, and every limitation this ADR already states about that key applies unchanged. What it does buy: a wrong-purpose decrypt now fails loudly (Fernet `InvalidToken`) instead of silently succeeding against the wrong secret class, and the schema itself (not just careful code) now enforces that every secret is one of four known kinds.

The migration was live-verified against realistic pre-existing data (one row per purpose, encrypted under the pre-migration single-key scheme, including the real dev environment's actual PBS/PVE tokens and MFA seeds) before being trusted — both the re-encryption and its downgrade path round-trip correctly; a real PBS `test-connection` call and a real generated TOTP code were confirmed working post-migration.
