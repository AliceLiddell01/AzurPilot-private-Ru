from tests.support.paths import REPOSITORY_ROOT

import json
from pathlib import Path

import pytest

from dev_tools import observability_mcp as target

ROOT = REPOSITORY_ROOT


class _FakeGrafanaApi:
    def __init__(
        self,
        *,
        accounts,
        tokens,
        created_token=(2, "fixture-value"),
        identity_results=None,
    ):
        self.accounts = accounts
        self.tokens = tokens
        self.created_token = created_token
        self.identity_results = list(identity_results or [])
        self.calls = []

    def search_service_account(self):
        self.calls.append("search")
        return list(self.accounts)

    def create_service_account(self):
        self.calls.append("create-account")
        account = {
            "id": 7,
            "name": target.CANONICAL_SERVICE_ACCOUNT,
            "role": target.CANONICAL_SERVICE_ACCOUNT_ROLE,
            "isDisabled": False,
        }
        self.accounts = [account]
        return account

    def update_service_account(self, account_id):
        self.calls.append(("update-account", account_id))
        account = {
            "id": account_id,
            "name": target.CANONICAL_SERVICE_ACCOUNT,
            "role": target.CANONICAL_SERVICE_ACCOUNT_ROLE,
            "isDisabled": False,
        }
        self.accounts = [account]
        return account

    def list_tokens(self, account_id):
        self.calls.append(("list-tokens", account_id))
        return list(self.tokens)

    def create_token(self, account_id):
        self.calls.append(("create-token", account_id))
        return self.created_token

    def verify_token_identity(self, token, account_id):
        self.calls.append(("verify-token-identity", token, account_id))
        if self.identity_results:
            result = self.identity_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return target.GrafanaIdentity(
            account_id=account_id,
        )

    def delete_token(self, account_id, token_id):
        self.calls.append(("delete-token", account_id, token_id))


class _EmptyResponse:
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return b""


class _JsonResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return target.json.dumps(self.payload).encode("utf-8")


def _account(*, role="Viewer", disabled=False, account_id=1):
    return {
        "id": account_id,
        "name": target.CANONICAL_SERVICE_ACCOUNT,
        "role": role,
        "isDisabled": disabled,
    }


def _env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER=admin\n"
        "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD=fixture-password\n",
        encoding="utf-8",
    )
    return env_file


def _compose_contract_payload():
    return {
        "services": {
            target.CANONICAL_GRAFANA_SERVICE: {
                "image": f"{target.GRAFANA_IMAGE_PREFIX}fixture",
                "environment": {
                    "GF_SECURITY_ADMIN_PASSWORD__FILE": target.GRAFANA_ADMIN_SECRET_PATH,
                    "GF_AUTH_ID_RESPONSE_HEADER_ENABLED": "true",
                    "GF_AUTH_ID_RESPONSE_HEADER_PREFIX": "X-Grafana",
                    "GF_AUTH_ID_RESPONSE_HEADER_NAMESPACES": "service-account",
                },
                "secrets": [
                    {
                        "source": target.GRAFANA_ADMIN_SECRET_NAME,
                        "target": target.GRAFANA_ADMIN_SECRET_PATH,
                    }
                ],
                "volumes": [
                    {
                        "type": "volume",
                        "source": "grafana-data",
                        "target": "/var/lib/grafana",
                    }
                ],
            }
        },
        "volumes": {
            "grafana-data": {
                "name": target.CANONICAL_GRAFANA_VOLUME,
                "external": True,
            }
        },
        "secrets": {
            target.GRAFANA_ADMIN_SECRET_NAME: {
                "environment": "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD"
            }
        },
    }


def test_identity_reuses_valid_gateway_secret_without_creating_token(
    monkeypatch, tmp_path
):
    api = _FakeGrafanaApi(accounts=[_account()], tokens=[])
    probes = iter([target.GatewayProbe(True, False, "MCP_GATEWAY_READY")])
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(
        target, "ensure_dynamic_tools_disabled", lambda apply: "disabled"
    )
    monkeypatch.setattr(target, "_gateway_probe", lambda: next(probes))
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "fixture-value")

    result = target.ensure_identity(
        repository_root=ROOT,
        env_file=_env_file(tmp_path),
        import_profile=False,
    )

    assert result["token"] == "reused"
    assert result["identity"] == "direct_grafana_api"
    assert "create-token" not in {
        call[0] for call in api.calls if isinstance(call, tuple)
    }
    assert "delete-token" not in {call[0] for call in api.calls if isinstance(call, tuple)}


