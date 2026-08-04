from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from dev_tools.russianization_audit import compact_json_bytes, json_bytes
from dev_tools.stage7_log_audit import BLOCKING_METRICS, COLUMNS


POLICY_SCOPE_SHA256 = "61daf92530b1b16080fdce1b7b424ae19cda12bdc5451c6e7adf1927c7873204"


def _ids(*values: int) -> tuple[str, ...]:
    return tuple(f"log-call:{value:04d}" for value in values)


POLICY_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "classification": "raw_external_payload",
        "stage_owner": "stage7",
        "runtime_owner": "machine/runtime expression",
        "evidence": (
            "Выражение или runtime-значение сохраняется без перевода; "
            "русифицируется окружающий first-party контекст."
        ),
        "points": {
            "deploy/Windows/alas.py": _ids(7),
            "deploy/Windows/logger.py": _ids(1, 2, 3, 4),
            "deploy/docker/deploy-image.sh": _ids(113, 114, 119),
            "deploy/install/emulator_windows.py": _ids(1),
            "deploy/logger.py": _ids(1, 2, 3, 4),
            "deploy/uv.py": _ids(2, 4),
            "module/config/code_generator.py": _ids(1),
            "module/config/time_source.py": _ids(2, 3),
            "module/logger.py": _ids(4, 5, 6, 7, 8, 25),
            "module/webui/launcher.py": _ids(1),
            "module/webui/patch.py": _ids(2, 3),
            "module/webui/process_manager.py": _ids(29),
            "module/webui/setting.py": _ids(1),
            "module/webui/utils.py": _ids(2),
            "module/webui/widgets.py": _ids(1),
            "scripts/Build-AzurPilot.ps1": _ids(1, 3),
            "scripts/Repair-AzurPilot.ps1": _ids(1, 3),
            "scripts/Start-AzurPilot.ps1": _ids(1, 3),
            "scripts/Update-AzurPilot.ps1": _ids(1),
            "scripts/lib/AzurPilot.Shortcut.psm1": _ids(8, 13, 14),
        },
    },
    {
        "classification": "technical_identifier",
        "stage_owner": "stage7",
        "runtime_owner": "logger formatting primitive",
        "evidence": "Структурный форматтер logger, а не пользовательское сообщение.",
        "points": {
            "deploy/Windows/logger.py": _ids(5, 6),
            "deploy/logger.py": _ids(5, 6),
            "module/logger.py": _ids(9, 10, 11),
        },
    },
    {
        "classification": "technical_identifier",
        "stage_owner": "stage7",
        "runtime_owner": "deploy command/output template",
        "evidence": (
            "Машинно-читаемый command/output template сохраняется без перевода."
        ),
        "points": {
            "deploy/docker/deploy-image.sh": _ids(109, 116, 117),
        },
    },
    {
        "classification": "stage8b_ocr",
        "stage_owner": "stage8b",
        "runtime_owner": "Stage 8B OCR model scope",
        "evidence": (
            "Точный guard запрещает выбор удалённых non-English OCR-моделей "
            "и принадлежит Stage 8B."
        ),
        "points": {
            "module/config/config.py": _ids(5),
        },
    },
    {
        "classification": "stage8c_scheduler",
        "stage_owner": "stage8c",
        "runtime_owner": "Stage 8C scheduler/task runtime",
        "evidence": "Точечный scheduler/task лог передаётся в Stage 8C.",
        "points": {
            "module/config/config.py": _ids(7, 9, 14, 20),
        },
    },
    {
        "classification": "stage8e_operation_siren",
        "stage_owner": "stage8e",
        "runtime_owner": "Stage 8E Operation Siren runtime",
        "evidence": (
            "Лог описывает цикл сброса Operation Siren и передаётся в Stage 8E."
        ),
        "points": {
            "module/config/utils.py": _ids(5, 6),
        },
    },
    {
        "classification": "stage8a_device",
        "stage_owner": "stage8a",
        "runtime_owner": "Stage 8A device/GPU runtime",
        "evidence": (
            "Лог описывает выбор GPU/device backend и передаётся в Stage 8A."
        ),
        "points": {
            "module/config/utils.py": _ids(7, 8, 9, 10),
        },
    },
    {
        "classification": "stage8a_device",
        "stage_owner": "stage8a",
        "runtime_owner": "Stage 8A device/input/live preview runtime",
        "evidence": (
            "Лог относится к device/scrcpy/screenshot/control API и "
            "передаётся в Stage 8A."
        ),
        "points": {
            "module/webui/api.py": _ids(
                1, 2, 3, 5, 6, 10, 11, 12, 13, 14, 15, 18, 20, 22, 24,
                25, 26, 27, 28, 30, 31, 32, 34, 35, 36, 37, 38, 39, 40,
                41, 42,
            ),
        },
    },
    {
        "classification": "stage8c_scheduler",
        "stage_owner": "stage8c",
        "runtime_owner": "Stage 8C shared WebUI/config runtime",
        "evidence": (
            "Лог относится к WebUI deployment/config API и передаётся в Stage 8C."
        ),
        "points": {
            "module/webui/api.py": _ids(43, 44, 45, 46, 47, 48, 49),
        },
    },
    {
        "classification": "stage8c_scheduler",
        "stage_owner": "stage8c",
        "runtime_owner": "Stage 8C shared feature runtime",
        "evidence": (
            "Функциональный WebUI-лог не относится к lifecycle Stage 7 и "
            "точечно передаётся в Stage 8C."
        ),
        "points": {
            "module/webui/app_event_tools.py": _ids(1),
            "module/webui/app_home.py": _ids(1, 2, 3),
            "module/webui/app_stat_commission.py": _ids(1),
            "module/webui/event_calculator.py": _ids(1, 2, 4),
        },
    },
    {
        "classification": "stage8e_operation_siren",
        "stage_owner": "stage8e",
        "runtime_owner": "Stage 8E Operation Siren runtime",
        "evidence": (
            "Лог относится к Operation Siren simulator и передаётся в Stage 8E."
        ),
        "points": {
            "module/webui/app_task_config.py": _ids(1),
        },
    },
    {
        "classification": "stage8c_scheduler",
        "stage_owner": "stage8c",
        "runtime_owner": "Stage 8C scheduler/task runtime",
        "evidence": "Лог относится к сохранению task config и передаётся в Stage 8C.",
        "points": {
            "module/webui/app_task_config.py": _ids(2, 3),
        },
    },
    {
        "classification": "technical_identifier",
        "stage_owner": "stage7",
        "runtime_owner": "runtime expression/technical identifier",
        "evidence": (
            "Точечное runtime-выражение или technical identifier, "
            "не подлежащий переводу."
        ),
        "points": {
            "module/webui/process_manager.py": _ids(1),
        },
    },
    {
        "classification": "stage8c_scheduler",
        "stage_owner": "stage8c",
        "runtime_owner": "Stage 8C integrations/shared runtime",
        "evidence": (
            "Сообщение относится к remote-access integration и "
            "передаётся в Stage 8C."
        ),
        "points": {
            "module/webui/remote_access.py": _ids(5, 6),
        },
    },
    {
        "classification": "test_fixture",
        "stage_owner": "developer",
        "runtime_owner": "developer failpoint fixture",
        "evidence": (
            "Точный TEST FAILPOINT используется только тестовым контуром rollback."
        ),
        "points": {
            "scripts/lib/AzurPilot.Shortcut.psm1": _ids(10, 11),
        },
    },
)


