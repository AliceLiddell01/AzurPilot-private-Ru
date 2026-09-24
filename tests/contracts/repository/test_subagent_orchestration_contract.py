from __future__ import annotations

import tomllib
from pathlib import Path

from tests.support.paths import REPOSITORY_ROOT

CONTEXT_ROOT = REPOSITORY_ROOT / ".codex" / "context"
AGENTS_ROOT = REPOSITORY_ROOT / ".codex" / "agents"
ROLE_NAMES = {
    "architecture_scout",
    "implementation_investigator",
    "regression_analyst",
    "adversarial_reviewer",
    "contract_consistency",
    "localization_reviewer",
}
ORCHESTRATION_OWNER = ".codex/context/12-SUBAGENT-ORCHESTRATION.md"


def _toml(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_project_config_selects_multi_agent_v2_and_keeps_shared_defaults() -> None:
    config = _toml(REPOSITORY_ROOT / ".codex" / "config.toml")
    agents = config["agents"]
    features = config["features"]

    assert isinstance(agents, dict)
    assert isinstance(features, dict)
    assert agents["default_subagent_model"] == "gpt-6-luna"
    assert agents["default_subagent_reasoning_effort"] == "max"
    assert "enabled" not in agents
    assert "max_concurrent_threads_per_session" not in agents

    v2 = features["multi_agent_v2"]
    assert isinstance(v2, dict)
    assert v2["enabled"] is True
    assert v2["max_concurrent_threads_per_session"] == 7


def test_project_roles_are_unique_read_only_and_inherit_shared_model_defaults() -> None:
    role_files = sorted(AGENTS_ROOT.glob("*.toml"))
    roles = [_toml(path) for path in role_files]
    names = [role.get("name") for role in roles]

    assert len(role_files) == 6
    assert set(names) == ROLE_NAMES
    assert len(names) == len(set(names))

    required = {"name", "description", "developer_instructions", "sandbox_mode"}
    common_markers = (
        "не изменяй файлы",
        "не выполняй git",
        "mcp",
        "публикацию",
        "pr",
        "жизненного цикла",
        "не создавай субагентов",
    )
    for role in roles:
        assert required <= role.keys()
        assert role["sandbox_mode"] == "read-only"
        assert "model" not in role
        assert "reasoning_effort" not in role
        assert isinstance(role["description"], str) and role["description"].strip()
        assert isinstance(role["developer_instructions"], str)
        instructions = role["developer_instructions"].lower()
        assert all(marker in instructions for marker in common_markers), role["name"]
        assert "пут" in instructions and "символ" in instructions, role["name"]


def test_orchestration_policy_has_one_owner_and_compact_routes() -> None:
    index = _text(CONTEXT_ROOT / "INDEX.md")
    owner = _text(CONTEXT_ROOT / "12-SUBAGENT-ORCHESTRATION.md")
    root_guidance = _text(REPOSITORY_ROOT / "AGENTS.md")
    skill = _text(
        REPOSITORY_ROOT
        / ".agents"
        / "skills"
        / "azurpilot-repository-development"
        / "SKILL.md"
    )
    verification = _text(CONTEXT_ROOT / "08-VERIFICATION.md")
    git_workflow = _text(CONTEXT_ROOT / "GIT-WORKFLOW.md")

    assert index.count("`12-SUBAGENT-ORCHESTRATION.md`") == 1
    assert ORCHESTRATION_OWNER in root_guidance
    assert ORCHESTRATION_OWNER in skill
    assert ORCHESTRATION_OWNER in verification
    assert ORCHESTRATION_OWNER in git_workflow
    assert "проактивно используй субагентов" in root_guidance.lower()
    assert "делегируй соответствующее исследование" in skill
    assert "независимую проверку субагентами" in verification
    assert "по умолчанию один основной codex" not in git_workflow.lower()

    role_ids = ROLE_NAMES - {"localization_reviewer"}
    for routed_text in (root_guidance, skill, verification):
        assert not any(role_id in routed_text for role_id in role_ids)
    assert "localization_reviewer" in verification
    assert "до 6" not in root_guidance.lower()
    assert len(owner.encode("utf-8")) < 32 * 1024


def test_orchestration_policy_preserves_root_ownership_and_independent_review() -> None:
    owner = _text(CONTEXT_ROOT / "12-SUBAGENT-ORCHESTRATION.md")
    normalized = " ".join(owner.casefold().split())
    investigation_rules, review_rules = normalized.split("## рабочие волны", maxsplit=1)

    assert "корневой агент" in investigation_rules
    assert "выполняет общие изменения" in investigation_rules
    assert "объединяет результаты" in investigation_rules
    assert "не дели связанные изменения" in investigation_rules
    assert "малой задаче" in investigation_rules
    assert "потолок, а не цель" in investigation_rules

    assert "свежая независимая волна проверки" in review_rules
    assert "новые потоки агентов" in review_rules
    assert "не участвовавшие в первоначальном исследовании или реализации" in review_rules
    assert "обязательно подключай `localization_reviewer`" in review_rules
    assert "всего текста изменённых pr-файлов" in review_rules


def test_localization_role_distinguishes_prose_from_machine_identifiers() -> None:
    role = _toml(AGENTS_ROOT / "localization_reviewer.toml")
    instructions = str(role["developer_instructions"]).lower()

    for surface in (
        "человекочитаемый текст",
        "комментарии",
        "строки документации",
        "журналы и сообщения оператора",
        "диагностику и ошибки",
        "справку cli",
        "текст webui",
        "документацию",
        "навыки и контекст",
        "тестовые данные и описания",
    ):
        assert surface in instructions
    for identifier_kind in (
        "технических и машинных идентификаторов",
        "api",
        "имён классов, функций и пакетов",
        "маркеров протокола",
        "путей и url",
        "точных внешних и игровых идентификаторов",
        "машинных контрактов",
    ):
        assert identifier_kind in instructions
    assert "верни ровно" in instructions
    assert "путь / символ" in instructions
    assert "почему это иностранный человекочитаемый текст" in instructions
    assert "не изменяй файлы" in instructions
    assert "не переписывай текст" in instructions
