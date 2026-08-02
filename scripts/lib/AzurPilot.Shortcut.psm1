Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

function Test-AzurPilotAdministrator {
    [CmdletBinding()]
    param()

    if (-not $IsWindows) {
        return $false
    }

    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)

    return $principal.IsInRole(
        [System.Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Get-AzurPilotShortcutSpecification {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$RepositoryPath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$PwshExecutablePath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$IconPath
    )

    $resolvedRepositoryPath = [System.IO.Path]::GetFullPath($RepositoryPath)
    $resolvedPwshPath = [System.IO.Path]::GetFullPath($PwshExecutablePath)
    $resolvedIconPath = [System.IO.Path]::GetFullPath($IconPath)
    $startScriptPath = Join-Path -Path $resolvedRepositoryPath -ChildPath 'scripts\Start-AzurPilot.ps1'

    if (-not (Test-Path -LiteralPath $resolvedRepositoryPath -PathType Container)) {
        throw ('Каталог AzurPilot не существует: {0}' -f $resolvedRepositoryPath)
    }

    if (-not (Test-Path -LiteralPath $resolvedPwshPath -PathType Leaf)) {
        throw ('Исполняемый файл PowerShell 7 не найден: {0}' -f $resolvedPwshPath)
    }

    if (-not (Test-Path -LiteralPath $startScriptPath -PathType Leaf)) {
        throw ('Start-AzurPilot.ps1 не найден: {0}' -f $startScriptPath)
    }

    if (-not (Test-Path -LiteralPath $resolvedIconPath -PathType Leaf)) {
        throw ('Значок проекта не найден: {0}' -f $resolvedIconPath)
    }

    $arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "{0}" -FromShortcut' -f $startScriptPath

    return [pscustomobject]@{
        TargetPath = $resolvedPwshPath
        Arguments = $arguments
        WorkingDirectory = $resolvedRepositoryPath
        IconLocation = '{0},0' -f $resolvedIconPath
        Description = 'Запуск AzurPilot через прозрачный модуль запуска этапа 2'
        StartScriptPath = $startScriptPath
        IconPath = $resolvedIconPath
    }
}

function Get-AzurPilotShortcutState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$ShortcutPath
    )

    $resolvedShortcutPath = [System.IO.Path]::GetFullPath($ShortcutPath)

    if (-not (Test-Path -LiteralPath $resolvedShortcutPath -PathType Leaf)) {
        return [pscustomobject]@{
            Exists = $false
            Path = $resolvedShortcutPath
            TargetPath = ''
            Arguments = ''
            WorkingDirectory = ''
            IconLocation = ''
            Description = ''
        }
    }

    $shell = $null
    $shortcut = $null

    try {
        $shell = New-Object -ComObject 'WScript.Shell'
        $shortcut = $shell.CreateShortcut($resolvedShortcutPath)

        return [pscustomobject]@{
            Exists = $true
            Path = $resolvedShortcutPath
            TargetPath = [string]$shortcut.TargetPath
            Arguments = [string]$shortcut.Arguments
            WorkingDirectory = [string]$shortcut.WorkingDirectory
            IconLocation = [string]$shortcut.IconLocation
            Description = [string]$shortcut.Description
        }
    }
    finally {
        if ($null -ne $shortcut) {
            [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
        }

        if ($null -ne $shell) {
            [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
        }
    }
}

function Test-AzurPilotShortcutState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$State,

        [Parameter(Mandatory)]
        [object]$Specification
    )

    if (-not [bool]$State.Exists) {
        return $false
    }

    return (
        [string]::Equals(
            [string]$State.TargetPath,
            [string]$Specification.TargetPath,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        [string]::Equals(
            [string]$State.Arguments,
            [string]$Specification.Arguments,
            [System.StringComparison]::Ordinal
        ) -and
        [string]::Equals(
            [string]$State.WorkingDirectory,
            [string]$Specification.WorkingDirectory,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        [string]::Equals(
            [string]$State.IconLocation,
            [string]$Specification.IconLocation,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        [string]::Equals(
            [string]$State.Description,
            [string]$Specification.Description,
            [System.StringComparison]::Ordinal
        )
    )
}

function Write-AzurPilotShortcutFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$ShortcutPath,

        [Parameter(Mandatory)]
        [object]$Specification
    )

    $shell = $null
    $shortcut = $null

    try {
        $shell = New-Object -ComObject 'WScript.Shell'
        $shortcut = $shell.CreateShortcut($ShortcutPath)
        $shortcut.TargetPath = [string]$Specification.TargetPath
        $shortcut.Arguments = [string]$Specification.Arguments
        $shortcut.WorkingDirectory = [string]$Specification.WorkingDirectory
        $shortcut.IconLocation = [string]$Specification.IconLocation
        $shortcut.Description = [string]$Specification.Description
        $shortcut.Save()
    }
    finally {
        if ($null -ne $shortcut) {
            [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
        }

        if ($null -ne $shell) {
            [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
        }
    }
}

function Copy-AzurPilotShortcutBackup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$ShortcutPath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$BackupRoot
    )

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $directoryName = '{0}-{1}' -f $timestamp, ([guid]::NewGuid().ToString('N').Substring(0, 8))
    $backupDirectory = Join-Path -Path $BackupRoot -ChildPath $directoryName

    New-Item -ItemType Directory -Path $backupDirectory -Force -ErrorAction Stop | Out-Null

    $backupPath = Join-Path -Path $backupDirectory -ChildPath 'AzurPilot.lnk'
    Copy-Item -LiteralPath $ShortcutPath -Destination $backupPath -Force -ErrorAction Stop

    $sourceHash = (
        Get-FileHash -LiteralPath $ShortcutPath -Algorithm SHA256 -ErrorAction Stop
    ).Hash
    $backupHash = (
        Get-FileHash -LiteralPath $backupPath -Algorithm SHA256 -ErrorAction Stop
    ).Hash

    if ($sourceHash -ne $backupHash) {
        throw 'Резервная копия ярлыка не совпадает с исходным файлом.'
    }

    return $backupPath
}

