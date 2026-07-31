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
PART_SHA256 = {
    "part0.txt": "504866ea312aed797f652b9840f8cb1f35ebbab03306f55b44a63fa9db78a96e",
    "part1.txt": "74f4c754d5d345da07db4320f327137fc5ac272ec48a1ba62a14f64fe8d9a96e",
    "part2.txt": "bf39c8cb46e6240c717e2c92f2df7cb023c65a6b96ecdde7404bbca3f6b6307e",
    "part3.txt": "d267ee467c49f49bf078dcf1f4b34898bce8c10ee1492b8d9763ed9b0b479801",
}
PATCH_SHA256 = "6a05f75f45fdcc6b6790b587b17f26b12fea36f5e574f2e1ae43a7de48d9b922"


def validate_parts(parts: list[Path]) -> None:
    failures = []
    for path in parts:
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        expected = PART_SHA256.get(path.name)
        if digest != expected:
            failures.append(f"{path.name}: {digest}, expected {expected}, bytes={len(content)}")
    if failures:
        raise SystemExit("Stage 5 payload part mismatch:\n" + "\n".join(failures))


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


def main() -> None:
    parts = sorted(PAYLOAD_DIR.glob("part*.txt"))
    if len(parts) != 4:
        raise SystemExit(f"Expected 4 Stage 5 payload parts, found {len(parts)}")
    validate_parts(parts)
    encoded = "".join(path.read_text(encoding="utf-8") for path in parts)
    patch = gzip.decompress(base64.b64decode(encoded))
    apply_patch(patch)
    bootstrap_ru_catalog()
    fix_stable_action_identifier()


if __name__ == "__main__":
    main()
