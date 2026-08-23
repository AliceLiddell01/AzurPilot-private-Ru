#requires -Version 7.6

[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryPath = 'C:\AzurPilot',

    [Parameter()]
    [string]$UvExecutablePath = '',

    [Parameter()]
    [string]$BootstrapPythonPath = '',

    [Parameter()]
    [string]$RepairWorkRoot = '',

    [Parameter()]
    [ValidateRange(1, 10)]
    [int]$BackupRetentionCount = 2,

    [Parameter()]
    [ValidateRange(60, 7200)]
    [int]$SyncTimeoutSeconds = 1800,

    [Parameter()]
    [switch]$DiagnosticOnly,

    [Parameter()]
    [string]$ShortcutPath = '',

    [Parameter()]
    [string]$IconPath = '',

    [Parameter()]
    [string]$ShortcutBackupRoot = '',

    [Parameter()]
    [switch]$RepairShortcut,

    [Parameter()]
    [switch]$ShortcutOnly,

    [Parameter()]
    [ValidateSet(
        'None',
        'AfterBackup',
        'AfterRebuild',
        'BeforeValidation'
    )]
    [string]$TestFailPoint = 'None'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$script:RepositoryPathParameter = $RepositoryPath
$script:UvExecutablePathParameter = $UvExecutablePath
$script:BootstrapPythonPathParameter = $BootstrapPythonPath
$script:RepairWorkRootParameter = $RepairWorkRoot
$script:BackupRetentionCountParameter = $BackupRetentionCount
$script:SyncTimeoutSecondsParameter = $SyncTimeoutSeconds
$script:DiagnosticOnlyParameter = $DiagnosticOnly
$script:ShortcutPathParameter = $ShortcutPath
$script:IconPathParameter = $IconPath
$script:ShortcutBackupRootParameter = $ShortcutBackupRoot
$script:RepairShortcutParameter = $RepairShortcut
$script:ShortcutOnlyParameter = $ShortcutOnly
$script:TestFailPointParameter = $TestFailPoint

$script:ExitCodeSuccess = 0
$script:ExitCodePreconditionFailure = 20
$script:ExitCodeActiveProcess = 21
$script:ExitCodeTransactionConflict = 22
$script:ExitCodeBootstrapUnavailable = 23
$script:ExitCodeRepairFailedRollbackSucceeded = 24
$script:ExitCodeRollbackFailed = 25
$script:ExitCodeDiagnosticFailure = 26
$script:ExitCodeShortcutFailure = 27
$script:ExitCodeElevationRequired = 28
$script:ExitCodeUnexpectedFailure = 30

$script:LogPath = $null
$script:ResolvedRepositoryPath = $null
$script:ResolvedRepairWorkRoot = $null
$script:ResolvedUpdateWorkRoot = $null
$script:ResolvedShortcutPath = $null
$script:ResolvedIconPath = $null
$script:ResolvedShortcutBackupRoot = $null
$script:ShortcutModulePath = $null
$script:RepairMutex = $null
$script:RepairMutexOwned = $false

function Protect-SensitiveText {
    [CmdletBinding()]
    param(
        [Parameter()]
        [AllowNull()]
        [string]$Text
    )

    if ($null -eq $Text) {
        return ''
    }

    $safeText = $Text
    $safeText = $safeText -replace '(?i)(https?://)([^/\s:@]+):([^/\s@]+)@', '$1<REDACTED>@'
    $safeText = $safeText -replace '(?i)([?&](?:access[_-]?token|api[_-]?key|token|auth|password|passwd|secret)=)[^&\s]+', '$1<REDACTED>'
    $safeText = $safeText -replace '(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+', '$1=<REDACTED>'

    return $safeText
}

function Write-ConsoleMessage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Message
    )

    Write-Information -MessageData $Message -InformationAction Continue
}

function Initialize-RepairLog {
    [CmdletBinding()]
    param()

    $baseDirectory = $env:LOCALAPPDATA

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        $baseDirectory = $env:TEMP
    }

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        throw 'Не удалось определить каталог для лога Repair.'
    }

    $logDirectory = Join-Path -Path $baseDirectory -ChildPath 'AzurPilot\logs'
    New-Item -ItemType Directory -Path $logDirectory -Force -ErrorAction Stop | Out-Null

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $fileName = 'Repair-AzurPilot-{0}-{1}.log' -f $timestamp, $PID
    $script:LogPath = Join-Path -Path $logDirectory -ChildPath $fileName

    New-Item -ItemType File -Path $script:LogPath -Force -ErrorAction Stop | Out-Null
}

function Write-RepairLog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateSet(
            'INFO',
            'WARN',
            'ERROR'
        )]
        [string]$Level,

        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Message
    )

    $safeMessage = Protect-SensitiveText -Text $Message
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = '[{0}] [{1}] {2}' -f $timestamp, $Level, $safeMessage

    Write-ConsoleMessage -Message $line

    if ($null -ne $script:LogPath) {
        Add-Content -LiteralPath $script:LogPath -Value $line -Encoding utf8 -ErrorAction Stop
    }
}

function Get-RepairException {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [int]$Code,

        [Parameter(Mandatory)]
        [string]$Message,

        [Parameter()]
        [AllowNull()]
        [System.Exception]$InnerException
    )

    $exception = if ($null -eq $InnerException) {
        [System.InvalidOperationException]::new($Message)
    } else {
        [System.InvalidOperationException]::new(
            $Message,
            $InnerException
        )
    }

    $exception.Data['ExitCode'] = $Code

    return $exception
}

function Complete-RepairFailure {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [int]$Code,

        [Parameter(Mandatory)]
        [string]$Message,

        [Parameter()]
        [AllowNull()]
        [System.Exception]$InnerException
    )

    throw (Get-RepairException -Code $Code -Message $Message -InnerException $InnerException)
}

function Resolve-FileSystemDirectory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$DisplayName,

        [Parameter()]
        [switch]$Create
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        Complete-RepairFailure -Code $script:ExitCodePreconditionFailure -Message ('Путь не задан: {0}' -f $DisplayName)
    }

    if ($Create -and -not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force -ErrorAction Stop | Out-Null
    }

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        Complete-RepairFailure -Code $script:ExitCodePreconditionFailure -Message ('Каталог не существует: {0}' -f $Path)
    }

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop

    if ($item.PSProvider.Name -ne 'FileSystem') {
        Complete-RepairFailure -Code $script:ExitCodePreconditionFailure -Message ('Путь не относится к файловой системе: {0}' -f $Path)
    }

    return Convert-Path -LiteralPath $Path -ErrorAction Stop
}

function Resolve-RequiredFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$DisplayName
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Complete-RepairFailure -Code $script:ExitCodePreconditionFailure -Message ('Отсутствует обязательный файл «{0}»: {1}' -f $DisplayName, $Path)
    }

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop

    if ($item.PSProvider.Name -ne 'FileSystem') {
        Complete-RepairFailure -Code $script:ExitCodePreconditionFailure -Message ('Файл не относится к файловой системе: {0}' -f $Path)
    }

    return Convert-Path -LiteralPath $Path -ErrorAction Stop
}

function Test-PathInside {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$CandidatePath,

        [Parameter(Mandatory)]
        [string]$ParentPath
    )

    $normalizedCandidate = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetFullPath($CandidatePath)
    )
    $normalizedParent = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetFullPath($ParentPath)
    )
    $parentPrefix = $normalizedParent + [System.IO.Path]::DirectorySeparatorChar

    return (
        [string]::Equals(
            $normalizedCandidate,
            $normalizedParent,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or
        $normalizedCandidate.StartsWith(
            $parentPrefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    )
}

function Get-PathHash {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    $normalizedPath = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetFullPath($Path)
    ).ToUpperInvariant()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalizedPath)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()

    try {
        $hashBytes = $sha256.ComputeHash($bytes)
    }
    finally {
        $sha256.Dispose()
    }

    return [Convert]::ToHexString($hashBytes).ToLowerInvariant()
}

