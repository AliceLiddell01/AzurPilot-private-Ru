#requires -Version 7.6

[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryPath = 'C:\AzurPilot',

    [Parameter()]
    [ValidateRange(5, 300)]
    [int]$TimeoutSeconds = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$script:RepositoryPathParameter = $RepositoryPath
$script:TimeoutSecondsParameter = $TimeoutSeconds

$script:ExitCodeSuccess = 0
$script:ExitCodePreconditionFailure = 20
$script:ExitCodeForeignOwnership = 21
$script:ExitCodeTimeout = 22
$script:ExitCodeEnvironmentFailure = 23
$script:ExitCodeUnexpectedFailure = 30

$script:LogPath = $null
$script:StopMutex = $null
$script:StopMutexOwned = $false

$lifecycleModulePath = Join-Path -Path $PSScriptRoot -ChildPath 'lib\AzurPilot.Lifecycle.psm1'
Import-Module -Name $lifecycleModulePath -Force -ErrorAction Stop

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

function Initialize-StopLog {
    [CmdletBinding()]
    param()

    $baseDirectory = $env:LOCALAPPDATA

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        $baseDirectory = $env:TEMP
    }

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        throw 'Не удалось определить каталог для лога остановки.'
    }

    $logDirectory = Join-Path -Path $baseDirectory -ChildPath 'AzurPilot\logs'
    New-Item -ItemType Directory -Path $logDirectory -Force -ErrorAction Stop | Out-Null
    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $script:LogPath = Join-Path -Path $logDirectory -ChildPath "Stop-AzurPilot-$timestamp-$PID.log"
    New-Item -ItemType File -Path $script:LogPath -Force -ErrorAction Stop | Out-Null
}

function Write-StopLog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateSet('INFO', 'WARN', 'ERROR')]
        [string]$Level,

        [Parameter(Mandatory)]
        [AllowEmptyString()]
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

function Get-StopException {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [int]$Code,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Message
    )

    $exception = [System.InvalidOperationException]::new($Message)
    $exception.Data['ExitCode'] = $Code
    return $exception
}

function Complete-StopFailure {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [int]$Code,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Message
    )

    throw (Get-StopException -Code $Code -Message $Message)
}

function Resolve-StopRequiredPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Path,

        [Parameter(Mandatory)]
        [ValidateSet('Container', 'Leaf')]
        [string]$PathType,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Label,

        [Parameter()]
        [int]$FailureCode = 20
    )

    if (-not (Test-Path -LiteralPath $Path -PathType $PathType)) {
        Complete-StopFailure -Code $FailureCode -Message ("{0} не найден: {1}" -f $Label, $Path)
    }

    try {
        return (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    } catch {
        Complete-StopFailure -Code $FailureCode -Message ('Не удалось разрешить путь «{0}»: {1}' -f $Label, $_.Exception.Message)
    }
}

function Get-ConfiguredWebUiPort {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$DeployConfigPath
    )

    $values = @(
        foreach ($line in Get-Content -LiteralPath $DeployConfigPath -Encoding utf8 -ErrorAction Stop) {
            $match = [regex]::Match($line, '^\s*WebuiPort\s*:\s*([^#\s]+)')

            if ($match.Success) {
                $match.Groups[1].Value.Trim('"', "'")
            }
        }
    )

    if ($values.Count -gt 1) {
        Complete-StopFailure -Code $script:ExitCodePreconditionFailure -Message 'В config\deploy.yaml найдено несколько значений WebuiPort.'
    }

    if ($values.Count -eq 0) {
        return 25548
    }

    $port = 0

    if (
        -not [int]::TryParse($values[0], [ref]$port) -or
        $port -lt 1 -or
        $port -gt 65535
    ) {
        Complete-StopFailure -Code $script:ExitCodePreconditionFailure -Message ("Некорректное значение WebuiPort: {0}" -f $values[0])
    }

    return $port
}

