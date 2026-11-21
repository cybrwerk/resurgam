from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The class-level defaults below (database_url's embedded password, and
# session_secret/secrets_local_encryption_key's empty strings) only exist
# so the app can boot at all with zero configuration -- they are not safe
# to run with. _validate_not_dev_defaults refuses to start on them unless
# dev_mode is explicitly set, so a production deployment that forgets to
# configure a real secret fails loudly at startup instead of silently
# running with a known-weak one (or, for session_secret, a hardcoded
# fallback -- see main.py's SessionMiddleware setup).
_DEFAULT_DATABASE_URL = "postgresql+psycopg://resurgam:resurgam-dev-only@localhost:55432/resurgam"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Set DEV_MODE=true for local development only -- it's the
    # one switch that lets the app boot with the class-level defaults
    # below (empty secrets, the hardcoded dev database password). Every
    # other deployment (including the appliance's docker-compose.yml,
    # which has no such env var set) keeps the secure default and fails
    # fast on a weak/missing secret rather than running with one.
    dev_mode: bool = False

    database_url: str = _DEFAULT_DATABASE_URL

    breakglass_enabled: bool = False
    breakglass_username: str = "admin"
    breakglass_password_hash: str = ""

    session_secret: str = ""
    session_ttl_seconds: int = 15 * 60
    # Trust `X-Forwarded-For` for the client IP (API token IP allowlists,
    # last-used IP). Only safe when the API is reachable solely through a
    # reverse proxy that sets the header itself -- the appliance's Caddy
    # does (it replaces any client-sent value), and its compose file turns
    # this on. Off by default: a directly reachable API would let any
    # client claim any IP.
    trust_proxy_headers: bool = False
    # Short-lived token issued between a successful password check and a
    # successful MFA check — never accepted by get_current_user, only by
    # the MFA-verify endpoints, so it can't be used to reach any business
    # endpoint on its own.
    mfa_challenge_ttl_seconds: int = 5 * 60
    # Token for a user whose admin made MFA mandatory but who has no second
    # factor yet: reaches only the MFA enrollment endpoints, never business
    # endpoints. Longer than the challenge -- scanning a QR code and finding
    # a security key takes a while.
    mfa_enrollment_ttl_seconds: int = 15 * 60

    # WebAuthn (FIDO2) relying-party identity. rp_id must be a registrable
    # domain suffix of the origin the browser actually calls this API
    # from (the frontend's own origin, not the API's) — "localhost" works
    # for local dev since both run on localhost in different ports.
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "RESURGAM"
    webauthn_origin: str = "http://localhost:5173"

    # Fernet key (urlsafe-base64, 32 bytes) for the interim local secrets
    # provider (docs/adr/0005). Generate with:
    #   uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secrets_local_encryption_key: str = ""

    connectivity_timeout_seconds: float = 5.0

    # Recovery Lab (ADR 0019, Phase B): the URL a probe appliance VM (on
    # a customer's own PVE cluster, reachable only via its uplink NIC --
    # see restore_test/execution.py) must use to call this backend back.
    # Distinct from wherever the backend actually binds/listens -- same
    # "operator sets the externally-reachable address once" precedent
    # `resurgam-lab-agent`'s own RESURGAM_BACKEND_URL env var already
    # established. Empty means Recovery Lab runs can't provision an
    # appliance yet (execute_restore_test_plan fails clearly).
    recovery_lab_public_backend_url: str = ""

    # Appliance only: the state file resurgam-host-probe.service writes
    # natively (see host_capabilities.py) -- the API container can't see
    # /dev/kvm itself, so it reports what the host probe found. Empty
    # (local dev) means the API checks its own process instead.
    host_capabilities_file: str = ""

    # Recovery Lab packages shipped with this backend (resurgam-lab-agent,
    # resurgam-lab-prober + manifest.json), built by backend/Dockerfile's
    # `lab-packages` stage. Outside the image (local dev) the directory is
    # usually missing; the download/guided-install/template-build endpoints
    # then answer 404 `lab_artifacts_missing` with how to build them.
    lab_artifacts_dir: str = "/app/artifacts"
    # Lab node pairing (ADR 0027): how long a one-time code stays valid, and
    # the appliance's TLS CA handed to the node so the agent can verify the
    # appliance (Caddy's `tls internal` root certificate; docker-compose's
    # ca-export service copies just that file -- never Caddy's CA keys --
    # into a volume mounted read-only at /appliance-ca). Absent = a public
    # certificate, trusted by the node's system store.
    lab_pairing_code_ttl_minutes: int = 30
    appliance_tls_ca_file: str = "/appliance-ca/root.crt"
    # Guided SSH jobs (agent install, template build) run in the API
    # process; their step log (never any credential) stays readable this
    # long after the job finished.
    lab_ssh_job_retention_seconds: int = 3600

    # File-restore job queue (ADR 0002, ADR 0013). ttl covers both "must be
    # claimed/completed by" and, for a completed download, "content stays
    # downloadable until" -- see worker/file_restore_worker.py's expiry sweep.
    file_restore_job_ttl_seconds: int = 600
    file_restore_worker_poll_interval_seconds: float = 1.0
    # Manifest §10.3 "Session-Lifecycle" -- neither has a RESURGAM-side
    # enforcement mechanism before this (see filerestore/sessions.py).
    file_restore_max_session_duration_seconds: int = 900
    # Downloaded content (single files and folder/multi-selection ZIPs) is written as a file to
    # this directory by the file-restore worker and streamed from there by the API -- never stored
    # in Postgres. On the appliance it is /var/lib/resurgam/file-restore, bind-mounted read-only
    # into the API container (docker-compose.yml) and writable for the native worker
    # (resurgam-file-restore-worker.service). Empty (local dev, API and worker on one host): a
    # `resurgam-file-restore` directory in the system temp directory.
    file_restore_output_dir: str = ""
    # Upper bound for one download's content (a ZIP: the uncompressed total of its files) -- a
    # disk-sized limit now that content lives on disk (it was 500 MB while it went through Postgres).
    file_restore_max_content_bytes: int = 50_000_000_000
    # Kept free on the output directory's file system: a job doesn't start below it, and an
    # extraction that would eat into it is aborted.
    file_restore_min_free_bytes: int = 2_000_000_000
    # How many entries one folder/multi-selection ZIP request may name.
    file_restore_archive_max_entries: int = 1000
    # Budget for one extraction call of a single file or ZIP download (proxmox-file-restore
    # extract / proxmox-backup-client restore) -- large selections legitimately take hours.
    file_restore_extract_timeout_seconds: int = 4 * 60 * 60
    # Lifetime of a download link (`POST /file-restore/jobs/{id}/download-ticket`): long enough
    # for the browser to start the request, short enough that a copied URL is useless soon after.
    file_restore_download_ticket_ttl_seconds: int = 60

    # EPIC 9 (manifest §11.2) restore-execution staleness safety net: with
    # no separate worker/scheduler for this feature (see ADR 0014), a
    # stuck PVE task has no automatic sweep the way file-restore jobs do
    # -- this bounds how long a plan can sit in restore_requested/
    # restore_running before a poll marks it failed on its own, rather
    # than relying solely on PVE eventually reporting a terminal status.
    # Generous default: real restores of large disks can genuinely take
    # hours.
    restore_execution_stale_after_seconds: int = 4 * 60 * 60

    # File Indexing (manifest §13, reopened into scope) -- a second,
    # separate privileged worker/job queue from file-restore's: a
    # multi-hour index job should not compete with file-restore's
    # interactive browse lane and transfer lane at all.
    file_index_worker_poll_interval_seconds: float = 1.0
    # How often the worker re-checks `customer_file_index_settings` for
    # due scheduled work -- coarser than the main poll loop since this
    # table is small and changes rarely.
    file_index_worker_schedule_check_interval_seconds: float = 60.0
    # Customer-specific retention (manifest §13): how often to sweep
    # `file_index_entries` older than each customer's configured
    # `retention_days`. Coarser than the schedule check -- retention is a
    # slow-moving, day-granularity concern, not worth checking every poll.
    file_index_worker_retention_check_interval_seconds: float = 3600.0
    # Must comfortably exceed one VM `list_path()` call's own 120s
    # subprocess timeout -- see restore-worker's file_restore.py.
    file_index_worker_heartbeat_timeout_seconds: int = 600
    # Throttle between VM `proxmox-file-restore list` subprocess calls --
    # the concrete "I/O limit" manifest §13 asks for on the expensive
    # (one-directory-per-call) VM path. Was 0.2s: confirmed live
    # (2026-11-12) that this pause was ~2 of a 100 GB VM's ~3 indexing
    # hours (34k directories), with a listing itself taking ~0.1s.
    file_index_worker_vm_call_delay_seconds: float = 0.05
    # Self-imposed per-run checkpoint on the VM path: after visiting this
    # many directories in one worker pass, persist the cursor and
    # re-queue rather than running unbounded.
    file_index_worker_vm_max_directories_per_job_run: int = 2000
    file_index_entries_insert_batch_size: int = 1000

    # Appliance updates (ADR 0024). updates_dir is the directory shared
    # with the host-side updater (bind-mounted rw into the backend
    # container): the backend writes incoming/<id>.bundle uploads and
    # requests/<id>.json, and only reads status/ and history.json back.
    # backups_dir holds the updater's pre-update pg_dump files (mounted
    # read-only). Outside the appliance neither exists and the
    # /updates endpoints answer 503 instead of failing.
    updates_dir: str = "/var/lib/resurgam/updates"
    backups_dir: str = "/var/lib/resurgam/backups"
    # Upper bound for one uploaded update bundle (images + source +
    # wheels); enforced while streaming, so an oversized upload is cut
    # off instead of filling the disk.
    updates_max_bundle_bytes: int = 4 * 1024**3
    # Release public key baked into the backend image (ADR 0024): used for
    # a fast signature pre-check right after an upload, so a bundle signed
    # by another key is rejected immediately. Absent (local dev) = no
    # pre-check; the host updater's verify is authoritative either way.
    release_public_key_file: str = "/etc/resurgam/release-signing.pub"

    # White-label logo upload limit (PUT /branding). PNG, JPEG and WebP
    # only (checked by magic bytes): the logo is served unauthenticated.
    branding_logo_max_bytes: int = 1024 * 1024

    # Real OIDC (manifest §6.3), first target: authentik.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://localhost:8000/api/v1/auth/oidc/callback"

    # Frontend (Milestone 2). frontend_origin is used for CORS; the OIDC
    # callback redirects the browser to frontend_success_url with the
    # session token in a URL fragment (never sent to any server).
    frontend_origin: str = "http://localhost:5173"
    frontend_success_url: str = "http://localhost:5173/auth/callback"

    @model_validator(mode="after")
    def _validate_not_dev_defaults(self) -> "Settings":
        if self.dev_mode:
            return self
        problems: list[str] = []
        if not self.session_secret:
            problems.append("SESSION_SECRET is not set")
        if not self.secrets_local_encryption_key:
            problems.append("SECRETS_LOCAL_ENCRYPTION_KEY is not set")
        if self.database_url == _DEFAULT_DATABASE_URL:
            problems.append("DATABASE_URL is still the hardcoded local-dev default")
        if self.breakglass_enabled and not self.breakglass_password_hash:
            problems.append("BREAKGLASS_ENABLED is true but BREAKGLASS_PASSWORD_HASH is not set")
        if problems:
            raise ValueError(
                "refusing to start with dev-only defaults outside dev mode: "
                + "; ".join(problems)
                + ". Set DEV_MODE=true for local development, or configure real values."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
