from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class TestSharedHandlersBaseRuntimeMessages:
    def test_representative_runtime_messages_are_russian(self):
        expected = {
            "module/base/api_client.py": (
                "[База — API] Попытка использовать",
                "Не удалось получить объявление",
                "Не удалось разобрать JSON объявления",
            ),
            "module/base/async_executor.py": (
                "Исключение в цикле событий AsyncExecutor",
                "Истекло время ожидания завершения задач",
            ),
            "module/base/base.py": (
                "[Базовый класс] Получен неизвестный объект конфигурации",
                "Создание пула фоновых потоков",
            ),
            "module/base/decorator.py": (
                "нет подходящего варианта",
                "Вызов отброшен",
            ),
            "module/base/device_id.py": (
                "Обнаружено изменение ID устройства для миграции",
                "ID устройства инициализирован",
            ),
            "module/base/filter.py": (
                "Некорректный фильтр",
            ),
            "module/base/resource.py": (
                "Отображение ресурсов",
            ),
            "module/base/retry.py": (
                "повторная попытка через",
            ),
            "module/base/ssh.py": (
                "Исполняемый файл SSH не найден",
                "Не удалось очистить отпечаток SSH-хоста",
            ),
            "module/handler/ambush.py": (
                "[Карта — засада] Обнаружена засада",
                "Уклонение от засады",
            ),
            "module/handler/auto_search.py": (
                "[Обработчик — автопоиск] Нет активной боковой панели флота",
                "Неизвестная настройка автопоиска",
            ),
            "module/handler/enemy_searching.py": (
                "[Обработчик — поиск] Возврат на страницу этапа",
                "Истекло время ожидания анимации поиска противника",
            ),
            "module/handler/fast_forward.py": (
                "Настройка автопоиска",
                "Не удалось применить настройки автопоиска.",
                "Ускорение перемещения",
            ),
            "module/handler/info_handler.py": (
                "[Обработчик — комиссии] Получена срочная комиссия",
                "Количество вариантов сюжета",
            ),
            "module/handler/login.py": (
                "[Вход] Вход выполнен успешно",
                "[Перезапуск] Попытка перезапуска приложения",
            ),
            "module/handler/mystery.py": (
                "Таинственная клетка",
                "Получена поддержка авианосца",
            ),
            "module/handler/strategy.py": (
                "[Стратегия — панель] Открытие панели стратегии",
                "Вход в режим воздушного удара",
            ),
        }
        for path, messages in expected.items():
            text = source(path)
            for message in messages:
                assert message in text, (path, message)

    def test_live_first_party_cjk_false_negatives_are_removed(self):
        api = source("module/base/api_client.py")
        fast_forward = source("module/handler/fast_forward.py")
        assert 'domain_type = "主域名"' not in api
        assert 'domain_type = "备用域名"' not in api
        assert "task_stop('无法确保自动搜索设置。')" not in fast_forward

    def test_api_raw_contract_is_preserved(self):
        text = source("module/base/api_client.py")
        required = (
            'PRIMARY_DOMAIN = "https://alas-apiv2.nanoda.work"',
            'FALLBACK_DOMAIN = "https://alas-apiv2.nanoda.work"',
            'ANNOUNCEMENT_PATH = "/api/get/announcement"',
            "ANNOUNCEMENT_CHECK_INTERVAL = 90",
            "requests.get(",
            "params=params",
            "timeout=timeout",
            'headers={"User-Agent": "alas AzurPilot"}',
            "success_codes=[200, 304]",
            'data.get("announcementId")',
            'data.get("title")',
            'data.get("content")',
            'data.get("url")',
            "response.text",
            "str(exc)",
        )
        for token in required:
            assert token in text, token

    def test_ssh_raw_contract_is_preserved(self):
        text = source("module/base/ssh.py")
        required = (
            '[ssh_executable, "-G", "-p", str(port), host]',
            '["ssh-keygen", "-R", target, "-f", str(known_hosts)]',
            "result.stdout.splitlines()",
            "result.stderr.strip()",
            "result.returncode == 0",
            "result.returncode != 1",
        )
        for token in required:
            assert token in text, token

    def test_retry_decorator_filter_and_resource_contracts_are_preserved(self):
        retry = source("module/base/retry.py")
        for token in (
            "except exceptions as e:",
            "_tries -= 1",
            "raise e",
            "time.sleep(_delay)",
            "_delay *= backoff",
            "random.uniform(*jitter)",
            "_delay = min(_delay, max_delay)",
        ):
            assert token in retry, token

        decorator = source("module/base/decorator.py")
        for token in (
            "record['options'] == data['options']",
            "all(flag)",
            "return record['func'](self, *args, **kwargs)",
            "return func(self, *args, **kwargs)",
        ):
            assert token in decorator, token

        filt = source("module/base/filter.py")
        for token in (
            "re.sub(r'[ \\t\\r\\n]', '', string)",
            "re.sub(r'[＞﹥›˃ᐳ❯]', '>', string)",
            "self.filter_raw = string.split('>')",
            "return ['1nVa1d'] + [None] * (len(self.attr) - 1)",
        ):
            assert token in filt, token

        resource = source("module/base/resource.py")
        for token in (
            "Resource.instances[key] = self",
            "obj.resource_release()",
            "del_cached_property(ASSETS, attr)",
            "gc.collect(2)",
        ):
            assert token in resource, token

    def test_auto_search_and_fast_forward_identifiers_are_preserved(self):
        auto = source("module/handler/auto_search.py")
        for token in (
            "fleet1_mob_fleet2_boss",
            "fleet1_boss_fleet2_mob",
            "fleet1_all_fleet2_standby",
            "fleet1_standby_fleet2_all",
            "sub_auto_call",
            "sub_standby",
            "AUTO_SEARCH_SETTINGS",
            "dic_setting_name_to_index",
            "dic_setting_index_to_name",
        ):
            assert token in auto, token

        fast = source("module/handler/fast_forward.py")
        for token in (
            "Switch('Fast_Forward'",
            "Switch('Fleet_Lock'",
            "Switch('Auto_Search'",
            "add_state('on'",
            "add_state('off'",
            "current='unknown'",
            "if current == 'unknown':",
            "GemsFarming.Scheduler.Enable",
            "100_percent_clear",
            "map_3_stars",
            "threat_safe_without_3_stars",
            "threat_safe",
        ):
            assert token in fast, token

    def test_login_restart_and_recognition_contracts_are_preserved(self):
        text = source("module/handler/login.py")
        for token in (
            "RESTART_TRIES = 4",
            "FIRST_TRY_WAIT_SECONDS = 30",
            "SUBSEQUENT_TRY_WAIT_SECONDS = 20",
            "self.device.app_stop()",
            "self.device.app_start()",
            "self.device.app_is_running()",
            '//*[@text="登录"]',
            '//*[@content-desc="登录"]',
            '//*[@text="同意"]',
            '//*[@text="隐私政策"]',
            '//*[@text="用户协议"]',
            '//*[@text="请滑动阅读协议内容"]',
        ):
            assert token in text, token

    def test_strategy_state_identifiers_are_preserved(self):
        text = source("module/handler/strategy.py")
        for token in (
            "add_state('line_ahead'",
            "add_state('double_line'",
            "add_state('diamond'",
            "SUBMARINE_HUNT.set('on' if sub_hunt else 'off'",
            "SUBMARINE_VIEW.set('on' if sub_view else 'off'",
            "buff = 'unknown'",
            "return buff",
        ):
            assert token in text, token
