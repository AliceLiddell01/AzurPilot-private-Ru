from __future__ import annotations

import hashlib
import json
from typing import Any

from dev_tools.russianization_audit import compact_json_bytes


GUI_POLICY_SCOPE_SHA256 = (
    "8b32ea25218501cb199dff243ed43e1ced16bff28b971a3d6f9d8d37f3b0d942"
)
GUI_POLICY_IDENTIFIERS = tuple(
    f"log-call:{value:04d}"
    for value in (
        1,
        2,
        3,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        18,
        21,
        27,
        29,
        30,
        31,
        32,
        33,
        34,
        35,
        36,
        37,
        38,
        39,
        40,
        41,
        42,
        43,
        44,
        45,
        46,
        47,
        48,
        49,
        53,
        54,
        55,
        56,
        57,
        58,
        59,
        60,
        61,
        62,
        63,
        64,
        65,
        66,
        67,
        68,
        69,
        71,
        72,
        73,
        74,
    )
)


def _digest(rows: list[dict[str, Any]]) -> str:
    selected = [
        "\0".join(
            (
                row["path"],
                row["stable_identifier"],
                row["message_or_template"],
            )
        )
        for row in rows
        if row["path"] == "gui.py"
        and row["stable_identifier"] in GUI_POLICY_IDENTIFIERS
    ]
    return hashlib.sha256("\n".join(sorted(selected)).encode("utf-8")).hexdigest()


def apply_gui_stable_policy(
    outputs: dict[str, bytes], metrics: dict[str, Any]
) -> tuple[dict[str, bytes], dict[str, Any], list[str]]:
    """Исключить только доказанные точки актуального gui.py из donor recovery.

    ``gui.py`` не входил в WIP Stage 7. После ответвления donor в нём появился
    обязательный no-reload/orphan-recovery contract. Поэтому старую версию файла
    переносить запрещено, а каждая сохранённая запись фиксируется отдельно и
    остаётся видимой в общем russianization backlog.
    """
    table = json.loads(outputs["scope.json"])
    rows = [
        dict(zip(table["columns"], row, strict=True))
        for row in table["entries"]
    ]
    lookup = {
        row["stable_identifier"]: row
        for row in rows
        if row["path"] == "gui.py"
    }
    errors = [
        f"Отсутствует gui stable-policy точка: gui.py {identifier}"
        for identifier in GUI_POLICY_IDENTIFIERS
        if identifier not in lookup
    ]
    digest = _digest(rows)
    if digest != GUI_POLICY_SCOPE_SHA256:
        errors.append(
            "Изменился digest точечных gui stable-policy шаблонов: "
            f"expected={GUI_POLICY_SCOPE_SHA256}, actual={digest}"
        )

    if not errors:
        for identifier in GUI_POLICY_IDENTIFIERS:
            row = lookup[identifier]
            row.update(
                {
                    "classification": "superseded_by_stable",
                    "stage_owner": "stable_contract",
                    "runtime_owner": "post-divergence WebUI supervisor contract",
                    "translation_required": False,
                    "evidence": (
                        "Точечная запись актуального WebUI supervisor. gui.py не "
                        "входил в donor WIP Stage 7 и содержит post-divergence "
                        "no-reload/orphan-recovery contract; перенос старой версии "
                        "запрещён. Запись остаётся в общем russianization backlog."
                    ),
                }
            )

    outputs = dict(outputs)
    outputs["scope.json"] = compact_json_bytes(
        {
            "schema_version": table["schema_version"],
            "columns": table["columns"],
            "entries": [
                [row[column] for column in table["columns"]]
                for row in rows
            ],
        }
    )
    return outputs, dict(metrics), errors
