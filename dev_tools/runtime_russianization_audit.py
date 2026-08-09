"""Permanent semantic integrity guard for the supported RU/Global runtime.

The structural translation gate proves that an explicit translation PR changes
only approved prose.  This guard solves the complementary problem: every PR,
including feature and bugfix work, must leave deterministic operator-facing
Python sinks Russian and must preserve the single Global/EN runtime identity.

The guard deliberately does not scan comments, docstrings, arbitrary string
literals, OCR results, exception messages, or external payloads as prose.
Those surfaces either are not operator sinks or have machine/raw contracts.
"""

from __future__ import annotations

import argparse
import ast
import re
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from dev_tools.translation_structural_gate import (
        _is_production_python,
        _parse_source,
    )
else:
    from translation_structural_gate import (  # type: ignore[import-not-found]
        _is_production_python,
        _parse_source,
    )

CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LATIN_RE = re.compile(r"[A-Za-z]")
URL_RE = re.compile(r"^(?:https?|wss?)://\S+$", re.IGNORECASE)
PACKAGE_RE = re.compile(r"^(?:[a-z][a-z0-9_]*\.){2,}[A-Za-z0-9_]+$")
MACHINE_IDENTIFIER_RE = re.compile(
    r"^_?[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*(?:\s*->\s*[A-Z][A-Z0-9_]*)?$"
)
EXCEPTION_TEMPLATE_RE = re.compile(
    r"^(?:\[Alas\]\s+)?(?:<>\s+)?(?:GameStuckError|GamePageUnknownError|"
    r"ScriptError|EmulatorNotRunningError|RequestHumanTakeover)(?::\s*/)?$"
)

TECHNICAL_TOKENS = frozenset(
    {
        "ADB",
        "API",
        "CDN",
        "DEBUG",
        "Electron",
        "ERROR",
        "HP",
        "HTTP",
        "INFO",
        "JSON",
        "META",
        "MuMu Pro",
        "OCR",
        "PID",
        "predict",
        "SSL",
        "UI",
        "WARNING",
        "WebUI",
        "CRITICAL",
        "True, False, None",
        "normal",
        "hard",
        "s",
        "s/round",
        "x",
    }
)

# Exact semantic exceptions that are neither ordinary prose nor safely
# recognizable from spelling alone.  These are reviewed machine/debug
# templates, not a file-wide or count-based baseline.
MACHINE_TEMPLATES = frozenset(
    {
        "[AzurPilot] <> :",
        "[bold]<<<  >>>[/bold]",
        "[%s] %s",
        "%s: %s",
        "AdbClient(, )",
        "Device(atx_agent_url=)",
        "E:/path\\\\to/alas/alas.exe, /root/alas/, ./relative/path/log.txt",
        "bored_visited_G3: , bored_visited_H2:",
        "customer.app_keptlive",
        "ensure_no_stage_entrance",
        "hr0",
        "hr1",
        "hr2",
        "hr3",
        "is_in_daily_reward ->",
        "pos: () =",
        "sdk_ver:",
        "u2.Device",
        "_storage_in_material -> EQUIPMENT_ENTER",
    }
)

GAME_METADATA_TOKENS = frozenset({"SUBMARINE", "EX"})
EXPECTED_UI_LOCALE = "ru-RU"
EXPECTED_BUILD_TIME_LOCALES = ("en-US",)
EXPECTED_SERVER = "en"
EXPECTED_PACKAGE = "com.YoStarEN.AzurLane"
EXPECTED_ASSET_ROOTS = ("en",)
EXPECTED_OCR_ALIAS = "azur_lane"
OCR_REGISTRIES = (
    "ONNX_MODEL_PARAMS",
    "CUSTOM_CTC_MODEL_PARAMS",
    "DEFAULT_ONNX_MODEL_VERSION",
)


@dataclass(frozen=True)
class RuntimeIdentity:
    ui_locale: str
    build_time_locales: tuple[str, ...]
    event_name_source: str
    event_name_fallback_order: tuple[str, ...]
    server: str
    valid_servers: tuple[str, ...]
    valid_packages: tuple[tuple[str, str], ...]
    channel_packages: tuple[tuple[str, str], ...]
    asset_roots: tuple[str, ...]
    ocr_aliases: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass
