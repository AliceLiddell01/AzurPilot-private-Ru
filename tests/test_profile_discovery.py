from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import module.webui.app_dependencies as dependencies
from dev_tools.postgresql_migration import _profile_names
from module.config.profile import (
    MAX_PROFILE_CONFIG_BYTES,
    MAX_PROFILE_CONFIG_CANDIDATES,
    SUPPORTED_MOD_PROFILE_ROOTS,
    InvalidProfileConfigError,
    ProfileDiscoveryError,
    discover_profile_configs,
    discover_profile_names,
    is_profile_payload,
    parse_profile_config_bytes,
)
from module.config.utils import alas_instance, alas_template, is_oobe_needed
from module.submodule.utils import MOD_CONFIG_DICT, MOD_DICT, get_config_mod
from module.webui.deploy_settings import _validate_instance_name

ROOT = Path(__file__).resolve().parents[1]


def _alas_profile() -> dict[str, object]:
    return {
        "Alas": {"Emulator": {}},
        "General": {},
        "Main": {"Scheduler": {}},
    }


def _mod_profile(root: str) -> dict[str, object]:
    return {
        root: {"Emulator": {}},
        f"{root}Main": {"Scheduler": {}},
    }


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture(autouse=True)
def _restore_mod_registry():
    snapshot = dict(MOD_CONFIG_DICT)
    yield
    MOD_CONFIG_DICT.clear()
    MOD_CONFIG_DICT.update(snapshot)


def test_real_profiles_are_discovered_and_service_json_is_not(tmp_path, monkeypatch):
    config = tmp_path / "config"
    _write_json(config / "alas.json", _alas_profile())
    _write_json(config / "ap.json", _alas_profile())
    _write_json(config / "secondary.json", _alas_profile())
    _write_json(config / "modded.fpy.json", _mod_profile("Fpy"))
    _write_json(config / "template.json", _alas_profile())
    _write_json(config / "inspect_report.json", {"summary": {"profiles": 4}})
    _write_json(
        config / "postgresql_cutover_report.json",
        {"cutover_ready": True, "reason_codes": []},
    )
    _write_json(config / "future_service.json", {"Alas": {}})
    _write_json(config / "random_dict.json", {"arbitrary": {"value": 1}})
    _write_json(config / "state/storage_backend.json", _alas_profile())

    monkeypatch.chdir(tmp_path)

    assert discover_profile_names(config) == ["alas", "ap", "modded", "secondary"]
    assert alas_instance() == ["alas", "ap", "modded", "secondary"]
    assert get_config_mod("modded") == "fpy"
    assert not is_oobe_needed()
    assert _validate_instance_name("alas", True) == "alas"
    with pytest.raises(ValueError, match="не существует"):
        _validate_instance_name("future_service", True)

    MOD_CONFIG_DICT.clear()
    assert dependencies.alas_instance() == ["alas", "modded", "secondary"]
    assert get_config_mod("modded") == "fpy"
    copy_from = alas_template() + dependencies.alas_instance()
    assert "template-alas" in copy_from
    assert "future_service" not in copy_from
    assert "inspect_report" not in copy_from


def test_service_json_does_not_suppress_oobe(tmp_path, monkeypatch):
    config = tmp_path / "config"
    _write_json(config / "future_service.json", {"Alas": {}})
    _write_json(config / "random_dict.json", {"status": "ready"})
    monkeypatch.chdir(tmp_path)

    assert discover_profile_names(config) == []
    assert is_oobe_needed()

    assert dependencies.alas_instance() == []
    assert dependencies.is_oobe_needed()


def test_internal_ap_remains_low_level_but_webui_oobe_stays_user_facing(
    tmp_path, monkeypatch
):
    config = tmp_path / "config"
    _write_json(config / "ap.json", _alas_profile())
    monkeypatch.chdir(tmp_path)

    assert alas_instance() == ["ap"]
    assert not is_oobe_needed()

    assert dependencies.alas_instance() == []
    assert dependencies.is_oobe_needed()


def test_malformed_array_and_oversized_candidates_are_skipped(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "malformed.json").write_text("{", encoding="utf-8")
    _write_json(config / "array.json", ["Alas"])
    (config / "oversized.json").write_bytes(b" " * (MAX_PROFILE_CONFIG_BYTES + 1))

    assert discover_profile_names(config) == []
    with pytest.raises(ProfileDiscoveryError, match="PROFILE_CONFIG_UNSAFE"):
        discover_profile_configs(config, strict=True)


def test_unreadable_candidate_is_skipped_or_rejected_by_mode(tmp_path, monkeypatch):
    config = tmp_path / "config"
    _write_json(config / "unreadable.json", _alas_profile())

    original_read_bytes = Path.read_bytes

    def fail_one(path: Path) -> bytes:
        if path.name == "unreadable.json":
            raise OSError("synthetic unreadable file")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_one)

    assert discover_profile_names(config) == []
    with pytest.raises(ProfileDiscoveryError, match="PROFILE_CONFIG_UNSAFE"):
        discover_profile_configs(config, strict=True)