function Enter-RepairMutex {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ResolvedRepositoryPath
    )

    $pathHash = Get-PathHash -Path $ResolvedRepositoryPath
    $mutexName = 'Local\AzurPilot.Repair.{0}' -f $pathHash
    $mutex = [System.Threading.Mutex]::new($false, $mutexName)
    $owned = $false

    try {
        $owned = $mutex.WaitOne(0, $false)
    }
    catch [System.Threading.AbandonedMutexException] {
        $owned = $true
        Write-RepairLog -Level 'WARN' -Message 'Обнаружен заброшенный мьютекс Repair. Владение восстановлено.'
    }
    catch {
        $mutex.Dispose()
        $message = 'Не удалось открыть мьютекс Repair: {0}' -f $_.Exception.Message
        Complete-RepairFailure -Code $script:ExitCodeUnexpectedFailure -Message $message -InnerException $_.Exception
    }

    if (-not $owned) {
        $mutex.Dispose()
        Complete-RepairFailure -Code $script:ExitCodeTransactionConflict -Message 'Другой экземпляр Repair уже работает с этой установкой.'
    }

    return [pscustomobject]@{
        Name = $mutexName
        Mutex = $mutex
        Owned = $owned
    }
}

function Invoke-NativeCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Executable,

        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [string[]]$Arguments,

        [Parameter()]
        [string]$WorkingDirectory = ''
    )

    $locationChanged = $false

    if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        Push-Location -LiteralPath $WorkingDirectory
        $locationChanged = $true
    }

    try {
        $nativeOutput = & $Executable @Arguments 2>&1
        $nativeExitCode = $LASTEXITCODE
        $nativeOutput = @($nativeOutput)

        return [pscustomobject]@{
            ExitCode = $nativeExitCode
            Output = [string[]]$nativeOutput
        }
    }
    finally {
        if ($locationChanged) {
            Pop-Location
        }
    }
}

function Write-NativeOutput {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Result,

        [Parameter()]
        [ValidateSet(
            'INFO',
            'WARN',
            'ERROR'
        )]
        [string]$Level = 'INFO',

        [Parameter()]
        [string]$Prefix = ''
    )

    foreach ($line in $Result.Output) {
        $text = [string]$line

        if ([string]::IsNullOrWhiteSpace($text)) {
            continue
        }

        $message = if ([string]::IsNullOrWhiteSpace($Prefix)) {
            $text
        } else {
            '{0} {1}' -f $Prefix, $text
        }

        Write-RepairLog -Level $Level -Message $message
    }
}

function Test-Executable {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Executable,

        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        return $false
    }

    try {
        $result = Invoke-NativeCommand -Executable $Executable -Arguments $Arguments
    }
    catch {
        return $false
    }

    return $result.ExitCode -eq 0
}

function Resolve-ExecutableCandidate {
    [CmdletBinding()]
    param(
        [Parameter()]
        [string]$ConfiguredPath,

        [Parameter()]
        [string]$DefaultPath,

        [Parameter(Mandatory)]
        [string]$CommandName,

        [Parameter(Mandatory)]
        [string[]]$TestArguments
    )

    $candidates = [System.Collections.Generic.List[string]]::new()

    foreach ($candidate in @(
        $ConfiguredPath
        $DefaultPath
    )) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }

        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $resolvedCandidate = Convert-Path -LiteralPath $candidate -ErrorAction Stop

            if (-not $candidates.Contains($resolvedCandidate)) {
                $candidates.Add($resolvedCandidate)
            }
        }
    }

    $commands = @(
        Get-Command -Name $CommandName -CommandType Application -ErrorAction SilentlyContinue
    )

    foreach ($command in $commands) {
        $commandPath = [string]$command.Path

        if (
            -not [string]::IsNullOrWhiteSpace($commandPath) -and
            -not $candidates.Contains($commandPath)
        ) {
            $candidates.Add($commandPath)
        }
    }

    foreach ($candidate in $candidates) {
        if (Test-Executable -Executable $candidate -Arguments $TestArguments) {
            return $candidate
        }
    }

    return $null
}

function Test-PythonHealth {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$PythonPath,

        [Parameter()]
        [string[]]$Imports = @()
    )

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        return [pscustomobject]@{
            Success = $false
            Details = 'python.exe отсутствует.'
        }
    }

    $importText = if ($Imports.Count -eq 0) {
        ''
    } else {
        $Imports -join [Environment]::NewLine
    }

    $pythonCode = @'
import sys
IMPORT_BLOCK
print(sys.version)
'@

    $pythonCode = $pythonCode.Replace(
        'IMPORT_BLOCK',
        $importText
    )

    try {
        $result = Invoke-NativeCommand -Executable $PythonPath -Arguments @(
            '-c'
            $pythonCode
        ) -WorkingDirectory $script:ResolvedRepositoryPath
    }
    catch {
        return [pscustomobject]@{
            Success = $false
            Details = $_.Exception.Message
        }
    }

    $details = ($result.Output -join [Environment]::NewLine).Trim()

    return [pscustomobject]@{
        Success = $result.ExitCode -eq 0
        Details = $details
    }
}

function Assert-AzurPilotStopped {
    [CmdletBinding()]
    param()

    if (-not $IsWindows) {
        Complete-RepairFailure -Code $script:ExitCodePreconditionFailure -Message 'Repair-AzurPilot.ps1 поддерживает только Windows.'
    }

    $repositoryRegex = [regex]::Escape($script:ResolvedRepositoryPath)
    $launcherPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'alas-launcher.exe'

    try {
        $processes = @(
            Get-CimInstance -ClassName Win32_Process -ErrorAction Stop
        )
    }
    catch {
        $message = 'Не удалось проверить процессы AzurPilot: {0}' -f $_.Exception.Message
        Complete-RepairFailure -Code $script:ExitCodePreconditionFailure -Message $message -InnerException $_.Exception
    }

    $matchingProcesses = @(
        $processes |
            Where-Object -FilterScript {
                $commandLine = [string]$_.CommandLine
                $executablePath = [string]$_.ExecutablePath

                $isLauncher = (
                    -not [string]::IsNullOrWhiteSpace($executablePath) -and
                    [string]::Equals(
                        $executablePath,
                        $launcherPath,
                        [System.StringComparison]::OrdinalIgnoreCase
                    )
                )

                $isRepositoryProcess = (
                    -not [string]::IsNullOrWhiteSpace($commandLine) -and
                    $commandLine -match $repositoryRegex -and
                    $commandLine -match '(?i)\b(gui|alas)\.py\b'
                )

                $isLauncher -or $isRepositoryProcess
            }
    )

    if ($matchingProcesses.Count -eq 0) {
        return
    }

    $processText = $matchingProcesses |
        Select-Object -Property ProcessId, Name, ExecutablePath, CommandLine |
        Format-List |
        Out-String

    $message = @(
        'AzurPilot сейчас запущен.'
        'Завершите его штатно и повторите Repair.'
        $processText.Trim()
    ) -join [Environment]::NewLine

    Complete-RepairFailure -Code $script:ExitCodeActiveProcess -Message $message
}

