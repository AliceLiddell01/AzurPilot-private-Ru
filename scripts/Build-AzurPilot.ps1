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
    [string]$AdbExecutablePath = '',

    [Parameter()]
    [string]$BootstrapCacheRoot = '',

    [Parameter()]
    [string]$ShortcutPath = '',

    [Parameter()]
    [string]$IconPath = '',

    [Parameter()]
    [string]$AllUsersShortcutPath = '',

    [Parameter()]
    [string]$ShortcutBackupRoot = '',

    [Parameter()]
    [switch]$MigrateAllUsersShortcut,

    [Parameter()]
    [switch]$ShortcutOnly,

    [Parameter()]
    [ValidateRange(60, 7200)]
    [int]$SyncTimeoutSeconds = 1800,

    [Parameter()]
    [switch]$NoShortcut
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$script:RepositoryPathParameter = $RepositoryPath
$script:UvExecutablePathParameter = $UvExecutablePath
$script:BootstrapPythonPathParameter = $BootstrapPythonPath
$script:AdbExecutablePathParameter = $AdbExecutablePath
$script:BootstrapCacheRootParameter = $BootstrapCacheRoot
$script:ShortcutPathParameter = $ShortcutPath
$script:IconPathParameter = $IconPath
$script:AllUsersShortcutPathParameter = $AllUsersShortcutPath
$script:ShortcutBackupRootParameter = $ShortcutBackupRoot
$script:MigrateAllUsersShortcutParameter = $MigrateAllUsersShortcut
$script:ShortcutOnlyParameter = $ShortcutOnly
$script:SyncTimeoutSecondsParameter = $SyncTimeoutSeconds
$script:NoShortcutParameter = $NoShortcut

$script:ExpectedUvVersion = '0.11.32'
$script:ExpectedPythonVersion = '3.14.6'
$script:ExpectedAdbVersion = '37.0.0'

$script:UvArchiveUrl = 'https://releases.astral.sh/github/uv/releases/download/0.11.32/uv-x86_64-pc-windows-msvc.zip'
$script:UvArchiveSha256 = 'ACFDE570451CFDB8689FA159A138EE805BA4E241C466432750302C86254B0984'
$script:UvArchiveName = 'uv-x86_64-pc-windows-msvc.zip'

$script:AdbArchiveUrl = 'https://dl.google.com/android/repository/platform-tools_r37.0.0-win.zip'
$script:AdbArchiveSha256 = '4FE305812DB074CEA32903A489D061EB4454CBC90A49E8FEA677F4B7AF764918'
$script:AdbArchiveName = 'platform-tools_r37.0.0-win.zip'

$script:ExitCodeSuccess = 0
$script:ExitCodePreconditionFailure = 20
$script:ExitCodeActiveProcess = 21
$script:ExitCodeConcurrentBuild = 22
$script:ExitCodeBootstrapUnavailable = 23
$script:ExitCodeExistingEnvironmentBroken = 24
$script:ExitCodeDependencyBuildFailure = 25
$script:ExitCodeAdbFailure = 26
$script:ExitCodeConfigFailure = 27
$script:ExitCodeShortcutFailure = 28
$script:ExitCodeElevationRequired = 29
$script:ExitCodeUnexpectedFailure = 30

$script:LogPath = $null
$script:ResolvedRepositoryPath = $null
$script:ResolvedCacheRoot = $null
$script:ResolvedShortcutPath = $null
$script:ResolvedIconPath = $null
$script:ResolvedAllUsersShortcutPath = $null
$script:ResolvedShortcutBackupRoot = $null
$script:ShortcutModulePath = $null
$script:BuildMutex = $null
$script:BuildMutexOwned = $false
$script:CreatedVenvByBuild = $false
$script:CreatedConfigByBuild = $false
$script:BuildMarkerPath = $null
$script:CoreBuildCompleted = $false

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

function Initialize-BuildLog {
    [CmdletBinding()]
    param()

    $baseDirectory = $env:LOCALAPPDATA

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        $baseDirectory = $env:TEMP
    }

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        throw 'Не удалось определить каталог для лога Build.'
    }

    $logDirectory = Join-Path -Path $baseDirectory -ChildPath 'AzurPilot\logs'
    New-Item -ItemType Directory -Path $logDirectory -Force -ErrorAction Stop | Out-Null

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $fileName = 'Build-AzurPilot-{0}-{1}.log' -f $timestamp, $PID
    $script:LogPath = Join-Path -Path $logDirectory -ChildPath $fileName

    New-Item -ItemType File -Path $script:LogPath -Force -ErrorAction Stop | Out-Null
}

