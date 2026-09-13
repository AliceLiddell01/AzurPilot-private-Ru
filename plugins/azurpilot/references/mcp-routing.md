# MCP routing

Этот пакет распространяет skills и metadata. Он не регистрирует MCP-серверы и
не загружает Connected App manifest. Единственным project-scoped repository-level
источником регистрации Codex является `.codex/config.toml`.

| Workflow | Codex route | Transport | Backend implementation | Fallback |
| --- | --- | --- | --- | --- |
| Development | `azurpilot-dev` | direct local stdio | `module.dev_mcp` | none |
| Game | `azurpilot-game` | direct local stdio | `module.game_mcp` | none |
| Troubleshooting | read-only evidence соответствующего direct route | direct local stdio | соответствующий `module.*_mcp` | none |
| ChatGPT/public | отдельная remote surface | authenticated HTTPS/remote | соответствующий `module.*_mcp.remote` той же backend family | не является Codex fallback |

Канонические локальные команды:

```text
azurpilot-dev  → uv run --locked --no-sync python -m module.dev_mcp
azurpilot-game → uv run --locked --no-sync python -m module.game_mcp
```

Для обычной Codex-сессии отсутствие direct callable catalog или несовместимый
contract означает fail-closed остановку и диагностику. Reconnect, OAuth или
Connected App refresh относятся только к явно выбранной ChatGPT/public remote
surface и не заменяют local stdio route.
