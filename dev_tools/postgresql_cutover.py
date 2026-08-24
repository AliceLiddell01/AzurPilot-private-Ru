"""Одноразовая guarded-активация production backend после cutover."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from module.application.errors import StorageConfigurationError, StorageError
from module.persistence.config import (
    DEFAULT_BACKEND_MARKER_PATH,
    LEGACY_BACKEND_MARKER_PATH,
    DatabaseSettings,
    migrate_legacy_backend_marker,
)
from module.persistence.database import LazyEngine, StorageHealthChecker
from module.persistence.local_environment import load_local_postgres_environment
from module.persistence.schema import EXPECTED_ALEMBIC_HEAD

CONFIRMATION = "АКТИВИРОВАТЬ-POSTGRESQL-БЕЗ-SQLITE-ROLLBACK"


def _git_revision(value: str) -> str:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise RuntimeError("Git provenance для production-маркера некорректен.")
    return value


def _load_ready_report(path: Path) -> tuple[dict[str, object], str]:
    if path.is_symlink():
        raise RuntimeError("Отчёт reconciliation отсутствует или небезопасен.")
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size > 4_194_304:
        raise RuntimeError("Отчёт reconciliation отсутствует или небезопасен.")
    raw = resolved.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Отчёт reconciliation повреждён.") from exc
    if not isinstance(payload, dict) or payload.get("cutover_ready") is not True:
        raise RuntimeError("Отчёт reconciliation не разрешает cutover.")
    if payload.get("reason_codes") not in ([], ()):
        raise RuntimeError("Отчёт reconciliation содержит блокирующие причины.")
    return payload, hashlib.sha256(raw).hexdigest()


def activate(arguments: argparse.Namespace) -> bool:
    if arguments.confirm != CONFIRMATION:
        raise RuntimeError("Точное подтверждение необратимой активации отсутствует.")
    if str(arguments.host).strip().lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("Production-маркер разрешён только для loopback PostgreSQL.")
    _, manifest_digest = _load_ready_report(Path(arguments.reconciliation_report))
    settings = DatabaseSettings(
        host=arguments.host,
        port=arguments.port,
        database=arguments.database,
        user=arguments.user,
        sslmode=arguments.sslmode,
        runtime_timezone=arguments.runtime_timezone,
    )
    if settings.user != "azurpilot_app":
        raise RuntimeError("Production-маркер разрешён только для app-роли PostgreSQL.")
    engine = LazyEngine(settings)
    try:
        StorageHealthChecker(engine).require_ready()
    finally:
        engine.dispose()

    marker = Path(arguments.marker).resolve()
    if marker.exists() or marker.is_symlink():
        raise RuntimeError("Production-маркер уже существует.")
    legacy = Path(arguments.legacy_marker).resolve() if arguments.legacy_marker else None
    invalid_legacy_digest: str | None = None
    if legacy is not None and legacy.exists():
        try:
            if migrate_legacy_backend_marker(target=marker, legacy=legacy):
                return True
        except StorageConfigurationError:
            if legacy.is_symlink() or not legacy.is_file():
                raise RuntimeError(
                    "Legacy production-маркер отсутствует или небезопасен."
                ) from None
            invalid_legacy_digest = hashlib.sha256(legacy.read_bytes()).hexdigest()
            if arguments.retire_invalid_legacy_marker_sha256 != invalid_legacy_digest:
                raise RuntimeError(
                    "Повреждённый legacy marker требует exact SHA-256 recovery guard."
                ) from None
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "backend": "postgresql",
        "version": 1,
        "alembic_head": EXPECTED_ALEMBIC_HEAD,
        "reconciliation_report_sha256": manifest_digest,
        "reviewed_head": _git_revision(arguments.reviewed_head),
        "merge_commit": _git_revision(arguments.merge_commit),
        "host": settings.host,
        "port": settings.port,
        "database": settings.database,
        "user": settings.user,
        "sslmode": settings.sslmode,
        "runtime_timezone": settings.runtime_timezone,
    }
    temporary = marker.with_suffix(marker.suffix + f".{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, marker)
        if legacy is not None and invalid_legacy_digest is not None:
            if (
                legacy.is_symlink()
                or not legacy.is_file()
                or hashlib.sha256(legacy.read_bytes()).hexdigest()
                != invalid_legacy_digest
            ):
                marker.unlink(missing_ok=True)
                raise RuntimeError(
                    "Повреждённый legacy marker изменился во время recovery."
                )
            try:
                legacy.unlink()
            except OSError:
                marker.unlink(missing_ok=True)
                raise
    finally:
        temporary.unlink(missing_ok=True)
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Одноразовая активация PostgreSQL после полного cutover."
    )
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--reconciliation-report", required=True)
    parser.add_argument("--marker", default=str(DEFAULT_BACKEND_MARKER_PATH))
    parser.add_argument("--legacy-marker", default=str(LEGACY_BACKEND_MARKER_PATH))
    parser.add_argument("--retire-invalid-legacy-marker-sha256")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database", default="azurpilot")
    parser.add_argument("--user", default="azurpilot_app")
    parser.add_argument("--sslmode", default="disable")
    parser.add_argument("--runtime-timezone", default="Asia/Novosibirsk")
    parser.add_argument("--reviewed-head", required=True)
    parser.add_argument("--merge-commit", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        load_local_postgres_environment(role="app")
        migrated = activate(_parser().parse_args(argv))
    except (OSError, RuntimeError, StorageError, ValueError) as exc:
        print(f"Ошибка активации production PostgreSQL: {exc}", file=sys.stderr)
        return 1
    if migrated:
        print("Legacy production-маркер PostgreSQL перенесён атомарно.")
    else:
        print("Production-маркер PostgreSQL создан атомарно.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
