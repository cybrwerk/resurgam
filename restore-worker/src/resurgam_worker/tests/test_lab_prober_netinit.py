from resurgam_worker.lab_prober import netinit


def test_needs_alias_false_when_ip_in_own_subnet():
    assert netinit.needs_alias("10.99.0.0/24", "10.99.0.50") is False


def test_needs_alias_true_when_ip_outside_own_subnet():
    assert netinit.needs_alias("10.99.0.0/24", "192.168.1.50") is True


def test_alias_for_target_is_a_slash_32():
    assert netinit.alias_for_target("192.168.1.50") == "192.168.1.50/32"


def test_add_alias_invokes_ip_command(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        netinit.subprocess,
        "run",
        lambda command, **kw: captured.update(command=command) or netinit.subprocess.CompletedProcess(command, 0),
    )
    netinit.add_alias("net0", "192.168.1.50/32")
    assert captured["command"] == ["ip", "addr", "add", "192.168.1.50/32", "dev", "net0"]
