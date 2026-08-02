from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PREFIX = "/tests/fixtures/stage7_webui_traceback/fixture-"
TRACEBACK_STYLESHEET = (
    '<link rel="stylesheet" href="/assets/gui/css/traceback-alas.css">'
)


class FixtureHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self) -> None:
        request_path = unquote(urlparse(self.path).path)
        if request_path.startswith(FIXTURE_PREFIX) and request_path.endswith(".html"):
            relative_path = request_path.lstrip("/")
            fixture = (ROOT / relative_path).resolve()
            if ROOT not in fixture.parents or not fixture.is_file():
                self.send_error(404, "Fixture not found")
                return

            content = fixture.read_text(encoding="utf-8")
            if TRACEBACK_STYLESHEET not in content:
                content = content.replace(
                    "</head>",
                    f"{TRACEBACK_STYLESHEET}</head>",
                    1,
                )
            payload = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return

        super().do_GET()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Локальный сервер ручной проверки Stage 7 traceback fixtures"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), FixtureHandler)
    print(f"Stage 7 fixture server: http://{args.host}:{args.port}")
    print(
        "Dark: "
        f"http://{args.host}:{args.port}"
        "/tests/fixtures/stage7_webui_traceback/fixture-dark.html"
    )
    print(
        "Light: "
        f"http://{args.host}:{args.port}"
        "/tests/fixtures/stage7_webui_traceback/fixture-light.html"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
