# Draft PR, merge и cleanup

## Pre-merge

После реализации и relevant gates:

1. перечитай base→head diff и выполни adversarial self-review;
2. проведи доступный CodeRabbit checkpoint и разберись с каждым finding;
3. создай содержательный commit и push в тематическую `codex/*` ветку;
4. создай или обнови **draft PR** в `personal/stable` с scope, base SHA,
   подсистемами, проверками, security result, rollback и ограничениями;
5. проверь required CI на exact head, отсутствие blocking review threads,
   итоговый diff и secret scan;
6. установи состояние `READY_FOR_CHATGPT_REVIEW` и остановись.

Финальное ревью выполняет пользователь через ChatGPT 5.6 Sol. Ни green CI, ни
self-review, ни CodeRabbit не дают разрешение на merge. Не запускай отдельное
«финальное ревью ChatGPT» самостоятельно.

### Граница состояний CodeRabbit

До финального ChatGPT review rate limit/cooldown CodeRabbit означает: не ждать,
сохранить последний exact head, выполнить остальные доступные gates и завершить
pre-merge прогон в `READY_FOR_CHATGPT_REVIEW`. Это исключение не отменяет
required CI, security/secret scan, обязательный product/live acceptance или
blocking review threads.

После финального ChatGPT review, но до отдельной текущей команды пользователя,
нужно только ожидать эту команду. Rate limit не возвращает lifecycle в
`READY_FOR_CHATGPT_REVIEW` и не меняет состояние `merge-authorized`.

После отдельной команды выполни exact-head revalidation и разрешённый merge,
затем post-merge verification и cleanup; итоговое состояние — `merged`. Rate
limit не может перевести merge-authorized или merged lifecycle обратно в
pre-merge состояние. Если после финального review изменился relevant diff,
повтори затронутые gates/review и получи новое актуальное разрешение на merge.

## Merge gate

Merge запрещён, пока нет отдельного текущего сообщения пользователя,
однозначно разрешающего merge именно этого PR. Фразы «сделай всё», «доведи до
конца» или старое разрешение для другой задачи недостаточны.

Перед merge заново проверь актуальный PR head, base, required `Python`/`Windows`/
`Security`, unresolved threads, relevant diff и secret scan. Если после
финального ChatGPT review изменился relevant diff, повтори затронутые проверки и
review. Используй только разрешённый проектом merge method.

## Post-merge cleanup

После фактически подтверждённого merge:

- проверь merged state и post-merge required checks;
- выполни релевантный post-merge smoke/verification;
- переключи основной checkout на `personal/stable` и синхронизируй его обычным
  разрешённым способом;
- безопасно удали task branch локально и на GitHub, если это допускает проект;
- удали WSL2 Arch CodeRabbit review checkout и только временные ресурсы этой
  задачи;
- не трогай пользовательские unrelated files.
