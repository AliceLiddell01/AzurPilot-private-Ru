from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.support.paths import REPOSITORY_ROOT

_REPOSITORY_ROOT = REPOSITORY_ROOT
_SKILLS_ROOT = _REPOSITORY_ROOT / ".agents" / "skills"
_PLUGIN_SKILL_PATH = _REPOSITORY_ROOT / "plugins" / "azurpilot" / "skills" / "azurpilot-development" / "SKILL.md"
_SKILL_NAMES = (
    "azurpilot-repository-development",
    "azurpilot-coderabbit-review",
)
_ABSOLUTE_LOCAL_PATH = re.compile(r"(?<![\w/:.`])(?:[A-Za-z]:[\\/]|\\\\|/(?!/))")
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s`]+")
_SECRET = re.compile(
    r"(?i)(?:\b(?:sk|rk|xox[baprs])-[A-Za-z0-9_-]{12,}|\b(?:ghp|github_pat)_[A-Za-z0-9_]{12,})"
)


def _frontmatter(path: Path) -> tuple[dict[str, object], str]:
    content = path.read_text(encoding="utf-8")
    assert content.startswith("---\n"), path
    match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    assert match is not None, path
    value = yaml.safe_load(match.group(1))
    assert isinstance(value, dict), path
    return value, content[match.end() :]


def _find_absolute_local_path(value: str) -> re.Match[str] | None:
    return _ABSOLUTE_LOCAL_PATH.search(_URL.sub("", value))


def _normalize_contract(value: str) -> str:
    return " ".join(value.lower().replace("`", "").replace("*", "").split())


def _section(value: str, start: str, end: str) -> str:
    assert start in value
    tail = value.split(start, maxsplit=1)[1]
    assert end in tail
    return tail.split(end, maxsplit=1)[0]


def test_repo_scoped_skills_have_unique_valid_frontmatter() -> None:
    skill_files = sorted(_SKILLS_ROOT.glob("*/SKILL.md"))
    discovered_skill_dirs = {path.parent.name for path in skill_files}
    assert set(_SKILL_NAMES) <= discovered_skill_dirs
    for skill_name in _SKILL_NAMES:
        assert (_SKILLS_ROOT / skill_name / "SKILL.md").is_file()

    names: list[str] = []
    for path in skill_files:
        frontmatter, _ = _frontmatter(path)
        name = frontmatter.get("name")
        description = frontmatter.get("description")
        assert name == path.parent.name
        assert isinstance(name, str)
        assert re.fullmatch(r"[a-z0-9-]+", name)
        assert isinstance(description, str) and description.strip()
        assert len(description) <= 1024
        names.append(name)

    assert len(names) == len(set(names))
    all_skill_names: list[str] = []
    for path in (_REPOSITORY_ROOT / "plugins").rglob("SKILL.md"):
        frontmatter, _ = _frontmatter(path)
        plugin_name = frontmatter.get("name")
        assert isinstance(plugin_name, str) and plugin_name.strip()
        all_skill_names.append(plugin_name)
    assert len(names + all_skill_names) == len(set(names + all_skill_names))


def test_skill_names_do_not_collide_with_the_dev_mcp_plugin_skill() -> None:
    plugin_frontmatter, _ = _frontmatter(_PLUGIN_SKILL_PATH)
    assert plugin_frontmatter["name"] == "azurpilot-development"
    assert "azurpilot-development" not in _SKILL_NAMES
    assert not (_SKILLS_ROOT / "azurpilot-development").exists()
    for required in ("dev_get_contract", "dev_list_smoke_capabilities", "PLUGIN_RUNTIME_INCOMPATIBLE"):
        assert required in _PLUGIN_SKILL_PATH.read_text(encoding="utf-8")


def test_development_description_has_positive_and_negative_routing() -> None:
    frontmatter, _ = _frontmatter(_SKILLS_ROOT / "azurpilot-repository-development" / "SKILL.md")
    description = str(frontmatter["description"]).lower()
    for trigger in (
        "разработ",
        "исправлен",
        "рефактор",
        "инфраструктур",
        "ci/тест",
        "upstream",
        "pr",
        "merge",
        "cleanup",
    ):
        assert trigger in description
    for boundary in ("read-only", "объяснен", "без изменения"):
        assert boundary in description


def test_coderabbit_description_routes_review_requests() -> None:
    frontmatter, _ = _frontmatter(_SKILLS_ROOT / "azurpilot-coderabbit-review" / "SKILL.md")
    description = str(frontmatter["description"]).lower()
    for trigger in ("coderabbit", "review", "pr", "findings", "rate limit", "wsl2 linux"):
        assert trigger in description
    for delegated_trigger in ("делегации", "canonical", "checkpoint"):
        assert delegated_trigger in description
    assert "подготовка pr к финальному ревью" not in description
    assert "не используй для generic pr preparation" in description


