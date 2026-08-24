# Правила создания PowerShell-скриптов с командами Git

Скрипты должны быть совместимы с PowerShell 7.6.x и запускаться через `pwsh`.

Документ содержит обязательные инварианты. Не раздувать реализацию дополнительными обёртками, логированием и проверками, если они не нужны фактическому контракту скрипта.

## 1. Базовый режим

В начале скрипта:

```powershell
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
```

Git и другие native-программы оцениваются по `$LASTEXITCODE`; ненулевой код не превращать автоматически в PowerShell exception без понимания семантики конкретной команды.

Обязательные правила:

- одна логическая операция на физическую строку;
- не использовать `;` для цепочки выполняемых команд;
- не использовать backtick для переноса команды;
- не использовать PowerShell aliases в production-скриптах;
- не использовать `Invoke-Expression`;
- не запускать Git через `cmd.exe`, `bash`, вложенный `powershell.exe`/`pwsh` или `Start-Process` без отдельной технической причины;
- для многострочных native-вызовов использовать массив аргументов/splatting.

## 2. Вызов Git и `$LASTEXITCODE`

Получить executable один раз:

```powershell
$gitCommand = Get-Command git -CommandType Application -ErrorAction Stop
$gitExecutable = $gitCommand.Path
```

Аргументы передавать отдельными элементами:

```powershell
$gitArguments = @(
    '-C'
    $RepositoryPath
    'status'
    '--short'
)

& $gitExecutable @gitArguments
$gitExitCode = $LASTEXITCODE
```

После **каждого** вызова Git сразу сохранить `$LASTEXITCODE`. Между native-вызовом и сохранением кода не должно быть логирования, pipeline, другого вызова или промежуточного вычисления.

Не использовать stdout как boolean успешности:

```powershell
# Запрещено
if (& $gitExecutable @gitArguments) {
}
```

## 3. Допустимые коды возврата

Для каждой Git-команды знать допустимые коды. Код `1` не всегда ошибка.

Например, для `git diff --quiet`:

```powershell
& $gitExecutable -C $RepositoryPath diff --quiet
$gitExitCode = $LASTEXITCODE

switch ($gitExitCode) {
    0 { $hasChanges = $false }
    1 { $hasChanges = $true }
    default {
        throw ('Не удалось проверить изменения. Git завершился с кодом {0}.' -f $gitExitCode)
    }
}
```

Не игнорировать неизвестный код возврата.

## 4. Входные значения и Git refs

Перед передачей пользовательского/внешнего значения в Git проверить обязательные строки через `IsNullOrWhiteSpace`.

```powershell
if ([string]::IsNullOrWhiteSpace($TaskBranch)) {
    throw 'Имя рабочей ветки не задано.'
}
```

Имя новой ветки валидировать правилами Git:

```powershell
$branchValidationOutput = & $gitExecutable check-ref-format --branch $TaskBranch
$gitExitCode = $LASTEXITCODE
$validatedBranchName = [string]($branchValidationOutput | Select-Object -Last 1)

if ($gitExitCode -ne 0 -or $validatedBranchName -cne $TaskBranch) {
    throw ('Недопустимое имя ветки Git: {0}' -f $TaskBranch)
}
```

Если команде нужна полная ссылка, формировать её явно, например `refs/heads/<branch>`. Не смешивать branch name, ref и pathspec.

## 5. Путь и рабочее дерево

Для существующего repository path:

1. проверить непустое значение;
2. проверить каталог через `Test-Path -LiteralPath ... -PathType Container`;
3. убедиться, что provider — `FileSystem`;
4. получить строковый абсолютный путь через `Convert-Path -LiteralPath`;
5. подтвердить именно worktree через `git rev-parse --is-inside-work-tree` и проверить одновременно exit code и stdout `true`.

Для Git-команд предпочитать `git -C <path>` вместо `Set-Location`.

Если смена текущего каталога действительно нужна, использовать `Push-Location` и `Pop-Location` в `finally`.

## 6. Аргументы, пути и pathspec

- каждый Git-аргумент — отдельный элемент массива;
- PowerShell filesystem path — через `-LiteralPath`, когда параметр это поддерживает;
- перед Git pathspec использовать `--`, если конкретная команда поддерживает такой separator;
- не добавлять `--` механически в синтаксис, который его не допускает;
- не собирать командную строку Git вручную и не исполнять её динамически.

Пример:

