from typing import ClassVar

from resurgam_api.connectivity.models import RemoteGuest, RemoteStorage, VmidAvailabilityResult
from resurgam_api.models.restore import RestorePlanNetworkMode, RestorePlanVmidIntent
from resurgam_api.restore.checks import (
    check_name_conflict,
    check_pbs_storage_visibility,
    check_storage_capacity,
    check_vmid,
    classify_network_risk,
)

_GUESTS = [
    RemoteGuest(vmid=100, node="pve1", guest_type="qemu", name="WinSRV-Eval", status="stopped"),
    RemoteGuest(vmid=101, node="pve1", guest_type="lxc", name="docker", status="running"),
]


class TestCheckVmid:
    def test_new_vmid_free_passes(self):
        nextid = VmidAvailabilityResult(reachable=True, vmid=200, free=True)
        entry = check_vmid(_GUESTS, 200, RestorePlanVmidIntent.NEW, nextid)
        assert entry["status"] == "pass"

    def test_new_vmid_taken_fails(self):
        nextid = VmidAvailabilityResult(reachable=True, vmid=100, free=False)
        entry = check_vmid(_GUESTS, 100, RestorePlanVmidIntent.NEW, nextid)
        assert entry["status"] == "fail"

    def test_new_vmid_in_guest_list_fails_even_if_nextid_disagrees(self):
        # belt-and-suspenders: the guest list itself is authoritative too,
        # not just PVE's own nextid check.
        nextid = VmidAvailabilityResult(reachable=True, vmid=100, free=True)
        entry = check_vmid(_GUESTS, 100, RestorePlanVmidIntent.NEW, nextid)
        assert entry["status"] == "fail"

    def test_existing_vmid_that_exists_passes(self):
        nextid = VmidAvailabilityResult(reachable=True, vmid=100, free=False)
        entry = check_vmid(_GUESTS, 100, RestorePlanVmidIntent.EXISTING, nextid)
        assert entry["status"] == "pass"

    def test_existing_vmid_that_does_not_exist_fails(self):
        nextid = VmidAvailabilityResult(reachable=True, vmid=999, free=True)
        entry = check_vmid(_GUESTS, 999, RestorePlanVmidIntent.EXISTING, nextid)
        assert entry["status"] == "fail"

    def test_unreachable_nextid_is_unknown_not_a_silent_pass(self):
        nextid = VmidAvailabilityResult(reachable=False, error="timeout")
        entry = check_vmid(_GUESTS, 200, RestorePlanVmidIntent.NEW, nextid)
        assert entry["status"] == "unknown"


class TestCheckStorageCapacity:
    _STORAGES: ClassVar[list[RemoteStorage]] = [
        RemoteStorage(storage="local-lvm", type="lvmthin", content=["images"], active=True, avail_bytes=1000),
        RemoteStorage(storage="offline-store", type="dir", content=["images"], active=False, avail_bytes=1000),
        RemoteStorage(storage="unknown-avail", type="dir", content=["images"], active=True, avail_bytes=None),
    ]

    def test_missing_storage_fails(self):
        entry = check_storage_capacity(self._STORAGES, "does-not-exist")
        assert entry["status"] == "fail"

    def test_inactive_storage_fails(self):
        entry = check_storage_capacity(self._STORAGES, "offline-store")
        assert entry["status"] == "fail"

    def test_unknown_available_space_is_unknown(self):
        entry = check_storage_capacity(self._STORAGES, "unknown-avail")
        assert entry["status"] == "unknown"

    def test_insufficient_space_fails(self):
        entry = check_storage_capacity(self._STORAGES, "local-lvm", min_free_bytes=2000)
        assert entry["status"] == "fail"

    def test_sufficient_space_passes(self):
        entry = check_storage_capacity(self._STORAGES, "local-lvm", min_free_bytes=500)
        assert entry["status"] == "pass"

    def test_no_minimum_requested_passes_if_active(self):
        entry = check_storage_capacity(self._STORAGES, "local-lvm")
        assert entry["status"] == "pass"


