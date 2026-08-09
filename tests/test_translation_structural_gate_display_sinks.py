from __future__ import annotations

import pytest

from dev_tools.translation_structural_gate import verify_source_pair


def assert_passes(path: str, base: str, head: str) -> None:
    assert verify_source_pair(base, head, path) == []


def assert_blocked(path: str, base: str, head: str) -> None:
    assert verify_source_pair(base, head, path)


@pytest.mark.parametrize(
    ("path", "base", "head"),
    [
        (
            "module/os/tasks/scheduling.py",
            "def f(self):\n    self._delay_smart_scheduling_to_server_update('行动力不足')\n",
            "def f(self):\n    self._delay_smart_scheduling_to_server_update('Недостаточно очков действия')\n",
        ),
        (
            "module/os/tasks/meowfficer_farming.py",
            "def f(self):\n    self._meow_target_zone_error('指定海域输入错误')\n",
            "def f(self):\n    self._meow_target_zone_error('Ошибка ввода целевой зоны')\n",
        ),
        (
            "module/os/tasks/meowfficer_farming.py",
            "def _meow_target_zones(self):\n    self._handle_coin_task_no_content('耄耋相接', message)\n",
            "def _meow_target_zones(self):\n    self._handle_coin_task_no_content('Фарм мяуфицеров', message)\n",
        ),
        (
            "module/os/tasks/meowfficer_farming.py",
            "def _meow_handle_normal_search(self):\n    self._handle_coin_task_no_content('耄耋相接', message)\n",
            "def _meow_handle_normal_search(self):\n    self._handle_coin_task_no_content('Фарм мяуфицеров', message)\n",
        ),
        (
            "module/island/island_rancher.py",
            "def f(self, post_id):\n    self.confirm_selected_character(f'牧场岗位{post_id}派遣')\n",
            "def f(self, post_id):\n    self.confirm_selected_character(f'Позиция ранчо {post_id}: назначение')\n",
        ),
    ],
)
def test_exact_display_call_argument_translation_passes(
    path: str, base: str, head: str
) -> None:
    assert_passes(path, base, head)


@pytest.mark.parametrize(
    ("path", "base", "head"),
    [
        (
            "module/os/tasks/other.py",
            "self._delay_smart_scheduling_to_server_update('行动力不足')\n",
            "self._delay_smart_scheduling_to_server_update('Недостаточно очков действия')\n",
        ),
        (
            "module/island/island_rancher.py",
            "self.confirm_selected_character('岗位', timeout=8)\n",
            "self.confirm_selected_character('Позиция', timeout=8)\n",
        ),
        (
            "module/island/island_rancher.py",
            "self.consume('岗位')\n",
            "self.consume('Позиция')\n",
        ),
        (
            "module/island/island_rancher.py",
            "other.confirm_selected_character('岗位')\n",
            "other.confirm_selected_character('Позиция')\n",
        ),
        (
            "module/os/tasks/scheduling.py",
            "other._delay_smart_scheduling_to_server_update('行动力不足')\n",
            "other._delay_smart_scheduling_to_server_update('Недостаточно очков действия')\n",
        ),
        (
            "module/os/tasks/meowfficer_farming.py",
            "def other(self):\n    self._handle_coin_task_no_content('耄耋相接', message)\n",
            "def other(self):\n    self._handle_coin_task_no_content('Фарм мяуфицеров', message)\n",
        ),
    ],
)
def test_exact_display_call_contract_is_fail_closed(
    path: str, base: str, head: str
) -> None:
    assert_blocked(path, base, head)


def test_mcp_tool_and_schema_descriptions_translation_passes() -> None:
    base = '''def list_tools():
    return [Tool(name="status", description="获取运行状态", inputSchema={"type": "object", "properties": {"instance": {"type": "string", "description": "实例名称"}}})]
'''
    head = '''def list_tools():
    return [Tool(name="status", description="Получить состояние выполнения", inputSchema={"type": "object", "properties": {"instance": {"type": "string", "description": "Имя экземпляра"}}})]
'''
    assert_passes("mcp_server_sse.py", base, head)


def test_mcp_nested_schema_descriptions_translation_passes() -> None:
    base = '''def list_tools():
    return [Tool(name="update", description="修改配置", inputSchema={"oneOf": [{"type": "string", "description": "新的配置值"}]})]
'''
    head = '''def list_tools():
    return [Tool(name="update", description="Изменить конфигурацию", inputSchema={"oneOf": [{"type": "string", "description": "Новое значение конфигурации"}]})]
'''
    assert_passes("mcp_server_sse.py", base, head)


