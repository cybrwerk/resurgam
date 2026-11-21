# 0008 — Frontend stack, OIDC browser handoff, and the dedicated styleguide

Status: Accepted (2026-09-13)

## Context

Milestone 2 (manifest §17) starts the frontend. Building a real browser-based client surfaced two things the backend-only Milestones didn't need to decide, plus a dedicated design document arrived mid-build that refines the manifest's own §26 design system.

## Decisions

### Frontend stack

- **Vite + React + TypeScript** (manifest §4.2, §25.1 mandate React/TS; Vite is the standard pairing).
- **react-router** for routing.
- **TanStack Query** for server-state (loading/error/caching) rather than ad hoc `useEffect` fetching — the app is list/detail-heavy and every page needs the same loading/error/empty handling (`components/QueryState.tsx` centralizes that pattern once).
- **react-i18next** for German/English, browser-language auto-detected on first visit, explicit choice persisted after. Confirmed live: a browser with a German locale renders German by default with no code path change needed — this is the language detector working as designed, not a bug that surfaced during testing.
- **Plain CSS with custom properties** (CSS Modules for component-scoped styles, a small global `app.css` for shared primitives like buttons/tables/forms), not a utility framework — the design tokens are precisely specified (colors, spacing, radii; see below) and a utility framework would fight, not help, matching them exactly.
- **Vitest + React Testing Library**, the standard Vite-native pairing.

### OIDC: backend callback became a browser redirect, not a JSON response

`GET /auth/oidc/callback` previously returned `{access_token, ...}` as JSON (ADR 0007), reasonable when there was no frontend to hand a session to. A real SPA can't read a JSON body returned to a navigation a third party (authentik) initiated — the browser lands on the backend's callback URL directly, not under frontend JS control. The callback now redirects (302) to `{FRONTEND_SUCCESS_URL}#access_token=...&token_type=bearer&expires_in=...` — a URL fragment, never transmitted to any server, read client-side only by `/auth/callback` (`AuthCallbackPage.tsx`). A failed login redirects to `{FRONTEND_ORIGIN}/login?oidc_error=<code>` instead of returning a JSON error body to the same dead-end. Break-glass login is unaffected — the frontend calls it directly via `fetch`, no third-party redirect involved, so a JSON response was already correct there and still is.

New settings: `FRONTEND_ORIGIN` (also used for CORS — `CORSMiddleware` added to `main.py`) and `FRONTEND_SUCCESS_URL`.

### The dedicated styleguide supersedes manifest §26 for frontend specifics

`docs/design/RESURGAM-Styleguide.md` (saved into the repo, provided mid-build) is a more detailed, more recent design document than the manifest's own §26. Where they conflict — palette (petrol/brass/bordeaux with different hex values than §26.3's teal/brass/bordeaux), typography (Inter instead of IBM Plex Sans for UI text), layout dimensions, and the navigation tree (§8.1, substantially reorganized from §26.2: "Operations" → "Jobs", "Recovery Lab" moves under a new top-level "Recovery", Customers moves under "Administration", plus new sections like Reporting) — the styleguide wins for anything actually implemented in the frontend. `src/styles/tokens.css` and `src/nav/navTree.ts` follow it exactly, not §26.

Four of the new nav's "Jobs" children (Backup Jobs, Verification, Prune & Garbage Collection, Replication & Sync) are real, not placeholders — they share one `TasksPage` component and one underlying task list (the Collector already normalizes all task types together), each filtering client-side by `task_type` rather than being four separately-faked pages.

## Consequences

- Any future OIDC provider beyond authentik follows the same redirect-with-fragment contract; the pattern isn't authentik-specific.
- Sections of the new nav tree with no backend yet at the time (Namespaces, PVE targets' distinct restore-target semantics vs. plain connection management, Reporting, Credentials, Users & Roles admin UI, Audit Log) rendered the shared "not part of this milestone yet" placeholder — the IA is visible as designed, matching the styleguide's own principle that structure should be honest about what's real. Namespaces, Users & Roles, and Audit Log have since shipped (see `docs/architecture-and-dependencies.md`'s running log). **"PVE targets' distinct restore-target semantics" was investigated (2026-09-16) and resolved as a non-issue, not built as a separate concept**: manifest §6.2 defines exactly one entity, `PveCluster`, with the exact fields already implemented (display name, API endpoint, TLS trust, token reference, allowed target storages, restore network rules, PBS-instance links); the styleguide's own nav IA (§8.1) uses "PVE-Ziele" (PVE Targets) purely as a role label — PVE clusters are always restore *destinations* in this system, never backup sources — not a hint at a distinct data model. This is now doubly confirmed in practice: Guided Restore (manifest EPIC 8, ADR 0012) uses `PveCluster` directly as its restore-target source for live node/storage/VMID queries, which is exactly the "restore-target semantics" this bullet worried didn't exist yet. No code change was needed; `PveClustersPage`/`PveCluster` already correctly serve this nav entry.
- `manifest §26` remains the historical record of the original design intent; it is not edited to match the styleguide (manifest is the product spec, versioned separately) — `docs/architecture-and-dependencies.md` and this ADR are where the supersession is recorded for anyone reconciling the two later.
