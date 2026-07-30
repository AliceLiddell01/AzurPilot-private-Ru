#requires -Version 7.6

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$UpdaterPath,

    [Parameter()]
    [string]$UvExecutablePath = '',

    [Parameter()]
    [string]$PythonExecutablePath = '',

    [Parameter()]
    [string]$RobocopyExecutablePath = '',

    [Parameter()]
    [string]$TarExecutablePath = '',

    [Parameter()]
    [switch]$KeepFixtures
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

function Invoke-NativeChecked {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position = 0)]
        [hashtable]$Request
    )

    $Executable = [string]$Request.Executable
    $Arguments = [string[]]$Request.Arguments
    $Operation = [string]$Request.Operation

    $AllowedExitCodes = if ($Request.ContainsKey('AllowedExitCodes')) {
        [int[]]$Request.AllowedExitCodes
    } else {
        [int[]]@(
            0
        )
    }

    if ([string]::IsNullOrWhiteSpace($Executable)) {
        throw 'Invoke-NativeChecked: не задан Executable.'
    }

    if ([string]::IsNullOrWhiteSpace($Operation)) {
        throw 'Invoke-NativeChecked: не задан Operation.'
    }

    $nativeOutput = & $Executable @Arguments 2>&1
    $nativeExitCode = $LASTEXITCODE
    $nativeOutput = @($nativeOutput)

    if ($AllowedExitCodes -notcontains $nativeExitCode) {
        $outputText = $nativeOutput -join [Environment]::NewLine

        throw (
            @(
                "Операция завершилась ошибкой: $Operation"
                "Код возврата: $nativeExitCode"
                "Исполняемый файл: $Executable"
                "Аргументы: $($Arguments -join ' ')"
                "Вывод:$([Environment]::NewLine)$outputText"
            ) -join [Environment]::NewLine
        )
    }

    return [pscustomobject]@{
        ExitCode = $nativeExitCode
        Output = [string[]]$nativeOutput
    }
}

function Invoke-GitChecked {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position = 0)]
        [hashtable]$Request
    )

    $RepositoryPath = [string]$Request.RepositoryPath
    $Arguments = [string[]]$Request.Arguments
    $Operation = [string]$Request.Operation

    $AllowedExitCodes = if ($Request.ContainsKey('AllowedExitCodes')) {
        [int[]]$Request.AllowedExitCodes
    } else {
        [int[]]@(
            0
        )
    }

    $fullArguments = @(
        '-C'
        $RepositoryPath
    ) + $Arguments

    return Invoke-NativeChecked @{
        Executable = $script:GitExecutable
        Arguments = $fullArguments
        Operation = $Operation
        AllowedExitCodes = $AllowedExitCodes
    }
}

function Get-GitValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position = 0)]
        [hashtable]$Request
    )

    $result = Invoke-GitChecked $Request

    return ($result.Output -join [Environment]::NewLine).Trim()
}

function Assert-Equal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Expected,

        [Parameter(Mandatory)]
        [object]$Actual,

        [Parameter(Mandatory)]
        [string]$Message
    )

    if ($Expected -ne $Actual) {
        throw "$Message Ожидалось: $Expected. Получено: $Actual"
    }
}

function Assert-True {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [bool]$Condition,

        [Parameter(Mandatory)]
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Get-DirectorySnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$LiteralPath
    )

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

    return [pscustomobject]@{
        FileCount = $files.Count
        TotalBytes = [int64]$totalBytes
    }
}

function Invoke-UvForFixture {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [Parameter()]
        [string]$ProjectEnvironment = ''
    )

    $savedProjectEnvironmentExists = Test-Path -LiteralPath 'Env:\UV_PROJECT_ENVIRONMENT'
    $savedProjectEnvironment = if ($savedProjectEnvironmentExists) {
        (Get-Item -LiteralPath 'Env:\UV_PROJECT_ENVIRONMENT' -ErrorAction Stop).Value
    } else {
        $null
    }

    Remove-Item -LiteralPath 'Env:\UV_PROJECT_ENVIRONMENT' -ErrorAction SilentlyContinue

    if (-not [string]::IsNullOrWhiteSpace($ProjectEnvironment)) {
        Set-Item -LiteralPath 'Env:\UV_PROJECT_ENVIRONMENT' -Value $ProjectEnvironment -ErrorAction Stop
    }

    try {
        return Invoke-NativeChecked @{
            Executable = $script:UvExecutable
            Arguments = $Arguments
            Operation = 'fixture uv command'
        }
    } finally {
        Remove-Item -LiteralPath 'Env:\UV_PROJECT_ENVIRONMENT' -ErrorAction SilentlyContinue

        if ($savedProjectEnvironmentExists) {
            Set-Item -LiteralPath 'Env:\UV_PROJECT_ENVIRONMENT' -Value $savedProjectEnvironment -ErrorAction Stop
        }
    }
}

