from __future__ import annotations

import re
from pathlib import Path

import yaml

from dev_tools.integration_contract_gate import check as integration_contract_check
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
    # Убираем markdown-разметку (backtick и bold), но сохраняем wildcard вроде codex/*.
    return " ".join(value.lower().replace("`", "").replace("**", "").split())


def _section(value: str, start: str, end: str) -> str:
    assert start in value
    tail = value.split(start, maxsplit=1)[1]
    assert end in tail
    return tail.split(end, maxsplit=1)[0]


def _numbered_contract_items(value: str) -> tuple[str, ...]:
    items: list[list[str]] = []
    current: list[str] | None = None
    for line in value.splitlines():
        match = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if match:
            if current is not None:
                items.append(current)
            current = [match.group(1)]
            continue
        if current is not None and line.strip():
            current.append(line.strip())
    if current is not None:
        items.append(current)
    return tuple(_normalize_contract(" ".join(item)) for item in items)


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
        "слияни",
        "очистк",
    ):
        assert trigger in description
    for boundary in ("объяснен", "без изменения", "файлов"):
        assert boundary in description


def test_coderabbit_description_routes_review_requests() -> None:
    frontmatter, _ = _frontmatter(_SKILLS_ROOT / "azurpilot-coderabbit-review" / "SKILL.md")
    description = str(frontmatter["description"]).lower()
    for trigger in ("coderabbit", "review", "pr", "findings", "rate limit", "host-native"):
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
        "явном запросе coderabbit/code review",
        "внутренней делегации",
        "host-native executable",
        "generic pr preparation",
        "обычной разработки вне такого checkpoint",
    ):
        assert required in review_content
    for required in (
        "проверку coderabbit запускай только по явному запросу пользователя",
        "явно передай её соседнему навыку",
        "`azurpilot-coderabbit-review`",
        "ограничения частоты, повторов и разбора результатов",
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
        item
        for item in custom_checks
        if isinstance(item, dict) and item.get("name") == "Declared scope contract"
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

    linked_references = set(
        re.findall(r"\]\((references/[^)]+\.md)\)", development_content)
    )
    reference_files = {
        path.relative_to(development_dir).as_posix()
        for path in (development_dir / "references").glob("*.md")
    }
    assert linked_references == reference_files
    assert linked_references
    for reference in linked_references:
        assert (development_dir / reference).is_file()

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
        "typed conflict",
        "host-native",
        "false positive",
        "rate limit",
        "provider finding не является verified finding disposition",
        "individual triage",
        "CODERABBIT_TRIAGE_REQUIRED",
    ):
        assert required.lower() in review_content.lower()
    workflow_content = " ".join(review_reference.read_text(encoding="utf-8").split())
    for required in (
        "provider finding и verified finding disposition — разные сущности",
        "affected code",
        "call sites",
        "ближайшие tests",
        "relevant contracts",
        "azur integrations coderabbit triage",
        "duplicate review",
    ):
        assert required.lower() in workflow_content.lower()


