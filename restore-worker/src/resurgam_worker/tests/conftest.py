import pytest

from resurgam_worker.lab_agent import lab_cleanup


@pytest.fixture(autouse=True)
def _lab_cleanup_registry(tmp_path, monkeypatch):
    """Keeps every test's lab leftover registry in its own temp dir, never
    the node's real /var/lib/resurgam-lab-agent/lab-cleanup.json."""
    path = str(tmp_path / "lab-cleanup.json")
    monkeypatch.setattr(lab_cleanup, "REGISTRY_PATH", path)
    for func in (lab_cleanup.record_mapping, lab_cleanup.record_snippet, lab_cleanup.sweep):
        defaults = list(func.__kwdefaults__ or {})
        if "path" in defaults:
            monkeypatch.setitem(func.__kwdefaults__, "path", path)
    return path
