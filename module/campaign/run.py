"""Исполнитель задач кампании.

Модуль предоставляет полный контур выполнения кампании:
- динамическую загрузку и создание модулей карт;
- проверку условий остановки по числу запусков, уровню, нефти, монетам и PT события;
- нормализацию имён этапов, включая event aliases, специальные SP и циклы этапов;
- продолжение автоматического поиска;
- обработку уведомлений о комиссиях.

Это верхнеуровневый оркестратор задач кампании, вызываемый методами задач из
``alas.py``. Он управляет полным жизненным циклом от загрузки файла карты до
циклического выполнения кампании.
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
from module.event_datamine.campaign_selector import (
    EventCampaignSelectorError,
    generated_campaign_ui_layout,
    resolve_generated_campaign_module,
)
from module.exception import CampaignEnd, RequestHumanTakeover, ScriptEnd
from module.handler.fast_forward import map_files, to_map_file_name
from module.logger import logger
from module.notify import handle_notify
from module.ui.page import page_campaign


class CampaignRun(CampaignEvent, ShopStatus):
    """Исполнитель задач кампании.

    Управляет полным жизненным циклом задачи: динамически загружает модуль карты,
    создаёт объект Campaign, выполняет кампанию в цикле и проверяет условия
    остановки. Это базовый исполнитель для Main, Event, GemsFarming и других
    campaign-задач.

    ``load_campaign()`` импортирует определение карты из ``campaign/``, а
    ``run()`` выполняет созданную Campaign до срабатывания условия остановки.

    Атрибуты:
        folder (str): имя каталога карты внутри ``campaign/``, например ``campaign_main``.
        name (str): имя файла карты, например ``7-2``, ``a1`` или ``sp3``.
        stage (str): идентификатор этапа для навигации UI, вычисляемый из ``name``.
        module: динамически загруженный модуль карты.
        config (AzurLaneConfig): конфигурация задачи.
        campaign (CampaignBase): текущий исполнитель кампании.
        run_count (int): число завершённых запусков.
        run_limit (int): ограничение числа запусков.
        is_stage_loop (bool): признак режима цикла этапов.
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

    def _resolve_generated_campaign_module(self, folder, name):
        """Разрешить generated-маршрут и остановить задачу при невалидной привязке."""

        try:
            return resolve_generated_campaign_module(folder, name)
        except EventCampaignSelectorError as error:
            logger.error_context(
                title='Не удалось безопасно разрешить карту generated-события',
                reason=str(error),
                impact=(
                    'Маршрутизация кампании остановлена; переход на случайную '
                    'legacy-карту запрещён.'
                ),
                action=(
                    'Проверьте Campaign.Event и перегенерируйте Event registry/artifact '
                    'из актуального source snapshot перед повторным запуском.'
                ),
                level=50,
            )
            raise RequestHumanTakeover from error

    def load_campaign(self, name, folder='campaign_main'):
        """Загрузить модуль карты кампании.

        Для generated-события канонический модуль разрешается через event registry
        до legacy-импорта. Исторический импорт остаётся fallback, если selector
        не закреплён за generated artifact.
        """
        route = getattr(self, '_campaign_load_route', None)
        if route is not None and route[:2] == (folder, name):
            generated_module = route[2]
        else:
            generated_module = self._resolve_generated_campaign_module(folder, name)
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
        """Проверить, сработало ли какое-либо условие остановки.

        Возвращает:
            bool: Сработало ли условие остановки.
        """
        # Ограничение числа запусков.
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
        # Ограничение уровня.
        if self.config.StopCondition_ReachLevel and self.campaign.config.LV_TRIGGERED:
            logger.hr(f'Сработало условие остановки: достигнут уровень {self.config.StopCondition_ReachLevel}')
            self.config.Scheduler_Enable = False
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config.config_name}>: кампания завершена",
                content=f"<{self.config.config_name}> {self.name}: достигнут лимит уровня"
            )
            return True
        # Ограничения ресурсов.
        if oil_check:
            # Проверить самоцветы.
            self.status_get_gems()
            # Проверить монеты.
            self.get_coin()
            if self.get_oil() < max(500, self.config.StopCondition_OilLimit):
                logger.hr('Сработало условие остановки: лимит нефти')
                self.config.task_delay(minute=(120, 240))
                return True
        # Ограничение монет.
        if oil_check and self.coin_limit_triggered():
            logger.hr('Сработало условие остановки: лимит монет')
            return True
        # Ограничение нефти в автоматическом поиске.
        if self.campaign.auto_search_oil_limit_triggered:
            logger.hr('Сработало условие остановки: лимит нефти в автопоиске')
            self.config.task_delay(minute=(120, 240))
            return True
        # Получение нового корабля.
        if self.config.StopCondition_GetNewShip and self.campaign.config.GET_SHIP_TRIGGERED:
            logger.hr('Сработало условие остановки: получен новый корабль')
            self.config.Scheduler_Enable = False
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config.config_name}>: кампания завершена",
                content=f"<{self.config.config_name}> {self.name}: получен новый корабль"
            )
            return True
        # Ограничение PT события.
        if oil_check and self.campaign.event_pt_limit_triggered():
            logger.hr('Сработало условие остановки: лимит очков события')
            return True
        # Балансировщик задач в автоматическом поиске.
        if self.config.TaskBalancer_Enable and self.campaign.auto_search_coin_limit_triggered:
            logger.hr('Сработало условие остановки: лимит монет в автопоиске')
            self.handle_task_balancer()
            return True
        # Обычный балансировщик задач.
        if oil_check and self.run_count >= 1:
            if self.config.TaskBalancer_Enable and self.triggered_task_balancer():
                logger.hr('Сработало условие остановки: лимит монет')
                self.handle_task_balancer()
                return True

        return False

    def _triggered_app_restart(self):
        """Проверить, требуется ли перезапуск приложения.

        Возвращает:
            bool: Требуется ли перезапуск приложения.
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
        """Нормализовать имя этапа кампании.

        У некоторых событий SP может иметь другое имя, например ``vsp`` или Muse SP.
        Для единообразного вызова соответствующий файл карты должен называться
        ``sp.py``.

        Аргументы:
            name (str): имя файла ``.py``.
            folder (str): каталог внутри ``campaign``.

        Возвращает:
            tuple[str, str]: нормализованные ``(name, folder)``.
        """
        name = to_map_file_name(name)
        # Специальная обработка d3-3 события event_20251218_cn.
        if folder == 'event_20251218_cn':
            # Преобразовать d3-3 в d3_3 для логики отступления после трёх боёв.
            if name == 'd3-3':
                name = 'd3_3'
                logger.info('[Кампания — запуск] Имя этапа d3-3 преобразовано в d3_3 (логика отступления после трёх боёв)')
            # d3 остаётся без изменений и использует стандартную логику.
            elif name == 'd3':
                logger.info('[Кампания — запуск] Для этапа d3 используется стандартная логика')
        # GemsFarming и ThreeOilLowCost автоматически выбирают событие или основную кампанию.
        if self.config.task.command in ['GemsFarming', 'ThreeOilLowCost']:
            if self.stage_is_main(name):
                logger.info(f'Этап {name} загружен из campaign_main')
                folder = 'campaign_main'
            else:
                folder = self.config.cross_get('GemsFarming.Campaign.Event')
                if folder is not None:
                    logger.info(f'Этап {name} загружен из события {folder}')
                else:
                    logger.warning('Не удалось определить последнее событие; используется campaign_main')
                    folder = 'campaign_main'
        # Нормализация специальных имён SP.
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
        # Преобразование в главы T.
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
            'war_archives_20230525_cn',
            'war_archives_20231026_cn',
            'war_archives_20240725_cn',
        ]:
            name = convert.get(name, name)
        # Преобразование между A/B/C/D и T/HT.
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
            # События с главами T.
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
            'war_archives_20230525_cn',
            'war_archives_20231026_cn',
            'war_archives_20240725_cn',
        ]:
            name = convert.get(name, name)
        else:
            reverse = {v: k for k, v in convert.items()}
            name = reverse.get(name, name)
        # Событие «Алхимик и таинственные острова»: исправление исторической опечатки.
        if folder == 'event_20221124_cn':
            name = name.replace('ht', 'th')
        # В главах TH отсутствуют map_percentage и 3_stars.
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
        # TSS-карта события event_20211125_cn является ограниченной по времени.
        if folder == 'event_20211125_cn' and 'tss' in name:
            self.config.override(
                StopCondition_OilLimit=0,  # Нефть не расходуется.
                StopCondition_MapAchievement='100_percent_clear',
                StopCondition_StageIncrease=True,
                Emotion_Mode='ignore',  # Настроение не расходуется.
                Fleet_Fleet2=0,  # Доступен только один флот.
                Submarine_Fleet=0,  # Подлодки недоступны.
            )
        # Сюжетное состояние события event_20230817_cn.
        if folder == 'event_20230817_cn':
            if name.startswith('e0'):
                name = 'a1'
        # В event_20240829_cn имя TP соответствует SP.
        if folder == 'event_20240829_cn':
            if name == 'tp':
                name = 'sp'
        # Цикл этапов.
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
                # Отключить непрерывную зачистку.
                logger.info('[Кампания — запуск] Непрерывная зачистка отключена')
                self.config.override(StopCondition_MapAchievement='non_stop')
                self.config.override(StopCondition_StageIncrease=False)
        # В hard-режиме при наличии файла использовать campaign_hard вместо campaign_main.
        if mode == 'hard' and folder == 'campaign_main' and name in map_files('campaign_hard'):
            folder = 'campaign_hard'
        # В event_20240912_cn нет индикатора «угроза: безопасно», поэтому нужен fallback MapAchievement.
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
        """Проверить, можно ли продолжить текущий автоматический поиск.

        Если меню автоматического поиска уже открыто, выполнен хотя бы один запуск
        и не задано условие достижения карты, повторный ensure_campaign_ui не нужен.

        Возвращает:
            bool: Можно ли продолжить автоматический поиск.
        """
        # В меню автоматического поиска нельзя обновить информацию о карте.
        # При заданном условии достижения карты продолжение запрещено.
        if self.config.StopCondition_MapAchievement != 'non_stop':
            return False

        return self.run_count > 0 and self.campaign.map_is_auto_search

    def after_campaign_run(self):
        """Расширяемый hook после завершения одного запуска кампании."""
        pass

    def handle_commission_notice(self):
        """Обработать уведомление о завершившейся комиссии.

        Если обнаружено уведомление, текущая задача останавливается и вызывается
        обработка комиссий.

        Исключения:
            TaskEnd: выбрасывается после обнаружения уведомления о комиссии.

        Страницы:
            вход: page_campaign
        """
        if self.config.is_task_enabled('Commission') and self.campaign.commission_notice_show_at_campaign():
            logger.info('[Кампания — запуск] Обнаружено уведомление о комиссии')
            self.config.task_call('Commission')
            self.config.task_stop('Обнаружено уведомление о комиссии')

    def run(self, name, folder='campaign_main', mode='normal', total=0):
        """Запустить задачу кампании для выбранной карты."""
        requested_name = to_map_file_name(name)
        generated_module = self._resolve_generated_campaign_module(
            folder,
            requested_name,
        )
        if generated_module is None:
            name, folder = self.handle_stage_name(requested_name, folder, mode=mode)
            generated_module = self._resolve_generated_campaign_module(folder, name)
        if generated_module is not None:
            name = generated_module.rsplit('.', 1)[-1]

        self.config.override(Campaign_Name=name, Campaign_Event=folder)
        self._campaign_load_route = (folder, name, generated_module)
        try:
            self.load_campaign(name, folder=folder)
        finally:
            del self._campaign_load_route
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