# Draft PR, merge и cleanup

## Pre-merge

После реализации и relevant gates:

1. перечитай base→head diff и выполни adversarial self-review;
2. проведи доступный CodeRabbit checkpoint и разберись с каждым finding;
3. создай содержательный commit и push в тематическую ветку из task contract
   формата `domain/<unique-capability-name>`; уже опубликованную `codex/<legacy-capability>`
   ветку продолжай только после проверки exact identity и head;
4. создай или обнови **draft PR** в `personal/stable` через typed spec и
   temporary body-file с explicit `--repo`, `--base`, `--head`; body обязан
   содержать exact identity, scope, подсистемы, проверки, security result,
   CodeRabbit disposition, rollback и ограничения. Body пиши на русском языке
   и делай полноценным отчётом: каждая секция должна содержать конкретные
   факты, а sections со scope/реализацией/проверками/CI/security/rollback/
   ограничениями — маркированные списки, а не короткие общие фразы. Английский
   допускается только для технических identifiers, API/tool names, protocol
   tokens и других необходимых специальных слов;
5. проверь required CI на exact head, отсутствие blocking review threads,
   итоговый diff и secret scan;
6. установи состояние `READY_FOR_CHATGPT_REVIEW` и остановись.

Финальное ревью выполняет пользователь выбранным им способом. Ни green CI, ни
self-review, ни CodeRabbit не дают разрешение на merge. Не запускай отдельное
«финальное пользовательское ревью» самостоятельно.

### Граница состояний CodeRabbit

До финального пользовательского review rate limit/cooldown CodeRabbit означает: не ждать,
сохранить последний exact head, выполнить остальные доступные gates и завершить
pre-merge прогон в `READY_FOR_CHATGPT_REVIEW`. Это исключение не отменяет
required CI, security/secret scan, обязательный product/live acceptance или
blocking review threads.

После финального пользовательского review, но до отдельной текущей команды пользователя,
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
финального пользовательского review изменился relevant diff, повтори затронутые проверки и
review. Используй только разрешённый проектом merge method.

## Post-merge cleanup

После фактически подтверждённого merge:

- проверь merged state и post-merge required checks;
- выполни релевантный post-merge smoke/verification;
- переключи основной checkout на `personal/stable` и синхронизируй его обычным
  разрешённым способом;
- безопасно удали task branch локально и на GitHub, если это допускает проект;
- сохрани permanent WSL2 CodeRabbit review checkout; удали только временные
  resources этой задачи;
- не трогай пользовательские unrelated files.
