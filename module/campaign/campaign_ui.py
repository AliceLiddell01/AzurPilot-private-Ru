"""
Модуль UI-навигации и управления главами кампании.

Отвечает за операции с пользовательским интерфейсом экрана кампании, включая:
- Переключение глав (числовые главы, главы событий, главы SP)
- Переключение режимов кампании (обычный/сложный/EX)
- Адаптацию под различные версии интерфейса событий (раскладки 20241219, 20260326 и др.)
- Получение входов на этапы и навигацию по главам

Модуль использует ModeSwitch для определения и переключения режима кампании,
а также CampaignOcr для распознавания индекса главы через OCR.
"""

from module.base.timer import Timer
from module.base.utils import area_offset
from module.campaign.assets import *
from module.campaign.campaign_event import CampaignEvent
from module.campaign.campaign_ocr import CampaignOcr
from module.exception import CampaignEnd, CampaignNameError, ScriptEnd
from module.logger import logger
from module.map.assets import WITHDRAW
from module.map.map_operation import MapOperation
from module.ui.assets import CAMPAIGN_CHECK
from module.ui.switch import Switch


class ModeSwitch(Switch):
    """Переключатель режима кампании.

    Расширяет класс Switch, отслеживая аномальное появление кнопки WITHDRAW в процессе переключения.
    Если во время переключения режима неожиданно появляется кнопка отступления, это свидетельствует о некорректном
    состоянии карты, что вызывает CampaignNameError для восстановления после сбоя.
    """

    def handle_additional(self, main):
        if main.appear(WITHDRAW, offset=(30, 30)):
            logger.warning(f'Переключение режима: появилась кнопка отступления')
            raise CampaignNameError


MODE_SWITCH_1 = ModeSwitch('Mode_switch_1', offset=(30, 10))
MODE_SWITCH_1.add_state('normal', SWITCH_1_NORMAL)
MODE_SWITCH_1.add_state('hard', SWITCH_1_HARD)
MODE_SWITCH_2 = ModeSwitch('Mode_switch_2', offset=(30, 10))
MODE_SWITCH_2.add_state('hard', SWITCH_2_HARD)
MODE_SWITCH_2.add_state('ex', SWITCH_2_EX)

# Переключатель режима события изменён с версии 20240725 на 20241219
# С версии 20241219 интерфейс стал стабильнее, поэтому используем эту дату в имени
MODE_SWITCH_20241219 = ModeSwitch('Mode_switch_20241219', is_selector=True, offset=(30, 30))
MODE_SWITCH_20241219.add_state('combat', SWITCH_20241219_COMBAT)
MODE_SWITCH_20241219.add_state('story', SWITCH_20241219_STORY)
ASIDE_SWITCH_20241219 = ModeSwitch('Aside_switch_20241219', is_selector=True, offset=(20, 20))
ASIDE_SWITCH_20241219.add_state('part1', CHAPTER_20241219_PART1)
ASIDE_SWITCH_20241219.add_state('part2', CHAPTER_20241219_PART2)
ASIDE_SWITCH_20241219.add_state('sp', CHAPTER_20241219_SP)
ASIDE_SWITCH_20241219.add_state('ex', CHAPTER_20241219_EX)
# Сокращаем unknown_timer для более быстрой обработки
# Из-за бага игры боковой индикатор может исчезнуть после отступления или завершения кампании
ASIDE_SWITCH_20241219.set_unknown_timer = Timer(0.6, count=2)

ASIDE_SWITCH_20260326 = ModeSwitch('Aside_switch_20260326', is_selector=True, offset=(30, 30))
ASIDE_SWITCH_20260326.add_state('part1', CHAPTER_20260326_PART1)
ASIDE_SWITCH_20260326.add_state('sp', CHAPTER_20260326_SP)
ASIDE_SWITCH_20260326.set_unknown_timer = Timer(0.6, count=2)


