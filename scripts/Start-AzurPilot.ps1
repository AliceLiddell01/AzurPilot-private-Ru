#requires -Version 7.6

[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryPath = 'C:\AzurPilot',

    [Parameter()]
    [switch]$NoBrowser,

    [Parameter()]
    [switch]$FromShortcut,

    [Parameter()]
    [switch]$VerboseBackendOutput,

    [Parameter()]
    [ValidateRange(5, 600)]
    [int]$StartupTimeoutSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$lifecycleModulePath = Join-Path -Path $PSScriptRoot -ChildPath 'lib\AzurPilot.Lifecycle.psm1'
Import-Module -Name $lifecycleModulePath -Force -ErrorAction Stop

$script:RepositoryPathParameter = $RepositoryPath
$script:NoBrowserParameter = $NoBrowser
$script:FromShortcutParameter = $FromShortcut
$script:VerboseBackendOutputParameter = $VerboseBackendOutput
$script:StartupTimeoutSecondsParameter = $StartupTimeoutSeconds

$script:ExitCodeSuccess = 0
$script:ExitCodePreconditionFailure = 20
$script:ExitCodeForeignPortOwner = 21
$script:ExitCodeConcurrentStartTimeout = 22
$script:ExitCodeEnvironmentFailure = 23
$script:ExitCodeReadinessTimeout = 24
$script:ExitCodeBackendFailure = 25
$script:ExitCodeBrowserFailure = 26
$script:ExitCodeUnexpectedFailure = 30

$script:LogPath = $null
$script:StartMutex = $null
$script:StartMutexOwned = $false
$script:StopEvent = $null
$script:ConsoleStopHandlerInstalled = $false
$script:IntentionalStopRequested = $false
$script:StartedProcess = $null
$script:StartedProcessData = $null
$script:BackendReady = $false

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

function Initialize-StartLog {
    [CmdletBinding()]
    param()

    $baseDirectory = $env:LOCALAPPDATA

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        $baseDirectory = $env:TEMP
    }

    if ([string]::IsNullOrWhiteSpace($baseDirectory)) {
        throw 'Не удалось определить каталог для лога запуска.'
    }

    $logDirectory = Join-Path -Path $baseDirectory -ChildPath 'AzurPilot\logs'
    New-Item -ItemType Directory -Path $logDirectory -Force -ErrorAction Stop | Out-Null

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $fileName = "Start-AzurPilot-$timestamp-$PID.log"
    $script:LogPath = Join-Path -Path $logDirectory -ChildPath $fileName

    New-Item -ItemType File -Path $script:LogPath -Force -ErrorAction Stop | Out-Null
}

function Write-StartLog {
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
    $line = "[$timestamp] [$Level] $safeMessage"

    Write-ConsoleMessage -Message $line

    if ($null -ne $script:LogPath) {
        Add-Content -LiteralPath $script:LogPath -Value $line -Encoding utf8 -ErrorAction Stop
    }
}

function Get-StartException {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [int]$Code,

        [Parameter(Mandatory)]
        [string]$Message
    )

    $exception = [System.InvalidOperationException]::new($Message)
    $exception.Data['ExitCode'] = $Code

    return $exception
}

function Complete-StartFailure {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [int]$Code,

        [Parameter(Mandatory)]
        [string]$Message
    )

    throw (Get-StartException -Code $Code -Message $Message)
}

function Show-ShortcutError {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Message
    )

    if (-not $FromShortcut) {
        return
    }

    $dialogMessage = $Message

    if ($null -ne $script:LogPath) {
        $lineBreak = [Environment]::NewLine
        $dialogMessage = '{0}{1}{1}Лог: {2}' -f $dialogMessage, $lineBreak, $script:LogPath
    }

    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop

        [void][System.Windows.Forms.MessageBox]::Show(
            $dialogMessage,
            'AzurPilot — ошибка запуска',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        )
    } catch {
        Write-StartLog -Level 'WARN' -Message "Не удалось показать Windows-диалог: $($_.Exception.Message)"
    }
}

function Resolve-RequiredPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [ValidateSet(
            'Container',
            'Leaf'
        )]
        [string]$PathType,

        [Parameter(Mandatory)]
        [string]$Label,

        [Parameter()]
        [int]$FailureCode = 20
    )

    $testPathType = if ($PathType -eq 'Container') {
        'Container'
    } else {
        'Leaf'
    }

    if (-not (Test-Path -LiteralPath $Path -PathType $testPathType)) {
        Complete-StartFailure -Code $FailureCode -Message "$Label не найден: $Path"
    }

    try {
        return (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    } catch {
        Complete-StartFailure -Code $FailureCode -Message "Не удалось разрешить путь «$Label»: $($_.Exception.Message)"
    }
}

function ConvertTo-SimpleBoolean {
    [CmdletBinding()]
    param(
        [Parameter()]
        [AllowNull()]
        [string]$Value,

        [Parameter(Mandatory)]
        [string]$Key,

        [Parameter(Mandatory)]
        [bool]$DefaultValue
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $DefaultValue
    }

    switch ($Value.Trim().ToLowerInvariant()) {
        'true' {
            return $true
        }

        '1' {
            return $true
        }

        'yes' {
            return $true
        }

        'on' {
            return $true
        }

        'false' {
            return $false
        }

        '0' {
            return $false
        }

        'no' {
            return $false
        }

        'off' {
            return $false
        }

        default {
            $message = 'Некорректное логическое значение {0}: {1}' -f $Key, $Value
            Complete-StartFailure -Code $script:ExitCodePreconditionFailure -Message $message
        }
    }
}

