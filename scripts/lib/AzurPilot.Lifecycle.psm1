#requires -Version 7.6

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

function Resolve-AzurPilotRepositoryPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$RepositoryPath
    )

    if (-not (Test-Path -LiteralPath $RepositoryPath -PathType Container)) {
        throw ('Каталог AzurPilot не существует: {0}' -f $RepositoryPath)
    }

    return (Resolve-Path -LiteralPath $RepositoryPath -ErrorAction Stop).Path.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Get-AzurPilotPathHash {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Path
    )

    $normalizedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ).ToUpperInvariant()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalizedPath)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()

    try {
        $hashBytes = $sha256.ComputeHash($bytes)
    } finally {
        $sha256.Dispose()
    }

    return [Convert]::ToHexString($hashBytes).ToLowerInvariant()
}

function Get-AzurPilotLifecycleName {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$RepositoryPath
    )

    $pathHash = Get-AzurPilotPathHash -Path $RepositoryPath

    return [pscustomobject]@{
        StartMutex = "Local\AzurPilot.Start.$pathHash"
        StopMutex = "Local\AzurPilot.Stop.$pathHash"
        StopEvent = "Local\AzurPilot.StopRequested.$pathHash"
    }
}

function New-AzurPilotStopEvent {
    [CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Low')]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Name
    )

    if (-not $PSCmdlet.ShouldProcess($Name, 'Создать объект координации остановки AzurPilot')) {
        return $null
    }

    $createdNew = $false
    $stopEvent = [System.Threading.EventWaitHandle]::new(
        $false,
        [System.Threading.EventResetMode]::ManualReset,
        $Name,
        [ref]$createdNew
    )

    if (-not $createdNew) {
        $stopEvent.Dispose()
        throw ('Объект координации остановки уже существует: {0}' -f $Name)
    }

    return $stopEvent
}

function Send-AzurPilotStopRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Name
    )

    $stopEvent = $null

    try {
        $stopEvent = [System.Threading.EventWaitHandle]::OpenExisting($Name)
        return $stopEvent.Set()
    } catch [System.Threading.WaitHandleCannotBeOpenedException] {
        return $false
    } finally {
        if ($null -ne $stopEvent) {
            $stopEvent.Dispose()
        }
    }
}

function Test-AzurPilotStopRequested {
    [CmdletBinding()]
    param(
        [Parameter()]
        [AllowNull()]
        [System.Threading.EventWaitHandle]$StopEvent
    )

    return $null -ne $StopEvent -and $StopEvent.WaitOne(0, $false)
}

function Test-AzurPilotMutexOwned {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Name
    )

    $mutex = $null
    $acquired = $false

    try {
        $mutex = [System.Threading.Mutex]::OpenExisting($Name)

        try {
            $acquired = $mutex.WaitOne(0, $false)
        } catch [System.Threading.AbandonedMutexException] {
            $acquired = $true
        }

        return -not $acquired
    } catch [System.Threading.WaitHandleCannotBeOpenedException] {
        return $false
    } finally {
        if ($null -ne $mutex) {
            if ($acquired) {
                $mutex.ReleaseMutex()
            }

            $mutex.Dispose()
        }
    }
}

function Get-AzurPilotListeningProcessIdCollection {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateRange(1, 65535)]
        [int]$Port
    )

    $netCommand = Get-Command -Name Get-NetTCPConnection -CommandType Function, Cmdlet -ErrorAction SilentlyContinue

    if ($null -eq $netCommand) {
        throw 'Командлет Get-NetTCPConnection недоступен.'
    }

    $connectionErrors = @()
    $connections = @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue -ErrorVariable connectionErrors
    )

    if ($connectionErrors.Count -gt 0 -and $connections.Count -eq 0) {
        $unexpectedErrors = @(
            $connectionErrors |
                Where-Object {
                    $_.CategoryInfo.Category -ne [System.Management.Automation.ErrorCategory]::ObjectNotFound
                }
        )

        if ($unexpectedErrors.Count -gt 0) {
            $errorText = $unexpectedErrors.Exception.Message -join [Environment]::NewLine
            throw ('Не удалось проверить TCP-порт {0}: {1}' -f $Port, $errorText)
        }
    }

    return @(
        $connections |
            Select-Object -ExpandProperty OwningProcess |
            Where-Object { $_ -gt 0 } |
            Sort-Object -Unique
    )
}