def _policy_lookup() -> dict[tuple[str, str], dict[str, str]]:
    lookup: dict[tuple[str, str], dict[str, str]] = {}
    for group in POLICY_GROUPS:
        for path, identifiers in group["points"].items():
            for identifier in identifiers:
                key = (path, identifier)
                if key in lookup:
                    raise RuntimeError(f"Дублирующая policy-точка: {path} {identifier}")
                lookup[key] = {
                    "classification": group["classification"],
                    "stage_owner": group["stage_owner"],
                    "runtime_owner": group["runtime_owner"],
                    "evidence": group["evidence"],
                }
    return lookup


def _scope_digest(rows: list[dict[str, Any]], keys: set[tuple[str, str]]) -> str:
    selected = []
    for row in rows:
        key = (row["path"], row["stable_identifier"])
        if key in keys:
            selected.append(
                "\0".join(
                    (row["path"], row["stable_identifier"], row["message_or_template"])
                )
            )
    return hashlib.sha256("\n".join(sorted(selected)).encode("utf-8")).hexdigest()


def apply_stage7_policy(
    outputs: dict[str, bytes], metrics: dict[str, Any]
) -> tuple[dict[str, bytes], dict[str, Any], list[str]]:
    table = json.loads(outputs["scope.json"])
    rows = [
        dict(zip(table["columns"], row, strict=True))
        for row in table["entries"]
    ]
    lookup = _policy_lookup()
    row_lookup = {
        (row["path"], row["stable_identifier"]): row
        for row in rows
    }
    missing = sorted(set(lookup) - set(row_lookup))
    errors = [f"Отсутствует policy-точка: {path} {identifier}" for path, identifier in missing]

    digest = _scope_digest(rows, set(lookup))
    if digest != POLICY_SCOPE_SHA256:
        errors.append(
            "Изменился digest точечных Stage 7 policy-шаблонов: "
            f"expected={POLICY_SCOPE_SHA256}, actual={digest}"
        )

    if not errors:
        for key, decision in lookup.items():
            row = row_lookup[key]
            row.update(decision)
            row["translation_required"] = False

    stage7 = [row for row in rows if row["stage_owner"] == "stage7"]
    transfers = [
        row for row in rows if str(row["stage_owner"]).startswith("stage8")
    ]
    metrics = dict(metrics)
    metrics.update(
        {
            "stage7_candidates_total": len(stage7),
            "stage7_translated": sum(
                row["classification"] == "stage7_first_party_message"
                and not row["translation_required"]
                for row in stage7
            ),
            "stage7_reviewed_technical": sum(
                row["classification"]
                in {"technical_identifier", "raw_external_payload"}
                for row in stage7
            ),
            "stage7_unresolved": sum(
                bool(row["translation_required"]) for row in stage7
            ),
            "stage7_unknown_classifications": sum(
                row["classification"] == "unknown" for row in stage7
            )
            + len(errors),
            "stage7_invalid_stage8_transfers": sum(
                not row["runtime_owner"].startswith("Stage 8")
                or not row["evidence"].strip()
                for row in transfers
            ),
            "stage7_policy_points": len(lookup),
            "stage7_policy_digest": digest,
        }
    )

    table = {
        "schema_version": table["schema_version"],
        "columns": list(COLUMNS),
        "entries": [[row[column] for column in COLUMNS] for row in rows],
    }
    outputs = dict(outputs)
    outputs["scope.json"] = compact_json_bytes(table)
    outputs["metrics.json"] = json_bytes(metrics)
    status = "PASS" if not any(metrics[key] for key in BLOCKING_METRICS) else "FAIL"
    report_lines = [
        "# Stage 7 — семантический аудит журналов",
        "",
        f"Статус: **{status}**",
        "",
        *(f"- {key}: {value}" for key, value in metrics.items()),
    ]
    if errors:
        report_lines.extend(("", "## Policy errors", ""))
        report_lines.extend(f"- {error}" for error in errors)
    outputs["report.md"] = ("\n".join(report_lines) + "\n").encode("utf-8")
    return outputs, metrics, errors
