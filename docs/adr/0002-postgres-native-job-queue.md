# 0002 — Postgres-native job queue

Status: Accepted (2026-09-11)

## Context

The manifest requires long-running actions to run asynchronously as jobs with a stable job ID, status retrievable via polling/SSE/WebSocket (§4.2), and an "authenticated internal queue" delivering typed jobs to the Restore Worker (§4, §15). It does not mandate a specific queue technology. Options considered: a dedicated broker (Redis + Arq/RQ, or Celery), or a Postgres-native queue (a jobs table consumed with `SELECT ... FOR UPDATE SKIP LOCKED`).

## Decision

Use a Postgres-native queue for the Control Plane's job/task system. PostgreSQL is already a mandatory dependency (§4.2: "FastAPI, SQLAlchemy, Alembic und PostgreSQL bilden den verbindlichen Backend-Stack"), so a `SKIP LOCKED`-based jobs table adds no new service, no new failure domain, and no new operational surface to the appliance — consistent with the manifest's general MVP-scope discipline (§1: "Implementiere keine Funktionen ausserhalb des definierten Scopes").

## Consequences

- One fewer service to package, secure, and monitor in the appliance (no Redis).
- Job throughput and polling latency are bounded by Postgres; acceptable for the MVP's expected job volume (file-restore sessions, sync/collector triggers, guided-restore preparation) but not a high-throughput event system.
- If job volume or fan-out requirements later exceed what Postgres polling handles comfortably, this can be swapped for Redis/Arq behind the same internal job-submission interface without changing the Restore Worker's job contract (§15's typed JSON job schema is queue-technology-agnostic).
- Applies to the Control Plane ↔ Restore Worker job queue introduced in Milestone 1. Milestone 0 (this repository's current state) has no queue at all.
