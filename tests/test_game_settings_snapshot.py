from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from module.game_settings.enforcement import GameSettingsEnforcementScanner
from module.game_settings.model import (
    FrameRateValue,
    GameSettingDefinition,
    GameSettingRequirement,
    GameSettingState,
    GameSettingsScanResult,
    StoryAutoplayValue,
    TextAutoScrollSpeedValue,
)
from module.game_settings.options_detector import (
    GameSettingOptionObservation,
    GameSettingRowObservation,
)
from module.game_settings.registry import (
    GAME_SETTINGS_OPTIONS_REGISTRY,
    GameSettingCheckSpec,
    build_game_settings_registry,
)
from module.game_settings.scanner import GameSettingsScanner
from module.game_settings.snapshot import (
    CURRENT_GAME_SETTINGS_SCOPE,
    GameSettingsEnvironmentScope,
    GameSettingsSnapshotAccessSource,
    GameSettingsSnapshotSource,
    GameSettingsSnapshotStatus,
    create_game_settings_snapshot,
    deserialize_game_settings_snapshot,
    game_settings_requirements_fingerprint,
    get_or_refresh_game_settings_snapshot,
    invalidate_game_settings_snapshot,
    load_game_settings_snapshot,
    save_game_settings_snapshot,
    serialize_game_settings_snapshot,
)
from module.game_settings.traversal import OptionsTraversalResult, OptionsViewport


ROOT = Path(__file__).resolve().parents[1]
_EXPECTED = (
    ("frame_rate", FrameRateValue.FPS_60),
    ("opsi_reduce_tb_guidance", GameSettingState.ON),
    ("opsi_auto_use_items", GameSettingState.ON),
    ("opsi_default_auto_mode_threat_safe", GameSettingState.OFF),
    ("story_autoplay", StoryAutoplayValue.ENABLED),
    ("text_auto_scroll_speed", TextAutoScrollSpeedValue.VERY_FAST),
    ("enable_idle_screen", GameSettingState.OFF),
    ("duplicate_ship_display", GameSettingState.OFF),
    ("display_quick_switch_prompt", GameSettingState.OFF),
    ("display_battle_result_cutscene", GameSettingState.OFF),
    ("custom_ship_names", GameSettingState.OFF),
)
_KEYS = tuple(key for key, _ in _EXPECTED)


def _result(overrides=None):
    by_key = {entry.key: entry for entry in GAME_SETTINGS_OPTIONS_REGISTRY}
    overrides = {} if overrides is None else overrides
    return GameSettingsScanResult(
        by_key[key].make_result(overrides.get(key, required))
        for key, required in _EXPECTED
    )


def _doc(result=None, **kwargs):
    result = _result() if result is None else result
    return json.loads(
        serialize_game_settings_snapshot(
            create_game_settings_snapshot(result, **kwargs)
        )
    )


def _write(path: Path, document) -> None:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


class _Audit(GameSettingsScanner):
    def __init__(self, result, path=None, error=None):
        self.result = result
        self.error = error
        if path is not None:
            self.game_settings_snapshot_path = path

    def _scan_game_settings(self):
        if self.error is not None:
            raise self.error
        return self.result


class _NoopEnforcement(GameSettingsEnforcementScanner):
    def __init__(self, result, path):
        self.result = result
        self.game_settings_snapshot_path = path

    def _scan_game_settings(self):
        return self.result


class _Device:
    def __init__(self, owner):
        self.owner = owner
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)

    def click(self, button):
        key = button.name[len("GAME_SETTINGS_") : -len("_TARGET")].lower()
        self.owner.events.append(f"click:{key}")
        if key != self.owner.verify_fail_key:
            self.owner.states[key] = GameSettingState.ON


