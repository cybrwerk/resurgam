# Milestone 0 assumptions to validate against a real PBS instance

Per manifest §21 task 7: this repository was built without access to a live PBS 4.2.x instance. Everything below is an assumption inferred from the manifest's documented command examples and general PBS CLI conventions, and **must** be verified against a real `proxmox-file-restore`/`proxmox-backup-client` (PBS 4.2.x) before Milestone 1 (or any production use) relies on it.

See [`milestone-0-manual-verification.md`](./milestone-0-manual-verification.md) for a step-by-step checklist to run these checks on a machine that actually has the PBS client tools installed.

## CLI output assumptions

- `proxmox-file-restore list <snapshot> <path> --repository '<repo>' --output-format json` produces a JSON array of objects, each with at least `filepath`/`path`, `type` (e.g. `file`, `directory`), and `size` fields. **Not verified against real output** — field names/casing/nesting may differ.
- `proxmox-file-restore status --output-format json` produces a JSON array or object describing active restore sessions (session name, PID/start time, state). **Not verified.**
- `proxmox-file-restore extract ... <target>` exits 0 on success and writes to `<target>`; when `<target>` is `-`, file bytes stream to stdout rather than JSON. The wrapper in this repo treats extract-to-stdout as a separate code path from the JSON-returning commands. **Not verified against real streaming behavior/framing.**
- `proxmox-file-restore stop <name>` and `--version` output formats (plain text vs. JSON) are assumed rather than confirmed.

## Environment assumptions

- `/dev/kvm` presence + read/write access is assumed to be a sufficient proxy for "nested virtualization is usable" per manifest §5.2. Actual QEMU-based restore-environment behavior (e.g. what `proxmox-file-restore` does internally when KVM is unavailable) has not been observed.
- `/dev/fuse` is only required for `mount`-based operations, which are explicitly out of scope for Milestone 0/MVP (manifest §10.4, §12) — its detection here is forward-looking only and not exercised by any current wrapper.

## Error handling assumptions

- Non-zero exit codes from any wrapped command are assumed to indicate failure; stderr content is assumed to be human-readable diagnostic text, not structured data. Real PBS error output format (e.g. whether errors are ever emitted as JSON on stdout even with a non-zero exit code) is unconfirmed.

## How to close these

Run each wrapped command against a real PBS 4.2.x test instance (per manifest Milestone 0 spike goal), capture actual JSON/text output (anonymized), replace the hand-written fixtures in `restore-worker/src/resurgam_worker/tests/fixtures/` with real captures, and update the Pydantic models in `pbs_client/models.py` to match observed fields exactly. Remove each item from this list only once verified.
