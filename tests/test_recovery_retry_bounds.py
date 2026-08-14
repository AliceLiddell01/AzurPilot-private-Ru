from __future__ import annotations

import ast
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TRANSPORT = ROOT / 'module/recovery/emulator_recovery.py'


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


def test_stage2_does_not_change_game_stuck_default_on_policy():
    argument = ROOT / 'module/config/argument/argument.yaml'
    data = yaml.safe_load(argument.read_text(encoding='utf-8'))
    assert data['Error']['GameStuckRestart'] is False