class _MutatingEnforcement(GameSettingsEnforcementScanner):
    def __init__(self, keys=("setting_a",), *, persistable=True):
        self.events = []
        self.states = {key: GameSettingState.OFF for key in keys}
        self.verify_fail_key = None
        self.persistable = persistable
        self.device = _Device(self)
        entries = []
        for index, key in enumerate(keys):
            definition = GameSettingDefinition(key, "options")
            requirement = GameSettingRequirement(definition, GameSettingState.ON)

            def detector(_image, *, _key=key):
                return self.states[_key]

            def observer(_image, *, _key=key, _index=index):
                y = 180 + _index * 90
                return GameSettingRowObservation(
                    value=self.states[_key],
                    row_bounds=(250, y - 5, 760, y + 25),
                    options=(
                        GameSettingOptionObservation(
                            value=GameSettingState.OFF,
                            bounds=(430, y, 486, y + 20),
                            click_bounds=(422, y - 8, 520, y + 28),
                            marker_activity=0.2,
                        ),
                        GameSettingOptionObservation(
                            value=GameSettingState.ON,
                            bounds=(560, y, 616, y + 20),
                            click_bounds=(552, y - 8, 650, y + 28),
                            marker_activity=0.2,
                        ),
                    ),
                )

            entries.append(
                GameSettingCheckSpec(
                    definition=definition,
                    detector=detector,
                    requirement=requirement,
                    observer=observer,
                )
            )
        self.check_registry = build_game_settings_registry(entries, require_enforce=True)
        self.scan_calls = 0

    def _scan_game_settings(self):
        self.scan_calls += 1
        self.events.append(f"scan:{self.scan_calls}")
        return GameSettingsScanResult(
            entry.make_result(self.states[entry.key]) for entry in self.check_registry
        )

    def _should_persist_game_settings_snapshot(self, _result):
        return self.persistable

    def invalidate_game_settings_snapshot(self):
        self.events.append("invalidate")

    def persist_game_settings_snapshot(
        self,
        _result,
        *,
        source=GameSettingsSnapshotSource.AUDIT,
    ):
        self.events.append(f"persist:{source.value}")
        return None

    def traverse_options(self, visitor):
        stopped = bool(
            visitor(
                OptionsViewport(
                    index=1,
                    scroll_offset=0.0,
                    is_top=True,
                    is_bottom=False,
                )
            )
        )
        return OptionsTraversalResult(
            visited_viewports=1,
            final_offset=0.0,
            reached_bottom=True,
            stopped_early=stopped,
        )

    def _wait_options_stable(self):
        return self.device.image.copy()

    def return_to_main(self):
        return True


