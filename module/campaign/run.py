"""战役任务运行器模块。

提供战役任务的完整运行框架，包括：
- 战役地图模块的动态加载与实例化
- 多种停止条件检测（运行次数、等级、石油、金币、活动 PT 等）
- 关卡名称的标准化处理（活动名称映射、特殊 SP 名称转换、关卡循环）
- 自动搜索续战逻辑
- 委托通知处理

本模块是战役任务的顶层编排器，被 alas.py 中的任务方法调用。
负责从加载地图文件到循环执行战役的完整生命周期管理。
"""

import copy
import importlib
import os
import random

from campaign import _adapt_generated_campaign_ui
from module.campaign.campaign_base import CampaignBase
from module.campaign.campaign_event import CampaignEvent
from module.shop.shop_status import ShopStatus
from module.campaign.campaign_ui import MODE_SWITCH_1
from module.config.config import AzurLaneConfig
from module.config.time_source import now as current_time
from module.event_datamine.campaign_selector import (
    generated_campaign_ui_layout,
    resolve_generated_campaign_module,
)
from module.exception import CampaignEnd, RequestHumanTakeover, ScriptEnd
from module.handler.fast_forward import map_files, to_map_file_name
from module.logger import logger
from module.notify import handle_notify
from module.ui.page import page_campaign


