import csv
import io
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from resurgam_api.api.v1.customers import get_customer_in_scope_or_404
from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import visible_customer_ids
from resurgam_api.db import get_db
from resurgam_api.inventory.billing import compute_customer_billing
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.tenancy import Customer
from resurgam_api.schemas.billing import CustomerBillingOut

router = APIRouter(prefix="/billing", tags=["billing"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)

_CSV_COLUMNS = [
    "customer_id",
    "customer_name",
    "customer_match_code",
    "vm_count",
    "ct_count",
    "host_count",
    "estimated_physical_bytes",
    "estimated_s3_bytes",
    "physical_bytes_estimate_complete",
    "pricing_plan_source",
    "price_per_vm_month",
    "price_per_ct_month",
    "price_per_host_month",
    "price_per_gb_month",
    "price_per_s3_gb_month",
    "currency",
    "estimated_monthly_cost",
]


@router.get("/customers", response_model=list[CustomerBillingOut])
def list_customer_billing(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[CustomerBillingOut]:
    """Not from the manifest — the billing computation `PricingPlan`
    (ADR 0009) was built for. Scope-filtered the same way as every other
    customer-scoped list (`/customers`, `/pricing-plans`, `/alarms`) --
    a `platform_admin` caller (`visible_customer_ids` returning `None`)
    gets every customer in one call, which already serves as the "global
    usage across all customers" API an MSP operator needs for their own
    billing/invoicing integration, raised directly by the user.
    """
    visible = visible_customer_ids(user)
    query = db.query(Customer).order_by(Customer.name)
    if visible is not None:
        query = query.filter(Customer.id.in_(visible))
    return [compute_customer_billing(db, customer) for customer in query.all()]


@router.get("/customers/export")
def export_customer_billing(
    db: Session = Depends(get_db), _user: AuthenticatedUser = Depends(_require_platform_admin)
) -> StreamingResponse:
    """Global CSV export of every customer's billing/usage snapshot in
    one file -- raised directly by the user, for an MSP operator to pull
    into their own invoicing workflow without scripting against the JSON
    endpoint. `platform_admin` only (unlike `GET /customers`, which
    scope-filters for other roles): a CSV export is inherently a
    cross-tenant bulk operation, same reasoning `GET /customers/export`
    already uses for the customer roster export.
    """
    rows = [compute_customer_billing(db, customer) for customer in db.query(Customer).order_by(Customer.name).all()]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_COLUMNS)
    for r in rows:
        writer.writerow(
            [
                r.customer_id,
                r.customer_name,
                r.customer_match_code,
                r.vm_count,
                r.ct_count,
                r.host_count,
                r.estimated_physical_bytes,
                r.estimated_s3_bytes,
                "true" if r.physical_bytes_estimate_complete else "false",
                r.pricing_plan_source or "",
                r.price_per_vm_month or "",
                r.price_per_ct_month or "",
                r.price_per_host_month or "",
                r.price_per_gb_month or "",
                "" if r.price_per_s3_gb_month is None else r.price_per_s3_gb_month,
                r.currency or "",
                r.estimated_monthly_cost or "",
            ]
        )

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=billing-usage.csv"},
    )


@router.get("/customers/{customer_id}", response_model=CustomerBillingOut)
def get_customer_billing(
    customer_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> CustomerBillingOut:
    customer = get_customer_in_scope_or_404(db, customer_id, user)
    return compute_customer_billing(db, customer)
