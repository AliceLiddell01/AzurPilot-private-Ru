"""Fail-closed structural verifier for translation-only pull requests."""

from __future__ import annotations

import argparse
import ast
import copy
import io
import re
import string
import subprocess
import sys
import token
import tokenize
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PROTECTED_PATHS = {
    ".github/workflows/ci.yml",
    "dev_tools/translation_structural_gate.py",
    "tests/test_translation_structural_gate.py",
}
ENTRY_POINTS = {"alas.py", "gui.py", "mcp_server_sse.py"}
LOGGER_METHODS = {"info", "warning", "error", "critical", "exception", "hr"}
HANDLE_NOTIFY_PROSE_KEYWORDS = {"title", "content"}
PERCENT_PLACEHOLDER = re.compile(
    r"%(?:\([^)]+\))?[#0\- +'I]*(?:\d+|\*)?(?:\.(?:\d+|\*))?"
    r"(?:hh|h|ll|l|L|j|z|t)?[diouxXeEfFgGcrsa%]"
)
STRING_PREFIX = re.compile(r"(?i)^([rubf]*)(\"\"\"|'''|\"|')")


@dataclass(frozen=True)
class SourceRange:
    start: tuple[int, int]
    end: tuple[int, int]

    def contains(self, start: tuple[int, int], end: tuple[int, int]) -> bool:
        return self.start <= start and end <= self.end


@dataclass(frozen=True)
class SiteContract:
    kind: str
    literal_values: tuple[str, ...]
    placeholders: tuple[tuple[str, str | None, str], ...]
    percent_placeholders: tuple[str, ...]


@dataclass
class ParsedSource:
    tree: ast.AST
    ranges: list[SourceRange]
    contracts: list[SiteContract]


def _is_production_python(path: str) -> bool:
    pure = PurePosixPath(path)
    return path in ENTRY_POINTS or (
        len(pure.parts) > 1
        and pure.parts[0] in {"campaign", "module"}
        and pure.suffix == ".py"
    )


def _call_name(node: ast.expr) -> tuple[str, ...] | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


def _display_value_templates(node: ast.AST) -> list[ast.AST]:
    if (
        isinstance(node, ast.IfExp)
        and isinstance(node.body, ast.Constant)
        and isinstance(node.body.value, str)
        and isinstance(node.orelse, ast.Constant)
        and isinstance(node.orelse.value, str)
    ):
        return [node.body, node.orelse]
    if isinstance(node, (ast.List, ast.Tuple)):
        result: list[ast.AST] = []
        for element in node.elts:
            result.extend(_display_value_templates(element))
        return result
    return []


def _safe_templates(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node]
    if isinstance(node, ast.JoinedStr):
        return [node]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, (ast.Constant, ast.JoinedStr))
    ):
        template = node.func.value
        if isinstance(template, ast.Constant) and not isinstance(template.value, str):
            return []
        return [template]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"strip", "lstrip", "rstrip"}
        and not node.args
        and not node.keywords
    ):
        return _safe_templates(node.func.value)
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mod)
        and isinstance(node.left, ast.Constant)
        and isinstance(node.left.value, str)
    ):
        return [node.left, *_display_value_templates(node.right)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*_safe_templates(node.left), *_safe_templates(node.right)]
    return []


def _literal_nodes(template: ast.AST) -> list[ast.Constant]:
    if isinstance(template, ast.Constant):
        return [template]
    if isinstance(template, ast.JoinedStr):
        return [
            value
            for value in template.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        ]
    raise TypeError(f"Unsupported template node: {type(template).__name__}")


def _format_placeholders(value: str) -> tuple[tuple[str, str | None, str], ...]:
    try:
        parsed = string.Formatter().parse(value)
        return tuple(
            (field_name, conversion, format_spec)
            for _, field_name, format_spec, conversion in parsed
            if field_name is not None
        )
    except ValueError as exc:
        return ((f"INVALID:{exc}", None, ""),)


def _control_signature(value: str) -> tuple[str, ...]:
    return tuple(character for character in value if character in "\n\r\t")


