"""Модуль обновления системы конфигурации.

Ключевой движок конфигурационной системы, отвечающий за:
- Чтение файлов определения YAML (task.yaml, argument.yaml, override.yaml, default.yaml)
- Генерацию классов конфигурации Python (config_generated.py)
- Генерацию файлов описания параметров (args.json, menu.json)
- Генерацию файлов интернационализации (i18n/*.json)
- Генерацию шаблона конфигурации (template.json)
- Обработку миграций и перенаправлений версий конфигурации
- Управление обновлениями данных событий и этапов

Конвейер генерации конфигурации:
    task.yaml + argument.yaml + override.yaml + default.yaml + gui.yaml
    → args.json (полные объединённые определения параметров)
    → menu.json (структура меню)
    → config_generated.py (Python-классы конфигурации)
    → template.json (шаблон конфигурации)
    → i18n/ru-RU.json (единственный активный runtime locale)

Вызов через командную строку:
    uv run -m module.config.config_updater

Основные классы:
- ConfigUpdater: базовый класс обновления и генерации конфигурации
- Event: парсер данных событий
- CampaignEvent: управление конфигурацией событий кампаний
"""

import json
import re
import typing as t
from copy import deepcopy

from deploy.utils import DEPLOY_TEMPLATE, poor_yaml_read, poor_yaml_write
from module.base.decorator import cached_property
from module.base.timer import timer
from module.config.deep import deep_default, deep_get, deep_iter, deep_set
from module.config.locale import EVENT_NAME_FALLBACK_ORDER, EVENT_NAME_SOURCE, UI_LOCALE
from module.config.env import IS_ON_PHONE_CLOUD
from module.config.server import (
    GLOBAL_PACKAGE,
    VALID_CHANNEL_PACKAGE,
    VALID_PACKAGE,
    VALID_SERVER_LIST,
    to_package,
    to_server,
)
from module.config.task_priority import get_scheduler_tasks, merge_task_priority
from module.config.utils import *
from module.config.redirect_utils.utils import *

# Шаблон заголовка config_generated.py
CONFIG_IMPORT = '''
# 此文件是配置系统的更新器。
# 负责读取配置定义、生成 config_generated.py 以及处理配置的版本迁移、i18n 生成等核心管理任务。
import datetime

# 此文件由 module/config/config_updater.py 自动生成。
# 请勿手动修改。


class GeneratedConfig:
    """
    自动生成的配置类
    """
'''.strip().split('\n')
ARCHIVES_PREFIX = {
    'en': 'archives ',
}
MAINS = ['Main', 'Main2', 'Main3']
EVENTS = ['Event', 'Event2', 'Event3', 'EventA', 'EventB', 'EventC', 'EventD', 'EventSp']
GEMS_FARMINGS = ['GemsFarming', 'ThreeOilLowCost']
RAIDS = ['Raid', 'RaidDaily', 'RaidScuttle']
WAR_ARCHIVES = ['WarArchives']
COALITIONS = ['Coalition', 'CoalitionSp', 'CoalitionScuttle']
MARITIME_ESCORTS = ['MaritimeEscort']
HOSPITAL = ['Hospital', 'HospitalEvent']


def fleet_autoscan_mode_to_scheduler_enable(value):
    """Однократно перенести legacy FleetAutoScan.Mode в Scheduler.Enable."""

    if value == 'disabled':
        return False
    if value in {'every_start', 'daily'}:
        return True
    raise ValueError('FleetAutoScan.Mode содержит неподдерживаемое значение')


def fleet_autoscan_fleets_redirect(value):
    """Нормализовать legacy selection перед переносом в новый task path."""

    if not isinstance(value, (list, tuple)):
        raise ValueError('FleetAutoScan.Fleets должен быть списком индексов')
    if not value or any(
        not isinstance(index, int)
        or isinstance(index, bool)
        or index not in range(1, 7)
        for index in value
    ):
        raise ValueError('FleetAutoScan.Fleets содержит недопустимый индекс')
    return sorted(set(value))


class Event:
    """Класс разбора данных события.

    Разбирает сведения о событии из campaign/Readme.md, включая:
    - date: дата события
    - directory: имя каталога события (например, 'event_20230101_cn')
    - name: английское название события
    - cn/en/jp/tw: названия события на соответствующих серверах

    Атрибуты:
        is_war_archives (bool): является ли событием архива боевых действий
        is_raid (bool): является ли рейдовым событием
        is_coalition (bool): является ли событием совместной операции/коллаборации
    """

    def __init__(self, text):
        self.date, self.directory, self.name, self.cn, self.en, self.jp, self.tw \
            = [x.strip() for x in text.strip('| \n').split('|')]

        self.directory = self.directory.replace(' ', '_')
        self.cn = self.cn.replace('、', '')
        self.en = self.en.replace(',', '').replace('\'', '').replace('\\', '')
        self.jp = self.jp.replace('、', '')
        self.tw = self.tw.replace('、', '')
        self.is_war_archives = self.directory.startswith('war_archives')
        self.is_raid = self.directory.startswith('raid_')
        self.is_coalition = self.directory.startswith('coalition_')
        for server in ARCHIVES_PREFIX.keys():
            if self.__getattribute__(server) == '-':
                self.__setattr__(server, None)
            else:
                if self.is_war_archives:
                    self.__setattr__(server, ARCHIVES_PREFIX[server] + self.__getattribute__(server))

    def __str__(self):
        return self.directory

    def __eq__(self, other):
        return str(self) == str(other)

    def __lt__(self, other):
        return str(self) < str(other)

    def __hash__(self):
        return hash(str(self))


