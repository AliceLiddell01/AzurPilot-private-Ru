"""Редактор единственного активного каталога ``ru-RU``."""

# Редактор работает только с активным каталогом ru-RU.
from pywebio.input import (actions, checkbox, input, input_group, input_update,
                           select)
from pywebio.output import put_buttons, put_markdown
from pywebio.session import defer_call, hold, run_js, set_env

import module.webui.lang as lang
from module.config.deep import deep_get, deep_iter, deep_set
from module.config.locale import UI_LOCALE
from module.config.utils import filepath_i18n, read_file, write_file


def translate():
    """
    启动 AzurPilot 翻译编辑器。

    Предоставляет интерфейс для последовательного редактирования каталога ru-RU.
    """
    set_env(output_animation=False)
    run_js(r"""$('head').append('<style>footer {display: none}</style>')""")

    put_markdown("""
        # Редактор русской локализации
        Нажмите `Enter`, чтобы сохранить строку и перейти дальше.
    """)

    dict_lang = {UI_LOCALE: read_file(filepath_i18n(UI_LOCALE))}
    modified = {UI_LOCALE: {}}

    list_path = []  # 完整路径，如 Menu.Task.name
    list_group = []  # 一级键（菜单分组）
    list_arg = []    # 二级键（任务名）
    list_key = []    # 三级键（字段名）
    for L, _ in deep_iter(dict_lang[UI_LOCALE], depth=3):
        list_path.append('.'.join(L))
        list_group.append(L[0])
        list_arg.append(L[1])
        list_key.append(L[2])
    total = len(list_path)

    class V:
        lang = UI_LOCALE
        untranslated_only = False
        clear = False

        idx = -1
        group = ''
        group_idx = 0
        groups = list(dict_lang[UI_LOCALE].keys())
        arg = ''
        arg_idx = 0
        args = []
        key = ''
        key_idx = 0
        keys = []

    def update_var(group=None, arg=None, key=None):
        if group:
            V.group = group
            V.idx = list_group.index(group)
            V.group_idx = V.idx
            V.arg = list_arg[V.idx]
            V.arg_idx = V.idx
            V.args = list(dict_lang[UI_LOCALE][V.group].keys())
            V.key = list_key[V.idx]
            V.key_idx = V.idx
            V.keys = list(dict_lang[UI_LOCALE][V.group][V.arg].keys())
        elif arg:
            V.arg = arg
            V.idx = list_arg.index(arg, V.group_idx)
            V.arg_idx = V.idx
            V.args = list(dict_lang[UI_LOCALE][V.group].keys())
            V.key = list_key[V.idx]
            V.key_idx = V.idx
            V.keys = list(dict_lang[UI_LOCALE][V.group][V.arg].keys())
        elif key:
            V.key = key
            V.idx = list_key.index(key, V.arg_idx)
            V.key_idx = V.idx
            V.keys = list(dict_lang[UI_LOCALE][V.group][V.arg].keys())

        update_form()

    def next_key():
        if V.idx + 1 > total:
            V.idx = -1

        V.idx += 1

        if V.untranslated_only:
            while True:
                # 调试：打印当前索引
                key = deep_get(dict_lang[V.lang], list_path[V.idx])
                if list_path[V.idx] == key or list_path[V.idx].split('.')[2] == key:
                    break
                else:
                    V.idx += 1
                if V.idx + 1 > total:
                    V.idx = 0
                    break

        (V.group, V.arg, V.key) = tuple(list_path[V.idx].split('.'))
        V.group_idx = list_group.index(V.group)
        V.arg_idx = list_arg.index(V.arg, V.group_idx)
        V.args = list(dict_lang[UI_LOCALE][V.group].keys())
        V.key_idx = list_key.index(V.key, V.arg_idx)
        V.keys = list(dict_lang[UI_LOCALE][V.group][V.arg].keys())

    def update_form():
        input_update('arg', options=V.args, value=V.arg)
        input_update('key', options=V.keys, value=V.key)
        input_update(UI_LOCALE, value=deep_get(
            dict_lang[UI_LOCALE], f'{V.group}.{V.arg}.{V.key}', 'Ключ не найден'))

        old = deep_get(dict_lang[V.lang],
                       f'{V.group}.{V.arg}.{V.key}', 'Ключ не найден')
        input_update(V.lang,
                     value=None if V.clear else old,
                     help_text=f'{V.group}.{V.arg}.{V.key}',
                     placeholder=old,
                     )

    def get_inputs():
        out = []
        old = deep_get(dict_lang[V.lang],
                       f'{V.group}.{V.arg}.{V.key}', 'Ключ не найден')
        out.append(
            input(
                name=V.lang,
                label=V.lang,
                value=None if V.clear else old,
                help_text=f'{V.group}.{V.arg}.{V.key}',
                placeholder=old,
            )
        )
        out.append(
            select(name='group', label='Group', options=V.groups, value=V.group,
                   onchange=lambda g: update_var(group=g), required=True)
        )
        out.append(
            select(name='arg', label='Arg', options=V.args, value=V.arg,
                   onchange=lambda a: update_var(arg=a), required=True)
        )
        out.append(
            select(name='key', label='Key', options=V.keys, value=V.key,
                   onchange=lambda k: update_var(key=k), required=True)
        )
        out.append(
            actions(name='action', buttons=[
                {"label": "Далее", "value": 'Next',
                    "type": "submit", "color": "success"},
                {"label": "Пропустить", "value": 'Skip',
                    "type": "submit", "color": "secondary"},
                {"label": "Сохранить", "value": "Submit",
                    "type": "submit", "color": "primary"},
                {"label": "Выйти и сохранить", "type": "cancel", "color": "secondary"},
            ])
        )

        return out

    def save():
        data = read_file(filepath_i18n(UI_LOCALE))
        for key, value in modified[UI_LOCALE].items():
            deep_set(data, key, value)
        write_file(filepath_i18n(UI_LOCALE), data)
    defer_call(save)

    def loop():
        while True:
            data = input_group(inputs=get_inputs())
            if data is None:
                save()
                break

            if data['action'] == 'Next':

                modified[V.lang][f'{V.group}.{V.arg}.{V.key}'] = data[V.lang].replace(
                    "\\n", "\n")
                deep_set(dict_lang[V.lang], f'{V.group}.{V.arg}.{V.key}', data[V.lang].replace(
                    "\\n", "\n"))
                next_key()
            if data['action'] == 'Skip':
                next_key()
            elif data['action'] == 'Submit':

                modified[V.lang][f'{V.group}.{V.arg}.{V.key}'] = data[V.lang].replace(
                    "\\n", "\n")
                deep_set(dict_lang[V.lang], f'{V.group}.{V.arg}.{V.key}', data[V.lang].replace(
                    "\\n", "\n"))
                continue

    def setting():
        data = input_group(inputs=[
            checkbox(name='check', label='Настройки', options=[
                {"label": 'Кнопка «Далее» показывает только непереведённые ключи',
                    'value': 'untranslated', 'selected': V.untranslated_only},
                {"label": 'Не подставлять прежнее значение в поле выбранного языка',
                 "value": "clear", "selected": V.clear}
            ])
        ])
        V.untranslated_only = True if 'untranslated' in data['check'] else False
        V.clear = True if 'clear' in data['check'] else False

    put_buttons([
        {"label": "Начать", "value": "start"},
        {"label": "Настройки", "value": "setting"}
    ], onclick=[loop, setting])
    next_key()
    setting()
    hold()