function Get-WebUiConfiguration {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$DeployConfigPath
    )

    $hostValue = Get-YamlScalarValue -Path $DeployConfigPath -Key 'WebuiHost'
    $portValue = Get-YamlScalarValue -Path $DeployConfigPath -Key 'WebuiPort'
    $sslKeyValue = Get-YamlScalarValue -Path $DeployConfigPath -Key 'WebuiSSLKey'
    $sslCertValue = Get-YamlScalarValue -Path $DeployConfigPath -Key 'WebuiSSLCert'
    $enableReloadValue = Get-YamlScalarValue -Path $DeployConfigPath -Key 'EnableReload'
    $enableReloadParameters = @{
        Value = $enableReloadValue
        Key = 'EnableReload'
        DefaultValue = $true
    }

    $enableReload = ConvertTo-SimpleBoolean @enableReloadParameters

    if ([string]::IsNullOrWhiteSpace($hostValue)) {
        $hostValue = '0.0.0.0'
    }

    $port = 25548

    if (-not [string]::IsNullOrWhiteSpace($portValue)) {
        $parsedPort = 0
        $portParsed = [int]::TryParse(
            $portValue,
            [System.Globalization.NumberStyles]::Integer,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [ref]$parsedPort
        )

        if (-not $portParsed -or $parsedPort -lt 1 -or $parsedPort -gt 65535) {
            Complete-StartFailure -Code $script:ExitCodePreconditionFailure -Message "Некорректное значение WebuiPort в config\deploy.yaml: $portValue"
        }

        $port = $parsedPort
    }

    $sslEnabled = (
        -not [string]::IsNullOrWhiteSpace($sslKeyValue) -and
        -not [string]::IsNullOrWhiteSpace($sslCertValue)
    )

    $browserHost = $hostValue

    if ($hostValue -in @(
        '0.0.0.0',
        '::',
        '[::]',
        'localhost'
    )) {
        $browserHost = '127.0.0.1'
    }

    $scheme = if ($sslEnabled) {
        'https'
    } else {
        'http'
    }

    try {
        $uriBuilder = [System.UriBuilder]::new(
            $scheme,
            $browserHost,
            $port,
            '/'
        )
        $browserUri = $uriBuilder.Uri
    } catch {
        Complete-StartFailure -Code $script:ExitCodePreconditionFailure -Message "Не удалось построить WebUI URL: $($_.Exception.Message)"
    }

    return [pscustomobject]@{
        BindHost = $hostValue
        Port = $port
        SslEnabled = $sslEnabled
        EnableReload = $enableReload
        BrowserUri = $browserUri
    }
}

function Invoke-PythonHealthCheck {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$PythonPath,

        [Parameter(Mandatory)]
        [string]$WorkingDirectory
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $PythonPath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $utf8Encoding = [System.Text.UTF8Encoding]::new($false)
    $startInfo.StandardOutputEncoding = $utf8Encoding
    $startInfo.StandardErrorEncoding = $utf8Encoding
    [void]$startInfo.ArgumentList.Add('-c')
    [void]$startInfo.ArgumentList.Add('raise SystemExit(0)')

    foreach ($environmentName in @(
        'PYTHONHOME',
        'pythonhome',
        'PYTHONPATH',
        'pythonpath',
        'VIRTUAL_ENV',
        'virtual_env',
        '__PYVENV_LAUNCHER__'
    )) {
        [void]$startInfo.Environment.Remove($environmentName)
    }

    $startInfo.Environment['PYTHONUTF8'] = '1'

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo

    try {
        if (-not $process.Start()) {
            Complete-StartFailure -Code $script:ExitCodeEnvironmentFailure -Message 'Python проекта не запустился.'
        }

        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()

        if (-not $process.WaitForExit(15000)) {
            try {
                $process.Kill($true)
            } catch {
                Write-StartLog -Level 'WARN' -Message "Не удалось остановить зависшую проверку Python: $($_.Exception.Message)"
            }

            Complete-StartFailure -Code $script:ExitCodeEnvironmentFailure -Message 'Проверка Python проекта превысила 15 секунд.'
        }

        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()

        if ($process.ExitCode -ne 0) {
            $details = ($stderr, $stdout -join [Environment]::NewLine).Trim()

            if ([string]::IsNullOrWhiteSpace($details)) {
                $details = "Код завершения: $($process.ExitCode)"
            }

            Complete-StartFailure -Code $script:ExitCodeEnvironmentFailure -Message "Python проекта неисправен. $details"
        }
    } catch {
        if ($_.Exception.Data.Contains('ExitCode')) {
            throw
        }

        Complete-StartFailure -Code $script:ExitCodeEnvironmentFailure -Message "Не удалось проверить Python проекта: $($_.Exception.Message)"
    } finally {
        $process.Dispose()
    }
}

