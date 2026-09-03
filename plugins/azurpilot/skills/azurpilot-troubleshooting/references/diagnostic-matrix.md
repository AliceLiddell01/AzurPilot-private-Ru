# Матрица диагностических сценариев

Это вспомогательная матрица классификации. Значения `N`, fingerprints и имена
будущих tools всегда берутся из текущего contract/catalog; исторические значения
incident не являются фикстурами production skill.

| Сценарий | Evidence | Диагноз и безопасное поведение |
| --- | --- | --- |
| A. Catalog drift | backend contract публикует capability, а текущая session её не видит; сравнение count/fingerprint/exact surface расходится | stale client/plugin/app/session snapshot; не выполнять mutation, выбрать один подтверждённый refresh, затем повторно проверить callable catalog и соответствующий contract. Вернуться к normal operation можно только при совместимости обоих источников. |
| B. Runtime drift | Git HEAD новее, чем код работающего backend process | stale deployment/runtime; выполнить только owned reload и подтвердить новый process/contract. |
| C. Exit/postcondition | command acknowledgement или exit success, но authoritative state не изменился | `STOP WRITES` → read-only recovery → Last Confirmed State; automatic retry запрещён даже при успешном transport acknowledgement/exit success. Следующая mutation допустима только после локализации причины и нового подтверждённого решения. |
| D. Authorization | app policy разрешает actions, но backend возвращает authorization failure | отдельно проверить OAuth/provider grant и required scope; UI policy alone не доказательство. |
| E. Timeout | control call оборван или не имеет результата | `STOP WRITES`, read-only recovery, Last Confirmed State; automatic retry запрещён. |
| F. Refresh without evidence | fork/subtask/same-directory fork или новый browser tab создан, но transcript, tool marker или machine result отсутствует | refresh не доказан; действие не считать выполненным и mutation не повторять. |
| G. Routing regression | обычный Game request загрузил только Development workflow | вернуть Game operation в `azurpilot-game-control`; catalog/runtime/auth mismatch направить сюда; Development не использовать как Game fallback. |
| H. Browser refresh unavailable | browser/UI automation дважды зависла на AX/DOM или не дала authoritative result | классифицировать `browser automation` как `unavailable` для текущего run; больше автоматически не повторять тот же Browser path и не считать tabs/forks refresh evidence. Если доступен `Computer Use`, выполнить ту же ограниченную операцию визуально в уже открытом browser window и повторить authoritative read-only verification. При недоступности или ошибке `Computer Use` остановить browser recovery без retry loop и без обхода Game/Dev MCP. |

Дополнительные быстрые признаки:

- `Unknown tool(namespace.name)` обычно локализует client/plugin binding;
- profile `stopped` вместе с game foreground — разные state domains;
- platform block до backend — внешний safety layer, а не Game MCP error;
- `GAME_*`/`DEV_*` result — вызов достиг соответствующего backend boundary.

Для сценария A fail-closed означает: пока требуемый action отсутствует в
callable surface текущей session, не вызывать ни старый аналог, ни цепочку
restart/login. Если `game_get_contract` не callable, зафиксируй `backend contract unavailable`:
capability gap не доказан, mutation запрещена, а диагностика остаётся на
client/plugin/app/session layer. После доказанной синхронизации и совместимости
contract верни пользовательскую операцию в normal Game workflow,
где mutation и её authoritative postcondition определяются текущим Game
contract.
