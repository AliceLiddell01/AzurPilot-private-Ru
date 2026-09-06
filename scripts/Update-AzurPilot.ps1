#requires -Version 7.6

[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryPath = 'C:\AzurPilot',

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$ExpectedBranch = 'personal/stable',

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$ExpectedOriginUrl = 'git@github.com:AliceLiddell01/AzurPilot-private-Ru.git',

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$RemoteName = 'origin',

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$RemoteBranch = 'personal/stable',

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$RequiredUpstreamPushUrl = 'DISABLED',

    [Parameter()]
    [string]$LogDirectory = '',

    [Parameter()]
    [string]$UvExecutablePath = '',

    [Parameter()]
    [string]$PythonExecutablePath = '',

    [Parameter()]
    [string]$DependencyWorkRoot = '',

    [Parameter()]
    [string]$PostgreSqlBackupRoot = '',

    [Parameter()]
    [string]$RobocopyExecutablePath = '',

    [Parameter()]
    [string]$TarExecutablePath = '',

    [Parameter()]
    [ValidateSet(
        'None',
        'AfterBackup',
        'AfterSync',
        'AfterMerge'
    )]
    [string]$TestFailPoint = 'None',

    [Parameter()]
    [string]$TestMutationRelativePath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

function Write-ConsoleMessage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Message
    )

    Write-Information -MessageData $Message -InformationAction Continue
}

$script:ExitCodeSuccess = 0
$script:ExitCodeNetworkFailure = 10
$script:ExitCodePreconditionFailure = 20
$script:ExitCodeLocalAhead = 21
$script:ExitCodeDiverged = 22
$script:ExitCodeDependencyFailure = 23
$script:ExitCodeUnexpectedFailure = 30
$script:LogPath = $null
$script:PostgreSqlBackupRootParameter = $PostgreSqlBackupRoot
$script:GitExecutable = $null
$script:UvExecutable = $null
$script:PythonExecutable = $null
$script:RobocopyExecutable = $null
$script:TarExecutable = $null
$script:ResolvedDependencyWorkRoot = $null
$script:RequestedLogDirectory = $LogDirectory

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

    return $safeText
}

function Initialize-UpdateLog {
    [CmdletBinding()]
    param()

    if ([string]::IsNullOrWhiteSpace($script:RequestedLogDirectory)) {
        $baseDirectory = $env:LOCALAPPDATA

        if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
            $baseDirectory = $env:TEMP
        }

        if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
            throw 'Не удалось определить каталог для журнала обновления.'
        }

        $resolvedLogDirectory = Join-Path -Path $baseDirectory -ChildPath 'AzurPilot\logs'
    } else {
        $resolvedLogDirectory = $script:RequestedLogDirectory
    }

    New-Item -ItemType Directory -Path $resolvedLogDirectory -Force -ErrorAction Stop | Out-Null

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $fileName = "Update-AzurPilot-$timestamp-$PID.log"
    $script:LogPath = Join-Path -Path $resolvedLogDirectory -ChildPath $fileName

    New-Item -ItemType File -Path $script:LogPath -Force -ErrorAction Stop | Out-Null
}

function Write-UpdateLog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateSet('INFO', 'WARN', 'ERROR')]
        [string]$Level,

        [Parameter(Mandatory)]
        [string]$Message
    )

    $safeMessage = Protect-SensitiveText -Text $Message
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$timestamp] [$Level] $safeMessage"

    Write-ConsoleMessage -Message $line

    if ($null -ne $script:LogPath) {
        Add-Content -LiteralPath $script:LogPath -Value $line -Encoding utf8 -ErrorAction Stop
    }
}

function Complete-Update {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [int]$Code,

        [Parameter(Mandatory)]
        [string]$Message
    )

    Write-UpdateLog -Level 'ERROR' -Message $Message

    if ($null -ne $script:LogPath) {
        Write-ConsoleMessage -Message "Лог: $script:LogPath"
    }

    exit $Code
}

function Invoke-NativeCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Executable,

        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $nativeOutput = & $Executable @Arguments 2>&1
    $nativeExitCode = $LASTEXITCODE
    $nativeOutput = @($nativeOutput)

    return [pscustomobject]@{
        ExitCode = $nativeExitCode
        Output = [string[]]$nativeOutput
    }
}

function Write-NativeOutput {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Result,

        [Parameter()]
        [ValidateSet('INFO', 'WARN', 'ERROR')]
        [string]$Level = 'INFO'
    )

    foreach ($line in $Result.Output) {
        $text = [string]$line

        if (-not [string]::IsNullOrWhiteSpace($text)) {
            Write-UpdateLog -Level $Level -Message $text
        }
    }
}

function Invoke-Git {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $fullArguments = @(
        '-C'
        $RepositoryPath
    ) + $Arguments

    return Invoke-NativeCommand -Executable $script:GitExecutable -Arguments $fullArguments
}

function Get-SingleGitValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Operation,

        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $result = Invoke-Git -Arguments $Arguments

    if ($result.ExitCode -ne 0) {
        Write-NativeOutput -Result $result -Level 'ERROR'
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message "Git не смог выполнить операцию: $Operation"
    }

    $value = ($result.Output -join [Environment]::NewLine).Trim()

    if ([string]::IsNullOrWhiteSpace($value)) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message "Git вернул пустое значение: $Operation"
    }

    return $value
}

function Test-GitAncestor {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Ancestor,

        [Parameter(Mandatory)]
        [string]$Descendant
    )

    $result = Invoke-Git -Arguments @(
        'merge-base'
        '--is-ancestor'
        $Ancestor
        $Descendant
    )

    if ($result.ExitCode -eq 0) {
        return $true
    }

    if ($result.ExitCode -eq 1) {
        return $false
    }

    Write-NativeOutput -Result $result -Level 'ERROR'
    Complete-Update -Code $script:ExitCodeUnexpectedFailure -Message 'git merge-base завершился непредусмотренным кодом возврата.'
}

function Assert-NoActiveGitOperation {
    [CmdletBinding()]
    param()

    $gitDirectory = Get-SingleGitValue -Operation 'определение каталога .git' -Arguments @(
        'rev-parse'
        '--absolute-git-dir'
    )

    $operationMarkers = @(
        'MERGE_HEAD'
        'CHERRY_PICK_HEAD'
        'REVERT_HEAD'
        'BISECT_LOG'
        'rebase-apply'
        'rebase-merge'
        'sequencer'
    )

    $foundMarkers = @()

    foreach ($marker in $operationMarkers) {
        $markerPath = Join-Path -Path $gitDirectory -ChildPath $marker

        if (Test-Path -LiteralPath $markerPath) {
            $foundMarkers += $markerPath
        }
    }

    if ($foundMarkers.Count -gt 0) {
        $markerText = $foundMarkers -join ', '
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message "Обнаружена незавершённая Git-операция: $markerText"
    }
}

function Assert-AzurPilotStopped {
    [CmdletBinding()]
    param()

    if (-not $IsWindows) {
        return
    }

    $repositoryRegex = [regex]::Escape($RepositoryPath)
    $launcherPath = Join-Path -Path $RepositoryPath -ChildPath 'alas-launcher.exe'

    try {
        $processes = @(
            Get-CimInstance -ClassName Win32_Process -ErrorAction Stop
        )
    } catch {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message "Не удалось проверить процессы AzurPilot: $($_.Exception.Message)"
    }

    $matchingProcesses = @(
        $processes |
            Where-Object {
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

    if ($matchingProcesses.Count -gt 0) {
        $processText = $matchingProcesses |
            Select-Object -Property ProcessId, Name, ExecutablePath, CommandLine |
            Format-List |
            Out-String

        Complete-Update -Code $script:ExitCodePreconditionFailure -Message "AzurPilot сейчас запущен. Завершите его штатно перед обновлением.$([Environment]::NewLine)$processText"
    }
}

function Resolve-ApplicationPath {
    [CmdletBinding()]
    param(
        [Parameter()]
        [string]$ConfiguredPath,

        [Parameter(Mandatory)]
        [string]$DefaultPath,

        [Parameter(Mandatory)]
        [string]$DisplayName
    )

    $candidatePath = $ConfiguredPath

    if ([string]::IsNullOrWhiteSpace($candidatePath)) {
        $candidatePath = $DefaultPath
    }

    if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
        return (Resolve-Path -LiteralPath $candidatePath -ErrorAction Stop).Path
    }

    $commands = @(
        Get-Command -Name $candidatePath -CommandType Application -ErrorAction SilentlyContinue
    )

    if ($commands.Count -gt 0) {
        return $commands[0].Source
    }

    Complete-Update -Code $script:ExitCodePreconditionFailure -Message "$DisplayName не найден: $candidatePath"
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

function Get-DirectorySnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$LiteralPath
    )

    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Container)) {
        throw "Каталог не найден: $LiteralPath"
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
        FileCount = $files.Count
        TotalBytes = [int64]$totalBytes
        KeyHashes = $keyHashes
    }
}