class AuditResult:
    consumer_sites: int
    categories: Counter[str]
    deferred_exception_text: int
    blockers: list[str]


def _path_like(text: str) -> bool:
    if " " in text or "\n" in text:
        return False
    return (
        text.startswith(("./", "../", "/"))
        or re.match(r"^[A-Za-z]:[/\\]", text) is not None
        or "\\" in text
    )


def classify_english_only(text: str) -> str | None:
    """Return a reviewed non-prose category, or ``None`` for actionable text."""

    stripped = text.strip()
    if stripped in GAME_METADATA_TOKENS:
        return "PRESERVE_GAME_METADATA"
    if stripped in TECHNICAL_TOKENS:
        return "PRESERVE_TECHNICAL"
    if stripped in MACHINE_TEMPLATES or EXCEPTION_TEMPLATE_RE.fullmatch(stripped):
        return "PRESERVE_MACHINE"
    if URL_RE.fullmatch(stripped) or PACKAGE_RE.fullmatch(stripped):
        return "PRESERVE_TECHNICAL"
    if _path_like(stripped):
        return "PRESERVE_TECHNICAL"
    if MACHINE_IDENTIFIER_RE.fullmatch(stripped) and (
        "_" in stripped or "." in stripped or "->" in stripped
    ):
        return "PRESERVE_MACHINE"
    return None


