"""Every privilege RESURGAM asks for must be explained in the UI in every
language -- the "Required permissions" panel translates the backend's
English reason per privilege (frontend i18n `connection.privilegeReasons`)."""

import json
from pathlib import Path

import pytest

from resurgam_api.connectivity.required_privileges import PBS_REQUIRED_PRIVILEGES, PVE_REQUIRED_PRIVILEGES

LOCALES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "i18n" / "locales"


@pytest.mark.skipif(not LOCALES.is_dir(), reason="frontend sources not present")
@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize(("product", "required"), [("pbs", PBS_REQUIRED_PRIVILEGES), ("pve", PVE_REQUIRED_PRIVILEGES)])
def test_every_required_privilege_has_a_ui_reason(language, product, required):
    reasons = json.loads((LOCALES / f"{language}.json").read_text(encoding="utf-8"))["connection"]["privilegeReasons"]
    missing = [p.name for p in required if not reasons[product].get(p.name.replace(".", "_"))]
    assert not missing, f"{language}.json lacks connection.privilegeReasons.{product} for {missing}"
