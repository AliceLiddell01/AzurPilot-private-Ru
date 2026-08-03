# Stage 8A — контракт журналов устройства и ADB

## Область

Stage 8A русифицирует first-party runtime-сообщения следующих слоёв:

- ADB connection/reconnect и выбор target;
- жизненный цикл эмулятора;
- screenshot backends;
- control/input backends;
- device-owned WebUI live preview/control.

OCR, scheduler/config API, combat и Operation Siren остаются за пределами этого этапа.

## Неизменяемые контракты

Перевод не меняет:

- `-s <serial>`, `ANDROID_SERIAL`, `-d` и `-e`;
- команды, аргументы, stdout/stderr и типы исключений;
- retry, timeout и порядок fallback;
- socket/protocol framing scrcpy;
- screenshot pipeline `numpy.ndarray` в BGR;
- координаты и сериализацию input-команд;
- package/server selection.

Immutable migration baseline хранится только в
`dev_tools/stage8a_semantic_policy.py`.

## Автоматическая проверка

```text
uv run python -m dev_tools.verify_stage8a --output-dir artifacts/stage8a
```

Verifier создаёт только временные CI artifacts:

```text
scope.json
metrics.json
report.md
semantic-findings.json
approved-delta.json
contract.json
unittest.log
```

Generated reports не коммитятся.

`dev_tools/stage8a_binary_log_audit.py` независимо проверяет AST logger-вызовов.
Прямая передача binary-payload-shaped значений (`image`, `frame`, `payload`,
`packet`, `video`, `screenshot`) является blocking finding. Разрешены только
metadata: byte count, format, width/height, shape/dtype, backend и status.
Имена metadata-переменных с суффиксами `_count`, `_size`, `_length`, `_shape`,
`_dtype`, `_format`, `_width` и `_height` не считаются raw binary payload.

Stage 8A изменяет часть сообщений, которые Stage 7 уже передал владельцу
`stage8a`. Поэтому изменение digest точечных Stage 7 policy-шаблонов допустимо
только вместе с bridge-тестом, подтверждающим, что drift ограничен ожидаемыми
Stage 8A runtime-точками и не маскирует посторонние изменения.

Security fixtures собираются только во время теста из безопасных фрагментов.
В Git-истории PR не допускаются даже тестовые строки, совпадающие с шаблонами
секретов Gitleaks.

## Exact-head CI

Stage 8A считается технически готовым только тогда, когда на фактическом head PR
выполнены и завершились со статусом `success` существующие required jobs.

Состояния `skipped`, `neutral`, `cancelled` и проверки с
`continue-on-error` не засчитываются как прохождение Stage 8A gate.

Изменение runtime-кода, semantic policy, verifier, acceptance runner, workflow
или blocking tests аннулирует доказательство предыдущего head и требует нового
exact-head запуска.

## Реальная приёмка

Безопасный acceptance runner:

```text
uv run python -m dev_tools.stage8a_device_acceptance --profile alas --serial-from-config --check-preview --check-control --check-reconnect
```

Runner:

- отказывается от `auto` и неоднозначного target;
- все device-scoped ADB-команды выполняет target-explicit через `adb -s <serial>`;
- проверяет ADB transport, package и один PNG screenshot;
- декодирует screenshot в `numpy.ndarray` BGR;
- при запросе проверяет пользовательский preview-контракт;
- сначала ожидает raw scrcpy в ограниченном acceptance-окне;
- отсутствие первого raw scrcpy кадра не считается самостоятельным отказом,
  потому что кадр может не появиться без изменения поверхности;
- при недоступности raw scrcpy проверяет фактический WebUI screenshot fallback:
  наличие ffmpeg и два последовательных BGR-кадра через настроенный screenshot backend;
- для настроенного `minitouch` выполняет только handshake и закрывает временный
  ADB forward без отправки touch-команд;
- отправляет один `KEYCODE_BACK` отдельной target-explicit ADB-командой только
  после отдельного подтверждения `BACK`;
- выполняет reconnect только после отдельного подтверждения `RECONNECT`;
- для валидного TCP serial после `adb -s <serial> reconnect` выполняет explicit
  `adb connect <serial>` к тому же endpoint, не перезапуская ADB server;
- после reconnect до 60 секунд проверяет восстановление именно выбранного target
  через `adb -s <serial> get-state`;
- не устанавливает APK, не очищает app data, не запускает task queue;
- не читает clipboard и не вводит пользовательский текст;
- удаляет временный screenshot;
- сохраняет уже пройденные безопасные шаги в sanitized FAIL-report;
- маскирует serial, IP/host, username/path, SSH location, URL credentials,
  authorization header, token-shaped значения, password/API-key/secret-shaped
  значения, private-key blocks и опасный HTML-shaped текст;
- представляет бинарные stdout/stderr только как количество байтов, не помещая
  screenshot, H.264, control packet или иной raw binary stream в artifact;
- ограничивает размер внешней диагностики и удаляет управляющие символы;
- записывает sanitized report в `artifacts/stage8a/device-acceptance.json`.

Read-only определение target и package выполняется до подтверждения `START`,
чтобы пользователь видел фактический выбор. Отказ от `START` гарантированно
происходит до screenshot, preview, control и reconnect.

Указанные `--check-control` и `--check-reconnect` считаются успешно пройденными
только при статусе `PASS`; отказ от `BACK` или `RECONNECT` не может сформировать
итоговый PASS.

PR остаётся Draft до точной команды пользователя `PASS — сливай`.
