"""ADR 0031 stage 1: client-side encryption per backup, from what PBS reports.

PBS shapes are the ones seen live on PBS 4.2 (2026-11-22): an unencrypted VM
backup lists every file with crypt-mode "none" and no fingerprint; an encrypted
one has "encrypt" data archives, a "sign-only" manifest and the key fingerprint.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from resurgam_api.collector import sync as collector_sync_module
from resurgam_api.collector.sync import sync_datastore_inventory
from resurgam_api.connectivity.models import RemoteSnapshot, SnapshotListResult, TrustConfig
from resurgam_api.connectivity.pbs_api import list_snapshots, snapshot_crypt_mode
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot
from resurgam_api.models.pbs import TlsTrustMode

FP_A = "ad:a6:9b:4c:ec:20:02:30:68:c5:12:2c:7f:ee:73:eb:9b:62:40:70:88:a7:be:19:c7:49:d1:1e:11:8a:5f:7d"
FP_B = "11:22:33:44:55:66:77:88:99:00:aa:bb:cc:dd:ee:ff:11:22:33:44:55:66:77:88:99:00:aa:bb:cc:dd:ee:ff"
UNENCRYPTED_FILES = [
    {"filename": "qemu-server.conf.blob", "crypt-mode": "none"},
    {"filename": "drive-scsi0.img.fidx", "crypt-mode": "none"},
    {"filename": "index.json.blob", "crypt-mode": "none"},
    {"filename": "client.log.blob"},
]
ENCRYPTED_FILES = [
    {"filename": "data.pxar.didx", "crypt-mode": "encrypt"},
    {"filename": "catalog.pcat1.didx", "crypt-mode": "encrypt"},
    {"filename": "index.json.blob", "crypt-mode": "sign-only"},
]


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (UNENCRYPTED_FILES, "none"),
        (ENCRYPTED_FILES, "encrypt"),
        (
            [{"filename": "a.img.fidx", "crypt-mode": "encrypt"}, {"filename": "b.img.fidx", "crypt-mode": "none"}],
            "mixed",
        ),
        ([{"filename": "a.img.fidx", "crypt-mode": "sign-only"}], "sign-only"),
        ([{"filename": "index.json.blob", "crypt-mode": "sign-only"}], None),
        (None, None),
    ],
)
def test_snapshot_crypt_mode_summarizes_the_data_archives(files, expected):
    assert snapshot_crypt_mode(files) == expected


def test_list_snapshots_parses_crypt_mode_and_fingerprint(monkeypatch):
    payload = {
        "data": [
            {
                "backup-type": "host",
                "backup-id": "enc",
                "backup-time": 1795381599,
                "files": ENCRYPTED_FILES,
                "fingerprint": FP_A,
            },
            {"backup-type": "vm", "backup-id": "106", "backup-time": 1794361154, "files": UNENCRYPTED_FILES},
        ]
    }

    def fake_get(url, headers=None, params=None, timeout=None, verify=None):
        class R:
            status_code = 200

            def json(self):
                return {"data": [{"ns": ""}]} if url.endswith("/namespace") else payload

            def raise_for_status(self):
                return None

        return R()

    monkeypatch.setattr(httpx, "get", fake_get)
    result = list_snapshots("pbs.example", 8007, TrustConfig(trust_mode=TlsTrustMode.CA), "token", "store1")

    by_id = {s.backup_id: s for s in result.snapshots}
    assert (by_id["enc"].crypt_mode, by_id["enc"].key_fingerprint) == ("encrypt", FP_A)
    assert (by_id["106"].crypt_mode, by_id["106"].key_fingerprint) == ("none", None)


def _sync(db_session, monkeypatch, datastore, snapshots):
    monkeypatch.setattr(
        collector_sync_module,
        "list_snapshots",
        lambda *a, **kw: SnapshotListResult(reachable=True, snapshots=snapshots),
    )
    from tests.test_collector_sync import _FakeSettings

    sync_datastore_inventory(db_session, _FakeSettings(), datastore)


def test_collector_stores_encryption_and_refreshes_existing_snapshots(db_session, monkeypatch):
    from tests.test_collector_sync import _make_datastore, _make_pbs_instance

    datastore = _make_datastore(db_session, _make_pbs_instance(db_session))
    _sync(
        db_session, monkeypatch, datastore, [RemoteSnapshot(backup_type="vm", backup_id="1", backup_time=1_700_000_000)]
    )
    snapshot = db_session.query(BackupSnapshot).one()
    assert (snapshot.crypt_mode, snapshot.key_fingerprint) == (None, None)  # PBS reported nothing

    # The next sync carries the encryption facts: the existing row is filled in.
    _sync(
        db_session,
        monkeypatch,
        datastore,
        [
            RemoteSnapshot(
                backup_type="vm", backup_id="1", backup_time=1_700_000_000, crypt_mode="encrypt", key_fingerprint=FP_A
            )
        ],
    )
    db_session.refresh(snapshot)
    assert (snapshot.crypt_mode, snapshot.key_fingerprint) == ("encrypt", FP_A)


def _group_with_snapshots(db_session, datastore_id, backup_id, snaps):
    group = BackupGroup(datastore_id=datastore_id, backup_type="vm", backup_id=backup_id)
    db_session.add(group)
    db_session.flush()
    start = datetime(2026, 11, 1, tzinfo=UTC)
    for i, (mode, fp) in enumerate(snaps):
        db_session.add(
            BackupSnapshot(
                backup_group_id=group.id, snapshot_time=start + timedelta(days=i), crypt_mode=mode, key_fingerprint=fp
            )
        )
    group.last_backup_at = start + timedelta(days=len(snaps) - 1)
    db_session.commit()
    return group.id


def _datastore(client, platform_admin_headers, monkeypatch):
    from tests.test_alarms_api import _create_pbs_instance, _import_datastore

    return _import_datastore(
        client, platform_admin_headers, _create_pbs_instance(client, platform_admin_headers), monkeypatch
    )


def test_group_and_datastore_show_keys_and_a_key_change(client, platform_admin_headers, db_session, monkeypatch):
    datastore_id = _datastore(client, platform_admin_headers, monkeypatch)
    changed = _group_with_snapshots(
        db_session, datastore_id, "100", [("none", None), ("encrypt", FP_A), ("encrypt", FP_B)]
    )
    _group_with_snapshots(db_session, datastore_id, "101", [("encrypt", FP_A), ("encrypt", FP_A)])

    groups = {
        g["id"]: g
        for g in client.get(f"/api/v1/datastores/{datastore_id}/backup-groups", headers=platform_admin_headers).json()
    }
    g = groups[str(changed)]
    assert (g["latest_crypt_mode"], g["latest_key_fingerprint"]) == ("encrypt", FP_B)
    assert g["key_fingerprints"] == [FP_A, FP_B]
    assert g["key_changed"] is True
    assert [x for x in groups.values() if x["backup_id"] == "101"][0]["key_changed"] is False

    snapshots = client.get(f"/api/v1/backup-groups/{changed}/snapshots", headers=platform_admin_headers).json()
    assert {s["crypt_mode"] for s in snapshots} == {"none", "encrypt"}

    datastore = client.get(f"/api/v1/datastores/{datastore_id}", headers=platform_admin_headers).json()
    assert datastore["key_fingerprints_in_use"] == sorted([FP_A, FP_B])
    assert datastore["encrypted_snapshot_count"] == 4


def test_key_changed_alarm_only_for_the_changed_group(client, platform_admin_headers, db_session, monkeypatch):
    datastore_id = _datastore(client, platform_admin_headers, monkeypatch)
    changed = _group_with_snapshots(db_session, datastore_id, "100", [("encrypt", FP_A), ("encrypt", FP_B)])
    _group_with_snapshots(db_session, datastore_id, "101", [("encrypt", FP_A), ("encrypt", FP_A)])
    _group_with_snapshots(
        db_session, datastore_id, "102", [(None, None), ("encrypt", FP_A)]
    )  # older row not synced yet
    turned_off = _group_with_snapshots(db_session, datastore_id, "103", [("encrypt", FP_A), ("none", None)])

    alarms = [
        a
        for a in client.get("/api/v1/alarms", headers=platform_admin_headers).json()
        if a["type"] == "backup_key_changed"
    ]

    assert {a["entity_id"] for a in alarms} == {str(changed), str(turned_off)}
    assert all(a["severity"] == "warning" for a in alarms)
    message = next(a["message"] for a in alarms if a["entity_id"] == str(changed))
    assert "11:22:33:44:…" in message and "ad:a6:9b:4c:…" in message
