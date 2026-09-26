# Git и Pull Request

Обычная разработка AzurPilotRu строится вокруг feature-ветки и Pull Request в `personal/stable`.

## Не работайте прямо в personal/stable

`personal/stable` — стабильная пользовательская ветка и источник автоматического обновления.

Изменения должны попадать туда через PR и required checks.

## master

`master` — чистое зеркало `wess09/AzurPilot:master`.

Fork-only изменение в `master` считается нарушением модели веток.

Upstream sync выполняется отдельно и не смешивается с обычной feature-разработкой.

## Рабочая ветка

Новая работа начинается от актуального `personal/stable`.

Имя ветки должно описывать capability/область, а не номер условного этапа разработки.

Пример:

```text
docs/full-project-documentation
runtime/emulator-recovery
webui/event-shop-safety
```

## Commits

Commit должен быть логически цельным.

Не нужно создавать отдельный commit после каждой мелкой правки только ради количества commits.

Сообщение должно объяснять смысл изменения; в персональном workflow используется понятный русскоязычный текст, например:

```text
docs(gitbook): описать обслуживание и диагностику
```

## Draft PR

Большая работа обычно публикуется как draft PR.

В PR должно быть понятно:

- цель;
- scope и границы;
- что реализовано;
- какие проверки фактически выполнены;
- состояние CI;
- security/secret checks;
- review disposition;
- migration/rollback;
- известные ограничения.

PR body — не формальность, а отчёт для человека, который будет принимать решение о merge.

## Required CI

Для `personal/stable` требуются:

- `Python`;
- `Windows`;
- `Security`.

Оценивайте checks на **актуальном head PR**, а не на старом commit до последних fixes.

## Review

CodeRabbit используется как дополнительный advisory review, а не как замена CI или человеческого review.

Подтверждённые findings исправляются в основной рабочей ветке и повторно проверяются relevant checks.

## Merge

Успешные тесты сами по себе не означают автоматическое разрешение merge.

Перед merge должны быть:

- актуальный head;
- required CI;
- завершённые review threads;
- отсутствие нерешённых блокирующих findings;
- финальный человеческий review;
- отдельное решение выполнить merge.

Для обычных feature PR предпочтительный итоговый способ может быть squash, если это соответствует конкретному PR и истории изменения.
