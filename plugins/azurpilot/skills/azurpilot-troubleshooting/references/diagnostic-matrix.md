# Матрица диагностических сценариев

Это reusable classification aid. Значения `N`, fingerprints и имена будущих
tools всегда берутся из текущего contract/catalog; исторические incident
values не являются fixtures production skill.

| Сценарий | Evidence | Диагноз и безопасное поведение |
| --- | --- | --- |
| A. Catalog drift | backend contract публикует capability, а текущая session её не видит; сравнение count/fingerprint/exact surface расходится | stale client/plugin/app/session snapshot; не выполнять mutation, выбрать один подтверждённый refresh и повторить read-only comparison. |
| B. Runtime drift | Git HEAD новее, чем код работающего backend process | stale deployment/runtime; выполнить только owned reload и подтвердить новый process/contract. |
| C. Exit/postcondition | command acknowledgement или exit success, но authoritative state не изменился | доверять postcondition, сохранить failure и искать backend/external cause. |
| D. Authorization | app policy разрешает actions, но backend возвращает authorization failure | отдельно проверить OAuth/provider grant и required scope; UI policy alone не доказательство. |
| E. Timeout | control call оборван или не имеет результата | `STOP WRITES`, read-only recovery, Last Confirmed State; automatic retry запрещён. |
| F. Fork without evidence | fork/subtask создан, но transcript, tool marker или machine result отсутствует | fork не доказал refresh и mutation; не повторять действие, вернуться к evidence. |
| G. Routing regression | обычный Game request загрузил только Development workflow | вернуть Game operation в `azurpilot-game-control`; catalog/runtime/auth mismatch направить сюда; Development не использовать как Game fallback. |

Дополнительные быстрые признаки:

- `Unknown tool(namespace.name)` обычно локализует client/plugin binding;
- profile `stopped` вместе с game foreground — разные state domains;
- platform block до backend — внешний safety layer, а не Game MCP error;
- `GAME_*`/`DEV_*` result — вызов достиг соответствующего backend boundary.

Для сценария A fail-closed означает: пока требуемый action отсутствует в
callable surface текущей session, не вызывать ни старый аналог, ни цепочку
restart/login. После доказанной синхронизации верни пользовательскую
операцию в normal Game workflow, где mutation и её authoritative postcondition
определяются текущим Game contract.