class ConfigGenerator:
    @cached_property
    def argument(self):
        """Загрузить argument.yaml и стандартизировать его структуру.

        Формат данных::

            <group>:
                <argument>:
                    type: checkbox|select|textarea|input
                    value:
                    option (Optional): список вариантов, если у параметра есть выбор.
                    validate (Optional): datetime
        """
        data = {}
        raw = read_file(filepath_argument('argument'))
        filtered_raw = {k: v for k, v in raw.items() if not k.startswith('_')}
        for path, value in deep_iter(filtered_raw, depth=2):
            arg = {
                'type': 'input',
                'value': '',
                # option
            }
            if not isinstance(value, dict):
                value = {'value': value}
            arg['type'] = data_to_type(value, arg=path[1])
            if isinstance(value['value'], datetime):
                arg['type'] = 'datetime'
                arg['validate'] = 'datetime'
            # Ручные определения имеют наивысший приоритет
            arg.update(value)
            deep_set(data, keys=path, value=arg)

        # Определение группы Storage
        arg = {
            'type': 'storage',
            'value': {},
            'valuetype': 'ignore',
            'display': 'disabled',
        }
        deep_set(data, keys=['Storage', 'Storage'], value=arg)
        return data

    @cached_property
    def task(self):
        """Загрузить файл определения задач task.yaml.

        Формат данных::

            <task_group>:
                <task>:
                    <group>:
        """
        return read_file(filepath_argument('task'))

    @cached_property
    def default(self):
        """Загрузить файл значений задач по умолчанию default.yaml.

        Формат данных::

            <task>:
                <group>:
                    <argument>: value
        """
        return read_file(filepath_argument('default'))

    @cached_property
    def override(self):
        """Загрузить файл неизменяемых переопределений override.yaml.

        Формат данных::

            <task>:
                <group>:
                    <argument>: value
        """
        return read_file(filepath_argument('override'))

    @cached_property
    def gui(self):
        """Загрузить файл определений ключей интерфейса GUI gui.yaml.

        Формат данных::

            <i18n_group>:
                <i18n_key>: value, value is None
        """
        return read_file(filepath_argument('gui'))

    @cached_property
    def dashboard(self):
        """Загрузить файл определения ресурсов панели управления dashboard.yaml.

        Формат данных::

            <dashboard>
              - <group>
        """
        return read_file(filepath_argument('dashboard'))


    @cached_property
    @timer
    def args(self):
        """
        Объединить несколько файлов определений в стандартизированный JSON.

            task.yaml ---+
        argument.yaml ---+-----> args.json
        override.yaml ---+
         default.yaml ---+

        """
        # Построение args
        data = {}
        # Добавление дашборда в args
        dashboard_and_task = {**self.task, **self.dashboard}
        for path, groups in deep_iter(dashboard_and_task, min_depth=1, depth=3):
            if 'tasks' not in path and 'Dashboard' not in path:
                continue
            task = path[2] if 'tasks' in path else path[0]
            # Добавление группы Storage для всех задач
            groups.append('Storage')
            for group in groups:
                if group not in self.argument:
                    print(f'`{task}.{group}` не связан ни с одной группой аргументов')
                    continue
                deep_set(data, keys=[task, group], value=deepcopy(self.argument[group]))

        def check_override(path, value):
            # Проверка существования параметра (пропуск при отсутствии)
            old = deep_get(data, keys=path, default=None)
            if old is None:
                print(f'Аргумент `{".".join(path)}` не существует')
                return False
            # Проверка совпадения типов (но допускаются различия типа `Interval`)
            old_value = old.get('value', None) if isinstance(old, dict) else old
            value = old.get('value', None) if isinstance(value, dict) else value
            if type(value) != type(old_value) \
                    and old_value is not None \
                    and path[2] not in ['SuccessInterval', 'FailureInterval']:
                print(
                    f'Тип `{value}` ({type(value)}) не совпадает с типом `{".".join(path)}` ({type(old_value)})')
                return False
            # Проверка, входит ли значение опции в список допустимых
            if isinstance(old, dict) and 'option' in old:
                if value not in old['option']:
                    print(f'`{value}` не является допустимым значением аргумента `{".".join(path)}`')
                    return False
            return True

        # Установка значений по умолчанию
        for p, v in deep_iter(self.default, depth=3):
            if not check_override(p, v):
                continue
            deep_set(data, keys=p + ['value'], value=v)
        # Переопределение неизменяемых параметров
        for p, v in deep_iter(self.override, depth=3):
            if not check_override(p, v):
                continue
            if isinstance(v, dict):
                typ = v.get('type')
                if typ == 'state':
                    pass
                elif typ == 'lock':
                    pass
                elif deep_get(v, keys='value') is not None:
                    deep_default(v, keys='display', value='hide')
                for arg_k, arg_v in v.items():
                    deep_set(data, keys=p + [arg_k], value=arg_v)
            else:
                deep_set(data, keys=p + ['value'], value=v)
                deep_set(data, keys=p + ['display'], value='hide')
        # Установка команды задачи
        for path, groups in deep_iter(self.task, depth=3):
            if 'tasks' not in path:
                continue
            task = path[2]
            if deep_get(data, keys=f'{task}.Scheduler.Command'):
                deep_set(data, keys=f'{task}.Scheduler.Command.value', value=task)
                deep_set(data, keys=f'{task}.Scheduler.Command.display', value='hide')

        # Для задач не основной кампании скрываем Campaign.Mode (Mode применим только к картам кампании)
        for task in list(data.keys()):
            if task not in MAINS:
                if deep_get(data, keys=f'{task}.Campaign.Mode') is not None:
                    deep_set(data, keys=f'{task}.Campaign.Mode.display', value='hide')

        return data

    @timer
    def generate_code(self):
        """
        Сгенерировать config_generated.py на основе args.json.

        args.json ---> config_generated.py

        """
        visited_group = set()
        visited_path = set()
        lines = CONFIG_IMPORT
        for path, data in deep_iter(self.argument, depth=2):
            group, arg = path
            if group not in visited_group:
                lines.append('')
                lines.append(f'    # 配置组 `{group}`')
                visited_group.add(group)

            option = ''
            if 'option' in data and data['option']:
                option = '  # ' + ', '.join([str(opt) for opt in data['option']])
            path = '.'.join(path)
            lines.append(f'    {path_to_arg(path)} = {repr(parse_value(data["value"], data=data))}{option}')
            visited_path.add(path)

        with open(filepath_code(), 'w', encoding='utf-8', newline='') as f:
            for text in lines:
                f.write(text + '\n')

    @timer
    def generate_i18n(self):
        """
        Загрузить старый файл перевода и сгенерировать новый.

                     args.json ---+-----> i18n/<lang>.json
        (old) i18n/<lang>.json ---+

        """
        lang = UI_LOCALE
        new = {}
        old = read_file(filepath_i18n(UI_LOCALE))

        def deep_load(keys, default=True, words=('name', 'help')):
            for word in words:
                k = keys + [str(word)]
                d = ".".join(k) if default else str(word)
                v = deep_get(old, keys=k, default=d)
                deep_set(new, keys=k, value=v)

        # Перевод меню
        for path, data in deep_iter(self.task, depth=3):
            if 'tasks' not in path:
                continue
            task_group, _, task = path
            if task_group != 'Dashboard':
                deep_load(['Menu', task_group])
                deep_load(['Task', task])
        # Перевод аргументов
        visited_group = set()
        dashboard_args = deep_get(read_file(filepath_argument("task")), 'Dashboard.tasks.Dashboard', default=[])
        for path, data in deep_iter(self.argument, depth=2):
            if path[0] not in dashboard_args:
                if path[0] not in visited_group:
                    deep_load([path[0], '_info'])
                    visited_group.add(path[0])
                deep_load(path)
            if 'option' in data:
                deep_load(path, words=data['option'], default=False)
        # Названия событий выбираются по серверным metadata, а не по UI locale.
        events = {}
        ordered_sources = (EVENT_NAME_SOURCE,) + tuple(
            source for source in EVENT_NAME_FALLBACK_ORDER
            if source != EVENT_NAME_SOURCE
        )
        for server in ordered_sources:
            for event in self.event:
                name = event.__getattribute__(server)
                if name:
                    deep_default(events, keys=event.directory, value=name)
        for event in sorted(self.event):
            name = events.get(event.directory, event.directory)
            deep_set(new, keys=f'Campaign.Event.{event.directory}', value=name)
        # Перевод имен пакетов
        for package, server in VALID_PACKAGE.items():
            path = ['Emulator', 'PackageName', package]
            if deep_get(new, keys=path) == package:
                deep_set(new, keys=path, value=server.upper())

        for package, server_and_channel in VALID_CHANNEL_PACKAGE.items():
            server, channel = server_and_channel
            name = deep_get(new, keys=['Emulator', 'PackageName', to_package(server)])
            value = f'{name} · канал {channel} · {package}'
            deep_set(new, keys=['Emulator', 'PackageName', package], value=value)
        # Имена игровых серверов
        for server, _list in VALID_SERVER_LIST.items():
            for index in range(len(_list)):
                path = ['Emulator', 'ServerName', f'{server}-{index}']
                prefix = server.split('_')[0].upper()
                prefix = '国服' if prefix == 'CN' else prefix
                deep_set(new, keys=path, value=f'[{prefix}] {_list[index]}')
        # Перевод интерфейса GUI
        for path, _ in deep_iter(self.gui, depth=2):
            group, key = path
            deep_load(keys=['Gui', group], words=(key,))
        content = json.dumps(new, indent=2, ensure_ascii=False, sort_keys=False, default=str)
        atomic_write(filepath_i18n(UI_LOCALE), content + '\n')

    @cached_property
    def menu(self):
        """
        Сгенерировать menu.json на основе task.yaml.

        task.yaml --> menu.json

        """
        data = {}
        for task_group in self.task.keys():
            if task_group != 'Dashboard':
                value = deep_get(self.task, keys=[task_group, 'menu'])
                if value not in ['collapse', 'list']:
                    value = 'collapse'
                deep_set(data, keys=[task_group, 'menu'], value=value)
                value = deep_get(self.task, keys=[task_group, 'page'])
                if value not in ['setting', 'tool']:
                    value = 'setting'
                deep_set(data, keys=[task_group, 'page'], value=value)
                tasks = deep_get(self.task, keys=[task_group, 'tasks'], default={})
                tasks = list(tasks.keys())
                deep_set(data, keys=[task_group, 'tasks'], value=tasks)

        return data

    @cached_property
    @timer
    def event(self):
        """
        Returns:
            list[Event]: Список событий, отсортированный от новых к старым.
        """

        def calc_width(text):
            return len(text) + len(re.findall(
                r'[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff、！（）]', text))

        lines = []
        data_lines = []
        data_widths = []
        column_width = [4] * 7  # `:---`
        events = []
        with open('./campaign/Readme.md', encoding='utf-8') as f:
            for text in f.readlines():
                if not re.search(r'^\|.+\|$', text):
                    # not a table line
                    lines.append(text)
                elif re.search(r'^.*\-{3,}.*$', text):
                    # is a delimiter line
                    continue
                else:
                    line_entries = [x.strip() for x in text.strip('| \n').split('|')]
                    data_lines.append(line_entries)
                    data_width = [calc_width(string) for string in line_entries]
                    data_widths.append(data_width)
                    column_width = [max(l1, l2) for l1, l2 in zip(column_width, data_width)]
                    if re.search(r'\d{8}', text):
                        event = Event(text)
                        events.append(event)
        for i, (line, old_width) in enumerate(zip(data_lines, data_widths)):
            lines.append('| ' + ' | '.join([cell + ' ' * (width - length) for cell, width, length in zip(line, column_width, old_width)]) + ' |\n')
            if i == 0:
                lines.append('| ' + ' | '.join([':' + '-' * (width - 1) for width in column_width]) + ' |\n')
        with open('./campaign/Readme.md', 'w', encoding='utf-8') as f:
            f.writelines(lines)
        return events[::-1]

    def insert_event(self):
        """
        Вставить информацию о событиях в `self.args`.

        ./campaign/Readme.md -----+
                                  v
                   args.json -----+-----> args.json
        """
        for server in ARCHIVES_PREFIX.keys():
            for event in self.event:
                name = event.__getattribute__(server)

                def insert(key):
                    opts = deep_get(self.args, keys=f'{key}.Campaign.Event.option_{server}', default=[])
                    if event not in opts:
                        opts.append(event)
                    deep_set(self.args, keys=f'{key}.Campaign.Event.option_{server}', value=opts)

                if name:
                    if event.is_raid:
                        if not hasattr(self, f'_{server}_latest_raid_date'):
                            setattr(self, f'_{server}_latest_raid_date', int(event.date))
                        if int(event.date) == getattr(self, f'_{server}_latest_raid_date'):
                            for task in RAIDS:
                                insert(task)
                    elif event.is_war_archives:
                        for task in WAR_ARCHIVES:
                            insert(task)
                    elif event.is_coalition:
                        if not hasattr(self, f'_{server}_latest_coalition_date'):
                            setattr(self, f'_{server}_latest_coalition_date', int(event.date))
                        if int(event.date) == getattr(self, f'_{server}_latest_coalition_date'):
                            for task in COALITIONS:
                                insert(task)
                    else:
                        if not hasattr(self, f'_{server}_latest_event_date'):
                            setattr(self, f'_{server}_latest_event_date', int(event.date))
                        if int(event.date) == getattr(self, f'_{server}_latest_event_date'):
                            for task in EVENTS + GEMS_FARMINGS:
                                insert(task)

        for task in EVENTS + GEMS_FARMINGS + WAR_ARCHIVES + RAIDS + COALITIONS:
            latest = {}
            for server in ARCHIVES_PREFIX.keys():
                latest[server] = deep_get(self.args, keys=f'{task}.Campaign.Event.option_{server}', default=[])
            options = set().union(*latest.values())
            options = sorted([option for option in options if option != 'campaign_main'])
            if task not in WAR_ARCHIVES:
                deep_set(self.args, keys=f'{task}.Campaign.Event.option_bold', value=options)
            deep_set(self.args, keys=f'{task}.Campaign.Event.option', value=options)
            deep_set(self.args, keys=f'{task}.Campaign.Event.option_bold', value=options)

    @staticmethod
    def generate_deploy_template():
        template = poor_yaml_read(DEPLOY_TEMPLATE)
        template['Language'] = UI_LOCALE
        aidlux = {
            'GitExecutable': './.venv/bin/git',
            'PythonExecutable': './.venv/bin/python',
            'AdbExecutable': './.venv/bin/adb',
        }

        docker = {
            'GitExecutable': './.venv/bin/git',
            'PythonExecutable': './.venv/bin/python',
            'AdbExecutable': './.venv/bin/adb',
        }

        linux = {
            'GitExecutable': './.venv/bin/git',
            'PythonExecutable': './.venv/bin/python',
            'AdbExecutable': './.venv/bin/adb',
            'SSHExecutable': '/usr/bin/ssh',
            'ReplaceAdb': 'false'
        }

        def update(suffix, *args):
            file = f'./config/deploy.{suffix}.yaml'
            new = deepcopy(template)
            for dic in args:
                new.update(dic)
            poor_yaml_write(data=new, file=file)

        update('template')
        update('template-AidLux', aidlux)
        update('template-docker', docker)
        update('template-linux', linux)

    def insert_package(self):
        option = deep_get(self.argument, keys='Emulator.PackageName.option')
        option += list(VALID_PACKAGE.keys())
        option += list(VALID_CHANNEL_PACKAGE.keys())
        deep_set(self.argument, keys='Emulator.PackageName.option', value=option)
        deep_set(self.args, keys='Alas.Emulator.PackageName.option', value=option)

    def insert_server(self):
        option = deep_get(self.argument, keys='Emulator.ServerName.option')
        server_list = []
        for server, _list in VALID_SERVER_LIST.items():
            for index in range(len(_list)):
                server_list.append(f'{server}-{index}')
        option += server_list
        deep_set(self.argument, keys='Emulator.ServerName.option', value=option)
        deep_set(self.args, keys='Alas.Emulator.ServerName.option', value=option)

    @timer
    def generate(self):
        _ = self.args
        _ = self.menu
        _ = self.event
        self.insert_event()
        self.insert_package()
        self.insert_server()
        write_file(filepath_args(), self.args)
        write_file(filepath_args('menu'), self.menu)
        self.generate_code()
        self.generate_i18n()
        self.generate_deploy_template()


