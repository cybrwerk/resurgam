from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    database: Literal["ok", "unreachable"]


class VersionResponse(BaseModel):
    version: str = Field(
        description="Running RESURGAM release version (MAJOR.MINOR.PATCH), or `0.0.0-dev` for an unversioned dev checkout.",
        examples=["0.2.0"],
    )