function Write-FixturePyproject {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$RepositoryPath,

        [Parameter(Mandatory)]
        [string]$Version,

        [Parameter()]
        [string]$RequiresPython = '>=3.14,<3.15'
    )

    Set-Content -LiteralPath (Join-Path -Path $RepositoryPath -ChildPath 'pyproject.toml') -Value @(
        '[project]'
        'name = "fixture"'
        ('version = "{0}"' -f $Version)
        ('requires-python = "{0}"' -f $RequiresPython)
        'dependencies = []'
        ''
        '[tool.uv]'
        'package = false'
    ) -Encoding utf8
}

function Write-FixtureLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$RepositoryPath
    )

    Invoke-UvForFixture -Arguments @(
        '--no-config'
        '--no-python-downloads'
        'lock'
        '--default-index'
        'https://pypi.org/simple'
        '--index-strategy'
        'first-index'
        '--project'
        $RepositoryPath
        '--python'
        $script:PythonExecutable
    ) | Out-Null
}

function Initialize-TestFixture {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Name
    )

    $fixtureRoot = Join-Path -Path $script:TestRoot -ChildPath $Name
    $originPath = Join-Path -Path $fixtureRoot -ChildPath 'origin.git'
    $producerPath = Join-Path -Path $fixtureRoot -ChildPath 'producer'
    $clientPath = Join-Path -Path $fixtureRoot -ChildPath 'client'
    $logPath = Join-Path -Path $fixtureRoot -ChildPath 'logs'
    $dependencyWorkPath = Join-Path -Path $fixtureRoot -ChildPath 'dependency-work'

    New-Item -ItemType Directory -Path $fixtureRoot -Force -ErrorAction Stop | Out-Null

    Invoke-NativeChecked @{
        Executable = $script:GitExecutable
        Arguments = @(
            'init'
            '--bare'
            $originPath
        )
        Operation = "создание bare origin для $Name"
    } | Out-Null

    Invoke-NativeChecked @{
        Executable = $script:GitExecutable
        Arguments = @(
            'init'
            '--initial-branch=personal/stable'
            $producerPath
        )
        Operation = "создание producer для $Name"
    } | Out-Null

    Set-Content -LiteralPath (Join-Path -Path $producerPath -ChildPath 'gui.py') -Value 'print("fixture")' -Encoding utf8
    Set-Content -LiteralPath (Join-Path -Path $producerPath -ChildPath 'app.txt') -Value 'initial' -Encoding utf8
    Set-Content -LiteralPath (Join-Path -Path $producerPath -ChildPath '.gitignore') -Value @(
        '.venv/'
    ) -Encoding utf8
    Write-FixturePyproject -RepositoryPath $producerPath -Version '0.0.0'
    Write-FixtureLock -RepositoryPath $producerPath

    Invoke-GitChecked @{
        RepositoryPath = $producerPath
        Arguments = @(
            'config'
            'user.name'
            'AzurPilot Test'
        )
        Operation = "настройка user.name для $Name"
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $producerPath
        Arguments = @(
            'config'
            'user.email'
            'azurpilot-test@example.invalid'
        )
        Operation = "настройка user.email для $Name"
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $producerPath
        Arguments = @(
            'add'
            '--'
            '.gitignore'
            'gui.py'
            'pyproject.toml'
            'uv.lock'
            'app.txt'
        )
        Operation = "добавление начальных файлов для $Name"
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $producerPath
        Arguments = @(
            'commit'
            '-m'
            'Initial fixture'
        )
        Operation = "создание начального commit для $Name"
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $producerPath
        Arguments = @(
            'remote'
            'add'
            'origin'
            $originPath
        )
        Operation = "добавление origin для $Name"
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $producerPath
        Arguments = @(
            'push'
            '--set-upstream'
            'origin'
            'personal/stable'
        )
        Operation = "публикация начальной ветки для $Name"
    } | Out-Null

    Invoke-NativeChecked @{
        Executable = $script:GitExecutable
        Arguments = @(
            'clone'
            '--branch'
            'personal/stable'
            '--single-branch'
            $originPath
            $clientPath
        )
        Operation = "клонирование client для $Name"
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $clientPath
        Arguments = @(
            'config'
            'user.name'
            'AzurPilot Test'
        )
        Operation = "настройка client user.name для $Name"
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $clientPath
        Arguments = @(
            'config'
            'user.email'
            'azurpilot-test@example.invalid'
        )
        Operation = "настройка client user.email для $Name"
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $clientPath
        Arguments = @(
            'remote'
            'add'
            'upstream'
            $originPath
        )
        Operation = "добавление upstream для $Name"
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $clientPath
        Arguments = @(
            'remote'
            'set-url'
            '--push'
            'upstream'
            'DISABLED'
        )
        Operation = "блокировка upstream push для $Name"
    } | Out-Null

    $clientVenvPath = Join-Path -Path $clientPath -ChildPath '.venv'

    Invoke-UvForFixture -Arguments @(
        '--no-python-downloads'
        'venv'
        $clientVenvPath
        '--python'
        $script:PythonExecutable
    ) | Out-Null

    Invoke-UvForFixture -ProjectEnvironment $clientVenvPath -Arguments @(
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
        $clientPath
        '--python'
        $script:PythonExecutable
    ) | Out-Null

    $originUrl = Get-GitValue @{
        RepositoryPath = $clientPath
        Arguments = @(
            'remote'
            'get-url'
            'origin'
        )
        Operation = "чтение origin URL для $Name"
    }

    return [pscustomobject]@{
        Name = $Name
        Root = $fixtureRoot
        Origin = $originPath
        OriginUrl = $originUrl
        Producer = $producerPath
        Client = $clientPath
        ClientVenv = $clientVenvPath
        Logs = $logPath
        DependencyWork = $dependencyWorkPath
    }
}

