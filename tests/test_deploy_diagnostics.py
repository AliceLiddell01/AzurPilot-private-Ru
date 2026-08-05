
import io
import queue
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import PropertyMock, patch

from deploy import uv
from module.config.time_source import NetworkTimeSource


class DeployDiagnosticTests(unittest.TestCase):
    def test_uv_startup_failure_preserves_external_exception_text(self) -> None:
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

    def test_unknown_dependency_request_keeps_request_value(self) -> None:
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

    def test_ntp_warning_preserves_external_error_detail(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
