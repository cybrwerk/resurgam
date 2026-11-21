from __future__ import annotations

import os
from dataclasses import dataclass


class MissingConfigError(Exception):
    def __init__(self, var_name: str) -> None:
        self.var_name = var_name
        super().__init__(f"required environment variable {var_name} is not set")


@dataclass(frozen=True)
class LabProberConfig:
    """`resurgam-lab-prober`'s own local config (Recovery Lab Phase B)
    -- env-var driven, matching `resurgam-lab-agent`'s own config
    precedent (`lab_agent/config.py`), written by the run's cloud-init
    snippet (`/etc/default/resurgam-lab-prober`) at clone time, not
    fetched from the backend at startup.
    """

    backend_url: str
    run_id: str
    run_token: str
    # From the same cloud-init snippet's *other* env file
    # (`/etc/default/resurgam-lab-router`, also loaded by this unit's
    # `EnvironmentFile=` -- see `restore-worker/debian`) -- this
    # appliance's own net0 subnet, needed to decide whether a probe
    # target's IP needs a secondary alias (`lab_prober/netinit.py`).
    own_subnet_cidr: str
    net0_interface: str = "net0"
    poll_interval_seconds: float = 5.0


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingConfigError(name)
    return value


def load_config() -> LabProberConfig:
    return LabProberConfig(
        backend_url=_require("RESURGAM_BACKEND_URL").rstrip("/"),
        run_id=_require("RESURGAM_LAB_RUN_ID"),
        run_token=_require("RESURGAM_LAB_RUN_TOKEN"),
        own_subnet_cidr=_require("SUBNET_CIDR"),
        # PVE's "net0" config slot name is not the guest's own interface
        # name (predictable naming, e.g. `enp0s18`) -- not yet live-
        # verified against a real booted appliance. The router bootstrap
        # script (debian/resurgam-lab-router's systemd unit) resolves
        # the real name and can override this via env if it differs.
        net0_interface=os.environ.get("RESURGAM_LAB_PROBER_NET0_INTERFACE", "net0"),
        poll_interval_seconds=float(os.environ.get("RESURGAM_LAB_PROBER_POLL_INTERVAL_SECONDS", "5")),
    )
