"""Вспомогательный модуль конфигурации MCP.

Обеспечивает структурированный доступ к конфигурационным данным для серверов MCP (Model Context Protocol).
Сервер MCP использует этот модуль для получения списков задач, их деталей и настроек,
что позволяет внешним AI-ассистентам запрашивать и изменять конфигурацию AzurPilot.

Основные функции:
- get_tasks(): получение списка имён всех доступных для планирования задач
- get_task_details(): получение подробных определений параметров задачи (с локализацией)
- get_dashboard_resources(): получение списка ресурсов панели управления

Источники данных конфигурации:
- args.json: объединённые полные определения параметров
- i18n/{lang}.json: файлы локализации
"""

import json
import os
from typing import Dict, Any, List, Optional
from module.config.locale import UI_LOCALE
from module.config.utils import read_file, filepath_args, filepath_i18n


class McpConfigHelper:
    """Помощник доступа к конфигурационным данным MCP.

    Считывает метаданные конфигурации из args.json и файлов i18n,
    предоставляя структурированную информацию о задачах и параметрах для MCP-сервера.

    Attributes:
        lang (str): Код текущего языка, например 'ru-RU'.
        args_data (dict): Данные определений параметров, загруженные из args.json.
        i18n_data (dict): Данные локализации, загруженные из файла i18n.
    """

    def __init__(self, lang=UI_LOCALE):
        if lang != UI_LOCALE:
            raise ValueError(f"Поддерживается только язык интерфейса {UI_LOCALE}.")
        self.lang = UI_LOCALE
        self.args_data = read_file(filepath_args("args"))
        self.i18n_data = read_file(filepath_i18n(UI_LOCALE))

    def get_tasks(self) -> List[str]:
        """Получить имена всех задач из args.json."""
        return list(self.args_data.keys())

    def get_task_details(self, task_name: str) -> Dict[str, Any]:
        """Получить сглаженные метаданные задачи, включая локализованное имя и текст справки."""
        if task_name not in self.args_data:
            return {}

        task_args = self.args_data[task_name]
        task_i18n = self.i18n_data.get("Task", {}).get(task_name, {})
        
        # Структурированные данные для использования AI.
        result = {
            "task_name": task_name,
            "display_name": task_i18n.get("name", task_name),
            "help": task_i18n.get("help", ""),
            "groups": {}
        }

        # Данные локализации параметров обычно находятся на верхнем уровне i18n_data[task_name]
        # или в Task[task_name] (общий дескриптор задачи).
        # AzurPilot организует данные локализации по ключам уровня задачи.
        spec_i18n = self.i18n_data.get(task_name, {})

        for group_name, group_data in task_args.items():
            if group_name == "Storage":  # Пропускаем группу Storage.
                continue
                
            # Разбираем метаданные локализации группы.
            group_meta = spec_i18n.get(group_name, {})
            info = group_meta.get("_info", {})
            group_display = info.get("name", group_name)
            group_help = info.get("help", "")

            group_result = {
                "display_name": group_display,
                "help": group_help,
                "arguments": {}
            }

            for arg_name, arg_meta in group_data.items():
                arg_i18n = spec_i18n.get(group_name, {}).get(arg_name, {})
                
                # Переводы вариантов.
                options = arg_meta.get("option", [])
                translated_options = {}
                for opt in options:
                    translated_options[str(opt)] = arg_i18n.get(str(opt), str(opt))

                group_result["arguments"][arg_name] = {
                    "display_name": arg_i18n.get("name", arg_name),
                    "help": arg_i18n.get("help", ""),
                    "type": arg_meta.get("type", "input"),
                    "default": arg_meta.get("value"),
                    "options": translated_options if translated_options else None
                }
            
            result["groups"][group_name] = group_result

        return result

    def get_dashboard_resources(self, config_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Извлечь информацию о ресурсах из секции Dashboard конфигурационных данных.

        Включает Value, Limit, Total и локализованные имена.
        """
        dashboard = config_data.get("Dashboard", {})
        resources = {}
        
        # Получаем локализованные имена элементов Dashboard.
        # Обычно они находятся в i18n_data["Gui"]["Dashboard"].
        dashboard_i18n = self.i18n_data.get("Gui", {}).get("Dashboard", {})
        
        for key, data in dashboard.items():
            if not isinstance(data, dict) or "Value" not in data:
                continue
                
            # Пытаемся получить понятное отображаемое имя.
            label = dashboard_i18n.get(key, key)
            
            res_item = {
                "label": label,
                "value": data.get("Value"),
            }
            if "Limit" in data:
                res_item["limit"] = data["Limit"]
            if "Total" in data:
                res_item["total"] = data["Total"]
            if "Record" in data:
                res_item["last_update"] = data["Record"]
                
            resources[key] = res_item
            
        return resources
