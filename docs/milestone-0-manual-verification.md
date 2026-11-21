# Milestone 0 — manual verification against a real PBS client

This repository's `restore-worker` code was written and tested with **hand-written fixtures**, because the development sandbox has neither `proxmox-backup-client` nor `proxmox-file-restore` installed and no live PBS instance reachable. Manifest §21 task 7 requires validating those assumptions against the real tools before Milestone 1 relies on them — this is that checklist, meant to be run on a machine that actually has the Proxmox client tooling (or can install it), such as your Zorin machine.

Cross-reference: [`milestone-0-assumptions.md`](./milestone-0-assumptions.md) lists exactly which assumptions this exercise should confirm or correct.

## 0. Prerequisites

- `proxmox-backup-client` installed. Zorin is Ubuntu/Debian-based, so the [official PBS client apt repo](https://pbs.proxmox.com/docs/installation.html#package-repositories) applies; `proxmox-file-restore` ships in the same package.
- Python 3.12+ and `uv` (for running this repo's wrapper code directly — not required just to poke the CLI by hand).
- Optional but more valuable: network access to a real PBS datastore with at least one VM and one CT/host snapshot, and an API token for it. Without this, only steps 1 and 2 below are possible — steps 3+ need a real repository/snapshot to point at.

## 1. Confirm the binaries and capture `--version` output

```bash
proxmox-backup-client --version
proxmox-file-restore --version
```

Record the exact output verbatim (paste into the "closing the assumptions" section below). This confirms/corrects the "`--version` output format" assumption.

## 2. Run this repo's capability check against the real binaries

```bash
cd restore-worker
uv sync
uv run python -c "
from resurgam_worker.capability.checks import run_capability_check
report = run_capability_check()
for check in report.checks:
    print(f'{check.name}: {check.status} — {check.message}')
print('overall:', report.overall_status)
"
```

Expected: both binaries `OK` (assuming step 1 succeeded), and `/dev/kvm`/`/dev/fuse` reported per your machine's actual devices — Zorin desktop hardware likely has `/dev/kvm` if virtualization is enabled in BIOS, but this is not required for this exercise.

## 3. Capture real `list` output (needs a real repository + snapshot)

```bash
proxmox-file-restore list '<snapshot>' / \
  --repository '<auth-id>@<server>:<datastore>' \
  --output-format json
```

Save the raw output to a file. Compare its shape against `restore-worker/src/resurgam_worker/tests/fixtures/list_result.json` and against the `FileRestoreListEntry` model in `restore-worker/src/resurgam_worker/pbs_client/models.py` — note every field name/type mismatch.

## 4. Run this repo's `list_path()` wrapper directly against the real repo

```bash
cd restore-worker
uv run python -c "
from resurgam_worker.pbs_client.file_restore import list_path
entries = list_path('<auth-id>@<server>:<datastore>', '<snapshot>', '/')
for e in entries:
    print(e)
"
```

If this raises `PbsOutputParseError`, that confirms the model needs adjusting — capture the raw output it failed on (the exception carries it) and use it to fix `models.py`.

## 5. Capture real `status` output

```bash
proxmox-file-restore status --output-format json
```

Do this both with zero active sessions and, if feasible, while a `list`/`extract` operation is in flight, to see the "running" shape. Compare against `status_result.json` and `RestoreSessionStatus`.

## 6. Exercise `extract` (single file) and `extract_to_bytes`

```bash
cd restore-worker
uv run python -c "
from resurgam_worker.pbs_client.file_restore import extract, extract_to_bytes
result = extract('<auth-id>@<server>:<datastore>', '<snapshot>', '/etc/hostname', '/tmp/resurgam-extract-test')
print(result)
data = extract_to_bytes('<auth-id>@<server>:<datastore>', '<snapshot>', '/etc/hostname')
print(len(data), 'bytes')
"
```

Confirm both succeed and that `/tmp/resurgam-extract-test` contains the expected file content. Delete the test file afterward — do not leave real restored content lying around outside the test.

## 7. Exercise `stop` (optional, only if you deliberately leave a session running)

Start a `list` against a large directory in the background, note the session name from `status()`, then:

```bash
uv run python -c "
from resurgam_worker.pbs_client.file_restore import stop
stop('<session-name>')
"
```

## Closing the assumptions

For each item in `docs/milestone-0-assumptions.md` that this exercise touched:

1. Update the assumption text with what was actually observed (or mark it confirmed as-is).
2. If real JSON output differs from the hand-written fixtures, replace the fixture file(s) under `restore-worker/src/resurgam_worker/tests/fixtures/` with the real (anonymized — strip real hostnames/tokens/paths) output, and adjust the Pydantic models in `pbs_client/models.py` plus the existing tests in `restore-worker/src/resurgam_worker/tests/` to match.
3. Remove the closed item from `milestone-0-assumptions.md`, or note "confirmed as documented" if no change was needed.

Do not commit real datastore names, API tokens, or unredacted internal hostnames/paths into fixtures or this document — manifest §14 ("Backup-Daten niemals in Applikationslogs schreiben") applies to test fixtures too.
