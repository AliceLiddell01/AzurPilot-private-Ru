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

Read-only порядок проверки такой: trust проекта → выбранный direct route →
negotiated MCP discovery через официальный SDK → `tools/list` → соответствующий
backend contract и callable catalog. Обязательный fresh client acceptance
создаётся как новый SDK process/session и дополнительно выполняет обязательные
read-only capability calls; для legacy-compatible server SDK сам выполняет
штатный `initialize` fallback. Plugin не реализует собственный parser и не
подменяет discovery универсальным handshake.
`dev_tools.mcp_status` намеренно разделяет поля
`source_config` (доказательство tracked `.codex/config.toml`) и
`effective_codex_registration` (только authoritative evidence из новой или
перезагруженной trusted Codex task). Значение `not_observable` или pending для
effective registration является честным ограничением наблюдаемости, а не
`ready`; collector не заменяет это состояние синтетическим CLI scrape.
Effective Codex registration — отдельная optional integration check. Если
изменение затрагивает Codex/plugin registration, client-visible schema или
routing, действует [единый контракт cross-thread continuation](../../../.agents/skills/azurpilot-repository-development/references/cross-thread-task-delegation.md),
а не ручной новый чат или Connected App fallback. Wrong-HEAD или недоступный
`create_thread` не блокируют уже успешный fresh MCP client gate.

При этой диагностике нельзя молча переключаться между transport routes и
использовать Connected App, OAuth или remote surface как fallback для direct
route. Reconnect и refresh относятся только к явно выбранной remote surface;
project trust и effective registration должны быть подтверждены отдельно.

| Workflow | Codex route | Transport | Backend implementation | Fallback |
| --- | --- | --- | --- | --- |
| Development | `azurpilot-dev` | direct local stdio | `module.dev_mcp` | none |
| Game | `azurpilot-game` | direct local stdio | `module.game_mcp` | none |
| Codex Desktop Development | `azurpilot_dev` | authenticated loopback local HTTP | `module.dev_mcp.local_http` | none |
| Codex Desktop Game | `azurpilot_game` | authenticated loopback local HTTP | `module.game_mcp.local_http` | none |
| Troubleshooting | read-only evidence соответствующего direct route | direct local stdio | соответствующий `module.*_mcp` | none |
| ChatGPT/public | отдельная remote surface | authenticated HTTPS/remote | соответствующий `module.*_mcp.remote` той же backend family | не является Codex fallback |

Canonical developer synchronization после candidate freeze:

```text
azur mcp sync --base <exact-base-sha>
```

`NO_CHANGES` — terminal no-op; `SYNCED` включает source/version finalization от
exact base, generated metadata, восстановление только доказанного owned stale
runtime, readiness и fresh-client acceptance. После изменения MCP source-set
повтори sync, который пересчитает версию от base и нового candidate. Unknown или
foreign ownership, port conflict и failure readiness остаются fail-closed.
Текущая внешняя Codex session не является postcondition; hot reload не
предполагается. `impact`, `status`, `versions`, `reconcile`, `start`, `stop` и
`restart` остаются admin/diagnostic capabilities. Внутренние `module.*_mcp` и
supervisor modules напрямую не запускаются. Если `azur` отсутствует в PATH,
workflow fail-closed; `uv run`, Python module entrypoint и shell wrapper не
являются fallback.

Codex Desktop aliases намеренно отличаются от protocol identities:
`azurpilot_dev` → `http://127.0.0.1:8775/mcp` и
`azurpilot_game` → `http://127.0.0.1:8776/mcp`. Их bearer tokens берутся из
user-level environment; literal token в repository config запрещён.
Supervisor `module.mcp_shared.local_http_supervisor` владеет обоими
процессами, проверяет `/ready` и завершает только exact-owned children.

Для обычной Codex-сессии отсутствие direct callable catalog или несовместимый
contract означает fail-closed остановку и диагностику. Reconnect, OAuth или
Connected App refresh относятся только к явно выбранной ChatGPT/public remote
surface и не заменяют local stdio route.

## Canonical bundle и lifecycle

`config/mcp-versions.toml` — единственный source of truth для first-party
server identity, API/contract schema, tool/capability fingerprints, source sets,
plugin version и skill bundle revision. `plugins/azurpilot/compatibility.json`
является производным snapshot. Normal candidate synchronization выполняется
через `azur mcp sync --base <exact-base-sha>`. Diagnostic/admin capabilities
`azur mcp status`, `versions`, `impact`, `reconcile`, `start`, `stop` и
`restart` остаются доступны; runtime reconcile не редактирует tracked source.
Backend source sets — bounded explicit mapping реальных MCP application
dependencies; management-only `azurpilot/tooling/mcp.py` не входит в runtime
identity. Sync проверяет generated bundle и base-to-candidate compatibility до
runtime acceptance. Session/plugin reload state остаётся диагностическим
evidence и не отменяет readiness свежего клиента.