function Initialize-RepairPath {
    [CmdletBinding()]
    param()

    $script:ResolvedRepositoryPath = Resolve-FileSystemDirectory -Path $RepositoryPath -DisplayName 'репозиторий AzurPilot'

    $baseDirectory = $env:LOCALAPPDATA

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        $baseDirectory = $env:TEMP
    }

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        Complete-RepairFailure -Code $script:ExitCodePreconditionFailure -Message 'Не удалось определить локальный каталог транзакций.'
    }

    $requestedRepairRoot = if ([string]::IsNullOrWhiteSpace($RepairWorkRoot)) {
        Join-Path -Path $baseDirectory -ChildPath 'AzurPilot\repair-transactions'
    } else {
        [System.IO.Path]::GetFullPath($RepairWorkRoot)
    }

    if (Test-PathInside -CandidatePath $requestedRepairRoot -ParentPath $script:ResolvedRepositoryPath) {
        Complete-RepairFailure -Code $script:ExitCodePreconditionFailure -Message 'Каталог транзакций Repair должен находиться вне репозитория.'
    }

    $script:ResolvedRepairWorkRoot = Resolve-FileSystemDirectory -Path $requestedRepairRoot -DisplayName 'каталог транзакций Repair' -Create

    $repositoryDrive = [System.IO.Path]::GetPathRoot($script:ResolvedRepositoryPath)
    $repairDrive = [System.IO.Path]::GetPathRoot($script:ResolvedRepairWorkRoot)

    if (
        -not [string]::Equals(
            $repositoryDrive,
            $repairDrive,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        Complete-RepairFailure -Code $script:ExitCodePreconditionFailure -Message 'Каталог транзакций Repair должен находиться на том же диске, что и .venv.'
    }

    $script:ResolvedUpdateWorkRoot = Join-Path -Path $baseDirectory -ChildPath 'AzurPilot\dependency-transactions'

    if ([string]::IsNullOrWhiteSpace($script:ShortcutPathParameter)) {
        if ([string]::IsNullOrWhiteSpace($env:ProgramData)) {
            $script:ResolvedShortcutPath = ''
        } else {
            $script:ResolvedShortcutPath = Join-Path -Path $env:ProgramData -ChildPath 'Microsoft\Windows\Start Menu\Programs\AzurPilot.lnk'
        }
    } else {
        $script:ResolvedShortcutPath = [System.IO.Path]::GetFullPath($script:ShortcutPathParameter)
    }

    $requestedIconPath = if ([string]::IsNullOrWhiteSpace($script:IconPathParameter)) {
        Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'assets\AzurPilot.ico'
    } else {
        [System.IO.Path]::GetFullPath($script:IconPathParameter)
    }
    $script:ResolvedIconPath = $requestedIconPath

    $requestedBackupRoot = if ([string]::IsNullOrWhiteSpace($script:ShortcutBackupRootParameter)) {
        Join-Path -Path $baseDirectory -ChildPath 'AzurPilot\backups\shortcuts'
    } else {
        [System.IO.Path]::GetFullPath($script:ShortcutBackupRootParameter)
    }
    $script:ResolvedShortcutBackupRoot = $requestedBackupRoot
    $script:ShortcutModulePath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'scripts\lib\AzurPilot.Shortcut.psm1'
}

function Assert-RequiredProjectFile {
    [CmdletBinding()]
    param()

    $requiredFiles = [ordered]@{
        'gui.py' = 'Python entrypoint'
        'deploy\uv.py' = 'uv orchestration'
        'pyproject.toml' = 'описание Python-проекта'
        'uv.lock' = 'dependency lockfile'
        'config\deploy.yaml' = 'deployment config'
    }

    foreach ($relativePath in $requiredFiles.Keys) {
        $fullPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath $relativePath
        [void](Resolve-RequiredFile -Path $fullPath -DisplayName $requiredFiles[$relativePath])
    }
}

function Assert-NoUpdateTransaction {
    [CmdletBinding()]
    param()

    if (-not (Test-Path -LiteralPath $script:ResolvedUpdateWorkRoot -PathType Container)) {
        return
    }

    $journalItems = @(
        Get-ChildItem -LiteralPath $script:ResolvedUpdateWorkRoot -Filter 'journal.json' -File -Recurse -Force -ErrorAction Stop
    )

    if ($journalItems.Count -eq 0) {
        return
    }

    $journalPaths = $journalItems.FullName -join [Environment]::NewLine
    $message = @(
        'Обнаружена незавершённая транзакция зависимостей этапа обновления.'
        'Repair не имеет права обходить восстановление скрипта обновления.'
        'Сначала запустите scripts\Update-AzurPilot.ps1 для штатного восстановления.'
        $journalPaths
    ) -join [Environment]::NewLine

    Complete-RepairFailure -Code $script:ExitCodeTransactionConflict -Message $message
}

function Get-DirectorySnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$LiteralPath
    )

    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Container)) {
        return [pscustomobject]@{
            Exists = $false
            FileCount = 0
            TotalBytes = [int64]0
            KeyHashes = [ordered]@{}
        }
    }

    $files = @(
        Get-ChildItem -LiteralPath $LiteralPath -File -Recurse -Force -ErrorAction Stop
    )

    $totalBytes = (
        $files |
            Measure-Object -Property Length -Sum
    ).Sum

    if ($null -eq $totalBytes) {
        $totalBytes = 0
    }

    $keyRelativePaths = @(
        'pyvenv.cfg'
        'Scripts\python.exe'
        'Scripts\uv.exe'
        'Scripts\adb.exe'
        'Scripts\git\cmd\git.exe'
    )
    $keyHashes = [ordered]@{}

    foreach ($relativePath in $keyRelativePaths) {
        $fullPath = Join-Path -Path $LiteralPath -ChildPath $relativePath

        if (Test-Path -LiteralPath $fullPath -PathType Leaf) {
            $keyHashes[$relativePath] = (
                Get-FileHash -LiteralPath $fullPath -Algorithm SHA256 -ErrorAction Stop
            ).Hash
        }
    }

    return [pscustomobject]@{
        Exists = $true
        FileCount = $files.Count
        TotalBytes = [int64]$totalBytes
        KeyHashes = $keyHashes
    }
}

function Get-ConfigSnapshot {
    [CmdletBinding()]
    param()

    $configPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'config'
    $hashes = [ordered]@{}

    if (-not (Test-Path -LiteralPath $configPath -PathType Container)) {
        return $hashes
    }

    $files = @(
        Get-ChildItem -LiteralPath $configPath -File -Recurse -Force -ErrorAction Stop |
            Sort-Object -Property FullName
    )

    foreach ($file in $files) {
        $relativePath = [System.IO.Path]::GetRelativePath(
            $configPath,
            $file.FullName
        )
        $hashes[$relativePath] = (
            Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256 -ErrorAction Stop
        ).Hash
    }

    return $hashes
}

function Assert-ConfigSnapshotEqual {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Expected,

        [Parameter(Mandatory)]
        [object]$Actual
    )

    $expectedJson = $Expected | ConvertTo-Json -Compress -Depth 8
    $actualJson = $Actual | ConvertTo-Json -Compress -Depth 8

    if ($expectedJson -ne $actualJson) {
        Complete-RepairFailure -Code $script:ExitCodeRollbackFailed -Message 'Контрольные суммы SHA-256 файлов конфигурации изменились во время Repair.'
    }
}

function Write-RepairJournal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction,

        [Parameter(Mandatory)]
        [ValidateSet(
            'Initialized',
            'BackupReady',
            'RebuildStarted',
            'RebuildCompleted',
            'Validated',
            'Completed',
            'RollbackStarted',
            'RolledBack'
        )]
        [string]$Phase,

        [Parameter()]
        [string]$LastError = ''
    )

    $Transaction.Phase = $Phase
    $Transaction.UpdatedAt = [DateTimeOffset]::Now.ToString('o')
    $Transaction.LastError = $LastError

    $journalJson = $Transaction |
        ConvertTo-Json -Depth 12

    Set-Content -LiteralPath $Transaction.JournalPath -Value $journalJson -Encoding utf8NoBOM -ErrorAction Stop
}

function Read-RepairJournal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$JournalPath
    )

    try {
        $content = Get-Content -LiteralPath $JournalPath -Raw -Encoding utf8 -ErrorAction Stop
        return $content | ConvertFrom-Json -DateKind String -ErrorAction Stop
    }
    catch {
        $message = 'Не удалось прочитать журнал Repair «{0}»: {1}' -f $JournalPath, $_.Exception.Message
        Complete-RepairFailure -Code $script:ExitCodeTransactionConflict -Message $message -InnerException $_.Exception
    }
}

function Get-IncompleteRepairTransaction {
    [CmdletBinding()]
    param()

    $journalItems = @(
        Get-ChildItem -LiteralPath $script:ResolvedRepairWorkRoot -Filter 'journal.json' -File -Recurse -Force -ErrorAction Stop
    )
    $incomplete = [System.Collections.Generic.List[object]]::new()

    foreach ($journalItem in $journalItems) {
        $journal = Read-RepairJournal -JournalPath $journalItem.FullName
        $phase = [string]$journal.Phase

        if ($phase -notin @(
            'Completed',
            'RolledBack'
        )) {
            $incomplete.Add($journal)
        }
    }

    return $incomplete.ToArray()
}

