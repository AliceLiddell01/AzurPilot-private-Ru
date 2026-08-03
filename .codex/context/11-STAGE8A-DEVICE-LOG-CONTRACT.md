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

Stage 8A изменяет часть сообщений, которые Stage 7 уже передал владельцу
`stage8a`. Поэтому изменение digest точечных Stage 7 policy-шаблонов допустимо
только вместе с bridge-тестом, подтверждающим, что drift ограничен ожидаемыми
Stage 8A runtime-точками и не маскирует посторонние изменения.

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
- всегда использует target-explicit `adb -s <serial>`;
- проверяет ADB transport, package и один PNG screenshot;
- декодирует screenshot в `numpy.ndarray` BGR;
- при запросе проверяет scrcpy preview;
- отправляет только один `KEYCODE_BACK` после отдельного подтверждения;
- выполняет reconnect только после отдельного подтверждения;
- не устанавливает APK, не очищает app data, не запускает task queue;
- не читает clipboard и не вводит пользовательский текст;
- удаляет временный screenshot;
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

PR остаётся Draft до точной команды пользователя `PASS — сливай`.