function Add-RemoteCommit {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Fixture,

        [Parameter(Mandatory)]
        [string]$RelativePath,

        [Parameter(Mandatory)]
        [string]$Content,

        [Parameter(Mandatory)]
        [string]$Message
    )

    $fullPath = Join-Path -Path $Fixture.Producer -ChildPath $RelativePath
    Set-Content -LiteralPath $fullPath -Value $Content -Encoding utf8

    Invoke-GitChecked @{
        RepositoryPath = $Fixture.Producer
        Arguments = @(
            'add'
            '--'
            $RelativePath
        )
        Operation = "добавление remote-изменения $RelativePath"
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $Fixture.Producer
        Arguments = @(
            'commit'
            '-m'
            $Message
        )
        Operation = "создание remote commit $Message"
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $Fixture.Producer
        Arguments = @(
            'push'
            'origin'
            'personal/stable'
        )
        Operation = "публикация remote commit $Message"
    } | Out-Null
}

function Add-RemoteDependencyCommit {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Fixture,

        [Parameter(Mandatory)]
        [string]$Version,

        [Parameter()]
        [string]$RequiresPython = '>=3.14,<3.15',

        [Parameter()]
        [switch]$CorruptLock
    )

    Write-FixturePyproject -RepositoryPath $Fixture.Producer -Version $Version -RequiresPython $RequiresPython

    if ($CorruptLock) {
        Set-Content -LiteralPath (Join-Path -Path $Fixture.Producer -ChildPath 'uv.lock') -Value 'invalid = [' -Encoding utf8
    } else {
        Write-FixtureLock -RepositoryPath $Fixture.Producer
    }

    Invoke-GitChecked @{
        RepositoryPath = $Fixture.Producer
        Arguments = @(
            'add'
            '--'
            'pyproject.toml'
            'uv.lock'
        )
        Operation = 'добавление remote dependency update'
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $Fixture.Producer
        Arguments = @(
            'commit'
            '-m'
            "Dependency update $Version"
        )
        Operation = 'создание remote dependency commit'
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $Fixture.Producer
        Arguments = @(
            'push'
            'origin'
            'personal/stable'
        )
        Operation = 'публикация remote dependency commit'
    } | Out-Null
}

function Add-LocalCommit {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Fixture,

        [Parameter(Mandatory)]
        [string]$Content,

        [Parameter(Mandatory)]
        [string]$Message
    )

    Set-Content -LiteralPath (Join-Path -Path $Fixture.Client -ChildPath 'app.txt') -Value $Content -Encoding utf8

    Invoke-GitChecked @{
        RepositoryPath = $Fixture.Client
        Arguments = @(
            'add'
            '--'
            'app.txt'
        )
        Operation = "добавление local-изменения $Message"
    } | Out-Null

    Invoke-GitChecked @{
        RepositoryPath = $Fixture.Client
        Arguments = @(
            'commit'
            '-m'
            $Message
        )
        Operation = "создание local commit $Message"
    } | Out-Null
}

