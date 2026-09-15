# Проверки и публикация

Выбирай проверки по фактическому diff, а не по названию задачи.

## Последовательность

1. Проверь структуру, diff, незапланированные файлы и `git diff --check`.
2. Выполни syntax/compile и repository-defined lint для затронутого языка.
3. Запусти targeted tests, затем полный релевантный набор один раз перед draft
   PR или финальным checkpoint, если после него не было существенного diff.
4. Запусти generator, dependency, migration, browser, device или live gates
   только когда этого требует изменённая граница.
5. Перед commit/push и повторно после существенного relevant fix запусти
   фактический secret scanner. Ручной поиск паттернов — только дополнение.
6. Проведи self-review base→head и проверь, что все заявленные результаты
   действительно получены текущими командами.

Если gate недоступен, запиши точную причину и не называй непроверенное состояние
готовым. CodeRabbit rate limit обрабатывается отдельно: cooldown не ждём и не
делаем бессмысленных retry; остальные gates продолжаем.

## Exact-head CI

После push draft PR дождись и проверь именно head PR, а не только имя ветки.
Required contexts должны быть `Python`, `Windows`, `Security`. Источник истины —
`.github/workflows/ci.yml` и `docs/ci.md`; permanent CI остаётся stage-agnostic и
не должен зависеть от historical SHA, committed evidence или временного номера
этапа.

В PR report разделяй локальные результаты, exact-head CI, внешний review и
ограничения среды. Не выдавай skipped/rate-limited status за substantive review.

## Delivery и PR publication gates

Для `azur delivery publish` manifest обязан быть closed-schema и содержать
exact repository, expected branch/local HEAD, base SHA, remote ref,
preimage/postimage и allowlist. Service не принимает unrelated staged paths,
не использует `git add .`, force/force-with-lease или blind retry. Gitleaks
запускается по staged index и exact committed range; рекурсивный scan всего
checkout не является заменой scoped evidence.

После push проверь `ls-remote` exact remote SHA. Timeout/unknown push оставляет
external journal в `in_flight`/`unknown`, а recovery выполняет только
read-only ref check.

Для `azur pr publish` используй typed spec и structured body с обязательными
разделами, temporary external Markdown file и `--body-file`. Каждый `gh pr`
вызов получает explicit `--repo`; read-back должен подтвердить repository,
base/head refs и SHAs, same-repository head и draft state. Duplicate,
cross-repository, wrong-head или provider-unknown result блокируют publication.

Финальный live gate этого capability должен включать фактический human CLI
вызов и agent CLI с `--json`; JSON выводится одним закрытым result envelope.
