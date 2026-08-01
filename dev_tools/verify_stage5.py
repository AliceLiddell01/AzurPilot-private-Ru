"""Локальная безопасная проверка Stage 5 без изменения рабочего deploy.yaml."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

import yaml

from deploy.language_migration import migrate_deploy_language
from module.config.locale import UI_LOCALE

SAFE_FIXTURE = """# disposable Stage 5 fixture
Deploy:
  Webui:
    Language: en-US
    Theme: dark
Profile:
  PackageName: com.YoStarEN.AzurLane
  ServerName: en-0
  OcrLanguage: en
  OcrModelVersionEnglish: azur_lane_v6_6
  Event: campaign_main
"""


def _without_language(value):
    if isinstance(value, dict):
        return {
            key: _without_language(child)
            for key, child in value.items()
            if key != "Language"
        }
    if isinstance(value, list):
        return [_without_language(child) for child in value]
    return value


def verify(source: Path | None) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="azurpilot-stage5-") as temp:
        fixture = Path(temp) / "deploy.yaml"
        if source is None:
            fixture.write_text(SAFE_FIXTURE, encoding="utf-8")
            source_kind = "built_in_fixture"
        else:
            shutil.copyfile(source, fixture)
            source_kind = "user_supplied_copy"

        before_bytes = fixture.read_bytes()
        before = yaml.safe_load(before_bytes.decode("utf-8"))
        result = migrate_deploy_language(str(fixture))
        after_bytes = fixture.read_bytes()
        after = yaml.safe_load(after_bytes.decode("utf-8"))
        second = migrate_deploy_language(str(fixture))

        if _without_language(before) != _without_language(after):
            raise RuntimeError("Миграция изменила значения, не относящиеся к Language.")
        if UI_LOCALE.encode("utf-8") not in after_bytes:
            raise RuntimeError("В копии конфигурации не найдено итоговое Language: ru-RU.")
        if second.changed or fixture.read_bytes() != after_bytes:
            raise RuntimeError("Повторная миграция не является byte-for-byte no-op.")

        return {
            "source": source_kind,
            "first_run_changed": result.changed,
            "second_run_changed": second.changed,
            "only_language_changed": True,
            "idempotent": True,
            "result_locale": UI_LOCALE,
            "original_size": len(before_bytes),
            "result_size": len(after_bytes),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy-copy", type=Path)
    args = parser.parse_args()
    if args.deploy_copy is not None and not args.deploy_copy.is_file():
        parser.error("Указанная копия deploy.yaml не существует.")
    print(json.dumps(verify(args.deploy_copy), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