class SnapshotSchemaTests(unittest.TestCase):
    def test_round_trip_is_typed_exact_eleven_and_has_no_derived_bools(self):
        result = _result()
        snapshot = create_game_settings_snapshot(
            result,
            scanned_at=datetime(2026, 8, 11, 0, 40, tzinfo=timezone.utc),
        )
        payload = serialize_game_settings_snapshot(snapshot)
        restored = deserialize_game_settings_snapshot(payload)
        document = json.loads(payload)
        self.assertEqual(restored.scan_result, result)
        self.assertEqual(tuple(item["key"] for item in document["settings"]), _KEYS)
        self.assertEqual(len(document["settings"]), 11)
        self.assertIsInstance(
            restored.scan_result.get("frame_rate").detected_value,
            FrameRateValue,
        )
        self.assertIsInstance(
            restored.scan_result.get("story_autoplay").detected_value,
            StoryAutoplayValue,
        )
        self.assertIsInstance(
            restored.scan_result.get("text_auto_scroll_speed").detected_value,
            TextAutoScrollSpeedValue,
        )
        self.assertIsInstance(
            restored.scan_result.get("custom_ship_names").detected_value,
            GameSettingState,
        )
        self.assertNotIn("all_required_compatible", document)
        self.assertTrue(payload.endswith("\n"))
        self.assertEqual(payload, serialize_game_settings_snapshot(restored))

    def test_corrupt_unsupported_incomplete_duplicate_and_typed_errors_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            cases = []
            unsupported = _doc()
            unsupported["schema_version"] = 999
            cases.append(("schema", unsupported, GameSettingsSnapshotStatus.UNSUPPORTED_SCHEMA))
            bool_schema = _doc()
            bool_schema["schema_version"] = True
            cases.append(("bool-schema", bool_schema, GameSettingsSnapshotStatus.UNSUPPORTED_SCHEMA))
            typed = _doc()
            typed["settings"][0]["detected"] = "120_fps"
            cases.append(("typed", typed, GameSettingsSnapshotStatus.CORRUPT))
            family = _doc()
            family["settings"][0]["kind"] = "toggle"
            cases.append(("family", family, GameSettingsSnapshotStatus.CORRUPT))
            incomplete = _doc()
            incomplete["settings"].pop()
            cases.append(("incomplete", incomplete, GameSettingsSnapshotStatus.INCOMPLETE))
            duplicate = _doc()
            duplicate["settings"].append(dict(duplicate["settings"][0]))
            cases.append(("duplicate", duplicate, GameSettingsSnapshotStatus.CORRUPT))
            changed = _doc()
            changed["settings"][-1]["required"] = "on"
            cases.append(("required", changed, GameSettingsSnapshotStatus.REQUIREMENTS_CHANGED))
            for name, document, expected in cases:
                with self.subTest(name=name):
                    path = directory / f"{name}.json"
                    _write(path, document)
                    self.assertIs(load_game_settings_snapshot(path=path).status, expected)
            for name, payload in (
                ("json", b'{"schema_version": 1'),
                ("utf8", b"\xff\xfe"),
                ("top", b"[]\n"),
            ):
                with self.subTest(name=name):
                    path = directory / f"raw-{name}.json"
                    path.write_bytes(payload)
                    self.assertIs(
                        load_game_settings_snapshot(path=path).status,
                        GameSettingsSnapshotStatus.CORRUPT,
                    )

    def test_invalid_timestamp_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            for index, value in enumerate(("bad", "2026-08-11T03:40:00")):
                path = Path(temp) / f"{index}.json"
                document = _doc()
                document["scanned_at"] = value
                _write(path, document)
                self.assertIs(
                    load_game_settings_snapshot(path=path).status,
                    GameSettingsSnapshotStatus.CORRUPT,
                )

    def test_incompatible_and_unknown_are_valid_records_but_do_not_satisfy(self):
        with tempfile.TemporaryDirectory() as temp:
            for index, state in enumerate((GameSettingState.ON, GameSettingState.UNKNOWN)):
                path = Path(temp) / f"{index}.json"
                save_game_settings_snapshot(
                    _result({"custom_ship_names": state}),
                    path=path,
                )
                loaded = load_game_settings_snapshot(path=path)
                self.assertTrue(loaded.valid)
                self.assertFalse(loaded.snapshot.all_required_compatible)
                self.assertFalse(loaded.snapshot.satisfies(("custom_ship_names",)))


