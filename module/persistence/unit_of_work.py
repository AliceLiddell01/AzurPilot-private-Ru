"""Явная короткая транзакция поверх одного SQLAlchemy Connection."""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Self

from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from module.application.errors import StorageError
from module.persistence.database import LazyEngine, translate_database_error
from module.persistence.fleet_state_repositories import PostgresFleetStateRepository
from module.persistence.repositories import (
    PostgresImportLedgerRepository,
    PostgresInstanceIdentityRepository,
    PostgresStatisticsRepository,
)
from module.persistence.runtime_repositories import PostgresRuntimeStatisticsRepository

logger = logging.getLogger(__name__)


class PostgresUnitOfWork:
    def __init__(self, engine: LazyEngine):
        self._engine = engine
        self._connection: Connection | None = None
        self.instances: PostgresInstanceIdentityRepository
        self.statistics: PostgresStatisticsRepository
        self.imports: PostgresImportLedgerRepository
        self.runtime: PostgresRuntimeStatisticsRepository
        self.fleet_state: PostgresFleetStateRepository

    def __enter__(self) -> Self:
        if self._connection is not None:
            raise RuntimeError("Unit of Work уже открыт.")
        connection: Connection | None = None
        try:
            connection = self._engine.get().connect()
            connection.begin()
            self._connection = connection
            self.instances = PostgresInstanceIdentityRepository(connection)
            self.statistics = PostgresStatisticsRepository(connection)
            self.imports = PostgresImportLedgerRepository(connection)
            self.runtime = PostgresRuntimeStatisticsRepository(connection)
            self.fleet_state = PostgresFleetStateRepository(connection)
        except SQLAlchemyError as exc:
            self._connection = None
            self._clear_repositories()
            self._close_quietly(connection)
            raise translate_database_error(exc) from None
        except BaseException:
            self._connection = None
            self._clear_repositories()
            self._close_quietly(connection)
            raise
        return self

    def _clear_repositories(self) -> None:
        for attribute in (
            "instances",
            "statistics",
            "imports",
            "runtime",
            "fleet_state",
        ):
            self.__dict__.pop(attribute, None)

    @staticmethod
    def _close_quietly(connection: Connection | None) -> None:
        if connection is None:
            return
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - исходная ошибка остаётся главной.
            logger.warning("Не удалось закрыть PostgreSQL connection при очистке.")

    def commit(self) -> None:
        connection = self._connection
        if connection is None or not connection.in_transaction():
            raise RuntimeError("Unit of Work не содержит активной транзакции.")
        try:
            connection.commit()
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def rollback(self) -> None:
        # Вызов без активной транзакции — no-op для безопасного использования из __exit__.
        connection = self._connection
        if connection is not None and connection.in_transaction():
            try:
                connection.rollback()
            except SQLAlchemyError as exc:
                raise translate_database_error(exc) from None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        cleanup_error: StorageError | None = None
        try:
            if self._connection is not None and self._connection.in_transaction():
                try:
                    self.rollback()
                except StorageError as error:
                    if cleanup_error is None:
                        cleanup_error = error
        finally:
            if self._connection is not None:
                try:
                    self._connection.close()
                except SQLAlchemyError as error:
                    if cleanup_error is None:
                        cleanup_error = translate_database_error(error)
                except Exception as error:  # noqa: BLE001 - storage boundary sanitizes cleanup.
                    if cleanup_error is None:
                        cleanup_error = translate_database_error(error)
                finally:
                    self._connection = None
                    self._clear_repositories()
        if exc_type is not None and cleanup_error is not None:
            logger.warning(
                "Ошибка очистки PostgreSQL Unit of Work подавлена исходным исключением."
            )
        elif cleanup_error is not None:
            raise cleanup_error
