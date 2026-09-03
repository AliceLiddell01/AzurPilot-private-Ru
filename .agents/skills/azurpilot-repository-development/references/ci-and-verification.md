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