@pytest.mark.parametrize(
    ("payload", "headers", "error_code"),
    [
        ({"folders:read": ["folders:id:1"]}, {}, "MCP_GRAFANA_TOKEN_IDENTITY_UNAVAILABLE"),
        (
            {"folders:read": ["folders:id:1"]},
            {"X-Grafana-Identity-Id": "service-account:8"},
            "MCP_GRAFANA_TOKEN_IDENTITY_FOREIGN",
        ),
        (
            {"dashboards:write": ["dashboards:id:1"]},
            {"X-Grafana-Identity-Id": "service-account:7"},
            "MCP_GRAFANA_TOKEN_EFFECTIVE_ROLE_INVALID",
        ),
        (
            {"folders:read": "folders:id:1"},
            {"X-Grafana-Identity-Id": "service-account:7"},
            "MCP_GRAFANA_TOKEN_IDENTITY_INVALID",
        ),
    ],
)
def test_grafana_api_identity_proof_rejects_ambiguous_or_unsafe_response(
    payload, headers, error_code
):
    api = target._GrafanaApi(
        target.DEFAULT_GRAFANA_URL,
        admin_user="admin",
        admin_password="fixture-password",
        opener=lambda *_args, **_kwargs: _JsonResponse(payload, headers),
    )

    with pytest.raises(target.ObservabilityMcpError, match=error_code):
        api.verify_token_identity("fixture-value", 7)


def test_grafana_api_identity_proof_accepts_canonical_header_and_permissions():
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return _JsonResponse(
            {"folders:read": ["folders:id:1"]},
            {"X-Grafana-Identity-Id": "service-account:7"},
        )

    api = target._GrafanaApi(
        target.DEFAULT_GRAFANA_URL,
        admin_user="admin",
        admin_password="fixture-password",
        opener=opener,
    )

    identity = api.verify_token_identity("fixture-value", 7)

    assert identity == target.GrafanaIdentity(7)
    assert requests[0].get_header("Authorization") == "Bearer fixture-value"


def test_grafana_api_identity_proof_fails_closed_when_endpoint_missing():
    calls = []

    def opener(request, **_kwargs):
        calls.append(request.full_url)
        if request.full_url.endswith("/api/access-control/user/permissions"):
            raise target.HTTPError(request.full_url, 404, "Not found", hdrs=None, fp=None)
        return _JsonResponse(
            [{"uid": "loki"}],
            {"X-Grafana-Identity-Id": "service-account:7"},
        )

    api = target._GrafanaApi(
        target.DEFAULT_GRAFANA_URL,
        admin_user="admin",
        admin_password="fixture-password",
        opener=opener,
    )

    with pytest.raises(
        target.ObservabilityMcpError,
        match="MCP_GRAFANA_TOKEN_IDENTITY_UNAVAILABLE",
    ):
        api.verify_token_identity("fixture-value", 7)
    assert calls == [
        f"{target.DEFAULT_GRAFANA_URL}/api/access-control/user/permissions",
    ]


def test_identity_uses_environment_without_env_file_and_reads_it_once(
    monkeypatch, tmp_path
):
    api = _FakeGrafanaApi(accounts=[_account()], tokens=[])
    probes = iter([target.GatewayProbe(True, False, "MCP_GATEWAY_READY")])
    env_file = tmp_path / "missing.env"
    load_calls = []
    constructed = {}
    original_load_env_file = target._load_env_file

    def load_env_file(path):
        load_calls.append(path)
        return original_load_env_file(path)

    def build_api(base_url, **kwargs):
        constructed.update(base_url=base_url, **kwargs)
        return api

    monkeypatch.setenv("AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER", "env-admin")
    monkeypatch.setenv(
        "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD", "env-password"
    )
    monkeypatch.delenv("AZURPILOT_OBSERVABILITY_GRAFANA_URL", raising=False)
    monkeypatch.setattr(target, "_load_env_file", load_env_file)
    monkeypatch.setattr(target, "_GrafanaApi", build_api)
    monkeypatch.setattr(
        target, "ensure_dynamic_tools_disabled", lambda apply: "disabled"
    )
    monkeypatch.setattr(target, "_gateway_probe", lambda: next(probes))
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "fixture-value")

    result = target.ensure_identity(
        repository_root=ROOT,
        env_file=env_file,
        import_profile=False,
    )

    assert result["token"] == "reused"
    assert load_calls == [env_file]
    assert constructed == {
        "base_url": target.DEFAULT_GRAFANA_URL,
        "admin_user": "env-admin",
        "admin_password": "env-password",
    }


