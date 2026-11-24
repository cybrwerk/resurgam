# 0031 — Backup encryption keys: visibility, a key store by fingerprint, and PVE storage encryption

Status: Accepted (2026-11-23, PR merged by the user). Stage 1 built 2026-11-23 (live-checked against PBS 4.2);
stage 2 built 2026-11-24 (see "As built" below); stage 3 later. Supersedes the key model of ADR 0022 (one key per datastore).

## Context

PBS client-side encryption ("zero knowledge") makes a backup unreadable without the exact key used
to create it. On Proxmox VE the key belongs to the **PBS storage definition**, not to a backup job:
every job that writes to that storage uses it. RESURGAM manages PVE backup jobs (ADR 0023) but has no
idea which of them encrypt, with which key, or whether it could read the result.

ADR 0022 added one uploaded key per RESURGAM datastore and passes it to every `proxmox-backup-client`
call for that datastore. A live test on PVE 9.2 / PBS 4.2 (2026-11-22, encrypted test backup
`host/resurgam-enc-test`) showed:

| Case | Result |
|---|---|
| Encrypted backup, no key in RESURGAM | Fails with the client's raw message `missing key - manifest was created with key ad:a6:…` |
| Encrypted backup, key stored | Listing works |
| **Unencrypted** backup in the same datastore, key stored | **Fails**: `manifest has no signature, but an encryption key is set - refusing to trust a potentially tampered, unsigned backup` |

So with ADR 0022's model, storing a key makes every unencrypted backup in that datastore unreadable
through RESURGAM, which is the normal state of any datastore after switching to encryption. One key
per datastore also cannot cover a key change: older backups need the old key.

## Facts this design relies on (checked against the vendor's own code and a live system)

- **PBS snapshot metadata carries the key fingerprint.** `proxmox-backup-client snapshot list
  --output-format json` (PBS 4.2) returns, per snapshot, `"fingerprint": "ad:a6:…"` and per file
  `"crypt-mode": "encrypt" | "sign-only" | "none"`. The PBS API the collector already calls returns
  the same fields. RESURGAM can therefore know which backups are encrypted, and with which key,
  without holding any key.
- **PVE stores the key's fingerprint in `storage.cfg`.** `PVE::Storage::PBSPlugin` (libpve-storage-perl
  9.1.15, `on_add_hook`/`on_update_hook`): the key itself goes to `/etc/pve/priv/storage/<id>.enc`
  (root only); the storage config gets `encryption-key <fingerprint>` (or `1` if the key has none).
  The storage API therefore exposes which key a storage uses, never the key.
- **`encryption-key=autogen`** makes PVE generate a key without passphrase
  (`proxmox-backup-client key create --kdf none`) and return its content **once** in the API response.
  On replacement PVE keeps the previous file as `<id>.enc.old`.
- **`master-pubkey`** (optional, only together with `encryption-key`): an RSA public key; every
  backup then carries a copy of the encryption key encrypted with it, recoverable with the matching
  private key.

## Decision

Three stages. Stages 1 and 2 only read from PBS/PVE; stage 3 writes to PVE.

### Stage 1 — Visibility (read-only)

- The collector stores per snapshot the crypt mode (`none` / `encrypt` / `sign-only` as PBS reports
  it for the data archives) and the key fingerprint, and per backup group the fingerprint(s) in use.
- The PVE storage sync stores per PBS storage whether `encryption-key` is set and its fingerprint.
- UI: backup groups, snapshots, PVE backup jobs (through their storage) and datastores show
  "encrypted (key `ab:cd:…`)" or "not encrypted"; a datastore lists the fingerprints in use.
- Alarms:
  - **encrypted backups whose key RESURGAM does not hold** (restore through RESURGAM impossible);
  - **key change**: a group's newest snapshot uses a different fingerprint than the one before;
  - **PVE storage key ≠ backups**: a PVE storage's configured fingerprint differs from the one its
    newest backups carry (the key was replaced on PVE, or the storage points elsewhere).

### Stage 2 — Key store by fingerprint

- Keys are stored by **fingerprint**, any number of them, no longer one per datastore. On upload
  RESURGAM reads the fingerprint (`proxmox-backup-client key show`), rejects duplicates, and accepts
  password-protected keyfiles (the password is stored with the key, same credential store, and passed
  via `PBS_ENCRYPTION_PASSWORD`). A key can optionally be assigned to a tenant.
- **Every read passes exactly the key whose fingerprint the snapshot carries** — and **no key** for
  an unencrypted snapshot. That fixes the mixed-datastore failure above and covers key changes.
- Clear errors: "This backup is encrypted with key `ab:cd:…`, which RESURGAM does not have. Upload
  it under Administration → Encryption keys."
