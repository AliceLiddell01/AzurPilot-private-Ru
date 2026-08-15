from __future__ import annotations

import os
import shutil
from pathlib import Path

from module.config.config_updater import ConfigGenerator, ConfigUpdater


ROOT = Path(__file__).resolve().parents[1]
GENERATED_PATHS = (
    Path('config/template.json'),
    Path('module/config/argument/args.json'),
    Path('module/config/config_generated.py'),
)


def test_materialize_generated_config_for_ci_diagnostics():
    """Временный CI-only materializer; удалить после переноса exact blobs."""
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        return

    previous = Path.cwd()
    os.chdir(ROOT)
    try:
        ConfigGenerator().generate()
        ConfigUpdater().update_file('template', is_template=True)
    finally:
        os.chdir(previous)

    diagnostics = Path(os.environ['RUNNER_TEMP']) / 'azurpilot-ci-python' / 'generated'
    for relative in GENERATED_PATHS:
        source = ROOT / relative
        target = diagnostics / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    assert all((diagnostics / path).is_file() for path in GENERATED_PATHS)