def test_identity_stores_new_token_before_reclaiming_obsolete_token(
    monkeypatch, tmp_path
):
    api = _FakeGrafanaApi(
        accounts=[_account()],
        tokens=[{"id": 1, "name": target.CANONICAL_TOKEN_NAME}],
        created_token=(2, "fixture-value"),
    )
    probes = iter(
        [
            target.GatewayProbe(False, True, "MCP_GATEWAY_AUTH_INVALID"),
            target.GatewayProbe(True, False, "MCP_GATEWAY_READY"),
        ]
    )
    stored = []
    events = []
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(target, "_gateway_probe", lambda: next(probes))
    def store_secret(token):
        stored.append(token)
        events.append("store")

    monkeypatch.setattr(target, "_store_secret", store_secret)
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    original_delete = api.delete_token

    def delete_token(account_id, token_id):
        events.append("delete")
        original_delete(account_id, token_id)

    api.delete_token = delete_token
    result = target.ensure_identity(
        repository_root=ROOT,
        env_file=_env_file(tmp_path),
        import_profile=False,
    )

    assert result["token"] == "created"
    assert stored == ["fixture-value"]
    assert events == ["store", "delete"]
    assert ("verify-token-identity", "fixture-value", 1) in api.calls
    assert ("delete-token", 1, 1) in api.calls


def test_identity_reclaims_new_token_when_gateway_probe_fails(
    monkeypatch, tmp_path
):
    api = _FakeGrafanaApi(accounts=[_account()], tokens=[])
    probes = iter(
        [
            target.GatewayProbe(False, True, "MCP_GATEWAY_AUTH_INVALID"),
            target.GatewayProbe(False, False, "MCP_GATEWAY_TOOL_ERROR"),
        ]
    )
    stored = []
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(target, "_gateway_probe", lambda: next(probes))
    monkeypatch.setattr(target, "_store_secret", stored.append)
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    with pytest.raises(
        target.ObservabilityMcpError,
        match="MCP_GATEWAY_AUTH_AFTER_TOKEN_STORE_FAILED",
    ):
        target.ensure_identity(
            repository_root=ROOT,
            env_file=_env_file(tmp_path),
            import_profile=False,
        )

    assert stored == ["fixture-value"]
    assert ("delete-token", 1, 2) in api.calls


@pytest.mark.parametrize(
    "identity_error",
    [
        "MCP_GRAFANA_TOKEN_IDENTITY_FOREIGN",
    ],
)
def test_identity_rotates_foreign_gateway_credential(
    monkeypatch, tmp_path, identity_error
):
    api = _FakeGrafanaApi(
        accounts=[_account()],
        tokens=[{"id": 1, "name": target.CANONICAL_TOKEN_NAME}],
        identity_results=[target.ObservabilityMcpError(identity_error)],
    )
    probes = iter(
        [
            target.GatewayProbe(True, False, "MCP_GATEWAY_READY"),
            target.GatewayProbe(True, False, "MCP_GATEWAY_READY"),
        ]
    )
    stored = []
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "foreign-value")
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(target, "_gateway_probe", lambda: next(probes))
    monkeypatch.setattr(target, "_store_secret", stored.append)
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    result = target.ensure_identity(
        repository_root=ROOT,
        env_file=_env_file(tmp_path),
        import_profile=False,
    )

    assert result["token"] == "created"
    assert stored == ["fixture-value"]
    assert ("delete-token", 1, 1) in api.calls


def test_identity_does_not_rotate_invalid_gateway_role(monkeypatch, tmp_path):
    api = _FakeGrafanaApi(
        accounts=[_account()],
        tokens=[{"id": 1, "name": target.CANONICAL_TOKEN_NAME}],
        identity_results=[
            target.ObservabilityMcpError("MCP_GRAFANA_TOKEN_EFFECTIVE_ROLE_INVALID")
        ],
    )
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "privileged-value")
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(
        target,
        "_gateway_probe",
        lambda: target.GatewayProbe(True, False, "MCP_GATEWAY_READY"),
    )
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    with pytest.raises(
        target.ObservabilityMcpError,
        match="MCP_GRAFANA_TOKEN_EFFECTIVE_ROLE_INVALID",
    ):
        target.ensure_identity(
            repository_root=ROOT,
            env_file=_env_file(tmp_path),
            import_profile=False,
        )

    assert not any(call[0] == "delete-token" for call in api.calls)