function Write-BuildLog {
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

function Get-BuildException {
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

function Complete-BuildFailure {
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

    throw (Get-BuildException -Code $Code -Message $Message -InnerException $InnerException)
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
        Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message ('Путь не задан: {0}' -f $DisplayName)
    }

    if ($Create -and -not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force -ErrorAction Stop | Out-Null
    }

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message ('Каталог не существует: {0}' -f $Path)
    }

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop

    if ($item.PSProvider.Name -ne 'FileSystem') {
        Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message ('Путь не относится к файловой системе: {0}' -f $Path)
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
        Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message ('Не найден обязательный файл {0}: {1}' -f $DisplayName, $Path)
    }

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop

    if ($item.PSProvider.Name -ne 'FileSystem') {
        Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message ('Файл не относится к файловой системе: {0}' -f $Path)
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
    $hashBytes = [System.Security.Cryptography.SHA256]::HashData($bytes)

    return [Convert]::ToHexString($hashBytes).ToLowerInvariant()
}

function Enter-BuildMutex {
    [CmdletBinding()]
    param()

    $pathHash = Get-PathHash -Path $script:ResolvedRepositoryPath
    $mutexName = 'Local\AzurPilot.Build.{0}' -f $pathHash
    $createdNew = $false

    try {
        $script:BuildMutex = [System.Threading.Mutex]::new(
            $true,
            $mutexName,
            [ref]$createdNew
        )
    }
    catch {
        Complete-BuildFailure -Code $script:ExitCodeConcurrentBuild -Message ('Не удалось создать Build mutex: {0}' -f $_.Exception.Message) -InnerException $_.Exception
    }

    $script:BuildMutexOwned = $createdNew

    Write-BuildLog -Level 'INFO' -Message ('Build mutex: {0}' -f $mutexName)

    if (-not $createdNew) {
        Complete-BuildFailure -Code $script:ExitCodeConcurrentBuild -Message 'Для этого checkout уже выполняется Build.'
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

        Write-BuildLog -Level $Level -Message $message
    }
}

function Test-Executable {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Executable,

        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [Parameter()]
        [string]$ExpectedText = ''
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

    if ($result.ExitCode -ne 0) {
        return $false
    }

    if ([string]::IsNullOrWhiteSpace($ExpectedText)) {
        return $true
    }

    $outputText = $result.Output -join [Environment]::NewLine

    return $outputText.Contains(
        $ExpectedText,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Get-ExecutableVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Executable,

        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        return ''
    }

    try {
        $result = Invoke-NativeCommand -Executable $Executable -Arguments $Arguments
    }
    catch {
        return ''
    }

    if ($result.ExitCode -ne 0) {
        return ''
    }

    return ($result.Output -join [Environment]::NewLine).Trim()
}

function Assert-AzurPilotStopped {
    [CmdletBinding()]
    param()

    if (-not $IsWindows) {
        Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message 'Build-AzurPilot.ps1 поддерживает только Windows.'
    }

    $repositoryRegex = [regex]::Escape($script:ResolvedRepositoryPath)
    $launcherPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'alas-launcher.exe'

    try {
        $processCollection = @(
            Get-CimInstance -ClassName Win32_Process -ErrorAction Stop
        )
    }
    catch {
        $message = 'Не удалось проверить процессы AzurPilot: {0}' -f $_.Exception.Message
        Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message $message -InnerException $_.Exception
    }

    $matchingProcessCollection = @(
        $processCollection |
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

    if ($matchingProcessCollection.Count -eq 0) {
        return
    }

    $processText = $matchingProcessCollection |
        Select-Object -Property ProcessId, Name, ExecutablePath, CommandLine |
        Format-List |
        Out-String

    $message = @(
        'AzurPilot сейчас запущен.'
        'Завершите его штатно и повторите Build.'
        $processText.Trim()
    ) -join [Environment]::NewLine

    Complete-BuildFailure -Code $script:ExitCodeActiveProcess -Message $message
}

function Initialize-BuildPath {
    [CmdletBinding()]
    param()

    $script:ResolvedRepositoryPath = Resolve-FileSystemDirectory -Path $script:RepositoryPathParameter -DisplayName 'checkout AzurPilot'

    $baseDirectory = $env:LOCALAPPDATA

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        $baseDirectory = $env:TEMP
    }

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message 'Не удалось определить локальный bootstrap cache.'
    }

    $requestedCacheRoot = if ([string]::IsNullOrWhiteSpace($script:BootstrapCacheRootParameter)) {
        Join-Path -Path $baseDirectory -ChildPath 'AzurPilot\bootstrap-cache'
    } else {
        [System.IO.Path]::GetFullPath($script:BootstrapCacheRootParameter)
    }

    if (Test-PathInside -CandidatePath $requestedCacheRoot -ParentPath $script:ResolvedRepositoryPath) {
        Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message 'Bootstrap cache должен находиться вне checkout.'
    }

    $script:ResolvedCacheRoot = Resolve-FileSystemDirectory -Path $requestedCacheRoot -DisplayName 'bootstrap cache' -Create

    if ($script:NoShortcutParameter) {
        $script:ResolvedShortcutPath = $null
    } else {
        $requestedShortcutPath = if ([string]::IsNullOrWhiteSpace($script:ShortcutPathParameter)) {
            if ([string]::IsNullOrWhiteSpace($env:APPDATA)) {
                Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message 'Не удалось определить пользовательский Start Menu.'
            }

            Join-Path -Path $env:APPDATA -ChildPath 'Microsoft\Windows\Start Menu\Programs\AzurPilot.lnk'
        } else {
            [System.IO.Path]::GetFullPath($script:ShortcutPathParameter)
        }

        $shortcutParent = Split-Path -Path $requestedShortcutPath -Parent
        [void](Resolve-FileSystemDirectory -Path $shortcutParent -DisplayName 'каталог локального shortcut' -Create)
        $script:ResolvedShortcutPath = $requestedShortcutPath
    }

    $shortcutRequired = (
        -not $script:NoShortcutParameter -or
        $script:MigrateAllUsersShortcutParameter -or
        $script:ShortcutOnlyParameter
    )

    if ($shortcutRequired) {
        $requestedIconPath = if ([string]::IsNullOrWhiteSpace($script:IconPathParameter)) {
            Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'assets\AzurPilot.ico'
        } else {
            [System.IO.Path]::GetFullPath($script:IconPathParameter)
        }

        $script:ResolvedIconPath = Resolve-RequiredFile -Path $requestedIconPath -DisplayName 'project-owned AzurPilot icon'
        $script:ShortcutModulePath = Resolve-RequiredFile -Path (
            Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'scripts\lib\AzurPilot.Shortcut.psm1'
        ) -DisplayName 'shortcut module'

        $requestedBackupRoot = if ([string]::IsNullOrWhiteSpace($script:ShortcutBackupRootParameter)) {
            Join-Path -Path $baseDirectory -ChildPath 'AzurPilot\backups\shortcuts'
        } else {
            [System.IO.Path]::GetFullPath($script:ShortcutBackupRootParameter)
        }

        $backupRootParameters = @{
            Path = $requestedBackupRoot
            DisplayName = 'shortcut backup root'
            Create = $true
        }
        $script:ResolvedShortcutBackupRoot = Resolve-FileSystemDirectory @backupRootParameters
    }

    if ($script:MigrateAllUsersShortcutParameter) {
        if ([string]::IsNullOrWhiteSpace($script:AllUsersShortcutPathParameter)) {
            if ([string]::IsNullOrWhiteSpace($env:ProgramData)) {
                Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message 'Не удалось определить ProgramData для all-users shortcut.'
            }

            $script:ResolvedAllUsersShortcutPath = Join-Path -Path $env:ProgramData -ChildPath 'Microsoft\Windows\Start Menu\Programs\AzurPilot.lnk'
        } else {
            $script:ResolvedAllUsersShortcutPath = [System.IO.Path]::GetFullPath($script:AllUsersShortcutPathParameter)
        }

        $allUsersParent = Split-Path -Path $script:ResolvedAllUsersShortcutPath -Parent
        [void](Resolve-FileSystemDirectory -Path $allUsersParent -DisplayName 'каталог all-users shortcut' -Create)
    }

    $venvPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv'
    $script:BuildMarkerPath = Join-Path -Path $venvPath -ChildPath '.azurpilot-build-owned'
}

function Assert-RequiredProjectFile {
    [CmdletBinding()]
    param()

    $requiredFileMap = [ordered]@{
        'gui.py' = 'Python entrypoint'
        'deploy\uv.py' = 'uv orchestration'
        'pyproject.toml' = 'описание Python-проекта'
        'uv.lock' = 'dependency lockfile'
        'config\deploy.template.yaml' = 'Windows deploy template'
        'scripts\Start-AzurPilot.ps1' = 'Stage 2 Start command'
    }

    foreach ($relativePath in $requiredFileMap.Keys) {
        $fullPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath $relativePath
        [void](Resolve-RequiredFile -Path $fullPath -DisplayName $requiredFileMap[$relativePath])
    }

    $pyprojectPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'pyproject.toml'
    $pyprojectText = Get-Content -LiteralPath $pyprojectPath -Raw -Encoding utf8 -ErrorAction Stop

    if ($pyprojectText -notmatch [regex]::Escape($script:ExpectedPythonVersion)) {
        Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message (
            'Build pin Python {0} не найден в pyproject.toml. Обновите Build pin осознанно.' -f
            $script:ExpectedPythonVersion
        )
    }

    if ($pyprojectText -notmatch ('uv=={0}' -f [regex]::Escape($script:ExpectedUvVersion))) {
        Complete-BuildFailure -Code $script:ExitCodePreconditionFailure -Message (
            'Build pin uv {0} не найден в pyproject.toml. Обновите Build pin осознанно.' -f
            $script:ExpectedUvVersion
        )
    }
}

