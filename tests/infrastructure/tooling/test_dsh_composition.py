"""Семантическая проверка эффективной композиции продуктового профиля Harness."""

from __future__ import annotations

from pathlib import Path

import pytest

import azurpilot.tooling.dsh_composition as composition
from azurpilot.dsh import BRIDGE_GUARD_PATH, BRIDGE_OVERLAY_PATH
from azurpilot.tooling.dsh_composition import (
    CompositionEntry,
    CompositionExpression,
    load_composition,
    overlay_composition,
    resolve_plugin_asset,
    validate_composition,
)
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.result import ExitCode, ResultCode, exit_code_for
from module.mcp_shared.windows_mcp_bridge_contract import BRIDGE_ROUTES

ROUTES = {route.server_name: route for route in BRIDGE_ROUTES.values()}

MCP_CLIENT = "@deepseek-ai/dsh-mcp-client"


def _composed_from_overlay() -> list[CompositionEntry]:
    """Собрать дерево так, как его печатает `--dump-config`.

    Относительное имя ассета из overlay-файла композитор превращает в
    абсолютный `file://`-адрес, поэтому проверка обязана сравнивать записи
    семантически, а не текстом строк.
    """

    composed: list[CompositionEntry] = []
    for entry in overlay_composition():
        plugin = entry.plugin
        if plugin.startswith("./"):
            plugin = BRIDGE_GUARD_PATH.as_uri()
        composed.append(
            CompositionEntry(entry.entry_id, plugin, dict(entry.config), entry.disabled)
        )
    composed.append(
        CompositionEntry(
            "mcp-github",
            "@modelcontextprotocol/server-github",
            {"serverName": "github", "transport": "stdio"},
        )
    )
    return composed


def _replace(
    composed: list[CompositionEntry], entry_id: str, **changes: object
) -> list[CompositionEntry]:
    updated: list[CompositionEntry] = []
    for entry in composed:
        if entry.entry_id != entry_id:
            updated.append(entry)
            continue
        updated.append(
            CompositionEntry(
                changes.get("entry_id", entry.entry_id),
                changes.get("plugin", entry.plugin),
                changes.get("config", dict(entry.config)),
                changes.get("disabled", entry.disabled),
            )
        )
    return updated


def test_tracked_overlay_declares_guard_and_both_bridge_clients() -> None:
    entries = overlay_composition()
    by_id = {entry.entry_id: entry for entry in entries}

    assert set(by_id) == {
        "azurpilot-bridge-guard",
        "mcp-azurpilot-dev",
        "mcp-azurpilot-game",
    }
    guard = by_id["azurpilot-bridge-guard"]
    assert resolve_plugin_asset(guard, BRIDGE_OVERLAY_PATH.parent) == (
        BRIDGE_GUARD_PATH.resolve()
    )
    for server_name in ("azurpilot-dev", "azurpilot-game"):
        owner = by_id[f"mcp-{server_name}"]
        assert owner.plugin == MCP_CLIENT
        assert owner.config["serverName"] == server_name


def test_effective_composition_realises_the_overlay() -> None:
    validate_composition(
        overlay_composition(),
        _composed_from_overlay(),
        overlay_dir=BRIDGE_OVERLAY_PATH.parent,
    )


