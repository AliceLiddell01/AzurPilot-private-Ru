#requires -Version 7.6

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$LifecycleModulePath,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$StopScriptPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

function Assert-True {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [bool]$Condition,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Get-DisposablePort {
    [CmdletBinding()]
    param()

    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)

    try {
        $listener.Start()
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}

$resolvedModulePath = (Resolve-Path -LiteralPath $LifecycleModulePath -ErrorAction Stop).Path
$resolvedStopScriptPath = (Resolve-Path -LiteralPath $StopScriptPath -ErrorAction Stop).Path
Import-Module -Name $resolvedModulePath -Force -ErrorAction Stop

$testRoot = Join-Path -Path ([System.IO.Path]::GetTempPath()) -ChildPath ("azurpilot-lifecycle-{0}" -f [guid]::NewGuid().ToString('N'))
$ownedProcess = $null
$mutexProcess = $null
$startingOwnerProcess = $null
$concurrentStopProcesses = @()
$concurrentStopOutputs = @()

try {
    New-Item -ItemType Directory -Path $testRoot -Force -ErrorAction Stop | Out-Null
    $repositoryPath = Join-Path -Path $testRoot -ChildPath 'Repository With Spaces'
    New-Item -ItemType Directory -Path $repositoryPath -Force -ErrorAction Stop | Out-Null
    $guiPath = Join-Path -Path $repositoryPath -ChildPath 'gui.py'
    New-Item -ItemType File -Path $guiPath -Force -ErrorAction Stop | Out-Null

    $names = Get-AzurPilotLifecycleName -RepositoryPath $repositoryPath
    $sameNames = Get-AzurPilotLifecycleName -RepositoryPath ($repositoryPath + [System.IO.Path]::DirectorySeparatorChar)
    Assert-True -Condition ($names.StartMutex -eq $sameNames.StartMutex) -Message 'Lifecycle namespace должен нормализовать завершающий separator.'
    $yamlContractPath = Join-Path -Path $testRoot -ChildPath 'yaml-contract.yaml'
    Set-Content -LiteralPath $yamlContractPath -Encoding utf8BOM -Value 'WebuiPort: "25548" # inline comment'
    Assert-True -Condition ((Get-YamlScalarValue -Path $yamlContractPath -Key 'WebuiPort') -eq '25548') -Message 'Общий YAML parser должен одинаково обрабатывать кавычки и inline comment.'

    $stopEvent = New-AzurPilotStopEvent -Name $names.StopEvent -Confirm:$false

    try {
        Assert-True -Condition (-not (Test-AzurPilotStopRequested -StopEvent $stopEvent)) -Message 'Новый stop event должен быть сброшен.'
        Assert-True -Condition (Send-AzurPilotStopRequest -Name $names.StopEvent) -Message 'Stop request должен открыть и установить существующий event.'
        Assert-True -Condition (Test-AzurPilotStopRequested -StopEvent $stopEvent) -Message 'Owner должен увидеть stop request.'
        Assert-True -Condition (Send-AzurPilotStopRequest -Name $names.StopEvent) -Message 'Повторный stop request должен быть идемпотентным.'
    } finally {
        $stopEvent.Dispose()
    }

    Assert-True -Condition (-not (Send-AzurPilotStopRequest -Name $names.StopEvent)) -Message 'Disposed event не должен создавать phantom state.'

    $pwshCommand = Get-Command -Name pwsh -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $mutexHolderPath = Join-Path -Path $testRoot -ChildPath 'mutex-holder.ps1'
    $mutexReadyPath = Join-Path -Path $testRoot -ChildPath 'mutex-ready.txt'
    Set-Content -LiteralPath $mutexHolderPath -Encoding utf8BOM -Value @'
#requires -Version 7.6
param([string]$Name, [string]$ReadyPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$mutex = [System.Threading.Mutex]::new($false, $Name)
try {
    [void]$mutex.WaitOne()
    Set-Content -LiteralPath $ReadyPath -Value $PID -Encoding ascii
    Start-Sleep -Seconds 120
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
'@
    $mutexStartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $mutexStartInfo.FileName = $pwshCommand.Path
    $mutexStartInfo.UseShellExecute = $false
    $mutexStartInfo.CreateNoWindow = $true
    foreach ($argument in @('-NoLogo', '-NoProfile', '-File', $mutexHolderPath, '-Name', $names.StartMutex, '-ReadyPath', $mutexReadyPath)) {
        [void]$mutexStartInfo.ArgumentList.Add($argument)
    }
    $mutexProcess = [System.Diagnostics.Process]::Start($mutexStartInfo)
    $mutexDeadline = [DateTimeOffset]::UtcNow.AddSeconds(10)

    while (-not (Test-Path -LiteralPath $mutexReadyPath -PathType Leaf) -and [DateTimeOffset]::UtcNow -lt $mutexDeadline) {
        Start-Sleep -Milliseconds 100
    }

    Assert-True -Condition (Test-AzurPilotMutexOwned -Name $names.StartMutex) -Message 'Shared helper должен видеть активного owner из другого процесса.'
    $mutexProcess.Kill($true)
    [void]$mutexProcess.WaitForExit(5000)
    Assert-True -Condition (-not (Test-AzurPilotMutexOwned -Name $names.StartMutex)) -Message 'После смерти owner mutex не должен считаться активным.'

    $venvScriptsPath = Join-Path -Path $repositoryPath -ChildPath '.venv\Scripts'
    $configPath = Join-Path -Path $repositoryPath -ChildPath 'config'
    New-Item -ItemType Directory -Path $venvScriptsPath -Force -ErrorAction Stop | Out-Null
    New-Item -ItemType Directory -Path $configPath -Force -ErrorAction Stop | Out-Null
    New-Item -ItemType File -Path (Join-Path -Path $venvScriptsPath -ChildPath 'python.exe') -Force -ErrorAction Stop | Out-Null
    $startingPort = Get-DisposablePort
    Set-Content -LiteralPath (Join-Path -Path $configPath -ChildPath 'deploy.yaml') -Value "WebuiPort: $startingPort" -Encoding utf8BOM
    $startingOwnerPath = Join-Path -Path $testRoot -ChildPath 'starting-owner.ps1'
    $startingReadyPath = Join-Path -Path $testRoot -ChildPath 'starting-ready.txt'
    $startingStoppedPath = Join-Path -Path $testRoot -ChildPath 'starting-stopped.txt'
    Set-Content -LiteralPath $startingOwnerPath -Encoding utf8BOM -Value @'
#requires -Version 7.6
param([string]$MutexName, [string]$EventName, [string]$ReadyPath, [string]$StoppedPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$mutex = [System.Threading.Mutex]::new($false, $MutexName)
$stopEvent = $null
try {
    [void]$mutex.WaitOne()
    $stopEvent = [System.Threading.EventWaitHandle]::new($false, [System.Threading.EventResetMode]::ManualReset, $EventName)
    Set-Content -LiteralPath $ReadyPath -Value $PID -Encoding ascii
    if (-not $stopEvent.WaitOne([TimeSpan]::FromSeconds(20))) {
        throw 'Stop request не получен.'
    }
    Set-Content -LiteralPath $StoppedPath -Value 'stopped' -Encoding ascii
} finally {
    if ($null -ne $stopEvent) {
        $stopEvent.Dispose()
    }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
'@
    $startingOwnerInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startingOwnerInfo.FileName = $pwshCommand.Path
    $startingOwnerInfo.UseShellExecute = $false
    $startingOwnerInfo.CreateNoWindow = $true
    foreach ($argument in @('-NoLogo', '-NoProfile', '-File', $startingOwnerPath, '-MutexName', $names.StartMutex, '-EventName', $names.StopEvent, '-ReadyPath', $startingReadyPath, '-StoppedPath', $startingStoppedPath)) {
        [void]$startingOwnerInfo.ArgumentList.Add($argument)
    }
    $startingOwnerProcess = [System.Diagnostics.Process]::Start($startingOwnerInfo)
    $startingDeadline = [DateTimeOffset]::UtcNow.AddSeconds(10)

    while (-not (Test-Path -LiteralPath $startingReadyPath -PathType Leaf) -and [DateTimeOffset]::UtcNow -lt $startingDeadline) {
        Start-Sleep -Milliseconds 100
    }

    Assert-True -Condition (Test-Path -LiteralPath $startingReadyPath -PathType Leaf) -Message 'Synthetic STARTING owner не подтвердил готовность.'

    foreach ($index in 1..2) {
        $stopInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $stopInfo.FileName = $pwshCommand.Path
        $stopInfo.UseShellExecute = $false
        $stopInfo.CreateNoWindow = $true
        $stopInfo.RedirectStandardOutput = $true
        $stopInfo.RedirectStandardError = $true
        $stopInfo.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
        $stopInfo.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
        foreach ($argument in @('-NoLogo', '-NoProfile', '-File', $resolvedStopScriptPath, '-RepositoryPath', $repositoryPath, '-TimeoutSeconds', '20')) {
            [void]$stopInfo.ArgumentList.Add($argument)
        }
        $stopProcess = [System.Diagnostics.Process]::Start($stopInfo)
        $stopProcess | Add-Member -NotePropertyName StandardOutputTask -NotePropertyValue $stopProcess.StandardOutput.ReadToEndAsync()
        $stopProcess | Add-Member -NotePropertyName StandardErrorTask -NotePropertyValue $stopProcess.StandardError.ReadToEndAsync()
        $concurrentStopProcesses += $stopProcess
    }

    foreach ($stopProcess in $concurrentStopProcesses) {
        Assert-True -Condition ($stopProcess.WaitForExit(25000)) -Message 'Concurrent Stop не завершился bounded.'
        $stopOutput = $stopProcess.StandardOutputTask.GetAwaiter().GetResult()
        $stopError = $stopProcess.StandardErrorTask.GetAwaiter().GetResult()
        $concurrentStopOutputs += $stopOutput
        Assert-True -Condition ($stopProcess.ExitCode -eq 0) -Message ("Concurrent Stop завершился с кодом {0}. stdout={1} stderr={2}" -f $stopProcess.ExitCode, $stopOutput, $stopError)
    }

    $concurrentOutputText = $concurrentStopOutputs -join "`n"
    Assert-True -Condition ($concurrentOutputText -match 'controller AzurPilot') -Message ("Один concurrent Stop должен наблюдать активный STARTING controller. Вывод: {0}" -f $concurrentOutputText)
    Assert-True -Condition ($concurrentOutputText -notmatch 'fallback') -Message ("Concurrent Stop during STARTING не должен применять fallback. Вывод: {0}" -f $concurrentOutputText)

    Assert-True -Condition ($startingOwnerProcess.WaitForExit(5000)) -Message 'Synthetic STARTING owner не завершился после stop request.'
    Assert-True -Condition (Test-Path -LiteralPath $startingStoppedPath -PathType Leaf) -Message 'Stop during STARTING не был подтверждён owner.'

    $listenerScriptPath = Join-Path -Path $testRoot -ChildPath 'listener.ps1'
    $readyPath = Join-Path -Path $testRoot -ChildPath 'ready.txt'
    Set-Content -LiteralPath $listenerScriptPath -Encoding utf8BOM -Value @'
#requires -Version 7.6
param([int]$Port, [string]$ReadyPath, [string]$GuiPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
try {
    $listener.Start()
    Set-Content -LiteralPath $ReadyPath -Value $PID -Encoding ascii
    Start-Sleep -Seconds 120
} finally {
    $listener.Stop()
}
'@
    $port = Get-DisposablePort
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $pwshCommand.Path
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    [void]$startInfo.ArgumentList.Add('-NoLogo')
    [void]$startInfo.ArgumentList.Add('-NoProfile')
    [void]$startInfo.ArgumentList.Add('-File')
    [void]$startInfo.ArgumentList.Add($listenerScriptPath)
    [void]$startInfo.ArgumentList.Add('-Port')
    [void]$startInfo.ArgumentList.Add([string]$port)
    [void]$startInfo.ArgumentList.Add('-ReadyPath')
    [void]$startInfo.ArgumentList.Add($readyPath)
    [void]$startInfo.ArgumentList.Add('-GuiPath')
    [void]$startInfo.ArgumentList.Add($guiPath)
    $ownedProcess = [System.Diagnostics.Process]::Start($startInfo)
    $readyDeadline = [DateTimeOffset]::UtcNow.AddSeconds(10)

    while (-not (Test-Path -LiteralPath $readyPath -PathType Leaf) -and [DateTimeOffset]::UtcNow -lt $readyDeadline) {
        Start-Sleep -Milliseconds 100
    }

    Assert-True -Condition (Test-Path -LiteralPath $readyPath -PathType Leaf) -Message 'Disposable listener не подтвердил готовность.'
    Set-Content -LiteralPath (Join-Path -Path $configPath -ChildPath 'deploy.yaml') -Value "WebuiPort: $port" -Encoding utf8BOM
    $foreignStopOutput = @(
        & $pwshCommand.Path -NoLogo -NoProfile -File $resolvedStopScriptPath -RepositoryPath $repositoryPath -TimeoutSeconds 10 2>&1
    )
    $foreignStopExitCode = $LASTEXITCODE
    Assert-True -Condition ($foreignStopExitCode -eq 21) -Message ("Stop foreign-process contract должен вернуть 21, получен {0}. Вывод: {1}" -f $foreignStopExitCode, ($foreignStopOutput -join [Environment]::NewLine))
    Assert-True -Condition (-not $ownedProcess.HasExited) -Message 'Полный Stop foreign-process scenario не должен завершать listener.'
    $ownership = Get-AzurPilotPortOwnershipState -Port $port -ProjectPythonPath $pwshCommand.Path -GuiPath $guiPath
    Assert-True -Condition ($ownership.State -eq 'AzurPilot') -Message 'Exact executable + gui.py argument должны доказать ownership.'
    Assert-True -Condition ($ownership.RootProcessIds -contains $ownedProcess.Id) -Message 'Ownership evidence должен указывать корневой disposable process.'
    $repositoryEvidence = @(Get-AzurPilotRepositoryProcessEvidence -ProjectPythonPath $pwshCommand.Path -GuiPath $guiPath)
    Assert-True -Condition ($repositoryEvidence.RootProcessId -contains $ownedProcess.Id) -Message 'Repository process inspection должен находить exact root независимо от port evidence.'

    $foreignGuiPath = Join-Path -Path $repositoryPath -ChildPath 'foreign-gui.py'
    New-Item -ItemType File -Path $foreignGuiPath -Force -ErrorAction Stop | Out-Null
    $foreignOwnership = Get-AzurPilotPortOwnershipState -Port $port -ProjectPythonPath $pwshCommand.Path -GuiPath $foreignGuiPath
    Assert-True -Condition ($foreignOwnership.State -eq 'Foreign') -Message 'Несовпадающий gui.py должен давать foreign ownership.'
    $foreignKillResult = Stop-AzurPilotOwnedProcessTree -RootProcessId $ownedProcess.Id -ProjectPythonPath $pwshCommand.Path -GuiPath $foreignGuiPath -Confirm:$false
    Assert-True -Condition (-not $foreignKillResult) -Message 'Foreign process нельзя завершать fallback-функцией.'
    Assert-True -Condition (-not $ownedProcess.HasExited) -Message 'Foreign safety test не должен убивать disposable listener.'

    $rootEvidence = $ownership.Evidence | Where-Object RootProcessId -EQ $ownedProcess.Id | Select-Object -First 1
    $ownedKillResult = Stop-AzurPilotOwnedProcessTree -RootProcessId $ownedProcess.Id -ProjectPythonPath $pwshCommand.Path -GuiPath $guiPath -ExpectedCreationDate $rootEvidence.RootCreationDate -Confirm:$false
    Assert-True -Condition $ownedKillResult -Message 'Exact-owned fallback должен завершить только доказанное disposable tree.'
    Assert-True -Condition ($ownedProcess.WaitForExit(5000)) -Message 'Disposable listener должен завершиться после exact-owned fallback.'
    $repositoryEvidenceAfter = @(Get-AzurPilotRepositoryProcessEvidence -ProjectPythonPath $pwshCommand.Path -GuiPath $guiPath)
    $remainingOwnedRoots = @($repositoryEvidenceAfter | Where-Object { $_.RootProcessId -eq $ownedProcess.Id })
    Assert-True -Condition ($remainingOwnedRoots.Count -eq 0) -Message 'После fallback exact root не должен оставаться в repository inspection.'

    Write-Information -MessageData 'Все изолированные lifecycle-регрессии пройдены.' -InformationAction Continue
} finally {
    foreach ($stopProcess in $concurrentStopProcesses) {
        if (-not $stopProcess.HasExited) {
            $stopProcess.Kill($true)
            [void]$stopProcess.WaitForExit(5000)
        }

        $stopProcess.Dispose()
    }

    if ($null -ne $startingOwnerProcess) {
        if (-not $startingOwnerProcess.HasExited) {
            $startingOwnerProcess.Kill($true)
            [void]$startingOwnerProcess.WaitForExit(5000)
        }

        $startingOwnerProcess.Dispose()
    }

    if ($null -ne $mutexProcess) {
        if (-not $mutexProcess.HasExited) {
            $mutexProcess.Kill($true)
            [void]$mutexProcess.WaitForExit(5000)
        }

        $mutexProcess.Dispose()
    }

    if ($null -ne $ownedProcess) {
        if (-not $ownedProcess.HasExited) {
            $ownedProcess.Kill($true)
            [void]$ownedProcess.WaitForExit(5000)
        }

        $ownedProcess.Dispose()
    }

    if (Test-Path -LiteralPath $testRoot -PathType Container) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction Stop
    }
}
