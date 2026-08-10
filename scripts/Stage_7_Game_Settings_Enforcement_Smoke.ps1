[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ExpectedCommit,

    [string]$RepositoryPath = 'C:\AzurPilot',

    [string]$ConfigName = 'ap',

    [string]$DeviceSerial = '127.0.0.1:16416',

    [switch]$Audit,

    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$TaskBranch = 'feature/game-settings-enforcement'
$StableBranch = 'personal/stable'
$ExpectedRepositoryHttps = 'https://github.com/AliceLiddell01/AzurPilot-private-Ru'
$ExpectedRepositorySsh = 'git@github.com:AliceLiddell01/AzurPilot-private-Ru'
$finalExitCode = 1
$temporaryPythonPath = $null
$originalBranch = $null
$originalCommit = $null
$switchedCheckout = $false

function Write-SmokeMessage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Message
    )

    Write-Information -MessageData $Message -InformationAction Continue
}

function Invoke-GitCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [int[]]$AllowedExitCodes = @(0)
    )

    $gitOutput = & $script:GitExecutable @Arguments 2>&1
    $gitExitCode = $LASTEXITCODE

    if ($AllowedExitCodes -notcontains $gitExitCode) {
        $safeArguments = $Arguments -join ' '
        $message = 'Git завершился с кодом {0}: {1}' -f $gitExitCode, $safeArguments
        throw [System.InvalidOperationException]::new($message)
    }

    return [pscustomobject]@{
        ExitCode = $gitExitCode
        Output = @($gitOutput)
    }
}

function Get-LastOutputLine {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Output
    )

    if ($Output.Count -eq 0) {
        return ''
    }

    return [string]($Output | Select-Object -Last 1)
}

function Test-CleanWorkingTree {
    [CmdletBinding()]
    param()

    $statusArguments = @(
        '-C'
        $script:RepositoryPath
        'status'
        '--porcelain=v1'
        '--untracked-files=all'
    )
    $statusResult = Invoke-GitCommand -Arguments $statusArguments

    if ($statusResult.Output.Count -ne 0) {
        throw 'Рабочее дерево не чистое. Smoke не будет менять checkout при наличии пользовательских изменений.'
    }
}

if ($Audit -and $Apply) {
    throw 'Укажите только один режим: -Audit или -Apply.'
}

if (-not $Audit -and -not $Apply) {
    $Audit = $true
}

if ([string]::IsNullOrWhiteSpace($RepositoryPath)) {
    throw 'Путь к репозиторию не задан.'
}

if ([string]::IsNullOrWhiteSpace($ConfigName)) {
    throw 'Имя конфигурации не задано.'
}

if ([string]::IsNullOrWhiteSpace($DeviceSerial)) {
    throw 'Серийный номер устройства не задан.'
}

if ([string]::IsNullOrWhiteSpace($ExpectedCommit)) {
    throw 'Ожидаемый commit SHA не задан.'
}

if (-not (Test-Path -LiteralPath $RepositoryPath -PathType Container)) {
    throw ('Каталог репозитория не существует: {0}' -f $RepositoryPath)
}

$repositoryItem = Get-Item -LiteralPath $RepositoryPath -Force -ErrorAction Stop

if ($repositoryItem.PSProvider.Name -ne 'FileSystem') {
    throw ('Путь не относится к файловой системе: {0}' -f $RepositoryPath)
}

$RepositoryPath = Convert-Path -LiteralPath $RepositoryPath
$script:RepositoryPath = $RepositoryPath
$gitCommand = Get-Command git -CommandType Application -ErrorAction Stop
$script:GitExecutable = $gitCommand.Path

