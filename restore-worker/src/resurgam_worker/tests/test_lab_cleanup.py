"""Leftovers of Recovery Lab runs (live finding 2026-11-21): a member's backup
mapping and the appliance snippet stayed on the node after the run's cleanup."""

from __future__ import annotations

import json
import os

from resurgam_worker.lab_agent import lab_cleanup
from resurgam_worker.pbs_client.errors import PbsClientError


def _registry(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _conf_dir(tmp_path, *vmids, content=""):
    conf = tmp_path / "qemu-server"
    conf.mkdir(exist_ok=True)
    for vmid in vmids:
        (conf / f"{vmid}.conf").write_text(content)
    return str(conf)


def test_mapping_is_kept_while_the_member_vm_exists(tmp_path, _lab_cleanup_registry):
    unmapped = []
    lab_cleanup.record_mapping("/dev/loop3", 107)

    lab_cleanup.sweep("secret", conf_dir=_conf_dir(tmp_path, 107), unmap_fn=lambda d, **kw: unmapped.append(d))

    assert unmapped == []
    assert len(_registry(_lab_cleanup_registry)["mappings"]) == 1


def test_mapping_is_unmapped_once_the_member_vm_is_gone(tmp_path, _lab_cleanup_registry):
    calls = []
    lab_cleanup.record_mapping("/dev/loop3", 107)

    lab_cleanup.sweep("secret", conf_dir=_conf_dir(tmp_path), unmap_fn=lambda d, **kw: calls.append((d, kw)))

    assert calls == [("/dev/loop3", {"token": "secret"})]
    assert _registry(_lab_cleanup_registry)["mappings"] == []


def test_a_failing_unmap_is_retried_then_dropped(tmp_path, _lab_cleanup_registry):
    attempts = []

    def failing(device, **kw):
        attempts.append(device)
        raise PbsClientError("not mapped")

    lab_cleanup.record_mapping("/dev/loop3", 107)
    conf = _conf_dir(tmp_path)
    for _ in range(lab_cleanup.MAX_UNMAP_ATTEMPTS + 1):
        lab_cleanup.sweep("secret", conf_dir=conf, unmap_fn=failing)

    assert len(attempts) == lab_cleanup.MAX_UNMAP_ATTEMPTS
    assert _registry(_lab_cleanup_registry)["mappings"] == []


def test_snippet_is_removed_only_after_the_grace_period_and_when_unreferenced(tmp_path, _lab_cleanup_registry):
    snippet = tmp_path / "resurgam-lab-abc.yaml"
    snippet.write_text("#cloud-config\n")
    clock = [1000.0]
    lab_cleanup.record_snippet(str(snippet), now=lambda: clock[0])
    conf = _conf_dir(tmp_path, 110, content="cicustom: user=local:snippets/resurgam-lab-abc.yaml\n")

    # Within the grace period, and later while the appliance still uses it: kept.
    lab_cleanup.sweep("s", conf_dir=conf, now=lambda: clock[0] + 10)
    clock[0] += lab_cleanup.SNIPPET_GRACE_SECONDS + 1
    lab_cleanup.sweep("s", conf_dir=conf, now=lambda: clock[0])
    assert snippet.exists()

    # The appliance VM is gone: removed.
    os.remove(os.path.join(conf, "110.conf"))
    lab_cleanup.sweep("s", conf_dir=conf, now=lambda: clock[0])
    assert not snippet.exists()
    assert _registry(_lab_cleanup_registry)["snippets"] == []


def test_nothing_happens_off_a_pve_node(tmp_path, _lab_cleanup_registry):
    calls = []
    lab_cleanup.record_mapping("/dev/loop3", 107)

    lab_cleanup.sweep("s", conf_dir=str(tmp_path / "missing"), unmap_fn=lambda d, **kw: calls.append(d))

    assert calls == []