function Invoke-PostgreSqlStartPreflight {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$PythonPath,

        [Parameter(Mandatory)]
        [string]$WorkingDirectory,

        [Parameter()]
        [AllowNull()]
        [System.Threading.EventWaitHandle]$StopEvent
    )

    $dockerCommand = Get-Command -Name 'docker.exe' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1

    if ($null -eq $dockerCommand) {
        $dockerCommand = Get-Command -Name 'docker' -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
    }

    if ($null -eq $dockerCommand) {
        Complete-StartFailure -Code $script:ExitCodeEnvironmentFailure -Message 'Docker CLI недоступен; PostgreSQL нельзя запустить.'
    }

    $composeFile = Join-Path -Path $WorkingDirectory -ChildPath 'infrastructure\observability\compose.yaml'
    $envFile = Join-Path -Path $WorkingDirectory -ChildPath '.env'
    if (-not (Test-Path -LiteralPath $composeFile -PathType Leaf) -or -not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        Complete-StartFailure -Code $script:ExitCodeEnvironmentFailure -Message 'Канонический Docker Compose PostgreSQL или локальный .env недоступен.'
    }

    $operations = @(
        [pscustomobject]@{
            Executable = $dockerCommand.Path
            Arguments = @(
                'compose'
                '--env-file'
                $envFile
                '--file'
                $composeFile
                'config'
                '--quiet'
            )
            TimeoutMilliseconds = 30000
            Failure = 'Docker Compose PostgreSQL не прошёл проверку конфигурации.'
        }
        [pscustomobject]@{
            Executable = $dockerCommand.Path
            Arguments = @(
                'compose'
                '--env-file'
                $envFile
                '--file'
                $composeFile
                'up'
                '--detach'
                '--wait'
                'postgres'
            )
            TimeoutMilliseconds = 210000
            Failure = 'PostgreSQL 18 в Docker Compose не достиг состояния готовности.'
        }
        [pscustomobject]@{
            Executable = $dockerCommand.Path
            Arguments = @(
                'compose'
                '--env-file'
                $envFile
                '--file'
                $composeFile
                'run'
                '--rm'
                '--no-deps'
                'postgres-bootstrap'
            )
            TimeoutMilliseconds = 210000
            Failure = 'Роли и права PostgreSQL не прошли одноразовый bootstrap.'
        }
        [pscustomobject]@{
            Executable = $PythonPath
            Arguments = @('-X', 'utf8', '-m', 'dev_tools.postgresql_runtime', 'prepare')
            TimeoutMilliseconds = 210000
            Failure = 'Production PostgreSQL не прошёл подготовку marker, schema upgrade или app-health.'
        }
    )

    foreach ($operation in $operations) {
        if (Test-AzurPilotStopRequested -StopEvent $StopEvent) {
            $script:IntentionalStopRequested = $true
            Write-StartLog -Level 'INFO' -Message 'PostgreSQL preflight отменён координированным запросом остановки.'
            return $false
        }

        $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $utf8Encoding = [System.Text.UTF8Encoding]::new($false)
        $startInfo.FileName = $operation.Executable
        $startInfo.WorkingDirectory = $WorkingDirectory
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $startInfo.StandardOutputEncoding = $utf8Encoding
        $startInfo.StandardErrorEncoding = $utf8Encoding
        foreach ($variableName in @('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', '__PYVENV_LAUNCHER__')) {
            [void]$startInfo.Environment.Remove($variableName)
        }
        $startInfo.Environment['PYTHONUTF8'] = '1'

        foreach ($argument in $operation.Arguments) {
            [void]$startInfo.ArgumentList.Add($argument)
        }

        $process = [System.Diagnostics.Process]::new()
        $process.StartInfo = $startInfo

        try {
            if (-not $process.Start()) {
                Complete-StartFailure -Code $script:ExitCodeEnvironmentFailure -Message $operation.Failure
            }

            $stdoutTask = $process.StandardOutput.ReadToEndAsync()
            $stderrTask = $process.StandardError.ReadToEndAsync()

            $deadline = [DateTimeOffset]::UtcNow.AddMilliseconds($operation.TimeoutMilliseconds)
            while (-not $process.HasExited) {
                if (Test-AzurPilotStopRequested -StopEvent $StopEvent) {
                    try {
                        $process.Kill($true)
                    }
                    catch {
                        Write-StartLog -Level 'WARN' -Message 'Не удалось остановить PostgreSQL preflight после запроса остановки.'
                    }

                    [void]$process.WaitForExit(5000)
                    $script:IntentionalStopRequested = $true
                    Write-StartLog -Level 'INFO' -Message 'PostgreSQL preflight отменён; дальнейшие Docker Compose операции не выполняются.'
                    return $false
                }

                $remainingMilliseconds = [int][Math]::Ceiling(
                    ($deadline - [DateTimeOffset]::UtcNow).TotalMilliseconds
                )
                if ($remainingMilliseconds -le 0) {
                    break
                }

                [void]$process.WaitForExit([Math]::Min(250, $remainingMilliseconds))
            }

            if (-not $process.HasExited) {
                try {
                    $process.Kill($true)
                }
                catch {
                    Write-StartLog -Level 'WARN' -Message 'Не удалось остановить зависшую проверку PostgreSQL.'
                }

                Complete-StartFailure -Code $script:ExitCodeEnvironmentFailure -Message $operation.Failure
            }

            $standardOutput = $stdoutTask.GetAwaiter().GetResult()
            $standardError = $stderrTask.GetAwaiter().GetResult()

            if ($process.ExitCode -ne 0) {
                if (-not [string]::IsNullOrWhiteSpace($standardOutput)) {
                    Write-StartLog -Level 'WARN' -Message ('Диагностика PostgreSQL: {0}' -f $standardOutput.Trim())
                }
                if (-not [string]::IsNullOrWhiteSpace($standardError)) {
                    Write-StartLog -Level 'WARN' -Message ('Ошибка диагностики PostgreSQL: {0}' -f $standardError.Trim())
                }
                Complete-StartFailure -Code $script:ExitCodeEnvironmentFailure -Message $operation.Failure
            }
        }
        catch {
            Complete-StartFailure -Code $script:ExitCodeEnvironmentFailure -Message (
                '{0} Ошибка запуска процесса: {1}' -f $operation.Failure, $_.Exception.Message
            )
        }
        finally {
            $process.Dispose()
        }
    }

    Write-StartLog -Level 'INFO' -Message 'PostgreSQL 18 в Docker Compose запущен; marker, schema upgrade и app-health подготовлены.'
    return $true
}

function Enter-RepositoryMutex {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ResolvedRepositoryPath
    )

    $lifecycleNames = Get-AzurPilotLifecycleName -RepositoryPath $ResolvedRepositoryPath
    $mutexName = $lifecycleNames.StartMutex
    $mutex = [System.Threading.Mutex]::new($false, $mutexName)
    $owned = $false

    try {
        $owned = $mutex.WaitOne(0, $false)
    } catch [System.Threading.AbandonedMutexException] {
        $owned = $true
        Write-StartLog -Level 'WARN' -Message 'Обнаружен заброшенный мьютекс Start. Владение восстановлено.'
    } catch {
        $mutex.Dispose()
        Complete-StartFailure -Code $script:ExitCodeUnexpectedFailure -Message "Не удалось открыть мьютекс Start: $($_.Exception.Message)"
    }

    return [pscustomobject]@{
        Name = $mutexName
        Mutex = $mutex
        Owned = $owned
    }
}

function Get-ReadinessClient {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [bool]$SslEnabled
    )

    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false

    if ($SslEnabled) {
        $handler.ServerCertificateCustomValidationCallback = [System.Net.Http.HttpClientHandler]::DangerousAcceptAnyServerCertificateValidator
    }

    $client = [System.Net.Http.HttpClient]::new($handler, $true)
    $client.Timeout = [TimeSpan]::FromSeconds(3)
    $client.DefaultRequestHeaders.UserAgent.ParseAdd('AzurPilot-Start/2C')

    return $client
}

