from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".ps1",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SCAN_ROOTS = (
    ROOT / "assets",
    ROOT / "config",
    ROOT / "deploy",
    ROOT / "dev_tools",
    ROOT / "module",
)
SCAN_FILES = (
    ROOT / "README.md",
    ROOT / "PRIVACY_AND_DISCLAIMER.md",
)


def _iter_text_files():
    for path in SCAN_FILES:
        if path.is_file():
            yield path
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                yield path


def test_upstream_project_domains_are_not_present_in_personal_runtime_or_templates():
    forbidden = "nanoda" + ".work"
    hits: list[str] = []
    for path in _iter_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if forbidden in text.lower():
            hits.append(str(path.relative_to(ROOT)))

    assert not hits, "Найдены запрещённые upstream project endpoints: " + ", ".join(hits)
