from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from module.dev_runtime import (
    DevEnvironment,
    DevSession,
    DevSessionManager,
    DevSessionState,
    DevTarget,
    DevTargetError,
    DevTargetRegistry,
    EvidenceStore,
    ProcessBackend,
    ProcessIdentity,
)
from module.dev_runtime import target as target_module

_TARGET_NAME = "synthetic-target"


def _write_profile(root: Path, name: str = _TARGET_NAME) -> Path:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    path = config / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "Alas": {"Emulator": {}},
                "General": {},
                "SyntheticTask": {
                    "Scheduler": {
                        "Enable": False,
                        "Command": "SyntheticTask",
                        "NextRun": "2026-08-31 00:00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _error_code(error: pytest.ExceptionInfo[DevTargetError]) -> str:
    return error.value.code


def test_configured_target_round_trips_against_one_structural_profile(tmp_path: Path) -> None:
    _write_profile(tmp_path)

    configured = DevTargetRegistry.configure(
        tmp_path,
        profile_name=_TARGET_NAME,
        explicit_consent=True,
    )
    loaded = DevTargetRegistry.load(tmp_path)

    assert configured == DevTarget(_TARGET_NAME)
    assert loaded == configured
    marker = tmp_path / "config" / "state" / "dev-runtime-target.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "profile_name": _TARGET_NAME,
        "mod_name": "alas",
    }


def test_missing_marker_fails_closed_when_default_profile_is_missing(tmp_path: Path) -> None:
    _write_profile(tmp_path)

    with pytest.raises(DevTargetError) as missing_marker:
        DevTargetRegistry.load(tmp_path)
    assert _error_code(missing_marker) == "DEV_TARGET_DEFAULT_PROFILE_MISSING"

    with pytest.raises(DevTargetError) as missing_profile:
        DevTargetRegistry.configure(tmp_path, profile_name="other-target")
    assert _error_code(missing_profile) == "DEV_TARGET_PROFILE_MISSING"


def test_missing_marker_defaults_to_ap_without_writing_marker(tmp_path: Path) -> None:
    _write_profile(tmp_path, "ap")

    loaded = DevTargetRegistry.load(tmp_path)

    assert loaded == DevTarget("ap")
    assert not (tmp_path / "config" / "state" / "dev-runtime-target.json").exists()


def test_session_accepts_legacy_profile_without_target_identity() -> None:
    session = DevSession(
        session_id="missing-target-identity",
        state=DevSessionState.STOPPED,
        repository_root=str(Path.cwd()),
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
        profile_name=_TARGET_NAME,
    )

    assert session.profile_name == _TARGET_NAME
    assert session.target_identity is None


def test_target_change_requires_explicit_consent(tmp_path: Path) -> None:
    _write_profile(tmp_path, "ap")
    _write_profile(tmp_path, _TARGET_NAME)

    assert DevTargetRegistry.configure(tmp_path, profile_name="ap") == DevTarget("ap")
    marker = tmp_path / "config" / "state" / "dev-runtime-target.json"
    before = marker.read_text(encoding="utf-8")
    with pytest.raises(DevTargetError) as denied:
        DevTargetRegistry.configure(tmp_path, profile_name=_TARGET_NAME)
    assert _error_code(denied) == "DEV_TARGET_CHANGE_REQUIRES_CONSENT"
    assert marker.read_text(encoding="utf-8") == before

    changed = DevTargetRegistry.configure(
        tmp_path,
        profile_name=_TARGET_NAME,
        explicit_consent=True,
    )
    assert changed == DevTarget(_TARGET_NAME)


def test_repeating_current_default_does_not_require_consent(tmp_path: Path) -> None:
    _write_profile(tmp_path, "ap")

    first = DevTargetRegistry.configure(tmp_path)
    second = DevTargetRegistry.configure(tmp_path, profile_name="ap")

    assert first == second == DevTarget("ap")


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 999, "profile_name": _TARGET_NAME, "mod_name": "alas"},
        {"schema_version": 1, "profile_name": "../outside", "mod_name": "alas"},
        {"schema_version": 1, "profile_name": _TARGET_NAME, "mod_name": "other"},
    ],
)
def test_target_marker_rejects_invalid_assignment(tmp_path: Path, payload: dict[str, object]) -> None:
    _write_profile(tmp_path)
    marker = tmp_path / "config" / "state" / "dev-runtime-target.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DevTargetError) as error:
        DevTargetRegistry.load(tmp_path)
    assert _error_code(error) in {"DEV_TARGET_STATE_CORRUPT", "DEV_TARGET_INVALID"}


