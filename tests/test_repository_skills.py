from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
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
        "feature",
        "bugfix",
        "refactor",
        "ci/test",
        "upstream",
        "pr",
        "merge",
        "cleanup",
    ):
        assert trigger in description
    for boundary in ("read-only", "перевода текста", "без изменения репозитория"):
        assert boundary in description


def test_coderabbit_description_routes_review_requests() -> None:
    frontmatter, _ = _frontmatter(_SKILLS_ROOT / "azurpilot-coderabbit-review" / "SKILL.md")
    description = str(frontmatter["description"]).lower()
    for trigger in ("coderabbit", "review", "pr", "findings", "rate limit", "wsl2 arch"):
        assert trigger in description
    for generic_trigger in ("подготовка pr к финальному ревью", "generic pr preparation"):
        assert generic_trigger not in description


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


def test_required_references_and_workflow_guardrails_are_present() -> None:
    development_dir = _SKILLS_ROOT / "azurpilot-repository-development"
    development_content = (development_dir / "SKILL.md").read_text(encoding="utf-8")
    for reference in (
        "references/engineering-contract.md",
        "references/ci-and-verification.md",
        "references/browser-and-live-testing.md",
        "references/pr-merge-cleanup.md",
    ):
        assert (development_dir / reference).is_file()
        assert reference in development_content
    for required in (
        "task-specific",
        "stage-agnostic",
        "русским",
        "текущем основном checkout",
        "WSL2",
        "Browser/Computer Use",
        "GIT-WORKFLOW.md",
        "upstream sync",
        "sync/*",
        "codex/port-upstream",
        "READY_FOR_CHATGPT_REVIEW",
        "ChatGPT 5.6 Sol",
        "явная команда",
        "post-merge verification",
        "rate limit",
    ):
        assert required.lower() in development_content.lower()

    review_dir = _SKILLS_ROOT / "azurpilot-coderabbit-review"
    review_content = (review_dir / "SKILL.md").read_text(encoding="utf-8")
    review_reference = review_dir / "references" / "review-workflow.md"
    assert review_reference.is_file()
    assert "references/review-workflow.md" in review_content
    assert "доведение PR до точки внешнего финального ревью" not in review_content
    for required in (
        "exact commit",
        "если PR существует",
        "partially confirmed",
        "insufficient evidence",
        "WSL2 Arch",
        "false positive",
        "rate limit",
        "READY_FOR_CHATGPT_REVIEW",
    ):
        assert required.lower() in review_content.lower()
    reference_content = review_reference.read_text(encoding="utf-8").lower()
    assert "coderabbit review --agent --committed --base-commit" in reference_content
    assert "отсутствие pr само по себе не блокирует" in reference_content
    assert "partially confirmed" in reference_content
    assert "insufficient evidence" in reference_content
    assert "другой canonical review checkout" not in reference_content
    assert "другую среду для coderabbit review" in reference_content
    assert "linked worktree" in reference_content


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
    canonical_paths = (
        _REPOSITORY_ROOT / "AGENTS.md",
        _REPOSITORY_ROOT / ".codex" / "context" / "GIT-WORKFLOW.md",
        _REPOSITORY_ROOT / ".codex" / "context" / "08-VERIFICATION.md",
    )
    merge_guards = {
        _REPOSITORY_ROOT / "AGENTS.md": (
            "только новое текущее сообщение пользователя",
            "не выполняет merge без",
        ),
        _REPOSITORY_ROOT / ".codex" / "context" / "GIT-WORKFLOW.md": (
            "до такой команды",
            "отдельной текущей команды",
        ),
        _REPOSITORY_ROOT / ".codex" / "context" / "08-VERIFICATION.md": (
            "merge не выполняется без отдельной текущей команды пользователя",
            "не является разрешением на merge",
        ),
    }
    for path in canonical_paths:
        content = path.read_text(encoding="utf-8").lower()
        assert "ready_for_chatgpt_review" in content
        assert "chatgpt 5.6 sol" in content
        assert merge_guards[path][0] in content
        assert merge_guards[path][1] in content
    combined = "\n".join(path.read_text(encoding="utf-8") for path in canonical_paths)
    assert "100% технического цикла" not in combined
    assert "auto-merge допустим после зелёных gates" not in combined
    assert "завершить прогон как ожидающий review" not in combined
    assert "READY_FOR_CHATGPT_REVIEW" in combined


def test_checkout_policy_defers_implementation_exceptions_to_canonical_workflow() -> None:
    agents_content = (_REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8").lower()
    workflow_content = (_REPOSITORY_ROOT / ".codex" / "context" / "GIT-WORKFLOW.md").read_text(
        encoding="utf-8"
    ).lower()
    assert "implementation checkout/worktree" in agents_content
    assert ".codex/context/git-workflow.md" in agents_content
    assert "отдельный wsl2 arch checkout разрешён только для независимого coderabbit review" not in agents_content
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
