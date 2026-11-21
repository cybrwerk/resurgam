"""Disk Mounts state machine, credentials and the expiry/orphan sweep (ADR 0025 §4/§5).

Shared by the user API (`api/v1/disk_mounts.py`), the agent backchannel
(`api/v1/lab_agent_disk_mounts.py`) and the scheduler loop
(`worker/recovery_lab_scheduler.py`). Nothing here commits; callers do.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from resurgam_api.errors import ResurgamApiError
from resurgam_api.lab_setup.uplink import PURPOSE_DISK_MOUNT, allocate_uplink_ip, release_uplink_ip
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.disk_mount import TERMINAL_STATUSES, DiskMount, DiskMountStatus
from resurgam_api.models.pve import PveCluster
from resurgam_api.schemas.disk_mount import MAX_LIFETIME_HOURS
from resurgam_api.secrets.key_derivation import derive_purpose_key

logger = logging.getLogger("resurgam_api.disk_mounts")

# Purpose for `derive_purpose_key`: a dedicated subkey, so this ciphertext can
# never be decrypted as any other kind of secret (and vice versa).
SHARE_PASSWORD_KEY_PURPOSE = "disk_mount_share_password"
APPROVAL_TIMEOUT = timedelta(hours=24)
QUEUE_TIMEOUT = timedelta(minutes=30)
PROVISIONING_TIMEOUT = timedelta(minutes=45)
TEARDOWN_BACKOFF_MAX = timedelta(minutes=15)
# After this many failed teardown reports the mount is given up (ends `failed`,
# address released) instead of retrying forever; whatever is left on the node is
# named in the error for a manual cleanup.
TEARDOWN_MAX_ATTEMPTS = 10
TEARDOWN_ERROR_MARKER = "\n\nTeardown: "
SNIPPET_PREFIX = "resurgam-disk-mount-"


# --- identity ---------------------------------------------------------------


def helper_name(mount_id: uuid.UUID) -> str:
    return f"rsgm-mount-{mount_id.hex[:8]}"


def snippet_filename(mount_id: uuid.UUID) -> str:
    return f"{SNIPPET_PREFIX}{mount_id.hex[:12]}.yaml"


def generate_credentials() -> tuple[str, str]:
    """(user, password): `rsgm-<8 hex>` (a valid Linux/Samba user name) and a
    24-character URL-safe random password (`secrets.token_urlsafe(18)`)."""
    return f"rsgm-{secrets.token_hex(4)}", secrets.token_urlsafe(18)


def _fernet(master_key: str) -> Fernet:
    if not master_key:
        raise RuntimeError("SECRETS_LOCAL_ENCRYPTION_KEY is not configured")
    return Fernet(derive_purpose_key(master_key, SHARE_PASSWORD_KEY_PURPOSE))


def encrypt_password(master_key: str, password: str) -> bytes:
    return _fernet(master_key).encrypt(password.encode("utf-8"))


def decrypt_password(master_key: str, ciphertext: bytes | None) -> str | None:
    if ciphertext is None:
        return None
    try:
        return _fernet(master_key).decrypt(ciphertext).decode("utf-8")
    except InvalidToken:
        logger.error("disk mount share password could not be decrypted (wrong key?)")
        return None


# --- audit -------------------------------------------------------------------


def record_audit(
    db: Session,
    mount: DiskMount,
    action: str,
    *,
    actor_user_id: uuid.UUID | None,
    correlation_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Audits one lifecycle event. `detail` must never contain the password --
    callers pass only ids, names and settings."""
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="disk_mount",
            target_id=str(mount.id),
            customer_id=mount.customer_id,
            correlation_id=correlation_id,
            detail=detail,
        )
    )


# --- transitions -------------------------------------------------------------


def has_resources(mount: DiskMount) -> bool:
    return bool(mount.helper_vmid or mount.mapped_devices or mount.snippet_volume)