def test_cross_thread_mcp_continuation_has_one_canonical_contract() -> None:
    development_dir = _SKILLS_ROOT / "azurpilot-repository-development"
    development = (development_dir / "SKILL.md").read_text(encoding="utf-8")
    reference_path = development_dir / "references" / "cross-thread-task-delegation.md"
    reference = reference_path.read_text(encoding="utf-8")
    reference_flat = " ".join(reference.lower().split())

    assert "references/cross-thread-task-delegation.md" in development
    assert "необязательной проверки регистрации codex" in " ".join(
        development.lower().split()
    )
    for required in (
        "source_reconciled",
        "runtime_ready=true",
        "fresh_mcp_client_acceptance",
        "LOCAL_MCP_SUPERVISOR_STOPPED",
        "Coordinator task",
        "Fresh independent task/thread",
        "Subagent",
        "fork",
        "same-directory child worker",
        "Connected App",
        "repository identity",
        "exact expected HEAD",
        "effective_codex_registration",
        "codex_registration_check",
        "terminal result",
        "BLOCKED_PRECONDITION",
    ):
        assert required.lower() in reference_flat

    branch_contract = _section(
        reference,
        "### Канонический branch-based запуск",
        "### Неиспользуемый working-tree маршрут",
    )
    branch_contract_flat = " ".join(branch_contract.lower().split())
    for required in (
        "startingstate.type=branch",
        "branchname",
        "branch tip",
        "expected head",
        "exact head",
    ):
        assert required in branch_contract_flat

    non_canonical_contract = _section(
        reference,
        "### Неиспользуемый working-tree маршрут",
        "`create_thread` асинхронен",
    )
    non_canonical_contract_flat = " ".join(non_canonical_contract.lower().split())
    assert "startingstate.type=working-tree" in non_canonical_contract_flat
    assert "не является exact-head continuation" in non_canonical_contract_flat
    assert "git switch" in non_canonical_contract_flat
    assert "git checkout" in non_canonical_contract_flat

    post_create_checks = _section(
        reference,
        "Перед optional registration check",
        "Если check продолжается",
    )
    post_create_checks_flat = " ".join(post_create_checks.lower().split())
    for required in (
        "фактический exact head",
        "expected head",
        "detached head допустим",
        "post-create `git switch`",
        "post-create `git checkout`",
        "blocked_precondition",
    ):
        assert required in post_create_checks_flat

    sequence = _section(
        reference,
        "## Каноническая последовательность",
        "Fresh task не исправляет",
    )
    sequence_items = _numbered_contract_items(sequence)
    assert len(sequence_items) == 8
    assert "branch tip" in sequence_items[1]
    assert "task не создаётся" in sequence_items[1]
    assert "startingstate.type=branch" in sequence_items[2]
    assert "фактический exact head" in sequence_items[3]
    assert "detached head допустим" in sequence_items[3]
    assert "post-create switch/checkout" in sequence_items[3]
    assert "blocked_precondition" in sequence_items[7]

    for relative in (
        Path(".codex/context/08-VERIFICATION.md"),
        Path(".codex/context/11-PYTHON-TOOLING.md"),
        Path(".agents/skills/azurpilot-repository-development/references/browser-and-live-testing.md"),
        Path("plugins/azurpilot/references/mcp-routing.md"),
        Path("plugins/azurpilot/skills/azurpilot-troubleshooting/SKILL.md"),
    ):
        document_path = _REPOSITORY_ROOT / relative
        raw_content = document_path.read_text(encoding="utf-8")
        content = raw_content.lower()
        targets = re.findall(
            r"\]\(([^)\s]*cross-thread-task-delegation\.md)\)",
            raw_content,
            flags=re.IGNORECASE,
        )
        assert targets
        for target in targets:
            assert (document_path.parent / target).is_file()
        assert (
            "cross-thread continuation" in content
            or "продолжения между задачами" in content
            or "продолжения задачи между потоками" in content
        )
        assert (
            "fresh independent" in content
            or "independent codex" in content
            or "independent task/thread" in content
                or "независимая codex" in content
                or ("независим" in content and "task/thread" in content)
                or "единый контракт cross-thread" in content
                or "продолжения между задачами" in content
                or "продолжения задачи между потоками" in content
            )


def test_operator_workflow_requires_literal_azur_and_separates_mcp_readiness() -> None:
    contract = integration_contract_check(_REPOSITORY_ROOT)
    checks = contract["checks"]
    assert isinstance(checks, dict)
    assert checks["operator_workflow_boundary"] == "ready"
    errors = contract["errors"]
    assert isinstance(errors, list)
    assert not any(str(error).startswith("operator workflow:") for error in errors)
    assert contract["ok"] is True

    development_skill = (
        _REPOSITORY_ROOT
        / "plugins"
        / "azurpilot"
        / "skills"
        / "azurpilot-development"
        / "SKILL.md"
    ).read_text(encoding="utf-8").lower()
    assert "каноническая codex-команда: uv run" not in development_skill