function Write-Stage2Config {
    [CmdletBinding()]
    param()

    $configPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'config\deploy.yaml'

    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        Write-BuildLog -Level 'INFO' -Message 'Существующий config\deploy.yaml сохранён без изменений.'
        return
    }

    $templatePath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'config\deploy.template.yaml'
    $templateText = Get-Content -LiteralPath $templatePath -Raw -Encoding utf8 -ErrorAction Stop

    $replacementMap = [ordered]@{
        'EnableReload' = 'false'
        'WebuiHost' = '127.0.0.1'
        'WebuiPort' = '25548'
    }

    $outputText = $templateText

    foreach ($key in $replacementMap.Keys) {
        $pattern = '(?m)^(\s*){0}:\s*.*$' -f [regex]::Escape($key)
        $matchCollection = [regex]::Matches($outputText, $pattern)

        if ($matchCollection.Count -ne 1) {
            Complete-BuildFailure -Code $script:ExitCodeConfigFailure -Message (
                'В deploy.template.yaml ожидалась одна строка {0}, найдено: {1}' -f
                $key,
                $matchCollection.Count
            )
        }

        $replacement = '${1}' + $key + ': ' + $replacementMap[$key]
        $outputText = [regex]::Replace(
            $outputText,
            $pattern,
            $replacement
        )
    }

    $configDirectory = Split-Path -Path $configPath -Parent
    $temporaryPath = Join-Path -Path $configDirectory -ChildPath (
        '.deploy.yaml.build-{0}.tmp' -f ([guid]::NewGuid().ToString('N'))
    )

    try {
        [System.IO.File]::WriteAllText(
            $temporaryPath,
            $outputText,
            [System.Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporaryPath -Destination $configPath -ErrorAction Stop
        $script:CreatedConfigByBuild = $true
    }
    catch {
        Complete-BuildFailure -Code $script:ExitCodeConfigFailure -Message (
            'Не удалось создать config\deploy.yaml: {0}' -f
            $_.Exception.Message
        ) -InnerException $_.Exception
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }

    Write-BuildLog -Level 'INFO' -Message 'Создан config\deploy.yaml со Stage 2 defaults.'
}

function Get-VerifiedArchive {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Name,

        [Parameter(Mandatory)]
        [uri]$Uri,

        [Parameter(Mandatory)]
        [string]$ExpectedSha256,

        [Parameter(Mandatory)]
        [string]$CacheDirectory
    )

    [void](Resolve-FileSystemDirectory -Path $CacheDirectory -DisplayName ('cache {0}' -f $Name) -Create)

    $archivePath = Join-Path -Path $CacheDirectory -ChildPath $Name

    if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
        $cachedHash = (
            Get-FileHash -LiteralPath $archivePath -Algorithm SHA256 -ErrorAction Stop
        ).Hash.ToUpperInvariant()

        if ($cachedHash -eq $ExpectedSha256.ToUpperInvariant()) {
            Write-BuildLog -Level 'INFO' -Message ('Используется verified cache: {0}' -f $archivePath)
            return $archivePath
        }

        Write-BuildLog -Level 'WARN' -Message ('Удалён cache с неверным SHA-256: {0}' -f $archivePath)
        Remove-Item -LiteralPath $archivePath -Force -ErrorAction Stop
    }

    $temporaryPath = '{0}.download-{1}.tmp' -f $archivePath, ([guid]::NewGuid().ToString('N'))

    try {
        Write-BuildLog -Level 'INFO' -Message ('Загрузка официального artifact: {0}' -f $Uri.AbsoluteUri)

        $requestParameters = @{
            Uri = $Uri
            OutFile = $temporaryPath
            MaximumRedirection = 10
            ErrorAction = 'Stop'
        }

        $null = Invoke-WebRequest @requestParameters

        $actualHash = (
            Get-FileHash -LiteralPath $temporaryPath -Algorithm SHA256 -ErrorAction Stop
        ).Hash.ToUpperInvariant()

        if ($actualHash -ne $ExpectedSha256.ToUpperInvariant()) {
            Complete-BuildFailure -Code $script:ExitCodeBootstrapUnavailable -Message (
                'SHA-256 официального artifact не совпал. Ожидался {0}, получен {1}.' -f
                $ExpectedSha256,
                $actualHash
            )
        }

        Move-Item -LiteralPath $temporaryPath -Destination $archivePath -ErrorAction Stop
    }
    catch {
        if ($_.Exception.Data.Contains('ExitCode')) {
            throw
        }

        Complete-BuildFailure -Code $script:ExitCodeBootstrapUnavailable -Message (
            'Не удалось загрузить или проверить artifact {0}: {1}' -f
            $Name,
            $_.Exception.Message
        ) -InnerException $_.Exception
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }

    Write-BuildLog -Level 'INFO' -Message ('Artifact verified: {0}' -f $archivePath)

    return $archivePath
}

function Expand-VerifiedArchive {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ArchivePath,

        [Parameter(Mandatory)]
        [string]$DestinationPath,

        [Parameter(Mandatory)]
        [string]$RequiredRelativeFile
    )

    $requiredPath = Join-Path -Path $DestinationPath -ChildPath $RequiredRelativeFile

    if (Test-Path -LiteralPath $requiredPath -PathType Leaf) {
        return $requiredPath
    }

    $temporaryDestination = '{0}.extract-{1}' -f $DestinationPath, ([guid]::NewGuid().ToString('N'))

    try {
        if (Test-Path -LiteralPath $temporaryDestination) {
            Remove-Item -LiteralPath $temporaryDestination -Recurse -Force -ErrorAction Stop
        }

        Expand-Archive -LiteralPath $ArchivePath -DestinationPath $temporaryDestination -Force -ErrorAction Stop

        $temporaryRequiredPath = Join-Path -Path $temporaryDestination -ChildPath $RequiredRelativeFile

        if (-not (Test-Path -LiteralPath $temporaryRequiredPath -PathType Leaf)) {
            Complete-BuildFailure -Code $script:ExitCodeBootstrapUnavailable -Message (
                'В archive отсутствует обязательный файл: {0}' -f
                $RequiredRelativeFile
            )
        }

        if (Test-Path -LiteralPath $DestinationPath) {
            Remove-Item -LiteralPath $DestinationPath -Recurse -Force -ErrorAction Stop
        }

        Move-Item -LiteralPath $temporaryDestination -Destination $DestinationPath -ErrorAction Stop
    }
    catch {
        if ($_.Exception.Data.Contains('ExitCode')) {
            throw
        }

        Complete-BuildFailure -Code $script:ExitCodeBootstrapUnavailable -Message (
            'Не удалось распаковать artifact {0}: {1}' -f
            $ArchivePath,
            $_.Exception.Message
        ) -InnerException $_.Exception
    }
    finally {
        if (Test-Path -LiteralPath $temporaryDestination) {
            Remove-Item -LiteralPath $temporaryDestination -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    return Resolve-RequiredFile -Path $requiredPath -DisplayName 'распакованный bootstrap executable'
}

function Test-UvVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$UvPath
    )

    $versionText = Get-ExecutableVersion -Executable $UvPath -Arguments @(
        '--version'
    )

    return $versionText -match ('(?i)^uv\s+{0}(?:\s|$)' -f [regex]::Escape($script:ExpectedUvVersion))
}

