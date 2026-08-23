from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from Crypto.Cipher import AES

from module.application.migration_models import RecordDisposition
from module.persistence.legacy import LegacySourceReader, create_consistent_snapshot
from module.persistence.legacy.reader import LegacySourceError

ROOT = Path(__file__).resolve().parents[1]
CL1_FIXTURE = ROOT / "tests" / "fixtures" / "postgresql_migration" / "cl1_shapes.json"


def _create_cl1(path: Path, payload: dict, *, encrypted: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE cl1_data (instance TEXT, month TEXT, data_json TEXT, "
            "encrypted_blob BLOB, PRIMARY KEY (instance, month))"
        )
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if encrypted:
            from Crypto.Hash import SHA256
            from Crypto.Protocol.KDF import PBKDF2

            key = PBKDF2(
                b"fixture-device",
                b"AlasCl1SecureStorage",
                dkLen=32,
                count=1000,
                hmac_hash_module=SHA256,
            )
            cipher = AES.new(key, AES.MODE_GCM, nonce=b"0" * 16)
            ciphertext, tag = cipher.encrypt_and_digest(text.encode("utf-8"))
            values = ("fixture", "2026-08", None, b"0" * 16 + tag + ciphertext)
        else:
            values = ("fixture", "2026-08", text, None)
        connection.execute("INSERT INTO cl1_data VALUES (?, ?, ?, ?)", values)


def _create_azurstats(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE resource_snapshots (id INTEGER PRIMARY KEY, instance TEXT "
            "NOT NULL, ts TEXT NOT NULL, oil INTEGER, coin INTEGER, gem INTEGER, "
            "pt INTEGER, cube INTEGER, core INTEGER, medal INTEGER, merit INTEGER, "
            "guild_coin INTEGER, action_point INTEGER, yellow_coin INTEGER, "
            "purple_coin INTEGER)"
        )
        connection.execute(
            "INSERT INTO resource_snapshots VALUES "
            "(1, 'fixture', '2026-08-01T01:00:00', 1, NULL, 3, 4, 5, 6, "
            "7, 8, 9, 10, 11, 12)"
        )
        connection.execute(
            "CREATE TABLE opsi_items (id INTEGER PRIMARY KEY, imgid TEXT NOT NULL, "
            "server TEXT, zone TEXT, zone_type TEXT, zone_id INTEGER, "
            "hazard_level INTEGER, item TEXT, amount INTEGER, tag TEXT, "
            "device_id TEXT, genre TEXT, combat_count INTEGER, created_at INTEGER)"
        )
        connection.execute(
            "INSERT INTO opsi_items VALUES (1, 'image-a', 'en', 'z', 'safe', 1, "
            "3, 'OperationCoin', 6, NULL, 'fixture', "
            "'opsi_meowfficer_farming', 3, 1785542400)"
        )


def _fixture_root(tmp_path: Path, *, encrypted: bool = False) -> Path:
    payload = json.loads(CL1_FIXTURE.read_text(encoding="utf-8"))
    _create_cl1(tmp_path / "config" / "cl1_data.db", payload, encrypted=encrypted)
    _create_azurstats(tmp_path / "config" / "azurstats_local.db")
    (tmp_path / "config" / "fixture.json").write_text('{"Alas": {}}', encoding="utf-8")
    return tmp_path


def _reader(
    root: Path,
    *,
    decryption_ids=(),
    legacy_timezone: str = "Asia/Novosibirsk",
) -> LegacySourceReader:
    return LegacySourceReader(
        root,
        legacy_timezone=legacy_timezone,
        profile_names=("fixture",),
        decryption_ids=decryption_ids,
    )


def test_readers_cover_all_schema_v1_families_without_source_mutation(tmp_path):
    root = _fixture_root(tmp_path)
    before = {
        path: sha256(path.read_bytes()).hexdigest()
        for path in (root / "config").glob("*.db")
    }

    plan = _reader(root).capture()

    datasets = dict(plan.dataset_counts())
    assert {
        "monthly_aggregate",
        "ap_purchase",
        "ap_snapshot",
        "currency_snapshot",
        "commission",
        "meow_timing",
        "meow_hazard",
        "siren_stat",
        "siren_event",
        "ap_notification",
        "resource_snapshot",
        "opsi_item",
    } <= datasets.keys()
    assert not any(
        record.disposition is RecordDisposition.QUARANTINE for record in plan.records
    )
    assert (
        sum(identity.evidence.value == "exact_profile" for identity in plan.identities)
        == 1
    )
    assert before == {
        path: sha256(path.read_bytes()).hexdigest()
        for path in (root / "config").glob("*.db")
    }
    summary = plan.safe_summary()
    assert dict(summary.resource_null_counts)["coin"] == 1
    assert summary.commission_item_count == 2


def test_safe_summary_excludes_quarantined_commission_parent_and_items(tmp_path):
    plan = _reader(_fixture_root(tmp_path)).capture()
    commission = next(
        record for record in plan.records if record.dataset == "commission"
    )
    quarantined = replace(
        commission,
        disposition=RecordDisposition.QUARANTINE,
        reason_code="SYNTHETIC_QUARANTINE",
    )
    changed = replace(
        plan,
        records=tuple(
            quarantined if record is commission else record for record in plan.records
        ),
    )

    summary = changed.safe_summary()

    assert summary.commission_parent_count == 0
    assert summary.commission_item_count == 0
    assert summary.commission_item_amount_sum == 0


def test_encrypted_cl1_uses_explicit_offline_key_without_plaintext_artifact(tmp_path):
    root = _fixture_root(tmp_path, encrypted=True)
    plan = _reader(root, decryption_ids=("fixture-device",)).capture()

    assert dict(plan.dataset_counts())["commission"] == 1
    assert not tuple(root.rglob("*.bak"))


def test_encrypted_json_boundary_violation_is_not_treated_as_wrong_key(tmp_path):
    payload: dict[str, object] = {"value": 1}
    for _ in range(20):
        payload = {"nested": payload}
    _create_cl1(tmp_path / "config" / "cl1_data.db", payload, encrypted=True)

    with pytest.raises(LegacySourceError, match="JSON_BOUNDS_EXCEEDED"):
        _reader(tmp_path, decryption_ids=("fixture-device",)).capture()


def test_legacy_cl1_json_bak_uses_proven_parent_identity_and_exact_old_shape(
    tmp_path,
):
    current = json.loads(CL1_FIXTURE.read_text(encoding="utf-8"))
    legacy = {
        "2026-08": current["battle_count"],
        "2026-08-akashi": current["akashi_encounters"],
        "2026-08-akashi-ap": current["akashi_ap"],
        "2026-08-akashi-ap-entries": current["akashi_ap_entries"],
    }
    path = tmp_path / "log" / "cl1" / "fixture" / "cl1_monthly.json.bak"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy), encoding="utf-8")

    plan = _reader(tmp_path).capture()

    assert dict(plan.dataset_counts()) == {
        "ap_purchase": 1,
        "monthly_aggregate": 3,
    }
    assert plan.identities[0].evidence.value == "exact_profile"


