"""Регрессии lifecycle-совместимости WebUI со Starlette."""

from starlette.testclient import TestClient

from module.webui.fastapi import asgi_app


def test_legacy_lifecycle_handlers_run_through_starlette_lifespan():
    events = []

    async def startup():
        events.append("startup")

    def shutdown():
        events.append("shutdown")

    application = asgi_app(
        {"index": lambda: None},
        on_startup=[startup],
        on_shutdown=[shutdown],
    )

    with TestClient(application) as client:
        assert client.get("/robots.txt").status_code == 200
        assert events == ["startup"]

    assert events == ["startup", "shutdown"]
