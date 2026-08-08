from __future__ import annotations

import ast
import io
import re
import subprocess
import tokenize
import warnings
from pathlib import Path


BASE_SHA = "f0b9a3c475eac6dff6c5073cdd2f5771f7c72f07"
PRODUCTION_FILES = (
    "module/commission/commission.py",
    "module/commission/project.py",
    "module/dorm/buy_furniture.py",
    "module/dorm/dorm.py",
    "module/event/campaign_abcd.py",
    "module/freebies/battle_pass.py",
    "module/freebies/data_key.py",
    "module/freebies/freebies.py",
    "module/freebies/mail_white.py",
    "module/freebies/supply_pack.py",
    "module/island/island.py",
    "module/island/island_air_drop.py",
    "module/island/island_business.py",
    "module/island/island_cargo_preparation.py",
    "module/island/island_daily_gather.py",
    "module/island/island_daily_interact.py",
    "module/island/island_daily_order.py",
    "module/island/island_farm.py",
    "module/island/island_fishery.py",
    "module/island/island_grill.py",
    "module/island/island_juu_coffee.py",
    "module/island/island_juu_eatery.py",
    "module/island/island_manufacture.py",
    "module/island/island_mine_forest.py",
    "module/island/island_pearl_sell.py",
    "module/island/island_rancher.py",
    "module/island/island_restaurant.py",
    "module/island/island_season.py",
    "module/island/island_select_character.py",
    "module/island/island_shop_base.py",
    "module/island/island_teahouse.py",
    "module/island/ui.py",
    "module/meowfficer/buy.py",
    "module/research/preset_generator.py",
    "module/research/project.py",
    "module/research/research.py",
    "module/research/rqueue.py",
    "module/research/selector.py",
    "module/research/ui.py",
    "module/reward/reward.py",
    "module/shop/base.py",
    "module/shop/clerk.py",
    "module/shop/shop_core.py",
    "module/shop/shop_general.py",
    "module/shop/shop_guild.py",
    "module/shop/shop_medal.py",
    "module/shop/shop_merit.py",
    "module/shop/shop_reward.py",
    "module/shop/shop_voucher.py",
    "module/shop/ui.py",
    "module/shop_event/clerk.py",
    "module/shop_event/item.py",
    "module/shop_event/shop_event.py",
    "module/shop_event/ui.py",
    "module/storage/box_disassemble.py",
    "module/storage/storage.py",
    "module/storage/ui.py",
    "module/tactical/tactical_class.py",
)

_STRING_TOKEN_HEAD = re.compile(r"(?i)^([rubf]*)(\'\'\'|\"\"\"|\'|\")")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _source_at(ref: str, path: str) -> str:
    return _git("show", f"{ref}:{path}")


def _dump_optional(node: ast.AST | None) -> str | None:
    return None if node is None else ast.dump(node, include_attributes=False)


def _docstrings(tree: ast.AST) -> list[tuple[str, str, str | None]]:
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return [
        (
            type(node).__name__,
            getattr(node, "name", "<module>"),
            ast.get_docstring(node, clean=False),
        )
        for node in ast.walk(tree)
        if isinstance(node, holders)
    ]


def _comments(source: str) -> list[str]:
    return [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]


def _imports(tree: ast.AST) -> list[str]:
    return [
        ast.dump(node, include_attributes=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]


def _definitions(tree: ast.AST) -> list[tuple[str, str, tuple[str, ...], str | None, str | None, tuple[str, ...]]]:
    result = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = [*node.args.posonlyargs, *node.args.args]
            result.append(
                (
                    type(node).__name__,
                    node.name,
                    tuple(arg.arg for arg in positional),
                    node.args.vararg.arg if node.args.vararg else None,
                    node.args.kwarg.arg if node.args.kwarg else None,
                    tuple(arg.arg for arg in node.args.kwonlyargs),
                )
            )
        elif isinstance(node, ast.ClassDef):
            result.append(("ClassDef", node.name, (), None, None, ()))
    return result


def _call_shapes(tree: ast.AST) -> list[tuple[str, int, tuple[str | None, ...]]]:
    return [
        (
            ast.dump(node.func, include_attributes=False),
            len(node.args),
            tuple(keyword.arg for keyword in node.keywords),
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]


def _numeric_literals(tree: ast.AST) -> list[object]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float, complex))
        and not isinstance(node.value, bool)
    ]


