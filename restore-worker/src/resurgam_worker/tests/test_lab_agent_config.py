import pytest

from resurgam_worker.lab_agent.config import MissingConfigError, load_config


def _set_required_env(monkeypatch):
    monkeypatch.setenv("RESURGAM_BACKEND_URL", "https://resurgam.example.com/api/v1/")
    monkeypatch.setenv("RESURGAM_LAB_AGENT_TOKEN", "agent-token-value")
    monkeypatch.setenv("RESURGAM_LAB_AGENT_PBS_TOKEN", "resurgam@pbs!dev:secretvalue")


def test_load_config_reads_required_vars_and_strips_trailing_slash(monkeypatch):
    _set_required_env(monkeypatch)

    config = load_config()

    assert config.backend_url == "https://resurgam.example.com/api/v1"
    assert config.agent_token == "agent-token-value"
    assert config.pbs_token == "resurgam@pbs!dev:secretvalue"
    assert config.poll_interval_seconds == 5.0
    assert config.overlay_dir == "/var/lib/resurgam-lab-agent/overlays"
    assert config.overlay_storage_id == "resurgam-lab-overlays"


def test_load_config_honors_optional_overrides(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("RESURGAM_LAB_AGENT_POLL_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("RESURGAM_LAB_AGENT_OVERLAY_DIR", "/tmp/overlays")
    monkeypatch.setenv("RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID", "my-lab-storage")
    monkeypatch.setenv("RESURGAM_LAB_AGENT_VM_MEMORY_MB", "4096")
    monkeypatch.setenv("RESURGAM_LAB_AGENT_VM_CORES", "4")

    config = load_config()

    assert config.poll_interval_seconds == 10.0
    assert config.overlay_dir == "/tmp/overlays"
    assert config.overlay_storage_id == "my-lab-storage"
    assert config.vm_memory_mb == 4096
    assert config.vm_cores == 4


def test_load_config_raises_on_missing_required_var(monkeypatch):
    monkeypatch.delenv("RESURGAM_BACKEND_URL", raising=False)
    monkeypatch.delenv("RESURGAM_LAB_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("RESURGAM_LAB_AGENT_PBS_TOKEN", raising=False)

    with pytest.raises(MissingConfigError):
        load_config()
