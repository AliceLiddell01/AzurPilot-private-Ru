"""Read-only API client for AzurPilot announcements.

Project-controlled telemetry and bug-log upload are intentionally absent.
This module performs GET requests only.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from module.logger import logger


class ApiClient:
    """GET-only client with endpoint fallback for public announcements."""

    PRIMARY_DOMAIN = "https://alas-apiv2.nanoda.work"
    FALLBACK_DOMAIN = "https://alas-apiv2.nanoda.work"
    ANNOUNCEMENT_PATH = "/api/get/announcement"
    ANNOUNCEMENT_CHECK_INTERVAL = 90

    @classmethod
    def _get_endpoints(cls, path: str) -> List[str]:
        endpoints = (
            f"{cls.PRIMARY_DOMAIN}{path}",
            f"{cls.FALLBACK_DOMAIN}{path}",
        )
        return list(dict.fromkeys(endpoints))

    @classmethod
    def _get_with_fallback(
        cls,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = 10,
        success_codes: Optional[List[int]] = None,
    ) -> Tuple[bool, int, str]:
        if success_codes is None:
            success_codes = [200]

        last_error: Optional[str] = None
        for index, endpoint in enumerate(cls._get_endpoints(path)):
            domain_type = "основной домен" if index == 0 else "резервный домен"
            try:
                logger.debug(f"[База — API] Попытка использовать {domain_type}: {endpoint}")
                response = requests.get(
                    endpoint,
                    params=params,
                    timeout=timeout,
                    headers={"User-Agent": "alas AzurPilot"},
                )
                if response.status_code in success_codes:
                    if index > 0:
                        logger.info(f"[База — API] Запрос через {domain_type} выполнен успешно")
                    return True, response.status_code, response.text

                logger.warning(
                    f"[База — API] {domain_type} вернул ошибочный статус: {response.status_code}"
                )
                last_error = f"HTTP {response.status_code}"
            except requests.exceptions.Timeout:
                logger.warning(f"[База — API] Истекло время ожидания запроса через {domain_type}")
                last_error = "Timeout"
            except requests.exceptions.RequestException as exc:
                logger.warning(f"[База — API] Ошибка запроса через {domain_type}: {exc}")
                last_error = str(exc)
            except Exception as exc:
                logger.warning(f"[База — API] Исключение при запросе через {domain_type}: {exc}")
                last_error = str(exc)

        return False, 0, last_error or "Unknown error"

    @classmethod
    def get_announcement(
        cls,
        timeout: int = 1,
        current_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            params: Dict[str, Any] = {"t": int(time.time())}
            if current_id is not None:
                params["id"] = current_id

            success, status_code, response_text = cls._get_with_fallback(
                cls.ANNOUNCEMENT_PATH,
                params=params,
                timeout=timeout,
                success_codes=[200, 304],
            )
            if not success:
                logger.warning(f"[База] Не удалось получить объявление: {response_text}")
                return None
            if status_code == 304 or not response_text.strip():
                return None

            try:
                data = json.loads(response_text)
            except json.JSONDecodeError as exc:
                logger.warning(
                    f"[База] Не удалось разобрать JSON объявления: {exc}, ответ={response_text[:100]}"
                )
                return None

            if not data or not data.get("announcementId"):
                logger.info("[База] Данные объявления пусты или не содержат ID")
                return None
            if data.get("title") and (data.get("content") or data.get("url")):
                return data
            return None
        except Exception as exc:
            logger.warning(f"[База] Исключение при получении объявления: {exc}")
            return None