class _ApprovedSiteCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.ranges: list[SourceRange] = []
        self.contracts: list[SiteContract] = []

    def _approve(
        self, expression: ast.AST, kind: str, *, logger_percent_arguments: bool = False
    ) -> None:
        templates = _safe_templates(expression)
        if not templates:
            return

        literals = [
            literal for template in templates for literal in _literal_nodes(template)
        ]
        for literal in literals:
            literal._translation_prose = True
            if not hasattr(literal, "lineno") or not hasattr(literal, "end_lineno"):
                continue
            self.ranges.append(
                SourceRange(
                    (literal.lineno, literal.col_offset),
                    (literal.end_lineno, literal.end_col_offset),
                )
            )

        values = tuple(literal.value for literal in literals)
        format_contract: tuple[tuple[str, str | None, str], ...] = ()
        percent_contract: tuple[str, ...] = ()
        if (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Attribute)
            and expression.func.attr == "format"
            and values
        ):
            format_contract = _format_placeholders("".join(values))
        elif (
            logger_percent_arguments
            or isinstance(expression, ast.BinOp)
            and isinstance(expression.op, ast.Mod)
        ) and values:
            percent_contract = tuple(PERCENT_PLACEHOLDER.findall("".join(values)))

        self.contracts.append(
            SiteContract(kind, values, format_contract, percent_contract)
        )

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        logger_target = None
        logger_method = None
        if name and len(name) == 2 and name[0] == "logger":
            logger_target = "logger"
            logger_method = name[1]
        elif name and len(name) == 3 and name[:2] == ("self", "logger"):
            logger_target = "self.logger"
            logger_method = name[2]

        if logger_target and logger_method and node.args:
            if logger_method in LOGGER_METHODS:
                self._approve(
                    node.args[0],
                    f"{logger_target}.{logger_method}",
                    logger_percent_arguments=len(node.args) > 1,
                )
            elif logger_method == "attr":
                self._approve(node.args[0], f"{logger_target}.attr label")
        elif name == ("handle_notify",):
            for keyword in node.keywords:
                if keyword.arg in HANDLE_NOTIFY_PROSE_KEYWORDS:
                    self._approve(
                        keyword.value,
                        f"handle_notify.{keyword.arg}",
                    )
        self.generic_visit(node)


class _ProseNormalizer(ast.NodeTransformer):
    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if getattr(node, "_translation_prose", False):
            return ast.copy_location(ast.Constant(value="<OPERATOR_PROSE>"), node)
        return node


def _parse_source(source: str, filename: str) -> ParsedSource:
    tree = ast.parse(source, filename=filename)
    collector = _ApprovedSiteCollector()
    collector.visit(tree)
    normalized = _ProseNormalizer().visit(copy.deepcopy(tree))
    ast.fix_missing_locations(normalized)
    return ParsedSource(normalized, collector.ranges, collector.contracts)


def _string_token_signature(value: str) -> str:
    match = STRING_PREFIX.match(value)
    if match is None:
        return "STRING"
    prefix, quote = match.groups()
    return f"STRING:{prefix}:{quote}"


def _token_stream(source: str, ranges: Iterable[SourceRange]) -> list[tuple[str, str]]:
    allowed = tuple(ranges)
    lines = source.splitlines(keepends=True)

    def byte_position(position: tuple[int, int]) -> tuple[int, int]:
        line, column = position
        if line < 1 or line > len(lines):
            return position
        return line, len(lines[line - 1][:column].encode("utf-8"))

    result: list[tuple[str, str]] = []
    fstring_middle = getattr(token, "FSTRING_MIDDLE", None)
    ignored = {tokenize.ENCODING, tokenize.ENDMARKER}

    for item in tokenize.tokenize(io.BytesIO(source.encode("utf-8")).readline):
        if item.type in ignored:
            continue
        value = item.string
        start = byte_position(item.start)
        end = byte_position(item.end)
        in_approved_literal = any(
            source_range.contains(start, end) for source_range in allowed
        )
        if item.type == token.STRING and in_approved_literal:
            value = _string_token_signature(value)
        elif (
            fstring_middle is not None
            and item.type == fstring_middle
            and in_approved_literal
        ):
            value = "<OPERATOR_PROSE>"
        result.append((token.tok_name[item.type], value))
    return result


def _first_ast_difference(base: object, head: object, path: str = "tree") -> str | None:
    if type(base) is not type(head):
        return f"{path}: {type(base).__name__} -> {type(head).__name__}"
    if isinstance(base, ast.AST):
        for field in base._fields:
            difference = _first_ast_difference(
                getattr(base, field), getattr(head, field), f"{path}.{field}"
            )
            if difference:
                return difference
        return None
    if isinstance(base, list):
        if len(base) != len(head):
            base_types = [type(item).__name__ for item in base]
            head_types = [type(item).__name__ for item in head]
            return f"{path}: list changed {base_types!r} -> {head_types!r}"
        for index, (base_item, head_item) in enumerate(zip(base, head, strict=True)):
            difference = _first_ast_difference(
                base_item, head_item, f"{path}[{index}]"
            )
            if difference:
                return difference
        return None
    if base != head:
        return f"{path}: {base!r} -> {head!r}"
    return None


