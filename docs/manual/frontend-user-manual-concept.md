# Concept: RESURGAM frontend user manual

Status: Concept — not yet written. This document defines scope, audience,
structure, and format for a real end-user manual of the RESURGAM web UI,
distinct from [`index.md`](index.md), which is a developer/operator
setup guide (how to run the three components locally). This manual is
for the people who use the running application day to day: backup
operators, restore operators, customer admins, auditors, and platform
admins (the six roles in `RoleKey`, manifest §7) — not for someone
building or deploying RESURGAM.

## 1. Audience and how it shapes the manual

RBAC in this app is **not** route-level — every authenticated user can
navigate to every nav entry (`ProtectedRoute` only checks "logged in or
not"). What differs per role is (a) which actions succeed (backend
returns 403, `QueryState` renders a "you don't have access to this"
empty state) and (b) which rows are visible at all (`visible_customer_ids`
scoping — a `customer_admin` only ever sees their own customer's data).

This means the manual **cannot** organize chapters by "what platform
admins see" vs. "what restore operators see" — everyone sees the same
pages. Instead, every chapter documents, per significant action: **which
role(s) can do this**, stated plainly (e.g. "Executing a restore plan
requires `restore_operator` or `platform_admin`"), so a customer_admin
reading the Restore Center chapter understands why the Execute button
is disabled for them without guessing.

The six roles, for reference when writing "who can do this" notes:

| Role | Scope shape | Typical reader |
|---|---|---|
| `viewer` | read-only, scoped | someone who just needs to see status |
| `backup_operator` | manages backup-side config | the person who runs the daily backup ops |
| `restore_operator` | can approve/execute restores | the person who runs DR drills and real restores |
| `customer_admin` | full control, one customer only | an MSP customer's own IT admin |
| `auditor` | read-only, sees audit trail | compliance/audit reader |
| `platform_admin` | full control, all customers | the RESURGAM operator's own staff |

## 2. Structure: mirror the nav tree, not an invented taxonomy

`frontend/src/nav/navTree.ts` is already the authoritative statement of
the app's information architecture — RESURGAM's own manifest-derived IA
(§26.2/Styleguide §8.1). The manual's chapters should mirror it exactly,
section for section, rather than re-deriving a "logical" grouping that
would drift from the real nav the first time it changes:

```
1. Getting started        (login, MFA, break-glass, SSO, profile, language/theme)
2. Overview                → /
3. Infrastructure          → PBS Instances, Datastores, PVE Targets
4. Backups                 → Virtual Machines, Containers, Hosts (File Backup: placeholder, skip)
5. Jobs                    → Backup, Verification, Prune & GC, Replication & Sync
6. Recovery                → Restore Center, File Recovery, File Index, Recovery Lab (Disk Mounts: placeholder, skip)
7. Reporting                → Capacity & Trends, Tenant Usage, Recovery Evidence
8. Administration           → Customers, Protection Policies, Pricing Plans, Users & Roles,
                              Credentials, Integrations, System Jobs, Audit Log
                              (System Settings: placeholder, skip)
```

**Only pages in `IMPLEMENTED_PATHS` get a chapter.** A nav entry not yet
backed by a real page (`File Backup`, `Disk Mounts`, `System Settings`
today) is skipped entirely rather than pre-written against a page that
doesn't exist — same "don't document vaporware" discipline the app
itself follows for its own nav (`navTree.ts`'s own comment: "nothing
here is faked as working"). When a placeholder becomes real, it gets a
chapter in the same PR that ships it (§5).

## 3. Per-page chapter template

Every chapter follows the same shape, so a reader learns the format once
and every subsequent page is faster to scan:

```markdown
## <Page title>

<One sentence: what this page is for, in plain language.>

**Who can do what here:**
- <action> — requires <role(s)>
- <action> — requires <role(s)>
(omit this block entirely for a page that's pure read-only for everyone)

**Before you start:** <real prerequisites — e.g. "a PBS instance must
already be connected under Infrastructure > PBS Instances">

### Walkthrough
1. <step>
2. <step>
...

### Common errors
- "<real error message or code>" — <what it means, what to do>
```

The "Common errors" section is deliberately sourced from real
`ApiError` codes this app actually returns (`error_code`s already
defined per endpoint), not invented examples — grep the relevant
`api/v1/*.py` router for `ResurgamApiError(code=...)` calls when writing
each chapter, so every documented error is one a user can actually hit.

## 4. Format and tooling

**Plain Markdown in `docs/manual/`, one file per nav section** (e.g.
`manual/infrastructure.md`, `manual/recovery.md`), not one monolithic
file and not a separate static-site generator (MkDocs/Docusaurus) for
now — this repo has no publishing pipeline today, and standing one up is
a separate decision with its own cost (hosting, CI, another dependency
to keep current) that shouldn't be bundled into writing the first
chapter. `index.md` gains a "User manual" section linking each file,
mirroring how it already links nothing external.

**No screenshots in the first pass.** A static screenshot goes stale the
next time a form field moves, and this app changes fast (this session
alone shipped a new page). Text that names exact field labels and
button text (which the i18n files make copy-pasteable — `t("recoveryLab.execute")`
→ "Execute") stays accurate through minor layout changes in a way a PNG
doesn't. If screenshots turn out to matter (e.g. onboarding a
non-technical customer_admin), generate them from the same Playwright
driver pattern used for this session's live UI verification — scripted
and regenerable, not hand-captured — as a later, separate pass once the
page set has stabilized.

**English only for the first pass**, even though the app itself is
EN/DE. Maintaining two manual languages in lockstep with an
actively-changing UI is real ongoing cost; the app's own i18n already
lets a German-speaking user run the UI in German while reading an
English manual (field names differ, but the walkthrough logic doesn't).
Revisit once there's an actual DE-speaking user base asking for it,
rather than building it speculatively.

## 5. Maintenance discipline

**A new page ships with its manual chapter in the same commit/PR**,
the same discipline this project already applies to ADRs (a new
architectural decision gets its ADR when it's made, not backfilled
later) and to `docs/architecture-and-dependencies.md`'s changelog
paragraph. A page merged without its chapter is treated the same as a
nav entry merged without being added to `IMPLEMENTED_PATHS` — an
inconsistency to fix immediately, not a follow-up ticket.

## 6. Suggested writing order (once this concept is approved)

Not alphabetical — start where the manual earns its keep fastest:

1. **Getting started** (§1 above) — every reader needs this first, and
   it's stable (login mechanics change rarely).
2. **Recovery Lab** — the newest, least self-explanatory page (Application
   Groups + Lab Runs, two tabs, a multi-step run lifecycle) — the
   highest-value chapter to write while the mechanics are freshest.
3. **Restore Center** — the other multi-step wizard-driven flow, and the
   one with real operational risk (four-eyes approval, execution).
4. **Infrastructure** (PBS Instances, Datastores, PVE Targets) —
   foundational setup every other chapter assumes has already happened.
5. Everything else, in nav order.

## 7. Worked example (proves the template holds up)

The following is a first draft of one real chapter, written against the
template above, to validate it before committing to it for the other
~20 pages. If this shape works well once written, the other chapters
follow the same skeleton; if it doesn't, revise the template here first
rather than in twenty already-written files.

---

## Recovery Lab

Recovery Lab proves that a backup actually restores and boots — not
just that the backup job succeeded. It restores an Application Group's
member VMs into an isolated network on a designated PVE cluster and
runs escalating checks against each one (guest agent → pingable →
scripted test), without waiting for a full copy first.

**Who can do what here:**
- Creating/editing/deleting an Application Group — requires
  `restore_operator` or `platform_admin` for that group's customer
- Creating and executing a Lab run — requires `restore_operator` or
  `platform_admin` for the run's customer
- Viewing existing groups and runs — any role with read access to that
  customer

**Before you start:**
- At least one PVE cluster must be enabled for Recovery Lab (under
  Infrastructure > PVE Targets > edit a cluster > "Recovery Lab
  compute"), with a probe appliance template VMID set.
- `resurgam-lab-agent` must be installed and running on that cluster
  (an operator/infrastructure task — see the cluster's "resurgam-lab-agent
  token" section for the credential it needs).
- The Application Group's members need at least one existing snapshot
  each — Recovery Lab always restores the most recent one.

### Walkthrough — defining an Application Group

1. Open Recovery Lab > **Application Groups** tab.
2. Click **New Application Group**, pick the customer, name the group
   (e.g. "Order Processing").
3. Click **Add member** for each VM in the group. For each member: pick
   its backup group, set a startup order (lower runs first — a database
   should start before the app server that depends on it), and pick
   check types:
   - **Level 1 — Guest agent**: cheapest, no network needed.
   - **Level 2 — Pingable**: needs the guest agent's reported IP first.
   - **Level 3 — Scripted test** (TCP port / HTTP / HTTPS / DNS / TLS
     handshake / SSH banner): proves the actual service works, not just
     that the VM exists.
4. Save.

### Walkthrough — running a Lab test

1. Open the **Lab Runs** tab, click **New Lab run**.
2. Pick the Application Group, a Recovery-Lab-enabled PVE cluster, a
   node, and a storage.
3. Click **Create** — this resolves each member's most recent snapshot
   but restores nothing yet.
4. Open the created run and click **Execute**. This provisions the
   isolation network and probe appliance and hands the first member off
   to `resurgam-lab-agent`. **Copy the appliance run token shown here —
   it is displayed exactly once.**
5. The run polls automatically while `running`; each member's status
   and probe results appear as they arrive. Click **Poll now** to check
   sooner than the automatic interval.
6. When finished, click **Clean up** to tear down the isolation network
   and stop any in-flight work. This does not currently destroy the
   restored/appliance VMs themselves — remove those on the PVE cluster
   directly if needed (a known current limitation, not an oversight).

### Common errors

- `not_a_lab_cluster` — the picked PVE cluster doesn't have Recovery Lab
  compute enabled. Enable it under PVE Targets first.
- `lab_appliance_template_not_configured` — the cluster is Lab-enabled
  but has no probe appliance template VMID set.
- `empty_application_group` — the group has no members; add at least one.
- `ct_not_yet_supported` — a member's backup group is a container, not a
  VM. Recovery Lab currently only restores VMs.
- `single_node_zone_requires_shared_node` — the cluster's SDN zone can't
  span nodes (a "Simple" zone); every member's target node must match.
- `already_executed` — this run was already executed; create a new run
  to test again.
