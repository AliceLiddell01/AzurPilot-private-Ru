"""Единое разрешение runtime-профиля в PostgreSQL app_instance."""

from __future__ import annotations

from hashlib import sha256
from uuid import UUID, uuid5

from module.application.errors import StorageConfigurationError
from module.application.storage_models import InstanceIdentity
from module.application.storage_ports import StorageUnitOfWork

_IDENTITY_NAMESPACE = UUID("bc6db2da-cb91-4d6e-bc33-bb598d715c13")


def runtime_instance_identity(instance: str) -> tuple[str, UUID]:
    if not isinstance(instance, str) or not instance or len(instance) > 128:
        raise StorageConfigurationError("Имя экземпляра хранилища некорректно.")
    digest = sha256(instance.encode("utf-8")).hexdigest()
    return digest, uuid5(_IDENTITY_NAMESPACE, digest)


def resolve_runtime_instance(
    uow: StorageUnitOfWork,
    instance: str,
) -> UUID:
    """Разрешить профиль через действующий app_instance/legacy alias contract."""

    resolved = _resolve_runtime_instance(uow, instance, register_missing=True)
    if resolved is None:
        raise StorageConfigurationError(
            "Идентификатор runtime-профиля не удалось зарегистрировать."
        )
    return resolved


def resolve_existing_runtime_instance(
    uow: StorageUnitOfWork,
    instance: str,
) -> UUID | None:
    """Разрешить уже известный runtime-профиль без регистрации или commit."""

    return _resolve_runtime_instance(uow, instance, register_missing=False)


def _resolve_runtime_instance(
    uow: StorageUnitOfWork,
    instance: str,
    *,
    register_missing: bool,
) -> UUID | None:
    digest, identity_id = runtime_instance_identity(instance)
    identity = uow.instances.resolve(
        alias_kind="legacy_instance",
        alias_digest=digest,
    )
    if identity is None:
        if not register_missing:
            return None
        identity = InstanceIdentity(identity_id, instance)
        uow.instances.register(
            identity,
            alias_kind="legacy_instance",
            alias_digest=digest,
            source_provenance="runtime_exact_profile",
        )
    elif identity.id != identity_id:
        raise StorageConfigurationError(
            "Идентификатор runtime-профиля не совпадает с ожидаемым идентификатором."
        )
    return identity.id
