[CmdletBinding()]
param(
    [Parameter()]
    [string]$RepositoryPath = (Split-Path -Parent $PSScriptRoot),

    [Parameter()]
    [string]$DeployConfigCopy
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

if ([string]::IsNullOrWhiteSpace($RepositoryPath)) {
    throw 'Путь к репозиторию не задан.'
}

if (-not (Test-Path -LiteralPath $RepositoryPath -PathType Container)) {
    throw ('Каталог репозитория не существует: {0}' -f $RepositoryPath)
}

$repositoryItem = Get-Item -LiteralPath $RepositoryPath -Force -ErrorAction Stop

if ($repositoryItem.PSProvider.Name -ne 'FileSystem') {
    throw ('Путь не относится к файловой системе: {0}' -f $RepositoryPath)
}

$RepositoryPath = Convert-Path -LiteralPath $RepositoryPath
$pythonExecutable = Join-Path -Path $RepositoryPath -ChildPath '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw ('Не найден Python подготовленной установки: {0}' -f $pythonExecutable)
}

if ([string]::IsNullOrWhiteSpace($DeployConfigCopy)) {
    $realDeployConfig = Join-Path -Path $RepositoryPath -ChildPath 'config\deploy.yaml'
    if (Test-Path -LiteralPath $realDeployConfig -PathType Leaf) {
        $DeployConfigCopy = Convert-Path -LiteralPath $realDeployConfig
    }
}
elseif (-not (Test-Path -LiteralPath $DeployConfigCopy -PathType Leaf)) {
    throw ('Исходный deploy.yaml для безопасной проверки не существует: {0}' -f $DeployConfigCopy)
}
else {
    $DeployConfigCopy = Convert-Path -LiteralPath $DeployConfigCopy
}

$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
$logDirectory = Join-Path -Path $localAppData -ChildPath 'AzurPilot\stage5-verification'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logPath = Join-Path -Path $logDirectory -ChildPath ('stage5-{0}.log' -f $timestamp)

$testArguments = @(
    '-m'
    'unittest'
    '-v'
    'tests/test_stage5_deploy_language_migration.py'
    'tests/test_stage5_locale_runtime.py'
    'tests/test_stage5_server_separation.py'
    'tests/test_stage5_generator.py'
    'tests/test_russianization_audit.py'
)

Push-Location -LiteralPath $RepositoryPath
try {
    & $pythonExecutable @testArguments 2>&1 |
        Tee-Object -FilePath $logPath
    $testExitCode = $LASTEXITCODE

    if ($testExitCode -ne 0) {
        throw ('Автоматические проверки Stage 5 завершились с кодом {0}.' -f $testExitCode)
    }

    $verifyArguments = @(
        '-m'
        'dev_tools.verify_stage5'
    )

    if (-not [string]::IsNullOrWhiteSpace($DeployConfigCopy)) {
        $verifyArguments += '--deploy-copy'
        $verifyArguments += $DeployConfigCopy
    }

    & $pythonExecutable @verifyArguments 2>&1 |
        Tee-Object -FilePath $logPath -Append
    $verifyExitCode = $LASTEXITCODE

    if ($verifyExitCode -ne 0) {
        throw ('Проверка безопасной копии deploy.yaml завершилась с кодом {0}.' -f $verifyExitCode)
    }
}
finally {
    Pop-Location
}

Write-Output ''
Write-Output 'Автоматическая часть Stage 5 прошла успешно.'
Write-Output ('Журнал: {0}' -f $logPath)
Write-Output 'Теперь запустите AzurPilot обычной командой Start и проверьте:'
Write-Output '1. Переключатель языка отсутствует.'
Write-Output '2. Существующий EN/Global профиль сохранил server, package и OCR-настройки.'
