from __future__ import annotations

import re
from pathlib import Path

from tests.support.paths import REPOSITORY_ROOT


CONTEXT_ROOT = REPOSITORY_ROOT / ".codex" / "context"
DURABLE_CONTEXT = tuple(
    path
    for path in sorted(CONTEXT_ROOT.glob("*.md"))
    if path.name != "MIGRATION-MAP.md"
)

# Это грубые потолки, а не снимок текущего числа строк. Они оставляют заметный
# запас для обычного редактирования и работают вместе с семантическими тестами.
ROOT_AGENT_BUDGET_BYTES = 24 * 1024
DURABLE_CONTEXT_PAGE_BUDGET_BYTES = 32 * 1024

_TASK_RESIDUE_PATTERNS = (
    re.compile(r"(?i)\bstage(?:[\s_-]*\d+)\b"),
    re.compile(r"(?i)\bincrement\b"),
    re.compile(r"(?i)\bfollow-up\b"),
    re.compile(r"(?i)\bprompt\b"),
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalized(value: str) -> str:
    return " ".join(value.lower().replace("`", "").split())


def _section(value: str, start: str, end: str) -> str:
    assert start in value
    tail = value.split(start, maxsplit=1)[1]
    assert end in tail
    return tail.split(end, maxsplit=1)[0]


def test_durable_context_contains_no_task_state_residue() -> None:
    for path in DURABLE_CONTEXT:
        content = _text(path)
        for pattern in _TASK_RESIDUE_PATTERNS:
            assert pattern.search(content) is None, (
                f"{path}: долговременный контекст содержит временное состояние "
                f"задачи ({pattern.pattern})"
            )


def test_final_review_policy_is_model_neutral() -> None:
    workflow = _text(CONTEXT_ROOT / "GIT-WORKFLOW.md")
    verification = _text(CONTEXT_ROOT / "08-VERIFICATION.md")
    sections = {
        "merge": _normalized(_section(workflow, "### Merge", "## 21.")),
        "ready": _normalized(
            _section(
                verification,
                "### Pre-merge `READY_FOR_CHATGPT_REVIEW`",
                "### После подтверждённого merge",
            )
        ),
    }
    delegated_reviewer = re.compile(
        r"(?i)(?:через|с\s+помощью)\s+\S+|\bмодел\w*\b|"
        r"\breviewer\b|\bассистент\w*\b|\bagent\w*\b"
    )
    for name, section in sections.items():
        sentences = [
            sentence.strip()
            for sentence in re.split(r"[.;\n]+", section)
            if "финаль" in sentence and ("ревью" in sentence or "review" in sentence)
        ]
        assert sentences, name
        for sentence in sentences:
            assert "пользоват" in sentence, (
                f"{name}: финальное ревью должно оставаться пользовательским"
            )
            assert delegated_reviewer.search(sentence) is None, (
                f"{name}: финальное пользовательское ревью нельзя делегировать "
                "конкретному бренду, модели или другому обязательному reviewer"
            )


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
    assert "только Global/EN" in glossary
    assert "Регион игры: CN/EN/JP/TW" not in glossary


def test_python_tooling_context_uses_canonical_integration_inventory() -> None:
    tooling = _text(CONTEXT_ROOT / "11-PYTHON-TOOLING.md")
    assert "IntegrationName" in tooling
    assert "ADAPTER_ORDER" in tooling
    assert "IntegrationRegistry" in tooling
    assert "Docker MCP Toolkit/Gateway" in tooling


def test_cli_live_acceptance_cannot_become_global_gate() -> None:
    verification = _text(CONTEXT_ROOT / "08-VERIFICATION.md")
    policy = _normalized(verification)
    assert "только если diff затрагивает" in policy
    assert "для несвязанного combat/ocr/documentation-исправления" in policy

    global_gate = re.compile(
        r"(?i)(?:cli|--json).{0,100}(?:обязател\w*|требует\w*).{0,100}"
        r"(?:для\s+(?:любого|каждого|всех)\s+(?:pr|изменени\w*|задач\w*)|всегда)"
    )
    routed_sources = (
        REPOSITORY_ROOT / "AGENTS.md",
        CONTEXT_ROOT / "08-VERIFICATION.md",
        REPOSITORY_ROOT
        / ".agents"
        / "skills"
        / "azurpilot-repository-development"
        / "SKILL.md",
    )
    for routed_path in routed_sources:
        assert global_gate.search(_text(routed_path)) is None, routed_path


def test_navigation_context_respects_coarse_byte_budgets() -> None:
    agents = _text(REPOSITORY_ROOT / "AGENTS.md")
    tooling = _text(CONTEXT_ROOT / "11-PYTHON-TOOLING.md")
    assert len(agents.encode("utf-8")) <= ROOT_AGENT_BUDGET_BYTES, (
        "AGENTS.md перестал быть короткой картой; переносите детали к владельцам "
        "правил, а не увеличивайте лимит под текущий снимок"
    )
    assert len(tooling.encode("utf-8")) <= DURABLE_CONTEXT_PAGE_BUDGET_BYTES, (
        "11-PYTHON-TOOLING.md снова разросся в инвентарный список/дорожную карту; сокращайте "
        "снимочные детали вместо увеличения лимита"
    )
