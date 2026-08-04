"""Персональные ограничения OCR-настроек в WebUI."""

from __future__ import annotations

from typing import Any

from module.ocr.model_policy import should_hide_personal_ocr_argument


class PersonalOcrSettingsMixin:
    """Скрывает параметры моделей, отсутствующих в EN/Global-форке."""

    def set_group(
        self,
        group,
        arg_dict,
        config: dict[str, Any],
        task: str,
    ) -> int:
        group_name = group[0]
        if isinstance(arg_dict, dict):
            arg_dict = {
                argument: definition
                for argument, definition in arg_dict.items()
                if not should_hide_personal_ocr_argument(
                    task=task,
                    group=group_name,
                    argument=argument,
                )
            }
        return super().set_group(group, arg_dict, config, task)
