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
    re.compile(
        r"(?i)\b(?:в|на|для)\s+(?:текущ\w*|следующ\w*|этом)\s+"
        r"(?:этап\w*|итерац\w*|increment)\b"
    ),
    re.compile(
        r"(?i)\b(?:в|на|для)\s+(?:этом|текущ\w*|следующ\w*)\s+"
        r"follow-up(?:\s+(?:pr|задач\w*))?\b"
    ),
    re.compile(
        r"(?i)\b(?:исходн\w*|предыдущ\w*|этот|текущ\w*)\s+prompt\b|"
        r"\bprompt\s+(?:требовал\w*|просил\w*|задавал\w*)\b"
    ),
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
    concrete_model = re.compile(
        r"(?i)\b(?:chatgpt\s*\d|gpt[-\s]?\d|claude\s*\d|gemini\s*\d)\b"
    )
    delegated_decision = re.compile(
        r"(?i)(?:merge|слияни\w*).{0,100}(?:разрешает|одобряет|решает).{0,80}"
        r"(?:coderabbit|reviewer|agent|ассистент|модел\w*)|"
        r"(?:coderabbit|reviewer|agent|ассистент|модел\w*).{0,80}"
        r"(?:разрешает|одобряет|решает).{0,100}(?:merge|слияни\w*)"
    )
    for name, section in sections.items():
        final_review_sentences = [
            sentence.strip()
            for sentence in re.split(r"[.;\n]+", section)
            if "финаль" in sentence and ("ревью" in sentence or "review" in sentence)
        ]
        assert final_review_sentences, name
        assert any("пользоват" in sentence for sentence in final_review_sentences), (
            f"{name}: ownership финального ревью должен оставаться у пользователя"
        )
        assert concrete_model.search(section) is None, name
        assert delegated_decision.search(section) is None, name


def test_task_residue_patterns_are_contextual() -> None:
    residue_examples = (
        "В текущей итерации добавим ещё один gate.",
        "В этом follow-up PR обновим policy.",
        "Исходный prompt требовал временный обход.",
        "Stage 12 оставляет старый route.",
    )
    allowed_examples = (
        "Prompt injection обрабатывается отдельной security boundary.",
        "API prompt contract является частью внешнего протокола.",
        "Increment используется как имя технического счётчика.",
        "Follow-up является названием внешнего события.",
    )
    for example in residue_examples:
        assert any(pattern.search(example) for pattern in _TASK_RESIDUE_PATTERNS)
    for example in allowed_examples:
        assert all(pattern.search(example) is None for pattern in _TASK_RESIDUE_PATTERNS)


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
    normalized = _normalized(tooling)
    assert "integrationname" in normalized
    assert "adapter_order" in normalized
    assert "integrationregistry" in normalized
    assert "docker mcp toolkit/gateway" in normalized
    assert "generic mcp proxy" in normalized


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
        assert global_gate.search(_normalized(_text(routed_path))) is None, routed_path


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
