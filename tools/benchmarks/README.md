# Инструменты benchmark

Benchmarks являются необязательными командами разработчика и не входят в required status checks.

- `uv run python -m tools.benchmarks.ocr_english_models --help`;
- `uv run python -m tools.benchmarks.screenshot_intervals --help`.

Измерения зависят от оборудования, эмулятора и состояния игры. Generated reports и screenshots должны оставаться вне Git. Быстрые parser/format/regression tests инструментов выполняются в job `Python`, но реальные измерения запускаются только вручную в подходящей контролируемой среде.