- **Key sheet**: per key, the printable paper key (`proxmox-backup-client key paperkey`, text/QR) as a
  PDF; an alarm until an admin confirms "key sheet stored safely".
- The ADR 0022 per-datastore key is migrated into the new store by its fingerprint.
- **Keys are downloadable** (required by the user, 2026-11-22: for migration and as a backup copy).
  This deliberately ends ADR 0022's write-only rule for keys:
  - **Per key**: the original keyfile exactly as uploaded or generated (PBS JSON format, usable
    directly with `proxmox-backup-client --keyfile` or as a PVE storage `encryption-key`); a
    password-protected key keeps its protection.
  - **Export of all keys**: one file encrypted with a passphrase the admin chooses (never plaintext),
    importable into another RESURGAM — the migration path to a new appliance.
  - **Protection**: only in a signed-in session, never with an API token; a fresh confirmation
    (password or MFA) right before the download; every download and export in the audit log (who,
    when, which fingerprints) and an alarm mail to the platform admins.
  - **Who** (decided by the user, 2026-11-22): platform admins always. Tenant admins may download the
    keys assigned to their own tenant only if a platform admin has allowed it for that tenant (off
    by default; the switch itself is audited). The all-keys export stays platform-admin only.

### Stage 3 — Encryption on PVE storages (later, separate step)

- In the PVE storage / backup-job management: "Encrypt backups on this storage" — generate a key
  (RESURGAM creates it and passes it to PVE, or uses `autogen` and captures the one-time response) or
  pick a stored one, optionally with a master public key.
- Activation only after the key sheet was downloaded and confirmed. Without that, no encrypted backup
  can be created through RESURGAM.
- Key change as a guided action: the old key stays in the store, the alarm from stage 1 explains that
  old and new backups need different keys.

### As built (stage 1 + 2), where it differs from the plan above

- **Password-protected keys are rejected for now** (`password_protected_key`), not stored with their
  password. PVE and vzdump only make keys without passphrase, so this covers every PVE-made key;
  the error tells how to convert a copy (`proxmox-backup-client key change-passphrase --kdf none`).
- **The fingerprint is read from the keyfile's own `fingerprint` field** (as PVE's `PBSPlugin` does),
  not by running `proxmox-backup-client key show`: the API container has no PBS client.
- **Key sheet**: one A4 PDF with the fingerprint, the full keyfile as text and as one QR code, and
  restore steps -- not PBS's `paperkey` output (same idea, no PBS client needed). Restore from it:
  scan or type it into a file and check it with `proxmox-backup-client key show`.
- **Fresh confirmation** (`auth/step_up.py`): password, or a TOTP code; break-glass: its password;
  SSO-only accounts: a sign-in no older than 10 minutes. API tokens never reach it.
- **Alarms built**: `encryption_key_missing` (one per datastore and missing key, critical),
  `encryption_key_copy_unconfirmed` (platform admins only), `backup_key_changed` (stage 1).
  **Not built yet**: the PVE-storage side of stage 1 (which PVE storage has which key configured, and
  the "PVE storage key ≠ backups" alarm) -- it moves to stage 3, which touches PVE storages anyway.
- **ADR 0022 keys** are moved into the store by the collector scheduler (every tick, a no-op once
  done) and detached from the datastore. `POST/DELETE /datastores/{id}/encryption-key` are kept as
  deprecated: POST adds the key to the store, DELETE only removes a not-yet-moved datastore key.
- **Configuration backups** still never contain keys; a datastore with encrypted backups is marked so
  the import asks for the key export.
- **Which RESURGAM paths use the per-backup key**: file browse/download/ZIP, file indexing
  (`external_mount`), Disk Mounts, Instant Recovery, Recovery Lab, guided restores. Guest-exec live
  indexing reads inside the running guest and needs no key. Instant Recovery, Disk Mounts, restore
  plans and lab plans refuse up front with `encryption_key_missing` (409) when the key is not stored.

## Not in scope

- RESURGAM is not the only copy of a key. The key sheet, a downloaded keyfile or the master key must
  exist outside RESURGAM; the UI says so.
- Moving existing backups to another key (re-encryption) — PBS cannot do that in place.
- Restores of encrypted VMs through PVE itself still need the key on the PVE storage; RESURGAM only
  shows whether it is there.

## Also found during the test (separate fix)

Downloads and ZIP exports from **host** backups assume the archive name `root.pxar`
(`filerestore/browse.py`, `CT_HOST_ARCHIVE_NAME`); host backups made with `proxmox-backup-client`
often use other names or several archives (`data.pxar`, `etc.pxar`), and the download fails with
`archive not found in manifest` while the listing works.
