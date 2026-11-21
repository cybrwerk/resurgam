from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class Customer(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "customers"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # Swiss/German business convention, not from the manifest: a debtor/
    # accounts-receivable number (Debitorennummer) and a three-letter
    # mnemonic lookup code (Matchcode), both required and unique.
    debitor_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    match_code: Mapped[str] = mapped_column(String(3), unique=True, nullable=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # How long to keep this customer's generated report PDFs (`ReportHistory`)
    # before the retention sweep purges them. Null means "use the platform-
    # wide floor" (`InstanceSettings.min_report_retention_days`) -- a set
    # value can extend retention but never shrink below that floor
    # (enforced in `api/v1/customers.py`, not here). Lives directly on
    # `Customer` rather than `CustomerFileIndexSettings` (an earlier draft
    # of this feature put it there) because report retention has nothing
    # to do with file indexing and must be settable for every customer,
    # including ones who have never opted into file indexing at all.
    report_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
