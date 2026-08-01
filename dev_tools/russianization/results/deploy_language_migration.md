# План миграции `Language` в пользовательском `config/deploy.yaml`

## Цель

На Stage 5 безопасно преобразовать старое значение `Language` в `ru-RU`, не перезаписывая неизвестные пользовательские ключи и не связывая UI locale с game server.

## Подтверждённая поверхность

Dependency map содержит 225 evidence entries по цепочке locale/server/OCR/package/assets. Конкретные файлы и строки находятся в `locale_dependency_map.json`.

## Контракт миграции

1. Прочитать существующий YAML без создания файла при обычном read path.
2. Если файл отсутствует, шаблон Stage 5 создаёт новый config с `Language: ru-RU`.
3. Если файл существует, изменить только скаляр `Language` patch-only механизмом, уже применяемым персональной веткой.
4. Сохранить порядок, неизвестные ключи, комментарии и все значения, не относящиеся к `Language`, насколько это обеспечивает существующий writer.
5. Не менять game server, package name, OCR model/profile и event-name source.
6. Значения `en-US`, `zh-CN`, `ja-JP`, `zh-TW`, `zh-MIAO`, пустое и неизвестное locale мигрируют в `ru-RU` с понятным first-party сообщением.
7. Повторный запуск при `Language: ru-RU` является no-op.
8. При ошибке парсинга не переписывать файл; вернуть русскую диагностическую ошибку и исходную exception detail.

## Обязательные regression fixtures

- каждый legacy locale;
- неизвестное locale;
- отсутствующий `Language`;
- дублирующийся/невалидный YAML;
- неизвестные nested keys;
- комментарии и нестандартный порядок;
- EN/Global server + foreign UI locale;
- повторный запуск.

## Запреты

Миграция не должна выполнять full dump поверх пользовательского файла, silent fallback на английский, смену server/OCR/package options или удаление неизвестных полей.
