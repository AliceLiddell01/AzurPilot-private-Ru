"""Ленивый per-process Engine и fail-closed health/error boundary."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import cast

from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool

from module.application.errors import (
    IncompatibleSchemaError,
    StorageAuthenticationError,
    StorageConflictError,
    StorageInvalidDataError,
    StorageUnavailableError,
)
from module.application.storage_models import StorageHealth, StorageHealthState
from module.persistence.config import DatabaseSettings
from module.persistence.schema import EXPECTED_ALEMBIC_HEAD

_SCHEMA_VALUE_ERRORS = (TypeError, ValueError)


def translate_database_error(exc: BaseException) -> Exception:
    """Преобразует DBAPI exception без включения SQL/DSN/credentials."""

    sqlstate = getattr(getattr(exc, "orig", exc), "sqlstate", None)
    original = getattr(exc, "orig", exc)
    diagnostic = str(original).casefold()
    authentication_markers = (
        "password authentication failed",
        "no password supplied",
        "authentication method 10 not supported",
    )
    if sqlstate in {"28P01", "28000"} or any(
        marker in diagnostic for marker in authentication_markers
    ):
        return StorageAuthenticationError("PostgreSQL отклонил аутентификацию.")
    if sqlstate == "23505":
        return StorageConflictError("PostgreSQL отклонил конфликтующую запись.")
    if isinstance(sqlstate, str) and sqlstate.startswith(("22", "23")):
        return StorageInvalidDataError("PostgreSQL отклонил некорректные данные.")
    if isinstance(sqlstate, str) and sqlstate.startswith("42"):
        return IncompatibleSchemaError("PostgreSQL schema несовместима.")
    return StorageUnavailableError("PostgreSQL временно недоступен.")


class LazyEngine:
    """Создаёт один bounded QueuePool на PID только при первом запросе."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        engine_factory: Callable[..., Engine] = create_engine,
        pid_reader: Callable[[], int] = os.getpid,
    ):
        self._settings = settings
        self._engine_factory = engine_factory
        self._pid_reader = pid_reader
        self._lock = threading.Lock()
        self._engine: Engine | None = None
        self._pid: int | None = None

    @property
    def owner_pid(self) -> int | None:
        return self._pid

    def get(self) -> Engine:
        pid = self._pid_reader()
        with self._lock:
            engine = self._engine
            if engine is not None and self._pid == pid:
                return engine
            if self._engine is not None and self._pid != pid:
                # В дочернем процессе не закрываем соединения родительского PID.
                self._engine.dispose(close=False)
                self._engine = None
            if self._engine is None:
                pool = self._settings.pool
                self._engine = self._engine_factory(
                    self._settings.sqlalchemy_url(),
                    poolclass=QueuePool,
                    pool_size=pool.size,
                    max_overflow=pool.max_overflow,
                    pool_timeout=pool.timeout_seconds,
                    pool_pre_ping=True,
                    connect_args=self._settings.connect_args(),
                )
                self._pid = pid
            return self._engine

    def dispose(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None
                self._pid = None

    def __getstate__(self) -> dict[str, object]:
        return {
            "settings": self._settings,
            "engine_factory": self._engine_factory,
            "pid_reader": self._pid_reader,
        }

    def __setstate__(self, state: dict[str, object]) -> None:
        self._settings = cast(DatabaseSettings, state["settings"])
        self._engine_factory = cast(Callable[..., Engine], state["engine_factory"])
        self._pid_reader = cast(Callable[[], int], state["pid_reader"])
        self._lock = threading.Lock()
        self._engine = None
        self._pid = None


class StorageHealthChecker:
    def __init__(
        self,
        engine: LazyEngine,
        *,
        expected_head: str = EXPECTED_ALEMBIC_HEAD,
    ):
        self._engine = engine
        self._expected_head = expected_head

    def check(self) -> StorageHealth:
        try:
            with self._engine.get().connect() as connection:
                connection.execute(select(1)).scalar_one()
                version = int(
                    connection.execute(text("SHOW server_version_num")).scalar_one()
                )
                if version < 180000 or version >= 190000:
                    return StorageHealth(StorageHealthState.INCOMPATIBLE_SCHEMA)
                heads = tuple(
                    connection.execute(
                        text(
                            "SELECT version_num FROM alembic_version "
                            "ORDER BY version_num"
                        )
                    ).scalars()
                )
        except SQLAlchemyError as exc:
            mapped = translate_database_error(exc)
            if isinstance(mapped, StorageAuthenticationError):
                state = StorageHealthState.AUTHENTICATION_FAILED
            elif isinstance(mapped, IncompatibleSchemaError):
                state = StorageHealthState.INCOMPATIBLE_SCHEMA
            else:
                state = StorageHealthState.UNAVAILABLE
            return StorageHealth(state)
        except _SCHEMA_VALUE_ERRORS:
            return StorageHealth(StorageHealthState.INCOMPATIBLE_SCHEMA)
        if heads != (self._expected_head,):
            return StorageHealth(
                StorageHealthState.INCOMPATIBLE_SCHEMA,
                schema_head=heads[0] if len(heads) == 1 else None,
            )
        return StorageHealth(StorageHealthState.READY, schema_head=heads[0])

    def require_ready(self) -> None:
        health = self.check()
        if health.state is StorageHealthState.READY:
            return
        if health.state is StorageHealthState.AUTHENTICATION_FAILED:
            raise StorageAuthenticationError("PostgreSQL отклонил аутентификацию.")
        if health.state is StorageHealthState.UNAVAILABLE:
            raise StorageUnavailableError("PostgreSQL временно недоступен.")
        raise IncompatibleSchemaError(
            "PostgreSQL schema не совпадает с ожидаемым head."
        )