function Assert-SnapshotEqual {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Expected,

        [Parameter(Mandatory)]
        [object]$Actual,

        [Parameter(Mandatory)]
        [string]$Context
    )

    if ($Expected.FileCount -ne $Actual.FileCount) {
        throw "${Context}: количество файлов различается."
    }

    if ($Expected.TotalBytes -ne $Actual.TotalBytes) {
        throw "${Context}: суммарный размер файлов различается."
    }

    $expectedJson = $Expected.KeyHashes | ConvertTo-Json -Compress
    $actualJson = $Actual.KeyHashes | ConvertTo-Json -Compress

    if ($expectedJson -ne $actualJson) {
        throw "${Context}: контрольные SHA-256 различаются."
    }
}

function Invoke-RobocopyMirror {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$SourcePath,

        [Parameter(Mandatory)]
        [string]$DestinationPath,

        [Parameter(Mandatory)]
        [string]$Operation
    )

    New-Item -ItemType Directory -Path $DestinationPath -Force -ErrorAction Stop | Out-Null

    $result = Invoke-NativeCommand -Executable $script:RobocopyExecutable -Arguments @(
        $SourcePath
        $DestinationPath
        '/MIR'
        '/COPY:DAT'
        '/DCOPY:DAT'
        '/R:2'
        '/W:1'
        '/XJ'
        '/NFL'
        '/NDL'
        '/NP'
        '/NJH'
        '/NJS'
    )

    if ($result.ExitCode -gt 7) {
        Write-NativeOutput -Result $result -Level 'ERROR'
        throw "$Operation завершилась с кодом robocopy $($result.ExitCode)."
    }

    Write-NativeOutput -Result $result
}

function Invoke-UvIsolated {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [Parameter()]
        [string]$ProjectEnvironment = '',

        [Parameter()]
        [string]$OverridePath = '',

        [Parameter()]
        [string]$ExecutablePath = ''
    )

    $environmentNames = @(
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
        $environmentPath = "Env:\$name"
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

    if (-not [string]::IsNullOrWhiteSpace($ProjectEnvironment)) {
        Set-Item -LiteralPath 'Env:\UV_PROJECT_ENVIRONMENT' -Value $ProjectEnvironment -ErrorAction Stop
    }

    if (-not [string]::IsNullOrWhiteSpace($OverridePath)) {
        Set-Item -LiteralPath 'Env:\UV_OVERRIDE' -Value $OverridePath -ErrorAction Stop
    }

    $effectiveExecutable = if ([string]::IsNullOrWhiteSpace($ExecutablePath)) {
        $script:UvExecutable
    } else {
        $ExecutablePath
    }

    try {
        return Invoke-NativeCommand -Executable $effectiveExecutable -Arguments $Arguments
    } finally {
        foreach ($name in $environmentNames) {
            $environmentPath = "Env:\$name"
            Remove-Item -LiteralPath $environmentPath -ErrorAction SilentlyContinue

            $saved = $savedEnvironment[$name]

            if ($saved.Exists) {
                Set-Item -LiteralPath $environmentPath -Value $saved.Value -ErrorAction Stop
            }
        }
    }
}

function Initialize-DependencyCandidate {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$RemoteSha
    )

    New-Item -ItemType Directory -Path $script:ResolvedDependencyWorkRoot -Force -ErrorAction Stop | Out-Null

    $transactionName = "dependency-$RemoteSha-$([guid]::NewGuid().ToString('N'))"
    $transactionPath = Join-Path -Path $script:ResolvedDependencyWorkRoot -ChildPath $transactionName
    $candidatePath = Join-Path -Path $transactionPath -ChildPath 'candidate'
    $archivePath = Join-Path -Path $transactionPath -ChildPath 'candidate.tar'
    $backupPath = Join-Path -Path $transactionPath -ChildPath 'venv-backup'
    $overridePath = Join-Path -Path $transactionPath -ChildPath 'overrides.txt'
    $journalPath = Join-Path -Path $transactionPath -ChildPath 'journal.json'

    New-Item -ItemType Directory -Path $candidatePath -Force -ErrorAction Stop | Out-Null

    $archiveResult = Invoke-Git -Arguments @(
        'archive'
        '--format=tar'
        "--output=$archivePath"
        $RemoteSha
        '--'
        'pyproject.toml'
        'uv.lock'
    )

    if ($archiveResult.ExitCode -ne 0) {
        Write-NativeOutput -Result $archiveResult -Level 'ERROR'
        throw 'Не удалось извлечь кандидатные файлы зависимостей из удалённого коммита.'
    }

    $extractResult = Invoke-NativeCommand -Executable $script:TarExecutable -Arguments @(
        '-xf'
        $archivePath
        '-C'
        $candidatePath
    )

    if ($extractResult.ExitCode -ne 0) {
        Write-NativeOutput -Result $extractResult -Level 'ERROR'
        throw 'Не удалось распаковать кандидатные файлы зависимостей.'
    }

    $candidatePyprojectPath = Join-Path -Path $candidatePath -ChildPath 'pyproject.toml'
    $candidateLockPath = Join-Path -Path $candidatePath -ChildPath 'uv.lock'

    foreach ($requiredPath in @($candidatePyprojectPath, $candidateLockPath)) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "В удалённом коммите отсутствует обязательный файл: $requiredPath"
        }
    }

    return [pscustomobject]@{
        TransactionPath = $transactionPath
        CandidatePath = $candidatePath
        CandidatePyprojectPath = $candidatePyprojectPath
        CandidateLockPath = $candidateLockPath
        BackupPath = $backupPath
        OverridePath = $overridePath
        JournalPath = $journalPath
    }
}

function Assert-CandidateDependencySource {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction
    )

    $validationScript = @'
from pathlib import Path
from urllib.parse import urlsplit
import json
import sys
import tomllib

pyproject_path = Path(sys.argv[1])
lock_path = Path(sys.argv[2])
override_path = Path(sys.argv[3])

blocked_hosts = {
    "mirrors.aliyun.com",
    "mirrors.cloud.tencent.com",
    "repo.huaweicloud.com",
}

def fail(message):
    raise RuntimeError(message)

pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8-sig"))
tool_uv = ((pyproject.get("tool") or {}).get("uv") or {})

allowed_keys = {
    "package",
    "override-dependencies",
}

unexpected_keys = sorted(set(tool_uv).difference(allowed_keys))

if unexpected_keys:
    fail(f"pyproject.toml содержит неподдерживаемые tool.uv keys: {unexpected_keys}")

if tool_uv.get("package") is not False:
    fail("tool.uv.package должен быть false")

overrides = tool_uv.get("override-dependencies") or []

if not isinstance(overrides, list):
    fail("tool.uv.override-dependencies должен быть списком")

for value in overrides:
    if not isinstance(value, str) or not value.strip():
        fail("override-dependencies содержит некорректную запись")

override_path.write_text(
    "\n".join(overrides) + ("\n" if overrides else ""),
    encoding="utf-8",
)

lock_text = lock_path.read_text(encoding="utf-8-sig")
lower_lock_text = lock_text.lower()

for host in blocked_hosts:
    if host in lower_lock_text:
        fail(f"uv.lock содержит запрещённый host: {host}")

lock_data = tomllib.loads(lock_text)
packages = lock_data.get("package") or []
registry_hosts = set()
artifact_hosts = set()
virtual_sources = []
unsupported_sources = []