def test_target_marker_rejects_missing_structural_profile(tmp_path: Path) -> None:
    _write_profile(tmp_path)
    DevTargetRegistry.configure(
        tmp_path,
        profile_name=_TARGET_NAME,
        explicit_consent=True,
    )
    marker = tmp_path / "config" / "state" / "dev-runtime-target.json"
    marker.write_text(
        json.dumps({"schema_version": 1, "profile_name": "missing-target", "mod_name": "alas"}),
        encoding="utf-8",
    )

    with pytest.raises(DevTargetError) as error:
        DevTargetRegistry.load(tmp_path)
    assert _error_code(error) == "DEV_TARGET_PROFILE_MISSING"


@pytest.mark.parametrize("marker_state", ["corrupt", "too_large", "missing_profile"])
def test_configure_recovers_invalid_marker_with_explicit_target_consent(
    tmp_path: Path,
    marker_state: str,
) -> None:
    _write_profile(tmp_path, "ap")
    _write_profile(tmp_path, _TARGET_NAME)
    marker = tmp_path / "config" / "state" / "dev-runtime-target.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker_state == "corrupt":
        marker.write_text("{", encoding="utf-8")
    elif marker_state == "too_large":
        marker.write_bytes(b"x" * (target_module._MAX_TARGET_BYTES + 1))
    else:
        marker.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profile_name": "missing-target",
                    "mod_name": "alas",
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(DevTargetError) as denied:
        DevTargetRegistry.configure(tmp_path, profile_name=_TARGET_NAME)
    assert _error_code(denied) == "DEV_TARGET_CHANGE_REQUIRES_CONSENT"

    configured = DevTargetRegistry.configure(
        tmp_path,
        profile_name=_TARGET_NAME,
        explicit_consent=True,
    )

    assert configured == DevTarget(_TARGET_NAME)


def test_configure_recovers_unreadable_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_profile(tmp_path, "ap")
    _write_profile(tmp_path, _TARGET_NAME)
    marker = tmp_path / "config" / "state" / "dev-runtime-target.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    original_read = target_module.read_bounded_bytes

    def read_bounded_bytes(path: Path, *, max_bytes: int) -> bytes:
        if Path(path).name == target_module.DEV_TARGET_FILE_NAME:
            raise OSError("synthetic unreadable marker")
        return original_read(path, max_bytes=max_bytes)

    monkeypatch.setattr(target_module, "read_bounded_bytes", read_bounded_bytes)

    configured = DevTargetRegistry.configure(
        tmp_path,
        profile_name=_TARGET_NAME,
        explicit_consent=True,
    )

    assert configured == DevTarget(_TARGET_NAME)


def test_target_marker_symlink_is_rejected_when_supported(tmp_path: Path) -> None:
    _write_profile(tmp_path)
    target = DevTargetRegistry.configure(
        tmp_path,
        profile_name=_TARGET_NAME,
        explicit_consent=True,
    )
    marker = tmp_path / "config" / "state" / "dev-runtime-target.json"
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(target.as_dict()), encoding="utf-8")
    marker.unlink()
    try:
        marker.symlink_to(replacement)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"создание символьной ссылки недоступно: {type(exc).__name__}")

    with pytest.raises(DevTargetError) as error:
        DevTargetRegistry.load(tmp_path)
    assert _error_code(error) == "DEV_TARGET_UNSAFE_PATH"