try {
    $workTreeArguments = @(
        '-C'
        $RepositoryPath
        'rev-parse'
        '--is-inside-work-tree'
    )
    $workTreeResult = Invoke-GitCommand -Arguments $workTreeArguments
    $workTreeValue = Get-LastOutputLine -Output $workTreeResult.Output

    if ($workTreeValue.Trim() -ne 'true') {
        throw ('Каталог не является рабочим деревом Git: {0}' -f $RepositoryPath)
    }

    $branchValidationArguments = @(
        'check-ref-format'
        '--branch'
        $TaskBranch
    )
    $branchValidationResult = Invoke-GitCommand -Arguments $branchValidationArguments
    $validatedBranch = Get-LastOutputLine -Output $branchValidationResult.Output

    if ($validatedBranch -cne $TaskBranch) {
        throw ('Недопустимое имя рабочей ветки: {0}' -f $TaskBranch)
    }

    Test-CleanWorkingTree

    $originArguments = @(
        '-C'
        $RepositoryPath
        'remote'
        'get-url'
        'origin'
    )
    $originResult = Invoke-GitCommand -Arguments $originArguments
    $originUrl = (Get-LastOutputLine -Output $originResult.Output).Trim()
    $normalizedOrigin = $originUrl -replace '\.git$', ''

    if ($normalizedOrigin -ne $ExpectedRepositoryHttps -and $normalizedOrigin -ne $ExpectedRepositorySsh) {
        throw 'origin не указывает на AliceLiddell01/AzurPilot-private-Ru.'
    }

    Write-SmokeMessage -Message 'Обновляю безопасные remote-tracking refs перед smoke...'
    $fetchArguments = @(
        '-C'
        $RepositoryPath
        'fetch'
        '--no-tags'
        'origin'
    )
    [void](Invoke-GitCommand -Arguments $fetchArguments)

    $remoteFeatureRef = 'refs/remotes/origin/{0}' -f $TaskBranch
    $remoteStableRef = 'refs/remotes/origin/{0}' -f $StableBranch

    $featureRefArguments = @(
        '-C'
        $RepositoryPath
        'show-ref'
        '--verify'
        '--quiet'
        $remoteFeatureRef
    )
    $featureRefResult = Invoke-GitCommand -Arguments $featureRefArguments -AllowedExitCodes @(0, 1)

    if ($featureRefResult.ExitCode -ne 0) {
        throw ('Remote feature ref не найден: {0}' -f $remoteFeatureRef)
    }

    $featureShaArguments = @(
        '-C'
        $RepositoryPath
        'rev-parse'
        ('{0}^{{commit}}' -f $remoteFeatureRef)
    )
    $featureShaResult = Invoke-GitCommand -Arguments $featureShaArguments
    $remoteFeatureSha = (Get-LastOutputLine -Output $featureShaResult.Output).Trim()

    if ($remoteFeatureSha -cne $ExpectedCommit.ToLowerInvariant()) {
        throw (
            'Remote feature head {0} не совпадает с ожидаемым commit {1}.' -f
            $remoteFeatureSha,
            $ExpectedCommit
        )
    }

    $stableRefArguments = @(
        '-C'
        $RepositoryPath
        'show-ref'
        '--verify'
        '--quiet'
        $remoteStableRef
    )
    $stableRefResult = Invoke-GitCommand -Arguments $stableRefArguments -AllowedExitCodes @(0, 1)

    if ($stableRefResult.ExitCode -ne 0) {
        throw ('Remote stable ref не найден: {0}' -f $remoteStableRef)
    }

    $ancestorArguments = @(
        '-C'
        $RepositoryPath
        'merge-base'
        '--is-ancestor'
        $remoteStableRef
        $ExpectedCommit
    )
    $ancestorResult = Invoke-GitCommand -Arguments $ancestorArguments -AllowedExitCodes @(0, 1)

    if ($ancestorResult.ExitCode -ne 0) {
        throw 'Текущий origin/personal/stable не является предком ожидаемого Stage 7 head. Smoke заблокирован.'
    }

    $originalCommitArguments = @(
        '-C'
        $RepositoryPath
        'rev-parse'
        'HEAD'
    )
    $originalCommitResult = Invoke-GitCommand -Arguments $originalCommitArguments
    $originalCommit = (Get-LastOutputLine -Output $originalCommitResult.Output).Trim()

    $originalBranchArguments = @(
        '-C'
        $RepositoryPath
        'symbolic-ref'
        '--quiet'
        '--short'
        'HEAD'
    )
    $originalBranchResult = Invoke-GitCommand -Arguments $originalBranchArguments -AllowedExitCodes @(0, 1)

    if ($originalBranchResult.ExitCode -eq 0) {
        $originalBranch = (Get-LastOutputLine -Output $originalBranchResult.Output).Trim()
    }

    if ($originalCommit -cne $ExpectedCommit.ToLowerInvariant()) {
        Write-SmokeMessage -Message ('Переключаю чистый checkout на Stage 7 head {0}...' -f $ExpectedCommit)
        $switchArguments = @(
            '-C'
            $RepositoryPath
            'switch'
            '--detach'
            $ExpectedCommit
        )
        [void](Invoke-GitCommand -Arguments $switchArguments)
        $switchedCheckout = $true
    }

    Test-CleanWorkingTree

    $headArguments = @(
        '-C'
        $RepositoryPath
        'rev-parse'
        'HEAD'
    )
    $headResult = Invoke-GitCommand -Arguments $headArguments
    $actualHead = (Get-LastOutputLine -Output $headResult.Output).Trim()

    if ($actualHead -cne $ExpectedCommit.ToLowerInvariant()) {
        throw 'Не удалось подтвердить точный Stage 7 head перед запуском Python.'
    }

    $pythonExecutable = Join-Path -Path $RepositoryPath -ChildPath '.venv\Scripts\python.exe'

    if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
        throw ('Python виртуального окружения не найден: {0}' -f $pythonExecutable)
    }

    $temporaryPythonFile = New-TemporaryFile
    $temporaryPythonPath = $temporaryPythonFile.FullName

    $pythonCode = @'