for package in packages:
    name = str(package.get("name") or "")
    source = package.get("source") or {}

    if set(source) == {"registry"}:
        host = urlsplit(str(source.get("registry"))).hostname

        if host:
            registry_hosts.add(host.lower())
    elif set(source) == {"virtual"}:
        virtual_sources.append(
            {
                "Package": name,
                "Value": source.get("virtual"),
            }
        )
    else:
        unsupported_sources.append(
            {
                "Package": name,
                "Source": source,
            }
        )

    artifacts = []

    sdist = package.get("sdist")

    if isinstance(sdist, dict):
        artifacts.append(sdist)

    artifacts.extend(package.get("wheels") or [])

    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue

        url = artifact.get("url")

        if isinstance(url, str):
            host = urlsplit(url).hostname

            if host:
                artifact_hosts.add(host.lower())

if registry_hosts.difference({"pypi.org"}):
    fail(f"uv.lock содержит неожиданные registry hosts: {sorted(registry_hosts)}")

if artifact_hosts.difference({"files.pythonhosted.org"}):
    fail(f"uv.lock содержит неожиданные artifact hosts: {sorted(artifact_hosts)}")

if unsupported_sources:
    fail(
        "uv.lock содержит неподдерживаемые source kinds: "
        + json.dumps(unsupported_sources, ensure_ascii=False, sort_keys=True)
    )

if len(virtual_sources) != 1:
    fail(f"Ожидался один virtual source, получено: {virtual_sources}")

virtual_source = virtual_sources[0]

if virtual_source.get("Value") != ".":
    fail(f"Virtual source должен указывать на '.': {virtual_source}")

result = {
    "PackageCount": len(packages),
    "RegistryHosts": sorted(registry_hosts),
    "ArtifactHosts": sorted(artifact_hosts),
    "VirtualSources": virtual_sources,
    "OverrideCount": len(overrides),
}

print(json.dumps(result, ensure_ascii=False, indent=2))
'@

    $result = Invoke-NativeCommand -Executable $script:PythonExecutable -Arguments @(
        '-c'
        $validationScript
        $Transaction.CandidatePyprojectPath
        $Transaction.CandidateLockPath
        $Transaction.OverridePath
    )

    if ($result.ExitCode -ne 0) {
        Write-NativeOutput -Result $result -Level 'ERROR'
        throw 'Кандидатные файлы зависимостей не прошли проверку источников.'
    }

    Write-NativeOutput -Result $result
}

function Write-DependencyJournal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction,

        [Parameter(Mandatory)]
        [ValidateSet(
            'CandidateValidated',
            'BackupReady',
            'EnvironmentSynchronized',
            'MergePending',
            'MergeCompleted'
        )]
        [string]$Phase
    )

    $journalTempPath = "$($Transaction.JournalPath).tmp"

    $journalData = [ordered]@{
        SchemaVersion = 1
        RepositoryPath = $RepositoryPath
        ExpectedBranch = $ExpectedBranch
        RemoteName = $RemoteName
        RemoteBranch = $RemoteBranch
        LocalSha = $Transaction.LocalSha
        RemoteSha = $Transaction.RemoteSha
        RemoteTrackingRef = $Transaction.RemoteTrackingRef
        Phase = $Phase
        TransactionPath = $Transaction.TransactionPath
        CandidatePath = $Transaction.CandidatePath
        CandidatePyprojectPath = $Transaction.CandidatePyprojectPath
        CandidateLockPath = $Transaction.CandidateLockPath
        BackupPath = $Transaction.BackupPath
        VenvPath = $Transaction.VenvPath
        SyncUvExecutable = $Transaction.SyncUvExecutable
        OverrideForUv = $Transaction.OverrideForUv
        OriginalSnapshot = $Transaction.OriginalSnapshot
        UpdatedAtUtc = [DateTime]::UtcNow.ToString('o')
    }

    $jsonParameters = @{
        InputObject = $journalData
        Depth = 8
    }

    $journalJson = ConvertTo-Json @jsonParameters
    $utf8Encoding = [System.Text.UTF8Encoding]::new($false)
    $journalStream = [System.IO.FileStream]::new(
        $journalTempPath,
        [System.IO.FileMode]::Create,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    $journalWriter = [System.IO.StreamWriter]::new(
        $journalStream,
        $utf8Encoding,
        4096,
        $true
    )

    try {
        $journalWriter.Write($journalJson)
        $journalWriter.Flush()
        $journalStream.Flush($true)
    }
    finally {
        $journalWriter.Dispose()
        $journalStream.Dispose()
    }

    [System.IO.File]::Move(
        $journalTempPath,
        $Transaction.JournalPath,
        $true
    )

    $Transaction |
        Add-Member -NotePropertyName Phase -NotePropertyValue $Phase -Force

    Write-UpdateLog -Level 'INFO' -Message "Журнал транзакции зависимостей: фаза=$Phase, путь=$($Transaction.JournalPath)"
}

function Read-DependencyJournal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$LiteralPath
    )

    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) {
        throw "Журнал транзакции зависимостей не найден: $LiteralPath"
    }

    try {
        $journalText = Get-Content -LiteralPath $LiteralPath -Raw -ErrorAction Stop
        return ConvertFrom-Json -InputObject $journalText -Depth 8 -ErrorAction Stop
    }
    catch {
        throw "Журнал транзакции зависимостей повреждён: $LiteralPath. $($_.Exception.Message)"
    }
}