class FingerprintTests(unittest.TestCase):
    def test_same_contract_is_stable_in_another_process(self):
        current = game_settings_requirements_fingerprint()
        command = (
            "from module.game_settings.snapshot import "
            "game_settings_requirements_fingerprint; "
            "print(game_settings_requirements_fingerprint())"
        )
        completed = subprocess.run(
            [sys.executable, "-c", command],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        output_lines = tuple(
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip()
        )
        self.assertTrue(output_lines)
        self.assertEqual(output_lines[-1], current)
        self.assertRegex(current, r"^[0-9a-f]{64}$")

    def test_required_value_key_add_and_remove_change_fingerprint(self):
        current = game_settings_requirements_fingerprint()
        changed = []
        for entry in GAME_SETTINGS_OPTIONS_REGISTRY:
            if entry.key == "custom_ship_names":
                changed.append(
                    replace(
                        entry,
                        requirement=GameSettingRequirement(
                            entry.definition,
                            GameSettingState.ON,
                        ),
                    )
                )
            else:
                changed.append(entry)
        definition = GameSettingDefinition("snapshot_extra", "options")
        extra = GameSettingCheckSpec(
            definition=definition,
            detector=lambda _image: GameSettingState.OFF,
            requirement=GameSettingRequirement(definition, GameSettingState.OFF),
        )
        self.assertNotEqual(game_settings_requirements_fingerprint(changed), current)
        self.assertNotEqual(
            game_settings_requirements_fingerprint(
                (*GAME_SETTINGS_OPTIONS_REGISTRY, extra)
            ),
            current,
        )
        self.assertNotEqual(
            game_settings_requirements_fingerprint(GAME_SETTINGS_OPTIONS_REGISTRY[:-1]),
            current,
        )

    def test_detector_identity_and_unrelated_git_sha_do_not_change_fingerprint(self):
        first = GAME_SETTINGS_OPTIONS_REGISTRY[0]
        alternate = replace(
            first,
            detector=lambda _image: first.requirement.expected_value,
        )
        registry = (alternate, *GAME_SETTINGS_OPTIONS_REGISTRY[1:])
        self.assertEqual(
            game_settings_requirements_fingerprint(registry),
            game_settings_requirements_fingerprint(),
        )
        with patch.dict(os.environ, {"GITHUB_SHA": "a" * 40}, clear=False):
            first_sha = game_settings_requirements_fingerprint()
        with patch.dict(os.environ, {"GITHUB_SHA": "b" * 40}, clear=False):
            second_sha = game_settings_requirements_fingerprint()
        self.assertEqual(first_sha, second_sha)

    def test_old_fingerprint_is_requirements_changed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            document = _doc()
            document["requirements_fingerprint"] = "0" * 64
            _write(path, document)
            self.assertIs(
                load_game_settings_snapshot(path=path).status,
                GameSettingsSnapshotStatus.REQUIREMENTS_CHANGED,
            )


class ScopeFreshnessPersistenceTests(unittest.TestCase):
    def test_server_resolution_ui_and_package_scope_mismatch(self):
        base = CURRENT_GAME_SETTINGS_SCOPE
        scopes = (
            GameSettingsEnvironmentScope(
                "jp", base.package_name, base.resolution, base.ui_profile
            ),
            GameSettingsEnvironmentScope(
                base.server, base.package_name, (1920, 1080), base.ui_profile
            ),
            GameSettingsEnvironmentScope(
                base.server, base.package_name, base.resolution, "legacy"
            ),
            GameSettingsEnvironmentScope(
                base.server, "other.package", base.resolution, base.ui_profile
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            for index, scope in enumerate(scopes):
                path = Path(temp) / f"{index}.json"
                save_game_settings_snapshot(_result(), path=path, scope=scope)
                self.assertIs(
                    load_game_settings_snapshot(path=path).status,
                    GameSettingsSnapshotStatus.SCOPE_MISMATCH,
                )

    def test_freshness_is_consumer_controlled_without_sleep(self):
        scanned = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
        now = scanned + timedelta(hours=3)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            save_game_settings_snapshot(_result(), path=path, scanned_at=scanned)
            self.assertTrue(
                load_game_settings_snapshot(path=path, max_age=None, now=now).valid
            )
            self.assertTrue(
                load_game_settings_snapshot(
                    path=path,
                    max_age=timedelta(hours=4),
                    now=now,
                ).valid
            )
            self.assertIs(
                load_game_settings_snapshot(
                    path=path,
                    max_age=timedelta(hours=2),
                    now=now,
                ).status,
                GameSettingsSnapshotStatus.STALE,
            )
            future_path = Path(temp) / "future.json"
            save_game_settings_snapshot(
                _result(),
                path=future_path,
                scanned_at=now + timedelta(minutes=1),
            )
            self.assertIs(
                load_game_settings_snapshot(
                    path=future_path,
                    max_age=timedelta(hours=4),
                    now=now,
                ).status,
                GameSettingsSnapshotStatus.STALE,
            )

    def test_missing_race_during_read_is_missing_not_corrupt(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            path.write_text("placeholder", encoding="utf-8")

            def disappear(*_args, **_kwargs):
                path.unlink()
                return ""

            with patch(
                "module.game_settings.snapshot.atomic_read_text",
                side_effect=disappear,
            ):
                loaded = load_game_settings_snapshot(path=path)
            self.assertIs(loaded.status, GameSettingsSnapshotStatus.MISSING)

    def test_atomic_success_and_failure_before_replace_preserves_previous(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / "snapshot.json"
            save_game_settings_snapshot(_result(), path=path)
            before = path.read_bytes()
            self.assertTrue(load_game_settings_snapshot(path=path).valid)
            self.assertEqual(list(directory.glob("snapshot.json.*.tmp")), [])
            with patch("deploy.atomic.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    save_game_settings_snapshot(
                        _result({"custom_ship_names": GameSettingState.ON}),
                        path=path,
                    )
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(directory.glob("snapshot.json.*.tmp")), [])

    def test_completed_audit_writes_and_operational_failure_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            _Audit(_result(), path).scan_game_settings()
            loaded = load_game_settings_snapshot(path=path)
            self.assertTrue(loaded.valid)
            self.assertIs(loaded.snapshot.source, GameSettingsSnapshotSource.AUDIT)
            before = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "audit failed"):
                _Audit(
                    _result(),
                    path,
                    RuntimeError("audit failed"),
                ).scan_game_settings()
            self.assertEqual(path.read_bytes(), before)

    def test_invalidation_is_idempotent_and_runtime_path_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            invalidate_game_settings_snapshot(path)
            save_game_settings_snapshot(_result(), path=path)
            invalidate_game_settings_snapshot(path)
            invalidate_game_settings_snapshot(path)
            self.assertFalse(path.exists())
        completed = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "--",
                "config/game_settings_snapshot.json",
            ],
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)

    def test_schema_contains_no_private_or_device_identity_data(self):
        document = _doc()
        expected_top_level = {
            "schema_version",
            "scanned_at",
            "source",
            "scope",
            "requirements_fingerprint",
            "settings",
        }
        expected_scope_fields = {
            "server",
            "package_name",
            "resolution",
            "ui_profile",
        }
        expected_setting_fields = {
            "key",
            "location",
            "kind",
            "value_family",
            "detected",
            "required",
        }
        self.assertEqual(set(document), expected_top_level)
        self.assertEqual(set(document["scope"]), expected_scope_fields)
        observed_fields = set(document)
        observed_fields.update(document["scope"])
        for setting in document["settings"]:
            self.assertEqual(set(setting), expected_setting_fields)
            observed_fields.update(setting)
        for forbidden in (
            "player_name",
            "uid",
            "device_serial",
            "token",
            "screenshot",
            "ocr_raw",
            "absolute_path",
        ):
            self.assertNotIn(forbidden, observed_fields)


class CacheApiTests(unittest.TestCase):
    def test_valid_cache_hit_constructs_no_scanner_and_repeat_read_does_not_write(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            save_game_settings_snapshot(
                _result(),
                path=path,
                scanned_at=datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc),
            )
            before = (path.read_bytes(), path.stat().st_mtime_ns)

            def forbidden():
                raise AssertionError("cache hit must not construct scanner/device")

            access = get_or_refresh_game_settings_snapshot(forbidden, path=path)
            second = load_game_settings_snapshot(path=path)
            self.assertIs(access.source, GameSettingsSnapshotAccessSource.SNAPSHOT)
            self.assertTrue(second.valid)
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_missing_stale_requirements_scope_misses_refresh_once(self):
        fixed = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            for case in ("missing", "stale", "requirements", "scope"):
                path = Path(temp) / f"{case}.json"
                if case == "stale":
                    save_game_settings_snapshot(
                        _result(),
                        path=path,
                        scanned_at=fixed - timedelta(days=2),
                    )
                elif case == "requirements":
                    document = _doc()
                    document["requirements_fingerprint"] = "0" * 64
                    _write(path, document)
                elif case == "scope":
                    save_game_settings_snapshot(
                        _result(),
                        path=path,
                        scope=GameSettingsEnvironmentScope(
                            "jp",
                            CURRENT_GAME_SETTINGS_SCOPE.package_name,
                            CURRENT_GAME_SETTINGS_SCOPE.resolution,
                            CURRENT_GAME_SETTINGS_SCOPE.ui_profile,
                        ),
                    )
                calls = 0

                def factory():
                    nonlocal calls
                    calls += 1
                    return _Audit(_result())

                access = get_or_refresh_game_settings_snapshot(
                    factory,
                    path=path,
                    max_age=(timedelta(hours=1) if case == "stale" else None),
                    now=fixed,
                )
                self.assertEqual(calls, 1)
                self.assertIs(
                    access.source,
                    GameSettingsSnapshotAccessSource.LIVE_AUDIT,
                )
                self.assertTrue(load_game_settings_snapshot(path=path).valid)

    def test_force_refresh_invokes_live_scanner_even_when_cache_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            save_game_settings_snapshot(_result(), path=path)
            calls = 0

            def factory():
                nonlocal calls
                calls += 1
                return _Audit(_result())

            access = get_or_refresh_game_settings_snapshot(
                factory,
                path=path,
                force_refresh=True,
            )
            self.assertEqual(calls, 1)
            self.assertIs(
                access.source,
                GameSettingsSnapshotAccessSource.LIVE_AUDIT,
            )
            self.assertIsNone(access.cache_status)


class EnforcementSnapshotTests(unittest.TestCase):
    def test_noop_and_unknown_blocked_keep_completed_initial_audit_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            canonical = Path(temp) / "canonical.json"
            result = _NoopEnforcement(
                _result(),
                canonical,
            ).enforce_required_game_settings()
            self.assertTrue(result.success)
            self.assertTrue(load_game_settings_snapshot(path=canonical).valid)

            unknown_path = Path(temp) / "unknown.json"
            unknown = _result({"custom_ship_names": GameSettingState.UNKNOWN})
            blocked = _NoopEnforcement(
                unknown,
                unknown_path,
            ).enforce_required_game_settings()
            loaded = load_game_settings_snapshot(path=unknown_path)
            self.assertTrue(blocked.blocked)
            self.assertTrue(loaded.valid)
            self.assertIs(
                loaded.snapshot.scan_result.get("custom_ship_names").detected_value,
                GameSettingState.UNKNOWN,
            )

    def test_invalidation_happens_once_before_first_click_and_success_persists_final_once(self):
        scanner = _MutatingEnforcement(("setting_a", "setting_b"))
        result = scanner.enforce_required_game_settings()
        self.assertTrue(result.success)
        self.assertEqual(scanner.events.count("invalidate"), 1)
        invalidate = scanner.events.index("invalidate")
        first_click = min(
            i
            for i, event in enumerate(scanner.events)
            if event.startswith("click:")
        )
        self.assertLess(invalidate, first_click)
        self.assertEqual(scanner.events.count("persist:audit"), 1)
        self.assertEqual(
            scanner.events.count("persist:enforcement_final_audit"),
            1,
        )
        self.assertGreater(
            scanner.events.index("persist:enforcement_final_audit"),
            scanner.events.index("scan:2"),
        )

    def test_custom_registry_still_invalidates_before_mutation(self):
        scanner = _MutatingEnforcement(("setting_a",), persistable=False)
        result = scanner.enforce_required_game_settings()
        self.assertTrue(result.success)
        self.assertIn("invalidate", scanner.events)
        self.assertNotIn("persist:audit", scanner.events)
        self.assertNotIn("persist:enforcement_final_audit", scanner.events)

    def test_partial_apply_failure_never_persists_final_snapshot(self):
        scanner = _MutatingEnforcement(("setting_a", "setting_b"))
        scanner.verify_fail_key = "setting_b"
        result = scanner.enforce_required_game_settings()
        self.assertFalse(result.success)
        self.assertEqual(result.changed_keys, ("setting_a",))
        self.assertIn("invalidate", scanner.events)
        self.assertEqual(scanner.events.count("persist:audit"), 1)
        self.assertNotIn("persist:enforcement_final_audit", scanner.events)


if __name__ == "__main__":
    unittest.main()
