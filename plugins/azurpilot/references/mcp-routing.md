# MCP routing

Этот пакет распространяет skills и metadata. Он не регистрирует MCP-серверы и
не загружает Connected App manifest. Единственным project-scoped repository-level
источником регистрации Codex является `.codex/config.toml`.

## Trust и effective registration

Trust проекта — обязательное предварительное условие для project-scoped route.
Если checkout имеет состояние `untrusted`, Codex пропускает project-scoped
`.codex/config.toml`; plugin не выдаёт trust, а диагностика не выполняет
automatic trust. Поэтому структурно корректный source config ещё не доказывает,
что route зарегистрирован в текущей Codex-сессии.

Read-only порядок проверки такой: trust проекта → effective registration обоих
routes → MCP `initialize` и `tools/list` → соответствующий backend contract и
callable catalog. `dev_tools.mcp_status` намеренно разделяет поля
`source_config` (доказательство tracked `.codex/config.toml`) и
`effective_codex_registration` (только authoritative evidence из новой или
перезагруженной trusted Codex task). Значение `not_observable` или pending для
effective registration является честным ограничением наблюдаемости, а не
`ready`; collector не заменяет это состояние синтетическим CLI scrape.

При этой диагностике нельзя использовать Connected App, OAuth или remote
surface как fallback для direct route. Reconnect и refresh относятся только к
явно выбранной remote surface; project trust и effective registration должны
быть подтверждены отдельно.

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