function Assert-DependencyJournal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Journal,

        [Parameter(Mandatory)]
        [string]$JournalPath
    )

    [string[]]$requiredProperties = @(
        'SchemaVersion'
        'RepositoryPath'
        'ExpectedBranch'
        'RemoteName'
        'RemoteBranch'
        'LocalSha'
        'RemoteSha'
        'RemoteTrackingRef'
        'Phase'
        'TransactionPath'
        'CandidatePath'
        'CandidatePyprojectPath'
        'CandidateLockPath'
        'BackupPath'
        'VenvPath'
        'SyncUvExecutable'
        'OverrideForUv'
        'OriginalSnapshot'
    )

    [string[]]$journalPropertyNames = @(
        $Journal.PSObject.Properties.Name
    )

    foreach ($requiredProperty in $requiredProperties) {
        if ($journalPropertyNames -notcontains $requiredProperty) {
            throw "Журнал транзакции зависимостей не содержит обязательное поле: $requiredProperty"
        }
    }

    if ([int]$Journal.SchemaVersion -ne 1) {
        throw "Неподдерживаемая версия журнала транзакции зависимостей: $($Journal.SchemaVersion)"
    }

    [string[]]$allowedPhases = @(
        'CandidateValidated'
        'BackupReady'
        'EnvironmentSynchronized'
        'MergePending'
        'MergeCompleted'
    )

    if ($allowedPhases -notcontains [string]$Journal.Phase) {
        throw "Журнал транзакции зависимостей содержит неизвестную фазу: $($Journal.Phase)"
    }

    if (
        [string]$Journal.LocalSha -notmatch '^[0-9a-fA-F]{40}$' -or
        [string]$Journal.RemoteSha -notmatch '^[0-9a-fA-F]{40}$'
    ) {
        throw 'Журнал транзакции зависимостей содержит некорректный SHA коммита.'
    }

    $normalizedRepositoryPath = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetFullPath($RepositoryPath)
    )
    $normalizedJournalRepositoryPath = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetFullPath([string]$Journal.RepositoryPath)
    )

    $repositoryMatches = [string]::Equals(
        $normalizedRepositoryPath,
        $normalizedJournalRepositoryPath,
        [System.StringComparison]::OrdinalIgnoreCase
    )

    if (-not $repositoryMatches) {
        throw "Журнал транзакции зависимостей относится к другому репозиторию: $($Journal.RepositoryPath)"
    }

    if ([string]$Journal.ExpectedBranch -ne $ExpectedBranch) {
        throw "Журнал транзакции зависимостей относится к другой ветке: $($Journal.ExpectedBranch)"
    }

    if (
        [string]$Journal.RemoteName -ne $RemoteName -or
        [string]$Journal.RemoteBranch -ne $RemoteBranch
    ) {
        throw 'Журнал транзакции зависимостей относится к другому удалённому источнику.'
    }

    $expectedRemoteTrackingRef = "refs/remotes/$RemoteName/$RemoteBranch"

    if ([string]$Journal.RemoteTrackingRef -ne $expectedRemoteTrackingRef) {
        throw "Журнал транзакции зависимостей содержит неожиданную отслеживаемую удалённую ссылку Git: $($Journal.RemoteTrackingRef)"
    }

    $transactionPath = [System.IO.Path]::GetFullPath(
        [string]$Journal.TransactionPath
    )
    $transactionParentPath = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetDirectoryName($transactionPath)
    )
    $normalizedWorkRoot = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetFullPath($script:ResolvedDependencyWorkRoot)
    )

    $transactionParentMatches = [string]::Equals(
        $transactionParentPath,
        $normalizedWorkRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )

    if (-not $transactionParentMatches) {
        throw "Журнал транзакции зависимостей указывает транзакцию вне рабочего корневого каталога: $transactionPath"
    }

    $expectedJournalPath = Join-Path -Path $transactionPath -ChildPath 'journal.json'
    $normalizedExpectedJournalPath = [System.IO.Path]::GetFullPath(
        $expectedJournalPath
    )
    $normalizedJournalPath = [System.IO.Path]::GetFullPath($JournalPath)

    $journalPathMatches = [string]::Equals(
        $normalizedExpectedJournalPath,
        $normalizedJournalPath,
        [System.StringComparison]::OrdinalIgnoreCase
    )

    if (-not $journalPathMatches) {
        throw 'Путь журнала транзакции зависимостей не соответствует каталогу транзакции.'
    }

    [string[]]$transactionPaths = @(
        [string]$Journal.CandidatePath
        [string]$Journal.CandidatePyprojectPath
        [string]$Journal.CandidateLockPath
        [string]$Journal.BackupPath
        [string]$Journal.SyncUvExecutable
    )

    foreach ($transactionChildPath in $transactionPaths) {
        if (-not (Test-PathInside -CandidatePath $transactionChildPath -ParentPath $transactionPath)) {
            throw "Журнал транзакции зависимостей содержит путь вне каталога транзакции: $transactionChildPath"
        }
    }

    $expectedVenvPath = Join-Path -Path $RepositoryPath -ChildPath '.venv'
    $normalizedExpectedVenvPath = [System.IO.Path]::GetFullPath($expectedVenvPath)
    $normalizedJournalVenvPath = [System.IO.Path]::GetFullPath(
        [string]$Journal.VenvPath
    )

    $venvPathMatches = [string]::Equals(
        $normalizedExpectedVenvPath,
        $normalizedJournalVenvPath,
        [System.StringComparison]::OrdinalIgnoreCase
    )

    if (-not $venvPathMatches) {
        throw "Журнал транзакции зависимостей указывает неожиданную .venv: $($Journal.VenvPath)"
    }

    [string[]]$requiredFiles = @(
        [string]$Journal.CandidatePyprojectPath
        [string]$Journal.CandidateLockPath
        [string]$Journal.SyncUvExecutable
    )

    foreach ($requiredFile in $requiredFiles) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "Журнал транзакции зависимостей ссылается на отсутствующий файл: $requiredFile"
        }
    }

    if (
        [string]$Journal.Phase -ne 'CandidateValidated' -and
        -not (Test-Path -LiteralPath ([string]$Journal.BackupPath) -PathType Container)
    ) {
        throw "Журнал транзакции зависимостей не имеет резервной копии .venv: $($Journal.BackupPath)"
    }
}

function Get-DependencyTransactionFromJournal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Journal,

        [Parameter(Mandatory)]
        [string]$JournalPath
    )

    return [pscustomobject]@{
        TransactionPath = [string]$Journal.TransactionPath
        CandidatePath = [string]$Journal.CandidatePath
        CandidatePyprojectPath = [string]$Journal.CandidatePyprojectPath
        CandidateLockPath = [string]$Journal.CandidateLockPath
        BackupPath = [string]$Journal.BackupPath
        OverridePath = [string]$Journal.OverrideForUv
        JournalPath = $JournalPath
        SyncUvExecutable = [string]$Journal.SyncUvExecutable
        VenvPath = [string]$Journal.VenvPath
        OverrideForUv = [string]$Journal.OverrideForUv
        OriginalSnapshot = $Journal.OriginalSnapshot
        LocalSha = [string]$Journal.LocalSha
        RemoteSha = [string]$Journal.RemoteSha
        RemoteTrackingRef = [string]$Journal.RemoteTrackingRef
        Phase = [string]$Journal.Phase
    }
}

function Test-DependencyEnvironmentState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction
    )

    $verifyResult = Invoke-UvIsolated -ProjectEnvironment $Transaction.VenvPath -OverridePath $Transaction.OverrideForUv -ExecutablePath $Transaction.SyncUvExecutable -Arguments @(
        '--no-config'
        '--no-python-downloads'
        'sync'
        '--dry-run'
        '--frozen'
        '--inexact'
        '--no-dev'
        '--no-install-project'
        '--default-index'
        'https://pypi.org/simple'
        '--index-strategy'
        'first-index'
        '--project'
        $Transaction.CandidatePath
        '--python'
        $script:PythonExecutable
    )

    if ($verifyResult.ExitCode -ne 0) {
        Write-NativeOutput -Result $verifyResult -Level 'ERROR'
        throw 'Проверка синхронизированной .venv завершилась ошибкой.'
    }

    $verifyText = $verifyResult.Output -join [Environment]::NewLine

    if (-not $verifyText.Contains(
        'Would make no changes',
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        Write-NativeOutput -Result $verifyResult -Level 'ERROR'
        throw 'Пробный запуск не подтвердил стабильное состояние .venv.'
    }

    Write-NativeOutput -Result $verifyResult
}

function Invoke-TestFailPoint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateSet(
            'AfterBackup',
            'AfterSync',
            'AfterMerge'
        )]
        [string]$Phase,

        [Parameter(Mandatory)]
        [object]$Transaction
    )

    if ($TestFailPoint -ne $Phase) {
        return
    }

    if (-not [string]::IsNullOrWhiteSpace($TestMutationRelativePath)) {
        if ([System.IO.Path]::IsPathFullyQualified($TestMutationRelativePath)) {
            throw 'TestMutationRelativePath должен быть относительным путём.'
        }

        $mutationPath = [System.IO.Path]::GetFullPath(
            (
                Join-Path -Path $Transaction.VenvPath -ChildPath $TestMutationRelativePath
            )
        )

        if (-not (Test-PathInside -CandidatePath $mutationPath -ParentPath $Transaction.VenvPath)) {
            throw 'TestMutationRelativePath выходит за пределы .venv.'
        }

        $mutationParent = [System.IO.Path]::GetDirectoryName($mutationPath)

        if (-not (Test-Path -LiteralPath $mutationParent -PathType Container)) {
            New-Item -ItemType Directory -Path $mutationParent -Force -ErrorAction Stop | Out-Null
        }

        Set-Content -LiteralPath $mutationPath -Value "failpoint=$Phase" -Encoding utf8NoBOM -ErrorAction Stop
    }

    $exitCode = switch ($Phase) {
        'AfterBackup' {
            91
        }

        'AfterSync' {
            92
        }

        'AfterMerge' {
            93
        }
    }

    Write-UpdateLog -Level 'WARN' -Message "TEST FAILPOINT: $Phase, exit=$exitCode"
    [Environment]::Exit($exitCode)
}

function Invoke-OrphanDependencyTransactionCleanup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$LiteralPath
    )

    $item = Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop

    if (-not $item.PSIsContainer) {
        throw "Неожиданный объект в рабочем корневом каталоге транзакций зависимостей: $LiteralPath"
    }

    if ($item.Name -notmatch '^dependency-[0-9a-fA-F]{40}-[0-9a-fA-F]{32}$') {
        throw "Неизвестный каталог в рабочем корневом каталоге транзакций зависимостей: $LiteralPath"
    }

    Remove-Item -LiteralPath $LiteralPath -Recurse -Force -ErrorAction Stop
    Write-UpdateLog -Level 'INFO' -Message "Удалена потерянная транзакция зависимостей: $LiteralPath"
}

