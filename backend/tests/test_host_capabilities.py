import json

from resurgam_api.host_capabilities import probe_kvm, read_kvm_status, write_state


def test_probe_reports_a_missing_device(tmp_path):
    status = probe_kvm(tmp_path / "kvm")
    assert status.available is False
    assert "does not exist" in status.detail


def test_probe_reports_an_inaccessible_device(tmp_path):
    device = tmp_path / "kvm"
    device.touch(mode=0o000)
    status = probe_kvm(device)
    assert status.available is False
    assert "isn't accessible" in status.detail


def test_probe_reports_an_accessible_device(tmp_path):
    device = tmp_path / "kvm"
    device.touch(mode=0o600)
    assert probe_kvm(device).available is True


def test_write_then_read_round_trips_through_the_state_file(tmp_path, monkeypatch):
    device = tmp_path / "kvm"
    device.touch(mode=0o600)
    monkeypatch.setattr("resurgam_api.host_capabilities.probe_kvm", lambda: probe_kvm(device))
    state_file = tmp_path / "host-capabilities.json"

    written = write_state(state_file)

    assert written.available is True
    assert json.loads(state_file.read_text())["checked_at"]
    assert read_kvm_status(state_file) == written
    assert not list(tmp_path.glob(".*.tmp"))


def test_read_reports_a_missing_state_file_as_not_yet_probed(tmp_path):
    status = read_kvm_status(tmp_path / "missing.json")
    assert status.available is False
    assert "resurgam-host-probe" in status.detail


def test_read_reports_a_malformed_state_file(tmp_path):
    state_file = tmp_path / "host-capabilities.json"
    state_file.write_text('{"kvm": {}}')
    status = read_kvm_status(state_file)
    assert status.available is False
    assert "malformed" in status.detail
