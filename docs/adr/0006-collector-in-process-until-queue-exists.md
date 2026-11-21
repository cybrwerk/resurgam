# 0006 — Collector runs in-process inside the backend until a job queue exists

Status: Accepted (2026-09-13)

## Context

The manifest treats the Collector as its own component (§4.1: "kann als unprivilegierter Worker oder Background Service laufen"), and the monorepo layout (ADR 0001) reserves a top-level `collector/` directory for it. But the Collector's job — normalizing PBS backup groups, snapshots, and tasks (§4.1, §9.4) — produces rows with real foreign keys into tables `backend/` owns (`Datastore`, `PbsInstance`). There is no queue or process boundary yet (ADR 0002's Postgres-native job queue is still unbuilt) for a genuinely separate process to receive work and share that schema safely.

## Decision

Implement the Collector's actual sync logic at `backend/src/resurgam_api/collector/sync.py`, invoked in-process from a `POST /api/v1/pbs-instances/{id}/sync` endpoint (on-demand only — no scheduler yet). It imports and writes through the same SQLAlchemy models and session as the rest of the backend, rather than duplicating them in a standalone package. The top-level `collector/` directory stays a placeholder documenting this reality and the plan to extract it.

## Consequences

- No model duplication or schema drift risk between two packages describing the same tables.
- The Collector is not yet "unprivileged" in the process-isolation sense the manifest describes — it runs with full backend privileges, inside the same FastAPI process, triggered by an API call rather than as a background service. This is a real, temporary gap from the target architecture, not a stylistic choice — it must be revisited once ADR 0002's job queue exists.
- Extraction path once the queue exists: the queue carries typed sync jobs (`{pbs_instance_id}`); a separate `collector/` process (or worker) consumes them, using its own DB session against the same schema `backend/`'s Alembic migrations already own. The normalization functions in `collector/sync.py` are already written as plain functions taking a `Session` and `Settings` — they don't depend on FastAPI request context, so moving them to a standalone process later is a relocation, not a rewrite.
- Sync is on-demand only in the meantime — an operator must call the endpoint themselves; there is no periodic/automatic refresh of inventory or task history yet.
