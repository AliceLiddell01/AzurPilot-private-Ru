import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _assignment_value(path: str, name: str):
    tree = ast.parse(_source(path), filename=path)
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and len(value.args) == 1
        ):
            value = value.args[0]
        return ast.literal_eval(value)
    raise AssertionError(f"Assignment {name!r} not found in {path}")


def _load_function(path: str, name: str, namespace: dict):
    tree = ast.parse(_source(path), filename=path)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    function = ast.FunctionDef(
        name=function.name,
        args=function.args,
        body=function.body,
        decorator_list=[],
        returns=function.returns,
        type_comment=function.type_comment,
        type_params=getattr(function, "type_params", []),
    )
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, f"<{path}:{name}>", "exec"), namespace)
    return namespace[name]


class TestWebUiLivePreviewPolicy(unittest.TestCase):
    def test_live_preview_websocket_routes_are_not_registered(self):
        path = "module/webui/fastapi.py"
        disabled = set(_assignment_value(path, "DISABLED_API_ROUTE_PATHS"))
        self.assertEqual(
            {"/ws/live_screenshot", "/ws/live_control"},
            disabled,
        )
        self.assertIn(
            'if getattr(route, "path", None) not in DISABLED_API_ROUTE_PATHS',
            _source(path),
        )

    def test_live_preview_buttons_are_replaced_with_empty_output(self):
        path = "module/webui/app_dependencies.py"
        labels = frozenset(_assignment_value(path, "LIVE_PREVIEW_BUTTON_LABELS"))
        self.assertEqual(
            frozenset({"Предпросмотр снимка", "截图预览"}),
            labels,
        )

        empty_output = object()
        normal_output = object()
        calls = []

        def put_none():
            return empty_output

        def original_put_button(*args, **kwargs):
            calls.append((args, kwargs))
            return normal_output

        put_button = _load_function(
            path,
            "put_button",
            {
                "Any": object,
                "LIVE_PREVIEW_BUTTON_LABELS": labels,
                "put_none": put_none,
                "_put_button": original_put_button,
            },
        )

        self.assertIs(empty_output, put_button(label="Предпросмотр снимка"))
        self.assertIs(empty_output, put_button("截图预览"))
        self.assertIs(normal_output, put_button(label="Открыть", color="on"))
        self.assertEqual([((), {"label": "Открыть", "color": "on"})], calls)

    def test_event_simulator_has_no_live_preview_entry_point(self):
        source = _source("module/webui/app_event_tools.py")
        self.assertNotIn("alasToggleLivePreview", source)
        self.assertNotIn("Предпросмотр снимка", source)

    def test_ws_scrcpy_preview_server_binary_is_removed(self):
        self.assertFalse(
            (ROOT / "bin/scrcpy/ws-scrcpy-server-v1.19-ws7.jar").exists()
        )


if __name__ == "__main__":
    unittest.main()
