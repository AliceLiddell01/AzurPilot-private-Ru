"""Dev Runtime facade к одному уже существующему WebUI owner."""

from __future__ import annotations

import math
from pathlib import Path

from module.application.runtime_control import (
    RuntimeControlOperation,
    RuntimeControlResult,
    RuntimeOwnerIdentity,
    SharedWebUIBootstrapper,
    WebUIControlClient,
)
from module.application.runtime_state import RuntimeStateStore
from module.dev_runtime.target import DevTargetError


class SharedWebUIRuntime:
    """Только typed calls и read-only ownership checks, без второго WebUI."""

    def __init__(
        self,
        repository_root: Path | str,
        *,
        control_client: WebUIControlClient | None = None,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.state = RuntimeStateStore(self.repository_root)
        self._control_client = control_client
        self._profile_name: str | None = None

    @property
    def profile_name(self) -> str:
        if self._profile_name is None:
            from module.dev_runtime.target import DevTargetRegistry

            self._profile_name = DevTargetRegistry.load(self.repository_root).profile_name
        return self._profile_name

    @property
    def log_file(self) -> Path:
        # ``set_file_logger`` использует стабильный профильный путь; Evidence
        # должен читать именно его, а не создавать искусственную границу с
        # датой, которая может не совпадать с журналом worker.
        try:
            profile_name = self.profile_name
        except DevTargetError as exc:
            raise RuntimeError(
                "Нельзя определить log target общего WebUI из-за ошибки development target registry"
            ) from exc
        return self.repository_root / "log" / f"{profile_name}.txt"

    def ensure_webui(self) -> RuntimeOwnerIdentity:
        return self._client().ensure_owner()

    def start_profile(self, *, session_id: str, idempotency_key: str | None = None) -> RuntimeControlResult:
        return self._client().call(
            RuntimeControlOperation.START_PROFILE,
            self.profile_name,
            session_id=session_id,
            idempotency_key=idempotency_key,
        )

    def stop_profile(self, *, session_id: str, idempotency_key: str | None = None) -> RuntimeControlResult:
        return self._client().call(
            RuntimeControlOperation.STOP_PROFILE,
            self.profile_name,
            session_id=session_id,
            idempotency_key=idempotency_key,
        )

    def owner_identity(self) -> RuntimeOwnerIdentity | None:
        raw = self._owner_reader()
        if raw is None:
            return None
        try:
            return RuntimeOwnerIdentity.from_value(raw)
        except Exception:  # noqa: BLE001 - invalid owner is unavailable.
            return None

    def matches_session(self, session_id: str, profile: str | None = None) -> bool:
        profile = profile or self.profile_name
        owner = self.owner_identity()
        if owner is None or not self._owner_matches(owner):
            return False
        record = self._worker_record(profile)
        if record is None or not self._process_matches(record):
            return False
        return self._session_state_matches(profile, session_id, record)

    def worker_present(self, profile: str | None = None) -> bool | None:
        """Проверить наличие worker без изменения registry или ProcessManager."""

        profile = profile or self.profile_name
        record = self._worker_record(profile)
        if record is None:
            snapshot = self.state.read(profile)
            if snapshot is not None and snapshot.phase.value != "stopped":
                return None
            return False
        try:
            from module.webui.worker_registry import process_matches

            return process_matches(record) is True
        except RuntimeError:
            return None
        except Exception:  # noqa: BLE001 - ошибка проверки identity переводит путь в fail-closed режим.
            return None

    def ready(self, profile: str | None = None, session_id: str | None = None) -> tuple[bool, str]:
        profile = profile or self.profile_name
        owner = self.owner_identity()
        if owner is None:
            return False, "общий WebUI owner не зарегистрирован"
        if not self._owner_matches(owner):
            return False, "идентичность общего WebUI owner не подтверждена"
        record = self._worker_record(profile)
        if record is None:
            return False, "worker development target не зарегистрирован"
        if not self._process_matches(record):
            return False, "worker development target не подтверждён"
        if session_id is not None and not self._session_state_matches(profile, session_id, record):
            return False, "worker не принадлежит текущей DevSession"
        return True, "общий WebUI и worker development target готовы"

    def _session_state_matches(
        self,
        profile: str,
        session_id: str,
        record: dict,
    ) -> bool:
        snapshot = self.state.read(profile)
        return (
            snapshot is not None
            and snapshot.freshness == "fresh"
            and snapshot.session_id == session_id
            and snapshot.worker_running is True
            and self._worker_identity_matches(snapshot, record)
        )

    def _client(self) -> WebUIControlClient:
        if self._control_client is None:
            self._control_client = WebUIControlClient(
                self.repository_root,
                owner_reader=self._owner_reader,
                owner_matches=self._owner_matches,
                bootstrapper=SharedWebUIBootstrapper(
                    self.repository_root,
                    owner_reader=self._owner_reader,
                    owner_matches=self._owner_matches,
                ),
            )
        return self._control_client

    @staticmethod
    def _owner_reader() -> RuntimeOwnerIdentity | None:
        from module.webui.worker_registry import get_owner_record_read_only

        record = get_owner_record_read_only()
        return None if record is None else RuntimeOwnerIdentity.from_value(record)

    @staticmethod
    def _owner_matches(owner: RuntimeOwnerIdentity) -> bool:
        from module.webui.worker_registry import process_matches

        try:
            return process_matches(owner.as_dict()) is True
        except RuntimeError:
            return False

    @staticmethod
    def _worker_record(profile: str) -> dict | None:
        from module.webui.worker_registry import get_worker_read_only

        record = get_worker_read_only(profile)
        return record if isinstance(record, dict) else None

    @staticmethod
    def _process_matches(record: dict) -> bool:
        from module.webui.worker_registry import process_matches

        try:
            return process_matches(record) is True
        except RuntimeError:
            return False

    @staticmethod
    def _worker_identity_matches(snapshot: object, record: dict) -> bool:
        worker_pid = getattr(snapshot, "worker_pid", None)
        worker_created_at = getattr(snapshot, "worker_created_at", None)
        record_pid = record.get("pid")
        record_created_at = record.get("created_at")
        if (
            isinstance(record_pid, bool)
            or not isinstance(record_pid, int)
            or isinstance(record_created_at, bool)
            or not isinstance(record_created_at, (int, float))
            or not math.isfinite(float(record_created_at))
            or float(record_created_at) <= 0
            or isinstance(worker_pid, bool)
            or not isinstance(worker_pid, int)
            or worker_pid <= 0
            or isinstance(worker_created_at, bool)
            or not isinstance(worker_created_at, (int, float))
            or not math.isfinite(float(worker_created_at))
            or float(worker_created_at) <= 0
        ):
            return False
        return (
            worker_pid == record_pid
            and float(worker_created_at) == float(record_created_at)
        )


__all__ = ["SharedWebUIRuntime"]
