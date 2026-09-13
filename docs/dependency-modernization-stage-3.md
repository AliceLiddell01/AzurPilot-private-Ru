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
  `module/ocr/rpc.py` напрямую импортирует `zmq` и использует его для
  loopback OCR transport с обработкой `zmq.error.ZMQError`.
- MCP/OpenAI и Starlette используют пакет `httpx2`; он явно закреплён в CI-группе,
  а remote MCP-тесты переведены на тот же API без скрытой транзитивной зависимости.
- `uv 0.12.13` согласован с CI, Windows bootstrap и Docker bootstrap image.

## Renovate safety contract

Сохранены `dependencyDashboard: true`, `automerge: false`,
`prConcurrentLimit: 4`, `prHourlyLimit: 2`, `commitHourlyLimit: 2`,
`separateMultipleMajor: false` и отдельный vulnerability path. Regression-тест
проверяет каждый из этих ограничителей, включая независимый commit budget.

## Device runtime contract

`uiautomator2` и `adbutils` используют собственные current resource APIs; проектный
`module/device/pkg_resources` shim и side-effect imports удалены после consumer
audit. Версии берутся через stdlib `importlib.metadata`, а старые патчи
`uiautomator2.init.Initer`, ATX Agent, minicap и `uiautomator2cache` удалены.

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
Для текущего Stage 4 отдельный длительный `alas` live run намеренно не
запускается до external ChatGPT review и повторной проверки exact head.

## Результат Stage 4: замена zerorpc

`zerorpc==0.6.3` удалён из runtime dependency и lockfile. `ModelProxy`/
`ModelProxyFactory` сохранили прежнюю границу методов, loopback-only адрес,
bounded timeout и локальный fallback, а `start_ocr_server` получил собственный
`pyzmq` ROUTER/DEALER transport. Управляющие кадры используют ограниченный JSON,
изображения и бинарные значения — безопасные bounded frames; pickle и публичные
или wildcard listeners не используются.

Для выбора транспорта сопоставлены loopback HTTP и stdlib process IPC. Прямой
`pyzmq` сохранён как минимальный вариант: он уже был direct runtime dependency,
сохраняет существующий endpoint и бинарный путь ndarray без нового HTTP-слоя
или pickle-based framing. Контракт ограничивает JSON control frame, бинарные
кадры, batch, nesting, timeout и lifecycle; rollback выполняется возвратом к
предыдущему commit без изменения `OcrClientAddress` и `OcrServerPort`.

Lockfile удаляет только цепочку, принадлежавшую `zerorpc`: `future`, `gevent`,
`msgpack`, `zope-event` и `zope-interface`; `pyzmq` остаётся прямой зависимостью.
Issue [#258](https://github.com/AliceLiddell01/AzurPilot-private-Ru/issues/258)
остаётся открытым до публикации и merge этого изменения, после чего его
acceptance matrix должна быть сверена с exact-head evidence.

## Acceptance state

Deterministic tests и source/runtime contracts не доказывают полную физическую
работу ADB. В этом checkout Dev MCP smoke не запускался: доступный Game MCP
контур не заменяет отдельный Dev MCP evidence. Реальный bounded device
acceptance выполнен на локальном ADB/TCP target с profile `alas`; полная
Windows/USB matrix и gameplay-сценарии не выполнялись. Канонический runner и
минимальный безопасный сценарий:

```powershell
uv run --locked --no-sync python -c "import adbutils, uiautomator2, zmq; from importlib import metadata; assert metadata.version('adbutils') == '2.12.0'; assert metadata.version('uiautomator2') == '3.7.0'; print('device imports: ok')"
uv run --locked --no-sync python -m tools.acceptance.device --profile alas --serial "<serial>" --check-preview --check-control --check-reconnect --report "<report-path>"
```

Перед runner следует проверить import/init без direct `setuptools` и убедиться,
что `serial` относится к ожидаемому Windows/MuMu target. Runner сам проверяет
`adb get-state`, Android boot readiness, package readiness, ADB shell и RGB
скриншот; дополнительные флаги проверяют preview, настроенный control backend
(`minitouch` handshake без касания), контрольный probe без игрового ввода и
target-explicit reconnect/recovery. Для ручного интерактивного запуска нужно убрать
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
