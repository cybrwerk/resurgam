import csv
import io
import uuid

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import visible_customer_ids
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.instance_settings import effective_min_report_retention_days
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.tenancy import Customer
from resurgam_api.schemas.customer import (
    CustomerCreateRequest,
    CustomerImportResult,
    CustomerImportRowError,
    CustomerOut,
    CustomerUpdateRequest,
)

_CSV_COLUMNS = ["name", "slug", "debitor_number", "match_code"]

router = APIRouter(prefix="/customers", tags=["customers"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _record_audit(db: Session, request: Request, *, actor_user_id, action: str, target_id, detail=None) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="customer",
            target_id=str(target_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def get_customer_in_scope_or_404(db: Session, customer_id: uuid.UUID, user: AuthenticatedUser) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise ResurgamApiError(code="not_found", message="Customer not found.", status_code=404)

    visible = visible_customer_ids(user)
    if visible is not None and customer.id not in visible:
        # 404, not 403: don't confirm existence of an out-of-scope object.
        raise ResurgamApiError(code="not_found", message="Customer not found.", status_code=404)
    return customer


@router.post("", response_model=CustomerOut)
def create_customer(
    body: CustomerCreateRequest, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> CustomerOut:
    if db.query(Customer).filter_by(slug=body.slug).one_or_none() is not None:
        raise ResurgamApiError(
            code="customer_slug_taken", message=f"Slug {body.slug!r} is already in use.", status_code=409
        )
    if db.query(Customer).filter_by(debitor_number=body.debitor_number).one_or_none() is not None:
        raise ResurgamApiError(
            code="customer_debitor_number_taken",
            message=f"Debitor number {body.debitor_number!r} is already in use.",
            status_code=409,
        )
    if db.query(Customer).filter_by(match_code=body.match_code).one_or_none() is not None:
        raise ResurgamApiError(
            code="customer_match_code_taken",
            message=f"Match code {body.match_code!r} is already in use.",
            status_code=409,
        )

    customer = Customer(name=body.name, slug=body.slug, debitor_number=body.debitor_number, match_code=body.match_code)
    db.add(customer)
    db.flush()

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="customer_created",
        target_id=customer.id,
        detail={
            "name": body.name,
            "slug": body.slug,
            "debitor_number": body.debitor_number,
            "match_code": body.match_code,
        },
    )
    db.commit()
    db.refresh(customer)
    return CustomerOut.model_validate(customer)


@router.get("", response_model=list[CustomerOut])
def list_customers(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[CustomerOut]:
    visible = visible_customer_ids(user)
    query = db.query(Customer).order_by(Customer.name)
    if visible is not None:
        query = query.filter(Customer.id.in_(visible))
    return [CustomerOut.model_validate(c) for c in query.all()]


@router.get("/export")
def export_customers(db: Session = Depends(get_db), _user=Depends(_require_platform_admin)) -> StreamingResponse:
    """Not from the manifest — requested directly, "for now" pending a
    real bulk-management UI. Every customer, not scope-filtered (only
    `platform_admin` can call this at all). `disabled` is included for
    round-trip fidelity but ignored on import — re-importing an export
    doesn't silently re-enable/disable anyone; use the dedicated
    endpoints for that.
    """
    customers = db.query(Customer).order_by(Customer.name).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([*_CSV_COLUMNS, "disabled"])
    for c in customers:
        writer.writerow([c.name, c.slug, c.debitor_number, c.match_code, "true" if c.disabled else "false"])

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=customers.csv"},
    )


@router.post("/import", response_model=CustomerImportResult)
def import_customers(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> CustomerImportResult:
    """Not from the manifest — requested directly, "for now" pending a
    real bulk-management UI. Upserts by `slug` (the stable identity key
    — see `CustomerUpdateRequest`'s own docstring): a row whose slug
    already exists updates that customer's `name`/`debitor_number`/
    `match_code`; a new slug creates one. One bad row (a format error or
    a `debitor_number`/`match_code` collision with a *different*
    customer) is reported and skipped, not a whole-file failure — a
    typo in row 40 of 200 shouldn't cost the other 199.
    """
    raw = file.file.read().decode("utf-8-sig")  # utf-8-sig: tolerate a BOM from Excel exports
    reader = csv.DictReader(io.StringIO(raw))
    missing_columns = [c for c in _CSV_COLUMNS if c not in (reader.fieldnames or [])]
    if missing_columns:
        raise ResurgamApiError(
            code="csv_missing_columns",
            message=f"CSV is missing required column(s): {', '.join(missing_columns)}",
            status_code=400,
        )

    created = updated = 0
    errors: list[CustomerImportRowError] = []

    for row_number, row in enumerate(reader, start=2):  # header is row 1
        try:
            validated = CustomerCreateRequest(
                name=(row.get("name") or "").strip(),
                slug=(row.get("slug") or "").strip(),
                debitor_number=(row.get("debitor_number") or "").strip(),
                match_code=(row.get("match_code") or "").strip(),
            )
        except ValidationError as exc:
            errors.append(CustomerImportRowError(row=row_number, message="; ".join(e["msg"] for e in exc.errors())))
            continue

        existing = db.query(Customer).filter_by(slug=validated.slug).one_or_none()
        existing_id = existing.id if existing is not None else None

        debitor_conflict = (
            db.query(Customer)
            .filter(Customer.debitor_number == validated.debitor_number, Customer.id != existing_id)
            .one_or_none()
        )
        if debitor_conflict is not None:
            errors.append(
                CustomerImportRowError(
                    row=row_number,
                    message=f"Debitor number {validated.debitor_number!r} is already in use by another customer.",
                )
            )
            continue
        match_code_conflict = (
            db.query(Customer)
            .filter(Customer.match_code == validated.match_code, Customer.id != existing_id)
            .one_or_none()
        )
        if match_code_conflict is not None:
            errors.append(
                CustomerImportRowError(
                    row=row_number,
                    message=f"Match code {validated.match_code!r} is already in use by another customer.",
                )
            )
            continue

        if existing is not None:
            existing.name = validated.name
            existing.debitor_number = validated.debitor_number
            existing.match_code = validated.match_code
            updated += 1
        else:
            db.add(
                Customer(
                    name=validated.name,
                    slug=validated.slug,
                    debitor_number=validated.debitor_number,
                    match_code=validated.match_code,
                )
            )
            created += 1

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="customers_imported",
        target_id="bulk-import",
        detail={"created": created, "updated": updated, "errors": len(errors)},
    )
    db.commit()
    return CustomerImportResult(created=created, updated=updated, errors=errors)


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(
    customer_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> CustomerOut:
    return CustomerOut.model_validate(get_customer_in_scope_or_404(db, customer_id, user))


@router.patch("/{customer_id}", response_model=CustomerOut)
def update_customer(
    customer_id: uuid.UUID,
    body: CustomerUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> CustomerOut:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise ResurgamApiError(code="not_found", message="Customer not found.", status_code=404)

    if (
        body.debitor_number is not None
        and body.debitor_number != customer.debitor_number
        and db.query(Customer).filter_by(debitor_number=body.debitor_number).one_or_none() is not None
    ):
        raise ResurgamApiError(
            code="customer_debitor_number_taken",
            message=f"Debitor number {body.debitor_number!r} is already in use.",
            status_code=409,
        )
    if (
        body.match_code is not None
        and body.match_code != customer.match_code
        and db.query(Customer).filter_by(match_code=body.match_code).one_or_none() is not None
    ):
        raise ResurgamApiError(
            code="customer_match_code_taken",
            message=f"Match code {body.match_code!r} is already in use.",
            status_code=409,
        )

    if body.name is not None:
        customer.name = body.name
    if body.debitor_number is not None:
        customer.debitor_number = body.debitor_number
    if body.match_code is not None:
        customer.match_code = body.match_code
    # Unlike the fields above, null is a real, meaningful value here
    # ("use the platform-wide floor") -- must be distinguished from the
    # field being omitted, same fix as PATCH /pve-clusters's
    # `lab_sdn_zone_name` (checks `model_fields_set`, not `is not None`).
    if "report_retention_days" in body.model_fields_set:
        if body.report_retention_days is not None:
            floor = effective_min_report_retention_days(db)
            if body.report_retention_days < floor:
                raise ResurgamApiError(
                    code="report_retention_below_floor",
                    message=f"report_retention_days must be at least the platform floor ({floor}).",
                    status_code=400,
                )
        customer.report_retention_days = body.report_retention_days

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="customer_updated",
        target_id=customer.id,
        detail={"name": customer.name, "debitor_number": customer.debitor_number, "match_code": customer.match_code},
    )
    db.commit()
    db.refresh(customer)
    return CustomerOut.model_validate(customer)


@router.post("/{customer_id}/disable", response_model=CustomerOut)
def disable_customer(
    customer_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> CustomerOut:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise ResurgamApiError(code="not_found", message="Customer not found.", status_code=404)
    customer.disabled = True
    _record_audit(db, request, actor_user_id=user.user_id, action="customer_disabled", target_id=customer.id)
    db.commit()
    db.refresh(customer)
    return CustomerOut.model_validate(customer)


@router.post("/{customer_id}/enable", response_model=CustomerOut)
def enable_customer(
    customer_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> CustomerOut:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise ResurgamApiError(code="not_found", message="Customer not found.", status_code=404)
    customer.disabled = False
    _record_audit(db, request, actor_user_id=user.user_id, action="customer_enabled", target_id=customer.id)
    db.commit()
    db.refresh(customer)
    return CustomerOut.model_validate(customer)