class TestCheckPbsStorageVisibility:
    _PBS_STORAGE = RemoteStorage(
        storage="pbs-main",
        type="pbs",
        content=["backup"],
        pbs_server="pbs1.example.internal",
        pbs_datastore="backup-store",
        pbs_namespace="tenant-a",
    )

    def test_not_linked_warns(self):
        entry = check_pbs_storage_visibility(
            [self._PBS_STORAGE], "backup-store", "tenant-a", "pbs1.example.internal", is_linked=False
        )
        assert entry["status"] == "warn"

    def test_no_matching_datastore_fails_with_a_clear_configure_message(self):
        entry = check_pbs_storage_visibility(
            [self._PBS_STORAGE], "some-other-datastore", "tenant-a", "pbs1.example.internal", is_linked=True
        )
        assert entry["status"] == "fail"
        assert entry["message"].startswith("PBS store not configured on this PVE node")
        assert "please configure" in entry["message"]

    def test_matching_datastore_wrong_namespace_fails_with_a_clear_configure_message(self):
        entry = check_pbs_storage_visibility(
            [self._PBS_STORAGE], "backup-store", "site-b/tenant-b", "pbs1.example.internal", is_linked=True
        )
        assert entry["status"] == "fail"
        assert entry["message"].startswith("PBS store not configured on this PVE node")
        assert "please configure" in entry["message"]

    def test_matching_datastore_and_namespace_wrong_server_warns(self):
        entry = check_pbs_storage_visibility(
            [self._PBS_STORAGE], "backup-store", "tenant-a", "some-other-pbs.example", is_linked=True
        )
        assert entry["status"] == "warn"

    def test_full_match_passes(self):
        entry = check_pbs_storage_visibility(
            [self._PBS_STORAGE], "backup-store", "tenant-a", "pbs1.example.internal", is_linked=True
        )
        assert entry["status"] == "pass"

    def test_root_namespace_normalizes_empty_string_and_none(self):
        root_storage = RemoteStorage(
            storage="pbs-root",
            type="pbs",
            content=["backup"],
            pbs_server="pbs1.example.internal",
            pbs_datastore="backup-store",
            pbs_namespace=None,
        )
        entry = check_pbs_storage_visibility(
            [root_storage], "backup-store", "", "pbs1.example.internal", is_linked=True
        )
        assert entry["status"] == "pass"


class TestCheckNameConflict:
    def test_conflict_warns(self):
        entry = check_name_conflict(_GUESTS, "docker")
        assert entry["status"] == "warn"

    def test_no_conflict_passes(self):
        entry = check_name_conflict(_GUESTS, "restore-test-1")
        assert entry["status"] == "pass"


class TestClassifyNetworkRisk:
    _RULES: ClassVar[dict] = {"production_bridges": ["vmbr0"], "safe_bridges": ["vmbr99"], "default_bridge": "vmbr99"}

    def test_none_mode_passes(self):
        entry = classify_network_risk(self._RULES, RestorePlanNetworkMode.NONE, None)
        assert entry["status"] == "pass"

    def test_isolated_mode_with_rules_passes(self):
        entry = classify_network_risk(self._RULES, RestorePlanNetworkMode.ISOLATED, None)
        assert entry["status"] == "pass"

    def test_isolated_mode_without_rules_is_unknown(self):
        entry = classify_network_risk(None, RestorePlanNetworkMode.ISOLATED, None)
        assert entry["status"] == "unknown"

    def test_original_mode_always_warns(self):
        entry = classify_network_risk(self._RULES, RestorePlanNetworkMode.ORIGINAL, None)
        assert entry["status"] == "warn"

    def test_original_mode_warns_even_with_no_rules_configured(self):
        entry = classify_network_risk(None, RestorePlanNetworkMode.ORIGINAL, None)
        assert entry["status"] == "warn"

    def test_custom_bridge_in_production_list_warns(self):
        entry = classify_network_risk(self._RULES, RestorePlanNetworkMode.CUSTOM, "vmbr0")
        assert entry["status"] == "warn"

    def test_custom_bridge_in_safe_list_passes(self):
        entry = classify_network_risk(self._RULES, RestorePlanNetworkMode.CUSTOM, "vmbr99")
        assert entry["status"] == "pass"

    def test_custom_bridge_unclassified_is_unknown(self):
        entry = classify_network_risk(self._RULES, RestorePlanNetworkMode.CUSTOM, "vmbr42")
        assert entry["status"] == "unknown"

    def test_custom_mode_without_rules_is_unknown(self):
        entry = classify_network_risk(None, RestorePlanNetworkMode.CUSTOM, "vmbr42")
        assert entry["status"] == "unknown"