function Restore-AzurPilotShortcutBackup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$ShortcutPath,

        [Parameter()]
        [AllowEmptyString()]
        [string]$BackupPath = ''
    )

    if ([string]::IsNullOrWhiteSpace($BackupPath)) {
        if (Test-Path -LiteralPath $ShortcutPath) {
            Remove-Item -LiteralPath $ShortcutPath -Force -ErrorAction Stop
        }

        return
    }

    if (-not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) {
        throw ('Резервная копия ярлыка отсутствует: {0}' -f $BackupPath)
    }

    $restorePath = '{0}.restore-{1}.lnk' -f (
        [System.IO.Path]::Combine(
            [System.IO.Path]::GetDirectoryName($ShortcutPath),
            [System.IO.Path]::GetFileNameWithoutExtension($ShortcutPath)
        )
    ), ([guid]::NewGuid().ToString('N'))
    $replacedFilePath = '{0}.replaced-{1}.lnk' -f (
        [System.IO.Path]::Combine(
            [System.IO.Path]::GetDirectoryName($ShortcutPath),
            [System.IO.Path]::GetFileNameWithoutExtension($ShortcutPath)
        )
    ), ([guid]::NewGuid().ToString('N'))

    try {
        Copy-Item -LiteralPath $BackupPath -Destination $restorePath -Force -ErrorAction Stop

        if (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) {
            [System.IO.File]::Replace(
                $restorePath,
                $ShortcutPath,
                $replacedFilePath,
                $true
            )
        } else {
            [System.IO.File]::Move(
                $restorePath,
                $ShortcutPath
            )
        }
    }
    finally {
        if (Test-Path -LiteralPath $restorePath -PathType Leaf) {
            Remove-Item -LiteralPath $restorePath -Force -ErrorAction SilentlyContinue
        }

        if (Test-Path -LiteralPath $replacedFilePath -PathType Leaf) {
            Remove-Item -LiteralPath $replacedFilePath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Set-AzurPilotShortcut {
    [CmdletBinding(
        SupportsShouldProcess,
        ConfirmImpact = 'Medium'
    )]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$ShortcutPath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$RepositoryPath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$PwshExecutablePath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$IconPath,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$BackupRoot,

        [Parameter()]
        [switch]$RequireAdministrator,

        [Parameter()]
        [ValidateSet(
            'None',
            'AfterTemporaryShortcut',
            'AfterReplaceBeforeValidation'
        )]
        [string]$TestFailPoint = 'None'
    )

    $resolvedShortcutPath = [System.IO.Path]::GetFullPath($ShortcutPath)
    $resolvedBackupRoot = [System.IO.Path]::GetFullPath($BackupRoot)
    $shortcutParent = [System.IO.Path]::GetDirectoryName($resolvedShortcutPath)

    if ([string]::IsNullOrWhiteSpace($shortcutParent)) {
        throw ('Не удалось определить родительский каталог ярлыка: {0}' -f $resolvedShortcutPath)
    }

    if ($RequireAdministrator -and -not (Test-AzurPilotAdministrator)) {
        $exception = [System.UnauthorizedAccessException]::new(
            'Для изменения общего ярлыка требуется явно запущенный PowerShell 7 от имени администратора.'
        )
        $exception.Data['AzurPilotShortcutReason'] = 'ElevationRequired'
        throw $exception
    }

    New-Item -ItemType Directory -Path $shortcutParent -Force -ErrorAction Stop | Out-Null
    New-Item -ItemType Directory -Path $resolvedBackupRoot -Force -ErrorAction Stop | Out-Null

    $specificationParameters = @{
        RepositoryPath = $RepositoryPath
        PwshExecutablePath = $PwshExecutablePath
        IconPath = $IconPath
    }
    $specification = Get-AzurPilotShortcutSpecification @specificationParameters
    $existingState = Get-AzurPilotShortcutState -ShortcutPath $resolvedShortcutPath

    if (Test-AzurPilotShortcutState -State $existingState -Specification $specification) {
        return [pscustomobject]@{
            Changed = $false
            BackupPath = ''
            State = $existingState
        }
    }

    if (-not $PSCmdlet.ShouldProcess($resolvedShortcutPath, 'Create or replace AzurPilot shortcut')) {
        return [pscustomobject]@{
            Changed = $false
            BackupPath = ''
            State = $existingState
        }
    }

    $backupPath = ''
    $temporaryPath = Join-Path -Path $shortcutParent -ChildPath (
        'AzurPilot.stage2-{0}.lnk' -f ([guid]::NewGuid().ToString('N'))
    )
    $replacedFilePath = Join-Path -Path $shortcutParent -ChildPath (
        'AzurPilot.replaced-{0}.lnk' -f ([guid]::NewGuid().ToString('N'))
    )
    $replacementCompleted = $false

    try {
        if ([bool]$existingState.Exists) {
            $backupParameters = @{
                ShortcutPath = $resolvedShortcutPath
                BackupRoot = $resolvedBackupRoot
            }
            $backupPath = Copy-AzurPilotShortcutBackup @backupParameters
        }

        $temporaryShortcutParameters = @{
            ShortcutPath = $temporaryPath
            Specification = $specification
        }
        Write-AzurPilotShortcutFile @temporaryShortcutParameters

        $temporaryState = Get-AzurPilotShortcutState -ShortcutPath $temporaryPath

        if (-not (Test-AzurPilotShortcutState -State $temporaryState -Specification $specification)) {
            throw 'Временный ярлык не прошёл проверку.'
        }

        if ($TestFailPoint -eq 'AfterTemporaryShortcut') {
            throw 'TEST FAILPOINT: AfterTemporaryShortcut'
        }

        if ([bool]$existingState.Exists) {
            [System.IO.File]::Replace(
                $temporaryPath,
                $resolvedShortcutPath,
                $replacedFilePath,
                $true
            )
        } else {
            [System.IO.File]::Move(
                $temporaryPath,
                $resolvedShortcutPath
            )
        }

        $replacementCompleted = $true

        if ($TestFailPoint -eq 'AfterReplaceBeforeValidation') {
            throw 'TEST FAILPOINT: AfterReplaceBeforeValidation'
        }

        $createdState = Get-AzurPilotShortcutState -ShortcutPath $resolvedShortcutPath

        if (-not (Test-AzurPilotShortcutState -State $createdState -Specification $specification)) {
            throw 'Итоговый ярлык не прошёл проверку.'
        }

        return [pscustomobject]@{
            Changed = $true
            BackupPath = $backupPath
            State = $createdState
        }
    }
    catch {
        $originalException = $_.Exception

        try {
            if ($replacementCompleted) {
                $restoreParameters = @{
                    ShortcutPath = $resolvedShortcutPath
                    BackupPath = $backupPath
                }
                Restore-AzurPilotShortcutBackup @restoreParameters
            }
        }
        catch {
            $rollbackMessage = (
                'Перенос ярлыка завершился ошибкой, а откат также не удался. ' +
                'Migration error: {0}. Rollback error: {1}'
            ) -f $originalException.Message, $_.Exception.Message

            throw [System.InvalidOperationException]::new(
                $rollbackMessage,
                $originalException
            )
        }

        throw [System.InvalidOperationException]::new(
            ('Перенос ярлыка завершился ошибкой. Исходный ярлык восстановлен: {0}' -f $originalException.Message),
            $originalException
        )
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }

        if (Test-Path -LiteralPath $replacedFilePath -PathType Leaf) {
            Remove-Item -LiteralPath $replacedFilePath -Force -ErrorAction SilentlyContinue
        }
    }
}

Export-ModuleMember -Function @(
    'Get-AzurPilotShortcutSpecification'
    'Get-AzurPilotShortcutState'
    'Set-AzurPilotShortcut'
    'Test-AzurPilotAdministrator'
    'Test-AzurPilotShortcutState'
)