def test_identity_rotates_invalid_gateway_secret(monkeypatch, tmp_path):
    api = _FakeGrafanaApi(accounts=[_account()], tokens=[])
    probes = iter(
        [
            target.GatewayProbe(False, True, "MCP_GATEWAY_AUTH_INVALID"),
            target.GatewayProbe(True, False, "MCP_GATEWAY_READY"),
        ]
    )
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(target, "_gateway_probe", lambda: next(probes))
    monkeypatch.setattr(target, "_store_secret", lambda token: None)
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    result = target.ensure_identity(
        repository_root=ROOT,
        env_file=_env_file(tmp_path),
        import_profile=False,
    )

    assert result["token"] == "created"


def test_identity_fails_closed_on_malformed_identity_response(monkeypatch, tmp_path):
    api = _FakeGrafanaApi(
        accounts=[_account()],
        tokens=[],
        identity_results=[
            target.ObservabilityMcpError("MCP_GRAFANA_TOKEN_IDENTITY_INVALID")
        ],
    )
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "fixture-value")
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(
        target,
        "_gateway_probe",
        lambda: target.GatewayProbe(True, False, "MCP_GATEWAY_READY"),
    )
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    with pytest.raises(
        target.ObservabilityMcpError,
        match="MCP_GRAFANA_TOKEN_IDENTITY_INVALID",
    ):
        target.ensure_identity(
            repository_root=ROOT,
            env_file=_env_file(tmp_path),
            import_profile=False,
        )


def test_identity_fails_closed_when_gateway_identity_tool_is_unavailable(
    monkeypatch, tmp_path
):
    api = _FakeGrafanaApi(accounts=[_account()], tokens=[])
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(
        target,
        "_gateway_probe",
        lambda: target.GatewayProbe(True, False, "MCP_GATEWAY_READY"),
    )
    monkeypatch.setattr(target, "runtime_tool_names", lambda: tuple(target.EXPECTED_PROFILE_TOOLS))
    monkeypatch.delenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE", raising=False)
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    with pytest.raises(
        target.ObservabilityMcpError,
        match="MCP_GATEWAY_IDENTITY_UNAVAILABLE",
    ):
        target.ensure_identity(
            repository_root=ROOT,
            env_file=_env_file(tmp_path),
            import_profile=False,
        )


def test_identity_reports_gateway_allowlist_drift(monkeypatch, tmp_path):
    api = _FakeGrafanaApi(accounts=[_account()], tokens=[])
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(
        target,
        "_gateway_probe",
        lambda: target.GatewayProbe(True, False, "MCP_GATEWAY_READY"),
    )
    monkeypatch.setattr(
        target,
        "runtime_tool_names",
        lambda: (_ for _ in ()).throw(
            target.ObservabilityMcpError("MCP_RUNTIME_ALLOWLIST_MISMATCH")
        ),
    )
    monkeypatch.delenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE", raising=False)
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    with pytest.raises(
        target.ObservabilityMcpError,
        match="MCP_GATEWAY_IDENTITY_ALLOWLIST_DRIFT",
    ):
        target.ensure_identity(
            repository_root=ROOT,
            env_file=_env_file(tmp_path),
            import_profile=False,
        )


def test_post_store_noncanonical_identity_deletes_only_new_token(
    monkeypatch, tmp_path
):
    api = _FakeGrafanaApi(
        accounts=[_account()],
        tokens=[{"id": 1, "name": target.CANONICAL_TOKEN_NAME}],
        identity_results=[
            target.GrafanaIdentity(1),
            target.ObservabilityMcpError("MCP_GRAFANA_TOKEN_IDENTITY_FOREIGN"),
        ],
    )
    probes = iter(
        [
            target.GatewayProbe(False, True, "MCP_GATEWAY_AUTH_INVALID"),
        ]
    )
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(target, "_gateway_probe", lambda: next(probes))
    monkeypatch.setattr(target, "_store_secret", lambda token: None)
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    with pytest.raises(
        target.ObservabilityMcpError,
        match="MCP_GRAFANA_TOKEN_IDENTITY_FOREIGN",
    ):
        target.ensure_identity(
            repository_root=ROOT,
            env_file=_env_file(tmp_path),
            import_profile=False,
        )

    assert ("delete-token", 1, 2) in api.calls
    assert ("delete-token", 1, 1) not in api.calls


