# 0026 — Release update source: GitHub/GitLab release check and bundle download

Status: Accepted (2026-10-09)

## Context

ADR 0024 delivers updates as signed bundles that an operator uploads in System Settings → Updates
(or copies onto the appliance). Each release therefore meant downloading ~420 MB to a workstation
and uploading it again, and nobody learned about a new release unless they went looking. The user
asked for "a repo checker which pulls versions from there", with a personal access token entered in
the web UI and linked to the repository, so the appliance keeps itself informed. RESURGAM lives on
GitHub (`cybrwerk/resurgam`, private); the user wanted GitLab to work the same way "just
putting a URL otherwise in".

## Decision

### One repository URL, two providers
The operator pastes the repository's web URL. `updates/release_source.py` derives everything else:

| provider | API base | repository path | auth header |
|---|---|---|---|
| `github` | `https://api.github.com` for github.com, `https://<host>/api/v3` otherwise (Enterprise Server) | `owner/repo` | `Authorization: Bearer <token>` |
| `gitlab` | `https://<host>/api/v4` | full project path (`group/sub/project`, URL-encoded) | `PRIVATE-TOKEN: <token>` |

The provider is guessed from the host name (`github`/`gitlab` in it) and can be overridden for a
neutral self-hosted name. A trailing `.git`, `/releases` or GitLab's `/-/…` suffix is ignored.

### Releases and bundles
- A release counts when its tag is a release version (`v0.3.0`, `0.3.0`, `resurgam-0.3.0` →
  `0.3.0`; plain `MAJOR.MINOR.PATCH` as in ADR 0024). GitHub drafts and GitLab upcoming releases
  are skipped; GitHub pre-releases only when the operator opts in.
- Its bundle is the asset named `resurgam-<version>.bundle`, else the only `*.bundle` asset. A
  newer release without one is still listed ("no bundle attached") but cannot be installed.
- GitHub: the asset's API URL with `Accept: application/octet-stream` (answers 302 to pre-signed
  storage). GitLab: the release link's `direct_asset_url`; `publish_release.py` puts the bundle into
  the project's generic package registry and links it.

### The token
Stored like the SMTP password: a `SecretReference` (new purpose `update_source_token`, its own HKDF
subkey) on the singleton `update_source_settings` row; write-only through the API (omit = keep,
`null` = remove). It is sent **only to the repository's API host, over the API base's scheme**:
redirects are followed by hand, the header is dropped on any hop to another host (GitHub's asset
storage) or another scheme, and a download redirect is only followed to `https` (plain `http` only
between hops on the API host of a source configured as `http://`, never as a downgrade). A failed
download reports only the HTTP status, never the body of whatever host the redirects ended on.
The stored token is bound to its source: saving the settings with another provider or API host
(scheme + host) and no new token removes it (`token_cleared: true` in the answer, shown in the UI),
and `POST /updates/source/test` without a token only uses the stored one when provider and API host
match the saved source (otherwise it tests without one and answers `stored_token_withheld: true`) —
so a token can never be pointed at a host nobody saved it for. Recommended scope is
read-only — GitHub fine-grained *Contents: read* on the one repository, GitLab `read_api`. The
release builder's write token (`publish_release.py`) never goes onto an appliance.

### Trust is unchanged
A downloaded bundle is not trusted because of where it came from. It lands in the same
`incoming/` directory as an upload; the backend checks that its manifest version equals the release
tag and pre-checks the signature against the baked-in release key; the host updater's `verify` and
`apply` (ADR 0024) are authoritative and unchanged. A compromised repository or token can therefore
only offer a bundle that gets rejected — or withhold updates.

### Checking: cached, scheduled, never auto-installing
- `POST /updates/source/check` and the collector scheduler (`run_due_check`, every
  `check_interval_hours`, default 24, when `auto_check` is on) store the newer releases on the
  settings row. `GET /updates/source` only reads that cache, so opening the page never waits on
  GitHub. A failed check records `last_check_error` and keeps the last good list.
- The collector scheduler was chosen over a new worker: it already runs a periodic tick with DB
  access and network, and the check is one HTTP call a day. Its failure is isolated like the
  heartbeat's.
- Nothing is downloaded or installed automatically — the operator starts the download and confirms
  the apply, as before.

### Downloading in the API process
`POST /updates/source/downloads` starts a daemon thread in the backend that streams the asset to
`incoming/.<id>.part` (size limit `UPDATES_MAX_BUNDLE_BYTES`, enforced while streaming) and renames
it to `incoming/resurgam-<version>-<id8>.bundle`. Progress is a status file
`<updates_dir>/downloads/<id>.json` (rewritten at least every 2 s while bytes arrive), so it
survives a page reload, and a backend restart mid-download shows up as "stopped making progress"
after 5 minutes instead of hanging (a heartbeat keeps touching the status file while the finished
file is inspected and its signature checked, so a slow verification is not mistaken for a dead
thread). Listing downloads sweeps `.part` files no running download owns (left by a restart) and
prunes finished status files after 7 days. One download at a time, enforced under an exclusive
`flock` on `downloads/.start.lock` so two simultaneous requests cannot both start one. Changing
"include pre-releases" drops the cached release list, and the cache is filtered by that setting. A thread instead of the host updater
because the host updater is root and deliberately has no network or credentials; instead of a
native worker because the backend container already owns `incoming/` and the download is plain
HTTPS with no privileges.

## Consequences

- The appliance needs outbound HTTPS to the API host (and, for GitHub, the asset storage it
  redirects to — currently `objects.githubusercontent.com` and/or
  `release-assets.githubusercontent.com`).
- The release procedure gains one step: `publish_release.py` after `build-bundle.sh` (needs the pushed
  commit and a builder token with write access).
- Without a token, GitHub's API allows 60 requests per hour per IP — enough for daily checks of a
  public repository, but RESURGAM's own repository is private, so a token is required there.
- Not part of the configuration export (ADR 0024) yet: after an import the update source has to be
  entered again.
