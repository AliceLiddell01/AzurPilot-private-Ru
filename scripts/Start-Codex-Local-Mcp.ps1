$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonExecutable = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
$stateDirectory = Join-Path $repositoryRoot 'config\state\local-mcp-http'

if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "Project Python для local MCP не найден: $pythonExecutable"
}

foreach ($tokenName in @('AZURPILOT_DEV_LOCAL_MCP_TOKEN', 'AZURPILOT_GAME_LOCAL_MCP_TOKEN')) {
    $tokenValue = [Environment]::GetEnvironmentVariable($tokenName, 'User')
    if ([string]::IsNullOrWhiteSpace($tokenValue)) {
        throw "User-level token для local MCP не задан: $tokenName"
    }
    Set-Item -Path "Env:$tokenName" -Value $tokenValue
}

New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
$stdoutLog = Join-Path $stateDirectory 'supervisor.stdout.log'
$stderrLog = Join-Path $stateDirectory 'supervisor.stderr.log'

$supervisorModule = 'module.mcp_shared.local_http_supervisor'
$statusCommand = @('-u', '-m', $supervisorModule, 'status')
try {
    $existingStatus = (& $pythonExecutable @statusCommand 2>$null | ConvertFrom-Json)
    if ($existingStatus.ok -eq $true -and $existingStatus.code -eq 'LOCAL_MCP_SUPERVISOR_READY') {
        exit 0
    }
} catch {
    # При отсутствии marker supervisor будет запущен ниже.
    $existingStatus = $null
}

Start-Process `
    -FilePath $pythonExecutable `
    -ArgumentList @('-u', '-m', $supervisorModule, 'serve') `
    -WorkingDirectory $repositoryRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog | Out-Null

$services = @(
    @{ Name = 'azurpilot-dev'; Port = 8775 },
    @{ Name = 'azurpilot-game'; Port = 8776 }
)
$deadline = (Get-Date).AddSeconds(25)
do {
    $ready = $true
    $supervisorReady = $false
    try {
        $statusPayload = (& $pythonExecutable @statusCommand 2>$null | ConvertFrom-Json)
        $supervisorReady = $statusPayload.ok -eq $true -and $statusPayload.code -eq 'LOCAL_MCP_SUPERVISOR_READY'
    } catch {
        $supervisorReady = $false
    }
    foreach ($service in $services) {
        try {
            $response = Invoke-WebRequest `
                -Uri "http://127.0.0.1:$($service.Port)/ready" `
                -Headers @{ Host = "127.0.0.1:$($service.Port)" } `
                -Method Get `
                -TimeoutSec 1 `
                -UseBasicParsing
            $payload = $response.Content | ConvertFrom-Json
            if ($response.StatusCode -ne 200 -or $payload.ok -ne $true -or $payload.code -ne 'LOCAL_MCP_READY' -or $payload.server_name -ne $service.Name -or $payload.transport -ne 'local_http') {
                $ready = $false
            }
        } catch {
            $ready = $false
        }
    }
    if ($ready -and $supervisorReady) {
        exit 0
    }
    Start-Sleep -Milliseconds 250
} while ((Get-Date) -lt $deadline)

throw 'Local MCP supervisor не достиг readiness обоих endpoints'