function Restore-OriginalVenv {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction
    )

    $venvPath = [string]$Transaction.VenvPath
    $backupPath = [string]$Transaction.BackupPath
    $originalVenvExisted = [bool]$Transaction.OriginalVenvExisted

    Write-RepairJournal -Transaction $Transaction -Phase 'RollbackStarted' -LastError ([string]$Transaction.LastError)

    $backupExists = Test-Path -LiteralPath $backupPath -PathType Container
    $currentVenvExists = Test-Path -LiteralPath $venvPath -PathType Container

    if ($originalVenvExisted -and -not $backupExists) {
        if (-not $currentVenvExists) {
            Complete-RepairFailure -Code $script:ExitCodeRollbackFailed -Message ('Исходная .venv и её резервная копия отсутствуют: {0}' -f $venvPath)
        }

        $currentSnapshot = Get-DirectorySnapshot -LiteralPath $venvPath
        $expectedSnapshotJson = $Transaction.OriginalVenvSnapshot | ConvertTo-Json -Compress -Depth 8
        $currentSnapshotJson = $currentSnapshot | ConvertTo-Json -Compress -Depth 8

        if ($expectedSnapshotJson -ne $currentSnapshotJson) {
            Complete-RepairFailure -Code $script:ExitCodeRollbackFailed -Message 'Резервная копия отсутствует, а текущая .venv уже отличается от исходного снимка.'
        }

        Write-RepairJournal -Transaction $Transaction -Phase 'RolledBack' -LastError ([string]$Transaction.LastError)
        Write-RepairLog -Level 'INFO' -Message 'Резервная копия не была создана, исходная .venv осталась неизменной.'
        return
    }

    if ($currentVenvExists) {
        Remove-Item -LiteralPath $venvPath -Recurse -Force -ErrorAction Stop
    }

    if ($originalVenvExisted) {
        Move-Item -LiteralPath $backupPath -Destination $venvPath -ErrorAction Stop

        if (-not (Test-Path -LiteralPath $venvPath -PathType Container)) {
            Complete-RepairFailure -Code $script:ExitCodeRollbackFailed -Message 'Не удалось вернуть исходную .venv на место.'
        }

        $restoredSnapshot = Get-DirectorySnapshot -LiteralPath $venvPath
        $expectedSnapshotJson = $Transaction.OriginalVenvSnapshot | ConvertTo-Json -Compress -Depth 8
        $restoredSnapshotJson = $restoredSnapshot | ConvertTo-Json -Compress -Depth 8

        if ($expectedSnapshotJson -ne $restoredSnapshotJson) {
            Complete-RepairFailure -Code $script:ExitCodeRollbackFailed -Message 'Восстановленная .venv не совпадает с исходным снимком.'
        }
    }

    Write-RepairJournal -Transaction $Transaction -Phase 'RolledBack' -LastError ([string]$Transaction.LastError)
    Write-RepairLog -Level 'INFO' -Message 'Исходное состояние .venv восстановлено.'
}

