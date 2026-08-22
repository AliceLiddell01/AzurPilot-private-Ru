"""Alembic environment без production runtime side effects."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from module.persistence.config import DatabaseSettings
from module.persistence.schema import SCHEMA_NAME, metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = metadata


def _settings() -> DatabaseSettings:
    return DatabaseSettings.from_environment()


def _include_name(
    name: str | None, type_: str, parent_names: dict[str, str | None]
) -> bool:
    if type_ == "schema":
        return name in {None, SCHEMA_NAME}
    return not (
        type_ == "table" and parent_names.get("schema_name") not in {None, SCHEMA_NAME}
    )


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
