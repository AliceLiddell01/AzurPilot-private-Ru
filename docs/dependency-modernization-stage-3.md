# Модернизация зависимостей Stage 3

В Stage 3 обновлён основной набор Python-зависимостей и инструменты сборки. Для
воспроизводимости версии, выбранные для продукта, зафиксированы в
`pyproject.toml`, а полное разрешение — в `uv.lock`.

## Выполненная миграция

- `adbutils` обновлён до `1.2.15`, а `uiautomator2` — до последнего совместимого
  релиза ветки 2.x `2.16.26`.
- `uiautomator2cache` остаётся direct dependency на exact pin `0.3.1`; device
  compatibility contract сохраняет `adbutils` на major line 1.x и
  `uiautomator2` на major line 2.x до отдельной миграции.
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

## Device compatibility shim

`module/device/pkg_resources` остаётся узким compatibility boundary для
`adbutils 1.x` и `uiautomator2 2.x`: их текущие модули используют
`pkg_resources.resource_filename` и `pkg_resources.get_distribution` при
импорте. Shim получает версии через stdlib `importlib.metadata`, а путь к
`adbutils/binaries` — через установленную distribution metadata, поэтому не
содержит fallback literals сегодняшних pins. После миграции на uiautomator2 3.x
необходимость shim и всех distributed side-effect imports нужно проверить и
удалить, если причина исчезнет.

## Отложенная миграция

`uiautomator2 3.7.0` в эту волну не включён. Renovate PR
[#248](https://github.com/AliceLiddell01/AzurPilot-private-Ru/pull/248) показывает
исходный major update, но простой bump затрагивает используемые проектом v2 API
`u2.init.Initer`, `_Service`, ATX Agent HTTP endpoints и методы
`set_new_command_timeout`/`_get_atx_agent_url`. Эти API нужны для локального
`uiautomator2cache`, запуска `minitouch`, фонового запуска `DroidCast` и текущих
recovery-путей устройства. Отдельный план миграции и acceptance matrix ведётся
в issue [#257](https://github.com/AliceLiddell01/AzurPilot-private-Ru/issues/257).

Переход на 3.x требует нового device-runtime adapter и физического Android
smoke; простое изменение pin нарушило бы управление устройством.

Изменение должно быть возобновлено отдельной задачей после подготовки нового
installer/runtime adapter и проверки USB, TCP, emulator и WindowsML матриц.

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
[#258](https://github.com/AliceLiddell01/AzurPilot-private-Ru/issues/258).

## Acceptance state

Deterministic tests и source/runtime contracts не доказывают полную физическую
работу ADB. Штатный Dev MCP target сейчас доступен: Universal Smoke Harness
выполнил bounded run на clean source snapshot, profile `ap`; `DEV_SMOKE_PASS`,
все 5 assertions прошли, `resources` подтверждены в `before` и `final`, cleanup
и source snapshot подтверждены. Immutable run id, spec hash и exact head
зафиксированы в PR description. Этот результат доказывает запуск текущего Dev
Runtime и typed MCP evidence, но не заменяет полный device acceptance по USB,
TCP/emulator, MuMu, selector/input, screenshot/BGR, minitouch и reconnect.
Поэтому внешний gate остаётся в состоянии **PENDING EXTERNAL ACCEPTANCE —
Windows/MuMu/ADB smoke**. Перед переводом PR из Draft нужен контролируемый
полный smoke на точном head. Канонический runner и минимальный безопасный
сценарий:

```powershell
uv run --locked --no-sync python -c "import module.device.pkg_resources; import adbutils, uiautomator2, zmq, zerorpc; from importlib import metadata; from module.device.pkg_resources import get_distribution, resource_filename; assert get_distribution('adbutils').version == metadata.version('adbutils'); assert get_distribution('uiautomator2').version == metadata.version('uiautomator2'); assert resource_filename('adbutils', 'binaries'); print('device imports: ok')"
uv run --locked --no-sync python tools/acceptance/device.py --profile alas --serial "<serial>" --check-preview --check-control --check-reconnect --non-interactive --report "<report-path>"
```

Перед runner следует проверить import/init без direct `setuptools` и убедиться,
что `serial` относится к ожидаемому Windows/MuMu target. Runner сам проверяет
`adb get-state`, Android boot readiness, package readiness, ADB shell и BGR
скриншот; дополнительные флаги проверяют preview, настроенный control backend
(`minitouch` handshake без касания), безопасный `KEYCODE_BACK` и target-explicit
reconnect/recovery. Для ручного интерактивного запуска нужно убрать
`--non-interactive` и подтвердить каждый шаг. Установка APK, очистка app data,
покупки, бой, task queue, clipboard, пользовательский текст и `adb kill-server`
runner'ом запрещены. В отчёт нельзя включать реальные serial, credentials или
локальные секреты.

Stage 3 не объявляется глобально завершённым только исправлением PR #256:
post-merge Dependency Dashboard refresh и оставшиеся dependency lines относятся
к отдельному completion contract.

## Изменение Rich

Rich 15 больше не сохраняет вывод, записанный внутри `Console.capture()`, в
`record` buffer. WebUI traceback и RichLog теперь используют изолированный
`io.StringIO` как output stream и печатают renderable напрямую в record buffer.
Так сохраняются HTML-вывод, экранирование пользовательского текста и редактирование
секретов.
