from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALAS = ROOT / 'alas.py'
WINDOWS_RECOVERY = ROOT / 'module/device/platform/platform_windows_recovery.py'
TRANSPORT = ROOT / 'module/recovery/emulator_recovery.py'


def _class_method_source(path: Path, class_name: str, method_name: str) -> str:
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    method = next(
        node for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )
    return ast.get_source_segment(source, method)


def test_mumu_cold_start_budget_is_explicit_three_attempts():
    source = WINDOWS_RECOVERY.read_text(encoding='utf-8')
    assert 'MUMU_START_ATTEMPTS = 3' in source
    body = _class_method_source(
        WINDOWS_RECOVERY,
        'RecoveryPlatformWindows',
        'emulator_start',
    )
    assert 'range(1, self.MUMU_START_ATTEMPTS + 1)' in body


def test_hard_kill_has_no_retry_loop_inside_transport_incident():
    source = TRANSPORT.read_text(encoding='utf-8')
    tree = ast.parse(source)
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == 'recover_emulator_transport'
    )
    force_calls = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == 'force_stop'
    ]
    assert len(force_calls) == 1


def test_final_game_health_is_one_direct_call_not_recursive_run():
    body = _class_method_source(ALAS, 'AzurLaneAutoScript', '_try_restart_emulator')
    assert body.count('self._try_restart_game()') == 1
    assert 'self.run(' not in body
    assert "reason='game_stuck'" not in body.split('if verify_game:', 1)[1]


def test_stage2_does_not_change_game_stuck_default_on_policy():
    argument = (ROOT / 'module/config/argument/argument.yaml').read_text(encoding='utf-8')
    error_block = argument.split('Error:', 1)[1].split('Optimization:', 1)[0]
    assert 'GameStuckRestart: false' in error_block