function Get-DependencyTransactionDirectory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Fixture
    )

    if (-not (Test-Path -LiteralPath $Fixture.DependencyWork -PathType Container)) {
        return @()
    }

    return @(
        Get-ChildItem -LiteralPath $Fixture.DependencyWork -Directory -Force -ErrorAction Stop
    )
}

function Assert-NoDependencyTransaction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Fixture,

        [Parameter(Mandatory)]
        [string]$Message
    )

    [object[]]$transactionDirectories = @(
        Get-DependencyTransactionDirectory -Fixture $Fixture
    )

    Assert-Equal -Expected 0 -Actual $transactionDirectories.Count -Message $Message
}

function Invoke-Updater {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Fixture,

        [Parameter()]
        [string]$ExpectedOriginUrl = '',

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

    $originUrl = $ExpectedOriginUrl

    if ([string]::IsNullOrWhiteSpace($originUrl)) {
        $originUrl = $Fixture.OriginUrl
    }

    $arguments = @(
        '-NoProfile'
        '-ExecutionPolicy'
        'Bypass'
        '-File'
        $UpdaterPath
        '-RepositoryPath'
        $Fixture.Client
        '-ExpectedBranch'
        'personal/stable'
        '-ExpectedOriginUrl'
        $originUrl
        '-RemoteName'
        'origin'
        '-RemoteBranch'
        'personal/stable'
        '-RequiredUpstreamPushUrl'
        'DISABLED'
        '-LogDirectory'
        $Fixture.Logs
        '-UvExecutablePath'
        $script:UvExecutable
        '-PythonExecutablePath'
        $script:PythonExecutable
        '-DependencyWorkRoot'
        $Fixture.DependencyWork
        '-RobocopyExecutablePath'
        $script:RobocopyExecutable
        '-TarExecutablePath'
        $script:TarExecutable
    )

    if ($TestFailPoint -ne 'None') {
        $arguments += @(
            '-TestFailPoint'
            $TestFailPoint
        )
    }

    if (-not [string]::IsNullOrWhiteSpace($TestMutationRelativePath)) {
        $arguments += @(
            '-TestMutationRelativePath'
            $TestMutationRelativePath
        )
    }

    $output = & $script:PwshExecutable @arguments 2>&1
    $exitCode = $LASTEXITCODE
    $output = @($output)

    Write-ConsoleMessage -Message ''
    Write-ConsoleMessage -Message "--- $($Fixture.Name): exit $exitCode ---"

    foreach ($line in $output) {
        Write-ConsoleMessage -Message ([string]$line)
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = [string[]]$output
    }
}

$utf8Encoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8Encoding
[Console]::OutputEncoding = $utf8Encoding
$OutputEncoding = $utf8Encoding
$env:PYTHONIOENCODING = 'utf-8'

if (-not (Test-Path -LiteralPath $UpdaterPath -PathType Leaf)) {
    throw "Updater не найден: $UpdaterPath"
}

$UpdaterPath = (Resolve-Path -LiteralPath $UpdaterPath -ErrorAction Stop).Path
$updaterRepositoryPath = Split-Path -Parent (Split-Path -Parent $UpdaterPath)

$gitCommands = @(
    Get-Command -Name 'git' -CommandType Application -ErrorAction Stop
)

if ($gitCommands.Count -eq 0) {
    throw 'Git не найден в PATH.'
}

$script:GitExecutable = $gitCommands[0].Source
$script:PwshExecutable = Join-Path -Path $PSHOME -ChildPath 'pwsh.exe'

if ([string]::IsNullOrWhiteSpace($UvExecutablePath)) {
    $UvExecutablePath = Join-Path -Path $updaterRepositoryPath -ChildPath '.venv\Scripts\uv.exe'
}

if ([string]::IsNullOrWhiteSpace($PythonExecutablePath)) {
    $PythonExecutablePath = Join-Path -Path $updaterRepositoryPath -ChildPath '.venv\Scripts\python.exe'
}

if ([string]::IsNullOrWhiteSpace($RobocopyExecutablePath)) {
    $RobocopyExecutablePath = Join-Path -Path $env:SystemRoot -ChildPath 'System32\robocopy.exe'
}

if ([string]::IsNullOrWhiteSpace($TarExecutablePath)) {
    $TarExecutablePath = Join-Path -Path $env:SystemRoot -ChildPath 'System32\tar.exe'
}

