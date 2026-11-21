from unittest.mock import MagicMock

from resurgam_worker.lab_prober import daemon
from resurgam_worker.lab_prober.client import ProbeTarget
from resurgam_worker.lab_prober.config import LabProberConfig

CONFIG = LabProberConfig(
    backend_url="https://resurgam.example.com/api/v1",
    run_id="fc0324af-d75c-4833-a7f1-97bb2594b16f",
    run_token="run-token",
    own_subnet_cidr="10.99.0.0/24",
)

TARGET = ProbeTarget(
    member_id="11111111-1111-1111-1111-111111111111", ip="10.99.0.50", check_types=["icmp", "tcp_port"]
)


def test_process_target_runs_every_check_type_and_submits(monkeypatch):
    client = MagicMock()
    calls = []
    monkeypatch.setattr(
        daemon,
        "run_check",
        lambda check_type, ip: (
            calls.append((check_type, ip))
            or {
                "check_type": check_type,
                "status": "pass",
                "detail": None,
                "checked_at": "2026-09-22T00:00:00+00:00",
            }
        ),
    )

    daemon.process_target(TARGET, CONFIG, client)

    assert calls == [("icmp", "10.99.0.50"), ("tcp_port", "10.99.0.50")]
    client.submit_probe_results.assert_called_once()
    args = client.submit_probe_results.call_args[0]
    assert args[0] == TARGET.member_id
    assert [r.check_type for r in args[1]] == ["icmp", "tcp_port"]


def test_process_target_adds_alias_when_ip_outside_own_subnet(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(
        daemon,
        "run_check",
        lambda check_type, ip: {
            "check_type": check_type,
            "status": "pass",
            "detail": None,
            "checked_at": "2026-09-22T00:00:00+00:00",
        },
    )
    alias_calls = []
    monkeypatch.setattr(daemon, "add_alias", lambda interface, cidr: alias_calls.append((interface, cidr)))

    outside_target = ProbeTarget(
        member_id="22222222-2222-2222-2222-222222222222", ip="192.168.1.50", check_types=["icmp"]
    )
    daemon.process_target(outside_target, CONFIG, client)

    assert alias_calls == [("net0", "192.168.1.50/32")]


def test_process_target_skips_alias_when_ip_already_in_own_subnet(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(
        daemon,
        "run_check",
        lambda check_type, ip: {
            "check_type": check_type,
            "status": "pass",
            "detail": None,
            "checked_at": "2026-09-22T00:00:00+00:00",
        },
    )
    alias_calls = []
    monkeypatch.setattr(daemon, "add_alias", lambda interface, cidr: alias_calls.append((interface, cidr)))

    daemon.process_target(TARGET, CONFIG, client)

    assert alias_calls == []