function Invoke-DependencyRecovery {
    [CmdletBinding()]
    param()

    if (-not (Test-Path -LiteralPath $script:ResolvedDependencyWorkRoot -PathType Container)) {
        return
    }

    [object[]]$transactionDirectories = @(
        Get-ChildItem -LiteralPath $script:ResolvedDependencyWorkRoot -Directory -Force -ErrorAction Stop
    )

    [object[]]$journalItems = @(
        foreach ($transactionDirectory in $transactionDirectories) {
            $journalPath = Join-Path -Path $transactionDirectory.FullName -ChildPath 'journal.json'

            if (Test-Path -LiteralPath $journalPath -PathType Leaf) {
                Get-Item -LiteralPath $journalPath -Force -ErrorAction Stop
            }
        }
    )

    if ($journalItems.Count -gt 1) {
        throw "Обнаружено несколько незавершённых транзакций зависимостей: $($journalItems.Count)"
    }

    foreach ($transactionDirectory in $transactionDirectories) {
        $journalPath = Join-Path -Path $transactionDirectory.FullName -ChildPath 'journal.json'

        if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) {
            Invoke-OrphanDependencyTransactionCleanup -LiteralPath $transactionDirectory.FullName
        }
    }

    if ($journalItems.Count -eq 0) {
        return
    }

    $journalItem = $journalItems[0]
    $journal = Read-DependencyJournal -LiteralPath $journalItem.FullName
    Assert-DependencyJournal -Journal $journal -JournalPath $journalItem.FullName

    $transaction = Get-DependencyTransactionFromJournal -Journal $journal -JournalPath $journalItem.FullName

    $currentHead = Get-SingleGitValue -Operation 'проверка HEAD для восстановления транзакции зависимостей' -Arguments @(
        'rev-parse'
        'HEAD'
    )

    Write-UpdateLog -Level 'WARN' -Message "Обнаружена незавершённая транзакция зависимостей. Фаза=$($transaction.Phase), HEAD=$currentHead"

    switch ([string]$transaction.Phase) {
        'CandidateValidated' {
            if ($currentHead -ne $transaction.LocalSha) {
                throw 'Журнал с фазой CandidateValidated имеет неожиданный HEAD. Автоматическое восстановление запрещено.'
            }

            Clear-DependencyTransaction -Transaction $transaction
            Write-UpdateLog -Level 'INFO' -Message 'Незавершённая транзакция не изменяла .venv и была удалена.'
        }

        'BackupReady' {
            if ($currentHead -eq $transaction.LocalSha) {
                Restore-DependencyEnvironment -Transaction $transaction
                Clear-DependencyTransaction -Transaction $transaction
                Write-UpdateLog -Level 'INFO' -Message 'Восстановление вернуло .venv к старому HEAD.'
            }
            elseif ($currentHead -eq $transaction.RemoteSha) {
                Test-DependencyEnvironmentState -Transaction $transaction
                Clear-DependencyTransaction -Transaction $transaction
                Write-UpdateLog -Level 'INFO' -Message 'Восстановление подтвердило новый HEAD и сохранило синхронизированную .venv.'
            }
            else {
                throw 'Журнал с фазой BackupReady имеет неоднозначный HEAD.'
            }
        }

        'EnvironmentSynchronized' {
            if ($currentHead -eq $transaction.LocalSha) {
                Restore-DependencyEnvironment -Transaction $transaction
                Clear-DependencyTransaction -Transaction $transaction
                Write-UpdateLog -Level 'INFO' -Message 'Восстановление откатило .venv после прерывания перед fast-forward.'
            }
            elseif ($currentHead -eq $transaction.RemoteSha) {
                Test-DependencyEnvironmentState -Transaction $transaction
                Clear-DependencyTransaction -Transaction $transaction
                Write-UpdateLog -Level 'INFO' -Message 'Восстановление подтвердило новый HEAD после завершённого fast-forward.'
            }
            else {
                throw 'Журнал с фазой EnvironmentSynchronized имеет неоднозначный HEAD.'
            }
        }

        'MergePending' {
            if ($currentHead -eq $transaction.LocalSha) {
                Restore-DependencyEnvironment -Transaction $transaction
                Clear-DependencyTransaction -Transaction $transaction
                Write-UpdateLog -Level 'INFO' -Message 'Восстановление откатило .venv после прерывания перед fast-forward.'
            }
            elseif ($currentHead -eq $transaction.RemoteSha) {
                Test-DependencyEnvironmentState -Transaction $transaction
                Clear-DependencyTransaction -Transaction $transaction
                Write-UpdateLog -Level 'INFO' -Message 'Восстановление подтвердило завершённый fast-forward.'
            }
            else {
                throw 'Журнал с фазой MergePending имеет неоднозначный HEAD.'
            }
        }

        'MergeCompleted' {
            if ($currentHead -ne $transaction.RemoteSha) {
                throw 'Журнал с фазой MergeCompleted не совпадает с текущим HEAD.'
            }

            Test-DependencyEnvironmentState -Transaction $transaction
            Clear-DependencyTransaction -Transaction $transaction
            Write-UpdateLog -Level 'INFO' -Message 'Восстановление завершило очистку после подтверждённого fast-forward.'
        }
    }
}

