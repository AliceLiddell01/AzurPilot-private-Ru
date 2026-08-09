# Runtime Russianization Release Snapshot

Дата проверки: 2026-08-09.

Этот документ фиксирует завершение текущего цикла runtime-русификации персональной Global/EN-версии. Это historical release snapshot, а не CI baseline: permanent проверки не используют указанные здесь SHA, номера PR или временные метрики.

## Snapshot

- Entry `personal/stable`: `4da251770c2abdfd3d9dc6af33185be3263add24`.
- Translation closure stable: `9aeba4e8e21a0a8f2ea50e90016f8699dae7035e`.
- Final release candidate source head: `67978f93177d4521faeeb3b7d302d17e84685022` (PR `#69`). Resulting stable SHA создаётся squash merge этого head и фиксируется в GitHub merge metadata и Wiki: включить собственный будущий merge SHA в тот же commit технически невозможно.
- Runtime scope: Windows 11, PowerShell 7.6, MuMu Player Global 15, Android 15, Global/EN Azur Lane.
- Concurrent open PRs not included in release snapshot: `#62 feature/game-settings-navigation`.
- GitHub tag/release и binaries не создавались: release означает validated `personal/stable` snapshot и этот отчёт.

## Residual audit

Repository-wide consumer-aware audit проверил entry points, `module/`, `campaign/`, WebUI, scheduler, device, OCR, common tasks, Campaign/Map/Combat/Settlement, Operation Siren, notifications и display builders.

- deterministic consumer sites: 4863;
- actionable `TRANSLATE`: 0;
- compliant Russian/mixed context: 4804;
- reviewed machine values: 35;
- reviewed technical values: 21;
- reviewed game metadata values: 3;
- `DEFERRED_EXCEPTION_TEXT`: 494 exception literals;
- functional ambiguity translated as prose: 0.

Сохранены без перевода raw stdout/stderr, HTTP/ADB/OCR payload, exception text/types, paths, URLs, package/API/state identifiers, recognition values, EN event metadata и игровые machine tokens. First-party контекст вокруг raw data остаётся русским.

Final translation closure выполнен PR `#68`; production diff менял только 39 Python-файлов с operator prose, а три теста обновили точные ожидаемые сообщения. Structural и functional изменений: 0.

## Permanent integrity

`dev_tools/runtime_russianization_audit.py` и `tests/test_runtime_russianization_audit.py` обеспечивают:

- CJK runtime regression protection в доказанных display sinks;
- ordinary operator English protection в deterministic sink contract;
- semantic allowances только для exact technical/machine/game категорий;
- `ru-RU` как единственную runtime UI locale;
- server `en` и package `com.YoStarEN.AzurLane`;
- canonical `assets/en` без CN/JP/TW roots;
- EN metadata без foreign fallback;
- OCR namespace `azur_lane` без foreign aliases.

Self-tests специально доказывают FAIL для CJK, обычного English sentence и foreign locale/server/package/assets/OCR, а также PASS для русского контекста, ADB/OCR/API, URL/path/package/game identifiers, deferred exception text и feature structure вне display sink.

## Windows/MuMu/Global acceptance

Acceptance выполнялась на изолированном clean checkout translation-closure SHA. Локальные config, logs, screenshots, device identifiers, account data и абсолютные пользовательские пути не коммитились.