def queue(db: Session, mount: DiskMount, cluster: PveCluster, *, now: datetime | None = None) -> None:
    """`pending_approval`/new -> `queued`. A `cluster_range` mount gets its
    address now (before the agent creates anything), so an exhausted range
    fails up front with the range's own clear 409."""
    now = now or datetime.now(UTC)
    if mount.ip_mode == "cluster_range":
        address = allocate_uplink_ip(db, cluster, purpose=PURPOSE_DISK_MOUNT, disk_mount_id=mount.id)
        if address is None:
            raise ResurgamApiError(
                code="disk_mount_cluster_range_unavailable",
                message="This cluster's lab uplink has no static range (it uses DHCP). Choose ip_mode dhcp or static.",
                status_code=400,
            )
        mount.ip_address_cidr = f"{address}/{cluster.lab_appliance_uplink_ip_prefix_length}"
        mount.gateway = cluster.lab_appliance_uplink_gateway
    mount.status = DiskMountStatus.QUEUED.value
    mount.queued_at = now


def finish(
    db: Session, mount: DiskMount, status: str, *, now: datetime | None = None, error: str | None = None
) -> None:
    """Moves to a terminal state: forgets the password ciphertext and frees a
    range address. Only valid once nothing is left on the node."""
    assert status in TERMINAL_STATUSES
    mount.status = status
    mount.teardown_reason = None
    mount.share_password_ciphertext = None
    mount.ended_at = now or datetime.now(UTC)
    mount.next_teardown_attempt_at = None
    if error is not None:
        mount.error = error
    release_uplink_ip(db, disk_mount_id=mount.id)


def request_teardown(
    db: Session, mount: DiskMount, reason: str, *, now: datetime | None = None, error: str | None = None
) -> None:
    """-> `stopping` (agent removes the helper, unmaps, deletes the snippet), or
    straight to the terminal state when nothing was created yet."""
    now = now or datetime.now(UTC)
    if error is not None:
        mount.error = error
    mount.teardown_attempts = 0
    if mount.status in (DiskMountStatus.PENDING_APPROVAL.value, DiskMountStatus.QUEUED.value) or (
        mount.status != DiskMountStatus.PROVISIONING.value and not has_resources(mount)
    ):
        finish(db, mount, reason, now=now)
        return
    mount.status = DiskMountStatus.STOPPING.value
    mount.teardown_reason = reason
    mount.share_password_ciphertext = None
    mount.next_teardown_attempt_at = now


def with_teardown_error(original: str | None, teardown_error: str) -> str:
    """The mount's error with the latest teardown error appended. The original
    (provisioning) error is kept; a previous teardown error is replaced, so
    retries do not pile up."""
    base = (original or "").split(TEARDOWN_ERROR_MARKER, 1)[0]
    if not base:
        return teardown_error
    return f"{base}{TEARDOWN_ERROR_MARKER}{teardown_error}"


def leftovers_text(mount: DiskMount) -> str:
    """Human-readable list of what may still exist on the node."""
    parts = []
    if mount.helper_vmid:
        parts.append(f"helper VM {mount.helper_vmid} ({helper_name(mount.id)})")
    for entry in mount.mapped_devices or []:
        if isinstance(entry, dict) and entry.get("device"):
            parts.append(f"mapped device {entry['device']}")
    if mount.snippet_volume:
        parts.append(f"snippet {mount.snippet_volume}")
    return ", ".join(parts) or "nothing recorded"


def give_up_teardown(db: Session, mount: DiskMount, reason: str, *, now: datetime | None = None) -> None:
    """Ends a `stopping` mount as `failed` without a confirmed teardown (retry
    limit reached, or an admin forced it): frees the uplink address and keeps
    the leftover resource fields so the error and the record name them."""
    error = with_teardown_error(
        mount.error,
        f"{reason} Check node {mount.target_node} and remove by hand if still present: {leftovers_text(mount)}.",
    )
    finish(db, mount, DiskMountStatus.FAILED.value, now=now, error=error)


