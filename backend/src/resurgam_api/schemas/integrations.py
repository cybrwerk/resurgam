from pydantic import BaseModel


class OidcSettingsOut(BaseModel):
    issuer: str
    client_id: str
    redirect_uri: str
    enabled: bool
    client_secret_configured: bool  # never the decrypted value itself
    source: str  # "database" | "environment"


class UpdateOidcSettingsRequest(BaseModel):
    issuer: str
    client_id: str
    client_secret: str | None = None  # omit to keep the currently-stored secret
    redirect_uri: str
    enabled: bool = True


class OidcDiscoveryTestRequest(BaseModel):
    issuer: str


class OidcDiscoveryTestResponse(BaseModel):
    reachable: bool
    error: str | None = None