function Resolve-UvBootstrap {
    [CmdletBinding()]
    param()

    $projectUvPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv\Scripts\uv.exe'
    $candidateCollection = [System.Collections.Generic.List[object]]::new()

    if (-not [string]::IsNullOrWhiteSpace($script:UvExecutablePathParameter)) {
        $candidateCollection.Add(
            [pscustomobject]@{
                Path = [System.IO.Path]::GetFullPath($script:UvExecutablePathParameter)
                Source = 'explicit'
                Required = $true
            }
        )
    }

    if (Test-Path -LiteralPath $projectUvPath -PathType Leaf) {
        $candidateCollection.Add(
            [pscustomobject]@{
                Path = $projectUvPath
                Source = 'project'
                Required = $false
            }
        )
    }

    $pathCommand = Get-Command -Name 'uv.exe' -CommandType Application -ErrorAction SilentlyContinue

    if ($null -ne $pathCommand) {
        $candidateCollection.Add(
            [pscustomobject]@{
                Path = [string]$pathCommand.Path
                Source = 'PATH'
                Required = $false
            }
        )
    }

    $seenPathCollection = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )

    foreach ($candidate in $candidateCollection) {
        if (-not $seenPathCollection.Add([string]$candidate.Path)) {
            continue
        }

        if (Test-UvVersion -UvPath ([string]$candidate.Path)) {
            $resolvedPath = Convert-Path -LiteralPath ([string]$candidate.Path) -ErrorAction Stop
            Write-BuildLog -Level 'INFO' -Message (
                'uv bootstrap ({0}): {1}' -f
                $candidate.Source,
                $resolvedPath
            )
            return $resolvedPath
        }

        if ([bool]$candidate.Required) {
            Complete-BuildFailure -Code $script:ExitCodeBootstrapUnavailable -Message (
                'Явный uv отсутствует, не запускается или имеет версию, отличную от {0}: {1}' -f
                $script:ExpectedUvVersion,
                $candidate.Path
            )
        }

        Write-BuildLog -Level 'WARN' -Message (
            'uv candidate пропущен: требуется версия {0}, путь {1}' -f
            $script:ExpectedUvVersion,
            $candidate.Path
        )
    }

    $uvCacheDirectory = Join-Path -Path $script:ResolvedCacheRoot -ChildPath ('uv\{0}' -f $script:ExpectedUvVersion)
    $archiveParameters = @{
        Name = $script:UvArchiveName
        Uri = [uri]$script:UvArchiveUrl
        ExpectedSha256 = $script:UvArchiveSha256
        CacheDirectory = $uvCacheDirectory
    }

    $archivePath = Get-VerifiedArchive @archiveParameters

    $extractPath = Join-Path -Path $uvCacheDirectory -ChildPath 'extracted'
    $expandParameters = @{
        ArchivePath = $archivePath
        DestinationPath = $extractPath
        RequiredRelativeFile = 'uv.exe'
    }

    $uvPath = Expand-VerifiedArchive @expandParameters

    if (-not (Test-UvVersion -UvPath $uvPath)) {
        Complete-BuildFailure -Code $script:ExitCodeBootstrapUnavailable -Message 'Распакованный официальный uv не прошёл version check.'
    }

    Write-BuildLog -Level 'INFO' -Message ('uv bootstrap (official pinned): {0}' -f $uvPath)

    return $uvPath
}

function Test-PythonBootstrap {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$PythonPath
    )

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        return $false
    }

    $pythonCode = @'
import sys

expected = (3, 14, 6)
print(sys.version)
raise SystemExit(0 if sys.version_info[:3] == expected else 1)
'@

    try {
        $result = Invoke-NativeCommand -Executable $PythonPath -Arguments @(
            '-c'
            $pythonCode
        )
    }
    catch {
        return $false
    }

    return $result.ExitCode -eq 0
}

function Resolve-BootstrapPython {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$UvPath
    )

    $candidateCollection = [System.Collections.Generic.List[object]]::new()

    if (-not [string]::IsNullOrWhiteSpace($script:BootstrapPythonPathParameter)) {
        $candidateCollection.Add(
            [pscustomobject]@{
                Path = [System.IO.Path]::GetFullPath($script:BootstrapPythonPathParameter)
                Source = 'explicit'
                Required = $true
            }
        )
    }

    $pathCommand = Get-Command -Name 'python.exe' -CommandType Application -ErrorAction SilentlyContinue

    if ($null -ne $pathCommand) {
        $candidateCollection.Add(
            [pscustomobject]@{
                Path = [string]$pathCommand.Path
                Source = 'PATH'
                Required = $false
            }
        )
    }

    foreach ($candidate in $candidateCollection) {
        if (Test-PythonBootstrap -PythonPath ([string]$candidate.Path)) {
            $resolvedPath = Convert-Path -LiteralPath ([string]$candidate.Path) -ErrorAction Stop
            Write-BuildLog -Level 'INFO' -Message (
                'Bootstrap Python ({0}): {1}' -f
                $candidate.Source,
                $resolvedPath
            )
            return $resolvedPath
        }

        if ([bool]$candidate.Required) {
            Complete-BuildFailure -Code $script:ExitCodeBootstrapUnavailable -Message (
                'Явный Bootstrap Python должен быть ровно {0}: {1}' -f
                $script:ExpectedPythonVersion,
                $candidate.Path
            )
        }
    }

    $pythonCacheRoot = Join-Path -Path $script:ResolvedCacheRoot -ChildPath ('python\{0}' -f $script:ExpectedPythonVersion)
    [void](Resolve-FileSystemDirectory -Path $pythonCacheRoot -DisplayName 'bootstrap Python cache' -Create)

    $existingPythonCollection = @(
        Get-ChildItem -LiteralPath $pythonCacheRoot -Filter 'python.exe' -File -Recurse -Force -ErrorAction SilentlyContinue |
            Sort-Object -Property FullName
    )

    foreach ($existingPython in $existingPythonCollection) {
        if (Test-PythonBootstrap -PythonPath $existingPython.FullName) {
            Write-BuildLog -Level 'INFO' -Message ('Bootstrap Python (verified cache): {0}' -f $existingPython.FullName)
            return $existingPython.FullName
        }
    }

    $uvArguments = @(
        'python'
        'install'
        $script:ExpectedPythonVersion
        '--install-dir'
        $pythonCacheRoot
        '--no-bin'
        '--managed-python'
    )

    Write-BuildLog -Level 'INFO' -Message ('Подготовка bootstrap Python {0} через pinned uv.' -f $script:ExpectedPythonVersion)
    $installResult = Invoke-NativeCommand -Executable $UvPath -Arguments $uvArguments -WorkingDirectory $script:ResolvedCacheRoot
    Write-NativeOutput -Result $installResult -Prefix '[uv python]'

    if ($installResult.ExitCode -ne 0) {
        Complete-BuildFailure -Code $script:ExitCodeBootstrapUnavailable -Message (
            'uv python install завершился с кодом {0}.' -f
            $installResult.ExitCode
        )
    }

    $installedPythonCollection = @(
        Get-ChildItem -LiteralPath $pythonCacheRoot -Filter 'python.exe' -File -Recurse -Force -ErrorAction Stop |
            Sort-Object -Property FullName
    )

    foreach ($installedPython in $installedPythonCollection) {
        if (Test-PythonBootstrap -PythonPath $installedPython.FullName) {
            Write-BuildLog -Level 'INFO' -Message ('Bootstrap Python (uv managed): {0}' -f $installedPython.FullName)
            return $installedPython.FullName
        }
    }

    Complete-BuildFailure -Code $script:ExitCodeBootstrapUnavailable -Message 'uv установил Python, но executable 3.14.6 не найден или не запускается.'
}

function Write-DeployUvHelper {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$HelperPath
    )

    $helperContent = @'
import os
import sys
from pathlib import Path

root = Path(os.environ["AZURPILOT_BUILD_ROOT"])
sys.path.insert(0, str(root))

from deploy.uv import sync_project_venv

bootstrap_uv = Path(os.environ["AZURPILOT_BUILD_UV"])
timeout = float(os.environ["AZURPILOT_BUILD_TIMEOUT"])