def verify_source_pair(base_source: str, head_source: str, path: str) -> list[str]:
    blockers: list[str] = []
    try:
        base = _parse_source(base_source, f"{path}@base")
    except (SyntaxError, tokenize.TokenError) as exc:
        return [f"BLOCKER: {path}: base syntax/token error: {exc}"]
    try:
        head = _parse_source(head_source, f"{path}@head")
    except (SyntaxError, tokenize.TokenError) as exc:
        return [f"BLOCKER: {path}: head syntax/token error: {exc}"]

    if ast.dump(base.tree, include_attributes=False) != ast.dump(
        head.tree, include_attributes=False
    ):
        detail = _first_ast_difference(base.tree, head.tree) or "unknown AST delta"
        blockers.append(f"BLOCKER: {path}: structural AST delta: {detail}")

    if len(base.contracts) != len(head.contracts):
        blockers.append(
            f"BLOCKER: {path}: approved prose site count changed "
            f"{len(base.contracts)} -> {len(head.contracts)}"
        )
    else:
        for index, (base_contract, head_contract) in enumerate(
            zip(base.contracts, head.contracts, strict=True), start=1
        ):
            if base_contract.kind != head_contract.kind:
                blockers.append(
                    f"BLOCKER: {path}: prose site {index} sink changed "
                    f"{base_contract.kind!r} -> {head_contract.kind!r}"
                )
            if tuple(map(_control_signature, base_contract.literal_values)) != tuple(
                map(_control_signature, head_contract.literal_values)
            ):
                blockers.append(
                    f"BLOCKER: {path}: prose site {index} newline/tab contract changed"
                )
            if base_contract.placeholders != head_contract.placeholders:
                blockers.append(
                    f"BLOCKER: {path}: prose site {index} .format placeholders changed"
                )
            if base_contract.percent_placeholders != head_contract.percent_placeholders:
                blockers.append(
                    f"BLOCKER: {path}: prose site {index} percent placeholders changed"
                )

    try:
        base_tokens = _token_stream(base_source, base.ranges)
        head_tokens = _token_stream(head_source, head.ranges)
    except (IndentationError, SyntaxError, tokenize.TokenError) as exc:
        blockers.append(f"BLOCKER: {path}: tokenization failed: {exc}")
    else:
        if base_tokens != head_tokens:
            mismatch = next(
                (
                    index
                    for index, pair in enumerate(
                        zip(base_tokens, head_tokens, strict=False), start=1
                    )
                    if pair[0] != pair[1]
                ),
                min(len(base_tokens), len(head_tokens)) + 1,
            )
            blockers.append(
                f"BLOCKER: {path}: lexical token stream changed near token {mismatch}"
            )
    return blockers


def _git(repository: Path, *arguments: str, text: bool = True) -> str | bytes:
    if text:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    else:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=False,
        )
    return completed.stdout


def _resolve_commit(repository: Path, revision: str) -> str:
    return str(_git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")).strip()


def _read_blob(repository: Path, revision: str, path: str) -> str:
    data = _git(repository, "show", f"{revision}:{path}", text=False)
    assert isinstance(data, bytes)
    return data.decode("utf-8", errors="strict")


def _changed_files(repository: Path, base: str, head: str) -> list[tuple[str, ...]]:
    output = _git(
        repository,
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        "--find-copies",
        "--find-copies-harder",
        base,
        head,
        "--",
        text=False,
    )
    assert isinstance(output, bytes)
    records = [record.decode("utf-8", errors="strict") for record in output.split(b"\0") if record]
    changes: list[tuple[str, ...]] = []
    index = 0
    while index < len(records):
        status = records[index]
        path_count = 2 if status.startswith(("R", "C")) else 1
        paths = tuple(records[index + 1 : index + 1 + path_count])
        if len(paths) != path_count:
            raise ValueError(f"truncated diff record for status {status!r}")
        changes.append((status, *paths))
        index += path_count + 1
    return changes


def run_gate(repository: Path, base: str, head: str) -> list[str]:
    blockers: list[str] = []
    base_sha = _resolve_commit(repository, base)
    head_sha = _resolve_commit(repository, head)
    changes = _changed_files(repository, base_sha, head_sha)

    for change in changes:
        status = change[0]
        paths = change[1:]
        for path in paths:
            if path in PROTECTED_PATHS:
                blockers.append(f"BLOCKER: translation PR modifies protected path: {path}")

        if status.startswith(("R", "C")):
            if any(_is_production_python(path) for path in paths):
                operation = "renamed" if status.startswith("R") else "copied"
                blockers.append(
                    f"BLOCKER: production file {operation}: " + " -> ".join(paths)
                )
            continue

        path = paths[0]
        if not _is_production_python(path):
            continue
        if status == "A":
            blockers.append(f"BLOCKER: production file added: {path}")
        elif status == "D":
            blockers.append(f"BLOCKER: production file deleted: {path}")
        elif status == "M":
            try:
                base_source = _read_blob(repository, base_sha, path)
                head_source = _read_blob(repository, head_sha, path)
            except UnicodeDecodeError as exc:
                blockers.append(f"BLOCKER: {path}: source is not strict UTF-8: {exc}")
            else:
                blockers.extend(verify_source_pair(base_source, head_source, path))
        else:
            blockers.append(f"BLOCKER: unsupported production file status {status}: {path}")
    return blockers


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a translation-only production Python diff."
    )
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        blockers = run_gate(arguments.repository.resolve(), arguments.base, arguments.head)
    except (OSError, subprocess.CalledProcessError, UnicodeError, ValueError) as exc:
        print(f"BLOCKER: verifier could not complete: {exc}", file=sys.stderr)
        return 2

    if blockers:
        print("Translation structural gate: FAIL", file=sys.stderr)
        for blocker in blockers:
            print(blocker, file=sys.stderr)
        return 1

    print("Translation structural gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
