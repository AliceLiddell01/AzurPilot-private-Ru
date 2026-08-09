"""
OpsiScheduling - 智能调度+模块

智能调度+功能，用于在侵蚀1练级和耄耋相接/其他黄币补充任务之间按代理模式调度。

功能说明:
    1. 黄币检查与任务代理 - 当黄币低于保留值时，代理执行黄币补充任务
    2. 行动力阈值推送通知 - 当行动力跨越阈值时发送推送通知
    3. 最低行动力保留检查 - 检查行动力是否低于最低保留值
    4. 任务智能调度+ - 由 OpsiScheduling 统一代理执行子任务

任务层级:
    - OpsiScheduling 是和 OpsiHazard1Leveling、OpsiMeowfficerFarming 相同层级的调度器
    - 它负责协调这些任务的执行顺序，并以子任务上下文代理执行

配置项:
    - Scheduler.Enable: 任务启用开关（启用此任务即启用智能调度+功能）
    - OperationCoinsPreserve: 智能调度+时侵蚀1保留的黄币阀值（优先级高于原配置）
    - UseSmartSchedulingOperationCoinsPreserve: 开启时使用黄币目标调度，关闭时使用体力调度
    - OperationCoinsReturnThreshold: 黄币目标调度回到侵蚀1前需要高于保留值的缓冲数量
    - ActionPointPreserve: 智能调度+时保留的行动力阀值（同时作用于所有任务）
    - ActionPointNotifyLevels: 行动力阈值列表，用于推送通知
此模块包含:
    - OpsiScheduling: 智能调度+任务主类
    - CoinTaskMixin: 黄币补充任务的通用 Mixin 类（供其他任务继承使用）
"""
import re
from datetime import timedelta

from module.config.config import Function, name_to_function
from module.config.deep import deep_get
from module.config.time_source import now as current_time

from module.logger import logger
from module.os.map import OSMap
from module.os_handler.action_point import ActionPointLimit


