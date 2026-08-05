from pathlib import Path


REPLACEMENTS = {
    ".codex/context/GIT-WORKFLOW.md": {
        "codex/stage4-updater-hardening": "codex/updater-hardening",
    },
    "tests/serve_webui_traceback.py": {
        "Локальный сервер ручной проверки Stage 7 traceback fixtures": "Локальный сервер ручной проверки traceback fixtures",
        "Stage 7 fixture server:": "Traceback fixture server:",
    },
    "tools/acceptance/ocr.py": {
        "Stage 8B OCR acceptance plan": "OCR acceptance plan",
        "Stage 8B real acceptance выполняется только на EN/Global profile.": "Реальная OCR-приёмка выполняется только на EN/Global profile.",
        '"title": "Stage 8B OCR acceptance: PASS"': '"title": "OCR acceptance: PASS"',
        'description="Безопасная EN/Global OCR-приёмка Stage 8B"': 'description="Безопасная EN/Global OCR-приёмка"',
        "Stage 8B OCR acceptance: FAIL —": "OCR acceptance: FAIL —",
        "Stage 8B OCR acceptance: PASS": "OCR acceptance: PASS",
    },
    "tools/acceptance/webui_smoke.py": {
        "Изолированный Windows/WebUI smoke-тест Stage 6": "Изолированный Windows/WebUI smoke-тест",
    },
}

for filename, replacements in REPLACEMENTS.items():
    path = Path(filename)
    text = path.read_text(encoding="utf-8")
    for old, new in replacements.items():
        count = text.count(old)
        if count != 1:
            raise SystemExit(f"{filename}: expected one occurrence of {old!r}, found {count}")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
