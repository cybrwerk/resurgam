# 0009 — Billing foundations: datastore role, per-group customer override, pricing plans

Status: Accepted (2026-09-14)

## Context

Billing (per-VM + per-GB fees, manifest not covering this at all — a requirement gathered conversationally) needs two things today's data model doesn't support: (1) a shared datastore holding VMs for more than one customer, and (2) a way to avoid double-counting a VM that's backed up to more than one PBS instance (e.g. a primary + an offsite/DR copy).

## Decisions

### Datastore role (primary/offsite), not cross-instance VM identity matching

The obvious way to solve "don't double-count a VM backed up to two PBS instances" is a cross-instance "protected system" identity that multiple `BackupGroup` rows map to. That was rejected: backup-type + backup-id (VMID) isn't guaranteed unique across different PVE clusters/PBS instances, so automatic matching risks silently merging two different customers' VM 100 into one billing record — a tenant-isolation bug, not just a cosmetic one. Manual linking was considered too, but adds a data-entry step for every DR pair.

Instead: `DatastoreCustomerAssignment.role` (`primary` | `offsite`, default `primary`, not from the manifest). A customer's per-VM billing count only includes backup groups under `primary`-role datastores; `offsite`-role datastores still count toward per-GB billing (it's real disk consumed) but not per-VM. This reframes "is this the same VM as that one" (hard, needs cross-instance identity) into "is this datastore the customer's primary copy or a secondary one" (easy — the operator setting up the assignment already knows this, no matching heuristic needed).

### Per-backup-group customer override, not a new Namespace entity

A shared datastore can now hold VMs belonging to different customers via a nullable `BackupGroup.customer_id` override — null means inherit the datastore's own `DatastoreCustomerAssignment`. A namespace-level tier (between datastore and group) was considered — matching the earlier idea of extending the RBAC `ScopeType` most-specific-wins pattern down through namespace to group — but deferred: namespaces aren't a first-class entity yet (just a string column on `BackupGroup`), and no live data has actually demonstrated a need for a namespace-level tier specifically. Two levels (datastore, group) cover the stated shared-datastore billing case; a third tier is easy to add later if it's actually needed.

Scope resolution (`auth/scope.py::effective_group_customer_id`) is applied per-group, not per-datastore: `GET /datastores/{id}/backup-groups` no longer requires the whole datastore be in the caller's scope — it filters the returned groups by their own effective customer, and only 404s the whole listing when *nothing* in it (neither the datastore's own assignment nor any individual group override) is visible, preserving the "don't confirm existence of an out-of-scope object" rule at the new finer granularity. `GET /backup-groups/{id}/snapshots` and `POST /backup-groups/{id}/assign-policy`'s customer-match check were updated the same way.

### PricingPlan: global default + per-customer override, data model and CRUD only

`pricing_plans`: `customer_id` nullable (null = global default), `price_per_vm_month`, `price_per_gb_month`, `currency`. At most one global plan and at most one plan per customer are enforced with partial unique indexes (`WHERE customer_id IS NULL` / `WHERE customer_id IS NOT NULL`), not just application logic — Postgres treats every `NULL` as distinct under a plain `UNIQUE` constraint, so a plain constraint would silently allow multiple global plans. This slice is data model + CRUD only — no invoice/report generation, no "what does this customer effectively pay" resolution endpoint yet.

### Billing computation (`inventory/billing.py`, `GET /billing/customers[/{id}]`) — added 2026-09-14

The gap named in "Consequences" below is now closed: `compute_customer_billing()` reads exactly as predicted — per customer, count `primary`-role backup groups (own override, else the datastore's assignment) for the per-VM fee; for the per-GB fee, every matching group counts (primary and offsite both, since it's real disk either way), each group's own logical bytes (its retained snapshots' summed `size_bytes`) scaled by *its own datastore's* dedup ratio (`UsageMeasurement.used_bytes / logical_bytes`, both already aggregate per-datastore figures) — not a single datastore-wide ratio applied everywhere, since a customer's VMs can span more than one datastore with different dedup characteristics. A datastore with no assignment row is treated as `primary` (the same default the column itself uses); a datastore with no usage measurement yet contributes 0 bytes for its groups and flips `physical_bytes_estimate_complete` to `false` rather than blocking the whole computation — same "partial rather than blocked" philosophy as `compute_usage_changes` returning `None`. `estimated_monthly_cost` is quantized to 2 decimal places (it's an estimate, not an invoice line — the raw `Decimal` division otherwise carries far more digits than any currency uses). Computed fresh on every call, never stored, same pattern as protection/usage/alarms. Verified against real data on the dev PBS (135 groups, ~2.16TB estimated physical bytes, correctly matching `135 × price_per_vm + 2159.46GB × price_per_gb`).

### Per-unit pricing split VM/CT/Host — added 2026-09-14

`PricingPlan.price_per_vm_month` became three rates — `price_per_vm_month`/`price_per_ct_month`/`price_per_host_month` — requested directly, not from the manifest. A VM, an LXC container, and a bare-metal agent backup are typically priced differently in practice, and `BackupType` (vm/ct/host) already distinguishes them everywhere else in the system (usage counts, the Datastores tree). `compute_customer_billing()` now counts each type separately (still gated the same way — `primary`-role datastores only, per the role decision above) and applies the matching rate; `CustomerBillingOut` gained `ct_count`/`host_count` alongside `vm_count`. Migration 0012 backfills the two new rates to `0`, not the existing VM rate, on every pre-existing plan — silently starting to charge for a unit type that was previously free (because nothing counted it) would be a real billing surprise, not a safe default.

## Consequences

- Billing computation exists now (see above) — this was the stated gap when this ADR was first written; no longer open.
- Per-unit pricing is VM/CT/Host, not a single flat rate — see the addendum above.
- If a true cross-instance protected-system identity is needed later (e.g. per-VM restore-point SLAs spanning instances), it can be added without disturbing this slice — `role` and the per-group override are both orthogonal to it.
- `docs/architecture-and-dependencies.md` records the customer-scope-resolution change; this ADR records *why* the simpler role-based approach was chosen over cross-instance matching.