```powershell
$gitArguments = @(
    '-C'
    $RepositoryPath
    'add'
    '--'
    $FilePath
)
```

## 7. stdout/stderr

- предпочитать `--porcelain`, `--format` и другие machine-readable режимы;
- не анализировать визуально оформленный вывод, если существует стабильный формат;
- stderr не подавлять без сохранения достаточной диагностики;
- при `2>&1` учитывать массив строк;
- захватить output, сразу сохранить `$LASTEXITCODE`, и только затем разбирать output.

## 8. Интерполяция перед `:`

В double-quoted строке обычную переменную непосредственно перед `:` не использовать:

```powershell
# Запрещено
throw "Ошибка для ветки $TaskBranch: Git завершился неудачно."

# Допустимо
throw "Ошибка для ветки ${TaskBranch}: Git завершился неудачно."

# Предпочтительно для сообщений
throw ('Ошибка для ветки {0}: Git завершился неудачно.' -f $TaskBranch)
```

Не изменять корректные scope/drive forms: `$global:`, `$script:`, `$local:`, `$private:`, `$using:`, `$env:`.

## 9. Исключения и cleanup

Пустые `catch` запрещены. Не использовать `Write-Error` непосредственно перед `throw` при `$ErrorActionPreference = 'Stop'`.

Если контекст не нужен:

```powershell
catch {
    throw
}
```

Если нужен контекст, сохранить исходное исключение как `InnerException`.

Временные файлы, каталоги, passfile/askpass и изменённое process-local состояние очищать в `finally`, если cleanup безопасен и принадлежит текущей операции.

## 10. Идемпотентность

Когда повторный запуск является контрактом скрипта, он не должен падать только потому, что безопасный шаг уже выполнен. Перед изменяющей операцией проверять текущее состояние там, где это необходимо для корректности.

Не превращать это правило в обязательный preflight каждой возможной сущности: проверять только состояние, влияющее на следующую изменяющую операцию.

## 11. Опасные Git-команды

В пользовательском checkout и опубликованных ветках запрещены:

```text
git push --force
git push --force-with-lease
git reset --hard
git clean -fd
git clean -fdx
git checkout -f
git branch -D
git reflog expire
git gc --prune=now
```

Исключения для disposable Codex worktree/clone регулируются `GIT-WORKFLOW.md`; предпочтительно удалить одноразовый worktree целиком вместо destructive cleanup внутри пользовательского checkout.

## 12. Логирование

Логировать существенные изменяющие операции и ошибки, но не каждую тривиальную команду.

Никогда не выводить токены, пароли, приватные ключи, credentials URL и секретные environment values.

При ошибке достаточно сохранить полезную безопасную диагностику: операция, exit code, repository path/ref и релевантный stderr/stdout.

## 13. Проверка изменённого PowerShell

Для затронутых `.ps1`/`.psm1` выполнить релевантные project gates:

1. PowerShell Parser через фактический `pwsh`;
2. PSScriptAnalyzer зафиксированной проектом версии;
3. targeted smoke для изменённой Git-логики в disposable repository;
4. Windows integration smoke только если изменение затрагивает соответствующий эксплуатационный поток;
5. полный набор PowerShell gates — когда этого требует `docs/ci.md` или изменение имеет широкий scope.

После последнего **существенного** изменения повторить затронутые checks. Не запускать полный PowerShell suite после чисто документальной/механической правки, если executable diff не изменился.

Если `pwsh`/обязательный gate недоступен в момент проверки, разрешён предварительный статический аудит, но commit/merge PowerShell-изменения остаётся blocked согласно `GIT-WORKFLOW.md`.

## 14. Статический аудит при недоступном runtime

Статический аудит не выдавать за фактический запуск. Минимально проверить:

- запрещённые `;`, backtick и `Invoke-Expression`;
- аргументы Git и немедленное сохранение `$LASTEXITCODE`;
- допустимые exit codes;
- обязательные входы и refs;
- `$Variable:` в double-quoted строках;
- `catch`/`finally` и сохранение исходной ошибки;
- destructive Git operations.

## 15. Итог Codex

При работе непосредственно в репозитории **не дублировать полный изменённый `.ps1`/`.psm1` в финальном ответе**. Достаточно кратко указать:

- какие файлы изменены;
- какие инварианты исправлены;
- какие parser/analyzer/smoke checks реально выполнены;
- что осталось blocked, если обязательный gate недоступен.

Полный файл выводить в чат только когда пользователь прямо просит текст файла, а не изменение репозитория.