function Initialize-DependencyEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$LocalSha,

        [Parameter(Mandatory)]
        [string]$RemoteSha,

        [Parameter(Mandatory)]
        [string]$RemoteTrackingRef
    )

    if (-not $IsWindows) {
        throw 'Транзакционная синхронизация зависимостей реализована только для Windows.'
    }

    $venvPath = Join-Path -Path $RepositoryPath -ChildPath '.venv'

    if (-not (Test-Path -LiteralPath $venvPath -PathType Container)) {
        throw "Рабочая .venv не найдена: $venvPath"
    }

    $transaction = Initialize-DependencyCandidate -RemoteSha $RemoteSha

    try {
        $transactionUvPath = Join-Path -Path $transaction.TransactionPath -ChildPath 'uv.exe'
        Copy-Item -LiteralPath $script:UvExecutable -Destination $transactionUvPath -Force -ErrorAction Stop

        $sourceUvHash = (
            Get-FileHash -LiteralPath $script:UvExecutable -Algorithm SHA256 -ErrorAction Stop
        ).Hash
        $transactionUvHash = (
            Get-FileHash -LiteralPath $transactionUvPath -Algorithm SHA256 -ErrorAction Stop
        ).Hash

        if ($sourceUvHash -ne $transactionUvHash) {
            throw 'Транзакционная копия uv.exe не прошла проверку SHA-256.'
        }

        $transaction | Add-Member -NotePropertyName SyncUvExecutable -NotePropertyValue $transactionUvPath

        Assert-CandidateDependencySource -Transaction $transaction

        $overrideFileInfo = Get-Item -LiteralPath $transaction.OverridePath -ErrorAction Stop
        $effectiveOverridePath = if ($overrideFileInfo.Length -gt 0) {
            $transaction.OverridePath
        } else {
            ''
        }

        $lockCheckResult = Invoke-UvIsolated -OverridePath $effectiveOverridePath -ExecutablePath $transaction.SyncUvExecutable -Arguments @(
            '--no-config'
            '--no-python-downloads'
            'lock'
            '--check'
            '--default-index'
            'https://pypi.org/simple'
            '--index-strategy'
            'first-index'
            '--project'
            $transaction.CandidatePath
            '--python'
            $script:PythonExecutable
        )

        if ($lockCheckResult.ExitCode -ne 0) {
            Write-NativeOutput -Result $lockCheckResult -Level 'ERROR'
            throw 'Кандидатный uv.lock не соответствует кандидатному pyproject.toml.'
        }

        Write-NativeOutput -Result $lockCheckResult

        $venvSnapshot = Get-DirectorySnapshot -LiteralPath $venvPath

        $transaction | Add-Member -NotePropertyName VenvPath -NotePropertyValue $venvPath
        $transaction | Add-Member -NotePropertyName OriginalSnapshot -NotePropertyValue $venvSnapshot
        $transaction | Add-Member -NotePropertyName OverrideForUv -NotePropertyValue $effectiveOverridePath
        $transaction | Add-Member -NotePropertyName LocalSha -NotePropertyValue $LocalSha
        $transaction | Add-Member -NotePropertyName RemoteSha -NotePropertyValue $RemoteSha
        $transaction | Add-Member -NotePropertyName RemoteTrackingRef -NotePropertyValue $RemoteTrackingRef

        Write-DependencyJournal -Transaction $transaction -Phase 'CandidateValidated'

        $backupDriveRoot = [System.IO.Path]::GetPathRoot($transaction.BackupPath)
        $backupDrive = [System.IO.DriveInfo]::new($backupDriveRoot)
        $requiredFreeBytes = $venvSnapshot.TotalBytes + 536870912

        if ($backupDrive.AvailableFreeSpace -lt $requiredFreeBytes) {
            throw (
                @(
                    'Недостаточно свободного места для резервной копии .venv.'
                    "Требуется минимум: $requiredFreeBytes"
                    "Доступно: $($backupDrive.AvailableFreeSpace)"
                ) -join [Environment]::NewLine
            )
        }

        Write-UpdateLog -Level 'INFO' -Message "Создание резервной копии .venv: $($transaction.BackupPath)"
        Invoke-RobocopyMirror -SourcePath $venvPath -DestinationPath $transaction.BackupPath -Operation 'Резервное копирование .venv'

        $backupSnapshot = Get-DirectorySnapshot -LiteralPath $transaction.BackupPath
        Assert-SnapshotEqual -Expected $venvSnapshot -Actual $backupSnapshot -Context 'Проверка резервной копии .venv'

        Write-DependencyJournal -Transaction $transaction -Phase 'BackupReady'
        Invoke-TestFailPoint -Phase 'AfterBackup' -Transaction $transaction

        Write-UpdateLog -Level 'INFO' -Message 'Синхронизация рабочей .venv по кандидатному файлу блокировки.'

        $syncResult = Invoke-UvIsolated -ProjectEnvironment $venvPath -OverridePath $effectiveOverridePath -ExecutablePath $transaction.SyncUvExecutable -Arguments @(
            '--no-config'
            '--no-python-downloads'
            'sync'
            '--frozen'
            '--inexact'
            '--no-dev'
            '--no-install-project'
            '--default-index'
            'https://pypi.org/simple'
            '--index-strategy'
            'first-index'
            '--project'
            $transaction.CandidatePath
            '--python'
            $script:PythonExecutable
        )

        if ($syncResult.ExitCode -ne 0) {
            Write-NativeOutput -Result $syncResult -Level 'ERROR'
            throw 'Синхронизация рабочей .venv завершилась ошибкой.'
        }

        Write-NativeOutput -Result $syncResult
        Test-DependencyEnvironmentState -Transaction $transaction
        Write-DependencyJournal -Transaction $transaction -Phase 'EnvironmentSynchronized'
        Invoke-TestFailPoint -Phase 'AfterSync' -Transaction $transaction

        return $transaction
    } catch {
        $transactionPropertyNames = if ($null -ne $transaction) {
            @(
                $transaction.PSObject.Properties.Name
            )
        } else {
            @()
        }

        $canRestore = (
            $null -ne $transaction -and
            $transactionPropertyNames -contains 'VenvPath' -and
            $transactionPropertyNames -contains 'OriginalSnapshot' -and
            (Test-Path -LiteralPath $transaction.BackupPath -PathType Container)
        )

        $recoverySucceeded = $false

        if ($canRestore) {
            try {
                Restore-DependencyEnvironment -Transaction $transaction
                $recoverySucceeded = $true
            }
            catch {
                Write-UpdateLog -Level 'ERROR' -Message "Автоматический откат .venv не удался: $($_.Exception.Message)"
            }
        }
        else {
            $recoverySucceeded = $true
        }

        if ($recoverySucceeded -and $null -ne $transaction) {
            try {
                Clear-DependencyTransaction -Transaction $transaction
            }
            catch {
                Write-UpdateLog -Level 'WARN' -Message "Не удалось удалить безопасно завершённую транзакцию зависимостей: $($_.Exception.Message)"
            }
        }

        throw
    }
}

function Restore-DependencyEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction
    )

    if (-not (Test-Path -LiteralPath $Transaction.BackupPath -PathType Container)) {
        throw "Резервная копия .venv не найдена: $($Transaction.BackupPath)"
    }

    Write-UpdateLog -Level 'WARN' -Message 'Выполняется точечный откат рабочей .venv.'
    Invoke-RobocopyMirror -SourcePath $Transaction.BackupPath -DestinationPath $Transaction.VenvPath -Operation 'Восстановление .venv'

    $restoredSnapshot = Get-DirectorySnapshot -LiteralPath $Transaction.VenvPath
    Assert-SnapshotEqual -Expected $Transaction.OriginalSnapshot -Actual $restoredSnapshot -Context 'Проверка восстановленной .venv'

    Write-UpdateLog -Level 'INFO' -Message 'Рабочая .venv восстановлена из резервной копии.'
}

function Clear-DependencyTransaction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Transaction
    )

    if (
        $null -ne $Transaction -and
        (Test-Path -LiteralPath $Transaction.TransactionPath)
    ) {
        Remove-Item -LiteralPath $Transaction.TransactionPath -Recurse -Force -ErrorAction Stop
        Write-UpdateLog -Level 'INFO' -Message 'Временная транзакция зависимостей удалена.'
    }
}

function Invoke-PostgreSqlOperation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [Parameter(Mandatory)]
        [string]$Operation,

        [Parameter()]
        [string]$FailureGuidance = ''
    )

    Push-Location -LiteralPath $RepositoryPath

    try {
        $result = Invoke-NativeCommand -Executable $script:PythonExecutable -Arguments @(
            '-X'
            'utf8'
            '-m'
            'dev_tools.postgresql_runtime'
            $Arguments
        )
    }
    finally {
        Pop-Location
    }

    if ($result.ExitCode -ne 0) {
        Write-NativeOutput -Result $result -Level 'ERROR'
        $failureMessage = 'Операция PostgreSQL «{0}» завершилась ошибкой.' -f $Operation
        if (-not [string]::IsNullOrWhiteSpace($FailureGuidance)) {
            $failureMessage = '{0} {1}' -f $failureMessage, $FailureGuidance
        }
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message $failureMessage
    }

    Write-UpdateLog -Level 'INFO' -Message ('PostgreSQL: {0}.' -f $Operation)
}

function Backup-ProductionPostgreSql {
    [CmdletBinding()]
    param()

    $baseDirectory = $env:LOCALAPPDATA

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        $baseDirectory = $env:TEMP
    }

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'Не удалось определить внешний каталог резервных копий PostgreSQL.'
    }

    $backupDirectory = if ([string]::IsNullOrWhiteSpace($script:PostgreSqlBackupRootParameter)) {
        Join-Path -Path $baseDirectory -ChildPath 'AzurPilot\backups\postgresql'
    }
    else {
        [System.IO.Path]::GetFullPath($script:PostgreSqlBackupRootParameter)
    }

    $repositoryRoot = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetFullPath($RepositoryPath)
    )
    $backupRoot = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetFullPath($backupDirectory)
    )
    $comparison = [System.StringComparison]::OrdinalIgnoreCase

    if (
        $backupRoot.Equals($repositoryRoot, $comparison) -or
        $backupRoot.StartsWith($repositoryRoot + [System.IO.Path]::DirectorySeparatorChar, $comparison)
    ) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'Каталог резервных копий PostgreSQL должен находиться вне репозитория.'
    }

    $backupDirectoryCreated = -not (Test-Path -LiteralPath $backupDirectory)
    New-Item -ItemType Directory -Path $backupDirectory -Force -ErrorAction Stop | Out-Null
    $backupItem = Get-Item -LiteralPath $backupDirectory -Force -ErrorAction Stop

    if ($backupItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'Каталог резервных копий PostgreSQL не может быть ссылкой или точкой повторного анализа.'
    }

    Protect-PostgreSqlBackupDirectory -Path $backupDirectory -CreatedByUpdater $backupDirectoryCreated
    $backupName = 'azurpilot-before-update-{0}.dump' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    $backupPath = Join-Path -Path $backupDirectory -ChildPath $backupName
    Invoke-PostgreSqlOperation -Arguments @(
        'backup'
        '--output'
        $backupPath
        '--transport'
        'docker'
    ) -Operation 'резервная копия перед обновлением создана и проверена'

    if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'Файл резервной копии PostgreSQL не создан.'
    }

    $backupFile = Get-Item -LiteralPath $backupPath -Force -ErrorAction Stop

    if ($backupFile.Length -le 0) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'Файл резервной копии PostgreSQL пуст.'
    }

    Write-UpdateLog -Level 'INFO' -Message ('Резервная копия PostgreSQL: {0}' -f $backupPath)
    return $backupPath
}