def test_effective_composition_allows_unrelated_entries() -> None:
    """Проверка не мешает остальным плагинам профиля."""

    composed = _composed_from_overlay()
    composed.extend(
        [
            CompositionEntry(
                "mcp-local",
                MCP_CLIENT,
                {"serverName": "local-mcp", "transport": "stdio"},
            ),
            CompositionEntry(None, "@example/anonymous-plugin", {}),
        ]
    )

    validate_composition(
        overlay_composition(), composed, overlay_dir=BRIDGE_OVERLAY_PATH.parent
    )


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            lambda composed: [
                *composed,
                CompositionEntry(
                    "stale-mcp-azurpilot-dev",
                    MCP_CLIENT,
                    {"serverName": "azurpilot-dev", "transport": "streamable-http"},
                ),
            ],
            id="чужая-запись-заявляет-маршрут-dev",
        ),
        pytest.param(
            lambda composed: _replace(
                composed, "azurpilot-bridge-guard", disabled=True
            ),
            id="страж-отключён",
        ),
        pytest.param(
            lambda composed: _replace(
                composed,
                "azurpilot-bridge-guard",
                disabled=CompositionExpression("process.env.AZURPILOT_SKIP_GUARD"),
            ),
            id="страж-отключён-выражением",
        ),
        pytest.param(
            lambda composed: _replace(
                composed,
                "mcp-azurpilot-game",
                config={
                    **next(
                        entry.config
                        for entry in composed
                        if entry.entry_id == "mcp-azurpilot-game"
                    ),
                    "url": "http://127.0.0.1:9999/game/mcp",
                },
            ),
            id="подменён-маршрут-game",
        ),
        pytest.param(
            lambda composed: [
                entry for entry in composed if entry.entry_id != "mcp-azurpilot-game"
            ],
            id="потерян-клиент-game",
        ),
        pytest.param(
            lambda composed: _replace(
                composed, "azurpilot-bridge-guard", plugin="@example/stale-guard"
            ),
            id="подменён-плагин-стража",
        ),
        pytest.param(
            lambda composed: _replace(
                composed,
                "mcp-azurpilot-dev",
                config={
                    **next(
                        entry.config
                        for entry in composed
                        if entry.entry_id == "mcp-azurpilot-dev"
                    ),
                    "failOnStartupError": False,
                },
            ),
            id="клиент-dev-больше-не-обязателен",
        ),
        pytest.param(
            lambda composed: [
                *composed,
                CompositionEntry(
                    "mcp-azurpilot-dev-copy",
                    MCP_CLIENT,
                    dict(
                        next(
                            entry.config
                            for entry in composed
                            if entry.entry_id == "mcp-azurpilot-dev"
                        )
                    ),
                ),
            ],
            id="вторая-запись-того-же-клиента",
        ),
    ],
)
def test_effective_composition_refuses_conflicts(mutation) -> None:
    with pytest.raises(ToolingError) as failure:
        validate_composition(
            overlay_composition(),
            mutation(_composed_from_overlay()),
            overlay_dir=BRIDGE_OVERLAY_PATH.parent,
        )

    assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


def test_effective_composition_refuses_literal_environment_swap() -> None:
    """Literal-значение вместо выражения `!!js` меняет контракт клиента."""

    composed = _composed_from_overlay()
    dev = next(entry for entry in composed if entry.entry_id == "mcp-azurpilot-dev")
    headers = {
        name: (value.expression if isinstance(value, CompositionExpression) else value)
        for name, value in dev.config["headers"].items()
    }
    mutated = _replace(
        composed,
        "mcp-azurpilot-dev",
        config={**dev.config, "headers": headers},
    )

    with pytest.raises(ToolingError) as failure:
        validate_composition(
            overlay_composition(), mutated, overlay_dir=BRIDGE_OVERLAY_PATH.parent
        )

    assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


def test_effective_composition_refuses_duplicated_identifier() -> None:
    composed = [
        *_composed_from_overlay(),
        CompositionEntry("mcp-azurpilot-dev", MCP_CLIENT, {}),
    ]

    with pytest.raises(ToolingError) as failure:
        validate_composition(
            overlay_composition(), composed, overlay_dir=BRIDGE_OVERLAY_PATH.parent
        )

    assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


@pytest.mark.parametrize(
    "overlay_text",
    [
        pytest.param("- insert:\n    - id: mcp-azurpilot-dev\n", id="без-клиента-game"),
        pytest.param(
            "- insert:\n    - id: azurpilot-bridge-guard\n      name: ./other.mjs\n",
            id="чужой-ассет-стража",
        ),
    ],
)
def test_overlay_must_declare_guard_and_both_routes(
    tmp_path: Path, overlay_text: str
) -> None:
    broken = tmp_path / "broken.patch.yml"
    broken.write_text(overlay_text, encoding="utf-8")

    with pytest.raises(ToolingError) as failure:
        validate_composition(
            overlay_composition(broken),
            _composed_from_overlay(),
            overlay_dir=tmp_path,
        )

    assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