def test_malformed_legacy_cl1_scalar_is_quarantined(tmp_path):
    path = tmp_path / "log" / "cl1" / "fixture" / "cl1_monthly.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"2026-08": -1}), encoding="utf-8")

    plan = _reader(tmp_path).capture()

    assert plan.records[0].disposition is RecordDisposition.QUARANTINE
    assert plan.records[0].reason_code == "CL1_RECORD_INVALID"


def test_unknown_shape_is_quarantined(tmp_path):
    payload = json.loads(CL1_FIXTURE.read_text(encoding="utf-8"))
    payload["unsupported_future_key"] = {"value": 1}
    _create_cl1(tmp_path / "config" / "cl1_data.db", payload)

    plan = _reader(tmp_path).capture()

    assert len(plan.records) == 1
    assert plan.records[0].disposition is RecordDisposition.QUARANTINE
    assert plan.records[0].reason_code == "CL1_SHAPE_UNKNOWN"


def test_ambiguous_naive_timestamp_is_quarantined(tmp_path):
    payload = json.loads(CL1_FIXTURE.read_text(encoding="utf-8"))
    payload["akashi_ap_entries"][0]["ts"] = "2026-11-01T01:30:00"
    _create_cl1(tmp_path / "config" / "cl1_data.db", payload)

    plan = _reader(tmp_path, legacy_timezone="America/New_York").capture()

    quarantines = [
        record
        for record in plan.records
        if record.disposition is RecordDisposition.QUARANTINE
    ]
    assert any(record.reason_code == "CL1_RECORD_INVALID" for record in quarantines)


def test_invalid_timezone_fails_before_source_access(tmp_path):
    with pytest.raises(LegacySourceError, match="TIMEZONE_POLICY_INVALID"):
        _reader(tmp_path, legacy_timezone="Not/A-Timezone")


def test_missing_sqlite_is_not_created(tmp_path):
    plan = _reader(tmp_path).capture()

    assert plan.records == ()
    assert not (tmp_path / "config" / "cl1_data.db").exists()


def test_schema_validation_fails_closed(tmp_path):
    path = tmp_path / "config" / "cl1_data.db"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE cl1_data (instance TEXT)")

    with pytest.raises(LegacySourceError, match="CL1_SCHEMA_UNSUPPORTED"):
        _reader(tmp_path).capture()


def test_snapshot_uses_sqlite_backup_and_stable_copy_protocol(tmp_path):
    source = _fixture_root(tmp_path / "source # percent%")
    destination = tmp_path / "destination # percent%"
    destination.mkdir()

    create_consistent_snapshot(source, destination)

    assert (
        _reader(destination).capture().dataset_counts()
        == _reader(source).capture().dataset_counts()
    )


def test_empty_opsi_table_stays_empty_and_zero_csv_is_not_fallback(tmp_path):
    root = _fixture_root(tmp_path)
    with closing(
        sqlite3.connect(root / "config" / "azurstats_local.db")
    ) as connection, connection:
        connection.execute("DELETE FROM opsi_items")
    csv_path = root / "log" / "azurstat_meowofficer_farming.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text(
        "a,b,c,d,e,f,g\n" + "0,0,0,0,0,0,0\n" * 6,
        encoding="utf-8",
    )

    plan = _reader(root).capture()

    assert "opsi_item" not in dict(plan.dataset_counts())
    assert plan.derived_csv_parity is False


def test_non_ascii_long_source_path_is_supported(tmp_path):
    root = _fixture_root(tmp_path / ("данные-" + "д" * 80))

    assert _reader(root).capture().records


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink недоступен")
def test_source_symlink_escape_is_rejected(tmp_path):
    outside = _fixture_root(tmp_path / "outside")
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    try:
        os.symlink(outside / "config" / "cl1_data.db", root / "config" / "cl1_data.db")
    except OSError:
        pytest.skip("создание symlink недоступно в среде")

    with pytest.raises(LegacySourceError, match="SOURCE_PATH_ESCAPE|SOURCE_SYMLINK"):
        _reader(root).capture()

    destination = tmp_path / "snapshot"
    destination.mkdir()
    with pytest.raises(
        LegacySourceError, match="SNAPSHOT_PATH_ESCAPE|SNAPSHOT_SYMLINK"
    ):
        create_consistent_snapshot(root, destination)
