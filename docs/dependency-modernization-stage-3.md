# Модернизация зависимостей Stage 3

В Stage 3 обновлён основной набор Python-зависимостей и инструменты сборки. Для
воспроизводимости версии, выбранные для продукта, зафиксированы в
`pyproject.toml`, а полное разрешение — в `uv.lock`.

## Выполненная миграция

- `adbutils` обновлён до `1.2.15`, а `uiautomator2` — до последнего совместимого
  релиза ветки 2.x `2.16.26`.
- WebUI и MCP обновлены до `pywebio 1.8.4`, `starlette 1.6.0`, `anyio 4.15.1`,
  `aiofiles 25.1.0`, `uvicorn 0.52.4`, `rich 15.0.0` и `mcp 2.2.0`.
- OpenAI-клиент обновлён до `3.13.0`; OCR/CV и численный стек обновлён до
  согласованных релизов `numpy 2.5.3`, `scipy 1.18.1`, `numba 0.67.0`,
  `rapidocr 3.9.2` и `onnxruntime 1.30.0` с сохранением platform markers.
- Удалены неиспользуемые прямые зависимости `chardet`, `importlib-metadata`,
  `importlib-resources`, `packaging`, `retrying`, `setuptools`, `wrapt` и
  `pyzmq`. Транзитивные пакеты остаются в lockfile, если их используют
  `uiautomator2` или `zerorpc`.
- MCP/OpenAI и Starlette используют пакет `httpx2`; он явно закреплён в CI-группе,
  а remote MCP-тесты переведены на тот же API без скрытой транзитивной зависимости.
- `uv 0.12.13` согласован с CI, Windows bootstrap и Docker bootstrap image.

## Отложенная миграция

`uiautomator2 3.7.0` в эту волну не включён. Его новая модель выполнения удаляет
используемые проектом v2 API `u2.init.Initer`, `_Service`, ATX Agent HTTP
endpoints и методы `set_new_command_timeout`/`_get_atx_agent_url`. Эти API нужны
для локального `uiautomator2cache`, запуска `minitouch`, фонового запуска
`DroidCast` и текущих recovery-путей устройства. Переход на 3.x требует отдельной
device-runtime миграции с заменой этих путей и физическим Android smoke; простое
изменение pin нарушило бы управление устройством.

Изменение должно быть возобновлено отдельной задачей после подготовки нового
installer/runtime adapter и проверки USB, TCP, emulator и WindowsML матриц.

## Изменение Rich

Rich 15 больше не сохраняет вывод, записанный внутри `Console.capture()`, в
`record` buffer. WebUI traceback и RichLog теперь используют изолированный
`io.StringIO` как output stream и печатают renderable напрямую в record buffer.
Так сохраняются HTML-вывод, экранирование пользовательского текста и редактирование
секретов.
