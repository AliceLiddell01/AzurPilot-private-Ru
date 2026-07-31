"""Однократный транспортный загрузчик реализации Stage 5.

Файл удаляется тем же commit, который публикует итоговую реализацию.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import subprocess
from pathlib import Path

PAYLOAD_DIR = Path(".github/stage5_payload")
PARTS = (
    ("fix00.txt", "cb75927c6fb7f01063ac102f3cf56db00d817f52a91def0809758e1791ff620f"),
    ("fix01.txt", "4445bf24412d1f157d8e287176f29014fe0c302bd5a5a26eabf3df7709639a04"),
    ("fix02.txt", "00d5989ee33006c50ced84cae080e3ab04e25d781e17b8a369c49e47fd4b28dd"),
    ("fix03.txt", "a4e9e8a23ae72f52a902973d4d0b0040d863174252f0d19d439e8f4cd93161a0"),
    ("fix04.txt", "bd9e14211c7cfe46404e4de40f05ce85104bdd1382d240a2206dc1b0618b2e0a"),
    ("fix05.txt", "2edb948ed22191d827e9f9dbb4ffd69fc8cc04838b513d16ecf5c3cf0f9da4f2"),
    ("fix06.txt", "846c27b0704517a26bd0d27f0b825288a6c3b1fbfc946f197683175dfaa19199"),
    ("fix07.txt", "dc3d7048f9635c8432a1325f4a7db97b4bafa1ed7edd496f5f81d85b466aee94"),
    ("part2.txt", "bf39c8cb46e6240c717e2c92f2df7cb023c65a6b96ecdde7404bbca3f6b6307e"),
    ("part3.txt", "d267ee467c49f49bf078dcf1f4b34898bce8c10ee1492b8d9763ed9b0b479801"),
)
PATCH_SHA256 = "6a05f75f45fdcc6b6790b587b17f26b12fea36f5e574f2e1ae43a7de48d9b922"

PERMANENT_WORKFLOW = r'''name: Lint

on: [push, pull_request]

permissions:
  contents: read

jobs:
  python-validation:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.14'
      - name: Install dependencies
        run: uv sync
      - name: Run ruff
        run: uv run ruff check . --select E9,F63,F7,F82 --ignore F821,F722
      - name: Run Stage 3 regression tests
        run: >-
          uv run python -m unittest -v
          tests/test_deploy_location.py
          tests/test_stage3d_deploy_set.py
          tests/test_stage3d_legacy_installer.py
          tests/test_stage3d_templates.py
          tests/test_stage3d_webui_settings.py
          tests/test_stage3e_release_cleanup.py
      - name: Run Stage 4 audit tests
        run: uv run python -m unittest -v tests/test_russianization_audit.py
      - name: Check Russianization audit baseline
        run: uv run python -m dev_tools.russianization_audit --check
      - name: Check generated configuration
        run: |
          uv run -m dev_tools.button_extract
          uv run -m module.config.config_updater
          git diff --binary > /tmp/generator-first.diff
          uv run -m dev_tools.button_extract
          uv run -m module.config.config_updater
          git diff --binary > /tmp/generator-second.diff
          cmp /tmp/generator-first.diff /tmp/generator-second.diff
          git diff --exit-code --ignore-space-at-eol
      - name: Run Stage 5 regression tests
        run: >-
          uv run python -m unittest -v
          tests/test_stage5_deploy_language_migration.py
          tests/test_stage5_locale_runtime.py
          tests/test_stage5_server_separation.py
          tests/test_stage5_generator.py
      - name: Run Stage 5 verifier
        run: uv run python -m dev_tools.verify_stage5
      - name: Check diff formatting
        run: git diff --check
      - name: Check forbidden deletions and secrets
        shell: bash
        env:
          EVENT_NAME: ${{ github.event_name }}
          PR_BASE_SHA: ${{ github.event.pull_request.base.sha }}
          PUSH_BEFORE_SHA: ${{ github.event.before }}
        run: |
          if [[ "$EVENT_NAME" == 'pull_request' ]]; then
            diff_range="${PR_BASE_SHA}...HEAD"
          elif [[ -n "$PUSH_BEFORE_SHA" && "$PUSH_BEFORE_SHA" != '0000000000000000000000000000000000000000' ]]; then
            diff_range="${PUSH_BEFORE_SHA}..HEAD"
          elif git rev-parse --verify HEAD^ >/dev/null 2>&1; then
            diff_range='HEAD^..HEAD'
          else
            echo 'No parent commit is available for the diff audit.'
            exit 0
          fi

          forbidden_deletions="$(git diff --name-status --diff-filter=D "$diff_range" | awk '$2 ~ /^assets\// || $2 ~ /^module\/config\/i18n\/(en-US|ja-JP|zh-CN|zh-MIAO|zh-TW)\.json$/ { print }')"
          if [[ -n "$forbidden_deletions" ]]; then
            echo 'Stage 5 attempted forbidden locale or asset deletions.'
            echo "$forbidden_deletions"
            exit 1
          fi

          DIFF_RANGE="$diff_range" python - <<'PY'
          import os
          import re
          import subprocess

          diff = subprocess.run(
              ['git', 'diff', '--unified=0', '--no-color', os.environ['DIFF_RANGE']],
              check=True,
              capture_output=True,
              text=True,
          ).stdout
          patterns = {
              'private key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
              'GitHub token': r'\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b',
              'AWS access key': r'\bAKIA[0-9A-Z]{16}\b',
              'webhook URL': r'https://(?:discord(?:app)?\.com/api/webhooks|hooks\.slack\.com/services)/[^\s]+',
          }
          matched = [name for name, pattern in patterns.items() if re.search(pattern, diff)]
          if matched:
              raise SystemExit('Potential secret patterns detected: ' + ', '.join(matched))
          print('Secret pattern audit: no matches.')
          PY

  powershell-validation:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install PSScriptAnalyzer
        shell: pwsh
        run: |
          Set-PSRepository -Name PSGallery -InstallationPolicy Trusted
          Install-Module -Name PSScriptAnalyzer -Scope CurrentUser -Force -ErrorAction Stop
      - name: Parse PowerShell scripts
        shell: pwsh
        run: |
          Write-Host ('PowerShell version: {0}' -f $PSVersionTable.PSVersion)
          $scriptRoot = Join-Path -Path $PWD -ChildPath 'scripts'
          $scriptFiles = @(
              Get-ChildItem -LiteralPath $scriptRoot -Recurse -File |
                  Where-Object {
                      $_.Extension -in @(
                          '.ps1'
                          '.psm1'
                      )
                  }
          )
          $parseFailures = [System.Collections.Generic.List[string]]::new()
          foreach ($scriptFile in $scriptFiles) {
              $tokens = $null
              $parseErrors = $null
              [void][System.Management.Automation.Language.Parser]::ParseFile(
                  $scriptFile.FullName,
                  [ref]$tokens,
                  [ref]$parseErrors
              )
              foreach ($parseError in $parseErrors) {
                  $parseFailures.Add(
                      '{0}:{1}: {2}' -f
                      $scriptFile.FullName,
                      $parseError.Extent.StartLineNumber,
                      $parseError.Message
                  )
              }
          }
          if ($parseFailures.Count -gt 0) {
              throw ($parseFailures -join [Environment]::NewLine)
          }
      - name: Analyze PowerShell scripts
        shell: pwsh
        run: |
          $analyzerParameters = @{
              Path = (Join-Path -Path $PWD -ChildPath 'scripts')
              Recurse = $true
              Severity = @(
                  'Error'
                  'Warning'
              )
              ExcludeRule = @(
                  'PSUseBOMForUnicodeEncodedFile'
              )
          }
          $findings = @(Invoke-ScriptAnalyzer @analyzerParameters)
          if ($findings.Count -gt 0) {
              $details = $findings | Format-List | Out-String
              throw $details
          }
'''

STAGE5_REPORT = '''# Stage 5 — единый русский runtime locale

## База и границы

- Базовая ветка: `personal/stable`.
- Базовый SHA: `84f002227589230703fb22469db1bd252efb6f3d`.
- Рабочая ветка: `chatgpt/stage5-single-russian-locale`.
- Активный locale интерфейса: только `ru-RU`.
- Массовый перевод строк относится к Stage 6.
- Удаление неиспользуемых иностранных locale и ассетов относится к Stage 9.

## Архитектурный результат

- Runtime WebUI загружает только `module/config/i18n/ru-RU.json`.
- Выбор языка в Home, OOBE и настройках развёртывания удалён.
- Locale браузера не может переключить runtime-язык.
- Locale интерфейса отделён от игрового сервера, package name, OCR и asset fallback.
- Источник названий событий задан явно: английский metadata-источник с серверным fallback.
- Файлы `en-US`, `ja-JP`, `zh-CN`, `zh-MIAO`, `zh-TW` сохранены как неактивное наследие.

## Миграция конфигурации

`deploy/language_migration.py` выполняет явную patch-only миграцию `Language` в `ru-RU` до первого чтения кешированной deploy-конфигурации.

Контракт миграции:

- отсутствие побочных эффектов при импорте и обычном чтении;
- сохранение комментариев, неизвестных ключей, CRLF и состояния финального перевода строки;
- атомарная запись через временный файл и replace;
- byte-for-byte no-op для уже мигрированного `ru-RU`;
- отказ без записи при дубликатах, повреждённом или неоднозначном YAML;
- отсутствие вывода значений потенциальных секретов в журнал.

## Генератор и аудит

- Активный генератор создаёт только `ru-RU`.
- Русский JSON записывается канонически с финальным переводом строки.
- Повторная генерация детерминирована.
- Stage 4 audit baseline обновлён и классифицирует активный runtime locale отдельно от legacy locale-файлов.
- Удалений locale-файлов и ассетов в Stage 5 нет.

## Проверки

CI выполняет:

- Ruff по критическим классам ошибок;
- 17 regression-тестов Stage 3;
- unit-тесты и baseline-check Stage 4;
- regression-тесты миграции, runtime locale, server separation и генератора Stage 5;
- безопасный verifier на копии deploy-конфигурации;
- проверку детерминизма генераторов и чистоты Git diff;
- PowerShell Parser и PSScriptAnalyzer на Windows;
- проверку запрещённых удалений и паттерн-аудит секретов.

## Ограничения ручной приёмки

CI не заменяет запуск реального WebUI, эмулятора и Azur Lane на пользовательской Windows-системе. После автоматических проверок остаётся один локальный acceptance-pass: запустить `scripts/Verify-AzurPilot-Stage5.ps1`, открыть WebUI и подтвердить отсутствие переключателя языка, постоянный `ru-RU` и неизменность EN/Global server/package/OCR.
'''


def read_payload() -> bytes:
    chunks: list[str] = []
    failures: list[str] = []
    for name, expected in PARTS:
        path = PAYLOAD_DIR / name
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != expected:
            failures.append(f"{name}: {digest}, expected {expected}, bytes={len(content)}")
        chunks.append(content.decode("utf-8"))
    if failures:
        raise SystemExit("Stage 5 payload part mismatch:\n" + "\n".join(failures))
    compressed = base64.b64decode("".join(chunks))
    return gzip.decompress(compressed)


def apply_patch(patch: bytes) -> None:
    digest = hashlib.sha256(patch).hexdigest()
    if digest != PATCH_SHA256:
        raise SystemExit(f"Unexpected Stage 5 payload digest: {digest}")
    completed = subprocess.run(
        ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
        input=patch,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def bootstrap_ru_catalog() -> None:
    target = Path("module/config/i18n/ru-RU.json")
    if not target.exists():
        target.write_bytes(Path("module/config/i18n/en-US.json").read_bytes())


def fix_stable_action_identifier() -> None:
    path = Path("module/webui/translate.py")
    source = path.read_text(encoding="utf-8")
    old = '{"label": "Сохранить", "value": "Сохранить",'
    new = '{"label": "Сохранить", "value": "Submit",'
    if old not in source:
        raise SystemExit("Expected localized submit action was not found")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def fix_canonical_i18n_newline() -> None:
    path = Path("module/config/config_updater.py")
    source = path.read_text(encoding="utf-8")
    if "import json\n" not in source:
        source = source.replace("import re\nimport typing as t\n", "import json\nimport re\nimport typing as t\n", 1)
    old = "        write_file(filepath_i18n(UI_LOCALE), new)\n"
    new = (
        "        content = json.dumps(new, indent=2, ensure_ascii=False, sort_keys=False, default=str)\n"
        "        atomic_write(filepath_i18n(UI_LOCALE), content + '\\n')\n"
    )
    if old not in source and new not in source:
        raise SystemExit("Expected active locale writer was not found")
    source = source.replace(old, new, 1)
    path.write_text(source, encoding="utf-8")


def write_final_project_files() -> None:
    Path(".github/workflows/lint.yml").write_text(PERMANENT_WORKFLOW, encoding="utf-8")
    report = Path("dev_tools/russianization/results/stage5_report.md")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(STAGE5_REPORT, encoding="utf-8")
    Path("stage5_failure.txt").unlink(missing_ok=True)
    Path("stage5_failure.log").unlink(missing_ok=True)


def main() -> None:
    patch = read_payload()
    apply_patch(patch)
    bootstrap_ru_catalog()
    fix_stable_action_identifier()
    fix_canonical_i18n_newline()
    write_final_project_files()


if __name__ == "__main__":
    main()
