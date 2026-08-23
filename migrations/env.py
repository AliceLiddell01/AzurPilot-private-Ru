"""Alembic environment без production runtime side effects."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from module.persistence.config import DatabaseSettings
from module.persistence.schema import SCHEMA_NAME, metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = metadata


def _is_downgrade_command() -> bool:
    command = getattr(getattr(config, "cmd_opts", None), "cmd", ())
    return bool(command and getattr(command[0], "__name__", None) == "downgrade")


def _require_confirmed_disposable_target(settings: DatabaseSettings) -> None:
    if os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE") != "1":
        raise RuntimeError("Для разрушительного downgrade требуется disposable opt-in.")
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
    if any(not value for value in expected.values()) or expected != actual:
        raise RuntimeError(
            "Для разрушительного downgrade требуется точное подтверждение "
            "test-only target."
        )


def _settings() -> DatabaseSettings:
    settings = DatabaseSettings.from_environment()
    if _is_downgrade_command():
        _require_confirmed_disposable_target(settings)
    return settings


def _include_name(
    name: str | None, type_: str, parent_names: dict[str, str | None]
) -> bool:
    if type_ == "schema":
        return name in {None, SCHEMA_NAME}
    return not (type_ == "table" and parent_names.get("schema_name") != SCHEMA_NAME)


def run_migrations_offline() -> None:
    settings = _settings()
    context.configure(
        url=settings.sqlalchemy_url().render_as_string(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=_include_name,
        compare_type=True,
        version_table="alembic_version",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    settings = _settings()
    connectable = create_engine(
        settings.sqlalchemy_url(),
        poolclass=pool.NullPool,
        connect_args=settings.connect_args(),
    )
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_schemas=True,
                include_name=_include_name,
                compare_type=True,
                version_table="alembic_version",
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