function Test-WebUiReady {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [System.Net.Http.HttpClient]$Client,

        [Parameter(Mandatory)]
        [System.Uri]$Uri
    )

    $response = $null

    try {
        $response = $Client.GetAsync(
            $Uri,
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()

        $statusCode = [int]$response.StatusCode

        if ($statusCode -ge 200 -and $statusCode -lt 400) {
            return $true
        }

        if ($statusCode -in @(
            401,
            403
        )) {
            return $true
        }

        return $false
    } catch {
        return $false
    } finally {
        if ($null -ne $response) {
            $response.Dispose()
        }
    }
}

function Add-BoundedOutputLine {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNull()]
        [System.Collections.Generic.Queue[string]]$Buffer,

        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Line,

        [Parameter()]
        [ValidateRange(10, 1000)]
        [int]$Limit = 200
    )

    if ([string]::IsNullOrWhiteSpace($Line)) {
        return
    }

    $Buffer.Enqueue($Line)

    while ($Buffer.Count -gt $Limit) {
        [void]$Buffer.Dequeue()
    }
}

function Get-BackendOutputLevel {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateSet(
            'stdout',
            'stderr'
        )]
        [string]$StreamName,

        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Line
    )

    if ($StreamName -eq 'stdout') {
        return 'INFO'
    }

    if ($Line -match '^\s*INFO:') {
        return 'INFO'
    }

    if ($Line -match '(?i)\bwarning\b|DeprecationWarning') {
        return 'WARN'
    }

    return 'ERROR'
}

function Write-BackendOutputLine {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateSet(
            'stdout',
            'stderr'
        )]
        [string]$StreamName,

        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Line
    )

    $level = Get-BackendOutputLevel -StreamName $StreamName -Line $Line
    $message = '[gui {0}] {1}' -f $StreamName, $Line
    Write-StartLog -Level $level -Message $message
}

function Read-AvailableProcessOutput {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$ProcessData
    )

    while (
        $null -ne $ProcessData.StandardOutputReadTask -and
        $ProcessData.StandardOutputReadTask.IsCompleted
    ) {
        try {
            $line = $ProcessData.StandardOutputReadTask.GetAwaiter().GetResult()
        } catch {
            Write-StartLog -Level 'WARN' -Message "Не удалось прочитать stdout gui.py: $($_.Exception.Message)"
            $ProcessData.StandardOutputReadTask = $null
            break
        }

        if ($null -eq $line) {
            $ProcessData.StandardOutputReadTask = $null
            break
        }

        if (-not [string]::IsNullOrWhiteSpace($line)) {
            Add-BoundedOutputLine -Buffer $ProcessData.StandardOutputBuffer -Line $line

            if ($VerboseBackendOutput) {
                Write-BackendOutputLine -StreamName 'stdout' -Line $line
            }
        }

        $ProcessData.StandardOutputReadTask = $ProcessData.Process.StandardOutput.ReadLineAsync()
    }

    while (
        $null -ne $ProcessData.StandardErrorReadTask -and
        $ProcessData.StandardErrorReadTask.IsCompleted
    ) {
        try {
            $line = $ProcessData.StandardErrorReadTask.GetAwaiter().GetResult()
        } catch {
            Write-StartLog -Level 'WARN' -Message "Не удалось прочитать stderr gui.py: $($_.Exception.Message)"
            $ProcessData.StandardErrorReadTask = $null
            break
        }

        if ($null -eq $line) {
            $ProcessData.StandardErrorReadTask = $null
            break
        }

        if (-not [string]::IsNullOrWhiteSpace($line)) {
            Add-BoundedOutputLine -Buffer $ProcessData.StandardErrorBuffer -Line $line

            if ($VerboseBackendOutput) {
                Write-BackendOutputLine -StreamName 'stderr' -Line $line
            }
        }

        $ProcessData.StandardErrorReadTask = $ProcessData.Process.StandardError.ReadLineAsync()
    }
}

function Write-BufferedProcessOutput {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$ProcessData
    )

    if ($ProcessData.StandardOutputBuffer.Count -gt 0) {
        Write-StartLog -Level 'INFO' -Message 'Последние строки stdout gui.py перед ошибкой:'

        foreach ($line in $ProcessData.StandardOutputBuffer) {
            Write-BackendOutputLine -StreamName 'stdout' -Line $line
        }
    }

    if ($ProcessData.StandardErrorBuffer.Count -gt 0) {
        Write-StartLog -Level 'INFO' -Message 'Последние строки stderr gui.py перед ошибкой:'

        foreach ($line in $ProcessData.StandardErrorBuffer) {
            Write-BackendOutputLine -StreamName 'stderr' -Line $line
        }
    }
}

function Read-ProcessOutputToEnd {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$ProcessData,

        [Parameter()]
        [ValidateRange(1, 30)]
        [int]$TimeoutSeconds = 5
    )

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)

    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        Read-AvailableProcessOutput -ProcessData $ProcessData

        if (
            $null -eq $ProcessData.StandardOutputReadTask -and
            $null -eq $ProcessData.StandardErrorReadTask
        ) {
            return
        }

        Start-Sleep -Milliseconds 50
    }

    Write-StartLog -Level 'WARN' -Message 'Не удалось полностью дочитать stdout/stderr gui.py за отведённое время.'
}

function Add-PathEntry {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [ValidateNotNull()]
        [System.Collections.Generic.List[string]]$Entries,

        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Entry
    )

    if ([string]::IsNullOrWhiteSpace($Entry)) {
        return
    }

    foreach ($existingEntry in $Entries) {
        if (
            [string]::Equals(
                $existingEntry,
                $Entry,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        ) {
            return
        }
    }

    [void]$Entries.Add($Entry)
}