class CampaignRun(CampaignEvent, ShopStatus):
    """战役任务运行器。

    管理战役任务的完整生命周期：从动态加载战役地图模块，到循环执行战役并
    检测各种停止条件。是所有战役类任务（Main、Event、GemsFarming 等）的
    基础运行框架。

    通过 load_campaign() 动态导入 campaign/ 目录下的地图定义文件，
    实例化对应的 Campaign 对象，然后通过 run() 方法循环执行战役。

    Attributes:
        folder (str): campaign/ 下的地图文件夹名称，如 'campaign_main'。
        name (str): 地图文件名，如 '7-2'、'a1'、'sp3'。
        stage (str): 关卡标识，由 name 计算得出，用于 UI 导航。
        module: 动态加载的地图模块对象。
        config (AzurLaneConfig): 配置对象。
        campaign (CampaignBase): 当前战役的执行实例。
        run_count (int): 已完成的运行次数。
        run_limit (int): 运行次数限制。
        is_stage_loop (bool): 是否处于关卡循环模式。
    """
    folder: str
    name: str
    stage: str
    module = None
    config: AzurLaneConfig
    campaign: CampaignBase
    run_count: int
    run_limit: int
    is_stage_loop = False

    def load_campaign(self, name, folder='campaign_main'):
        """Загрузить модуль карты кампании.

        Для текущего generated-события канонический модуль разрешается через
        event registry до legacy-импорта. Исторический импорт остаётся fallback,
        если selector не относится к текущему generated-событию.
        """
        generated_module = resolve_generated_campaign_module(
            folder,
            name,
            now=current_time(),
        )
        if generated_module is not None:
            name = generated_module.rsplit('.', 1)[-1]

        module_name = generated_module or f'campaign.{folder}.{name}'
        source_identity = (folder, module_name)
        if getattr(self, '_campaign_source_identity', None) == source_identity:
            return False

        self.name = name
        self.folder = folder

        if folder.startswith('campaign_'):
            self.stage = '-'.join(name.split('_')[1:3])
        if folder.startswith('event') or folder.startswith('war_archives'):
            self.stage = name

        try:
            if generated_module is None:
                self.module = importlib.import_module('.' + name, f'campaign.{folder}')
            else:
                self.module = importlib.import_module(generated_module)
                _adapt_generated_campaign_ui(
                    self.module,
                    generated_campaign_ui_layout(generated_module),
                )
        except ModuleNotFoundError as error:
            missing_name = error.name or ''
            if missing_name != module_name and not module_name.startswith(f'{missing_name}.'):
                raise

            logger.warning(f'Файл карты не найден: {module_name}')
            logger.warning('[Кампания] Файл карты не найден. Обычно это означает, что выбранная пользователем карта не поддерживается или рабочий каталог задан неверно.')
            if generated_module is None and not os.path.exists(f'./campaign/{folder}'):
                logger.warning(f'[Кампания — запуск] Каталог не существует: ./campaign/{folder}')
            elif generated_module is None:
                files = map_files(folder)
                logger.warning(f'[Кампания — запуск] Доступные файлы: {files}')

            logger.critical(f'[Кампания] Возможная причина 1: в событии ({folder}) нет этапа {name}')
            logger.critical(f'[Кампания] Возможная причина 2: версия Alas устарела. Проверьте обновления или создайте файл карты с помощью dev_tools/map_extractor.py')
            raise RequestHumanTakeover

        config = copy.deepcopy(self.config).merge(self.module.Config())
        device = self.device
        self.campaign = self.module.Campaign(config=config, device=device)
        self._campaign_source_identity = source_identity

        return True

    def triggered_stop_condition(self, oil_check=True):
        """
        检查是否触发停止条件。

        Returns:
            bool: 是否触发停止条件。
        """
        # 运行次数限制
        if self.run_limit and self.config.StopCondition_RunCount <= 0:
            logger.hr('Сработало условие остановки: число запусков')
            self.config.StopCondition_RunCount = 0
            self.config.Scheduler_Enable = False
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config.config_name}>: кампания завершена",
                content=f"<{self.config.config_name}> {self.name}: достигнут лимит запусков"
            )
            return True
        # 等级限制
        if self.config.StopCondition_ReachLevel and self.campaign.config.LV_TRIGGERED:
            logger.hr(f'Сработало условие остановки: достигнут уровень {self.config.StopCondition_ReachLevel}')
            self.config.Scheduler_Enable = False
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config.config_name}>: кампания завершена",
                content=f"<{self.config.config_name}> {self.name}: достигнут лимит уровня"
            )
            return True
        # 石油限制
        if oil_check:
            # 钻石限制
            self.status_get_gems()
            # 金币限制
            self.get_coin()
            if self.get_oil() < max(500, self.config.StopCondition_OilLimit):
                logger.hr('Сработало условие остановки: лимит нефти')
                self.config.task_delay(minute=(120, 240))
                return True
        # 金币限制
        if oil_check and self.coin_limit_triggered():
            logger.hr('Сработало условие остановки: лимит монет')
            return True
        # 自动搜索石油限制
        if self.campaign.auto_search_oil_limit_triggered:
            logger.hr('Сработало условие остановки: лимит нефти в автопоиске')
            self.config.task_delay(minute=(120, 240))
            return True
        # 获得新舰船
        if self.config.StopCondition_GetNewShip and self.campaign.config.GET_SHIP_TRIGGERED:
            logger.hr('Сработало условие остановки: получен новый корабль')
            self.config.Scheduler_Enable = False
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config.config_name}>: кампания завершена",
                content=f"<{self.config.config_name}> {self.name}: получен новый корабль"
            )
            return True
        # 活动限制
        if oil_check and self.campaign.event_pt_limit_triggered():
            logger.hr('Сработало условие остановки: лимит очков события')
            return True
        # 自动搜索任务均衡器
        if self.config.TaskBalancer_Enable and self.campaign.auto_search_coin_limit_triggered:
            logger.hr('Сработало условие остановки: лимит монет в автопоиске')
            self.handle_task_balancer()
            return True
        # 任务均衡器
        if oil_check and self.run_count >= 1:
            if self.config.TaskBalancer_Enable and self.triggered_task_balancer():
                logger.hr('Сработало условие остановки: лимит монет')
                self.handle_task_balancer()
                return True

        return False

    def _triggered_app_restart(self):
        """
        检查是否触发重启条件。

        Returns:
            bool: 是否触发重启条件。
        """
        if not self.campaign.emotion.is_ignore:
            if self.campaign.emotion.triggered_bug():
                logger.info('[Кампания — запуск] Выполняется перезапуск для обхода ошибки настроения')
                return True

        return False

    def handle_app_restart(self):
        if self._triggered_app_restart():
            self.config.task_call('Restart')
            return True

        return False

    def handle_stage_name(self, name, folder, mode='normal'):
        """
        处理错误的关卡名称。
        部分活动中 SP 的名称可能不同，如 'vsp'、muse sp。
        为方便调用，其地图文件应命名为 'sp.py'。

        Args:
            name (str): .py 文件名称。
            folder (str): campaign 下的文件夹名称。

        Returns:
            str, str: (name, folder)。
        """
        name = to_map_file_name(name)
        # 处理 event_20251218_cn d3-3 特殊情况
        if folder == 'event_20251218_cn':
            # 将 d3-3 转换为 d3_3 以使用三战撤退逻辑
            if name == 'd3-3':
                name = 'd3_3'
                logger.info('[Кампания — запуск] Имя этапа d3-3 преобразовано в d3_3 (логика отступления после трёх боёв)')
            # d3 保持不变，使用标准逻辑
            elif name == 'd3':
                logger.info('[Кампания — запуск] Для этапа d3 используется стандартная логика')
        # GemsFarming 和 ThreeOilLowCost 自动选择活动或主线章节
        if self.config.task.command in ['GemsFarming', 'ThreeOilLowCost']:
            if self.stage_is_main(name):
                logger.info(f'Этап {name} загружен из campaign_main')
                folder = 'campaign_main'
            else:
                folder = self.config.cross_get('GemsFarming.Campaign.Event')
                if folder is not None:
                    logger.info(f'Этап {name} загружен из события {folder}')
                else:
                    logger.warning(f'Не удалось определить последнее событие; используется campaign_main')
                    folder = 'campaign_main'
        # 处理特殊 SP 地图名称
        if folder == 'event_20201126_cn' and name == 'vsp':
            name = 'sp'
        if folder == 'event_20210723_cn' and name == 'vsp':
            name = 'sp'
        if folder == 'event_20220324_cn' and name == 'esp':
            name = 'sp'
        if folder == 'event_20220818_cn' and name == 'esp':
            name = 'sp'
        if folder == 'event_20221124_cn' and name in ['asp', 'a.sp']:
            name = 'sp'
        if folder == 'event_20240425_cn':
            if name in ['μsp', 'usp', 'iisp']:
                name = 'sp'
            name = name.replace('lsp', 'isp').replace('1sp', 'isp')
            if name == 'isp':
                name = 'isp1'
        if folder == 'event_20240724_cn':
            if name in ['ysp', 'y.sp']:
                name = 'sp'
        # 转换为 T 章节
        convert = {
            'a1': 't1',
            'a2': 't2',
            'a3': 't3',
            'a4': 't4',
            'a5': 't5',
            'a6': 't6',
            'sp1': 't1',
            'sp2': 't2',
            'sp3': 't3',
            'sp4': 't4',
            'sp5': 't5',
            'sp6': 't6',
        }
        if folder in [
            'event_20211125_cn',
            'event_20231026_cn',
            'event_20241024_cn',
            'event_20250424_cn',
            'event_20250724_cn',
            'event_20250814_cn',
            'event_20251023_cn',
            'event_20260326_cn',
            'event_20260625_cn',
            'war_archives_20230525_cn',
            'war_archives_20231026_cn',
            'war_archives_20240725_cn',
        ]:
            name = convert.get(name, name)
        # 在 A/B/C/D 和 T/HT 之间转换
        convert = {
            'a1': 't1',
            'a2': 't2',
            'a3': 't3',
            'b1': 't4',
            'b2': 't5',
            'b3': 't6',
            'c1': 'ht1',
            'c2': 'ht2',
            'c3': 'ht3',
            'd1': 'ht4',
            'd2': 'ht5',
            'd3': 'ht6',
        }
        if folder in [
            'event_20200917_cn',
            'event_20221124_cn',
            'event_20230525_cn',
            'war_archives_20200917_cn',
            # T 章节
            'event_20211125_cn',
            'event_20231026_cn',
            'event_20231123_cn',
            'event_20240725_cn',
            'event_20240829_cn',
            'event_20241024_cn',
            'event_20241121_cn',
            'event_20250424_cn',
            'event_20250724_cn',
            'event_20250814_cn',
            'event_20251023_cn',
            'event_20260326_cn',
            'event_20260625_cn',
            'war_archives_20230525_cn',
            'war_archives_20231026_cn',
            'war_archives_20240725_cn',
        ]:
            name = convert.get(name, name)
        else:
            reverse = {v: k for k, v in convert.items()}
            name = reverse.get(name, name)
        # 炼金术士与秘密群岛
        # 处理拼写错误
        if folder == 'event_20221124_cn':
            name = name.replace('ht', 'th')
        # TH 章节没有 map_percentage 和 3_stars
        if folder == 'event_20221124_cn' and name.startswith('th'):
            if self.config.StopCondition_MapAchievement not in ['non_stop', 'non_stop_clear_all']:
                logger.info(f'[Кампания — запуск] Для главы TH события event_20221124_cn '
                            f'StopCondition.MapAchievement принудительно задано как threat_safe')
                self.config.override(StopCondition_MapAchievement='threat_safe')
        if folder == 'event_20250724_cn' and name.startswith('ts'):
            if self.config.StopCondition_MapAchievement not in ['non_stop', 'non_stop_clear_all']:
                logger.info(f'[Кампания — запуск] Для главы TS события event_20250724_cn '
                            f'StopCondition.MapAchievement принудительно задано как threat_safe')
                self.config.override(StopCondition_MapAchievement='threat_safe')
        # event_20211125_cn 的 TSS 地图为限时地图
        if folder == 'event_20211125_cn' and 'tss' in name:
            self.config.override(
                StopCondition_OilLimit=0,  # No oil cost
                StopCondition_MapAchievement='100_percent_clear',
                StopCondition_StageIncrease=True,
                Emotion_Mode='ignore',  # No emotion cost
                Fleet_Fleet2=0,  # Has only one fleet
                Submarine_Fleet=0,  # No submarine
            )
        # event_20230817_cn 剧情状态
        if folder == 'event_20230817_cn':
            if name.startswith('e0'):
                name = 'a1'
        # event_20240829_cn，TP -> SP
        if folder == 'event_20240829_cn':
            if name == 'tp':
                name = 'sp'
        # 关卡循环
        for alias, stages in self.config.STAGE_LOOP_ALIAS.items():
            alias_folder, alias = alias
            if folder == alias_folder and name == alias.lower():
                stages = [i.strip(' \t\r\n') for i in stages.split('>')]
                cycle = len(stages)
                count = int(self.config.StopCondition_RunCount)
                if count == 0:
                    stage = random.choice(stages)
                    logger.info(f'Цикл этапов в {name.upper()}: выбран случайный этап {stage}')
                else:
                    index = count % cycle
                    index = 0 if index == 0 else cycle - index
                    stage = stages[index]
                    logger.info(f'Цикл этапов в {name.upper()}, осталось запусков: run_count={count}; '
                                f'выбран следующий этап по порядку: {stage}')
                name = stage.lower()
                self.is_stage_loop = True
                # 禁用连续通关
                logger.info('[Кампания — запуск] Непрерывная зачистка отключена')
                self.config.override(StopCondition_MapAchievement='non_stop')
                self.config.override(StopCondition_StageIncrease=False)
        # 如果模式为 hard 且文件存在，将 campaign_main 转换为 campaign_hard
        if mode == 'hard' and folder == 'campaign_main' and name in map_files('campaign_hard'):
            folder = 'campaign_hard'
        # event_20240912_cn 没有 "威胁：安全" 指示器，回退 MapAchievement
        if folder == 'event_20240912_cn':
            if self.config.StopCondition_MapAchievement == 'threat_safe':
                logger.info(
                    'В событии event_20240912_cn значение MapAchievement=threat_safe заменено на map_3_stars')
                self.config.override(StopCondition_MapAchievement='map_3_stars')
            if self.config.StopCondition_MapAchievement == 'threat_safe_without_3_stars':
                logger.info(
                    'В событии event_20240912_cn значение MapAchievement=threat_safe_without_3_stars заменено на 100_percent_clear')
                self.config.override(StopCondition_MapAchievement='100_percent_clear')
        if folder == 'event_20260417_cn':
            if name in ['vsp', ]:
                name = 'sp'
        return name, folder

    def can_use_auto_search_continue(self):
        """检查是否可以继续使用自动搜索。

        当已在自动搜索菜单中、已完成至少一次运行、且未设置地图成就条件时，
        可以跳过 ensure_campaign_ui 直接继续自动搜索。

        Returns:
            bool: 是否可以继续使用自动搜索。
        """
        # 自动搜索菜单中无法更新地图信息
        # 如果设置了地图成就则关闭
        if self.config.StopCondition_MapAchievement != 'non_stop':
            return False

        return self.run_count > 0 and self.campaign.map_is_auto_search

    def after_campaign_run(self):
        """单次战役完成后的扩展钩子。"""
        pass

    def handle_commission_notice(self):
        """
        检查委托通知。如果发现委托完成，停止当前任务并调用委托处理。

        Raises:
            TaskEnd: 发现委托通知时抛出。

        Pages:
            in: page_campaign
        """
        if self.config.is_task_enabled('Commission') and self.campaign.commission_notice_show_at_campaign():
            logger.info('[Кампания — запуск] Обнаружено уведомление о комиссии')
            self.config.task_call('Commission')
            self.config.task_stop('Commission notice found')

    def run(self, name, folder='campaign_main', mode='normal', total=0):
        """Запустить задачу кампании для выбранной карты."""
        requested_name = to_map_file_name(name)
        routing_time = current_time()
        generated_module = resolve_generated_campaign_module(
            folder,
            requested_name,
            now=routing_time,
        )
        if generated_module is None:
            name, folder = self.handle_stage_name(requested_name, folder, mode=mode)
            generated_module = resolve_generated_campaign_module(
                folder,
                name,
                now=routing_time,
            )
        if generated_module is not None:
            name = generated_module.rsplit('.', 1)[-1]

        self.config.override(Campaign_Name=name, Campaign_Event=folder)
        self.load_campaign(name, folder=folder)
        self.run_count = 0
        self.run_limit = self.config.StopCondition_RunCount
        while 1:
            # Условия завершения.
            if total and self.run_count >= total:
                break
            if self.campaign.event_time_limit_triggered():
                self.config.task_stop()

            # Логирование текущего запуска.
            logger.hr(name, level=1)
            if self.config.StopCondition_RunCount > 0:
                logger.info(f'[Кампания — запуск] Осталось запусков: {self.config.StopCondition_RunCount}')
            else:
                logger.info(f'[Кампания — запуск] Выполнено запусков: {self.run_count}')

            # Приведение UI к состоянию выбранной карты.
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            if not self.device.has_cached_image:
                self.device.screenshot()
            self.campaign.device.image = self.device.image
            if self.campaign.is_in_map():
                logger.info('[Кампания] Уже на карте; выполняю отступление.')
                try:
                    self.campaign.withdraw()
                except CampaignEnd:
                    pass
                self.campaign.ensure_campaign_ui(name=self.stage, mode=mode)
            elif self.campaign.is_in_auto_search_menu():
                if self.can_use_auto_search_continue():
                    logger.info('[Кампания] Открыто меню автопоиска; ensure_campaign_ui пропущен.')
                else:
                    logger.info('[Кампания] Открыто меню автопоиска; закрываю его.')
                    # После task-balancer события event_20240725 выход из автопоиска здесь больше не выполняется отдельно.
                    self.campaign.ensure_campaign_ui(name=self.stage, mode=mode)
            else:
                self.campaign.ensure_campaign_ui(name=self.stage, mode=mode)
            self.config.override(Campaign_Mode=self.campaign.config.Campaign_Mode)
            self.disable_raid_on_event()
            self.handle_commission_notice()

            # В сложном режиме проверяем оставшиеся попытки.
            if self.ui_page_appear(page_campaign) and MODE_SWITCH_1.get(main=self) == 'normal':
                from module.hard.hard import OCR_HARD_REMAIN
                remain = OCR_HARD_REMAIN.ocr(self.device.image)
                if not remain:
                    logger.info('[Кампания — запуск] В сложном режиме не осталось попыток; задача отложена до завтра')
                    self.config.task_delay(server_update=True)
                    break

            # Условия завершения.
            if self.triggered_stop_condition(oil_check=not self.campaign.is_in_auto_search_menu()):
                break

            # Сохранение изменённой конфигурации.
            if len(self.config.modified):
                logger.info('[Кампания — запуск] Конфигурация панели управления обновлена')
                self.config.update()

            # Запуск карты.
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            try:
                self.campaign.run()
            except ScriptEnd as e:
                logger.hr('Сценарий завершён')
                logger.info(str(e))
                # После отступления останавливаем задачу и передаём управление планировщику.
                if str(e) == 'DefeatWithdraw=withdraw_stop':
                    self.config.Scheduler_Enable = False
                break

            # Сохранение изменений конфигурации карты.
            if len(self.campaign.config.modified):
                logger.info('[Кампания — запуск] Конфигурация панели управления обновлена')
                self.campaign.config.update()
            # Постобработка завершённого запуска.
            self.run_count += 1
            if self.config.StopCondition_RunCount:
                self.config.StopCondition_RunCount -= 1
            self.after_campaign_run()
            # Условия завершения после запуска.
            if self.triggered_stop_condition(oil_check=False):
                break
            # Ограничение одноразового этапа.
            if self.campaign.config.MAP_IS_ONE_TIME_STAGE:
                if self.run_count >= 1:
                    logger.hr('Сработало ограничение одноразового этапа')
                    self.campaign.handle_map_stop()
                    break
            # Цикл этапов.
            if self.is_stage_loop:
                if self.run_count >= 1:
                    logger.hr('Сработало переключение этапа в цикле')
                    break
            # Планировщик.
            if self.config.task_switched():
                self.campaign.ensure_auto_search_exit()
                self.config.task_stop()

        self.campaign.ensure_auto_search_exit()