def test_overlay_must_use_canonical_route_and_mandatory_activation(
    tmp_path: Path,
) -> None:
    dev = next(
        entry
        for entry in overlay_composition()
        if entry.entry_id == "mcp-azurpilot-dev"
    )
    game = next(
        entry
        for entry in overlay_composition()
        if entry.entry_id == "mcp-azurpilot-game"
    )
    guard = next(
        entry
        for entry in overlay_composition()
        if entry.entry_id == "azurpilot-bridge-guard"
    )
    broken = tmp_path / "broken.patch.yml"
    broken.write_text(
        "- insert:\n"
        f"    - id: {guard.entry_id}\n"
        f"      name: {guard.plugin}\n"
        f"    - id: {dev.entry_id}\n"
        f"      name: '{dev.plugin}'\n"
        f"      config:\n"
        f"        serverName: azurpilot-dev\n"
        f"        url: {ROUTES['azurpilot-dev'].bridge_url}\n"
        f"        failOnStartupError: false\n"
        f"    - id: {game.entry_id}\n"
        f"      name: '{game.plugin}'\n"
        f"      config:\n"
        f"        serverName: azurpilot-game\n"
        f"        url: http://127.0.0.1:9999/game/mcp\n"
        f"        failOnStartupError: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ToolingError) as failure:
        validate_composition(
            overlay_composition(broken),
            _composed_from_overlay(),
            overlay_dir=tmp_path,
        )

    assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


def test_composition_reader_keeps_expressions_and_refuses_damaged_input() -> None:
    entries = load_composition(
        "- id: mcp-azurpilot-dev\n"
        f"  name: '{MCP_CLIENT}'\n"
        "  config:\n"
        "    serverName: azurpilot-dev\n"
        "    headers:\n"
        "      Authorization: !!js '`Bearer ${process.env.TOKEN}`'\n"
    )

    assert len(entries) == 1
    headers = entries[0].config["headers"]
    assert isinstance(headers["Authorization"], CompositionExpression)
    assert headers["Authorization"] == CompositionExpression(
        "`Bearer ${process.env.TOKEN}`"
    )
    assert headers["Authorization"] != CompositionExpression("чужое выражение")

    assert load_composition("") == ()
    for damaged in ("- id: [", "just-a-string", "id: без-списка"):
        with pytest.raises(ToolingError) as failure:
            load_composition(damaged)
        assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


def test_plugin_asset_resolution_distinguishes_assets_from_packages(
    tmp_path: Path,
) -> None:
    relative = CompositionEntry("guard", "./guard.mjs", {})
    absolute = CompositionEntry("guard", (tmp_path / "guard.mjs").as_uri(), {})
    package = CompositionEntry("client", MCP_CLIENT, {})

    assert (
        resolve_plugin_asset(relative, tmp_path) == (tmp_path / "guard.mjs").resolve()
    )
    assert (
        resolve_plugin_asset(absolute, tmp_path) == (tmp_path / "guard.mjs").resolve()
    )
    assert resolve_plugin_asset(package, tmp_path) is None


def test_composition_failure_is_a_precondition() -> None:
    assert exit_code_for(ResultCode.TOOLING_PRECONDITION_FAILED) is (
        ExitCode.PRECONDITION
    )


def test_composition_rejects_unknown_server_names() -> None:
    """Канонический набор маршрутов моста принадлежит владельцу идентичности."""

    assert set(composition.BRIDGE_MCP_SERVER_NAMES) == {
        "azurpilot-dev",
        "azurpilot-game",
    }


def _refusal(composed: list[CompositionEntry]) -> str:
    with pytest.raises(ToolingError) as failure:
        validate_composition(
            overlay_composition(), composed, overlay_dir=BRIDGE_OVERLAY_PATH.parent
        )
    assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED
    return str(failure.value)


def test_nested_group_client_cannot_shadow_required_route() -> None:
    """Дочерняя запись группы резервирует сервер в той же корневой области."""

    for container in ("group: true", "name: cordis:group"):
        composed = [
            *_composed_from_overlay(),
            *load_composition(
                f"- id: extra-group\n"
                f"  {container}\n"
                f"  config:\n"
                f"    - id: stale-dev\n"
                f"      name: '{MCP_CLIENT}'\n"
                f"      config:\n"
                f"        serverName: azurpilot-dev\n"
                f"        url: http://127.0.0.1:9999/dev/mcp\n"
            ),
        ]
        assert "azurpilot-dev" in _refusal(composed)


def test_group_with_unrelated_children_is_allowed() -> None:
    """Контейнер сам по себе не конфликтует: значение имеет заявка сервера."""

    composed = [
        *_composed_from_overlay(),
        *load_composition(
            "- id: planning\n"
            "  name: cordis:group\n"
            "  group: true\n"
            "  config:\n"
            "    - id: plan-mode\n"
            "      name: '@deepseek-ai/dsh-plan-mode'\n"
            "      config:\n"
            "        section: режим\n"
        ),
    ]

    validate_composition(
        overlay_composition(), composed, overlay_dir=BRIDGE_OVERLAY_PATH.parent
    )


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(
            "- id: js-dev\n"
            f"  name: '{MCP_CLIENT}'\n"
            "  config:\n"
            "    serverName: !!js 'azurpilot-dev'\n",
            id="выражение-вместо-имени-сервера",
        ),
        pytest.param(
            "- id: nameless\n  config:\n    serverName: azurpilot-dev\n",
            id="запись-без-имени-плагина",
        ),
        pytest.param(
            f"- id: no-server-name\n  name: '{MCP_CLIENT}'\n"
            "  config:\n    url: http://127.0.0.1:9999/dev/mcp\n",
            id="клиент-без-объявленного-сервера",
        ),
        pytest.param(
            "- id: expression-container\n"
            "  name: !!js 'cordis:group'\n"
            "  config:\n"
            "    - id: nested\n"
            f"      name: '{MCP_CLIENT}'\n",
            id="неразбираемое-имя-со-списком-записей",
        ),
    ],
)
def test_unverifiable_claim_is_refused(row: str) -> None:
    """Неизвестное состояние заявки — запрет, а не отсутствие заявки."""

    with pytest.raises(ToolingError) as failure:
        composed = [*_composed_from_overlay(), *load_composition(row)]
        validate_composition(
            overlay_composition(), composed, overlay_dir=BRIDGE_OVERLAY_PATH.parent
        )

    assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


