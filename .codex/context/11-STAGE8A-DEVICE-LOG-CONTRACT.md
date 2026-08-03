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
- записывает sanitized report в `artifacts/stage8a/device-acceptance.json`.

PR остаётся Draft до точной команды пользователя `PASS — сливай`.
