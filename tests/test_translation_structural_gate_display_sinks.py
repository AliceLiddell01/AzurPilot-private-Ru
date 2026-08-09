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


def test_self_logger_attr_align_label_translation_passes() -> None:
    assert_passes(
        "module/os/globe_detection.py",
        'self.logger.attr_align("全球地图中心", loca)\n',
        'self.logger.attr_align("Центр карты мира", loca)\n',
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
    ],
)
def test_attr_align_non_label_or_unknown_target_changes_fail(
    base: str, head: str
) -> None:
    assert_blocked("module/os/globe_detection.py", base, head)


def test_self_notify_push_inline_content_translation_passes() -> None:
    base = """self.notify_push(
    title="[AzurPilot info] 侵蚀 1 - 行动力低于最低保留",
    content=f"总行动力 {total_ap} 低于最低保留 {min_reserve}",
)
"""
    head = """self.notify_push(
    title="[AzurPilot info] 侵蚀 1 - 行动力低于最低保留",
    content=f"Всего очков действия: {total_ap}; минимальный резерв: {min_reserve}",
)
"""
    assert_passes("module/os/tasks/hazard_leveling.py", base, head)


def test_scheduling_local_content_prose_translation_passes() -> None:
    base = """def check_and_notify_action_point_threshold(self):
    content = f"总行动力: {total_ap}"
    if previous_ap is not None:
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
    if previous_ap is not None:
        if ap_delta > 0:
            content = f"Всего очков действия: {total_ap}; увеличено на {ap_delta} очков действия"
        else:
            content = f"Всего очков действия: {total_ap}; уменьшено на {abs(ap_delta)} очков действия"
    self.notify_push(
        title="[AzurPilot] 行动力出现变化！",
        content=content,
    )
"""
    assert_passes("module/os/tasks/scheduling.py", base, head)


def test_scheduling_coin_status_provenance_translation_passes() -> None:
    base = """def _handle_smart_scheduling_no_task(self, yellow_coins, coin_target, total_ap):
    if yellow_coins < coin_target:
        coin_status = f"黄币 {yellow_coins} 低于补黄币目标 {coin_target}"
    else:
        coin_status = f"黄币 {yellow_coins} 已达到补黄币阈值 {coin_target}"
    if self.run_once():
        self.notify_push(
            title="[AzurPilot] 固定标题",
            content=f"{coin_status}\\n总行动力 {total_ap}",
        )
"""
    head = """def _handle_smart_scheduling_no_task(self, yellow_coins, coin_target, total_ap):
    if yellow_coins < coin_target:
        coin_status = f"Жёлтые монеты {yellow_coins} ниже цели пополнения {coin_target}"
    else:
        coin_status = f"Жёлтые монеты {yellow_coins} достигли порога пополнения {coin_target}"
    if self.run_once():
        self.notify_push(
            title="[AzurPilot] 固定标题",
            content=f"{coin_status}\\nВсего очков действия: {total_ap}",
        )
"""
    assert_passes("module/os/tasks/scheduling.py", base, head)


def test_scheduling_coin_status_extra_use_stays_exact() -> None:
    base = """def _handle_smart_scheduling_no_task(self, yellow_coins, coin_target):
    if yellow_coins < coin_target:
        coin_status = "不足"
    else:
        coin_status = "充足"
    consume(coin_status)
    self.notify_push(title="固定", content=f"{coin_status}")
"""
    head = """def _handle_smart_scheduling_no_task(self, yellow_coins, coin_target):
    if yellow_coins < coin_target:
        coin_status = "Недостаточно"
    else:
        coin_status = "Достаточно"
    consume(coin_status)
    self.notify_push(title="固定", content=f"{coin_status}")
"""
    assert_blocked("module/os/tasks/scheduling.py", base, head)


def test_hazard_report_builder_translation_passes() -> None:
    base = """def _format_check_report(self, ships, full, minutes):
    lines = []
    lines.append("【舰船经验检测报告】")
    if full:
        status = "已满"
        time_str = "0分钟"
    else:
        status = progress_str
        time_str = f"{minutes}分钟"
    lines.append(f"进度：{status} │ 预计时间：{time_str}")
    return "\\n".join(lines)
"""
    head = """def _format_check_report(self, ships, full, minutes):
    lines = []
    lines.append("【Отчёт о проверке опыта кораблей】")
    if full:
        status = "Максимум"
        time_str = "0 мин"
    else:
        status = progress_str
        time_str = f"{minutes} мин"
    lines.append(f"Прогресс: {status} │ Осталось: {time_str}")
    return "\\n".join(lines)
"""
    assert_passes("module/os/tasks/hazard_leveling.py", base, head)