class CoinTaskMixin:
    """
    黄币补充任务的通用 Mixin 类。
    
    提供黄币补充任务（OpsiObscure、OpsiAbyssal、OpsiStronghold、OpsiMeowfficerFarming）
    所需的通用功能，包括配置读取、通知与无内容标记。
    
    使用方法:
        class OpsiMeowfficerFarming(CoinTaskMixin, OSMap):
            ...
    """
    
    # 任务名称映射（用于通知显示）
    TASK_NAMES = {
        'OpsiMeowfficerFarming': 'Фарм мяуфицеров',
        'OpsiObscure': 'Скрытые зоны',
        'OpsiAbyssal': 'Абиссальные зоны',
        'OpsiStronghold': 'Крепости Сирен'
    }
    
    # 配置路径常量
    CONFIG_PATH_CL1_PRESERVE = 'OpsiHazard1Leveling.OpsiHazard1Leveling.OperationCoinsPreserve'
    # 四个独立任务开关的配置路径
    CONFIG_PATH_ENABLE_MEOWFFICER = 'OpsiScheduling.OpsiScheduling.EnableMeowfficerFarming'
    CONFIG_PATH_ENABLE_OBSCURE = 'OpsiScheduling.OpsiScheduling.EnableObscure'
    CONFIG_PATH_ENABLE_ABYSSAL = 'OpsiScheduling.OpsiScheduling.EnableAbyssal'
    CONFIG_PATH_ENABLE_STRONGHOLD = 'OpsiScheduling.OpsiScheduling.EnableStronghold'
    # 智能调度+新增配置路径
    CONFIG_PATH_USE_SMART_CL1_PRESERVE = 'OpsiScheduling.OpsiScheduling.UseSmartSchedulingOperationCoinsPreserve'
    CONFIG_PATH_SMART_CL1_PRESERVE = 'OpsiScheduling.OpsiScheduling.OperationCoinsPreserve'
    CONFIG_PATH_SMART_AP_PRESERVE = 'OpsiScheduling.OpsiScheduling.ActionPointPreserve'
    CONFIG_PATH_SMART_COIN_RETURN_THRESHOLD = 'OpsiScheduling.OpsiScheduling.OperationCoinsReturnThreshold'
    CONFIG_PATH_SMART_STATE = 'OpsiScheduling.Storage.Storage'
    STATE_KEY_COIN_REPLENISH_START = 'CoinReplenishStart'
    STATE_KEY_AP_REPLENISH_ACTIVE = 'ApReplenishActive'
    STATE_KEY_SCHEDULING_MODE = 'SchedulingMode'
    SCHEDULING_MODE_COIN_TARGET = 'coin_target'
    SCHEDULING_MODE_ACTION_POINT = 'action_point'
    RUNTIME_ATTR_LAST_NOTIFIED_COIN_TASK = '_smart_scheduling_last_notified_coin_task'
    RUNTIME_ATTR_LAST_COIN_TASK_NOTIFICATION_ATTEMPT = '_smart_scheduling_last_coin_task_notification_attempt'
    RUNTIME_ATTR_PREVENT_OVERFLOW_DELAY = '_prevent_action_point_overflow_delay'
    # 各任务的配置路径常量（集中管理，避免硬编码）
    CONFIG_PATH_MEOW_AP_PRESERVE = 'OpsiMeowfficerFarming.OpsiMeowfficerFarming.ActionPointPreserve'
    CONFIG_PATH_CL1_MIN_AP_RESERVE = 'OpsiHazard1Leveling.OpsiHazard1Leveling.MinimumActionPointReserve'
    
    # 耄耋相接任务名称
    TASK_NAME_MEOWFFICER_FARMING = 'OpsiMeowfficerFarming'
    TASK_NAME_HAZARD1_LEVELING = 'OpsiHazard1Leveling'
    TASK_NAME_SCHEDULING = 'OpsiScheduling'
    TASK_NAME_OBSCURE = 'OpsiObscure'
    TASK_NAME_ABYSSAL = 'OpsiAbyssal'
    TASK_NAME_STRONGHOLD = 'OpsiStronghold'
    AP_NOTIFY_MIN_INTERVAL_MINUTES = 30

    def _config_enabled(self, keys, default=False):
        """
        严格读取布尔配置，兼容 WebUI checkbox 历史值 [] / [True]。
        """
        value = self.config.cross_get(keys=keys, default=default)
        if isinstance(value, list):
            return any(bool(item) for item in value)
        return value is True

    def is_running_smart_scheduling_task(self):
        """判断当前是否由 OpsiScheduling 代执行子任务。"""
        return bool(
            getattr(self, '_smart_scheduling_context', False)
            or getattr(self.config, '_smart_scheduling_context', False)
        )

    def is_running_prevent_action_point_overflow_task(self):
        """判断当前是否由防止行动力溢出任务代执行子任务。"""
        return bool(
            getattr(self, '_prevent_action_point_overflow_context', False)
            or getattr(self.config, '_prevent_action_point_overflow_context', False)
        )

    def delay_opsi_active_task(self, *args, **kwargs):
        """
        延迟当前实际执行的大世界子任务。

        当 OpsiScheduling 代执行子任务时，将子任务延迟映射到智能调度+；
        防止行动力溢出代跑时由防止行动力溢出任务统一更新下次运行时间。
        """
        if self.is_running_smart_scheduling_task():
            self._clear_coin_task_notification_state()
            if self.is_running_prevent_action_point_overflow_task():
                kwargs.pop('task', None)
                setattr(
                    self,
                    self.RUNTIME_ATTR_PREVENT_OVERFLOW_DELAY,
                    (args, kwargs),
                )
                logger.info('[Операция «Сирена» — умное планирование+] Запрос на перенос подзадачи передан задаче защиты от переполнения очков действия')
                return

            kwargs.pop('task', None)
            if kwargs.get('server_update') is True:
                kwargs['server_update'] = self.config.cross_get(
                    keys=f'{self.TASK_NAME_SCHEDULING}.Scheduler.ServerUpdate',
                    default='00:00',
                )
            logger.info('[Операция «Сирена» — умное планирование+] Перенос подзадачи сопоставлен задаче «Умное планирование+»')
            self.config.task_delay(
                *args,
                task=self.TASK_NAME_SCHEDULING,
                **kwargs,
            )
            return

        task = kwargs.pop('task', None)
        if task is None:
            task = self._get_current_coin_task_name()
        self.config.task_delay(*args, task=task, **kwargs)

    def _is_direct_prevent_overflow_coin_task(self):
        """判断防止行动力溢出任务是否正在直接代跑黄币补充任务。"""
        if not self.is_running_prevent_action_point_overflow_task():
            return False
        owner = getattr(self.config, '_task_switch_owner', None)
        return getattr(owner, 'command', None) == 'OpsiPreventActionPointOverflow'

    def _clear_coin_task_notification_state(self):
        """清理本轮补币阶段的通知成功和尝试状态。"""
        for key in (
            self.RUNTIME_ATTR_LAST_NOTIFIED_COIN_TASK,
            self.RUNTIME_ATTR_LAST_COIN_TASK_NOTIFICATION_ATTEMPT,
        ):
            if hasattr(self.config, key):
                delattr(self.config, key)

    def _delay_smart_scheduling_to_server_update(self, reason):
        """将实际运行智能调度+的任务延迟到服务器刷新。"""
        self._clear_coin_task_notification_state()
        if self.is_running_prevent_action_point_overflow_task():
            setattr(
                self,
                self.RUNTIME_ATTR_PREVENT_OVERFLOW_DELAY,
                ((), {'server_update': True}),
            )
            logger.info(f'[Операция «Сирена» — умное планирование+] {reason}; задача защиты от переполнения очков действия отложена до обновления сервера')
            return

        logger.info(f'[Операция «Сирена» — умное планирование+] {reason}; «Умное планирование+» отложено до обновления сервера')
        self.config.task_delay(
            server_update=self.config.cross_get(
                keys=f'{self.TASK_NAME_SCHEDULING}.Scheduler.ServerUpdate',
                default='00:00',
            ),
            task=self.TASK_NAME_SCHEDULING,
        )
    
    # ==================== 推送通知相关方法 ====================
    
    def notify_push(self, title, content):
        """
        发送推送通知（智能调度+功能）
        
        Args:
            title (str): 通知标题（会自动添加实例名称前缀）
            content (str): 通知内容
            
        Notes:
            - 仅在启用智能调度+时生效
            - 启动器推送和 OnePush 推送分别由各自配置控制
            - 标题会自动格式化为 "[AzurPilot <实例名>] 原标题" 的形式

        Returns:
            bool: True 表示推送成功发送，False 表示未发送或发送失败
        """
        # 检查是否启用智能调度+
        if not self.is_smart_scheduling_enabled():
            return False

        launcher_enabled = getattr(self.config, 'OpsiGeneral_LauncherPush', True)
        onepush_enabled = bool(getattr(self.config, 'OpsiGeneral_NotifyOpsiMail', False))
        if not launcher_enabled and not onepush_enabled:
            return False

        # 获取实例名称并格式化标题
        instance_name = getattr(self.config, 'config_name', 'AzurPilot')
        if title.startswith('[AzurPilot]'):
            formatted_title = f"[AzurPilot <{instance_name}>]{title[len('[AzurPilot]'):]}"
        elif title.startswith('[AzurPilot info]'):
            formatted_title = f"[AzurPilot <{instance_name}>]{title[len('[AzurPilot info]'):]}"
        elif title.startswith('[Alas]'):
            formatted_title = f"[AzurPilot <{instance_name}>]{title[len('[Alas]'):]}"
        elif title.startswith('[Alas info]'):
            formatted_title = f"[AzurPilot <{instance_name}>]{title[len('[Alas info]'):]}"
        else:
            formatted_title = f"[AzurPilot <{instance_name}>] {title}"

        webui_success = False
        if launcher_enabled:
            try:
                from module.notify import notify_webui
                launcher_title, launcher_content = self._format_launcher_notification(
                    instance_name=instance_name,
                    title=title,
                    content=content
                )
                webui_success = notify_webui(
                    instance_name,
                    title=launcher_title,
                    content=launcher_content
                )
                if webui_success:
                    logger.info(f"[Операция «Сирена» — умное планирование+] Уведомление лаунчера отправлено: {launcher_title}")
            except Exception as e:
                logger.error(f"[Операция «Сирена» — умное планирование+] Ошибка отправки уведомления лаунчера: {e}")

        if not onepush_enabled:
            return webui_success

        # 检查是否配置了 OnePush。启动器推送不依赖 OnePush 配置。
        push_config = (
            self.config.OpsiGeneral_OpsiOnePushConfig
            if self.config.OpsiGeneral_IndependentPush
            else self.config.Error_OnePushConfig
        )
        if not self._is_push_config_valid(push_config):
            logger.warning("[Операция «Сирена» — умное планирование+] Конфигурация уведомлений не задана либо provider равен null; отправка через OnePush пропущена. Настройте канал в AzurPilot → Обработка ошибок → OnePush.")
            return webui_success

        try:
            from module.notify import handle_notify as notify_handle_notify
            success = notify_handle_notify(
                push_config,
                title=formatted_title,
                content=content
            )
            if success:
                logger.info(f"[Операция «Сирена» — умное планирование+] Уведомление отправлено: {formatted_title}")
            else:
                logger.warning(f"[Операция «Сирена» — умное планирование+] Не удалось отправить уведомление: {formatted_title}")
            return bool(success or webui_success)
        except Exception as e:
            logger.error(f"[Операция «Сирена» — умное планирование+] Ошибка отправки уведомления: {e}")
            return webui_success

    def _format_launcher_notification(self, instance_name, title, content):
        """
        启动器通知走更轻一点的本地文案，OnePush 仍保留原始标题和正文。
        """
        plain_title = title.strip()
        for prefix in ('[AzurPilot info]', '[AzurPilot]', '[Alas info]', '[Alas]'):
            if plain_title.startswith(prefix):
                plain_title = plain_title[len(prefix):].strip()
                break
        if not plain_title:
            plain_title = '大世界有新消息'

        if '行动力出现变化' in plain_title:
            launcher_title = f"{instance_name} 行动力动了一下喵~"
        elif '行动力不足' in plain_title or '行动力低于最低保留' in plain_title:
            launcher_title = f"{instance_name} 大世界行动力不够喵~"
        elif '黄币与行动力双重不足' in plain_title:
            launcher_title = f"{instance_name} 大世界补给和行动力都告急喵~"
        elif '代理执行' in plain_title:
            launcher_title = f"{instance_name} 大世界要换个活干喵~"
        elif '黄币充足' in plain_title or '凭证' in plain_title:
            launcher_title = f"{instance_name} 大世界补给有消息喵~"
        elif '检测' in plain_title or '报告' in plain_title or '检查' in plain_title:
            launcher_title = f"{instance_name} 大世界检查报告来啦喵~"
        else:
            launcher_title = f"{instance_name} 的大世界小铃铛响了喵~"

        launcher_content = f"{plain_title}\n{content}".strip()
        if not launcher_content.endswith(('喵', '喵~', '。', '！', '~')):
            launcher_content = f"{launcher_content} 喵~"
        return launcher_title, launcher_content
    
    def _is_push_config_valid(self, push_config):
        """
        检查推送配置是否有效
        
        Args:
            push_config: 推送配置字符串或对象
            
        Returns:
            bool: True 表示配置有效，False 表示无效
        """
        if not push_config:
            return False
        
        # 尝试解析为结构化数据
        if isinstance(push_config, dict):
            provider = push_config.get('provider')
            return provider is not None and provider.lower() != 'null'
        
        # 回退到字符串匹配
        if isinstance(push_config, str):
            push_config_lower = push_config.lower()
            if 'provider:null' in push_config_lower or 'provider: null' in push_config_lower:
                return False
            if 'provider' in push_config_lower:
                if re.search(r'provider\s*[:=]\s*null', push_config_lower):
                    return False
        
        return True

    def _can_send_ap_notification(self, key):
        """
        限制体力相关推送尝试的最小间隔，避免失败时高频重试。
        """
        now = current_time()
        attempt_key = f'{key}_attempt'
        last_notify = getattr(self.config, attempt_key, None) or getattr(self.config, key, None)
        min_interval = timedelta(minutes=self.AP_NOTIFY_MIN_INTERVAL_MINUTES)
        if last_notify and now - last_notify < min_interval:
            logger.info(
                f"Уведомление об AP пропущено ({key}, последнее: {last_notify}, ожидание: {self.AP_NOTIFY_MIN_INTERVAL_MINUTES} мин)"
            )
            return False
        setattr(self.config, attempt_key, now)
        return True

    def _mark_ap_notification_sent(self, key):
        """仅在至少一个通知渠道发送成功后记录成功时间。"""
        setattr(self.config, key, current_time())
    
    def check_and_notify_action_point_threshold(self):
        """
        发送行动力变化推送通知。
        需要类中包含 _action_point_total 属性。
        """
        if not hasattr(self, '_action_point_total'):
            return
            
        total_ap = self._action_point_total

        instance_name = getattr(self.config, 'config_name', 'default')
        # AP 快照由各任务模块自行管理（如 _record_ap_and_coins），此处仅保留推送逻辑。
        previous_ap = None
        try:
            from module.statistics.cl1_database import db as cl1_db
            last_notification = cl1_db.get_last_ap_notification(instance_name)
            if isinstance(last_notification, dict):
                previous_ap = last_notification.get('ap')
        except Exception:
            logger.exception('Не удалось загрузить последнее уведомление об AP')

        content = f"Всего очков действия: {total_ap}"
        if previous_ap is not None:
            ap_delta = total_ap - previous_ap
            if ap_delta == 0:
                logger.info('[Операция «Сирена» — умное планирование+] Очки действия не изменились, уведомление пропущено')
                return
            if ap_delta > 0:
                content = f"Всего очков действия: {total_ap}; увеличено на {ap_delta} очков действия"
            else:
                content = f"Всего очков действия: {total_ap}; уменьшено на {abs(ap_delta)} очков действия"

        if not self._can_send_ap_notification('_last_ap_notification_time'):
            return

        pushed = self.notify_push(
            title="[AzurPilot] 行动力出现变化！",
            content=content
        )
        if pushed:
            self._mark_ap_notification_sent('_last_ap_notification_time')
            try:
                from module.statistics.cl1_database import db as cl1_db
                cl1_db.async_set_last_ap_notification(instance_name, total_ap)
            except Exception:
                logger.exception('Не удалось сохранить последнее уведомление об AP')

    
    def _get_smart_scheduling_operation_coins_preserve(self):
        """
        获取智能调度+模式下的侵蚀1黄币保留值

        Returns:
            int: 保留的黄币数量
        """
        # 检查是否启用智能调度+黄币保留配置
        use_smart_preserve = self._is_coin_target_scheduling_enabled()
        
        if not use_smart_preserve:
            # 开关未开启，回退到侵蚀1原配置
            cl1_preserve_original = self.config.cross_get(
                keys=self.CONFIG_PATH_CL1_PRESERVE
            )
            # 保证返回 int 以免后续比较报错
            if cl1_preserve_original is None:
                cl1_preserve_original = 0
            logger.info(f'[Операция «Сирена» — умное планирование+] Резерв жёлтых монет взят из исходной конфигурации: {cl1_preserve_original} (планирование целевого запаса отключено)')
            return cl1_preserve_original
        else:
            # 开关开启，使用智能调度+自己的配置，允许为 0
            preserve = self.config.cross_get(
                keys=self.CONFIG_PATH_SMART_CL1_PRESERVE
            )
            if preserve is None:
                preserve = 0
            logger.info(f'[Операция «Сирена» — умное планирование+] Резерв жёлтых монет взят из конфигурации «Умного планирования+»: {preserve} (функция включена)')
            return preserve
    
    def _get_smart_scheduling_action_point_preserve(self):
        """
        获取智能调度+模式下的行动力保留“覆盖值”。

        注意：此处不做回退。
        - 返回值 > 0：表示启用智能调度+覆盖值（由调用方决定覆盖哪个任务的阀值）
        - 返回值 == 0：表示不覆盖，调用方应回退到各自任务的原配置

        Returns:
            int: 智能调度+行动力保留覆盖值（0 表示不覆盖）
        """
        preserve = self.config.cross_get(
            keys=self.CONFIG_PATH_SMART_AP_PRESERVE
        )
        return preserve or 0

    def _is_coin_target_scheduling_enabled(self):
        """判断是否启用黄币目标调度。关闭时使用体力调度。"""
        return self._config_enabled(
            keys=self.CONFIG_PATH_USE_SMART_CL1_PRESERVE
        )

    def _get_coin_task_action_point_preserve(self):
        """获取智能调度+用于启动黄币补充任务的行动力阈值。"""
        smart_ap_preserve = self._get_smart_scheduling_action_point_preserve()
        if smart_ap_preserve > 0:
            return smart_ap_preserve
        return self.config.cross_get(
            keys=self.CONFIG_PATH_MEOW_AP_PRESERVE
        ) or 1000

    def _get_smart_scheduling_operation_coins_return_threshold(self):
        """
        获取智能调度+补黄币阶段的回补增量。

        进入补黄币阶段后，黄币需要达到“侵蚀 1 保留值 + 此阈值”，才允许回到侵蚀 1。
        """
        threshold = self.config.cross_get(
            keys=self.CONFIG_PATH_SMART_COIN_RETURN_THRESHOLD,
            default=0,
        )
        try:
            threshold = int(threshold or 0)
        except (TypeError, ValueError):
            logger.warning(f'[Операция «Сирена» — умное планирование+] Недопустимый порог пополнения жёлтых монет: {threshold}; используется 0')
            threshold = 0
        return max(threshold, 0)

    def _get_smart_scheduling_state(self):
        """读取智能调度+持久化运行状态。"""
        state = self.config.cross_get(
            keys=self.CONFIG_PATH_SMART_STATE,
            default={},
        )
        if not isinstance(state, dict):
            return {}
        return dict(state)

    def _get_smart_scheduling_state_value(self, key, default=None):
        """读取单个智能调度+运行状态。"""
        return self._get_smart_scheduling_state().get(key, default)

    def _set_smart_scheduling_state_value(self, key, value):
        """写入单个智能调度+运行状态并立即持久化。"""
        state = self._get_smart_scheduling_state()
        if state.get(key) == value:
            return
        state[key] = value
        self.config.modified[self.CONFIG_PATH_SMART_STATE] = state
        self.config.save()

    def _clear_smart_scheduling_state_value(self, key):
        """清理单个智能调度+运行状态并立即持久化。"""
        state = self._get_smart_scheduling_state()
        if key not in state:
            return
        state.pop(key, None)
        self.config.modified[self.CONFIG_PATH_SMART_STATE] = state
        self.config.save()

    def _get_coin_replenish_target(self, yellow_coins, cl1_preserve):
        """
        获取本轮补黄币目标值。

        目标值与模拟器保持一致：侵蚀 1 保留值 + 回补阈值。
        """
        start_coins = self._get_smart_scheduling_state_value(
            self.STATE_KEY_COIN_REPLENISH_START
        )
        if start_coins is None or yellow_coins < start_coins:
            start_coins = yellow_coins
            self._set_smart_scheduling_state_value(
                self.STATE_KEY_COIN_REPLENISH_START,
                start_coins,
            )

        return_threshold = self._get_smart_scheduling_operation_coins_return_threshold()
        target = cl1_preserve + return_threshold
        return target, start_coins, return_threshold

    def _clear_coin_replenish_target(self):
        """清理本轮补黄币状态。"""
        self._clear_smart_scheduling_state_value(
            self.STATE_KEY_COIN_REPLENISH_START
        )

    def _is_coin_replenish_active(self):
        """判断当前是否处于补黄币阶段。"""
        return self._get_smart_scheduling_state_value(
            self.STATE_KEY_COIN_REPLENISH_START
        ) is not None

    def _set_ap_replenish_active(self):
        """标记体力调度补黄币阶段已开始。"""
        self._set_smart_scheduling_state_value(
            self.STATE_KEY_AP_REPLENISH_ACTIVE,
            True,
        )

    def _clear_ap_replenish_active(self):
        """清理体力调度补黄币状态。"""
        self._clear_smart_scheduling_state_value(
            self.STATE_KEY_AP_REPLENISH_ACTIVE
        )

    def _is_ap_replenish_active(self):
        """判断当前是否处于体力调度补黄币阶段。"""
        return bool(
            self._get_smart_scheduling_state_value(
                self.STATE_KEY_AP_REPLENISH_ACTIVE,
                False,
            )
        )

    def _sync_smart_scheduling_mode_state(self, coin_target_scheduling):
        """同步调度模式，并清理另一模式遗留的补黄币状态。"""
        current_mode = (
            self.SCHEDULING_MODE_COIN_TARGET
            if coin_target_scheduling
            else self.SCHEDULING_MODE_ACTION_POINT
        )
        state = self._get_smart_scheduling_state()
        previous_mode = state.get(self.STATE_KEY_SCHEDULING_MODE)
        if previous_mode == current_mode:
            return

        if previous_mode is None:
            if coin_target_scheduling:
                state.pop(self.STATE_KEY_AP_REPLENISH_ACTIVE, None)
            else:
                state.pop(self.STATE_KEY_COIN_REPLENISH_START, None)
        else:
            state.pop(self.STATE_KEY_COIN_REPLENISH_START, None)
            state.pop(self.STATE_KEY_AP_REPLENISH_ACTIVE, None)
            self._clear_coin_task_notification_state()
            logger.info(
                f'[Операция «Сирена» — умное планирование+] Режим планирования изменён с {previous_mode} на {current_mode}; '
                'состояние предыдущего режима очищено'
            )

        state[self.STATE_KEY_SCHEDULING_MODE] = current_mode
        self.config.modified[self.CONFIG_PATH_SMART_STATE] = state
        self.config.save()

    def _get_effective_cl1_ap_preserve(self):
        """
        获取智能调度+下侵蚀 1 使用的行动力保留值。
        """
        preserve = self.config.cross_get(
            keys=self.CONFIG_PATH_CL1_MIN_AP_RESERVE,
            default=200,
        )
        return preserve

    def _get_current_coin_task_name(self):
        """
        获取当前任务名称（用于调度范围检查）
        
        Returns:
            str: 任务命令名称（如 'OpsiObscure'），如果不可用则返回类名
        """
        if hasattr(self.config, 'task') and hasattr(self.config.task, 'command') and self.config.task.command:
            return self.config.task.command
        return self.__class__.__name__
    
    def _get_enabled_coin_tasks(self):
        """
        获取智能调度+中启用的黄币补充任务列表，并按 TaskPriority 排序。
        
        Returns:
            list: 启用的任务名称列表
        """
        enabled_tasks = []
        
        # 检查每个任务的独立开关
        task_config_map = {
            'OpsiStronghold': self.CONFIG_PATH_ENABLE_STRONGHOLD,
            'OpsiObscure': self.CONFIG_PATH_ENABLE_OBSCURE,
            'OpsiAbyssal': self.CONFIG_PATH_ENABLE_ABYSSAL,
            'OpsiMeowfficerFarming': self.CONFIG_PATH_ENABLE_MEOWFFICER,
        }
        
        for task_name, config_path in task_config_map.items():
            if self._config_enabled(keys=config_path):
                enabled_tasks.append(task_name)

        # 按照 OpsiScheduling_TaskPriority 配置的顺序进行过滤和排序
        try:
            priority_str = self.config.OpsiScheduling_TaskPriority
            if priority_str:
                priorities = [p.strip() for p in priority_str.split('>') if p.strip()]
                def sort_key(task):
                    try:
                        return priorities.index(task)
                    except ValueError:
                        return len(priorities)
                enabled_tasks = sorted(enabled_tasks, key=sort_key)
        except Exception as e:
            logger.warning(f'[Операция «Сирена» — умное планирование+] Не удалось упорядочить задачи пополнения жёлтых монет по приоритету: {e}; используется порядок по умолчанию')
        
        return enabled_tasks

    def _handle_coin_task_no_content(self, task_display_name, log_message):
        """
        处理黄币补充任务没有可执行内容的情况。
        """
        logger.info(f'[Операция «Сирена» — умное планирование+] {log_message}; подготовка к завершению текущей задачи')
        task_name = self._get_current_coin_task_name()
        logger.info(f'[Операция «Сирена» — умное планирование+] Обрабатываемая задача: {task_name}')

        if self.is_running_smart_scheduling_task():
            if '没有更多' not in log_message:
                self._smart_scheduling_no_content_task = task_name
            logger.info(f'[Операция «Сирена» — умное планирование+] Выполнение через диспетчер «Умного планирования+»: для {task_display_name} нет доступных действий')
            if self._is_direct_prevent_overflow_coin_task():
                self.delay_opsi_active_task(server_update=True)
                self.config.task_stop()
            return True

        if self.is_smart_scheduling_enabled():
            logger.info(f'[Операция «Сирена» — умное планирование+] «Умное планирование+» включено; для {task_display_name} нет доступных действий')
            self.config.task_delay(server_update=True)
            self.config.task_stop()

        with self.config.multi_set():
            try:
                from module.config.utils import get_os_reset_remain
            except ImportError:
                get_os_reset_remain = None

            if task_name in ('OpsiObscure', 'OpsiAbyssal') and get_os_reset_remain is not None:
                remain = get_os_reset_remain()
                if remain == 0:
                    logger.info(f'[Операция «Сирена» — умное планирование+] Для {task_name} больше нет доступных действий; до сброса Операции «Сирена» менее суток, повторный запуск через 2,5 часа')
                    self.config.task_delay(minute=150, server_update=True)
                else:
                    logger.info(f'[Операция «Сирена» — умное планирование+] Для {task_name} больше нет доступных действий; повторный запуск отложен до следующего обновления сервера')
                    self.config.task_delay(server_update=True)
            else:
                logger.info(f'[Операция «Сирена» — умное планирование+] Для {task_name} больше нет доступных действий; повторный запуск отложен до следующего обновления сервера')
                self.config.task_delay(server_update=True)
        
        self.config.task_stop()
        return True


