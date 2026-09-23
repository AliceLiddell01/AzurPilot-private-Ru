"""Ограниченное хранилище спецификаций Smoke и конечных результатов."""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ValidationError

from module.dev_runtime.bounded_io import BoundedReadTooLarge, read_bounded_bytes
from module.dev_runtime.contracts import DevEnvironment
from module.dev_runtime.smoke import (
    _LEGACY_SMOKE_SCHEMA_VERSIONS,
    _SAFE_ID,
    SMOKE_MAX_RUN_AGE_SECONDS,
    SMOKE_MAX_RUN_BYTES,
    SMOKE_MAX_RUNS,
    SMOKE_MAX_SPEC_BYTES,
    SMOKE_SCHEMA_VERSION,
    SMOKE_STATE_SCHEMA_VERSION,
    SmokeControl,
    SmokeResult,
    SmokeRunRecord,
    SmokeSourceSnapshot,
    SmokeSpec,
    SmokeState,
    SmokeStoreError,
    _canonical_payload_hash,
    _drop_legacy_file_log_assertions,
    _identifier,
    _normalize_legacy_spec_payload,
    _safe_model_json,
    _validate_json_model,
)
from module.dev_runtime.target import target_identity
from module.dev_runtime.task_sandbox import (
    TaskSandboxError,
    _atomic_json_write,
    _ensure_scoped_path,
    _exclusive_policy_lock,
    _is_reparse_point,
)


