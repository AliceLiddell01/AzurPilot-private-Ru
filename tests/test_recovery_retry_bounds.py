from __future__ import annotations

import ast
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TRANSPORT = ROOT / 'module/recovery/emulator_recovery.py'
ARGUMENTS = ROOT / 'module/config/argument/argument.yaml'
PLATFORM_WINDOWS_RECOVERY = ROOT / 'module/device/platform/platform_windows_recovery.py'


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


def test_stage3_enables_both_recovery_policies_in_source_schema():
    data = yaml.safe_load(ARGUMENTS.read_text(encoding='utf-8'))
    assert data['Error']['GameStuckRestart'] is True
    assert data['Error']['AdbOfflineRestart'] is True


def test_mumu_cold_start_attempts_remain_bounded_to_three():
    source = PLATFORM_WINDOWS_RECOVERY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    recovery_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == 'RecoveryPlatformWindows'
    )
    assignment = next(
        node for node in recovery_class.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'MUMU_START_ATTEMPTS' for target in node.targets)
    )
    assert isinstance(assignment.value, ast.Constant)
    assert assignment.value.value == 3
