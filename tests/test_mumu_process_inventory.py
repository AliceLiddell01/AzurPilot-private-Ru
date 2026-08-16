from __future__ import annotations

import ast
from pathlib import Path

from dev_tools.mumu_process_inventory import (
    classify_relationship,
    display_report_path,
    format_error,
    mask_personal_path,
)


ROOT = Path(__file__).resolve().parents[1]
INVENTORY_TOOL = ROOT / "dev_tools" / "mumu_process_inventory.py"


def test_selected_instance_name_has_priority_over_generic_mumu_classification():
    relationship = classify_relationship(
        name="NemuHeadless.exe",
        executable=r"C:\Program Files\Netease\MuMu\NemuHeadless.exe",
        command_line=(
            r'"C:\Program Files\Netease\MuMu\NemuHeadless.exe" '
            r'--comment MuMuPlayer-15.0-1 --startvm'
        ),
        instance_name="MuMuPlayer-15.0-1",
        instance_id=1,
    )

    assert relationship == "selected-instance-token"


def test_neighbor_instance_name_is_not_selected_by_prefix_substring():
    relationship = classify_relationship(
        name="NemuHeadless.exe",
        executable=r"C:\Program Files\Netease\MuMu\NemuHeadless.exe",
        command_line=(
            r'"C:\Program Files\Netease\MuMu\NemuHeadless.exe" '
            r'--comment MuMuPlayer-15.0-10 --startvm'
        ),
        instance_name="MuMuPlayer-15.0-1",
        instance_id=1,
    )

    assert relationship == "mumu-related-unclassified"


def test_instance_id_token_is_reported_without_claiming_process_ownership():
    relationship = classify_relationship(
        name="MuMuPlayer.exe",
        executable=r"C:\Program Files\Netease\MuMu\MuMuPlayer.exe",
        command_line=r'"C:\Program Files\Netease\MuMu\MuMuPlayer.exe" -v 1',
        instance_name="MuMuPlayer-15.0-1",
        instance_id=1,
    )

    assert relationship == "selected-instance-id-token"


def test_other_mumu_process_remains_unclassified():
    relationship = classify_relationship(
        name="MuMuManager.exe",
        executable=r"C:\Program Files\Netease\MuMu\MuMuManager.exe",
        command_line=r'"C:\Program Files\Netease\MuMu\MuMuManager.exe" service',
        instance_name="MuMuPlayer-15.0-1",
        instance_id=1,
    )

    assert relationship == "mumu-related-unclassified"


def test_unrelated_process_is_ignored():
    relationship = classify_relationship(
        name="python.exe",
        executable=r"C:\Python\python.exe",
        command_line=r'python worker.py',
        instance_name="MuMuPlayer-15.0-1",
        instance_id=1,
    )

    assert relationship == "unrelated"


def test_python_running_mumu_named_inventory_script_is_not_false_positive():
    relationship = classify_relationship(
        name="python.exe",
        executable=r"C:\AzurPilot\.venv\Scripts\python.exe",
        command_line=r'python mumu_process_inventory_stage2_v1.py --repository C:\AzurPilot',
        instance_name="MuMuPlayerGlobal-15.0-1",
        instance_id=1,
    )

    assert relationship == "unrelated"


def test_unrelated_process_with_selected_instance_token_is_not_false_positive():
    relationship = classify_relationship(
        name="python.exe",
        executable=r"C:\Python\python.exe",
        command_line=r'python worker.py --label MuMuPlayerGlobal-15.0-1 -v 1',
        instance_name="MuMuPlayerGlobal-15.0-1",
        instance_id=1,
    )

    assert relationship == "unrelated"


def test_mask_personal_path_hides_other_windows_profiles_case_insensitively():
    assert (
        mask_personal_path(r"C:\Users\OtherUser\AppData\Local\MuMu\file.log")
        == r"%USERPROFILE%\AppData\Local\MuMu\file.log"
    )
    assert (
        mask_personal_path(r"c:/uSeRs/SecondUser/Desktop/report.txt")
        == r"%USERPROFILE%/Desktop/report.txt"
    )


def test_display_report_path_hides_windows_profile_in_temp_path():
    path = Path(
        r"C:\Users\SensitiveUser\AppData\Local\Temp\AzurPilot-MuMu-Inventory-abc\mumu-process-inventory.json"
    )

    rendered = display_report_path(path)

    assert rendered.startswith(r"%USERPROFILE%\AppData\Local\Temp")
    assert "SensitiveUser" not in rendered


def test_format_error_masks_windows_profile():
    rendered = format_error(
        RuntimeError(r"Не удалось открыть C:\Users\SensitiveUser\Desktop\inventory.json")
    )

    assert "SensitiveUser" not in rendered
    assert r"%USERPROFILE%\Desktop\inventory.json" in rendered


def test_inventory_tool_contains_no_destructive_process_operation():
    source = INVENTORY_TOOL.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(INVENTORY_TOOL))

    destructive_attributes = {"kill", "terminate", "send_signal"}
    destructive_qualified_calls = {
        ("os", "kill"),
        ("os", "system"),
        ("subprocess", "run"),
        ("subprocess", "Popen"),
        ("subprocess", "call"),
        ("subprocess", "check_call"),
        ("subprocess", "check_output"),
    }

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        assert node.func.attr not in destructive_attributes
        if isinstance(node.func.value, ast.Name):
            assert (node.func.value.id, node.func.attr) not in destructive_qualified_calls

    string_literals = {
        node.value.casefold()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for forbidden in ("taskkill", "shutdown_player", "launch_player"):
        assert forbidden not in string_literals