def test_existing_session_keeps_recorded_target_after_registry_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# синтетический gui\n", encoding="utf-8")
    config = root / "config"
    config.mkdir()
    profile_payload = {
        "Alas": {"Emulator": {}},
        "General": {},
        "SyntheticTask": {
            "Scheduler": {
                "Enable": False,
                "Command": "SyntheticTask",
                "NextRun": "2026-08-31 00:00:00",
            }
        },
    }
    for profile_name in ("profile-a", "profile-b"):
        (config / f"{profile_name}.json").write_text(
            json.dumps(profile_payload), encoding="utf-8"
        )
    python = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    target_a = DevTargetRegistry.configure(
        root,
        profile_name="profile-a",
        explicit_consent=True,
    )
    environment_a = DevEnvironment(root, python, target_a)
    identity = ProcessIdentity(
        pid=7401,
        created_at=71.0,
        executable=str(environment_a.python_executable),
        command_line=tuple(ProcessBackend.expected_command(environment_a, "recorded-target-session")),
        cwd=str(root),
    )
    EvidenceStore.create(
        environment_a,
        session_id="recorded-target-session",
        root_tasks=["SyntheticTask"],
        excluded_tasks=[],
        timestamp="2026-08-31T00:00:00+00:00",
    )
    session = DevSession(
        session_id="recorded-target-session",
        state=DevSessionState.RUNNING,
        repository_root=str(root),
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
        process=identity,
        profile_name="profile-a",
        target_identity=target_module.target_identity(DevTarget("profile-a")),
    )
    state_path = root / "config" / "state" / "dev-runtime-session.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(session.as_dict()), encoding="utf-8")

    DevTargetRegistry.configure(
        root,
        profile_name="profile-b",
        explicit_consent=True,
    )
    environment_b = DevEnvironment(root, python, DevTarget("profile-b"))
    backend = ProcessBackend()
    monkeypatch.setattr(backend, "capture", lambda _pid: identity)
    monkeypatch.setattr(backend, "request_stop", lambda _identity: True)
    monkeypatch.setattr(backend, "wait_exit", lambda _identity, _timeout: True)
    manager = DevSessionManager(
        environment_b,
        process_backend=backend,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
        readiness_probe=lambda _environment, _identity: (True, "ready"),
    )

    assert manager.status().state == "running_owned"
    assert manager.get_evidence().ok is True
    stopped = manager.stop()

    assert stopped.ok is True
    restored = DevSession.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
    assert restored.state is DevSessionState.STOPPED
    assert restored.profile_name == "profile-a"
    assert restored.target_identity == target_module.target_identity(DevTarget("profile-a"))


def test_long_lived_manager_refreshes_target_before_new_read_only_call(
    tmp_path: Path,
) -> None:
    _write_profile(tmp_path, "profile-a")
    _write_profile(tmp_path, "profile-b")
    target_a = DevTargetRegistry.configure(
        tmp_path,
        profile_name="profile-a",
        explicit_consent=True,
    )
    environment = DevEnvironment(tmp_path, Path("python"), target_a)
    manager = DevSessionManager(
        environment,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
    )

    DevTargetRegistry.configure(
        tmp_path,
        profile_name="profile-b",
        explicit_consent=True,
    )

    result = manager.list_tasks()

    assert result.ok is True
    assert manager.environment.profile_name == "profile-b"


def test_long_lived_manager_reports_target_registry_error_as_result(
    tmp_path: Path,
) -> None:
    _write_profile(tmp_path)
    target = DevTargetRegistry.configure(
        tmp_path,
        profile_name=_TARGET_NAME,
        explicit_consent=True,
    )
    manager = DevSessionManager(
        DevEnvironment(tmp_path, Path("python"), target),
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
    )
    marker = tmp_path / "config" / "state" / "dev-runtime-target.json"
    marker.write_text("{", encoding="utf-8")

    result = manager.list_tasks()

    assert result.ok is False
    assert result.code == "DEV_TARGET_STATE_CORRUPT"
    assert result.details["error"]["code"] == "DEV_TARGET_STATE_CORRUPT"