function Invoke-AzurPilotBackendStart {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$PythonPath,

        [Parameter(Mandatory)]
        [string]$GuiPath,

        [Parameter(Mandatory)]
        [string]$ResolvedRepositoryPath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$StopEventName
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $PythonPath
    $startInfo.WorkingDirectory = $ResolvedRepositoryPath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = [bool]$FromShortcut
    [void]$startInfo.ArgumentList.Add($GuiPath)
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $utf8Encoding = [System.Text.UTF8Encoding]::new($false)
    $startInfo.StandardOutputEncoding = $utf8Encoding
    $startInfo.StandardErrorEncoding = $utf8Encoding

    foreach ($environmentName in @(
        'PYTHONHOME',
        'pythonhome',
        'PYTHONPATH',
        'pythonpath',
        'VIRTUAL_ENV',
        'virtual_env',
        '__PYVENV_LAUNCHER__'
    )) {
        [void]$startInfo.Environment.Remove($environmentName)
    }

    $startInfo.Environment['PYTHONUTF8'] = '1'
    $startInfo.Environment['PYTHONUNBUFFERED'] = '1'
    $startInfo.Environment['AZURPILOT_LIFECYCLE_STOP_EVENT'] = $StopEventName

    $pathEntries = [System.Collections.Generic.List[string]]::new()
    $venvScriptsPath = Split-Path -Path $PythonPath -Parent
    $venvGitPath = Join-Path -Path $venvScriptsPath -ChildPath 'git\cmd'

    Add-PathEntry -Entries $pathEntries -Entry $venvScriptsPath

    if (Test-Path -LiteralPath $venvGitPath -PathType Container) {
        Add-PathEntry -Entries $pathEntries -Entry $venvGitPath
    }

    $existingPath = [string]$startInfo.Environment['PATH']

    foreach ($existingEntry in @($existingPath -split [regex]::Escape([System.IO.Path]::PathSeparator))) {
        Add-PathEntry -Entries $pathEntries -Entry $existingEntry
    }

    $pathSeparator = [string][System.IO.Path]::PathSeparator
    $startInfo.Environment['PATH'] = $pathEntries -join $pathSeparator

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo

    try {
        if (-not $process.Start()) {
            Complete-StartFailure -Code $script:ExitCodeBackendFailure -Message 'Не удалось запустить gui.py.'
        }

        return [pscustomobject]@{
            Process = $process
            StandardOutputReadTask = $process.StandardOutput.ReadLineAsync()
            StandardErrorReadTask = $process.StandardError.ReadLineAsync()
            StandardOutputBuffer = [System.Collections.Generic.Queue[string]]::new()
            StandardErrorBuffer = [System.Collections.Generic.Queue[string]]::new()
        }
    } catch {
        $process.Dispose()

        if ($_.Exception.Data.Contains('ExitCode')) {
            throw
        }

        Complete-StartFailure -Code $script:ExitCodeBackendFailure -Message "Не удалось запустить gui.py: $($_.Exception.Message)"
    }
}

function Invoke-StartedBackendStop {
    [CmdletBinding()]
    param(
        [Parameter()]
        [ValidateNotNullOrEmpty()]
        [string]$Reason = 'завершение управляющего процесса Start',

        [Parameter()]
        [switch]$Intentional
    )

    if ($null -eq $script:StartedProcess) {
        return
    }

    try {
        if ($script:StartedProcess.HasExited) {
            return
        }

        $backendProcessId = $script:StartedProcess.Id
        $logLevel = if ($Intentional) {
            'INFO'
        } else {
            'WARN'
        }
        Write-StartLog -Level $logLevel -Message (
            'Останавливается серверный процесс с PID {0}. Причина: {1}.' -f
            $backendProcessId,
            $Reason
        )

        $script:StartedProcess.Kill($true)
        [void]$script:StartedProcess.WaitForExit(10000)

        if (-not $script:StartedProcess.HasExited) {
            throw (
                'Серверный процесс с PID {0} не завершился за 10 секунд после Kill(entireProcessTree).' -f
                $backendProcessId
            )
        }

        if ($null -ne $script:StartedProcessData) {
            Read-ProcessOutputToEnd -ProcessData $script:StartedProcessData
        }

        Write-StartLog -Level 'INFO' -Message (
            'Серверный процесс с PID {0} и его дерево процессов остановлены.' -f
            $backendProcessId
        )
    } catch {
        Write-StartLog -Level 'WARN' -Message (
            'Не удалось остановить серверный процесс с PID {0}: {1}' -f
            $script:StartedProcess.Id,
            $_.Exception.Message
        )
    }
}

function Enable-AzurPilotConsoleStopHandler {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [System.Threading.EventWaitHandle]$StopEvent
    )

    if ($FromShortcut) {
        return $false
    }

    if (-not ('AzurPilotConsoleStopHandler' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Threading;

public static class AzurPilotConsoleStopHandler
{
    private static EventWaitHandle stopEvent;
    private static ConsoleCancelEventHandler handler;

    public static void Install(EventWaitHandle target)
    {
        if (handler != null)
        {
            throw new InvalidOperationException("Console stop handler is already installed.");
        }

        stopEvent = target ?? throw new ArgumentNullException(nameof(target));
        handler = OnCancelKeyPress;
        Console.CancelKeyPress += handler;
    }

    public static void Remove()
    {
        if (handler != null)
        {
            Console.CancelKeyPress -= handler;
        }

        handler = null;
        stopEvent = null;
    }

    private static void OnCancelKeyPress(object sender, ConsoleCancelEventArgs args)
    {
        if (args.SpecialKey != ConsoleSpecialKey.ControlC)
        {
            return;
        }

        args.Cancel = true;
        stopEvent?.Set();
    }
}
'@ -ErrorAction Stop
    }

    [AzurPilotConsoleStopHandler]::Install($StopEvent)
    return $true
}

function Wait-ForIntentionalBackendStop {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$ProcessData,

        [Parameter()]
        [ValidateRange(1, 60)]
        [int]$GracefulTimeoutSeconds = 15
    )

    $script:IntentionalStopRequested = $true
    Write-StartLog -Level 'INFO' -Message 'Получен координированный запрос штатной остановки.'
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($GracefulTimeoutSeconds)

    while (
        -not $ProcessData.Process.HasExited -and
        [DateTimeOffset]::UtcNow -lt $deadline
    ) {
        Read-AvailableProcessOutput -ProcessData $ProcessData
        [void]$ProcessData.Process.WaitForExit(200)
    }

    if (-not $ProcessData.Process.HasExited) {
        Write-StartLog -Level 'WARN' -Message (
            'Штатная остановка не завершилась за {0} секунд; применяется принудительный fallback доказанного дерева AzurPilot.' -f
            $GracefulTimeoutSeconds
        )
        Invoke-StartedBackendStop -Reason 'таймаут штатной остановки' -Intentional
    }

    if ($ProcessData.Process.HasExited) {
        Read-ProcessOutputToEnd -ProcessData $ProcessData
    }
}

