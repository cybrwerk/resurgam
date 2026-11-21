# 0003 — Synchronous SQLAlchemy

Status: Accepted (2026-09-12)

## Context

FastAPI supports both sync (`def`) and async (`async def`) endpoints, and SQLAlchemy 2.0 supports both sync and async engines/sessions. The manifest mandates SQLAlchemy + Alembic + PostgreSQL (§4.2) but does not mandate sync vs. async.

## Decision

Use synchronous SQLAlchemy (`Session`, `create_engine`, `psycopg` in its sync mode) for the Control Plane, at least through Milestone 1. FastAPI runs sync endpoint functions in a worker thread pool automatically, so this does not block the event loop for other requests; it does mean a single request's DB work occupies one thread for its duration.

## Consequences

- Simpler session lifecycle and simpler, more standard test patterns (e.g. the SAVEPOINT-per-test pattern used in `backend/tests/conftest.py` is well-documented for sync SQLAlchemy).
- No need for `asyncpg`/async session plumbing, async Alembic migration runners, or async-aware dependency injection throughout the codebase.
- Ceiling on concurrent DB-bound requests is the size of FastAPI's default thread pool, not the event loop — acceptable for the expected MVP load (an operations control plane for a bounded number of PBS/PVE instances and users, not a high-concurrency public API).
- If a real throughput bottleneck appears later (e.g. many simultaneous collector-triggered queries), this can be revisited component-by-component; it does not need to be an all-or-nothing rewrite since FastAPI allows sync and async endpoints to coexist in the same app.