def test_mcp_schema_machine_values_stay_exact() -> None:
    base = 'Tool(name="status", description="状态", inputSchema={"type": "object"})\n'
    head = 'Tool(name="status", description="Состояние", inputSchema={"type": "объект"})\n'
    assert_blocked("mcp_server_sse.py", base, head)


def test_mcp_description_contract_is_function_scoped() -> None:
    assert_blocked(
        "mcp_server_sse.py",
        "def other():\n    return Tool(name='status', description='状态', inputSchema={})\n",
        "def other():\n    return Tool(name='status', description='Состояние', inputSchema={})\n",
    )


@pytest.mark.parametrize(
    ("path", "base", "head"),
    [
        (
            "module/commission/commission.py",
            "def _record_commission_income(self):\n    text = '奖励'\n    text += '今日累计'\n    tracked.append(text)\n",
            "def _record_commission_income(self):\n    text = 'Награда'\n    text += 'Итого за сегодня'\n    tracked.append(text)\n",
        ),
        (
            "module/logger.py",
            "def error_context():\n    message = '原因'\n    message += '影响'\n    logger.log(level, message)\n",
            "def error_context():\n    message = 'Причина'\n    message += 'Влияние'\n    logger.log(level, message)\n",
        ),
        (
            "module/logger.py",
            "def error_context(title, reason):\n    message = '\\n'.join([f'[错误] {title}', f'原因：{reason}'])\n    logger.log(level, message)\n",
            "def error_context(title, reason):\n    message = '\\n'.join([f'[Ошибка] {title}', f'Причина: {reason}'])\n    logger.log(level, message)\n",
        ),
        (
            "module/os/tasks/meowfficer_farming.py",
            "def _meow_target_zones(self):\n    message = '未设置目标海域'\n    logger.warning(message)\n",
            "def _meow_target_zones(self):\n    message = 'Целевая зона не задана'\n    logger.warning(message)\n",
        ),
        (
            "module/os/tasks/meowfficer_farming.py",
            "def _meow_handle_normal_search(self):\n    message = f'未找到符合条件的海域 ({hazard_level})'\n    logger.warning(message)\n",
            "def _meow_handle_normal_search(self):\n    message = f'Подходящая зона не найдена ({hazard_level})'\n    logger.warning(message)\n",
        ),
    ],
)
def test_exact_display_assignment_translation_passes(
    path: str, base: str, head: str
) -> None:
    assert_passes(path, base, head)


def test_exact_display_assignment_is_function_scoped() -> None:
    assert_blocked(
        "module/logger.py",
        "def other():\n    message = '原因'\n",
        "def other():\n    message = 'Причина'\n",
    )


def test_joined_display_assignment_separator_stays_exact() -> None:
    assert_blocked(
        "module/logger.py",
        "def error_context():\n    message = '\\n'.join(['原因', '影响'])\n",
        "def error_context():\n    message = ', '.join(['Причина', 'Влияние'])\n",
    )


def test_meow_error_builder_translation_passes() -> None:
    base = '''def _meow_target_zones(self):
    errors = []
    errors.append(f'无法识别: {tokens}')
    self._meow_target_zone_error(f'输入错误: {"; ".join(errors)}')
'''
    head = '''def _meow_target_zones(self):
    errors = []
    errors.append(f'Не распознано: {tokens}')
    self._meow_target_zone_error(f'Ошибка ввода: {"; ".join(errors)}')
'''
    assert_passes("module/os/tasks/meowfficer_farming.py", base, head)


def test_plotter_labels_translation_passes_but_machine_color_stays_exact() -> None:
    base = "def plot_single_sample_history(self):\n    ax1.plot(times, values, color='blue', label='行动力')\n    ax1.set_xlabel('时间 (天)')\n    plt.title('轨迹图')\n"
    head = "def plot_single_sample_history(self):\n    ax1.plot(times, values, color='blue', label='Очки действия')\n    ax1.set_xlabel('Время (дни)')\n    plt.title('График траектории')\n"
    assert_passes("module/os_simulator/plotter.py", base, head)
    assert_blocked(
        "module/os_simulator/plotter.py",
        base,
        head.replace("color='blue'", "color='синий'"),
    )


