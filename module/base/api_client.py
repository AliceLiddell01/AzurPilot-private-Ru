"""Совместимый локальный shim для удалённого API AzurPilot.

В персональной сборке проектные сетевые endpoints не используются. Модуль временно
сохранён только для совместимости со старыми импортами до их полного удаления.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class ApiClient:
    """Не выполняет сетевые запросы к инфраструктуре исходного проекта."""

    @classmethod
    def get_announcement(
        cls,
        timeout: int = 1,
        current_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Совместимый no-op: удалённые объявления в персональной сборке отключены."""
        _ = timeout, current_id
        return None