function Invoke-IncompleteRepairRecovery {
    [CmdletBinding()]
    param()

    $incompleteTransactions = @(Get-IncompleteRepairTransaction)

    if ($incompleteTransactions.Count -gt 1) {
        Complete-RepairFailure -Code $script:ExitCodeTransactionConflict -Message ('Обнаружено несколько незавершённых транзакций Repair: {0}' -f $incompleteTransactions.Count)
    }

    if ($incompleteTransactions.Count -eq 0) {
        return
    }

    $transaction = $incompleteTransactions[0]
    $transactionRepositoryPath = [string]$transaction.RepositoryPath

    if (
        -not [string]::Equals(
            $transactionRepositoryPath,
            $script:ResolvedRepositoryPath,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        Complete-RepairFailure -Code $script:ExitCodeTransactionConflict -Message 'Незавершённая транзакция Repair относится к другому пути репозитория.'
    }

    Write-RepairLog -Level 'WARN' -Message ('Обнаружена незавершённая транзакция Repair. Фаза: {0}' -f $transaction.Phase)

    if ([string]$transaction.Phase -eq 'Initialized') {
        $backupPath = [string]$transaction.BackupPath

        if (-not (Test-Path -LiteralPath $backupPath)) {
            $transactionPath = [string]$transaction.TransactionPath
            Remove-Item -LiteralPath $transactionPath -Recurse -Force -ErrorAction Stop
            Write-RepairLog -Level 'INFO' -Message 'Незавершённая транзакция не успела изменить .venv и удалена.'
            return
        }
    }

    Restore-OriginalVenv -Transaction $transaction
}

function Import-AzurPilotShortcutModule {
    [CmdletBinding()]
    param()

    [void](Resolve-RequiredFile -Path $script:ShortcutModulePath -DisplayName 'модуль ярлыков')
    [void](Resolve-RequiredFile -Path $script:ResolvedIconPath -DisplayName 'значок проекта AzurPilot')
    [void](Resolve-RequiredFile -Path (
        Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'scripts\Start-AzurPilot.ps1'
    ) -DisplayName 'команда Start этапа 2')

    Import-Module -Name $script:ShortcutModulePath -Force -ErrorAction Stop
}

function Get-ShortcutDiagnostic {
    [CmdletBinding()]
    param()

    if (
        -not $IsWindows -or
        [string]::IsNullOrWhiteSpace($script:ResolvedShortcutPath)
    ) {
        return $null
    }

    if (
        -not (Test-Path -LiteralPath $script:ShortcutModulePath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $script:ResolvedIconPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath (
            Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'scripts\Start-AzurPilot.ps1'
        ) -PathType Leaf)
    ) {
        $exists = Test-Path -LiteralPath $script:ResolvedShortcutPath -PathType Leaf

        return [pscustomobject]@{
            Exists = $exists
            Path = $script:ResolvedShortcutPath
            Healthy = $false
            Details = 'Модуль ярлыков, команда Start или значок проекта ещё не установлены.'
            TargetPath = ''
            Arguments = ''
            WorkingDirectory = ''
            IconLocation = ''
        }
    }

    try {
        Import-AzurPilotShortcutModule
        $pwshPath = [string](Get-Process -Id $PID -ErrorAction Stop).Path
        $specificationParameters = @{
            RepositoryPath = $script:ResolvedRepositoryPath
            PwshExecutablePath = $pwshPath
            IconPath = $script:ResolvedIconPath
        }
        $specification = Get-AzurPilotShortcutSpecification @specificationParameters
        $state = Get-AzurPilotShortcutState -ShortcutPath $script:ResolvedShortcutPath
        $healthy = Test-AzurPilotShortcutState -State $state -Specification $specification

        return [pscustomobject]@{
            Exists = [bool]$state.Exists
            Path = $script:ResolvedShortcutPath
            Healthy = $healthy
            Details = if ($healthy) {
                'Ярлык соответствует спецификации этапа 2.'
            } else {
                'Ярлык не соответствует цели, аргументам, рабочему каталогу, значку или описанию.'
            }
            TargetPath = [string]$state.TargetPath
            Arguments = [string]$state.Arguments
            WorkingDirectory = [string]$state.WorkingDirectory
            IconLocation = [string]$state.IconLocation
        }
    }
    catch {
        Write-RepairLog -Level 'WARN' -Message ('Не удалось выполнить диагностику ярлыка: {0}' -f $_.Exception.Message)

        return [pscustomobject]@{
            Exists = Test-Path -LiteralPath $script:ResolvedShortcutPath -PathType Leaf
            Path = $script:ResolvedShortcutPath
            Healthy = $false
            Details = $_.Exception.Message
            TargetPath = ''
            Arguments = ''
            WorkingDirectory = ''
            IconLocation = ''
        }
    }
}

function Invoke-AzurPilotShortcutRepair {
    [CmdletBinding()]
    param()

    if ([string]::IsNullOrWhiteSpace($script:ResolvedShortcutPath)) {
        Complete-RepairFailure -Code $script:ExitCodeShortcutFailure -Message 'Не удалось определить путь ярлыка.'
    }

    Import-AzurPilotShortcutModule

    $shortcutParent = Split-Path -Path $script:ResolvedShortcutPath -Parent
    New-Item -ItemType Directory -Path $shortcutParent -Force -ErrorAction Stop | Out-Null
    New-Item -ItemType Directory -Path $script:ResolvedShortcutBackupRoot -Force -ErrorAction Stop | Out-Null

    $requireAdministrator = $false

    if (-not [string]::IsNullOrWhiteSpace($env:ProgramData)) {
        $administratorCheckParameters = @{
            CandidatePath = $script:ResolvedShortcutPath
            ParentPath = [System.IO.Path]::GetFullPath($env:ProgramData)
        }
        $requireAdministrator = Test-PathInside @administratorCheckParameters
    }

    $pwshPath = [string](Get-Process -Id $PID -ErrorAction Stop).Path
    $shortcutParameters = @{
        ShortcutPath = $script:ResolvedShortcutPath
        RepositoryPath = $script:ResolvedRepositoryPath
        PwshExecutablePath = $pwshPath
        IconPath = $script:ResolvedIconPath
        BackupRoot = $script:ResolvedShortcutBackupRoot
        RequireAdministrator = $requireAdministrator
    }

    try {
        $result = Set-AzurPilotShortcut @shortcutParameters
    }
    catch {
        $reason = [string]$_.Exception.Data['AzurPilotShortcutReason']

        if ($reason -eq 'ElevationRequired') {
            $elevatedCommand = (
                "& '{0}' -NoLogo -NoProfile -File '{1}' -RepositoryPath '{2}' " +
                '-ShortcutOnly -RepairShortcut'
            ) -f $pwshPath, $PSCommandPath, $script:ResolvedRepositoryPath

            Complete-RepairFailure -Code $script:ExitCodeElevationRequired -Message (
                '{0}{1}Запустите PowerShell 7 от имени администратора и выполните:{1}{2}' -f
                $_.Exception.Message,
                [Environment]::NewLine,
                $elevatedCommand
            ) -InnerException $_.Exception
        }

        Complete-RepairFailure -Code $script:ExitCodeShortcutFailure -Message (
            'Не удалось восстановить ярлык: {0}' -f $_.Exception.Message
        ) -InnerException $_.Exception
    }

    if ([bool]$result.Changed) {
        Write-RepairLog -Level 'INFO' -Message ('Ярлык восстановлен: {0}' -f $script:ResolvedShortcutPath)

        if (-not [string]::IsNullOrWhiteSpace([string]$result.BackupPath)) {
            Write-RepairLog -Level 'INFO' -Message ('Резервная копия предыдущего ярлыка: {0}' -f $result.BackupPath)
        }
    } else {
        Write-RepairLog -Level 'INFO' -Message ('Ярлык уже исправен: {0}' -f $script:ResolvedShortcutPath)
    }
}

function Get-EnvironmentDiagnostic {
    [CmdletBinding()]
    param()

    $issues = [System.Collections.Generic.List[string]]::new()
    $warnings = [System.Collections.Generic.List[string]]::new()

    $venvPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv'
    $pythonPath = Join-Path -Path $venvPath -ChildPath 'Scripts\python.exe'
    $uvPath = Join-Path -Path $venvPath -ChildPath 'Scripts\uv.exe'
    $adbPath = Join-Path -Path $venvPath -ChildPath 'Scripts\adb.exe'
    $managedPythonRoot = Join-Path -Path $venvPath -ChildPath 'python'
    $postgresqlCheckPerformed = $false
    $postgresqlHealthy = $null

    if (-not (Test-Path -LiteralPath $venvPath -PathType Container)) {
        $issues.Add('Каталог .venv отсутствует.')
    }

    $pythonHealth = Test-PythonHealth -PythonPath $pythonPath

    if (-not $pythonHealth.Success) {
        $issues.Add(('Python проекта неисправен: {0}' -f $pythonHealth.Details))
    }

    $uvHealthy = Test-Executable -Executable $uvPath -Arguments @(
        '--version'
    )

    if (-not $uvHealthy) {
        $issues.Add('uv проекта отсутствует или не запускается.')
    }

    $managedPythonCandidates = @()

    if (Test-Path -LiteralPath $managedPythonRoot -PathType Container) {
        $managedPythonCandidates = @(
            Get-ChildItem -LiteralPath $managedPythonRoot -Directory -Filter 'cpython-*' -Force -ErrorAction Stop
        )
    }

    if ($managedPythonCandidates.Count -eq 0) {
        $issues.Add('Управляемая среда Python внутри .venv отсутствует.')
    }

    if ($pythonHealth.Success) {
        $importHealth = Test-PythonHealth -PythonPath $pythonPath -Imports @(
            'import yaml'
            'import uvicorn'
            'import pywebio'
            'import starlette'
            'import rich'
            'import numpy'
            'import cv2'
            'import adbutils'
        )

        if (-not $importHealth.Success) {
            $issues.Add(('Не пройдена проверка импорта обязательных модулей Python: {0}' -f $importHealth.Details))
        }
    }

    if ($pythonHealth.Success -and $uvHealthy) {
        $syncCheck = Invoke-NativeCommand -Executable $uvPath -Arguments @(
            'sync'
            '--dry-run'
            '--frozen'
            '--no-dev'
            '--no-install-project'
            '--project'
            $script:ResolvedRepositoryPath
            '--python'
            $pythonPath
        ) -WorkingDirectory $script:ResolvedRepositoryPath

        if ($syncCheck.ExitCode -ne 0) {
            $issues.Add('Пробный запуск uv не подтвердил согласованность .venv с uv.lock.')
            Write-NativeOutput -Result $syncCheck -Level 'WARN' -Prefix '[uv dry-run]'
        } else {
            $syncCheckText = $syncCheck.Output -join [Environment]::NewLine

            if (
                -not $syncCheckText.Contains(
                    'Would make no changes',
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            ) {
                $issues.Add('Пробный запуск uv обнаружил изменения, необходимые для согласования .venv с uv.lock.')
                Write-NativeOutput -Result $syncCheck -Level 'WARN' -Prefix '[uv dry-run]'
            }
        }
    }

    if ($pythonHealth.Success) {
        $postgresqlCheckPerformed = $true
        $wslState = Invoke-NativeCommand -Executable 'wsl.exe' -Arguments @(
            '--distribution'
            'Archlinux'
            '--exec'
            'systemctl'
            'is-active'
            '--quiet'
            'postgresql'
        ) -WorkingDirectory $script:ResolvedRepositoryPath

        $postgresqlHealth = Invoke-NativeCommand -Executable $pythonPath -Arguments @(
            '-X'
            'utf8'
            '-m'
            'dev_tools.postgresql_runtime'
            'health'
        ) -WorkingDirectory $script:ResolvedRepositoryPath

        $postgresqlSecurity = Invoke-NativeCommand -Executable $pythonPath -Arguments @(
            '-X'
            'utf8'
            '-m'
            'dev_tools.postgresql_security'
        ) -WorkingDirectory $script:ResolvedRepositoryPath

        $postgresqlHealthy = (
            $wslState.ExitCode -eq 0 -and
            $postgresqlHealth.ExitCode -eq 0 -and
            $postgresqlSecurity.ExitCode -eq 0
        )

        if (-not $postgresqlHealthy) {
            $warnings.Add('Production PostgreSQL не прошёл диагностику: проверьте WSL Archlinux, marker, app-доступ, schema head, loopback listener, SCRAM и HBA. Repair не изменяет БД.')
        }
    }

    $adbHealthy = Test-Executable -Executable $adbPath -Arguments @(
        'version'
    )

    if (-not $adbHealthy) {
        $warnings.Add('ADB отсутствует или не запускается. Внешний вспомогательный ADB будет определён на этапе 2E.')
    }

    $shortcutDiagnostic = Get-ShortcutDiagnostic

    if ($null -ne $shortcutDiagnostic -and -not [bool]$shortcutDiagnostic.Healthy) {
        $warnings.Add(
            (
                'Ярлык требует восстановления: {0}. ' +
                'Запустите Repair с -RepairShortcut в PowerShell 7 от имени администратора.'
            ) -f $shortcutDiagnostic.Details
        )
    }

    return [pscustomobject]@{
        Issues = $issues.ToArray()
        Warnings = $warnings.ToArray()
        VenvPath = $venvPath
        PythonPath = $pythonPath
        UvPath = $uvPath
        AdbPath = $adbPath
        Healthy = $issues.Count -eq 0
        PostgreSqlCheckPerformed = $postgresqlCheckPerformed
        PostgreSqlHealthy = $postgresqlHealthy
    }
}

function Write-DiagnosticResult {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Diagnostic
    )

    foreach ($warning in $Diagnostic.Warnings) {
        Write-RepairLog -Level 'WARN' -Message $warning
    }

    foreach ($issue in $Diagnostic.Issues) {
        Write-RepairLog -Level 'ERROR' -Message $issue
    }

    if ($Diagnostic.Healthy) {
        Write-RepairLog -Level 'INFO' -Message 'Диагностика: окружение исправно.'
    } else {
        Write-RepairLog -Level 'WARN' -Message ('Диагностика: найдено проблем окружения: {0}' -f $Diagnostic.Issues.Count)
    }
}

function Resolve-BootstrapUv {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Diagnostic
    )

    $resolveParameters = @{
        ConfiguredPath = $UvExecutablePath
        DefaultPath = $Diagnostic.UvPath
        CommandName = 'uv.exe'
        TestArguments = @(
            '--version'
        )
    }

    $uvExecutable = Resolve-ExecutableCandidate @resolveParameters

    if ($null -eq $uvExecutable) {
        Complete-RepairFailure -Code $script:ExitCodeBootstrapUnavailable -Message 'Не найден исправный uv. Укажите -UvExecutablePath или выполните Build-AzurPilot.ps1.'
    }

    return $uvExecutable
}

