"""Фиксированная developer-only диагностика PostgreSQL поверх существующих Engine/UoW."""

from __future__ import annotations

import re
from collections.abc import Callable

from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

from module.application.database_diagnostics import (
    DatabaseCheckDescriptor,
    DatabaseCheckResult,
    DatabaseCheckStatus,
    DatabaseStatusSnapshot,
)
from module.application.errors import (
    IncompatibleSchemaError,
    StorageAuthenticationError,
    StorageConfigurationError,
    StorageUnavailableError,
)
from module.application.instance_identity import runtime_instance_identity
from module.application.storage_models import StorageHealthState
from module.persistence.database import (
    LazyEngine,
    StorageHealthChecker,
    translate_database_error,
)
from module.persistence.schema import EXPECTED_ALEMBIC_HEAD, SCHEMA_NAME, metadata
from module.persistence.unit_of_work import PostgresUnitOfWork

_APP_ROLE = "azurpilot_app"
SCHEMA_MARKER_VERSION = 1
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUIRED_TABLES = tuple(sorted(table.name for table in metadata.sorted_tables))
_REQUIRED_TABLES_SQL = ", ".join(f":table_{index}" for index in range(len(_REQUIRED_TABLES)))

_DESCRIPTORS = (
    DatabaseCheckDescriptor("backend_marker", "Проверить готовность canonical backend marker", target_scoped=False),
    DatabaseCheckDescriptor("connectivity", "Проверить доступность PostgreSQL и ожидаемую версию сервера"),
    DatabaseCheckDescriptor("app_role", "Проверить использование разрешённой app-роли"),
    DatabaseCheckDescriptor("schema_head", "Сверить текущий Alembic head с ожидаемой схемой"),
    DatabaseCheckDescriptor("schema_marker", "Проверить версию schema marker"),
    DatabaseCheckDescriptor("target_resolution", "Разрешить configured target через app_instance alias"),
    DatabaseCheckDescriptor("required_tables", "Проверить фиксированный набор обязательных domain tables"),
    DatabaseCheckDescriptor("domain_consistency", "Проверить отсутствие orphan instance aliases"),
    DatabaseCheckDescriptor("transaction", "Проверить короткую read-only транзакцию с rollback"),
    DatabaseCheckDescriptor("config_match", "Проверить совпадение marker и локального PostgreSQL contract", target_scoped=False),
)
_DESCRIPTORS_BY_ID = {item.check_id: item for item in _DESCRIPTORS}


def _failure(
    check_id: str,
    exc: BaseException,
) -> DatabaseCheckResult:
    if isinstance(exc, StorageAuthenticationError):
        return DatabaseCheckResult(
            check_id,
            DatabaseCheckStatus.FAIL,
            "DEV_DATABASE_AUTHENTICATION_FAILED",
            "PostgreSQL отклонил app-аутентификацию",
        )
    if isinstance(exc, IncompatibleSchemaError):
        return DatabaseCheckResult(
            check_id,
            DatabaseCheckStatus.FAIL,
            "DEV_DATABASE_SCHEMA_INCOMPATIBLE",
            "PostgreSQL schema несовместима с текущим контрактом",
        )
    if isinstance(exc, StorageConfigurationError):
        return DatabaseCheckResult(
            check_id,
            DatabaseCheckStatus.FAIL,
            "DEV_DATABASE_CONFIGURATION_INVALID",
            "Конфигурация PostgreSQL не прошла структурную проверку",
        )
    if isinstance(exc, StorageUnavailableError):
        return DatabaseCheckResult(
            check_id,
            DatabaseCheckStatus.UNAVAILABLE,
            "DEV_DATABASE_UNAVAILABLE",
            "PostgreSQL недоступен для диагностической проверки",
        )
    return DatabaseCheckResult(
        check_id,
        DatabaseCheckStatus.UNAVAILABLE,
        "DEV_DATABASE_CHECK_UNAVAILABLE",
        "Диагностическая проверка PostgreSQL не завершилась",
    )


