from pathlib import Path

import pytest

from dev_tools import observability_mcp as target

ROOT = Path(__file__).resolve().parents[1]


class _FakeGrafanaApi:
    def __init__(self, *, accounts, tokens, created_token=(2, "fixture-value")):
        self.accounts = accounts
        self.tokens = tokens
        self.created_token = created_token
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

    def verify_token(self, token):
        self.calls.append(("verify-token", token))

    def delete_token(self, account_id, token_id):
        self.calls.append(("delete-token", account_id, token_id))


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


def test_identity_reuses_valid_gateway_secret_without_creating_token(
    monkeypatch, tmp_path
):
    api = _FakeGrafanaApi(accounts=[_account()], tokens=[])
    probes = iter([target.GatewayProbe(True, False, "MCP_GATEWAY_READY")])
    monkeypatch.setattr(target, "_GrafanaApi", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(target, "_gateway_probe", lambda: next(probes))
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

    result = target.ensure_identity(
        repository_root=ROOT,
        env_file=_env_file(tmp_path),
        import_profile=False,
    )

    assert result["token"] == "reused"
    assert "create-token" not in {
        call[0] for call in api.calls if isinstance(call, tuple)
    }
    assert "delete-token" not in {call[0] for call in api.calls if isinstance(call, tuple)}


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
    monkeypatch.setattr(target, "ensure_dynamic_tools_disabled", lambda apply: "disabled")
    monkeypatch.setattr(target, "_gateway_probe", lambda: next(probes))
    monkeypatch.setattr(target, "_checked_docker", lambda *args, **kwargs: None)

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
    monkeypatch.setattr(target, "_store_secret", lambda token: (stored.append(token), events.append("store")))
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
    assert ("verify-token", "fixture-value") in api.calls
    assert ("delete-token", 1, 1) in api.calls


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
