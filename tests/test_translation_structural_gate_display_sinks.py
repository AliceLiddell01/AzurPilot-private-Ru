from __future__ import annotations

import pytest

from dev_tools.translation_structural_gate import verify_source_pair


def assert_passes(path: str, base: str, head: str) -> None:
    assert verify_source_pair(base, head, path) == []


def assert_blocked(path: str, base: str, head: str) -> None:
    assert verify_source_pair(base, head, path)


def test_logger_attr_align_label_translation_passes() -> None:
    assert_passes(
        "module/os/globe_detection.py",
        'logger.attr_align("全球地图中心", loca)\n',
        'logger.attr_align("Центр карты мира", loca)\n',
    )


@pytest.mark.parametrize(
    ("base", "head"),
    [
        (
            'logger.attr_align("相似度", "raw", front="0.1s")\n',
            'logger.attr_align("Сходство", "сырой", front="0.1s")\n',
        ),
        (
            'logger.attr_align("相似度", value, front="0.1s")\n',
            'logger.attr_align("Сходство", value, front="0,1с")\n',
        ),
        (
            'logger.attr_align("相似度", value, align=22)\n',
            'logger.attr_align("Сходство", value, align=24)\n',
        ),
        (
            'logger.attr_align("相似度", value)\n',
            'self.logger.attr_align("Сходство", value)\n',
        ),
        (
            'logger.attr_align("相似度", value)\n',
            'other.attr_align("Сходство", value)\n',
        ),
        (
            'self.logger.attr_align("相似度", value)\n',
            'self.logger.attr_align("Сходство", value)\n',
        ),
    ],
)
def test_attr_align_non_label_or_unknown_target_changes_fail(
    base: str, head: str
) -> None:
    assert_blocked("module/os/globe_detection.py", base, head)


def test_self_notify_push_inline_content_translation_passes() -> None:
    assert_passes(
        "module/os/tasks/hazard_leveling.py",
        """self.notify_push(\n"
        "    title=\"[AzurPilot info] 侵蚀 1 - 行动力低于最低保留\",\n"
        "    content=f\"总行动力 {total_ap} 低于最低保留 {min_reserve}\",\n"
        ")\n""",
        """self.notify_push(\n"
        "    title=\"[AzurPilot info] 侵蚀 1 - 行动力低于最低保留\",\n"
        "    content=f\"Всего очков действия: {total_ap}; минимальный резерв: {min_reserve}\",\n"
        ")\n""",
    )


def test_scheduling_local_content_prose_translation_passes() -> None:
    base = """def check_and_notify_action_point_threshold(self):
    content = f"总行动力: {total_ap}"
    if ap_delta > 0:
        content = f"总行动力: {total_ap} 上涨{ap_delta}行动力"
    else:
        content = f"总行动力: {total_ap} 下跌{abs(ap_delta)}行动力"
    self.notify_push(
        title="[AzurPilot] 行动力出现变化！",
        content=content,
    )
"""
    head = """def check_and_notify_action_point_threshold(self):
    content = f"Всего очков действия: {total_ap}"
    if ap_delta > 0:
        content = f"Всего очков действия: {total_ap}; увеличено на {ap_delta}"
    else:
        content = f"Всего очков действия: {total_ap}; уменьшено на {abs(ap_delta)}"
    self.notify_push(
        title="[AzurPilot] 行动力出现变化！",
        content=content,
    )
"""
    assert_passes("module/os/tasks/scheduling.py", base, head)


@pytest.mark.parametrize(
    ("path", "base", "head"),
    [
        (
            "module/os/tasks/hazard_leveling.py",
            'self.notify_push(title="[AzurPilot] 行动力不足", content="正文")\n',
            'self.notify_push(title="[AzurPilot] Недостаточно AP", content="Текст")\n',
        ),
        (
            "module/os/tasks/hazard_leveling.py",
            'self.notify_push(title="固定", content=f"AP {total_ap}")\n',
            'self.notify_push(title="固定", content=f"AP {other_ap}")\n',
        ),
        (
            "module/os/tasks/hazard_leveling.py",
            'self.notify_push(title="固定", content=f"AP {total_ap!r}")\n',
            'self.notify_push(title="固定", content=f"AP {total_ap!s}")\n',
        ),
        (
            "module/os/tasks/hazard_leveling.py",
            'self.notify_push(title="固定", content=f"AP {total_ap:.1f}")\n',
            'self.notify_push(title="固定", content=f"AP {total_ap:.0f}")\n',
        ),
        (
            "module/os/tasks/hazard_leveling.py",
            'self.notify_push(title="固定", content="A\\nB")\n',
            'self.notify_push(title="固定", content="А B")\n',
        ),
        (
            "module/os/tasks/hazard_leveling.py",
            'self.notify_push(title="固定", content="正文")\n',
            'self.notify_push(content="Текст", title="固定")\n',
        ),
        (
            "module/os/tasks/hazard_leveling.py",
            'self.notify_push(title="固定", content="正文")\n',
            'self.notify_push(title="固定", body="Текст")\n',
        ),
        (
            "module/os/tasks/hazard_leveling.py",
            'self.notify_push(title="固定", content="正文")\n',
            'other.notify_push(title="固定", content="Текст")\n',
        ),
        (
            "module/os/tasks/hazard_leveling.py",
            'self.notify_push("固定", "正文")\n',
            'self.notify_push("固定", "Текст")\n',
        ),
        (
            "module/os/tasks/unknown.py",
            'self.notify_push(title="固定", content="正文")\n',
            'self.notify_push(title="固定", content="Текст")\n',
        ),
        (
            "module/os/tasks/scheduling.py",
            """def other(self):
    content = "正文"
    self.notify_push(title="固定", content=content)
""",
            """def other(self):
    content = "Текст"
    self.notify_push(title="固定", content=content)
""",
        ),
        (
            "module/os/tasks/scheduling.py",
            """def check_and_notify_action_point_threshold(self):
    content = f"AP {total_ap}"
    self.notify_push(title="固定", content=content)
""",
            """def check_and_notify_action_point_threshold(self):
    content = f"AP {other_ap}"
    self.notify_push(title="固定", content=content)
""",
        ),
    ],
)
def test_notify_push_sensitive_or_structural_changes_fail(
    path: str, base: str, head: str
) -> None:
    assert_blocked(path, base, head)