def test_symlink_candidate_is_skipped_and_strict_migration_rejects_it(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    outside = tmp_path / "outside.json"
    _write_json(outside, _alas_profile())
    link = config / "linked.json"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink недоступен в тестовом окружении: {exc}")

    assert discover_profile_names(config) == []
    with pytest.raises(ProfileDiscoveryError, match="PROFILE_CONFIG_UNSAFE"):
        discover_profile_names(config, strict=True)


def test_reparse_candidate_is_rejected_even_without_platform_symlink_support(
    tmp_path, monkeypatch
):
    import module.config.profile as profile_module

    config = tmp_path / "config"
    candidate = config / "linked.json"
    _write_json(candidate, _alas_profile())
    original_is_link = profile_module._is_link

    monkeypatch.setattr(
        profile_module,
        "_is_link",
        lambda path: path == candidate or original_is_link(path),
    )

    assert discover_profile_names(config) == []
    with pytest.raises(ProfileDiscoveryError, match="PROFILE_CONFIG_UNSAFE"):
        discover_profile_names(config, strict=True)


def test_candidate_count_guard_fails_closed(tmp_path):
    config = tmp_path / "config"
    for index in range(MAX_PROFILE_CONFIG_CANDIDATES + 1):
        _write_json(config / f"service_{index:03}.json", {"status": "ready"})

    assert discover_profile_names(config) == []
    with pytest.raises(ProfileDiscoveryError, match="PROFILE_CONFIG_COUNT_EXCEEDED"):
        discover_profile_names(config, strict=True)


def test_upload_parser_accepts_profiles_and_rejects_reports_and_unknown_mods():
    identity, data = parse_profile_config_bytes(
        json.dumps(_alas_profile()).encode(), "alas.json"
    )
    assert identity.name == "alas"
    assert identity.mod_name == "alas"
    assert is_profile_payload(data)

    identity, _ = parse_profile_config_bytes(
        json.dumps(_mod_profile("Fpy")).encode(), "modded.fpy.json"
    )
    assert identity.name == "modded"
    assert identity.mod_name == "fpy"

    invalid_uploads = (
        (b"{", "broken.json"),
        (b"[]", "list.json"),
        (json.dumps({"Alas": {}}).encode(), "weak.json"),
        (json.dumps({"report": {"status": "ready"}}).encode(), "report.json"),
        (json.dumps(_mod_profile("Other")).encode(), "modded.other.json"),
        (json.dumps(_alas_profile()).encode(), "template-copy.json"),
    )
    for content, filename in invalid_uploads:
        with pytest.raises(InvalidProfileConfigError):
            parse_profile_config_bytes(content, filename)


def test_migration_profile_inventory_uses_canonical_classifier(tmp_path):
    config = tmp_path / "config"
    _write_json(config / "alas.json", _alas_profile())
    _write_json(config / "future_service.json", {"Alas": {}})
    _write_json(config / "report.json", {"cutover_ready": True})
    _write_json(config / "modded.maa.json", _mod_profile("Maa"))

    assert _profile_names(tmp_path) == ("alas", "modded")


def test_production_templates_satisfy_the_canonical_payload_contract():
    alas_template_payload = json.loads(
        (ROOT / "config/template.json").read_text(encoding="utf-8")
    )
    maa_template_payload = json.loads(
        (ROOT / "config/template.maa.json").read_text(encoding="utf-8")
    )
    fpy_template_payload = json.loads(
        (ROOT / "config/template.fpy.json").read_text(encoding="utf-8")
    )

    assert is_profile_payload(alas_template_payload)
    assert is_profile_payload(maa_template_payload, "maa")
    assert is_profile_payload(fpy_template_payload, "fpy")
    assert set(SUPPORTED_MOD_PROFILE_ROOTS) == set(MOD_DICT)


def test_legacy_upload_rejects_root_service_json_before_write(tmp_path, monkeypatch):
    from module.webui.api import api_import_legacy_upload

    class Upload:
        filename = "old/config/future_service.json"

        async def read(self) -> bytes:
            return json.dumps({"status": "ready"}).encode()

    class Form:
        @staticmethod
        def getlist(_name: str) -> list[Upload]:
            return [Upload()]

    class Request:
        @staticmethod
        async def form() -> Form:
            return Form()

    monkeypatch.chdir(tmp_path)
    response = asyncio.run(api_import_legacy_upload(Request()))

    assert response.status_code == 400
    assert not (tmp_path / "config/future_service.json").exists()


def test_legacy_upload_accepts_canonical_profile(tmp_path, monkeypatch):
    from module.webui.api import api_import_legacy_upload

    class Upload:
        filename = "old/config/alas.json"

        async def read(self) -> bytes:
            return json.dumps(_alas_profile()).encode()

    class Form:
        @staticmethod
        def getlist(_name: str) -> list[Upload]:
            return [Upload()]

    class Request:
        @staticmethod
        async def form() -> Form:
            return Form()

    monkeypatch.chdir(tmp_path)
    response = asyncio.run(api_import_legacy_upload(Request()))

    assert response.status_code == 200
    assert discover_profile_names(tmp_path / "config") == ["alas"]


def test_discovery_does_not_materialize_non_profile_json(tmp_path):
    config = tmp_path / "config"
    report = config / "future_service.json"
    _write_json(report, {"status": "ready"})
    before = report.read_bytes()

    assert discover_profile_names(config) == []
    assert report.read_bytes() == before