function Enter-StopMutex {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Name,

        [Parameter(Mandatory)]
        [ValidateRange(1, 300)]
        [int]$WaitSeconds
    )

    $mutex = [System.Threading.Mutex]::new($false, $Name)
    $owned = $false

    try {
        $owned = $mutex.WaitOne([TimeSpan]::FromSeconds($WaitSeconds), $false)
    } catch [System.Threading.AbandonedMutexException] {
        $owned = $true
        Write-StopLog -Level 'WARN' -Message 'Обнаружен заброшенный мьютекс Stop. Владение восстановлено.'
    } catch {
        $mutex.Dispose()
        throw
    }

    if (-not $owned) {
        $mutex.Dispose()
        Complete-StopFailure -Code $script:ExitCodeTimeout -Message 'Другой Stop не завершился за отведённое время.'
    }

    return $mutex
}

function Get-CurrentOwnership {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [int]$Port,

        [Parameter(Mandatory)]
        [string]$ProjectPythonPath,

        [Parameter(Mandatory)]
        [string]$GuiPath
    )

    try {
        return Get-AzurPilotPortOwnershipState -Port $Port -ProjectPythonPath $ProjectPythonPath -GuiPath $GuiPath
    } catch {
        Complete-StopFailure -Code $script:ExitCodePreconditionFailure -Message $_.Exception.Message
    }
}

function Get-RepositoryProcessEvidence {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ProjectPythonPath,

        [Parameter(Mandatory)]
        [string]$GuiPath
    )

    try {
        return @(Get-AzurPilotRepositoryProcessEvidence -ProjectPythonPath $ProjectPythonPath -GuiPath $GuiPath)
    } catch {
        Complete-StopFailure -Code $script:ExitCodePreconditionFailure -Message $_.Exception.Message
    }
}

function Wait-ForLifecycleStopped {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [DateTimeOffset]$Deadline,

        [Parameter(Mandatory)]
        [string]$StartMutexName,

        [Parameter(Mandatory)]
        [int]$Port,

        [Parameter(Mandatory)]
        [string]$ProjectPythonPath,

        [Parameter(Mandatory)]
        [string]$GuiPath
    )

    $foreignObservationKey = ''
    $foreignObservationCount = 0

    while ([DateTimeOffset]::UtcNow -lt $Deadline) {
        $ownership = Get-CurrentOwnership -Port $Port -ProjectPythonPath $ProjectPythonPath -GuiPath $GuiPath

        if ($ownership.State -eq 'Foreign') {
            $currentForeignKey = @($ownership.ForeignProcessIds | Sort-Object) -join ','

            if ($currentForeignKey -eq $foreignObservationKey) {
                $foreignObservationCount += 1
            } else {
                $foreignObservationKey = $currentForeignKey
                $foreignObservationCount = 1
            }

            if ($foreignObservationCount -ge 5) {
                Complete-StopFailure -Code $script:ExitCodeForeignOwnership -Message ("На порту {0} устойчиво обнаружен процесс, не принадлежащий этому AzurPilot. Никакие процессы не остановлены." -f $Port)
            }
        } else {
            $foreignObservationKey = ''
            $foreignObservationCount = 0
        }

        $startOwnerActive = Test-AzurPilotMutexOwned -Name $StartMutexName

        if ($ownership.State -eq 'Free' -and -not $startOwnerActive) {
            $repositoryProcesses = @(Get-RepositoryProcessEvidence -ProjectPythonPath $ProjectPythonPath -GuiPath $GuiPath)

            if ($repositoryProcesses.Count -eq 0) {
                return $true
            }
        }

        Start-Sleep -Milliseconds 200
    }

    return $false
}

function Invoke-ExactOwnedFallback {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Ownership,

        [Parameter(Mandatory)]
        [string]$ProjectPythonPath,

        [Parameter(Mandatory)]
        [string]$GuiPath
    )

    $rootEvidence = @(
        $Ownership.Evidence |
            Where-Object Owned |
            Group-Object -Property RootProcessId |
            ForEach-Object { $_.Group | Select-Object -First 1 }
    )

    if ($rootEvidence.Count -eq 0) {
        return $false
    }

    Write-StopLog -Level 'WARN' -Message 'Штатная остановка не завершилась вовремя; применяется принудительный fallback только для доказанного дерева текущего checkout.'

    foreach ($evidence in $rootEvidence) {
        $fallbackParameters = @{
            RootProcessId = $evidence.RootProcessId
            ProjectPythonPath = $ProjectPythonPath
            GuiPath = $GuiPath
            ExpectedCreationDate = $evidence.RootCreationDate
        }
        $stopped = Stop-AzurPilotOwnedProcessTree @fallbackParameters

        if (-not $stopped) {
            return $false
        }
    }

    return $true
}

