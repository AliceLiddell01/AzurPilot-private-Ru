# Проверенная архитектура Game workflow

Эта справка фиксирует устойчивые связи между Game MCP и bot architecture. Она
не является копией task catalog и не заменяет runtime contract конкретной
сессии.

## Источники текущего форка

- `docs/game-mcp.md` описывает standalone stateless read/control plane,
  canonical `profile`, раздельные Game scopes и границы legacy SSE.
- `module/game_mcp/contract.py` и `module/game_mcp/server.py` являются
  источниками contract, tool schemas, annotations и required scopes.
- `module/game_mcp/adapter.py` валидирует bounded arguments, выбирает profile и
  сериализует sanitized responses.
- `module/application/services.py` и `module/application/legacy_adapters.py`
  проецируют generated task/config sources через typed application boundary.
- `module/application/legacy_game_adapters.py` владеет узкими profile, emulator,
  ADB, config, log и screenshot adapters; Game MCP не выдаёт arbitrary shell,
  SQL, filesystem или Dev Runtime access.

Каждый profile-dependent request должен явно идентифицировать `<profile>`.
Нельзя выводить serial, package, локальные пути или credentials в пользовательскую
сводку.

## Граница жизненного цикла

В `alas.py` методы `start()` и `restart()` используют существующий
`LoginHandler`. `goto_main()` сначала проверяет состояние приложения, затем
использует UI navigation для уже запущенного приложения либо запускает
существующий login flow и после этого переходит к main. `LoginHandler` и
`module/device/app_control.py` получают package/server/control method из
конфигурации, а не из универсального значения skill.

`module/ui/ui.py` предоставляет authoritative `is_in_main()` и
`ui_goto_main()`. Эти UI checks не следует заменять предположением по имени
профиля, foreground-факту или exit code внешнего процесса.

Из этого следуют отдельные claims:

```text
profile lifecycle != emulator lifecycle != ADB readiness
game process running != game foreground != login completed != main UI
```

Если конкретная версия Game MCP публикует composite runtime/login action, его
проверки должны следовать его schema и application adapter. Отсутствие action в
current catalog означает capability gap, а не разрешение на прямой ADB/input
обход.

## Контекст upstream

Публичный upstream ALAS подтверждает reuse `LoginHandler.app_restart()` для
`restart()`, `LoginHandler.app_start()` для `start()` и различие веток
`goto_main()` для already-running и not-running application. Public issue logs
также показывают, что restart/login flows зависят от фактического server/package
variant. Это контекст для чтения текущего форка, а не универсальный API:

- <https://github.com/LmeSzinc/AzurLaneAutoScript/blob/master/alas.py>
- <https://github.com/LmeSzinc/AzurLaneAutoScript/issues/4858>
- <https://github.com/LmeSzinc/AzurLaneAutoScript/issues/4859>
- <https://github.com/LmeSzinc/AzurLaneAutoScript/issues/5750>

При расхождении приоритет имеют текущий форк, executable tests и live
contract/catalog.