def test_developer_workflow_uses_terminal_mcp_sync_smoke_run_and_intent_delivery() -> None:
    paths = {
        "навык репозитория": (
            _REPOSITORY_ROOT
            / ".agents/skills/azurpilot-repository-development/SKILL.md"
        ),
        "владелец проверок": _REPOSITORY_ROOT / ".codex/context/08-VERIFICATION.md",
        "владелец инструментов": _REPOSITORY_ROOT / ".codex/context/11-PYTHON-TOOLING.md",
        "владелец Git": _REPOSITORY_ROOT / ".codex/context/GIT-WORKFLOW.md",
        "навык разработки плагина": (
            _REPOSITORY_ROOT
            / "plugins/azurpilot/skills/azurpilot-development/SKILL.md"
        ),
        "справочник маршрутизации плагина": (
            _REPOSITORY_ROOT / "plugins/azurpilot/references/mcp-routing.md"
        ),
    }
    contracts = {
        "навык репозитория": (
            "azur mcp sync --base",
            "NO_CHANGES",
            "приёмка новым клиентом",
            "dev_run_smoke",
            "dev_start_smoke",
            "delivery publish --message",
        ),
        "владелец проверок": (
            "azur mcp sync --base",
            "NO_CHANGES",
            "заморозки варианта изменений",
            "delivery publish --message",
        ),
        "владелец инструментов": (
            "azur mcp sync --base",
            "exact base",
            "delivery publish --message",
            "in-memory",
        ),
        "владелец Git": (
            "azur mcp sync --base",
            "SYNCED",
            "delivery publish",
            "preimage/postimage",
        ),
        "навык разработки плагина": (
            "azur mcp sync --base",
            "NO_CHANGES",
            "fresh-client acceptance",
            "dev_run_smoke",
            "dev_start_smoke",
            "terminal result",
        ),
        "справочник маршрутизации плагина": ("azur mcp sync --base", "NO_CHANGES", "fresh-client acceptance"),
    }
    for owner, path in paths.items():
        content = " ".join(path.read_text(encoding="utf-8").casefold().split())
        for contract_index, phrase in enumerate(contracts[owner], start=1):
            assert phrase.casefold() in content, (
                f"{owner}: в {path} не выполнено требование контракта №{contract_index}"
            )

    normal_contract = _normalize_contract(
        " | ".join(
            paths[owner].read_text(encoding="utf-8")
            for owner in (
                "навык репозитория",
                "владелец проверок",
                "владелец инструментов",
                "владелец Git",
                "навык разработки плагина",
            )
        )
    )
    assert "dev_start_smoke" in normal_contract
    assert "dev_capture_smoke_game_checkpoint" not in normal_contract
    assert "не запускай validate manifest перед обычной публикацией" in normal_contract
    assert "вызови validate manifest перед обычной публикацией" not in normal_contract
    plugin_skill = paths["навык разработки плагина"].read_text(encoding="utf-8")
    assert "dev_validate_smoke` оставлен для необязательной read-only проверки" in " ".join(
        plugin_skill.split()
    )
    assert "dev_validate_smoke` → `dev_run_smoke" not in plugin_skill


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
    raw_merge_section = _section(workflow, "### Слияние", "## 21.")
    merge_section = _normalize_contract(raw_merge_section)
    items = _numbered_contract_items(raw_merge_section)
    assert len(items) >= 3

    final_review_index = next(
        (
            index
            for index, item in enumerate(items)
            if "финаль" in item and "пользоват" in item and "ревью" in item
        ),
        None,
    )
    assert final_review_index is not None, 'В разделе "Слияние" отсутствует финальная проверка пользователя.'
    authorization_index = next(
        (
            index
            for index, item in enumerate(items)
            if "разреш" in item and "отдельн" in item and "текущ" in item and "pr" in item
        ),
        None,
    )
    assert authorization_index is not None, 'В разделе "Слияние" отсутствует отдельное разрешение для текущего PR.'
    merge_action_index = next(
        (
            index
            for index, item in enumerate(items)
            if "слияни" in item and "провер" in item
        ),
        None,
    )
    assert merge_action_index is not None, 'В разделе "Слияние" отсутствует проверка действия и повторной проверки.'
    assert final_review_index < authorization_index < merge_action_index

    assert "старое разрешение" in merge_section
    assert "разрешение для другого pr" in merge_section
    assert "недостаточ" in merge_section
    assert all(token in merge_section for token in ("ci", "coderabbit", "самостоятельная проверка"))
    assert "не являются разрешением на слияние" in merge_section
    assert "ready_for_chatgpt_review" in merge_section
    assert "merge-authorized" in merge_section

