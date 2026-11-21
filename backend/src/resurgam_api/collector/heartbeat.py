"""Connection heartbeat: a periodic health check of every non-disabled PBS
instance and PVE cluster, run by `worker/collector_scheduler.py`.

Raised directly by the user: `last_health_check_at` / `last_health_status`
used to be written only by the manual "Test connection" buttons, so a
connection that died after onboarding kept showing its old "ok" forever,
and PVE clusters weren't touched by any background job at all. The
heartbeat writes the same two columns (plus `version` when reachable)
the manual test-connection endpoints write, so the UI's "last heartbeat"
is simply `last_health_check_at`.

Cadence: its own fixed interval (`HEARTBEAT_INTERVAL_MINUTES`), checked
on every scheduler tick, deliberately independent of -- and normally
shorter than -- the inventory sync interval (`collector_poll_interval_
minutes`, default 15): a heartbeat is just two small GETs per target
(`/version` + `/access/permissions`, via `connectivity.health`), and "is the connection alive" should go stale far sooner than
"is the inventory current". Not a System Settings field: there's no
known need to tune it, and the existing settings pattern can take it
later if one appears.

Isolation: every target is checked and committed on its own -- a target
whose check raises (secret missing, unexpected error) is rolled back,
logged and skipped, never stopping the other targets or the inventory
sync that runs in the same tick.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from resurgam_api.config import Settings
from resurgam_api.connectivity.health import HealthCheckResult, apply_health_result, check_pbs_health, check_pve_health
from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.token_format import token_secret_missing
from resurgam_api.models.pbs import ConnectionHealthStatus, PbsInstance, TlsTrustMode
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.secrets import SecretReference
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider
from resurgam_api.secrets.provider import SecretNotFoundError

logger = logging.getLogger("resurgam_api.collector.heartbeat")

HEARTBEAT_INTERVAL_MINUTES = 5


# Classification is the shared `connectivity.health` check -- the very
# same call (privilege check included) the manual "Test connection"
# endpoints make, so the heartbeat and the button can never disagree on
# what "ok" / "auth_failed" / "missing_privileges" / ... means.


def _token_for(db: Session, settings: Settings, secret_ref_id) -> str:
    """The stored combined token, or "" when no value is stored at all --
    the health check then reports `auth_failed: secret not set` instead of
    the heartbeat raising (and retrying, and logging a traceback) every
    tick. A bare token ID (configuration import) is passed through; the
    health check recognises it the same way."""
    secret_ref = db.get(SecretReference, secret_ref_id)
    if secret_ref is None:
        return ""
    try:
        return LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key).retrieve(
            secret_ref.reference_key
        )
    except SecretNotFoundError:
        return ""


def stored_token_is_placeholder(db: Session, settings: Settings, secret_ref_id) -> bool:
    """True when the stored token is a secret-less placeholder (a bare
    token ID or an empty value), as written by a configuration import
    (`config_backup`). The collector scheduler skips such PBS instances
    instead of sending requests that can only be rejected with HTTP 401;
    the heartbeat reports them as `auth_failed: secret not set`. A
    reference with no stored value at all is *not* a placeholder (that is
    a broken secret, handled -- and logged -- by the callers as before)."""
    secret_ref = db.get(SecretReference, secret_ref_id)
    if secret_ref is None:
        return False
    try:
        stored = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key).retrieve(
            secret_ref.reference_key
        )
    except SecretNotFoundError:
        return False
    return token_secret_missing(stored)


def _trust(target: PbsInstance | PveCluster) -> TrustConfig:
    return TrustConfig(trust_mode=TlsTrustMode(target.tls_trust_mode), fingerprint_sha256=target.tls_fingerprint_sha256)


def heartbeat_pbs_instance(db: Session, settings: Settings, instance: PbsInstance) -> HealthCheckResult:
    """Health-check one PBS instance (`connectivity.health.check_pbs_health`)
    and record the result on it via `apply_health_result`
    (`last_health_check_at`, `last_health_status`, `version`). Flushes
    only; the caller commits."""
    token = _token_for(db, settings, instance.api_token_secret_ref_id)
    result = check_pbs_health(
        instance.host, instance.port, _trust(instance), token, timeout=settings.connectivity_timeout_seconds
    )
    apply_health_result(instance, result)
    db.flush()
    return result


def heartbeat_pve_cluster(db: Session, settings: Settings, cluster: PveCluster) -> HealthCheckResult:
    """Health-check one PVE cluster (`check_pve_health`) and record the
    result on it -- same columns and semantics as `heartbeat_pbs_instance`."""
    token = _token_for(db, settings, cluster.api_token_secret_ref_id)
    result = check_pve_health(
        cluster.host, cluster.port, _trust(cluster), token, timeout=settings.connectivity_timeout_seconds
    )
    apply_health_result(cluster, result)
    db.flush()
    return result


def _due(model, cutoff: datetime):
    return select(model).where(
        model.disabled.is_(False),
        (model.last_health_check_at.is_(None)) | (model.last_health_check_at <= cutoff),
    )


def run_due_heartbeats(
    db: Session, settings: Settings, interval_minutes: int = HEARTBEAT_INTERVAL_MINUTES
) -> dict[str, int]:
    """Health-check every non-disabled PBS instance and PVE cluster whose
    last check is older than *interval_minutes* (or that was never
    checked). Commits after each target; a target that raises is rolled
    back and logged, and the remaining targets still run.

    Returns `{"checked": n, "failed": m}` for logging/tests (`failed`
    counts targets whose check *raised*, e.g. a missing secret -- not ones
    found unreachable/auth_failed/..., which are successful heartbeats with
    a bad result).
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=interval_minutes)
    checked = failed = 0
    targets: list[tuple[str, object]] = [
        ("PBS instance", instance) for instance in db.execute(_due(PbsInstance, cutoff)).scalars().all()
    ]
    targets += [("PVE cluster", cluster) for cluster in db.execute(_due(PveCluster, cutoff)).scalars().all()]
    # Capture ids up front: after a rollback the ORM objects are expired
    # and touching them could raise again.
    labelled = [(kind, target, target.id) for kind, target in targets]

    for kind, target, target_id in labelled:
        try:
            if isinstance(target, PbsInstance):
                outcome = heartbeat_pbs_instance(db, settings, target)
            else:
                outcome = heartbeat_pve_cluster(db, settings, target)
            db.commit()
            checked += 1
            if outcome.status != ConnectionHealthStatus.OK:
                logger.warning("heartbeat: %s %s is %s: %s", kind, target_id, outcome.status.value, outcome.message)
        except Exception:  # one target's failure must not abort the others or the inventory sync
            db.rollback()
            failed += 1
            logger.exception("heartbeat: health check failed for %s %s", kind, target_id)

    return {"checked": checked, "failed": failed}