function Protect-PostgreSqlBackupDirectory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [bool]$CreatedByUpdater
    )

    if (-not $IsWindows) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'Защита каталога резервных копий PostgreSQL поддерживается только в Windows.'
    }

    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $directory = [System.IO.DirectoryInfo]::new($Path)
    $ownershipSecurity = [System.IO.FileSystemAclExtensions]::GetAccessControl(
        $directory,
        [System.Security.AccessControl.AccessControlSections]::Owner
    )
    $currentOwner = $ownershipSecurity.GetOwner(
        [System.Security.Principal.SecurityIdentifier]
    )

    if ($currentOwner -ne $identity.User -and -not $CreatedByUpdater) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'Текущий пользователь не является владельцем каталога резервных копий PostgreSQL.'
    }

    $security = [System.Security.AccessControl.DirectorySecurity]::new()
    $security.SetOwner($identity.User)
    $security.SetAccessRuleProtection($true, $false)
    $inheritance = (
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $identity.User,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        $inheritance,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    [void]$security.AddAccessRule($rule)
    [System.IO.FileSystemAclExtensions]::SetAccessControl($directory, $security)
}

function Invoke-ProductionPostgreSqlSchemaUpgrade {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$BackupPath
    )

    $guidance = (
        'Остановите AzurPilot. Проверенный внешний дамп: {0}. ' +
        'Остановите Docker Compose PostgreSQL, восстановите дамп только в именованный target volume, ' +
        'затем повторите проверку Docker Compose, marker, schema head и app health.'
    ) -f $BackupPath
    Invoke-PostgreSqlOperation -Arguments @('upgrade') -Operation 'Alembic upgrade применён от имени migrator' -FailureGuidance $guidance
    Invoke-PostgreSqlOperation -Arguments @('health') -Operation 'schema head и доступ app-роли проверены' -FailureGuidance $guidance
}