def test_plotter_legend_labels_translation_passes() -> None:
    base = "def plot_single_sample_history(self):\n    ax1.legend(lines + [patch], labels + ['侵蚀1', '坠机'], loc='upper left')\n"
    head = "def plot_single_sample_history(self):\n    ax1.legend(lines + [patch], labels + ['Коррозия 1', 'Сбой'], loc='upper left')\n"
    assert_passes("module/os_simulator/plotter.py", base, head)
    assert_blocked(
        "module/os_simulator/plotter.py",
        base,
        head.replace("lines + [patch]", "other_lines + [patch]"),
    )


def test_plotter_display_contract_is_function_scoped() -> None:
    assert_blocked(
        "module/os_simulator/plotter.py",
        "def other(self):\n    plt.title('轨迹图')\n",
        "def other(self):\n    plt.title('График траектории')\n",
    )


def test_plotter_display_contract_is_receiver_scoped() -> None:
    assert_blocked(
        "module/os_simulator/plotter.py",
        "def plot_single_sample_history(self):\n    other.plot(times, values, label='行动力')\n",
        "def plot_single_sample_history(self):\n    other.plot(times, values, label='Очки действия')\n",
    )


@pytest.mark.parametrize(
    ("path", "base", "head"),
    [
        (
            "module/island/island_business.py",
            "def __init__(self):\n    logger.info(f\"Режим: {'启用' if enabled else '禁用'}\")\n",
            "def __init__(self):\n    logger.info(f\"Режим: {'включён' if enabled else 'выключен'}\")\n",
        ),
        (
            "module/island/island_mine_forest.py",
            "def _record_working_post(self):\n    logger.info(f\"Продукт: {name or '未知'}\")\n",
            "def _record_working_post(self):\n    logger.info(f\"Продукт: {name or 'неизвестно'}\")\n",
        ),
        (
            "module/island/island_daily_order.py",
            "def _ocr_cooldown_below_urgent(self):\n    logger.warning(f\"OCR: {'失败' if seconds is None else f'过短({seconds}秒)'}\")\n",
            "def _ocr_cooldown_below_urgent(self):\n    logger.warning(f\"OCR: {'ошибка' if seconds is None else f'слишком мало ({seconds} с)'}\")\n",
        ),
        (
            "module/tactical/tactical_class.py",
            "def _tactical_get_finish(self):\n    logger.info(f\"Состояние: {['运行中' if state else '空闲' for state in states]}\")\n",
            "def _tactical_get_finish(self):\n    logger.info(f\"Состояние: {['выполняется' if state else 'свободно' for state in states]}\")\n",
        ),
        (
            "module/tactical/tactical_class.py",
            "def _tactical_get_finish(self):\n    logger.info(f\"Состояние: {['运行中' for state in states]}\")\n",
            "def _tactical_get_finish(self):\n    logger.info(f\"Состояние: {['выполняется' for state in states]}\")\n",
        ),
        (
            "module/tactical/tactical_class.py",
            "def _tactical_get_finish(self):\n    logger.info(f\"Состояние: {[f'第 {index} 项' for index in indexes]}\")\n",
            "def _tactical_get_finish(self):\n    logger.info(f\"Состояние: {[f'Элемент {index}: активен' for index in indexes]}\")\n",
        ),
    ],
)
def test_exact_nested_display_values_translation_passes(
    path: str, base: str, head: str
) -> None:
    assert_passes(path, base, head)


@pytest.mark.parametrize(
    ("path", "base", "head"),
    [
        (
            "module/island/island_business.py",
            "def other(self):\n    logger.info(f\"Режим: {'启用' if enabled else '禁用'}\")\n",
            "def other(self):\n    logger.info(f\"Режим: {'включён' if enabled else 'выключен'}\")\n",
        ),
        (
            "module/island/island_business.py",
            "def __init__(self):\n    other.info(f\"Режим: {'启用' if enabled else '禁用'}\")\n",
            "def __init__(self):\n    other.info(f\"Режим: {'включён' if enabled else 'выключен'}\")\n",
        ),
        (
            "module/island/island_business.py",
            "def __init__(self):\n    logger.info(f\"Режим: {mapping['启用']}\")\n",
            "def __init__(self):\n    logger.info(f\"Режим: {mapping['включён']}\")\n",
        ),
    ],
)
def test_exact_nested_display_values_contract_is_fail_closed(
    path: str, base: str, head: str
) -> None:
    assert_blocked(path, base, head)


