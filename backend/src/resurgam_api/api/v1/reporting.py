import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from resurgam_api.api.v1.customers import get_customer_in_scope_or_404
from resurgam_api.auth.dependency import get_current_user
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import visible_customer_ids
from resurgam_api.branding import get_branding_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.report_history import ReportHistory, ReportHistoryContent
from resurgam_api.reporting.framework_mapping import FRAMEWORK_SECTION_CONTROLS
from resurgam_api.reporting.pdf import render_recovery_evidence_pdf
from resurgam_api.reporting.recovery_evidence import compute_recovery_evidence_report
from resurgam_api.schemas.report_history import ReportHistoryOut
from resurgam_api.schemas.reporting import RecoveryEvidenceReportOut

router = APIRouter(prefix="/reporting", tags=["reporting"])

_DEFAULT_WINDOW_DAYS = 90


def _resolve_date_range(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    resolved_to = date_to or datetime.now(UTC).date()
    resolved_from = date_from or (resolved_to - timedelta(days=_DEFAULT_WINDOW_DAYS))
    return resolved_from, resolved_to


def _build_report(
    db: Session,
    customer_id: uuid.UUID,
    date_from: date | None,
    date_to: date | None,
    framework: bool,
    user: AuthenticatedUser,
) -> RecoveryEvidenceReportOut:
    customer = get_customer_in_scope_or_404(db, customer_id, user)
    resolved_from, resolved_to = _resolve_date_range(date_from, date_to)
    report = compute_recovery_evidence_report(db, customer, resolved_from, resolved_to, user.user_id)
    if framework:
        report.framework = True
        report.section_controls = FRAMEWORK_SECTION_CONTROLS
    return report


@router.get("/recovery-evidence", response_model=RecoveryEvidenceReportOut)
def get_recovery_evidence_report(
    customer_id: uuid.UUID,
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    framework: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RecoveryEvidenceReportOut:
    """Manifest Reporting -- compliance evidence for one customer at a
    time (an evidence document is inherently "for Customer X", not a
    cross-tenant view). No `platform_admin` gate: any role with
    visibility into this customer (`get_customer_in_scope_or_404`, same
    404-not-403 posture as billing's per-customer endpoint) can pull
    their own evidence report. `date_from`/`date_to` default to a
    90-day trailing window. `framework=True` annotates each section with
    its ISO27001/SOC2/ISO9001 control references -- same data, same
    aggregation, purely a render-time flag.
    """
    return _build_report(db, customer_id, date_from, date_to, framework, user)


@router.get("/recovery-evidence/pdf")
def get_recovery_evidence_report_pdf(
    customer_id: uuid.UUID,
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    framework: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Same report as `GET /recovery-evidence`, rendered as a PDF -- and,
    as a side effect, permanently archived: the exact bytes returned
    here are hashed (SHA256) and stored in `report_history`/
    `report_history_content` before the response goes out, so this
    generation can be re-downloaded byte-for-byte later via
    `GET /recovery-evidence/history/{id}/pdf`, even if the underlying
    data (and therefore a freshly-generated report) has since changed.
    """
    report = _build_report(db, customer_id, date_from, date_to, framework, user)
    # Generated *before* rendering, not derived from the PDF bytes, so it
    # can be printed on every page of the document itself -- unlike the
    # SHA-256 checksum below, which is a hash of the rendered bytes and
    # therefore can never appear inside the file it's a hash of. This ID
    # is also this row's own primary key, so looking it up (printed on
    # the page) resolves straight to the checksum recorded for it.
    report_id = uuid.uuid4()
    pdf_bytes = render_recovery_evidence_pdf(report, branding=get_branding_settings(db), report_id=report_id)

    history = ReportHistory(
        id=report_id,
        report_type="recovery_evidence",
        customer_id=customer_id,
        date_from=report.date_from,
        date_to=report.date_to,
        framework=framework,
        generated_by_user_id=user.user_id,
        sha256_checksum=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    db.add(history)
    db.flush()
    db.add(ReportHistoryContent(report_history_id=history.id, pdf_content=pdf_bytes))
    db.commit()

    filename = f"recovery-evidence-{report.customer_match_code}-{report.date_to}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/recovery-evidence/history", response_model=list[ReportHistoryOut])
def list_recovery_evidence_history(
    customer_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ReportHistoryOut]:
    """Every past PDF generation for this customer, newest first --
    metadata only (checksum included), no PDF bytes inline. Same
    visibility rule as `GET /recovery-evidence` itself.
    """
    get_customer_in_scope_or_404(db, customer_id, user)
    rows = (
        db.query(ReportHistory)
        .filter(ReportHistory.report_type == "recovery_evidence", ReportHistory.customer_id == customer_id)
        .order_by(ReportHistory.created_at.desc())
        .all()
    )
    return [ReportHistoryOut.model_validate(row, from_attributes=True) for row in rows]


@router.get("/recovery-evidence/history/{history_id}/pdf")
def download_recovery_evidence_history_pdf(
    history_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Re-downloads the *exact* PDF bytes generated at the time, not a
    fresh regeneration -- the whole point of keeping this archive.
    """
    history = db.get(ReportHistory, history_id)
    if history is None or history.report_type != "recovery_evidence":
        raise ResurgamApiError(code="not_found", message="Report history entry not found.", status_code=404)

    visible = visible_customer_ids(user)
    if visible is not None and (history.customer_id is None or history.customer_id not in visible):
        # 404, not 403: don't confirm existence of an out-of-scope object.
        raise ResurgamApiError(code="not_found", message="Report history entry not found.", status_code=404)

    content = db.get(ReportHistoryContent, history_id)
    if content is None:
        raise ResurgamApiError(code="not_found", message="Report history entry not found.", status_code=404)

    filename = f"recovery-evidence-{history.date_to}-{history.sha256_checksum[:8]}.pdf"
    return Response(
        content=content.pdf_content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
