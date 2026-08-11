from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_webui_app_imports_without_missing_shared_dependencies():
    result = subprocess.run(
        [sys.executable, "-c", "import module.webui.app"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        "module.webui.app failed to import.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