$script:UvExecutable = (Resolve-Path -LiteralPath $UvExecutablePath -ErrorAction Stop).Path
$script:PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutablePath -ErrorAction Stop).Path
$script:RobocopyExecutable = (Resolve-Path -LiteralPath $RobocopyExecutablePath -ErrorAction Stop).Path
$script:TarExecutable = (Resolve-Path -LiteralPath $TarExecutablePath -ErrorAction Stop).Path

foreach ($requiredExecutable in @(
    $script:PwshExecutable
    $script:UvExecutable
    $script:PythonExecutable
    $script:RobocopyExecutable
    $script:TarExecutable
)) {
    if (-not (Test-Path -LiteralPath $requiredExecutable -PathType Leaf)) {
        throw "Обязательный executable не найден: $requiredExecutable"
    }
}

$script:TestRoot = Join-Path -Path $env:TEMP -ChildPath "AzurPilot-UpdateTests-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $script:TestRoot -Force -ErrorAction Stop | Out-Null

$testSucceeded = $false

try {
    Write-ConsoleMessage -Message "Изолированные fixtures: $script:TestRoot"
    Write-ConsoleMessage -Message "uv: $script:UvExecutable"
    Write-ConsoleMessage -Message "Python: $script:PythonExecutable"

    $fixture = Initialize-TestFixture -Name '01-up-to-date'
    $headBefore = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD до no-op'
    }
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 0 -Actual $result.ExitCode -Message 'No-op обновление должно завершаться успешно.'
    $headAfter = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD после no-op'
    }
    Assert-Equal -Expected $headBefore -Actual $headAfter -Message 'No-op не должен менять HEAD.'

    $secondResult = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 0 -Actual $secondResult.ExitCode -Message 'Повторный no-op должен быть идемпотентным.'

    $fixture = Initialize-TestFixture -Name '02-fast-forward'
    Add-RemoteCommit -Fixture $fixture -RelativePath 'app.txt' -Content 'remote update' -Message 'Remote code update'
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 0 -Actual $result.ExitCode -Message 'Fast-forward обновление должно завершаться успешно.'
    $clientHead = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'client HEAD после fast-forward'
    }
    $remoteHead = Get-GitValue @{
        RepositoryPath = $fixture.Producer
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'remote HEAD после fast-forward'
    }
    Assert-Equal -Expected $remoteHead -Actual $clientHead -Message 'После fast-forward client HEAD должен совпадать с remote HEAD.'

    $fixture = Initialize-TestFixture -Name '03-dirty-tree'
    Add-Content -LiteralPath (Join-Path -Path $fixture.Client -ChildPath 'app.txt') -Value 'dirty' -Encoding utf8
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 20 -Actual $result.ExitCode -Message 'Грязное рабочее дерево должно блокировать обновление.'

    $fixture = Initialize-TestFixture -Name '04-local-ahead'
    Add-LocalCommit -Fixture $fixture -Content 'local ahead' -Message 'Local ahead'
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 21 -Actual $result.ExitCode -Message 'Локальная ветка впереди должна блокировать обновление.'

    $fixture = Initialize-TestFixture -Name '05-diverged'
    Add-LocalCommit -Fixture $fixture -Content 'local divergent' -Message 'Local divergent'
    Add-RemoteCommit -Fixture $fixture -RelativePath 'gui.py' -Content 'print("remote divergent")' -Message 'Remote divergent'
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 22 -Actual $result.ExitCode -Message 'Разошедшиеся ветки должны блокировать обновление.'

    $fixture = Initialize-TestFixture -Name '06-dependency-fast-forward'
    $venvSnapshotBefore = Get-DirectorySnapshot -LiteralPath $fixture.ClientVenv
    Add-RemoteDependencyCommit -Fixture $fixture -Version '0.0.1'
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 0 -Actual $result.ExitCode -Message 'Корректное изменение зависимостей должно завершаться успешно.'
    $clientHead = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'client HEAD после dependency fast-forward'
    }
    $remoteHead = Get-GitValue @{
        RepositoryPath = $fixture.Producer
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'remote HEAD после dependency fast-forward'
    }
    Assert-Equal -Expected $remoteHead -Actual $clientHead -Message 'Dependency fast-forward должен обновить HEAD.'
    $venvSnapshotAfter = Get-DirectorySnapshot -LiteralPath $fixture.ClientVenv
    Assert-Equal -Expected $venvSnapshotBefore.FileCount -Actual $venvSnapshotAfter.FileCount -Message 'Пустое dependency update не должно менять число файлов .venv.'
    Assert-Equal -Expected $venvSnapshotBefore.TotalBytes -Actual $venvSnapshotAfter.TotalBytes -Message 'Пустое dependency update не должно менять размер .venv.'
    Assert-NoDependencyTransaction -Fixture $fixture -Message 'Успешная транзакция не должна оставлять временные dependency transactions.'

    $fixture = Initialize-TestFixture -Name '07-recovery-after-backup'
    $headBefore = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD до failpoint AfterBackup'
    }
    Add-RemoteDependencyCommit -Fixture $fixture -Version '0.0.2'
    $markerRelativePath = 'recovery-marker-after-backup.txt'
    $markerPath = Join-Path -Path $fixture.ClientVenv -ChildPath $markerRelativePath
    $result = Invoke-Updater -Fixture $fixture -TestFailPoint 'AfterBackup' -TestMutationRelativePath $markerRelativePath
    Assert-Equal -Expected 91 -Actual $result.ExitCode -Message 'Failpoint AfterBackup должен аварийно завершать updater кодом 91.'
    $headAfterCrash = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD после failpoint AfterBackup'
    }
    Assert-Equal -Expected $headBefore -Actual $headAfterCrash -Message 'Failpoint AfterBackup не должен менять HEAD.'
    Assert-True -Condition (Test-Path -LiteralPath $markerPath -PathType Leaf) -Message 'Failpoint AfterBackup должен оставить тестовую мутацию .venv.'
    [object[]]$transactionDirectories = @(
        Get-DependencyTransactionDirectory -Fixture $fixture
    )
    Assert-Equal -Expected 1 -Actual $transactionDirectories.Count -Message 'Failpoint AfterBackup должен оставить один recovery journal.'
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 0 -Actual $result.ExitCode -Message 'Повторный запуск должен восстановить .venv после AfterBackup и завершить обновление.'
    Assert-True -Condition (-not (Test-Path -LiteralPath $markerPath)) -Message 'Recovery после AfterBackup должен удалить тестовую мутацию восстановлением backup.'
    $clientHead = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD после recovery AfterBackup'
    }
    $remoteHead = Get-GitValue @{
        RepositoryPath = $fixture.Producer
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'remote HEAD после recovery AfterBackup'
    }
    Assert-Equal -Expected $remoteHead -Actual $clientHead -Message 'Recovery после AfterBackup должен затем выполнить fast-forward.'
    Assert-NoDependencyTransaction -Fixture $fixture -Message 'Recovery после AfterBackup должен удалить transaction.'

    $fixture = Initialize-TestFixture -Name '08-recovery-after-sync'
    $headBefore = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD до failpoint AfterSync'
    }
    Add-RemoteDependencyCommit -Fixture $fixture -Version '0.0.3'
    $markerRelativePath = 'recovery-marker-after-sync.txt'
    $markerPath = Join-Path -Path $fixture.ClientVenv -ChildPath $markerRelativePath
    $result = Invoke-Updater -Fixture $fixture -TestFailPoint 'AfterSync' -TestMutationRelativePath $markerRelativePath
    Assert-Equal -Expected 92 -Actual $result.ExitCode -Message 'Failpoint AfterSync должен аварийно завершать updater кодом 92.'
    $headAfterCrash = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD после failpoint AfterSync'
    }
    Assert-Equal -Expected $headBefore -Actual $headAfterCrash -Message 'Failpoint AfterSync не должен менять HEAD.'
    Assert-True -Condition (Test-Path -LiteralPath $markerPath -PathType Leaf) -Message 'Failpoint AfterSync должен оставить тестовую мутацию .venv.'
    [object[]]$transactionDirectories = @(
        Get-DependencyTransactionDirectory -Fixture $fixture
    )
    Assert-Equal -Expected 1 -Actual $transactionDirectories.Count -Message 'Failpoint AfterSync должен оставить один recovery journal.'
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 0 -Actual $result.ExitCode -Message 'Повторный запуск должен восстановить .venv после AfterSync и завершить обновление.'
    Assert-True -Condition (-not (Test-Path -LiteralPath $markerPath)) -Message 'Recovery после AfterSync должен удалить тестовую мутацию восстановлением backup.'
    $clientHead = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD после recovery AfterSync'
    }
    $remoteHead = Get-GitValue @{
        RepositoryPath = $fixture.Producer
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'remote HEAD после recovery AfterSync'
    }
    Assert-Equal -Expected $remoteHead -Actual $clientHead -Message 'Recovery после AfterSync должен затем выполнить fast-forward.'
    Assert-NoDependencyTransaction -Fixture $fixture -Message 'Recovery после AfterSync должен удалить transaction.'

    $fixture = Initialize-TestFixture -Name '09-recovery-after-merge'
    Add-RemoteDependencyCommit -Fixture $fixture -Version '0.0.4'
    $markerRelativePath = 'recovery-marker-after-merge.txt'
    $markerPath = Join-Path -Path $fixture.ClientVenv -ChildPath $markerRelativePath
    $result = Invoke-Updater -Fixture $fixture -TestFailPoint 'AfterMerge' -TestMutationRelativePath $markerRelativePath
    Assert-Equal -Expected 93 -Actual $result.ExitCode -Message 'Failpoint AfterMerge должен аварийно завершать updater кодом 93.'
    $clientHead = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD после failpoint AfterMerge'
    }
    $remoteHead = Get-GitValue @{
        RepositoryPath = $fixture.Producer
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'remote HEAD после failpoint AfterMerge'
    }
    Assert-Equal -Expected $remoteHead -Actual $clientHead -Message 'Failpoint AfterMerge должен происходить после изменения HEAD.'
    Assert-True -Condition (Test-Path -LiteralPath $markerPath -PathType Leaf) -Message 'Failpoint AfterMerge должен оставить тестовую мутацию после backup.'
    [object[]]$transactionDirectories = @(
        Get-DependencyTransactionDirectory -Fixture $fixture
    )
    Assert-Equal -Expected 1 -Actual $transactionDirectories.Count -Message 'Failpoint AfterMerge должен оставить один recovery journal.'
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 0 -Actual $result.ExitCode -Message 'Повторный запуск должен завершить cleanup после AfterMerge.'
    Assert-True -Condition (Test-Path -LiteralPath $markerPath -PathType Leaf) -Message 'Recovery после AfterMerge не должен восстанавливать старую .venv поверх нового HEAD.'
    Assert-NoDependencyTransaction -Fixture $fixture -Message 'Recovery после AfterMerge должен удалить transaction.'

    $fixture = Initialize-TestFixture -Name '10-corrupt-recovery-journal'
    $headBefore = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD до corrupt recovery journal'
    }
    $corruptTransactionName = "dependency-$headBefore-$([guid]::NewGuid().ToString('N'))"
    $corruptTransactionPath = Join-Path -Path $fixture.DependencyWork -ChildPath $corruptTransactionName
    New-Item -ItemType Directory -Path $corruptTransactionPath -Force -ErrorAction Stop | Out-Null
    $corruptJournalPath = Join-Path -Path $corruptTransactionPath -ChildPath 'journal.json'
    Set-Content -LiteralPath $corruptJournalPath -Value '{invalid-json' -Encoding utf8NoBOM -ErrorAction Stop
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 20 -Actual $result.ExitCode -Message 'Повреждённый recovery journal должен блокировать updater кодом 20.'
    $headAfter = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD после corrupt recovery journal'
    }
    Assert-Equal -Expected $headBefore -Actual $headAfter -Message 'Повреждённый journal не должен менять HEAD.'
    Assert-True -Condition (Test-Path -LiteralPath $corruptJournalPath -PathType Leaf) -Message 'Повреждённый journal должен сохраняться для анализа.'

    $fixture = Initialize-TestFixture -Name '11-ambiguous-recovery-journals'
    $headBefore = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD до ambiguous recovery journals'
    }
    foreach ($journalIndex in 1..2) {
        $transactionName = "dependency-$headBefore-$([guid]::NewGuid().ToString('N'))"
        $transactionPath = Join-Path -Path $fixture.DependencyWork -ChildPath $transactionName
        New-Item -ItemType Directory -Path $transactionPath -Force -ErrorAction Stop | Out-Null
        $journalPath = Join-Path -Path $transactionPath -ChildPath 'journal.json'
        Set-Content -LiteralPath $journalPath -Value '{}' -Encoding utf8NoBOM -ErrorAction Stop
    }
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 20 -Actual $result.ExitCode -Message 'Несколько recovery journals должны блокировать updater кодом 20.'
    [object[]]$transactionDirectories = @(
        Get-DependencyTransactionDirectory -Fixture $fixture
    )
    Assert-Equal -Expected 2 -Actual $transactionDirectories.Count -Message 'Неоднозначные journals должны сохраняться для анализа.'

    $fixture = Initialize-TestFixture -Name '12-orphan-transaction-cleanup'
    $headBefore = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD до orphan transaction cleanup'
    }
    $orphanTransactionName = "dependency-$headBefore-$([guid]::NewGuid().ToString('N'))"
    $orphanTransactionPath = Join-Path -Path $fixture.DependencyWork -ChildPath $orphanTransactionName
    New-Item -ItemType Directory -Path $orphanTransactionPath -Force -ErrorAction Stop | Out-Null
    Set-Content -LiteralPath (Join-Path -Path $orphanTransactionPath -ChildPath 'candidate.tmp') -Value 'orphan' -Encoding utf8NoBOM -ErrorAction Stop
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 0 -Actual $result.ExitCode -Message 'Orphan transaction без journal должна безопасно удаляться.'
    Assert-True -Condition (-not (Test-Path -LiteralPath $orphanTransactionPath)) -Message 'Orphan transaction должна быть удалена.'
    $headAfter = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD после orphan transaction cleanup'
    }
    Assert-Equal -Expected $headBefore -Actual $headAfter -Message 'Orphan cleanup не должен менять HEAD.'

    $fixture = Initialize-TestFixture -Name '13-invalid-dependency-lock'
    $headBefore = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD до invalid dependency lock'
    }
    $venvSnapshotBefore = Get-DirectorySnapshot -LiteralPath $fixture.ClientVenv
    Add-RemoteDependencyCommit -Fixture $fixture -Version '0.0.5' -CorruptLock
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 23 -Actual $result.ExitCode -Message 'Некорректный lockfile должен блокировать fast-forward.'
    $headAfter = Get-GitValue @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'rev-parse'
            'HEAD'
        )
        Operation = 'HEAD после invalid dependency lock'
    }
    Assert-Equal -Expected $headBefore -Actual $headAfter -Message 'Ошибка зависимостей не должна менять HEAD.'
    $venvSnapshotAfter = Get-DirectorySnapshot -LiteralPath $fixture.ClientVenv
    Assert-Equal -Expected $venvSnapshotBefore.FileCount -Actual $venvSnapshotAfter.FileCount -Message 'Ошибка до sync не должна менять число файлов .venv.'
    Assert-Equal -Expected $venvSnapshotBefore.TotalBytes -Actual $venvSnapshotAfter.TotalBytes -Message 'Ошибка до sync не должна менять размер .venv.'
    Assert-NoDependencyTransaction -Fixture $fixture -Message 'Ошибка до backup не должна оставлять transaction.'

    $fixture = Initialize-TestFixture -Name '14-network-failure'
    $missingOrigin = Join-Path -Path $fixture.Root -ChildPath 'missing-origin.git'
    Invoke-GitChecked @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'remote'
            'set-url'
            'origin'
            $missingOrigin
        )
        Operation = 'настройка недоступного origin'
    } | Out-Null
    $result = Invoke-Updater -Fixture $fixture -ExpectedOriginUrl $missingOrigin
    Assert-Equal -Expected 10 -Actual $result.ExitCode -Message 'Недоступный origin должен возвращать код сетевой ошибки.'

    $fixture = Initialize-TestFixture -Name '15-wrong-branch'
    Invoke-GitChecked @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'switch'
            '-c'
            'feature/test'
        )
        Operation = 'создание неправильной ветки'
    } | Out-Null
    $result = Invoke-Updater -Fixture $fixture
    Assert-Equal -Expected 20 -Actual $result.ExitCode -Message 'Неправильная ветка должна блокировать обновление.'

    $fixture = Initialize-TestFixture -Name '16-wrong-origin'
    $wrongOrigin = Join-Path -Path $fixture.Root -ChildPath 'other-origin.git'
    Invoke-NativeChecked @{
        Executable = $script:GitExecutable
        Arguments = @(
            'init'
            '--bare'
            $wrongOrigin
        )
        Operation = 'создание неправильного origin'
    } | Out-Null
    Invoke-GitChecked @{
        RepositoryPath = $fixture.Client
        Arguments = @(
            'remote'
            'set-url'
            'origin'
            $wrongOrigin
        )
        Operation = 'подмена origin'
    } | Out-Null
    $result = Invoke-Updater -Fixture $fixture -ExpectedOriginUrl $fixture.OriginUrl
    Assert-Equal -Expected 20 -Actual $result.ExitCode -Message 'Подменённый origin должен блокировать обновление.'

    $testSucceeded = $true
    Write-ConsoleMessage -Message ''
    Write-ConsoleMessage -Message 'Все изолированные тесты updater, dependency sync и recovery journal пройдены.'
} finally {
    if ($testSucceeded -and -not $KeepFixtures) {
        Remove-Item -LiteralPath $script:TestRoot -Recurse -Force -ErrorAction Stop
        Write-ConsoleMessage -Message 'Временные fixtures удалены.'
    } else {
        Write-ConsoleMessage -Message "Fixtures сохранены для анализа: $script:TestRoot"
    }
}