sync_project_venv(
    root=root,
    bootstrap_uv=bootstrap_uv,
    capture_output=False,
    timeout=timeout,
)
'@

    Set-Content -LiteralPath $HelperPath -Value $helperContent -Encoding utf8NoBOM -ErrorAction Stop
}

function Get-IsolatedEnvironmentName {
    [CmdletBinding()]
    param()

    return @(
        'AZURPILOT_BUILD_ROOT'
        'AZURPILOT_BUILD_UV'
        'AZURPILOT_BUILD_TIMEOUT'
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
}

function Save-IsolatedEnvironment {
    [CmdletBinding()]
    param()

    $savedEnvironment = @{}

    foreach ($name in Get-IsolatedEnvironmentName) {
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

    return $savedEnvironment
}

function Restore-IsolatedEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [hashtable]$SavedEnvironment
    )

    foreach ($name in Get-IsolatedEnvironmentName) {
        $environmentPath = 'Env:\{0}' -f $name

        if (Test-Path -LiteralPath $environmentPath) {
            Remove-Item -LiteralPath $environmentPath -ErrorAction Stop
        }

        $saved = $SavedEnvironment[$name]

        if ($saved.Exists) {
            Set-Item -LiteralPath $environmentPath -Value $saved.Value -ErrorAction Stop
        }
    }
}

function Invoke-DeployUvBuild {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$BootstrapUvPath,

        [Parameter(Mandatory)]
        [string]$BootstrapPythonExecutable
    )

    $venvPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv'

    if (Test-Path -LiteralPath $venvPath) {
        if (Test-Path -LiteralPath $script:BuildMarkerPath -PathType Leaf) {
            Write-BuildLog -Level 'WARN' -Message 'Удаляется незавершённая .venv, ранее созданная Build.'
            Remove-Item -LiteralPath $venvPath -Recurse -Force -ErrorAction Stop
        } else {
            Complete-BuildFailure -Code $script:ExitCodeExistingEnvironmentBroken -Message (
                'Существующая .venv неисправна и не принадлежит незавершённому Build. Используйте Repair-AzurPilot.ps1.'
            )
        }
    }

    New-Item -ItemType Directory -Path $venvPath -Force -ErrorAction Stop | Out-Null
    Set-Content -LiteralPath $script:BuildMarkerPath -Value (
        'AzurPilot Build ownership marker {0:o}' -f [DateTimeOffset]::UtcNow
    ) -Encoding utf8NoBOM -ErrorAction Stop
    $script:CreatedVenvByBuild = $true

    $helperPath = Join-Path -Path $script:ResolvedCacheRoot -ChildPath (
        'deploy-uv-build-{0}.py' -f ([guid]::NewGuid().ToString('N'))
    )
    Write-DeployUvHelper -HelperPath $helperPath

    $savedEnvironment = Save-IsolatedEnvironment

    Set-Item -LiteralPath 'Env:\AZURPILOT_BUILD_ROOT' -Value $script:ResolvedRepositoryPath -ErrorAction Stop
    Set-Item -LiteralPath 'Env:\AZURPILOT_BUILD_UV' -Value $BootstrapUvPath -ErrorAction Stop
    Set-Item -LiteralPath 'Env:\AZURPILOT_BUILD_TIMEOUT' -Value ([string]$script:SyncTimeoutSecondsParameter) -ErrorAction Stop
    Set-Item -LiteralPath 'Env:\PYTHONDONTWRITEBYTECODE' -Value '1' -ErrorAction Stop

    try {
        $buildResult = Invoke-NativeCommand -Executable $BootstrapPythonExecutable -Arguments @(
            $helperPath
        ) -WorkingDirectory $script:ResolvedRepositoryPath
    }
    finally {
        Restore-IsolatedEnvironment -SavedEnvironment $savedEnvironment

        if (Test-Path -LiteralPath $helperPath -PathType Leaf) {
            Remove-Item -LiteralPath $helperPath -Force -ErrorAction SilentlyContinue
        }
    }

    Write-NativeOutput -Result $buildResult -Prefix '[deploy.uv]'

    if ($buildResult.ExitCode -ne 0) {
        Complete-BuildFailure -Code $script:ExitCodeDependencyBuildFailure -Message (
            'deploy\uv.py завершился с кодом {0}.' -f
            $buildResult.ExitCode
        )
    }
}

function Copy-UvTool {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$SourceUvPath
    )

    $scriptsPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv\Scripts'
    [void](Resolve-FileSystemDirectory -Path $scriptsPath -DisplayName '.venv Scripts' -Create)

    $destinationPath = Join-Path -Path $scriptsPath -ChildPath 'uv.exe'

    if (
        (Test-Path -LiteralPath $destinationPath -PathType Leaf) -and
        (Test-UvVersion -UvPath $destinationPath)
    ) {
        return
    }

    $temporaryPath = Join-Path -Path $scriptsPath -ChildPath (
        '.uv.exe.build-{0}.tmp' -f ([guid]::NewGuid().ToString('N'))
    )

    try {
        Copy-Item -LiteralPath $SourceUvPath -Destination $temporaryPath -Force -ErrorAction Stop

        if (-not (Test-UvVersion -UvPath $temporaryPath)) {
            Complete-BuildFailure -Code $script:ExitCodeBootstrapUnavailable -Message 'Копия uv.exe не прошла version check.'
        }

        Move-Item -LiteralPath $temporaryPath -Destination $destinationPath -Force -ErrorAction Stop
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }

    Write-BuildLog -Level 'INFO' -Message ('Pinned uv.exe сохранён: {0}' -f $destinationPath)
}

function Test-AdbExecutable {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$AdbPath
    )

    if (-not (Test-Path -LiteralPath $AdbPath -PathType Leaf)) {
        return $false
    }

    $adbDirectory = Split-Path -Path $AdbPath -Parent

    foreach ($fileName in @(
        'AdbWinApi.dll'
        'AdbWinUsbApi.dll'
        'libwinpthread-1.dll'
    )) {
        $companionPath = Join-Path -Path $adbDirectory -ChildPath $fileName

        if (-not (Test-Path -LiteralPath $companionPath -PathType Leaf)) {
            return $false
        }
    }

    return Test-Executable -Executable $AdbPath -Arguments @(
        'version'
    ) -ExpectedText 'Android Debug Bridge version'
}

function Get-AdbVersionText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$AdbPath
    )

    return Get-ExecutableVersion -Executable $AdbPath -Arguments @(
        'version'
    )
}