def test_hazard_report_builder_local_value_extra_use_stays_exact() -> None:
    base = """def _format_check_report(self, full):
    lines = []
    status = "已满" if full else "未满"
    consume(status)
    lines.append(f"状态: {status}")
    return "\\n".join(lines)
"""
    head = """def _format_check_report(self, full):
    lines = []
    status = "Максимум" if full else "Не максимум"
    consume(status)
    lines.append(f"Статус: {status}")
    return "\\n".join(lines)
"""
    assert_blocked("module/os/tasks/hazard_leveling.py", base, head)


def test_hazard_report_builder_is_path_and_function_scoped() -> None:
    base = """def other(self):
    lines = []
    lines.append("检测报告")
    return "\\n".join(lines)
"""
    head = """def other(self):
    lines = []
    lines.append("Отчёт проверки")
    return "\\n".join(lines)
"""
    assert_blocked("module/os/tasks/hazard_leveling.py", base, head)


def test_hazard_report_builder_join_separator_stays_exact() -> None:
    base = """def _format_check_report(self):
    lines = []
    lines.append("检测报告")
    return "\\n".join(lines)
"""
    head = """def _format_check_report(self):
    lines = []
    lines.append("Отчёт проверки")
    return " | ".join(lines)
"""
    assert_blocked("module/os/tasks/hazard_leveling.py", base, head)


def test_scheduling_local_content_requires_exclusive_sink_use() -> None:
    base = """def check_and_notify_action_point_threshold(self):
    content = "正文"
    consume(content)
    self.notify_push(title="固定", content=content)
"""
    head = """def check_and_notify_action_point_threshold(self):
    content = "Текст"
    consume(content)
    self.notify_push(title="固定", content=content)
"""
    assert_blocked("module/os/tasks/scheduling.py", base, head)


def test_scheduling_local_content_post_sink_assignment_stays_exact() -> None:
    base = """def check_and_notify_action_point_threshold(self):
    content = "正文"
    self.notify_push(title="固定", content=content)
    content = "其他用途"
"""
    head = """def check_and_notify_action_point_threshold(self):
    content = "正文"
    self.notify_push(title="固定", content=content)
    content = "Другое назначение"
"""
    assert_blocked("module/os/tasks/scheduling.py", base, head)


def test_scheduling_local_content_without_sink_stays_exact() -> None:
    base = """def check_and_notify_action_point_threshold(self):
    content = "正文"
"""
    head = """def check_and_notify_action_point_threshold(self):
    content = "Текст"
"""
    assert_blocked("module/os/tasks/scheduling.py", base, head)


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


def test_scheduling_task_names_display_values_translation_passes() -> None:
    base = """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': '耄耋相接',
        'OpsiObscure': '隐秘海域',
        'OpsiAbyssal': '深渊坐标',
        'OpsiStronghold': '塞壬要塞',
    }
"""
    head = """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': 'Фарм мяуфицеров',
        'OpsiObscure': 'Скрытые зоны',
        'OpsiAbyssal': 'Абиссальные зоны',
        'OpsiStronghold': 'Крепости Сирен',
    }
"""
    assert_passes("module/os/tasks/scheduling.py", base, head)


