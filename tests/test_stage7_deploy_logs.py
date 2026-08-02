import io
import queue
import re
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import PropertyMock, patch

from deploy import uv
from module.config.time_source import NetworkTimeSource


ROOT = Path(__file__).resolve().parents[1]


class Stage7DeployLogTests(unittest.TestCase):
    def test_deploy_entry_points_have_russian_first_party_context(self):
        expected = {
            "deploy/adb.py": (
                "Чтобы исправить ошибку:",
                "Запуск службы ADB",
                "включите ADB в настройках эмулятора",
            ),
            "deploy/config.py": (
                "Конфигурация запуска",
                "Операция завершилась с ошибкой",
                "Последняя команда:",
            ),
            "deploy/patch.py": (
                "Сначала распакуйте установщик AzurPilot",
                "исправление trust_env не требуется",
            ),
            "deploy/uv.py": (
                "Для подготовки среды Python AzurPilot требуется uv.",
                "Не удалось подготовить среду uv:",
            ),
            "deploy/Windows/config.py": (
                "Конфигурация запуска",
                "Операция завершилась с ошибкой",
            ),
            "deploy/Windows/pip.py": (
                "Обновление зависимостей",
                "Не удалось выполнить uv sync:",
            ),
        }

        for relative_path, messages in expected.items():
            with self.subTest(path=relative_path):
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                for message in messages:
                    self.assertIn(message, source)

    def test_config_infrastructure_diagnostics_are_russian(self):
        expected = {
            "module/config/config.py": (
                "[Конфигурация] Используется шаблон в режиме только для чтения",
                "[Конфигурация] Файл конфигурации не найден.",
                "[Конфигурация] Сохранение",
            ),
            "module/config/config_updater.py": (
                "не связан ни с одной группой аргументов",
                "не является допустимым значением аргумента",
            ),
            "module/config/time_source.py": (
                "Сетевое время синхронизировано:",
                "временно используется системное время:",
            ),
            "module/config/watcher.py": (
                "[Конфигурация: наблюдение]",
            ),
        }

        for relative_path, messages in expected.items():
            with self.subTest(path=relative_path):
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                for message in messages:
                    self.assertIn(message, source)

    def test_uv_startup_failure_preserves_external_exception_text(self):
        external_payload = "external failure: access denied"
        with (
            patch.object(uv, "in_project_venv", return_value=False),
            patch.object(uv, "project_root", return_value=Path(".")),
            patch.object(uv, "sync_project_venv", side_effect=RuntimeError(external_payload)),
            patch.dict(uv.os.environ, {uv.NO_BOOTSTRAP_ENV: ""}, clear=False),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit) as context:
                uv.ensure_uv_environment()

        self.assertEqual(context.exception.code, 1)
        self.assertIn("Не удалось подготовить среду uv:", stderr.getvalue())
        self.assertIn(external_payload, stderr.getvalue())

    def test_unknown_dependency_request_keeps_request_value(self):
        requests = queue.Queue()
        responses = queue.Queue()
        request = "external-request-value"
        requests.put(request)
        requests.put("shutdown")

        uv.dependency_sync_service(requests, responses, root=Path("."))

        response = responses.get_nowait()
        self.assertFalse(response["success"])
        self.assertEqual([], response["command"])
        self.assertEqual("", response["output"])
        self.assertEqual(
            f"Неизвестный запрос синхронизации зависимостей: {request}",
            response["error"],
        )

    def test_ntp_warning_preserves_external_error_detail(self):
        source = NetworkTimeSource()
        source.retry_interval = 0
        external_payload = "external socket failure"

        with (
            patch.object(
                NetworkTimeSource,
                "servers",
                new_callable=PropertyMock,
                return_value=["ntp.example"],
            ),
            patch.object(source, "_query_server", side_effect=OSError(external_payload)),
            patch.object(source, "_log_warning") as log_warning,
        ):
            self.assertFalse(source.refresh(force=True))

        message = log_warning.call_args.args[0]
        self.assertIn("Не удалось синхронизировать время по NTP", message)
        self.assertIn("ntp.example", message)
        self.assertIn(external_payload, message)

    def test_machine_output_and_legacy_app_keyword_are_preserved(self):
        uv_source = (ROOT / "deploy/uv.py").read_text(encoding="utf-8")
        windows_app_source = (ROOT / "deploy/Windows/app.py").read_text(encoding="utf-8")

        self.assertIn('logger.info(f"{prefix} {redact_sensitive_text(line)}")', uv_source)
        self.assertIn(
            "logger.info(f'Обновление app.asar [Update app.asar] {update} -----> {source}')",
            windows_app_source,
        )

    def test_docker_installer_locale_branches_keep_their_structure(self):
        source = (ROOT / "deploy/docker/deploy-image.sh").read_text(encoding="utf-8")
        messages = {}
        for locale, key, template in re.findall(
            r"^\s*(en|zh):([a-z_]+)\) printf '([^']*)'",
            source,
            flags=re.MULTILINE,
        ):
            messages.setdefault(locale, {})[key] = template

        self.assertEqual(38, len(messages["en"]))
        self.assertEqual(list(messages["en"]), list(messages["zh"]))
        for key in messages["en"]:
            self.assertEqual(messages["en"][key].count("%s"), messages["zh"][key].count("%s"))

        self.assertIn('2) LANGUAGE="en" ;;', source)
        self.assertIn('""|1) LANGUAGE="zh" ;;', source)
        self.assertIn('LANGUAGE="zh"', source)
        self.assertIn('docker_cmd logs --tail 120 "${CONTAINER}"', source)


if __name__ == "__main__":
    unittest.main()