def test_expression_disabled_duplicate_client_is_still_a_claim() -> None:
    """`disabled` выражением не снимает запись статически, поэтому это заявка."""

    template = (
        "- id: off-dev\n"
        f"  name: '{MCP_CLIENT}'\n"
        "  config:\n"
        "    serverName: azurpilot-dev\n"
        "    url: http://127.0.0.1:9999/dev/mcp\n"
        "  disabled: {disabled}\n"
    )
    expressed = load_composition(template.format(disabled="!!js 'false'"))
    assert "azurpilot-dev" in _refusal([*_composed_from_overlay(), *expressed])

    literal = load_composition(template.format(disabled="true"))
    validate_composition(
        overlay_composition(),
        [*_composed_from_overlay(), *literal],
        overlay_dir=BRIDGE_OVERLAY_PATH.parent,
    )


def test_include_file_is_walked_for_claims(tmp_path: Path) -> None:
    """Смонтированный файл входит в ту же область и не может скрыть дубликат."""

    duplicate = tmp_path / "extra.yml"
    duplicate.write_text(
        f"- id: stale-game\n  name: '{MCP_CLIENT}'\n"
        "  config:\n    serverName: azurpilot-game\n",
        encoding="utf-8",
    )
    unrelated = tmp_path / "other.yml"
    unrelated.write_text(
        f"- id: other\n  name: '{MCP_CLIENT}'\n  config:\n    serverName: other\n",
        encoding="utf-8",
    )
    loop = tmp_path / "loop.yml"
    loop.write_text(
        "- id: loop\n  name: cordis:include\n  config:\n    path: loop.yml\n",
        encoding="utf-8",
    )

    def composed(path: str) -> list[CompositionEntry]:
        return [
            *_composed_from_overlay(),
            *load_composition(
                f"- id: include\n  name: cordis:include\n  config:\n    path: {path}\n"
            ),
        ]

    assert "azurpilot-game" in _refusal(composed(str(duplicate)))
    validate_composition(
        overlay_composition(),
        composed(str(unrelated)),
        overlay_dir=BRIDGE_OVERLAY_PATH.parent,
    )
    assert "не объявляет буквальный путь" in _refusal(
        composed("!!js 'process.env.AZURPILOT_EXTRA_ENTRIES'")
    )
    assert "недоступен для проверки" in _refusal(
        composed(str(tmp_path / "missing.yml"))
    )
    assert "образуют цикл" in _refusal(composed(str(loop)))


def test_literal_disabled_subtree_is_not_a_claim(tmp_path: Path) -> None:
    """Литеральное `disabled: true` снимает запись вместе с её составом."""

    composed = [
        *_composed_from_overlay(),
        *load_composition(
            "- id: off-group\n"
            "  group: true\n"
            "  disabled: true\n"
            "  config:\n"
            "    - id: stale-dev\n"
            f"      name: '{MCP_CLIENT}'\n"
            "      config:\n"
            "        serverName: azurpilot-dev\n"
            "- id: off-include\n"
            "  name: cordis:include\n"
            "  disabled: true\n"
            "  config:\n"
            f"    path: {tmp_path / 'missing.yml'}\n"
        ),
    ]

    validate_composition(
        overlay_composition(), composed, overlay_dir=BRIDGE_OVERLAY_PATH.parent
    )
