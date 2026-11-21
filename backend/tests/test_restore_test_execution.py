from resurgam_api.models.restore_test import RestoreTestPlanMemberStatus, RestoreTestPlanStatus
from resurgam_api.restore_test import execution

# --- generate_vnet_name ----------------------------------------------------


def test_generate_vnet_name_fits_pve_constraints():
    for _ in range(20):
        name = execution.generate_vnet_name()
        assert 2 <= len(name) <= 8
        assert name[0].isalpha()
        assert name.isalnum()


# --- generate_appliance_run_token ------------------------------------------


def test_generate_appliance_run_token_is_random_and_nonempty():
    a = execution.generate_appliance_run_token()
    b = execution.generate_appliance_run_token()
    assert a != b
    assert len(a) > 20


def test_appliance_run_token_hash_roundtrip():
    token = execution.generate_appliance_run_token()
    token_hash = execution.hash_appliance_run_token(token)
    assert execution.verify_appliance_run_token(token, token_hash) is True
    assert execution.verify_appliance_run_token("wrong-token", token_hash) is False


# --- zone_requires_single_node ----------------------------------------------


def test_zone_requires_single_node_for_simple_and_none():
    assert execution.zone_requires_single_node("simple") is True
    assert execution.zone_requires_single_node(None) is True


def test_zone_requires_single_node_false_for_spanning_zone_types():
    for zone_type in ("vlan", "vxlan", "evpn", "qinq"):
        assert execution.zone_requires_single_node(zone_type) is False


# --- next_startable_member ---------------------------------------------------


def _member(order, status):
    return {"startup_order": order, "member_status": status}


def test_next_startable_member_picks_lowest_order_when_all_pending():
    members = [_member(2, "pending"), _member(1, "pending"), _member(3, "pending")]
    result = execution.next_startable_member(members)
    assert result["startup_order"] == 1


def test_next_startable_member_none_when_none_pending():
    members = [_member(1, "passed"), _member(2, "booting")]
    assert execution.next_startable_member(members) is None


def test_next_startable_member_blocked_by_earlier_member_still_restoring():
    members = [_member(1, "restore_running"), _member(2, "pending")]
    assert execution.next_startable_member(members) is None


def test_next_startable_member_unblocked_once_earlier_member_boots():
    members = [_member(1, "booting"), _member(2, "pending")]
    result = execution.next_startable_member(members)
    assert result["startup_order"] == 2


def test_next_startable_member_first_member_always_eligible():
    members = [_member(1, "pending"), _member(2, "pending")]
    result = execution.next_startable_member(members)
    assert result["startup_order"] == 1


# --- probe_outcome_to_member_status ------------------------------------------


def test_probe_outcome_all_pass_yields_passed():
    checks = [{"status": "pass"}, {"status": "pass"}]
    assert execution.probe_outcome_to_member_status(checks) == RestoreTestPlanMemberStatus.PASSED.value


def test_probe_outcome_any_fail_yields_failed():
    checks = [{"status": "pass"}, {"status": "fail"}]
    assert execution.probe_outcome_to_member_status(checks) == RestoreTestPlanMemberStatus.FAILED.value


def test_probe_outcome_empty_yields_failed():
    assert execution.probe_outcome_to_member_status([]) == RestoreTestPlanMemberStatus.FAILED.value


def test_probe_outcome_arp_pass_rescues_icmp_fail():
    # Real case from a live run (2026-09-27): ICMP blocked by the
    # guest's own firewall must not fail the member when ARP already
    # proves it's up and answering on the Lab VNet.
    checks = [
        {"check_type": "guest_agent", "status": "pass"},
        {"check_type": "icmp", "status": "fail"},
        {"check_type": "arp", "status": "pass"},
    ]
    assert execution.probe_outcome_to_member_status(checks) == RestoreTestPlanMemberStatus.PASSED.value


def test_probe_outcome_icmp_and_arp_both_fail_is_still_failed():
    checks = [
        {"check_type": "guest_agent", "status": "pass"},
        {"check_type": "icmp", "status": "fail"},
        {"check_type": "arp", "status": "fail"},
    ]
    assert execution.probe_outcome_to_member_status(checks) == RestoreTestPlanMemberStatus.FAILED.value


def test_probe_outcome_liveness_pass_does_not_rescue_an_unrelated_check_fail():
    checks = [
        {"check_type": "icmp", "status": "pass"},
        {"check_type": "tcp_port", "status": "fail"},
    ]
    assert execution.probe_outcome_to_member_status(checks) == RestoreTestPlanMemberStatus.FAILED.value


# --- aggregate_run_status -----------------------------------------------------


def test_aggregate_run_status_all_pending_is_planned():
    assert execution.aggregate_run_status(["pending", "pending"]) == RestoreTestPlanStatus.PLANNED.value


def test_aggregate_run_status_any_in_flight_is_running():
    assert execution.aggregate_run_status(["passed", "booting"]) == RestoreTestPlanStatus.RUNNING.value