function Invoke-AzurPilotStop {
    [CmdletBinding()]
    param()

    try {
        Initialize-StopLog
        Write-StopLog -Level 'INFO' -Message 'Остановка AzurPilot.'

        if (-not $IsWindows) {
            Complete-StopFailure -Code $script:ExitCodePreconditionFailure -Message 'Команда Stop поддерживает только Windows.'
        }

        $resolvedRepositoryPath = Resolve-AzurPilotRepositoryPath -RepositoryPath $script:RepositoryPathParameter
        Write-StopLog -Level 'INFO' -Message ("Путь к репозиторию: {0}" -f $resolvedRepositoryPath)
        $guiPath = Resolve-StopRequiredPath -Path (Join-Path -Path $resolvedRepositoryPath -ChildPath 'gui.py') -PathType Leaf -Label 'gui.py'
        $pythonPathParameters = @{
            Path = Join-Path -Path $resolvedRepositoryPath -ChildPath '.venv\Scripts\python.exe'
            PathType = 'Leaf'
            Label = 'Python проекта'
            FailureCode = $script:ExitCodeEnvironmentFailure
        }
        $projectPythonPath = Resolve-StopRequiredPath @pythonPathParameters
        $deployConfigPath = Resolve-StopRequiredPath -Path (Join-Path -Path $resolvedRepositoryPath -ChildPath 'config\deploy.yaml') -PathType Leaf -Label 'config\deploy.yaml'
        $port = Get-ConfiguredWebUiPort -DeployConfigPath $deployConfigPath
        $lifecycleNames = Get-AzurPilotLifecycleName -RepositoryPath $resolvedRepositoryPath
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds($script:TimeoutSecondsParameter)
        $script:StopMutex = Enter-StopMutex -Name $lifecycleNames.StopMutex -WaitSeconds $script:TimeoutSecondsParameter
        $script:StopMutexOwned = $true

        $ownership = Get-CurrentOwnership -Port $port -ProjectPythonPath $projectPythonPath -GuiPath $guiPath

        if ($ownership.State -eq 'Foreign') {
            Complete-StopFailure -Code $script:ExitCodeForeignOwnership -Message ("На порту {0} обнаружен процесс, не принадлежащий этому AzurPilot. Никакие процессы не остановлены." -f $port)
        }

        $startOwnerActive = Test-AzurPilotMutexOwned -Name $lifecycleNames.StartMutex

        $repositoryProcesses = @()

        if ($ownership.State -eq 'Free') {
            $repositoryProcesses = @(Get-RepositoryProcessEvidence -ProjectPythonPath $projectPythonPath -GuiPath $guiPath)
        }

        if (
            $ownership.State -eq 'Free' -and
            -not $startOwnerActive -and
            $repositoryProcesses.Count -eq 0
        ) {
            Write-StopLog -Level 'INFO' -Message 'AzurPilot уже остановлен.'
            return $script:ExitCodeSuccess
        }

        if ($ownership.State -eq 'AzurPilot' -or $repositoryProcesses.Count -gt 0) {
            Write-StopLog -Level 'INFO' -Message 'Найден работающий AzurPilot этого репозитория.'
        } else {
            Write-StopLog -Level 'INFO' -Message 'Найден активный controller AzurPilot; backend ещё не занял WebUI-порт.'
        }

        $requestSent = $false

        while ($startOwnerActive -and -not $requestSent -and [DateTimeOffset]::UtcNow -lt $deadline) {
            $requestSent = Send-AzurPilotStopRequest -Name $lifecycleNames.StopEvent

            if (-not $requestSent) {
                Start-Sleep -Milliseconds 100
                $startOwnerActive = Test-AzurPilotMutexOwned -Name $lifecycleNames.StartMutex
            }
        }

        if ($requestSent) {
            Write-StopLog -Level 'INFO' -Message 'Отправлен запрос на штатную остановку.'

            if (Wait-ForLifecycleStopped -Deadline $deadline -StartMutexName $lifecycleNames.StartMutex -Port $port -ProjectPythonPath $projectPythonPath -GuiPath $guiPath) {
                Write-StopLog -Level 'INFO' -Message 'Серверный процесс завершён; WebUI-порт и lifecycle ownership освобождены.'
                Write-StopLog -Level 'INFO' -Message 'AzurPilot остановлен.'
                return $script:ExitCodeSuccess
            }
        }

        $ownership = Get-CurrentOwnership -Port $port -ProjectPythonPath $projectPythonPath -GuiPath $guiPath

        if ($ownership.State -eq 'Foreign') {
            Complete-StopFailure -Code $script:ExitCodeForeignOwnership -Message ("На порту {0} обнаружен процесс, не принадлежащий этому AzurPilot. Никакие процессы не остановлены." -f $port)
        }

        if ($ownership.State -eq 'Free') {
            $repositoryProcesses = @(Get-RepositoryProcessEvidence -ProjectPythonPath $projectPythonPath -GuiPath $guiPath)

            if ($repositoryProcesses.Count -gt 0) {
                $ownership = [pscustomobject]@{
                    State = 'AzurPilot'
                    ProcessIds = @()
                    ForeignProcessIds = @()
                    RootProcessIds = @($repositoryProcesses | Select-Object -ExpandProperty RootProcessId)
                    Evidence = $repositoryProcesses
                }
            }
        }

        if ($ownership.State -eq 'AzurPilot') {
            $fallbackSucceeded = Invoke-ExactOwnedFallback -Ownership $ownership -ProjectPythonPath $projectPythonPath -GuiPath $guiPath

            if ($fallbackSucceeded) {
                $fallbackDeadline = [DateTimeOffset]::UtcNow.AddSeconds(10)

                if (Wait-ForLifecycleStopped -Deadline $fallbackDeadline -StartMutexName $lifecycleNames.StartMutex -Port $port -ProjectPythonPath $projectPythonPath -GuiPath $guiPath) {
                    Write-StopLog -Level 'INFO' -Message 'Принудительный fallback завершил доказанное дерево AzurPilot; порт и lifecycle ownership освобождены.'
                    Write-StopLog -Level 'INFO' -Message 'AzurPilot остановлен.'
                    return $script:ExitCodeSuccess
                }
            }
        }

        Complete-StopFailure -Code $script:ExitCodeTimeout -Message ("AzurPilot не подтвердил полную остановку за {0} секунд." -f $script:TimeoutSecondsParameter)
    } catch {
        $exitCode = $script:ExitCodeUnexpectedFailure

        if ($_.Exception.Data.Contains('ExitCode')) {
            $exitCode = [int]$_.Exception.Data['ExitCode']
        }

        $errorMessage = $_.Exception.Message

        try {
            Write-StopLog -Level 'ERROR' -Message $errorMessage
        } catch {
            Write-ConsoleMessage -Message ("AzurPilot: {0}" -f $errorMessage)
        }

        return $exitCode
    } finally {
        if ($null -ne $script:StopMutex) {
            if ($script:StopMutexOwned) {
                try {
                    $script:StopMutex.ReleaseMutex()
                } catch {
                    if ($null -ne $script:LogPath) {
                        Write-StopLog -Level 'WARN' -Message ("Не удалось освободить мьютекс Stop: {0}" -f $_.Exception.Message)
                    }
                }
            }

            $script:StopMutex.Dispose()
        }

        if ($null -ne $script:LogPath) {
            Write-ConsoleMessage -Message ("Лог: {0}" -f $script:LogPath)
        }
    }
}

$stopExitCode = Invoke-AzurPilotStop
exit $stopExitCode