from __future__ import annotations

import sys
import traceback

repository_path = sys.argv[5]
if repository_path not in sys.path:
    sys.path.insert(0, repository_path)

from module.game_settings.enforcement import GameSettingsEnforcementScanner
from module.game_settings.model import is_unknown_game_setting_value
from module.game_settings.registry import (
    GAME_SETTINGS_OPTIONS_REGISTRY,
    GAME_SETTINGS_PRODUCTION_KEYS,
)
from module.ui.page import page_main, page_main_white


EXPECTED_KEYS = (
    "frame_rate",
    "opsi_reduce_tb_guidance",
    "opsi_auto_use_items",
    "opsi_default_auto_mode_threat_safe",
    "story_autoplay",
    "text_auto_scroll_speed",
    "enable_idle_screen",
    "duplicate_ship_display",
    "display_quick_switch_prompt",
    "display_battle_result_cutscene",
    "custom_ship_names",
)

DISPLAY_VALUES = {
    "on": "ON",
    "off": "OFF",
    "unknown": "UNKNOWN",
    "30_fps": "30 FPS",
    "60_fps": "60 FPS",
    "disabled": "Disabled",
    "enabled": "Enabled",
    "slow": "Slow",
    "normal": "Normal",
    "fast": "Fast",
    "very_fast": "Very Fast",
}


def display_value(value) -> str:
    if value is None:
        return "-"
    return DISPLAY_VALUES.get(value.value, value.value)


def print_audit(title: str, audit) -> tuple[list, list]:
    print(title)
    unknown = []
    mismatches = []
    for check in audit:
        required = check.required_value
        detected_text = display_value(check.detected_value)
        required_text = display_value(required)
        compatible = check.compatible
        print(
            f"- {check.key}: detected={detected_text} "
            f"required={required_text} compatible={compatible}"
        )
        if is_unknown_game_setting_value(check.detected_value):
            unknown.append(check)
        elif check.is_required and check.compatible is False:
            mismatches.append(check)
    return unknown, mismatches


def is_main(scanner) -> bool:
    return scanner.ui_current is page_main or scanner.ui_current is page_main_white


def ensure_main(scanner) -> bool:
    if is_main(scanner):
        return True
    try:
        scanner.return_to_main()
    except Exception as exc:
        print(f"Main cleanup error: {type(exc).__name__}: {exc}")
        return False
    return is_main(scanner)