class OpsiScheduling(CoinTaskMixin, OSMap):
    """
    智能调度+任务主类
    
    负责协调大世界（Operation Siren）中的各项任务调度，
    包括侵蚀1练级、耄耋相接、隐秘海域、深渊坐标、塞壬要塞等。
    
    主要功能:
        1. 黄币管理 - 当黄币不足时代理执行补充任务
        2. 行动力监控 - 监控行动力并发送阈值通知
        3. 任务协调 - 统一决定并代理执行子任务
    """

    def _make_opsi_task_function(self, task_name):
        """从当前配置数据构造临时代跑任务对象。"""
        data = deep_get(self.config.data, keys=task_name, default=None)
        if isinstance(data, dict):
            task = Function(data)
            if task.command != "Unknown":
                return task
        return name_to_function(task_name)

    def _run_with_opsi_task_context(self, task_name, func, *args, **kwargs):
        """
        以指定大世界子任务身份执行逻辑，保证统计和配置读取仍按子任务归类。
        """
        previous_task = self.config.task
        previous_bind = getattr(self.config, '_bind_task_override', None)
        previous_context = getattr(self, '_smart_scheduling_context', None)
        previous_config_context = getattr(self.config, '_smart_scheduling_context', None)
        previous_disable_task_switch = getattr(self.config, '_disable_task_switch', False)
        previous_task_switch_owner = getattr(self.config, '_task_switch_owner', None)
        self._smart_scheduling_context = True
        self.config._smart_scheduling_context = True
        self.config._disable_task_switch = task_name not in (
            self.TASK_NAME_HAZARD1_LEVELING,
            self.TASK_NAME_MEOWFFICER_FARMING,
        )
        self.config._task_switch_owner = previous_task
        self.config.task = self._make_opsi_task_function(task_name)
        self.config._bind_task_override = task_name
        self.config.bind(task_name)
        try:
            return func(*args, **kwargs)
        finally:
            self.config.task = previous_task

            if previous_context is None:
                if hasattr(self, '_smart_scheduling_context'):
                    delattr(self, '_smart_scheduling_context')
            else:
                self._smart_scheduling_context = previous_context

            if previous_config_context is None:
                if hasattr(self.config, '_smart_scheduling_context'):
                    delattr(self.config, '_smart_scheduling_context')
            else:
                self.config._smart_scheduling_context = previous_config_context
            self.config._disable_task_switch = previous_disable_task_switch
            if previous_task_switch_owner is None:
                if hasattr(self.config, '_task_switch_owner'):
                    delattr(self.config, '_task_switch_owner')
            else:
                self.config._task_switch_owner = previous_task_switch_owner

            if previous_bind is None:
                if hasattr(self.config, '_bind_task_override'):
                    delattr(self.config, '_bind_task_override')
                self.config.bind(self.config.task)
            else:
                self.config._bind_task_override = previous_bind
                self.config.bind(previous_bind)

    def _get_scheduling_action_point(self):
        """
        读取智能调度+决策所需的行动力。

        Returns:
            tuple[int, int]: (总行动力, 当前真实行动力)
        """
        self.action_point_enter()
        self.action_point_safe_get()
        self.action_point_quit()
        self.check_and_notify_action_point_threshold()
        return (
            int(getattr(self, '_action_point_total', 0) or 0),
            int(getattr(self, '_action_point_current', 0) or 0),
        )

    def _run_scheduled_meowfficer_farming(self, ap_preserve):
        """
        由智能调度+执行一轮耄耋相接。
        """
        if not hasattr(self, 'run_meowfficer_farming_once'):
            logger.error('[Операция «Сирена» — умное планирование+] Текущий экземпляр не поддерживает запуск фарма мяуфицеров')
            self.config.task_stop()

        logger.info('[Операция «Сирена» — умное планирование+] Выполнение одного цикла фарма мяуфицеров')
        self._run_with_opsi_task_context(
            self.TASK_NAME_MEOWFFICER_FARMING,
            self.run_meowfficer_farming_once,
            ap_preserve=ap_preserve,
        )

    def handle_first_auto_search(self, run):
        """由智能调度+决策是否执行 os_init 阶段跳过的首次自律寻敌。"""
        if not getattr(self, "_smart_scheduling_first_auto_search_pending", False):
            return
        self._smart_scheduling_first_auto_search_pending = False

        if not run:
            logger.info("Следующей задачей «Умного планирования+» будет прокачка в зоне коррозии 1; инициализация автопоиска врагов пропущена")
            return

        self.run_first_auto_search()

    def _handle_smart_scheduling_no_task(self, yellow_coins, total_ap, current_ap, coin_target, meow_ap_preserve):
        """
        处理黄币和行动力不足导致没有可运行任务的情况。

        防止行动力溢出任务代跑智能调度+时，需要清理当前真实行动力，因此直接跑一轮耄耋相接。
        普通智能调度+保持延后，不按行动力恢复时间唤起。
        """
        if self.is_running_prevent_action_point_overflow_task() and current_ap > 0:
            logger.info(
                f'Контекст защиты от переполнения очков действия: жёлтых монет недостаточно, а суммарные очки действия не достигли резерва для их пополнения; '
                f'выполняется фарм мяуфицеров для расходования текущих очков действия (текущие={current_ap}, суммарные={total_ap})'
            )
            if yellow_coins < coin_target:
                coin_status = f'Жёлтые монеты {yellow_coins} ниже целевого запаса {coin_target}'
            else:
                coin_status = f'Жёлтые монеты {yellow_coins} достигли порога пополнения {coin_target}'
            self.handle_first_auto_search(run=True)
            if self._run_scheduled_coin_task_once(self.TASK_NAME_MEOWFFICER_FARMING, 0):
                self.notify_push(
                    title='[AzurPilot] 防止行动力溢出 - 已执行耄耋相接',
                    content=(
                        f'{coin_status}\n'
                        f'Всего очков действия {total_ap} не превышает резерв для пополнения монет {meow_ap_preserve}\n'
                        f'OpsiScheduling выполнил один цикл фарма мяуфицеров, чтобы израсходовать текущие очки действия {current_ap}'
                    )
                )
                return

            logger.warning('[Операция «Сирена» — защита от переполнения очков действия] Для фарма мяуфицеров нет доступных действий; продолжить расходование текущих очков действия невозможно')
            self._delay_smart_scheduling_to_server_update('耄耋相接无可执行内容')
            self.config.task_stop()
            return

        self._notify_coins_ap_insufficient(yellow_coins, total_ap, coin_target, meow_ap_preserve)
        self._delay_smart_scheduling_for_ap_limit(total_ap, meow_ap_preserve)

    def _run_scheduled_hazard1_leveling(self, ap_preserve):
        """
        由智能调度+执行一轮侵蚀 1 练级。
        """
        if not hasattr(self, 'run_hazard1_leveling_once'):
            logger.error('[Операция «Сирена» — умное планирование+] Текущий экземпляр не поддерживает прокачку в зоне коррозии 1')
            self.config.task_stop()

        logger.info('[Операция «Сирена» — умное планирование+] Выполнение одного цикла прокачки в зоне коррозии 1')
        self.handle_first_auto_search(run=False)
        if hasattr(self, 'os_check_leveling'):
            self._run_with_opsi_task_context(
                self.TASK_NAME_HAZARD1_LEVELING,
                self.os_check_leveling,
            )
        self._run_with_opsi_task_context(
            self.TASK_NAME_HAZARD1_LEVELING,
            self.run_hazard1_leveling_once,
            ap_preserve=ap_preserve,
        )

    def _run_scheduled_coin_task_once(self, task_name, ap_preserve):
        """由智能调度+代理执行一轮黄币补充任务。"""
        if not hasattr(self, '_smart_scheduling_no_content_task'):
            self._smart_scheduling_no_content_task = None
        self._smart_scheduling_no_content_task = None

        task_display = self.TASK_NAMES.get(task_name, task_name)
        logger.info(f'[Операция «Сирена» — умное планирование+] Выполнение одного цикла задачи через диспетчер: {task_display}')
        if task_name == self.TASK_NAME_MEOWFFICER_FARMING:
            self._run_scheduled_meowfficer_farming(ap_preserve)
        elif task_name == self.TASK_NAME_OBSCURE:
            if not hasattr(self, 'clear_obscure'):
                logger.error('[Операция «Сирена» — умное планирование+] Текущий экземпляр не поддерживает зачистку скрытых зон')
                self.config.task_stop()
            self._run_with_opsi_task_context(task_name, self.clear_obscure)
        elif task_name == self.TASK_NAME_ABYSSAL:
            if not hasattr(self, 'clear_abyssal'):
                logger.error('[Операция «Сирена» — умное планирование+] Текущий экземпляр не поддерживает зачистку абиссальных зон')
                self.config.task_stop()
            self._run_with_opsi_task_context(task_name, self.clear_abyssal)
        elif task_name == self.TASK_NAME_STRONGHOLD:
            if not hasattr(self, 'clear_stronghold'):
                logger.error('[Операция «Сирена» — умное планирование+] Текущий экземпляр не поддерживает зачистку крепостей Сирен')
                self.config.task_stop()
            self._run_with_opsi_task_context(task_name, self.clear_stronghold)
        else:
            logger.error(f'[Операция «Сирена» — умное планирование+] Диспетчер не поддерживает задачу пополнения жёлтых монет: {task_name}')
            self.config.task_stop()

        no_content_task = getattr(self, '_smart_scheduling_no_content_task', None)
        self._smart_scheduling_no_content_task = None
        if no_content_task == task_name:
            logger.info(f'[Операция «Сирена» — умное планирование+] Для {task_display} нет доступных действий')
            return False
        return True

    def _delay_smart_scheduling_for_ap_limit(self, total_ap, min_ap_reserve):
        """
        因行动力不足推迟智能调度+。
        """
        logger.warning(f'[Операция «Сирена» — умное планирование+] Очки действия достигли минимального резерва ({total_ap} <= {min_ap_reserve})')
        self._notify_ap_insufficient(total_ap, min_ap_reserve)
        self._delay_smart_scheduling_to_server_update('行动力不足')
        self.config.task_stop()

    def run_smart_scheduling_once(self):
        """执行一轮智能调度+决策。"""
        yellow_coins = self.get_yellow_coins()
        total_ap, current_ap = self._get_scheduling_action_point()
        cl1_preserve = self._get_smart_scheduling_operation_coins_preserve()
        cl1_ap_preserve = self._get_effective_cl1_ap_preserve()
        meow_ap_preserve = self._get_coin_task_action_point_preserve()
        coin_target_scheduling = self._is_coin_target_scheduling_enabled()
        self._sync_smart_scheduling_mode_state(coin_target_scheduling)
        coin_replenish_active = self._is_coin_replenish_active()
        ap_replenish_active = self._is_ap_replenish_active()

        logger.info(f'[Операция «Сирена» — умное планирование+] Жёлтые монеты: {yellow_coins}, резерв: {cl1_preserve}')
        if self.is_running_prevent_action_point_overflow_task():
            logger.info(
                f'[Операция «Сирена» — умное планирование+] Очки действия: текущие={current_ap}, суммарные={total_ap}, '
                f'резерв CL1={cl1_ap_preserve}, резерв пополнения монет={meow_ap_preserve}'
            )
        else:
            logger.info(
                f'[Операция «Сирена» — умное планирование+] Суммарные очки действия: {total_ap}, '
                f'резерв CL1={cl1_ap_preserve}, резерв пополнения монет={meow_ap_preserve}'
            )

        try:
            if coin_target_scheduling and (yellow_coins < cl1_preserve or coin_replenish_active):
                coin_target, start_coins, return_threshold = self._get_coin_replenish_target(
                    yellow_coins,
                    cl1_preserve,
                )
                logger.info(
                    f'[Операция «Сирена» — умное планирование+] Целевой запас монет: текущие={yellow_coins}, начальные={start_coins}, '
                    f'порог пополнения={return_threshold}, цель={coin_target}'
                )
                if yellow_coins >= coin_target:
                    logger.info(f'[Операция «Сирена» — умное планирование+] Жёлтые монеты пополнены ({yellow_coins} >= {coin_target}), возврат к прокачке в зоне коррозии 1')
                    self._clear_coin_replenish_target()
                else:
                    logger.info(f'[Операция «Сирена» — умное планирование+] Жёлтые монеты не пополнены ({yellow_coins} < {coin_target}), требуется задача их пополнения')
                    if total_ap <= meow_ap_preserve:
                        logger.warning(f'[Операция «Сирена» — умное планирование+] Недостаточно очков действия для задачи пополнения жёлтых монет ({total_ap} <= {meow_ap_preserve})')
                        self._handle_smart_scheduling_no_task(
                            yellow_coins,
                            total_ap,
                            current_ap,
                            coin_target,
                            meow_ap_preserve,
                        )
                        return

                    self._dispatch_coin_task(
                        yellow_coins,
                        total_ap,
                        coin_target,
                        meow_ap_preserve,
                    )
                    return

            if not coin_target_scheduling and (yellow_coins < cl1_preserve or ap_replenish_active):
                if not ap_replenish_active:
                    self._set_ap_replenish_active()
                logger.info(
                    f'[Операция «Сирена» — умное планирование+] Пополнение жёлтых монет по очкам действия: монеты={yellow_coins}, '
                    f'порог монет={cl1_preserve}, суммарные очки действия={total_ap}, порог очков действия={meow_ap_preserve}'
                )
                if total_ap <= meow_ap_preserve:
                    logger.info(f'[Операция «Сирена» — умное планирование+] Очки действия достигли порога планирования ({total_ap} <= {meow_ap_preserve}), пополнение жёлтых монет остановлено')
                    self._clear_ap_replenish_active()
                    overflow_cleanup = (
                        self.is_running_prevent_action_point_overflow_task()
                        and current_ap > 0
                    )
                    if yellow_coins < cl1_preserve or overflow_cleanup:
                        self._handle_smart_scheduling_no_task(
                            yellow_coins,
                            total_ap,
                            current_ap,
                            cl1_preserve,
                            meow_ap_preserve,
                        )
                        return
                    logger.info(
                        f'[Операция «Сирена» — умное планирование+] Жёлтые монеты пополнены ({yellow_coins} >= {cl1_preserve}), '
                        'возврат к прокачке в зоне коррозии 1'
                    )
                else:
                    self._dispatch_coin_task(
                        yellow_coins,
                        total_ap,
                        cl1_preserve,
                        meow_ap_preserve,
                    )
                    return

            if total_ap <= cl1_ap_preserve:
                self._delay_smart_scheduling_for_ap_limit(total_ap, cl1_ap_preserve)

            logger.info(f'[Операция «Сирена» — умное планирование+] Жёлтых монет достаточно ({yellow_coins} >= {cl1_preserve}), запуск прокачки в зоне коррозии 1')
            self._execute_hazard1_leveling(yellow_coins, total_ap)
        except ActionPointLimit as e:
            logger.warning(f'[Операция «Сирена» — умное планирование+] Недостаточно очков действия при выполнении подзадачи: {e}')
            preserve = getattr(e, 'preserve', None) or cl1_ap_preserve
            current = getattr(e, 'total', None) or getattr(e, 'current', None) or total_ap
            self._delay_smart_scheduling_for_ap_limit(current, preserve)

    def run_smart_scheduling(self):
        """
        执行智能调度+主逻辑

        此方法是智能调度+任务的入口点，负责：
        1. 检查是否启用智能调度+
        2. 根据黄币和行动力状态决定当前应该执行的任务
        3. 按代理模式协调子任务执行
        """
        logger.hr('Операция «Сирена» — умное планирование+', level=1)

        # 检查是否启用智能调度+
        if not self.is_smart_scheduling_enabled():
            logger.info('[Операция «Сирена» — умное планирование+] «Умное планирование+» отключено, выполнение пропущено')
            return

        while True:
            self.run_smart_scheduling_once()
            self.config.check_task_switch()

    def _notify_coins_ap_insufficient(self, yellow_coins, total_ap, coin_target, meow_ap_preserve):
        """
        发送黄币与行动力双重不足的通知
        """
        if not self.is_smart_scheduling_enabled():
            return

        if not self._can_send_ap_notification('_last_ap_coins_insufficient_notification_time'):
            return
        
        pushed = self.notify_push(
            title="[AzurPilot] 智能调度+ - 黄币与行动力双重不足",
            content=(
                f"Жёлтые монеты: {yellow_coins}, порог пополнения: {coin_target}\n"
                f"Всего очков действия {total_ap} недостаточно (требуется {meow_ap_preserve})\nЗадача отложена"
            )
        )
        if pushed:
            self._mark_ap_notification_sent('_last_ap_coins_insufficient_notification_time')
    
    def _notify_ap_insufficient(self, total_ap, min_reserve):
        """
        发送行动力低于最低保留的通知
        """
        if not self.is_smart_scheduling_enabled():
            return

        if not self._can_send_ap_notification('_last_ap_insufficient_notification_time'):
            return
        
        pushed = self.notify_push(
            title="[AzurPilot] 智能调度+ - 行动力不足",
            content=f"Всего очков действия {total_ap} не превышает минимальный резерв {min_reserve}, задача отложена"
        )
        if pushed:
            self._mark_ap_notification_sent('_last_ap_insufficient_notification_time')
    
    def _dispatch_coin_task(self, yellow_coins, total_ap, coin_target, meow_ap_preserve):
        """
        调度黄币补充任务。

        所有黄币补充任务都由 OpsiScheduling 代理执行一轮，不启用、关闭、推迟子任务调度器。
        """
        all_coin_tasks = self._get_enabled_coin_tasks()
        if not all_coin_tasks:
            logger.error('[Операция «Сирена» — умное планирование+] Ни одна задача пополнения жёлтых монет не включена, «Умное планирование+» остановлено')
            self.notify_push(
                title='[AzurPilot] 智能调度+ - 未启用黄币补充任务',
                content='Включите хотя бы одну из задач: фарм мяуфицеров, скрытые зоны, абиссальные зоны или крепости Сирен',
            )
            self._delay_smart_scheduling_to_server_update('未启用黄币补充任务')
            self.config.task_stop()

        self.handle_first_auto_search(run=True)
        task_names = '、'.join([self.TASK_NAMES.get(task, task) for task in all_coin_tasks])
        logger.info(f'[Операция «Сирена» — умное планирование+] Включённые задачи пополнения жёлтых монет: {task_names}')

        for task_name in all_coin_tasks:
            if self._run_scheduled_coin_task_once(task_name, meow_ap_preserve):
                self._notify_coin_task_proxy(
                    yellow_coins,
                    total_ap,
                    coin_target,
                    meow_ap_preserve,
                    task_name,
                )
                return

        logger.warning('[Операция «Сирена» — умное планирование+] Во всех включённых задачах пополнения жёлтых монет нет доступных действий; текущий цикл завершён')
        self._delay_smart_scheduling_to_server_update('黄币补充任务均无可执行内容')
        self.config.task_stop()

    def _notify_coin_task_proxy(self, yellow_coins, total_ap, coin_target, meow_ap_preserve, task_name):
        """
        发送代理执行黄币补充任务的通知。
        """
        if not self.is_smart_scheduling_enabled():
            return

        state_key = self.RUNTIME_ATTR_LAST_NOTIFIED_COIN_TASK
        if getattr(self.config, state_key, None) == task_name:
            return

        attempt_key = self.RUNTIME_ATTR_LAST_COIN_TASK_NOTIFICATION_ATTEMPT
        last_attempt = getattr(self.config, attempt_key, None)
        now = current_time()
        if isinstance(last_attempt, tuple) and len(last_attempt) == 2:
            attempted_task, attempted_at = last_attempt
            if (
                attempted_task == task_name
                and isinstance(attempted_at, type(now))
                and now - attempted_at < timedelta(minutes=self.AP_NOTIFY_MIN_INTERVAL_MINUTES)
            ):
                return
        setattr(self.config, attempt_key, (task_name, now))

        task_display = self.TASK_NAMES.get(task_name, task_name)
        pushed = self.notify_push(
            title="[AzurPilot] 智能调度+ - 已代理执行黄币补充任务",
            content=(f"Жёлтые монеты: {yellow_coins}, порог пополнения: {coin_target}\n"
                     f"Всего очков действия: {total_ap} (требуется {meow_ap_preserve})\n"
                     f"Выполнен один цикл задачи {task_display} для пополнения жёлтых монет")
        )
        if pushed:
            setattr(self.config, state_key, task_name)
    
    def _execute_hazard1_leveling(self, yellow_coins, total_ap):
        """
        执行侵蚀1练级任务
        """
        self._clear_coin_task_notification_state()
        logger.info('[Операция «Сирена» — умное планирование+] Выполнение задачи прокачки в зоне коррозии 1')
        self._run_scheduled_hazard1_leveling(self._get_effective_cl1_ap_preserve())
    
    def notify_action_point_threshold(self, title, content):
        """
        发送行动力阈值变化通知
        
        Args:
            title (str): 通知标题
            content (str): 通知内容
        """
        if not self.is_smart_scheduling_enabled():
            return

        if not self._can_send_ap_notification('_last_ap_threshold_notification_time'):
            return
        
        pushed = self.notify_push(title=title, content=content)
        if pushed:
            self._mark_ap_notification_sent('_last_ap_threshold_notification_time')
