from __future__ import annotations

import re
from pathlib import Path

from tests.support.paths import REPOSITORY_ROOT


CONTEXT_ROOT = REPOSITORY_ROOT / ".codex" / "context"
DURABLE_CONTEXT = tuple(
    path
    for path in sorted(CONTEXT_ROOT.glob("*.md"))
    if path.name not in {"MIGRATION-MAP.md", "09-SOURCES-MAINTENANCE.md"}
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_durable_context_contains_no_task_prompt_residue() -> None:
    forbidden = (
        re.compile(r"(?i)\bstage[\s_-]*\d\b"),
        re.compile(r"(?i)\bв\s+текущем\s+increment\b"),
        re.compile(r"(?i)\bв\s+этом\s+follow-up\b"),
        re.compile(r"(?i)\bисходн\w*\s+prompt\b"),
    )
    for path in DURABLE_CONTEXT:
        content = _text(path)
        for pattern in forbidden:
            assert pattern.search(content) is None, (path, pattern.pattern)


def test_canonical_agent_context_is_not_pinned_to_reviewer_model() -> None:
    paths = (
        REPOSITORY_ROOT / "AGENTS.md",
        CONTEXT_ROOT / "08-VERIFICATION.md",
        CONTEXT_ROOT / "GIT-WORKFLOW.md",
        REPOSITORY_ROOT
        / ".agents"
        / "skills"
        / "azurpilot-repository-development"
        / "SKILL.md",
    )
    for path in paths:
        assert "ChatGPT 5.6 Sol" not in _text(path), path


def test_project_map_matches_current_postgresql_runtime_boundary() -> None:
    project_map = _text(CONTEXT_ROOT / "01-PROJECT-MAP.md")
    assert "Production consumers пока не подключены" not in project_map
    assert "module.persistence.runtime" in project_map
    assert "module/application" in project_map


def test_product_context_keeps_global_en_runtime_boundary() -> None:
    config = _text(CONTEXT_ROOT / "03-CONFIG-I18N.md")
    glossary = _text(CONTEXT_ROOT / "10-GLOSSARY.md")
    assert "server — только" in config
    assert "com.YoStarEN.AzurLane" in config
    assert "Product runtime сейчас только Global/EN" in glossary
    assert "Регион игры: CN/EN/JP/TW" not in glossary


def test_python_tooling_context_is_current_and_bounded() -> None:
    tooling = _text(CONTEXT_ROOT / "11-PYTHON-TOOLING.md")
    assert len(tooling.splitlines()) <= 360
    for required in (
        "azurpilot.tooling",
        "azurpilot.integrations",
        "IntegrationRegistry",
        "Docker MCP Toolkit/Gateway",
        "CodeRabbit",
        "Semgrep",
        "Grafana",
        "Context7",
        "Docker Docs",
        "Docker Hub",
    ):
        assert required in tooling
    assert "В текущем increment выполнены" not in tooling
    assert "Не входит в этот increment" not in tooling


def test_cli_live_acceptance_is_scope_derived() -> None:
    verification = _text(CONTEXT_ROOT / "08-VERIFICATION.md")
    skill_ref = _text(
        REPOSITORY_ROOT
        / ".agents"
        / "skills"
        / "azurpilot-repository-development"
        / "references"
        / "ci-and-verification.md"
    )
    assert "Если diff затрагивает" in verification
    assert "azur delivery" in verification and "azur pr" in verification
    assert "Для несвязанного combat/OCR/" in verification
    assert "Human CLI + " in skill_ref
    assert "acceptance обязателен только когда diff затрагивает" in skill_ref


def test_root_agent_contract_stays_navigation_sized() -> None:
    agents = _text(REPOSITORY_ROOT / "AGENTS.md")
    assert len(agents.splitlines()) <= 220
    assert ".codex/context/INDEX.md" in agents
    assert ".codex/context/GIT-WORKFLOW.md" in agents
    assert ".codex/context/08-VERIFICATION.md" in agents
    assert ".codex/context/11-PYTHON-TOOLING.md" in agents