| Flow | Status | Evidence | Limitations |
|---|---|---|---|
| Update-AzurPilot | PASS | fast-forward policy, installed SHA already current | Изолированный checkout |
| Build/Start-AzurPilot | PASS | pinned toolchain, prepared `.venv`, WebUI readiness `200 OK` | Start штатно остаётся foreground до остановки backend |
| WebUI `ru-RU` | PASS | login, Home/navigation/settings DOM на русском до и после restart | Один desktop viewport |
| Global identity | PASS | server `en`, Global package, `assets/en`, foreign fallback guard | Один поддерживаемый package |
| ADB | PASS | target-explicit transport, boot complete, package readiness, reconnect | Один MuMu target |
| Screenshot/BGR | PASS | ADB PNG 1280×720 `uint8` BGR; два последовательных `nemu_ipc` кадра | scrcpy дал handshake без видеоблока; fallback проверен |
| Safe control | PASS | minitouch handshake и один target-explicit `KEYCODE_BACK` | Touch по игровым координатам не отправлялся |
| Compact OCR | PASS | bundled `sets_num` 1000/1000; live screenshot дал пять safe numeric values; DML+CPU providers | Значения сверялись только на безопасном статическом экране |
| General English OCR | PASS | live screen: 27 detections и 12 ожидаемых safe UI labels | Chat/profile/UID не публиковались |
| Scheduler | PASS | read-only queue resolution и next-task selection на реальном profile | Task queue не запускалась |
| Common task | SKIP_UNSAFE_RUNTIME_ACTION | dispatcher/module construction и product tests | Выполнение могло изменить ресурсы аккаунта |
| Campaign/Map | PASS | state-driven `page_main_white → page_campaign_menu → page_os`, новый screenshot после каждого action | Stage checkout не выполнялся |
| Combat/Settlement | SKIP_UNSAFE_RUNTIME_ACTION | production contracts и full regression suite | Бой и reward settlement намеренно не запускались |
| Operation Siren port/local map | PASS | state-driven вход на локальную карту и визуальный PORT state | Mission checkout запрещён runner contract |
| Operation Siren zone OCR | PASS | три последовательных General-English samples стабильно распознали и сопоставили `Liverpool` | Одна безопасная текущая зона |
| Operation Siren routine | SKIP_UNSAFE_RUNTIME_ACTION | dispatcher/product tests | Auto-search, `os_init`, zone init и расход AP не запускались |
| Raw external handling | PASS | first-party Russian context + exact external exception marker | Один synthetic marker без публикации real payload |
| Repair `-DiagnosticOnly` | PASS | окружение исправно, `.venv` не изменялась | Локальный shortcut диагностирован отдельно от runtime |
| Restart/config/orphans | PASS | WebUI повторно готов; exact config hashes unchanged; после cleanup 0 AzurPilot processes/listeners | Emulator оставлен запущенным как внешний target |
| Privacy | PASS | acceptance artifacts были временными и удалены; отчёт не содержит serial/account/path | Raw local logs не публикуются |

`tools.acceptance.ocr` подтвердил compact fixture accuracy 100%, но его report adapter завершился на несовпадении текущего benchmark schema (`model_paths` против устаревшего `model_path`) до live-value stage. Реальные compact/general модели затем проверены in-memory на том же exact head; дефект acceptance adapter не затрагивает runtime OCR и оставлен как отдельный functional tooling finding.

## CI and independent review

Translation PR exact head прошёл required contexts `Python`, `Windows`, `Security`; GitHub structural step `Verify translation-only structure from PR base` был `EXECUTED/SUCCESS`. CodeRabbit required status был PASS, blocking findings и unresolved threads отсутствовали; повторный content review был ограничен внешней квотой.

Финальный integrity/docs PR `#69` на exact head `67978f93177d4521faeeb3b7d302d17e84685022` прошёл `Python`, `Windows`, `Security` и Declared Scope. CodeRabbit завершил review; все actionable threads должны быть разрешены перед merge. Resulting squash merge проверяется post-merge CI до объявления snapshot завершённым.

## Documentation and future changes

Обновлены README, `docs/ci.md`, `.codex/context` и Wiki форка. Translation structural gate остаётся специальной base→head защитой веток `codex/translate-*`; feature/bugfix/refactor PR используют свой Declared Scope и обычные product tests, включая permanent localization integrity.

Upstream transfer в этот snapshot не входит. Будущий перенос upstream сначала анализирует Global/EN delta, отдельно переводит новый first-party runtime и не смешивает functional adaptation с translation-only diff.

## Deferred functional findings

- `campaign/event_20251023_cn/campaign_base.py`: diagnostic text ссылается на `event_20241024`; не исправлялось в localization cycle.
- `tools/acceptance/ocr.py`: report adapter ожидает singular `model_path`, тогда как текущий benchmark возвращает `model_paths`; runtime OCR проверен отдельно, functional fix не смешивался с release/docs scope.

## Scope boundaries

- No hidden functional fix or refactor.
- No dependency change.
- No upstream sync/transfer.
- `master` untouched.
- Phase 6 в roadmap отсутствует.