function Resolve-AdbSource {
    [CmdletBinding()]
    param()

    $projectAdbPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv\Scripts\adb.exe'
    $candidateCollection = [System.Collections.Generic.List[object]]::new()

    if (-not [string]::IsNullOrWhiteSpace($script:AdbExecutablePathParameter)) {
        $candidateCollection.Add(
            [pscustomobject]@{
                Path = [System.IO.Path]::GetFullPath($script:AdbExecutablePathParameter)
                Source = 'explicit'
                Required = $true
            }
        )
    }

    if (Test-Path -LiteralPath $projectAdbPath -PathType Leaf) {
        $candidateCollection.Add(
            [pscustomobject]@{
                Path = $projectAdbPath
                Source = 'project'
                Required = $false
            }
        )
    }

    $pathCommand = Get-Command -Name 'adb.exe' -CommandType Application -ErrorAction SilentlyContinue

    if ($null -ne $pathCommand) {
        $candidateCollection.Add(
            [pscustomobject]@{
                Path = [string]$pathCommand.Path
                Source = 'PATH'
                Required = $false
            }
        )
    }

    $seenPathCollection = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )

    foreach ($candidate in $candidateCollection) {
        if (-not $seenPathCollection.Add([string]$candidate.Path)) {
            continue
        }

        if (Test-AdbExecutable -AdbPath ([string]$candidate.Path)) {
            $resolvedPath = Convert-Path -LiteralPath ([string]$candidate.Path) -ErrorAction Stop
            Write-BuildLog -Level 'INFO' -Message (
                'ADB source ({0}): {1}' -f
                $candidate.Source,
                $resolvedPath
            )
            return $resolvedPath
        }

        if ([bool]$candidate.Required) {
            Complete-BuildFailure -Code $script:ExitCodeAdbFailure -Message (
                'Явный ADB отсутствует, не запускается или не имеет companion DLL: {0}' -f
                $candidate.Path
            )
        }
    }

    $adbCacheDirectory = Join-Path -Path $script:ResolvedCacheRoot -ChildPath ('platform-tools\{0}' -f $script:ExpectedAdbVersion)
    $archiveParameters = @{
        Name = $script:AdbArchiveName
        Uri = [uri]$script:AdbArchiveUrl
        ExpectedSha256 = $script:AdbArchiveSha256
        CacheDirectory = $adbCacheDirectory
    }

    $archivePath = Get-VerifiedArchive @archiveParameters

    $extractPath = Join-Path -Path $adbCacheDirectory -ChildPath 'extracted'
    $expandParameters = @{
        ArchivePath = $archivePath
        DestinationPath = $extractPath
        RequiredRelativeFile = 'platform-tools\adb.exe'
    }

    $adbPath = Expand-VerifiedArchive @expandParameters

    if (-not (Test-AdbExecutable -AdbPath $adbPath)) {
        Complete-BuildFailure -Code $script:ExitCodeAdbFailure -Message 'Официальный ADB не прошёл health check.'
    }

    $versionText = Get-AdbVersionText -AdbPath $adbPath

    if ($versionText -notmatch ('(?m)^Version\s+{0}(?:[-\s]|$)' -f [regex]::Escape($script:ExpectedAdbVersion))) {
        Complete-BuildFailure -Code $script:ExitCodeAdbFailure -Message (
            'Официальный ADB не соответствует pinned версии {0}: {1}' -f
            $script:ExpectedAdbVersion,
            $versionText
        )
    }

    Write-BuildLog -Level 'INFO' -Message ('ADB source (official pinned): {0}' -f $adbPath)

    return $adbPath
}

function Copy-AdbTool {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$SourceAdbPath
    )

    $sourceDirectory = Split-Path -Path $SourceAdbPath -Parent
    $destinationDirectory = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv\Scripts'
    [void](Resolve-FileSystemDirectory -Path $destinationDirectory -DisplayName '.venv Scripts' -Create)

    $adbFileNameCollection = @(
        'adb.exe'
        'AdbWinApi.dll'
        'AdbWinUsbApi.dll'
        'libwinpthread-1.dll'
    )
    $projectAdbPath = Join-Path -Path $destinationDirectory -ChildPath 'adb.exe'
    $destinationAlreadyCurrent = Test-AdbExecutable -AdbPath $projectAdbPath

    if ($destinationAlreadyCurrent) {
        foreach ($fileName in $adbFileNameCollection) {
            $sourcePath = Join-Path -Path $sourceDirectory -ChildPath $fileName
            $destinationPath = Join-Path -Path $destinationDirectory -ChildPath $fileName

            if (
                -not (Test-Path -LiteralPath $sourcePath -PathType Leaf) -or
                -not (Test-Path -LiteralPath $destinationPath -PathType Leaf)
            ) {
                $destinationAlreadyCurrent = $false
                break
            }

            $sourceHash = (
                Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256 -ErrorAction Stop
            ).Hash
            $destinationHash = (
                Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256 -ErrorAction Stop
            ).Hash

            if ($sourceHash -ne $destinationHash) {
                $destinationAlreadyCurrent = $false
                break
            }
        }
    }

    if ($destinationAlreadyCurrent) {
        Write-BuildLog -Level 'INFO' -Message ('Project ADB уже соответствует выбранному source: {0}' -f $projectAdbPath)
        return
    }

    $stagingDirectory = Join-Path -Path $script:ResolvedCacheRoot -ChildPath (
        'adb-stage-{0}' -f ([guid]::NewGuid().ToString('N'))
    )

    try {
        New-Item -ItemType Directory -Path $stagingDirectory -Force -ErrorAction Stop | Out-Null

        foreach ($fileName in $adbFileNameCollection) {
            $sourcePath = Join-Path -Path $sourceDirectory -ChildPath $fileName
            $stagingPath = Join-Path -Path $stagingDirectory -ChildPath $fileName

            [void](Resolve-RequiredFile -Path $sourcePath -DisplayName ('ADB component {0}' -f $fileName))
            Copy-Item -LiteralPath $sourcePath -Destination $stagingPath -Force -ErrorAction Stop
        }

        $stagingAdbPath = Join-Path -Path $stagingDirectory -ChildPath 'adb.exe'

        if (-not (Test-AdbExecutable -AdbPath $stagingAdbPath)) {
            Complete-BuildFailure -Code $script:ExitCodeAdbFailure -Message 'Staged ADB не прошёл health check.'
        }

        foreach ($fileName in $adbFileNameCollection) {
            $stagingPath = Join-Path -Path $stagingDirectory -ChildPath $fileName
            $destinationPath = Join-Path -Path $destinationDirectory -ChildPath $fileName

            Copy-Item -LiteralPath $stagingPath -Destination $destinationPath -Force -ErrorAction Stop
        }
    }
    catch {
        if ($_.Exception.Data.Contains('ExitCode')) {
            throw
        }

        Complete-BuildFailure -Code $script:ExitCodeAdbFailure -Message (
            'Не удалось сохранить ADB в .venv: {0}' -f
            $_.Exception.Message
        ) -InnerException $_.Exception
    }
    finally {
        if (Test-Path -LiteralPath $stagingDirectory) {
            Remove-Item -LiteralPath $stagingDirectory -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    if (-not (Test-AdbExecutable -AdbPath $projectAdbPath)) {
        Complete-BuildFailure -Code $script:ExitCodeAdbFailure -Message 'Project ADB не прошёл итоговый health check.'
    }

    Write-BuildLog -Level 'INFO' -Message ('ADB сохранён: {0}' -f $projectAdbPath)
}

function Test-CoreProjectEnvironment {
    [CmdletBinding()]
    param()

    $pythonPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv\Scripts\python.exe'
    $managedPythonRoot = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv\python'

    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        return [pscustomobject]@{
            Healthy = $false
            Details = 'Project python.exe отсутствует.'
        }
    }

    if (-not (Test-Path -LiteralPath $managedPythonRoot -PathType Container)) {
        return [pscustomobject]@{
            Healthy = $false
            Details = 'Managed Python root отсутствует.'
        }
    }

    $pythonCode = @'
import yaml
import uvicorn
import pywebio
import starlette
import rich
import numpy
import cv2
import adbutils
import sys

print(sys.version)
'@

    $pythonResult = Invoke-NativeCommand -Executable $pythonPath -Arguments @(
        '-c'
        $pythonCode
    ) -WorkingDirectory $script:ResolvedRepositoryPath

    if ($pythonResult.ExitCode -ne 0) {
        return [pscustomobject]@{
            Healthy = $false
            Details = 'Required Python imports не прошли: {0}' -f ($pythonResult.Output -join [Environment]::NewLine)
        }
    }

    return [pscustomobject]@{
        Healthy = $true
        Details = 'Project Python и managed runtime исправны.'
    }
}

