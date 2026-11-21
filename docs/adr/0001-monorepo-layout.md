# 0001 — Monorepo layout

Status: Accepted (2026-09-11)

## Context

The manifest describes RESURGAM as composed of several independently deployable components: web UI, control plane API, collector, restore worker, and the appliance build itself. §21 (the Milestone 0 task list) phrases the repository structure as a single set of top-level directories (`frontend`, `backend`, `collector`, `restore-worker`, `appliance`, `docs`), while §25.1 separately lists component names as if they map to independent repos (`resurgam-api`, `resurgam-web`, `resurgam-collector`, `resurgam-worker`, `resurgam-lab-agent`, `resurgam-appliance`). The two are not contradictory, but a choice has to be made for how the code is actually organized before any code is written.

## Decision

Use a single monorepo (this repository) with one top-level directory per component, matching §21's phrasing. Component names from §25.1 are used for published artifacts (container images, PyPI/npm package names) rather than for separate source repos.

## Consequences

- Single CI pipeline and single versioning scheme to start; simpler for the current project stage (pre-MVP, no external contributors yet).
- Cross-component changes (e.g. an API contract change touching both `backend/` and `frontend/`) land in one commit/PR instead of being coordinated across repos.
- If a component later needs independent release cadence or external-only visibility (e.g. `resurgam-lab-agent` distributed separately to lab nodes), it can be split out of the monorepo at that point; this decision does not preclude that.