function Wait-ForExistingBackend {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [int]$Port,

        [Parameter(Mandatory)]
        [string]$ProjectPythonPath,

        [Parameter(Mandatory)]
        [string]$GuiPath,

        [Parameter(Mandatory)]
        [System.Net.Http.HttpClient]$ReadinessClient,

        [Parameter(Mandatory)]
        [System.Uri]$BrowserUri,

        [Parameter(Mandatory)]
        [int]$TimeoutSeconds
    )

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)

    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        $ownershipParameters = @{
            Port = $Port
            ProjectPythonPath = $ProjectPythonPath
            GuiPath = $GuiPath
        }

        $ownership = Get-AzurPilotPortOwnershipState @ownershipParameters

        if ($ownership.State -eq 'Foreign') {
            $foreignText = $ownership.ForeignProcessIds -join ', '
            Complete-StartFailure -Code $script:ExitCodeForeignPortOwner -Message "Порт $Port занят посторонним или неидентифицируемым процессом. PID: $foreignText"
        }

        if (
            $ownership.State -eq 'AzurPilot' -and
            (Test-WebUiReady -Client $ReadinessClient -Uri $BrowserUri)
        ) {
            return $true
        }

        Start-Sleep -Milliseconds 250
    }

    return $false
}

function Wait-ForStartedBackend {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$ProcessData,

        [Parameter(Mandatory)]
        [int]$Port,

        [Parameter(Mandatory)]
        [string]$ProjectPythonPath,

        [Parameter(Mandatory)]
        [string]$GuiPath,

        [Parameter(Mandatory)]
        [System.Net.Http.HttpClient]$ReadinessClient,

        [Parameter(Mandatory)]
        [System.Uri]$BrowserUri,

        [Parameter(Mandatory)]
        [int]$TimeoutSeconds
    )

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)

    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        Read-AvailableProcessOutput -ProcessData $ProcessData

        if (Test-AzurPilotStopRequested -StopEvent $script:StopEvent) {
            Wait-ForIntentionalBackendStop -ProcessData $ProcessData
            return $false
        }

        if ($ProcessData.Process.HasExited) {
            Read-ProcessOutputToEnd -ProcessData $ProcessData
            Write-BufferedProcessOutput -ProcessData $ProcessData
            Complete-StartFailure -Code $script:ExitCodeBackendFailure -Message "gui.py завершился до готовности. Код: $($ProcessData.Process.ExitCode)"
        }

        $ownershipParameters = @{
            Port = $Port
            ProjectPythonPath = $ProjectPythonPath
            GuiPath = $GuiPath
        }

        $ownership = Get-AzurPilotPortOwnershipState @ownershipParameters

        if ($ownership.State -eq 'Foreign') {
            $foreignText = $ownership.ForeignProcessIds -join ', '
            Read-ProcessOutputToEnd -ProcessData $ProcessData
            Write-BufferedProcessOutput -ProcessData $ProcessData
            Invoke-StartedBackendStop -Reason 'порт перехвачен посторонним процессом во время запуска'
            Complete-StartFailure -Code $script:ExitCodeForeignPortOwner -Message "Порт $Port занят посторонним или неидентифицируемым процессом. PID: $foreignText"
        }

        if (
            $ownership.State -eq 'AzurPilot' -and
            (Test-WebUiReady -Client $ReadinessClient -Uri $BrowserUri)
        ) {
            return $true
        }

        Start-Sleep -Milliseconds 250
    }

    Read-ProcessOutputToEnd -ProcessData $ProcessData
    Write-BufferedProcessOutput -ProcessData $ProcessData
    Invoke-StartedBackendStop -Reason 'истекло время ожидания готовности'
    Complete-StartFailure -Code $script:ExitCodeReadinessTimeout -Message "WebUI не стал готов за $TimeoutSeconds секунд: $BrowserUri"
}

function Open-WebUiBrowser {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [System.Uri]$BrowserUri
    )

    if ($NoBrowser) {
        Write-StartLog -Level 'INFO' -Message "Открытие браузера отключено параметром -NoBrowser. URL: $BrowserUri"
        return $true
    }

    try {
        Start-Process -FilePath $BrowserUri.AbsoluteUri -ErrorAction Stop | Out-Null
        Write-StartLog -Level 'INFO' -Message "Открыт системный браузер: $BrowserUri"
        return $true
    } catch {
        Write-StartLog -Level 'ERROR' -Message "Серверный процесс работает, но браузер открыть не удалось: $($_.Exception.Message)"
        Write-ConsoleMessage -Message "Откройте вручную: $BrowserUri"

        if ($FromShortcut) {
            try {
                Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop

                [void][System.Windows.Forms.MessageBox]::Show(
                    (
                        'AzurPilot запущен, но браузер не открылся автоматически.' +
                        [Environment]::NewLine +
                        [Environment]::NewLine +
                        'Откройте вручную:' +
                        [Environment]::NewLine +
                        $BrowserUri
                    ),
                    'AzurPilot запущен',
                    [System.Windows.Forms.MessageBoxButtons]::OK,
                    [System.Windows.Forms.MessageBoxIcon]::Warning
                )
            } catch {
                Write-StartLog -Level 'WARN' -Message "Не удалось показать URL через Windows-диалог: $($_.Exception.Message)"
            }
        }

        return $false
    }
}