def is_digit_chapter(chapter):
    """
    Определяет, является ли глава числовой.

    Args:
         chapter (int, str): Обозначение главы, например 7, 'd', 'sp'.

    Returns:
        bool: Является ли глава числовой.
    """
    if isinstance(chapter, int):
        return True
    try:
        return chapter[0].isdigit()
    except IndexError:
        return False


class CampaignUI(MapOperation, CampaignEvent, CampaignOcr):
    """Класс навигации и управления UI кампании.

    Предоставляет все необходимые операции с интерфейсом кампании, включая смену глав, смену режимов,
    получение входов на этапы и т. д. Поддерживает основную кампанию, события, главы SP и Военный архив.

    Объединяет функционал MapOperation (управление картой), CampaignEvent (проверка событий)
    и CampaignOcr (распознавание глав через OCR) в единую систему управления интерфейсом кампании.

    Является одним из базовых классов CampaignBase, обеспечивая поддержку уровня пользовательского интерфейса.

    Attributes:
        ENTRANCE (Button): Кнопка входа на текущий этап, устанавливается через `ensure_campaign_ui()`.
        stage_entrance (dict): Словарь сопоставления названий этапов с их кнопками входа,
            генерируемый сопоставлением шаблонов CampaignOcr.
        campaign_chapter (str): Идентификатор текущей главы, например '7', 'd', 'sp'.
    """
    ENTRANCE = Button(area=(), color=(), button=(), name='default_button')

    def campaign_ensure_chapter(self, chapter, skip_first_screenshot=True):
        """
        Гарантирует переключение на указанную главу.

        Args:
            chapter (int, str): Обозначение главы, например 7, 'd', 'sp'.
            skip_first_screenshot: Пропускать ли первый снимок экрана.
        """
        index = self._campaign_get_chapter_index(chapter)
        isdigit = is_digit_chapter(chapter)

        # Повторно используем логику ui_ensure_index.
        logger.hr("Проверка номера главы в UI")
        retry = Timer(1, count=2)
        error_confirm = Timer(0.2, count=0)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.handle_chapter_additional():
                continue

            current = self.get_chapter_index()
            current_isdigit = is_digit_chapter(self.campaign_chapter)

            logger.attr("Текущий номер", current)
            diff = index - current
            if diff == 0:
                break

            # При поиске D3 OCR может ошибочно распознать его как 3-7
            if not (isdigit == current_isdigit):
                continue

            # Из-за медленной анимации 14-4 может распознаться OCR как 4-1; нужно подтвердить, что это действительно 4-1
            if index >= 11 and index % 10 == current:
                error_confirm.start()
                if not error_confirm.reached():
                    continue
            else:
                error_confirm.reset()

            # Переключаем главу
            if retry.reached():
                button = CHAPTER_NEXT if diff > 0 else CHAPTER_PREV
                self.device.multi_click(button, n=abs(diff), interval=(0.2, 0.3))
                retry.reset()

    def handle_chapter_additional(self):
        """
        Дополнительная обработка при смене главы, вызываемая из campaign_ensure_chapter().

        Returns:
            bool: Была ли выполнена обработка.
        """
        return False

    def campaign_ensure_mode(self, mode='normal'):
        """
        Гарантирует переключение на указанный режим кампании.

        Args:
            mode (str): 'normal', 'hard', 'ex'.
        """
        if mode == 'hard':
            self.config.override(Campaign_Mode='hard')

        switch_2 = MODE_SWITCH_2.get(main=self)

        if switch_2 == 'unknown':
            if mode == 'ex':
                logger.warning('Запрошен переход в EX, но переключатель режима EX отсутствует')
            elif mode == 'normal':
                MODE_SWITCH_1.set('hard', main=self)
            elif mode == 'hard':
                MODE_SWITCH_1.set('normal', main=self)
            else:
                logger.warning(f'Неизвестный режим кампании: {mode}')
        else:
            if mode == 'ex':
                MODE_SWITCH_2.set('hard', main=self)
            elif mode == 'normal':
                MODE_SWITCH_2.set('ex', main=self)
                MODE_SWITCH_1.set('hard', main=self)
            elif mode == 'hard':
                MODE_SWITCH_2.set('ex', main=self)
                MODE_SWITCH_1.set('normal', main=self)
            else:
                logger.warning(f'Неизвестный режим кампании: {mode}')

    def campaign_ensure_mode_20241219(self, mode='combat'):
        """
        Гарантирует переключение на режим кампании версии 20241219.

        Args:
            mode (str): 'combat' или 'story'.
        """
        if mode in ['normal', 'hard', 'ex', 'combat']:
            MODE_SWITCH_20241219.set('combat', main=self)
        elif mode in ['story']:
            MODE_SWITCH_20241219.set('story', main=self)
        else:
            logger.warning(f'Неизвестный режим кампании: {mode}')

    def campaign_ensure_aside_20241219(self, chapter):
        """
        Гарантирует переключение на вкладку боковой панели версии 20241219.

        Args:
            chapter: 'part1', 'part2', 'sp', 'ex'.
        """
        if chapter in ['part1', 'a', 'c', 't']:
            ASIDE_SWITCH_20241219.set('part1', main=self)
        elif chapter in ['part2', 'b', 'd']:
            ASIDE_SWITCH_20241219.set('part2', main=self)
        elif chapter in ['sp', 'ex_sp']:
            ASIDE_SWITCH_20241219.set('sp', main=self)
        elif chapter in ['ex', 'ex_ex']:
            ASIDE_SWITCH_20241219.set('ex', main=self)
        else:
            logger.warning(f'Неизвестная вкладка кампании: {chapter}')

    def campaign_ensure_aside_20260326(self, chapter):
        """
        Гарантирует переключение на вкладку боковой панели версии 20260326.

        Args:
            chapter: 'part1', 'sp'.
        """
        if chapter in ['part1', 't', 'ht']:
            ASIDE_SWITCH_20260326.set('part1', main=self)
        elif chapter in ['sp', 'ex_sp']:
            ASIDE_SWITCH_20260326.set('sp', main=self)
        else:
            logger.warning(f'Неизвестная вкладка кампании: {chapter}')

    def campaign_get_mode_names(self, name):
        """
        Получает варианты названий этапа в обычном и сложном режимах.
        t1 -> [t1, ht1]
        ht1 -> [t1, ht1]
        a1 -> [a1, c1]

        Args:
            name (str): Название этапа.

        Returns:
            list[str]: Список названий этапа в обычном и сложном режимах.
        """
        if name.startswith('t'):
            return [f't{name[1:]}', f'ht{name[1:]}']
        if name.startswith('ht'):
            return [f't{name[2:]}', f'ht{name[2:]}']
        if name.startswith('a') or name.startswith('c'):
            return [f'a{name[1:]}', f'c{name[1:]}']
        if name.startswith('b') or name.startswith('d'):
            return [f'b{name[1:]}', f'd{name[1:]}']
        return [name]

    def _campaign_name_is_hard(self, name):
        """
        Повторно использует определение из campaign_get_mode_names() для проверки, является ли этап сложным.

        Args:
            name: 'a1', 'ht1', 'sp1'.

        Returns:
            bool: Является ли этап сложным режимом.
        """
        mode_names = self.campaign_get_mode_names(name)
        if len(mode_names) == 2 and mode_names[1] == name:
            return True
        else:
            return False

    def campaign_get_entrance(self, name):
        """
        Получает кнопку входа на этап.

        Args:
            name (str): Название этапа, например '7-2', 'd3', 'sp3'.

        Returns:
            Button: Кнопка входа на этап.
        """
        entrance_name = name
        # Особый случай: d3_3 использует в UI вход d3, но загружает другую боевую логику из d3_3.py
        search_name = name
        if name == 'd3_3':
            search_name = 'd3'
            logger.info(f'[Кампания — UI] Для этапа {name} в UI используется вход {search_name}')

        if self.config.MAP_HAS_MODE_SWITCH:
            for mode_name in self.campaign_get_mode_names(search_name):
                if mode_name in self.stage_entrance:
                    search_name = mode_name

        if search_name not in self.stage_entrance:
            logger.warning(f'Этап не найден: {search_name}')
            raise CampaignNameError

        entrance = self.stage_entrance[search_name]
        entrance.name = entrance_name
        return entrance

    def campaign_set_chapter_main(self, chapter, mode='normal'):
        """
        Устанавливает главу основной кампании.

        Переходит на страницу основной кампании и переключает на указанную главу и режим.

        Args:
            chapter (str): Идентификатор главы, например '7', '12'.
            mode (str): 'normal' или 'hard'.

        Returns:
            bool: True при успешной установке; False, если это не числовая глава основной кампании.
        """
        if chapter.isdigit():
            self.ui_goto_campaign()
            self.campaign_ensure_mode('normal')
            self.campaign_ensure_chapter(chapter)
            if mode == 'hard':
                self.campaign_ensure_mode('hard')
                # info_bar может показывать, что сложный режим этой карты ещё не открыт.
                # На английском сервере есть баг: HM12 отображается как закрытый, хотя фактически в него можно войти.
                self.handle_info_bar()
                self.campaign_ensure_chapter(chapter)
            return True
        else:
            return False

    def campaign_set_chapter_event(self, chapter, mode='normal'):
        """
        Устанавливает главу кампании события.

        Переходит на страницу кампании события и автоматически переключает режим согласно обозначению главы (a/b — обычный, c/d — сложный).

        Args:
            chapter (str): Идентификатор главы, например 'a', 'b', 'c', 'd', 'sp' и т. д.
            mode (str): 'normal' или 'hard'.

        Returns:
            bool: True при успешной установке; False, если это не глава события.
        """
        if chapter in ['a', 'b', 'c', 'd', 'ex_sp', 'as', 'bs', 'cs', 'ds', 't', 'ts', 'tss', 'ht', 'hts']:
            self.ui_goto_event()
            if chapter in ['a', 'b', 'as', 'bs', 't', 'ts', 'tss']:
                self.campaign_ensure_mode('normal')
            elif chapter in ['c', 'd', 'cs', 'ds', 'ht', 'hts']:
                self.campaign_ensure_mode('hard')
            elif chapter == 'ex_sp':
                self.campaign_ensure_mode('ex')
            self.campaign_ensure_chapter(chapter)
            return True
        else:
            return False

    def campaign_set_chapter_sp(self, chapter, mode='normal'):
        """
        Устанавливает главу SP.

        Переходит на страницу SP и выбирает главу SP.

        Args:
            chapter (str): Идентификатор главы, должен быть 'sp'.
            mode (str): 'normal' или 'hard' (не используется).

        Returns:
            bool: True при успешной установке; False, если это не глава SP.
        """
        if chapter == 'sp':
            self.ui_goto_sp()
            self.campaign_ensure_chapter(chapter)
            return True
        else:
            return False

    def campaign_set_chapter_20241219(self, chapter, stage, mode='combat'):
        """
        Устанавливает главу события версии 20241219.

        Обрабатывает раскладку интерфейса событий, ставшую стабильной с декабря 2024 года, с поддержкой конфигураций боковой панели:
        - MAP_CHAPTER_SWITCH_20241219: стандартные четыре зоны (part1/part2/sp/ex)
        - MAP_CHAPTER_SWITCH_20241219_SP: упрощённая раскладка SP
        - MAP_CHAPTER_SWITCH_20241219_SPEX: раскладка SP с зоной EX

        Args:
            chapter (str): Идентификатор главы, например 'a', 'b', 'sp', 'ex_sp' и т. д.
            stage (str): Номер этапа, например '1', '2'.
            mode (str): 'combat' или 'story'.

        Returns:
            bool: True при успешной установке; False, если версия не подходит.
        """
        if self.config.MAP_CHAPTER_SWITCH_20241219:
            if self._campaign_name_is_hard(f'{chapter}{stage}'):
                self.config.override(Campaign_Mode='hard')
            # part1、part2、sp、ex
            if mode == 'story':
                self.campaign_ensure_mode_20241219('story')
                return True
            if chapter in ['a', 'c', 't']:
                self.ui_goto_event()
                self.campaign_ensure_mode_20241219('combat')
                self.campaign_ensure_aside_20241219('part1')
                self.campaign_ensure_chapter(chapter)
                return True
            if chapter in ['b', 'd', 'ttl']:
                self.ui_goto_event()
                self.campaign_ensure_mode_20241219('combat')
                self.campaign_ensure_aside_20241219('part2')
                self.campaign_ensure_chapter(chapter)
                return True
            if chapter in ['ex_sp']:
                self.ui_goto_event()
                self.campaign_ensure_mode_20241219('combat')
                self.campaign_ensure_aside_20241219('sp')
                self.campaign_ensure_chapter(chapter)
                return True
            # Некоторые события называют обычные этапы SP1/SP2...
            # Направляем их на page_event и сохраняем боковую панель по умолчанию.
            if chapter in ['sp']:
                self.ui_goto_event()
                self.campaign_ensure_mode_20241219('combat')
                self.campaign_ensure_chapter(chapter)
                return True
            if chapter in ['ex_ex']:
                self.ui_goto_event()
                self.campaign_ensure_mode_20241219('combat')
                self.campaign_ensure_aside_20241219('ex')
                self.campaign_ensure_chapter(chapter)
                return True
        if self.config.MAP_CHAPTER_SWITCH_20241219_SP:
            if self._campaign_name_is_hard(f'{chapter}{stage}'):
                self.config.override(Campaign_Mode='hard')
            # (пусто), normal, sp, (пусто)
            if chapter in ['sp', 't', 'ht']:
                self.ui_goto_event()
                self.campaign_ensure_mode_20241219('combat')
                # normal расположен на позиции part2
                self.campaign_ensure_aside_20241219('part2')
                self.campaign_ensure_chapter(chapter)
                return True
            if chapter in ['ex_sp']:
                self.ui_goto_event()
                self.campaign_ensure_mode_20241219('combat')
                self.campaign_ensure_aside_20241219('sp')
                self.campaign_ensure_chapter(chapter)
                return True
        if self.config.MAP_CHAPTER_SWITCH_20241219_SPEX:
            if self._campaign_name_is_hard(f'{chapter}{stage}'):
                self.config.override(Campaign_Mode='hard')
            # normal、sp、ex
            try:
                ASIDE_SWITCH_20241219.offset = area_offset((-20, -20, 20, 20), (0, -37))
                if chapter in ['sp', 't', 'ht']:
                    self.ui_goto_event()
                    self.campaign_ensure_mode_20241219('combat')
                    # normal расположен на позиции part2
                    self.campaign_ensure_aside_20241219('part2')
                    self.campaign_ensure_chapter(chapter)
                    return True
                if chapter in ['ex_sp']:
                    self.ui_goto_event()
                    self.campaign_ensure_mode_20241219('combat')
                    self.campaign_ensure_aside_20241219('sp')
                    self.campaign_ensure_chapter(chapter)
                    return True
                if chapter in ['ex_sp']:
                    self.ui_goto_event()
                    self.campaign_ensure_mode_20241219('combat')
                    self.campaign_ensure_aside_20241219('sp')
                    self.campaign_ensure_chapter(chapter)
                    return True
            finally:
                ASIDE_SWITCH_20241219.offset = (20, 20)
        return False

    def campaign_set_chapter_20260326(self, chapter, stage, mode='combat'):
        """
        Устанавливает главу события версии 20260326.

        Обрабатывает раскладку интерфейса событий версии марта 2026 года, где боковая панель разделена на part1 и sp.

        Args:
            chapter (str): Идентификатор главы, например 't', 'ht', 'ex_sp'.
            stage (str): Номер этапа.
            mode (str): 'combat' или 'story'.

        Returns:
            bool: True при успешной установке; False, если версия не подходит.
        """
        if self.config.MAP_CHAPTER_SWITCH_20260326:
            if self._campaign_name_is_hard(f'{chapter}{stage}'):
                self.config.override(Campaign_Mode='hard')
            if mode == 'story':
                self.campaign_ensure_mode_20241219('story')
                return True
            if chapter in ['t', 'ht']:
                self.ui_goto_event()
                self.campaign_ensure_mode_20241219('combat')
                self.campaign_ensure_aside_20260326('part1')
                self.campaign_ensure_chapter(chapter)
                return True
            if chapter in ['ex_sp']:
                self.ui_goto_event()
                self.campaign_ensure_mode_20241219('combat')
                self.campaign_ensure_aside_20260326('sp')
                self.campaign_ensure_chapter(chapter)
                return True
        return False

    def campaign_set_chapter(self, name, mode='normal'):
        """
        Устанавливает главу кампании.

        Args:
            name (str): Название этапа, например '7-2', 'd3', 'sp3'.
            mode (str): 'normal' или 'hard'.
        """
        # Особый случай: d3_3 использует d3 при навигации по главам
        chapter_name = name
        if name == 'd3_3':
            chapter_name = 'd3'
        
        chapter, stage = self._campaign_separate_name(chapter_name)

        if self.campaign_set_chapter_main(chapter, mode):
            pass
        elif self.campaign_set_chapter_20260326(chapter, stage, mode):
            pass
        elif self.campaign_set_chapter_20241219(chapter, stage, mode):
            pass
        elif self.campaign_set_chapter_event(chapter, mode):
            pass
        elif self.campaign_set_chapter_sp(chapter, mode):
            pass
        else:
            logger.warning(f'[Кампания — UI] Неизвестная глава кампании: {name}')

    def handle_campaign_ui_additional(self):
        """
        Дополнительная обработка пользовательского интерфейса кампании.

        Returns:
            bool: Была ли выполнена обработка.
        """
        if self.appear(WITHDRAW, offset=(30, 30)):
            # logger.info("发现 WITHDRAW 按钮，等待地图加载完成以防止游戏客户端 bug")
            self.ensure_no_info_bar(timeout=2)
            try:
                self.withdraw()
            except CampaignEnd:
                pass
            return True
        return False

    def ensure_campaign_ui(self, name, mode='normal', skip_first_screenshot=True):
        """
        Гарантирует переход в интерфейс указанного этапа кампании.

        Args:
            name (str): Название этапа, например '7-2', 'd3', 'sp3'.
            mode (str): 'normal' или 'hard'.
            skip_first_screenshot: Пропускать ли первый снимок экрана.

        Raises:
            ScriptEnd: Выбрасывается, если после всех попыток не удалось перейти к этапу.
        """
        timeout = Timer(5, count=20).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if timeout.reached():
                break
            try:
                self.campaign_set_chapter(name, mode)
                self.ENTRANCE = self.campaign_get_entrance(name=name)
                return True
            except CampaignNameError:
                pass

            if self.handle_campaign_ui_additional():
                continue

        logger.warning('[Кампания] Ошибка имени кампании')
        raise ScriptEnd('Campaign name error')

    def commission_notice_show_at_campaign(self):
        """
        Проверяет, отображается ли в интерфейсе кампании уведомление о завершении заказов.

        Returns:
            bool: Завершён ли какой-либо заказ.
        """
        return self.appear(CAMPAIGN_CHECK, offset=(20, 20)) and self.appear(COMMISSION_NOTICE_AT_CAMPAIGN)
