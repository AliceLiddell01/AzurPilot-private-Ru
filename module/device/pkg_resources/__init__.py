"""Минимальный совместимый слой ``pkg_resources`` для старого device stack."""

import importlib.metadata
import os
import sys

from module.logger import logger


class FakeDistributionObject:
    def __init__(self, dist, version):
        self.dist = dist
        self.version = version

    def __str__(self):
        return f"{self.__class__.__name__}({self.dist}={self.version})"

    __repr__ = __str__


class DistributionNotFound(Exception):
    """Совместимое исключение отсутствующей package metadata."""


# Зарегистрировать совместимый модуль до импорта adbutils и uiautomator2.
try:
    sys.modules["pkg_resources"] = sys.modules[__name__]
except KeyError:
    logger.error(
        "[Устройство — совместимость] Не удалось зарегистрировать совместимый "
        "модуль pkg_resources"
    )


def resource_filename(*args):
    """Вернуть путь к resource, который запрашивает adbutils."""
    if args != ("adbutils", "binaries"):
        return None

    try:
        distribution = importlib.metadata.distribution(args[0])
    except importlib.metadata.PackageNotFoundError:
        return None
    return os.fspath(distribution.locate_file(os.path.join(*args)))


def get_distribution(dist):
    """Вернуть metadata установленной device-библиотеки."""
    if dist not in {"adbutils", "uiautomator2"}:
        return None

    try:
        version = importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError as exc:
        raise DistributionNotFound(dist) from exc
    return FakeDistributionObject(dist=dist, version=version)