def _percent_placeholder_signatures(tree: ast.AST) -> list[tuple[str, ...]]:
    pattern = re.compile(r"%(?:\([^)]+\))?[#0\- +]*(?:\d+|\*)?(?:\.\d+|\.\*)?[hlL]?[diouxXeEfFgGcrsa%]")
    result = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
        ):
            result.append(tuple(pattern.findall(node.left.value)))
    return result


def _assert_ast_equal_except_string_values(left: ast.AST, right: ast.AST, path: str = "root") -> None:
    if type(left) is not type(right):
        raise AssertionError(f"AST node type changed at {path}: {type(left).__name__} != {type(right).__name__}")

    if isinstance(left, ast.Constant):
        if isinstance(left.value, str) and isinstance(right.value, str):
            if left.kind != right.kind:
                raise AssertionError(f"String literal kind changed at {path}: {left.kind!r} != {right.kind!r}")
            return
        if left.value != right.value or left.kind != right.kind:
            raise AssertionError(f"Constant changed at {path}: {left.value!r} != {right.value!r}")
        return

    if isinstance(left, ast.JoinedStr):
        if len(left.values) != len(right.values):
            raise AssertionError(f"f-string segment count changed at {path}")
        for index, (l_value, r_value) in enumerate(zip(left.values, right.values, strict=True)):
            child_path = f"{path}.values[{index}]"
            if type(l_value) is not type(r_value):
                raise AssertionError(f"f-string segment type changed at {child_path}")
            if isinstance(l_value, ast.Constant) and isinstance(l_value.value, str):
                _assert_ast_equal_except_string_values(l_value, r_value, child_path)
                continue
            if isinstance(l_value, ast.FormattedValue):
                if ast.dump(l_value.value, include_attributes=False) != ast.dump(r_value.value, include_attributes=False):
                    raise AssertionError(f"f-string expression changed at {child_path}")
                if l_value.conversion != r_value.conversion:
                    raise AssertionError(f"f-string conversion changed at {child_path}")
                if _dump_optional(l_value.format_spec) != _dump_optional(r_value.format_spec):
                    raise AssertionError(f"f-string format_spec changed at {child_path}")
                continue
            _assert_ast_equal_except_string_values(l_value, r_value, child_path)
        return

    for field in left._fields:
        l_value = getattr(left, field)
        r_value = getattr(right, field)
        child_path = f"{path}.{field}"
        if isinstance(l_value, ast.AST):
            if not isinstance(r_value, ast.AST):
                raise AssertionError(f"AST/scalar mismatch at {child_path}")
            _assert_ast_equal_except_string_values(l_value, r_value, child_path)
        elif isinstance(l_value, list):
            if not isinstance(r_value, list) or len(l_value) != len(r_value):
                raise AssertionError(f"AST list shape changed at {child_path}")
            for index, (l_item, r_item) in enumerate(zip(l_value, r_value, strict=True)):
                item_path = f"{child_path}[{index}]"
                if isinstance(l_item, ast.AST):
                    if not isinstance(r_item, ast.AST):
                        raise AssertionError(f"AST/scalar mismatch at {item_path}")
                    _assert_ast_equal_except_string_values(l_item, r_item, item_path)
                elif l_item != r_item:
                    raise AssertionError(f"Scalar list value changed at {item_path}: {l_item!r} != {r_item!r}")
        elif l_value != r_value:
            raise AssertionError(f"AST scalar changed at {child_path}: {l_value!r} != {r_value!r}")


def _string_token_shape(value: str) -> tuple[str, str]:
    match = _STRING_TOKEN_HEAD.match(value)
    if not match:
        raise AssertionError(f"Cannot parse string token prefix/quote: {value[:20]!r}")
    return match.group(1).lower(), match.group(2)