function Resolve-BootstrapPython {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Diagnostic
    )

    $candidates = [System.Collections.Generic.List[string]]::new()

    if (-not [string]::IsNullOrWhiteSpace($BootstrapPythonPath)) {
        if (-not (Test-Path -LiteralPath $BootstrapPythonPath -PathType Leaf)) {
            Complete-RepairFailure -Code $script:ExitCodeBootstrapUnavailable -Message ('Указанный вспомогательный Python не найден: {0}' -f $BootstrapPythonPath)
        }

        $candidates.Add(
            (Convert-Path -LiteralPath $BootstrapPythonPath -ErrorAction Stop)
        )
    }

    $commands = @(
        Get-Command -Name 'python.exe' -CommandType Application -ErrorAction SilentlyContinue
    )

    foreach ($command in $commands) {
        $commandPath = [string]$command.Path

        if (
            -not [string]::IsNullOrWhiteSpace($commandPath) -and
            -not $candidates.Contains($commandPath)
        ) {
            $candidates.Add($commandPath)
        }
    }

    $versionCheckCode = @'
import sys

raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
'@

    foreach ($candidate in $candidates) {
        if (Test-PathInside -CandidatePath $candidate -ParentPath $Diagnostic.VenvPath) {
            continue
        }

        if (
            Test-Executable -Executable $candidate -Arguments @(
                '-c'
                $versionCheckCode
            )
        ) {
            return $candidate
        }
    }

    Complete-RepairFailure -Code $script:ExitCodeBootstrapUnavailable -Message 'Не найден внешний Python 3.10+ для запуска deploy\uv.py. Укажите -BootstrapPythonPath.'
}

function Copy-BootstrapUv {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$SourcePath,

        [Parameter(Mandatory)]
        [string]$TransactionPath
    )

    $bootstrapDirectory = Join-Path -Path $TransactionPath -ChildPath 'bootstrap'
    New-Item -ItemType Directory -Path $bootstrapDirectory -Force -ErrorAction Stop | Out-Null

    $destinationPath = Join-Path -Path $bootstrapDirectory -ChildPath 'uv.exe'
    Copy-Item -LiteralPath $SourcePath -Destination $destinationPath -Force -ErrorAction Stop

    $sourceHash = (
        Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256 -ErrorAction Stop
    ).Hash
    $destinationHash = (
        Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256 -ErrorAction Stop
    ).Hash

    if ($sourceHash -ne $destinationHash) {
        Complete-RepairFailure -Code $script:ExitCodeBootstrapUnavailable -Message 'Транзакционная копия uv.exe не прошла проверку SHA-256.'
    }

    return [pscustomobject]@{
        Path = $destinationPath
        Hash = $destinationHash
    }
}

function Initialize-RepairTransaction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Diagnostic,

        [Parameter(Mandatory)]
        [string]$BootstrapUv,

        [Parameter(Mandatory)]
        [string]$BootstrapPython
    )

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $transactionName = 'repair-{0}-{1}' -f $timestamp, ([guid]::NewGuid().ToString('N'))
    $transactionPath = Join-Path -Path $script:ResolvedRepairWorkRoot -ChildPath $transactionName
    $backupPath = Join-Path -Path $transactionPath -ChildPath 'venv-backup'
    $journalPath = Join-Path -Path $transactionPath -ChildPath 'journal.json'
    $helperPath = Join-Path -Path $transactionPath -ChildPath 'invoke_deploy_uv.py'

    New-Item -ItemType Directory -Path $transactionPath -Force -ErrorAction Stop | Out-Null

    $bootstrapUvCopy = Copy-BootstrapUv -SourcePath $BootstrapUv -TransactionPath $transactionPath
    $lockPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'uv.lock'
    $lockHash = (
        Get-FileHash -LiteralPath $lockPath -Algorithm SHA256 -ErrorAction Stop
    ).Hash
    $configSnapshot = Get-ConfigSnapshot
    $venvSnapshot = Get-DirectorySnapshot -LiteralPath $Diagnostic.VenvPath

    $transaction = [pscustomobject]@{
        SchemaVersion = 1
        TransactionId = $transactionName
        TransactionPath = $transactionPath
        JournalPath = $journalPath
        HelperPath = $helperPath
        RepositoryPath = $script:ResolvedRepositoryPath
        VenvPath = $Diagnostic.VenvPath
        BackupPath = $backupPath
        OriginalVenvExisted = $venvSnapshot.Exists
        OriginalVenvSnapshot = $venvSnapshot
        ConfigSnapshot = $configSnapshot
        LockHash = $lockHash
        BootstrapUvPath = $bootstrapUvCopy.Path
        BootstrapUvHash = $bootstrapUvCopy.Hash
        BootstrapPythonPath = $BootstrapPython
        CreatedAt = [DateTimeOffset]::Now.ToString('o')
        UpdatedAt = [DateTimeOffset]::Now.ToString('o')
        Phase = 'Initialized'
        LastError = ''
    }

    Write-RepairJournal -Transaction $transaction -Phase 'Initialized'

    return $transaction
}

function Backup-CurrentVenv {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction
    )

    if (-not [bool]$Transaction.OriginalVenvExisted) {
        Write-RepairJournal -Transaction $Transaction -Phase 'BackupReady'
        Write-RepairLog -Level 'INFO' -Message 'Исходная .venv отсутствует. Резервная копия не требуется.'
        return
    }

    $venvPath = [string]$Transaction.VenvPath
    $backupPath = [string]$Transaction.BackupPath

    if (-not (Test-Path -LiteralPath $venvPath -PathType Container)) {
        Complete-RepairFailure -Code $script:ExitCodeRepairFailedRollbackSucceeded -Message ('Исходная .venv исчезла до создания резервной копии: {0}' -f $venvPath)
    }

    if (Test-Path -LiteralPath $backupPath) {
        Complete-RepairFailure -Code $script:ExitCodeTransactionConflict -Message ('Путь резервной копии уже существует: {0}' -f $backupPath)
    }

    Write-RepairLog -Level 'INFO' -Message ('Перемещение исходной .venv в резервную копию: {0}' -f $backupPath)
    Move-Item -LiteralPath $venvPath -Destination $backupPath -ErrorAction Stop

    $backupSnapshot = Get-DirectorySnapshot -LiteralPath $backupPath
    $expectedJson = $Transaction.OriginalVenvSnapshot | ConvertTo-Json -Compress -Depth 8
    $actualJson = $backupSnapshot | ConvertTo-Json -Compress -Depth 8

    if ($expectedJson -ne $actualJson) {
        Complete-RepairFailure -Code $script:ExitCodeRollbackFailed -Message 'Резервная копия .venv не совпадает с исходным снимком.'
    }

    Write-RepairJournal -Transaction $Transaction -Phase 'BackupReady'
}

function Invoke-TestFailPoint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateSet(
            'AfterBackup',
            'AfterRebuild',
            'BeforeValidation'
        )]
        [string]$Phase
    )

    if ($TestFailPoint -ne $Phase) {
        return
    }

    $exitCode = switch ($Phase) {
        'AfterBackup' {
            91
        }

        'AfterRebuild' {
            92
        }

        'BeforeValidation' {
            93
        }
    }

    Write-RepairLog -Level 'WARN' -Message ('TEST FAILPOINT: {0}, exit={1}' -f $Phase, $exitCode)
    [Environment]::Exit($exitCode)
}