def test_identity_preserves_original_failure_when_token_reclaim_fails(
    monkeypatch, tmp_path
):
    api = _FakeGrafanaApi(accounts=[_account()], tokens=[])
    probes = iter(
        [
            target.GatewayProbe(False, True, "MCP_GATEWAY_AUTH_INVALID"),
            target.GatewayProbe(False, False, "MCP_GATEWAY_TOOL_ERROR"),
        ]
    )

    def fail_delete(account_id, token_id):
        raise target.ObservabilityMcpError("MCP_GRAFANA_TOKEN_DELETE_FAILED")

    api.delete_token = fail_delete
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(target, "_gateway_probe", lambda: next(probes))
    monkeypatch.setattr(target, "_store_secret", lambda token: None)
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    with pytest.raises(
        target.ObservabilityMcpError,
        match="MCP_GATEWAY_AUTH_AFTER_TOKEN_STORE_FAILED",
    ):
        target.ensure_identity(
            repository_root=ROOT,
            env_file=_env_file(tmp_path),
            import_profile=False,
        )


def test_identity_reclaims_new_token_when_secret_store_fails(monkeypatch, tmp_path):
    api = _FakeGrafanaApi(accounts=[_account()], tokens=[])
    probes = iter([target.GatewayProbe(False, True, "MCP_GATEWAY_AUTH_INVALID")])

    def fail_store(token):
        raise target.ObservabilityMcpError("MCP_SECRET_STORE_FAILED")

    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(
        target, "ensure_dynamic_tools_disabled", lambda apply: "disabled"
    )
    monkeypatch.setattr(target, "_gateway_probe", lambda: next(probes))
    monkeypatch.setattr(target, "_store_secret", fail_store)
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    with pytest.raises(target.ObservabilityMcpError, match="MCP_SECRET_STORE_FAILED"):
        target.ensure_identity(
            repository_root=ROOT,
            env_file=_env_file(tmp_path),
            import_profile=False,
        )

    assert ("delete-token", 1, 2) in api.calls


def test_grafana_api_accepts_empty_delete_response():
    api = target._GrafanaApi(
        target.DEFAULT_GRAFANA_URL,
        admin_user="admin",
        admin_password="fixture-password",
        opener=lambda *_args, **_kwargs: _EmptyResponse(),
    )

    api.delete_token(1, 2)


@pytest.mark.parametrize(
    ("payload", "authentication_failed"),
    [
        ({"isError": True, "data": {"status": 401}}, False),
        (
            {
                "isError": True,
                "content": [{"type": "text", "text": "401 Unauthorized"}],
            },
            True,
        ),
    ],
)
def test_gateway_probe_classifies_only_error_text(
    monkeypatch, payload, authentication_failed
):
    monkeypatch.setattr(
        target,
        "_run_docker",
        lambda *args, **kwargs: target.subprocess.CompletedProcess(
            args, 0, json.dumps(payload), ""
        ),
    )

    result = target._gateway_probe()

    assert result.authentication_failed is authentication_failed
    assert result.code == (
        "MCP_GATEWAY_AUTH_INVALID"
        if authentication_failed
        else "MCP_GATEWAY_TOOL_ERROR"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://[::1]:3000",
    ],
)
def test_grafana_url_accepts_only_loopback_hosts(url):
    assert target._grafana_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://grafana.example:3000",
        "https://192.168.1.20:3000",
    ],
)
def test_grafana_url_rejects_non_loopback_hosts(url):
    with pytest.raises(target.ObservabilityMcpError, match="MCP_GRAFANA_URL_INVALID"):
        target._grafana_url(url)


def test_identity_fails_closed_on_duplicate_canonical_accounts(monkeypatch, tmp_path):
    api = _FakeGrafanaApi(accounts=[_account(), _account(account_id=2)], tokens=[])
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    with pytest.raises(
        target.ObservabilityMcpError,
        match="MCP_GRAFANA_SERVICE_ACCOUNT_DUPLICATE",
    ):
        target.ensure_identity(
            repository_root=ROOT,
            env_file=_env_file(tmp_path),
            import_profile=False,
        )


