# Инструменты benchmark

Benchmarks являются необязательными командами разработчика и не входят в required status checks.

- `uv run python -m tools.benchmarks.ocr_english_models --help`;
- `uv run python -m tools.benchmarks.screenshot_intervals --help`.
- `uv run --locked --no-sync python -m tools.benchmarks.ocr_rpc_transport --help`.

Измерения зависят от оборудования, эмулятора и состояния игры. Generated reports и screenshots должны оставаться вне Git. Быстрые parser/format/regression tests инструментов выполняются в job `Python`, но реальные измерения запускаются только вручную в подходящей контролируемой среде.

`ocr_rpc_transport` выполняет bounded transport-only synthetic benchmark для
сопоставления `zerorpc` и `pyzmq`: startup/readiness, single request, batch и
повторяющаяся последовательность запросов. Синтетический endpoint не загружает
OCR-модель и измеряет только сериализацию, transport и wire decode. Каждый
запуск фиксирует Python, platform, package versions и exact Git HEAD; report
сохраняется вне Git.

Пример воспроизводимого запуска выполняется в двух окружениях: старое
`zerorpc`-окружение должно быть checkout-ом baseline, новое `pyzmq`-окружение —
целевым checkout-ом. Значения `--runs`, `--iterations` и `--startup-runs`
ограничивают общий объём измерения.

```powershell
uv run --locked --no-sync python -m tools.benchmarks.ocr_rpc_transport `
  --transport new --repo-root C:\AzurPilot --output <new-report.json>
<baseline-python> -m tools.benchmarks.ocr_rpc_transport `
  --transport old --repo-root <baseline-checkout> --output <old-report.json>
uv run --locked --no-sync python -m tools.benchmarks.ocr_rpc_transport `
  --compare <old-report.json> <new-report.json>
```
