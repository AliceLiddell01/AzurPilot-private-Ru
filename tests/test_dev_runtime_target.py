from __future__ import annotations

import json
from pathlib import Path

import pytest

from module.dev_runtime import DevTarget, DevTargetError, DevTargetRegistry


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

    configured = DevTargetRegistry.configure(tmp_path, profile_name=_TARGET_NAME)
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


def test_target_resolution_fails_closed_without_marker_or_profile(tmp_path: Path) -> None:
    _write_profile(tmp_path)

    with pytest.raises(DevTargetError) as missing_marker:
        DevTargetRegistry.load(tmp_path)
    assert _error_code(missing_marker) == "DEV_TARGET_NOT_CONFIGURED"

    with pytest.raises(DevTargetError) as missing_profile:
        DevTargetRegistry.configure(tmp_path, profile_name="other-target")
    assert _error_code(missing_profile) == "DEV_TARGET_PROFILE_MISSING"


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


def test_target_marker_rejects_missing_and_duplicate_structural_profile(tmp_path: Path) -> None:
    _write_profile(tmp_path)
    DevTargetRegistry.configure(tmp_path, profile_name=_TARGET_NAME)
    (tmp_path / "config" / f"{_TARGET_NAME}-extra.json").write_text(
        json.dumps({"Alas": {"Emulator": {}}, "General": {}, "Task": {"Scheduler": {}}}),
        encoding="utf-8",
    )
    marker = tmp_path / "config" / "state" / "dev-runtime-target.json"
    marker.write_text(
        json.dumps({"schema_version": 1, "profile_name": "missing-target", "mod_name": "alas"}),
        encoding="utf-8",
    )

    with pytest.raises(DevTargetError) as error:
        DevTargetRegistry.load(tmp_path)
    assert _error_code(error) == "DEV_TARGET_PROFILE_MISSING"


def test_target_marker_symlink_is_rejected_when_supported(tmp_path: Path) -> None:
    _write_profile(tmp_path)
    target = DevTargetRegistry.configure(tmp_path, profile_name=_TARGET_NAME)
    marker = tmp_path / "config" / "state" / "dev-runtime-target.json"
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(target.as_dict()), encoding="utf-8")
    marker.unlink()
    try:
        marker.symlink_to(replacement)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {type(exc).__name__}")

    with pytest.raises(DevTargetError) as error:
        DevTargetRegistry.load(tmp_path)
    assert _error_code(error) == "DEV_TARGET_UNSAFE_PATH"