@pytest.mark.parametrize("invalid_id", [None, "1", 0, -1])
def test_identity_fails_closed_on_malformed_existing_account_id(
    monkeypatch, tmp_path, invalid_id
):
    api = _FakeGrafanaApi(accounts=[_account(account_id=invalid_id)], tokens=[])
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    with pytest.raises(
        target.ObservabilityMcpError,
        match="MCP_GRAFANA_SERVICE_ACCOUNT_RESPONSE_INVALID",
    ):
        target.ensure_identity(
            repository_root=ROOT,
            env_file=_env_file(tmp_path),
            import_profile=False,
        )


def test_secret_value_is_passed_only_on_stdin(monkeypatch):
    calls = []

    def checked_docker(arguments, *, input_text=None, timeout=120):
        calls.append((arguments, input_text))

    monkeypatch.setattr(target, "_checked_docker", checked_docker)
    target._store_secret("fixture-value")

    assert calls == [
        (["mcp", "secret", "set", target.CANONICAL_SECRET_NAME], "fixture-value\n")
    ]
    assert "fixture-value" not in " ".join(calls[0][0])


def test_gateway_tool_arguments_accept_protocol_names_and_reject_shell_like_keys(
    monkeypatch,
):
    calls = []

    def run_docker(arguments, *, input_text=None, timeout=120):
        calls.append(arguments)
        return target.subprocess.CompletedProcess(
            arguments, 0, '{"ok": true}\n', ""
        )

    monkeypatch.setattr(target, "_run_docker", run_docker)

    assert target._gateway_tool_call(
        "alerting_manage_rules", {"operation": "list", "rule_limit": "50"}
    ) == {"ok": True}
    assert calls[0][-2:] == ["operation=list", "rule_limit=50"]

    with pytest.raises(target.ObservabilityMcpError, match="MCP_TOOL_ARGUMENT_INVALID"):
        target._gateway_tool_call("list_datasources", {"bad;key": "value"})


def test_admin_api_401_is_credentials_rejected_not_stale_volume():
    def reject_credentials(*_args, **_kwargs):
        raise target.HTTPError(
            target.DEFAULT_GRAFANA_URL,
            401,
            "Unauthorized",
            hdrs=None,
            fp=None,
        )

    api = target._GrafanaApi(
        target.DEFAULT_GRAFANA_URL,
        admin_user="admin",
        admin_password="fixture-password",
        opener=reject_credentials,
    )

    with pytest.raises(
        target.ObservabilityMcpError,
        match="MCP_GRAFANA_ADMIN_CREDENTIALS_REJECTED",
    ):
        api.search_service_account()


def test_identity_does_not_bypass_admin_api_when_gateway_token_is_valid(
    monkeypatch, tmp_path
):
    class RejectingAdminApi:
        def search_service_account(self):
            raise target.ObservabilityMcpError(
                "MCP_GRAFANA_ADMIN_CREDENTIALS_REJECTED"
            )

    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: RejectingAdminApi())
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        target,
        "_gateway_probe",
        lambda: pytest.fail("Gateway probe must not bypass Admin API"),
    )

    with pytest.raises(
        target.ObservabilityMcpError,
        match="MCP_GRAFANA_ADMIN_CREDENTIALS_REJECTED",
    ):
        target.ensure_identity(
            repository_root=ROOT,
            env_file=_env_file(tmp_path),
            import_profile=False,
        )


