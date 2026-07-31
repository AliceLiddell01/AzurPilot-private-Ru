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
PATCH_SHA256 = "6a05f75f45fdcc6b6790b587b17f26b12fea36f5e574f2e1ae43a7de48d9b922"


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
    if target.exists():
        return
    source = Path("module/config/i18n/en-US.json")
    target.write_bytes(source.read_bytes())


def fix_stable_action_identifier() -> None:
    path = Path("module/webui/translate.py")
    source = path.read_text(encoding="utf-8")
    old = '{"label": "Сохранить", "value": "Сохранить",'
    new = '{"label": "Сохранить", "value": "Submit",'
    if old not in source:
        raise SystemExit("Expected localized submit action was not found")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    parts = sorted(PAYLOAD_DIR.glob("part*.txt"))
    if len(parts) != 4:
        raise SystemExit(f"Expected 4 Stage 5 payload parts, found {len(parts)}")

    encoded = "".join(path.read_text(encoding="utf-8") for path in parts)
    patch = gzip.decompress(base64.b64decode(encoded))
    apply_patch(patch)
    bootstrap_ru_catalog()
    fix_stable_action_identifier()


if __name__ == "__main__":
    main()