def main() -> int:
    mode = sys.argv[1]
    config_name = sys.argv[2]
    device_serial = sys.argv[3]
    expected_commit = sys.argv[4]
    repository_path_value = sys.argv[5]
    scanner = None

    print("STAGE 7 GAME SETTINGS ENFORCEMENT SMOKE")
    print()
    print(f"Repository: {repository_path_value}")
    print(f"Commit: {expected_commit}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Device: {device_serial}")
    print(f"Mode: {mode}")

    try:
        if tuple(GAME_SETTINGS_PRODUCTION_KEYS) != EXPECTED_KEYS:
            raise RuntimeError(
                "Production registry identity differs from the accepted Stage 7 set"
            )
        if tuple(entry.key for entry in GAME_SETTINGS_OPTIONS_REGISTRY) != EXPECTED_KEYS:
            raise RuntimeError("Production registry order differs from Stage 7 contract")
        if any(entry.requirement is None for entry in GAME_SETTINGS_OPTIONS_REGISTRY):
            raise RuntimeError("Production registry contains a requirement-less entry")
        if any(not entry.enforce_supported for entry in GAME_SETTINGS_OPTIONS_REGISTRY):
            raise RuntimeError("Production registry contains a non-enforceable entry")

        print()
        print("Canonical requirements:")
        for entry in GAME_SETTINGS_OPTIONS_REGISTRY:
            print(
                f"- {entry.key}: {display_value(entry.requirement.expected_value)}"
            )

        scanner = GameSettingsEnforcementScanner(config_name, device=device_serial)
        scanner.device.screenshot()
        height, width = scanner.device.image.shape[:2]
        print(f"Resolution: {width}x{height}")

        if (width, height) != (1280, 720):
            raise RuntimeError("Unsupported resolution. Expected exactly 1280x720")

        print()
        initial = scanner.scan_game_settings()
        if tuple(check.key for check in initial) != EXPECTED_KEYS:
            raise RuntimeError("Initial audit returned an incomplete or reordered registry")

        unknown, mismatches = print_audit("Initial audit:", initial)
        print()
        print(f"Unknown count: {len(unknown)}")
        print(f"Mismatch count: {len(mismatches)}")

        if unknown:
            print("NO MUTATION: at least one required setting is UNKNOWN or missing")
            main_ok = ensure_main(scanner)
            print(f"Final page: {'Main' if main_ok else 'NOT MAIN'}")
            print("Exit: 1")
            return 1

        print()
        print("Planned changes:")
        if mismatches:
            for check in mismatches:
                print(
                    f"- {check.key}: {display_value(check.detected_value)} "
                    f"-> {display_value(check.required_value)}"
                )
        else:
            print("- none")

        if mode == "Audit":
            main_ok = ensure_main(scanner)
            print()
            print("Apply: NOT RUN (read-only Audit mode)")
            print("Verification: audit values are known; mismatches are allowed in Audit mode")
            print("Final audit: not required in read-only mode")
            print("Idempotency: not exercised in Audit mode")
            print(f"Final page: {'Main' if main_ok else 'NOT MAIN'}")
            exit_code = 0 if main_ok else 1
            print(f"Exit: {exit_code}")
            return exit_code

        if mode != "Apply":
            raise RuntimeError(f"Unsupported mode: {mode}")

        print()
        print("Apply:")
        enforcement = scanner.enforce_required_game_settings(reaudit_on_noop=True)
        print(f"- success={enforcement.success}")
        print(f"- blocked={enforcement.blocked}")
        print(f"- changed_count={len(enforcement.changes)}")
        if enforcement.failed_key is not None:
            print(f"- failed_key={enforcement.failed_key}")
        if enforcement.blocked_reason is not None:
            print(f"- blocked_reason={enforcement.blocked_reason}")
        if enforcement.failure_reason is not None:
            print(f"- failure_reason={enforcement.failure_reason}")

        print()
        print("Verification:")
        for change in enforcement.changes:
            print(
                f"- {change.key}: before={display_value(change.before)} "
                f"after={display_value(change.after)} verified={change.verified}"
            )

        changed_keys = tuple(change.key for change in enforcement.changes)
        if any(key not in EXPECTED_KEYS for key in changed_keys):
            raise RuntimeError("Enforcement reported a change outside the production registry")

        if not enforcement.success:
            main_ok = ensure_main(scanner)
            print("Final audit: unavailable because enforcement failed")
            print("Idempotency: not run after failed enforcement")
            print(f"Final page: {'Main' if main_ok else 'NOT MAIN'}")
            print("Exit: 1")
            return 1

        if enforcement.after is None:
            raise RuntimeError("Successful enforcement did not provide a final audit")

        final_unknown, final_mismatches = print_audit(
            "Final audit:",
            enforcement.after,
        )
        if final_unknown or final_mismatches:
            raise RuntimeError("Final audit is not fully compatible")
        if enforcement.after.all_required_compatible is not True:
            raise RuntimeError("Final audit aggregate is not fully compatible")

        print()
        print("Idempotency:")
        second = scanner.enforce_required_game_settings(reaudit_on_noop=True)
        print(f"changed_count_second_run={len(second.changes)}")
        if not second.success:
            raise RuntimeError("Second enforcement did not succeed")
        if second.changes:
            raise RuntimeError("Second enforcement was not a no-op")
        if second.after is None or second.after.all_required_compatible is not True:
            raise RuntimeError("Second enforcement did not confirm final compatibility")

        main_ok = ensure_main(scanner)
        print()
        print(f"Final page: {'Main' if main_ok else 'NOT MAIN'}")
        if not main_ok:
            print("Exit: 1")
            return 1

        print("Exit: 0")
        return 0
    except Exception as exc:
        print()
        print(f"SMOKE ERROR: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        main_ok = False
        if scanner is not None:
            main_ok = ensure_main(scanner)
        print(f"Final page: {'Main' if main_ok else 'NOT MAIN'}")
        print("Exit: 1")
        return 1


raise SystemExit(main())
'@

    Set-Content -LiteralPath $temporaryPythonPath -Value $pythonCode -Encoding utf8

    $modeValue = 'Audit'

    if ($Apply) {
        $modeValue = 'Apply'
        Write-SmokeMessage -Message 'ВНИМАНИЕ: этот запуск изменит только перечисленные Game Settings,'
        Write-SmokeMessage -Message 'которые сейчас не соответствуют canonical требованиям бота.'
    }

    $pythonArguments = @(
        $temporaryPythonPath
        $modeValue
        $ConfigName
        $DeviceSerial
        $ExpectedCommit.ToLowerInvariant()
        $RepositoryPath
    )

    Push-Location -LiteralPath $RepositoryPath

    try {
        & $pythonExecutable @pythonArguments
        $pythonExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    $finalExitCode = $pythonExitCode
}
catch {
    $failureMessage = $_.Exception.Message
    Write-SmokeMessage -Message ('STAGE 7 SMOKE INFRASTRUCTURE FAIL: {0}' -f $failureMessage)
    $finalExitCode = 99
}
finally {
    if ($switchedCheckout) {
        try {
            Test-CleanWorkingTree

            if (-not [string]::IsNullOrWhiteSpace($originalBranch)) {
                Write-SmokeMessage -Message ('Восстанавливаю исходную ветку {0}...' -f $originalBranch)
                $restoreArguments = @(
                    '-C'
                    $RepositoryPath
                    'switch'
                    $originalBranch
                )
                [void](Invoke-GitCommand -Arguments $restoreArguments)
            }
            else {
                Write-SmokeMessage -Message ('Восстанавливаю исходный detached commit {0}...' -f $originalCommit)
                $restoreArguments = @(
                    '-C'
                    $RepositoryPath
                    'switch'
                    '--detach'
                    $originalCommit
                )
                [void](Invoke-GitCommand -Arguments $restoreArguments)
            }

            $restoredHeadArguments = @(
                '-C'
                $RepositoryPath
                'rev-parse'
                'HEAD'
            )
            $restoredHeadResult = Invoke-GitCommand -Arguments $restoredHeadArguments
            $restoredHead = (Get-LastOutputLine -Output $restoredHeadResult.Output).Trim()

            if ($restoredHead -cne $originalCommit) {
                throw 'Исходный checkout не восстановлен после smoke.'
            }

            Test-CleanWorkingTree
        }
        catch {
            Write-SmokeMessage -Message ('Не удалось безопасно восстановить исходный checkout: {0}' -f $_.Exception.Message)
            $finalExitCode = 98
        }
    }

    if ($null -ne $temporaryPythonPath) {
        if (Test-Path -LiteralPath $temporaryPythonPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPythonPath -Force -ErrorAction SilentlyContinue
        }
    }
}

exit $finalExitCode
