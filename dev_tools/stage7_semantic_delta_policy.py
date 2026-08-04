from __future__ import annotations

from typing import Any


EXPECTED_DELTAS = {
    (
        "placeholder_mismatch",
        "module/webui/process_manager.py",
        "logger.exception",
        "ex",
        "logger.exception",
        "[{…}] Необработанная ошибка рабочего процесса: {…}",
    ),
    (
        "sequence_insert",
        "module/webui/utils.py",
        "",
        "",
        "traceback_console.print",
        "renderable",
    ),
    (
        "sequence_delete",
        "module/webui/utils.py",
        "raise",
        "quq",
        "",
        "",
    ),
    (
        "sequence_insert",
        "module/config/config.py",
        "",
        "",
        "raise",
        "Неподдерживаемая OCR-модель: {…}",
    ),
}


def _descriptor(finding: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    old_rows = finding.get("old") or []
    new_rows = finding.get("new") or []
    if isinstance(old_rows, dict):
        old_rows = [old_rows]
    if isinstance(new_rows, dict):
        new_rows = [new_rows]
    old = old_rows[0] if len(old_rows) == 1 else {}
    new = new_rows[0] if len(new_rows) == 1 else {}
    return (
        str(finding.get("kind", "")),
        str(finding.get("path", "")),
        str(old.get("call_kind", "")),
        str(old.get("message_or_template", "")),
        str(new.get("call_kind", "")),
        str(new.get("message_or_template", "")),
    )


def apply_semantic_delta_policy(
    metrics: dict[str, Any], findings: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """Разрешить только доказанные дельты scanner inventory.

    Инварианты не отключаются. Любое новое, исчезнувшее или изменённое finding
    снова блокирует Stage 7. OCR guard принадлежит Stage 8B и разрешён только
    по точному path/call/template descriptor.
    """
    actual = {_descriptor(finding) for finding in findings}
    errors = []
    if actual != EXPECTED_DELTAS:
        missing = sorted(EXPECTED_DELTAS - actual)
        unexpected = sorted(actual - EXPECTED_DELTAS)
        if missing:
            errors.append(f"Отсутствуют ожидаемые semantic deltas: {missing}")
        if unexpected:
            errors.append(f"Обнаружены неизвестные semantic deltas: {unexpected}")

    result = dict(metrics)
    if not errors:
        result["stage7_placeholder_mismatches"] = 0
        result["stage7_sequence_mismatches"] = 0
        result["stage7_approved_semantic_deltas"] = len(EXPECTED_DELTAS)
    else:
        result["stage7_unknown_classifications"] = (
            int(result.get("stage7_unknown_classifications", 0)) + len(errors)
        )
    return result, errors
