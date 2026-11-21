from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from resurgam_api.db import get_db
from resurgam_api.schemas.system import HealthResponse, ReadinessResponse, VersionResponse
from resurgam_api.version import running_version

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@router.get("/readiness", response_model=ReadinessResponse)
def readiness(db: Session = Depends(get_db)) -> ReadinessResponse:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return ReadinessResponse(status="degraded", database="unreachable")
    return ReadinessResponse(status="ready", database="ok")


@router.get("/version", response_model=VersionResponse)
def version() -> VersionResponse:
    """The running RESURGAM release version (ADR 0024).

    Deliberately **no auth required**: it returns nothing but the version
    string, and both the host-side updater's post-update health check
    (which has no session) and the login page footer need it. Resolved
    from `RESURGAM_VERSION`, else the repository `VERSION` file, else
    `0.0.0-dev`.
    """
    return VersionResponse(version=running_version())