function Invoke-AzurPilotStart {
    [CmdletBinding()]
    param()

    $readinessClient = $null
    $browserOpenFailed = $false

    try {
        Initialize-StartLog
        Write-StartLog -Level 'INFO' -Message 'Запуск AzurPilot, этап 2C.'
        Write-StartLog -Level 'INFO' -Message "PowerShell: $($PSVersionTable.PSVersion)"
        Write-StartLog -Level 'INFO' -Message "Путь к репозиторию: $RepositoryPath"

        if (-not $IsWindows) {
            Complete-StartFailure -Code $script:ExitCodePreconditionFailure -Message 'Команда Start поддерживает только Windows.'
        }

        $repositoryPathParameters = @{
            Path = $RepositoryPath
            PathType = 'Container'
            Label = 'Каталог AzurPilot'
        }

        $resolvedRepositoryPath = Resolve-RequiredPath @repositoryPathParameters

        $lifecycleNames = Get-AzurPilotLifecycleName -RepositoryPath $resolvedRepositoryPath
        $mutexData = Enter-RepositoryMutex -ResolvedRepositoryPath $resolvedRepositoryPath
        $script:StartMutex = $mutexData.Mutex
        $script:StartMutexOwned = $mutexData.Owned

        Write-StartLog -Level 'INFO' -Message "Мьютекс Start: $($mutexData.Name)"
        Write-StartLog -Level 'INFO' -Message "Мьютекс Start захвачен: $($mutexData.Owned)"

        if ($mutexData.Owned) {
            $script:StopEvent = New-AzurPilotStopEvent -Name $lifecycleNames.StopEvent -ReuseExisting

            if ($null -eq $script:StopEvent) {
                Write-StartLog -Level 'WARN' -Message 'Объект координации остановки не создан; координация штатной остановки недоступна.'
            } else {
                $script:ConsoleStopHandlerInstalled = Enable-AzurPilotConsoleStopHandler -StopEvent $script:StopEvent
                Write-StartLog -Level 'INFO' -Message 'Координация штатной остановки активна.'
            }
        }

        $guiPathParameters = @{
            Path = Join-Path -Path $resolvedRepositoryPath -ChildPath 'gui.py'
            PathType = 'Leaf'
            Label = 'gui.py'
        }

        $guiPath = Resolve-RequiredPath @guiPathParameters

        $deployConfigPathParameters = @{
            Path = Join-Path -Path $resolvedRepositoryPath -ChildPath 'config\deploy.yaml'
            PathType = 'Leaf'
            Label = 'config\deploy.yaml'
        }

        $deployConfigPath = Resolve-RequiredPath @deployConfigPathParameters

        $projectPythonPathParameters = @{
            Path = Join-Path -Path $resolvedRepositoryPath -ChildPath '.venv\Scripts\python.exe'
            PathType = 'Leaf'
            Label = 'Python проекта'
            FailureCode = $script:ExitCodeEnvironmentFailure
        }

        $projectPythonPath = Resolve-RequiredPath @projectPythonPathParameters

        $pythonHealthParameters = @{
            PythonPath = $projectPythonPath
            WorkingDirectory = $resolvedRepositoryPath
        }

        Invoke-PythonHealthCheck @pythonHealthParameters

        Write-StartLog -Level 'INFO' -Message "Python проекта исправен: $projectPythonPath"

        if (Test-AzurPilotStopRequested -StopEvent $script:StopEvent) {
            $script:IntentionalStopRequested = $true
            Write-StartLog -Level 'INFO' -Message 'Запуск отменён координированным запросом остановки до старта backend.'
            return $script:ExitCodeSuccess
        }

        $webUiConfiguration = Get-WebUiConfiguration -DeployConfigPath $deployConfigPath
        $browserUri = $webUiConfiguration.BrowserUri

        Write-StartLog -Level 'INFO' -Message "Адрес привязки WebUI: $($webUiConfiguration.BindHost):$($webUiConfiguration.Port)"
        Write-StartLog -Level 'INFO' -Message "WebUI URL: $browserUri"
        Write-StartLog -Level 'INFO' -Message "SSL включён: $($webUiConfiguration.SslEnabled)"

        $readinessClient = Get-ReadinessClient -SslEnabled $webUiConfiguration.SslEnabled

        if (-not $mutexData.Owned) {
            Write-StartLog -Level 'INFO' -Message 'Другой Start уже управляет этим репозиторием. Ожидание существующего WebUI.'

            $existingBackendWaitParameters = @{
                Port = $webUiConfiguration.Port
                ProjectPythonPath = $projectPythonPath
                GuiPath = $guiPath
                ReadinessClient = $readinessClient
                BrowserUri = $browserUri
                TimeoutSeconds = $StartupTimeoutSeconds
            }

            $existingReady = Wait-ForExistingBackend @existingBackendWaitParameters

            if (-not $existingReady) {
                Complete-StartFailure -Code $script:ExitCodeConcurrentStartTimeout -Message "Другой Start не довёл WebUI до готовности за $StartupTimeoutSeconds секунд."
            }

            Write-StartLog -Level 'INFO' -Message 'AzurPilot уже запущен и готов.'
            Write-StartLog -Level 'INFO' -Message 'Этот запуск только открывает существующий WebUI; текущая консоль не управляет серверным процессом.'
            Write-StartLog -Level 'INFO' -Message ("Для остановки: {0}" -f (Join-Path -Path $resolvedRepositoryPath -ChildPath 'scripts\Stop-AzurPilot.ps1'))

            $browserOpened = Open-WebUiBrowser -BrowserUri $browserUri

            if (-not $browserOpened) {
                return $script:ExitCodeBrowserFailure
            }

            return $script:ExitCodeSuccess
        }

        $initialOwnershipParameters = @{
            Port = $webUiConfiguration.Port
            ProjectPythonPath = $projectPythonPath
            GuiPath = $guiPath
        }

        $initialOwnership = Get-AzurPilotPortOwnershipState @initialOwnershipParameters

        if ($initialOwnership.State -eq 'Foreign') {
            $foreignText = $initialOwnership.ForeignProcessIds -join ', '
            Complete-StartFailure -Code $script:ExitCodeForeignPortOwner -Message "Порт $($webUiConfiguration.Port) занят посторонним или неидентифицируемым процессом. PID: $foreignText"
        }

        if ($initialOwnership.State -eq 'AzurPilot') {
            $existingBackendWaitParameters = @{
                Port = $webUiConfiguration.Port
                ProjectPythonPath = $projectPythonPath
                GuiPath = $guiPath
                ReadinessClient = $readinessClient
                BrowserUri = $browserUri
                TimeoutSeconds = $StartupTimeoutSeconds
            }

            $existingReady = Wait-ForExistingBackend @existingBackendWaitParameters

            if (-not $existingReady) {
                Complete-StartFailure -Code $script:ExitCodeReadinessTimeout -Message "Найден процесс AzurPilot, но WebUI не стал готов за $StartupTimeoutSeconds секунд."
            }

            Write-StartLog -Level 'INFO' -Message 'AzurPilot уже запущен и готов.'
            Write-StartLog -Level 'INFO' -Message 'Этот запуск только открывает существующий WebUI; текущая консоль не управляет серверным процессом.'
            Write-StartLog -Level 'INFO' -Message ("Для остановки: {0}" -f (Join-Path -Path $resolvedRepositoryPath -ChildPath 'scripts\Stop-AzurPilot.ps1'))

            $browserOpened = Open-WebUiBrowser -BrowserUri $browserUri

            if (-not $browserOpened) {
                return $script:ExitCodeBrowserFailure
            }

            return $script:ExitCodeSuccess
        }

        if (Test-AzurPilotStopRequested -StopEvent $script:StopEvent) {
            $script:IntentionalStopRequested = $true
            Write-StartLog -Level 'INFO' -Message 'Запуск отменён координированным запросом остановки до PostgreSQL preflight.'
            return $script:ExitCodeSuccess
        }

        $preflightParameters = @{
            PythonPath = $projectPythonPath
            WorkingDirectory = $resolvedRepositoryPath
            StopEvent = $script:StopEvent
        }
        $preflightCompleted = Invoke-PostgreSqlStartPreflight @preflightParameters

        if (-not $preflightCompleted -or (Test-AzurPilotStopRequested -StopEvent $script:StopEvent)) {
            $script:IntentionalStopRequested = $true
            Write-StartLog -Level 'INFO' -Message 'Запуск отменён координированным запросом остановки после preflight.'
            return $script:ExitCodeSuccess
        }

        Write-StartLog -Level 'INFO' -Message 'Запуск gui.py без обновления Git и без синхронизации зависимостей.'

        $backendStartParameters = @{
            PythonPath = $projectPythonPath
            GuiPath = $guiPath
            ResolvedRepositoryPath = $resolvedRepositoryPath
            StopEventName = $lifecycleNames.StopEvent
        }

        $script:StartedProcessData = Invoke-AzurPilotBackendStart @backendStartParameters

        $script:StartedProcess = $script:StartedProcessData.Process

        Write-StartLog -Level 'INFO' -Message "gui.py запущен. PID: $($script:StartedProcess.Id)"

        $startedBackendWaitParameters = @{
            ProcessData = $script:StartedProcessData
            Port = $webUiConfiguration.Port
            ProjectPythonPath = $projectPythonPath
            GuiPath = $guiPath
            ReadinessClient = $readinessClient
            BrowserUri = $browserUri
            TimeoutSeconds = $StartupTimeoutSeconds
        }

        $startedReady = Wait-ForStartedBackend @startedBackendWaitParameters

        if (-not $startedReady -and $script:IntentionalStopRequested) {
            Write-StartLog -Level 'INFO' -Message 'AzurPilot штатно остановлен во время запуска.'
            return $script:ExitCodeSuccess
        }

        $script:BackendReady = $true
        Write-StartLog -Level 'INFO' -Message "WebUI готов: $browserUri"

        $browserOpened = Open-WebUiBrowser -BrowserUri $browserUri

        if (-not $browserOpened) {
            $browserOpenFailed = $true
        }

        Write-StartLog -Level 'INFO' -Message "Ожидание завершения серверного процесса с PID $($script:StartedProcess.Id)."

        while (-not $script:StartedProcess.HasExited) {
            Read-AvailableProcessOutput -ProcessData $script:StartedProcessData

            if (Test-AzurPilotStopRequested -StopEvent $script:StopEvent) {
                Wait-ForIntentionalBackendStop -ProcessData $script:StartedProcessData
                break
            }

            [void]$script:StartedProcess.WaitForExit(250)
        }

        Read-ProcessOutputToEnd -ProcessData $script:StartedProcessData

        $backendExitCode = $script:StartedProcess.ExitCode
        Write-StartLog -Level 'INFO' -Message "Серверный процесс завершился с кодом $backendExitCode."

        if ($script:IntentionalStopRequested) {
            Write-StartLog -Level 'INFO' -Message 'AzurPilot остановлен штатно по внешнему запросу.'
            return $script:ExitCodeSuccess
        }

        if ($backendExitCode -ne 0) {
            Write-BufferedProcessOutput -ProcessData $script:StartedProcessData
            Complete-StartFailure -Code $script:ExitCodeBackendFailure -Message "AzurPilot завершился с кодом $backendExitCode."
        }

        if ($browserOpenFailed) {
            return $script:ExitCodeBrowserFailure
        }

        return $script:ExitCodeSuccess
    } catch {
        $exitCode = $script:ExitCodeUnexpectedFailure

        if ($_.Exception.Data.Contains('ExitCode')) {
            $exitCode = [int]$_.Exception.Data['ExitCode']
        }

        if (
            $null -ne $script:StartedProcess -and
            -not $script:StartedProcess.HasExited
        ) {
            Invoke-StartedBackendStop -Reason 'ошибка или прерывание управляющего процесса Start'
        }

        $errorMessage = $_.Exception.Message

        try {
            Write-StartLog -Level 'ERROR' -Message $errorMessage
        } catch {
            Write-ConsoleMessage -Message "AzurPilot: $errorMessage"
        }

        Show-ShortcutError -Message $errorMessage

        return $exitCode
    } finally {
        if ($null -ne $readinessClient) {
            $readinessClient.Dispose()
        }

        if ($null -ne $script:StartedProcess) {
            if (-not $script:StartedProcess.HasExited) {
                Invoke-StartedBackendStop -Reason 'выход управляющего процесса Start'
            }

            $script:StartedProcess.Dispose()
        }

        if ($null -ne $script:StopEvent) {
            if ($script:ConsoleStopHandlerInstalled) {
                [AzurPilotConsoleStopHandler]::Remove()
            }

            $script:StopEvent.Dispose()
        }

        if ($null -ne $script:StartMutex) {
            if ($script:StartMutexOwned) {
                try {
                    $script:StartMutex.ReleaseMutex()
                } catch {
                    if ($null -ne $script:LogPath) {
                        Write-StartLog -Level 'WARN' -Message "Не удалось освободить мьютекс Start: $($_.Exception.Message)"
                    }
                }
            }

            $script:StartMutex.Dispose()
        }

        if ($null -ne $script:LogPath) {
            Write-ConsoleMessage -Message "Лог: $script:LogPath"
        }
    }
}

$startExitCode = Invoke-AzurPilotStart
exit $startExitCode
