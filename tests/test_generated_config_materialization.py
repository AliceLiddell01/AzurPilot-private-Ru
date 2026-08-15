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


def _write_corrected_source_copy(diagnostics: Path, relative: Path, old: str, new: str) -> None:
    source = ROOT / relative
    text = source.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise AssertionError(f'Ожидалась ровно одна collateral-подстрока в {relative}')
    target = diagnostics / 'corrected' / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.replace(old, new), encoding='utf-8')


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

    _write_corrected_source_copy(
        diagnostics,
        Path('module/config/argument/argument.yaml'),
        '    option: [ 1, 2, 3, 4, 5, 6]\n  ShipIndex:',
        '    option: [ 1, 2, 3, 4, 5, 6 ]\n  ShipIndex:',
    )
    _write_corrected_source_copy(
        diagnostics,
        Path('module/config/config.py'),
        "            task (str): 要调用另一个任务名称，如 `Restart`。",
        "            task (str): 要调用的任务名称，如 `Restart`。",
    )

    assert all((diagnostics / path).is_file() for path in GENERATED_PATHS)
    assert (diagnostics / 'corrected/module/config/argument/argument.yaml').is_file()
    assert (diagnostics / 'corrected/module/config/config.py').is_file()
