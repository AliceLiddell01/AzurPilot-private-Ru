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


def read_payload() -> bytes:
    chunks = []
    failures = []
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


def main() -> None:
    patch = read_payload()
    apply_patch(patch)
    bootstrap_ru_catalog()
    fix_stable_action_identifier()


if __name__ == "__main__":
    main()