def _exception_literal_count(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        for literal in ast.walk(node.exc):
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                count += 1
    return count


def audit_source(source: str, path: str) -> AuditResult:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        parsed = _parse_source(source, path)

    categories: Counter[str] = Counter()
    blockers: list[str] = []
    for index, contract in enumerate(parsed.contracts, start=1):
        text = "".join(contract.literal_values).strip()
        if not text:
            categories["ALREADY_COMPLIANT"] += 1
            continue
        if CJK_RE.search(text):
            categories["TRANSLATE"] += 1
            blockers.append(
                f"{path}: consumer site {index} ({contract.kind}) contains CJK operator text"
            )
            continue
        if CYRILLIC_RE.search(text):
            categories["ALREADY_COMPLIANT"] += 1
            continue
        if LATIN_RE.search(text):
            category = classify_english_only(text)
            if category is None:
                categories["TRANSLATE"] += 1
                blockers.append(
                    f"{path}: consumer site {index} ({contract.kind}) contains "
                    f"unclassified English operator text: {text!r}"
                )
            else:
                categories[category] += 1
            continue
        categories["ALREADY_COMPLIANT"] += 1

    return AuditResult(
        consumer_sites=len(parsed.contracts),
        categories=categories,
        deferred_exception_text=_exception_literal_count(parsed.tree),
        blockers=blockers,
    )


def _literal_value(node: ast.AST, known: dict[str, object]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in known:
        return known[node.id]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values = [_literal_value(item, known) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(values)
        if isinstance(node, ast.Set):
            return set(values)
        return values
    if isinstance(node, ast.Dict):
        return {
            _literal_value(key, known): _literal_value(value, known)
            for key, value in zip(node.keys, node.values, strict=True)
        }
    raise ValueError(f"unsupported literal node: {type(node).__name__}")


def _top_level_values(path: Path) -> tuple[dict[str, object], dict[str, ast.AST]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    values: dict[str, object] = {}
    nodes: dict[str, ast.AST] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name):
            continue
        nodes[target.id] = statement.value
        try:
            values[target.id] = _literal_value(statement.value, values)
        except ValueError:
            continue
    return values, nodes


def read_runtime_identity(repository: Path) -> RuntimeIdentity:
    locale_values, _ = _top_level_values(repository / "module/config/locale.py")
    server_values, _ = _top_level_values(repository / "module/config/server.py")
    _, ocr_nodes = _top_level_values(repository / "module/ocr/al_ocr.py")

    ocr_aliases: list[tuple[str, tuple[str, ...]]] = []
    for registry in OCR_REGISTRIES:
        node = ocr_nodes.get(registry)
        if not isinstance(node, ast.Dict):
            raise TypeError(f"OCR registry {registry} is not a literal mapping")
        keys = tuple(
            sorted(
                str(ast.literal_eval(key))
                for key in node.keys
                if key is not None
            )
        )
        ocr_aliases.append((registry, keys))

    supported_roots = {"en", "cn", "jp", "tw"}
    assets = repository / "assets"
    asset_roots = tuple(
        sorted(
            path.name
            for path in assets.iterdir()
            if path.is_dir() and path.name in supported_roots
        )
    )
    valid_packages = dict(server_values["VALID_PACKAGE"])
    channel_packages = dict(server_values["VALID_CHANNEL_PACKAGE"])
    return RuntimeIdentity(
        ui_locale=str(locale_values["UI_LOCALE"]),
        build_time_locales=tuple(locale_values["BUILD_TIME_LOCALES"]),
        event_name_source=str(locale_values["EVENT_NAME_SOURCE"]),
        event_name_fallback_order=tuple(locale_values["EVENT_NAME_FALLBACK_ORDER"]),
        server=str(server_values["server"]),
        valid_servers=tuple(server_values["VALID_SERVER"]),
        valid_packages=tuple(sorted((str(key), str(value)) for key, value in valid_packages.items())),
        channel_packages=tuple(sorted((str(key), str(value)) for key, value in channel_packages.items())),
        asset_roots=asset_roots,
        ocr_aliases=tuple(ocr_aliases),
    )


def validate_runtime_identity(identity: RuntimeIdentity) -> list[str]:
    blockers: list[str] = []
    if identity.ui_locale != EXPECTED_UI_LOCALE:
        blockers.append(f"runtime locale must be {EXPECTED_UI_LOCALE}")
    if identity.build_time_locales != EXPECTED_BUILD_TIME_LOCALES:
        blockers.append("build-time locale sources changed")
    if identity.event_name_source != EXPECTED_SERVER or identity.event_name_fallback_order:
        blockers.append("event metadata must use EN without foreign fallback")
    if identity.server != EXPECTED_SERVER or identity.valid_servers != (EXPECTED_SERVER,):
        blockers.append("runtime server must be EN only")
    expected_packages = ((EXPECTED_PACKAGE, EXPECTED_SERVER),)
    if identity.valid_packages != expected_packages or identity.channel_packages:
        blockers.append("runtime package must be the single Global package")
    if identity.asset_roots != EXPECTED_ASSET_ROOTS:
        blockers.append("runtime assets must use assets/en without foreign roots")
    for registry, aliases in identity.ocr_aliases:
        if aliases != (EXPECTED_OCR_ALIAS,):
            blockers.append(f"OCR registry {registry} exposes a foreign alias")
    return blockers


def run_audit(repository: Path) -> AuditResult:
    repository = repository.resolve()
    total = AuditResult(0, Counter(), 0, [])
    for path in sorted(repository.rglob("*.py")):
        relative = path.relative_to(repository).as_posix()
        if not _is_production_python(relative):
            continue
        result = audit_source(path.read_text(encoding="utf-8"), relative)
        total.consumer_sites += result.consumer_sites
        total.categories.update(result.categories)
        total.deferred_exception_text += result.deferred_exception_text
        total.blockers.extend(result.blockers)

    try:
        identity = read_runtime_identity(repository)
    except (KeyError, OSError, SyntaxError, TypeError, ValueError) as exc:
        total.blockers.append(f"runtime identity could not be verified: {exc}")
    else:
        total.blockers.extend(validate_runtime_identity(identity))
    return total


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify permanent RU/Global runtime localization integrity."
    )
    parser.add_argument("--repository", type=Path, default=Path("."))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = run_audit(args.repository)
    if result.blockers:
        print("Runtime russianization integrity: FAIL")
        for blocker in result.blockers:
            print(f"BLOCKER: {blocker}")
        return 1

    categories = ", ".join(
        f"{name}={count}" for name, count in sorted(result.categories.items())
    )
    print("Runtime russianization integrity: PASS")
    print(f"consumer_sites={result.consumer_sites}")
    print(f"actionable_TRANSLATE={result.categories['TRANSLATE']}")
    print(f"deferred_exception_text={result.deferred_exception_text}")
    print(f"categories: {categories}")
    print("runtime_identity=RU_GLOBAL_EN_ONLY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
