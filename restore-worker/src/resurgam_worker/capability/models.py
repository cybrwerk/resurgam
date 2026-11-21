from enum import Enum

from pydantic import BaseModel


class CapabilityStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    MISSING = "missing"


class CapabilityCheckResult(BaseModel):
    name: str
    status: CapabilityStatus
    message: str
    detail: str | None = None


class CapabilityReport(BaseModel):
    checks: list[CapabilityCheckResult]

    @property
    def overall_status(self) -> CapabilityStatus:
        statuses = {check.status for check in self.checks}
        if CapabilityStatus.MISSING in statuses:
            return CapabilityStatus.MISSING
        if CapabilityStatus.DEGRADED in statuses:
            return CapabilityStatus.DEGRADED
        return CapabilityStatus.OK

    def get(self, name: str) -> CapabilityCheckResult | None:
        return next((c for c in self.checks if c.name == name), None)