class PostgresDatabaseDiagnostics:
    """Фиксированный catalog без SQL, table или column names от вызывающего кода."""

    def __init__(
        self,
        engine: LazyEngine | None,
        *,
        marker_ready: bool,
        schema_marker_version: int | None,
        config_match: bool,
    ) -> None:
        self._engine = engine
        self._marker_ready = marker_ready
        self._schema_marker_version = schema_marker_version
        self._config_match = config_match

    def list_checks(self) -> tuple[DatabaseCheckDescriptor, ...]:
        return _DESCRIPTORS

    def run_check(self, check_id: str, target_profile: str) -> DatabaseCheckResult:
        if not isinstance(check_id, str) or check_id not in _DESCRIPTORS_BY_ID:
            raise ValueError("Неизвестный database diagnostic check")
        if not isinstance(target_profile, str) or not _SAFE_TARGET.fullmatch(target_profile):
            raise ValueError("target_profile имеет недопустимый формат")
        handlers: dict[str, Callable[[str], DatabaseCheckResult]] = {
            "backend_marker": lambda _target: self._marker_check(),
            "connectivity": lambda _target: self._connection_check("connectivity", self._connectivity),
            "app_role": lambda _target: self._connection_check("app_role", self._app_role),
            "schema_head": lambda _target: self._connection_check("schema_head", self._schema_head),
            "schema_marker": lambda _target: self._schema_marker_check(),
            "target_resolution": lambda target: self._target_resolution(target),
            "required_tables": lambda _target: self._connection_check("required_tables", self._required_tables),
            "domain_consistency": lambda _target: self._connection_check("domain_consistency", self._domain_consistency),
            "transaction": lambda _target: self._connection_check("transaction", self._transaction),
            "config_match": lambda _target: self._config_match_check(),
        }
        return handlers[check_id](target_profile)

    def get_status(self, target_profile: str) -> DatabaseStatusSnapshot:
        checks = tuple(
            self.run_check(descriptor.check_id, target_profile)
            for descriptor in _DESCRIPTORS
        )
        by_id = {item.check_id: item for item in checks}
        schema_head = by_id["schema_head"].observed
        current_schema_head = schema_head if isinstance(schema_head, str) else None
        domain = by_id["domain_consistency"]
        domain_consistency = domain.observed if isinstance(domain.observed, bool) else None
        return DatabaseStatusSnapshot(
            target_profile=target_profile,
            marker_ready=by_id["backend_marker"].status is DatabaseCheckStatus.PASS,
            connectivity=by_id["connectivity"].status is DatabaseCheckStatus.PASS,
            app_role_ready=by_id["app_role"].status is DatabaseCheckStatus.PASS,
            expected_schema_head=EXPECTED_ALEMBIC_HEAD,
            current_schema_head=current_schema_head,
            schema_marker_version=self._schema_marker_version,
            target_resolved=by_id["target_resolution"].status is DatabaseCheckStatus.PASS,
            required_tables_ready=by_id["required_tables"].status is DatabaseCheckStatus.PASS,
            domain_consistency=domain_consistency,
            transaction_ready=by_id["transaction"].status is DatabaseCheckStatus.PASS,
            config_match=by_id["config_match"].status is DatabaseCheckStatus.PASS,
            checks=checks,
        )

    def _marker_check(self) -> DatabaseCheckResult:
        if self._marker_ready:
            return DatabaseCheckResult(
                "backend_marker",
                DatabaseCheckStatus.PASS,
                "DEV_DATABASE_MARKER_READY",
                "Canonical backend marker соответствует текущему schema head",
                True,
            )
        return DatabaseCheckResult(
            "backend_marker",
            DatabaseCheckStatus.FAIL,
            "DEV_DATABASE_MARKER_INVALID",
            "Canonical backend marker отсутствует или не соответствует текущей схеме",
            False,
        )

    def _schema_marker_check(self) -> DatabaseCheckResult:
        if self._schema_marker_version == SCHEMA_MARKER_VERSION:
            return DatabaseCheckResult(
                "schema_marker",
                DatabaseCheckStatus.PASS,
                "DEV_DATABASE_SCHEMA_MARKER_READY",
                "Версия schema marker поддерживается",
                self._schema_marker_version,
            )
        return DatabaseCheckResult(
            "schema_marker",
            DatabaseCheckStatus.FAIL,
            "DEV_DATABASE_SCHEMA_MARKER_INVALID",
            "Версия schema marker не подтверждена",
            self._schema_marker_version,
        )

    def _config_match_check(self) -> DatabaseCheckResult:
        return DatabaseCheckResult(
            "config_match",
            DatabaseCheckStatus.PASS if self._config_match else DatabaseCheckStatus.FAIL,
            "DEV_DATABASE_CONFIG_MATCH" if self._config_match else "DEV_DATABASE_CONFIG_MISMATCH",
            "Marker и локальный PostgreSQL contract согласованы" if self._config_match else "Marker и локальный PostgreSQL contract не согласованы",
            self._config_match,
        )

    def _connection_check(
        self,
        check_id: str,
        operation: Callable[[Connection], DatabaseCheckResult],
    ) -> DatabaseCheckResult:
        if self._engine is None:
            return DatabaseCheckResult(
                check_id,
                DatabaseCheckStatus.UNAVAILABLE,
                "DEV_DATABASE_CONNECTION_UNAVAILABLE",
                "Диагностический engine не собран из canonical config",
            )
        try:
            with self._engine.get().connect() as connection:
                return operation(connection)
        except SQLAlchemyError as exc:
            return _failure(check_id, translate_database_error(exc))
        except (StorageAuthenticationError, IncompatibleSchemaError, StorageConfigurationError, StorageUnavailableError) as exc:
            return _failure(check_id, exc)
        except Exception as exc:  # noqa: BLE001 — developer diagnostics завершаются fail-closed.
            return _failure(check_id, exc)

    @staticmethod
    def _connectivity(connection: Connection) -> DatabaseCheckResult:
        health = StorageHealthChecker(
            _ConnectionEngineAdapter(connection),
        ).check()
        # StorageHealthChecker обычно получает LazyEngine; этот адаптер — узкий
        # проверяемый мост, повторно использующий существующую health-семантику.
        if health.state is StorageHealthState.READY:
            return DatabaseCheckResult(
                "connectivity",
                DatabaseCheckStatus.PASS,
                "DEV_DATABASE_CONNECTED",
                "PostgreSQL доступен и сервер/schema health подтверждены",
                True,
            )
        if health.state is StorageHealthState.AUTHENTICATION_FAILED:
            return DatabaseCheckResult(
                "connectivity",
                DatabaseCheckStatus.FAIL,
                "DEV_DATABASE_AUTHENTICATION_FAILED",
                "PostgreSQL отклонил app-аутентификацию",
                False,
            )
        if health.state is StorageHealthState.INCOMPATIBLE_SCHEMA:
            return DatabaseCheckResult(
                "connectivity",
                DatabaseCheckStatus.FAIL,
                "DEV_DATABASE_SCHEMA_INCOMPATIBLE",
                "PostgreSQL доступен, но schema health не подтверждён",
                False,
            )
        return DatabaseCheckResult(
            "connectivity",
            DatabaseCheckStatus.UNAVAILABLE,
            "DEV_DATABASE_UNAVAILABLE",
            "PostgreSQL недоступен",
            False,
        )

    @staticmethod
    def _app_role(connection: Connection) -> DatabaseCheckResult:
        current_user = connection.execute(text("SELECT current_user")).scalar_one_or_none()
        matches = current_user == _APP_ROLE
        return DatabaseCheckResult(
            "app_role",
            DatabaseCheckStatus.PASS if matches else DatabaseCheckStatus.FAIL,
            "DEV_DATABASE_APP_ROLE_READY" if matches else "DEV_DATABASE_APP_ROLE_MISMATCH",
            "Используется разрешённая app-роль" if matches else "Текущее подключение не использует разрешённую app-роль",
            matches,
        )

    @staticmethod
    def _schema_head(connection: Connection) -> DatabaseCheckResult:
        # Alembic сохраняет свой version table в текущем schema contract так
        # же, как StorageHealthChecker. Не подставлять schema/table из запроса.
        heads = tuple(
            value
            for value in connection.execute(
                text(
                    "SELECT version_num FROM alembic_version "
                    "ORDER BY version_num LIMIT 2"
                )
            ).scalars().all()
        )
        if heads == (EXPECTED_ALEMBIC_HEAD,):
            return DatabaseCheckResult(
                "schema_head",
                DatabaseCheckStatus.PASS,
                "DEV_DATABASE_SCHEMA_HEAD_READY",
                "Alembic current совпадает с ожидаемым head",
                heads[0],
            )
        observed = heads[0] if len(heads) == 1 and isinstance(heads[0], str) else None
        return DatabaseCheckResult(
            "schema_head",
            DatabaseCheckStatus.FAIL,
            "DEV_DATABASE_SCHEMA_HEAD_MISMATCH",
            "Alembic current не совпадает с ожидаемым head",
            observed,
        )

    @staticmethod
    def _required_tables(connection: Connection) -> DatabaseCheckResult:
        values = {f"table_{index}": name for index, name in enumerate(_REQUIRED_TABLES)}
        rows = connection.execute(
            text(
                f"SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = :schema_name AND table_name IN ({_REQUIRED_TABLES_SQL})"
            ),
            {"schema_name": SCHEMA_NAME, **values},
        ).scalars()
        present = {row for row in rows if isinstance(row, str)}
        ready = present == set(_REQUIRED_TABLES)
        return DatabaseCheckResult(
            "required_tables",
            DatabaseCheckStatus.PASS if ready else DatabaseCheckStatus.FAIL,
            "DEV_DATABASE_TABLES_READY" if ready else "DEV_DATABASE_TABLES_MISSING",
            "Обязательные domain tables присутствуют" if ready else "Обязательный набор domain tables неполон",
            ready,
        )

    @staticmethod
    def _domain_consistency(connection: Connection) -> DatabaseCheckResult:
        orphan = connection.execute(
            text(
                f"SELECT 1 FROM {SCHEMA_NAME}.legacy_instance_alias alias "
                f"LEFT JOIN {SCHEMA_NAME}.app_instance instance "
                "ON instance.id = alias.instance_id "
                "WHERE instance.id IS NULL LIMIT 1"
            )
        ).scalar_one_or_none()
        consistent = orphan is None
        return DatabaseCheckResult(
            "domain_consistency",
            DatabaseCheckStatus.PASS if consistent else DatabaseCheckStatus.FAIL,
            "DEV_DATABASE_DOMAIN_CONSISTENT" if consistent else "DEV_DATABASE_DOMAIN_INCONSISTENT",
            "Связи app_instance и aliases согласованы" if consistent else "Обнаружены несогласованные instance aliases",
            consistent,
        )

    @staticmethod
    def _transaction(connection: Connection) -> DatabaseCheckResult:
        transaction = connection.begin()
        try:
            connection.execute(text("SELECT 1")).scalar_one()
        finally:
            transaction.rollback()
        return DatabaseCheckResult(
            "transaction",
            DatabaseCheckStatus.PASS,
            "DEV_DATABASE_TRANSACTION_READY",
            "Короткая диагностическая транзакция подтверждена и откатана",
            True,
        )

    def _target_resolution(self, target_profile: str) -> DatabaseCheckResult:
        if self._engine is None:
            return DatabaseCheckResult(
                "target_resolution",
                DatabaseCheckStatus.UNAVAILABLE,
                "DEV_DATABASE_CONNECTION_UNAVAILABLE",
                "Нельзя разрешить target без диагностического engine",
            )
        try:
            digest, expected_id = runtime_instance_identity(target_profile)
            with PostgresUnitOfWork(self._engine) as uow:
                identity = uow.instances.resolve(
                    alias_kind="legacy_instance",
                    alias_digest=digest,
                )
            resolved = identity is not None and identity.id == expected_id
            return DatabaseCheckResult(
                "target_resolution",
                DatabaseCheckStatus.PASS if resolved else DatabaseCheckStatus.FAIL,
                "DEV_DATABASE_TARGET_RESOLVED" if resolved else "DEV_DATABASE_TARGET_UNRESOLVED",
                "Configured development target разрешён в app_instance" if resolved else "Configured development target отсутствует в app_instance",
                resolved,
            )
        except SQLAlchemyError as exc:
            return _failure("target_resolution", translate_database_error(exc))
        except Exception as exc:  # noqa: BLE001 — детали persistence наружу не выдаются.
            return _failure("target_resolution", exc)


class _ConnectionEngineAdapter:
    """Адаптировать уже открытую connection к StorageHealthChecker без I/O."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def get(self) -> _ConnectionEngineAdapter:
        return self

    def connect(self) -> _ConnectionContext:
        return _ConnectionContext(self._connection)


class _ConnectionContext:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __enter__(self) -> Connection:
        return self._connection

    def __exit__(self, *_args: object) -> None:
        return None


__all__ = ["PostgresDatabaseDiagnostics"]