class SmokeStateStore:
    """Атомарное хранилище состояния с отдельными файлами `spec.json`, `result.json` и `control.json`."""

    def __init__(self, environment: DevEnvironment, *, now: Callable[[], datetime] | None = None) -> None:
        self.environment = environment
        self.now = now or (lambda: datetime.now(UTC))
        self.root = _ensure_scoped_path(
            environment.repository_root / "config" / "state" / "dev-runtime-smoke",
            environment.repository_root,
            label="корень состояния SmokeRun",
        )
        self.lock_path = _ensure_scoped_path(
            environment.repository_root / "config" / "state" / "dev-runtime-smoke.lock",
            environment.repository_root,
            label="блокировка состояния SmokeRun",
        )
        self._thread_lock = threading.RLock()

    def _run_dir(self, smoke_id: str) -> Path:
        if not _SAFE_ID.fullmatch(smoke_id):
            raise SmokeStoreError("DEV_SMOKE_ID_INVALID", "smoke_id имеет недопустимый формат")
        return _ensure_scoped_path(self.root / smoke_id, self.environment.repository_root, label="каталог SmokeRun")

    def _file(self, smoke_id: str, name: str) -> Path:
        directory = self._run_dir(smoke_id)
        if name not in {"spec.json", "state.json", "result.json", "control.json"}:
            raise SmokeStoreError("DEV_SMOKE_STATE_INVALID", "неизвестный файл состояния")
        return _ensure_scoped_path(directory / name, self.environment.repository_root, label="файл состояния SmokeRun")

    @contextmanager
    def _locked(self):
        self.root.parent.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self.root) or _is_reparse_point(self.lock_path):
            raise SmokeStoreError("DEV_SMOKE_STATE_UNSAFE_PATH", "состояние SmokeRun не должно быть ссылкой или точкой соединения")
        with self._thread_lock:
            try:
                with _exclusive_policy_lock(self.lock_path):
                    yield
            except TaskSandboxError as exc:
                raise SmokeStoreError(exc.code, str(exc)) from exc

    @staticmethod
    def _read_json(path: Path, maximum: int) -> object:
        try:
            if _is_reparse_point(path):
                raise SmokeStoreError("DEV_SMOKE_STATE_UNSAFE_PATH", "файл состояния SmokeRun не должен быть ссылкой")
            data = read_bounded_bytes(path, max_bytes=maximum)
        except FileNotFoundError as exc:
            raise SmokeStoreError("DEV_SMOKE_STATE_MISSING", "файл состояния SmokeRun отсутствует") from exc
        except BoundedReadTooLarge as exc:
            raise SmokeStoreError("DEV_SMOKE_STATE_TOO_LARGE", "файл состояния SmokeRun превышает ограничение") from exc
        except OSError as exc:
            raise SmokeStoreError("DEV_SMOKE_STATE_UNREADABLE", "файл состояния SmokeRun невозможно прочитать") from exc
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise SmokeStoreError("DEV_SMOKE_STATE_CORRUPT", "файл состояния SmokeRun содержит некорректный JSON") from exc

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, object]) -> None:
        try:
            _atomic_json_write(path, payload)
        except TaskSandboxError as exc:
            raise SmokeStoreError(exc.code, str(exc)) from exc

    @staticmethod
    def _versioned_payload(
        payload: object,
        *,
        current_version: int,
        corrupt_code: str,
        unsupported_code: str,
        label: str,
    ) -> tuple[Mapping[str, object], bool]:
        if not isinstance(payload, Mapping) or type(payload.get("schema_version")) is not int:
            raise SmokeStoreError(corrupt_code, f"{label} не содержит целочисленную schema_version")
        version = payload["schema_version"]
        if version == current_version:
            return payload, False
        if version in _LEGACY_SMOKE_SCHEMA_VERSIONS:
            return payload, True
        if version > current_version:
            raise SmokeStoreError(
                unsupported_code,
                f"{label} содержит неизвестную будущую schema_version",
            )
        raise SmokeStoreError(corrupt_code, f"{label} содержит неподдерживаемую schema_version")

    @staticmethod
    def _is_legacy(record: SmokeRunRecord) -> bool:
        return record._legacy_schema_version is not None

    @staticmethod
    def _reject_legacy_mutation(record: SmokeRunRecord) -> None:
        if record._legacy_schema_version is not None:
            raise SmokeStoreError(
                "DEV_SMOKE_LEGACY_IMMUTABLE",
                "Исторический SmokeRun доступен только для bounded read/migration",
            )

    def _load_result_unlocked(
        self,
        smoke_id: str,
        *,
        record: SmokeRunRecord | None = None,
    ) -> SmokeResult | None:
        path = self._file(smoke_id, "result.json")
        if not os.path.lexists(path):
            return None
        loaded_record = record or self._load_unlocked(smoke_id)
        raw = self._read_json(path, SMOKE_MAX_RUN_BYTES)
        payload, legacy = self._versioned_payload(
            raw,
            current_version=SMOKE_STATE_SCHEMA_VERSION,
            corrupt_code="DEV_SMOKE_RESULT_CORRUPT",
            unsupported_code="DEV_SMOKE_RESULT_UNSUPPORTED",
            label="SmokeResult",
        )
        normalized = (
            _drop_legacy_file_log_assertions(payload)
            if legacy
            else payload
        )
        try:
            result = _validate_json_model(SmokeResult, normalized)
        except ValidationError as exc:
            raise SmokeStoreError("DEV_SMOKE_RESULT_CORRUPT", "SmokeResult имеет некорректную схему") from exc
        if not isinstance(result, SmokeResult):
            raise SmokeStoreError("DEV_SMOKE_RESULT_CORRUPT", "SmokeResult имеет некорректный тип")
        if legacy:
            object.__setattr__(result, "_legacy_schema_version", raw.get("schema_version"))
        if not self._result_matches_record(result, loaded_record):
            raise SmokeStoreError("DEV_SMOKE_RESULT_MISMATCH", "SmokeResult не соответствует замороженному SmokeRun")
        return result

    def create(self, spec: SmokeSpec, source: SmokeSourceSnapshot, *, created_at: str, deadline_at: str, smoke_id: str | None = None) -> SmokeRunRecord:
        smoke_id = smoke_id or str(uuid.uuid4())
        smoke_id = _identifier(smoke_id, field_name="smoke_id")
        record = SmokeRunRecord(
            smoke_id=smoke_id,
            state=SmokeState.CREATED,
            spec_hash=spec.spec_hash(),
            source=source,
            created_at=created_at,
            deadline_at=deadline_at,
            target_profile=self.environment.profile_name,
            target_identity=target_identity(self.environment.dev_target),
        )
        with self._locked():
            self.root.mkdir(parents=True, exist_ok=True)
            if any(
                record.state
                in {
                    SmokeState.CREATED,
                    SmokeState.PREPARING,
                    SmokeState.RUNNING,
                    SmokeState.EVALUATING,
                    SmokeState.CLEANING_UP,
                    SmokeState.AWAITING_EXTERNAL_EVALUATION,
                }
                and not self._is_legacy(record)
                for record in self.list_records_unlocked()
            ):
                raise SmokeStoreError("DEV_SMOKE_ACTIVE_CONFLICT", "Активный SmokeRun уже существует")
            run_dir = self._run_dir(smoke_id)
            if os.path.lexists(run_dir):
                raise SmokeStoreError("DEV_SMOKE_ID_CONFLICT", "SmokeRun с таким smoke_id уже существует")
            run_dir.mkdir()
            self._write_json(self._file(smoke_id, "spec.json"), spec.canonical_dict())
            self._write_json(self._file(smoke_id, "state.json"), _safe_model_json(record))
            self._write_json(self._file(smoke_id, "control.json"), _safe_model_json(SmokeControl()))
        return record

    def _load_unlocked(self, smoke_id: str) -> SmokeRunRecord:
        raw = self._read_json(self._file(smoke_id, "state.json"), SMOKE_MAX_RUN_BYTES)
        payload, legacy = self._versioned_payload(
            raw,
            current_version=SMOKE_STATE_SCHEMA_VERSION,
            corrupt_code="DEV_SMOKE_STATE_CORRUPT",
            unsupported_code="DEV_SMOKE_STATE_UNSUPPORTED",
            label="состояние SmokeRun",
        )
        normalized = _drop_legacy_file_log_assertions(payload) if legacy else payload
        try:
            record = _validate_json_model(SmokeRunRecord, normalized)
        except ValidationError as exc:
            raise SmokeStoreError("DEV_SMOKE_STATE_CORRUPT", "состояние SmokeRun имеет некорректную схему") from exc
        if not isinstance(record, SmokeRunRecord):
            raise SmokeStoreError("DEV_SMOKE_STATE_CORRUPT", "состояние SmokeRun имеет некорректный тип")
        if legacy:
            object.__setattr__(record, "_legacy_schema_version", raw.get("schema_version"))
        return record

    def load(self, smoke_id: str) -> SmokeRunRecord:
        with self._locked():
            return self._load_unlocked(smoke_id)

    def load_spec(self, smoke_id: str) -> SmokeSpec:
        with self._locked():
            raw = self._read_json(self._file(smoke_id, "spec.json"), SMOKE_MAX_SPEC_BYTES)
            payload, legacy = self._versioned_payload(
                raw,
                current_version=SMOKE_SCHEMA_VERSION,
                corrupt_code="DEV_SMOKE_SPEC_CORRUPT",
                unsupported_code="DEV_SMOKE_SPEC_UNSUPPORTED",
                label="SmokeSpec",
            )
            normalized = _normalize_legacy_spec_payload(payload) if legacy else payload
            try:
                spec = _validate_json_model(SmokeSpec, normalized)
            except ValidationError as exc:
                raise SmokeStoreError("DEV_SMOKE_SPEC_CORRUPT", "SmokeSpec имеет некорректную схему") from exc
            if not isinstance(spec, SmokeSpec):
                raise SmokeStoreError("DEV_SMOKE_SPEC_CORRUPT", "SmokeSpec имеет некорректный тип")
            record = self._load_unlocked(smoke_id)
            raw_hash = _canonical_payload_hash(payload)
            expected_hash = raw_hash if legacy else spec.spec_hash()
            if expected_hash != record.spec_hash:
                raise SmokeStoreError("DEV_SMOKE_SPEC_HASH_MISMATCH", "spec_hash не соответствует замороженной спецификации")
            if legacy:
                object.__setattr__(spec, "_legacy_schema_version", payload.get("schema_version"))
                object.__setattr__(spec, "_legacy_spec_hash", raw_hash)
            return spec

    @staticmethod
    def _next_state_allowed(current: SmokeState, target: SmokeState) -> bool:
        allowed = {
            SmokeState.CREATED: {SmokeState.PREPARING, SmokeState.FINISHED},
            SmokeState.PREPARING: {SmokeState.RUNNING, SmokeState.EVALUATING, SmokeState.CLEANING_UP, SmokeState.FINISHED},
            SmokeState.RUNNING: {SmokeState.EVALUATING, SmokeState.CLEANING_UP, SmokeState.FINISHED},
            SmokeState.EVALUATING: {SmokeState.CLEANING_UP, SmokeState.FINISHED},
            SmokeState.CLEANING_UP: {SmokeState.AWAITING_EXTERNAL_EVALUATION, SmokeState.FINISHED},
            SmokeState.AWAITING_EXTERNAL_EVALUATION: {SmokeState.FINISHED},
            SmokeState.FINISHED: set(),
        }
        return target == current or target in allowed[current]

    def update(self, smoke_id: str, updates: Mapping[str, object]) -> SmokeRunRecord:
        with self._locked():
            current = self._load_unlocked(smoke_id)
            self._reject_legacy_mutation(current)
            if current.state is SmokeState.FINISHED and updates:
                raise SmokeStoreError("DEV_SMOKE_STATE_IMMUTABLE", "завершённый SmokeRun нельзя изменять")
            updated = self._updated_unlocked(current, updates)
            self._write_json(self._file(smoke_id, "state.json"), _safe_model_json(updated))
            return updated

    def _updated_unlocked(self, current: SmokeRunRecord, updates: Mapping[str, object]) -> SmokeRunRecord:
        payload = _safe_model_json(current)
        for key, value in updates.items():
            if key not in payload:
                raise SmokeStoreError("DEV_SMOKE_STATE_INVALID", "неизвестное поле состояния SmokeRun")
            if isinstance(value, BaseModel):
                payload[key] = _safe_model_json(value)
            elif isinstance(value, (list, tuple)):
                payload[key] = [_safe_model_json(item) if isinstance(item, BaseModel) else item for item in value]
            else:
                payload[key] = value
        try:
            updated = _validate_json_model(SmokeRunRecord, payload)
        except ValidationError as exc:
            raise SmokeStoreError("DEV_SMOKE_STATE_INVALID", "обновление SmokeRun нарушает строгую схему") from exc
        if not isinstance(updated, SmokeRunRecord):
            raise SmokeStoreError("DEV_SMOKE_STATE_INVALID", "обновление SmokeRun имеет неверный тип")
        if not self._next_state_allowed(current.state, updated.state):
            raise SmokeStoreError("DEV_SMOKE_STATE_TRANSITION_INVALID", "переход состояния SmokeRun запрещён")
        immutable = (
            "smoke_id",
            "spec_hash",
            "source",
            "created_at",
            "deadline_at",
            "target_profile",
            "target_identity",
        )
        if any(getattr(current, key) != getattr(updated, key) for key in immutable):
            raise SmokeStoreError("DEV_SMOKE_STATE_IMMUTABLE", "замороженные поля SmokeRun нельзя изменить")
        return updated

    @staticmethod
    def _result_matches_record(result: SmokeResult, record: SmokeRunRecord) -> bool:
        return (
            result.smoke_id == record.smoke_id
            and result.spec_hash == record.spec_hash
            and result.source == record.source
            and result.session_id == record.session_id
            and result.target_profile == record.target_profile
            and result.target_identity == record.target_identity
            and result.outcome == record.outcome
            and result.product_execution_outcome == record.product_execution_outcome
            and result.evidence_completeness == record.evidence_completeness
            and result.harness_runtime_outcome == record.harness_runtime_outcome
            and result.operator_intervention_outcome == record.operator_intervention_outcome
            and result.finished_at == record.finished_at
            and result.assertions == record.assertions
            and result.cleanup == record.cleanup
            and result.primary_failure == record.primary_failure
            and result.harness_failure == record.harness_failure
            and result.external_verdict == record.external_verdict
        )

    def finish(self, smoke_id: str, updates: Mapping[str, object], result: SmokeResult) -> SmokeRunRecord:
        """Атомарно подготовить неизменяемый результат и зафиксировать завершённое состояние."""

        with self._locked():
            current = self._load_unlocked(smoke_id)
            self._reject_legacy_mutation(current)
            if current.state is SmokeState.FINISHED:
                raise SmokeStoreError("DEV_SMOKE_STATE_IMMUTABLE", "завершённый SmokeRun нельзя изменять")
            updated = self._updated_unlocked(current, updates)
            if updated.state is not SmokeState.FINISHED or not self._result_matches_record(result, updated):
                raise SmokeStoreError("DEV_SMOKE_RESULT_MISMATCH", "SmokeResult не соответствует финальному SmokeRun")
            result_path = self._file(smoke_id, "result.json")
            result_payload = _safe_model_json(result)
            if os.path.lexists(result_path):
                existing = self._read_json(result_path, SMOKE_MAX_RUN_BYTES)
                if existing != result_payload:
                    raise SmokeStoreError("DEV_SMOKE_RESULT_IMMUTABLE", "SmokeResult уже существует и неизменяем")
            else:
                # Результат записывается первым: при сбое записи состояния запуск остаётся незавершённым
                # и может быть безопасно восстановлен, но не выдаётся за PASS.
                self._write_json(result_path, result_payload)
            self._write_json(self._file(smoke_id, "state.json"), _safe_model_json(updated))
            return updated

    def save_result(self, result: SmokeResult) -> None:
        with self._locked():
            record = self._load_unlocked(result.smoke_id)
            self._reject_legacy_mutation(record)
            if record.state is not SmokeState.FINISHED:
                raise SmokeStoreError("DEV_SMOKE_RESULT_STATE_INVALID", "результат разрешён только для завершённого SmokeRun")
            if not self._result_matches_record(result, record):
                raise SmokeStoreError("DEV_SMOKE_RESULT_MISMATCH", "SmokeResult не соответствует замороженному SmokeRun")
            path = self._file(result.smoke_id, "result.json")
            if os.path.lexists(path):
                existing = self._read_json(path, SMOKE_MAX_RUN_BYTES)
                if existing != _safe_model_json(result):
                    raise SmokeStoreError("DEV_SMOKE_RESULT_IMMUTABLE", "SmokeResult уже существует и неизменяем")
                return
            self._write_json(path, _safe_model_json(result))

    def load_result(self, smoke_id: str) -> SmokeResult | None:
        with self._locked():
            record = self._load_unlocked(smoke_id)
            return self._load_result_unlocked(smoke_id, record=record)

    def request_cancel(self, smoke_id: str, timestamp: str) -> SmokeControl:
        with self._locked():
            record = self._load_unlocked(smoke_id)
            self._reject_legacy_mutation(record)
            control_path = self._file(smoke_id, "control.json")
            if os.path.lexists(control_path):
                raw = self._read_json(control_path, 32 * 1024)
                try:
                    control = _validate_json_model(SmokeControl, raw)
                except ValidationError as exc:
                    raise SmokeStoreError("DEV_SMOKE_CONTROL_CORRUPT", "данные отмены SmokeRun повреждены") from exc
            else:
                control = SmokeControl()
            if not control.cancel_requested:
                control = SmokeControl(cancel_requested=True, requested_at=timestamp)
                self._write_json(control_path, _safe_model_json(control))
            return control

    def is_cancel_requested(self, smoke_id: str) -> bool:
        with self._locked():
            path = self._file(smoke_id, "control.json")
            if not os.path.lexists(path):
                return False
            raw = self._read_json(path, 32 * 1024)
            try:
                return _validate_json_model(SmokeControl, raw).cancel_requested
            except ValidationError as exc:
                raise SmokeStoreError("DEV_SMOKE_CONTROL_CORRUPT", "данные отмены SmokeRun повреждены") from exc

    def list_records(self) -> list[SmokeRunRecord]:
        with self._locked():
            if not os.path.lexists(self.root):
                return []
            if _is_reparse_point(self.root):
                raise SmokeStoreError("DEV_SMOKE_STATE_UNSAFE_PATH", "корень состояния SmokeRun не должен быть ссылкой")
            records: list[SmokeRunRecord] = []
            try:
                entries = list(os.scandir(self.root))
            except OSError as exc:
                raise SmokeStoreError("DEV_SMOKE_STATE_UNREADABLE", "корень SmokeRun невозможно прочитать") from exc
            if len(entries) > SMOKE_MAX_RUNS * 4:
                raise SmokeStoreError("DEV_SMOKE_RETENTION_INVALID", "корень SmokeRun содержит слишком много каталогов")
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                record = self._load_unlocked(entry.name)
                self._validate_finished_result_unlocked(record)
                records.append(record)
            return sorted(records, key=lambda item: item.created_at)

    def prune(self, *, active_ids: set[str] | None = None, now: datetime | None = None) -> int:
        active_ids = active_ids or set()
        now = now or self.now()
        removed = 0
        with self._locked():
            if not os.path.lexists(self.root):
                return 0
            records = self.list_records_unlocked()
            protected = {
                record.smoke_id
                for record in records
                if not self._is_legacy(record)
                and (
                    record.smoke_id in active_ids
                or record.state in {SmokeState.CREATED, SmokeState.PREPARING, SmokeState.RUNNING, SmokeState.EVALUATING, SmokeState.CLEANING_UP, SmokeState.AWAITING_EXTERNAL_EVALUATION}
                )
            }
            cutoff = now.astimezone(UTC) - timedelta(seconds=SMOKE_MAX_RUN_AGE_SECONDS)
            candidates = []
            completed = [record for record in records if record.smoke_id not in protected]
            overflow = max(0, len(completed) - SMOKE_MAX_RUNS)
            for record in completed:
                try:
                    created = datetime.fromisoformat(record.created_at)
                except ValueError:
                    continue
                if created < cutoff or overflow > 0:
                    candidates.append(record)
                    if overflow > 0:
                        overflow -= 1
            for record in candidates:
                run_dir = self._run_dir(record.smoke_id)
                if _is_reparse_point(run_dir):
                    continue
                try:
                    for child in run_dir.iterdir():
                        if _is_reparse_point(child) or child.is_dir():
                            continue
                        child.unlink(missing_ok=True)
                    run_dir.rmdir()
                except OSError as exc:
                    raise SmokeStoreError(
                        "DEV_SMOKE_RETENTION_CLEANUP_FAILED",
                        "Старый каталог SmokeRun невозможно безопасно удалить",
                    ) from exc
                removed += 1
        return removed

    def list_records_unlocked(self) -> list[SmokeRunRecord]:
        entries = list(os.scandir(self.root)) if os.path.lexists(self.root) else []
        if len(entries) > SMOKE_MAX_RUNS * 4:
            raise SmokeStoreError("DEV_SMOKE_RETENTION_INVALID", "корень SmokeRun содержит слишком много каталогов")
        records: list[SmokeRunRecord] = []
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                record = self._load_unlocked(entry.name)
                self._validate_finished_result_unlocked(record)
                records.append(record)
        return sorted(records, key=lambda item: item.created_at)

    def _validate_finished_result_unlocked(self, record: SmokeRunRecord) -> None:
        if record.state is not SmokeState.FINISHED:
            return
        result = self._load_result_unlocked(record.smoke_id, record=record)
        if result is None:
            raise SmokeStoreError("DEV_SMOKE_RESULT_CORRUPT", "Завершённый SmokeRun не содержит SmokeResult")


__all__ = ["SmokeStateStore"]
