from __future__ import annotations

from pathlib import Path

import pytest

from dev_tools import observability_compose_migration as migration


def _state(mode: str, *, volumes: list[str] | None = None) -> dict[str, object]:
    return {
        "canonical_project": migration.CANONICAL_PROJECT,
        "legacy_project": migration.LEGACY_PROJECT,
        "legacy_containers": [],
        "legacy_networks": [],
        "legacy_persistent_volumes": [
            {"name": name, "exists": True, "labels": {}}
            for name in volumes or []
        ],
        "mode": mode,
    }


def test_existing_install_missing_legacy_volume_fails_closed():
    state = _state("migration", volumes=list(migration.LEGACY_VOLUMES[:-1]))

    with pytest.raises(
        migration.ComposeMigrationError,
        match="LEGACY_PERSISTENT_VOLUME_MISSING",
    ):
        migration._require_legacy_volumes(state)


def test_existing_install_does_not_create_replacement_volumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    state = _state("migration", volumes=list(migration.LEGACY_VOLUMES))
    calls: list[tuple[str, ...]] = []
    inventories = iter((state, state))

    monkeypatch.setattr(migration, "inventory", lambda: next(inventories))
    monkeypatch.setattr(
        migration,
        "_require_legacy_volumes",
        lambda _state: calls.append(("require-volumes",)),
    )
    monkeypatch.setattr(
        migration,
        "_remove_legacy_project",
        lambda _state: calls.append(("remove-project",)),
    )
    monkeypatch.setattr(
        migration,
        "_run_compose",
        lambda _root, *arguments, **_kwargs: calls.append(
            tuple(str(argument) for argument in arguments)
        )
        or "",
    )
    monkeypatch.setattr(
        migration,
        "_verify_canonical_project",
        lambda _root: [{"Service": service} for service in ("postgres", "pgadmin")],
    )
    monkeypatch.setattr(
        migration,
        "_create_fresh_legacy_volumes",
        lambda: pytest.fail("existing install must not create empty volumes"),
    )

    result = migration.migrate(tmp_path)

    assert result["mode"] == "migration"
    assert ("require-volumes",) in calls
    assert ("remove-project",) in calls
    assert not any(
        call[:2] == ("volume", "create") for call in calls
    )


def test_fresh_install_creates_only_declared_external_volumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    initial = _state("fresh")
    final = _state("migration", volumes=list(migration.LEGACY_VOLUMES))
    created = []
    inventories = iter((initial, final))

    monkeypatch.setattr(migration, "inventory", lambda: next(inventories))
    monkeypatch.setattr(
        migration,
        "_create_fresh_legacy_volumes",
        lambda: created.extend(migration.LEGACY_VOLUMES),
    )
    monkeypatch.setattr(
        migration,
        "_run_compose",
        lambda _root, *arguments, **_kwargs: "",
    )
    monkeypatch.setattr(
        migration,
        "_verify_canonical_project",
        lambda _root: [{"Service": service} for service in ("postgres", "pgadmin")],
    )

    result = migration.migrate(tmp_path)

    assert result["mode"] == "fresh"
    assert created == list(migration.LEGACY_VOLUMES)


def test_legacy_cleanup_never_removes_persistent_volumes(monkeypatch):
    commands: list[list[str]] = []
    state = {
        "legacy_containers": [
            {"id": "container-id", "state": "running"},
        ],
        "legacy_networks": [{"id": "network-id"}],
    }

    monkeypatch.setattr(migration, "_project_containers", lambda _project: [])
    monkeypatch.setattr(migration, "_project_networks", lambda _project: [])
    monkeypatch.setattr(
        migration,
        "_run",
        lambda arguments, **_kwargs: commands.append(arguments) or "",
    )

    migration._remove_legacy_project(state)

    assert [command[:2] for command in commands] == [
        ["container", "stop"],
        ["container", "rm"],
        ["network", "rm"],
    ]


def test_redisinsight_is_optional_for_canonical_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    services = (
        "postgres",
        "pgadmin",
        "redis",
        *migration.VOLUME_SERVICES.values(),
    )
    monkeypatch.setattr(
        migration,
        "_compose_records",
        lambda _root: [{"Service": service, "State": "running", "Health": "healthy"} for service in services],
    )
    monkeypatch.setattr(migration, "_verify_canonical_mounts", lambda _root: None)

    records = migration._verify_canonical_project(tmp_path)

    assert {record["Service"] for record in records} == set(services)


def test_unhealthy_redisinsight_does_not_block_canonical_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    services = (
        "postgres",
        "pgadmin",
        "redis",
        *migration.VOLUME_SERVICES.values(),
        "redisinsight",
    )
    monkeypatch.setattr(
        migration,
        "_compose_records",
        lambda _root: [
            {
                "Service": service,
                "State": "restarting" if service == "redisinsight" else "running",
                "Health": "unhealthy" if service == "redisinsight" else "healthy",
            }
            for service in services
        ],
    )
    monkeypatch.setattr(migration, "_verify_canonical_mounts", lambda _root: None)

    records = migration._verify_canonical_project(tmp_path)

    assert {record["Service"] for record in records} == set(services)


def test_redisinsight_start_failure_is_non_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    state = _state("fresh")
    inventories = iter((state, state))
    monkeypatch.setattr(migration, "inventory", lambda: next(inventories))
    monkeypatch.setattr(migration, "_create_fresh_legacy_volumes", lambda: None)
    monkeypatch.setattr(
        migration,
        "_verify_canonical_project",
        lambda _root: [{"Service": "postgres"}],
    )

    def run_compose(_root: Path, *arguments: str, **_kwargs: object) -> str:
        if "redisinsight" in arguments:
            raise migration.ComposeMigrationError("DOCKER_COMMAND_FAILED")
        return ""

    monkeypatch.setattr(migration, "_run_compose", run_compose)

    result = migration.migrate(tmp_path)

    assert result["optional_services"] == {
        "redisinsight": {
            "status": "unavailable",
            "reason_code": "DOCKER_COMMAND_FAILED",
        }
    }