function Get-AzurPilotWindowsProcessRecord {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateRange(1, [int]::MaxValue)]
        [int]$ProcessId
    )

    try {
        return Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
    } catch {
        return $null
    }
}

function Test-AzurPilotCommandLinePathArgument {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$CommandLine,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Path
    )

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }

    $escapedPath = [regex]::Escape([System.IO.Path]::GetFullPath($Path))
    $pattern = '(?i)(?:^|\s)(?:"{0}"|{0})(?=\s|$)' -f $escapedPath
    return [regex]::IsMatch($CommandLine, $pattern)
}

function Get-AzurPilotProcessOwnershipEvidence {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateRange(1, [int]::MaxValue)]
        [int]$ProcessId,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$ProjectPythonPath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$GuiPath
    )

    $expectedPythonPath = [System.IO.Path]::GetFullPath($ProjectPythonPath)
    $expectedGuiPath = [System.IO.Path]::GetFullPath($GuiPath)
    $currentProcessId = $ProcessId
    $visitedProcessIds = @{}
    $chainProcessIds = [System.Collections.Generic.List[int]]::new()

    foreach ($depth in 0..11) {
        if ($currentProcessId -le 0 -or $visitedProcessIds.ContainsKey($currentProcessId)) {
            break
        }

        $visitedProcessIds[$currentProcessId] = $true
        [void]$chainProcessIds.Add($currentProcessId)
        $processRecord = Get-AzurPilotWindowsProcessRecord -ProcessId $currentProcessId

        if ($null -eq $processRecord) {
            break
        }

        $executablePath = [string]$processRecord.ExecutablePath
        $commandLine = [string]$processRecord.CommandLine
        $pythonMatches = (
            -not [string]::IsNullOrWhiteSpace($executablePath) -and
            [string]::Equals(
                [System.IO.Path]::GetFullPath($executablePath),
                $expectedPythonPath,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        )
        $guiMatches = Test-AzurPilotCommandLinePathArgument -CommandLine $commandLine -Path $expectedGuiPath

        if ($pythonMatches -and $guiMatches) {
            return [pscustomobject]@{
                Owned = $true
                ObservedProcessId = $ProcessId
                RootProcessId = [int]$processRecord.ProcessId
                RootCreationDate = $processRecord.CreationDate
                ChainProcessIds = $chainProcessIds.ToArray()
            }
        }

        $currentProcessId = [int]$processRecord.ParentProcessId
    }

    return [pscustomobject]@{
        Owned = $false
        ObservedProcessId = $ProcessId
        RootProcessId = $null
        RootCreationDate = $null
        ChainProcessIds = $chainProcessIds.ToArray()
    }
}

function Get-AzurPilotPortOwnershipState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateRange(1, 65535)]
        [int]$Port,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$ProjectPythonPath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$GuiPath
    )

    $processIds = @(Get-AzurPilotListeningProcessIdCollection -Port $Port)

    if ($processIds.Count -eq 0) {
        return [pscustomobject]@{
            State = 'Free'
            ProcessIds = @()
            ForeignProcessIds = @()
            RootProcessIds = @()
            Evidence = @()
        }
    }

    $evidence = @(
        foreach ($processId in $processIds) {
            Get-AzurPilotProcessOwnershipEvidence -ProcessId $processId -ProjectPythonPath $ProjectPythonPath -GuiPath $GuiPath
        }
    )
    $foreignProcessIds = @($evidence | Where-Object { -not $_.Owned } | Select-Object -ExpandProperty ObservedProcessId)
    $rootProcessIds = @($evidence | Where-Object Owned | Select-Object -ExpandProperty RootProcessId -Unique)

    if ($foreignProcessIds.Count -gt 0) {
        return [pscustomobject]@{
            State = 'Foreign'
            ProcessIds = $processIds
            ForeignProcessIds = $foreignProcessIds
            RootProcessIds = $rootProcessIds
            Evidence = $evidence
        }
    }

    return [pscustomobject]@{
        State = 'AzurPilot'
        ProcessIds = $processIds
        ForeignProcessIds = @()
        RootProcessIds = $rootProcessIds
        Evidence = $evidence
    }
}

