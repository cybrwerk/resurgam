from pydantic import BaseModel, Field


class SetupStatusOut(BaseModel):
    """Deliberately unauthenticated -- the frontend needs this before any
    login exists at all, to decide whether to show the first-boot setup
    wizard or the normal login page (manifest §5.1/§17 Milestone 5's
    "Setup-Wizard")."""

    needs_setup: bool = Field(
        description="True if no real (non-breakglass) platform admin account exists yet. "
        "Breakglass never counts as setup being complete -- it's documented as a stopgap (ADR 0004), "
        "not a substitute for a real admin account.",
    )
    kvm_available: bool = Field(
        description="Whether /dev/kvm is present and accessible to the native file-restore/file-index workers "
        "(on the appliance this comes from resurgam-host-probe.service, which checks as the workers' own "
        "user, since the API container itself never gets the device; in local dev, from this process) -- manifest "
        "§5.2's required nested-virtualization functional test ('Funktionstest beim Appliance-Setup'). "
        "False means VM-path file restores will be slow or unavailable (proxmox-file-restore's own "
        "internal QEMU micro-VM, ADR 0011).",
    )
    kvm_detail: str = Field(description="Human-readable explanation of the kvm_available result.")


class SetupCompleteRequest(BaseModel):
    """Same shape as UserCreateRequest (POST /users) -- setup is really
    just that endpoint's one-time, unauthenticated escape hatch for the
    very first account, since nothing can call the authenticated version
    before any admin exists."""

    email: str
    display_name: str
    password: str = Field(min_length=8)