function Write-DeployUvHelper {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction
    )

    $helperContent = @'
import os
import sys
from pathlib import Path

root = Path(os.environ["AZURPILOT_REPAIR_ROOT"])
sys.path.insert(0, str(root))

from deploy.uv import sync_project_venv

bootstrap_uv = Path(os.environ["AZURPILOT_REPAIR_UV"])
timeout = float(os.environ["AZURPILOT_REPAIR_TIMEOUT"])

sync_project_venv(
    root=root,
    bootstrap_uv=bootstrap_uv,
    capture_output=False,
    timeout=timeout,
)
'@

    Set-Content -LiteralPath $Transaction.HelperPath -Value $helperContent -Encoding utf8NoBOM -ErrorAction Stop
}

function Invoke-DeployUvRepair {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction
    )

    Write-DeployUvHelper -Transaction $Transaction
    Write-RepairJournal -Transaction $Transaction -Phase 'RebuildStarted'

    $environmentNames = @(
        'AZURPILOT_REPAIR_ROOT'
        'AZURPILOT_REPAIR_UV'
        'AZURPILOT_REPAIR_TIMEOUT'
        'PYTHONHOME'
        'PYTHONPATH'
        'PYTHONDONTWRITEBYTECODE'
        'VIRTUAL_ENV'
        '__PYVENV_LAUNCHER__'
        'UV_CONFIG_FILE'
        'UV_DEFAULT_INDEX'
        'UV_EXTRA_INDEX_URL'
        'UV_FIND_LINKS'
        'UV_INDEX'
        'UV_INDEX_STRATEGY'
        'UV_INDEX_URL'
        'UV_KEYRING_PROVIDER'
        'UV_OVERRIDE'
        'UV_PROJECT_ENVIRONMENT'
        'PIP_CONFIG_FILE'
        'PIP_EXTRA_INDEX_URL'
        'PIP_FIND_LINKS'
        'PIP_INDEX_URL'
        'PIP_TRUSTED_HOST'
    )
    $savedEnvironment = @{}

    foreach ($name in $environmentNames) {
        $environmentPath = 'Env:\{0}' -f $name
        $exists = Test-Path -LiteralPath $environmentPath
        $savedEnvironment[$name] = [pscustomobject]@{
            Exists = $exists
            Value = if ($exists) {
                (Get-Item -LiteralPath $environmentPath -ErrorAction Stop).Value
            } else {
                $null
            }
        }

        if ($exists) {
            Remove-Item -LiteralPath $environmentPath -ErrorAction Stop
        }
    }

    Set-Item -LiteralPath 'Env:\AZURPILOT_REPAIR_ROOT' -Value $script:ResolvedRepositoryPath -ErrorAction Stop
    Set-Item -LiteralPath 'Env:\AZURPILOT_REPAIR_UV' -Value ([string]$Transaction.BootstrapUvPath) -ErrorAction Stop
    Set-Item -LiteralPath 'Env:\AZURPILOT_REPAIR_TIMEOUT' -Value ([string]$SyncTimeoutSeconds) -ErrorAction Stop
    Set-Item -LiteralPath 'Env:\PYTHONDONTWRITEBYTECODE' -Value '1' -ErrorAction Stop

    try {
        $result = Invoke-NativeCommand -Executable ([string]$Transaction.BootstrapPythonPath) -Arguments @(
            [string]$Transaction.HelperPath
        ) -WorkingDirectory $script:ResolvedRepositoryPath
    }
    finally {
        foreach ($name in $environmentNames) {
            $environmentPath = 'Env:\{0}' -f $name

            if (Test-Path -LiteralPath $environmentPath) {
                Remove-Item -LiteralPath $environmentPath -ErrorAction Stop
            }

            $saved = $savedEnvironment[$name]

            if ($saved.Exists) {
                Set-Item -LiteralPath $environmentPath -Value $saved.Value -ErrorAction Stop
            }
        }
    }

    Write-NativeOutput -Result $result -Level 'INFO' -Prefix '[deploy.uv]'

    if ($result.ExitCode -ne 0) {
        Complete-RepairFailure -Code $script:ExitCodeRepairFailedRollbackSucceeded -Message ('deploy\uv.py завершился с кодом {0}.' -f $result.ExitCode)
    }

    Write-RepairJournal -Transaction $Transaction -Phase 'RebuildCompleted'
}

function Restore-AuxiliaryTool {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction
    )

    $venvScriptsPath = Join-Path -Path $Transaction.VenvPath -ChildPath 'Scripts'
    New-Item -ItemType Directory -Path $venvScriptsPath -Force -ErrorAction Stop | Out-Null

    $newUvPath = Join-Path -Path $venvScriptsPath -ChildPath 'uv.exe'

    if (-not (Test-Executable -Executable $newUvPath -Arguments @('--version'))) {
        Copy-Item -LiteralPath $Transaction.BootstrapUvPath -Destination $newUvPath -Force -ErrorAction Stop
        Write-RepairLog -Level 'INFO' -Message 'uv.exe восстановлен в новой .venv.'
    }

    if (-not [bool]$Transaction.OriginalVenvExisted) {
        return
    }

    $backupScriptsPath = Join-Path -Path $Transaction.BackupPath -ChildPath 'Scripts'

    foreach ($fileName in @(
        'adb.exe'
        'AdbWinApi.dll'
        'AdbWinUsbApi.dll'
    )) {
        $sourcePath = Join-Path -Path $backupScriptsPath -ChildPath $fileName
        $destinationPath = Join-Path -Path $venvScriptsPath -ChildPath $fileName

        if (
            (Test-Path -LiteralPath $sourcePath -PathType Leaf) -and
            -not (Test-Path -LiteralPath $destinationPath -PathType Leaf)
        ) {
            Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Force -ErrorAction Stop
        }
    }

    $sourceGitPath = Join-Path -Path $backupScriptsPath -ChildPath 'git'
    $destinationGitPath = Join-Path -Path $venvScriptsPath -ChildPath 'git'

    if (
        (Test-Path -LiteralPath $sourceGitPath -PathType Container) -and
        -not (Test-Path -LiteralPath $destinationGitPath -PathType Container)
    ) {
        Copy-Item -LiteralPath $sourceGitPath -Destination $destinationGitPath -Recurse -Force -ErrorAction Stop
    }
}

function Assert-RepairedEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction
    )

    $currentLockHash = (
        Get-FileHash -LiteralPath (Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'uv.lock') -Algorithm SHA256 -ErrorAction Stop
    ).Hash

    if ($currentLockHash -ne [string]$Transaction.LockHash) {
        Complete-RepairFailure -Code $script:ExitCodeRollbackFailed -Message 'uv.lock изменился во время Repair.'
    }

    $currentConfigSnapshot = Get-ConfigSnapshot
    Assert-ConfigSnapshotEqual -Expected $Transaction.ConfigSnapshot -Actual $currentConfigSnapshot

    $diagnostic = Get-EnvironmentDiagnostic
    Write-DiagnosticResult -Diagnostic $diagnostic

    if (-not $diagnostic.Healthy) {
        Complete-RepairFailure -Code $script:ExitCodeRepairFailedRollbackSucceeded -Message 'Восстановленная .venv не прошла итоговую диагностику.'
    }

    Write-RepairJournal -Transaction $Transaction -Phase 'Validated'
}

function Complete-RepairTransaction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction
    )

    Write-RepairJournal -Transaction $Transaction -Phase 'Completed'
    Write-RepairLog -Level 'INFO' -Message ('Транзакция Repair завершена: {0}' -f $Transaction.TransactionPath)
}

