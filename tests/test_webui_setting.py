from __future__ import annotations

from unittest.mock import patch

from module.webui.setting import _close_runtime_control_server


def test_runtime_control_server_close_failure_does_not_abort_cleanup() -> None:
    class Server:
        def close(self) -> None:
            raise OSError("synthetic close failure")

    with patch("module.logger.logger.warning") as warning:
        _close_runtime_control_server(Server())

    warning.assert_called_once()