def teardown_failed(db: Session, mount: DiskMount, error: str, *, now: datetime | None = None) -> bool:
    """Keeps `stopping`, retried after 1, 2, 4 ... 15 minutes; after
    TEARDOWN_MAX_ATTEMPTS ends the mount (`give_up_teardown`). Returns True if
    it gave up."""
    now = now or datetime.now(UTC)
    mount.teardown_attempts = (mount.teardown_attempts or 0) + 1
    mount.error = with_teardown_error(mount.error, error)
    if mount.teardown_attempts >= TEARDOWN_MAX_ATTEMPTS:
        give_up_teardown(db, mount, f"Gave up after {mount.teardown_attempts} failed teardown attempts.", now=now)
        return True
    # Cap the exponent first: 2**(n-1) minutes overflows timedelta at n ~ 42.
    exponent = min(mount.teardown_attempts - 1, 4)
    mount.next_teardown_attempt_at = now + min(timedelta(minutes=2**exponent), TEARDOWN_BACKOFF_MAX)
    return False


def extend(mount: DiskMount, hours: int, *, now: datetime | None = None) -> datetime:
    """Adds `hours`, never more than MAX_LIFETIME_HOURS ahead of now."""
    now = now or datetime.now(UTC)
    base = max(mount.expires_at or now, now)
    mount.expires_at = min(base + timedelta(hours=hours), now + timedelta(hours=MAX_LIFETIME_HOURS))
    return mount.expires_at


# --- sweep -------------------------------------------------------------------


def sweep(db: Session, *, now: datetime | None = None) -> list[tuple[uuid.UUID, str]]:
    """Enforces lifetimes and catches orphans (ADR 0025 §4). Returns
    `(mount_id, action)` for every change, each audited (actor = system)."""
    now = now or datetime.now(UTC)
    changes: list[tuple[uuid.UUID, str]] = []

    def act(mount: DiskMount, action: str, detail: dict | None = None) -> None:
        record_audit(db, mount, action, actor_user_id=None, detail={"source": "disk_mount_sweep", **(detail or {})})
        changes.append((mount.id, action))

    expired = (
        db.query(DiskMount)
        .filter(DiskMount.status == DiskMountStatus.READY.value, DiskMount.expires_at <= now)
        .with_for_update(skip_locked=True)
    )
    for mount in expired.all():
        request_teardown(db, mount, DiskMountStatus.EXPIRED.value, now=now)
        act(mount, "disk_mount_expired", {"expires_at": mount.expires_at.isoformat() if mount.expires_at else None})

    stale_approval = (
        db.query(DiskMount)
        .filter(
            DiskMount.status == DiskMountStatus.PENDING_APPROVAL.value, DiskMount.created_at <= now - APPROVAL_TIMEOUT
        )
        .with_for_update(skip_locked=True)
    )
    for mount in stale_approval.all():
        finish(db, mount, DiskMountStatus.EXPIRED.value, now=now, error="Not approved within 24 hours.")
        act(mount, "disk_mount_approval_expired")

    unclaimed = (
        db.query(DiskMount)
        .filter(DiskMount.status == DiskMountStatus.QUEUED.value, DiskMount.queued_at <= now - QUEUE_TIMEOUT)
        .with_for_update(skip_locked=True)
    )
    for mount in unclaimed.all():
        finish(
            db,
            mount,
            DiskMountStatus.FAILED.value,
            now=now,
            error="resurgam-lab-agent did not pick the mount up within 30 minutes. Is the agent running on the "
            "cluster (Lab Compute setup shows when it last checked in)?",
        )
        act(mount, "disk_mount_failed", {"reason": "not_claimed"})

    stuck = (
        db.query(DiskMount)
        .filter(DiskMount.status == DiskMountStatus.PROVISIONING.value)
        .with_for_update(skip_locked=True)
    )
    for mount in stuck.all():
        last = mount.last_progress_at or mount.claimed_at or mount.queued_at or mount.created_at
        if last > now - PROVISIONING_TIMEOUT:
            continue
        request_teardown(
            db,
            mount,
            DiskMountStatus.FAILED.value,
            now=now,
            error="The helper did not become ready within 45 minutes; whatever was created is being removed.",
        )
        act(mount, "disk_mount_failed", {"reason": "provisioning_timeout"})
    return changes
