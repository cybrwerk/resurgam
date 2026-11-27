from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class S3EndpointOut(BaseModel):
    """A PBS S3 endpoint (PBS 4.x `GET /config/s3`, ADR 0033), as the Collector
    last read it, with RESURGAM's own on-site mark."""

    id: UUID = Field(description="RESURGAM's id of this endpoint row; use it in PUT /s3-endpoints/{id}/location.")
    pbs_instance_id: UUID = Field(description="The PBS instance the endpoint is configured on.")
    pbs_instance_name: str = Field(description="Display name of that PBS instance.")
    name: str = Field(description="The endpoint id on PBS (what a datastore's `backend` refers to as `client`).")
    endpoint: str = Field(description="Host name or IP address of the S3 service, as configured on PBS.")
    port: int | None = Field(description="Port of the S3 service; null when PBS uses its default (443).")
    region: str | None = Field(description="S3 region configured on PBS, if any.")
    path_style: bool = Field(
        description="Whether PBS addresses buckets path-style (`host/bucket`) instead of by subdomain."
    )
    datastores: list[str] = Field(description="Names of the S3-backed datastores on this PBS that use the endpoint.")
    on_site: bool = Field(
        description=(
            "False (the default): datastores on this endpoint count as offsite for 3-2-1 -- a copy landing there is "
            "an offsite copy, a backup stored there needs no further copy. True: a platform admin marked the object "
            "store as standing on site (e.g. a MinIO in the same rack); it then counts like a local datastore."
        )
    )
    on_site_reason: str | None = Field(
        description="Reason given with the last change of `on_site`; null if never changed."
    )
    on_site_changed_at: datetime | None = Field(description="When `on_site` was last changed; null if never.")
    on_site_changed_by: str | None = Field(description="Display name of who last changed `on_site`; null if never.")
    removed_from_pbs_at: datetime | None = Field(
        description="When the Collector last found the endpoint missing from PBS; null while PBS still lists it."
    )


class S3EndpointLocationRequest(BaseModel):
    on_site: bool = Field(
        description="True: the object store stands on site, its datastores do not count as offsite. False: offsite."
    )
    reason: str = Field(
        min_length=10,
        max_length=2000,
        description="Why the endpoint is (or is no longer) on site. Recorded with the change and in the audit log.",
    )

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 10:
            raise ValueError("reason must be at least 10 characters")
        return value