def test_recover_admin_uses_one_shot_same_volume_and_runs_full_verification(
    monkeypatch, tmp_path
):
    env_file = _env_file(tmp_path)
    compose_calls = []
    events = []

    def checked_compose(
        *,
        repository_root,
        env_file,
        arguments,
        error_code="MCP_GRAFANA_COMPOSE_COMMAND_FAILED",
        input_text=None,
        timeout=120,
    ):
        compose_calls.append(
            {
                "arguments": list(arguments),
                "input_text": input_text,
                "error_code": error_code,
            }
        )
        if arguments == ["config", "--format", "json"]:
            return target.subprocess.CompletedProcess(
                arguments, 0, json.dumps(_compose_contract_payload()), ""
            )
        return target.subprocess.CompletedProcess(arguments, 0, "", "")

    def run_docker(arguments, *, input_text=None, timeout=120):
        assert arguments == ["volume", "inspect", target.CANONICAL_GRAFANA_VOLUME]
        assert input_text is None
        return target.subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps([{"Name": target.CANONICAL_GRAFANA_VOLUME}]),
            "",
        )

    class AdminApi:
        def __init__(self, *_args, **_kwargs):
            pass

        def verify_admin_credentials(self):
            events.append("admin-api")

    monkeypatch.setattr(target, "_checked_compose", checked_compose)
    monkeypatch.setattr(target, "_run_docker", run_docker)
    monkeypatch.setattr(target, "_GrafanaApi", AdminApi)
    monkeypatch.setattr(
        target,
        "ensure_identity",
        lambda **_kwargs: events.append("ensure-identity")
        or {
            "ok": True,
            "account": target.CANONICAL_SERVICE_ACCOUNT,
            "role": target.CANONICAL_SERVICE_ACCOUNT_ROLE,
            "token": "reused",
            "gateway": "MCP_GATEWAY_READY",
        },
    )
    monkeypatch.setattr(
        target,
        "_gateway_probe",
        lambda: events.append("gateway")
        or target.GatewayProbe(True, False, "MCP_GATEWAY_READY"),
    )

    result = target.recover_admin_credentials(
        repository_root=ROOT,
        env_file=env_file,
        import_profile=False,
    )

    assert events == ["admin-api", "ensure-identity", "gateway"]
    assert result == {
        "ok": True,
        "admin_api": "verified",
        "volume": target.CANONICAL_GRAFANA_VOLUME,
        "identity": {
            "ok": True,
            "account": target.CANONICAL_SERVICE_ACCOUNT,
            "role": target.CANONICAL_SERVICE_ACCOUNT_ROLE,
            "token": "reused",
            "gateway": "MCP_GATEWAY_READY",
        },
        "gateway": "MCP_GATEWAY_READY",
    }
    assert [call["arguments"][0] for call in compose_calls] == [
        "config",
        "stop",
        "ps",
        "run",
        "up",
    ]
    reset_call = next(call for call in compose_calls if call["arguments"][0] == "run")
    assert reset_call["arguments"] == [
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "/bin/sh",
        target.CANONICAL_GRAFANA_SERVICE,
        "-c",
        "exec grafana cli admin reset-admin-password --password-from-stdin "
        f"--user-id {target.GRAFANA_ADMIN_USER_ID} < {target.GRAFANA_ADMIN_SECRET_PATH}",
    ]
    assert reset_call["input_text"] is None
    assert "fixture-password" not in " ".join(reset_call["arguments"])
    assert target.GRAFANA_ADMIN_SECRET_PATH in reset_call["arguments"][-1]
    assert not any(
        call["arguments"][:2] in (["volume", "rm"], ["volume", "create"])
        or "down" in call["arguments"]
        for call in compose_calls
    )


def test_recover_admin_restarts_grafana_when_reset_fails(monkeypatch, tmp_path):
    env_file = _env_file(tmp_path)
    compose_calls = []

    def checked_compose(
        *,
        repository_root,
        env_file,
        arguments,
        error_code="MCP_GRAFANA_COMPOSE_COMMAND_FAILED",
        input_text=None,
        timeout=120,
    ):
        compose_calls.append((list(arguments), input_text, error_code))
        if arguments == ["config", "--format", "json"]:
            return target.subprocess.CompletedProcess(
                arguments, 0, json.dumps(_compose_contract_payload()), ""
            )
        if arguments[0] == "run":
            raise target.ObservabilityMcpError("MCP_GRAFANA_ADMIN_RESET_FAILED")
        return target.subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(target, "_checked_compose", checked_compose)
    monkeypatch.setattr(
        target,
        "_run_docker",
        lambda arguments, **_kwargs: target.subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps([{"Name": target.CANONICAL_GRAFANA_VOLUME}]),
            "",
        ),
    )

    with pytest.raises(
        target.ObservabilityMcpError, match="MCP_GRAFANA_ADMIN_RESET_FAILED"
    ):
        target.recover_admin_credentials(
            repository_root=ROOT,
            env_file=env_file,
            import_profile=False,
        )

    assert [call[0][0] for call in compose_calls] == [
        "config",
        "stop",
        "ps",
        "run",
        "up",
    ]