def test_new_capability_branch_contract_does_not_restore_codex_default() -> None:
    workflow = (
        _REPOSITORY_ROOT / ".codex" / "context" / "GIT-WORKFLOW.md"
    ).read_text(encoding="utf-8")
    normalized = _normalize_contract(workflow)
    assert "<domain>/<unique-capability-name>" in normalized
    assert "codex/*" in normalized
    assert "прежним пространством имён" in normalized
    assert "новые обычные задачи это пространство имён не используют" in normalized
    assert "sync/*" in normalized


def test_fast_track_and_retry_budget_preserve_pre_merge_gate() -> None:
    workflow = (_REPOSITORY_ROOT / ".codex" / "context" / "GIT-WORKFLOW.md").read_text(
        encoding="utf-8"
    ).lower()
    fast_track = workflow.split("### быстрый режим", maxsplit=1)[1].split("### стандартный", maxsplit=1)[0]
    assert "ready_for_chatgpt_review" in fast_track
    assert "остановка" in fast_track
    assert "не даёт разрешения на слияние" in fast_track
    assert "merge + короткий post-merge smoke" not in fast_track

    workflow_flat = " ".join(workflow.split())
    for required in (
        "после исчерпания числа повторов обязательной проверки продукта или безопасности слияние блокируется",
        "если навык coderabbit вернул `rate_limited`",
        "жизненный цикл git может достичь `ready_for_chatgpt_review`",
        "это не отменяет обязательные ci, проверку безопасности и секретов, обязательную приёмку продукта или блокирующие обсуждения",
        "правила ожидания, повторного запуска и разбора результатов сервиса описаны в соответствующем навыке и справочнике",
    ):
        assert required in workflow_flat


def test_rate_limit_cannot_reopen_merge_authorized_or_merged_lifecycle() -> None:
    workflow = (_REPOSITORY_ROOT / ".codex" / "context" / "GIT-WORKFLOW.md").read_text(
        encoding="utf-8"
    )
    workflow_post_merge = workflow.split("## 23. После слияния и откат", maxsplit=1)[1].split(
        "## 24. Защита веток", maxsplit=1
    )[0]
    normalized = _normalize_contract(workflow)
    assert "merge-authorized" in normalized
    assert "merged" in normalized
    assert "ready_for_chatgpt_review" not in _normalize_contract(workflow_post_merge)


def test_checkout_policy_defers_implementation_exceptions_to_canonical_workflow() -> None:
    agents_content = (_REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8").lower()
    workflow_content = " ".join((_REPOSITORY_ROOT / ".codex" / "context" / "GIT-WORKFLOW.md").read_text(
        encoding="utf-8"
    ).lower().split())
    assert "git-workflow.md" in agents_content
    assert "для любых git/pr-операций следуй только" in agents_content
    assert "параллельная разработка" not in agents_content
    assert "опасный reproduction/experiment" not in agents_content
    for exception in (
        "параллельная разработка",
        "опасное воспроизведение/эксперимент",
        "несовместимое состояние зависимостей или среды выполнения",
    ):
        assert exception in workflow_content


def test_ci_contract_runs_for_any_pr_and_stable_push() -> None:
    workflow = yaml.safe_load(
        (_REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    ci_doc = (_REPOSITORY_ROOT / "docs" / "ci.md").read_text(encoding="utf-8")
    assert isinstance(workflow, dict)
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    pull_request = triggers.get("pull_request")
    assert isinstance(pull_request, dict)
    assert "branches" not in pull_request

    push = triggers.get("push")
    assert isinstance(push, dict)
    assert push.get("branches") == ["personal/stable"]

    assert "независимо от target branch" in ci_doc
    assert "push-trigger" in ci_doc.lower()

    for event in triggers.values():
        if isinstance(event, dict):
            assert "paths" not in event
            assert "paths-ignore" not in event

    assert workflow.get("permissions") == {"contents": "read"}
    concurrency = workflow.get("concurrency")
    assert isinstance(concurrency, dict)
    assert "github.event.pull_request.number" in str(concurrency.get("group", ""))

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