function Clear-ExpiredRepairTransaction {
    [CmdletBinding()]
    param()

    $completedTransactions = [System.Collections.Generic.List[object]]::new()
    $journalItems = @(
        Get-ChildItem -LiteralPath $script:ResolvedRepairWorkRoot -Filter 'journal.json' -File -Recurse -Force -ErrorAction Stop
    )

    foreach ($journalItem in $journalItems) {
        $journal = Read-RepairJournal -JournalPath $journalItem.FullName

        if ([string]$journal.Phase -eq 'Completed') {
            $completedTransactions.Add(
                [pscustomobject]@{
                    Journal = $journal
                    UpdatedAt = [DateTimeOffset]::ParseExact(
                        [string]$journal.UpdatedAt,
                        'o',
                        [System.Globalization.CultureInfo]::InvariantCulture,
                        [System.Globalization.DateTimeStyles]::None
                    )
                }
            )
        }
    }

    $sortedTransactions = @(
        $completedTransactions |
            Sort-Object -Property UpdatedAt -Descending
    )

    if ($sortedTransactions.Count -le $BackupRetentionCount) {
        return
    }

    $expiredTransactions = $sortedTransactions[$BackupRetentionCount..($sortedTransactions.Count - 1)]

    foreach ($expiredTransaction in $expiredTransactions) {
        $transactionPath = [string]$expiredTransaction.Journal.TransactionPath

        if (
            -not [string]::IsNullOrWhiteSpace($transactionPath) -and
            (Test-PathInside -CandidatePath $transactionPath -ParentPath $script:ResolvedRepairWorkRoot) -and
            (Test-Path -LiteralPath $transactionPath -PathType Container)
        ) {
            Remove-Item -LiteralPath $transactionPath -Recurse -Force -ErrorAction Stop
            Write-RepairLog -Level 'INFO' -Message ('Удалена устаревшая резервная копия Repair: {0}' -f $transactionPath)
        }
    }
}

function Invoke-AzurPilotRepair {
    [CmdletBinding()]
    param()

    try {
        Initialize-RepairLog
        Write-RepairLog -Level 'INFO' -Message 'Запуск AzurPilot Repair, этап 2D.'
        Write-RepairLog -Level 'INFO' -Message ('PowerShell: {0}' -f $PSVersionTable.PSVersion)
        Write-RepairLog -Level 'INFO' -Message ('RepositoryPath: {0}' -f $RepositoryPath)

        if (-not $IsWindows) {
            Complete-RepairFailure -Code $script:ExitCodePreconditionFailure -Message 'Repair-AzurPilot.ps1 поддерживает только Windows.'
        }

        Initialize-RepairPath

        $mutexData = Enter-RepairMutex -ResolvedRepositoryPath $script:ResolvedRepositoryPath
        $script:RepairMutex = $mutexData.Mutex
        $script:RepairMutexOwned = $mutexData.Owned

        Write-RepairLog -Level 'INFO' -Message ('Мьютекс Repair: {0}' -f $mutexData.Name)

        if ($script:ShortcutOnlyParameter) {
            if (-not $script:RepairShortcutParameter) {
                Complete-RepairFailure -Code $script:ExitCodePreconditionFailure -Message 'Параметр -ShortcutOnly требует -RepairShortcut.'
            }

            Invoke-AzurPilotShortcutRepair
            Write-RepairLog -Level 'INFO' -Message 'Операция Repair только для ярлыка завершена успешно.'
            return $script:ExitCodeSuccess
        }

        Assert-RequiredProjectFile
        Assert-AzurPilotStopped
        Assert-NoUpdateTransaction

        if ($DiagnosticOnly) {
            $incompleteTransactions = @(Get-IncompleteRepairTransaction)

            if ($incompleteTransactions.Count -gt 0) {
                Complete-RepairFailure -Code $script:ExitCodeTransactionConflict -Message 'DiagnosticOnly обнаружил незавершённую транзакцию Repair. Запустите Repair без -DiagnosticOnly для безопасного отката.'
            }
        } else {
            Invoke-IncompleteRepairRecovery
        }

        $diagnostic = Get-EnvironmentDiagnostic
        Write-DiagnosticResult -Diagnostic $diagnostic

        if ($diagnostic.PostgreSqlCheckPerformed -and -not $diagnostic.PostgreSqlHealthy) {
            Write-RepairLog -Level 'ERROR' -Message 'Диагностика PostgreSQL завершилась ошибкой; автоматическое исправление БД запрещено.'
            return $script:ExitCodeDiagnosticFailure
        }

        if ($diagnostic.Healthy) {
            Write-RepairLog -Level 'INFO' -Message 'Repair не требуется. Изменения .venv не выполнялись.'

            if (-not $DiagnosticOnly) {
                Clear-ExpiredRepairTransaction

                if ($script:RepairShortcutParameter) {
                    Invoke-AzurPilotShortcutRepair
                }
            }

            return $script:ExitCodeSuccess
        }

        if ($DiagnosticOnly) {
            Write-RepairLog -Level 'WARN' -Message 'DiagnosticOnly: окружение требует восстановления, но изменения запрещены.'
            return $script:ExitCodeDiagnosticFailure
        }

        $bootstrapUv = Resolve-BootstrapUv -Diagnostic $diagnostic
        $bootstrapPython = Resolve-BootstrapPython -Diagnostic $diagnostic

        Write-RepairLog -Level 'INFO' -Message ('Вспомогательный uv: {0}' -f $bootstrapUv)
        Write-RepairLog -Level 'INFO' -Message ('Вспомогательный Python: {0}' -f $bootstrapPython)

        $transactionParameters = @{
            Diagnostic = $diagnostic
            BootstrapUv = $bootstrapUv
            BootstrapPython = $bootstrapPython
        }

        $transaction = Initialize-RepairTransaction @transactionParameters

        try {
            Backup-CurrentVenv -Transaction $transaction
            Invoke-TestFailPoint -Phase 'AfterBackup'

            Invoke-DeployUvRepair -Transaction $transaction
            Restore-AuxiliaryTool -Transaction $transaction
            Invoke-TestFailPoint -Phase 'AfterRebuild'
            Invoke-TestFailPoint -Phase 'BeforeValidation'

            Assert-RepairedEnvironment -Transaction $transaction
            Complete-RepairTransaction -Transaction $transaction

            try {
                Clear-ExpiredRepairTransaction
            }
            catch {
                Write-RepairLog -Level 'WARN' -Message (
                    'Не удалось выполнить необязательную очистку старых резервных копий Repair: {0}' -f
                    $_.Exception.Message
                )
            }

            if ($script:RepairShortcutParameter) {
                Invoke-AzurPilotShortcutRepair
            }

            Write-RepairLog -Level 'INFO' -Message 'Окружение AzurPilot успешно восстановлено.'
            return $script:ExitCodeSuccess
        }
        catch {
            $repairException = $_.Exception
            $transaction.LastError = $repairException.Message

            try {
                Write-RepairLog -Level 'ERROR' -Message ('Repair завершился ошибкой: {0}' -f $repairException.Message)
                Restore-OriginalVenv -Transaction $transaction
            }
            catch {
                $rollbackException = $_.Exception
                $message = 'Repair завершился ошибкой, затем не удался откат: {0}' -f $rollbackException.Message
                Complete-RepairFailure -Code $script:ExitCodeRollbackFailed -Message $message -InnerException $repairException
            }

            $message = 'Repair завершился ошибкой. Исходная .venv восстановлена: {0}' -f $repairException.Message
            Complete-RepairFailure -Code $script:ExitCodeRepairFailedRollbackSucceeded -Message $message -InnerException $repairException
        }
    }
    catch {
        $exitCode = $script:ExitCodeUnexpectedFailure

        if ($_.Exception.Data.Contains('ExitCode')) {
            $exitCode = [int]$_.Exception.Data['ExitCode']
        }

        $errorMessage = $_.Exception.Message

        try {
            Write-RepairLog -Level 'ERROR' -Message $errorMessage
        }
        catch {
            Write-ConsoleMessage -Message ('AzurPilot Repair: {0}' -f $errorMessage)
        }

        return $exitCode
    }
    finally {
        if ($null -ne $script:RepairMutex) {
            if ($script:RepairMutexOwned) {
                try {
                    $script:RepairMutex.ReleaseMutex()
                }
                catch {
                    if ($null -ne $script:LogPath) {
                        Write-RepairLog -Level 'WARN' -Message ('Не удалось освободить мьютекс Repair: {0}' -f $_.Exception.Message)
                    }
                }
            }

            $script:RepairMutex.Dispose()
        }

        if ($null -ne $script:LogPath) {
            Write-ConsoleMessage -Message ('Лог: {0}' -f $script:LogPath)
        }
    }
}

$repairExitCode = Invoke-AzurPilotRepair
exit $repairExitCode
