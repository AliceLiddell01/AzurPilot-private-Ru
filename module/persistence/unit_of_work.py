"""Явная короткая транзакция поверх одного SQLAlchemy Connection."""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy import Connection
from sqlalchemy.engine import Transaction
from sqlalchemy.exc import DBAPIError

from module.application.errors import StorageError
from module.persistence.database import LazyEngine, translate_database_error
from module.persistence.repositories import (
    PostgresImportLedgerRepository,
    PostgresInstanceIdentityRepository,
    PostgresStatisticsRepository,
)


class PostgresUnitOfWork:
    def __init__(self, engine: LazyEngine):
        self._engine = engine
        self._connection: Connection | None = None
        self._transaction: Transaction | None = None
        self.instances: PostgresInstanceIdentityRepository
        self.statistics: PostgresStatisticsRepository
        self.imports: PostgresImportLedgerRepository

    def __enter__(self) -> Self:
        if self._connection is not None:
            raise RuntimeError("Unit of Work уже открыт.")
        connection: Connection | None = None
        try:
            connection = self._engine.get().connect()
            transaction = connection.begin()
        except DBAPIError as exc:
            if connection is not None:
                try:
                    connection.close()
                except DBAPIError:
                    pass
            raise translate_database_error(exc) from None
        self._connection = connection
        self._transaction = transaction
        self.instances = PostgresInstanceIdentityRepository(self._connection)
        self.statistics = PostgresStatisticsRepository(self._connection)
        self.imports = PostgresImportLedgerRepository(self._connection)
        return self

    def commit(self) -> None:
        if self._transaction is None:
            raise RuntimeError("Unit of Work не открыт.")
        try:
            self._transaction.commit()
        except DBAPIError as exc:
            raise translate_database_error(exc) from None
        self._transaction = None

    def rollback(self) -> None:
        if self._transaction is not None:
            try:
                self._transaction.rollback()
            except DBAPIError as exc:
                raise translate_database_error(exc) from None
            finally:
                self._transaction = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        cleanup_error: StorageError | None = None
        try:
            if self._transaction is not None:
                try:
                    self.rollback()
                except StorageError as error:
                    cleanup_error = error
        finally:
            if self._connection is not None:
                try:
                    self._connection.close()
                except DBAPIError as error:
                    cleanup_error = translate_database_error(error)
                self._connection = None
        if exc_type is None and cleanup_error is not None:
            raise cleanup_error