function Get-AzurPilotRepositoryProcessEvidence {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$ProjectPythonPath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$GuiPath
    )

    $expectedPythonPath = [System.IO.Path]::GetFullPath($ProjectPythonPath)
    $expectedGuiPath = [System.IO.Path]::GetFullPath($GuiPath)

    try {
        $processRecords = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    } catch {
        throw ('Не удалось перечислить процессы Windows: {0}' -f $_.Exception.Message)
    }

    return @(
        foreach ($processRecord in $processRecords) {
            $executablePath = [string]$processRecord.ExecutablePath

            if ([string]::IsNullOrWhiteSpace($executablePath)) {
                continue
            }

            if (-not [string]::Equals(
                [System.IO.Path]::GetFullPath($executablePath),
                $expectedPythonPath,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                continue
            }

            $commandLine = [string]$processRecord.CommandLine

            if (-not (Test-AzurPilotCommandLinePathArgument -CommandLine $commandLine -Path $expectedGuiPath)) {
                continue
            }

            [pscustomobject]@{
                Owned = $true
                ObservedProcessId = [int]$processRecord.ProcessId
                RootProcessId = [int]$processRecord.ProcessId
                RootCreationDate = $processRecord.CreationDate
                ChainProcessIds = @([int]$processRecord.ProcessId)
            }
        }
    )
}

function Stop-AzurPilotOwnedProcessTree {
    [CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
    param(
        [Parameter(Mandatory)]
        [ValidateRange(1, [int]::MaxValue)]
        [int]$RootProcessId,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$ProjectPythonPath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$GuiPath,

        [Parameter()]
        [AllowNull()]
        [object]$ExpectedCreationDate
    )

    $evidence = Get-AzurPilotProcessOwnershipEvidence -ProcessId $RootProcessId -ProjectPythonPath $ProjectPythonPath -GuiPath $GuiPath

    if (-not $evidence.Owned -or $evidence.RootProcessId -ne $RootProcessId) {
        return $false
    }

    if (
        $null -ne $ExpectedCreationDate -and
        $evidence.RootCreationDate -ne $ExpectedCreationDate
    ) {
        return $false
    }

    if (-not $PSCmdlet.ShouldProcess("PID $RootProcessId", 'Принудительно завершить доказанное дерево AzurPilot')) {
        return $false
    }

    $taskkillCommand = Get-Command -Name taskkill.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $taskkillArguments = @(
        '/PID'
        [string]$RootProcessId
        '/T'
        '/F'
    )
    & $taskkillCommand.Path @taskkillArguments 2>&1 | Out-Null
    $taskkillExitCode = $LASTEXITCODE

    if ($taskkillExitCode -ne 0) {
        return $null -eq (Get-AzurPilotWindowsProcessRecord -ProcessId $RootProcessId)
    }

    return $null -eq (Get-AzurPilotWindowsProcessRecord -ProcessId $RootProcessId)
}

Export-ModuleMember -Function @(
    'Resolve-AzurPilotRepositoryPath'
    'Get-AzurPilotPathHash'
    'Get-AzurPilotLifecycleName'
    'New-AzurPilotStopEvent'
    'Send-AzurPilotStopRequest'
    'Test-AzurPilotStopRequested'
    'Test-AzurPilotMutexOwned'
    'Get-AzurPilotListeningProcessIdCollection'
    'Get-AzurPilotWindowsProcessRecord'
    'Get-AzurPilotProcessOwnershipEvidence'
    'Get-AzurPilotPortOwnershipState'
    'Get-AzurPilotRepositoryProcessEvidence'
    'Stop-AzurPilotOwnedProcessTree'
)
