# Добавление новой функции

Новая функция должна начинаться не с файла, а с **наблюдаемого поведения**.

## 1. Сформулируйте результат

Ответьте:

- кто пользователь функции;
- что он сможет сделать;
- что считается успешным результатом;
- какие ошибки должны быть видимыми;
- какие действия могут быть необратимыми или расходовать ресурсы.

## 2. Найдите владельца поведения

Используйте [карту подсистем](subsystems.md).

Например:

- game UI → `module/`;
- campaign map → `campaign/` + runtime owner;
- CLI lifecycle → `azurpilot/tooling/`;
- persistence → application + persistence + migration;
- пользовательская настройка → source YAML + generator + WebUI consumer;
- MCP → отдельный Dev/Game contract.

Не создавайте новый параллельный слой только потому, что существующий код сложный.

## 3. Проверьте сквозной поток

Изменение редко заканчивается одним function call.

Для новой пользовательской настройки поток может быть:

```text
argument.yaml
→ generated args/config/i18n
→ WebUI
→ profile
→ runtime consumer
→ test
→ документация
```

Для persistence:

```text
application contract
→ repository/UoW
→ PostgreSQL implementation
→ migration
→ production wiring
→ tests/rollback
```

## 4. Реализуйте минимальный связный diff

Не форматируйте соседний проект целиком и не переименовывайте несвязанные сущности.

Если во время работы обнаружена отдельная проблема, не превращайте feature PR в бесконечный cleanup без необходимости.

## 5. Добавьте тесты

Начните с targeted tests.

Проверяйте не только happy path, но и:

- invalid input;
- unknown/ambiguous state;
- timeout;
- retry bounds;
- idempotency;
- ownership;
- rollback;
- secret/redaction behavior — если релевантно.

## 6. Обновите generated outputs

Если изменён source generator, пересоберите производные файлы и убедитесь, что повторная генерация не оставляет diff.

## 7. Обновите документацию

Если меняется пользовательское поведение, обновите GitBook в том же PR.

Не заставляйте пользователя изучать diff или исходный код, чтобы узнать о новой функции.

## 8. Пройдите проверки

Порядок от дешёвых к дорогим:

1. syntax/static;
2. targeted tests;
3. lint;
4. relevant integration;
5. full relevant suite;
6. security/secret checks;
7. live acceptance — только если нужен реальный device/browser/game;
8. exact-head CI.

## 9. Rollback

До merge должно быть понятно, как вернуться назад.

Для обычного кода это может быть revert. Для schema/data/update/lifecycle функции нужен более строгий recovery plan.
