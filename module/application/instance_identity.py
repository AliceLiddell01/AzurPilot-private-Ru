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

    digest, identity_id = runtime_instance_identity(instance)
    identity = uow.instances.resolve(
        alias_kind="legacy_instance",
        alias_digest=digest,
    )
    if identity is None:
        identity = InstanceIdentity(identity_id, instance)
        uow.instances.register(
            identity,
            alias_kind="legacy_instance",
            alias_digest=digest,
            source_provenance="runtime_exact_profile",
        )
    elif identity.id != identity_id:
        raise StorageConfigurationError(
            "Идентификатор экземпляра не совпадает с происхождением миграции."
        )
    return identity.id
