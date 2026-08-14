from __future__ import annotations

from pathlib import Path

from dev_tools.mumu_process_inventory import classify_relationship


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


def test_inventory_tool_contains_no_destructive_process_operation():
    source = INVENTORY_TOOL.read_text(encoding="utf-8")

    for forbidden in (
        ".kill(",
        ".terminate(",
        "taskkill",
        "shutdown_player",
        "launch_player",
        "subprocess.run",
        "subprocess.Popen",
        "os.system",
    ):
        assert forbidden not in source