class ConfigUpdater:
    # Формат: source, target, (опционально) convert_func
    redirection = [
        (
            'Alas.FleetAutoScan.Mode',
            'FleetAutoScan.Scheduler.Enable',
            fleet_autoscan_mode_to_scheduler_enable,
        ),
        (
            'Alas.FleetAutoScan.Fleets',
            'FleetAutoScan.FleetAutoScan.Fleets',
            fleet_autoscan_fleets_redirect,
        ),
        # ('OpsiDaily.OpsiDaily.BuySupply', 'OpsiShop.Scheduler.Enable'),
        # ('OpsiDaily.Scheduler.Enable', 'OpsiDaily.OpsiDaily.DoMission'),
        # ('OpsiShop.Scheduler.Enable', 'OpsiShop.OpsiShop.BuySupply'),
        # ('ShopOnce.GuildShop.Filter', 'ShopOnce.GuildShop.Filter', bp_redirect),
        # ('ShopOnce.MedalShop2.Filter', 'ShopOnce.MedalShop2.Filter', bp_redirect),
        # (('Alas.DropRecord.SaveResearch', 'Alas.DropRecord.UploadResearch'),
        #  'Alas.DropRecord.ResearchRecord', upload_redirect),
        # (('Alas.DropRecord.SaveCommission', 'Alas.DropRecord.UploadCommission'),
        #  'Alas.DropRecord.CommissionRecord', upload_redirect),
        # (('Alas.DropRecord.SaveOpsi', 'Alas.DropRecord.UploadOpsi'),
        #  'Alas.DropRecord.OpsiRecord', upload_redirect),
        # (('Alas.DropRecord.SaveMeowfficerTalent', 'Alas.DropRecord.UploadMeowfficerTalent'),
        #  'Alas.DropRecord.MeowfficerTalent', upload_redirect),
        # ('Alas.DropRecord.SaveCombat', 'Alas.DropRecord.CombatRecord', upload_redirect),
        # ('Alas.DropRecord.SaveMeowfficer', 'Alas.DropRecord.MeowfficerBuy', upload_redirect),
        # ('Alas.Emulator.PackageName', 'Alas.DropRecord.API', api_redirect),
        # ('Alas.RestartEmulator.Enable', 'Alas.RestartEmulator.ErrorRestart'),
        # ('OpsiGeneral.OpsiGeneral.BuyActionPoint', 'OpsiGeneral.OpsiGeneral.BuyActionPointLimit', action_point_redirect),
        # ('BattlePass.BattlePass.BattlePassReward', 'Freebies.BattlePass.Collect'),
        # ('DataKey.Scheduler.Enable', 'Freebies.DataKey.Collect'),
        # ('DataKey.DataKey.ForceGet', 'Freebies.DataKey.ForceCollect'),
        # ('SupplyPack.SupplyPack.WeeklyFreeSupplyPack', 'Freebies.SupplyPack.Collect'),
        # ('Commission.Commission.CommissionFilter', 'Commission.Commission.CustomFilter'),
        # 2023.02.17
        # ('OpsiAshBeacon.OpsiDossierBeacon.Enable', 'OpsiAshBeacon.OpsiAshBeacon.AttackMode', dossier_redirect),
        # ('General.Retirement.EnhanceFavourite', 'General.Enhance.ShipToEnhance', enhance_favourite_redirect),
        # ('General.Retirement.EnhanceFilter', 'General.Enhance.Filter'),
        # ('General.Retirement.EnhanceCheckPerCategory', 'General.Enhance.CheckPerCategory', enhance_check_redirect),
        # ('General.Retirement.OldRetireN', 'General.OldRetire.N'),
        # ('General.Retirement.OldRetireR', 'General.OldRetire.R'),
        # ('General.Retirement.OldRetireSR', 'General.OldRetire.SR'),
        # ('General.Retirement.OldRetireSSR', 'General.OldRetire.SSR'),
        # (('GemsFarming.GemsFarming.FlagshipChange', 'GemsFarming.GemsFarming.FlagshipEquipChange'),
        #  'GemsFarming.GemsFarming.ChangeFlagship',
        #  change_ship_redirect),
        # (('GemsFarming.GemsFarming.VanguardChange', 'GemsFarming.GemsFarming.VanguardEquipChange'),
        #  'GemsFarming.GemsFarming.ChangeVanguard',
        #  change_ship_redirect),
        # ('Alas.DropRecord.API', 'Alas.DropRecord.API', api_redirect2)
        # 2025.04.17
        # ('Coalition.Coalition.Mode', 'Coalition.Coalition.Mode', coalition_to_frostfall),
        # 2025.06.26
        # ('Coalition.Coalition.Mode', 'Coalition.Coalition.Mode', coalition_to_little_academy),
    ]
    redirection += [
        (f'{task}.GemsFarming.ALLowHighFlagshipLevel', f'{task}.GemsFarming.AllowHighFlagshipLevel')
        for task in [*GEMS_FARMINGS, 'Ambush11']
    ]
    redirection += [
        (f'{task}.GemsFarming.ALLowLowVanguardLevel', f'{task}.GemsFarming.AllowLowVanguardLevel')
        for task in [*GEMS_FARMINGS, 'Ambush11']
    ]

    # redirection += [
    #     (
    #         (f'{task}.Emotion.CalculateEmotion', f'{task}.Emotion.IgnoreLowEmotionWarn'),
    #         f'{task}.Emotion.Mode',
    #         emotion_mode_redirect
    #     ) for task in [
    #         'Main', 'Main2', 'Main3', 'GemsFarming',
    #         'Event', 'Event2', 'EventA', 'EventB', 'EventC', 'EventD', 'EventSp', 'Raid', 'RaidDaily',
    #         'Sos', 'WarArchives',
    #     ]
    # ]

    @cached_property
    def args(self):
        return read_file(filepath_args())

    def config_update(self, old, is_template=False):
        """
        Args:
            old: Словарь старой конфигурации.
            is_template: Является ли конфигурация шаблоном.

        Returns:
            Обновлённый словарь конфигурации.
        """
        new = {}

        for keys, data in deep_iter(self.args, depth=3):
            # Пропуск не-словарей (листовые значения: строки, числа и т. д.)
            if not isinstance(data, dict):
                continue
            missing = object()
            value = deep_get(old, keys=keys, default=missing)
            if value is missing:
                value = data['value']
            elif data.get('strict') and (value is None or value == ''):
                raise ValueError(f'Параметр {keys} не может быть пустым')
            typ = data['type']
            display = data.get('display')
            value_empty = value == '' and not data.get('preserve_empty')
            if is_template or value is None or value_empty \
                    or typ in ['lock', 'state'] or (display == 'hide' and typ != 'stored'):
                value = data['value']
            value = parse_value(value, data=data)
            deep_set(new, keys=keys, value=value)

        # Обновление до последнего события
        server = to_server(
            deep_get(new, 'Alas.Emulator.PackageName', GLOBAL_PACKAGE)
        )
        if not is_template:
            for task in EVENTS + RAIDS + COALITIONS:
                opts = deep_get(self.args, keys=f'{task}.Campaign.Event.option_{server}', default=[])
                if opts and not deep_get(new, keys=f'{task}.Campaign.Event', default='campaign_main') in opts:
                    deep_set(new,
                             keys=f'{task}.Campaign.Event',
                             value=opts[0])

            for task in ['GemsFarming']:
                opts = deep_get(self.args, keys=f'{task}.Campaign.Event.option_{server}', default=[])
                if opts and deep_get(new, keys=f'{task}.Campaign.Event', default='campaign_main') not in opts:
                    deep_set(new,
                             keys=f'{task}.Campaign.Event',
                             value=opts[0])
        # В архивах боевых действий нельзя выбирать campaign_main
        for task in WAR_ARCHIVES:
            opts = deep_get(self.args, keys=f'{task}.Campaign.Event.option_{server}', default=[])
            if opts and deep_get(new, keys=f'{task}.Campaign.Event', default='campaign_main') == 'campaign_main':
                deep_set(new,
                          keys=f'{task}.Campaign.Event',
                          value=opts[0])

        # В событии не допускается уровень 12-4 по умолчанию
        def default_stage(t, stage):
            if deep_get(new, keys=f'{t}.Campaign.Name', default='12-4') in ['7-2', '12-4']:
                deep_set(new, keys=f'{t}.Campaign.Name', value=stage)

        for task in EVENTS + WAR_ARCHIVES:
            default_stage(task, 'D3')
        for task in COALITIONS:
            default_stage(task, 'TC-3')

        # Задачи коллабораций используют унифицированные названия уровней: простой, обычный, сложный.
        # Устаревшие TC-1/2/3 мигрируют при загрузке, событие Frostfall переводит их обратно в runtime.
        if not is_template:
            for task in COALITIONS:
                stage_key = f'{task}.Coalition.Mode'
                stage = deep_get(new, keys=stage_key)
                stage = coalition_to_little_academy(stage)
                deep_set(new, keys=stage_key, value=stage)

        if not is_template:
            missing = object()
            legacy_fleet_autoscan_mode = deep_get(
                old,
                'Alas.FleetAutoScan.Mode',
                default=missing,
            )
            if (
                    legacy_fleet_autoscan_mode is not missing
                    and (
                        legacy_fleet_autoscan_mode is None
                        or legacy_fleet_autoscan_mode == ''
                    )
            ):
                fleet_autoscan_mode_to_scheduler_enable(legacy_fleet_autoscan_mode)
            new = self.config_redirect(old, new)
            old_priority = deep_get(old, 'General.YukikazeTaskManager.TaskPriorityAdjustment')
            new_priority = deep_get(new, 'General.YukikazeTaskManager.TaskPriorityAdjustment')
            template_priority = deep_get(
                self.args, 'General.YukikazeTaskManager.TaskPriorityAdjustment.value'
            )
            if (
                    isinstance(old_priority, str)
                    and 'OpsiScheduling' not in old_priority
                    and isinstance(new_priority, str)
                    and new_priority == old_priority
                    and old_priority.replace(
                        '> OpsiCrossMonth\n> Commission > Tactical > Research',
                        '> OpsiCrossMonth\n> OpsiScheduling\n> Commission > Tactical > Research',
                    ) == template_priority
            ):
                deep_set(new, 'General.YukikazeTaskManager.TaskPriorityAdjustment', template_priority)
            else:
                deep_set(
                    new,
                    'General.YukikazeTaskManager.TaskPriorityAdjustment',
                    merge_task_priority(new_priority, template_priority, get_scheduler_tasks(self.args)),
                )
        new = self._override(new)

        return new

    def config_redirect(self, old, new):
        """
        Преобразовать старую конфигурацию в новый формат.

        Args:
            old: Словарь старой конфигурации.
            new: Словарь новой конфигурации.

        Returns:
            Преобразованный словарь конфигурации.
        """
        for row in self.redirection:
            if len(row) == 2:
                source, target = row
                update_func = None
            elif len(row) == 3:
                source, target, update_func = row
            else:
                continue

            if isinstance(source, tuple):
                value = []
                error = False
                for attribute in source:
                    tmp = deep_get(old, keys=attribute)
                    if tmp is None:
                        error = True
                        continue
                    value.append(tmp)
                if error:
                    continue
            else:
                value = deep_get(old, keys=source)
                if value is None:
                    continue

            if update_func is not None:
                value = update_func(value)

            if isinstance(target, tuple):
                for k, v in zip(target, value):
                    # Разрешено обновление одинаковых ключей
                    if (deep_get(old, keys=k) is None) or (source == target):
                        deep_set(new, keys=k, value=v)
            elif (deep_get(old, keys=target) is None) or (source == target):
                deep_set(new, keys=target, value=value)

        return new

    def _override(self, data):
        def remove_drop_save(key):
            value = deep_get(data, keys=key, default='do_not')
            if value == 'save_and_upload':
                value = 'upload'
                deep_set(data, keys=key, value=value)
            elif value == 'save':
                value = 'do_not'
                deep_set(data, keys=key, value=value)

        if IS_ON_PHONE_CLOUD:
            deep_set(data, 'Alas.Emulator.Serial', '127.0.0.1:5555')
            deep_set(data, 'Alas.Emulator.ScreenshotMethod', 'DroidCast_raw')
            deep_set(data, 'Alas.Emulator.ControlMethod', 'MaaTouch')
            for arg in deep_get(self.args, keys='Alas.DropRecord', default={}).keys():
                remove_drop_save(arg)

        return data

    def save_callback(self, key: str, value: t.Any) -> t.Iterable[t.Tuple[str, t.Any]]:
        """
        Функция обратного вызова при сохранении конфигурации для связанного обновления параметров.

        Args:
            key: Путь ключа в JSON конфигурации, например "Main.Emotion.Fleet1Value".
            value: Заданное пользователем значение, например "98".

        Yields:
            str: Путь ключа в JSON конфигурации для обновления, например "Main.Emotion.Fleet1Record".
            any: Устанавливаемое значение, например "2020-01-01 00:00:00".
        """
        if "Emotion" in key and "Value" in key:
            key = key.split(".")
            key[-1] = key[-1].replace("Value", "Record")
            yield ".".join(key), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Двусторонняя синхронизация умного расписания и конфигурации Corrosion 1
        # При изменении запаса монет в умном расписании синхронизируем с Corrosion 1
        if key == 'OpsiScheduling.OpsiScheduling.OperationCoinsPreserve':
            yield 'OpsiHazard1Leveling.OpsiHazard1Leveling.OperationCoinsPreserve', value
        # При изменении запаса монет в Corrosion 1 синхронизируем с умным расписанием
        elif key == 'OpsiHazard1Leveling.OpsiHazard1Leveling.OperationCoinsPreserve':
            yield 'OpsiScheduling.OpsiScheduling.OperationCoinsPreserve', value
        
        # Примечание: динамическое обновление выпадающего меню доступно только в pywebio > 1.8.0
        # elif key == 'Alas.Emulator.ScreenshotMethod' and value == 'nemu_ipc':
        #     yield 'Alas.Emulator.ControlMethod', 'nemu_ipc'
        # elif key == 'Alas.Emulator.ControlMethod' and value == 'nemu_ipc':
        #     yield 'Alas.Emulator.ScreenshotMethod', 'nemu_ipc'

    def read_file(self, config_name, is_template=False):
        """
        Прочитать и обновить файл конфигурации.

        Args:
            config_name: Имя файла конфигурации, соответствующее ./config/{file}.json.
            is_template: Является ли конфигурация шаблоном.

        Returns:
            Обновлённый словарь конфигурации.
        """
        old = read_file(filepath_config(config_name))
        new = self.config_update(old, is_template=is_template)
        # Обновленная конфигурация не записывается в файл: запись закомментирована для производительности
        # self.write_file(config_name, new)
        return new

    @staticmethod
    def write_file(config_name, data, mod_name='alas'):
        """
        Записать файл конфигурации.

        Args:
            config_name: Имя файла конфигурации, соответствующее ./config/{file}.json.
            data: Записываемые данные конфигурации.
            mod_name: Имя модуля, по умолчанию 'alas'.
        """
        write_file(filepath_config(config_name, mod_name), data)

    @timer
    def update_file(self, config_name, is_template=False):
        """
        Прочитать, обновить и записать файл конфигурации.

        Args:
            config_name: Имя файла конфигурации, соответствующее ./config/{file}.json.
            is_template: Является ли конфигурация шаблоном.

        Returns:
            Обновлённый словарь конфигурации.
        """
        data = self.read_file(config_name, is_template=is_template)
        self.write_file(config_name, data)
        return data


if __name__ == '__main__':
    """
    Выполнить полный цикл генерации конфигурации.

                 task.yaml -+----------------> menu.json
             argument.yaml -+-> args.json ---> config_generated.py
             override.yaml -+       |
                  gui.yaml --------\\|
                                   ||
    (old) i18n/<lang>.json --------\\========> i18n/<lang>.json
    (old)    template.json ---------\\========> template.json
    """
    # Убеждаемся, что запуск выполняется из корня Alas
    import os

    os.chdir(os.path.join(os.path.dirname(__file__), '../../'))

    ConfigGenerator().generate()
    ConfigUpdater().update_file('template', is_template=True)
