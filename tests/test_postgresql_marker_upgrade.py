from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_tools import postgresql_runtime
from module.application.errors import StorageConfigurationError
from module.application.storage_models import StorageHealth, StorageHealthState
from module.persistence.config import (
    DatabaseSettings,
    advance_backend_marker_schema_head,
    load_backend_marker_for_diagnostics,
    load_backend_marker_for_schema_upgrade,
)
from module.persistence.schema import EXPECTED_ALEMBIC_HEAD

_PREVIOUS_HEAD = "0003_fleet_state_core"


def _marker_payload(head: str) -> dict[str, object]:
    return {
        "backend": "postgresql",
        "version": 1,
        "alembic_head": head,
        "reconciliation_report_sha256": "a" * 64,
        "reviewed_head": "b" * 40,
        "merge_commit": "c" * 40,
        "host": "127.0.0.1",
        "port": 5432,
        "database": "azurpilot",
        "user": "azurpilot_app",
        "sslmode": "disable",
        "runtime_timezone": "Asia/Novosibirsk",
    }


def _write_marker(path: Path, head: str) -> dict[str, object]:
    payload = _marker_payload(head)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def test_runtime_rejects_stale_marker_but_upgrade_loader_accepts_it(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "storage_backend.json"
    _write_marker(marker, _PREVIOUS_HEAD)

    with pytest.raises(StorageConfigurationError, match="несовместимый schema head"):
        DatabaseSettings.from_backend_marker(marker)

    settings, marker_head = load_backend_marker_for_schema_upgrade(marker)
    diagnostic_settings, diagnostic_head, marker_version = load_backend_marker_for_diagnostics(marker)

    assert marker_head == _PREVIOUS_HEAD
    assert settings.user == "azurpilot_app"
    assert settings.database == "azurpilot"
    assert diagnostic_settings == settings
    assert diagnostic_head == marker_head
    assert marker_version == 1


def test_marker_schema_advance_changes_only_head_and_is_idempotent(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "storage_backend.json"
    original = _write_marker(marker, _PREVIOUS_HEAD)

    assert advance_backend_marker_schema_head(
        marker,
        previous_head=_PREVIOUS_HEAD,
    )

    updated = json.loads(marker.read_text(encoding="utf-8"))
    assert updated == {**original, "alembic_head": EXPECTED_ALEMBIC_HEAD}
    assert DatabaseSettings.from_backend_marker(marker).user == "azurpilot_app"
    assert not advance_backend_marker_schema_head(
        marker,
        previous_head=_PREVIOUS_HEAD,
    )


def test_marker_schema_advance_rejects_unexpected_current_head(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "storage_backend.json"
    _write_marker(marker, _PREVIOUS_HEAD)
    before = marker.read_bytes()

    with pytest.raises(StorageConfigurationError, match="изменился"):
        advance_backend_marker_schema_head(
            marker,
            previous_head="другая-ревизия",
        )

    assert marker.read_bytes() == before


def test_upgrade_requires_canonical_migrator_on_marker_endpoint() -> None:
    marker_settings = DatabaseSettings(
        host="127.0.0.1",
        port=5432,
        database="azurpilot",
        user="azurpilot_app",
        sslmode="disable",
        runtime_timezone="Asia/Novosibirsk",
    )
    migrator_settings = DatabaseSettings(
        host="127.0.0.1",
        port=5432,
        database="azurpilot",
        user="azurpilot_migrator",
        sslmode="disable",
        runtime_timezone="Asia/Novosibirsk",
    )

    postgresql_runtime._require_upgrade_endpoint_match(
        marker_settings,
        migrator_settings,
    )

    wrong_role = DatabaseSettings(
        host=migrator_settings.host,
        port=migrator_settings.port,
        database=migrator_settings.database,
        user="postgres",
        sslmode=migrator_settings.sslmode,
        runtime_timezone=migrator_settings.runtime_timezone,
    )
    with pytest.raises(StorageConfigurationError, match="azurpilot_migrator"):
        postgresql_runtime._require_upgrade_endpoint_match(
            marker_settings,
            wrong_role,
        )

    for field_name, value in (
        ("host", "localhost"),
        ("port", 6543),
        ("database", "another_database"),
        ("sslmode", "require"),
        ("runtime_timezone", "UTC"),
    ):
        values = {
            "host": migrator_settings.host,
            "port": migrator_settings.port,
            "database": migrator_settings.database,
            "user": migrator_settings.user,
            "sslmode": migrator_settings.sslmode,
            "runtime_timezone": migrator_settings.runtime_timezone,
        }
        values[field_name] = value
        mismatched = DatabaseSettings(**values)
        with pytest.raises(StorageConfigurationError, match="не совпадает"):
            postgresql_runtime._require_upgrade_endpoint_match(
                marker_settings,
                mismatched,
            )


class _LocalEnvironment:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def require_app_runtime_match(self, settings: DatabaseSettings) -> None:
        self.events.append(("marker-endpoint", settings.user, settings.database))


class _Engine:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def dispose(self) -> None:
        self.events.append("dispose")


def _install_upgrade_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_head: dict[str, str],
    events: list[object],
    upgrade_allowed: bool,
    marker_head: str = _PREVIOUS_HEAD,
) -> None:
    app_settings = DatabaseSettings(
        host="127.0.0.1",
        port=5432,
        database="azurpilot",
        user="azurpilot_app",
        sslmode="disable",
        runtime_timezone="Asia/Novosibirsk",
    )
    migrator_settings = DatabaseSettings(
        host="127.0.0.1",
        port=5432,
        database="azurpilot",
        user="azurpilot_migrator",
        sslmode="disable",
        runtime_timezone="Asia/Novosibirsk",
    )

    monkeypatch.setattr(
        postgresql_runtime,
        "load_local_postgres_environment",
        lambda *_args, **_kwargs: _LocalEnvironment(events),
    )
    monkeypatch.setattr(
        postgresql_runtime,
        "load_backend_marker_for_schema_upgrade",
        lambda _marker: (app_settings, marker_head),
    )
    monkeypatch.setattr(
        postgresql_runtime.DatabaseSettings,
        "from_environment",
        lambda **_kwargs: migrator_settings,
    )
    monkeypatch.setattr(
        postgresql_runtime,
        "LazyEngine",
        lambda _settings: _Engine(events),
    )

    class _HealthChecker:
        def __init__(self, _engine, *, expected_head=EXPECTED_ALEMBIC_HEAD):
            self.expected_head = expected_head

        def check(self) -> StorageHealth:
            events.append(("health", self.expected_head, database_head["value"]))
            state = (
                StorageHealthState.READY
                if database_head["value"] == self.expected_head
                else StorageHealthState.INCOMPATIBLE_SCHEMA
            )
            return StorageHealth(state, schema_head=database_head["value"])

        def require_ready(self) -> None:
            health = self.check()
            if health.state is not StorageHealthState.READY:
                raise StorageConfigurationError("Тестовая schema несовместима.")

    monkeypatch.setattr(postgresql_runtime, "StorageHealthChecker", _HealthChecker)

    def upgrade(_configuration, revision: str) -> None:
        if not upgrade_allowed:
            pytest.fail("Alembic upgrade не должен выполняться в этом сценарии")
        events.append(("upgrade", revision))
        database_head["value"] = EXPECTED_ALEMBIC_HEAD

    monkeypatch.setattr(postgresql_runtime.command, "upgrade", upgrade)

    def advance(_marker, *, previous_head: str) -> bool:
        assert database_head["value"] == EXPECTED_ALEMBIC_HEAD
        events.append(("advance-marker", previous_head))
        return True

    monkeypatch.setattr(
        postgresql_runtime,
        "advance_backend_marker_schema_head",
        advance,
    )

    for key in (
        "AZURPILOT_POSTGRES_HOST",
        "AZURPILOT_POSTGRES_PORT",
        "AZURPILOT_POSTGRES_DATABASE",
        "AZURPILOT_POSTGRES_USER",
        "AZURPILOT_POSTGRES_SSLMODE",
        "AZURPILOT_POSTGRES_RUNTIME_TIMEZONE",
        "AZURPILOT_POSTGRES_PASSWORD",
        "PGPASSWORD",
    ):
        monkeypatch.setenv(key, "test-original")


def test_runtime_upgrade_migrates_database_before_advancing_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    database_head = {"value": _PREVIOUS_HEAD}
    _install_upgrade_fakes(
        monkeypatch,
        database_head=database_head,
        events=events,
        upgrade_allowed=True,
    )

    postgresql_runtime._upgrade(tmp_path / "storage_backend.json")

    upgrade_index = events.index(("upgrade", "head"))
    marker_index = events.index(("advance-marker", _PREVIOUS_HEAD))
    assert upgrade_index < marker_index
    assert (
        "health",
        EXPECTED_ALEMBIC_HEAD,
        EXPECTED_ALEMBIC_HEAD,
    ) in events[upgrade_index:marker_index]


def test_runtime_upgrade_finishes_marker_after_previous_partial_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    database_head = {"value": EXPECTED_ALEMBIC_HEAD}
    _install_upgrade_fakes(
        monkeypatch,
        database_head=database_head,
        events=events,
        upgrade_allowed=False,
    )

    postgresql_runtime._upgrade(tmp_path / "storage_backend.json")

    assert ("advance-marker", _PREVIOUS_HEAD) in events
    assert not any(
        isinstance(event, tuple) and event[0] == "upgrade"
        for event in events
    )


def test_runtime_upgrade_rejects_unknown_database_head_without_advancing_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    database_head = {"value": "неизвестная_ревизия_бд"}
    _install_upgrade_fakes(
        monkeypatch,
        database_head=database_head,
        events=events,
        upgrade_allowed=False,
    )

    with pytest.raises(StorageConfigurationError, match="Тестовая schema несовместима"):
        postgresql_runtime._upgrade(tmp_path / "storage_backend.json")

    assert not any(
        isinstance(event, tuple) and event[0] == "advance-marker"
        for event in events
    )


def test_runtime_upgrade_rejects_unknown_marker_head_before_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    database_head = {"value": EXPECTED_ALEMBIC_HEAD}
    _install_upgrade_fakes(
        monkeypatch,
        database_head=database_head,
        events=events,
        upgrade_allowed=False,
        marker_head="неизвестная_ревизия_marker",
    )

    with pytest.raises(
        StorageConfigurationError,
        match="неизвестный или недопустимый schema head",
    ):
        postgresql_runtime._upgrade(tmp_path / "storage_backend.json")

    assert not any(
        isinstance(event, tuple) and event[0] == "advance-marker"
        for event in events
    )
