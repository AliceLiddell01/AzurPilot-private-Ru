# Continuous Integration

## Required checks

Pull request в `personal/stable` защищён тремя устойчивыми status checks из `.github/workflows/ci.yml`:

- `Python` — зависимости, Ruff, компиляция, продуктовые regression-тесты и проверка generated-файлов;
- `Windows` — PowerShell Parser, PSScriptAnalyzer и Windows-регрессии;
- `Security` — Gitleaks, аудит добавленных файлов, security/privacy-регрессии и браузерная DOM-проверка.

В ruleset эти checks добавляются как три отдельных значения: `Python`, `Windows`, `Security`.

## Принцип CI

CI проверяет продуктовые свойства и устойчивые контракты. Номера исторических этапов, committed evidence, stage-specific baselines и временные migration gates не являются архитектурой постоянного CI.

Тяжёлые реальные acceptance-прогоны и benchmarks остаются локальными инструментами в `tools/acceptance/` и `tools/benchmarks/`. Их unit/regression-контракты включаются в постоянные jobs только там, где они защищают production-поведение без требования реального устройства.

## Не-CI автоматизации

Workflow для публикации Docker-образа, синхронизации зеркала и обработки Issues не являются required pull-request checks и управляются отдельно от продуктового CI.