def _assert_token_structure(left: str, right: str, path: str) -> None:
    left_tokens = list(tokenize.generate_tokens(io.StringIO(left).readline))
    right_tokens = list(tokenize.generate_tokens(io.StringIO(right).readline))
    if len(left_tokens) != len(right_tokens):
        raise AssertionError(f"Token count changed in {path}: {len(left_tokens)} != {len(right_tokens)}")

    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", None)
    for index, (l_token, r_token) in enumerate(zip(left_tokens, right_tokens, strict=True)):
        if l_token.type != r_token.type:
            raise AssertionError(
                f"Token type changed in {path} at #{index}: {tokenize.tok_name[l_token.type]} != {tokenize.tok_name[r_token.type]}"
            )
        if l_token.type == tokenize.STRING:
            if _string_token_shape(l_token.string) != _string_token_shape(r_token.string):
                raise AssertionError(f"String prefix/quote changed in {path} at token #{index}")
            continue
        if fstring_middle is not None and l_token.type == fstring_middle:
            continue
        if l_token.string != r_token.string:
            raise AssertionError(
                f"Non-string token changed in {path} at #{index}: {l_token.string!r} != {r_token.string!r}"
            )


def test_phase2b_allowlisted_structural_parity() -> None:
    _git("fetch", "--no-tags", "--filter=blob:none", "--depth=1", "origin", BASE_SHA)

    changed_production = tuple(
        line
        for line in _git("diff", "--name-only", BASE_SHA, "HEAD", "--", "module").splitlines()
        if line
    )
    assert set(changed_production) == set(PRODUCTION_FILES), (
        f"Production changed-file set mismatch: expected {len(PRODUCTION_FILES)}, got {len(changed_production)}; "
        f"missing={sorted(set(PRODUCTION_FILES) - set(changed_production))}; "
        f"extra={sorted(set(changed_production) - set(PRODUCTION_FILES))}"
    )
    assert len(changed_production) == 58

    numstat = _git("diff", "--numstat", BASE_SHA, "HEAD", "--", "module").splitlines()
    assert len(numstat) == 58
    for row in numstat:
        added, deleted, path = row.split("\t", 2)
        assert added != "-" and deleted != "-", f"Binary production diff is forbidden: {path}"
        assert int(added) == int(deleted), f"Non-symmetric production diff in {path}: +{added}/-{deleted}"

    verified = 0
    for path in PRODUCTION_FILES:
        base_source = _source_at(BASE_SHA, path)
        head_source = Path(path).read_text(encoding="utf-8")

        assert "\ufffd" not in base_source, f"U+FFFD in base source: {path}"
        assert "\ufffd" not in head_source, f"U+FFFD in head source: {path}"
        assert len(base_source) - len(base_source.rstrip("\n")) == len(head_source) - len(head_source.rstrip("\n")), (
            f"EOF newline parity changed: {path}"
        )

        base_tree = ast.parse(base_source, filename=f"{BASE_SHA}:{path}")
        head_tree = ast.parse(head_source, filename=f"HEAD:{path}")

        assert _docstrings(base_tree) == _docstrings(head_tree), f"Docstring changed: {path}"
        assert _comments(base_source) == _comments(head_source), f"Comment changed: {path}"
        assert _imports(base_tree) == _imports(head_tree), f"Import structure changed: {path}"
        assert _definitions(base_tree) == _definitions(head_tree), f"Symbol/signature structure changed: {path}"
        assert _call_shapes(base_tree) == _call_shapes(head_tree), f"Call target/shape changed: {path}"
        assert _numeric_literals(base_tree) == _numeric_literals(head_tree), f"Numeric literal changed: {path}"
        assert _percent_placeholder_signatures(base_tree) == _percent_placeholder_signatures(head_tree), (
            f"Percent-format placeholder signature changed: {path}"
        )

        _assert_ast_equal_except_string_values(base_tree, head_tree, path)
        _assert_token_structure(base_source, head_source, path)
        verified += 1

    assert verified == 58
    warnings.warn(
        "PHASE2B_STRUCTURAL_PROOF_PASS: 58/58 production files; AST mismatches are limited to string literal values; "
        "f-string expressions/conversions/format-specs, imports, symbols/signatures, call shapes, numeric literals, "
        "percent placeholders, comments/docstrings, token structure and EOF parity preserved",
        stacklevel=1,
    )
