# Глоссарий

| Термин | Значение |
|---|---|
| ALAS | Историческое имя/база AzurLaneAutoScript; встречается в символах и путях |
| AzurPilot | Текущий продуктовый проект и персональный форк |
| instance | Именованный пользовательский config и связанный runtime context |
| task | Верхнеуровневая команда планировщика |
| group | Группа параметров внутри task |
| argument | Конкретный параметр конфигурации |
| Button | Объект распознавания UI с областями поиска/клика |
| Template | Шаблонное изображение для matching |
| Page | Узел графа игровых экранов |
| state loop | screenshot → распознавание → одно действие → новый screenshot |
| handler | Общий обработчик повторяющегося состояния/диалога |
| server | Product runtime сейчас только Global/EN (`en`); CN/JP/TW могут встречаться лишь как inherited upstream compatibility code |
| generated file | Файл, создаваемый generator из source YAML/schema/metadata |
| Operation Siren | Отдельный большой игровой режим («большой мир») |
| `azur` | Repository-owned Python CLI из `azurpilot.cli` |
| tooling | Typed service/CLI infrastructure в `azurpilot.tooling`, не gameplay layer |
| integration | Внешняя developer capability через `azurpilot.integrations` |
| integration adapter | Typed boundary конкретного внешнего provider без generic gateway fallback |
| `READY_FOR_CHATGPT_REVIEW` | Draft PR прошёл доступные pre-merge gates и остановлен для финального пользовательского review |
| `personal/stable` | Стабильная пользовательская ветка форка |
| `master` | Чистое зеркало upstream master |
| upstream | Исходный `wess09/AzurPilot` |
| takeover | Остановка автоматизации с требованием ручного вмешательства |
| recoverable | Ошибка, после которой верхний уровень может безопасно восстановить выполнение |
| smoke test | Короткая проверка сквозного базового сценария |
| secret scan | Проверка на случайно добавленные tokens/credentials/personal data |
