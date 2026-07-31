#requires -Version 7.6

[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryPath = 'C:\AzurPilot'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$TemporaryBranch = 'chatgpt/stage3-integration'
$StableBranch = 'personal/stable'
$CheckpointCommit = 'd6cb4f686a8d68bf3fb07aea067403296f69d990'
$OriginalStableCommit = '9602b2dbc345a12da8365c8b2cbd90163740ad0b'
$RemoteName = 'origin'
$TemporaryRef = 'refs/heads/' + $TemporaryBranch
$StableRef = 'refs/heads/' + $StableBranch

if (-not (Test-Path -LiteralPath $RepositoryPath -PathType Container)) {
    throw ('Repository directory does not exist: ' + $RepositoryPath)
}

$Repo = Convert-Path -LiteralPath $RepositoryPath
$Git = (Get-Command git -CommandType Application -ErrorAction Stop).Path

$currentBranchOutput = & $Git -C $Repo branch --show-current 2>&1
$currentBranchExitCode = $LASTEXITCODE
if ($currentBranchExitCode -ne 0) {
    throw ('Unable to read current branch. Git exit code: ' + $currentBranchExitCode)
}
$currentBranch = ([string]($currentBranchOutput | Select-Object -Last 1)).Trim()

if ($currentBranch -ne $StableBranch) {
    throw ('Expected current branch ' + $StableBranch + ', got ' + $currentBranch)
}

$currentHeadOutput = & $Git -C $Repo rev-parse HEAD 2>&1
$currentHeadExitCode = $LASTEXITCODE
if ($currentHeadExitCode -ne 0) {
    throw ('Unable to read current HEAD. Git exit code: ' + $currentHeadExitCode)
}
$currentHead = ([string]($currentHeadOutput | Select-Object -Last 1)).Trim()

if ($currentHead -ne $CheckpointCommit) {
    throw ('Expected checkpoint HEAD ' + $CheckpointCommit + ', got ' + $currentHead)
}

$statusOutput = @(
    & $Git -C $Repo status --porcelain=v1 --untracked-files=all 2>&1
)
$statusExitCode = $LASTEXITCODE
if ($statusExitCode -ne 0) {
    throw ('Unable to inspect working tree. Git exit code: ' + $statusExitCode)
}
if ($statusOutput.Count -ne 0) {
    throw ('Working tree is not clean:' + [Environment]::NewLine + ($statusOutput -join [Environment]::NewLine))
}

$ancestorOutput = & $Git -C $Repo merge-base --is-ancestor $OriginalStableCommit $CheckpointCommit 2>&1
$ancestorExitCode = $LASTEXITCODE
if ($ancestorExitCode -eq 1) {
    throw 'Checkpoint is not a descendant of the expected stable commit.'
}
if ($ancestorExitCode -ne 0) {
    throw ('Unable to verify checkpoint ancestry. Git exit code: ' + $ancestorExitCode + [Environment]::NewLine + ($ancestorOutput -join [Environment]::NewLine))
}

$localTemporaryOutput = & $Git -C $Repo show-ref --verify --quiet $TemporaryRef 2>&1
$localTemporaryExitCode = $LASTEXITCODE

if ($localTemporaryExitCode -eq 1) {
    $createBranchOutput = & $Git -C $Repo branch $TemporaryBranch $CheckpointCommit 2>&1
    $createBranchExitCode = $LASTEXITCODE
    if ($createBranchExitCode -ne 0) {
        throw ('Unable to create local temporary branch. Git exit code: ' + $createBranchExitCode + [Environment]::NewLine + ($createBranchOutput -join [Environment]::NewLine))
    }
}
elseif ($localTemporaryExitCode -eq 0) {
    $localTemporaryCommitOutput = & $Git -C $Repo rev-parse $TemporaryRef 2>&1
    $localTemporaryCommitExitCode = $LASTEXITCODE
    if ($localTemporaryCommitExitCode -ne 0) {
        throw ('Unable to read local temporary branch. Git exit code: ' + $localTemporaryCommitExitCode)
    }
    $localTemporaryCommit = ([string]($localTemporaryCommitOutput | Select-Object -Last 1)).Trim()

    if ($localTemporaryCommit -eq $OriginalStableCommit) {
        $moveTemporaryOutput = & $Git -C $Repo update-ref $TemporaryRef $CheckpointCommit $OriginalStableCommit 2>&1
        $moveTemporaryExitCode = $LASTEXITCODE
        if ($moveTemporaryExitCode -ne 0) {
            throw ('Unable to move local temporary branch. Git exit code: ' + $moveTemporaryExitCode + [Environment]::NewLine + ($moveTemporaryOutput -join [Environment]::NewLine))
        }
    }
    elseif ($localTemporaryCommit -ne $CheckpointCommit) {
        throw ('Local temporary branch points to unexpected commit: ' + $localTemporaryCommit)
    }
}
else {
    throw ('Unable to inspect local temporary branch. Git exit code: ' + $localTemporaryExitCode + [Environment]::NewLine + ($localTemporaryOutput -join [Environment]::NewLine))
}

$remoteTemporaryOutput = @(
    & $Git -C $Repo ls-remote --heads $RemoteName $TemporaryRef 2>&1
)
$remoteTemporaryExitCode = $LASTEXITCODE
if ($remoteTemporaryExitCode -ne 0) {
    throw ('Unable to inspect remote temporary branch. Git exit code: ' + $remoteTemporaryExitCode + [Environment]::NewLine + ($remoteTemporaryOutput -join [Environment]::NewLine))
}

$remoteTemporaryCommit = $null
if ($remoteTemporaryOutput.Count -eq 1) {
    $remoteParts = $remoteTemporaryOutput[0] -split '\s+'
    $remoteTemporaryCommit = $remoteParts[0].Trim()
}
elseif ($remoteTemporaryOutput.Count -gt 1) {
    throw 'Remote temporary branch returned more than one ref.'
}

if (
    $null -ne $remoteTemporaryCommit -and
    $remoteTemporaryCommit -notin @(
        $OriginalStableCommit
        $CheckpointCommit
    )
) {
    throw ('Remote temporary branch points to unexpected commit: ' + $remoteTemporaryCommit)
}

if ($remoteTemporaryCommit -ne $CheckpointCommit) {
    $pushRefspec = $TemporaryRef + ':' + $TemporaryRef
    $pushOutput = & $Git -C $Repo push $RemoteName $pushRefspec 2>&1
    $pushExitCode = $LASTEXITCODE
    if ($pushExitCode -ne 0) {
        throw ('Unable to push checkpoint. Git exit code: ' + $pushExitCode + [Environment]::NewLine + ($pushOutput -join [Environment]::NewLine))
    }
}

$verifyRemoteOutput = @(
    & $Git -C $Repo ls-remote --heads $RemoteName $TemporaryRef 2>&1
)
$verifyRemoteExitCode = $LASTEXITCODE
if ($verifyRemoteExitCode -ne 0) {
    throw ('Unable to verify remote checkpoint. Git exit code: ' + $verifyRemoteExitCode)
}
if ($verifyRemoteOutput.Count -ne 1) {
    throw 'Remote temporary branch verification returned an unexpected number of refs.'
}
$verifyRemoteParts = $verifyRemoteOutput[0] -split '\s+'
$verifiedRemoteCommit = $verifyRemoteParts[0].Trim()

if ($verifiedRemoteCommit -ne $CheckpointCommit) {
    throw ('Remote checkpoint verification failed. Got: ' + $verifiedRemoteCommit)
}

$switchTemporaryOutput = & $Git -C $Repo switch $TemporaryBranch 2>&1
$switchTemporaryExitCode = $LASTEXITCODE
if ($switchTemporaryExitCode -ne 0) {
    throw ('Unable to switch to temporary branch. Git exit code: ' + $switchTemporaryExitCode + [Environment]::NewLine + ($switchTemporaryOutput -join [Environment]::NewLine))
}

$restoreStableOutput = & $Git -C $Repo update-ref $StableRef $OriginalStableCommit $CheckpointCommit 2>&1
$restoreStableExitCode = $LASTEXITCODE
if ($restoreStableExitCode -ne 0) {
    throw ('Unable to restore local stable ref. Git exit code: ' + $restoreStableExitCode + [Environment]::NewLine + ($restoreStableOutput -join [Environment]::NewLine))
}

$switchStableOutput = & $Git -C $Repo switch $StableBranch 2>&1
$switchStableExitCode = $LASTEXITCODE
if ($switchStableExitCode -ne 0) {
    throw ('Unable to switch back to stable branch. Git exit code: ' + $switchStableExitCode + [Environment]::NewLine + ($switchStableOutput -join [Environment]::NewLine))
}

$finalHeadOutput = & $Git -C $Repo rev-parse HEAD 2>&1
$finalHeadExitCode = $LASTEXITCODE
if ($finalHeadExitCode -ne 0) {
    throw ('Unable to verify final HEAD. Git exit code: ' + $finalHeadExitCode)
}
$finalHead = ([string]($finalHeadOutput | Select-Object -Last 1)).Trim()

if ($finalHead -ne $OriginalStableCommit) {
    throw ('Final stable HEAD is unexpected: ' + $finalHead)
}

$finalStatusOutput = @(
    & $Git -C $Repo status --porcelain=v1 --untracked-files=all 2>&1
)
$finalStatusExitCode = $LASTEXITCODE
if ($finalStatusExitCode -ne 0) {
    throw ('Unable to verify final working tree. Git exit code: ' + $finalStatusExitCode)
}
if ($finalStatusOutput.Count -ne 0) {
    throw ('Final working tree is not clean:' + [Environment]::NewLine + ($finalStatusOutput -join [Environment]::NewLine))
}

Write-Information -MessageData 'Checkpoint published successfully.' -InformationAction Continue
Write-Information -MessageData ('Remote temporary branch: ' + $TemporaryBranch + ' -> ' + $CheckpointCommit) -InformationAction Continue
Write-Information -MessageData ('Local stable branch: ' + $StableBranch + ' -> ' + $OriginalStableCommit) -InformationAction Continue