function Test-FrozenProjectEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$UvPath
    )

    $pythonPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv\Scripts\python.exe'

    if (-not (Test-UvVersion -UvPath $UvPath)) {
        return [pscustomobject]@{
            Healthy = $false
            Details = ('uv отсутствует или не соответствует {0}: {1}' -f $script:ExpectedUvVersion, $UvPath)
        }
    }

    $dryRunArguments = @(
        'sync'
        '--project'
        $script:ResolvedRepositoryPath
        '--python'
        $pythonPath
        '--dry-run'
        '--frozen'
        '--no-dev'
        '--no-install-project'
    )

    $dryRunResult = Invoke-NativeCommand -Executable $UvPath -Arguments $dryRunArguments -WorkingDirectory $script:ResolvedRepositoryPath

    if ($dryRunResult.ExitCode -ne 0) {
        return [pscustomobject]@{
            Healthy = $false
            Details = 'Frozen dry-run не прошёл: {0}' -f ($dryRunResult.Output -join [Environment]::NewLine)
        }
    }

    return [pscustomobject]@{
        Healthy = $true
        Details = 'Frozen dependency state исправно.'
    }
}

function Test-ProjectEnvironment {
    [CmdletBinding()]
    param()

    $coreState = Test-CoreProjectEnvironment

    if (-not $coreState.Healthy) {
        return $coreState
    }

    $uvPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv\Scripts\uv.exe'
    $frozenState = Test-FrozenProjectEnvironment -UvPath $uvPath

    if (-not $frozenState.Healthy) {
        return $frozenState
    }

    $adbPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv\Scripts\adb.exe'

    if (-not (Test-AdbExecutable -AdbPath $adbPath)) {
        return [pscustomobject]@{
            Healthy = $false
            Details = 'Project ADB отсутствует или не запускается.'
        }
    }

    return [pscustomobject]@{
        Healthy = $true
        Details = 'Project environment исправно.'
    }
}

function Import-AzurPilotShortcutModule {
    [CmdletBinding()]
    param()

    if ($null -eq $script:ShortcutModulePath) {
        Complete-BuildFailure -Code $script:ExitCodeShortcutFailure -Message 'Shortcut module path не инициализирован.'
    }

    Import-Module -Name $script:ShortcutModulePath -Force -ErrorAction Stop
}

function Invoke-AzurPilotShortcutWrite {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$Scope,

        [Parameter()]
        [switch]$RequireAdministrator
    )

    Import-AzurPilotShortcutModule

    $pwshPath = [string](Get-Process -Id $PID -ErrorAction Stop).Path
    $shortcutParameters = @{
        ShortcutPath = $Path
        RepositoryPath = $script:ResolvedRepositoryPath
        PwshExecutablePath = $pwshPath
        IconPath = $script:ResolvedIconPath
        BackupRoot = $script:ResolvedShortcutBackupRoot
        RequireAdministrator = $RequireAdministrator
    }

    try {
        $result = Set-AzurPilotShortcut @shortcutParameters
    }
    catch {
        $reason = [string]$_.Exception.Data['AzurPilotShortcutReason']

        if ($reason -eq 'ElevationRequired') {
            $elevatedCommand = (
                "& '{0}' -NoLogo -NoProfile -File '{1}' -RepositoryPath '{2}' " +
                '-ShortcutOnly -MigrateAllUsersShortcut'
            ) -f $pwshPath, $PSCommandPath, $script:ResolvedRepositoryPath

            Complete-BuildFailure -Code $script:ExitCodeElevationRequired -Message (
                '{0}{1}Запустите PowerShell 7 от имени администратора и выполните:{1}{2}' -f
                $_.Exception.Message,
                [Environment]::NewLine,
                $elevatedCommand
            ) -InnerException $_.Exception
        }

        Complete-BuildFailure -Code $script:ExitCodeShortcutFailure -Message (
            'Не удалось обновить {0} shortcut: {1}' -f
            $Scope,
            $_.Exception.Message
        ) -InnerException $_.Exception
    }

    if ([bool]$result.Changed) {
        Write-BuildLog -Level 'INFO' -Message ('{0} shortcut обновлён: {1}' -f $Scope, $Path)

        if (-not [string]::IsNullOrWhiteSpace([string]$result.BackupPath)) {
            Write-BuildLog -Level 'INFO' -Message ('Backup предыдущего shortcut: {0}' -f $result.BackupPath)
        }
    } else {
        Write-BuildLog -Level 'INFO' -Message ('{0} shortcut уже исправен: {1}' -f $Scope, $Path)
    }
}

function Write-LocalShortcut {
    [CmdletBinding()]
    param()

    if ($script:NoShortcutParameter) {
        Write-BuildLog -Level 'INFO' -Message 'Создание локального shortcut отключено параметром -NoShortcut.'
        return
    }

    if ($script:MigrateAllUsersShortcutParameter) {
        Write-BuildLog -Level 'INFO' -Message 'Локальный duplicate shortcut не создаётся во время all-users migration.'
        return
    }

    $parameters = @{
        Path = $script:ResolvedShortcutPath
        Scope = 'Локальный'
    }
    Invoke-AzurPilotShortcutWrite @parameters
}

function Write-SharedShortcut {
    [CmdletBinding()]
    param()

    if (-not $script:MigrateAllUsersShortcutParameter) {
        return
    }

    $requireAdministrator = $false

    if (-not [string]::IsNullOrWhiteSpace($env:ProgramData)) {
        $programDataPath = [System.IO.Path]::GetFullPath($env:ProgramData)
        $administratorCheckParameters = @{
            CandidatePath = $script:ResolvedAllUsersShortcutPath
            ParentPath = $programDataPath
        }
        $requireAdministrator = Test-PathInside @administratorCheckParameters
    }

    $parameters = @{
        Path = $script:ResolvedAllUsersShortcutPath
        Scope = 'All-users'
        RequireAdministrator = $requireAdministrator
    }
    Invoke-AzurPilotShortcutWrite @parameters
}

