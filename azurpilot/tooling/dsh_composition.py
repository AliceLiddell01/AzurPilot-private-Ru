"""Эффективная композиция продуктового профиля DeepSeek Harness.

Модуль владеет семантической проверкой entry-list, который DeepSeek Harness
собирает из базового слоя, пользовательского профиля и overlay-файла клиента
AzurPilot. Разбирается дерево, а не текст YAML: сравниваются идентификаторы
записей, спецификаторы плагинов, конфигурация клиентов моста и заявки на
`serverName`. Поэтому проверка не зависит от порядка строк, комментариев и
форматирования composed-файла.

Запускатель `azur dsh launch` использует модуль как предварительную проверку:
точный pin DeepSeek Harness `0.1.7-rc.2` держит список обязательных entry в
глобальной константе `dsh-app-boot` и не позволяет расширить его из профиля,
поэтому отсутствие или конфликт обязательной записи закрывает запуск здесь.

Отказ относится к предусловию клиента и не расширяет словарь `ResultCode`:
`azurpilot/tooling/result.py` входит в общий набор исходников MCP, поэтому
отдельный код здесь сдвинул бы замороженную идентичность моста Windows MCP
ради проверки, которая к его исходникам не относится.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import yaml

from azurpilot.dsh import BRIDGE_GUARD_PATH, BRIDGE_OVERLAY_PATH
from module.mcp_shared.windows_mcp_bridge_contract import BRIDGE_ROUTES, BridgeRoute

from .errors import ToolingError
from .mcp_source_identity import BRIDGE_MCP_SERVER_NAMES
from .result import ResultCode

__all__ = (
    "CompositionEntry",
    "CompositionExpression",
    "load_composition",
    "overlay_composition",
    "resolve_plugin_asset",
    "validate_composition",
)

_JAVASCRIPT_TAG = "tag:yaml.org,2002:js"

#: Записи-контейнеры DeepSeek Harness: дочерние записи лежат списком в `config`.
_GROUP_PLUGINS = frozenset({"cordis:group", "@deepseek-ai/cordis-plugin-group"})

#: Записи вложения DeepSeek Harness: монтируют файл как ещё одно дерево записей.
_INCLUDE_PLUGINS = frozenset({"cordis:include", "@deepseek-ai/cordis-plugin-include"})

#: Признак клиента MCP в спецификаторе плагина: и имя пакета, и путь к нему.
_MCP_CLIENT_MARKER = "dsh-mcp-client"


class CompositionExpression:
    """Выражение `!!js` entry-list DeepSeek Harness.

    Значение выражения зависит от окружения процесса, поэтому сравнивать его с
    буквальным значением нельзя: равенство определяется текстом выражения.
    """

    __slots__ = ("expression",)

    def __init__(self, expression: str) -> None:
        self.expression = " ".join(str(expression).split())

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, CompositionExpression)
            and other.expression == self.expression
        )

    def __hash__(self) -> int:
        return hash(self.expression)

    def __repr__(self) -> str:
        return f"!!js {self.expression!r}"


class _CompositionLoader(yaml.SafeLoader):
    """Загрузчик entry-list: выражение `!!js` читается как текст выражения."""


_CompositionLoader.add_constructor(
    _JAVASCRIPT_TAG,
    lambda loader, node: CompositionExpression(loader.construct_scalar(node)),
)


@dataclass(frozen=True)
class CompositionEntry:
    """Одна запись entry-list DeepSeek Harness после композиции слоёв.

    Запись-контейнер (`group`) подключает дочерние записи списком в `config`, а
    запись вложения (`cordis:include`) монтирует чужой файл в той же корневой
    области. Поэтому состав композиции определяется обходом дерева, а не списком
    верхнего уровня.
    """

    entry_id: str | None
    plugin: str
    config: Mapping[str, Any]
    disabled: object = None
    group: bool = False
    children: tuple[CompositionEntry, ...] = ()

    @property
    def enabled(self) -> bool:
        """Включена ли запись статически.

        `disabled` может быть выражением `!!js`: статическая проверка не может
        подтвердить такую запись, поэтому включённой считается только запись без
        `disabled` или с явным `false`.
        """

        return self.disabled is None or self.disabled is False

    @property
    def container(self) -> bool:
        """Подключает ли запись дочерние записи списком `config`."""

        return self.group or self.plugin in _GROUP_PLUGINS

    @property
    def include(self) -> bool:
        """Монтирует ли запись файл с другими записями."""

        return self.plugin in _INCLUDE_PLUGINS


def load_composition(text: str) -> tuple[CompositionEntry, ...]:
    """Разобрать собранный entry-list DeepSeek Harness.

    Ожидается список записей. Файл, который не является списком, считается
    повреждённой композицией: молча принять его за пустой список нельзя.
    """

    return tuple(
        entry
        for entry in (_entry(item) for item in _load_list(text))
        if entry is not None
    )


def overlay_composition(path: Path | None = None) -> tuple[CompositionEntry, ...]:
    """Прочитать записи, которые overlay-файл клиента AzurPilot добавляет в профиль."""

    source = BRIDGE_OVERLAY_PATH if path is None else path
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as error:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Overlay-файл клиента AzurPilot недоступен для проверки композиции.",
        ) from error
    entries: list[CompositionEntry] = []
    for patch in _load_list(text):
        if not isinstance(patch, Mapping):
            continue
        inserts = patch.get("insert")
        if not isinstance(inserts, list):
            continue
        entries.extend(
            entry for entry in (_entry(item) for item in inserts) if entry is not None
        )
    return tuple(entries)


def _load_list(text: str) -> list[Any]:
    try:
        document = yaml.load(text, Loader=_CompositionLoader)
    except yaml.YAMLError as error:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Эффективная композиция профиля DeepSeek Harness не разбирается как YAML.",
        ) from error
    if document is None:
        return []
    if not isinstance(document, list):
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Эффективная композиция профиля DeepSeek Harness не является списком записей.",
        )
    return document


def resolve_plugin_asset(entry: CompositionEntry, anchor: Path) -> Path | None:
    """Разрешить спецификатор плагина в путь ассета рядом с overlay-файлом.

    Спецификаторы пакетов без пути возвращают `None`: отслеживаемые ассеты клиента
    AzurPilot лежат рядом с overlay-файлом и подключаются относительным путём.
    """

    if entry.plugin.startswith("file://"):
        return Path(url2pathname(urlparse(entry.plugin).path)).resolve()
    if entry.plugin.startswith((".", "/")):
        return (anchor / entry.plugin).resolve()
    return None


def validate_composition(
    expected: Sequence[CompositionEntry],
    composed: Sequence[CompositionEntry],
    *,
    overlay_dir: Path,
    guard_path: Path = BRIDGE_GUARD_PATH,
    server_names: Iterable[str] = BRIDGE_MCP_SERVER_NAMES,
) -> None:
    """Подтвердить, что эффективная композиция реализует overlay клиента AzurPilot.

    Проверяются три инварианта: сам overlay объявляет страж и ровно по одному
    клиенту на канонический маршрут моста; эффективная композиция содержит все
    его записи без изменений и включёнными; `serverName` каждого семейства
    заявлен ровно одной включённой записью — той самой, что объявил overlay.

    Состав композиции обходится рекурсивно: записи-контейнеры `group` и
    смонтированные файлы `cordis:include` резервируют `serverName` в той же
    корневой области, что и записи overlay-файла. Запрос, который нельзя
    проверить статически (выражение вместо имени сервера, вложение без
    читаемого файла), отклоняется: неизвестное состояние означает запрет.
    """

    names = tuple(server_names)
    _require_overlay_coherence(
        expected, overlay_dir=overlay_dir, guard_path=guard_path, server_names=names
    )

    for entry in expected:
        identifier = _identifier(entry)
        matches = [
            candidate
            for candidate in composed
            if candidate.entry_id is not None and candidate.entry_id == identifier
        ]
        if not matches:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Эффективная композиция профиля DeepSeek Harness не содержит "
                f"обязательной записи {identifier!r} клиента AzurPilot.",
            )
        if len(matches) > 1:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Эффективная композиция профиля DeepSeek Harness содержит "
                f"несколько записей ({len(matches)}) с идентификатором "
                f"{identifier!r}; ожидается ровно одна.",
            )
        realised = matches[0]
        if not _same_plugin(realised, entry, overlay_dir):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                f"Запись {identifier!r} подключает другой плагин "
                f"({realised.plugin!r} вместо {entry.plugin!r}).",
            )
        if not realised.enabled:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                f"Обязательная запись {identifier!r} отключена слоями профиля "
                "DeepSeek Harness.",
            )
        if not _same_configuration(realised.config, entry.config):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                f"Конфигурация обязательной записи {identifier!r} изменена слоями "
                "профиля DeepSeek Harness.",
            )

    declared = _declared_server_names(expected, names)
    claimed = _claimed_server_names(composed, names, anchor=overlay_dir)
    for server_name in names:
        owners = claimed.get(server_name, ())
        if not owners:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Эффективная композиция профиля DeepSeek Harness не содержит "
                f"включённого клиента для маршрута {server_name!r}.",
            )
        if len(owners) > 1:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                f"Маршрут {server_name!r} заявлен несколькими включёнными записями "
                f"({', '.join(sorted(owners))}); зарезервировать сервер может только "
                f"одна — {declared[server_name]!r}.",
            )
        if owners[0] != declared[server_name]:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                f"Маршрут {server_name!r} заявлен записью {owners[0]!r} вместо "
                f"объявленной overlay-файлом {declared[server_name]!r}.",
            )


def _require_overlay_coherence(
    expected: Sequence[CompositionEntry],
    *,
    overlay_dir: Path,
    guard_path: Path,
    server_names: Sequence[str],
) -> None:
    guards = [
        entry
        for entry in expected
        if resolve_plugin_asset(entry, overlay_dir) == guard_path.resolve()
    ]
    if len(guards) != 1:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Overlay-файл клиента AzurPilot должен объявлять ровно один страж "
            f"{guard_path.name}; найдено {len(guards)}.",
        )
    declared = _declared_server_names(expected, server_names)
    for server_name, identifier in declared.items():
        owner = next(entry for entry in expected if _identifier(entry) == identifier)
        route = _route(server_name)
        url = owner.config.get("url") if isinstance(owner.config, Mapping) else None
        if url != route.bridge_url:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                f"Overlay-файл клиента AzurPilot направляет {server_name!r} на "
                f"{url!r} вместо канонического маршрута {route.bridge_url!r}.",
            )
        if owner.config.get("failOnStartupError") is not True:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                f"Overlay-файл клиента AzurPilot не помечает {server_name!r} "
                "обязательным при активации (failOnStartupError).",
            )


def _route(server_name: str) -> BridgeRoute:
    """Найти канонический маршрут моста по имени сервера."""

    for route in BRIDGE_ROUTES.values():
        if route.server_name == server_name:
            return route
    raise ToolingError(  # pragma: no cover - закрытый каталог маршрутов моста
        ResultCode.TOOLING_PRECONDITION_FAILED,
        f"Канонический контракт моста Windows MCP не содержит маршрут {server_name!r}.",
    )


def _declared_server_names(
    expected: Sequence[CompositionEntry], server_names: Sequence[str]
) -> dict[str, str]:
    declared: dict[str, str] = {}
    for server_name in server_names:
        owners = [
            entry for entry in expected if _claimed_server_name(entry) == server_name
        ]
        if len(owners) != 1:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Overlay-файл клиента AzurPilot должен объявлять ровно одного "
                f"клиента маршрута {server_name!r}; найдено {len(owners)}.",
            )
        declared[server_name] = _identifier(owners[0])
    return declared


def _claimed_server_names(
    entries: Sequence[CompositionEntry],
    server_names: Sequence[str],
    *,
    anchor: Path,
) -> dict[str, list[str]]:
    """Собрать владельцев каждого `serverName` обходом всего дерева композиции.

    Обход повторяет состав композиции DeepSeek Harness: дочерние записи
    контейнеров `group` и записи смонтированных файлов `cordis:include` входят в
    ту же корневую область, что и записи верхнего уровня.
    """

    claimed: dict[str, list[str]] = {}
    _collect_claims(
        entries,
        server_names=server_names,
        claimed=claimed,
        anchor=anchor,
        visited=(),
    )
    return claimed


def _collect_claims(
    entries: Sequence[CompositionEntry],
    *,
    server_names: Sequence[str],
    claimed: dict[str, list[str]],
    anchor: Path,
    visited: tuple[Path, ...],
) -> None:
    for entry in entries:
        # Литеральное `disabled: true` снимает запись вместе с её составом, а
        # выражение `!!js` — нет: DSH не пропускает такую запись статически,
        # поэтому она остаётся заявкой и проходит проверку имени сервера.
        if entry.disabled is True:
            continue
        if entry.include:
            _collect_include_claims(
                entry,
                server_names=server_names,
                claimed=claimed,
                anchor=anchor,
                visited=visited,
            )
            continue
        if entry.container:
            _collect_claims(
                entry.children,
                server_names=server_names,
                claimed=claimed,
                anchor=anchor,
                visited=visited,
            )
            continue
        server_name = _claimed_server_name(entry)
        if server_name is None or server_name not in server_names:
            continue
        claimed.setdefault(server_name, []).append(_identifier(entry))


def _collect_include_claims(
    entry: CompositionEntry,
    *,
    server_names: Sequence[str],
    claimed: dict[str, list[str]],
    anchor: Path,
    visited: tuple[Path, ...],
) -> None:
    """Проверить записи файла, смонтированного через `cordis:include`."""

    path = entry.config.get("path") if isinstance(entry.config, Mapping) else None
    if not isinstance(path, str) or not path.strip():
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            f"Запись вложения {_identifier(entry)!r} не объявляет буквальный путь "
            "файла: состав вложения нельзя проверить статически.",
        )
    target = Path(path)
    if not target.is_absolute():
        target = anchor / target
    target = target.resolve()
    if target in visited:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            f"Вложения композиции профиля DeepSeek Harness образуют цикл на файле "
            f"{str(target)!r}.",
        )
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as error:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            f"Файл вложения композиции профиля DeepSeek Harness {str(target)!r} "
            "недоступен для проверки.",
        ) from error
    _collect_claims(
        load_composition(text),
        server_names=server_names,
        claimed=claimed,
        anchor=target.parent,
        visited=(*visited, target),
    )


def _claimed_server_name(entry: CompositionEntry) -> str | None:
    """Имя сервера, которое запись заявляет, либо отказ проверки.

    Запись клиента MCP без буквального `serverName` не доказывает ни владение
    маршрутом, ни его отсутствие, поэтому считается непроверяемой.
    """

    config = entry.config if isinstance(entry.config, Mapping) else None
    declares = config is not None and "serverName" in config
    client = _MCP_CLIENT_MARKER in entry.plugin
    if declares:
        server_name = config["serverName"]
        if isinstance(server_name, str):
            return server_name
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            f"Запись {_identifier(entry)!r} объявляет serverName не буквальной "
            f"строкой ({server_name!r}): маршрут нельзя ни подтвердить, ни "
            "опровергнуть статически.",
        )
    if client:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            f"Запись {_identifier(entry)!r} подключает клиент MCP без буквального "
            "serverName: маршрут нельзя ни подтвердить, ни опровергнуть статически.",
        )
    return None


def _same_plugin(
    realised: CompositionEntry, declared: CompositionEntry, anchor: Path
) -> bool:
    """Сравнить спецификаторы плагинов.

    Относительное имя ассета рядом с overlay-файлом при композиции превращается
    в абсолютный `file://`-адрес, поэтому спецификаторы сравниваются как пути,
    когда они указывают на файл, и как строки в остальных случаях.
    """

    left = resolve_plugin_asset(realised, anchor)
    right = resolve_plugin_asset(declared, anchor)
    if left is not None or right is not None:
        return left == right
    return realised.plugin == declared.plugin


def _identifier(entry: CompositionEntry) -> str:
    return entry.entry_id or entry.plugin


def _same_configuration(
    realised: Mapping[str, Any], declared: Mapping[str, Any]
) -> bool:
    return _normalised(realised) == _normalised(declared)


def _normalised(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _normalised(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalised(item) for item in value]
    return value


def _entry(item: Any) -> CompositionEntry | None:
    """Разобрать одну запись entry-list.

    Запись без буквального имени плагина не отбрасывается: за неразбираемым
    спецификатором может скрываться и контейнер, и клиент MCP, поэтому состав
    такой записи всё равно должен попасть под проверку.
    """

    if not isinstance(item, Mapping):
        return None
    plugin = item.get("name")
    identifier = item.get("id")
    config = item.get("config")
    group = item.get("group") is True
    if not isinstance(plugin, str) and isinstance(config, list) and not group:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Эффективная композиция профиля DeepSeek Harness содержит запись "
            f"{identifier!r} с неразбираемым именем плагина (не буквальная строка) "
            "и списком дочерних записей: её состав нельзя проверить статически.",
        )
    children: tuple[CompositionEntry, ...] = ()
    if (group or plugin in _GROUP_PLUGINS) and isinstance(config, list):
        children = tuple(
            entry for entry in (_entry(child) for child in config) if entry is not None
        )
    return CompositionEntry(
        entry_id=identifier if isinstance(identifier, str) else None,
        plugin=plugin if isinstance(plugin, str) else "",
        config=config if isinstance(config, Mapping) else {},
        disabled=item.get("disabled"),
        group=group,
        children=children,
    )
