# Benchmark tools

Benchmarks are optional developer commands and are not required status checks.

- `uv run python -m tools.benchmarks.ocr_english_models --help`
- `uv run python -m tools.benchmarks.screenshot_intervals --help`

Hardware, emulator, and game measurements are environment-specific. Keep generated reports and screenshots out of version control. Fast parser and formatting regressions remain covered by the `Python` job.