def test_coderabbit_supports_explicit_and_delegated_entry_points() -> None:
    review_skill = _SKILLS_ROOT / "azurpilot-coderabbit-review" / "SKILL.md"
    development_skill = _SKILLS_ROOT / "azurpilot-repository-development" / "SKILL.md"
    review_content = " ".join(review_skill.read_text(encoding="utf-8").lower().split())
    development_content = " ".join(development_skill.read_text(encoding="utf-8").lower().split())

    for required in (
        "явно запрашивает coderabbit/code review",
        "делегирует canonical coderabbit review checkpoint",
        "internal trigger",
        "отдельный пользовательский coderabbit-запрос не требуется",
        "generic pr preparation",
        "обычной разработки вне такого checkpoint",
    ):
        assert required in review_content
    for required in (
        "coderabbit review checkpoint",
        "явно делегируй",
        "sibling skill `azurpilot-coderabbit-review`",
        "не требует повторного пользовательского coderabbit-запроса",
    ):
        assert required in development_content


def test_repository_coderabbit_config_is_scope_aware_and_review_only() -> None:
    config = yaml.safe_load(
        (_REPOSITORY_ROOT / ".coderabbit.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(config, dict)
    assert config["language"] == "ru-RU"

    reviews = config["reviews"]
    assert isinstance(reviews, dict)
    assert reviews["profile"] == "assertive"
    assert reviews["request_changes_workflow"] is True
    auto_review = reviews["auto_review"]
    assert isinstance(auto_review, dict)
    assert auto_review["enabled"] is False
    assert auto_review["base_branches"] == ["personal/stable"]

    custom_checks = reviews["pre_merge_checks"]["custom_checks"]
    assert isinstance(custom_checks, list)
    scope_check = next(
        check for check in custom_checks if isinstance(check, dict) and check.get("name") == "Declared scope contract"
    )
    assert scope_check["mode"] == "error"
    assert "scope" in str(scope_check["instructions"]).lower()


def test_implicit_invocation_is_not_disabled() -> None:
    for skill_name in _SKILL_NAMES:
        skill_dir = _SKILLS_ROOT / skill_name
        metadata_path = skill_dir / "agents" / "openai.yaml"
        if metadata_path.exists():
            metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
            assert isinstance(metadata, dict)
            policy = metadata.get("policy", {})
            assert isinstance(policy, dict)
            assert policy.get("allow_implicit_invocation", True) is not False
        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert "allow_implicit_invocation: false" not in content


def test_development_skill_routes_to_canonical_workflow_owners() -> None:
    development_dir = _SKILLS_ROOT / "azurpilot-repository-development"
    development_content = (development_dir / "SKILL.md").read_text(encoding="utf-8")

    for reference in (
        "references/engineering-contract.md",
        "references/browser-and-live-testing.md",
    ):
        assert (development_dir / reference).is_file()
        assert reference in development_content

    for retired_reference in (
        "references/ci-and-verification.md",
        "references/pr-merge-cleanup.md",
    ):
        assert retired_reference not in development_content
        assert not (development_dir / retired_reference).exists()

    normalized = _normalize_contract(development_content)
    assert ".codex/context/git-workflow.md" in normalized
    assert ".codex/context/08-verification.md" in normalized
    for duplicated_policy in (
        "merge-authorized",
        "exact-head revalidation",
        "required ci",
        "ready_for_chatgpt_review",
    ):
        assert duplicated_policy not in normalized

    review_dir = _SKILLS_ROOT / "azurpilot-coderabbit-review"
    review_content = " ".join((review_dir / "SKILL.md").read_text(encoding="utf-8").split())
    review_reference = review_dir / "references" / "review-workflow.md"
    assert review_reference.is_file()
    assert "references/review-workflow.md" in review_content
    assert "доведение PR до точки внешнего финального ревью" not in review_content
    for required in (
        "exact commit",
        "если PR существует",
        "partially confirmed",
        "insufficient evidence",
        "WSL2 Linux",
        "false positive",
        "rate limit",
        "READY_FOR_CHATGPT_REVIEW",
    ):
        assert required.lower() in review_content.lower()


def test_new_skills_contain_no_local_paths_secrets_or_stage_baselines() -> None:
    for path in _SKILLS_ROOT.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        assert _find_absolute_local_path(content) is None, path
        assert not _SECRET.search(content), path
        assert not re.search(r"(?i)\bstage[\s_-]*\d", content), path
        assert "Bearer " not in content
        assert "CONTROL_PLANE_API_KEY=" not in content


def test_canonical_lifecycle_requires_final_review_before_merge() -> None:
    workflow = (_REPOSITORY_ROOT / ".codex" / "context" / "GIT-WORKFLOW.md").read_text(
        encoding="utf-8"
    )
    merge_section = _normalize_contract(_section(workflow, "### Merge", "## 21."))

    causal_chain = re.compile(
        r"пользователь.{0,120}завершил.{0,120}финальн\w*.{0,80}"
        r"пользовательск\w*.{0,80}ревью.{0,160}после этого.{0,120}"
        r"отдельн\w*.{0,80}текущ\w*.{0,80}(?:сообщени|команд).{0,160}"
        r"только затем.{0,120}merge"
    )
    assert causal_chain.search(merge_section), (
        "GIT-WORKFLOW должен связывать финальное пользовательское ревью → отдельное текущее "
        "merge-разрешение → merge одним нормативным правилом"
    )

    assert re.search(
        r"стар\w+ разрешени\w*.{0,120}(?:недостаточ|не подход)",
        merge_section,
    )
    assert re.search(
        r"(?:ci.{0,80}coderabbit.{0,80}self-review|"
        r"self-review.{0,80}coderabbit.{0,80}ci).{0,160}"
        r"не являются разрешением на merge",
        merge_section,
    )
    assert "ready_for_chatgpt_review" in merge_section
    assert "merge-authorized" in merge_section


def test_new_capability_branch_contract_does_not_restore_codex_default() -> None:
    workflow = (
        _REPOSITORY_ROOT / ".codex" / "context" / "GIT-WORKFLOW.md"
    ).read_text(encoding="utf-8")
    normalized = _normalize_contract(workflow)
    assert "<domain>/<unique-capability-name>" in normalized
    assert "codex/*" in normalized
    assert "compatibility/legacy" in normalized
    assert "новые обычные задачи этот namespace не используют" in normalized
    assert "sync/*" in normalized


def test_fast_track_and_retry_budget_preserve_pre_merge_gate() -> None:
    workflow = (_REPOSITORY_ROOT / ".codex" / "context" / "GIT-WORKFLOW.md").read_text(
        encoding="utf-8"
    ).lower()
    fast_track = workflow.split("### fast-track", maxsplit=1)[1].split("### стандартный", maxsplit=1)[0]
    assert "ready_for_chatgpt_review" in fast_track
    assert "stop" in fast_track
    assert "не даёт разрешения на merge" in fast_track
    assert "merge + короткий post-merge smoke" not in fast_track

    workflow_flat = " ".join(workflow.split())
    for required in (
        "после исчерпания бюджета retry для обязательного product/security gate merge блокируется",
        "coderabbit rate limit/cooldown не является product/security gate",
        "не блокирует `ready_for_chatgpt_review`",
        "не обходит required ci, security/secret scan",
    ):
        assert required in workflow_flat


def test_rate_limit_cannot_reopen_merge_authorized_or_merged_lifecycle() -> None:
    workflow = (_REPOSITORY_ROOT / ".codex" / "context" / "GIT-WORKFLOW.md").read_text(
        encoding="utf-8"
    )
    workflow_post_merge = workflow.split("## 24. Post-merge и rollback", maxsplit=1)[1].split(
        "## 25. Branch protection", maxsplit=1
    )[0]
    normalized = _normalize_contract(workflow)
    assert "merge-authorized" in normalized
    assert "merged" in normalized
    assert "ready_for_chatgpt_review" not in workflow_post_merge.lower()


def test_checkout_policy_defers_implementation_exceptions_to_canonical_workflow() -> None:
    agents_content = (_REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8").lower()
    workflow_content = (_REPOSITORY_ROOT / ".codex" / "context" / "GIT-WORKFLOW.md").read_text(
        encoding="utf-8"
    ).lower()
    assert ".codex/context/git-workflow.md" in agents_content
    assert "параллельная разработка" not in agents_content
    assert "опасный reproduction/experiment" not in agents_content
    for exception in (
        "параллельная разработка",
        "опасный reproduction/experiment",
        "несовместимое состояние зависимостей/runtime",
    ):
        assert exception in workflow_content


def test_ci_contract_keeps_stable_stage_agnostic_required_contexts() -> None:
    workflow = yaml.safe_load(
        (_REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    ci_doc = (_REPOSITORY_ROOT / "docs" / "ci.md").read_text(encoding="utf-8")
    assert isinstance(workflow, dict)
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    for event_name in ("pull_request", "push"):
        event = triggers.get(event_name)
        assert isinstance(event, dict)
        assert event.get("branches") == ["personal/stable"]
    for event in triggers.values():
        if isinstance(event, dict):
            assert "paths" not in event
            assert "paths-ignore" not in event

    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    job_names = {
        job.get("name")
        for job in jobs.values()
        if isinstance(job, dict)
    }
    assert {"Python", "Windows", "Security"} <= job_names
    for invariant in ("текущее продуктовое поведение", "historical SHA", "stage-specific"):
        assert invariant.lower() in ci_doc.lower()
