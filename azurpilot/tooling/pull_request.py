"""Fail-closed GitHub draft PR publication через typed spec и body-file."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any

from pydantic import ValidationError

from .contracts import (
    OperationState,
    PrPreparationDetails,
    PrPublicationSpec,
    PullRequestBody,
    PullRequestDetails,
    PullRequestEvidence,
    PullRequestIdentity,
    RepositoryIdentity,
    ResultCode,
    ToolingResult,
)
from .errors import ToolingError
from .filesystem import bounded_read_text, canonical_path, path_has_link
from .git import GitClient, repository_identity_from_remote
from .process import ProcessSpec, StructuredProcessRunner
from .repository import RepositoryResolver, ResolvedRepository

_MAX_SPEC_BYTES = 512 * 1024
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_SAFE_REMOTE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
# Поля `baseRefOid` и `headRepositoryOwner` в `gh pr --json` требуют gh >= 2.63.0.
_MIN_GH_VERSION = "2.63.0"
_UNKNOWN_JSON_FIELD_MARKERS = (
    "unknown json field",
    "unknown field",
    "not a valid json field",
    "invalid json field",
)
_PR_FIELDS = (
    "number,state,isDraft,body,baseRefName,baseRefOid,headRefName,headRefOid,"
    "headRepository,headRepositoryOwner,isCrossRepository"
)
_BODY_HEADINGS = (
    "## Цель",
    "## Scope",
    "## Реализация",
    "## Проверки",
    "## CI",
    "## Security / secret scan",
    "## CodeRabbit review и disposition",
    "## Readiness",
    "## Migration / rollback",
    "## Ограничения",
)
_BODY_CONTENT_MINIMUMS = (
    ("goal", "Цель", 160),
    ("scope", "Scope", 260),
    ("implementation", "Реализация", 450),
    ("checks", "Проверки", 360),
    ("ci", "CI", 180),
    ("security_secret_scan", "Security / secret scan", 200),
    ("migration_rollback", "Migration / rollback", 160),
    ("limitations", "Ограничения", 160),
)
_BODY_BULLET_FIELDS = frozenset(
    {
        "scope",
        "implementation",
        "checks",
        "ci",
        "security_secret_scan",
        "migration_rollback",
        "limitations",
    }
)
_BODY_MINIMUM_TOTAL_CHARS = 2_000


def _error(
    code: ResultCode,
    message: str,
    *,
    state: OperationState = OperationState.FAILED,
) -> ToolingError:
    return ToolingError(code, message, state=state)


def load_pr_spec(path: str | os.PathLike[str]) -> PrPublicationSpec:
    """Прочитать абсолютный закрытый PR spec без свободных полей."""

    spec_path = Path(path).expanduser()
    try:
        if not spec_path.is_absolute() or path_has_link(spec_path):
            raise _error(
                ResultCode.TOOLING_PR_BODY_INVALID,
                "PR spec должен быть абсолютным обычным файлом без symlink/reparse point.",
            )
        document = json.loads(
            bounded_read_text(spec_path, max_bytes=_MAX_SPEC_BYTES)
        )
        return PrPublicationSpec.model_validate(document)
    except ToolingError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _error(
            ResultCode.TOOLING_PR_BODY_INVALID,
            "Не удалось разобрать закрытый PR spec.",
        ) from exc


class PullRequestBodyRenderer:
    """Рендерит полный русскоязычный PR-отчёт из structured body model."""

    @classmethod
    def render(cls, body: PullRequestBody, *, base_sha: str, head_sha: str) -> str:
        cls.validate(body, base_sha=base_sha, head_sha=head_sha)
        review = body.coderabbit_review
        if review is None:
            review_text = (
                "Проверка CodeRabbit ещё не выполнялась на этой точке lifecycle. "
                "После создания draft PR проверка выполняется в постоянном WSL2 review clone; "
                "результат и disposition будут добавлены отдельным обновлением body."
            )
        else:
            findings = list(review.findings)
            lines = [
                f"Последний проверенный head: `{review.reviewed_head}`.",
                f"Текущий head: `{head_sha}`.",
                f"Base SHA: `{review.base_sha}`.",
                f"Количество findings: {len(findings)}.",
            ]
            if review.reviewed_head != head_sha:
                lines.append(
                    "Повторная проверка текущего head не выполнена; ниже сохранён "
                    "последний фактически полученный CodeRabbit result."
                )
            if review.rate_limit:
                lines.append(f"Ограничение rate limit: {review.rate_limit}")
            if review.history:
                lines.extend(("", review.history))
            if findings:
                lines.extend(
                    (
                        "",
                        "| Уровень | Путь | Влияние | Решение | Исправление | SHA исправления |",
                        "| --- | --- | --- | --- | --- | --- |",
                    )
                )
                lines.extend(
                    "| {severity} | {path} | {impact} | {disposition} | {resolution} | {fix_head} |".format(
                        severity=finding.severity.value,
                        path=_table_cell(finding.path),
                        impact=_table_cell(finding.impact),
                        disposition=finding.disposition.value,
                        resolution=_table_cell(finding.resolution),
                        fix_head=finding.fix_head or "—",
                    )
                    for finding in findings
                )
            else:
                lines.append("На последнем проверенном head findings не было.")
            review_text = "\n".join(lines)

        readiness_lines = [
            f"Статус реализации: `{body.readiness.implementation_status}`.",
            f"Итог: `{body.readiness.overall_outcome}`.",
            f"READY_FOR_CHATGPT_REVIEW: `{str(body.readiness.ready_for_chatgpt_review).lower()}`.",
            f"Merge-ready: `{str(body.readiness.merge_ready).lower()}`.",
            f"Внешний reviewer: `{body.readiness.external_reviewer_status}`.",
        ]
        if body.readiness.reviewer_limitation:
            readiness_lines.append(
                f"Ограничение reviewer: {body.readiness.reviewer_limitation}"
            )
        readiness_lines.extend(
            f"- gate `{gate.name}`: `{gate.state.value}`; "
            f"required=`{str(gate.required).lower()}`; evidence: {gate.evidence}"
            for gate in body.readiness.mandatory_gates
        )
        sections = (
            ("Цель", body.goal),
            ("Scope", body.scope),
            ("Реализация", body.implementation),
            ("Проверки", body.checks),
            ("CI", body.ci),
            ("Security / secret scan", body.security_secret_scan),
            ("CodeRabbit review и disposition", review_text),
            ("Readiness", "\n".join(readiness_lines)),
            (
                "Migration / rollback",
                f"Предполагаемый способ merge: `{body.merge_method}`.\n\n{body.migration_rollback}",
            ),
            ("Ограничения", body.limitations),
        )
        rendered = "\n".join(
            f"## {heading}\n\n{value.strip()}" for heading, value in sections
        )
        return _normalize_body(rendered)

    @classmethod
    def validate(cls, body: PullRequestBody, *, base_sha: str, head_sha: str) -> None:
        values = {field: getattr(body, field) for field, _, _ in _BODY_CONTENT_MINIMUMS}
        total_chars = sum(len(value.strip()) for value in values.values())
        if total_chars < _BODY_MINIMUM_TOTAL_CHARS:
            raise _error(
                ResultCode.TOOLING_PR_BODY_INVALID,
                "PR body слишком короткий: нужен полный содержательный отчёт, а не набор коротких тезисов.",
            )
        too_short = [
            heading
            for field, heading, minimum in _BODY_CONTENT_MINIMUMS
            if len(values[field].strip()) < minimum
        ]
        if too_short:
            raise _error(
                ResultCode.TOOLING_PR_BODY_INVALID,
                "Секции PR body недостаточно содержательны: "
                + ", ".join(too_short)
                + ".",
            )
        missing_bullets = [
            heading
            for field, heading, _ in _BODY_CONTENT_MINIMUMS
            if field in _BODY_BULLET_FIELDS
            and not any(
                line.lstrip().startswith(("- ", "* "))
                for line in values[field].splitlines()
            )
        ]
        if missing_bullets:
            raise _error(
                ResultCode.TOOLING_PR_BODY_INVALID,
                "В секциях PR body нужны маркированные факты: "
                + ", ".join(missing_bullets)
                + ".",
            )
        review = body.coderabbit_review
        if review is not None and (
            review.base_sha != base_sha
            or (
                review.reviewed_head != head_sha
                and not review.rate_limit
            )
        ):
            raise _error(
                ResultCode.TOOLING_PR_BODY_INVALID,
                "CodeRabbit evidence в PR body не относится к exact base/head spec "
                "и не содержит явного rate-limit объяснения.",
            )
        # ReadinessState itself enforces the cross-field invariant; keep this
        # explicit at the renderer boundary so a future model replacement does
        # not reintroduce "live missing but ready" PR bodies.
        readiness = body.readiness
        blocking = any(
            gate.required
            and gate.state.value in {"FAIL", "BLOCKED_PRECONDITION"}
            for gate in readiness.mandatory_gates
        )
        if blocking and (
            readiness.ready_for_chatgpt_review
            or readiness.merge_ready
            or readiness.overall_outcome == "READY"
        ):
            raise _error(
                ResultCode.TOOLING_PR_BODY_INVALID,
                "PR body не может быть READY при blocked mandatory gate.",
            )

    @classmethod
    def validate_rendered(cls, rendered: str) -> None:
        normalized = _normalize_body(rendered)
        lines = normalized.splitlines()
        if tuple(line for line in lines if line in _BODY_HEADINGS) != _BODY_HEADINGS:
            raise _error(
                ResultCode.TOOLING_PR_BODY_INVALID,
                "PR body не содержит обязательные разделы в заданном порядке.",
            )

    @staticmethod
    def body_sha256(rendered: str) -> str:
        return hashlib.sha256(_normalize_body(rendered).encode("utf-8")).hexdigest()

    @staticmethod
    def sections() -> tuple[str, ...]:
        return _BODY_HEADINGS


class GitHubProvider:
    """Минимальный `gh pr` adapter без shell и без неявного repository context."""

    def __init__(
        self,
        cwd: Path | None = None,
        runner: StructuredProcessRunner | None = None,
    ) -> None:
        self.cwd = canonical_path(cwd or Path.cwd())
        self.runner = runner or StructuredProcessRunner()
        self.executable = which("gh")

    def _run(self, args: tuple[str, ...], *, timeout_seconds: float = 120.0) -> str:
        if self.executable is None:
            raise _error(
                ResultCode.TOOLING_PROVIDER_UNAVAILABLE,
                "GitHub CLI gh не найден; PR publication заблокирована.",
            )
        try:
            result = self.runner.run(
                ProcessSpec(
                    executable=self.executable,
                    argv=args,
                    cwd=self.cwd,
                    timeout_seconds=timeout_seconds,
                    max_output_bytes=256 * 1024,
                    env={
                        "NO_COLOR": "1",
                        "GH_PAGER": "cat",
                        "GH_PROMPT_DISABLED": "1",
                    },
                )
            )
        except ToolingError as error:
            if error.code in {
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
            }:
                raise _error(
                    ResultCode.TOOLING_PROVIDER_UNAVAILABLE,
                    "Не удалось запустить GitHub CLI gh.",
                ) from error
            raise _error(
                ResultCode.TOOLING_PROVIDER_FAILED,
                "GitHub CLI gh завершился до получения provider evidence.",
            ) from error
        if result.timed_out:
            raise _error(
                ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN,
                "GitHub provider timeout; состояние PR требует read-only recovery.",
                state=OperationState.IN_FLIGHT,
            )
        if result.returncode != 0:
            stderr = result.stderr.casefold()
            if "--json" in args and any(
                marker in stderr for marker in _UNKNOWN_JSON_FIELD_MARKERS
            ):
                raise _error(
                    ResultCode.TOOLING_PROVIDER_UNAVAILABLE,
                    "GitHub CLI gh не поддерживает поля PR JSON; требуется gh >= "
                    f"{_MIN_GH_VERSION} (включая baseRefOid).",
                )
            raise _error(
                ResultCode.TOOLING_PROVIDER_FAILED,
                "GitHub provider отклонил операцию; publication postcondition не доказано.",
            )
        if getattr(result, "stdout_truncated", False) or getattr(
            result, "stderr_truncated", False
        ):
            raise _error(
                ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN,
                "GitHub provider вернул усечённый ответ; postcondition не подтверждено.",
                state=OperationState.IN_FLIGHT,
            )
        return result.stdout

    def create(self, spec: PrPublicationSpec, body_file: Path) -> int:
        output = self._run(
            (
                "pr",
                "create",
                "--repo",
                spec.repository.slug,
                "--base",
                spec.base_ref,
                "--head",
                spec.head_ref,
                "--title",
                spec.title,
                "--draft",
                "--body-file",
                str(body_file),
            ),
            timeout_seconds=15 * 60,
        )
        match = re.search(r"/pull/(\d+)(?:[/?#]|$)", output.strip())
        if match is None:
            raise _error(
                ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN,
                "gh создал PR без распознаваемого URL; состояние требует recovery.",
                state=OperationState.IN_FLIGHT,
            )
        return int(match.group(1))

    def view(self, spec: PrPublicationSpec, number: int) -> dict[str, Any]:
        return self._json(
            (
                "pr",
                "view",
                str(number),
                "--repo",
                spec.repository.slug,
                "--json",
                _PR_FIELDS,
            )
        )

    def list_candidates(self, spec: PrPublicationSpec) -> list[dict[str, Any]]:
        payload = self._json(
            (
                "pr",
                "list",
                "--repo",
                spec.repository.slug,
                "--state",
                "all",
                "--head",
                spec.head_ref,
                "--base",
                spec.base_ref,
                "--limit",
                "20",
                "--json",
                _PR_FIELDS,
            )
        )
        if not isinstance(payload, list):
            raise _error(
                ResultCode.TOOLING_PROVIDER_FAILED,
                "GitHub provider вернул PR list не в ожидаемой форме.",
            )
        return [item for item in payload if isinstance(item, dict)]

    def edit(self, spec: PrPublicationSpec, number: int, body_file: Path) -> None:
        self._run(
            (
                "pr",
                "edit",
                str(number),
                "--repo",
                spec.repository.slug,
                "--body-file",
                str(body_file),
            ),
            timeout_seconds=120.0,
        )

    def _json(self, args: tuple[str, ...]) -> Any:
        output = self._run(args)
        try:
            return json.loads(output)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _error(
                ResultCode.TOOLING_PROVIDER_FAILED,
                "GitHub provider вернул повреждённый JSON.",
            ) from exc


@dataclass(frozen=True)
class _ValidatedPr:
    spec: PrPublicationSpec
    repository: ResolvedRepository
    git: GitClient
    rendered_body: str
    body_sha256: str


class PullRequestService:
    """prepare/publish/verify draft PR с exact GitHub identity и body read-back."""

    def __init__(
        self,
        *,
        resolver: RepositoryResolver | None = None,
        runner: StructuredProcessRunner | None = None,
        git_factory: type[GitClient] = GitClient,
        provider_factory: Callable[[Path, StructuredProcessRunner], GitHubProvider]
        | None = None,
    ) -> None:
        self.runner = runner or StructuredProcessRunner()
        self.resolver = resolver or RepositoryResolver(runner=self.runner)
        self.git_factory = git_factory
        self.provider_factory = provider_factory or (
            lambda root, runner: GitHubProvider(root, runner)
        )

    def prepare(
        self,
        spec_path: str | os.PathLike[str],
        repository_root: str | os.PathLike[str] | None = None,
    ) -> ToolingResult[PrPreparationDetails, PullRequestEvidence]:
        context = self._validate(load_pr_spec(spec_path), repository_root)
        return ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message="PR spec, exact base/head и structured body подтверждены.",
            details=PrPreparationDetails(
                repository=context.spec.repository,
                base_ref=context.spec.base_ref,
                base_sha=context.spec.base_sha,
                head_ref=context.spec.head_ref,
                head_sha=context.spec.head_sha,
                body_sha256=context.body_sha256,
            ),
            evidence=PullRequestEvidence(
                body_sha256=context.body_sha256,
                body_sections=PullRequestBodyRenderer.sections(),
            ),
        )

    def publish(
        self,
        spec_path: str | os.PathLike[str],
        repository_root: str | os.PathLike[str] | None = None,
    ) -> ToolingResult[PullRequestDetails, PullRequestEvidence]:
        context = self._validate(load_pr_spec(spec_path), repository_root)
        provider = self.provider_factory(context.repository.path, self.runner)
        with tempfile.TemporaryDirectory(prefix="azur-pr-body-") as temporary:
            body_file = Path(temporary) / "body.md"
            body_file.write_text(context.rendered_body, encoding="utf-8", newline="\n")
            number, payload = self._find_or_create(context, provider, body_file)
            identity = self._verify_identity(
                context.spec, payload, expected_number=number
            )
            if self._payload_body_sha(payload) != context.body_sha256:
                payload = self._update_body_with_readback(
                    context, provider, number, body_file
                )
                identity = self._verify_identity(
                    context.spec, payload, expected_number=number
                )
            return self._result(context, identity, number, payload)

    def verify(
        self,
        number: int,
        spec_path: str | os.PathLike[str],
        repository_root: str | os.PathLike[str] | None = None,
    ) -> ToolingResult[PullRequestDetails, PullRequestEvidence]:
        if number < 1:
            raise _error(ResultCode.TOOLING_INVALID_INVOCATION, "PR number должен быть положительным.")
        context = self._validate(load_pr_spec(spec_path), repository_root)
        provider = self.provider_factory(context.repository.path, self.runner)
        payload = provider.view(context.spec, number)
        identity = self._verify_identity(
            context.spec, payload, expected_number=number
        )
        if self._payload_body_sha(payload) != context.body_sha256:
            raise _error(
                ResultCode.TOOLING_PR_BODY_INVALID,
                "PR body read-back не совпал с exact rendered body.",
            )
        return self._result(context, identity, number, payload)

    def _find_or_create(
        self,
        context: _ValidatedPr,
        provider: GitHubProvider,
        body_file: Path,
    ) -> tuple[int, dict[str, Any]]:
        if context.spec.pr_number is not None:
            payload = provider.view(context.spec, context.spec.pr_number)
            self._verify_identity(
                context.spec, payload, expected_number=context.spec.pr_number
            )
            return context.spec.pr_number, payload

        candidates = provider.list_candidates(context.spec)
        if len(candidates) > 1:
            raise _error(
                ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN,
                "Для exact base/head найдено несколько PR; duplicate publication запрещена.",
                state=OperationState.UNKNOWN,
            )
        if candidates:
            payload = candidates[0]
            identity = self._verify_identity(context.spec, payload)
            return identity.number, payload

        last_error: ToolingError | None = None
        for attempt in range(2):
            try:
                number = provider.create(context.spec, body_file)
                return number, provider.view(context.spec, number)
            except ToolingError as error:
                last_error = error
                recovered = self._recover_candidate(context, provider)
                if recovered is not None:
                    return recovered
                if error.code is not ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN or attempt:
                    break
        raise _error(
            ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN,
            "Результат PR publication не подтверждён; повторная публикация вслепую запрещена.",
            state=OperationState.IN_FLIGHT,
        ) from last_error

    def _recover_candidate(
        self,
        context: _ValidatedPr,
        provider: GitHubProvider,
    ) -> tuple[int, dict[str, Any]] | None:
        try:
            candidates = provider.list_candidates(context.spec)
        except ToolingError as error:
            raise _error(
                ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN,
                "После неоднозначного provider результата PR list недоступен.",
                state=OperationState.IN_FLIGHT,
            ) from error
        if len(candidates) > 1:
            raise _error(
                ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN,
                "После неоднозначного provider результата найдено несколько PR.",
                state=OperationState.UNKNOWN,
            )
        if not candidates:
            return None
        payload = candidates[0]
        identity = self._verify_identity(context.spec, payload)
        return identity.number, payload

    def _update_body_with_readback(
        self,
        context: _ValidatedPr,
        provider: GitHubProvider,
        number: int,
        body_file: Path,
    ) -> dict[str, Any]:
        """Обновить body с обязательным read-back после любого неоднозначного edit."""

        try:
            provider.edit(context.spec, number, body_file)
        except ToolingError as edit_error:
            try:
                payload = provider.view(context.spec, number)
            except ToolingError as read_error:
                if _is_unknown_provider_result(edit_error):
                    raise _error(
                        ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN,
                        "Результат PR edit неизвестен, а read-back недоступен; повторная mutation запрещена.",
                        state=OperationState.IN_FLIGHT,
                    ) from read_error
                raise edit_error from read_error
            self._verify_identity(
                context.spec, payload, expected_number=number
            )
            if self._payload_body_sha(payload) == context.body_sha256:
                return payload
            if _is_unknown_provider_result(edit_error):
                raise _error(
                    ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN,
                    "PR edit не подтверждён read-back; повторная mutation запрещена.",
                    state=OperationState.IN_FLIGHT,
                ) from edit_error
            raise

        try:
            payload = provider.view(context.spec, number)
        except ToolingError as read_error:
            raise _error(
                ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN,
                "PR edit завершился, но его read-back недоступен; postcondition неизвестно.",
                state=OperationState.IN_FLIGHT,
            ) from read_error
        self._verify_identity(context.spec, payload, expected_number=number)
        if self._payload_body_sha(payload) != context.body_sha256:
            raise _error(
                ResultCode.TOOLING_PR_BODY_INVALID,
                "PR body read-back не совпал с exact rendered body.",
            )
        return payload

    def _validate(
        self,
        spec: PrPublicationSpec,
        repository_root: str | os.PathLike[str] | None,
    ) -> _ValidatedPr:
        if not spec.draft:
            raise _error(
                ResultCode.TOOLING_PR_BODY_INVALID,
                "PR publication contract разрешает только draft=True.",
            )
        _validate_ref(spec.base_ref)
        _validate_ref(spec.head_ref)
        if not _SAFE_REMOTE.fullmatch(spec.remote_name):
            raise _error(ResultCode.TOOLING_PR_BODY_INVALID, "PR remote имеет небезопасное имя.")
        if spec.repository.host.casefold() == "local":
            raise _error(
                ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
                "PR provider требует hosted repository; local/file remote запрещён.",
            )
        rendered = PullRequestBodyRenderer.render(
            spec.body, base_sha=spec.base_sha, head_sha=spec.head_sha
        )
        PullRequestBodyRenderer.validate_rendered(rendered)
        repository = self.resolver.resolve(repository_root)
        git = self.git_factory(repository.path, self.runner)
        actual_identity = repository_identity_from_remote(
            git.remote_url(spec.remote_name)
        )
        if not _same_repository(actual_identity, spec.repository):
            raise _error(
                ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
                "Git remote identity не совпадает с PR spec repository.",
            )
        push_url = git.remote_push_url(spec.remote_name)
        if push_url and push_url.upper() != "DISABLED":
            push_identity = repository_identity_from_remote(push_url)
            if not _same_repository(push_identity, spec.repository):
                raise _error(
                    ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
                    "Git pushurl не совпадает с PR spec repository.",
                )
        if git.active_operation():
            raise _error(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "В checkout уже выполняется незавершённая Git-операция.",
                state=OperationState.CONFLICT,
            )
        if git.branch() != spec.head_ref:
            raise _error(ResultCode.TOOLING_PRECONDITION_FAILED, "Local branch не совпадает с PR spec head_ref.")
        if git.head() != spec.head_sha:
            raise _error(ResultCode.TOOLING_PRECONDITION_FAILED, "Local HEAD не совпадает с PR spec head_sha.")
        if git.remote_ref(spec.remote_name, spec.base_ref) != spec.base_sha:
            raise _error(ResultCode.TOOLING_PRECONDITION_FAILED, "Remote base SHA не совпадает с PR spec.")
        if git.remote_ref(spec.remote_name, spec.head_ref) != spec.head_sha:
            raise _error(ResultCode.TOOLING_PRECONDITION_FAILED, "Remote head SHA не совпадает с PR spec.")
        if not git.is_ancestor(spec.base_sha, spec.head_sha):
            raise _error(ResultCode.TOOLING_PRECONDITION_FAILED, "Base SHA не является предком head SHA.")
        return _ValidatedPr(
            spec=spec,
            repository=repository,
            git=git,
            rendered_body=rendered,
            body_sha256=PullRequestBodyRenderer.body_sha256(rendered),
        )

    def _result(
        self,
        context: _ValidatedPr,
        identity: PullRequestIdentity,
        number: int,
        payload: dict[str, Any],
    ) -> ToolingResult[PullRequestDetails, PullRequestEvidence]:
        return ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message=f"Draft PR #{number} создан или подтверждён с exact identity.",
            details=PullRequestDetails(
                identity=identity,
                body_sha256=context.body_sha256,
            ),
            evidence=PullRequestEvidence(
                body_sha256=context.body_sha256,
                body_sections=PullRequestBodyRenderer.sections(),
            ),
        )

    @staticmethod
    def _payload_body_sha(payload: dict[str, Any]) -> str:
        body = payload.get("body")
        if not isinstance(body, str):
            raise _error(
                ResultCode.TOOLING_PR_BODY_INVALID,
                "Provider PR body имеет неверный тип.",
            )
        return PullRequestBodyRenderer.body_sha256(body)

    @staticmethod
    def _verify_identity(
        spec: PrPublicationSpec,
        payload: dict[str, Any],
        *,
        expected_number: int | None = None,
    ) -> PullRequestIdentity:
        if bool(payload.get("isCrossRepository", False)):
            raise _error(
                ResultCode.TOOLING_PR_IDENTITY_MISMATCH,
                "Cross-repository PR не совпадает с exact same-repository contract.",
            )
        try:
            number = int(payload["number"])
            state = str(payload["state"])
            draft = bool(payload["isDraft"])
            base_ref = str(payload["baseRefName"])
            base_sha_value = payload["baseRefOid"]
            head_ref = str(payload["headRefName"])
            head_sha_value = payload["headRefOid"]
            if not isinstance(base_sha_value, str) or not isinstance(
                head_sha_value, str
            ):
                raise TypeError("provider SHA is not a string")
            base_sha = base_sha_value
            head_sha = head_sha_value
        except (KeyError, TypeError, ValueError) as exc:
            raise _error(
                ResultCode.TOOLING_PR_IDENTITY_MISMATCH,
                "Provider PR identity не содержит обязательные поля.",
            ) from exc
        head_repository = _head_repository_identity(payload, spec)
        try:
            identity = PullRequestIdentity(
                repository=spec.repository,
                number=number,
                state=state,
                draft=draft,
                base_repository=spec.repository,
                base_ref=base_ref,
                base_sha=base_sha,
                head_repository=head_repository,
                head_ref=head_ref,
                head_sha=head_sha,
            )
        except ValidationError as exc:
            raise _error(
                ResultCode.TOOLING_PR_IDENTITY_MISMATCH,
                "Provider PR identity нарушает закрытую схему.",
            ) from exc
        if (
            not _same_repository(identity.repository, spec.repository)
            or not _same_repository(identity.base_repository, spec.repository)
            or identity.base_ref != spec.base_ref
            or identity.base_sha != spec.base_sha
            or not _same_repository(identity.head_repository, spec.repository)
            or identity.head_ref != spec.head_ref
            or identity.head_sha != spec.head_sha
            or identity.draft is not spec.draft
            or identity.state.casefold() != "open"
            or (expected_number is not None and identity.number != expected_number)
        ):
            raise _error(
                ResultCode.TOOLING_PR_IDENTITY_MISMATCH,
                "Provider PR не совпадает с exact repository/base/head/draft identity.",
            )
        return identity


def _head_repository_identity(
    payload: dict[str, Any], spec: PrPublicationSpec
) -> RepositoryIdentity:
    raw = payload.get("headRepository")
    if not isinstance(raw, dict):
        raise _error(
            ResultCode.TOOLING_PR_IDENTITY_MISMATCH,
            "Provider не вернул head repository identity.",
        )
    repository = raw.get("name")
    owner_raw = payload.get("headRepositoryOwner")
    owner = owner_raw.get("login") if isinstance(owner_raw, dict) else None
    if not isinstance(repository, str) or not isinstance(owner, str):
        raise _error(
            ResultCode.TOOLING_PR_IDENTITY_MISMATCH,
            "Provider head repository identity имеет неверный формат.",
        )

    return RepositoryIdentity(
        host=spec.repository.host,
        owner=owner,
        repository=repository,
    )


def _table_cell(value: str) -> str:
    return " ".join(value.replace("|", "\\|").splitlines()).strip()


def _normalize_body(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def _validate_ref(value: str) -> None:
    if (
        not _SAFE_REF.fullmatch(value)
        or value.startswith("/")
        or value.endswith(("/", "."))
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(character in value for character in "~^:?*[\\")
    ):
        raise _error(ResultCode.TOOLING_PR_BODY_INVALID, "Git ref имеет небезопасный формат.")


def _same_repository(left: Any, right: Any) -> bool:
    return (
        left.host.casefold() == right.host.casefold()
        and left.owner.casefold() == right.owner.casefold()
        and left.repository.casefold() == right.repository.casefold()
    )


def _is_unknown_provider_result(error: ToolingError) -> bool:
    return error.code in {
        ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN,
        ResultCode.TOOLING_TIMEOUT,
    } or error.state in {OperationState.IN_FLIGHT, OperationState.UNKNOWN}


__all__ = [
    "GitHubProvider",
    "PullRequestBodyRenderer",
    "PullRequestService",
    "load_pr_spec",
]