def test_aggregate_run_status_all_passed_is_passed():
    assert execution.aggregate_run_status(["passed", "passed"]) == RestoreTestPlanStatus.PASSED.value


def test_aggregate_run_status_all_failed_is_failed():
    assert execution.aggregate_run_status(["failed", "failed"]) == RestoreTestPlanStatus.FAILED.value


def test_aggregate_run_status_mixed_terminal_is_partial():
    assert execution.aggregate_run_status(["passed", "failed"]) == RestoreTestPlanStatus.PARTIAL.value


# --- member_probe_plan --------------------------------------------------------


def test_member_probe_plan_needs_guest_agent_first():
    plan = execution.member_probe_plan(["guest_agent", "icmp"], [], None)
    assert plan.needs_guest_agent_check is True
    assert plan.guest_agent_failed is False
    assert plan.ready_for_appliance is False
    assert plan.is_complete is False


def test_member_probe_plan_guest_agent_failure_short_circuits_everything():
    results = [{"check_type": "guest_agent", "status": "fail"}]
    plan = execution.member_probe_plan(["guest_agent", "icmp", "tcp_port"], results, None)
    assert plan.guest_agent_failed is True
    assert plan.ready_for_appliance is False
    assert plan.is_complete is True  # finalizes FAILED without ever waiting on the appliance


def test_member_probe_plan_ready_for_appliance_once_guest_agent_passes_and_ip_known():
    results = [{"check_type": "guest_agent", "status": "pass"}]
    plan = execution.member_probe_plan(["guest_agent", "icmp", "http"], results, "10.99.0.50")
    assert plan.needs_guest_agent_check is False
    assert plan.ready_for_appliance is True
    assert plan.network_checks_pending == ["icmp", "http"]
    assert plan.is_complete is False


def test_member_probe_plan_network_only_member_needs_no_guest_agent_result():
    plan = execution.member_probe_plan(["tcp_port"], [], "10.99.0.50")
    assert plan.needs_guest_agent_check is False
    assert plan.guest_agent_failed is False
    assert plan.ready_for_appliance is True


def test_member_probe_plan_blocked_without_resolved_ip():
    plan = execution.member_probe_plan(["icmp"], [], None)
    assert plan.ready_for_appliance is False
    assert plan.is_complete is False


def test_member_probe_plan_complete_once_every_check_type_has_a_result():
    results = [
        {"check_type": "guest_agent", "status": "pass"},
        {"check_type": "icmp", "status": "pass"},
    ]
    plan = execution.member_probe_plan(["guest_agent", "icmp"], results, "10.99.0.50")
    assert plan.is_complete is True
    assert plan.ready_for_appliance is False  # nothing left pending


def test_member_probe_plan_no_check_types_is_vacuously_complete():
    plan = execution.member_probe_plan([], [], None)
    assert plan.is_complete is True
    assert plan.ready_for_appliance is False


# --- merge_probe_results -------------------------------------------------------


def test_merge_probe_results_overlays_by_check_type():
    existing = [{"check_type": "guest_agent", "status": "pass"}]
    incoming = [{"check_type": "icmp", "status": "pass"}]
    merged = execution.merge_probe_results(existing, incoming)
    assert {r["check_type"] for r in merged} == {"guest_agent", "icmp"}


def test_merge_probe_results_incoming_overwrites_same_check_type():
    existing = [{"check_type": "icmp", "status": "fail", "detail": "old"}]
    incoming = [{"check_type": "icmp", "status": "pass", "detail": "new"}]
    merged = execution.merge_probe_results(existing, incoming)
    assert len(merged) == 1
    assert merged[0]["status"] == "pass"
    assert merged[0]["detail"] == "new"


# --- render_appliance_cloud_init_snippet / subnet_prefix_length ----------------


def test_subnet_prefix_length_parses_cidr():
    assert execution.subnet_prefix_length("10.99.0.0/24") == 24
    assert execution.subnet_prefix_length("10.99.0.0/16") == 16


def test_render_appliance_cloud_init_snippet_embeds_all_values():
    content = execution.render_appliance_cloud_init_snippet(
        backend_url="https://resurgam.example.com/api/v1",
        plan_id="fc0324af-d75c-4833-a7f1-97bb2594b16f",
        run_token="secret-token",
        gateway_ip="10.99.0.1",
        subnet_cidr="10.99.0.0/24",
        dns_ip=None,
        dhcp_range_start="10.99.0.10",
        dhcp_range_end="10.99.0.200",
    )
    assert "RESURGAM_BACKEND_URL=https://resurgam.example.com/api/v1" in content
    assert "RESURGAM_LAB_RUN_TOKEN=secret-token" in content
    assert "GATEWAY_IP=10.99.0.1" in content
    assert "DNS_IP=10.99.0.1" in content  # defaults to gateway_ip when unset
    assert "DHCP_RANGE_START=10.99.0.10" in content
    assert "resurgam-lab-router.service" in content
    assert "resurgam-lab-prober.service" in content