def test_logger_attr_align_label_translation_passes() -> None:
    assert_passes(
        "module/os/globe_detection.py",
        'logger.attr_align("全球地图中心", loca)\n',
        'logger.attr_align("Центр карты мира", loca)\n',
    )


def test_logger_attr_align_value_translation_passes() -> None:
    assert_passes(
        "module/map_detection/homography.py",
        'def search_tile_rectangle(self):\n    logger.attr_align("瓦片矩形", f"{count} 个矩形 ({state})")\n',
        'def search_tile_rectangle(self):\n    logger.attr_align("Прямоугольники клеток", f"{count} прямоугольников ({state})")\n',
    )
    assert_blocked(
        "module/map_detection/homography.py",
        'def search_tile_rectangle(self):\n    logger.attr_align("瓦片矩形", f"{count} 个矩形 ({state})")\n',
        'def search_tile_rectangle(self):\n    logger.attr_align("Прямоугольники клеток", f"{total} прямоугольников ({state})")\n',
    )


def test_logger_conditional_display_translation_passes() -> None:
    assert_passes(
        "module/os_shop/shop.py",
        "logger.info(f'已购买 {count} 个物品' if count else '未购买物品')\n",
        "logger.info(f'Куплено {count} предметов' if count else 'Ничего не куплено')\n",
    )
    assert_blocked(
        "module/os_shop/shop.py",
        "logger.info('已购买' if count else '未购买')\n",
        "logger.info('Куплено' if total else 'Не куплено')\n",
    )


def test_logger_debug_translation_passes() -> None:
    assert_passes(
        "module/os/map.py",
        'logger.debug("Failed to update battle counter", exc_info=True)\n',
        'logger.debug("Не удалось обновить счётчик боёв", exc_info=True)\n',
    )


def test_logger_debug_structure_stays_exact() -> None:
    assert_blocked(
        "module/os/map.py",
        'logger.debug("Failed to update battle counter", exc_info=True)\n',
        'logger.debug("Не удалось обновить счётчик боёв", exc_info=False)\n',
    )


def test_logger_attr_get_fallback_translation_passes() -> None:
    assert_passes(
        "module/device/connection.py",
        '''def get_orientation(self):
    logger.attr("Ориентация", f'{value} ({mapping.get(value, "Unknown")})')
''',
        '''def get_orientation(self):
    logger.attr("Ориентация", f'{value} ({mapping.get(value, "Неизвестно")})')
''',
    )


def test_logger_attr_get_fallback_contract_is_function_scoped() -> None:
    assert_blocked(
        "module/device/connection.py",
        '''def other(self):
    logger.attr("Ориентация", f'{value} ({mapping.get(value, "Unknown")})')
''',
        '''def other(self):
    logger.attr("Ориентация", f'{value} ({mapping.get(value, "Неизвестно")})')
''',
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


def test_scheduling_task_names_nested_class_stays_exact() -> None:
    base = """class Outer:
    class CoinTaskMixin:
        TASK_NAMES = {
            'OpsiMeowfficerFarming': '耄耋相接',
            'OpsiObscure': '隐秘海域',
            'OpsiAbyssal': '深渊坐标',
            'OpsiStronghold': '塞壬要塞',
        }
"""
    head = """class Outer:
    class CoinTaskMixin:
        TASK_NAMES = {
            'OpsiMeowfficerFarming': 'Фарм мяуфицеров',
            'OpsiObscure': 'Скрытые зоны',
            'OpsiAbyssal': 'Абиссальные зоны',
            'OpsiStronghold': 'Крепости Сирен',
        }
"""
    assert_blocked("module/os/tasks/scheduling.py", base, head)


def test_scheduling_task_names_local_class_stays_exact() -> None:
    base = """def build():
    class CoinTaskMixin:
        TASK_NAMES = {
            'OpsiMeowfficerFarming': '耄耋相接',
            'OpsiObscure': '隐秘海域',
            'OpsiAbyssal': '深渊坐标',
            'OpsiStronghold': '塞壬要塞',
        }
    return CoinTaskMixin
"""
    head = """def build():
    class CoinTaskMixin:
        TASK_NAMES = {
            'OpsiMeowfficerFarming': 'Фарм мяуфицеров',
            'OpsiObscure': 'Скрытые зоны',
            'OpsiAbyssal': 'Абиссальные зоны',
            'OpsiStronghold': 'Крепости Сирен',
        }
    return CoinTaskMixin
"""
    assert_blocked("module/os/tasks/scheduling.py", base, head)
