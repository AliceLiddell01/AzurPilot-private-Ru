from __future__ import annotations

from unittest.mock import patch

import pytest

from module.webui.setting import _close_runtime_control_server


@pytest.mark.parametrize("error", [OSError("synthetic close failure"), RuntimeError("synthetic close failure")])
def test_runtime_control_server_close_failure_does_not_abort_cleanup(error: Exception) -> None:
    class Server:
        def close(self) -> None:
            raise error

    with patch("module.logger.logger.warning") as warning:
        _close_runtime_control_server(Server())

    warning.assert_called_once()