function Invoke-AzurPilotBuild {
    [CmdletBinding()]
    param()

    Initialize-BuildPath
    Enter-BuildMutex

    Write-BuildLog -Level 'INFO' -Message 'Запуск AzurPilot Stage 2F shortcut orchestration.'
    Write-BuildLog -Level 'INFO' -Message ('PowerShell: {0}' -f $PSVersionTable.PSVersion)
    Write-BuildLog -Level 'INFO' -Message ('RepositoryPath: {0}' -f $script:ResolvedRepositoryPath)

    if ($script:ShortcutOnlyParameter) {
        Write-LocalShortcut
        Write-SharedShortcut
        Write-BuildLog -Level 'INFO' -Message 'Shortcut-only операция завершена успешно.'
        return $script:ExitCodeSuccess
    }

    Assert-AzurPilotStopped
    Assert-RequiredProjectFile

    Write-BuildLog -Level 'INFO' -Message 'Запуск AzurPilot Stage 2E Build.'
    Write-BuildLog -Level 'INFO' -Message ('PowerShell: {0}' -f $PSVersionTable.PSVersion)
    Write-BuildLog -Level 'INFO' -Message ('RepositoryPath: {0}' -f $script:ResolvedRepositoryPath)

    $lockPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'uv.lock'
    $templatePath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'config\deploy.template.yaml'
    $lockHashBefore = (
        Get-FileHash -LiteralPath $lockPath -Algorithm SHA256 -ErrorAction Stop
    ).Hash
    $templateHashBefore = (
        Get-FileHash -LiteralPath $templatePath -Algorithm SHA256 -ErrorAction Stop
    ).Hash

    Write-Stage2Config

    $configPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'config\deploy.yaml'
    $configHashBeforeEnvironment = (
        Get-FileHash -LiteralPath $configPath -Algorithm SHA256 -ErrorAction Stop
    ).Hash
    $venvPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv'
    $venvExists = Test-Path -LiteralPath $venvPath -PathType Container
    $ownedPartialExists = Test-Path -LiteralPath $script:BuildMarkerPath -PathType Leaf
    $fullBuildRequired = -not $venvExists -or $ownedPartialExists

    if ($venvExists -and -not $ownedPartialExists) {
        $coreState = Test-CoreProjectEnvironment

        if (-not $coreState.Healthy) {
            Complete-BuildFailure -Code $script:ExitCodeExistingEnvironmentBroken -Message (
                'Существующая .venv не прошла core health check: {0}{1}Используйте Repair-AzurPilot.ps1.' -f
                $coreState.Details,
                [Environment]::NewLine
            )
        }
    }

    $uvBootstrapPath = Resolve-UvBootstrap

    if ($fullBuildRequired) {
        Write-BuildLog -Level 'INFO' -Message 'Полный Build требуется: .venv отсутствует или содержит Build ownership marker.'

        $bootstrapPythonExecutable = Resolve-BootstrapPython -UvPath $uvBootstrapPath
        Invoke-DeployUvBuild -BootstrapUvPath $uvBootstrapPath -BootstrapPythonExecutable $bootstrapPythonExecutable
    } else {
        $frozenState = Test-FrozenProjectEnvironment -UvPath $uvBootstrapPath

        if (-not $frozenState.Healthy) {
            Complete-BuildFailure -Code $script:ExitCodeExistingEnvironmentBroken -Message (
                'Существующая .venv не согласована с uv.lock: {0}{1}Используйте Repair-AzurPilot.ps1.' -f
                $frozenState.Details,
                [Environment]::NewLine
            )
        }

        Write-BuildLog -Level 'INFO' -Message 'Существующая .venv прошла core и frozen checks. Dependency rebuild не требуется.'
    }

    Copy-UvTool -SourceUvPath $uvBootstrapPath

    $adbSourcePath = Resolve-AdbSource
    Copy-AdbTool -SourceAdbPath $adbSourcePath

    $finalEnvironmentState = Test-ProjectEnvironment

    if (-not $finalEnvironmentState.Healthy) {
        Complete-BuildFailure -Code $script:ExitCodeDependencyBuildFailure -Message (
            'Итоговая Build validation не прошла: {0}' -f
            $finalEnvironmentState.Details
        )
    }

    if (Test-Path -LiteralPath $script:BuildMarkerPath -PathType Leaf) {
        Remove-Item -LiteralPath $script:BuildMarkerPath -Force -ErrorAction Stop
    }

    $script:CoreBuildCompleted = $true

    Write-LocalShortcut
    Write-SharedShortcut

    $lockHashAfter = (
        Get-FileHash -LiteralPath $lockPath -Algorithm SHA256 -ErrorAction Stop
    ).Hash
    $templateHashAfter = (
        Get-FileHash -LiteralPath $templatePath -Algorithm SHA256 -ErrorAction Stop
    ).Hash
    $configHashAfter = (
        Get-FileHash -LiteralPath $configPath -Algorithm SHA256 -ErrorAction Stop
    ).Hash

    if ($lockHashAfter -ne $lockHashBefore) {
        Complete-BuildFailure -Code $script:ExitCodeDependencyBuildFailure -Message 'uv.lock изменился во время Build.'
    }

    if ($templateHashAfter -ne $templateHashBefore) {
        Complete-BuildFailure -Code $script:ExitCodeConfigFailure -Message 'deploy.template.yaml изменился во время Build.'
    }

    if ($configHashAfter -ne $configHashBeforeEnvironment) {
        Complete-BuildFailure -Code $script:ExitCodeConfigFailure -Message 'config\deploy.yaml был изменён после этапа его создания/проверки.'
    }

    Write-BuildLog -Level 'INFO' -Message 'Build завершён успешно.'
    Write-BuildLog -Level 'INFO' -Message 'Для запуска используйте: scripts\Start-AzurPilot.ps1'

    return $script:ExitCodeSuccess
}

$exitCode = $script:ExitCodeUnexpectedFailure

try {
    Initialize-BuildLog
    $exitCode = Invoke-AzurPilotBuild
}
catch {
    $exception = $_.Exception
    $requestedExitCode = $script:ExitCodeUnexpectedFailure

    if ($exception.Data.Contains('ExitCode')) {
        $requestedExitCode = [int]$exception.Data['ExitCode']
    }

    Write-BuildLog -Level 'ERROR' -Message $exception.Message

    if (
        -not $script:CoreBuildCompleted -and
        $script:CreatedVenvByBuild -and
        -not [string]::IsNullOrWhiteSpace($script:ResolvedRepositoryPath)
    ) {
        $venvPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath '.venv'

        if (
            (Test-Path -LiteralPath $script:BuildMarkerPath -PathType Leaf) -and
            (Test-Path -LiteralPath $venvPath -PathType Container)
        ) {
            try {
                Remove-Item -LiteralPath $venvPath -Recurse -Force -ErrorAction Stop
                Write-BuildLog -Level 'INFO' -Message 'Partial .venv, созданная Build, удалена.'
            }
            catch {
                Write-BuildLog -Level 'WARN' -Message (
                    'Не удалось удалить partial .venv: {0}' -f
                    $_.Exception.Message
                )
            }
        }
    }

    if (
        -not $script:CoreBuildCompleted -and
        $script:CreatedConfigByBuild -and
        -not [string]::IsNullOrWhiteSpace($script:ResolvedRepositoryPath)
    ) {
        $configPath = Join-Path -Path $script:ResolvedRepositoryPath -ChildPath 'config\deploy.yaml'

        if (Test-Path -LiteralPath $configPath -PathType Leaf) {
            try {
                Remove-Item -LiteralPath $configPath -Force -ErrorAction Stop
                Write-BuildLog -Level 'INFO' -Message 'Созданный текущим Build config\deploy.yaml удалён после ошибки.'
            }
            catch {
                Write-BuildLog -Level 'WARN' -Message (
                    'Не удалось удалить созданный Build config: {0}' -f
                    $_.Exception.Message
                )
            }
        }
    }

    $exitCode = $requestedExitCode
}
finally {
    if ($script:BuildMutexOwned -and $null -ne $script:BuildMutex) {
        try {
            $script:BuildMutex.ReleaseMutex()
        }
        catch {
            Write-BuildLog -Level 'WARN' -Message ('Не удалось освободить Build mutex: {0}' -f $_.Exception.Message)
        }
    }

    if ($null -ne $script:BuildMutex) {
        $script:BuildMutex.Dispose()
    }

    if ($null -ne $script:LogPath) {
        Write-ConsoleMessage -Message ('Лог: {0}' -f $script:LogPath)
    }
}

exit $exitCode
