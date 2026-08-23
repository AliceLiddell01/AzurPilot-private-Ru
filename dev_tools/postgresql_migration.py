"""Offline CLI для PostgreSQL Stage 3 без production wiring."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

from module.application.errors import StorageError
from module.application.migration_models import RecordDisposition
from module.application.migration_service import MigrationService, finalize_rehearsal
from module.persistence import DatabaseSettings, LazyEngine
from module.persistence.legacy import LegacySourceReader, create_consistent_snapshot
from module.persistence.legacy.reader import LegacySourceError
from module.persistence.migration_target import PostgresMigrationTarget


def _has_link_component(root: Path, path: Path) -> bool:
    current = path
    while current != root:
        if current.is_symlink() or current.is_junction():
            return True
        current = current.parent
    return False


def _profile_names(root: Path) -> tuple[str, ...]:
    names: list[str] = []
    config = root / "config"
    if not config.is_dir():
        return ()
    paths = sorted(config.glob("*.json"))
    if len(paths) > 512:
        raise LegacySourceError("PROFILE_CONFIG_COUNT_EXCEEDED")
    for path in paths:
        if path.name.startswith(("template", "args", "menu")):
            continue
        resolved = path.resolve(strict=True)
        if (
            _has_link_component(root, path)
            or not resolved.is_relative_to(root)
            or resolved.stat().st_size > 1024 * 1024
        ):
            raise LegacySourceError("PROFILE_CONFIG_UNSAFE")
        try:
            data = json.loads(resolved.read_text(encoding="utf-8"))
        except OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError:
            continue
        if isinstance(data, dict) and isinstance(data.get("Alas"), dict):
            names.append(path.stem)
    return tuple(names)


def _decryption_ids(root: Path) -> tuple[str, ...]:
    path = root / "log" / "device_id.json"
    if not path.is_file():
        return ()
    resolved = path.resolve(strict=True)
    if (
        _has_link_component(root, path)
        or not resolved.is_relative_to(root)
        or resolved.stat().st_size > 16_384
    ):
        raise LegacySourceError("DECRYPTION_PROVENANCE_UNSAFE")
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError:
        return ()
    value = data.get("device_id") if isinstance(data, dict) else None
    return (value,) if isinstance(value, str) and value else ()


def _reader(root: Path, timezone: str) -> LegacySourceReader:
    return LegacySourceReader(
        root,
        legacy_timezone=timezone,
        profile_names=_profile_names(root),
        decryption_ids=_decryption_ids(root),
    )


def _target(settings: DatabaseSettings) -> PostgresMigrationTarget:
    return PostgresMigrationTarget(LazyEngine(settings))


def _require_disposable(settings: DatabaseSettings, scratch: str | None = None) -> None:
    expected = {
        "host": os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE_HOST"),
        "port": os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE_PORT"),
        "database": os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE_DATABASE"),
        "user": os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE_USER"),
    }
    actual = {
        "host": settings.host,
        "port": str(settings.port),
        "database": settings.database,
        "user": settings.user,
    }
    if os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE") != "1" or expected != actual:
        raise LegacySourceError("DISPOSABLE_TARGET_NOT_CONFIRMED")
    if scratch is not None:
        expected_scratch = os.environ.get(
            "AZURPILOT_POSTGRES_DISPOSABLE_SCRATCH_DATABASE"
        )
        if (
            not expected_scratch
            or expected_scratch != scratch
            or scratch in {settings.database, "postgres"}
        ):
            raise LegacySourceError("SCRATCH_TARGET_NOT_CONFIRMED")


def _run_pg(executable: str, arguments: list[str], settings: DatabaseSettings) -> None:
    environment = os.environ.copy()
    if settings.password:
        environment["PGPASSWORD"] = settings.password
    result = subprocess.run(
        [executable, *arguments],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=180,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
    )
    if result.returncode != 0:
        raise LegacySourceError("POSTGRES_BACKUP_COMMAND_FAILED")


def _pg_tool(name: str) -> str:
    variable = f"AZURPILOT_{name.upper()}"
    configured = os.environ.get(variable)
    executable = configured or shutil.which(name)
    if not executable or not Path(executable).is_file():
        raise LegacySourceError("POSTGRES_BACKUP_TOOL_UNAVAILABLE")
    return executable


def _dump_restore(
    settings: DatabaseSettings,
    scratch_database: str,
    dump_path: Path,
) -> DatabaseSettings:
    dump = _pg_tool("pg_dump")
    restore = _pg_tool("pg_restore")
    common = [
        "--host",
        settings.host,
        "--port",
        str(settings.port),
        "--username",
        settings.user,
    ]
    _run_pg(
        dump,
        [
            *common,
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--file",
            str(dump_path),
            settings.database,
        ],
        settings,
    )
    _run_pg(restore, ["--list", str(dump_path)], settings)
    _run_pg(
        restore,
        [
            *common,
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            "--dbname",
            scratch_database,
            str(dump_path),
        ],
        settings,
    )
    return replace(settings, database=scratch_database)


def _write_report(payload: str, path: Path | None) -> None:
    if path is None:
        print(payload, end="")
        return
    parent = path.parent.resolve(strict=True)
    if path.exists() or path.is_symlink() or not parent.is_dir():
        raise LegacySourceError("REPORT_TARGET_UNSAFE")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)


def _inspection_payload(reader: LegacySourceReader) -> str:
    plan = reader.capture()
    payload = {
        "format": "azurpilot-postgresql-source-inspection-v1",
        "manifest_digest": plan.manifest_digest,
        "sources": [
            {
                "logical_id": item.logical_id,
                "source_kind": item.source_kind,
                "size": item.size,
                "sha256": item.sha256,
                "schema_fingerprint": item.schema_fingerprint,
                "integrity": item.integrity,
            }
            for item in plan.manifest
        ],
        "timezone_policy": plan.timezone_policy,
        "dataset_counts": dict(plan.dataset_counts()),
        "identity_groups": len(plan.identities),
        "unresolved_identities": sum(
            item.evidence.value == "unresolved" for item in plan.identities
        ),
        "quarantined_records": sum(
            item.disposition is RecordDisposition.QUARANTINE for item in plan.records
        ),
        "derived_csv_parity": plan.derived_csv_parity,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline migration legacy SQLite/CL1 в PostgreSQL schema v1."
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--legacy-timezone", required=True)
    parser.add_argument("--report", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inspect")
    for command in ("import", "reconcile"):
        child = subparsers.add_parser(command)
        child.add_argument("--chunk-size", type=int, default=500)
    full = subparsers.add_parser("full-rehearsal")
    full.add_argument("--chunk-size", type=int, default=500)
    full.add_argument("--scratch-database", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        source_root = arguments.source_root.resolve(strict=True)
        if arguments.command == "inspect":
            _write_report(
                _inspection_payload(_reader(source_root, arguments.legacy_timezone)),
                arguments.report,
            )
            print("STATUS:INSPECTED")
            return 0

        settings = DatabaseSettings.from_environment()
        if arguments.command == "full-rehearsal":
            _require_disposable(settings, arguments.scratch_database)
            with tempfile.TemporaryDirectory(prefix="azurpilot-stage3-") as temporary:
                temp_root = Path(temporary).resolve(strict=True)
                snapshot = temp_root / "snapshot"
                snapshot.mkdir()
                create_consistent_snapshot(source_root, snapshot)
                reader = _reader(snapshot, arguments.legacy_timezone)
                primary_target = _target(settings)
                try:
                    service = MigrationService(reader, primary_target)
                    first = service.run(chunk_size=arguments.chunk_size)
                    repeat = service.run(chunk_size=arguments.chunk_size)
                finally:
                    primary_target.dispose()
                restored_settings = _dump_restore(
                    settings,
                    arguments.scratch_database,
                    temp_root / "migration.dump",
                )
                restored_target = _target(restored_settings)
                try:
                    restored = MigrationService(reader, restored_target).run(
                        chunk_size=arguments.chunk_size
                    )
                finally:
                    restored_target.dispose()
                report = finalize_rehearsal(first, repeat, restored)
        else:
            migration_target = _target(settings)
            try:
                report = MigrationService(
                    _reader(source_root, arguments.legacy_timezone),
                    migration_target,
                ).run(chunk_size=arguments.chunk_size)
            finally:
                migration_target.dispose()
        _write_report(report.to_json(), arguments.report)
        if report.cutover_ready:
            print("STATUS:READY")
        else:
            print("STATUS:NOT_READY:" + ",".join(report.reason_codes))
        return 0 if report.cutover_ready else 4
    except LegacySourceError as exc:
        print(f"ERROR:{exc}")
        return 2
    except StorageError as exc:
        print(f"ERROR:{exc.code}")
        return 3
    except OSError:
        print("ERROR:FILESYSTEM_OPERATION_FAILED")
        return 2
    except ValueError:
        print("ERROR:ARGUMENT_INVALID")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
