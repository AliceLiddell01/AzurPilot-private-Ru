# Модернизация зависимостей Stage 3

В Stage 3 обновлён основной набор Python-зависимостей и инструменты сборки. Для
воспроизводимости версии, выбранные для продукта, зафиксированы в
`pyproject.toml`, а полное разрешение — в `uv.lock`.

## Выполненная миграция

- `adbutils` обновлён до `2.12.0`, а `uiautomator2` — до `3.7.0`.
- `uiautomator2cache` удалён из direct dependencies. Локальный runtime использует
  встроенный `u2.jar` и прямой ADB transport; HTTP phone-cloud сохраняет отдельный
  адаптер для старого endpoint-контракта.
- WebUI и MCP обновлены до `pywebio 1.8.4`, `starlette 1.6.0`, `anyio 4.15.1`,
  `aiofiles 25.1.0`, `uvicorn 0.52.4`, `rich 15.0.0` и `mcp 2.2.0`.
- OpenAI-клиент обновлён до `3.13.0`; OCR/CV и численный стек обновлён до
  согласованных релизов `numpy 2.5.3`, `scipy 1.18.1`, `numba 0.67.0`,
  `rapidocr 3.9.2` и `onnxruntime 1.30.0` с сохранением platform markers.
- Удалены неиспользуемые прямые зависимости `chardet`, `importlib-metadata`,
  `importlib-resources`, `packaging`, `retrying`, `setuptools` и `wrapt`.
  `pyzmq==27.2.0` сохранён как direct runtime dependency: production-код
  `module/ocr/rpc.py` напрямую импортирует `zmq` и обрабатывает
  `zmq.error.ZMQError`.
- MCP/OpenAI и Starlette используют пакет `httpx2`; он явно закреплён в CI-группе,
  а remote MCP-тесты переведены на тот же API без скрытой транзитивной зависимости.
- `uv 0.12.13` согласован с CI, Windows bootstrap и Docker bootstrap image.

## Renovate safety contract

Сохранены `dependencyDashboard: true`, `automerge: false`,
`prConcurrentLimit: 4`, `prHourlyLimit: 2`, `commitHourlyLimit: 2`,
`separateMultipleMajor: true` и отдельный vulnerability path. Regression-тест
проверяет каждый из этих ограничителей, включая независимый commit budget.

## Device runtime contract

`module/device/pkg_resources` остаётся узким compatibility boundary для metadata
и resources `adbutils`/`uiautomator2`: версии берутся через stdlib
`importlib.metadata`, а пути — через установленную distribution metadata. Старые
патчи `uiautomator2.init.Initer`, ATX Agent, minicap и `uiautomator2cache` удалены.

Локальный transport создаётся через `u2.connect_usb(self.adb)`. Server 3.x
самостоятельно подготавливает встроенный `u2.jar`; `minitouch` запускается
явно из уже закэшированного device binary перед ADB forward. HTTP-сценарий
использует `HttpUiautomator2`, который сохраняет JSON-RPC, shell, screenshot,
service и input операции без локального ADB fallback.

## Результат миграции uiautomator2

Переход на 3.x выполнен вместе с обновлением `adbutils`, lockfile и runtime
adapter. Контрактные тесты проверяют отсутствие legacy installer/cache API,
прямое USB-подключение, RGB screenshot и сохранение HTTP ветки. Issue
[#257](https://github.com/AliceLiddell01/AzurPilot-private-Ru/issues/257)
остаётся открытым до публикации и merge этого изменения, после чего его
acceptance matrix должна быть сверена с exact-head evidence. В текущем checkout
проверены локальный ADB target, TCP reconnect, selector/input API, RGB
screenshots, `minitouch` handshake и uiautomator2 control; отдельные USB,
MuMu и полные gameplay-сценарии этим bounded запуском не доказаны.

## Решение по zerorpc

`zerorpc==0.6.3` остаётся реально используемой runtime dependency OCR RPC.
`ModelProxy`/`ModelProxyFactory` используют loopback client с bounded timeout и
локальным fallback, а `start_ocr_server` — server lifecycle и
`zmq.error.ZMQError`. Lockfile подтверждает coupling `zerorpc → pyzmq`.

PyPI публикует `0.6.3` с 2019 года, при этом upstream repository содержит более
поздние изменения; это release lag и bounded legacy-risk, а не доказательство
полного abandoned status. Большая замена transport не входит в Stage 3.
Evidence-based audit, варианты loopback HTTP/process IPC, migration surface и
acceptance зафиксированы в issue
[#258](https://github.com/AliceLiddell01/AzurPilot-private-Ru/issues/258). Issue
остаётся открытым: этот Stage 3 PR не меняет transport и не объявляет
`zerorpc` заменённым. Закрытие требует отдельного adapter/replacement,
измерений, lifecycle- и malformed-payload acceptance.

## Acceptance state

Deterministic tests и source/runtime contracts не доказывают полную физическую
работу ADB. В этом checkout Dev MCP smoke не запускался: доступный Game MCP
контур не заменяет отдельный Dev MCP evidence. Реальный bounded device
acceptance выполнен на локальном ADB/TCP target с profile `ap`; полный
Windows/MuMu/USB gate и gameplay-сценарии не выполнялись. Состояние внешнего
gate: **PENDING EXTERNAL ACCEPTANCE — Windows/MuMu/ADB smoke**. Перед переводом
PR из Draft нужен контролируемый полный smoke на exact head. Канонический
runner и минимальный безопасный сценарий:

```powershell
uv run --locked --no-sync python -c "import module.device.pkg_resources; import adbutils, uiautomator2, zmq, zerorpc; from importlib import metadata; from module.device.pkg_resources import get_distribution, resource_filename; assert get_distribution('adbutils').version == metadata.version('adbutils'); assert get_distribution('uiautomator2').version == metadata.version('uiautomator2'); assert resource_filename('adbutils', 'binaries'); print('device imports: ok')"
uv run --locked --no-sync python tools/acceptance/device.py --profile alas --serial "<serial>" --check-preview --check-control --check-reconnect --non-interactive --report "<report-path>"
```

Перед runner следует проверить import/init без direct `setuptools` и убедиться,
что `serial` относится к ожидаемому Windows/MuMu target. Runner сам проверяет
`adb get-state`, Android boot readiness, package readiness, ADB shell и RGB
скриншот; дополнительные флаги проверяют preview, настроенный control backend
(`minitouch` handshake без касания), безопасный `KEYCODE_BACK` и target-explicit
reconnect/recovery. Для ручного интерактивного запуска нужно убрать
`--non-interactive` и подтвердить каждый шаг. Установка APK, очистка app data,
покупки, бой, task queue, clipboard, пользовательский текст и `adb kill-server`
runner'ом запрещены. В отчёт нельзя включать реальные serial, credentials или
локальные секреты.

Физический device gate остаётся внешним ограничением: deterministic tests и
Dev MCP не заменяют Windows/MuMu/ADB smoke. Issue
[#204](https://github.com/AliceLiddell01/AzurPilot-private-Ru/issues/204)
остаётся открытым до Renovate reconciliation на последнем merged head; до
разрешённого merge состояние ветки не объявляется глобально завершённым.

## Изменение Rich

Rich 15 больше не сохраняет вывод, записанный внутри `Console.capture()`, в
`record` buffer. WebUI traceback и RichLog теперь используют изолированный
`io.StringIO` как output stream и печатают renderable напрямую в record buffer.
Так сохраняются HTML-вывод, экранирование пользовательского текста и редактирование
секретов.
