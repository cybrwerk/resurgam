# collector

This directory is a placeholder. The Collector's actual logic — normalizing PBS backup groups, snapshots, and task history into RESURGAM's data model (manifest §4.1, §9.4) — is implemented today at [`../backend/src/resurgam_api/collector/`](../backend/src/resurgam_api/collector/), running in-process inside the backend and triggered on-demand via `POST /api/v1/pbs-instances/{id}/sync`.

This is deliberate, not an oversight: see [`../docs/adr/0006-collector-in-process-until-queue-exists.md`](../docs/adr/0006-collector-in-process-until-queue-exists.md). The Collector needs the same database models the backend owns (`Datastore`, `PbsInstance`), and there's no job queue yet (`../docs/adr/0002-postgres-native-job-queue.md`) for a truly separate, unprivileged worker process to receive work across a process boundary. Once that queue exists, this directory becomes the real standalone service the manifest describes, and the sync functions move here largely as-is — they're already written as plain functions independent of any web-request context.

Until then: no periodic/scheduled sync, no independent process, no separate privilege boundary from the rest of the backend.
