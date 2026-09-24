"""Совместимость импортов старого модуля реестра worker."""

import sys

from module.application import runtime_worker_registry

sys.modules[__name__] = runtime_worker_registry