try {
    Initialize-UpdateLog

    Write-UpdateLog -Level 'INFO' -Message 'Запуск контролируемого обновления AzurPilot.'
    Write-UpdateLog -Level 'INFO' -Message "Репозиторий: $RepositoryPath"
    Write-UpdateLog -Level 'INFO' -Message "Разрешённый источник: $RemoteName/$RemoteBranch"

    $gitCommand = Get-Command -Name 'git' -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    $script:GitExecutable = $gitCommand.Path

    if (-not (Test-Path -LiteralPath $RepositoryPath -PathType Container)) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message "Каталог репозитория не существует: $RepositoryPath"
    }

    $RepositoryPath = (Resolve-Path -LiteralPath $RepositoryPath -ErrorAction Stop).Path

    $defaultUvPath = Join-Path -Path $RepositoryPath -ChildPath '.venv\Scripts\uv.exe'
    $defaultPythonPath = Join-Path -Path $RepositoryPath -ChildPath '.venv\Scripts\python.exe'
    $defaultRobocopyPath = Join-Path -Path $env:SystemRoot -ChildPath 'System32\robocopy.exe'
    $defaultTarPath = Join-Path -Path $env:SystemRoot -ChildPath 'System32\tar.exe'

    $script:UvExecutable = Resolve-ApplicationPath -ConfiguredPath $UvExecutablePath -DefaultPath $defaultUvPath -DisplayName 'uv'
    $script:PythonExecutable = Resolve-ApplicationPath -ConfiguredPath $PythonExecutablePath -DefaultPath $defaultPythonPath -DisplayName 'Python'
    $script:RobocopyExecutable = Resolve-ApplicationPath -ConfiguredPath $RobocopyExecutablePath -DefaultPath $defaultRobocopyPath -DisplayName 'robocopy'
    $script:TarExecutable = Resolve-ApplicationPath -ConfiguredPath $TarExecutablePath -DefaultPath $defaultTarPath -DisplayName 'tar'

    if ([string]::IsNullOrWhiteSpace($DependencyWorkRoot)) {
        $baseWorkDirectory = $env:LOCALAPPDATA

        if ([string]::IsNullOrWhiteSpace($baseWorkDirectory)) {
            $baseWorkDirectory = $env:TEMP
        }

        if ([string]::IsNullOrWhiteSpace($baseWorkDirectory)) {
            Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'Не удалось определить каталог транзакций зависимостей.'
        }

        $script:ResolvedDependencyWorkRoot = Join-Path -Path $baseWorkDirectory -ChildPath 'AzurPilot\dependency-transactions'
    } else {
        $script:ResolvedDependencyWorkRoot = [System.IO.Path]::GetFullPath($DependencyWorkRoot)
    }

    if (Test-PathInside -CandidatePath $script:ResolvedDependencyWorkRoot -ParentPath $RepositoryPath) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'Каталог транзакций зависимостей должен находиться вне репозитория.'
    }

    $requiredFiles = @(
        'gui.py'
        'pyproject.toml'
        'uv.lock'
    )

    foreach ($relativePath in $requiredFiles) {
        $fullPath = Join-Path -Path $RepositoryPath -ChildPath $relativePath

        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            Complete-Update -Code $script:ExitCodePreconditionFailure -Message "Отсутствует обязательный файл: $fullPath"
        }
    }

    $gitTopLevel = Get-SingleGitValue -Operation 'определение корня репозитория' -Arguments @(
        'rev-parse'
        '--show-toplevel'
    )

    $normalizedGitTopLevel = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetFullPath($gitTopLevel)
    )
    $normalizedRepositoryPath = [System.IO.Path]::TrimEndingDirectorySeparator(
        [System.IO.Path]::GetFullPath($RepositoryPath)
    )

    $pathsEqual = [string]::Equals(
        $normalizedGitTopLevel,
        $normalizedRepositoryPath,
        [System.StringComparison]::OrdinalIgnoreCase
    )

    if (-not $pathsEqual) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message "Указанный путь не является корнем Git-репозитория. Корень: $gitTopLevel"
    }

    $currentBranch = Get-SingleGitValue -Operation 'определение текущей ветки' -Arguments @(
        'branch'
        '--show-current'
    )

    if ($currentBranch -ne $ExpectedBranch) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message "Ожидалась ветка $ExpectedBranch, но активна $currentBranch."
    }

    $originUrl = Get-SingleGitValue -Operation 'проверка URL origin' -Arguments @(
        'remote'
        'get-url'
        $RemoteName
    )

    if ($originUrl -ne $ExpectedOriginUrl) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message "Неверный URL $RemoteName. Ожидалось: $ExpectedOriginUrl. Получено: $originUrl"
    }

    if (
        $TestFailPoint -ne 'None' -and
        -not [System.IO.Path]::IsPathFullyQualified($originUrl)
    ) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'TestFailPoint разрешён только для локального тестового источника origin.'
    }

    if (
        $TestFailPoint -eq 'None' -and
        -not [string]::IsNullOrWhiteSpace($TestMutationRelativePath)
    ) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'TestMutationRelativePath требует активный TestFailPoint.'
    }

    $upstreamPushUrl = Get-SingleGitValue -Operation 'проверка push URL upstream' -Arguments @(
        'remote'
        'get-url'
        '--push'
        'upstream'
    )

    if ($upstreamPushUrl -ne $RequiredUpstreamPushUrl) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message "URL отправки upstream должен быть $RequiredUpstreamPushUrl. Получено: $upstreamPushUrl"
    }

    $trackingBranch = Get-SingleGitValue -Operation 'проверка отслеживаемой ветки' -Arguments @(
        'rev-parse'
        '--abbrev-ref'
        '--symbolic-full-name'
        '@{upstream}'
    )

    $expectedTrackingBranch = "$RemoteName/$RemoteBranch"

    if ($trackingBranch -ne $expectedTrackingBranch) {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message "Ветка должна отслеживать $expectedTrackingBranch. Получено: $trackingBranch"
    }

    Assert-NoActiveGitOperation
    Assert-AzurPilotStopped

    try {
        Invoke-DependencyRecovery
    }
    catch {
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message "Восстановление транзакции зависимостей заблокировано. $($_.Exception.Message)"
    }

    $statusResult = Invoke-Git -Arguments @(
        'status'
        '--porcelain=v1'
        '--untracked-files=all'
    )

    if ($statusResult.ExitCode -ne 0) {
        Write-NativeOutput -Result $statusResult -Level 'ERROR'
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'Не удалось проверить состояние рабочего дерева.'
    }

    if ($statusResult.Output.Count -gt 0) {
        Write-NativeOutput -Result $statusResult -Level 'ERROR'
        Complete-Update -Code $script:ExitCodePreconditionFailure -Message 'Рабочее дерево содержит изменения. Обновление отменено.'
    }

    $localSha = Get-SingleGitValue -Operation 'определение локального коммита' -Arguments @(
        'rev-parse'
        'HEAD'
    )

    Write-UpdateLog -Level 'INFO' -Message "Локальный коммит до получения изменений: $localSha"

    $remoteTrackingRef = "refs/remotes/$RemoteName/$RemoteBranch"
    $fetchRefspec = "refs/heads/${RemoteBranch}:$remoteTrackingRef"

    $fetchResult = Invoke-Git -Arguments @(
        'fetch'
        '--no-tags'
        $RemoteName
        $fetchRefspec
    )

    if ($fetchResult.ExitCode -ne 0) {
        Write-NativeOutput -Result $fetchResult -Level 'WARN'
        Complete-Update -Code $script:ExitCodeNetworkFailure -Message 'Не удалось проверить обновления. Локальная ветка не изменена.'
    }

    Write-NativeOutput -Result $fetchResult

    $remoteSha = Get-SingleGitValue -Operation 'определение удалённого коммита' -Arguments @(
        'rev-parse'
        $remoteTrackingRef
    )

    Write-UpdateLog -Level 'INFO' -Message "Удалённый коммит после получения изменений: $remoteSha"

    if ($localSha -eq $remoteSha) {
        Write-UpdateLog -Level 'INFO' -Message 'Результат: установленная версия уже актуальна.'
        Write-ConsoleMessage -Message "Лог: $script:LogPath"
        exit $script:ExitCodeSuccess
    }

    $localIsAncestor = Test-GitAncestor -Ancestor $localSha -Descendant $remoteSha

    if ($localIsAncestor) {
        $postgresqlBackupPath = Backup-ProductionPostgreSql

        $dependencyDiffResult = Invoke-Git -Arguments @(
            'diff'
            '--name-only'
            $localSha
            $remoteSha
            '--'
            'pyproject.toml'
            'uv.lock'
        )

        if ($dependencyDiffResult.ExitCode -ne 0) {
            Write-NativeOutput -Result $dependencyDiffResult -Level 'ERROR'
            Complete-Update -Code $script:ExitCodeUnexpectedFailure -Message 'Не удалось проверить изменения зависимостей.'
        }

        $dependencyTransaction = $null
        $dependenciesChanged = $dependencyDiffResult.Output.Count -gt 0

        if ($dependenciesChanged) {
            Write-NativeOutput -Result $dependencyDiffResult -Level 'WARN'
            Write-UpdateLog -Level 'INFO' -Message 'Обнаружены изменения зависимостей. Начинается транзакционная подготовка .venv.'

            try {
                $dependencyTransaction = Initialize-DependencyEnvironment -LocalSha $localSha -RemoteSha $remoteSha -RemoteTrackingRef $remoteTrackingRef
            } catch {
                Complete-Update -Code $script:ExitCodeDependencyFailure -Message "Подготовка зависимостей завершилась ошибкой. HEAD не изменён. $($_.Exception.Message)"
            }
        }

        if ($null -ne $dependencyTransaction) {
            Write-DependencyJournal -Transaction $dependencyTransaction -Phase 'MergePending'
        }

        Write-UpdateLog -Level 'INFO' -Message 'Доступно безопасное обновление fast-forward.'

        $mergeResult = Invoke-Git -Arguments @(
            'merge'
            '--ff-only'
            $remoteTrackingRef
        )

        if ($mergeResult.ExitCode -ne 0) {
            Write-NativeOutput -Result $mergeResult -Level 'ERROR'
            $headAfterFailedMerge = Get-SingleGitValue -Operation 'проверка HEAD после неудачного обновления fast-forward' -Arguments @(
                'rev-parse'
                'HEAD'
            )

            if (
                $null -ne $dependencyTransaction -and
                $headAfterFailedMerge -eq $localSha
            ) {
                try {
                    Restore-DependencyEnvironment -Transaction $dependencyTransaction
                    Clear-DependencyTransaction -Transaction $dependencyTransaction
                }
                catch {
                    Complete-Update -Code $script:ExitCodeUnexpectedFailure -Message "Обновление fast-forward не выполнено, а откат .venv также завершился ошибкой. $($_.Exception.Message)"
                }
            }

            Complete-Update -Code $script:ExitCodeUnexpectedFailure -Message 'Обновление fast-forward завершилось ошибкой.'
        }

        Write-NativeOutput -Result $mergeResult

        $newHead = Get-SingleGitValue -Operation 'проверка HEAD после обновления fast-forward' -Arguments @(
            'rev-parse'
            'HEAD'
        )

        if ($newHead -ne $remoteSha) {
            Complete-Update -Code $script:ExitCodeUnexpectedFailure -Message "После обновления fast-forward HEAD не совпадает с $remoteTrackingRef."
        }

        Invoke-ProductionPostgreSqlSchemaUpgrade -BackupPath $postgresqlBackupPath

        $statusAfterResult = Invoke-Git -Arguments @(
            'status'
            '--porcelain=v1'
            '--untracked-files=all'
        )

        if ($statusAfterResult.ExitCode -ne 0) {
            Write-NativeOutput -Result $statusAfterResult -Level 'ERROR'
            Complete-Update -Code $script:ExitCodeUnexpectedFailure -Message 'Не удалось проверить рабочее дерево после обновления.'
        }

        if ($statusAfterResult.Output.Count -gt 0) {
            Write-NativeOutput -Result $statusAfterResult -Level 'ERROR'
            Complete-Update -Code $script:ExitCodeUnexpectedFailure -Message 'После обновления fast-forward рабочее дерево стало содержать изменения.'
        }

        if ($null -ne $dependencyTransaction) {
            Invoke-TestFailPoint -Phase 'AfterMerge' -Transaction $dependencyTransaction
            Write-DependencyJournal -Transaction $dependencyTransaction -Phase 'MergeCompleted'

            try {
                Clear-DependencyTransaction -Transaction $dependencyTransaction
            } catch {
                Write-UpdateLog -Level 'WARN' -Message "Обновление успешно, но временную резервную копию не удалось удалить: $($_.Exception.Message)"
            }
        }

        Write-UpdateLog -Level 'INFO' -Message "Результат: обновлено с $localSha до $newHead."

        if ($dependenciesChanged) {
            Write-UpdateLog -Level 'INFO' -Message 'Зависимости: рабочая .venv синхронизирована до обновления fast-forward и проверена.'
        } else {
            Write-UpdateLog -Level 'INFO' -Message 'Зависимости: метаданные не изменялись, синхронизация не требовалась.'
        }

        Write-ConsoleMessage -Message "Лог: $script:LogPath"
        exit $script:ExitCodeSuccess
    }

    $remoteIsAncestor = Test-GitAncestor -Ancestor $remoteSha -Descendant $localSha

    if ($remoteIsAncestor) {
        Complete-Update -Code $script:ExitCodeLocalAhead -Message 'Локальная ветка опережает origin/personal/stable. Автоматическое обновление запрещено.'
    }

    Complete-Update -Code $script:ExitCodeDiverged -Message 'Локальная и удалённая ветки разошлись. Автоматическое обновление запрещено.'
} catch {
    $message = $_.Exception.Message

    try {
        Write-UpdateLog -Level 'ERROR' -Message "Непредвиденная ошибка обновления: $message"
    } catch {
        Write-ConsoleMessage -Message "Непредвиденная ошибка обновления: $message"
    }

    if ($null -ne $script:LogPath) {
        Write-ConsoleMessage -Message "Лог: $script:LogPath"
    }

    exit $script:ExitCodeUnexpectedFailure
}