@pytest.mark.parametrize(
    ("path", "base", "head"),
    [
        (
            "module/os/tasks/scheduling.py",
            """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': '耄耋相接',
        'OpsiObscure': '隐秘海域',
        'OpsiAbyssal': '深渊坐标',
        'OpsiStronghold': '塞壬要塞',
    }
""",
            """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': 'Фарм мяуфицеров',
        'OpsiHidden': 'Скрытые зоны',
        'OpsiAbyssal': 'Абиссальные зоны',
        'OpsiStronghold': 'Крепости Сирен',
    }
""",
        ),
        (
            "module/os/tasks/scheduling.py",
            """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': '耄耋相接',
        'OpsiObscure': '隐秘海域',
        'OpsiAbyssal': '深渊坐标',
        'OpsiStronghold': '塞壬要塞',
    }
""",
            """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiObscure': 'Скрытые зоны',
        'OpsiMeowfficerFarming': 'Фарм мяуфицеров',
        'OpsiAbyssal': 'Абиссальные зоны',
        'OpsiStronghold': 'Крепости Сирен',
    }
""",
        ),
        (
            "module/os/tasks/scheduling.py",
            """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': '耄耋相接',
        'OpsiObscure': '隐秘海域',
        'OpsiAbyssal': '深渊坐标',
        'OpsiStronghold': '塞壬要塞',
    }
""",
            """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': 'Фарм мяуфицеров',
        'OpsiObscure': 'Скрытые зоны',
        'OpsiAbyssal': 'Абиссальные зоны',
        'OpsiStronghold': 'Крепости Сирен',
        'Extra': 'Лишнее',
    }
""",
        ),
        (
            "module/os/tasks/scheduling.py",
            """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': '耄耋相接',
        'OpsiObscure': '隐秘海域',
        'OpsiAbyssal': '深渊坐标',
        'OpsiStronghold': '塞壬要塞',
    }
""",
            """class OtherMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': 'Фарм мяуфицеров',
        'OpsiObscure': 'Скрытые зоны',
        'OpsiAbyssal': 'Абиссальные зоны',
        'OpsiStronghold': 'Крепости Сирен',
    }
""",
        ),
        (
            "module/os/tasks/other.py",
            """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': '耄耋相接',
        'OpsiObscure': '隐秘海域',
        'OpsiAbyssal': '深渊坐标',
        'OpsiStronghold': '塞壬要塞',
    }
""",
            """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': 'Фарм мяуфицеров',
        'OpsiObscure': 'Скрытые зоны',
        'OpsiAbyssal': 'Абиссальные зоны',
        'OpsiStronghold': 'Крепости Сирен',
    }
""",
        ),
        (
            "module/os/tasks/scheduling.py",
            """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': '耄耋相接',
        'OpsiObscure': '隐秘海域',
        'OpsiAbyssal': '深渊坐标',
        'OpsiStronghold': '塞壬要塞',
    }
""",
            """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': f'Фарм {name}',
        'OpsiObscure': 'Скрытые зоны',
        'OpsiAbyssal': 'Абиссальные зоны',
        'OpsiStronghold': 'Крепости Сирен',
    }
""",
        ),
    ],
)
def test_scheduling_task_names_mapping_contract_is_fail_closed(
    path: str, base: str, head: str
) -> None:
    assert_blocked(path, base, head)


def test_scheduling_task_names_consumer_expression_stays_exact() -> None:
    base = """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': '耄耋相接',
        'OpsiObscure': '隐秘海域',
        'OpsiAbyssal': '深渊坐标',
        'OpsiStronghold': '塞壬要塞',
    }

    def display(self, task_name):
        task_display = self.TASK_NAMES.get(task_name, task_name)
        logger.info(f'Task: {task_display}')
"""
    head = """class CoinTaskMixin:
    TASK_NAMES = {
        'OpsiMeowfficerFarming': 'Фарм мяуфицеров',
        'OpsiObscure': 'Скрытые зоны',
        'OpsiAbyssal': 'Абиссальные зоны',
        'OpsiStronghold': 'Крепости Сирен',
    }

    def display(self, task_name):
        task_display = self.TASK_NAMES.get(other_name, task_name)
        logger.info(f'Task: {task_display}')
"""
    assert_blocked("module/os/tasks/scheduling.py", base, head)


def test_scheduling_task_names_function_local_shadow_stays_exact() -> None:
    base = """class CoinTaskMixin:
    def display(self):
        TASK_NAMES = {
            'OpsiMeowfficerFarming': '耄耋相接',
            'OpsiObscure': '隐秘海域',
            'OpsiAbyssal': '深渊坐标',
            'OpsiStronghold': '塞壬要塞',
        }
        return TASK_NAMES
"""
    head = """class CoinTaskMixin:
    def display(self):
        TASK_NAMES = {
            'OpsiMeowfficerFarming': 'Фарм мяуфицеров',
            'OpsiObscure': 'Скрытые зоны',
            'OpsiAbyssal': 'Абиссальные зоны',
            'OpsiStronghold': 'Крепости Сирен',
        }
        return TASK_NAMES
"""
    assert_blocked("module/os/tasks/scheduling.py", base, head)
